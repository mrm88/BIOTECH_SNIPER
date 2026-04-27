"""Tests for :mod:`biotech_sniper.llm.ensemble`.

These tests are fully hermetic: the live xAI / Anthropic / Gemini APIs
are never hit. We inject tiny duck-typed fakes that mimic the
``score_ticker`` / ``deep_science_review`` surface of the real
clients and assert on the orchestration math, divergence detection,
SQLite persistence, and feature-flag toggle behaviour.

Test matrix
-----------
* ``test_weighting_formula_documented`` — fixed inputs against the
  documented formula in :data:`biotech_sniper.config.ENSEMBLE_WEIGHTS`
  (VAL-M2-044).
* ``test_divergence_flag_two_level_gap`` — Claude/Gemini grade gap
  matrix (VAL-M2-045).
* ``test_persists_to_scoring_cache`` — round-trip through SQLite
  (VAL-M2-046).
* ``test_fast_only_mode`` — ``ensemble_score == grok_score`` when
  the deep tier is disabled (VAL-M2-047).
* ``test_anthropic_only_deep`` — Gemini disabled but Claude active;
  ``divergence_flag`` is ``False`` because there is no second deep
  source.
* ``test_all_on_mode`` — all three clients active; divergence flag
  reflects the deep-tier disagreement.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

import pytest

from biotech_sniper import config, db
from biotech_sniper.llm import ensemble
from biotech_sniper.llm.ensemble import (
    DIVERGENCE_THRESHOLD,
    EnsembleError,
    EnsembleScorer,
    compute_ensemble,
    letter_grade_distance,
)


# ---------------------------------------------------------------------------
# Lightweight fakes — single-call replays for the three clients.
# ---------------------------------------------------------------------------


class _FakeXAIClient:
    """Stand-in for :class:`biotech_sniper.llm.xai_client.XAIClient`.

    Returns a canned ``score_ticker`` result and records the call.
    """

    def __init__(self, *, probability: float, rationale: str = "fast rationale"):
        self.probability = float(probability)
        self.rationale = rationale
        self.calls: list[tuple[str, dict]] = []

    def score_ticker(self, ticker: str, context: Mapping[str, Any]) -> dict:
        self.calls.append((ticker, dict(context)))
        return {
            "probability": self.probability,
            "rationale": self.rationale,
            "confidence": 0.85,
            "model_id": "grok-4-fixture",
            "latency_ms": 12,
            "cost_usd": 0.0,
        }


class _FakeDeepClient:
    """Shared fake for Claude and Gemini deep-tier clients."""

    def __init__(
        self,
        *,
        letter_grade: str,
        probability: float,
        rationale: str,
        model_id: str,
    ):
        self.letter_grade = letter_grade
        self.probability = float(probability)
        self.rationale = rationale
        self.model_id = model_id
        self.calls: list[tuple[dict, dict]] = []

    def deep_science_review(
        self, science_profile: Mapping[str, Any], full_context: Mapping[str, Any]
    ) -> dict:
        self.calls.append((dict(science_profile), dict(full_context)))
        return {
            "science_profile": dict(science_profile),
            "letter_grade": self.letter_grade,
            "probability": self.probability,
            "rationale": self.rationale,
            "citations": [{"source": "fixture", "quote_or_url": "fixture-citation"}],
            "model_id": self.model_id,
            "latency_ms": 42,
            "cost_usd": 0.0,
        }


# ---------------------------------------------------------------------------
# compute_ensemble — pure math
# ---------------------------------------------------------------------------


def test_weighting_formula_documented() -> None:
    """Fixed inputs against the documented weighting formula.

    Exercises the canonical formula::

        ensemble_score = w_grok*grok_score
                       + w_claude*claude_probability
                       + w_gemini*gemini_probability

    using the default weights from :data:`config.ENSEMBLE_WEIGHTS`.
    """
    weights = config.ENSEMBLE_WEIGHTS
    assert pytest.approx(sum(weights.values())) == 1.0

    grok = 0.80
    claude_p = 0.60
    gemini_p = 0.40

    out = compute_ensemble(
        grok_score=grok,
        claude_grade="A",
        claude_probability=claude_p,
        gemini_grade="A-",
        gemini_probability=gemini_p,
    )

    expected = (
        weights["grok"] * grok
        + weights["claude"] * claude_p
        + weights["gemini"] * gemini_p
    )
    assert out["ensemble_score"] == pytest.approx(expected)
    assert out["fast_score"] == pytest.approx(grok)
    # deep_score is the renormalised weighted average over only the
    # active deep providers (Claude + Gemini here).
    deep_total = weights["claude"] + weights["gemini"]
    expected_deep = (
        (weights["claude"] / deep_total) * claude_p
        + (weights["gemini"] / deep_total) * gemini_p
    )
    assert out["deep_score"] == pytest.approx(expected_deep)
    assert out["divergence_flag"] is False
    assert set(out["active_weights"]) == {"grok", "claude", "gemini"}
    assert pytest.approx(sum(out["active_weights"].values())) == 1.0


def test_weighting_with_provider_disabled_renormalises() -> None:
    """When Gemini is disabled, the remaining weights renormalise to 1.0."""
    grok = 0.50
    claude_p = 0.70
    out = compute_ensemble(
        grok_score=grok,
        claude_grade="B",
        claude_probability=claude_p,
        gemini_grade=None,
        gemini_probability=None,
    )
    weights = config.ENSEMBLE_WEIGHTS
    base_total = weights["grok"] + weights["claude"]
    expected_w_grok = weights["grok"] / base_total
    expected_w_claude = weights["claude"] / base_total
    expected_score = expected_w_grok * grok + expected_w_claude * claude_p
    assert out["ensemble_score"] == pytest.approx(expected_score)
    assert pytest.approx(sum(out["active_weights"].values())) == 1.0
    assert out["divergence_flag"] is False  # only one deep source


def test_compute_ensemble_fast_only_returns_grok_score() -> None:
    out = compute_ensemble(
        grok_score=0.42,
        claude_grade=None,
        claude_probability=None,
        gemini_grade=None,
        gemini_probability=None,
    )
    assert out["ensemble_score"] == pytest.approx(0.42)
    assert out["deep_score"] is None
    assert out["divergence_flag"] is False


# ---------------------------------------------------------------------------
# Divergence detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claude_grade", "gemini_grade", "expected_flag"),
    [
        # Per VAL-M2-045: A vs B- is the canonical divergence example.
        ("A", "B-", True),
        # B+ vs B is a one-grade gap and must NOT trigger.
        ("B+", "B", False),
        # A+ vs B+ is a 3-grade gap — divergence.
        ("A+", "B+", True),
        # Identical grades — never divergent.
        ("A-", "A-", False),
        # Two-grade gap is exactly at the threshold and triggers.
        ("A", "A-", False),  # gap = 1 (A=1, A-=2)
        ("A", "B+", True),   # gap = 2 (A=1, B+=3)
    ],
)
def test_divergence_flag_two_level_gap(
    claude_grade: str, gemini_grade: str, expected_flag: bool
) -> None:
    out = compute_ensemble(
        grok_score=0.5,
        claude_grade=claude_grade,
        claude_probability=0.5,
        gemini_grade=gemini_grade,
        gemini_probability=0.5,
    )
    assert out["divergence_flag"] is expected_flag


def test_letter_grade_distance_canonical() -> None:
    # Sanity checks against the canonical ordering.
    assert letter_grade_distance("A+", "A+") == 0
    assert letter_grade_distance("A+", "A") == 1
    assert letter_grade_distance("A", "B-") == 4
    assert letter_grade_distance("A+", "F") == 10


def test_letter_grade_distance_unknown_raises() -> None:
    with pytest.raises(ValueError):
        letter_grade_distance("Z+", "A")


def test_divergence_threshold_is_two() -> None:
    """Sanity guard: a different threshold would silently change all
    callers, so we lock the exported constant in the test suite."""
    assert DIVERGENCE_THRESHOLD == 2


# ---------------------------------------------------------------------------
# EnsembleScorer — feature-flag toggle modes
# ---------------------------------------------------------------------------


def _candidate(**overrides: Any) -> dict[str, Any]:
    base = {
        "ticker": "TESTX",
        "as_of_date": "2025-04-26",
        "fast_context": {"phase": "P3"},
        "science_profile": {"moa": "anti-XYZ"},
        "full_context": {"nct_id": "NCT00000001"},
    }
    base.update(overrides)
    return base


def test_fast_only_mode(tmp_path: Path) -> None:
    """``ensemble_score == grok_score`` when the deep tier is disabled.

    Mirrors VAL-M2-047: ``LLM_PROVIDERS_DEEP=`` (empty) collapses the
    deep tier off entirely. We exercise the same code path by simply
    omitting the deep clients from the constructor.
    """
    xai = _FakeXAIClient(probability=0.66, rationale="grok-only")
    db_path = tmp_path / "ensemble_fast_only.db"
    scorer = EnsembleScorer(
        xai_client=xai,
        claude_client=None,
        gemini_client=None,
        db_path=db_path,
    )
    result = scorer.score(_candidate())

    assert result["ticker"] == "TESTX"
    assert result["fast_score"] == pytest.approx(0.66)
    assert result["deep_score"] is None
    assert result["ensemble_score"] == pytest.approx(0.66)
    assert result["claude_grade"] is None
    assert result["gemini_grade"] is None
    assert result["divergence_flag"] is False
    assert result["model_breakdown"]["claude"] is None
    assert result["model_breakdown"]["gemini"] is None
    assert result["model_breakdown"]["grok"]["score"] == pytest.approx(0.66)


def test_anthropic_only_deep_mode(tmp_path: Path) -> None:
    """Gemini disabled — Claude alone provides the deep tier.

    The pipeline must still produce a valid ensemble (graceful
    degradation per the feature description). Divergence is False
    because there is no second deep source to disagree.
    """
    xai = _FakeXAIClient(probability=0.50)
    claude = _FakeDeepClient(
        letter_grade="A",
        probability=0.70,
        rationale="claude only deep",
        model_id="claude-opus-fixture",
    )
    db_path = tmp_path / "ensemble_anthropic_only.db"
    scorer = EnsembleScorer(
        xai_client=xai,
        claude_client=claude,
        gemini_client=None,
        db_path=db_path,
    )
    result = scorer.score(_candidate())

    weights = config.ENSEMBLE_WEIGHTS
    base_total = weights["grok"] + weights["claude"]
    expected_w_grok = weights["grok"] / base_total
    expected_w_claude = weights["claude"] / base_total
    expected = expected_w_grok * 0.50 + expected_w_claude * 0.70

    assert result["ensemble_score"] == pytest.approx(expected)
    assert result["claude_grade"] == "A"
    assert result["gemini_grade"] is None
    assert result["divergence_flag"] is False
    assert result["model_breakdown"]["claude"]["letter_grade"] == "A"
    assert result["model_breakdown"]["gemini"] is None
    assert result["science_grade"] == "A"  # falls back to Claude


def test_all_on_mode_with_divergence(tmp_path: Path) -> None:
    """All three providers on; Claude vs Gemini disagree by ≥2 grades.

    The result MUST set ``divergence_flag=True`` and surface the
    rationales from each model in ``divergence_notes``.
    """
    xai = _FakeXAIClient(probability=0.40)
    claude = _FakeDeepClient(
        letter_grade="A",
        probability=0.80,
        rationale="claude is bullish on the readout",
        model_id="claude-opus-fixture",
    )
    gemini = _FakeDeepClient(
        letter_grade="B-",  # 4-grade gap from A
        probability=0.30,
        rationale="gemini is skeptical on the powering",
        model_id="gemini-2.5-pro-fixture",
    )
    db_path = tmp_path / "ensemble_all_on.db"
    scorer = EnsembleScorer(
        xai_client=xai,
        claude_client=claude,
        gemini_client=gemini,
        db_path=db_path,
    )
    result = scorer.score(_candidate())

    weights = config.ENSEMBLE_WEIGHTS
    expected = (
        weights["grok"] * 0.40
        + weights["claude"] * 0.80
        + weights["gemini"] * 0.30
    )
    assert result["ensemble_score"] == pytest.approx(expected)
    assert result["divergence_flag"] is True
    assert result["claude_grade"] == "A"
    assert result["gemini_grade"] == "B-"
    notes = result["divergence_notes"]
    assert notes is not None
    assert notes["claude"]["letter_grade"] == "A"
    assert "bullish" in notes["claude"]["rationale"]
    assert notes["gemini"]["letter_grade"] == "B-"
    assert "skeptical" in notes["gemini"]["rationale"]


def test_all_on_mode_without_divergence(tmp_path: Path) -> None:
    """All three providers on but the deep models agree → no divergence."""
    xai = _FakeXAIClient(probability=0.60)
    claude = _FakeDeepClient(
        letter_grade="B+",
        probability=0.55,
        rationale="claude says B+",
        model_id="claude-opus-fixture",
    )
    gemini = _FakeDeepClient(
        letter_grade="B",  # 1-grade gap from B+
        probability=0.50,
        rationale="gemini says B",
        model_id="gemini-2.5-pro-fixture",
    )
    db_path = tmp_path / "ensemble_no_divergence.db"
    scorer = EnsembleScorer(
        xai_client=xai,
        claude_client=claude,
        gemini_client=gemini,
        db_path=db_path,
    )
    result = scorer.score(_candidate())
    assert result["divergence_flag"] is False
    assert result["divergence_notes"] is None


# ---------------------------------------------------------------------------
# Persistence to scoring_cache
# ---------------------------------------------------------------------------


def test_persists_to_scoring_cache(tmp_path: Path) -> None:
    """A successful ``score(...)`` upserts one row into ``scoring_cache``.

    Mirrors VAL-M2-046: after a synthetic ensemble call, the row
    contains ``grok_score``, ``claude_grade``, ``gemini_grade``,
    ``ensemble_score``, ``divergence_flag`` (correct boolean type).
    """
    xai = _FakeXAIClient(probability=0.42)
    claude = _FakeDeepClient(
        letter_grade="A",
        probability=0.70,
        rationale="claude rationale",
        model_id="claude-opus-fixture",
    )
    gemini = _FakeDeepClient(
        letter_grade="B-",
        probability=0.30,
        rationale="gemini rationale",
        model_id="gemini-2.5-pro-fixture",
    )
    db_path = tmp_path / "scoring_cache_test.db"
    scorer = EnsembleScorer(
        xai_client=xai,
        claude_client=claude,
        gemini_client=gemini,
        db_path=db_path,
    )
    cand = _candidate(grok_rank=3)
    scorer.score(cand)

    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ticker, as_of_date, grok_rank, grok_score, "
            "claude_grade, claude_probability, gemini_grade, "
            "gemini_probability, science_grade, ensemble_score, "
            "divergence_flag, payload "
            "FROM scoring_cache "
            "WHERE ticker=? AND as_of_date=?",
            (cand["ticker"], cand["as_of_date"]),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == "TESTX"
    assert row["as_of_date"] == "2025-04-26"
    assert row["grok_rank"] == 3
    assert row["grok_score"] == pytest.approx(0.42)
    assert row["claude_grade"] == "A"
    assert row["claude_probability"] == pytest.approx(0.70)
    assert row["gemini_grade"] == "B-"
    assert row["gemini_probability"] == pytest.approx(0.30)
    assert row["science_grade"] == "A"
    assert row["ensemble_score"] is not None
    assert isinstance(row["divergence_flag"], int)
    assert row["divergence_flag"] == 1

    # Payload JSON is round-trippable and contains the model breakdown.
    payload = json.loads(row["payload"])
    assert "model_breakdown" in payload
    assert payload["model_breakdown"]["claude"]["letter_grade"] == "A"
    assert payload["model_breakdown"]["gemini"]["letter_grade"] == "B-"


def test_persists_idempotent_on_rerun(tmp_path: Path) -> None:
    """Running ``score`` twice on the same (ticker, date) does not duplicate."""
    xai = _FakeXAIClient(probability=0.50)
    db_path = tmp_path / "idempotent.db"
    scorer = EnsembleScorer(xai_client=xai, db_path=db_path)
    scorer.score(_candidate())
    scorer.score(_candidate())

    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM scoring_cache "
            "WHERE ticker='TESTX' AND as_of_date='2025-04-26'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_persist_disabled(tmp_path: Path) -> None:
    """``persist=False`` skips the SQLite write."""
    xai = _FakeXAIClient(probability=0.5)
    db_path = tmp_path / "no_persist.db"
    scorer = EnsembleScorer(
        xai_client=xai, db_path=db_path, persist=False,
    )
    result = scorer.score(_candidate())
    assert result["ensemble_score"] == pytest.approx(0.5)
    # The DB file should not have been created (no ``run_migrations``
    # was called) — the parent directory may exist but no file.
    assert not db_path.exists()


# ---------------------------------------------------------------------------
# Validation surface
# ---------------------------------------------------------------------------


def test_score_requires_ticker_and_date() -> None:
    scorer = EnsembleScorer(xai_client=_FakeXAIClient(probability=0.5))
    with pytest.raises(EnsembleError):
        scorer.score({"as_of_date": "2025-04-26"})
    with pytest.raises(EnsembleError):
        scorer.score({"ticker": "TESTX"})


def test_fast_tier_failure_degrades_to_deep(tmp_path: Path) -> None:
    """If xAI raises, the ensemble falls back to the deep tier."""

    class _BoomXAI:
        def score_ticker(self, *_args, **_kwargs):
            raise RuntimeError("simulated xai outage")

    claude = _FakeDeepClient(
        letter_grade="B",
        probability=0.55,
        rationale="claude",
        model_id="claude-opus-fixture",
    )
    db_path = tmp_path / "fallback.db"
    scorer = EnsembleScorer(
        xai_client=_BoomXAI(),
        claude_client=claude,
        db_path=db_path,
    )
    result = scorer.score(_candidate())
    assert result["fast_score"] is None
    # With only Claude active, the renormalised weight collapses to 1.0
    # so ``ensemble_score == claude_probability``.
    assert result["ensemble_score"] == pytest.approx(0.55)
    assert result["divergence_flag"] is False


def test_from_config_skips_unconfigured_providers(monkeypatch, tmp_path: Path) -> None:
    """:meth:`EnsembleScorer.from_config` skips providers without keys.

    With no API keys set in the environment, every provider should
    be skipped and the resulting scorer should have all clients set
    to ``None`` (the pipeline degrades to a no-op rather than
    crashing on startup).
    """
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    # Force config helpers to re-read the env (they read on each
    # call, so this is sufficient).
    scorer = EnsembleScorer.from_config(
        db_path=tmp_path / "from_config.db", persist=False,
    )
    assert scorer._xai is None
    assert scorer._claude is None
    assert scorer._gemini is None

    # Scoring with no providers attached produces ``None`` for every
    # numeric field but does NOT raise (graceful degradation).
    result = scorer.score(_candidate())
    assert result["fast_score"] is None
    assert result["deep_score"] is None
    assert result["ensemble_score"] is None
    assert result["divergence_flag"] is False
