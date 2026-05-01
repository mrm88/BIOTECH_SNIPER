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

from biotech_sniper import db as project_db
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


# ---------------------------------------------------------------------------
# f-fix-m5-03 — Rejection persistence with audit_path=None
# ---------------------------------------------------------------------------
#
# Pre-fix bug: ``_persist_skip`` short-circuited with ``return`` when
# ``audit_path is None`` BEFORE invoking ``record_stage2_skip``,
# producing zero ``news_match_log`` rows for every rejection scenario
# whose caller omitted the audit path. This violated the f-m5-03
# expectedBehavior bullet "Each rejection scenario writes
# news_match_log row with the documented reason" and contradicted the
# function's own preceding comment ("we still write the news_match_log
# row").
#
# Per VAL-M5-014..027 + f-m5-03's own design, EVERY rejection — both
# the four cheap-first paths (cooldown_active, armed_file_missing,
# daily_cap_exceeded) and both post-fanout paths (unanimity_failed,
# probability_below_threshold) — MUST persist exactly ONE
# ``news_match_log`` row with ``matched=0`` and the canonical reason,
# even when ``audit_path=None`` (i.e. the audit-JSON merge is skipped
# but the SQLite row is mandatory).


import datetime as f_fix_m5_03_dt
import sqlite3 as f_fix_m5_03_sqlite3
from pathlib import Path as F_FIX_M5_03_Path
from typing import Any as F_FIX_M5_03_Any


