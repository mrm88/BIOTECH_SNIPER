"""Train a LightGBM ranker on ``data/training/catalysts.parquet`` (M5).

This module is the M5 supplementary-signal trainer. It reads the
catalysts feature store materialised by
:mod:`biotech_sniper.training.build_feature_store`, runs k-fold
cross-validation (k≥3) to produce honest out-of-fold metrics, fits a
final LightGBM booster on the full dataset, and writes both the
booster (``models/ranker_v{N}.lgb``) and a JSON manifest sidecar
(``models/ranker_v{N}.manifest.json``).

Versioning
----------

If ``models/ranker_v1.lgb`` already exists, the next run produces
``models/ranker_v2.lgb`` while leaving v1 (and its manifest)
untouched. Subsequent re-runs continue to monotonically increment the
version. The version number is detected from existing files in the
output directory matching ``ranker_v{N}.lgb`` — the exact version
number passed in ``--out`` is used as a starting hint when no
existing files are found, otherwise it is overridden by ``max+1``.

Targets
-------

The binary target is ``profitable = (option_pnl_pct > 0).astype(int)``
which captures the realised option leg P&L outcome. This is a
supplementary signal layer (the LLM ensemble continues to drive
``pre_score``) so the ranker only needs to model
profitability — not magnitude.

Categorical features
--------------------

The four categorical columns in the parquet schema
(``sector``, ``science_grade``, ``prior_phase2_data_quality``,
``indication_class``) are converted to ``pandas.Categorical`` codes
and the column names are passed to LightGBM via the
``categorical_feature`` parameter so the booster can treat them
natively (avoiding ``Could not convert string to float`` errors —
VAL-M5-023).

Cross-validation
----------------

The trainer uses :class:`StratifiedKFold` when both classes have at
least ``k`` samples; otherwise it falls back to a shuffled
:class:`KFold` (avoids ``sklearn`` as a hard dependency by
implementing both splitters in this module). Reported
``metrics.auc`` and ``metrics.log_loss`` come from the *out-of-fold*
predictions (never from in-fold training set evaluations —
VAL-M5-022). Per-fold AUCs are also captured and their mean+std
recorded under ``cv.auc_mean`` / ``cv.auc_std``.

CLI
---

::

    python -m biotech_sniper.training.train_ranker \
        --features data/training/catalysts.parquet \
        --out models/ranker_v1.lgb \
        [--cv-folds 3] [--seed 42]

On success the trainer prints a single-line summary with the CV AUC
mean ± std and the path to the saved booster. Exit code 0 on success;
2 if the parquet is missing or unreadable; 3 on training/CV failure.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Iterable, Sequence

import numpy as np
import pandas as pd

from biotech_sniper.paths import BASE_DIR
from biotech_sniper.training.build_feature_store import (
    PRIOR_PHASE2_DATA_QUALITY_MAP,
    REQUIRED_FEATURE_COLS,
)

__all__ = [
    "FEATURE_COLS",
    "CATEGORICAL_FEATURES",
    "TARGET_COL_PRIMARY",
    "TrainResult",
    "MissingFeatureStoreError",
    "InsufficientDataError",
    "build_target",
    "kfold_indices",
    "stratified_kfold_indices",
    "train",
    "main",
]


_log = logging.getLogger(__name__)


# Canonical 11-feature column list (VAL-M5-020 expects the manifest's
# ``features`` to match this list).
FEATURE_COLS: Final[tuple[str, ...]] = tuple(REQUIRED_FEATURE_COLS)

# Categorical columns passed to LightGBM via ``categorical_feature``
# (VAL-M5-023). Order matches the canonical feature list and is the
# subset of FEATURE_COLS whose dtype is categorical in the parquet.
CATEGORICAL_FEATURES: Final[tuple[str, ...]] = (
    "sector",
    "science_grade",
    "prior_phase2_data_quality",
    "indication_class",
)

# Primary binary target column derived from the parquet's
# ``option_pnl_pct``. ``directional_correct`` is also exposed in the
# parquet but it is correlated by construction (a correct directional
# call usually maps to positive option P&L); profitability is the
# operationally meaningful target here.
TARGET_COL_PRIMARY: Final[str] = "profitable"

# LightGBM hyper-parameter set tuned for the small (10s–100s of rows)
# catalyst dataset. ``min_data_in_leaf=1`` / ``min_data_in_bin=1``
# allow the booster to actually fit on the 6-row seed; ``verbosity=-1``
# silences the tiny-dataset warnings.
DEFAULT_LGB_PARAMS: Final[dict[str, Any]] = {
    "objective": "binary",
    "metric": "binary_logloss",
    "learning_rate": 0.05,
    "num_leaves": 7,
    "min_data_in_leaf": 1,
    "min_data_in_bin": 1,
    "feature_fraction": 1.0,
    "bagging_fraction": 1.0,
    "bagging_freq": 0,
    "lambda_l1": 0.0,
    "lambda_l2": 0.0,
    "verbosity": -1,
    "deterministic": True,
    "force_col_wise": True,
}

DEFAULT_NUM_BOOST_ROUND: Final[int] = 100

# Filename pattern for ranker artifacts (``ranker_v<N>.lgb``).
_RANKER_FNAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^ranker_v(?P<version>\d+)\.lgb$"
)


class MissingFeatureStoreError(FileNotFoundError):
    """Raised when ``--features`` does not point at a readable parquet."""


class InsufficientDataError(ValueError):
    """Raised when the feature store has too few rows or one-class targets."""


@dataclass
class TrainResult:
    """Structured summary of a successful training run."""

    model_path: Path
    manifest_path: Path
    version: int
    training_rows: int
    cv_k: int
    cv_auc_mean: float
    cv_auc_std: float
    metrics_auc: float
    metrics_log_loss: float
    fold_aucs: list[float]
    trained_at: str
    features: list[str]


# ---------------------------------------------------------------------------
# Data preparation
# ---------------------------------------------------------------------------


def _load_parquet(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise MissingFeatureStoreError(
            f"Feature store parquet not found at {path}. Run "
            "`python -m biotech_sniper.training.build_feature_store` first."
        )
    try:
        df = pd.read_parquet(path)
    except Exception as exc:  # pragma: no cover - depends on pyarrow
        raise MissingFeatureStoreError(
            f"Could not read parquet at {path}: {exc}"
        ) from exc
    return df


def build_target(df: pd.DataFrame) -> np.ndarray:
    """Return the binary profitability target as ``int64`` array.

    A row is labelled ``1`` when ``option_pnl_pct > 0`` and ``0``
    otherwise. Missing values are treated as losses (``0``) so the
    label is always defined.
    """

    if "option_pnl_pct" not in df.columns:
        raise InsufficientDataError(
            "Parquet is missing the 'option_pnl_pct' column required "
            "to derive the binary target."
        )
    pnl = pd.to_numeric(df["option_pnl_pct"], errors="coerce").fillna(0.0)
    return (pnl > 0).astype(np.int64).to_numpy()


def _prepare_feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Return a frame containing only :data:`FEATURE_COLS` with
    canonical dtypes for LightGBM consumption.

    Categorical columns are coerced to ``pandas.Categorical`` (so the
    LightGBM Dataset constructor recognises them and emits the
    correct ``feature_type`` per column). Numeric columns are
    coerced to ``float64`` with NaN preserved (LightGBM handles NaN
    natively).
    """

    missing = [c for c in FEATURE_COLS if c not in df.columns]
    if missing:
        raise InsufficientDataError(
            f"Parquet is missing required feature columns: {missing}"
        )

    out = df[list(FEATURE_COLS)].copy()
    for col in CATEGORICAL_FEATURES:
        # ``astype('category')`` gives lightgbm a proper categorical
        # dtype regardless of whether the parquet round-trip already
        # preserved it.
        if not isinstance(out[col].dtype, pd.CategoricalDtype):
            out[col] = out[col].astype("category")
    numeric_cols = [c for c in FEATURE_COLS if c not in CATEGORICAL_FEATURES]
    for col in numeric_cols:
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")
    return out


