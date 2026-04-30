"""Stage-2 dispatcher: direction + OTM strike routing tests.

Feature: f-m3-09-direction-and-strike-routing.

Verifies the contract assertions:

* VAL-M3-048 — ``ensemble.direction='bullish'`` synthesises a single-leg
  play_card with ``option_type='call'``, ``side='buy'``,
  ``purpose='entry'``, ``event='news_event_entry'`` and a strike that
  is OTM (strike > stock_price).
* VAL-M3-049 — Symmetric: ``direction='bearish'`` →
  ``option_type='put'``, strike < stock_price.
* VAL-M3-050 — The OTM offset is the existing
  :data:`biotech_sniper.sectors.unified_scorer.DEFAULT_OTM_BY_CATALYST`
  midpoint — NOT a hardcoded literal in the new event-driven path.
* VAL-M3-051 — Catalyst-type derivation maps ``matched_keywords`` into
  the EXISTING ``DEFAULT_OTM_BY_CATALYST`` keyspace (uppercase keys
  PDUFA, READOUT, LABEL_EXT, ADCOM, CONTRACT, DEFAULT). The lookup
  goes through ``detect_catalyst_type`` (or its keyword-driven
  sibling); no parallel keyword table is allowed.
* VAL-M3-052 — Stage-1 candidate_event direction metadata is overridden
  by the ensemble verdict. A row whose ``matched_keywords`` would
  suggest a bearish read still routes to a CALL when the ensemble
  unanimously returns ``direction='bullish'``.
* VAL-M3-080 — Unknown ``matched_keywords`` (partnership / collaboration
  / license deal / acquisition / merger / unknown_phrase) fall through
  to ``catalyst_type='DEFAULT'`` with NO KeyError.
* VAL-M3-092 — News-event entry path is single-leg only — the dispatcher
  ALWAYS emits ``len(option_legs) == 1``. A multi-strike-shaped
  play_card carrying ``event='news_event_entry'`` is rejected by the
  defensive validator
  :func:`biotech_sniper.exec.stage2_dispatcher.assert_single_leg_for_news_entry`
  with the typed exception
  :class:`biotech_sniper.exec.stage2_dispatcher.UnsupportedMultiStrikeForNewsEntry`.

Per the dual-path test convention (see ``library`` / ``AGENTS.md`` /
``skills/python-worker/SKILL.md``), the canonical bodies live in this
file; the contract-form node IDs at
``tests/exec/test_stage2_direction_routing.py`` re-export the same
bodies via ``from tests.test_stage2_dispatcher import *``.
"""

from __future__ import annotations

from typing import Optional

import pytest

from biotech_sniper.exec.stage2_dispatcher import (
    EVENT_NEWS_ENTRY,
    ORDER_CLASS_SIMPLE,
    PURPOSE_ENTRY,
    SIDE_BUY,
    DirectionUnavailable,
    DispatchResult,
    UnsupportedMultiStrikeForNewsEntry,
    assert_single_leg_for_news_entry,
    build_play_card,
    derive_catalyst_type,
)
from biotech_sniper.llm.ensemble import (
    ALL_PROVIDERS,
    EnsembleEventResult,
    ProviderResult,
)
from biotech_sniper.sectors.unified_scorer import DEFAULT_OTM_BY_CATALYST


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ensemble_result(
    direction: Optional[str] = "bullish",
    *,
    label: str = "material",
    probability: float = 0.85,
    run_id: str = "run-stage2-dispatcher-test",
    candidate_event_id: Optional[int] = 42,
) -> EnsembleEventResult:
    """Synthesise a 4/4 EnsembleEventResult with the requested
    consensus direction. Tests do NOT exercise the unanimity gate
    here — the dispatcher consumes a post-fanout, post-unanimity
    result, so we build a happy-path object directly."""
    rows = [
        ProviderResult(
            provider=p,
            label=label,
            probability=probability,
            direction=direction,
            rationale=f"{p}-stub",
            citations=[],
            latency_ms=10,
            cost_usd=0.001,
        )
        for p in ALL_PROVIDERS
    ]
    return EnsembleEventResult(
        candidate_event_id=candidate_event_id,
        run_id=run_id,
        per_provider_results=rows,
        successful_providers=list(ALL_PROVIDERS),
        failed_providers=[],
        label=label,
        direction=direction,
        mean_probability=probability,
        label_histogram={label: 4},
    )


