"""Stage-2 dispatcher: build single-leg ``news_event_entry`` play_cards.

Feature: f-m3-09-direction-and-strike-routing.

The dispatcher is the bridge between the Stage-2 ensemble + gates and
:class:`biotech_sniper.paper_executor.PaperExecutor`. It consumes a
post-fanout, post-unanimity, post-probability-threshold ensemble result
plus its source ``candidate_events`` row and synthesises a single-leg
play_card whose:

* ``option_type`` derives from the ensemble's consensus
  ``direction``: ``bullish → 'call'``, ``bearish → 'put'`` (per
  VAL-M3-048 / VAL-M3-049).
* ``strike`` is OTM relative to the live underlying price; the OTM
  offset is the midpoint of the existing
  :data:`biotech_sniper.sectors.unified_scorer.DEFAULT_OTM_BY_CATALYST`
  range for the derived ``catalyst_type`` (per VAL-M3-050).
* ``catalyst_type`` is one of the EXISTING uppercase keys
  ``PDUFA / READOUT / LABEL_EXT / ADCOM / CONTRACT / DEFAULT`` —
  derived through :func:`derive_catalyst_type`, which delegates to
  :func:`biotech_sniper.sectors.unified_scorer.detect_catalyst_type`
  for LABEL_EXT / PDUFA / READOUT / DEFAULT and adds targeted
  pre-checks for ADCOM and CONTRACT (per VAL-M3-051). Unknown
  ``matched_keywords`` fall back to ``DEFAULT`` (per VAL-M3-080) —
  no ``KeyError`` ever escapes the dispatcher.
* ``event`` is the new ``news_event_entry`` enum value; ``side='buy'``,
  ``purpose='entry'``, ``order_class='simple'``.
* ``option_legs`` is a list of EXACTLY one leg dict (per VAL-M3-092 —
  no multi-strike, no spreads on the news_event_entry path). The
  defensive validator :func:`assert_single_leg_for_news_entry`
  rejects multi-strike-shaped play_cards carrying
  ``event='news_event_entry'`` with the typed exception
  :class:`UnsupportedMultiStrikeForNewsEntry`.

Direction conflict semantics (VAL-M3-052)
-----------------------------------------

Stage-1 emits ``candidate_events`` rows that may carry a stale or
keyword-derived ``direction`` hint upstream. The dispatcher IGNORES
that field at routing time — the ensemble's consensus direction is
the sole source of truth. A candidate row with negative-keyword
metadata still routes to a CALL when the ensemble unanimously
returns ``direction='bullish'``.

Single-source-of-truth invariants
---------------------------------

* The OTM offset constant is IMPORTED from
  :mod:`biotech_sniper.sectors.unified_scorer` — never re-defined as
  a parallel literal here.
* The catalyst-type keyword table is delegated to
  :func:`detect_catalyst_type` plus thin pre-checks for ADCOM /
  CONTRACT (which the existing function only handles via the
  ``sector=`` parameter, not via the combined ``notes`` string).

This module performs NO database writes, NO network calls, and NO
filesystem I/O — it is a pure transform from
``(candidate_event, ensemble_result, stock_price)`` to a play_card
dict. Persistence and order submission live in
:class:`biotech_sniper.paper_executor.PaperExecutor` (wired by
``f-m3-10-paper-executor-wiring``).
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

from biotech_sniper.llm.ensemble import EnsembleEventResult
from biotech_sniper.sectors.unified_scorer import (
    DEFAULT_OTM_BY_CATALYST,
    calculate_otm_strike,
    detect_catalyst_type,
)

__all__ = [
    "DispatchResult",
    "DispatcherError",
    "DirectionUnavailable",
    "UnsupportedMultiStrikeForNewsEntry",
    "EVENT_NEWS_ENTRY",
    "ORDER_CLASS_SIMPLE",
    "PURPOSE_ENTRY",
    "SIDE_BUY",
    "build_play_card",
    "derive_catalyst_type",
    "assert_single_leg_for_news_entry",
    # f-fix-m3-13: thin orchestration helper composing cooldown ->
    # armed -> cap -> score_candidate_event with a single
    # short-circuit return on first rejection (VAL-M3-071).
    "Stage2ChainResult",
    "run_stage2_chain",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants — the canonical ``news_event_entry`` order shape.
# ---------------------------------------------------------------------------

#: ``paper_orders.event`` enum value for Stage-2 entries (per the v10
#: migration that extends the CHECK enum).
EVENT_NEWS_ENTRY: str = "news_event_entry"

#: Reading-B Stage-2 entries are always single-leg long calls/puts —
#: ``order_class='simple'`` matches ``PaperExecutor._validate_single_leg``.
ORDER_CLASS_SIMPLE: str = "simple"

#: ``paper_orders.purpose`` value for entries (matches the daily-curated
#: ``open`` event's purpose).
PURPOSE_ENTRY: str = "entry"

#: ``paper_orders.side`` for entries — Reading-B Stage-2 only opens
#: long positions (puts on bearish, calls on bullish); shorts are
#: out of scope for the news_event_entry path.
SIDE_BUY: str = "buy"


# ---------------------------------------------------------------------------
# Mapping tables — direction (ensemble verdict) → leg shape.
# ---------------------------------------------------------------------------

# Ensemble verdict direction → option_type the executor will submit.
_DIRECTION_TO_LEG_TYPE: dict[str, str] = {
    "bullish": "call",
    "bearish": "put",
}

# Ensemble verdict direction → unified_scorer.calculate_otm_strike()
# direction key. The unified scorer was authored before the
# bullish/bearish vocabulary settled; its ``direction`` argument
# expects the LONG_CALLS / LONG_PUTS / CALL_SPREAD strings used by
# the daily-curated path.
_DIRECTION_TO_UNIFIED: dict[str, str] = {
    "bullish": "LONG_CALLS",
    "bearish": "LONG_PUTS",
}


# Pre-check tokens for catalyst-type derivation. ADCOM and CONTRACT
# are handled by ``detect_catalyst_type`` only via the ``sector=``
# parameter — for matched_keywords-driven derivation we add targeted
# substring checks here. The bulk of the
# LABEL_EXT / PDUFA / READOUT / DEFAULT mapping remains delegated to
# detect_catalyst_type so the canonical keyword vocabulary stays in
# :mod:`biotech_sniper.sectors.unified_scorer`.
#
# f-misc-05: the previous readout-keyword pre-check tuples (a
# substring set + a whole-word set) have been retired — the canonical
# READOUT tokens VAL-M3-051 mandates (``p3 readout`` / ``p2 readout`` /
# ``p1 readout`` / ``phase 1`` / ``first-in-human``) now live directly
# on :func:`biotech_sniper.sectors.unified_scorer.detect_catalyst_type`'s
# ``readout_signals`` list. The dispatcher delegates 100% of the
# readout / pdufa / label_ext / default mapping to that helper.
_ADCOM_TOKENS: tuple[str, ...] = ("adcom", "advisory committee")
_CONTRACT_TOKENS: tuple[str, ...] = ("contract award",)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DispatcherError(Exception):
    """Base exception for stage2_dispatcher errors."""


class DirectionUnavailable(DispatcherError):
    """Raised when the ensemble result has no usable consensus direction.

    The dispatcher is a post-unanimity-gate consumer: it expects
    ``ensemble_result.direction`` to be exactly ``'bullish'`` or
    ``'bearish'``. ``None`` (or any other value) signals that an
    upstream gate should have short-circuited; calling the dispatcher
    in that state is a programming error and yields this exception
    rather than a malformed play_card.
    """


class UnsupportedMultiStrikeForNewsEntry(DispatcherError):
    """Raised when a play_card carrying ``event='news_event_entry'``
    has anything other than EXACTLY one leg.

    Per VAL-M3-092, the news_event_entry path is single-leg only — no
    multi-strike, no spreads. The dispatcher itself only ever emits
    single-leg cards, so this exception is purely defensive — it
    catches programmer errors (e.g., a future caller hand-assembling
    a multi-strike card and tagging it news_event_entry) at the
    boundary BEFORE the play_card reaches the PaperExecutor.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_keywords(
    matched_keywords: Optional[Union[str, Iterable[Any]]],
) -> str:
    """Collapse ``matched_keywords`` into a lowercase whitespace-joined string.

    Accepts the comma-CSV form persisted in ``candidate_events``
    (the canonical wire shape — see
    :mod:`biotech_sniper.news_daemon.emit`) or any iterable of
    keyword strings (convenient for tests / synthetic invocations).
    Empty / ``None`` inputs yield ``""`` so the downstream
    ``detect_catalyst_type`` call cleanly falls through to
    ``DEFAULT``.
    """

    if matched_keywords is None:
        return ""
    if isinstance(matched_keywords, str):
        kws = [k.strip() for k in matched_keywords.split(",") if k.strip()]
    else:
        kws = []
        for k in matched_keywords:
            if k is None:
                continue
            text = str(k).strip()
            if text:
                kws.append(text)
    return " ".join(kws).lower()


