"""Tests for the f-m2-20 unified scorer CLI.

Exercises ``python -m biotech_sniper.sectors.unified_scorer`` end-to-end
through :func:`biotech_sniper.sectors.unified_scorer.main` with mocked
LLM clients (cassette-style fakes) so no live network calls are made.

The five canonical test cases from the f-m2-20 feature spec are:

* ``test_cli_emits_json_summary`` — last stdout line parses as JSON
  with the documented keys.
* ``test_cli_writes_scoring_cache_rows`` — a tmp-db run upserts one
  row per ticker into ``scoring_cache``.
* ``test_cli_emits_play_cards`` — ``play_cards/<date>/<TICKER>.json``
  files appear when ``--no-emit-play-cards`` is NOT passed.
* ``test_cli_warns_on_missing_gemini_key`` — deleting
  ``GEMINI_API_KEY`` produces a warning AND drops gemini from the
  ``providers_used`` summary while still exiting 0.
* ``test_cli_handles_unknown_ticker_gracefully`` — a junk ticker
  whose scoring raises is logged at WARNING and the run still exits
  0 with the remaining tickers scored.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Mapping

import pytest

from biotech_sniper.llm.ensemble import EnsembleScorer
from biotech_sniper.sectors import unified_scorer


# ---------------------------------------------------------------------------
# Lightweight fakes for the per-tier clients.
# ---------------------------------------------------------------------------


class _FakeXAIClient:
    """Cassette-style xAI fake. Returns a deterministic probability."""

    def __init__(self, probability: float = 0.65) -> None:
        self.probability = float(probability)
        self.calls: list[tuple[str, dict]] = []

    def score_ticker(self, ticker: str, context: Mapping[str, Any]) -> dict:
        self.calls.append((ticker, dict(context)))
        return {
            "probability": self.probability,
            "rationale": f"fast tier rationale for {ticker}",
            "confidence": 0.7,
            "model_id": "grok-4-fixture",
            "latency_ms": 4,
            "cost_usd": 0.0,
        }


class _FakeDeepClient:
    """Cassette-style deep-tier fake (Claude / Gemini)."""

    def __init__(
        self,
        *,
        letter_grade: str = "B+",
        probability: float = 0.62,
        model_id: str = "claude-opus-fixture",
    ) -> None:
        self.letter_grade = letter_grade
        self.probability = float(probability)
        self.model_id = model_id
        self.calls: list[tuple[dict, dict]] = []

    def deep_science_review(
        self,
        science_profile: Mapping[str, Any],
        full_context: Mapping[str, Any],
    ) -> dict:
        self.calls.append((dict(science_profile), dict(full_context)))
        return {
            "letter_grade": self.letter_grade,
            "probability": self.probability,
            "rationale": f"{self.model_id} rationale",
            "citations": [{"source": "fixture", "url": "https://fix"}],
            "model_id": self.model_id,
            "latency_ms": 6,
            "cost_usd": 0.0,
        }


def _make_fake_scorer(db_path: Path, *, raise_on: tuple[str, ...] = ()) -> EnsembleScorer:
    """Build an EnsembleScorer wired to ``db_path`` with cassette fakes.

    ``raise_on`` lists tickers that should raise on score so we can
    exercise the per-ticker failure-tolerance path.
    """
    xai = _FakeXAIClient(probability=0.70)
    claude = _FakeDeepClient(
        letter_grade="A",
        probability=0.72,
        model_id="claude-opus-fixture",
    )
    gemini = _FakeDeepClient(
        letter_grade="A-",
        probability=0.66,
        model_id="gemini-2.5-pro-fixture",
    )

    real_scorer = EnsembleScorer(
        xai_client=xai,
        claude_client=claude,
        gemini_client=gemini,
        db_path=db_path,
    )

    if not raise_on:
        return real_scorer

    raising = set(raise_on)
    inner_score = real_scorer.score

    class _RaisingScorer(EnsembleScorer):
        def __init__(self) -> None:  # type: ignore[no-untyped-def]
            # Reuse the underlying scorer's wiring; we just intercept score().
            self.__dict__.update(real_scorer.__dict__)

        def score(self, candidate):  # type: ignore[override]
            ticker = str((candidate or {}).get("ticker") or "")
            if ticker in raising:
                raise RuntimeError(f"unknown ticker: {ticker}")
            return inner_score(candidate)

    return _RaisingScorer()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _redirect_data_dir(monkeypatch, db_dir: Path) -> Path:
    """Point :data:`paths.DATA_DIR` at a tmp directory.

    Mirrors the helper used in :mod:`tests.test_unified_scorer` so
    function-local imports of ``DATA_DIR`` (in select_top_n,
    _load_tradeable_tickers_from_universe, etc.) all see the
    redirected path.
    """
    db_dir.mkdir(parents=True, exist_ok=True)
    from biotech_sniper import paths as _paths

    monkeypatch.setattr(_paths, "DATA_DIR", db_dir, raising=True)
    return db_dir / "alpha_sniper.db"


def _redirect_play_cards_root(monkeypatch, base_dir: Path) -> Path:
    """Point :data:`play_card_formatter.BASE` at a tmp base directory."""
    base_dir.mkdir(parents=True, exist_ok=True)
    from biotech_sniper import play_card_formatter as _pcf

    monkeypatch.setattr(_pcf, "BASE", base_dir, raising=True)
    return base_dir / "play_cards"


def _patch_scorer_factory(monkeypatch, db_path: Path, *, raise_on=()) -> None:
    """Replace ``unified_scorer._build_scorer`` with a fake-backed factory."""

    def fake_build_scorer(*, dry_run: bool = False):  # noqa: ARG001 - signature parity
        return _make_fake_scorer(db_path, raise_on=raise_on)

    monkeypatch.setattr(
        unified_scorer, "_build_scorer", fake_build_scorer, raising=True
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cli_emits_json_summary(monkeypatch, tmp_path: Path, capsys) -> None:
    """``main(...)`` last stdout line parses as JSON with the documented keys."""
    db_path = _redirect_data_dir(monkeypatch, tmp_path / "data")
    _redirect_play_cards_root(monkeypatch, tmp_path)
    _patch_scorer_factory(monkeypatch, db_path)

    rc = unified_scorer.main(
        [
            "--tickers",
            "SRPT,VRTX",
            "--date",
            "2026-04-27",
            "--no-emit-play-cards",
            # f-m3-16: empty universe lookup is now a strict reject;
            # this test exercises the CLI plumbing, not the gate, so
            # bypass it explicitly with --no-chain-gate.
            "--no-chain-gate",
        ]
    )
    assert rc == 0

    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert lines, "expected at least one stdout line"
    summary = json.loads(lines[-1])

    required = {
        "as_of_date",
        "tickers_scored",
        "providers_used",
        "rows_upserted",
        "play_cards_written",
    }
    assert required.issubset(summary.keys()), summary
    assert summary["as_of_date"] == "2026-04-27"
    assert summary["tickers_scored"] == ["SRPT", "VRTX"]
    assert "xai" in summary["providers_used"]
    # providers_used must be sorted alphabetically per the spec.
    assert summary["providers_used"] == sorted(summary["providers_used"])
    assert summary["rows_upserted"] == 2
    # --no-emit-play-cards was passed → none should be written.
    assert summary["play_cards_written"] == 0

    # Per-ticker score lines precede the JSON summary.
    score_lines = [ln for ln in lines if ln.startswith("[score]")]
    assert len(score_lines) == 2
    assert any("SRPT" in ln for ln in score_lines)
    assert any("VRTX" in ln for ln in score_lines)


def test_cli_writes_scoring_cache_rows(monkeypatch, tmp_path: Path) -> None:
    """Each ticker scored UPSERTs one row into ``scoring_cache``."""
    db_path = _redirect_data_dir(monkeypatch, tmp_path / "data")
    _redirect_play_cards_root(monkeypatch, tmp_path)
    _patch_scorer_factory(monkeypatch, db_path)

    rc = unified_scorer.main(
        [
            "--tickers",
            "SRPT,VRTX,BMRN",
            "--date",
            "2026-04-27",
            "--no-emit-play-cards",
            "--no-chain-gate",
        ]
    )
    assert rc == 0

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT ticker FROM scoring_cache "
            "WHERE as_of_date = ? ORDER BY ticker ASC",
            ("2026-04-27",),
        ).fetchall()
    finally:
        conn.close()

    assert [r[0] for r in rows] == ["BMRN", "SRPT", "VRTX"]


def test_cli_emits_play_cards(monkeypatch, tmp_path: Path, capsys) -> None:
    """Without ``--no-emit-play-cards`` a JSON file is written per ticker."""
    db_path = _redirect_data_dir(monkeypatch, tmp_path / "data")
    play_cards_root = _redirect_play_cards_root(monkeypatch, tmp_path)
    _patch_scorer_factory(monkeypatch, db_path)

    rc = unified_scorer.main(
        [
            "--tickers",
            "SRPT,VRTX",
            "--date",
            "2026-04-27",
            "--no-chain-gate",
        ]
    )
    assert rc == 0

    out_dir = play_cards_root / "2026-04-27"
    assert out_dir.is_dir()
    written = sorted(p.name for p in out_dir.glob("*.json"))
    # Both tickers had ensemble_score >= 0.55 and grade >= C+, so both
    # qualify for play-card emission via select_top_n.
    assert "SRPT.json" in written
    assert "VRTX.json" in written

    # Summary must reflect the count.
    captured = capsys.readouterr()
    summary = json.loads(
        [ln for ln in captured.out.splitlines() if ln.strip()][-1]
    )
    assert summary["play_cards_written"] == len(written)


def test_cli_warns_on_missing_gemini_key(
    monkeypatch, tmp_path: Path, capsys, caplog
) -> None:
    """Missing GEMINI_API_KEY → WARNING logged + gemini dropped from providers_used.

    The pipeline still exits 0 because the ensemble can compute with
    the remaining providers (xai-only or xai+anthropic).
    """
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    # ANTHROPIC_API_KEY may or may not be set in the test env. Either
    # way, the assertion below only checks that gemini is NOT in
    # providers_used; it does not require anthropic to be present.
    #
    # f-misc-03 isolation: ``_resolve_deep_providers`` falls back to
    # ``_config.LLM_PROVIDERS["deep"]`` (a module-level constant
    # captured at import time) when ``LLM_PROVIDERS_DEEP`` is unset.
    # Sibling tests in this suite (notably ``tests/test_config.py``
    # and ``tests/test_gemini_client.py``) re-import / reload
    # ``biotech_sniper.config`` while ``LLM_PROVIDERS_DEEP=anthropic``
    # is briefly in scope. ``monkeypatch.setenv`` restores the env
    # var on teardown but does NOT undo the module reload, so the
    # leaked ``LLM_PROVIDERS["deep"] = ["anthropic"]`` persists in
    # ``sys.modules`` and elides ``gemini`` from this test's deep-
    # provider list. Pin the env var explicitly to the canonical
    # default so this test is robust regardless of any prior reload
    # state in the same xdist worker.
    monkeypatch.setenv("LLM_PROVIDERS_DEEP", "anthropic,gemini")

    db_path = _redirect_data_dir(monkeypatch, tmp_path / "data")
    _redirect_play_cards_root(monkeypatch, tmp_path)
    _patch_scorer_factory(monkeypatch, db_path)

    with caplog.at_level(
        logging.WARNING, logger="biotech_sniper.sectors.unified_scorer"
    ):
        rc = unified_scorer.main(
            [
                "--tickers",
                "SRPT",
                "--date",
                "2026-04-27",
                "--no-emit-play-cards",
                "--no-chain-gate",
            ]
        )
    assert rc == 0

    # WARNING line names "gemini" and the env var.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "gemini" in (rec.getMessage() or "")
        and "GEMINI_API_KEY" in (rec.getMessage() or "")
        for rec in warnings
    ), [rec.getMessage() for rec in warnings]

    summary = json.loads(
        [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()][-1]
    )
    assert "gemini" not in summary["providers_used"]
    assert "xai" in summary["providers_used"]


def test_cli_handles_unknown_ticker_gracefully(
    monkeypatch, tmp_path: Path, capsys, caplog
) -> None:
    """A scoring failure on one ticker logs WARNING and the run still exits 0."""
    db_path = _redirect_data_dir(monkeypatch, tmp_path / "data")
    _redirect_play_cards_root(monkeypatch, tmp_path)
    _patch_scorer_factory(monkeypatch, db_path, raise_on=("JUNK",))

    with caplog.at_level(
        logging.WARNING, logger="biotech_sniper.sectors.unified_scorer"
    ):
        rc = unified_scorer.main(
            [
                "--tickers",
                "JUNK,SRPT",
                "--date",
                "2026-04-27",
                "--no-emit-play-cards",
                "--no-chain-gate",
            ]
        )
    assert rc == 0

    # JUNK warning was logged, but SRPT still scored.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "JUNK" in (rec.getMessage() or "") for rec in warnings
    ), [rec.getMessage() for rec in warnings]

    summary = json.loads(
        [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()][-1]
    )
    assert "SRPT" in summary["tickers_scored"]
    assert "JUNK" not in summary["tickers_scored"]
    assert summary["rows_upserted"] == 1


# ---------------------------------------------------------------------------
# Bonus tests — pin small invariants so future refactors don't regress.
# ---------------------------------------------------------------------------


def test_cli_dry_run_skips_deep_tier(monkeypatch, tmp_path: Path, capsys) -> None:
    """``--dry-run`` removes deep providers from ``providers_used``."""
    db_path = _redirect_data_dir(monkeypatch, tmp_path / "data")
    _redirect_play_cards_root(monkeypatch, tmp_path)
    _patch_scorer_factory(monkeypatch, db_path)

    rc = unified_scorer.main(
        [
            "--tickers",
            "SRPT",
            "--date",
            "2026-04-27",
            "--dry-run",
            "--no-emit-play-cards",
        ]
    )
    assert rc == 0
    summary = json.loads(
        [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()][-1]
    )
    # --dry-run forces deep-tier off; only xAI remains regardless of env keys.
    assert summary["providers_used"] == ["xai"]
    assert summary["play_cards_written"] == 0


def test_cli_dry_run_warns_on_missing_gemini_key(
    monkeypatch, tmp_path: Path, capsys, caplog
) -> None:
    """Even with ``--dry-run``, missing GEMINI_API_KEY emits a WARNING.

    Regression test for f-m2-23 / VAL-M2-050: the per-provider key-
    presence check must run regardless of ``--dry-run`` so that
    operators see warnings about missing deep-tier credentials. The
    dry-run flag still suppresses the deep tier from
    ``providers_used`` (only ``xai`` remains).
    """
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    # f-misc-03 isolation: pin LLM_PROVIDERS_DEEP=anthropic,gemini so
    # this test is robust against config-module reloads from sibling
    # tests (``test_config.py`` / ``test_gemini_client.py``) that
    # leak ``LLM_PROVIDERS["deep"] = ["anthropic"]`` via
    # ``importlib.reload(_config)`` while ``LLM_PROVIDERS_DEEP=anthropic``
    # is briefly in scope. See companion comment in
    # ``test_cli_warns_on_missing_gemini_key`` above.
    monkeypatch.setenv("LLM_PROVIDERS_DEEP", "anthropic,gemini")

    db_path = _redirect_data_dir(monkeypatch, tmp_path / "data")
    _redirect_play_cards_root(monkeypatch, tmp_path)
    _patch_scorer_factory(monkeypatch, db_path)

    with caplog.at_level(
        logging.WARNING, logger="biotech_sniper.sectors.unified_scorer"
    ):
        rc = unified_scorer.main(
            [
                "--tickers",
                "SRPT",
                "--date",
                "2030-01-02",
                "--dry-run",
                "--no-emit-play-cards",
            ]
        )
    assert rc == 0

    # WARNING line names "gemini" and the env var, even in --dry-run.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "gemini" in (rec.getMessage() or "")
        and "GEMINI_API_KEY" in (rec.getMessage() or "")
        for rec in warnings
    ), [rec.getMessage() for rec in warnings]

    summary = json.loads(
        [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()][-1]
    )
    # --dry-run still suppresses deep tier from providers_used.
    assert summary["providers_used"] == ["xai"]
    assert "gemini" not in summary["providers_used"]


def test_cli_seed_fallback_when_universe_empty(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """With no universe rows and no ``--tickers``, the 5-ticker seed fires."""
    db_path = _redirect_data_dir(monkeypatch, tmp_path / "data")
    _redirect_play_cards_root(monkeypatch, tmp_path)
    _patch_scorer_factory(monkeypatch, db_path)

    rc = unified_scorer.main(
        [
            "--date",
            "2026-04-27",
            "--top-n",
            "5",
            "--no-emit-play-cards",
            # f-m3-16: empty universe lookup is now a strict reject;
            # the seed-fallback feeds 5 tickers but the chain gate
            # would reject them all on an empty universe. This test
            # exercises the seed-fallback resolver, not the gate, so
            # bypass the gate explicitly.
            "--no-chain-gate",
        ]
    )
    assert rc == 0
    summary = json.loads(
        [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()][-1]
    )
    assert summary["tickers_scored"] == ["SRPT", "VRTX", "BMRN", "ARWR", "IONS"]