def _candidate_event(
    *,
    id_: int = 42,
    ticker: str = "TESTBIO",
    matched_keywords: str = "pdufa",
) -> dict:
    return {
        "id": id_,
        "ticker": ticker,
        "source_news_event_id": id_ * 10,
        "matched_keywords": matched_keywords,
        "calendar_match": None,
        "emitted_at": "2026-04-30T00:00:00.000000Z",
        "dedup_key": f"deadbeef{id_}",
    }


# ---------------------------------------------------------------------------
# VAL-M3-048 — bullish → OTM call
# ---------------------------------------------------------------------------


def test_bullish_routes_to_otm_call():
    """``direction='bullish'`` synthesises a single-leg call with
    strike > stock_price and the canonical news_event_entry tags."""

    er = _make_ensemble_result(direction="bullish")
    cand = _candidate_event(matched_keywords="pdufa")
    stock_price = 100.0

    res = build_play_card(
        candidate_event=cand,
        ensemble_result=er,
        stock_price=stock_price,
        expiry="2026-07-17",
    )

    assert isinstance(res, DispatchResult)
    pc = res.play_card

    # Single-leg
    legs = pc["option_legs"]
    assert isinstance(legs, list) and len(legs) == 1
    leg = legs[0]

    # OTM call: strike > stock_price
    assert leg["option_type"] == "call"
    assert res.option_type == "call"
    assert leg["strike"] > stock_price
    assert res.strike > stock_price

    # Canonical news_event_entry tags
    assert pc["event"] == EVENT_NEWS_ENTRY == "news_event_entry"
    assert pc["purpose"] == PURPOSE_ENTRY == "entry"
    assert pc["side"] == SIDE_BUY == "buy"
    assert leg["side"] == "buy"
    assert pc["order_class"] == ORDER_CLASS_SIMPLE == "simple"
    assert pc["direction"] == "bullish"

    # Ticker is uppercased and propagated
    assert pc["ticker"] == "TESTBIO"
    assert leg["ticker"] == "TESTBIO"

    # Catalyst-type tagged for downstream audit
    assert pc["catalyst_type"] == "PDUFA"
    assert res.catalyst_type == "PDUFA"


# ---------------------------------------------------------------------------
# VAL-M3-049 — bearish → OTM put
# ---------------------------------------------------------------------------


def test_bearish_routes_to_otm_put():
    """Symmetric to -048: ``direction='bearish'`` → OTM put."""

    er = _make_ensemble_result(direction="bearish")
    cand = _candidate_event(matched_keywords="phase 3")
    stock_price = 100.0

    res = build_play_card(
        candidate_event=cand,
        ensemble_result=er,
        stock_price=stock_price,
        expiry="2026-07-17",
    )
    pc = res.play_card
    legs = pc["option_legs"]
    assert len(legs) == 1
    leg = legs[0]

    assert leg["option_type"] == "put"
    assert res.option_type == "put"
    assert leg["strike"] < stock_price
    assert res.strike < stock_price

    # Same canonical tags
    assert pc["event"] == "news_event_entry"
    assert pc["purpose"] == "entry"
    assert pc["side"] == "buy"
    assert pc["order_class"] == "simple"
    assert pc["direction"] == "bearish"
    # READOUT catalyst type for "phase 3" matched_keyword
    assert pc["catalyst_type"] == "READOUT"


# ---------------------------------------------------------------------------
# VAL-M3-050 — OTM offset reuses unified_scorer DEFAULT_OTM_BY_CATALYST
# ---------------------------------------------------------------------------