def derive_catalyst_type(
    matched_keywords: Optional[Union[str, Iterable[Any]]],
) -> str:
    """Derive a :data:`DEFAULT_OTM_BY_CATALYST` key from ``matched_keywords``.

    Per VAL-M3-051, the resolved key MUST be one of the existing
    uppercase keys ``PDUFA / READOUT / LABEL_EXT / ADCOM / CONTRACT
    / DEFAULT``. Per VAL-M3-080, an unknown / unrecognised set of
    keywords falls back to ``'DEFAULT'`` — the dispatcher MUST NOT
    raise ``KeyError`` for any input.

    Mapping rules (delegated through
    :func:`biotech_sniper.sectors.unified_scorer.detect_catalyst_type`,
    plus targeted ADCOM / CONTRACT pre-checks):

    * pdufa / fda approval / pdufa date / nda / bla → ``PDUFA``
    * phase 3 / p3 readout / topline / phase 2 / p2 / phase 1 /
      first-in-human → ``READOUT``
    * snda / sbla / label extension → ``LABEL_EXT``
    * adcom / advisory committee → ``ADCOM``
    * contract award → ``CONTRACT``
    * partnership / collaboration / license / m&a / acquisition /
      merger / unknown phrase → ``DEFAULT`` (no specialised OTM slot
      exists today; calibration deferred per VAL-M3-051 note).

    f-misc-05: catalyst classification (PDUFA / READOUT / LABEL_EXT /
    DEFAULT) is delegated 100% to :func:`detect_catalyst_type`. The
    only pre-checks retained here are for ADCOM / CONTRACT, which the
    canonical detector surfaces only through its ``sector=`` argument
    — for matched_keywords-driven derivation they need targeted
    substring checks at the dispatcher boundary.
    """

    combined = _normalize_keywords(matched_keywords)

    # ADCOM / CONTRACT are surfaced by the ``sector=`` parameter on
    # the canonical detector, not by its keyword table — so we
    # pre-check the combined string here. This is NOT a parallel
    # keyword table (forbidden by VAL-M3-051): it is two narrow
    # token checks against the existing canonical vocabulary.
    if any(token in combined for token in _ADCOM_TOKENS):
        return "ADCOM"
    if any(token in combined for token in _CONTRACT_TOKENS):
        return "CONTRACT"

    # Delegate LABEL_EXT / PDUFA / READOUT / DEFAULT to the
    # canonical detector. ``detect_catalyst_type`` returns
    # ``"DEFAULT"`` on miss, so no exception escapes. Per f-misc-05
    # the readout-keyword vocabulary lives entirely on
    # ``detect_catalyst_type.readout_signals``; no parallel pre-check
    # remains here.
    catalyst_type = detect_catalyst_type(notes=combined)

    # Defensive: any unexpected value collapses to DEFAULT so the
    # downstream ``DEFAULT_OTM_BY_CATALYST[catalyst_type]`` lookup
    # never raises ``KeyError``.
    if catalyst_type not in DEFAULT_OTM_BY_CATALYST:
        return "DEFAULT"
    return catalyst_type


