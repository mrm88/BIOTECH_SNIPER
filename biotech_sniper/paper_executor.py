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

Sizing logic — ``size_position(play_card)``, the ``$250``
``RISK_PER_PLAY_USD`` cap, the mid-price computation
(``mid = (bid + ask) / 2`` from the chain row), the concurrency cap
(``MAX_CONCURRENT_PLAYS=3``), and the deployed-capital cap
(``MAX_DEPLOYED_USD=750``) — all land in feature ``f-m3-04`` and
live in this module. Constants are sourced from
:mod:`biotech_sniper.config` so the executor body has no hardcoded
cap literals (validators grep for the cap values to catch
regressions). Full execution telemetry / liquidity probing lands
in ``f-m3-11`` and ``f-m3-12``.

Validation contract assertions added by f-m3-04
-----------------------------------------------

* **VAL-M3-019** — ``size_position(play_card)`` returns
  ``floor(RISK_PER_PLAY_USD / (mid * 100))`` when ``mid * 100 ≤
  RISK_PER_PLAY_USD``.
* **VAL-M3-020** — ``mid = (bid + ask) / 2``; both bid and ask
  zero/None raises :class:`MissingQuoteData`.
* **VAL-M3-021** — ``mid * 100 > RISK_PER_PLAY_USD`` returns
  ``qty=0`` from ``size_position`` and raises
  :class:`ContractTooExpensive` from ``execute``.
* **VAL-M3-022** — ``state/calibration_params.json``
  ``risk_per_play_usd`` overrides the default cap when present.
* **VAL-M3-023** — ``len(client.get_positions()) >=
  MAX_CONCURRENT_PLAYS`` raises :class:`ConcurrencyCapExceeded`.
* **VAL-M3-024** — ``deployed + planned_cost > MAX_DEPLOYED_USD``
  raises :class:`DeployedCapExceeded`.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from biotech_sniper import config as _config
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
    "ContractTooExpensive",
    "ConcurrencyCapExceeded",
    "DeployedCapExceeded",
    "MissingQuoteData",
    "size_position",
    "_options_positions",
    "_is_multi_strike",
    "MULTI_STRIKE_FILLABLE_CLASSIFICATIONS",
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

#: Liquidity-probe (f-m3-12) classifications that *unlock* multi-strike
#: entries. When a play card carries
#: ``play_card['liquidity_classification']`` set to one of these
#: values (case-insensitive), a 2-leg play card with both legs as
#: ``side='buy'`` is interpreted as a multi-strike entry per the
#: f-m3-08 contract. Any other classification (or no classification
#: when 2 legs are still present and both are buys) defaults to the
#: same multi-strike treatment — the gating behaviour exists so that
#: a future liquidity-probe upstream can demote a tradeable ticker
#: back to single-strike by stamping ``"unfillable"`` on the card.
MULTI_STRIKE_FILLABLE_CLASSIFICATIONS: frozenset[str] = frozenset(
    {"fillable", "partial"}
)

#: Liquidity-probe classifications that **force** single-strike even
#: if the play card carries 2 buy legs. ``"unfillable"`` is the
#: signal the f-m3-12 probe will stamp when the chain is too thin
#: to support a multi-strike entry. The first leg is taken and the
#: second is dropped (with a structured WARNING log).
MULTI_STRIKE_BLOCKED_CLASSIFICATIONS: frozenset[str] = frozenset(
    {"unfillable"}
)


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


class MissingQuoteData(PaperExecutorError):
    """Raised by :func:`size_position` when bid AND ask are unusable.

    The play card's chain row must carry usable bid/ask. ``last``
    alone is not a substitute — entering on a stale "last" risks
    huge slippage on illiquid options. We refuse to guess and raise
    so the caller surfaces the incomplete chain to the operator.
    """


class ContractTooExpensive(PaperExecutorError):
    """Raised when a single contract costs more than the per-play cap.

    :func:`size_position` returns ``0`` in that case (cannot size a
    fractional contract); :meth:`PaperExecutor.execute` re-raises
    this exception so callers know the play was skipped because of
    the price, not because of a broker rejection. A row with
    ``status='rejected'`` is persisted before the exception is
    raised so the orders table reflects the skip per VAL-M3-033.
    """