def test_otm_offset_reuses_unified_scorer_constant():
    """The OTM percentage used MUST equal the midpoint of
    DEFAULT_OTM_BY_CATALYST[catalyst_type]; no parallel hardcoded
    literal is allowed."""

    for catalyst_type, (lo, hi) in DEFAULT_OTM_BY_CATALYST.items():
        # Pick a matched_keywords token that derives to this catalyst
        matched = {
            "PDUFA": "pdufa",
            "READOUT": "phase 3",
            "LABEL_EXT": "snda",
            "ADCOM": "adcom",
            "CONTRACT": "contract award",
            "DEFAULT": "partnership",
        }[catalyst_type]
        er = _make_ensemble_result(direction="bullish")
        cand = _candidate_event(matched_keywords=matched)
        res = build_play_card(
            candidate_event=cand,
            ensemble_result=er,
            stock_price=100.0,
            expiry="2026-07-17",
        )
        expected_midpoint = (lo + hi) / 2.0
        assert res.catalyst_type == catalyst_type, (
            f"{matched!r} should derive to {catalyst_type}, got {res.catalyst_type}"
        )
        assert res.otm_pct == pytest.approx(expected_midpoint), (
            f"otm_pct for {catalyst_type} must be midpoint {expected_midpoint}, "
            f"got {res.otm_pct}"
        )


def test_otm_offset_constant_is_imported_not_redefined(tmp_path):
    """Defensive grep: the dispatcher source MUST import
    DEFAULT_OTM_BY_CATALYST from unified_scorer rather than redefining
    a parallel dict literal. (VAL-M3-050 negative control.)"""

    from pathlib import Path

    import biotech_sniper.exec.stage2_dispatcher as mod

    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert "DEFAULT_OTM_BY_CATALYST" in src
    # No re-definition: the dispatcher should not assign a new dict
    # literal of OTM percentages of its own.
    assert "DEFAULT_OTM_BY_CATALYST = {" not in src
    # And it must explicitly import from unified_scorer.
    assert "from biotech_sniper.sectors.unified_scorer" in src


# ---------------------------------------------------------------------------
# VAL-M3-051 — Catalyst-type keyword mapping uses real uppercase keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "matched_keywords,expected",
    [
        # PDUFA-family
        ("pdufa", "PDUFA"),
        ("fda approval", "PDUFA"),
        ("pdufa date", "PDUFA"),
        ("nda", "PDUFA"),
        ("bla", "PDUFA"),
        # READOUT-family
        ("phase 3", "READOUT"),
        ("p3 readout", "READOUT"),
        ("topline", "READOUT"),
        ("phase 2", "READOUT"),
        ("p2", "READOUT"),
        ("phase 1", "READOUT"),
        ("first-in-human", "READOUT"),
        # LABEL_EXT-family
        ("snda", "LABEL_EXT"),
        ("sbla", "LABEL_EXT"),
        ("label extension", "LABEL_EXT"),
        # ADCOM-family
        ("adcom", "ADCOM"),
        ("advisory committee", "ADCOM"),
        # CONTRACT-family
        ("contract award", "CONTRACT"),
        # DEFAULT-family (deals / m&a)
        ("partnership", "DEFAULT"),
        ("collaboration", "DEFAULT"),
        ("license deal", "DEFAULT"),
        ("acquisition", "DEFAULT"),
        ("merger", "DEFAULT"),
    ],
)
def test_catalyst_type_keyword_mapping(matched_keywords, expected):
    """Each parametrised input lands in one of the real uppercase
    DEFAULT_OTM_BY_CATALYST keys."""

    resolved = derive_catalyst_type(matched_keywords)
    assert resolved == expected, (
        f"{matched_keywords!r} → {resolved}, expected {expected}"
    )
    assert resolved in DEFAULT_OTM_BY_CATALYST


def test_catalyst_type_keys_are_uppercase_canonical():
    """The full keyset of DEFAULT_OTM_BY_CATALYST is the canonical
    uppercase set defined by VAL-M3-051. The dispatcher's catalyst-type
    derivation never returns a value outside this set."""

    expected_keys = {"PDUFA", "READOUT", "LABEL_EXT", "ADCOM", "CONTRACT", "DEFAULT"}
    assert expected_keys.issubset(set(DEFAULT_OTM_BY_CATALYST.keys()))


