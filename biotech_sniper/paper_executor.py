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
from biotech_sniper import hold_policy as _hold_policy
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
    "_maybe_demote_unfillable_to_single_leg",
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


def _leg_cost_estimate(leg: Mapping[str, Any]) -> float:
    """Return a non-negative cost-per-contract estimate for ``leg``.

    Used by :func:`_maybe_demote_unfillable_to_single_leg` to pick
    the **cheaper** of two unfillable legs. Resolution order:

    * ``mid = (bid + ask) / 2`` when both bid and ask are populated
    * ``bid`` or ``ask`` (whichever is populated, one-sided quote)
    * ``limit_price`` if numeric and > 0
    * ``float('inf')`` so a leg with no price information at all
      sorts AFTER every priced leg (we never prefer it).
    """
    if not isinstance(leg, Mapping):
        return float("inf")
    bid = _coerce_quote(leg.get("bid"))
    ask = _coerce_quote(leg.get("ask"))
    if bid > 0.0 and ask > 0.0:
        return (bid + ask) / 2.0
    if bid > 0.0 or ask > 0.0:
        return bid + ask  # the populated side
    try:
        limit = float(leg.get("limit_price") or 0.0)
    except (TypeError, ValueError):
        limit = 0.0
    if limit > 0.0:
        return limit
    return float("inf")