def _build_occ_symbol(
    ticker: str,
    expiry: Optional[str],
    option_type: str,
    strike: float,
) -> Optional[str]:
    """Construct a standard OCC option symbol from its components.

    Format: ``TICKER + YYMMDD + (C|P) + STRIKE * 1000`` zero-padded
    to 8 digits — matches
    :func:`biotech_sniper.rotation_engine._build_occ_symbol`. Returns
    ``None`` when any component cannot be parsed cleanly so callers
    can decide whether to skip the symbol or hand the card off to the
    chain probe to fill it.
    """

    if not isinstance(ticker, str) or not ticker.strip():
        return None
    if expiry is None:
        return None
    expiry_str = str(expiry).strip()
    if len(expiry_str) < 8:
        return None
    try:
        if "-" in expiry_str:
            datetime.date.fromisoformat(expiry_str[:10])
            yymmdd = expiry_str.replace("-", "")[2:8]
        else:
            yymmdd = expiry_str[2:8]
            datetime.datetime.strptime(yymmdd, "%y%m%d")
    except (TypeError, ValueError):
        return None
    opt_raw = option_type.strip().lower() if isinstance(option_type, str) else ""
    if opt_raw.startswith("c"):
        cp = "C"
    elif opt_raw.startswith("p"):
        cp = "P"
    else:
        return None
    try:
        strike_thousandths = int(round(float(strike) * 1000.0))
    except (TypeError, ValueError):
        return None
    if strike_thousandths < 0:
        return None
    return f"{ticker.strip().upper()}{yymmdd}{cp}{strike_thousandths:08d}"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchResult:
    """Return value of :func:`build_play_card`.

    Attributes
    ----------
    play_card:
        The fully-constructed single-leg play_card dict — the shape
        :class:`biotech_sniper.paper_executor.PaperExecutor` consumes.
    direction:
        The ensemble verdict direction echoed for caller convenience
        (``'bullish'`` / ``'bearish'``).
    option_type:
        Resolved option leg type (``'call'`` / ``'put'``).
    catalyst_type:
        Resolved key into :data:`DEFAULT_OTM_BY_CATALYST` (one of
        ``PDUFA / READOUT / LABEL_EXT / ADCOM / CONTRACT / DEFAULT``).
    otm_pct:
        The midpoint of ``DEFAULT_OTM_BY_CATALYST[catalyst_type]``
        used as the OTM offset for strike calculation.
    strike:
        Resolved option strike (rounded to standard increments by
        :func:`calculate_otm_strike`).
    stock_price:
        The underlying price echoed for audit / regression purposes.
    """

    play_card: dict[str, Any]
    direction: str
    option_type: str
    catalyst_type: str
    otm_pct: float
    strike: float
    stock_price: float


