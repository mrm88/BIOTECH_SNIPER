"""Alpaca order-state subscriber + ``execution_events`` writer (f-m3-11).

This module provides the canonical entry points for recording every
state change on a paper-trading order to the ``execution_events``
SQLite table:

* :func:`record_execution_event` — low-level writer; inserts a single
  row given an explicit ``paper_order_id`` / ``event_type`` / ``event_at``.
  Validates the monotonic-state-transition invariant at insert time
  and raises :class:`IllegalStateTransition` when a caller tries to
  rewrite history with a non-canonical sequence.
* :class:`ExecutionSubscriber` — poll-based subscriber that walks the
  open ``paper_orders`` rows, fetches their current state from Alpaca,
  and records any change (delta vs. the last persisted event). The
  poll loop is intentionally simple — it does NOT manage its own
  background thread; callers (cron jobs, the audit script, integration
  tests) drive the loop explicitly via :meth:`poll_once`.

The validation contract assertions exercised here:

* **VAL-M3-058** — ``execution_events`` schema is asserted at write
  time: bad ``event_type`` values are blocked by the SQLite CHECK
  constraint and surface as :class:`sqlite3.IntegrityError` (not
  silently dropped).
* **VAL-M3-062** — every successfully-submitted ``paper_orders`` row
  has exactly one ``execution_events`` row with
  ``event_type='submitted'`` (written by :class:`PaperExecutor` via
  this module's :func:`record_execution_event`).
* **VAL-M3-064** — order state transitions are monotonic and
  complete; the runtime validator below enforces the legal-sequence
  set on every insert.
* **VAL-M3-069** — write-then-submit invariant: the
  ``paper_orders.created_at`` for a given ``client_order_id`` is
  asserted to be ``<=`` the ``event_at`` of the corresponding
  ``submitted`` ``execution_events`` row at insert time.

Legal state-transition graph
----------------------------
::

    submitted ─► accepted ──► partial_fill* ─► filled
                          ─► canceled
                          ─► expired
    submitted ─► rejected

The graph is encoded in :data:`LEGAL_TRANSITIONS` as a mapping from
``previous_event_type`` (or :data:`_INITIAL_STATE` for the very first
event on an order) to the set of allowed next states. Any transition
not in this mapping raises :class:`IllegalStateTransition`.
"""

from __future__ import annotations

import datetime as _dt
import json as _json
import logging as _logging
import sqlite3 as _sqlite3
from pathlib import Path as _Path
from typing import Any, Mapping, Optional

from biotech_sniper import db as _db_module
from biotech_sniper import logging_setup as _logging_setup


__all__ = [
    "EVENT_TYPES",
    "TERMINAL_EVENT_TYPES",
    "LEGAL_TRANSITIONS",
    "IllegalStateTransition",
    "WriteThenSubmitViolation",
    "record_execution_event",
    "validate_state_transition",
    "ExecutionSubscriber",
    "main",
]

# f-m4-11: do NOT eagerly auto-configure the root logger from module
# import. Entrypoints (the daily orchestrator, the intraday runner,
# and :func:`main` below) own the ``logging_setup.configure`` call so
# the destination log file (``daily.log`` / ``intraday.log`` /
# ``watchdog.log``) is correct. Using a plain :func:`logging.getLogger`
# here means importing ``execution_subscriber`` (e.g. from a unit
# test or another helper) does not lock the destination to ``app.log``
# before the entrypoint can install the JSON formatter.
logger = _logging.getLogger(__name__)


#: All ``event_type`` values the SQLite CHECK constraint accepts.
#: Mirrors :data:`biotech_sniper.db.schema.sql` exactly.
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "submitted",
        "accepted",
        "partial_fill",
        "filled",
        "canceled",
        "expired",
        "rejected",
    }
)

#: Terminal states — once observed, no further transitions are legal
#: for the same ``paper_order_id``.
TERMINAL_EVENT_TYPES: frozenset[str] = frozenset(
    {"filled", "canceled", "expired", "rejected"}
)

#: Sentinel for "no prior event has been recorded yet". Used as the
#: key in :data:`LEGAL_TRANSITIONS` for the very first event on an
#: order.
_INITIAL_STATE: str = "__initial__"

