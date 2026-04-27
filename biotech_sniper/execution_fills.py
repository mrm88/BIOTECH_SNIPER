"""Side-aware fill recorder + slippage computation (f-m3-11).

This module provides the canonical entry points for recording fill
events to the ``execution_fills`` SQLite table:

* :func:`compute_slippage_bps` — pure helper. Given the filled price,
  the submit-time mid, and the order side, returns the side-aware
  slippage in basis points (positive = worse-than-mid):

  * **buy**: positive when filled ABOVE mid (paid more than expected).
  * **sell**: positive when filled BELOW mid (received less than
    expected).

  ::

      raw_bps   = ((filled_price - mid) / mid) * 10000
      slippage_bps = +raw_bps if side == 'buy' else -raw_bps

* :func:`compute_slippage_usd` — dollar-denominated slippage:
  ``(filled_price - mid) * filled_qty * 100`` for buys (negative when
  paid more than mid is the *cost* dollar amount; we keep sign
  consistent with bps so callers can sum slippage across mixed sides
  without book-keeping).

* :func:`record_fill` — high-level writer. Computes slippage from the
  parent ``paper_orders`` row (loads ``requested_mid_at_submit`` and
  ``side``), inserts an ``execution_fills`` row, and returns its id.
  Also writes the matching ``execution_events`` row (``filled`` or
  ``partial_fill`` per ``partial_qty_remaining``) via
  :func:`biotech_sniper.execution_subscriber.record_execution_event`
  so the lifecycle stream stays consistent.

* :func:`record_fill_event` — lower-level helper used by the
  subscriber: writes ONLY the ``execution_fills`` row, no
  ``execution_events``. Suitable for tests that drive both writers
  explicitly.

Validation contract assertions exercised
----------------------------------------
* **VAL-M3-059** — schema asserted by the migration; the helper
  inserts required-NOT-NULL columns explicitly.
* **VAL-M3-063** — every ``filled`` / ``partial_fill`` paper_orders
  row has at least one ``execution_fills`` row with non-NULL
  ``slippage_bps`` matching the side-aware re-computation within
  1e-6.
"""

from __future__ import annotations

import datetime as _dt
import logging as _logging
import sqlite3 as _sqlite3
from pathlib import Path as _Path
from typing import Any, Optional

from biotech_sniper import db as _db_module


__all__ = [
    "compute_slippage_bps",
    "compute_slippage_usd",
    "compute_time_to_fill_ms",
    "record_fill",
    "record_fill_event",
    "FillContextMissing",
]

logger = _logging.getLogger(__name__)


class FillContextMissing(ValueError):
    """Raised when ``record_fill`` cannot locate the parent paper_orders row.

    The fill writer needs ``requested_mid_at_submit`` (for slippage)
    and ``side`` (for orientation) from the parent row. A missing
    row means the executor never wrote the order — which is itself
    a write-then-submit violation. We surface a typed error rather
    than silently writing a fill row with NULL slippage.
    """


# ---------------------------------------------------------------------------
# Pure compute helpers
# ---------------------------------------------------------------------------


def _normalise_side(side: Any) -> str:
    """Return ``'buy'`` or ``'sell'`` from a free-form side value.

    Accepts ``OrderSide`` enums (``.value`` access) and plain
    strings ``'buy'``/``'sell'`` (case-insensitive). Anything else
    raises :class:`ValueError`.
    """
    if hasattr(side, "value"):
        side = side.value
    text = str(side).strip().lower()
    if text in {"buy", "sell"}:
        return text
    raise ValueError(
        f"side must be 'buy' or 'sell', got {side!r}; cannot orient slippage"
    )


def compute_slippage_bps(
    filled_price: float,
    requested_mid_at_submit: float,
    side: Any,
) -> float:
    """Return the side-aware slippage in basis points.

    Formula::

        raw_bps = ((filled_price - mid) / mid) * 10000
        bps     = +raw_bps if side == 'buy' else -raw_bps

    Sign convention: positive bps = worse-than-mid for the actor.
    A buy filled ABOVE mid pays MORE than expected → positive bps.
    A sell filled BELOW mid receives LESS than expected → positive
    bps. Validators (VAL-M3-063) recompute this and assert equality
    within 1e-6 of the stored value.

    Raises
    ------
    ValueError
        If ``requested_mid_at_submit`` is non-positive (would divide
        by zero) or if ``side`` is not ``'buy'``/``'sell'``.
    """
    s = _normalise_side(side)
    mid = float(requested_mid_at_submit)
    if mid <= 0:
        raise ValueError(
            f"requested_mid_at_submit must be > 0 to compute slippage, "
            f"got {mid!r}"
        )
    raw_bps = ((float(filled_price) - mid) / mid) * 10000.0
    return raw_bps if s == "buy" else -raw_bps


