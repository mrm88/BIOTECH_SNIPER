"""LightGBM ranker loader (M5 supplementary signal layer).

This module is the runtime counterpart to
:mod:`biotech_sniper.training.train_ranker`. It loads a trained
booster + its manifest sidecar and exposes a thin wrapper
(:class:`RankerModel`) whose ``predict_proba`` API mirrors
scikit-learn's binary classifier convention so callers do not need
to know that the underlying model is a LightGBM ``Booster``.

Critical invariants (per the M5 mission contract):

* The ranker is **supplementary** — its output never replaces the
  LLM ensemble score that drives trade gating, sizing, or entries.
  Callers in :mod:`biotech_sniper.sectors.unified_scorer` are
  responsible for keeping the ``ensemble_score`` / ``pre_score``
  field invariant under the ``LIGHTGBM_RANKER_ENABLED`` toggle.
* ``lightgbm`` is **not imported** at module load. The import is
  deferred to :func:`load_ranker` (and from there to
  :class:`RankerModel.predict_proba`) so that disabling the feature
  flag keeps ``lightgbm`` absent from ``sys.modules`` (VAL-M5-030).
* Manifest mismatches (extra / missing / re-ordered features) raise
  :class:`RankerSchemaMismatch` with a structured diff. This guards
  against silently scoring against an out-of-date model artefact.

Public surface::

    from biotech_sniper.ranker import load_ranker, RankerSchemaMismatch

    model = load_ranker("models/ranker_v1.lgb")
    proba = model.predict_proba(df.head(1))   # shape (1, 2)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

from biotech_sniper.training.build_feature_store import (
    PRIOR_PHASE2_DATA_QUALITY_MAP,
    REQUIRED_FEATURE_COLS,
)

__all__ = [
    "CATEGORICAL_FEATURES",
    "EXPECTED_FEATURES",
    "RankerModel",
    "RankerSchemaMismatch",
    "load_ranker",
]


_log = logging.getLogger(__name__)


# Canonical 11-feature column list expected by the booster. Re-exported
# from build_feature_store so the ranker scorer and the trainer stay
# in lockstep — drift between them is exactly what
# :class:`RankerSchemaMismatch` is designed to surface.
EXPECTED_FEATURES: tuple[str, ...] = tuple(REQUIRED_FEATURE_COLS)

# Categorical columns LightGBM was trained against. Must match the
# trainer's ``CATEGORICAL_FEATURES`` constant.
CATEGORICAL_FEATURES: tuple[str, ...] = (
    "sector",
    "science_grade",
    "prior_phase2_data_quality",
    "indication_class",
)


class RankerSchemaMismatch(ValueError):
    """Raised when the loaded manifest does not match the expected schema.

    The exception message includes a structured diff of the
    expected vs. actual feature lists so operators can identify
    whether the model is out-of-date or whether the call site has
    drifted away from the canonical schema.
    """

    def __init__(
        self,
        message: str,
        *,
        expected: Sequence[str],
        actual: Sequence[str],
        extra: Sequence[str] = (),
        missing: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.expected: list[str] = list(expected)
        self.actual: list[str] = list(actual)
        self.extra: list[str] = list(extra)
        self.missing: list[str] = list(missing)


@dataclass
class _LoadedManifest:
    """Internal container for a parsed manifest sidecar."""

    path: Path
    data: dict
    features: tuple[str, ...]
    categorical_features: tuple[str, ...]


def _resolve_manifest_path(model_path: Path) -> Path:
    """Return the canonical manifest sidecar path for ``model_path``.

    ``models/ranker_v3.lgb`` → ``models/ranker_v3.manifest.json``.
    Raises :class:`FileNotFoundError` when the sidecar is missing so
    callers see a single, consistent error type rather than a
    JSONDecodeError downstream.
    """
    if model_path.suffix == ".lgb":
        sidecar = model_path.with_suffix(".manifest.json")
    else:
        # Fallback for tests / one-offs that pass a .json directly.
        sidecar = model_path
    if not sidecar.exists():
        raise FileNotFoundError(
            f"Ranker manifest sidecar not found at {sidecar}. "
            "Run `python -m biotech_sniper.training.train_ranker` "
            "to materialise the booster + manifest pair."
        )
    return sidecar


def _load_manifest(model_path: Path) -> _LoadedManifest:
    sidecar = _resolve_manifest_path(model_path)
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RankerSchemaMismatch(
            f"Manifest sidecar at {sidecar} is not valid JSON: {exc}",
            expected=list(EXPECTED_FEATURES),
            actual=[],
            missing=list(EXPECTED_FEATURES),
        ) from exc
    if not isinstance(data, dict):  # pragma: no cover - defensive
        raise RankerSchemaMismatch(
            f"Manifest sidecar at {sidecar} did not parse to a JSON object.",
            expected=list(EXPECTED_FEATURES),
            actual=[],
            missing=list(EXPECTED_FEATURES),
        )
    features = tuple(data.get("features", ()))
    cat_features = tuple(data.get("categorical_features", ()))
    return _LoadedManifest(
        path=sidecar,
        data=data,
        features=features,
        categorical_features=cat_features,
    )


def _validate_manifest(
    manifest: _LoadedManifest,
    *,
    expected_features: Sequence[str],
) -> None:
    """Compare ``manifest.features`` against ``expected_features``.

    Raises :class:`RankerSchemaMismatch` with a structured diff when
    the feature list differs (extra columns, missing columns, or
    re-ordered columns).
    """
    actual = list(manifest.features)
    expected = list(expected_features)
    if actual == expected:
        return

    actual_set = set(actual)
    expected_set = set(expected)
    extra = sorted(actual_set - expected_set)
    missing = sorted(expected_set - actual_set)

    if not extra and not missing:
        # Same set, different order — call this out explicitly so the
        # operator sees the order drift rather than a confusing empty
        # diff.
        raise RankerSchemaMismatch(
            "Ranker manifest features re-ordered relative to expected "
            f"schema (manifest={manifest.path}). expected={expected!r} "
            f"actual={actual!r}.",
            expected=expected,
            actual=actual,
        )

    parts = []
    if missing:
        parts.append(f"missing={missing}")
    if extra:
        parts.append(f"extra={extra}")
    diff_str = ", ".join(parts)
    raise RankerSchemaMismatch(
        f"Ranker manifest schema mismatch (manifest={manifest.path}): "
        f"{diff_str}. expected={expected!r} actual={actual!r}.",
        expected=expected,
        actual=actual,
        extra=extra,
        missing=missing,
    )


def _coerce_features(
    X: pd.DataFrame | np.ndarray | Iterable,
    *,
    feature_names: Sequence[str],
    categorical: Sequence[str],
) -> pd.DataFrame:
    """Return ``X`` re-shaped into the canonical feature DataFrame.

    Numeric columns are coerced to ``float64`` (NaN preserved — the
    booster handles NaN natively). Categorical columns are coerced to
    ``pandas.Categorical`` so the booster sees them as native
    categoricals rather than failing with a string→float cast.
    """
    if isinstance(X, pd.DataFrame):
        df = X.copy()
    else:
        arr = np.asarray(X)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        df = pd.DataFrame(arr, columns=list(feature_names))

    missing = [c for c in feature_names if c not in df.columns]
    if missing:
        raise RankerSchemaMismatch(
            "predict_proba: input is missing required feature columns "
            f"{missing}. expected={list(feature_names)!r} "
            f"actual_columns={list(df.columns)!r}",
            expected=list(feature_names),
            actual=list(df.columns),
            missing=list(missing),
        )

    df = df[list(feature_names)].copy()
    cat_set = set(categorical)
    for col in feature_names:
        if col in cat_set:
            if not isinstance(df[col].dtype, pd.CategoricalDtype):
                df[col] = df[col].astype("category")
        else:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    return df


class RankerModel:
    """Thin wrapper over a LightGBM ``Booster`` exposing ``predict_proba``.

    The wrapper's responsibilities:

    * Hold a reference to the loaded ``Booster`` and the manifest's
      feature list (so prediction-time inputs are normalised against
      the trained schema).
    * Coerce inputs into the canonical 11-feature DataFrame on every
      ``predict_proba`` call so callers can pass a 1-row DataFrame,
      a 2-D array, or even a ``dict`` of per-feature scalars.
    * Translate the booster's class-1 probability into the standard
      sklearn-shaped ``(N, 2)`` matrix where column 0 is P(class=0)
      and column 1 is P(class=1) and each row sums to 1.
    """

    def __init__(
        self,
        booster: Any,
        *,
        manifest: dict,
        features: Sequence[str],
        categorical_features: Sequence[str],
        manifest_path: Path,
        model_path: Path,
    ) -> None:
        self._booster = booster
        self.manifest = manifest
        self.features: tuple[str, ...] = tuple(features)
        self.categorical_features: tuple[str, ...] = tuple(categorical_features)
        self.manifest_path = manifest_path
        self.model_path = model_path

    @property
    def booster(self) -> Any:
        """Return the underlying LightGBM booster (for advanced callers)."""
        return self._booster

    def predict_proba(self, X: pd.DataFrame | np.ndarray | Iterable) -> np.ndarray:
        """Return ``(N, 2)`` class-probability matrix for ``X``.

        Column 0 holds ``P(class=0)`` (i.e. the unprofitable label
        probability), column 1 holds ``P(class=1)`` (profitable).
        Every row sums to 1 within float tolerance — this matches
        the sklearn binary-classifier convention so downstream code
        can treat the wrapper as a drop-in classifier.
        """
        df = _coerce_features(
            X,
            feature_names=self.features,
            categorical=self.categorical_features,
        )
        # ``raw_score=False`` returns the sigmoid of the booster's
        # raw output, i.e. the class-1 probability for a binary
        # objective. ``predict_disable_shape_check=True`` is NOT
        # used — we want a clear LightGBM error when the column
        # count is wrong (it surfaces as a proper failure rather
        # than silent NaNs).
        raw = self._booster.predict(df, raw_score=False)
        p1 = np.asarray(raw, dtype=np.float64).reshape(-1)
        # Clamp into [0, 1] so the row-sum invariant holds even
        # under rounding drift on extreme inputs.
        p1 = np.clip(p1, 0.0, 1.0)
        p0 = 1.0 - p1
        return np.column_stack((p0, p1))

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"RankerModel(model_path={self.model_path!r}, "
            f"version={self.manifest.get('version')!r}, "
            f"features={list(self.features)!r})"
        )


def load_ranker(
    path: str | Path,
    *,
    expected_features: Sequence[str] | None = None,
) -> RankerModel:
    """Load a LightGBM ranker booster + manifest from disk.

    Parameters
    ----------
    path:
        Path to the booster file (``.lgb``). The manifest sidecar
        is resolved by replacing the suffix with ``.manifest.json``.
    expected_features:
        Override the canonical schema (defaults to
        :data:`EXPECTED_FEATURES`). Tests / future ranker variants
        can pass a custom list when they know the manifest should
        differ from the build_feature_store schema.

    Returns
    -------
    RankerModel
        Wrapper exposing ``predict_proba(X)`` (and the underlying
        booster via ``.booster``).

    Raises
    ------
    FileNotFoundError
        When the booster or the manifest sidecar is missing.
    RankerSchemaMismatch
        When the manifest's feature list does not match the
        expected schema.
    """
    model_path = Path(path)
    if not model_path.exists():
        raise FileNotFoundError(
            f"Ranker booster file not found at {model_path}. "
            "Run `python -m biotech_sniper.training.train_ranker` "
            "to materialise the artefact."
        )

    manifest = _load_manifest(model_path)

    expected = (
        tuple(expected_features) if expected_features is not None else EXPECTED_FEATURES
    )
    _validate_manifest(manifest, expected_features=expected)

    # Defer the heavy lightgbm import until we are sure the manifest
    # is OK — that way a manifest-mismatch error never pulls
    # lightgbm into ``sys.modules`` unnecessarily.
    import lightgbm as lgb  # noqa: WPS433 - intentional local import

    try:
        booster = lgb.Booster(model_file=str(model_path))
    except Exception as exc:  # pragma: no cover - depends on lgb internals
        raise RuntimeError(
            f"Failed to load LightGBM booster from {model_path}: {exc}"
        ) from exc

    # Sanity-check feature count agreement between the booster's
    # internal feature_name list and the manifest. A mismatch here
    # almost always indicates a corrupted artefact (booster + manifest
    # written at different times) and is just as critical to surface
    # as the manifest-vs-expected diff.
    booster_features = list(booster.feature_name())
    if booster_features != list(manifest.features):
        raise RankerSchemaMismatch(
            f"Booster feature_name() list does not match manifest features "
            f"(model={model_path}, manifest={manifest.path}). "
            f"booster={booster_features!r} manifest={list(manifest.features)!r}",
            expected=list(manifest.features),
            actual=booster_features,
        )

    cat_features = (
        manifest.categorical_features
        if manifest.categorical_features
        else CATEGORICAL_FEATURES
    )

    return RankerModel(
        booster=booster,
        manifest=manifest.data,
        features=manifest.features,
        categorical_features=cat_features,
        manifest_path=manifest.path,
        model_path=model_path,
    )
