"""Stage-2 wiring: dispatcher → :class:`PaperExecutor.execute`.

Feature: ``f-m3-10-paper-executor-wiring``.

This module is the bridge between the Stage-2 dispatcher (which
synthesises a single-leg ``news_event_entry`` play_card from a
post-fanout, post-gate ensemble result) and the existing
:class:`biotech_sniper.paper_executor.PaperExecutor` (which is the
sole paper-trading order entry point in the project).

The wiring itself is deliberately thin — every guardrail downstream
(paper-only base_url check, write-then-submit invariant, single-leg
validation, $250 sizing, options-only concurrency=3 / deployed=$750
caps, sell-bypass on exits) lives in :class:`PaperExecutor`. This
module only:

1. Consumes a :class:`DispatchResult` (or builds one via
   :func:`biotech_sniper.exec.stage2_dispatcher.build_play_card`).
2. Stamps the live chain quote (``bid``/``ask``) onto the leg so
   :func:`PaperExecutor.size_position` can derive
   ``qty = floor($250 / mid / 100)``.
3. Runs the defensive single-leg validator
   :func:`assert_single_leg_for_news_entry` so multi-strike-shaped
   cards never reach the broker (per VAL-M3-092).
4. Pre-checks underlying tradability (per VAL-M3-090): a halted /
   delisted underlying short-circuits with a typed
   :class:`UnderlyingUnavailable` BEFORE any ``paper_orders`` row is
   written and BEFORE any broker call is made — so Stage-2 can
   record the ``underlying_unavailable`` audit reason and AVOID
   advancing ``ticker_cooldown.last_entry_at``.
5. Hands the play_card off to :meth:`PaperExecutor.execute`. The
   executor's own :class:`ContractTooExpensive` propagates unchanged
   (per VAL-M3-091); :class:`ConcurrencyCapExceeded` /
   :class:`DeployedCapExceeded` continue to enforce the global
   options-only caps.

Validation contract assertions fulfilled
----------------------------------------

* **VAL-M3-053** — ``RISK_PER_PLAY_USD=$250`` per-play sizing.
* **VAL-M3-054** — Same global concurrency=3 / deployed=$750 caps as
  daily-curated entries.
* **VAL-M3-055** — Caps count OPTIONS positions only;
  pre-existing equities are ignored
  (delegated to the existing :func:`_options_positions` filter).
* **VAL-M3-056** — Sell-to-close orders bypass the caps (existing
  behaviour; the wiring never produces sell legs but the regression
  test pins the inherited semantics).
* **VAL-M3-058** — ``news_event_entry`` is distinct from ``open``;
  the dispatcher tags ``play_card['event']='news_event_entry'`` and
  the executor persists it verbatim.
* **VAL-M3-090** — Halted / delisted underlying short-circuits with
  :class:`UnderlyingUnavailable`; no ``paper_orders`` row is
  written; no broker call is made.
* **VAL-M3-091** — ``$250`` sizing → ``qty=0`` raises
  :class:`biotech_sniper.paper_executor.ContractTooExpensive`
  (re-exported here for caller convenience).
* **VAL-M3-092** — News-event entry is single-leg only; multi-strike
  shapes raise :class:`UnsupportedMultiStrikeForNewsEntry`.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from biotech_sniper.alpaca_client import AlpacaClientError
from biotech_sniper.exec.stage2_dispatcher import (
    EVENT_NEWS_ENTRY,
    DispatcherError,
    DispatchResult,
    UnsupportedMultiStrikeForNewsEntry,
    assert_single_leg_for_news_entry,
    build_play_card,
)
from biotech_sniper.llm.ensemble import EnsembleEventResult
from biotech_sniper.paper_executor import (
    ConcurrencyCapExceeded,
    ContractTooExpensive,
    DeployedCapExceeded,
    OrderRejected,
    PaperExecutor,
    PaperExecutorError,
)


__all__ = [
    "submit_news_event_entry",
    "ensure_chain_quote_on_leg",
    "UnderlyingUnavailable",
    "Stage2WiringError",
    # Re-exports for convenient typed-exception handling at the
    # Stage-2 caller boundary.
    "ContractTooExpensive",
    "ConcurrencyCapExceeded",
    "DeployedCapExceeded",
    "OrderRejected",
    "UnsupportedMultiStrikeForNewsEntry",
    "EVENT_NEWS_ENTRY",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class Stage2WiringError(PaperExecutorError):
    """Base class for errors raised by the Stage-2 wiring layer."""


class UnderlyingUnavailable(Stage2WiringError):
    """Raised when the underlying ticker is halted / delisted / untradable.

    Per VAL-M3-090, this MUST be a typed exception so the Stage-2
    caller can:

    * NOT advance ``ticker_cooldown.last_entry_at`` (a halted
      underlying is not a "successful entry"; the cooldown UPSERT
      is intentionally skipped),
    * write the audit reason ``underlying_unavailable`` to the
      Stage-2 audit log,
    * leave the ``paper_orders`` table count unchanged for this
      ticker (no row is written; the broker is never contacted).

    The exception's ``args[0]`` is a short human-readable reason
    naming the ticker so an operator inspecting the audit log can
    immediately see which symbol tripped the gate.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_quote_value(value: Any) -> Optional[float]:
    """Return ``float(value)`` clamped to ``> 0`` or ``None``.

    Mirrors the conservative parsing in :func:`PaperExecutor._coerce_quote`
    but returns ``None`` instead of ``0.0`` so callers can distinguish
    "no quote present" from "quote present but zero".
    """
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out <= 0.0:
        return None
    return out