def compute_slippage_usd(
    filled_price: float,
    requested_mid_at_submit: float,
    filled_qty: int,
    side: Any,
) -> float:
    """Return the side-aware slippage in dollars.

    Mirrors :func:`compute_slippage_bps` but in raw dollars per the
    contract's standard 100-multiplier (each option contract
    represents 100 underlying shares). Formula::

        raw_usd = (filled_price - mid) * filled_qty * 100
        usd     = +raw_usd if side == 'buy' else -raw_usd

    Sign convention matches bps: positive = worse-than-mid.
    """
    s = _normalise_side(side)
    diff = (float(filled_price) - float(requested_mid_at_submit))
    raw_usd = diff * float(filled_qty) * 100.0
    return raw_usd if s == "buy" else -raw_usd


def compute_time_to_fill_ms(
    submitted_at: Any,
    filled_at: Any,
) -> int:
    """Return the elapsed milliseconds between submission and fill.

    Both arguments may be ISO-8601 strings or :class:`datetime`
    instances. Naive datetimes are assumed UTC. Negative deltas
    (which can happen when broker clocks drift) are clamped to
    ``0`` so downstream consumers never see a negative latency.
    """
    submitted = _to_aware(submitted_at)
    filled = _to_aware(filled_at)
    delta = filled - submitted
    ms = int(delta.total_seconds() * 1000)
    return max(0, ms)