# ---------------------------------------------------------------------------
# Core: build_play_card
# ---------------------------------------------------------------------------


def build_play_card(
    *,
    candidate_event: Mapping[str, Any],
    ensemble_result: EnsembleEventResult,
    stock_price: float,
    expiry: Optional[str] = None,
) -> DispatchResult:
    """Synthesise a single-leg ``news_event_entry`` play_card.

    Parameters
    ----------
    candidate_event:
        Mapping form of the ``candidate_events`` row that triggered
        Stage-2 scoring. The dispatcher reads ``id`` / ``ticker`` /
        ``matched_keywords`` from this mapping; any other Stage-1
        ``direction`` hint present on the row is IGNORED — the
        ensemble verdict is the sole source of truth for routing
        direction (per VAL-M3-052).
    ensemble_result:
        The :class:`EnsembleEventResult` produced by
        :func:`biotech_sniper.llm.ensemble.score_candidate_event` AFTER
        the unanimity gate (label='material', unanimous direction)
        passed. The dispatcher does NOT re-validate the gate result —
        it expects the caller to short-circuit on gate rejection.
    stock_price:
        Live underlying price. Strike calculation uses this as the
        reference for the OTM offset.
    expiry:
        Optional ISO-8601 expiry date (``YYYY-MM-DD``). When
        provided, the leg's ``symbol`` is populated with a standard
        OCC option symbol; when omitted, ``symbol`` is left empty
        and the chain probe fills it later.

    Returns
    -------
    DispatchResult
        Frozen dataclass with the play_card and resolution metadata.

    Raises
    ------
    DirectionUnavailable
        When ``ensemble_result.direction`` is not exactly
        ``'bullish'`` or ``'bearish'`` (the upstream unanimity gate
        should have short-circuited; this is a defensive check).
    """

    direction = ensemble_result.direction
    if direction not in _DIRECTION_TO_LEG_TYPE:
        raise DirectionUnavailable(
            f"ensemble_result.direction must be 'bullish' or 'bearish'; "
            f"got {direction!r}. Upstream unanimity gate should have "
            "short-circuited before invoking the dispatcher."
        )

    matched_keywords = candidate_event.get("matched_keywords")
    catalyst_type = derive_catalyst_type(matched_keywords)

    # OTM offset = midpoint of the catalyst-type-specific range.
    # This explicitly REUSES the unified_scorer constant per
    # VAL-M3-050 — never a parallel hardcoded literal here.
    min_otm, max_otm = DEFAULT_OTM_BY_CATALYST[catalyst_type]
    otm_pct = (min_otm + max_otm) / 2.0

    # Delegate strike rounding + sign to the existing unified_scorer
    # helper. Passing ``otm_pct`` explicitly (not the 0.15 sentinel)
    # ensures the helper does NOT re-derive the midpoint from the
    # catalyst_type — we already did that above.
    strike = float(
        calculate_otm_strike(
            stock_price=float(stock_price),
            direction=_DIRECTION_TO_UNIFIED[direction],
            otm_pct=otm_pct,
            catalyst_type=catalyst_type,
        )
    )
    option_type = _DIRECTION_TO_LEG_TYPE[direction]

    raw_ticker = candidate_event.get("ticker", "")
    ticker = str(raw_ticker).strip().upper()

    candidate_event_id = (
        candidate_event.get("id")
        if "id" in candidate_event
        else candidate_event.get("candidate_event_id")
    )

    occ_symbol = _build_occ_symbol(ticker, expiry, option_type, strike) or ""

    leg: dict[str, Any] = {
        "symbol": occ_symbol,
        "option_type": option_type,
        "strike": strike,
        "expiry": expiry,
        "ticker": ticker,
        "side": SIDE_BUY,
    }

    play_card_id = (
        f"news-{candidate_event_id}-{ensemble_result.run_id}"
        if candidate_event_id is not None
        else f"news-{ensemble_result.run_id}"
    )

    play_card: dict[str, Any] = {
        "play_card_id": play_card_id,
        "ticker": ticker,
        "option_legs": [leg],
        "side": SIDE_BUY,
        "purpose": PURPOSE_ENTRY,
        "event": EVENT_NEWS_ENTRY,
        "order_class": ORDER_CLASS_SIMPLE,
        "stock_price": float(stock_price),
        "catalyst_type": catalyst_type,
        "direction": direction,
        "candidate_event_id": candidate_event_id,
        "run_id": ensemble_result.run_id,
        "otm_pct": otm_pct,
    }

    logger.info(
        "stage2_dispatcher.build_play_card ticker=%s direction=%s "
        "option_type=%s catalyst_type=%s otm_pct=%.4f strike=%.4f "
        "stock_price=%.4f candidate_event_id=%s run_id=%s",
        ticker,
        direction,
        option_type,
        catalyst_type,
        otm_pct,
        strike,
        float(stock_price),
        candidate_event_id,
        ensemble_result.run_id,
    )

    return DispatchResult(
        play_card=play_card,
        direction=direction,
        option_type=option_type,
        catalyst_type=catalyst_type,
        otm_pct=otm_pct,
        strike=strike,
        stock_price=float(stock_price),
    )


