"""Tests for ``biotech_sniper.ranker`` and the unified-scorer
integration that decorates play cards with a supplementary signal
field when ``LIGHTGBM_RANKER_ENABLED`` is on (f-m5-04).

Coverage map → validation contract:

* VAL-M5-025: ``load_ranker`` returns an object exposing
  ``predict_proba``.
* VAL-M5-026: ``predict_proba`` on a parquet-schema row returns a
  ``(1, 2)`` matrix that sums to 1.
* VAL-M5-027: manifest mismatch raises ``RankerSchemaMismatch``.
* VAL-M5-028: ``LIGHTGBM_RANKER_ENABLED`` defaults to ``False``.
* VAL-M5-029: with the flag on, every emitted play card carries
  the supplementary signal field.
* VAL-M5-030: with the flag off, ``lightgbm`` is not present in
  ``sys.modules`` and the play-card payload omits the field.
* VAL-M5-031: ``ensemble_score`` (a.k.a. ``pre_score``) is
  invariant under flag toggle and the supplementary field is not
  referenced by trade-gating modules.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from biotech_sniper import config as bs_config
from biotech_sniper import ranker as bs_ranker
from biotech_sniper.ranker import RankerSchemaMismatch, load_ranker
from biotech_sniper.training import train_ranker as tr
from biotech_sniper.training.build_feature_store import (
    PRIOR_PHASE2_DATA_QUALITY_MAP,
    REQUIRED_FEATURE_COLS,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


_SECTORS = ["BIOTECH", "PHARMA", "MEDDEV"]
_SCIENCE_GRADES = ["A", "B", "C", "D", "F"]
_INDICATIONS = ["oncology", "rare", "cardio", "neuro", "immunology"]


def _synthetic_parquet(
    path: Path,
    *,
    n_pos: int = 18,
    n_neg: int = 18,
    seed: int = 7,
) -> pd.DataFrame:
    """Materialise a small parquet that mirrors the catalysts schema.

    Mirrors the helper in ``tests/test_train_ranker.py`` so this file
    is self-contained (we avoid coupling tests across worker
    handoffs).
    """

    rng = np.random.default_rng(seed)
    rows: list[dict] = []
    for label in (1,) * n_pos + (0,) * n_neg:
        sector = _SECTORS[int(rng.integers(len(_SECTORS)))]
        letter = _SCIENCE_GRADES[
            int(rng.integers(0, 2)) if label else int(rng.integers(2, 5))
        ]
        prior = PRIOR_PHASE2_DATA_QUALITY_MAP[letter]
        indication = _INDICATIONS[int(rng.integers(len(_INDICATIONS)))]
        market_cap = float(rng.uniform(5e8, 5e10))
        rows.append(
            {
                "ticker": f"T{len(rows):03d}",
                "ticker_raw": f"T{len(rows):03d}",
                "entry_date": "2025-01-15",
                "catalyst_date": "2025-02-10",
                "resolved": True,
                "science_grade": letter,
                "base_rate": float(rng.uniform(0.1, 0.9)),
                "p_ensemble": float(rng.uniform(0.1, 0.9)),
                "iv_at_entry": float(rng.uniform(0.5, 2.0)),
                "dte_at_entry": float(rng.integers(20, 90)),
                "market_cap": market_cap,
                "sector": sector,
                "prior_phase2_data_quality": str(prior),
                "sponsor_size": float(np.log10(market_cap + 1.0)),
                "indication_class": indication,
                "days_to_event": float(rng.integers(5, 40)),
                "directional_correct": bool(label),
                "option_pnl_pct": float(
                    rng.uniform(20.0, 200.0) if label else rng.uniform(-100.0, -10.0)
                ),
                "iv_crush_pct": float(rng.uniform(0.0, 30.0)),
            }
        )
    df = pd.DataFrame(rows)
    df["science_grade"] = pd.Categorical(
        df["science_grade"], categories=_SCIENCE_GRADES, ordered=True
    )
    df["sector"] = df["sector"].astype("category")
    df["indication_class"] = df["indication_class"].astype("category")
    df["prior_phase2_data_quality"] = pd.Categorical(
        df["prior_phase2_data_quality"],
        categories=[str(v) for v in sorted(PRIOR_PHASE2_DATA_QUALITY_MAP.values())],
        ordered=True,
    )
    for col in ("ticker", "ticker_raw", "entry_date", "catalyst_date"):
        df[col] = df[col].astype("string")
    df.to_parquet(path, engine="pyarrow", index=False)
    return df


def _materialise_ranker(tmp_path: Path) -> tuple[Path, pd.DataFrame]:
    """Write a synthetic parquet, train v1, and return ``(model_path, df)``."""

    parquet = tmp_path / "catalysts.parquet"
    df = _synthetic_parquet(parquet)
    result = tr.train(
        features_path=parquet,
        out_path=tmp_path / "models" / "ranker_v1.lgb",
        cv_folds=3,
        seed=42,
    )
    return result.model_path, df


def _reset_ranker_singleton() -> None:
    """Wipe the in-process ranker cache so flag toggles re-load cleanly."""
    from biotech_sniper.sectors import unified_scorer as _scorer

    _scorer._RANKER_SINGLETON_STATE["model"] = None
    _scorer._RANKER_SINGLETON_STATE["model_path"] = None
    _scorer._RANKER_SINGLETON_STATE["load_attempted_for_path"] = None


@pytest.fixture
def isolated_base_dir(monkeypatch, tmp_path: Path) -> Path:
    """Repoint ``BASE_DIR`` (used by ``models/`` resolution) at ``tmp_path``.

    The unified-scorer's ranker loader resolves ``models/ranker_v*.lgb``
    relative to the imported :data:`BASE_DIR` symbol. Tests that
    exercise the play-card emission path therefore have to relocate
    that symbol into ``tmp_path`` so the synthetic ranker is the only
    artefact discoverable by :func:`_resolve_ranker_model_path`.
    """
    from biotech_sniper.sectors import unified_scorer as _scorer

    monkeypatch.setattr(_scorer, "BASE_DIR", tmp_path)
    _reset_ranker_singleton()
    yield tmp_path
    _reset_ranker_singleton()


# ---------------------------------------------------------------------------
# 1. Ranker module — load_ranker + predict_proba contracts
# ---------------------------------------------------------------------------


def test_load_ranker_returns_predict_proba(tmp_path: Path) -> None:
    """VAL-M5-025: returned object exposes ``.predict_proba``."""

    model_path, _ = _materialise_ranker(tmp_path)
    model = load_ranker(model_path)
    assert hasattr(model, "predict_proba"), (
        "load_ranker must return an object exposing predict_proba"
    )
    assert callable(model.predict_proba)


def test_predict_proba_returns_two_column_softmax(tmp_path: Path) -> None:
    """VAL-M5-026: single-row DataFrame yields shape (1, 2) summing to 1."""

    model_path, df = _materialise_ranker(tmp_path)
    model = load_ranker(model_path)

    feature_cols = list(REQUIRED_FEATURE_COLS)
    X = df[feature_cols].head(1).copy()

    proba = model.predict_proba(X)

    assert proba.shape == (1, 2), (
        f"predict_proba must return shape (1, 2); got {proba.shape}"
    )
    assert abs(float(proba.sum()) - 1.0) < 1e-6, (
        "Each row of predict_proba must sum to 1 within float tolerance "
        f"(got row_sum={float(proba.sum())})"
    )
    assert (proba >= 0.0).all() and (proba <= 1.0).all(), (
        "predict_proba probabilities must lie in [0, 1]"
    )


def test_predict_proba_multi_row_shape(tmp_path: Path) -> None:
    """N-row input → (N, 2) probability matrix that sums to 1 per row."""

    model_path, df = _materialise_ranker(tmp_path)
    model = load_ranker(model_path)

    X = df[list(REQUIRED_FEATURE_COLS)].head(5).copy()
    proba = model.predict_proba(X)
    assert proba.shape == (5, 2)
    row_sums = proba.sum(axis=1)
    np.testing.assert_allclose(row_sums, np.ones(5), atol=1e-6)


def test_manifest_mismatch_extra_feature_raises(tmp_path: Path) -> None:
    """VAL-M5-027: extra feature in manifest → RankerSchemaMismatch."""

    model_path, _ = _materialise_ranker(tmp_path)
    manifest_path = model_path.with_suffix(".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["features"] = list(manifest["features"]) + ["extra_feature"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RankerSchemaMismatch) as excinfo:
        load_ranker(model_path)
    msg = str(excinfo.value)
    assert "extra" in msg.lower()
    assert "extra_feature" in msg
    # Diff must surface the extra entry on the exception itself.
    assert "extra_feature" in excinfo.value.extra


def test_manifest_mismatch_missing_feature_raises(tmp_path: Path) -> None:
    """Missing feature in manifest → RankerSchemaMismatch with diff."""

    model_path, _ = _materialise_ranker(tmp_path)
    manifest_path = model_path.with_suffix(".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dropped = manifest["features"].pop()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RankerSchemaMismatch) as excinfo:
        load_ranker(model_path)
    assert dropped in excinfo.value.missing


def test_manifest_missing_sidecar_raises_file_not_found(tmp_path: Path) -> None:
    """Sidecar missing → FileNotFoundError (not RankerSchemaMismatch)."""

    model_path, _ = _materialise_ranker(tmp_path)
    sidecar = model_path.with_suffix(".manifest.json")
    sidecar.unlink()
    with pytest.raises(FileNotFoundError):
        load_ranker(model_path)


def test_load_ranker_missing_model_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_ranker(tmp_path / "models" / "ranker_v1.lgb")


# ---------------------------------------------------------------------------
# 2. Feature flag default + import semantics
# ---------------------------------------------------------------------------


def test_feature_flag_defaults_to_false(monkeypatch) -> None:
    """VAL-M5-028: ``LIGHTGBM_RANKER_ENABLED`` defaults to ``False``.

    We re-import ``config`` with the env var unset so the module-level
    constant is recomputed from a clean environment.
    """
    monkeypatch.delenv("LIGHTGBM_RANKER_ENABLED", raising=False)
    import importlib

    fresh = importlib.reload(bs_config)
    try:
        assert fresh.LIGHTGBM_RANKER_ENABLED is False
    finally:
        importlib.reload(bs_config)


def test_lightgbm_not_imported_when_flag_off(monkeypatch) -> None:
    """VAL-M5-030: with flag OFF, attaching the supplementary score
    is a no-op AND ``lightgbm`` is not pulled into ``sys.modules``."""
    from biotech_sniper.sectors import unified_scorer as _scorer

    # Force the flag off and clear any cached ranker.
    monkeypatch.setattr(bs_config, "LIGHTGBM_RANKER_ENABLED", False)
    _reset_ranker_singleton()

    # Wipe any stale lightgbm import that earlier tests may have done.
    sys.modules.pop("lightgbm", None)
    sys.modules.pop("biotech_sniper.ranker", None)

    card = {"ticker": "TEST", "ensemble_score": 0.7}
    result = _scorer.attach_supplementary_score(card)

    assert "ranker_score" not in result
    assert "lightgbm" not in sys.modules, (
        "lightgbm must not be imported when LIGHTGBM_RANKER_ENABLED=False"
    )


# ---------------------------------------------------------------------------
# 3. Unified-scorer attach_supplementary_score behaviour
# ---------------------------------------------------------------------------


def test_attach_supplementary_score_when_flag_on(
    monkeypatch, isolated_base_dir: Path
) -> None:
    """VAL-M5-029: flag on → card receives the supplementary field."""

    _materialise_ranker(isolated_base_dir)
    monkeypatch.setattr(bs_config, "LIGHTGBM_RANKER_ENABLED", True)
    _reset_ranker_singleton()

    from biotech_sniper.sectors import unified_scorer as _scorer

    card = {
        "ticker": "DEMO",
        "as_of_date": "2026-04-27",
        "rank": 1,
        "ensemble_score": 0.7,
        "science_grade": "B",
        "claude_grade": "B",
        "claude_probability": 0.65,
        "gemini_grade": "B",
        "gemini_probability": 0.7,
        "grok_score": 0.72,
        "grok_rank": 1,
        "divergence_flag": False,
    }
    out = _scorer.attach_supplementary_score(card)
    assert "ranker_score" in out
    score = out["ranker_score"]
    assert isinstance(score, float)
    assert 0.0 <= score <= 1.0


def test_attach_supplementary_score_no_op_when_flag_off(
    monkeypatch, isolated_base_dir: Path
) -> None:
    _materialise_ranker(isolated_base_dir)
    monkeypatch.setattr(bs_config, "LIGHTGBM_RANKER_ENABLED", False)
    _reset_ranker_singleton()

    from biotech_sniper.sectors import unified_scorer as _scorer

    card = {"ticker": "DEMO", "ensemble_score": 0.7}
    out = _scorer.attach_supplementary_score(card)
    assert "ranker_score" not in out


def test_attach_supplementary_score_missing_artefact_warns(
    monkeypatch, isolated_base_dir: Path, caplog
) -> None:
    """Flag on but no model file → no decoration, WARNING logged, no crash."""
    monkeypatch.setattr(bs_config, "LIGHTGBM_RANKER_ENABLED", True)
    _reset_ranker_singleton()

    from biotech_sniper.sectors import unified_scorer as _scorer

    with caplog.at_level("WARNING"):
        out = _scorer.attach_supplementary_score({"ticker": "DEMO"})
    assert "ranker_score" not in out
    assert any("supplementary signal disabled" in r.message for r in caplog.records)


def test_attach_supplementary_score_caches_singleton(
    monkeypatch, isolated_base_dir: Path
) -> None:
    """Subsequent calls re-use the loaded model without re-reading disk."""

    _materialise_ranker(isolated_base_dir)
    monkeypatch.setattr(bs_config, "LIGHTGBM_RANKER_ENABLED", True)
    _reset_ranker_singleton()

    from biotech_sniper.sectors import unified_scorer as _scorer

    _scorer.attach_supplementary_score({"ticker": "A"})
    first_model = _scorer._RANKER_SINGLETON_STATE["model"]
    assert first_model is not None
    _scorer.attach_supplementary_score({"ticker": "B"})
    second_model = _scorer._RANKER_SINGLETON_STATE["model"]
    assert second_model is first_model


# ---------------------------------------------------------------------------
# 4. Play-card emission integration (flag on/off)
# ---------------------------------------------------------------------------


def _stub_select_top_n(monkeypatch, candidates: list[dict]) -> None:
    """Stub :func:`unified_scorer.select_top_n` to return a fixed list.

    The play-card formatter calls ``select_top_n`` to fetch candidates
    from SQLite. For the integration test we sidestep the database
    entirely by patching that function with a deterministic stub.
    """
    from biotech_sniper.sectors import unified_scorer as _scorer

    monkeypatch.setattr(_scorer, "select_top_n", lambda *a, **kw: candidates)


def _make_candidate(
    ticker: str, ensemble_score: float, *, cache_id: int = 1
) -> dict:
    return {
        "id": cache_id,
        "ticker": ticker,
        "as_of_date": "2026-04-27",
        "ensemble_score": ensemble_score,
        "science_grade": "B",
        "claude_grade": "B",
        "claude_probability": ensemble_score,
        "gemini_grade": "B",
        "gemini_probability": ensemble_score,
        "grok_score": ensemble_score,
        "grok_rank": 1,
        "divergence_flag": False,
    }


def test_play_card_flag_on_carries_ranker_score(
    monkeypatch, isolated_base_dir: Path
) -> None:
    """VAL-M5-029: with the flag on, every emitted card has the field."""

    _materialise_ranker(isolated_base_dir)
    monkeypatch.setattr(bs_config, "LIGHTGBM_RANKER_ENABLED", True)
    _reset_ranker_singleton()

    candidates = [
        _make_candidate("AAA", 0.71, cache_id=1),
        _make_candidate("BBB", 0.66, cache_id=2),
        _make_candidate("CCC", 0.60, cache_id=3),
    ]
    _stub_select_top_n(monkeypatch, candidates)

    from biotech_sniper import play_card_formatter as _pcf

    written = _pcf.emit_play_cards(
        as_of_date="2026-04-27",
        n=3,
        base_dir=isolated_base_dir,
        run_debate_for_divergent=False,
    )
    assert len(written) == 3
    for path in written:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert "ranker_score" in payload, (
            f"Flag-on card {path.name} missing supplementary signal field"
        )
        score = payload["ranker_score"]
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0


def test_play_card_flag_off_omits_ranker_score(
    monkeypatch, isolated_base_dir: Path
) -> None:
    """VAL-M5-030: with flag off the field is absent on every card."""

    monkeypatch.setattr(bs_config, "LIGHTGBM_RANKER_ENABLED", False)
    _reset_ranker_singleton()

    candidates = [
        _make_candidate("AAA", 0.71, cache_id=1),
        _make_candidate("BBB", 0.66, cache_id=2),
    ]
    _stub_select_top_n(monkeypatch, candidates)

    from biotech_sniper import play_card_formatter as _pcf

    written = _pcf.emit_play_cards(
        as_of_date="2026-04-27",
        n=2,
        base_dir=isolated_base_dir,
        run_debate_for_divergent=False,
    )
    for path in written:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert "ranker_score" not in payload


def test_pre_score_invariant_under_flag_toggle(
    monkeypatch, isolated_base_dir: Path
) -> None:
    """VAL-M5-031: ``ensemble_score`` is byte-identical with flag on vs off."""

    _materialise_ranker(isolated_base_dir)

    candidates = [
        _make_candidate("AAA", 0.71, cache_id=1),
        _make_candidate("BBB", 0.66, cache_id=2),
        _make_candidate("CCC", 0.60, cache_id=3),
    ]
    _stub_select_top_n(monkeypatch, candidates)

    from biotech_sniper import play_card_formatter as _pcf

    # Flag OFF baseline.
    monkeypatch.setattr(bs_config, "LIGHTGBM_RANKER_ENABLED", False)
    _reset_ranker_singleton()
    off_dir = isolated_base_dir / "play_cards_off"
    off_dir.mkdir()
    written_off = _pcf.emit_play_cards(
        as_of_date="2026-04-27",
        n=3,
        base_dir=off_dir,
        run_debate_for_divergent=False,
    )
    off_scores = {
        json.loads(p.read_text())["ticker"]: json.loads(p.read_text())[
            "ensemble_score"
        ]
        for p in written_off
    }

    # Flag ON.
    monkeypatch.setattr(bs_config, "LIGHTGBM_RANKER_ENABLED", True)
    _reset_ranker_singleton()
    on_dir = isolated_base_dir / "play_cards_on"
    on_dir.mkdir()
    written_on = _pcf.emit_play_cards(
        as_of_date="2026-04-27",
        n=3,
        base_dir=on_dir,
        run_debate_for_divergent=False,
    )
    on_scores = {
        json.loads(p.read_text())["ticker"]: json.loads(p.read_text())[
            "ensemble_score"
        ]
        for p in written_on
    }

    assert set(off_scores) == set(on_scores) == {"AAA", "BBB", "CCC"}
    for tkr, off_val in off_scores.items():
        on_val = on_scores[tkr]
        # Ensemble score must be float-equal under flag toggle (both
        # sides come from the same candidate dict; the toggle never
        # recomputes the LLM ensemble).
        assert off_val == on_val, (
            f"ensemble_score for {tkr} must be invariant under flag "
            f"toggle (off={off_val}, on={on_val})"
        )


# ---------------------------------------------------------------------------
# 5. Boundary / safety: no executor or risk module references the field
# ---------------------------------------------------------------------------


def test_ranker_field_not_referenced_in_trade_gating_modules() -> None:
    """VAL-M5-031 boundary: the supplementary field appears only in the
    scorer, the play-card builder, and reporting — never in
    paper_executor / position sizing / risk gating."""

    repo_root = Path(__file__).resolve().parent.parent / "biotech_sniper"
    forbidden_files = [
        repo_root / "paper_executor.py",
        repo_root / "alpaca_client.py",
        repo_root / "rotation_engine.py",
        repo_root / "hold_policy.py",
        repo_root / "stop_loss.py",
        repo_root / "iv_crush_exit_rules.py",
        repo_root / "liquidity_probe.py",
        repo_root / "execution_subscriber.py",
        repo_root / "execution_fills.py",
    ]
    for path in forbidden_files:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        assert "ranker_score" not in text, (
            f"{path.relative_to(repo_root.parent)} must not reference "
            f"the supplementary signal field"
        )


def test_grep_verification_passes() -> None:
    """Mirrors the feature's verification step — the only files
    referencing the supplementary field are the scorer, the play-card
    builder/formatter, reports, and tests."""

    repo_root = Path(__file__).resolve().parent.parent / "biotech_sniper"
    offenders: list[Path] = []
    for py in repo_root.rglob("*.py"):
        rel = py.relative_to(repo_root)
        if any(
            part in {"reports", "tests"}
            for part in rel.parts
        ):
            continue
        text = py.read_text(encoding="utf-8")
        if "ranker_score" not in text:
            continue
        # Allowed: unified_scorer + play card builder/formatter.
        if py.name in {"unified_scorer.py", "play_card_builder.py"}:
            continue
        offenders.append(py)
    assert not offenders, (
        "Found unexpected references to the supplementary signal field "
        f"in: {[str(o) for o in offenders]}"
    )


# ---------------------------------------------------------------------------
# 6. Smoke: lightgbm import only happens once flag is on
# ---------------------------------------------------------------------------


def test_subprocess_flag_off_keeps_lightgbm_unimported(tmp_path: Path) -> None:
    """End-to-end: a fresh Python interpreter with flag off + scoring
    work performed should not have ``lightgbm`` in ``sys.modules``."""

    script = (
        "import sys\n"
        "import os\n"
        "os.environ['LIGHTGBM_RANKER_ENABLED'] = '0'\n"
        "from biotech_sniper.sectors import unified_scorer as us\n"
        "us.attach_supplementary_score({'ticker': 'X'})\n"
        "print('LIGHTGBM_PRESENT=' + str('lightgbm' in sys.modules))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert proc.returncode == 0, (
        f"subprocess failed: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "LIGHTGBM_PRESENT=False" in proc.stdout, (
        f"lightgbm leaked into sys.modules with flag OFF: {proc.stdout!r}"
    )