def _maybe_demote_unfillable_to_single_leg(
    play_card: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    """Return a synthesized 1-leg play card when ``play_card`` should demote.

    Demotion rules (f-m3-16):

    * The card carries ``option_legs`` of length **exactly 2**.
    * Both legs are ``side='buy'`` (case-insensitive).
    * ``liquidity_classification`` (case-insensitive) is in
      :data:`MULTI_STRIKE_BLOCKED_CLASSIFICATIONS` (currently just
      ``'unfillable'``).
    * No ``order_class`` value other than ``'simple'``.

    Returns:

    * A new ``dict`` shaped like ``play_card`` but with a single
      leg — the **cheaper** of the two — when all rules above match.
      The ``liquidity_classification`` is dropped from the demoted
      card so downstream logic does not re-trigger the demotion path
      (single-leg cards are unaffected by the classification, but
      keeping it is misleading).
    * ``None`` when the demotion does not apply — the caller should
      proceed with the original card unchanged.

    The helper also emits a structured ``WARNING`` log naming the
    demotion reason and the discarded leg, satisfying the f-m3-16
    contract that operators see *why* the multi-strike entry was
    converted to a single submission.
    """
    legs = play_card.get("option_legs")
    if (
        not isinstance(legs, Sequence)
        or isinstance(legs, (str, bytes))
        or len(legs) != 2
    ):
        return None
    for leg in legs:
        if not isinstance(leg, Mapping):
            return None
        side = str(leg.get("side", "buy")).strip().lower()
        if side != "buy":
            return None

    order_class = play_card.get("order_class")
    if order_class is not None and str(order_class).strip().lower() != "simple":
        return None

    classification = play_card.get("liquidity_classification")
    if not (
        isinstance(classification, str)
        and classification.strip().lower()
        in MULTI_STRIKE_BLOCKED_CLASSIFICATIONS
    ):
        return None

    leg_a, leg_b = legs[0], legs[1]
    cost_a = _leg_cost_estimate(leg_a)
    cost_b = _leg_cost_estimate(leg_b)
    # Prefer the cheaper leg. On tie, keep the first leg
    # (deterministic — matches the play-card builder's natural
    # ordering and avoids flapping between runs).
    if cost_b < cost_a:
        kept_leg, dropped_leg = leg_b, leg_a
        kept_index = 1
    else:
        kept_leg, dropped_leg = leg_a, leg_b
        kept_index = 0

    play_card_id = play_card.get("play_card_id")
    classification_normalised = classification.strip().lower()
    logger.warning(
        "paper_executor.unfillable_demotion play_card_id=%s "
        "classification=%s kept_leg=%d kept_symbol=%s "
        "kept_cost_estimate=%.4f dropped_symbol=%s "
        "dropped_cost_estimate=%.4f reason=%s",
        play_card_id,
        classification_normalised,
        kept_index,
        kept_leg.get("symbol") if isinstance(kept_leg, Mapping) else None,
        cost_a if kept_index == 0 else cost_b,
        dropped_leg.get("symbol") if isinstance(dropped_leg, Mapping) else None,
        cost_b if kept_index == 0 else cost_a,
        f"liquidity_classification={classification_normalised!r} forces "
        "single-leg submission; discarding the more expensive leg",
    )

    # Build the demoted card. ``liquidity_classification`` is
    # stripped so downstream consumers (e.g. recursive validators)
    # don't see a single-leg card carrying a multi-strike-only flag.
    demoted: dict[str, Any] = {
        k: v for k, v in play_card.items()
        if k not in {"option_legs", "liquidity_classification"}
    }
    demoted["option_legs"] = [dict(kept_leg)]
    return demoted


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

    # f-m3-14: one-sided quote handling. When BOTH bid and ask are
    # populated, ``mid = (bid + ask) / 2`` is the standard fillable
    # estimate. When only ONE side is present, falling back to
    # ``(bid + ask) / 2`` would HALVE the per-contract cost (the
    # missing side coerces to 0) and oversize the position — e.g.
    # bid=$2.00 ask=None would yield mid=$1.00 → 2 contracts @
    # $400 cost, blowing the $250 per-play cap. We instead use the
    # populated side directly (equivalently ``max(bid, ask)`` since
    # the missing side is always ``0`` after coercion). This is the
    # conservative estimate — single-sided quotes always overstate
    # the worst-case cost rather than understating it.
    if bid > 0.0 and ask > 0.0:
        mid = (bid + ask) / 2.0
    else:
        mid = max(bid, ask)
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


#: f-m3-09 exit-event values that map to ``paper_orders.purpose='exit'``.
#: ``'open'`` and ``None`` map to ``'entry'``.
_EXIT_EVENT_VALUES: frozenset[str] = frozenset(
    {"iv_crush_exit", "stop_loss", "adverse_news", "rotation"}
)


def _infer_purpose(play_card: Mapping[str, Any]) -> Optional[str]:
    """Return the f-m3-11 ``purpose`` value implied by ``play_card``.

    Resolution order:

    1. Explicit ``play_card['purpose']`` if set to one of
       ``'entry'``, ``'exit'``, ``'liquidity_probe'``.
    2. Otherwise mapped from ``play_card['event']``:

       * ``'iv_crush_exit'`` / ``'stop_loss'`` / ``'adverse_news'`` /
         ``'rotation'`` → ``'exit'``.
       * ``'open'`` → ``'entry'``.
       * any other / missing → ``'entry'`` (the default for all
         non-tagged entry submissions).

    Returns ``None`` only when the play card explicitly carries an
    unrecognised ``purpose`` value (defensive — the persistence
    layer's CHECK constraint allows NULL but rejects unknown
    strings).
    """
    explicit = play_card.get("purpose")
    if isinstance(explicit, str):
        normalised = explicit.strip().lower()
        if normalised in {"entry", "exit", "liquidity_probe"}:
            return normalised
        return None
    event = play_card.get("event")
    if isinstance(event, str):
        if event in _EXIT_EVENT_VALUES:
            return "exit"
        if event == "open":
            return "entry"
    return "entry"


def _compute_mid_at_submit(leg: Mapping[str, Any]) -> Optional[float]:
    """Return the option mid implied by ``leg``'s bid/ask quote.

    The mid is ``(bid + ask) / 2`` when both are populated. A
    one-sided quote (only bid OR ask) returns the populated value
    (a defensive heuristic — the slippage computation will treat
    this as the best-known submit-time benchmark). When both bid
    and ask are missing, the helper falls back to ``limit_price``
    if present, and finally to ``None`` (no mid recoverable).

    Returns ``None`` only when neither side of the quote nor the
    limit price is available; the caller still writes the
    ``paper_orders`` row but ``requested_mid_at_submit`` stays NULL
    and the slippage computation downstream is skipped.
    """
    bid = _coerce_quote(leg.get("bid"))
    ask = _coerce_quote(leg.get("ask"))
    if bid > 0.0 and ask > 0.0:
        return (bid + ask) / 2.0
    if bid > 0.0 or ask > 0.0:
        return bid + ask  # one-sided
    try:
        limit = float(leg.get("limit_price") or 0.0)
    except (TypeError, ValueError):
        return None
    if limit > 0.0:
        return limit
    return None


def _derive_client_order_id(
    play_card: Mapping[str, Any],
    leg: Mapping[str, Any],
    *,
    fallback_seed: Optional[str] = None,
) -> str:
    """Return the ``client_order_id`` to stamp on the new ``paper_orders`` row.

    Resolution order:

    1. Explicit ``play_card['client_order_id']`` (preferred — the
       caller already computed a deterministic id, e.g. an exit
       trigger via :func:`hold_policy.make_exit_client_order_id`).
    2. Explicit ``leg['client_order_id']`` (legacy callers that
       stamp the id on the leg directly).
    3. Auto-generated ``f"auto-{fallback_seed or uuid.uuid4().hex}"``
       so each submission still satisfies the
       ``NOT NULL UNIQUE`` constraint without forcing every
       legacy caller to supply an id explicitly.

    The auto-generated form is intentionally distinguishable from
    a deterministic id so audit consumers can tell at a glance
    which orders went through the f-m3-11 idempotency-aware path
    versus the auto-id fallback.
    """
    explicit = play_card.get("client_order_id") or leg.get("client_order_id")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    seed = fallback_seed or uuid.uuid4().hex
    return f"auto-{seed}"


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
        client_order_id: str,
        requested_mid_at_submit: Optional[float] = None,
        purpose: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> None:
        """Insert (or replace) a row in the ``paper_orders`` table.

        ``id`` is the executor-generated UUID4; ``alpaca_order_id`` is
        the broker-assigned id (NULL on rejection paths). The replace
        semantics on the PK keep the helper idempotent: if a caller
        retries with the same internal ``id`` (rare; only happens if
        the executor is re-invoked after a partial crash) the row is
        overwritten rather than duplicated.

        f-m3-11 augmentation: ``client_order_id`` is required (the
        ``paper_orders.client_order_id`` column is ``NOT NULL UNIQUE``);
        ``requested_mid_at_submit`` and ``purpose`` are nullable but
        strongly encouraged for all new submissions so downstream
        slippage analytics has the data it needs.

        ``created_at`` is optional; when provided the helper preserves
        it (used by :meth:`_update_order_after_submit` to keep the
        same row's submit-time timestamp through the lifecycle).
        """
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO paper_orders (
                    id, play_card_id, alpaca_order_id, symbol, side,
                    qty, status, reason, event, parent_play_card_id,
                    requested_mid_at_submit, purpose, client_order_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    requested_mid_at_submit,
                    purpose,
                    client_order_id,
                    created_at if created_at is not None else _utc_now_iso(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _lookup_by_client_order_id(
        self, client_order_id: str
    ) -> Optional[dict[str, Any]]:
        """Return the persisted ``paper_orders`` row matching ``client_order_id``.

        Used by :meth:`execute` to enforce the f-m3-11 idempotency
        invariant: a duplicate write-then-submit attempt on the same
        ``client_order_id`` short-circuits without contacting the
        broker. Rejected rows are NOT excluded — a previous local
        rejection (cap-exceeded etc.) blocks a retry under the same
        deterministic id; the caller supplies a fresh client_order_id
        when retrying after an explicit policy adjustment.
        """
        if not client_order_id:
            return None
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                SELECT id, play_card_id, alpaca_order_id, symbol, side,
                       qty, status, reason, event, parent_play_card_id,
                       requested_mid_at_submit, purpose, client_order_id,
                       created_at
                FROM paper_orders
                WHERE client_order_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (client_order_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def _record_rejection_event(
        self,
        internal_id: str,
        reason: str,
    ) -> None:
        """Best-effort record of a ``rejected`` execution_events row.

        f-cross-04: every ``paper_orders`` rejection path also writes
        a corresponding ``execution_events`` row with
        ``event_type='rejected'`` so the telemetry stream is
        traceable end-to-end (VAL-CROSS-043). The transition
        ``_INITIAL_STATE → 'rejected'`` is permitted by
        :data:`biotech_sniper.execution_subscriber.LEGAL_TRANSITIONS`
        — the rejected event becomes the first (and last) lifecycle
        row when no ``submitted`` event was ever recorded for the
        order (cap-exceeded, broker AlpacaClientError before the
        accept lifecycle event, etc.).

        Telemetry failure NEVER breaks the rejection path. The
        caller has already raised the relevant
        :class:`PaperExecutorError`; we swallow any exception here
        and log via :meth:`logging.Logger.exception` so the
        operator sees the failure but the order rejection still
        propagates cleanly. This mirrors the best-effort pattern
        used by the successful-submit telemetry below.
        """
        try:
            from biotech_sniper.execution_subscriber import (
                record_execution_event as _record_execution_event,
            )
            _record_execution_event(
                self.db_path,
                paper_order_id=internal_id,
                event_type="rejected",
                event_at=_utc_now_iso(),
                raw_payload={"reason": reason},
            )
        except Exception:  # pragma: no cover — telemetry is best-effort
            logger.exception(
                "paper_executor.rejection_event_record_failed "
                "internal_id=%s",
                internal_id,
            )

    def _update_order_after_submit(
        self,
        order_id: str,
        *,
        alpaca_order_id: Optional[str],
        status: str,
        qty: Optional[int],
        reason: Optional[str] = None,
    ) -> None:
        """Update the row written by :meth:`_persist_order_row` with broker outcome.

        Called by :meth:`execute` AFTER the Alpaca submission call
        returns. Preserves the original ``created_at`` (which is the
        write-then-submit timestamp; the f-m3-11 invariant requires
        ``paper_orders.created_at <= execution_events.event_at`` for
        the corresponding ``submitted`` event).
        """
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE paper_orders
                SET alpaca_order_id = ?, status = ?, qty = COALESCE(?, qty),
                    reason = ?
                WHERE id = ?
                """,
                (alpaca_order_id, status, qty, reason, order_id),
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

        # f-m3-16: unfillable demotion. A 2-leg BUY card stamped with
        # ``liquidity_classification='unfillable'`` previously fell
        # through to ``_validate_single_leg`` which rejected it as
        # ``UnsupportedOrderShape`` (>1 leg). The required behaviour
        # is to **demote** to single-leg by selecting the cheaper of
        # the two legs and submitting it as a normal single-strike
        # entry, with a structured WARNING log naming the demotion
        # reason. ``_is_multi_strike`` already returns False for the
        # unfillable classification (see f-m3-08), so we detect the
        # demotion here right before single-leg validation.
        demoted = _maybe_demote_unfillable_to_single_leg(play_card)
        if demoted is not None:
            play_card = demoted

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
                _internal_id = uuid.uuid4().hex
                self._persist_order_row(
                    order_id=_internal_id,
                    play_card_id=play_card_id,
                    alpaca_order_id=None,
                    symbol=str(leg_preview.get("symbol") or "") or None,
                    side=str(leg_preview.get("side") or "buy"),
                    qty=0,
                    status="rejected",
                    reason=msg,
                    event=event,
                    parent_play_card_id=parent_play_card_id,
                    client_order_id=_derive_client_order_id(
                        play_card, leg_preview, fallback_seed=_internal_id
                    ),
                    requested_mid_at_submit=mid if mid > 0 else None,
                    purpose=_infer_purpose(play_card),
                )
                # f-cross-04: emit the matching execution_events row
                # so VAL-CROSS-043 traceability holds (every
                # paper_orders rejection has a telemetry trail).
                self._record_rejection_event(_internal_id, msg)
                raise ContractTooExpensive(msg)

            # Inject the sized qty so the rest of the pipeline (the
            # standard ``_validate_single_leg`` + alpaca-py request
            # builder) sees a fully-populated leg without us needing
            # to mutate the caller's play_card.
            sized_leg = {**leg_preview, "qty": sized_qty}
            play_card = {**play_card, "option_legs": [sized_leg]}

        leg = _validate_single_leg(play_card)

        symbol = str(leg["symbol"])
        qty = int(leg["qty"])
        side = _coerce_side(leg.get("side", "buy"))
        tif = _coerce_tif(leg.get("time_in_force"))
        limit_price = leg.get("limit_price")
        side_str = side.value if hasattr(side, "value") else str(side)

        # f-m3-11: compute the mid snapshot, purpose and the
        # deterministic ``client_order_id`` up-front so every
        # persistence call (success OR rejection) carries the
        # augmented fields. ``internal_id`` is the SAME UUID4 for
        # the lifetime of this single execute() call so the
        # write-then-submit + post-submit UPDATE land on the same
        # paper_orders row.
        requested_mid_at_submit = _compute_mid_at_submit(leg)
        purpose = _infer_purpose(play_card)
        internal_id = uuid.uuid4().hex
        client_order_id = _derive_client_order_id(
            play_card, leg, fallback_seed=internal_id
        )

        # f-m3-11 idempotency: if a non-rejected paper_orders row
        # already exists for this client_order_id, short-circuit.
        # Same-day re-submissions of the same logical exit / probe
        # never double-submit to the broker. Note: this lookup runs
        # AFTER the cap checks and sizing rejection above so a
        # transient cap-rejected row does not block a later retry
        # under a fresh client_order_id.
        existing = self._lookup_by_client_order_id(client_order_id)
        if existing is not None and (
            (existing.get("status") or "").lower() != "rejected"
        ):
            logger.info(
                "paper_executor.idempotent_skip client_order_id=%s "
                "existing_status=%s play_card_id=%s",
                client_order_id,
                existing.get("status"),
                play_card_id,
            )
            return existing.get("alpaca_order_id") or ""

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
                        order_id=internal_id,
                        play_card_id=play_card_id,
                        alpaca_order_id=None,
                        symbol=symbol,
                        side=side_str,
                        qty=qty,
                        status="rejected",
                        reason=f"OrderRejected: {reason}",
                        event=event,
                        parent_play_card_id=parent_play_card_id,
                        client_order_id=client_order_id,
                        requested_mid_at_submit=requested_mid_at_submit,
                        purpose=purpose,
                    )
                    # f-cross-04: VAL-CROSS-043 traceability.
                    self._record_rejection_event(
                        internal_id, f"OrderRejected: {reason}"
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
                order_id=internal_id,
                play_card_id=play_card_id,
                alpaca_order_id=None,
                symbol=symbol,
                side=side_str,
                qty=qty,
                status="rejected",
                reason=msg,
                event=event,
                parent_play_card_id=parent_play_card_id,
                client_order_id=client_order_id,
                requested_mid_at_submit=requested_mid_at_submit,
                purpose=purpose,
            )
            # f-cross-04: VAL-CROSS-043 traceability.
            self._record_rejection_event(internal_id, msg)
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
                order_id=internal_id,
                play_card_id=play_card_id,
                alpaca_order_id=None,
                symbol=symbol,
                side=side_str,
                qty=qty,
                status="rejected",
                reason=msg,
                event=event,
                parent_play_card_id=parent_play_card_id,
                client_order_id=client_order_id,
                requested_mid_at_submit=requested_mid_at_submit,
                purpose=purpose,
            )
            # f-cross-04: VAL-CROSS-043 traceability.
            self._record_rejection_event(internal_id, msg)
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

        # f-m3-11 write-then-submit invariant (VAL-M3-069): persist a
        # ``paper_orders`` row with ``status='submitted'`` BEFORE
        # contacting Alpaca. The row carries the deterministic
        # ``client_order_id`` and the mid snapshot, so the
        # ``execution_subscriber`` (or a downstream slippage
        # computation) can always resolve back to the local row even
        # if the broker's response is delayed or lost. The row's
        # ``created_at`` defaults to the in-process UTC now, which is
        # the timestamp we compare against ``execution_events.event_at``
        # for the corresponding ``submitted`` event.
        submit_at_iso = _utc_now_iso()
        self._persist_order_row(
            order_id=internal_id,
            play_card_id=play_card_id,
            alpaca_order_id=None,
            symbol=symbol,
            side=side_str,
            qty=qty,
            status="submitted",
            reason=None,
            event=event,
            parent_play_card_id=parent_play_card_id,
            client_order_id=client_order_id,
            requested_mid_at_submit=requested_mid_at_submit,
            purpose=purpose,
            created_at=submit_at_iso,
        )

        try:
            order_dict = self.client.submit_order(order_request)
        except AlpacaClientError as exc:
            reason = _broker_reason(exc)
            logger.warning(
                "paper_executor.order_rejected play_card_id=%s symbol=%s "
                "qty=%s side=%s client_order_id=%s reason=%s",
                play_card_id,
                symbol,
                qty,
                side_str,
                client_order_id,
                reason,
            )
            # f-m3-11: update the existing write-then-submit row to
            # reflect the broker-side rejection. The row keeps its
            # original ``created_at`` so the chronological invariant
            # against ``execution_events`` is preserved.
            self._update_order_after_submit(
                internal_id,
                alpaca_order_id=None,
                status="rejected",
                qty=None,
                reason=reason,
            )
            # f-cross-04: emit the matching execution_events row so
            # VAL-CROSS-043 traceability holds. The 'submitted'
            # event was NEVER written for this order (broker
            # rejection short-circuits the success path at line
            # ~1633), so 'rejected' is the first lifecycle event
            # for this paper_orders row — permitted by the
            # _INITIAL_STATE → 'rejected' carve-out in
            # LEGAL_TRANSITIONS.
            self._record_rejection_event(internal_id, reason)
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
            self._update_order_after_submit(
                internal_id,
                alpaca_order_id=None,
                status="rejected",
                qty=None,
                reason=reason,
            )
            # f-cross-04: VAL-CROSS-043 traceability.
            self._record_rejection_event(internal_id, reason)
            raise OrderRejected(reason)

        broker_status = (order_dict.get("status") or "submitted").lower()
        broker_qty = order_dict.get("qty")
        try:
            persisted_qty = int(broker_qty) if broker_qty is not None else qty
        except (TypeError, ValueError):
            persisted_qty = qty

        self._update_order_after_submit(
            internal_id,
            alpaca_order_id=alpaca_order_id,
            status=broker_status,
            qty=persisted_qty,
        )

        # f-m3-11: write the corresponding ``execution_events`` row
        # for the broker-acknowledged ``submitted`` state. The
        # event_at is the same wall-clock instant as the broker's
        # response (or the ``submit_at_iso`` we recorded just before
        # the call, whichever is later). Downstream poll loops in
        # :mod:`biotech_sniper.execution_subscriber` will record
        # subsequent state transitions (accepted → filled etc.).
        try:
            from biotech_sniper.execution_subscriber import (
                record_execution_event as _record_execution_event,
            )
            _record_execution_event(
                self.db_path,
                paper_order_id=internal_id,
                event_type="submitted",
                event_at=_utc_now_iso(),
                raw_payload=order_dict,
            )
        except Exception:  # pragma: no cover — defensive
            # Telemetry failure must never break the order submission
            # path. The subscriber's poll loop will pick up the state
            # on the next iteration.
            logger.exception(
                "paper_executor.execution_event_record_failed "
                "internal_id=%s",
                internal_id,
            )

        logger.info(
            "paper_executor.order_submitted play_card_id=%s symbol=%s "
            "qty=%s side=%s alpaca_order_id=%s client_order_id=%s "
            "status=%s",
            play_card_id,
            symbol,
            persisted_qty,
            side_str,
            alpaca_order_id,
            client_order_id,
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
                    _internal_id = uuid.uuid4().hex
                    self._persist_order_row(
                        order_id=_internal_id,
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
                        client_order_id=_derive_client_order_id(
                            play_card,
                            sized_leg,
                            fallback_seed=_internal_id,
                        ),
                        requested_mid_at_submit=mid if mid > 0 else None,
                        purpose=_infer_purpose(play_card),
                    )
                    # f-cross-04: VAL-CROSS-043 traceability.
                    self._record_rejection_event(_internal_id, msg)
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

        # f-m3-19 fix #2: every observed broker status change must
        # land an ``execution_events`` row. The initial ``submitted``
        # event is written inside :meth:`execute` (the executor's
        # write-then-submit invariant); this loop covers everything
        # AFTER submission. We seed ``last_event_type`` from the DB
        # so the validator never trips when wait_for_fill is called
        # in a process where ``execute`` already wrote a state.
        paper_order_id = self._lookup_paper_order_id(order_id)
        last_event_type: Optional[str] = (
            self._latest_event_type(paper_order_id)
            if paper_order_id
            else None
        )

        last_status: Optional[str] = None
        last_order: dict[str, Any] = {}
        while True:
            order = self.client.get_order(order_id)
            status = (order.get("status") or "").lower()
            last_order = order

            if status and status != last_status:
                self._update_order_status(order_id, order)
                last_status = status

                # Emit the execution_events row for the observed
                # transition. Mapping is identical to the subscriber's
                # so the two writers stay in lock-step.
                if paper_order_id:
                    self._record_event_for_status(
                        paper_order_id=paper_order_id,
                        broker_status=status,
                        order=order,
                        last_event_type=last_event_type,
                    )
                    # Refresh ``last_event_type`` from the DB rather
                    # than guessing — keeps the loop honest if a
                    # concurrent subscriber poll lands an event in
                    # between our writes.
                    refreshed = self._latest_event_type(paper_order_id)
                    if refreshed is not None:
                        last_event_type = refreshed

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

    # ------------------------------------------------------------------
    # f-m3-09: hold-policy-gated exit submission
    # ------------------------------------------------------------------

    def submit_exit(
        self,
        play: Mapping[str, Any],
        event: str,
        *,
        today: Optional[Any] = None,
        sell_qty: Optional[int] = None,
    ) -> Optional[str]:
        """Submit an exit (``side='sell'``) for ``play`` tagged with ``event``.

        This is the f-m3-09 entry point that ALL specialised exit
        triggers (iv_crush_exit, stop_loss, adverse_news, rotation)
        funnel through. It guarantees:

        * ``event`` is one of the four allowed exit triggers
          (:data:`hold_policy.ALLOWED_EXIT_EVENTS`); anything else
          raises :class:`hold_policy.HoldPolicyViolation` BEFORE any
          network call.
        * On a non-catalyst date, the sell is refused unless the
          event is allowed (the hold-rule guard from VAL-M3-047).
        * ``client_order_id`` is forced to
          ``f"{ticker}-{event}-{date}"`` so the broker dedupes
          same-day re-submissions of the same exit.
        * Idempotency is ALSO enforced locally: if the ``orders``
          table already has a non-rejected row matching the same
          ``(ticker, event, date)`` triplet, the call returns the
          existing ``alpaca_order_id`` (or ``None`` when the persisted
          row was a sentinel) without contacting the broker.

        Parameters
        ----------
        play:
            Mapping describing the active position to exit. Required
            keys: ``ticker`` (string) and ``symbol`` (OCC option
            symbol). Optional but strongly recommended:
            ``play_card_id`` (string id of the entry play card —
            used as ``parent_play_card_id`` on the exit row),
            ``catalyst_date`` (ISO string — used by the hold guard),
            ``qty`` (current open contract count — defaults to
            ``sell_qty`` when supplied, otherwise must be present).
        event:
            One of :data:`hold_policy.ALLOWED_EXIT_EVENTS`.
        today:
            Override for the date used in the
            ``client_order_id`` and the hold-rule check. Defaults to
            UTC today.
        sell_qty:
            Optional explicit sell quantity. Defaults to the play's
            ``qty``. Must be ``>= 1``.

        Returns
        -------
        str | None
            The broker-assigned ``alpaca_order_id`` on a fresh
            submission, or the previously persisted id when the
            idempotency check short-circuits.
        """
        if event not in _hold_policy.ALLOWED_EXIT_EVENTS:
            raise _hold_policy.HoldPolicyViolation(
                f"submit_exit: event={event!r} is not an allowed exit "
                f"trigger; allowed events are "
                f"{sorted(_hold_policy.ALLOWED_EXIT_EVENTS)}"
            )

        ticker_raw = play.get("ticker") or play.get("symbol")
        if not isinstance(ticker_raw, str) or not ticker_raw.strip():
            raise UnsupportedOrderShape(
                "submit_exit: play['ticker'] must be a non-empty string"
            )
        ticker = ticker_raw.strip().upper()

        symbol_raw = play.get("symbol") or play.get("option_symbol")
        if not isinstance(symbol_raw, str) or not symbol_raw.strip():
            raise UnsupportedOrderShape(
                "submit_exit: play['symbol'] must be a non-empty OCC "
                "option symbol"
            )
        symbol = symbol_raw.strip()

        today_date = _hold_policy.coerce_date(today)
        catalyst_raw = (
            play.get("catalyst_date")
            or play.get("pdufa_date")
            or play.get("estimated_announcement")
        )

        # Gate the sell through hold_policy. The arbiter raises when
        # the event is not allowed for a non-catalyst date.
        _hold_policy.assert_exit_allowed(
            event=event,
            side="sell",
            catalyst_date=catalyst_raw,
            today=today_date,
            play_id=play.get("play_id") or play.get("play_card_id"),
        )

        # Resolve sell qty.
        if sell_qty is None:
            qty_raw = play.get("qty") or play.get("contracts") or play.get(
                "open_qty"
            )
            try:
                sell_qty = int(qty_raw) if qty_raw is not None else 0
            except (TypeError, ValueError):
                sell_qty = 0
        if not isinstance(sell_qty, int) or sell_qty < 1:
            raise UnsupportedOrderShape(
                f"submit_exit: sell_qty must be >= 1, got {sell_qty!r}"
            )

        client_order_id = _hold_policy.make_exit_client_order_id(
            ticker, event, today_date
        )
        parent_play_card_id = play.get("play_card_id")
        exit_play_card_id = _hold_policy.make_exit_play_card_id(
            parent_play_card_id, ticker, event, today_date
        )

        # Idempotency: if a non-rejected exit row already exists for
        # this (ticker, event, date), short-circuit. The lookup keys
        # off the deterministic ``exit_play_card_id`` which already
        # bakes in (parent, event, date) so a fresh call within the
        # same day finds the prior row exactly.
        existing = self._lookup_exit_by_play_card_id(
            exit_play_card_id, event=event
        )
        if existing is not None:
            logger.info(
                "paper_executor.submit_exit.idempotent_skip "
                "ticker=%s event=%s date=%s play_card_id=%s",
                ticker,
                event,
                today_date.isoformat(),
                parent_play_card_id,
            )
            return existing.get("alpaca_order_id")

        sell_card: dict[str, Any] = {
            "play_card_id": exit_play_card_id,
            "parent_play_card_id": parent_play_card_id,
            "ticker": ticker,
            "event": event,
            "client_order_id": client_order_id,
            "option_legs": [
                {
                    "symbol": symbol,
                    "side": "sell",
                    "qty": int(sell_qty),
                    "client_order_id": client_order_id,
                }
            ],
        }

        result = self.execute(sell_card)
        # ``execute`` returns ``str`` for single-leg cards. Defensive
        # coercion for the (impossible) list path keeps the contract
        # honest.
        if isinstance(result, list):
            return result[0] if result else None
        return result

    def _lookup_exit_by_play_card_id(
        self,
        exit_play_card_id: str,
        *,
        event: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Return the persisted ``orders`` row for ``exit_play_card_id``, if any.

        Used by :meth:`submit_exit` to short-circuit a duplicate
        submission. Rows with ``status='rejected'`` are NOT
        considered — a previous transient broker rejection should
        not block a retry. Optionally restricts the lookup by
        ``event`` so callers do not match an unrelated entry that
        coincidentally reused the same id namespace.

        The exact-match on ``play_card_id`` works because the f-m3-09
        helpers (:func:`hold_policy.make_exit_play_card_id` /
        :func:`hold_policy.make_exit_client_order_id`) bake the
        ``(parent, event, date)`` triplet into the id deterministically:
        a same-day re-submission produces the same string and matches
        exactly.
        """
        conn = self._connect()
        try:
            if event is not None:
                cursor = conn.execute(
                    """
                    SELECT id, alpaca_order_id, status, event,
                           parent_play_card_id, symbol, qty
                    FROM paper_orders
                    WHERE play_card_id = ?
                      AND event = ?
                      AND status != 'rejected'
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (exit_play_card_id, event),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT id, alpaca_order_id, status, event,
                           parent_play_card_id, symbol, qty
                    FROM paper_orders
                    WHERE play_card_id = ?
                      AND status != 'rejected'
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (exit_play_card_id,),
                )
            row = cursor.fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

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
                       requested_mid_at_submit, purpose, client_order_id,
                       created_at
                FROM paper_orders
                WHERE play_card_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (play_card_id,),
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # f-m3-12: liquidity-probe gated entry sizing
    # ------------------------------------------------------------------

    def size_entry(
        self,
        candidate: Mapping[str, Any],
        *,
        liquidity_probe_module: Any = None,
    ) -> dict[str, Any]:
        """Probe chain liquidity and return a sizing decision.

        This is the canonical entry point that the play-card builder
        / orchestrator calls BEFORE invoking :meth:`execute` on a
        real-entry candidate. It runs a 1-contract liquidity probe
        against the intended ``(ticker, expiry, strike)`` chain and
        translates the probe's classification into one of three
        actions:

        * ``"skip"``   — classification ``"unfillable"``: do NOT
          submit any real-entry order for this ticker today.
          ``play_card`` is ``None``.
        * ``"single"`` — classification ``"fillable"``: submit a
          single-strike full-size entry. ``play_card`` is the
          original candidate (or its ``play_card`` projection)
          unmodified.
        * ``"multi"``  — classification ``"partial"``: split the
          entry across 2 strikes. The returned ``play_card`` is the
          candidate with ``liquidity_classification="partial"``
          stamped on it so :meth:`execute` dispatches via the
          :func:`_is_multi_strike` path.

        Parameters
        ----------
        candidate:
            Mapping describing the proposed entry. Required keys:
            ``ticker``, ``expiry``, ``strike``. Optional: ``side``
            (defaults to ``"buy"``), ``play_card`` (the full play
            card to forward when classification permits), ``symbol``
            (the canonical OCC option symbol — preferred over the
            synthesised default).
        liquidity_probe_module:
            Optional override for the
            :mod:`biotech_sniper.liquidity_probe` module — tests
            inject a stub. Defaults to the real module.

        Returns
        -------
        dict
            ``{"action": "skip"|"single"|"multi",
              "classification": "...", "probe_result": ProbeResult,
              "play_card": <play card or None>}``.
        """
        if liquidity_probe_module is None:
            from biotech_sniper import liquidity_probe as liquidity_probe_module  # type: ignore[no-redef]

        ticker_raw = candidate.get("ticker")
        expiry_raw = candidate.get("expiry")
        strike_raw = candidate.get("strike")
        side_raw = candidate.get("side") or "buy"
        if not (
            isinstance(ticker_raw, str)
            and ticker_raw.strip()
            and isinstance(expiry_raw, str)
            and expiry_raw.strip()
            and strike_raw is not None
        ):
            raise UnsupportedOrderShape(
                "size_entry: candidate must include non-empty 'ticker', "
                "'expiry', and 'strike'"
            )

        try:
            strike_val = float(strike_raw)
        except (TypeError, ValueError):
            raise UnsupportedOrderShape(
                f"size_entry: strike must be numeric; got {strike_raw!r}"
            )

        # f-m3-20 fix (2): prefer the candidate's resolved option-leg
        # symbol over the bare underlying ticker. The play-card
        # builder stores the canonical OCC option symbol on
        # ``candidate['play_card']['option_legs'][0]['symbol']`` —
        # for a put-card candidate that symbol encodes ``P`` (put);
        # the previous code passed only ``candidate['symbol']``
        # (often missing) into :func:`probe_chain`, which then fell
        # through to :func:`_build_probe_symbol` and synthesised a
        # CALL symbol from ``side='buy'``. That broke probes for
        # put cards (we'd be probing the wrong contract). We now
        # walk the play_card → option_legs[0]['symbol'] chain and
        # only fall back to ``candidate['symbol']`` when no leg
        # symbol is available.
        play_card_in_for_symbol: Optional[Mapping[str, Any]] = (
            candidate.get("play_card")
            if isinstance(candidate.get("play_card"), Mapping)
            else None
        )
        leg_symbol: Optional[str] = None
        if play_card_in_for_symbol is not None:
            legs_in = play_card_in_for_symbol.get("option_legs")
            if (
                isinstance(legs_in, Sequence)
                and not isinstance(legs_in, (str, bytes))
                and len(legs_in) > 0
                and isinstance(legs_in[0], Mapping)
            ):
                raw_leg_symbol = legs_in[0].get("symbol")
                if isinstance(raw_leg_symbol, str) and raw_leg_symbol.strip():
                    leg_symbol = raw_leg_symbol.strip()

        candidate_symbol_raw = candidate.get("symbol")
        candidate_symbol: Optional[str] = (
            candidate_symbol_raw.strip()
            if isinstance(candidate_symbol_raw, str)
            and candidate_symbol_raw.strip()
            else None
        )
        symbol_override = leg_symbol or candidate_symbol
        try:
            probe_result = liquidity_probe_module.probe_chain(
                ticker_raw,
                expiry_raw,
                strike_val,
                str(side_raw),
                alpaca_client=self.client,
                db_path=self.db_path,
                poll_interval_seconds=self.poll_interval_seconds,
                symbol_override=symbol_override,
            )
        except liquidity_probe_module.DailyCapExceeded as exc:
            # Daily cap is treated like ``"unfillable"`` for sizing
            # purposes — the safest fallback when we cannot probe.
            logger.warning(
                "paper_executor.size_entry.daily_cap_exceeded "
                "ticker=%s reason=%s",
                ticker_raw,
                exc,
            )
            return {
                "action": "skip",
                "classification": "unfillable",
                "probe_result": None,
                "play_card": None,
                "reason": "liquidity_probe_daily_cap_exceeded",
            }

        classification = probe_result.classification
        play_card_in: Optional[Mapping[str, Any]] = candidate.get(
            "play_card"
        ) if isinstance(candidate.get("play_card"), Mapping) else None

        if classification == "unfillable":
            return {
                "action": "skip",
                "classification": classification,
                "probe_result": probe_result,
                "play_card": None,
            }

        # Build the outgoing play card. The caller may pass the full
        # play card on ``candidate['play_card']``; otherwise fall
        # through to ``candidate`` itself (already conformant with
        # :meth:`execute`'s expected shape if it carries
        # ``option_legs``).
        outgoing: dict[str, Any] = dict(
            play_card_in if play_card_in is not None else candidate
        )

        if classification == "partial":
            outgoing["liquidity_classification"] = "partial"
            action = "multi"
        else:  # "fillable"
            outgoing["liquidity_classification"] = "fillable"
            action = "single"

        # f-m3-20 fix (3): normalise the leg count so the play card's
        # actual ``option_legs`` length matches the classification
        # action. The downstream ``execute()`` dispatcher uses
        # :func:`_is_multi_strike` (which inspects leg count) to
        # decide multi-strike vs single-strike fan-out, so when the
        # probe classifies the chain as ``fillable`` (action=
        # ``single``) but the original card carries 2 legs, we MUST
        # truncate to 1 leg or the executor will silently multi-fan
        # the entry — diverging from the probe's intent. Symmetrically,
        # a ``partial`` classification (action=``multi``) on a 1-leg
        # card cannot be honoured (there's no second leg to split
        # to); we log a WARNING and leave the card as a single-leg
        # entry so :func:`_is_multi_strike` returns ``False`` and
        # the executor submits a single-strike order.
        legs_out = outgoing.get("option_legs")
        if (
            isinstance(legs_out, Sequence)
            and not isinstance(legs_out, (str, bytes))
        ):
            n_legs = len(legs_out)
            if action == "single" and n_legs > 1:
                # Pick the cheaper of the legs (deterministic on tie:
                # keep the first leg). Mirrors the demotion logic in
                # :func:`_maybe_demote_unfillable_to_single_leg`.
                cheapest_index = 0
                cheapest_cost = (
                    _leg_cost_estimate(legs_out[0])
                    if isinstance(legs_out[0], Mapping)
                    else float("inf")
                )
                for idx in range(1, n_legs):
                    leg_i = legs_out[idx]
                    if not isinstance(leg_i, Mapping):
                        continue
                    cost_i = _leg_cost_estimate(leg_i)
                    if cost_i < cheapest_cost:
                        cheapest_cost = cost_i
                        cheapest_index = idx
                kept_leg = legs_out[cheapest_index]
                logger.info(
                    "paper_executor.size_entry.truncate_to_single "
                    "play_card_id=%s classification=%s "
                    "kept_index=%d kept_symbol=%s n_legs_before=%d",
                    outgoing.get("play_card_id"),
                    classification,
                    cheapest_index,
                    kept_leg.get("symbol")
                    if isinstance(kept_leg, Mapping)
                    else None,
                    n_legs,
                )
                outgoing["option_legs"] = [
                    dict(kept_leg) if isinstance(kept_leg, Mapping) else kept_leg
                ]
            elif action == "multi" and n_legs <= 1:
                logger.warning(
                    "paper_executor.size_entry.partial_on_single_leg "
                    "play_card_id=%s classification=%s n_legs=%d "
                    "reason=partial_classification_on_single_leg_card; "
                    "leaving card as single-strike entry",
                    outgoing.get("play_card_id"),
                    classification,
                    n_legs,
                )
                # Card stays single-leg; ``_is_multi_strike`` will
                # therefore return False and ``execute()`` will
                # dispatch the single-leg path. We deliberately
                # leave ``action='multi'`` and
                # ``liquidity_classification='partial'`` on the
                # decision dict so the caller can see that the
                # probe classified the chain as partial — but the
                # actual card shape forces a single-strike entry.

        return {
            "action": action,
            "classification": classification,
            "probe_result": probe_result,
            "play_card": outgoing,
        }

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
                "UPDATE paper_orders SET status = ? WHERE alpaca_order_id = ?",
                (status, alpaca_order_id),
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # f-m3-19: telemetry emission from wait_for_fill.
    # ------------------------------------------------------------------

    def _lookup_paper_order_id(self, alpaca_order_id: str) -> Optional[str]:
        """Resolve the local ``paper_orders.id`` for an Alpaca order id.

        ``wait_for_fill`` only knows the broker-side ``alpaca_order_id``;
        the ``execution_events`` writer needs the local
        ``paper_orders.id`` (the executor-generated UUID4 the
        write-then-submit row carries). The lookup is restricted to
        rows whose ``alpaca_order_id`` is non-NULL/non-empty so a
        rejected stub row never matches.

        Returns ``None`` when no row exists yet — wait_for_fill is
        sometimes invoked from tests that never went through
        :meth:`execute` (i.e. the cassette-driven poll loop in
        ``test_paper_executor.py``). In that case telemetry is
        silently skipped; the test only asserts on
        ``paper_orders.status``.
        """
        if not alpaca_order_id:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT id FROM paper_orders
                WHERE alpaca_order_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (alpaca_order_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        if isinstance(row, sqlite3.Row):
            return row["id"]
        return row[0]

    def _latest_event_type(self, paper_order_id: str) -> Optional[str]:
        """Return the most recent ``execution_events.event_type`` for the order.

        Used to dedup repeat status reports from the broker (the
        bounded poll loop sees the same ``accepted`` payload until
        it ticks over to ``filled``; we must not write a fresh
        ``accepted`` row on every iteration).
        """
        if not paper_order_id:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT event_type FROM execution_events
                WHERE paper_order_id = ?
                ORDER BY event_at DESC, id DESC
                LIMIT 1
                """,
                (paper_order_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        if isinstance(row, sqlite3.Row):
            return row["event_type"]
        return row[0]

    def _record_event_for_status(
        self,
        *,
        paper_order_id: str,
        broker_status: str,
        order: Mapping[str, Any],
        last_event_type: Optional[str],
    ) -> None:
        """Translate a broker status into ``execution_events`` (+ fills).

        Mirrors :meth:`ExecutionSubscriber.poll_once` so the two
        writers stay in lock-step regardless of which path observes
        the broker first. The mapping table
        :data:`biotech_sniper.execution_subscriber._BROKER_STATUS_TO_EVENT_TYPE`
        is the single source of truth.

        Behaviour:

        * Unmapped statuses (``new``, ``done_for_day`` aliases, etc.
          not in the canonical enum) are silently dropped — the
          subscriber will pick the canonical lifecycle up on its
          next tick.
        * Repeat events (same ``event_type`` as the last persisted
          event) are skipped EXCEPT for ``partial_fill`` which is
          deliberately allowed to repeat (each broker-emitted
          partial fill is a distinct event).
        * For ``partial_fill`` / ``filled``: writes BOTH
          ``execution_fills`` (slippage / time-to-fill) AND the
          matching ``execution_events`` row via
          :func:`biotech_sniper.execution_fills.record_fill`. Falls
          back to the events-only writer when the broker payload
          lacks fill metrics or the parent row has no
          ``requested_mid_at_submit`` to compute slippage against.
        * For all other transitions: writes only the
          ``execution_events`` row.

        Failures during telemetry MUST NOT break the order
        lifecycle; the wait_for_fill caller cares about the
        broker-side outcome, not the audit trail. We swallow
        :class:`IllegalStateTransition` (the validator surfaces it
        as ERROR; the subscriber will replay the canonical sequence
        next tick) and any unexpected exception (logged, no
        re-raise).
        """
        # Lazy imports keep the paper_executor module importable
        # without forcing the f-m3-11 telemetry modules to load
        # eagerly (they pull in db migrations, paths.py, etc.).
        from biotech_sniper.execution_subscriber import (
            _BROKER_STATUS_TO_EVENT_TYPE,
            IllegalStateTransition as _IllegalStateTransition,
            record_execution_event as _record_execution_event,
        )
        from biotech_sniper.execution_fills import (
            FillContextMissing as _FillContextMissing,
            record_fill as _record_fill,
        )

        event_type = _BROKER_STATUS_TO_EVENT_TYPE.get(broker_status)
        if event_type is None:
            logger.debug(
                "paper_executor.wait_for_fill.unmapped_status "
                "paper_order_id=%s broker_status=%s",
                paper_order_id,
                broker_status,
            )
            return

        # Dedup: skip if the broker repeats the same status. partial_fill
        # is exempt because each partial fill is a distinct event row.
        if event_type == last_event_type and event_type != "partial_fill":
            return

        event_at = (
            order.get("filled_at")
            or order.get("updated_at")
            or order.get("submitted_at")
            or _utc_now_iso()
        )

        try:
            if event_type in ("partial_fill", "filled"):
                filled_price = order.get("filled_avg_price")
                filled_qty_raw = order.get("filled_qty")
                if filled_price is None or filled_qty_raw in (None, 0, "0"):
                    # Broker reports a fill status without populated
                    # fill metrics (e.g., the cassette ticked over
                    # to "filled" but the subsequent payload still
                    # carries the intermediate fill_qty=0). Fall
                    # back to the events-only writer so the
                    # lifecycle row still lands.
                    _record_execution_event(
                        self.db_path,
                        paper_order_id=paper_order_id,
                        event_type=event_type,
                        event_at=event_at,
                        raw_payload=order,
                    )
                    return
                try:
                    filled_qty_int = int(float(filled_qty_raw))
                except (TypeError, ValueError):
                    filled_qty_int = 0
                # Look up the parent qty so we can compute the
                # remaining qty for partial fills.
                parent_qty: Optional[int] = None
                conn = self._connect()
                try:
                    qrow = conn.execute(
                        "SELECT qty FROM paper_orders WHERE id = ?",
                        (paper_order_id,),
                    ).fetchone()
                finally:
                    conn.close()
                if qrow is not None:
                    raw = (
                        qrow["qty"]
                        if isinstance(qrow, sqlite3.Row)
                        else qrow[0]
                    )
                    try:
                        parent_qty = (
                            int(raw) if raw is not None else None
                        )
                    except (TypeError, ValueError):
                        parent_qty = None
                partial_qty_remaining = (
                    0
                    if event_type == "filled"
                    else max(0, (parent_qty or 0) - filled_qty_int)
                )
                try:
                    _record_fill(
                        self.db_path,
                        paper_order_id=paper_order_id,
                        filled_price=float(filled_price),
                        filled_qty=filled_qty_int,
                        filled_at=event_at,
                        partial_qty_remaining=partial_qty_remaining,
                        raw_payload=order,
                    )
                except _FillContextMissing:
                    # Fall back to events-only when the parent row
                    # lacks the slippage context. The lifecycle
                    # stream stays complete; the validator will
                    # surface the missing fills row downstream.
                    logger.warning(
                        "paper_executor.wait_for_fill.fill_context_missing "
                        "paper_order_id=%s; recording event-only",
                        paper_order_id,
                    )
                    _record_execution_event(
                        self.db_path,
                        paper_order_id=paper_order_id,
                        event_type=event_type,
                        event_at=event_at,
                        raw_payload=order,
                    )
            else:
                _record_execution_event(
                    self.db_path,
                    paper_order_id=paper_order_id,
                    event_type=event_type,
                    event_at=event_at,
                    raw_payload=order,
                )
        except _IllegalStateTransition:
            # The runtime validator caught a non-canonical
            # transition (e.g., wait_for_fill picked up a 'filled'
            # without an intermediate 'accepted' because the broker
            # batched both into a single response). Logged at ERROR
            # so the operator sees it; the lifecycle row is dropped
            # rather than corrupting the validator's monotonic
            # invariant.
            logger.error(
                "paper_executor.wait_for_fill.illegal_transition "
                "paper_order_id=%s last=%s next=%s",
                paper_order_id,
                last_event_type,
                event_type,
            )
        except Exception:  # noqa: BLE001 — telemetry must not break execute()
            logger.exception(
                "paper_executor.wait_for_fill.event_record_failed "
                "paper_order_id=%s event_type=%s",
                paper_order_id,
                event_type,
            )