def ensure_chain_quote_on_leg(
    play_card: Mapping[str, Any],
    *,
    bid: Optional[float],
    ask: Optional[float],
) -> dict[str, Any]:
    """Return a deep-copied play_card whose single leg carries ``bid`` / ``ask``.

    The dispatcher's ``build_play_card`` produces a leg with
    ``symbol`` / ``option_type`` / ``strike`` / ``expiry`` / ``ticker`` /
    ``side`` but no chain quote — the chain probe runs separately
    and the wiring stitches the quote onto the leg here so the
    downstream :func:`PaperExecutor.size_position` call can derive
    ``qty`` from the mid.

    Any caller-supplied ``bid`` / ``ask`` already on the leg is
    overridden by the explicit arguments — there is exactly one
    source of truth for the quote per submission.

    Returns a fresh ``dict`` so the caller's ``DispatchResult`` /
    play_card structures stay immutable.
    """
    legs = play_card.get("option_legs")
    if (
        not isinstance(legs, list)
        or len(legs) != 1
        or not isinstance(legs[0], Mapping)
    ):
        raise UnsupportedMultiStrikeForNewsEntry(
            "ensure_chain_quote_on_leg requires a single-leg play_card; "
            f"got option_legs={legs!r}"
        )
    leg = dict(legs[0])
    leg["bid"] = _coerce_quote_value(bid)
    leg["ask"] = _coerce_quote_value(ask)
    new_card = dict(play_card)
    new_card["option_legs"] = [leg]
    return new_card


def _check_underlying_tradable(
    executor: PaperExecutor,
    ticker: str,
) -> None:
    """Pre-flight halted / delisted check (VAL-M3-090).

    Calls :meth:`AlpacaClient.get_latest_trade` for the underlying:

    * If the call raises an :class:`AlpacaClientError`, the
      underlying is treated as unavailable and we raise
      :class:`UnderlyingUnavailable`.
    * If the call returns ``None`` or a non-positive price, the
      underlying has no recent trade — also treated as unavailable.
    * Otherwise the call succeeds and the wiring proceeds with
      submission.

    The pre-check is separate from any ``stock_price`` value already
    in the play_card on purpose: the play_card price might come from
    a delayed snapshot, but ``get_latest_trade`` reflects the live
    paper-feed state. A halt that started AFTER the play_card was
    built will be caught here.

    The check is best-effort: if the executor's wrapped client does
    NOT expose ``get_latest_trade`` (e.g. tests inject a stripped-
    down double), we degrade to a no-op and rely on the broker's
    own rejection path. Production callers always pass an
    :class:`AlpacaClient`, which implements the method.
    """
    if not ticker:
        raise UnderlyingUnavailable(
            "underlying ticker missing from candidate_event"
        )

    client = getattr(executor, "client", None)
    get_latest_trade = getattr(client, "get_latest_trade", None)
    if not callable(get_latest_trade):
        # Best-effort degradation — broker rejection path handles it.
        return

    try:
        price = get_latest_trade(ticker)
    except AlpacaClientError as exc:
        logger.warning(
            "stage2_paper_executor.underlying_unavailable ticker=%s "
            "reason=%s",
            ticker,
            exc,
        )
        raise UnderlyingUnavailable(
            f"underlying {ticker!r} unavailable: {exc}"
        ) from exc

    if price is None or price <= 0:
        logger.warning(
            "stage2_paper_executor.underlying_unavailable ticker=%s "
            "price=%r reason=no-recent-trade",
            ticker,
            price,
        )
        raise UnderlyingUnavailable(
            f"underlying {ticker!r} has no recent trade — "
            "likely halted or delisted"
        )


# ---------------------------------------------------------------------------
# Public API: submit_news_event_entry
# ---------------------------------------------------------------------------


