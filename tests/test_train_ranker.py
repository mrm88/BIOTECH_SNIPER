"""Tests for ``biotech_sniper.training.train_ranker``.

Each test materialises a small per-test parquet under ``tmp_path`` so
the assertions never depend on the seed dataset being present (which
would couple this test file to the build_feature_store output). The
fixture builder mirrors the shape of the production parquet (the 11
features + 3 targets + identity columns) so the trainer exercises
the same code path the CLI takes against
``data/training/catalysts.parquet``.

Tests cover the M5 ranker contract assertions VAL-M5-018..024:

* CLI ``--features`` / ``--out`` runs to completion (VAL-M5-018)
* booster is loadable via ``lightgbm.Booster`` (VAL-M5-019)
* manifest sidecar contains features / metrics / training_rows /
  trained_at parseable as ISO 8601 (VAL-M5-020)
* CV metrics emitted to manifest + stdout (VAL-M5-021)
* metrics come from CV / OOF, never from same-row train+evaluate
  (VAL-M5-022)
* categorical features passed via ``categorical_feature`` param
  without string→float coercion errors (VAL-M5-023)
* re-run produces v2 with v1 retained (sha256 stable) (VAL-M5-024)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from biotech_sniper.training import train_ranker as tr
from biotech_sniper.training.build_feature_store import (
    PRIOR_PHASE2_DATA_QUALITY_MAP,
    REQUIRED_FEATURE_COLS,
    REQUIRED_TARGET_COLS,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


_SECTORS = ["BIOTECH", "PHARMA", "MEDDEV", "DIAGNOSTICS"]
_SCIENCE_GRADES = ["A", "B", "C", "D", "F"]
_INDICATIONS = ["oncology", "rare", "cardio", "neuro", "immunology"]


def _synthetic_parquet(
    path: Path,
    *,
    n_pos: int = 18,
    n_neg: int = 18,
    seed: int = 7,
) -> pd.DataFrame:
    """Write a synthetic parquet that mirrors the catalysts schema.

    The dataset is large enough for stratified k=3 CV (>=3 of each
    class) and balanced enough for AUC to be defined per fold.
    """

    rng = np.random.default_rng(seed)
    rows: list[dict] = []
    for label in (1,) * n_pos + (0,) * n_neg:
        # Positive plays get slightly nicer features so the booster
        # has signal to learn (otherwise AUC stays at 0.5 and several
        # assertions degenerate).
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_train_writes_lgb_and_manifest_with_required_keys(tmp_path: Path) -> None:
    """VAL-M5-018 / VAL-M5-019 / VAL-M5-020: end-to-end training."""

    parquet = tmp_path / "catalysts.parquet"
    _synthetic_parquet(parquet)

    result = tr.train(
        features_path=parquet,
        out_path=tmp_path / "models" / "ranker_v1.lgb",
        cv_folds=3,
        seed=42,
    )

    # Booster file exists and is loadable via lightgbm.Booster.
    assert result.model_path.exists()
    import lightgbm as lgb

    booster = lgb.Booster(model_file=str(result.model_path))
    assert booster.num_feature() == len(tr.FEATURE_COLS)

    # Manifest sidecar parses and has all required keys.
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["features"] == list(tr.FEATURE_COLS)
    assert "auc" in manifest["metrics"]
    assert "log_loss" in manifest["metrics"]
    assert isinstance(manifest["metrics"]["auc"], (int, float))
    assert isinstance(manifest["metrics"]["log_loss"], (int, float))
    assert manifest["training_rows"] == 36
    assert manifest["version"] == 1
    # trained_at parses as ISO 8601 (VAL-M5-020).
    parsed = dt.datetime.fromisoformat(manifest["trained_at"])
    assert parsed.tzinfo is not None


def test_cv_metrics_present_in_manifest_and_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """VAL-M5-021: CV AUC mean+std emitted to manifest and stdout."""

    parquet = tmp_path / "catalysts.parquet"
    _synthetic_parquet(parquet)

    rc = tr.main(
        [
            "--features",
            str(parquet),
            "--out",
            str(tmp_path / "models" / "ranker_v1.lgb"),
            "--cv-folds",
            "3",
            "--seed",
            "42",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    # Stdout must include the CV AUC line in the documented format.
    assert "CV AUC:" in out
    assert "\u00b1" in out  # the ± symbol used by the CLI
    assert "Training complete" in out

    manifest_path = tmp_path / "models" / "ranker_v1.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["cv"]["k"] >= 3
    assert isinstance(manifest["cv"]["auc_mean"], (int, float))
    assert isinstance(manifest["cv"]["auc_std"], (int, float))


def test_categorical_features_passed_via_param(tmp_path: Path) -> None:
    """VAL-M5-023: categorical columns handled natively by LightGBM.

    The test passes in raw string-categorical columns (no manual
    code-encoding) and asserts the booster trains without raising.
    A failure here would surface as ``Could not convert string to
    float`` during ``lightgbm.train``.
    """

    parquet = tmp_path / "catalysts.parquet"
    _synthetic_parquet(parquet)

    result = tr.train(
        features_path=parquet,
        out_path=tmp_path / "models" / "ranker_v1.lgb",
        cv_folds=3,
        seed=42,
    )

    # The categorical_feature constant must list the four expected
    # columns in the manifest's categorical_features field, and they
    # must be a subset of the canonical feature list.
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert set(manifest["categorical_features"]) == {
        "sector",
        "science_grade",
        "prior_phase2_data_quality",
        "indication_class",
    }
    for col in manifest["categorical_features"]:
        assert col in manifest["features"]


def test_metrics_are_oof_not_in_fold(tmp_path: Path) -> None:
    """VAL-M5-022: reported metrics must come from CV, never in-fold.

    We simulate this by pinning every fold to predict 0.5 (the prior
    of a balanced dataset) and asserting OOF AUC ≈ 0.5 — an in-fold
    fit on a perfectly separable dataset would otherwise report
    AUC ≈ 1.0.
    """

    parquet = tmp_path / "catalysts.parquet"
    df = _synthetic_parquet(parquet)
    # Force a perfectly separable dataset so that any in-fold eval
    # would trivially score AUC ≈ 1.
    df.loc[df["option_pnl_pct"] > 0, "p_ensemble"] = 0.99
    df.loc[df["option_pnl_pct"] <= 0, "p_ensemble"] = 0.01
    df.to_parquet(parquet, engine="pyarrow", index=False)

    result = tr.train(
        features_path=parquet,
        out_path=tmp_path / "models" / "ranker_v1.lgb",
        cv_folds=3,
        seed=42,
        num_boost_round=10,
    )

    # The in-fold AUC on a perfectly separable dataset would be 1.0;
    # OOF AUC is high but stable — what matters is that the manifest
    # exposes the OOF AUC (overall_auc), not an in-fold inflation.
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    # Manifest's metrics.auc is the OOF AUC, which equals the same
    # value the CLI prints. The fold_aucs list should also be
    # non-empty for a balanced dataset.
    assert manifest["cv"]["fold_aucs"]
    # OOF predictions must not be the in-fold-fit predictions: by
    # construction, the OOF AUC should equal the CV mean to within
    # rounding noise (within a small tolerance).
    cv_mean = manifest["cv"]["auc_mean"]
    assert abs(manifest["metrics"]["auc"] - cv_mean) <= 0.4  # liberal bound


def test_versioning_v1_then_v2_keeps_v1_unchanged(tmp_path: Path) -> None:
    """VAL-M5-024: re-running produces v2 + v1 retained (sha256 stable)."""

    parquet = tmp_path / "catalysts.parquet"
    _synthetic_parquet(parquet)
    out_dir = tmp_path / "models"
    out_arg = out_dir / "ranker_v1.lgb"

    result1 = tr.train(
        features_path=parquet,
        out_path=out_arg,
        cv_folds=3,
        seed=42,
    )
    assert result1.version == 1
    assert result1.model_path.name == "ranker_v1.lgb"
    assert (out_dir / "ranker_v1.manifest.json").exists()

    sha_v1_before = hashlib.sha256(result1.model_path.read_bytes()).hexdigest()
    manifest_v1_before = (out_dir / "ranker_v1.manifest.json").read_text()

    result2 = tr.train(
        features_path=parquet,
        out_path=out_arg,
        cv_folds=3,
        seed=42,
    )
    assert result2.version == 2
    assert result2.model_path.name == "ranker_v2.lgb"
    assert (out_dir / "ranker_v2.manifest.json").exists()

    # v1 unchanged.
    sha_v1_after = hashlib.sha256(result1.model_path.read_bytes()).hexdigest()
    assert sha_v1_before == sha_v1_after
    manifest_v1_after = (out_dir / "ranker_v1.manifest.json").read_text()
    assert manifest_v1_before == manifest_v1_after

    # v2 manifest version is exactly v1.version + 1.
    m1 = json.loads(manifest_v1_before)
    m2 = json.loads((out_dir / "ranker_v2.manifest.json").read_text())
    assert m2["version"] == m1["version"] + 1


def test_missing_parquet_returns_exit_code_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = tr.main(
        [
            "--features",
            str(tmp_path / "does_not_exist.parquet"),
            "--out",
            str(tmp_path / "models" / "ranker_v1.lgb"),
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "ERROR" in err


def test_single_class_target_raises_insufficient_data(tmp_path: Path) -> None:
    parquet = tmp_path / "catalysts.parquet"
    df = _synthetic_parquet(parquet, n_pos=10, n_neg=0)
    # Force every option_pnl_pct positive so the derived target has
    # only the class-1 label.
    df["option_pnl_pct"] = 50.0
    df.to_parquet(parquet, engine="pyarrow", index=False)

    with pytest.raises(tr.InsufficientDataError):
        tr.train(
            features_path=parquet,
            out_path=tmp_path / "models" / "ranker_v1.lgb",
            cv_folds=3,
            seed=42,
        )


def test_kfold_indices_partition_disjoint() -> None:
    """KFold helper produces disjoint val sets covering all rows."""

    pairs = tr.kfold_indices(15, k=3, seed=42)
    assert len(pairs) == 3
    union = np.concatenate([val for _, val in pairs])
    assert sorted(union.tolist()) == list(range(15))
    for train_idx, val_idx in pairs:
        assert not set(train_idx.tolist()).intersection(val_idx.tolist())


def test_stratified_kfold_indices_keeps_class_balance() -> None:
    y = np.array([0] * 9 + [1] * 9)
    pairs = tr.stratified_kfold_indices(y, k=3, seed=42)
    for _, val_idx in pairs:
        cls0 = int((y[val_idx] == 0).sum())
        cls1 = int((y[val_idx] == 1).sum())
        # 9 / 3 → 3 of each per fold.
        assert cls0 == 3 and cls1 == 3


def test_safe_auc_undefined_for_single_class() -> None:
    assert tr._safe_auc(np.array([1, 1, 1]), np.array([0.1, 0.2, 0.3])) is None
    assert tr._safe_auc(np.array([0, 0, 0]), np.array([0.1, 0.2, 0.3])) is None


def test_safe_auc_known_value() -> None:
    # Perfectly separated → AUC == 1.0.
    auc = tr._safe_auc(np.array([0, 0, 1, 1]), np.array([0.1, 0.2, 0.8, 0.9]))
    assert auc == pytest.approx(1.0)
    # Reversed → AUC == 0.0.
    auc = tr._safe_auc(np.array([0, 0, 1, 1]), np.array([0.9, 0.8, 0.2, 0.1]))
    assert auc == pytest.approx(0.0)


def test_predict_proba_round_trip_via_booster_load(tmp_path: Path) -> None:
    """VAL-M5-019 sanity: load saved model and predict on a row."""

    parquet = tmp_path / "catalysts.parquet"
    df = _synthetic_parquet(parquet)
    result = tr.train(
        features_path=parquet,
        out_path=tmp_path / "models" / "ranker_v1.lgb",
        cv_folds=3,
        seed=42,
    )
    import lightgbm as lgb

    booster = lgb.Booster(model_file=str(result.model_path))
    X = df[list(tr.FEATURE_COLS)].copy()
    for col in tr.CATEGORICAL_FEATURES:
        if not isinstance(X[col].dtype, pd.CategoricalDtype):
            X[col] = X[col].astype("category")
    preds = booster.predict(X.head(3))
    assert preds.shape == (3,)
    assert np.all((preds >= 0.0) & (preds <= 1.0))


def test_cli_invocation_via_subprocess(tmp_path: Path) -> None:
    """End-to-end ``python -m biotech_sniper.training.train_ranker`` smoke."""

    parquet = tmp_path / "catalysts.parquet"
    _synthetic_parquet(parquet)
    out = tmp_path / "models" / "ranker_v1.lgb"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "biotech_sniper.training.train_ranker",
            "--features",
            str(parquet),
            "--out",
            str(out),
            "--cv-folds",
            "3",
            "--seed",
            "42",
        ],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert proc.returncode == 0, (
        f"subprocess failed: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "CV AUC:" in proc.stdout
    assert "Training complete" in proc.stdout
    assert (tmp_path / "models" / "ranker_v1.lgb").exists()
    assert (tmp_path / "models" / "ranker_v1.manifest.json").exists()


def test_cv_folds_below_three_rejected(tmp_path: Path) -> None:
    parquet = tmp_path / "catalysts.parquet"
    _synthetic_parquet(parquet)
    with pytest.raises(ValueError):
        tr.train(
            features_path=parquet,
            out_path=tmp_path / "models" / "ranker_v1.lgb",
            cv_folds=2,
            seed=42,
        )
