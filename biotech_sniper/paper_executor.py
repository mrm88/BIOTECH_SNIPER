"""Paper-trading order executor (M3 feature f-m3-03).

This module exposes :class:`PaperExecutor`, the **only** entry point
for submitting options orders against Alpaca's paper-trading sandbox
in the biotech_sniper project. Production code MUST construct an
``AlpacaClient`` (which itself is paper-only by default) and pass it
to :class:`PaperExecutor`. The executor then:

* asserts the wrapped client targets the paper URL exactly — any
  other URL raises :class:`PaperOnlyViolation` regardless of the
  underlying ``LIVE_MODE`` toggle (defence-in-depth around the
  ``AlpacaClient`` guardrail);
* validates that every play card describes a single-leg long call
  or put — multi-leg / spread shapes raise
  :class:`UnsupportedOrderShape` BEFORE any network call;
* submits the order via :meth:`AlpacaClient.submit_order` and
  persists a row to the ``orders`` SQLite table on every code path
  (submitted → accepted → filled, OR rejected with the broker's
  error message stored in ``reason``);
* exposes :meth:`get_order` (status query passthrough),
  :meth:`wait_for_fill` (bounded poll loop for the
  ``accepted → filled`` transition validated by VAL-M3-016), and
  :meth:`get_orders_for_play` (lookup by ``play_card_id``).

Validation contract assertions fulfilled
----------------------------------------

* **VAL-M3-014** — single-leg long call submission roundtrip; returned
  ``order_id`` queryable via :meth:`AlpacaClient.get_order`.
* **VAL-M3-015** — single-leg long put submission roundtrip; returned
  order's ``side='buy'``, ``qty>=1``, ``order_class='simple'``.
* **VAL-M3-016** — order status walks ``accepted → filled`` within a
  bounded poll loop (default 30 s).
* **VAL-M3-017** — multi-leg play cards raise
  :class:`UnsupportedOrderShape` and submit no order.
* **VAL-M3-018** — failed submission raises :class:`OrderRejected`,
  persists a row with ``status='rejected'`` + ``reason='<broker
  msg>'``, and never persists a row with ``status='submitted'`` for
  the failed attempt.

Sizing logic (``$250`` cap, mid-price computation, concurrency / capital
caps) is intentionally **not** implemented in this module — it lands
in feature ``f-m3-04`` (``size_position``). Likewise, full execution
telemetry / liquidity probing lands in ``f-m3-11`` and ``f-m3-12``.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from biotech_sniper import db as db_module
from biotech_sniper.alpaca_client import (
    AlpacaClient,
    AlpacaClientError,
    PAPER_BASE_URL,
)
from biotech_sniper.paths import DATA_DIR


__all__ = [
    "PaperExecutor",
    "PaperExecutorError",
    "PaperOnlyViolation",
    "UnsupportedOrderShape",
    "OrderRejected",
    "DEFAULT_DB_PATH",
    "TERMINAL_STATUSES",
    "FILLED_STATUSES",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default SQLite database path used when callers do not inject one. The
#: f-m3-03 feature persists into the same db that the M2 migration
#: created (``data/alpha_sniper.db`` under the resolved
#: :data:`biotech_sniper.paths.DATA_DIR`).
DEFAULT_DB_PATH: Path = DATA_DIR / "alpha_sniper.db"


#: Order statuses that the Alpaca paper sandbox treats as terminal —
#: the poll loop in :meth:`PaperExecutor.wait_for_fill` exits as soon
#: as it observes any of these. ``'rejected'`` is included so a
#: post-submission rejection (rare but possible) breaks the loop
#: cleanly instead of polling for the full timeout.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"filled", "canceled", "expired", "rejected", "done_for_day"}
)

#: Subset of :data:`TERMINAL_STATUSES` that represent a successful
#: fill. Used by :meth:`PaperExecutor.wait_for_fill` callers to
#: distinguish a genuine fill from a cancel / expiry.
FILLED_STATUSES: frozenset[str] = frozenset({"filled"})


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class PaperExecutorError(Exception):
    """Base class for every error raised by :class:`PaperExecutor`."""


class PaperOnlyViolation(PaperExecutorError):
    """Raised when constructing against a non-paper Alpaca client.

    Surfaces with a message naming the offending ``base_url`` so
    operators can immediately see which side of the guardrail
    tripped. Defence-in-depth around the ``AlpacaClient``
    LIVE_MODE check — the executor refuses to operate against any
    URL other than :data:`biotech_sniper.alpaca_client.PAPER_BASE_URL`
    even when the client itself was constructed against the live URL
    with both LIVE_MODE gates open.
    """


class UnsupportedOrderShape(PaperExecutorError):
    """Raised when a play card describes an order shape we don't support.

    Currently only single-leg long call/put orders are accepted (the
    M3 validation contract scope). Multi-leg, spread, or
    bracket-class orders raise this exception BEFORE any network call
    so the validator's "no order submitted" assertion holds.
    """


class OrderRejected(PaperExecutorError):
    """Raised when Alpaca rejects the order at submission time.

    The exception's ``args[0]`` is a human-readable summary that
    includes the broker reason. Callers receive a persisted row in
    the ``orders`` table with ``status='rejected'`` and the broker
    reason already written to ``reason`` — no follow-up bookkeeping
    is required.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with ms.

    Mirrors the ``strftime('%Y-%m-%dT%H:%M:%fZ', 'now')`` default the
    SQLite schema uses for ``created_at`` columns so timestamps
    written from Python and from the DB stay format-compatible.
    """
    import datetime as _dt

    return (
        _dt.datetime.now(_dt.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{_dt.datetime.now(_dt.timezone.utc).microsecond // 1000:03d}Z"
    )


def _coerce_side(value: Any) -> OrderSide:
    """Map a play-card ``side`` string to an :class:`OrderSide`.

    Accepts ``"buy"`` / ``"sell"`` (case-insensitive). Anything else
    raises :class:`UnsupportedOrderShape` because the M3 contract is
    explicit that this executor only places long entries; sells go
    through the dedicated exit triggers in ``f-m3-05`` /
    ``f-m3-09``. We do NOT silently coerce unknown values — that
    would mask a calibration bug at the play-card builder.
    """
    if isinstance(value, OrderSide):
        return value
    text = str(value).strip().lower()
    if text == "buy":
        return OrderSide.BUY
    if text == "sell":
        return OrderSide.SELL
    raise UnsupportedOrderShape(
        f"unrecognised side {value!r}; expected 'buy' or 'sell'"
    )


def _coerce_tif(value: Any) -> TimeInForce:
    """Map a ``time_in_force`` string to an :class:`TimeInForce` enum.

    Defaults to ``TimeInForce.DAY`` (the most common single-leg
    option order TIF). ``TimeInForce.GTC`` is also accepted for
    completeness with the alpaca-py SDK.
    """
    if value is None:
        return TimeInForce.DAY
    if isinstance(value, TimeInForce):
        return value
    text = str(value).strip().lower()
    mapping = {
        "day": TimeInForce.DAY,
        "gtc": TimeInForce.GTC,
        "ioc": TimeInForce.IOC,
        "fok": TimeInForce.FOK,
    }
    if text in mapping:
        return mapping[text]
    raise UnsupportedOrderShape(
        f"unrecognised time_in_force {value!r}; expected one of {sorted(mapping)}"
    )


def _validate_single_leg(play_card: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the single leg from ``play_card['option_legs']`` or raise.

    The validator runs BEFORE any network call so a rejected play
    card never produces a half-submitted order on Alpaca's side. We
    enforce the contract from the f-m3-03 description and VAL-M3-017:

    * ``play_card['option_legs']`` MUST be a list with exactly one
      entry. Length 0 or > 1 raises :class:`UnsupportedOrderShape`.
    * ``play_card['order_class']``, when present, MUST equal
      ``'simple'``. ``'mleg'`` / ``'bracket'`` etc. raise.
    * The single leg MUST have a non-empty ``symbol`` (the OCC
      option symbol the broker recognises) and a positive integer
      ``qty``. Missing / invalid values raise.
    * The leg's ``option_type``, when present, MUST be ``'call'`` or
      ``'put'``. Anything else raises (defence against a future
      writer adding an unsupported strategy).
    """

    legs = play_card.get("option_legs")
    if not isinstance(legs, Sequence) or isinstance(legs, (str, bytes)):
        raise UnsupportedOrderShape(
            "play_card['option_legs'] must be a list of leg dicts; "
            f"got {type(legs).__name__}"
        )
    if len(legs) == 0:
        raise UnsupportedOrderShape(
            "play_card['option_legs'] is empty; need exactly one leg"
        )
    if len(legs) > 1:
        raise UnsupportedOrderShape(
            f"multi-leg orders are not supported by PaperExecutor "
            f"(got {len(legs)} legs); only single-leg long calls/puts are "
            "accepted at the f-m3-03 milestone"
        )

    order_class = play_card.get("order_class")
    if order_class is not None and str(order_class).strip().lower() != "simple":
        raise UnsupportedOrderShape(
            f"play_card['order_class']={order_class!r} unsupported; "
            "only 'simple' (single-leg) orders are accepted"
        )

    leg = legs[0]
    if not isinstance(leg, Mapping):
        raise UnsupportedOrderShape(
            f"option_legs[0] must be a dict, got {type(leg).__name__}"
        )

    symbol = leg.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise UnsupportedOrderShape(
            "option_legs[0]['symbol'] must be a non-empty OCC option symbol"
        )

    qty_raw = leg.get("qty")
    try:
        qty = int(qty_raw)
    except (TypeError, ValueError):
        raise UnsupportedOrderShape(
            f"option_legs[0]['qty'] must be a positive integer, got {qty_raw!r}"
        )
    if qty < 1:
        raise UnsupportedOrderShape(
            f"option_legs[0]['qty'] must be >= 1, got {qty}"
        )

    option_type = leg.get("option_type")
    if option_type is not None:
        normalised = str(option_type).strip().lower()
        if normalised not in {"call", "put"}:
            raise UnsupportedOrderShape(
                f"option_legs[0]['option_type']={option_type!r} unsupported; "
                "only 'call' and 'put' are accepted at f-m3-03"
            )

    return leg