#: Legal next-state mapping. ``LEGAL_TRANSITIONS[prev]`` is the set
#: of event types allowed to follow ``prev``. The very first event
#: on an order is constrained against ``LEGAL_TRANSITIONS[_INITIAL_STATE]``.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    _INITIAL_STATE: frozenset({"submitted"}),
    "submitted": frozenset({"accepted", "rejected"}),
    "accepted": frozenset({"partial_fill", "filled", "canceled", "expired"}),
    "partial_fill": frozenset({"partial_fill", "filled", "canceled", "expired"}),
    # Terminal states — no further transitions allowed.
    "filled": frozenset(),
    "canceled": frozenset(),
    "expired": frozenset(),
    "rejected": frozenset(),
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class IllegalStateTransition(ValueError):
    """Raised when a caller tries to record an out-of-order event.

    The exception's ``args[0]`` includes the ``paper_order_id``, the
    last persisted event_type, and the rejected next event_type so
    the audit trail in the operator log unambiguously names the
    violation.
    """


class WriteThenSubmitViolation(ValueError):
    """Raised when a ``submitted`` event is written before its row.

    The f-m3-11 invariant (VAL-M3-069) is that the ``paper_orders``
    row exists locally with ``created_at <= event_at`` BEFORE the
    corresponding ``execution_events`` ``submitted`` row is written.
    Out-of-order writes signal a buggy executor — we surface the
    violation rather than silently letting the event row land.
    """


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with ms precision.

    Matches the ``strftime('%Y-%m-%dT%H:%M:%fZ', 'now')`` SQLite
    default so timestamps written from Python and SQL stay
    format-compatible.
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    return (
        now.strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{now.microsecond // 1000:03d}Z"
    )


def _coerce_event_at(value: Any) -> str:
    """Normalise a ``event_at`` argument to an ISO-8601 string.

    Accepts:
    * ``None`` → falls back to :func:`_utc_now_iso`.
    * :class:`datetime.datetime` (with or without tzinfo) → ISO-8601
      with ``Z`` suffix when naive.
    * ``str`` → returned as-is (caller's responsibility to keep
      ISO-8601).
    """
    if value is None:
        return _utc_now_iso()
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        return value.astimezone(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S."
        ) + f"{value.microsecond // 1000:03d}Z"
    return str(value)


# ---------------------------------------------------------------------------
# State-transition validator
# ---------------------------------------------------------------------------


def validate_state_transition(
    previous_event_type: Optional[str],
    next_event_type: str,
) -> None:
    """Raise :class:`IllegalStateTransition` if the transition is not legal.

    ``previous_event_type=None`` means "no prior event recorded";
    constrained against :data:`LEGAL_TRANSITIONS[_INITIAL_STATE]`,
    which by design contains only ``'submitted'``. Any other
    first-event attempt is rejected.

    Public so tests can assert the validator directly.
    """
    if next_event_type not in EVENT_TYPES:
        raise IllegalStateTransition(
            f"unknown event_type {next_event_type!r}; allowed: "
            f"{sorted(EVENT_TYPES)}"
        )
    key = previous_event_type if previous_event_type is not None else _INITIAL_STATE
    allowed = LEGAL_TRANSITIONS.get(key)
    if allowed is None:
        raise IllegalStateTransition(
            f"unknown previous event_type {previous_event_type!r}; "
            f"cannot validate transition to {next_event_type!r}"
        )
    if next_event_type not in allowed:
        raise IllegalStateTransition(
            f"illegal transition {previous_event_type!r} -> "
            f"{next_event_type!r}; allowed next states from "
            f"{previous_event_type!r}: {sorted(allowed)}"
        )


def _last_event_type_for(
    conn: _sqlite3.Connection, paper_order_id: str
) -> Optional[str]:
    """Return the most recent ``event_type`` recorded for ``paper_order_id``.

    Sorted by ``event_at`` then by ``id`` to break ties so the
    ordering matches the validation contract's
    ``ORDER BY paper_order_id, event_at`` query.

    Returns ``None`` when no events exist yet for the order.
    """
    row = conn.execute(
        """
        SELECT event_type
        FROM execution_events
        WHERE paper_order_id = ?
        ORDER BY event_at DESC, id DESC
        LIMIT 1
        """,
        (paper_order_id,),
    ).fetchone()
    if row is None:
        return None
    return row[0] if not isinstance(row, _sqlite3.Row) else row["event_type"]


def _paper_orders_created_at(
    conn: _sqlite3.Connection, paper_order_id: str
) -> Optional[str]:
    """Return the ``paper_orders.created_at`` for ``paper_order_id`` or ``None``.

    Used to enforce the write-then-submit invariant on the very
    first ``submitted`` event for an order.
    """
    row = conn.execute(
        "SELECT created_at FROM paper_orders WHERE id = ?",
        (paper_order_id,),
    ).fetchone()
    if row is None:
        return None
    return row[0] if not isinstance(row, _sqlite3.Row) else row["created_at"]


# ---------------------------------------------------------------------------
# Low-level writer
# ---------------------------------------------------------------------------


def record_execution_event(
    db_path: _Path | str,
    *,
    paper_order_id: str,
    event_type: str,
    event_at: Any = None,
    raw_payload: Optional[Any] = None,
    enforce_monotonic: bool = True,
) -> int:
    """Insert one row into ``execution_events`` and return the new id.

    Parameters
    ----------
    db_path:
        SQLite database path. Opened via
        :func:`biotech_sniper.db.connect` so PRAGMAs are correct and
        the schema is migrated lazily.
    paper_order_id:
        FK target on ``paper_orders.id``.
    event_type:
        One of :data:`EVENT_TYPES`. Anything else raises
        :class:`IllegalStateTransition` BEFORE the SQLite INSERT
        (which would otherwise raise IntegrityError from the CHECK
        constraint, with a less helpful message).
    event_at:
        ISO-8601 timestamp string. ``None`` (the default) uses
        :func:`_utc_now_iso`. ``datetime`` instances are normalised
        to UTC.
    raw_payload:
        Optional broker payload (dict / json-serialisable). Stored
        as ``json.dumps(...)`` text so post-hoc forensics can
        replay the event without a separate log scrape.
    enforce_monotonic:
        When ``True`` (default), the validator runs and rejects
        non-canonical transitions. Set to ``False`` ONLY for
        recovery / backfill paths that need to re-insert legacy
        events out of order; tests should NEVER pass ``False``.

    Returns
    -------
    int
        The auto-generated ``execution_events.id`` of the new row.

    Raises
    ------
    IllegalStateTransition
        Either the ``event_type`` is unknown OR the transition from
        the last-persisted event for this ``paper_order_id`` is not
        in :data:`LEGAL_TRANSITIONS`.
    WriteThenSubmitViolation
        ``event_type='submitted'`` was requested but no
        ``paper_orders`` row exists yet for ``paper_order_id``, OR
        the row's ``created_at`` is later than ``event_at``.
    """
    if event_type not in EVENT_TYPES:
        raise IllegalStateTransition(
            f"unknown event_type {event_type!r}; allowed: "
            f"{sorted(EVENT_TYPES)}"
        )

    event_at_iso = _coerce_event_at(event_at)

    # Serialise payload before opening the connection so any error
    # surfaces immediately and we don't lock the DB while doing JSON
    # work.
    if raw_payload is None:
        payload_text: Optional[str] = None
    elif isinstance(raw_payload, (str, bytes)):
        payload_text = (
            raw_payload.decode("utf-8")
            if isinstance(raw_payload, bytes)
            else raw_payload
        )
    else:
        try:
            payload_text = _json.dumps(raw_payload, default=str, sort_keys=True)
        except (TypeError, ValueError):
            payload_text = repr(raw_payload)

    conn = _db_module.connect(db_path)
    try:
        # Lazy migration so callers don't need to remember.
        _db_module.run_migrations(conn)

        if enforce_monotonic:
            previous = _last_event_type_for(conn, paper_order_id)
            validate_state_transition(previous, event_type)

            # f-m3-11 write-then-submit invariant (VAL-M3-069): the
            # very first ``submitted`` event must reference an
            # already-persisted ``paper_orders`` row whose
            # ``created_at`` is no later than the event timestamp.
            if event_type == "submitted":
                row_created_at = _paper_orders_created_at(
                    conn, paper_order_id
                )
                if row_created_at is None:
                    raise WriteThenSubmitViolation(
                        f"submitted event recorded for paper_order_id="
                        f"{paper_order_id!r} but no paper_orders row "
                        "exists yet (write-then-submit violation)"
                    )
                if row_created_at > event_at_iso:
                    raise WriteThenSubmitViolation(
                        f"paper_orders.created_at={row_created_at!r} > "
                        f"execution_events.event_at={event_at_iso!r} for "
                        f"paper_order_id={paper_order_id!r} — submitted "
                        "event recorded BEFORE its row (write-then-submit "
                        "violation)"
                    )

        cursor = conn.execute(
            """
            INSERT INTO execution_events (
                paper_order_id, event_type, event_at, raw_payload
            ) VALUES (?, ?, ?, ?)
            """,
            (paper_order_id, event_type, event_at_iso, payload_text),
        )
        conn.commit()
        new_id = int(cursor.lastrowid or 0)
    finally:
        conn.close()

    # f-m4-11: keep the legacy f-string format for the internal
    # ``event_recorded`` DEBUG line — once :func:`logging_setup.configure`
    # has run the JSONFormatter wraps this in a JSON object with the
    # formatted message in ``event`` automatically. Existing tests
    # (test_execution_events.py) grep for ``paper_order_id=<id>`` in
    # the formatted message text, so we preserve that contract.
    logger.debug(
        "execution_subscriber.event_recorded paper_order_id=%s "
        "event_type=%s event_at=%s id=%s",
        paper_order_id,
        event_type,
        event_at_iso,
        new_id,
    )
    return new_id


# ---------------------------------------------------------------------------
# Poll-based subscriber
# ---------------------------------------------------------------------------


#: Mapping from Alpaca's broker-side ``status`` strings to the
#: ``execution_events.event_type`` enum. Alpaca emits a richer set
#: of statuses than the f-m3-11 telemetry table tracks; non-mapped
#: statuses are dropped (logged at DEBUG) so the validator never
#: sees a row for a status that has no canonical telemetry slot.
_BROKER_STATUS_TO_EVENT_TYPE: dict[str, str] = {
    "new": "submitted",
    "submitted": "submitted",
    "accepted": "accepted",
    "pending_new": "submitted",
    "pending_submit": "submitted",
    "partially_filled": "partial_fill",
    "partial_fill": "partial_fill",
    "filled": "filled",
    "canceled": "canceled",
    "cancelled": "canceled",
    "expired": "expired",
    "rejected": "rejected",
    "done_for_day": "expired",
}


class ExecutionSubscriber:
    """Poll the broker for order-state updates and record events.

    The subscriber is the canonical bridge between :class:`AlpacaClient`
    and the ``execution_events`` table. Production wiring (M4 cron):

    1. ``master_unified_run`` finishes its order submission loop.
    2. The watchdog timer (or the daily run) instantiates an
       ``ExecutionSubscriber`` against the same ``data/alpha_sniper.db``
       and the Alpaca paper client.
    3. :meth:`poll_once` walks every ``paper_orders`` row whose
       latest ``execution_events`` event is non-terminal, fetches
       the current broker state, and inserts a new row whenever the
       state has changed.

    Tests inject a duck-typed ``client`` (cassette-backed) so the
    subscriber's logic is exercised without a real network call.
    """

    def __init__(
        self,
        client: Any,
        *,
        db_path: _Path | str,
    ) -> None:
        self.client = client
        self.db_path = _Path(db_path)

    # ------------------------------------------------------------------
    # Read helpers (test-friendly)
    # ------------------------------------------------------------------

    def _open_orders(
        self,
        conn: _sqlite3.Connection,
        *,
        date: Optional[str] = None,
    ) -> list[Mapping[str, Any]]:
        """Return ``paper_orders`` rows that have not reached a terminal state.

        An order is "open" when:

        * its latest ``execution_events.event_type`` is not in
          :data:`TERMINAL_EVENT_TYPES`, OR
        * it has no ``execution_events`` rows at all (which means
          the executor wrote the row but the subscriber has not
          picked up the broker-side ``submitted`` event yet — the
          subscriber emits the missing event on the next poll).

        When ``date`` is provided (ISO-8601 ``YYYY-MM-DD``), the
        query is additionally narrowed to ``paper_orders`` whose
        ``created_at`` falls on that calendar date. f-m3-19 adds
        this filter so the production CLI can scope its poll to the
        current trading day rather than walking every historical
        open order on every cron tick.
        """
        params: list[Any] = []
        sql = (
            "SELECT po.id, po.alpaca_order_id, po.status, po.qty, "
            "po.side, po.requested_mid_at_submit, "
            "COALESCE("
            "  (SELECT ee.event_type FROM execution_events ee "
            "   WHERE ee.paper_order_id = po.id "
            "   ORDER BY ee.event_at DESC, ee.id DESC LIMIT 1), "
            "  NULL"
            ") AS last_event_type "
            "FROM paper_orders po "
            "WHERE po.alpaca_order_id IS NOT NULL "
            "  AND po.alpaca_order_id != '' "
        )
        if date:
            sql += "  AND substr(po.created_at, 1, 10) = ? "
            params.append(date)
        rows = conn.execute(sql, params).fetchall()
        out: list[Mapping[str, Any]] = []
        for row in rows:
            if isinstance(row, _sqlite3.Row):
                last = row["last_event_type"]
                d = dict(row)
            else:
                last = row[6]
                d = {
                    "id": row[0],
                    "alpaca_order_id": row[1],
                    "status": row[2],
                    "qty": row[3],
                    "side": row[4],
                    "requested_mid_at_submit": row[5],
                    "last_event_type": row[6],
                }
            if last is not None and last in TERMINAL_EVENT_TYPES:
                continue
            out.append(d)
        return out

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def poll_once(self, *, date: Optional[str] = None) -> int:
        """Walk every open order and record any state change.

        Returns the number of ``execution_events`` rows inserted by
        this poll iteration so callers (cron metrics, tests) can
        assert progress.

        The method is idempotent in the steady state: orders whose
        broker status maps to the SAME ``event_type`` as the last
        persisted event are skipped (no duplicate row).

        f-m3-19 contract: when the broker reports ``partial_fill``
        or ``filled``, the writer ALSO records the matching
        ``execution_fills`` row (slippage / time-to-fill telemetry).
        For every other transition only ``execution_events`` is
        written — the fills table is reserved for actual fills, not
        intermediate state changes.

        Parameters
        ----------
        date:
            Optional ISO-8601 ``YYYY-MM-DD`` string. When supplied
            the open-orders query is narrowed to that calendar date
            (matched against ``paper_orders.created_at``). Used by
            the production CLI in :func:`main` so cron jobs only
            poll today's orders.
        """
        conn = _db_module.connect(self.db_path)
        try:
            _db_module.run_migrations(conn)
            open_orders = self._open_orders(conn, date=date)
        finally:
            conn.close()

        recorded = 0
        for row in open_orders:
            paper_order_id = row["id"]
            alpaca_order_id = row["alpaca_order_id"]
            last_event_type = row.get("last_event_type")
            parent_qty = row.get("qty")
            try:
                broker = self.client.get_order(alpaca_order_id)
            except Exception:
                logger.exception(
                    "execution_subscriber_get_order_failed",
                    extra={
                        "event": "execution_subscriber_get_order_failed",
                        "paper_order_id": str(paper_order_id),
                        "alpaca_order_id": str(alpaca_order_id),
                    },
                )
                continue

            broker_status = (broker.get("status") or "").strip().lower()
            event_type = _BROKER_STATUS_TO_EVENT_TYPE.get(broker_status)
            if event_type is None:
                logger.debug(
                    "execution_subscriber_unmapped_status",
                    extra={
                        "event": "execution_subscriber_unmapped_status",
                        "paper_order_id": str(paper_order_id),
                        "broker_status": str(broker_status),
                    },
                )
                continue

            # Skip duplicate events; partial_fill on partial_fill is
            # explicitly allowed (the legal-transition graph permits
            # repeated partial_fill rows since each partial fill is
            # a distinct broker event).
            if event_type == last_event_type and event_type != "partial_fill":
                continue

            # Resolve the broker-emitted timestamp.
            event_at = (
                broker.get("filled_at")
                or broker.get("updated_at")
                or broker.get("submitted_at")
                or _utc_now_iso()
            )

            try:
                if event_type in ("partial_fill", "filled"):
                    # f-m3-19 fix #1: a fill event must produce BOTH
                    # an ``execution_fills`` row (slippage /
                    # time-to-fill telemetry) AND the matching
                    # ``execution_events`` row. ``record_fill``
                    # writes the fills row and emits the events row
                    # via ``record_execution_event``. We swallow
                    # context-missing errors (no parent mid stored)
                    # by falling back to the events-only path so a
                    # legacy paper_orders row from before f-m3-11
                    # still produces a lifecycle event.
                    filled_price = broker.get("filled_avg_price")
                    filled_qty = broker.get("filled_qty")
                    if (
                        filled_price is None
                        or filled_qty in (None, 0, "0")
                    ):
                        # Broker reports a fill status without
                        # populated fill metrics — fall back to the
                        # events-only writer so the lifecycle row
                        # still lands. The next poll will pick up
                        # the populated payload.
                        record_execution_event(
                            self.db_path,
                            paper_order_id=paper_order_id,
                            event_type=event_type,
                            event_at=event_at,
                            raw_payload=broker,
                        )
                    else:
                        try:
                            try:
                                filled_qty_int = int(float(filled_qty))
                            except (TypeError, ValueError):
                                filled_qty_int = 0
                            qty_int: int = (
                                int(parent_qty) if parent_qty is not None else 0
                            )
                            partial_qty_remaining = (
                                0
                                if event_type == "filled"
                                else max(0, qty_int - filled_qty_int)
                            )
                            from biotech_sniper.execution_fills import (
                                record_fill as _record_fill,
                                FillContextMissing as _FillContextMissing,
                            )
                            _record_fill(
                                self.db_path,
                                paper_order_id=paper_order_id,
                                filled_price=float(filled_price),
                                filled_qty=filled_qty_int,
                                filled_at=event_at,
                                partial_qty_remaining=partial_qty_remaining,
                                raw_payload=broker,
                            )
                        except _FillContextMissing:
                            # No requested_mid_at_submit / side on
                            # the parent row — fall back to writing
                            # ONLY the events row so the lifecycle
                            # stream is still complete. The
                            # validator will surface the missing
                            # fills row downstream.
                            logger.warning(
                                "execution_subscriber_fill_context_missing",
                                extra={
                                    "event": (
                                        "execution_subscriber_fill_context_missing"
                                    ),
                                    "paper_order_id": str(paper_order_id),
                                    "fallback": "event_only",
                                },
                            )
                            record_execution_event(
                                self.db_path,
                                paper_order_id=paper_order_id,
                                event_type=event_type,
                                event_at=event_at,
                                raw_payload=broker,
                            )
                else:
                    record_execution_event(
                        self.db_path,
                        paper_order_id=paper_order_id,
                        event_type=event_type,
                        event_at=event_at,
                        raw_payload=broker,
                    )
                recorded += 1
            except IllegalStateTransition:
                # The runtime validator caught a non-canonical
                # transition. Re-raise so the test suite (and the
                # operator) sees the violation immediately rather
                # than silently dropping the event.
                logger.error(
                    "execution_subscriber_illegal_transition",
                    extra={
                        "event": "execution_subscriber_illegal_transition",
                        "paper_order_id": str(paper_order_id),
                        "last": str(last_event_type),
                        "next": str(event_type),
                    },
                )
                raise
        return recorded


# ---------------------------------------------------------------------------
# CLI entry point (f-m3-19)
# ---------------------------------------------------------------------------


def _default_db_path() -> _Path:
    """Resolve the project's canonical SQLite db path lazily.

    Imported at call-time so unit tests don't pay the
    ``biotech_sniper.paths`` import cost just by importing the
    module.
    """
    from biotech_sniper.paths import DATA_DIR

    return DATA_DIR / "alpha_sniper.db"


def main(
    argv: Optional[list[str]] = None,
    *,
    client: Any = None,
    db_path: Optional[_Path | str] = None,
) -> int:
    """CLI for the execution subscriber.

    Production wiring (f-m3-19): ``python -m
    biotech_sniper.execution_subscriber --poll-once --date <today>``
    runs a single :meth:`ExecutionSubscriber.poll_once` against
    every open ``paper_orders`` row created on ``<today>`` and
    exits.

    The function returns an integer exit code so callers (cron, the
    intraday scanner Job 6) can short-circuit on error. Non-zero
    means at least one broker call raised AND the broker error was
    logged at ERROR; the orders table is unchanged.

    Test hook: tests inject a stub Alpaca client via the ``client``
    keyword (not exposed on the command line) so the poll loop can
    exercise hermetic broker payloads. Likewise ``db_path`` overrides
    the default ``data/alpha_sniper.db`` for test isolation.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.execution_subscriber",
        description=(
            "Poll-once telemetry subscriber for paper_orders. Records "
            "execution_events / execution_fills rows for every "
            "broker-side state change observed since the last poll."
        ),
    )
    parser.add_argument(
        "--poll-once",
        action="store_true",
        help="Run a single poll iteration and exit.",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help=(
            "Restrict the poll to paper_orders rows whose "
            "created_at falls on this YYYY-MM-DD calendar date. "
            "Defaults to today (UTC) when --poll-once is set and "
            "--date is omitted."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Skip broker contact entirely. Logs the count of open "
            "orders that WOULD be polled and exits 0. Used by the "
            "intraday scanner's smoke check before live broker "
            "wiring is enabled."
        ),
    )
    args = parser.parse_args(argv)

    # f-m4-11: every CLI invocation routes through ``logging_setup`` so
    # the JSONFormatter is attached even when the module is run via
    # ``python -m biotech_sniper.execution_subscriber`` (i.e. NOT from
    # the daily/intraday entrypoints, which do their own configure).
    # ``configure`` is idempotent — if a parent process already set
    # ``log_name='intraday'`` (or any other name) the call is a no-op
    # and the existing destination is preserved.
    _logging_setup.configure(log_name="intraday")

    target_db = _Path(db_path) if db_path is not None else _default_db_path()
    target_db.parent.mkdir(parents=True, exist_ok=True)

    target_date: Optional[str] = args.date
    if (args.poll_once or args.dry_run) and target_date is None:
        target_date = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")

    if args.dry_run:
        # Defence-in-depth: dry-run NEVER instantiates an Alpaca
        # client, so missing credentials or a network outage cannot
        # break the smoke command.
        conn = _db_module.connect(target_db)
        try:
            _db_module.run_migrations(conn)
            stub = ExecutionSubscriber(client=None, db_path=target_db)
            open_orders = stub._open_orders(conn, date=target_date)
        finally:
            conn.close()
        logger.info(
            "execution_subscriber_dry_run",
            extra={
                "event": "execution_subscriber_dry_run",
                "db_path": str(target_db),
                "date": str(target_date),
                "open_orders": int(len(open_orders)),
            },
        )
        return 0

    if not args.poll_once:
        parser.error("one of --poll-once or --dry-run is required")
        return 2  # pragma: no cover — argparse exits

    if client is None:
        # Lazily build the Alpaca paper client. Failures here are
        # logged but turned into a non-zero exit so cron sees the
        # misconfig (missing creds, wrong base url, etc.) instead
        # of silently skipping the poll.
        try:
            from biotech_sniper.alpaca_client import AlpacaClient

            client = AlpacaClient()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "execution_subscriber_alpaca_client_unavailable",
                extra={
                    "event": "execution_subscriber_alpaca_client_unavailable",
                    "error": repr(exc),
                },
            )
            return 1

    subscriber = ExecutionSubscriber(client, db_path=target_db)
    try:
        recorded = subscriber.poll_once(date=target_date)
    except Exception:  # noqa: BLE001
        logger.exception(
            "execution_subscriber_poll_failed",
            extra={
                "event": "execution_subscriber_poll_failed",
                "db_path": str(target_db),
                "date": str(target_date),
            },
        )
        return 1

    logger.info(
        "execution_subscriber_poll_complete",
        extra={
            "event": "execution_subscriber_poll_complete",
            "db_path": str(target_db),
            "date": str(target_date),
            "recorded": int(recorded),
        },
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI smoke
    import sys as _sys

    _sys.exit(main())