def f_fix_m5_03_seed_synthetic_news_event(db_path: F_FIX_M5_03_Path,
                                          ticker: str) -> int:
    """Insert one synthetic ``news_events`` row and return its id."""
    conn = f_fix_m5_03_sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            """
            INSERT INTO news_events (
                ticker, source, published_at, title, url, raw_payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                ticker,
                "test_rejection_persistence",
                "2026-04-30T14:00:00.000Z",
                f"{ticker} phase 3 readout primary endpoint",
                f"https://example.com/{ticker}-readout",
                f"{ticker} phase 3 readout primary endpoint",
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def f_fix_m5_03_seed_candidate_event(
    db_path: F_FIX_M5_03_Path,
    *,
    ticker: str,
    matched_keywords: str = "phase 3 readout",
) -> dict[str, F_FIX_M5_03_Any]:
    """Seed a news_events + candidate_events row pair; return candidate row dict."""
    news_event_id = f_fix_m5_03_seed_synthetic_news_event(db_path, ticker)
    conn = f_fix_m5_03_sqlite3.connect(str(db_path))
    conn.row_factory = f_fix_m5_03_sqlite3.Row
    try:
        with conn:
            cur = conn.execute(
                """
                INSERT INTO candidate_events (
                    ticker, source_news_event_id, matched_keywords,
                    calendar_match, emitted_at, dedup_key
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker,
                    news_event_id,
                    matched_keywords,
                    None,
                    "2026-04-30T14:00:01.000Z",
                    f"dedup-{ticker}-{news_event_id}",
                ),
            )
            cand_id = int(cur.lastrowid or 0)
        row = conn.execute(
            "SELECT id, ticker, source_news_event_id, matched_keywords, "
            "calendar_match, emitted_at, dedup_key "
            "FROM candidate_events WHERE id = ?",
            (cand_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row)


def f_fix_m5_03_news_match_log_rows(
    db_path: F_FIX_M5_03_Path,
    ticker: str,
) -> list[dict[str, F_FIX_M5_03_Any]]:
    conn = f_fix_m5_03_sqlite3.connect(str(db_path))
    conn.row_factory = f_fix_m5_03_sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT ticker, news_event_id, matched, reason, logged_at "
            "FROM news_match_log WHERE ticker = ? ORDER BY id ASC",
            (ticker,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def f_fix_m5_03_make_uniform_providers(
    *,
    label: str = "material",
    direction: str = "bullish",
    probability: float = 0.85,
):
    """Build {provider_name: callable} mapping returning identical payloads.

    Each callable accepts the ``(candidate, name=...)`` signature the
    ensemble fan-out invokes (per ``_run_one_provider``).
    """
    from biotech_sniper.llm.ensemble import ALL_PROVIDERS

    def _factory(provider_name: str):
        def _stub(_row, *, name: str = provider_name):
            return {
                "label": label,
                "probability": probability,
                "direction": direction,
                "rationale": f"{name}-stub",
                "citations": [],
                "latency_ms": 10,
                "cost_usd": 0.001,
            }

        return _stub

    return {p: _factory(p) for p in ALL_PROVIDERS}


def f_fix_m5_03_make_per_provider_providers(per_provider: dict):
    from biotech_sniper.llm.ensemble import ALL_PROVIDERS

    def _factory(provider_name: str):
        payload = dict(per_provider[provider_name])

        def _stub(_row, *, name: str = provider_name):
            return dict(payload)

        return _stub

    return {p: _factory(p) for p in ALL_PROVIDERS}


@pytest.fixture
def f_fix_m5_03_temp_db(tmp_path) -> F_FIX_M5_03_Path:
    """Fresh sqlite db at v10 (Reading-B foundations)."""
    from biotech_sniper.migrations.runner import run as _run

    p = tmp_path / "stage2_persist_skip.db"
    _run(p, target_version=project_db.CURRENT_VERSION, take_backup_first=False)
    return p


@pytest.fixture
def f_fix_m5_03_armed_present(tmp_path) -> F_FIX_M5_03_Path:
    p = tmp_path / "armed_present" / ".armed"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")
    return p


@pytest.fixture
def f_fix_m5_03_armed_missing(tmp_path) -> F_FIX_M5_03_Path:
    return tmp_path / "armed_missing" / ".armed"


@pytest.fixture
def f_fix_m5_03_audit_path_absent(tmp_path) -> F_FIX_M5_03_Path:
    """Path to an audit_latest.json that MUST remain absent post-test."""
    return tmp_path / "state" / "audit_latest.json"


class TestRejectionPersistenceWithAuditPathNone:
    """f-fix-m5-03 — Each rejection writes ONE news_match_log row even when
    audit_path is None.

    Pre-fix: ``_persist_skip`` returned early when ``audit_path is None``,
    silently dropping the news_match_log write for every rejection
    scenario whose caller omitted the audit path. Post-fix: the
    short-circuit is removed; ``record_stage2_skip`` is invoked
    unconditionally (db_path permitting); the audit-JSON merge is
    skipped silently inside the recorder when ``audit_path is None``.

    Each parametrised case asserts:
    * ``run_stage2_chain(...)`` rejects with the expected canonical reason.
    * ``news_match_log`` has EXACTLY ONE row for the ticker with
      ``matched=0`` and ``reason=<canonical>``.
    * ``state/audit_latest.json`` was NOT created (file is absent).
    """

    def test_cooldown_rejection_persists_news_match_log_with_audit_path_none(
        self,
        f_fix_m5_03_temp_db,
        f_fix_m5_03_armed_present,
        f_fix_m5_03_audit_path_absent,
    ):
        from biotech_sniper.exec.stage2_dispatcher import run_stage2_chain
        from biotech_sniper.llm.stage2_gates import (
            GATE_REASON_COOLDOWN_ACTIVE,
        )

        ticker = "COOL"
        candidate = f_fix_m5_03_seed_candidate_event(
            f_fix_m5_03_temp_db, ticker=ticker
        )

        # Seed cooldown active 1h ago against 24h window.
        now = f_fix_m5_03_dt.datetime(
            2026, 4, 30, 12, 0, 0, tzinfo=f_fix_m5_03_dt.timezone.utc,
        )
        one_hour_ago = now - f_fix_m5_03_dt.timedelta(hours=1)
        last_entry_at = (
            one_hour_ago.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        )
        conn = f_fix_m5_03_sqlite3.connect(str(f_fix_m5_03_temp_db))
        try:
            with conn:
                conn.execute(
                    "INSERT INTO ticker_cooldown "
                    "(ticker, last_entry_at, last_event_id, cooldown_hours) "
                    "VALUES (?, ?, NULL, 24)",
                    (ticker, last_entry_at),
                )
        finally:
            conn.close()

        result = run_stage2_chain(
            candidate_event_row=candidate,
            db_path=f_fix_m5_03_temp_db,
            armed_path=f_fix_m5_03_armed_present,
            audit_path=None,  # <-- THE bug condition
            now=now,
            providers=f_fix_m5_03_make_uniform_providers(),
        )

        assert result.passed is False
        assert result.reason == GATE_REASON_COOLDOWN_ACTIVE

        # Exactly ONE row in news_match_log with matched=0 and canonical reason.
        rows = f_fix_m5_03_news_match_log_rows(f_fix_m5_03_temp_db, ticker)
        assert len(rows) == 1, rows
        assert rows[0]["ticker"] == ticker
        assert rows[0]["matched"] == 0
        assert rows[0]["reason"] == GATE_REASON_COOLDOWN_ACTIVE

        # ZERO audit_latest.json writes.
        assert not f_fix_m5_03_audit_path_absent.exists()

    def test_armed_missing_rejection_persists_news_match_log_with_audit_path_none(
        self,
        f_fix_m5_03_temp_db,
        f_fix_m5_03_armed_missing,
        f_fix_m5_03_audit_path_absent,
    ):
        from biotech_sniper.exec.stage2_dispatcher import run_stage2_chain
        from biotech_sniper.llm.stage2_gates import (
            GATE_REASON_ARMED_FILE_MISSING,
        )

        ticker = "ARMD"
        candidate = f_fix_m5_03_seed_candidate_event(
            f_fix_m5_03_temp_db, ticker=ticker
        )

        assert not f_fix_m5_03_armed_missing.exists()

        result = run_stage2_chain(
            candidate_event_row=candidate,
            db_path=f_fix_m5_03_temp_db,
            armed_path=f_fix_m5_03_armed_missing,
            audit_path=None,  # <-- THE bug condition
            providers=f_fix_m5_03_make_uniform_providers(),
        )

        assert result.passed is False
        assert result.reason == GATE_REASON_ARMED_FILE_MISSING

        rows = f_fix_m5_03_news_match_log_rows(f_fix_m5_03_temp_db, ticker)
        assert len(rows) == 1, rows
        assert rows[0]["ticker"] == ticker
        assert rows[0]["matched"] == 0
        assert rows[0]["reason"] == GATE_REASON_ARMED_FILE_MISSING

        assert not f_fix_m5_03_audit_path_absent.exists()

    def test_cap_rejection_persists_news_match_log_with_audit_path_none(
        self,
        f_fix_m5_03_temp_db,
        f_fix_m5_03_armed_present,
        f_fix_m5_03_audit_path_absent,
    ):
        from biotech_sniper.exec.stage2_dispatcher import run_stage2_chain
        from biotech_sniper.llm.stage2_gates import (
            GATE_REASON_DAILY_CAP_EXCEEDED,
            STAGE2_LEDGER_PURPOSE,
        )

        ticker = "CAPP"
        candidate = f_fix_m5_03_seed_candidate_event(
            f_fix_m5_03_temp_db, ticker=ticker
        )

        # Seed today's stage2 ledger to $20 — projection ($0.50) trips cap.
        conn = f_fix_m5_03_sqlite3.connect(str(f_fix_m5_03_temp_db))
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO llm_cost_ledger (
                        provider, model_id, purpose, cost_usd, called_at
                    ) VALUES (
                        'perplexity', 'sonar', ?, 20.00,
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    )
                    """,
                    (STAGE2_LEDGER_PURPOSE,),
                )
        finally:
            conn.close()

        result = run_stage2_chain(
            candidate_event_row=candidate,
            db_path=f_fix_m5_03_temp_db,
            armed_path=f_fix_m5_03_armed_present,
            audit_path=None,  # <-- THE bug condition
            providers=f_fix_m5_03_make_uniform_providers(),
        )

        assert result.passed is False
        assert result.reason == GATE_REASON_DAILY_CAP_EXCEEDED

        rows = f_fix_m5_03_news_match_log_rows(f_fix_m5_03_temp_db, ticker)
        assert len(rows) == 1, rows
        assert rows[0]["ticker"] == ticker
        assert rows[0]["matched"] == 0
        assert rows[0]["reason"] == GATE_REASON_DAILY_CAP_EXCEEDED

        assert not f_fix_m5_03_audit_path_absent.exists()

    def test_unanimity_rejection_persists_news_match_log_with_audit_path_none(
        self,
        f_fix_m5_03_temp_db,
        f_fix_m5_03_armed_present,
        f_fix_m5_03_audit_path_absent,
    ):
        from biotech_sniper.exec.stage2_dispatcher import run_stage2_chain
        from biotech_sniper.llm.stage2_gates import (
            GATE_REASON_UNANIMITY_FAILED,
        )

        ticker = "UNAN"
        candidate = f_fix_m5_03_seed_candidate_event(
            f_fix_m5_03_temp_db, ticker=ticker
        )

        # 3 material + 1 non_material → unanimity gate fails.
        per_provider = {
            "xai": {"label": "material", "probability": 0.85, "direction": "bullish",
                    "rationale": "x", "citations": [], "latency_ms": 10, "cost_usd": 0.001},
            "anthropic": {"label": "material", "probability": 0.85, "direction": "bullish",
                          "rationale": "a", "citations": [], "latency_ms": 10, "cost_usd": 0.001},
            "gemini": {"label": "material", "probability": 0.85, "direction": "bullish",
                       "rationale": "g", "citations": [], "latency_ms": 10, "cost_usd": 0.001},
            "perplexity": {"label": "non_material", "probability": 0.85,
                           "direction": "bullish", "rationale": "p", "citations": [],
                           "latency_ms": 10, "cost_usd": 0.001},
        }

        result = run_stage2_chain(
            candidate_event_row=candidate,
            db_path=f_fix_m5_03_temp_db,
            armed_path=f_fix_m5_03_armed_present,
            audit_path=None,  # <-- THE bug condition
            providers=f_fix_m5_03_make_per_provider_providers(per_provider),
        )

        assert result.passed is False
        assert result.reason == GATE_REASON_UNANIMITY_FAILED

        rows = f_fix_m5_03_news_match_log_rows(f_fix_m5_03_temp_db, ticker)
        assert len(rows) == 1, rows
        assert rows[0]["ticker"] == ticker
        assert rows[0]["matched"] == 0
        assert rows[0]["reason"] == GATE_REASON_UNANIMITY_FAILED

        assert not f_fix_m5_03_audit_path_absent.exists()

    def test_probability_rejection_persists_news_match_log_with_audit_path_none(
        self,
        f_fix_m5_03_temp_db,
        f_fix_m5_03_armed_present,
        f_fix_m5_03_audit_path_absent,
    ):
        from biotech_sniper.exec.stage2_dispatcher import run_stage2_chain
        from biotech_sniper.llm.stage2_gates import (
            GATE_REASON_PROBABILITY_BELOW_THRESHOLD,
        )

        ticker = "PROB"
        candidate = f_fix_m5_03_seed_candidate_event(
            f_fix_m5_03_temp_db, ticker=ticker
        )

        # 4/4 material; mean(0.6, 0.65, 0.7, 0.7) = 0.6625 < 0.75.
        probs = {"xai": 0.6, "anthropic": 0.65, "gemini": 0.7, "perplexity": 0.7}
        per_provider = {
            name: {
                "label": "material",
                "probability": probs[name],
                "direction": "bullish",
                "rationale": f"{name}",
                "citations": [],
                "latency_ms": 10,
                "cost_usd": 0.001,
            }
            for name in probs
        }

        result = run_stage2_chain(
            candidate_event_row=candidate,
            db_path=f_fix_m5_03_temp_db,
            armed_path=f_fix_m5_03_armed_present,
            audit_path=None,  # <-- THE bug condition
            providers=f_fix_m5_03_make_per_provider_providers(per_provider),
        )

        assert result.passed is False
        assert result.reason == GATE_REASON_PROBABILITY_BELOW_THRESHOLD

        rows = f_fix_m5_03_news_match_log_rows(f_fix_m5_03_temp_db, ticker)
        assert len(rows) == 1, rows
        assert rows[0]["ticker"] == ticker
        assert rows[0]["matched"] == 0
        assert rows[0]["reason"] == GATE_REASON_PROBABILITY_BELOW_THRESHOLD

        assert not f_fix_m5_03_audit_path_absent.exists()