# ---------------------------------------------------------------------------
# K-fold splitters (no sklearn dependency)
# ---------------------------------------------------------------------------


def kfold_indices(
    n: int,
    *,
    k: int,
    seed: int,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Return shuffled K-fold ``(train_idx, val_idx)`` index pairs.

    The implementation matches sklearn's ``KFold(shuffle=True)``
    semantics closely (deterministic on ``seed``, near-equal fold
    sizes) without the dependency.
    """

    if k < 2:
        raise ValueError(f"k must be >= 2, got {k}")
    if n < k:
        raise ValueError(f"need at least {k} samples for {k}-fold, got {n}")

    rng = np.random.default_rng(seed)
    permuted = rng.permutation(n)
    folds = np.array_split(permuted, k)
    pairs: list[tuple[np.ndarray, np.ndarray]] = []
    for i in range(k):
        val_idx = np.sort(folds[i])
        train_idx = np.sort(np.concatenate([folds[j] for j in range(k) if j != i]))
        pairs.append((train_idx, val_idx))
    return pairs


def stratified_kfold_indices(
    y: np.ndarray,
    *,
    k: int,
    seed: int,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Return shuffled stratified K-fold index pairs.

    Each class is shuffled and partitioned into ``k`` chunks
    independently; fold ``i`` is the union of the i-th chunk from
    each class. This matches sklearn's ``StratifiedKFold(shuffle=True)``
    behaviour for binary targets and works as long as every class has
    at least ``k`` members.
    """

    if k < 2:
        raise ValueError(f"k must be >= 2, got {k}")

    rng = np.random.default_rng(seed)
    classes, counts = np.unique(y, return_counts=True)
    if counts.min() < k:
        raise ValueError(
            f"stratified split needs >= {k} samples per class; got "
            f"{dict(zip(classes.tolist(), counts.tolist()))}"
        )

    # For each class, produce k near-equal index chunks (shuffled).
    per_class_chunks: list[list[np.ndarray]] = []
    for cls in classes:
        cls_idx = np.where(y == cls)[0]
        rng.shuffle(cls_idx)
        per_class_chunks.append(np.array_split(cls_idx, k))

    pairs: list[tuple[np.ndarray, np.ndarray]] = []
    for fold in range(k):
        val_parts = [chunks[fold] for chunks in per_class_chunks]
        val_idx = np.sort(np.concatenate(val_parts))
        all_idx = np.arange(len(y))
        train_idx = np.sort(np.setdiff1d(all_idx, val_idx, assume_unique=False))
        pairs.append((train_idx, val_idx))
    return pairs


def _choose_splits(
    y: np.ndarray, *, k: int, seed: int
) -> tuple[str, list[tuple[np.ndarray, np.ndarray]]]:
    """Pick the strongest splitter the data supports.

    Returns ``(splitter_name, fold_indices)``. Stratified is
    preferred when both classes have at least ``k`` samples;
    otherwise we fall back to plain shuffled K-fold.
    """

    classes, counts = np.unique(y, return_counts=True)
    if len(classes) < 2 or counts.min() < k:
        return "kfold", kfold_indices(len(y), k=k, seed=seed)
    return "stratified_kfold", stratified_kfold_indices(y, k=k, seed=seed)


# ---------------------------------------------------------------------------
# Metrics (sklearn-free implementations)
# ---------------------------------------------------------------------------


def _safe_log_loss(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Binary log loss with epsilon clipping."""

    eps = 1e-15
    p = np.clip(np.asarray(y_pred, dtype=np.float64), eps, 1 - eps)
    y = np.asarray(y_true, dtype=np.float64)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _safe_auc(y_true: np.ndarray, y_pred: np.ndarray) -> float | None:
    """Binary ROC AUC via Mann-Whitney U; returns ``None`` if undefined.

    Returns ``None`` when ``y_true`` does not contain both classes
    (the AUC is degenerate). Otherwise returns the standard
    rank-based AUC.
    """

    y = np.asarray(y_true, dtype=np.int64)
    if y.size == 0 or np.unique(y).size < 2:
        return None
    p = np.asarray(y_pred, dtype=np.float64)
    pos_mask = y == 1
    pos = p[pos_mask]
    neg = p[~pos_mask]
    n_pos = pos.size
    n_neg = neg.size
    if n_pos == 0 or n_neg == 0:
        return None
    # Rank-based AUC via Mann-Whitney U statistic, ties get average
    # rank (matches sklearn behaviour).
    order = np.argsort(p, kind="mergesort")
    ranks = np.empty_like(order, dtype=np.float64)
    ranks[order] = np.arange(1, p.size + 1, dtype=np.float64)
    # Average ranks across ties.
    sorted_p = p[order]
    i = 0
    while i < sorted_p.size:
        j = i
        while j + 1 < sorted_p.size and sorted_p[j + 1] == sorted_p[i]:
            j += 1
        if j > i:
            avg = (ranks[order[i]] + ranks[order[j]]) / 2.0
            ranks[order[i : j + 1]] = avg
        i = j + 1
    sum_ranks_pos = ranks[pos_mask].sum()
    auc = (sum_ranks_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)
    return float(auc)


# ---------------------------------------------------------------------------
# Versioning helpers
# ---------------------------------------------------------------------------


def _detect_next_version(out_dir: Path, hint_version: int) -> int:
    """Return the next free ``ranker_v{N}.lgb`` version number.

    If no ``ranker_v*.lgb`` files exist in ``out_dir``, returns
    ``hint_version`` (so a first run honours whatever the operator
    passed via ``--out``). Otherwise returns ``max_existing + 1``.
    """

    if not out_dir.exists():
        return hint_version
    existing: list[int] = []
    for f in out_dir.iterdir():
        if not f.is_file():
            continue
        m = _RANKER_FNAME_RE.match(f.name)
        if m:
            existing.append(int(m.group("version")))
    if not existing:
        return hint_version
    return max(existing) + 1


def _resolve_output_paths(
    out_arg: Path,
) -> tuple[Path, Path, int]:
    """Resolve the canonical model + manifest paths.

    The user-provided ``--out`` is treated as a hint: the directory
    is honoured as-is, but the filename is rewritten as
    ``ranker_v{N}.lgb`` where ``N`` is the next free version found
    in that directory. The manifest sidecar lives next to the
    booster as ``ranker_v{N}.manifest.json``.
    """

    out_arg = out_arg.expanduser()
    out_dir = out_arg.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # Pick the version hint from ``--out`` if it matches the canonical
    # pattern; otherwise default to v1.
    m = _RANKER_FNAME_RE.match(out_arg.name)
    hint_version = int(m.group("version")) if m else 1

    version = _detect_next_version(out_dir, hint_version)
    model_path = out_dir / f"ranker_v{version}.lgb"
    manifest_path = out_dir / f"ranker_v{version}.manifest.json"
    return model_path, manifest_path, version


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------


def _make_lgb_dataset(
    X: pd.DataFrame, y: np.ndarray, *, reference: Any | None = None
) -> Any:
    """Construct a ``lightgbm.Dataset`` honouring our categorical list."""

    import lightgbm as lgb  # local import to keep module import cheap

    return lgb.Dataset(
        X,
        label=y,
        categorical_feature=list(CATEGORICAL_FEATURES),
        free_raw_data=False,
        reference=reference,
    )


def _train_one_booster(
    X_train: pd.DataFrame,
    y_train: np.ndarray,
    *,
    params: dict[str, Any],
    num_boost_round: int,
) -> Any:
    """Train a single LightGBM booster on the supplied fold."""

    import lightgbm as lgb

    # ``categorical_feature`` is wired through the Dataset constructor
    # in :func:`_make_lgb_dataset` (the modern LightGBM API path);
    # passing the same kwarg to ``lgb.train`` is deprecated in
    # lightgbm>=4.5 so we deliberately keep it on the Dataset only.
    train_set = _make_lgb_dataset(X_train, y_train)
    booster = lgb.train(
        params=params,
        train_set=train_set,
        num_boost_round=num_boost_round,
    )
    return booster


def _predict_proba(booster: Any, X: pd.DataFrame) -> np.ndarray:
    preds = booster.predict(X, raw_score=False)
    # binary objective → 1-D array of class-1 probabilities.
    return np.asarray(preds, dtype=np.float64)


def train(
    *,
    features_path: Path,
    out_path: Path,
    cv_folds: int = 3,
    seed: int = 42,
    num_boost_round: int = DEFAULT_NUM_BOOST_ROUND,
    params: dict[str, Any] | None = None,
) -> TrainResult:
    """Train a LightGBM ranker with k-fold CV and persist artifacts.

    Returns a :class:`TrainResult` describing the run. Raises
    :class:`MissingFeatureStoreError` when the parquet is missing,
    :class:`InsufficientDataError` when the parquet has too few rows
    or only one class, and propagates LightGBM training errors.
    """

    if cv_folds < 3:
        raise ValueError(
            f"cv_folds must be >= 3 (per spec), got {cv_folds}"
        )

    df = _load_parquet(features_path)
    if df.empty:
        raise InsufficientDataError(
            f"Feature store at {features_path} is empty; cannot train."
        )

    X = _prepare_feature_frame(df)
    y = build_target(df)

    if len(X) < cv_folds:
        raise InsufficientDataError(
            f"Need at least {cv_folds} rows for {cv_folds}-fold CV, "
            f"got {len(X)}."
        )

    classes, counts = np.unique(y, return_counts=True)
    if classes.size < 2:
        raise InsufficientDataError(
            "Target column has only one class; cannot train a binary "
            f"classifier (counts={dict(zip(classes.tolist(), counts.tolist()))})."
        )

    splitter_name, folds = _choose_splits(y, k=cv_folds, seed=seed)
    _log.info(
        "train_ranker: %s splitter selected (k=%d, n=%d, classes=%s)",
        splitter_name,
        cv_folds,
        len(y),
        dict(zip(classes.tolist(), counts.tolist())),
    )

    used_params = dict(DEFAULT_LGB_PARAMS)
    if params:
        used_params.update(params)
    used_params["seed"] = seed
    used_params["bagging_seed"] = seed
    used_params["feature_fraction_seed"] = seed
    used_params["data_random_seed"] = seed

    # --- Cross-validation ---------------------------------------------------
    oof_preds = np.full(len(y), np.nan, dtype=np.float64)
    fold_aucs: list[float] = []
    for fold_idx, (train_idx, val_idx) in enumerate(folds):
        booster = _train_one_booster(
            X.iloc[train_idx],
            y[train_idx],
            params=used_params,
            num_boost_round=num_boost_round,
        )
        preds = _predict_proba(booster, X.iloc[val_idx])
        oof_preds[val_idx] = preds
        fold_auc = _safe_auc(y[val_idx], preds)
        if fold_auc is not None:
            fold_aucs.append(fold_auc)
        _log.info(
            "train_ranker: fold %d/%d auc=%s n_train=%d n_val=%d",
            fold_idx + 1,
            len(folds),
            f"{fold_auc:.4f}" if fold_auc is not None else "undef",
            len(train_idx),
            len(val_idx),
        )

    # OOF metrics — if any val sample was unassigned (shouldn't happen
    # with KFold), fill with the prior probability so log_loss is
    # defined.
    if np.isnan(oof_preds).any():  # pragma: no cover - defensive
        prior = float(np.mean(y))
        oof_preds = np.where(np.isnan(oof_preds), prior, oof_preds)

    overall_auc = _safe_auc(y, oof_preds)
    if overall_auc is None:
        # Single-class y was already rejected above; this should be
        # unreachable. Fall back to 0.5 (random) so the manifest
        # still has a numeric value.
        overall_auc = 0.5
    overall_log_loss = _safe_log_loss(y, oof_preds)

    if fold_aucs:
        cv_auc_mean = float(np.mean(fold_aucs))
        cv_auc_std = float(np.std(fold_aucs, ddof=0))
    else:
        # Per-fold AUCs were undefined for every fold (e.g. tiny
        # imbalanced dataset). Fall back to the OOF AUC so the
        # manifest still has a meaningful numeric pair, but record
        # the fact that the per-fold series is empty.
        cv_auc_mean = float(overall_auc)
        cv_auc_std = 0.0

    # --- Final model on all data -------------------------------------------
    final_booster = _train_one_booster(
        X,
        y,
        params=used_params,
        num_boost_round=num_boost_round,
    )

    # --- Persist artifacts -------------------------------------------------
    model_path, manifest_path, version = _resolve_output_paths(out_path)
    final_booster.save_model(str(model_path))

    trained_at = (
        dt.datetime.now(tz=dt.timezone.utc).replace(microsecond=0).isoformat()
    )

    manifest = {
        "version": version,
        "trained_at": trained_at,
        "training_rows": int(len(X)),
        "features": list(FEATURE_COLS),
        "categorical_features": list(CATEGORICAL_FEATURES),
        "target": TARGET_COL_PRIMARY,
        "metrics": {
            "auc": float(overall_auc),
            "log_loss": float(overall_log_loss),
        },
        "cv": {
            "k": int(cv_folds),
            "splitter": splitter_name,
            "auc_mean": float(cv_auc_mean),
            "auc_std": float(cv_auc_std),
            "fold_aucs": [float(a) for a in fold_aucs],
            "seed": int(seed),
        },
        "lightgbm": {
            "params": {k: v for k, v in used_params.items() if not callable(v)},
            "num_boost_round": int(num_boost_round),
        },
        "source": {
            "features_path": str(features_path),
        },
    }

    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )

    return TrainResult(
        model_path=model_path,
        manifest_path=manifest_path,
        version=version,
        training_rows=int(len(X)),
        cv_k=int(cv_folds),
        cv_auc_mean=float(cv_auc_mean),
        cv_auc_std=float(cv_auc_std),
        metrics_auc=float(overall_auc),
        metrics_log_loss=float(overall_log_loss),
        fold_aucs=[float(a) for a in fold_aucs],
        trained_at=trained_at,
        features=list(FEATURE_COLS),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.training.train_ranker",
        description=(
            "Train a LightGBM ranker on the catalysts feature store "
            "(data/training/catalysts.parquet). Idempotently bumps the "
            "model version on every re-run."
        ),
    )
    parser.add_argument(
        "--features",
        type=Path,
        required=True,
        help="Path to the input parquet (catalysts feature store).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help=(
            "Output path hint (e.g. models/ranker_v1.lgb). The actual "
            "filename is rewritten as ranker_v{N}.lgb where N is the "
            "next free version in the output directory."
        ),
    )
    parser.add_argument(
        "--cv-folds",
        type=int,
        default=3,
        help="Number of CV folds (default: 3, must be >= 3).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for splitting + training (default: 42).",
    )
    parser.add_argument(
        "--num-boost-round",
        type=int,
        default=DEFAULT_NUM_BOOST_ROUND,
        help=f"LightGBM boosting rounds (default: {DEFAULT_NUM_BOOST_ROUND}).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = train(
            features_path=args.features,
            out_path=args.out,
            cv_folds=args.cv_folds,
            seed=args.seed,
            num_boost_round=args.num_boost_round,
        )
    except MissingFeatureStoreError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except InsufficientDataError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # pragma: no cover - defensive
        print(f"ERROR: training failed: {exc}", file=sys.stderr)
        return 3

    print(
        f"CV AUC: {result.cv_auc_mean:.4f} \u00b1 {result.cv_auc_std:.4f} "
        f"(k={result.cv_k}, fold_aucs={[round(a, 4) for a in result.fold_aucs]})"
    )
    print(
        f"OOF AUC: {result.metrics_auc:.4f}  "
        f"OOF log_loss: {result.metrics_log_loss:.4f}  "
        f"training_rows: {result.training_rows}"
    )
    print(f"Manifest written → {result.manifest_path}")
    print(f"Training complete → {result.model_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