class ConcurrencyCapExceeded(PaperExecutorError):
    """Raised when ``len(client.get_positions()) >= MAX_CONCURRENT_PLAYS``.

    The cap is sourced from :data:`biotech_sniper.config.MAX_CONCURRENT_PLAYS`
    so the executor body has no hardcoded literal. A
    ``status='rejected'`` row is persisted with the exception message
    in ``reason`` for auditability (VAL-M3-033).
    """


class DeployedCapExceeded(PaperExecutorError):
    """Raised when the planned entry would push deployed > MAX_DEPLOYED_USD.

    Deployed capital is computed as
    ``sum(qty * avg_entry_price * 100)`` over the broker's open
    positions. The cap is sourced from
    :data:`biotech_sniper.config.MAX_DEPLOYED_USD`. A
    ``status='rejected'`` row is persisted with the exception
    message in ``reason``.
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


def _is_multi_strike(play_card: Mapping[str, Any]) -> bool:
    """Return ``True`` iff the play card declares a multi-strike entry.

    Multi-strike (f-m3-08) means a single play card with **exactly 2
    BUY legs at different strikes** of the same ticker. Spreads
    (one buy + one sell) are NOT multi-strike; they remain rejected
    by :func:`_validate_single_leg`.

    The detection is intentionally conservative — it returns ``False``
    for:

    * any leg-count other than 2,
    * any leg whose ``side`` is not ``'buy'`` (i.e. spreads, condors,
      vertical SELL legs),
    * an explicit ``order_class`` of anything other than
      ``'simple'`` (multi-strike orders are submitted as two
      independent simple orders, not as a single multi-leg request),
    * a ``liquidity_classification`` whose value is in
      :data:`MULTI_STRIKE_BLOCKED_CLASSIFICATIONS`.

    The decision to single- vs multi-strike thus comes from the play
    card itself — the upstream selector (f-m2 selection logic) and
    the f-m3-12 liquidity probe collaboratively populate the legs
    list and the optional ``liquidity_classification`` flag.
    """
    legs = play_card.get("option_legs")
    if (
        not isinstance(legs, Sequence)
        or isinstance(legs, (str, bytes))
        or len(legs) != 2
    ):
        return False
    for leg in legs:
        if not isinstance(leg, Mapping):
            return False
        side = str(leg.get("side", "buy")).strip().lower()
        if side != "buy":
            return False
    order_class = play_card.get("order_class")
    if order_class is not None and str(order_class).strip().lower() != "simple":
        return False
    classification = play_card.get("liquidity_classification")
    if (
        isinstance(classification, str)
        and classification.strip().lower()
        in MULTI_STRIKE_BLOCKED_CLASSIFICATIONS
    ):
        return False
    return True


def _validate_single_leg(
    play_card: Mapping[str, Any],
    *,
    require_qty: bool = True,
) -> Mapping[str, Any]:
    """Return the single leg from ``play_card['option_legs']`` or raise.

    The validator runs BEFORE any network call so a rejected play
    card never produces a half-submitted order on Alpaca's side. We
    enforce the contract from the f-m3-03 description and VAL-M3-017:

    * ``play_card['option_legs']`` MUST be a list with exactly one
      entry. Length 0 or > 1 raises :class:`UnsupportedOrderShape`.
    * ``play_card['order_class']``, when present, MUST equal
      ``'simple'``. ``'mleg'`` / ``'bracket'`` etc. raise.
    * The single leg MUST have a non-empty ``symbol`` (the OCC
      option symbol the broker recognises) and, when ``require_qty``
      is ``True`` (the default for the ``execute`` path), a positive
      integer ``qty``. ``size_position`` calls this with
      ``require_qty=False`` since it computes the qty itself from
      the chain quote.
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

    if require_qty:
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


def _coerce_quote(value: Any) -> float:
    """Return ``float(value)`` clamped to ``>= 0`` or ``0.0`` on parse fail.

    Quotes from chain rows can arrive as ``None`` (no recent quote),
    integers, or strings; downstream math wants a non-negative
    float. We fail soft to ``0.0`` here so the caller decides whether
    a missing-quote situation is fatal (it is for both bid AND ask)
    or merely partial (a one-sided quote is still usable for the
    mid).
    """
    if value is None:
        return 0.0
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if out > 0.0 else 0.0


