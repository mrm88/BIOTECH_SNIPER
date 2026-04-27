"""Tests for the f-m2-07 wiring of the EnsembleScorer into
:mod:`biotech_sniper.sectors.unified_scorer`.

The feature replaces the legacy cron-agent file-based stub (which used
to read from ``scores/*_claude.txt`` and ``scores/*_gemini.txt``) with
direct in-process :class:`biotech_sniper.llm.ensemble.EnsembleScorer`
calls. These tests pin three behaviours:

* The legacy ``scores/*.txt`` pattern is no longer read by the
  scorer or play-card formatter (``test_no_legacy_scores_txt_reads``).
* The ensemble symbols are re-exported at the
  ``biotech_sniper.sectors.unified_scorer`` namespace so downstream
  callers (M3 selection, M5 ranker) can import the canonical helpers
  without breaking (``test_unified_scorer_reexports_ensemble``).
* :func:`score_candidate_with_models` runs the
  :class:`EnsembleScorer`, persists one row to the SQLite
  ``scoring_cache`` table, and is idempotent on
  ``(ticker, as_of_date)`` (``test_score_candidate_persists_to_sqlite``,
  ``test_score_candidate_idempotent_on_rerun``).
* A full ``score_candidate_with_models`` call succeeds even when the
  legacy ``scores/`` directory is absent
  (``test_full_run_with_empty_scores_dir_succeeds``).
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

from biotech_sniper.llm.ensemble import EnsembleScorer
from biotech_sniper.sectors import unified_scorer


# ---------------------------------------------------------------------------
# Lightweight fakes — same shape as the per-tier clients used by the
# real EnsembleScorer (see tests/scoring/test_ensemble.py).
# ---------------------------------------------------------------------------


class _FakeXAIClient:
    def __init__(self, *, probability: float):
        self.probability = float(probability)
        self.calls: list[tuple[str, dict]] = []

    def score_ticker(self, ticker: str, context: Mapping[str, Any]) -> dict:
        self.calls.append((ticker, dict(context)))
        return {
            "probability": self.probability,
            "rationale": "fast tier rationale",
            "confidence": 0.7,
            "model_id": "grok-4-fixture",
            "latency_ms": 5,
            "cost_usd": 0.0,
        }


class _FakeDeepClient:
    def __init__(
        self, *, letter_grade: str, probability: float, model_id: str
    ):
        self.letter_grade = letter_grade
        self.probability = float(probability)
        self.model_id = model_id
        self.calls: list[tuple[dict, dict]] = []

    def deep_science_review(
        self, science_profile: Mapping[str, Any], full_context: Mapping[str, Any]
    ) -> dict:
        self.calls.append((dict(science_profile), dict(full_context)))
        return {
            "letter_grade": self.letter_grade,
            "probability": self.probability,
            "rationale": f"{self.model_id} rationale",
            "citations": [{"source": "fixture", "quote_or_url": "fixture-cite"}],
            "model_id": self.model_id,
            "latency_ms": 7,
            "cost_usd": 0.0,
        }


# ---------------------------------------------------------------------------
# 1. Legacy file-based stub is gone.
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parent.parent

_LEGACY_PATTERN = re.compile(r"scores/[^\s\"']*_(claude|gemini)\.txt")


def test_no_legacy_scores_txt_reads_in_unified_scorer() -> None:
    """``unified_scorer.py`` must not read ``scores/*_claude.txt`` etc.

    Mirrors the verification step in the feature spec:
      grep -nE "scores/.*_(claude|gemini)\\.txt" \\
        biotech_sniper/sectors/unified_scorer.py
    """
    src = (
        REPO_ROOT / "biotech_sniper" / "sectors" / "unified_scorer.py"
    ).read_text(encoding="utf-8")
    assert not _LEGACY_PATTERN.search(src), (
        "unified_scorer.py contains a legacy scores/*.txt reference; "
        "the cron-agent file stub must be replaced by EnsembleScorer."
    )


def test_no_legacy_scores_txt_reads_in_play_card_formatter() -> None:
    """``play_card_formatter.py`` must not read ``scores/*_claude.txt`` etc."""
    src = (
        REPO_ROOT / "biotech_sniper" / "play_card_formatter.py"
    ).read_text(encoding="utf-8")
    assert not _LEGACY_PATTERN.search(src), (
        "play_card_formatter.py contains a legacy scores/*.txt reference."
    )


def test_play_card_formatter_uses_canonical_unified_scorer_import() -> None:
    """The formatter must import via ``biotech_sniper.sectors.unified_scorer``.

    The historic ``from sectors.unified_scorer import ...`` pattern
    relies on a ``sys.path.insert`` hack and breaks under standard
    package imports. f-m2-07 fixes the import.
    """
    src = (
        REPO_ROOT / "biotech_sniper" / "play_card_formatter.py"
    ).read_text(encoding="utf-8")
    assert "from biotech_sniper.sectors.unified_scorer import" in src
    assert (
        re.search(r"^\s*from sectors\.unified_scorer import", src, re.MULTILINE)
        is None
    ), "play_card_formatter.py still uses the legacy non-package import path."


# ---------------------------------------------------------------------------
# 2. Ensemble symbols re-exported.
# ---------------------------------------------------------------------------


def test_unified_scorer_reexports_ensemble() -> None:
    """Downstream callers must be able to ``from biotech_sniper.sectors.
    unified_scorer import compute_ensemble`` (and the rest of the
    ensemble surface) without importing the M2 ``llm.ensemble`` module
    explicitly. This protects VAL-M2-044 / VAL-M2-079 + M3 / M5
    consumers per the feature description.
    """
    from biotech_sniper.sectors.unified_scorer import (
        DIVERGENCE_THRESHOLD,
        EnsembleScorer as ReexportedEnsembleScorer,
        compute_ensemble,
        letter_grade_distance,
    )

    # The re-exports MUST be the same objects as the canonical
    # implementation in :mod:`biotech_sniper.llm.ensemble`.
    from biotech_sniper.llm import ensemble as canonical

    assert ReexportedEnsembleScorer is canonical.EnsembleScorer
    assert compute_ensemble is canonical.compute_ensemble
    assert letter_grade_distance is canonical.letter_grade_distance
    assert DIVERGENCE_THRESHOLD == canonical.DIVERGENCE_THRESHOLD


def test_unified_scorer_does_not_duplicate_ensemble_logic() -> None:
    """Re-exports only — no second copy of the formula in unified_scorer."""
    src = (
        REPO_ROOT / "biotech_sniper" / "sectors" / "unified_scorer.py"
    ).read_text(encoding="utf-8")
    # The canonical formula (``ensemble_score = w_grok * grok_score + ...``)
    # lives ONLY in ``biotech_sniper/llm/ensemble.py``. Locally
    # redefining ``compute_ensemble`` would shadow the re-export.
    assert "def compute_ensemble(" not in src
    assert "def letter_grade_distance(" not in src


# ---------------------------------------------------------------------------
# 3. score_candidate_with_models runs the ensemble + persists.
# ---------------------------------------------------------------------------


def _make_test_scorer(db_path: Path) -> tuple[EnsembleScorer, Any, Any, Any]:
    """Build an EnsembleScorer wired to the temp ``db_path``."""
    xai = _FakeXAIClient(probability=0.55)
    claude = _FakeDeepClient(
        letter_grade="A",
        probability=0.70,
        model_id="claude-opus-fixture",
    )
    gemini = _FakeDeepClient(
        letter_grade="B-",
        probability=0.30,
        model_id="gemini-2.5-pro-fixture",
    )
    scorer = EnsembleScorer(
        xai_client=xai,
        claude_client=claude,
        gemini_client=gemini,
        db_path=db_path,
    )
    return scorer, xai, claude, gemini


def _redirect_data_dir(monkeypatch, db_dir: Path) -> Path:
    """Point :mod:`biotech_sniper.paths.DATA_DIR` at ``db_dir``.

    The unified_scorer cache lookup reads from
    ``DATA_DIR / "alpha_sniper.db"``. Tests redirect both the paths
    module and the local re-imports inside the scorer so the cache hit
    falls on the temporary db file instead of the real project db.
    """
    db_dir.mkdir(parents=True, exist_ok=True)
    from biotech_sniper import paths as _paths

    monkeypatch.setattr(_paths, "DATA_DIR", db_dir, raising=True)
    return db_dir / "alpha_sniper.db"


def test_score_candidate_persists_to_sqlite(monkeypatch, tmp_path: Path) -> None:
    """A score call writes one row into ``scoring_cache``.

    Mirrors VAL-M2-046 / VAL-M2-057 at the unified_scorer surface.
    """
    db_path = _redirect_data_dir(monkeypatch, tmp_path / "data")
    scorer, *_ = _make_test_scorer(db_path)

    claude_p, gemini_p, ensemble_p, divergence = (
        unified_scorer.score_candidate_with_models(
            prompt="probe",
            ticker="TESTA",
            as_of_date="2025-04-26",
            scorer=scorer,
        )
    )

    assert claude_p == pytest.approx(0.70)
    assert gemini_p == pytest.approx(0.30)
    assert ensemble_p is not None
    assert divergence is True

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT ticker, as_of_date, ensemble_score, divergence_flag "
            "FROM scoring_cache WHERE ticker=? AND as_of_date=?",
            ("TESTA", "2025-04-26"),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0][0] == "TESTA"
    assert rows[0][1] == "2025-04-26"
    assert rows[0][2] is not None
    assert rows[0][3] == 1


def test_score_candidate_idempotent_on_rerun(monkeypatch, tmp_path: Path) -> None:
    """Two calls on the same (ticker, as_of_date) → exactly one row.

    The second call hits the SQLite cache short-circuit and does NOT
    invoke the underlying clients again. Mirrors VAL-M2-058.
    """
    db_path = _redirect_data_dir(monkeypatch, tmp_path / "data")
    scorer, xai, claude, gemini = _make_test_scorer(db_path)

    unified_scorer.score_candidate_with_models(
        prompt="probe",
        ticker="DUPL",
        as_of_date="2025-04-26",
        scorer=scorer,
    )
    # Second invocation — cache hit, clients should NOT be called again.
    second = unified_scorer.score_candidate_with_models(
        prompt="probe",
        ticker="DUPL",
        as_of_date="2025-04-26",
        scorer=scorer,
    )

    # Cache hit returns the persisted values.
    assert second[0] == pytest.approx(0.70)
    assert second[1] == pytest.approx(0.30)
    assert second[2] is not None
    assert second[3] is True

    # Underlying clients invoked exactly once total.
    assert len(xai.calls) == 1
    assert len(claude.calls) == 1
    assert len(gemini.calls) == 1

    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM scoring_cache WHERE ticker=? AND as_of_date=?",
            ("DUPL", "2025-04-26"),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_full_run_with_empty_scores_dir_succeeds(
    monkeypatch, tmp_path: Path
) -> None:
    """Scoring runs end-to-end even when the legacy ``scores/`` dir is absent.

    The legacy cron-agent stub used to fall back to reading
    ``scores/*_claude.txt`` etc.; after f-m2-07 the scoring path
    must rely entirely on the EnsembleScorer + SQLite cache.
    """
    db_path = _redirect_data_dir(monkeypatch, tmp_path / "data")
    scorer, *_ = _make_test_scorer(db_path)

    # Sanity: the temp working directory has no legacy ``scores/`` tree.
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "scores").exists()

    claude_p, gemini_p, ensemble_p, divergence = (
        unified_scorer.score_candidate_with_models(
            prompt="empty-scores-dir probe",
            ticker="EMPTY",
            as_of_date="2025-04-26",
            scorer=scorer,
        )
    )
    assert ensemble_p is not None
    assert claude_p is not None
    assert gemini_p is not None
    assert isinstance(divergence, bool)


def test_score_candidate_no_providers_does_not_crash(
    monkeypatch, tmp_path: Path
) -> None:
    """When every provider is disabled (no keys), the call still
    returns ``(None, None, None, False)`` rather than raising.

    Mirrors VAL-M2-049 / VAL-M2-050: missing keys degrade the
    pipeline gracefully — workers running on a fresh laptop without
    LLM keys must not see ImportError or KeyError.
    """
    db_path = _redirect_data_dir(monkeypatch, tmp_path / "data")
    scorer = EnsembleScorer(
        xai_client=None,
        claude_client=None,
        gemini_client=None,
        db_path=db_path,
    )

    claude_p, gemini_p, ensemble_p, divergence = (
        unified_scorer.score_candidate_with_models(
            prompt="no-providers",
            ticker="EMPT2",
            as_of_date="2025-04-26",
            scorer=scorer,
        )
    )
    assert claude_p is None
    assert gemini_p is None
    assert ensemble_p is None
    assert divergence is False


# ---------------------------------------------------------------------------
# 4. audit.py legacy probes warn rather than ImportError.
# ---------------------------------------------------------------------------


def test_audit_module_has_no_top_level_legacy_imports() -> None:
    """audit.py must not contain ``from intelligence.X import Y`` /
    ``from sectors.X import Y`` top-level statements (the legacy
    sandbox-style imports). The f-m2-07 rewrite uses the proper
    package paths inside try/except blocks instead.
    """
    src = (REPO_ROOT / "biotech_sniper" / "audit.py").read_text(encoding="utf-8")
    # The legacy patterns we must NOT find at the start of any line.
    legacy = re.compile(
        r"^\s*from\s+(intelligence|sectors)\.", re.MULTILINE
    )
    matches = legacy.findall(src)
    assert not matches, (
        "audit.py still imports from non-package legacy paths "
        "(intelligence.* / sectors.*); rewrite to use "
        "biotech_sniper.intelligence.* / biotech_sniper.sectors.*."
    )


def test_audit_module_has_no_legacy_syspath_inserts() -> None:
    """No ``sys.path.insert(... 'intelligence' ...)`` style mutations
    at module top level — these are the source of the recurring
    ``No module named intelligence`` ImportErrors logged in earlier
    feature handoffs.
    """
    src = (REPO_ROOT / "biotech_sniper" / "audit.py").read_text(encoding="utf-8")
    pat = re.compile(
        r"sys\.path\.insert\([^\)]*['\"](intelligence|sectors)['\"]",
        re.MULTILINE,
    )
    assert pat.search(src) is None