def _to_aware(value: Any) -> _dt.datetime:
    """Coerce a string / datetime to an aware (UTC) datetime."""
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt_ = _dt.datetime.fromisoformat(s)
        except ValueError:
            raise ValueError(f"unparseable timestamp: {value!r}")
        return dt_ if dt_.tzinfo else dt_.replace(tzinfo=_dt.timezone.utc)
    raise TypeError(
        f"timestamp must be str or datetime, got {type(value).__name__}"
    )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _load_paper_order_context(
    conn: _sqlite3.Connection,
    paper_order_id: str,
) -> tuple[Optional[float], Optional[str], Optional[str], Optional[int]]:
    """Return ``(requested_mid_at_submit, side, created_at, qty)`` or all-None.

    Used by :func:`record_fill` to source the fields it needs from
    the parent ``paper_orders`` row. Centralised so the SQL stays
    in one place and tests that probe the helper get the same
    behaviour as the writer.
    """
    row = conn.execute(
        """
        SELECT requested_mid_at_submit, side, created_at, qty
        FROM paper_orders
        WHERE id = ?
        """,
        (paper_order_id,),
    ).fetchone()
    if row is None:
        return (None, None, None, None)
    if isinstance(row, _sqlite3.Row):
        return (
            row["requested_mid_at_submit"],
            row["side"],
            row["created_at"],
            row["qty"],
        )
    return tuple(row)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def record_fill_event(
    db_path: _Path | str,
    *,
    paper_order_id: str,
    filled_at: Any,
    filled_price: float,
    filled_qty: int,
    requested_mid_at_submit: float,
    slippage_bps: float,
    slippage_usd: float,
    time_to_fill_ms: int,
    partial_qty_remaining: int,
) -> int:
    """Insert one row into ``execution_fills`` and return the new id.

    Lower-level than :func:`record_fill`: the caller supplies every
    metric directly. Used by the subscriber's poll loop after it
    has already computed slippage from the broker payload, and by
    tests that want to drive the writer in isolation.
    """
    conn = _db_module.connect(db_path)
    try:
        _db_module.run_migrations(conn)
        cursor = conn.execute(
            """
            INSERT INTO execution_fills (
                paper_order_id, filled_at, filled_price, filled_qty,
                requested_mid_at_submit, slippage_bps, slippage_usd,
                time_to_fill_ms, partial_qty_remaining
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                paper_order_id,
                _coerce_iso(filled_at),
                float(filled_price),
                int(filled_qty),
                float(requested_mid_at_submit),
                float(slippage_bps),
                float(slippage_usd),
                int(time_to_fill_ms),
                int(partial_qty_remaining),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def record_fill(
    db_path: _Path | str,
    *,
    paper_order_id: str,
    filled_price: float,
    filled_qty: int,
    filled_at: Any = None,
    partial_qty_remaining: int = 0,
    raw_payload: Optional[Any] = None,
    write_event: bool = True,
) -> int:
    """High-level fill recorder.

    Loads the parent ``paper_orders`` row to source
    ``requested_mid_at_submit`` and ``side``, computes side-aware
    slippage (bps + usd), measures ``time_to_fill_ms`` against the
    parent row's ``created_at``, and inserts the
    ``execution_fills`` row.

    When ``write_event=True`` (default), also writes the matching
    ``execution_events`` row (``'filled'`` if
    ``partial_qty_remaining == 0`` else ``'partial_fill'``). The
    event row is written via
    :func:`biotech_sniper.execution_subscriber.record_execution_event`
    so the monotonic-state validator runs over the lifecycle.

    Raises
    ------
    FillContextMissing
        If the parent ``paper_orders`` row is missing OR has no
        ``requested_mid_at_submit`` to compute slippage against.
    ValueError
        Forwarded from :func:`compute_slippage_bps` if the side or
        mid is invalid.
    """
    conn = _db_module.connect(db_path)
    try:
        _db_module.run_migrations(conn)
        mid, side, created_at, _qty = _load_paper_order_context(
            conn, paper_order_id
        )
    finally:
        conn.close()

    if mid is None:
        raise FillContextMissing(
            f"paper_orders row for paper_order_id={paper_order_id!r} "
            f"is missing or has NULL requested_mid_at_submit; cannot "
            "compute slippage"
        )
    if side is None:
        raise FillContextMissing(
            f"paper_orders row for paper_order_id={paper_order_id!r} "
            "has NULL side; cannot orient slippage"
        )

    fill_at_iso = _coerce_iso(filled_at) if filled_at is not None else _utc_now_iso()
    submitted_at = created_at or fill_at_iso

    slippage_bps_value = compute_slippage_bps(
        float(filled_price), float(mid), side
    )
    slippage_usd_value = compute_slippage_usd(
        float(filled_price), float(mid), int(filled_qty), side
    )
    ttf_ms = compute_time_to_fill_ms(submitted_at, fill_at_iso)

    fill_id = record_fill_event(
        db_path,
        paper_order_id=paper_order_id,
        filled_at=fill_at_iso,
        filled_price=float(filled_price),
        filled_qty=int(filled_qty),
        requested_mid_at_submit=float(mid),
        slippage_bps=slippage_bps_value,
        slippage_usd=slippage_usd_value,
        time_to_fill_ms=ttf_ms,
        partial_qty_remaining=int(partial_qty_remaining),
    )

    if write_event:
        # Imported lazily to avoid a circular dependency: the
        # subscriber module does NOT import this one, so we are
        # only one-way coupled.
        from biotech_sniper.execution_subscriber import (
            record_execution_event,
        )

        event_type = "filled" if int(partial_qty_remaining) == 0 else "partial_fill"
        try:
            record_execution_event(
                db_path,
                paper_order_id=paper_order_id,
                event_type=event_type,
                event_at=fill_at_iso,
                raw_payload=raw_payload,
            )
        except Exception:
            logger.exception(
                "execution_fills.event_record_failed paper_order_id=%s "
                "event_type=%s",
                paper_order_id,
                event_type,
            )
            # Don't lose the fill row over an event-side failure;
            # the validator will catch the missing event downstream
            # but the slippage data is preserved.

    logger.info(
        "execution_fills.record paper_order_id=%s qty=%d price=%.4f "
        "slippage_bps=%.4f side=%s ttf_ms=%d partial_qty_remaining=%d",
        paper_order_id,
        int(filled_qty),
        float(filled_price),
        slippage_bps_value,
        side,
        ttf_ms,
        int(partial_qty_remaining),
    )
    return fill_id


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    now = _dt.datetime.now(_dt.timezone.utc)
    return (
        now.strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{now.microsecond // 1000:03d}Z"
    )


def _coerce_iso(value: Any) -> str:
    """Normalise a timestamp argument to an ISO-8601 string."""
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        v = value.astimezone(_dt.timezone.utc)
        return (
            v.strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{v.microsecond // 1000:03d}Z"
        )
    return str(value)