def size_position(
    play_card: Mapping[str, Any],
    *,
    risk_per_play_usd: Optional[int] = None,
) -> int:
    """Return the contract qty implied by the play card's quote + cap.

    Reads ``play_card['option_legs'][0]['bid']`` and ``['ask']``,
    computes ``mid = (bid + ask) / 2``, then returns
    ``floor(cap / (mid * 100))`` where ``cap`` is the per-play USD
    risk cap (``risk_per_play_usd`` when supplied; otherwise
    :func:`biotech_sniper.config.get_risk_per_play_usd` which honours
    ``state/calibration_params.json``).

    Returns ``0`` when ``mid * 100 > cap`` (a single contract would
    blow the per-play cap; ``execute`` translates this into a
    :class:`ContractTooExpensive` rejection).

    Raises :class:`MissingQuoteData` when both bid AND ask are
    missing/zero — refusing to guess from ``last`` keeps the sizing
    deterministic and avoids slippage on illiquid options.

    Note: this function does NOT mutate the play card; the caller
    receives the qty and decides what to do with it.
    """
    leg = _validate_single_leg(play_card, require_qty=False)
    bid = _coerce_quote(leg.get("bid"))
    ask = _coerce_quote(leg.get("ask"))

    if bid <= 0.0 and ask <= 0.0:
        raise MissingQuoteData(
            "option_legs[0] has no usable bid or ask: "
            f"bid={leg.get('bid')!r}, ask={leg.get('ask')!r}. "
            "size_position refuses to size from `last` alone."
        )

    mid = (bid + ask) / 2.0
    cost_per_contract = mid * 100.0

    cap = (
        risk_per_play_usd
        if risk_per_play_usd is not None
        else _config.get_risk_per_play_usd()
    )

    if cost_per_contract > cap:
        return 0

    if cost_per_contract <= 0:
        # Defensive: bid+ask both > 0 but mid * 100 <= 0 cannot happen
        # arithmetically; treat it as "no size" rather than divide-by-zero.
        return 0

    return int(math.floor(cap / cost_per_contract))


def _planned_cost_usd(leg: Mapping[str, Any], qty: int) -> float:
    """Return the projected dollar cost of entering ``qty`` of ``leg``.

    Prefers the chain mid (``(bid + ask) / 2``) as the most accurate
    fillable price; falls back to ``limit_price`` and finally to
    ``0.0`` when neither is available. Multiplied by 100 because
    each option contract represents 100 underlying shares.
    """
    bid = _coerce_quote(leg.get("bid"))
    ask = _coerce_quote(leg.get("ask"))
    if bid > 0.0 and ask > 0.0:
        per_contract = (bid + ask) / 2.0
    elif bid > 0.0 or ask > 0.0:
        per_contract = bid + ask  # one-sided, use the populated leg
    else:
        try:
            per_contract = float(leg.get("limit_price") or 0.0)
        except (TypeError, ValueError):
            per_contract = 0.0
    return float(qty) * per_contract * 100.0


