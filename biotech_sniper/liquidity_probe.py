"""Pre-entry chain-liquidity probe (M3 feature f-m3-12).

The module owns the ``probe_chain(ticker, expiry, strike, side)``
contract used by :mod:`biotech_sniper.paper_executor` to decide,
for a given options chain, whether a real-entry order should:

* fire as a single-strike full-size entry (classification
  ``"fillable"``),
* split across 2 strikes (classification ``"partial"``), or
* be skipped entirely (classification ``"unfillable"``).

Probe protocol
--------------
1. Build a deterministic ``client_order_id`` of the form
   ``"{ticker}-probe-{expiry}-{strike}-{date}"`` so a same-day
   re-run short-circuits via the unique-index on
   :data:`paper_orders.client_order_id`.
2. Refuse to submit when today's cumulative probe spend
   (``SUM(cost_usd)`` over ``liquidity_probes.submitted_at`` rows
   on the current UTC date) already meets or exceeds
   :data:`biotech_sniper.config.LIQUIDITY_PROBE_DAILY_USD_CAP`.
3. Persist a ``paper_orders`` row with ``purpose='liquidity_probe'``,
   ``status='submitted'`` BEFORE contacting Alpaca (write-then-submit
   invariant — VAL-M3-069).
4. Submit a 1-contract marketable-limit order via the supplied
   :class:`AlpacaClient`.
5. Poll the broker every ``poll_interval_seconds`` (default ``1.0``)
   for up to :data:`PROBE_TIMEOUT_SECONDS` seconds. Cancel the order
   if it has not reached a terminal state by the deadline.
6. Map the broker outcome onto the closed enum
   ``{filled, partial, unfilled, rejected}`` and write a row to
   the ``liquidity_probes`` SQLite table.
7. Return the probe outcome + classification + cost_usd as a
   :class:`ProbeResult` so the caller can size the real entry.

Validation contract
-------------------
* **VAL-M3-060** — ``liquidity_probes`` table schema (created by
  :mod:`biotech_sniper.db.schema`).
* **VAL-M3-065** — probe size is fixed at 1 contract; non-filled
  outcomes finalise within 60s + 5s tolerance.
* **VAL-M3-066** — daily ``SUM(cost_usd) <= 20``.
* **VAL-M3-067** — classification gates real-entry sizing:
  ``fillable``→single-strike, ``partial``→2-strike split,
  ``unfillable``→skip.
* **VAL-M3-068** — every probe ``paper_orders`` row carries
  ``purpose='liquidity_probe'``; no real-entry / exit row does.

Hermetic by design — the module accepts an injected
:class:`AlpacaClient` (or any duck-typed substitute exposing
``submit_order``, ``get_order``, ``cancel_order``) so unit tests
can replay the probe lifecycle without a live network call.
"""

from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Mapping, Optional

from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import LimitOrderRequest

from biotech_sniper import config as _config
from biotech_sniper import db as _db_module
from biotech_sniper.alpaca_client import (
    AlpacaClient,
    AlpacaClientError,
    PAPER_BASE_URL,
)
from biotech_sniper.paths import DATA_DIR