# ---------------------------------------------------------------------------
# Defensive validator (VAL-M3-092)
# ---------------------------------------------------------------------------


def assert_single_leg_for_news_entry(
    play_card: Mapping[str, Any],
) -> Optional[Mapping[str, Any]]:
    """Defensive single-leg validator for ``news_event_entry`` play_cards.

    Per VAL-M3-092, a play_card with ``event='news_event_entry'``
    MUST contain EXACTLY one leg in ``option_legs``. Anything else
    (zero legs, two-or-more legs, missing list) raises
    :class:`UnsupportedMultiStrikeForNewsEntry`.

    For non-news_event_entry events the validator is a no-op (the
    daily-curated path has its own multi-strike handling in
    :class:`biotech_sniper.paper_executor.PaperExecutor`); it returns
    ``None`` in that case so callers can chain it freely.

    Returns
    -------
    Optional[Mapping[str, Any]]
        The single leg dict for ``news_event_entry`` play_cards
        (caller convenience); ``None`` for any other event.

    Raises
    ------
    UnsupportedMultiStrikeForNewsEntry
        When ``event == 'news_event_entry'`` and
        ``len(option_legs) != 1``.
    """

    event = play_card.get("event")
    if event != EVENT_NEWS_ENTRY:
        return None

    legs = play_card.get("option_legs")
    if not isinstance(legs, Sequence) or isinstance(legs, (str, bytes)):
        raise UnsupportedMultiStrikeForNewsEntry(
            f"play_card['option_legs'] must be a list of leg dicts on "
            f"news_event_entry; got {type(legs).__name__}"
        )
    n = len(legs)
    if n != 1:
        raise UnsupportedMultiStrikeForNewsEntry(
            f"news_event_entry play_card must have exactly 1 leg "
            f"(VAL-M3-092: single-leg only — no multi-strike, no spreads); "
            f"got {n}"
        )
    return legs[0]