def _options_positions(
    positions: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Return only the option positions from an Alpaca positions list.

    Alpaca position dicts carry an ``asset_class`` field whose value
    is ``'us_option'`` for option contracts and ``'us_equity'`` for
    shares (the SDK enum is rendered as a lowercase string by
    :func:`biotech_sniper.alpaca_client._enum_value`). The
    :class:`PaperExecutor` caps (``MAX_CONCURRENT_PLAYS`` and
    ``MAX_DEPLOYED_USD``) MUST count only option contracts:

    * Concurrency: a paper account that holds 6 unrelated equities
      from earlier manual sandbox testing must not block a fresh
      single-leg options entry that would still leave the operator
      below the 3-option concurrency cap.
    * Deployed capital: equity share positions are denominated in
      shares, not contracts. Multiplying ``qty * avg_entry_price *
      100`` (the option-contract math) over a 1000-share, $200-avg
      equity holding fabricates a $20M "deployed" sum that has no
      basis in reality and trips
      :class:`DeployedCapExceeded` on every entry.

    Positions whose ``asset_class`` field is missing or unrecognised
    are treated as **non-options** (skipped). This is the
    conservative default: when in doubt, do not count it as an
    option contract — refusing to size in the rare ambiguous case
    is preferable to artificially inflating the cap.
    """
    out: list[Mapping[str, Any]] = []
    for position in positions or ():
        if not isinstance(position, Mapping):
            continue
        asset_class = position.get("asset_class")
        if asset_class is None:
            # Legacy / older payloads with no asset_class field at
            # all are treated as non-options. This matches the
            # f-m3-07b spec: "accept legacy ``asset_class`` absent →
            # treat as not-options (skip)".
            continue
        if str(asset_class).strip().lower() == "us_option":
            out.append(position)
    return out


def _deployed_capital_usd(positions: Sequence[Mapping[str, Any]]) -> float:
    """Sum ``qty * avg_entry_price * 100`` across active positions.

    Mirrors VAL-M3-024 exactly. Missing or unparseable fields are
    treated as ``0.0`` so a malformed broker payload does not
    artificially relax the cap. Numeric coercion handles the
    pydantic-typed ``Decimal`` values alpaca-py occasionally emits.
    """
    total = 0.0
    for position in positions or ():
        if not isinstance(position, Mapping):
            continue
        qty_raw = position.get("qty")
        avg_raw = position.get("avg_entry_price")
        try:
            qty = float(qty_raw) if qty_raw is not None else 0.0
        except (TypeError, ValueError):
            qty = 0.0
        try:
            avg = float(avg_raw) if avg_raw is not None else 0.0
        except (TypeError, ValueError):
            avg = 0.0
        total += abs(qty) * avg * 100.0
    return total


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

    def execute(
        self, play_card: Mapping[str, Any]
    ) -> "str | list[str]":
        """Submit a paper-trading entry from ``play_card``.

        Dispatches based on the play card shape:

        * **Single-strike** (1 leg in ``option_legs``) — submits one
          order and returns the broker-assigned alpaca_order_id as
          a ``str``. Default behaviour. Backward-compatible with the
          f-m3-03 contract.
        * **Multi-strike** (2 BUY legs in ``option_legs``, no
          ``order_class != 'simple'`` and no
          ``liquidity_classification`` in
          :data:`MULTI_STRIKE_BLOCKED_CLASSIFICATIONS`) — submits
          one order per leg, both tagged with the same
          ``play_card_id`` so :meth:`get_orders_for_play` returns
          both rows. Returns a ``list[str]`` of alpaca_order_ids in
          submission order. The per-play ``risk_per_play_usd`` cap
          is **split evenly** across the legs so the combined
          notional stays inside the cap (per AGENTS.md §"Risk
          defaults").

        Anything else (1+2 mixed sides, 3+ legs, mleg/bracket order
        class) raises :class:`UnsupportedOrderShape` BEFORE any
        network call.

        Single-strike contract (unchanged from f-m3-03):

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
        ContractTooExpensive
            If :func:`size_position` returns ``0`` for the supplied
            chain quote (mid * 100 > per-play cap). A
            ``status='rejected'`` row is persisted before the
            exception is raised. No order is submitted.
        ConcurrencyCapExceeded
            If the broker already reports ≥ ``MAX_CONCURRENT_PLAYS``
            open positions. Persists a ``status='rejected'`` row.
        DeployedCapExceeded
            If ``deployed_capital + planned_cost > MAX_DEPLOYED_USD``.
            Persists a ``status='rejected'`` row.
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

        # f-m3-08: multi-strike dispatch. Detected here so the rest of
        # the method can stay focused on the single-leg fast path. A
        # play card with 2 BUY legs is decomposed into two independent
        # single-leg orders; the per-play cap is split evenly across
        # the legs.
        if _is_multi_strike(play_card):
            return self._execute_multi_strike(play_card)

        play_card_id = play_card.get("play_card_id")
        parent_play_card_id = play_card.get("parent_play_card_id")
        event = play_card.get("event")

        # f-m3-04: when the leg carries a chain quote (bid/ask), run
        # ``size_position`` to derive the contract count. A ``0``
        # result means the contract alone exceeds the per-play cap;
        # we persist a rejection row and raise
        # :class:`ContractTooExpensive`. When the leg already carries
        # an explicit ``qty`` (legacy callers / pre-sized cards), we
        # honour it and skip sizing — `_validate_single_leg` will
        # surface a missing-qty as ``UnsupportedOrderShape``.
        legs_preview = play_card.get("option_legs")
        leg_preview: Mapping[str, Any] = (
            legs_preview[0]
            if isinstance(legs_preview, Sequence)
            and not isinstance(legs_preview, (str, bytes))
            and len(legs_preview) >= 1
            and isinstance(legs_preview[0], Mapping)
            else {}
        )
        has_quote = (
            leg_preview.get("bid") is not None
            or leg_preview.get("ask") is not None
        )
        has_explicit_qty = leg_preview.get("qty") is not None

        if has_quote and not has_explicit_qty:
            sized_qty = size_position(play_card)
            if sized_qty == 0:
                cap = _config.get_risk_per_play_usd()
                bid = _coerce_quote(leg_preview.get("bid"))
                ask = _coerce_quote(leg_preview.get("ask"))
                mid = (bid + ask) / 2.0 if (bid + ask) > 0 else 0.0
                msg = (
                    f"ContractTooExpensive: mid=${mid:.2f} (bid={bid}, "
                    f"ask={ask}) implies ${mid * 100:.2f} per contract, "
                    f"exceeds risk_per_play_usd=${cap}"
                )
                logger.warning(
                    "paper_executor.contract_too_expensive play_card_id=%s "
                    "symbol=%s mid=%.4f cap=%s",
                    play_card_id,
                    leg_preview.get("symbol"),
                    mid,
                    cap,
                )
                self._persist_order_row(
                    order_id=uuid.uuid4().hex,
                    play_card_id=play_card_id,
                    alpaca_order_id=None,
                    symbol=str(leg_preview.get("symbol") or "") or None,
                    side=str(leg_preview.get("side") or "buy"),
                    qty=0,
                    status="rejected",
                    reason=msg,
                    event=event,
                    parent_play_card_id=parent_play_card_id,
                )
                raise ContractTooExpensive(msg)

            # Inject the sized qty so the rest of the pipeline (the
            # standard ``_validate_single_leg`` + alpaca-py request
            # builder) sees a fully-populated leg without us needing
            # to mutate the caller's play_card.
            sized_leg = {**leg_preview, "qty": sized_qty}
            play_card = {**play_card, "option_legs": [sized_leg]}

        leg = _validate_single_leg(play_card)

        client_order_id = play_card.get("client_order_id") or leg.get(
            "client_order_id"
        )

        symbol = str(leg["symbol"])
        qty = int(leg["qty"])
        side = _coerce_side(leg.get("side", "buy"))
        tif = _coerce_tif(leg.get("time_in_force"))
        limit_price = leg.get("limit_price")
        side_str = side.value if hasattr(side, "value") else str(side)

        # f-m3-04: Concurrency + deployed-capital caps. Both are
        # sourced from :mod:`biotech_sniper.config` so this module
        # has no hardcoded literals (validators grep the cap values
        # to catch regressions).
        # ``get_positions`` is invoked via a duck-typed lookup so
        # exit-only callers (or tests that omit positions on the
        # client double) degrade to "no caps" rather than crashing
        # — production callers always pass an
        # :class:`AlpacaClient`, which implements ``get_positions``.
        #
        # f-m3-05: caps apply to ENTRIES only. Exit submissions
        # (``side='sell'`` — the IV-crush exit, stop-loss, adverse-
        # news, and rotation triggers) reduce capital deployment
        # rather than add to it, and must not be blocked when the
        # broker already reports ``MAX_CONCURRENT_PLAYS`` open
        # positions (which is precisely the state in which exits
        # are most likely to fire). We skip both the position probe
        # and the cap arithmetic when ``side`` is sell so the exit
        # path stays a clean passthrough to the broker.
        is_entry = side == OrderSide.BUY
        positions: list[dict[str, Any]] = []
        if is_entry:
            get_positions = getattr(self.client, "get_positions", None)
            if callable(get_positions):
                try:
                    raw_positions = get_positions() or []
                except AlpacaClientError as exc:
                    # Surface broker errors during the cap probe as a
                    # rejection so the orders table reflects the failure
                    # without leaking a half-submitted entry.
                    reason = _broker_reason(exc)
                    logger.warning(
                        "paper_executor.position_probe_failed "
                        "play_card_id=%s reason=%s",
                        play_card_id,
                        reason,
                    )
                    self._persist_order_row(
                        order_id=uuid.uuid4().hex,
                        play_card_id=play_card_id,
                        alpaca_order_id=None,
                        symbol=symbol,
                        side=side_str,
                        qty=qty,
                        status="rejected",
                        reason=f"OrderRejected: {reason}",
                        event=event,
                        parent_play_card_id=parent_play_card_id,
                    )
                    raise OrderRejected(
                        f"Failed to read positions before submission: {reason}"
                    ) from exc
                positions = list(raw_positions)

        # f-m3-07b: caps apply to OPTION positions only. A paper
        # account often holds unrelated equity holdings from manual
        # sandbox testing; those must not trip
        # :class:`ConcurrencyCapExceeded` or
        # :class:`DeployedCapExceeded` against an options-only
        # strategy. Filter the broker payload before either cap
        # arithmetic runs. The filter helper
        # :func:`_options_positions` keeps only entries whose
        # ``asset_class == 'us_option'`` (case-insensitive); equities,
        # crypto, and any payload missing ``asset_class`` are
        # excluded. The unfiltered list is no longer referenced past
        # this point.
        option_positions = _options_positions(positions) if is_entry else []

        cap_concurrent = _config.MAX_CONCURRENT_PLAYS
        if is_entry and len(option_positions) >= cap_concurrent:
            msg = (
                f"ConcurrencyCapExceeded: {len(option_positions)} active "
                f"option positions meets/exceeds "
                f"MAX_CONCURRENT_PLAYS={cap_concurrent}"
            )
            logger.warning(
                "paper_executor.concurrency_cap_exceeded play_card_id=%s "
                "symbol=%s active_options=%s cap=%s",
                play_card_id,
                symbol,
                len(option_positions),
                cap_concurrent,
            )
            self._persist_order_row(
                order_id=uuid.uuid4().hex,
                play_card_id=play_card_id,
                alpaca_order_id=None,
                symbol=symbol,
                side=side_str,
                qty=qty,
                status="rejected",
                reason=msg,
                event=event,
                parent_play_card_id=parent_play_card_id,
            )
            raise ConcurrencyCapExceeded(msg)

        cap_deployed = _config.MAX_DEPLOYED_USD
        deployed = (
            _deployed_capital_usd(option_positions) if is_entry else 0.0
        )
        planned_cost = _planned_cost_usd(leg, qty) if is_entry else 0.0
        if is_entry and (deployed + planned_cost) > cap_deployed:
            msg = (
                f"DeployedCapExceeded: deployed=${deployed:.2f} + "
                f"planned=${planned_cost:.2f} = "
                f"${deployed + planned_cost:.2f} > "
                f"MAX_DEPLOYED_USD=${cap_deployed}"
            )
            logger.warning(
                "paper_executor.deployed_cap_exceeded play_card_id=%s "
                "symbol=%s deployed=%.2f planned=%.2f cap=%s",
                play_card_id,
                symbol,
                deployed,
                planned_cost,
                cap_deployed,
            )
            self._persist_order_row(
                order_id=uuid.uuid4().hex,
                play_card_id=play_card_id,
                alpaca_order_id=None,
                symbol=symbol,
                side=side_str,
                qty=qty,
                status="rejected",
                reason=msg,
                event=event,
                parent_play_card_id=parent_play_card_id,
            )
            raise DeployedCapExceeded(msg)

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

    # ------------------------------------------------------------------
    # Multi-strike entry (f-m3-08).
    # ------------------------------------------------------------------

    def _execute_multi_strike(
        self, play_card: Mapping[str, Any]
    ) -> list[str]:
        """Submit one order per leg of a multi-strike play card.

        Each leg is dispatched through the single-leg :meth:`execute`
        path (with a synthesized 1-leg play card) so all the existing
        guardrails — paper-only check, position cap, deployed-capital
        cap, sizing, persistence — apply to every leg without
        duplication.

        Cap split
        ---------
        The per-play cap (``config.get_risk_per_play_usd()``,
        default $250) is **split evenly** across the legs. With 2
        legs the per-leg cap is ``cap // 2`` so the **combined
        notional stays inside the original cap** even after integer
        rounding. Each leg's qty is computed from its own bid/ask
        quote against this per-leg cap; legs that cannot size at
        ``qty >= 1`` raise :class:`ContractTooExpensive` and
        persist a rejection row exactly like the single-leg path.

        The decision whether the multi-strike entry should fire at
        all comes from the **play card** (via
        ``liquidity_classification``); the executor only enforces
        the cap split mechanic.

        Returns
        -------
        list[str]
            One broker-assigned alpaca_order_id per leg, in
            submission order.

        Raises
        ------
        ContractTooExpensive
            If any leg's per-contract cost exceeds the per-leg cap.
            The leg in question persists a rejection row; legs
            already submitted retain their own rows so the
            ``orders`` table never silently rolls back.
        UnsupportedOrderShape
            If a leg fails the standard single-leg validator (e.g.
            missing symbol, qty <= 0, unsupported option_type).
            Subsequent legs are NOT submitted once the exception is
            raised.
        """
        legs_list = list(play_card.get("option_legs") or [])
        if len(legs_list) != 2:
            # Defensive — _is_multi_strike already enforced length 2,
            # but this guard makes the helper safe to call directly.
            raise UnsupportedOrderShape(
                "multi-strike entry requires exactly 2 legs; got "
                f"{len(legs_list)}"
            )

        cap = _config.get_risk_per_play_usd()
        per_leg_cap = max(int(cap) // len(legs_list), 1)

        play_card_id = play_card.get("play_card_id")
        logger.info(
            "paper_executor.multi_strike_dispatch play_card_id=%s "
            "legs=%d cap_per_play=%s per_leg_cap=%s",
            play_card_id,
            len(legs_list),
            cap,
            per_leg_cap,
        )

        order_ids: list[str] = []
        for index, leg in enumerate(legs_list):
            if not isinstance(leg, Mapping):
                raise UnsupportedOrderShape(
                    f"option_legs[{index}] must be a dict for multi-strike; "
                    f"got {type(leg).__name__}"
                )

            # Pre-size the leg against the per-leg cap so the combined
            # notional respects the original cap. Legs that already
            # carry an explicit ``qty`` honour the caller's choice
            # (they may have been pre-sized upstream).
            sized_leg = dict(leg)
            has_quote = (
                sized_leg.get("bid") is not None
                or sized_leg.get("ask") is not None
            )
            has_explicit_qty = sized_leg.get("qty") is not None
            if has_quote and not has_explicit_qty:
                synthetic_card = {
                    "play_card_id": play_card_id,
                    "option_legs": [sized_leg],
                }
                sized_qty = size_position(
                    synthetic_card, risk_per_play_usd=per_leg_cap
                )
                if sized_qty == 0:
                    bid = _coerce_quote(sized_leg.get("bid"))
                    ask = _coerce_quote(sized_leg.get("ask"))
                    mid = (bid + ask) / 2.0 if (bid + ask) > 0 else 0.0
                    msg = (
                        f"ContractTooExpensive (multi-strike leg "
                        f"{index}): mid=${mid:.2f} (bid={bid}, "
                        f"ask={ask}) implies ${mid * 100:.2f} per "
                        f"contract, exceeds per-leg cap "
                        f"${per_leg_cap}"
                    )
                    logger.warning(
                        "paper_executor.multi_strike_contract_too_expensive "
                        "play_card_id=%s leg=%d symbol=%s mid=%.4f "
                        "per_leg_cap=%s",
                        play_card_id,
                        index,
                        sized_leg.get("symbol"),
                        mid,
                        per_leg_cap,
                    )
                    self._persist_order_row(
                        order_id=uuid.uuid4().hex,
                        play_card_id=play_card_id,
                        alpaca_order_id=None,
                        symbol=str(sized_leg.get("symbol") or "") or None,
                        side=str(sized_leg.get("side") or "buy"),
                        qty=0,
                        status="rejected",
                        reason=msg,
                        event=play_card.get("event"),
                        parent_play_card_id=play_card.get(
                            "parent_play_card_id"
                        ),
                    )
                    raise ContractTooExpensive(msg)
                sized_leg["qty"] = sized_qty

            # Build a synthetic single-leg play card and dispatch
            # through :meth:`execute` so ALL the standard guardrails
            # (paper-only check, concurrency cap, deployed cap,
            # validation, persistence) apply to each leg uniformly.
            synthetic_card = {
                **{
                    k: v
                    for k, v in play_card.items()
                    if k not in {"option_legs", "liquidity_classification"}
                },
                "option_legs": [sized_leg],
            }
            result = self.execute(synthetic_card)
            # ``result`` is a ``str`` since the synthetic card is
            # single-leg; the recursive _is_multi_strike check
            # therefore returns False.
            if isinstance(result, list):
                # Defensive — should never happen but keep the
                # contract honest.
                order_ids.extend(result)
            else:
                order_ids.append(str(result))

        return order_ids

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