def test_derive_catalyst_type_accepts_csv_or_iterable():
    """matched_keywords is stored as comma-joined CSV in
    candidate_events; the dispatcher accepts both that and an
    iterable of keyword strings."""

    assert derive_catalyst_type("pdufa,fda approval") == "PDUFA"
    assert derive_catalyst_type(["pdufa", "fda approval"]) == "PDUFA"
    assert derive_catalyst_type(("phase 3",)) == "READOUT"
    assert derive_catalyst_type([" snda ", " label extension "]) == "LABEL_EXT"


# ---------------------------------------------------------------------------
# VAL-M3-052 — Ensemble direction overrides Stage-1 metadata
# ---------------------------------------------------------------------------


def test_ensemble_direction_overrides_stage1_metadata():
    """Stage-1 candidate_event metadata may carry stale or
    keyword-derived direction hints (e.g., a 'negative' keyword
    suggesting bearish). When the ensemble unanimously returns
    ``direction='bullish'``, the routed leg is a CALL — Stage-1
    metadata is ignored at routing time."""

    cand = _candidate_event(matched_keywords="failed,negative")
    # A Stage-1 row may also carry a stale top-level 'direction' field
    # from upstream heuristics — the dispatcher must NOT honour it.
    cand["direction"] = "bearish"

    er = _make_ensemble_result(direction="bullish")
    stock_price = 100.0
    res = build_play_card(
        candidate_event=cand,
        ensemble_result=er,
        stock_price=stock_price,
        expiry="2026-07-17",
    )
    pc = res.play_card
    leg = pc["option_legs"][0]
    assert leg["option_type"] == "call"
    assert leg["strike"] > stock_price
    assert pc["direction"] == "bullish"


def test_stage1_negative_keyword_does_not_force_put_when_ensemble_bullish():
    """Companion to -052: even if matched_keywords contains a
    negative-leaning token, ensemble bullish → CALL."""

    cand = _candidate_event(matched_keywords="adverse event,setback")
    er = _make_ensemble_result(direction="bullish")
    res = build_play_card(
        candidate_event=cand,
        ensemble_result=er,
        stock_price=100.0,
        expiry="2026-07-17",
    )
    leg = res.play_card["option_legs"][0]
    assert leg["option_type"] == "call"


def test_dispatcher_raises_when_ensemble_direction_missing():
    """Defensive: an ensemble result with ``direction=None``
    (caller failed the unanimity gate but invoked the dispatcher
    anyway) raises ``DirectionUnavailable`` rather than producing
    an order shape with no option_type."""

    er = _make_ensemble_result(direction=None)
    cand = _candidate_event()
    with pytest.raises(DirectionUnavailable):
        build_play_card(
            candidate_event=cand,
            ensemble_result=er,
            stock_price=100.0,
            expiry="2026-07-17",
        )


# ---------------------------------------------------------------------------
# VAL-M3-080 — Unknown matched_keywords fall back to DEFAULT (no KeyError)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "matched_keywords",
    [
        "partnership",
        "collaboration",
        "license deal",
        "acquisition",
        "merger",
        "unknown_phrase",
    ],
)
def test_unknown_catalyst_falls_back_to_default(matched_keywords):
    """The exact fixture matrix from VAL-M3-080 — every entry must
    derive to ``catalyst_type='DEFAULT'`` and the dispatcher must
    NOT raise ``KeyError``."""

    er = _make_ensemble_result(direction="bullish")
    cand = _candidate_event(matched_keywords=matched_keywords)

    # 1) derive_catalyst_type returns 'DEFAULT' directly.
    assert derive_catalyst_type(matched_keywords) == "DEFAULT"

    # 2) build_play_card succeeds (no KeyError) and the resulting
    #    catalyst_type / OTM offset are the DEFAULT midpoint.
    res = build_play_card(
        candidate_event=cand,
        ensemble_result=er,
        stock_price=100.0,
        expiry="2026-07-17",
    )
    assert res.catalyst_type == "DEFAULT"
    lo, hi = DEFAULT_OTM_BY_CATALYST["DEFAULT"]
    assert res.otm_pct == pytest.approx((lo + hi) / 2.0)


