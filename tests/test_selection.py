"""Tests for f-m2-11 selection logic.

Covers:

* :func:`biotech_sniper.sectors.unified_scorer.select_top_n` —
  ordering, threshold filters (``MIN_ENSEMBLE_SCORE`` /
  ``MIN_SCIENCE_GRADE``), the N (max_concurrent) cap, and
  deterministic tie-breaking.
* :func:`biotech_sniper.play_card_formatter.emit_play_cards` — the
  on-disk ``play_cards/YYYY-MM-DD/<TICKER>.json`` directory is set-
  equal to the ``select_top_n`` return list and written in score-
  desc order.
* :data:`biotech_sniper.config.MIN_ENSEMBLE_SCORE` /
  ``MIN_SCIENCE_GRADE`` are present, importable, and have the
  documented defaults.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from biotech_sniper import config, db
from biotech_sniper.sectors.unified_scorer import (
    LETTER_GRADE_ORDER,
    select_top_n,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Path:
    """Create a fresh ``alpha_sniper.db`` with the project schema."""
    target = tmp_path / "data" / "alpha_sniper.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(target)
    try:
        db.run_migrations(conn)
    finally:
        conn.close()
    return target


def _insert_scoring_cache_row(
    db_path: Path,
    *,
    ticker: str,
    as_of_date: str,
    ensemble_score: float | None,
    science_grade: str | None,
    grok_score: float | None = 0.5,
    claude_grade: str | None = "B",
    claude_probability: float | None = 0.6,
    gemini_grade: str | None = "B",
    gemini_probability: float | None = 0.6,
    divergence_flag: int = 0,
    payload: str | None = None,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO scoring_cache (ticker, as_of_date, grok_rank, "
            "grok_score, claude_grade, claude_probability, gemini_grade, "
            "gemini_probability, science_grade, ensemble_score, "
            "divergence_flag, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?)",
            (
                ticker,
                as_of_date,
                None,
                grok_score,
                claude_grade,
                claude_probability,
                gemini_grade,
                gemini_probability,
                science_grade,
                ensemble_score,
                int(divergence_flag),
                payload or json.dumps({"fixture": True}),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# config defaults
# ---------------------------------------------------------------------------


def test_config_exposes_min_ensemble_score_default() -> None:
    """``MIN_ENSEMBLE_SCORE`` defaults to the documented value (``0.55``)."""
    assert hasattr(config, "MIN_ENSEMBLE_SCORE")
    assert isinstance(config.MIN_ENSEMBLE_SCORE, float)
    assert config.MIN_ENSEMBLE_SCORE == pytest.approx(0.55)


def test_config_exposes_min_science_grade_default() -> None:
    """``MIN_SCIENCE_GRADE`` defaults to the documented ``"C+"``."""
    assert hasattr(config, "MIN_SCIENCE_GRADE")
    assert config.MIN_SCIENCE_GRADE == "C+"
    # Must be a valid member of the canonical letter-grade ordering so
    # downstream callers can translate it into an allowed-list.
    assert config.MIN_SCIENCE_GRADE in LETTER_GRADE_ORDER


# ---------------------------------------------------------------------------
# select_top_n — ordering
# ---------------------------------------------------------------------------


def test_select_top_n_orders_by_ensemble_score_desc(tmp_path: Path) -> None:
    """Survivors must be returned in ``ensemble_score DESC`` order."""
    db_path = _make_db(tmp_path)
    rows = [
        ("AAA", 0.60),
        ("BBB", 0.90),
        ("CCC", 0.75),
        ("DDD", 0.65),
    ]
    for ticker, score in rows:
        _insert_scoring_cache_row(
            db_path,
            ticker=ticker,
            as_of_date="2025-04-26",
            ensemble_score=score,
            science_grade="B",
        )

    result = select_top_n("2025-04-26", n=10, db_path=db_path)
    tickers = [r["ticker"] for r in result]
    assert tickers == ["BBB", "CCC", "DDD", "AAA"]


# ---------------------------------------------------------------------------
# select_top_n — threshold filters
# ---------------------------------------------------------------------------


def test_select_top_n_filters_below_min_ensemble_score(tmp_path: Path) -> None:
    """Rows with ``ensemble_score < MIN_ENSEMBLE_SCORE`` are dropped."""
    db_path = _make_db(tmp_path)
    # MIN_ENSEMBLE_SCORE default = 0.55
    _insert_scoring_cache_row(
        db_path,
        ticker="HIGH",
        as_of_date="2025-04-26",
        ensemble_score=0.80,
        science_grade="B",
    )
    _insert_scoring_cache_row(
        db_path,
        ticker="EQUAL",  # exactly at the threshold — must pass
        as_of_date="2025-04-26",
        ensemble_score=0.55,
        science_grade="B",
    )
    _insert_scoring_cache_row(
        db_path,
        ticker="LOW",
        as_of_date="2025-04-26",
        ensemble_score=0.40,
        science_grade="B",
    )
    _insert_scoring_cache_row(
        db_path,
        ticker="NULLSCORE",
        as_of_date="2025-04-26",
        ensemble_score=None,
        science_grade="A",
    )

    result = select_top_n("2025-04-26", n=10, db_path=db_path)
    tickers = {r["ticker"] for r in result}
    assert tickers == {"HIGH", "EQUAL"}
    # And ordering is still preserved within the survivors.
    assert [r["ticker"] for r in result] == ["HIGH", "EQUAL"]


def test_select_top_n_filters_below_min_science_grade(tmp_path: Path) -> None:
    """Rows with worse-than-``MIN_SCIENCE_GRADE`` are dropped.

    Default ``MIN_SCIENCE_GRADE = "C+"`` admits A+/A/A-/B+/B/B-/C+
    and rejects C / C- / D / F. Naive lexicographic SQL ``>=`` would
    accept ``"D"`` (greater than ``"C+"`` in ASCII) so this test
    pins the canonical-ordering behaviour.
    """
    db_path = _make_db(tmp_path)
    _insert_scoring_cache_row(
        db_path,
        ticker="GRADE_A",
        as_of_date="2025-04-26",
        ensemble_score=0.80,
        science_grade="A",
    )
    _insert_scoring_cache_row(
        db_path,
        ticker="GRADE_BMINUS",
        as_of_date="2025-04-26",
        ensemble_score=0.78,
        science_grade="B-",
    )
    _insert_scoring_cache_row(
        db_path,
        ticker="GRADE_CPLUS",  # exactly at threshold — must pass
        as_of_date="2025-04-26",
        ensemble_score=0.77,
        science_grade="C+",
    )
    _insert_scoring_cache_row(
        db_path,
        ticker="GRADE_C",
        as_of_date="2025-04-26",
        ensemble_score=0.85,
        science_grade="C",
    )
    _insert_scoring_cache_row(
        db_path,
        ticker="GRADE_D",
        as_of_date="2025-04-26",
        ensemble_score=0.99,  # high score must NOT save a D grade
        science_grade="D",
    )
    _insert_scoring_cache_row(
        db_path,
        ticker="GRADE_F",
        as_of_date="2025-04-26",
        ensemble_score=0.95,
        science_grade="F",
    )
    _insert_scoring_cache_row(
        db_path,
        ticker="GRADE_NULL",
        as_of_date="2025-04-26",
        ensemble_score=0.70,
        science_grade=None,
    )

    result = select_top_n("2025-04-26", n=10, db_path=db_path)
    tickers = {r["ticker"] for r in result}
    assert tickers == {"GRADE_A", "GRADE_BMINUS", "GRADE_CPLUS"}


def test_select_top_n_combined_thresholds(tmp_path: Path) -> None:
    """Both filters must apply (ensemble_score AND science_grade)."""
    db_path = _make_db(tmp_path)
    _insert_scoring_cache_row(
        db_path,
        ticker="PASS",
        as_of_date="2025-04-26",
        ensemble_score=0.70,
        science_grade="B",
    )
    _insert_scoring_cache_row(
        db_path,
        ticker="LOW_SCORE",
        as_of_date="2025-04-26",
        ensemble_score=0.30,
        science_grade="A",
    )
    _insert_scoring_cache_row(
        db_path,
        ticker="LOW_GRADE",
        as_of_date="2025-04-26",
        ensemble_score=0.95,
        science_grade="D",
    )

    result = select_top_n("2025-04-26", n=10, db_path=db_path)
    assert [r["ticker"] for r in result] == ["PASS"]


def test_select_top_n_threshold_overrides_take_effect(tmp_path: Path) -> None:
    """Caller-supplied overrides bypass the config defaults."""
    db_path = _make_db(tmp_path)
    _insert_scoring_cache_row(
        db_path,
        ticker="CALL_C",
        as_of_date="2025-04-26",
        ensemble_score=0.40,
        science_grade="C",
    )
    _insert_scoring_cache_row(
        db_path,
        ticker="CALL_D",
        as_of_date="2025-04-26",
        ensemble_score=0.42,
        science_grade="D",
    )

    # With the documented defaults both rows fail.
    assert select_top_n("2025-04-26", n=10, db_path=db_path) == []

    # Lowering both thresholds explicitly admits both rows in
    # score-desc order.
    relaxed = select_top_n(
        "2025-04-26",
        n=10,
        min_ensemble_score=0.30,
        min_science_grade="D",
        db_path=db_path,
    )
    assert [r["ticker"] for r in relaxed] == ["CALL_D", "CALL_C"]


# ---------------------------------------------------------------------------
# select_top_n — N cap (max_concurrent)
# ---------------------------------------------------------------------------


def test_select_top_n_caps_at_n(tmp_path: Path) -> None:
    """Callers asking for ``n`` get at most ``n`` rows back."""
    db_path = _make_db(tmp_path)
    for idx, score in enumerate([0.95, 0.90, 0.85, 0.80, 0.75, 0.70]):
        _insert_scoring_cache_row(
            db_path,
            ticker=f"T{idx}",
            as_of_date="2025-04-26",
            ensemble_score=score,
            science_grade="A",
        )

    result = select_top_n(
        "2025-04-26",
        n=config.RISK_DEFAULTS["max_concurrent"],  # =4
        db_path=db_path,
    )
    assert len(result) == 4
    # Highest 4 scores in order.
    assert [r["ticker"] for r in result] == ["T0", "T1", "T2", "T3"]


def test_select_top_n_n_zero_returns_empty(tmp_path: Path) -> None:
    """``n <= 0`` returns ``[]`` without touching the db."""
    db_path = _make_db(tmp_path)
    _insert_scoring_cache_row(
        db_path,
        ticker="ANY",
        as_of_date="2025-04-26",
        ensemble_score=0.99,
        science_grade="A",
    )
    assert select_top_n("2025-04-26", n=0, db_path=db_path) == []
    assert select_top_n("2025-04-26", n=-3, db_path=db_path) == []


def test_select_top_n_returns_empty_when_db_missing(tmp_path: Path) -> None:
    """Cold-start (no db file yet) returns ``[]`` rather than raising."""
    missing = tmp_path / "data" / "does_not_exist.db"
    assert select_top_n("2025-04-26", n=4, db_path=missing) == []


# ---------------------------------------------------------------------------
# select_top_n — deterministic tie-break
# ---------------------------------------------------------------------------


def test_select_top_n_breaks_ties_deterministically(tmp_path: Path) -> None:
    """Equal scores resolve in ``ticker ASC`` order."""
    db_path = _make_db(tmp_path)
    # Insert in a deliberately non-alphabetical / non-insertion order
    # so a "stable sort by insertion time" implementation would fail.
    for ticker in ["MNO", "ABC", "XYZ", "DEF"]:
        _insert_scoring_cache_row(
            db_path,
            ticker=ticker,
            as_of_date="2025-04-26",
            ensemble_score=0.80,  # identical scores → tie
            science_grade="B",
        )

    first_run = select_top_n("2025-04-26", n=10, db_path=db_path)
    assert [r["ticker"] for r in first_run] == ["ABC", "DEF", "MNO", "XYZ"]

    # Run again — must produce the same order. (Reproducibility is the
    # property under test; a non-deterministic ORDER BY would surface
    # here even if the first run happened to be alphabetical by luck.)
    second_run = select_top_n("2025-04-26", n=10, db_path=db_path)
    assert [r["ticker"] for r in second_run] == [
        r["ticker"] for r in first_run
    ]


def test_select_top_n_other_dates_excluded(tmp_path: Path) -> None:
    """Only rows with the matching ``as_of_date`` are returned."""
    db_path = _make_db(tmp_path)
    _insert_scoring_cache_row(
        db_path,
        ticker="TODAY",
        as_of_date="2025-04-26",
        ensemble_score=0.80,
        science_grade="B",
    )
    _insert_scoring_cache_row(
        db_path,
        ticker="YESTERDAY",
        as_of_date="2025-04-25",
        ensemble_score=0.99,
        science_grade="A",
    )
    result = select_top_n("2025-04-26", n=10, db_path=db_path)
    assert [r["ticker"] for r in result] == ["TODAY"]


# ---------------------------------------------------------------------------
# play_card_formatter.emit_play_cards — directory mirrors select_top_n
# ---------------------------------------------------------------------------


def test_emit_play_cards_writes_set_equal_to_select_top_n(
    tmp_path: Path,
) -> None:
    """Files in ``play_cards/<date>/`` map exactly to the SQL selection."""
    from biotech_sniper.play_card_formatter import emit_play_cards

    db_path = _make_db(tmp_path)
    base_dir = tmp_path / "repo"
    base_dir.mkdir()

    # Three pass + two filtered-out rows.
    fixtures = [
        ("ALPHA", 0.95, "A"),
        ("BETA", 0.65, "B"),
        ("GAMMA", 0.80, "B-"),
        ("LOW_SCORE", 0.20, "A"),  # filtered (score)
        ("LOW_GRADE", 0.99, "F"),  # filtered (grade)
    ]
    for ticker, score, grade in fixtures:
        _insert_scoring_cache_row(
            db_path,
            ticker=ticker,
            as_of_date="2025-04-26",
            ensemble_score=score,
            science_grade=grade,
        )

    written = emit_play_cards(
        as_of_date="2025-04-26",
        n=10,
        base_dir=base_dir,
        db_path=db_path,
    )

    # The written list mirrors select_top_n's score-desc order.
    expected_ordered = ["ALPHA", "GAMMA", "BETA"]
    assert [p.stem for p in written] == expected_ordered

    out_dir = base_dir / "play_cards" / "2025-04-26"
    assert out_dir.is_dir()
    on_disk = sorted(p.name for p in out_dir.glob("*.json"))
    assert on_disk == sorted(f"{t}.json" for t in expected_ordered)

    # Per-card payload includes rank reflecting score-desc order.
    alpha = json.loads((out_dir / "ALPHA.json").read_text())
    gamma = json.loads((out_dir / "GAMMA.json").read_text())
    beta = json.loads((out_dir / "BETA.json").read_text())
    assert alpha["rank"] == 1
    assert gamma["rank"] == 2
    assert beta["rank"] == 3
    # ``grade`` field is the science_grade — needed for VAL-M2-084.
    assert alpha["grade"] == "A"
    assert gamma["grade"] == "B-"
    assert beta["grade"] == "B"
    # Filtered tickers do NOT have files.
    assert not (out_dir / "LOW_SCORE.json").exists()
    assert not (out_dir / "LOW_GRADE.json").exists()


def test_emit_play_cards_caps_at_max_concurrent(tmp_path: Path) -> None:
    """When more than ``n`` candidates pass thresholds, only N files land."""
    from biotech_sniper.play_card_formatter import emit_play_cards

    db_path = _make_db(tmp_path)
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    for idx, score in enumerate([0.95, 0.90, 0.85, 0.80, 0.75, 0.70]):
        _insert_scoring_cache_row(
            db_path,
            ticker=f"T{idx}",
            as_of_date="2025-04-26",
            ensemble_score=score,
            science_grade="A",
        )

    emit_play_cards(
        as_of_date="2025-04-26",
        n=4,  # max_concurrent
        base_dir=base_dir,
        db_path=db_path,
    )

    out_dir = base_dir / "play_cards" / "2025-04-26"
    files = sorted(p.name for p in out_dir.glob("*.json"))
    assert files == ["T0.json", "T1.json", "T2.json", "T3.json"]


def test_emit_play_cards_clears_stale_entries_on_rerun(
    tmp_path: Path,
) -> None:
    """Re-emission removes yesterday's stale tickers from the directory."""
    from biotech_sniper.play_card_formatter import emit_play_cards

    db_path = _make_db(tmp_path)
    base_dir = tmp_path / "repo"
    base_dir.mkdir()

    _insert_scoring_cache_row(
        db_path,
        ticker="OLD",
        as_of_date="2025-04-26",
        ensemble_score=0.80,
        science_grade="A",
    )
    emit_play_cards(
        as_of_date="2025-04-26", n=4, base_dir=base_dir, db_path=db_path,
    )

    out_dir = base_dir / "play_cards" / "2025-04-26"
    assert (out_dir / "OLD.json").exists()

    # Replace OLD with NEW (e.g. OLD's score dropped below threshold)
    # and re-emit. Simulate by deleting OLD's row and inserting NEW.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM scoring_cache WHERE ticker='OLD'")
        conn.commit()
    finally:
        conn.close()
    _insert_scoring_cache_row(
        db_path,
        ticker="NEW",
        as_of_date="2025-04-26",
        ensemble_score=0.85,
        science_grade="A",
    )
    emit_play_cards(
        as_of_date="2025-04-26", n=4, base_dir=base_dir, db_path=db_path,
    )

    files = sorted(p.name for p in out_dir.glob("*.json"))
    assert files == ["NEW.json"]