def _broker_reason(exc: BaseException) -> str:
    """Extract a short human-readable reason from a broker exception.

    Walks the cause chain so the reason includes both the executor's
    classification (e.g. ``AlpacaClientError``) and the underlying
    broker message ("invalid symbol", "insufficient buying power").
    """
    parts: list[str] = []
    cur: BaseException | None = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        msg = str(cur).strip()
        if msg:
            parts.append(msg)
        cur = cur.__cause__ or cur.__context__
    if not parts:
        parts.append(type(exc).__name__)
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# PaperExecutor
# ---------------------------------------------------------------------------


class PaperExecutor:
    """Submit, track, and persist Alpaca paper-trading options orders.

    Parameters
    ----------
    client:
        An :class:`AlpacaClient` instance whose ``base_url`` MUST equal
        :data:`PAPER_BASE_URL` exactly. Any other URL raises
        :class:`PaperOnlyViolation` from the constructor (no network
        call attempted).
    db_path:
        Optional path to the SQLite database file. ``None`` (the
        default) uses :data:`DEFAULT_DB_PATH` so production code does
        not need to know the path. Tests inject a ``tmp_path``-derived
        location to keep state isolated.
    poll_interval_seconds:
        Default poll interval used by :meth:`wait_for_fill`. Tests
        override this to ``0`` so the poll loop runs synchronously
        through whatever sequence of statuses the cassette dictates.
    """

    def __init__(
        self,
        client: AlpacaClient,
        *,
        db_path: Optional[Path] = None,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        if client is None:
            raise TypeError("client must be an AlpacaClient instance")

        base_url = getattr(client, "base_url", None)
        if base_url != PAPER_BASE_URL:
            raise PaperOnlyViolation(
                "PaperExecutor refuses to operate against a non-paper "
                f"Alpaca client (base_url={base_url!r}). Expected "
                f"{PAPER_BASE_URL!r}. The paper-only guardrail is "
                "enforced regardless of LIVE_MODE; only construct "
                "PaperExecutor with an AlpacaClient that targets the "
                "paper sandbox."
            )

        self.client = client
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        self.poll_interval_seconds = poll_interval_seconds

        # Lazily create the orders table on construction so callers do
        # not need to remember to run migrations themselves. This keeps
        # the test ergonomics simple — pytest builds a tmp_path db,
        # constructs the executor, and the table is ready.
        self._ensure_orders_table()

    # ------------------------------------------------------------------
    # Internal: schema bootstrap + persistence helpers.
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open a SQLite connection with the project's PRAGMAs set."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return db_module.connect(self.db_path)

    def _ensure_orders_table(self) -> None:
        """Idempotently create the ``orders`` table + supporting indices.

        The table is part of ``schema.sql`` (added by f-m3-03), but
        unit tests sometimes point :class:`PaperExecutor` at a
        freshly-created database that has never seen
        :func:`run_migrations`. Running the migration on construction
        is cheap (a sequence of ``CREATE ... IF NOT EXISTS``
        statements) and guarantees the persistence helpers below can
        always find the table.
        """
        conn = self._connect()
        try:
            db_module.run_migrations(conn)
        finally:
            conn.close()

    def _persist_order_row(
        self,
        *,
        order_id: str,
        play_card_id: Optional[str],
        alpaca_order_id: Optional[str],
        symbol: Optional[str],
        side: Optional[str],
        qty: Optional[int],
        status: str,
        reason: Optional[str],
        event: Optional[str],
        parent_play_card_id: Optional[str],
    ) -> None:
        """Insert (or replace) a row in the ``orders`` table.

        ``id`` is the executor-generated UUID4; ``alpaca_order_id`` is
        the broker-assigned id (NULL on rejection paths). The replace
        semantics on the PK keep the helper idempotent: if a caller
        retries with the same internal ``id`` (rare; only happens if
        the executor is re-invoked after a partial crash) the row is
        overwritten rather than duplicated.
        """
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO orders (
                    id, play_card_id, alpaca_order_id, symbol, side,
                    qty, status, reason, event, parent_play_card_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    play_card_id,
                    alpaca_order_id,
                    symbol,
                    side,
                    qty,
                    status,
                    reason,
                    event,
                    parent_play_card_id,
                    _utc_now_iso(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self, play_card: Mapping[str, Any]) -> str:
        """Submit a single-leg long call/put order to Alpaca paper.

        Parameters
        ----------
        play_card:
            Mapping describing the order. Required keys:

            * ``option_legs`` — list of length exactly 1 containing a
              dict with ``symbol`` (OCC option symbol), ``qty`` (int >= 1),
              and ``side`` (``'buy'`` or ``'sell'``). Optional keys
              honoured by this method: ``option_type`` (``'call'``/``'put'``,
              validated for shape only), ``limit_price`` (float — when
              present the order is submitted as a limit order; when
              absent a market order is used), ``time_in_force``
              (``'day'``/``'gtc'``/...; defaults to ``'day'``).
            * ``play_card_id`` — string identifier persisted into the
              ``orders`` table for downstream lookup (e.g.
              :meth:`get_orders_for_play`). Optional but strongly
              encouraged; when missing, the persisted row stores
              ``play_card_id=NULL``.

            Optional keys: ``client_order_id`` (string) propagated to
            Alpaca for idempotent retries; ``parent_play_card_id``
            (string) carried through to the persisted row for exit
            orders.

        Returns
        -------
        str
            The broker-assigned ``alpaca_order_id``. The caller can
            pass this directly to :meth:`get_order` or
            :meth:`wait_for_fill`.

        Raises
        ------
        UnsupportedOrderShape
            If the play card describes a multi-leg / spread order or
            is missing required fields. No network call is made.
        OrderRejected
            If Alpaca returns a 4xx / rejects the order. A row with
            ``status='rejected'`` and ``reason=<broker msg>`` is
            persisted before the exception is raised; no
            ``status='submitted'`` row is left behind.
        PaperOnlyViolation
            (Re-checked on every call as defence-in-depth.) If the
            wrapped client's ``base_url`` ever drifts away from the
            paper sandbox, the executor refuses to forward the
            order.
        """
        # Defence-in-depth: re-check the paper-only guardrail in case
        # the caller swapped ``client.base_url`` after construction.
        if getattr(self.client, "base_url", None) != PAPER_BASE_URL:
            raise PaperOnlyViolation(
                "PaperExecutor.client.base_url drifted away from "
                f"{PAPER_BASE_URL!r}; refusing to submit order."
            )

        leg = _validate_single_leg(play_card)

        play_card_id = play_card.get("play_card_id")
        parent_play_card_id = play_card.get("parent_play_card_id")
        client_order_id = play_card.get("client_order_id") or leg.get(
            "client_order_id"
        )
        event = play_card.get("event")

        symbol = str(leg["symbol"])
        qty = int(leg["qty"])
        side = _coerce_side(leg.get("side", "buy"))
        tif = _coerce_tif(leg.get("time_in_force"))
        limit_price = leg.get("limit_price")

        # Build the alpaca-py request model. We deliberately use a
        # typed request rather than a dict so the SDK's pydantic
        # validation runs before we hit the network — surfacing
        # malformed values as ``ValueError`` rather than as a 422
        # from the broker.
        order_request: Any
        if limit_price is not None:
            order_request = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                type=OrderType.LIMIT,
                time_in_force=tif,
                limit_price=float(limit_price),
                client_order_id=client_order_id,
            )
        else:
            order_request = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                type=OrderType.MARKET,
                time_in_force=tif,
                client_order_id=client_order_id,
            )

        internal_id = uuid.uuid4().hex
        side_str = side.value if hasattr(side, "value") else str(side)

        try:
            order_dict = self.client.submit_order(order_request)
        except AlpacaClientError as exc:
            reason = _broker_reason(exc)
            logger.warning(
                "paper_executor.order_rejected play_card_id=%s symbol=%s "
                "qty=%s side=%s reason=%s",
                play_card_id,
                symbol,
                qty,
                side_str,
                reason,
            )
            self._persist_order_row(
                order_id=internal_id,
                play_card_id=play_card_id,
                alpaca_order_id=None,
                symbol=symbol,
                side=side_str,
                qty=qty,
                status="rejected",
                reason=reason,
                event=event,
                parent_play_card_id=parent_play_card_id,
            )
            raise OrderRejected(
                f"Alpaca rejected order for {symbol}: {reason}"
            ) from exc

        alpaca_order_id = str(order_dict.get("id") or "")
        if not alpaca_order_id:
            # Defensive: the SDK should always populate ``id`` on
            # success, but if it does not we treat the response as a
            # rejection so downstream consumers never see a row with
            # an empty alpaca_order_id and status='submitted'.
            reason = "broker response missing order id"
            logger.error(
                "paper_executor.empty_order_id play_card_id=%s symbol=%s",
                play_card_id,
                symbol,
            )
            self._persist_order_row(
                order_id=internal_id,
                play_card_id=play_card_id,
                alpaca_order_id=None,
                symbol=symbol,
                side=side_str,
                qty=qty,
                status="rejected",
                reason=reason,
                event=event,
                parent_play_card_id=parent_play_card_id,
            )
            raise OrderRejected(reason)

        broker_status = (order_dict.get("status") or "submitted").lower()
        broker_qty = order_dict.get("qty")
        try:
            persisted_qty = int(broker_qty) if broker_qty is not None else qty
        except (TypeError, ValueError):
            persisted_qty = qty

        self._persist_order_row(
            order_id=internal_id,
            play_card_id=play_card_id,
            alpaca_order_id=alpaca_order_id,
            symbol=order_dict.get("symbol") or symbol,
            side=order_dict.get("side") or side_str,
            qty=persisted_qty,
            status=broker_status,
            reason=None,
            event=event,
            parent_play_card_id=parent_play_card_id,
        )

        logger.info(
            "paper_executor.order_submitted play_card_id=%s symbol=%s "
            "qty=%s side=%s alpaca_order_id=%s status=%s",
            play_card_id,
            symbol,
            persisted_qty,
            side_str,
            alpaca_order_id,
            broker_status,
        )
        return alpaca_order_id

    def get_order(self, order_id: str) -> dict[str, Any]:
        """Fetch the latest broker-side state for ``order_id``.

        Thin passthrough to :meth:`AlpacaClient.get_order` so callers
        only depend on :class:`PaperExecutor` for the order lifecycle.
        """
        if not order_id:
            raise ValueError("order_id must be a non-empty string")
        return self.client.get_order(order_id)

    def wait_for_fill(
        self,
        order_id: str,
        *,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: Optional[float] = None,
    ) -> dict[str, Any]:
        """Poll the broker until the order reaches a terminal state.

        Returns the final order dict (status ∈ :data:`TERMINAL_STATUSES`).
        Implements the bounded poll loop required by VAL-M3-016 — the
        loop exits as soon as the broker reports ``filled`` (or any
        other terminal status), and never runs longer than
        ``timeout_seconds`` (default 30s per the contract).

        Parameters
        ----------
        order_id:
            Broker-assigned order id (the return value of
            :meth:`execute`).
        timeout_seconds:
            Maximum wall time to wait. Default 30s — matches the
            VAL-M3-016 contract.
        poll_interval_seconds:
            Override the default ``poll_interval_seconds`` set on the
            executor. Tests pass ``0`` to turn the loop into a tight
            sequence over a queued cassette.

        Notes
        -----
        Each iteration queries the broker via :meth:`AlpacaClient.get_order`.
        The persisted ``orders`` row is updated on every observed
        status change so the SQLite snapshot tracks the broker.
        """
        if not order_id:
            raise ValueError("order_id must be a non-empty string")

        interval = (
            poll_interval_seconds
            if poll_interval_seconds is not None
            else self.poll_interval_seconds
        )
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))

        last_status: Optional[str] = None
        last_order: dict[str, Any] = {}
        while True:
            order = self.client.get_order(order_id)
            status = (order.get("status") or "").lower()
            last_order = order

            if status and status != last_status:
                self._update_order_status(order_id, order)
                last_status = status

            if status in TERMINAL_STATUSES:
                return order

            if time.monotonic() >= deadline:
                logger.warning(
                    "paper_executor.wait_for_fill_timeout order_id=%s "
                    "last_status=%s",
                    order_id,
                    status,
                )
                return order

            if interval > 0:
                time.sleep(interval)

    def get_orders_for_play(self, play_card_id: str) -> list[dict[str, Any]]:
        """Return all persisted orders for ``play_card_id``.

        Rows are ordered by ``created_at`` ascending so callers see
        the chronological lifecycle (entry → exits) without having
        to sort client-side. Used by validators (VAL-M3-034) and by
        the rotation engine (f-m3-10) to find the existing entry for
        an active play.
        """
        if not play_card_id:
            return []
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                SELECT id, play_card_id, alpaca_order_id, symbol, side,
                       qty, status, reason, event, parent_play_card_id,
                       created_at
                FROM orders
                WHERE play_card_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (play_card_id,),
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Internal: status updates during the poll loop.
    # ------------------------------------------------------------------

    def _update_order_status(
        self,
        alpaca_order_id: str,
        order: Mapping[str, Any],
    ) -> None:
        """Mirror a broker status change into the local ``orders`` row.

        Idempotent — runs an ``UPDATE`` keyed on ``alpaca_order_id``;
        when no matching row exists (e.g. tests that build a poll
        loop without a prior ``execute()``) the update is a no-op.
        """
        status = (order.get("status") or "").lower()
        if not status:
            return
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE orders SET status = ? WHERE alpaca_order_id = ?",
                (status, alpaca_order_id),
            )
            conn.commit()
        finally:
            conn.close()