def test_empty_matched_keywords_falls_back_to_default():
    """Empty / None matched_keywords also lands in DEFAULT (no KeyError)."""

    assert derive_catalyst_type("") == "DEFAULT"
    assert derive_catalyst_type(None) == "DEFAULT"
    assert derive_catalyst_type([]) == "DEFAULT"


# ---------------------------------------------------------------------------
# VAL-M3-092 — Single-leg only (no multi-strike, no spreads)
# ---------------------------------------------------------------------------


def test_dispatcher_always_emits_single_leg():
    """The dispatcher's only emission shape is a single-leg play_card —
    a structural enforcement of the news_event_entry contract."""

    er = _make_ensemble_result(direction="bullish")
    cand = _candidate_event(matched_keywords="pdufa")
    res = build_play_card(
        candidate_event=cand,
        ensemble_result=er,
        stock_price=100.0,
        expiry="2026-07-17",
    )
    legs = res.play_card["option_legs"]
    assert isinstance(legs, list)
    assert len(legs) == 1


def test_assert_single_leg_for_news_entry_accepts_single_leg():
    """Defensive validator passes for a well-formed single-leg
    news_event_entry play_card."""

    er = _make_ensemble_result(direction="bullish")
    cand = _candidate_event()
    res = build_play_card(
        candidate_event=cand,
        ensemble_result=er,
        stock_price=100.0,
        expiry="2026-07-17",
    )
    # Returns the single leg dict for caller convenience; does not raise.
    leg = assert_single_leg_for_news_entry(res.play_card)
    assert leg is res.play_card["option_legs"][0]


def test_assert_single_leg_for_news_entry_rejects_multi_strike():
    """A multi-strike-shaped play_card carrying
    ``event='news_event_entry'`` raises the typed exception
    ``UnsupportedMultiStrikeForNewsEntry`` (VAL-M3-092)."""

    multi = {
        "play_card_id": "pc-multi",
        "ticker": "TESTBIO",
        "event": "news_event_entry",
        "order_class": "simple",
        "side": "buy",
        "purpose": "entry",
        "option_legs": [
            {"symbol": "TESTBIO260717C00120000", "option_type": "call",
             "strike": 120.0, "expiry": "2026-07-17", "side": "buy", "ticker": "TESTBIO"},
            {"symbol": "TESTBIO260717C00150000", "option_type": "call",
             "strike": 150.0, "expiry": "2026-07-17", "side": "buy", "ticker": "TESTBIO"},
        ],
    }
    with pytest.raises(UnsupportedMultiStrikeForNewsEntry):
        assert_single_leg_for_news_entry(multi)


def test_assert_single_leg_for_news_entry_rejects_zero_legs():
    """An empty option_legs list is also rejected as multi-strike-shaped
    (the contract is exactly-one)."""

    bad = {
        "event": "news_event_entry",
        "option_legs": [],
    }
    with pytest.raises(UnsupportedMultiStrikeForNewsEntry):
        assert_single_leg_for_news_entry(bad)


def test_assert_single_leg_for_news_entry_only_applies_to_news_entry():
    """For non-news_event_entry events the validator is a no-op: it
    does NOT raise on multi-strike daily-curated cards (those have
    their own dedicated handling in PaperExecutor)."""

    # event='open' (daily-curated multi-strike) — validator is a no-op.
    daily = {
        "event": "open",
        "option_legs": [
            {"symbol": "TESTBIO260717C00120000"},
            {"symbol": "TESTBIO260717C00150000"},
        ],
    }
    # Returns None; does not raise.
    assert assert_single_leg_for_news_entry(daily) is None