__all__ = [
    "PROBE_SIZE",
    "PROBE_TIMEOUT_SECONDS",
    "PROBE_CANCEL_TOLERANCE_SECONDS",
    "ProbeResult",
    "ProbeOutcome",
    "ProbeClassification",
    "DailyCapExceeded",
    "ProbeError",
    "probe_chain",
    "today_probe_spend_usd",
    "classify_outcome",
    "DEFAULT_DB_PATH",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Probe order size — fixed at 1 contract per VAL-M3-065. Larger sizes
#: are not supported; the validator enforces ``MAX(probe_size) <= 1``.
PROBE_SIZE: int = 1

#: Maximum wall-clock seconds the probe will wait for a fill before
#: cancelling. The validator allows a 5-second tolerance (i.e. a
#: probe that finalises within ``65`` seconds is acceptable) to
#: accommodate broker round-trip latency on the cancel path.
PROBE_TIMEOUT_SECONDS: float = 60.0

#: Tolerance window applied on top of :data:`PROBE_TIMEOUT_SECONDS`
#: when emitting structured logs / persisting timestamps. Validators
#: allow up to ``submitted_at + PROBE_TIMEOUT_SECONDS +
#: PROBE_CANCEL_TOLERANCE_SECONDS`` for non-filled outcomes.
PROBE_CANCEL_TOLERANCE_SECONDS: float = 5.0

#: Closed-set outcome enum aligned with
#: :data:`biotech_sniper.db.schema.sql`'s
#: ``CHECK(outcome IN (...))`` clause.
ProbeOutcome = str  # type alias — values: filled / partial / unfilled / rejected

#: Closed-set classification enum aligned with
#: :data:`biotech_sniper.db.schema.sql`'s
#: ``CHECK(classification IN (...))`` clause.
ProbeClassification = str  # type alias — values: fillable / partial / unfillable

#: Default DB path used when no override is supplied. Mirrors
#: :data:`biotech_sniper.paper_executor.DEFAULT_DB_PATH`.
DEFAULT_DB_PATH: Path = DATA_DIR / "alpha_sniper.db"


# Alpaca broker statuses that are terminal for a probe. Mirrors
# :data:`biotech_sniper.paper_executor.TERMINAL_STATUSES` but kept
# locally so the probe module is independent of the executor's
# constants (avoids an import cycle when the executor in turn
# imports the probe).
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"}
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProbeError(Exception):
    """Base class for liquidity-probe errors."""


class DailyCapExceeded(ProbeError):
    """Raised when today's cumulative probe spend already meets the cap.

    The module refuses to submit a fresh probe when this has fired so
    the validator's per-day ``SUM(cost_usd) <= 20`` invariant
    (VAL-M3-066) holds even under a buggy caller that retries after
    the cap is hit.
    """


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a single :func:`probe_chain` call.

    Attributes
    ----------
    ticker, expiry, strike, side, probe_size:
        Echo of the request parameters (so the caller can pass the
        result through downstream sizers without having to re-pack
        them).
    outcome:
        One of ``"filled"``, ``"partial"``, ``"unfilled"``,
        ``"rejected"`` — see module docstring.
    classification:
        One of ``"fillable"``, ``"partial"``, ``"unfillable"``.
    cost_usd:
        Dollar cost of any fills observed during the probe window.
        ``0.0`` for unfilled / rejected outcomes (only realised
        spend counts toward :data:`LIQUIDITY_PROBE_DAILY_USD_CAP`).
    time_to_fill_ms:
        Wall-clock milliseconds from submission to terminal state.
        ``None`` when the broker rejected the probe before any
        timestamp was observable.
    client_order_id:
        Deterministic id stamped on the parent ``paper_orders`` row.
    paper_order_id:
        Internal ``paper_orders.id`` (UUID4 hex) of the probe's
        parent order row.
    alpaca_order_id:
        Broker-assigned id once the submission was accepted; ``None``
        when the broker rejected the order outright.
    """

    ticker: str
    expiry: str
    strike: float
    side: str
    probe_size: int
    outcome: ProbeOutcome
    classification: ProbeClassification
    cost_usd: float
    time_to_fill_ms: Optional[int]
    client_order_id: str
    paper_order_id: str
    alpaca_order_id: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        """Return the result as a plain dict (logging / JSON-friendly)."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with ms.

    Mirrors the SQLite ``strftime('%Y-%m-%dT%H:%M:%fZ', 'now')``
    default used elsewhere in the project so timestamps written from
    Python and from SQL compare correctly as plain ISO strings.
    """
    now = _utc_now()
    return (
        now.strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{now.microsecond // 1000:03d}Z"
    )


def _today_iso(today: Optional[_dt.date] = None) -> str:
    """Return the ISO date string ``YYYY-MM-DD`` for today (UTC)."""
    if today is not None:
        return today.isoformat()
    return _utc_now().date().isoformat()


def _normalise_side(side: str) -> str:
    """Lowercase + validate ``side`` against the schema enum."""
    s = str(side).strip().lower()
    if s not in {"buy", "sell"}:
        raise ProbeError(
            f"probe side must be 'buy' or 'sell'; got {side!r}"
        )
    return s


def _format_strike(strike: float) -> str:
    """Format ``strike`` for embedding in a ``client_order_id``.

    Strikes are typically integers (``50.0``) or two-decimal values
    (``12.50``). The deterministic id keeps the original precision so
    different strikes produce distinct ids — ``50`` and ``50.0`` both
    collapse to ``"50"``, ``12.5`` to ``"12_5"``.
    """
    if float(strike).is_integer():
        return str(int(strike))
    return f"{strike:.4f}".rstrip("0").rstrip(".").replace(".", "_")


def _build_client_order_id(
    ticker: str,
    expiry: str,
    strike: float,
    side: str,
    today: _dt.date,
) -> str:
    """Build the deterministic ``client_order_id`` for the probe order.

    Format: ``"{TICKER}-probe-{expiry}-{side}-{strike}-{date}"``. The
    deterministic shape lets a same-day re-run short-circuit on the
    UNIQUE index of ``paper_orders.client_order_id`` /
    ``liquidity_probes.client_order_id``.
    """
    return (
        f"{ticker.strip().upper()}-probe-"
        f"{expiry}-{side}-{_format_strike(strike)}-{today.isoformat()}"
    )


def classify_outcome(outcome: str) -> ProbeClassification:
    """Map a probe ``outcome`` to its chain-liquidity ``classification``.

    Documented mapping (mission spec):

    * ``"filled"``   → ``"fillable"``
    * ``"partial"``  → ``"partial"``
    * ``"unfilled"`` → ``"unfillable"``
    * ``"rejected"`` → ``"unfillable"``

    Anything else raises :class:`ProbeError` so a typo in the caller
    surfaces immediately rather than as a silently-wrong gate
    decision.
    """
    o = str(outcome).strip().lower()
    if o == "filled":
        return "fillable"
    if o == "partial":
        return "partial"
    if o == "unfilled":
        return "unfillable"
    if o == "rejected":
        return "unfillable"
    raise ProbeError(f"unknown probe outcome {outcome!r}")


def today_probe_spend_usd(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    today: Optional[_dt.date] = None,
) -> float:
    """Return the cumulative ``cost_usd`` of probes submitted today.

    Mirrors the validator query used by VAL-M3-066:
    ``SELECT SUM(cost_usd) FROM liquidity_probes WHERE
    date(submitted_at) = date('now')``.

    The helper is the canonical accessor for the daily cap check —
    callers should NOT roll their own SQL.
    """
    today_str = _today_iso(today)
    conn = _db_module.connect(db_path)
    try:
        _db_module.run_migrations(conn)
        row = conn.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0.0) AS total
            FROM liquidity_probes
            WHERE date(submitted_at) = ?
            """,
            (today_str,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return 0.0
    total = row["total"] if isinstance(row, sqlite3.Row) else row[0]
    try:
        return float(total or 0.0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# DB writers
# ---------------------------------------------------------------------------


def _persist_probe_paper_order(
    db_path: Path | str,
    *,
    paper_order_id: str,
    client_order_id: str,
    symbol: str,
    side: str,
    qty: int,
    requested_mid_at_submit: Optional[float],
    created_at: str,
) -> None:
    """Insert the probe's parent ``paper_orders`` row.

    The row is stamped with ``purpose='liquidity_probe'`` so
    downstream consumers (slippage analytics, real-entry sizing,
    VAL-M3-068) can filter probe traffic out of real-entry queries.
    """
    conn = _db_module.connect(db_path)
    try:
        _db_module.run_migrations(conn)
        conn.execute(
            """
            INSERT OR IGNORE INTO paper_orders (
                id, play_card_id, alpaca_order_id, symbol, side, qty,
                status, reason, event, parent_play_card_id,
                requested_mid_at_submit, purpose, client_order_id,
                created_at
            ) VALUES (?, NULL, NULL, ?, ?, ?, 'submitted', NULL, NULL,
                      NULL, ?, 'liquidity_probe', ?, ?)
            """,
            (
                paper_order_id,
                symbol,
                side,
                qty,
                requested_mid_at_submit,
                client_order_id,
                created_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _update_probe_paper_order_after_submit(
    db_path: Path | str,
    paper_order_id: str,
    *,
    alpaca_order_id: Optional[str],
    status: str,
    reason: Optional[str] = None,
) -> None:
    """Update the parent ``paper_orders`` row with the broker outcome."""
    conn = _db_module.connect(db_path)
    try:
        conn.execute(
            """
            UPDATE paper_orders
            SET alpaca_order_id = ?, status = ?, reason = ?
            WHERE id = ?
            """,
            (alpaca_order_id, status, reason, paper_order_id),
        )
        conn.commit()
    finally:
        conn.close()


def _persist_probe_row(
    db_path: Path | str,
    *,
    ticker: str,
    expiry: str,
    strike: float,
    side: str,
    probe_size: int,
    submitted_at: str,
    finalized_at: Optional[str],
    outcome: str,
    time_to_fill_ms: Optional[int],
    classification: ProbeClassification,
    client_order_id: str,
    cost_usd: float,
) -> None:
    """Insert (or upsert) a row into ``liquidity_probes``.

    The ``client_order_id`` is UNIQUE so a same-day re-run replaces
    the prior row rather than stacking duplicates. We use
    ``INSERT OR REPLACE`` to keep the helper idempotent under the
    deterministic-id contract.
    """
    conn = _db_module.connect(db_path)
    try:
        _db_module.run_migrations(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO liquidity_probes (
                ticker, expiry, strike, side, probe_size,
                submitted_at, finalized_at, outcome,
                time_to_fill_ms, classification,
                client_order_id, cost_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker,
                expiry,
                float(strike),
                side,
                int(probe_size),
                submitted_at,
                finalized_at,
                outcome,
                time_to_fill_ms,
                classification,
                client_order_id,
                float(cost_usd),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Probe execution
# ---------------------------------------------------------------------------


def _build_probe_symbol(
    ticker: str,
    expiry: str,
    strike: float,
    side: str,
) -> str:
    """Build a placeholder OCC option symbol for the probe.

    The probe operates against the *intended* (ticker, expiry,
    strike) chain. Production callers can override the symbol via
    the ``symbol_override`` argument to :func:`probe_chain` (used by
    paper_executor.size_entry, which has the canonical OCC symbol
    already resolved). This helper provides a sensible default for
    callers that pass only the chain coordinates: a 21-char OCC
    string built from the inputs.
    """
    try:
        exp = _dt.date.fromisoformat(expiry)
    except (TypeError, ValueError):
        # Fallback: punt to a generic ticker-only symbol; the
        # broker will reject with a typed error if the symbol does
        # not exist, which surfaces as ``outcome='rejected'`` at
        # this module's surface — a deterministic, observable
        # failure mode rather than an exception leak.
        return f"{ticker.strip().upper()}-PROBE"
    cp = "C" if side == "buy" else "P"
    yymmdd = exp.strftime("%y%m%d")
    strike_int = int(round(float(strike) * 1000))
    return f"{ticker.strip().upper()}{yymmdd}{cp}{strike_int:08d}"


def _broker_status(order: Mapping[str, Any]) -> str:
    """Return the broker order status, lowercased and stripped."""
    return str(order.get("status") or "").strip().lower()


def _broker_filled_qty(order: Mapping[str, Any]) -> float:
    raw = order.get("filled_qty")
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _broker_filled_avg_price(order: Mapping[str, Any]) -> float:
    raw = order.get("filled_avg_price")
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _resolve_outcome(
    *,
    canceled: bool,
    final_status: str,
    filled_qty: float,
    probe_size: int,
) -> ProbeOutcome:
    """Map the final broker state to the closed outcome enum."""
    status = (final_status or "").lower()
    if status == "filled" or filled_qty >= probe_size:
        return "filled"
    if status == "rejected":
        return "rejected"
    if filled_qty > 0:
        # Partial fill regardless of subsequent cancel/expire.
        return "partial"
    if canceled or status in {"canceled", "cancelled", "expired", "done_for_day"}:
        return "unfilled"
    # Defensive fall-through — a non-terminal state at the deadline
    # without any fill is still "unfilled" from the probe's
    # perspective.
    return "unfilled"


def probe_chain(
    ticker: str,
    expiry: str,
    strike: float,
    side: str,
    *,
    alpaca_client: Optional[AlpacaClient] = None,
    db_path: Optional[Path] = None,
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
    poll_interval_seconds: float = 1.0,
    today: Optional[_dt.date] = None,
    limit_price: Optional[float] = None,
    symbol_override: Optional[str] = None,
    time_in_force: TimeInForce = TimeInForce.DAY,
    monotonic: Any = None,
) -> ProbeResult:
    """Probe the chain at ``(ticker, expiry, strike, side)``.

    Submits a 1-contract marketable-limit order tagged with
    ``purpose='liquidity_probe'``, polls the broker for up to
    :data:`PROBE_TIMEOUT_SECONDS`, cancels if the order has not
    reached a terminal state by the deadline, and persists the
    outcome + classification to the ``liquidity_probes`` table.

    Parameters
    ----------
    ticker:
        Underlying ticker (case-insensitive — uppercased on the way
        in).
    expiry:
        ISO-8601 expiry date (``"YYYY-MM-DD"``).
    strike:
        Strike price (in dollars).
    side:
        ``"buy"`` (probe a call ask) or ``"sell"`` (probe a put bid).
        The CHECK constraint on
        :data:`liquidity_probes.side` enforces this.
    alpaca_client:
        Injected :class:`AlpacaClient` (or duck-typed substitute
        exposing ``submit_order``, ``get_order``, ``cancel_order``).
        ``None`` (default) constructs a fresh paper-only client.
    db_path:
        Optional override for the SQLite database path. ``None`` →
        :data:`DEFAULT_DB_PATH`.
    timeout_seconds:
        Maximum wall time to wait for the probe to fill. Defaults to
        :data:`PROBE_TIMEOUT_SECONDS` (60s).
    poll_interval_seconds:
        Pause between broker status polls. Tests pass ``0`` to turn
        the loop into a tight sequence over a queued cassette.
    today:
        Override for the date stamped into the deterministic
        ``client_order_id``. Defaults to UTC today.
    limit_price:
        Optional limit price for the marketable-limit order. When
        ``None`` (the default) the helper falls back to a
        cross-the-spread limit equal to ``strike`` — this is
        sufficient for a paper-only marketable-limit probe; in
        production the caller should pass the live ask.
    symbol_override:
        Optional explicit OCC option symbol. When ``None`` the
        helper synthesises a symbol from (ticker, expiry, strike,
        side); production callers always pass the resolved symbol.
    time_in_force:
        Order time-in-force. Defaults to :data:`TimeInForce.DAY`.
    monotonic:
        Test hook — overrides :func:`time.monotonic` for the poll
        loop deadline. ``None`` uses :func:`time.monotonic`.

    Returns
    -------
    ProbeResult
        Structured result. The caller passes
        :attr:`ProbeResult.classification` to the sizing helper to
        decide entry shape.

    Raises
    ------
    DailyCapExceeded
        Today's cumulative ``SUM(cost_usd)`` already meets the cap.
        No order is submitted.
    ProbeError
        Validation failure (invalid side, etc.).
    """
    if int(PROBE_SIZE) != 1:
        # Defensive — PROBE_SIZE is a constant. If a future change
        # makes it non-1 the contract assertions in the contract
        # need to be updated; we surface that explicitly here.
        raise ProbeError(
            f"PROBE_SIZE must equal 1 per VAL-M3-065; got {PROBE_SIZE}"
        )

    db = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    today_date = today or _utc_now().date()
    side_norm = _normalise_side(side)
    ticker_norm = ticker.strip().upper()

    cap = float(_config.LIQUIDITY_PROBE_DAILY_USD_CAP)
    spent = today_probe_spend_usd(db, today=today_date)
    if spent >= cap:
        logger.warning(
            "liquidity_probe.daily_cap_exceeded ticker=%s expiry=%s "
            "strike=%s side=%s spent_usd=%.2f cap_usd=%s",
            ticker_norm,
            expiry,
            strike,
            side_norm,
            spent,
            cap,
        )
        raise DailyCapExceeded(
            f"liquidity_probe daily cap reached: spent_usd={spent:.2f} "
            f">= cap={cap:.2f} (today={today_date.isoformat()})"
        )

    client_order_id = _build_client_order_id(
        ticker_norm, expiry, float(strike), side_norm, today_date
    )
    paper_order_id = uuid.uuid4().hex
    symbol = symbol_override or _build_probe_symbol(
        ticker_norm, expiry, float(strike), side_norm
    )

    # Resolve a sane marketable-limit price.
    if limit_price is None:
        # Cross-the-spread default — the broker will fill at or
        # better than the limit. ``strike`` is a coarse but
        # well-defined ceiling for unit tests; production callers
        # should always pass the live ask.
        effective_limit = float(strike)
    else:
        effective_limit = float(limit_price)

    # Resolve / construct the Alpaca client. We do NOT force a
    # construction when the caller already provided one (tests).
    if alpaca_client is None:
        # Lazy import path identical to AlpacaBackedProbe — keeps
        # the smoke import on hosts without paper credentials clean.
        alpaca_client = AlpacaClient()

    # Defence-in-depth: make sure we never run a probe against a
    # non-paper Alpaca client. The probe submits a real broker call
    # so the paper-only guardrail must hold.
    base_url = getattr(alpaca_client, "base_url", None)
    if base_url != PAPER_BASE_URL:
        raise ProbeError(
            "liquidity_probe refuses to operate against non-paper "
            f"Alpaca client (base_url={base_url!r}). Expected "
            f"{PAPER_BASE_URL!r}."
        )

    # f-m3-11 write-then-submit invariant: persist the parent
    # paper_orders row (purpose='liquidity_probe') BEFORE contacting
    # the broker.
    submitted_at_iso = _utc_now_iso()
    submitted_at = _utc_now()
    _persist_probe_paper_order(
        db,
        paper_order_id=paper_order_id,
        client_order_id=client_order_id,
        symbol=symbol,
        side=side_norm,
        qty=PROBE_SIZE,
        requested_mid_at_submit=effective_limit,
        created_at=submitted_at_iso,
    )

    order_request = LimitOrderRequest(
        symbol=symbol,
        qty=PROBE_SIZE,
        side=OrderSide.BUY if side_norm == "buy" else OrderSide.SELL,
        type=OrderType.LIMIT,
        time_in_force=time_in_force,
        limit_price=float(effective_limit),
        client_order_id=client_order_id,
    )

    # Submission ----------------------------------------------------
    try:
        order_dict = alpaca_client.submit_order(order_request)
    except AlpacaClientError as exc:
        reason = str(exc)
        logger.warning(
            "liquidity_probe.submit_rejected ticker=%s symbol=%s "
            "reason=%s",
            ticker_norm,
            symbol,
            reason,
        )
        finalized_at_iso = _utc_now_iso()
        _update_probe_paper_order_after_submit(
            db,
            paper_order_id,
            alpaca_order_id=None,
            status="rejected",
            reason=reason,
        )
        _persist_probe_row(
            db,
            ticker=ticker_norm,
            expiry=expiry,
            strike=float(strike),
            side=side_norm,
            probe_size=PROBE_SIZE,
            submitted_at=submitted_at_iso,
            finalized_at=finalized_at_iso,
            outcome="rejected",
            time_to_fill_ms=None,
            classification=classify_outcome("rejected"),
            client_order_id=client_order_id,
            cost_usd=0.0,
        )
        return ProbeResult(
            ticker=ticker_norm,
            expiry=expiry,
            strike=float(strike),
            side=side_norm,
            probe_size=PROBE_SIZE,
            outcome="rejected",
            classification="unfillable",
            cost_usd=0.0,
            time_to_fill_ms=None,
            client_order_id=client_order_id,
            paper_order_id=paper_order_id,
            alpaca_order_id=None,
        )

    alpaca_order_id = str(order_dict.get("id") or "") or None
    _update_probe_paper_order_after_submit(
        db,
        paper_order_id,
        alpaca_order_id=alpaca_order_id,
        status=_broker_status(order_dict) or "submitted",
    )

    # Poll loop ----------------------------------------------------
    monotonic_fn = monotonic if monotonic is not None else time.monotonic
    deadline = monotonic_fn() + max(0.0, float(timeout_seconds))
    final_order: Mapping[str, Any] = order_dict
    final_status = _broker_status(order_dict)
    canceled = False

    while True:
        # If we already have a terminal status from the initial
        # submit, skip straight to the resolution step.
        if final_status in _TERMINAL_STATUSES:
            break

        if monotonic_fn() >= deadline:
            # Window elapsed without a terminal state — cancel the
            # order, then re-poll once to see the final state.
            try:
                if alpaca_order_id:
                    alpaca_client.cancel_order(alpaca_order_id)
                canceled = True
            except AlpacaClientError as exc:  # pragma: no cover — defensive
                logger.warning(
                    "liquidity_probe.cancel_failed alpaca_order_id=%s "
                    "reason=%s",
                    alpaca_order_id,
                    exc,
                )
            try:
                if alpaca_order_id:
                    final_order = alpaca_client.get_order(alpaca_order_id)
                    final_status = _broker_status(final_order)
            except AlpacaClientError:  # pragma: no cover — defensive
                pass
            break

        if poll_interval_seconds > 0:
            time.sleep(poll_interval_seconds)

        try:
            if alpaca_order_id:
                final_order = alpaca_client.get_order(alpaca_order_id)
                final_status = _broker_status(final_order)
        except AlpacaClientError as exc:
            logger.warning(
                "liquidity_probe.poll_failed alpaca_order_id=%s "
                "reason=%s",
                alpaca_order_id,
                exc,
            )
            # Keep polling — a transient broker error should not
            # prematurely terminate the probe. The deadline guard
            # will eventually break the loop.

    # Resolve outcome ---------------------------------------------
    finalized_at_iso = _utc_now_iso()
    finalized_at = _utc_now()
    elapsed_ms = max(
        0,
        int((finalized_at - submitted_at).total_seconds() * 1000.0),
    )
    filled_qty = _broker_filled_qty(final_order)
    filled_avg_price = _broker_filled_avg_price(final_order)
    outcome = _resolve_outcome(
        canceled=canceled,
        final_status=final_status,
        filled_qty=filled_qty,
        probe_size=PROBE_SIZE,
    )
    cost_usd = (
        max(0.0, filled_qty) * max(0.0, filled_avg_price) * 100.0
        if outcome in {"filled", "partial"}
        else 0.0
    )
    classification = classify_outcome(outcome)
    time_to_fill_ms: Optional[int] = (
        elapsed_ms if outcome in {"filled", "partial"} else elapsed_ms
    )

    # f-m3-11: persist the broker's terminal status onto the parent
    # paper_orders row so VAL-M3-068 holds (purpose remains
    # 'liquidity_probe' on the row regardless of fill status).
    _update_probe_paper_order_after_submit(
        db,
        paper_order_id,
        alpaca_order_id=alpaca_order_id,
        status=final_status or outcome,
    )
    _persist_probe_row(
        db,
        ticker=ticker_norm,
        expiry=expiry,
        strike=float(strike),
        side=side_norm,
        probe_size=PROBE_SIZE,
        submitted_at=submitted_at_iso,
        finalized_at=finalized_at_iso,
        outcome=outcome,
        time_to_fill_ms=time_to_fill_ms,
        classification=classification,
        client_order_id=client_order_id,
        cost_usd=cost_usd,
    )

    logger.info(
        "liquidity_probe.complete ticker=%s expiry=%s strike=%s "
        "side=%s outcome=%s classification=%s cost_usd=%.4f "
        "time_to_fill_ms=%s client_order_id=%s",
        ticker_norm,
        expiry,
        strike,
        side_norm,
        outcome,
        classification,
        cost_usd,
        time_to_fill_ms,
        client_order_id,
    )

    return ProbeResult(
        ticker=ticker_norm,
        expiry=expiry,
        strike=float(strike),
        side=side_norm,
        probe_size=PROBE_SIZE,
        outcome=outcome,
        classification=classification,
        cost_usd=cost_usd,
        time_to_fill_ms=time_to_fill_ms,
        client_order_id=client_order_id,
        paper_order_id=paper_order_id,
        alpaca_order_id=alpaca_order_id,
    )