# ---------------------------------------------------------------------------
# f-fix-m3-13 — Thin Stage-2 chain orchestrator (cheap-first short-circuit)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Stage2ChainResult:
    """Outcome of :func:`run_stage2_chain`.

    Attributes
    ----------
    passed:
        ``True`` only when EVERY cheap gate passed AND the 4-provider
        fan-out completed. ``False`` whenever any gate rejected.
    reason:
        Canonical short-circuit reason from the rejecting gate
        (``cooldown_active`` / ``armed_file_missing`` /
        ``daily_cap_exceeded``). ``None`` when the chain reached
        ``score_candidate_event`` and ran the fan-out.
    gate:
        Symbolic name of the gate that produced the rejection
        (``"cooldown"`` / ``"armed"`` / ``"cap"``). ``None`` on a
        clean fan-out.
    ensemble_result:
        The :class:`EnsembleEventResult` produced by
        :func:`score_candidate_event` when all cheap gates passed.
        ``None`` whenever the chain short-circuited.
    """

    passed: bool
    reason: Optional[str]
    gate: Optional[str]
    ensemble_result: Optional[EnsembleEventResult] = None


def run_stage2_chain(
    *,
    candidate_event_row: Mapping[str, Any],
    db_path: Optional[Any] = None,
    armed_path: Optional[Any] = None,
    audit_path: Optional[Any] = None,
    now: Optional[datetime.datetime] = None,
    providers: Optional[Mapping[str, Any]] = None,
) -> Stage2ChainResult:
    """Compose the Stage-2 cheap-first chain at the orchestration entry.

    Order (per VAL-M3-071 / VAL-M5-014..027):
    cooldown → armed → cap → fan-out → unanimity → probability. A
    single short-circuit return on the first rejection guarantees
    zero LLM invocations / zero ``llm_cost_ledger`` rows / zero
    ``ensemble_scores_event`` rows when ANY cheap gate rejects. On
    post-fan-out (unanimity / probability) rejections, the four
    ``ensemble_scores_event`` rows persist for forensic review per
    VAL-M5-015.

    f-m5-03 — Each rejection writes ONE row into ``news_match_log``
    (``matched=0``, ``reason=<canonical>``) AND, when ``audit_path``
    is supplied, merges a per-reason entry into
    ``state/audit_latest.json`` capturing gate-specific forensic
    metadata (``last_avg_probability`` / ``last_cooldown_remaining_seconds``
    / ``last_total_usd``) so ops dashboards can surface close-to-pass
    rejections without re-running the gate. Persistence failures
    inside :func:`record_stage2_skip` are swallowed at WARNING — a
    bookkeeping miss must NOT mutate the gate decision.
    """
    # Late imports keep stage2_dispatcher import-cheap.
    from biotech_sniper.llm.ensemble import score_candidate_event
    from biotech_sniper.llm.stage2_gates import (
        armed_gate,
        cooldown_gate,
        daily_cap_gate,
        evaluate_post_fanout_gates,
        record_stage2_skip,
    )

    ticker = str(candidate_event_row.get("ticker", "")).strip().upper()
    candidate_event_id = candidate_event_row.get("id")
    if candidate_event_id is None:
        candidate_event_id = candidate_event_row.get("candidate_event_id")
    news_event_id = candidate_event_row.get("source_news_event_id")

    def _persist_skip(
        reason: str,
        *,
        avg_probability: Optional[float] = None,
        cooldown_remaining_seconds: Optional[int] = None,
        today_total_usd: float = 0.0,
        projected_cost: float = 0.0,
        cap_value: float = 0.0,
    ) -> None:
        """Persist a news_match_log row + audit JSON entry for ``reason``.

        Best-effort: missing ``db_path`` skips both writes (we cannot
        even write the news_match_log row without a SQLite target).
        ``audit_path=None`` is handled inside ``record_stage2_skip`` —
        the news_match_log row STILL lands (per f-fix-m5-03 — the
        previous early-return short-circuit silently dropped every
        rejection's audit row), and only the JSON-merge is skipped.
        ``record_stage2_skip`` itself swallows sqlite/OS errors at
        WARNING.
        """
        if db_path is None:
            return
        try:
            cei: Optional[int] = (
                int(candidate_event_id)
                if candidate_event_id is not None
                else None
            )
        except (TypeError, ValueError):
            cei = None
        try:
            nei: Optional[int] = (
                int(news_event_id) if news_event_id is not None else None
            )
        except (TypeError, ValueError):
            nei = None
        record_stage2_skip(
            db_path=db_path,
            audit_path=audit_path,
            ticker=ticker,
            candidate_event_id=cei,
            news_event_id=nei,
            today_total_usd=today_total_usd,
            projected_cost=projected_cost,
            cap=cap_value,
            reason=reason,
            avg_probability=avg_probability,
            cooldown_remaining_seconds=cooldown_remaining_seconds,
        )

    # ---- Cheap-first cooldown gate ----------------------------------
    cool = cooldown_gate(ticker=ticker, db_path=db_path, now=now)
    if not cool.passed:
        _persist_skip(
            cool.reason or "cooldown_active",
            cooldown_remaining_seconds=cool.remaining_seconds,
        )
        return Stage2ChainResult(passed=False, reason=cool.reason, gate="cooldown")

    # ---- Armed gate ----
    arm = armed_gate(armed_path=armed_path)
    if not arm.passed:
        _persist_skip(arm.reason or "armed_file_missing")
        return Stage2ChainResult(passed=False, reason=arm.reason, gate="armed")

    # ---- Cap gate ----
    # ``daily_cap_gate`` itself does NOT persist when we omit
    # ticker/audit_path; we route persistence through the local
    # ``_persist_skip`` so the news_match_log row + audit JSON entry
    # are uniform across all five gates.
    cap = daily_cap_gate(db_path=db_path)
    if not cap.passed:
        _persist_skip(
            cap.reason or "daily_cap_exceeded",
            today_total_usd=float(cap.today_total_usd),
            projected_cost=float(cap.projected_cost),
            cap_value=float(cap.cap),
        )
        return Stage2ChainResult(passed=False, reason=cap.reason, gate="cap")

    # ---- Fan-out (LLM cost incurred here) ----
    ensemble = score_candidate_event(
        candidate_event_row, db_path=db_path, providers=providers,
    )

    # ---- Post-fanout gates: unanimity → probability (cheap-first) ----
    # The post-fanout chain canonicalises rejection reason as the
    # FIRST gate that rejected (VAL-M3-031). For VAL-M5-014..018 we
    # surface the canonical rejection reason — ``unanimity_failed``
    # OR ``probability_below_threshold`` — and persist the
    # corresponding metadata.
    post = evaluate_post_fanout_gates(ensemble)
    if not post.passed:
        gate_label: str
        if post.reason == "unanimity_failed" or post.reason == "direction_split":
            gate_label = "unanimity"
        elif post.reason == "probability_below_threshold":
            gate_label = "probability"
        elif post.reason == "insufficient_providers":
            # Fewer than 4 successful providers — surface as
            # unanimity rejection (the canonical structural
            # short-circuit reason). The probability gate also
            # would reject with the same reason, but unanimity is
            # the conventional name in audit logs.
            gate_label = "unanimity"
        else:
            gate_label = "post_fanout"

        avg_p: Optional[float] = None
        if post.probability_gate is not None:
            avg_p = post.probability_gate.mean_probability

        _persist_skip(
            post.reason or "post_fanout_failed",
            avg_probability=avg_p,
        )
        return Stage2ChainResult(
            passed=False,
            reason=post.reason,
            gate=gate_label,
            ensemble_result=ensemble,
        )

    return Stage2ChainResult(
        passed=True, reason=None, gate=None, ensemble_result=ensemble,
    )