def submit_news_event_entry(
    executor: PaperExecutor,
    *,
    candidate_event: Mapping[str, Any],
    ensemble_result: EnsembleEventResult,
    stock_price: float,
    bid: Optional[float],
    ask: Optional[float],
    expiry: Optional[str],
    check_underlying_tradable: bool = True,
) -> str:
    """Build a Stage-2 ``news_event_entry`` play_card and submit it via PaperExecutor.

    Parameters
    ----------
    executor:
        A :class:`biotech_sniper.paper_executor.PaperExecutor` instance
        whose wrapped ``AlpacaClient`` targets the paper sandbox.
    candidate_event:
        The Stage-1 ``candidate_events`` row that triggered Stage-2
        scoring. ``id`` / ``ticker`` / ``matched_keywords`` are read.
    ensemble_result:
        Post-fanout, post-unanimity-gate ensemble verdict.
    stock_price:
        Live underlying price used by the dispatcher for OTM strike
        calculation. Distinct from the broker tradability check
        below — that one calls ``get_latest_trade`` independently.
    bid, ask:
        Live option chain quote for the resolved OTM strike. Used
        by :func:`PaperExecutor.size_position` to derive
        ``qty = floor($250 / mid / 100)``. Both ``None`` raises
        :class:`biotech_sniper.paper_executor.MissingQuoteData` from
        :func:`size_position` downstream — the wiring does not
        catch that case (the caller is expected to fetch the chain).
    expiry:
        ISO-8601 expiry date (``YYYY-MM-DD``) for the OCC option
        symbol. Forwarded to :func:`build_play_card`.
    check_underlying_tradable:
        When ``True`` (default), the wiring calls
        :meth:`AlpacaClient.get_latest_trade` to confirm the
        underlying is tradable. A halt / delist short-circuits with
        :class:`UnderlyingUnavailable` BEFORE the dispatcher runs
        and BEFORE any ``paper_orders`` row is written. Set to
        ``False`` only in tests where the chain quote is already
        known to be valid.

    Returns
    -------
    str
        The broker-assigned ``alpaca_order_id`` returned by
        :meth:`PaperExecutor.execute`. The caller can use this to
        poll fill status, persist telemetry, etc.

    Raises
    ------
    UnderlyingUnavailable
        Halted / delisted underlying. No ``paper_orders`` row
        written; no broker call made.
    UnsupportedMultiStrikeForNewsEntry
        Defensive validator caught a multi-strike-shaped play_card
        (the dispatcher itself never emits one — this fires only on
        programmer error in the dispatcher).
    DirectionUnavailable
        Ensemble verdict has no consensus direction (an upstream
        unanimity-gate bug). The dispatcher itself raises this
        before the wiring runs; re-listed here for callers.
    ContractTooExpensive
        Single contract mid > ``RISK_PER_PLAY_USD``. The executor
        persists a ``status='rejected'`` row and propagates this
        exception. Stage-2 should treat as a soft-skip per VAL-M3-091.
    ConcurrencyCapExceeded
        ≥ ``MAX_CONCURRENT_PLAYS`` open OPTION positions (per
        VAL-M3-054 / VAL-M3-055). Equity holdings are ignored.
    DeployedCapExceeded
        Deployed + planned cost > ``MAX_DEPLOYED_USD`` (per
        VAL-M3-054). Counts options-only.
    OrderRejected
        Broker-side rejection (e.g. invalid OCC symbol, account
        ineligible). The executor persists a ``status='rejected'``
        row first.
    """

    ticker = str(candidate_event.get("ticker", "")).strip().upper()

    # VAL-M3-090 — halted / delisted underlying short-circuits with
    # a typed exception BEFORE any paper_orders row is written and
    # BEFORE the dispatcher runs (so Stage-2 doesn't even spend the
    # OTM-strike calculation effort on an untradable ticker).
    if check_underlying_tradable:
        _check_underlying_tradable(executor, ticker)

    # Build the single-leg news_event_entry play_card.
    dispatch: DispatchResult = build_play_card(
        candidate_event=candidate_event,
        ensemble_result=ensemble_result,
        stock_price=float(stock_price),
        expiry=expiry,
    )

    # Stitch the chain quote onto the leg so size_position can run.
    play_card = ensure_chain_quote_on_leg(
        dispatch.play_card, bid=bid, ask=ask
    )

    # Defensive single-leg invariant (VAL-M3-092). The dispatcher's
    # output is already single-leg, so this is a no-op on the happy
    # path; the validator catches any future programmer error
    # before the play_card reaches the broker.
    assert_single_leg_for_news_entry(play_card)

    logger.info(
        "stage2_paper_executor.submit ticker=%s direction=%s "
        "option_type=%s strike=%.4f catalyst_type=%s "
        "candidate_event_id=%s run_id=%s expiry=%s bid=%s ask=%s",
        ticker,
        dispatch.direction,
        dispatch.option_type,
        dispatch.strike,
        dispatch.catalyst_type,
        play_card.get("candidate_event_id"),
        play_card.get("run_id"),
        expiry,
        bid,
        ask,
    )

    # Hand off to the existing PaperExecutor.execute(). All sizing,
    # cap, and persistence semantics are inherited unchanged.
    return executor.execute(play_card)
