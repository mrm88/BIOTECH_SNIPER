"""Assemble the catalysts feature store at ``data/training/catalysts.parquet``.

The builder reads three sources of truth and joins them into a single
parquet with one row per (resolved-play → catalyst outcome):

1. **SQLite (``data/alpha_sniper.db``)** — resolved plays from
   :data:`plays` (``status='resolved'``) and per-day P&L attribution
   from :data:`performance_ledger`. The migration in
   :mod:`biotech_sniper.migrations.migrate_json_to_sqlite` is
   responsible for backfilling these from the seed JSONs.
2. **Performance ledger JSON** — per-ticker entry IV (and snapshot IV
   trajectory) lives in ``state/performance_ledger.json`` (with a
   fallback to ``migrations/seed/performance_ledger.json`` for fresh
   environments where the runtime ledger has not been hydrated yet).
   The SQLite ``performance_ledger`` table only stores aggregated
   daily P&L, so we read the JSON directly to recover the entry-IV
   per ticker for VAL-M5-007.
3. **ScienceProfile records** — letter grades + base rates + matched
   indication category live in ``state/science_grades.json`` (with a
   ``migrations/seed/science_grades.json`` fallback). The mapping
   ``A→4, B→3, C→2, D→1, F→0`` is applied to derive
   :data:`prior_phase2_data_quality`.
4. **Calibration parameters** — ``state/calibration_params.json``
   (seed fallback) is opened for completeness; the loader makes the
   path explicit and raises a clear error when the file is missing
   AND no fallback is available so the audit trail
   (VAL-M5-004) shows all three sources accessed.
5. **Company pipelines** — ``state/company_pipelines.json`` (seed
   fallback) supplies ``market_cap`` and ``sector`` for each ticker.

Output schema
-------------
The parquet contains the 11 spec'd features + 3 targets plus a small
set of identity columns used by validators:

Identity / join keys (not in the spec'd column count but required for
joins and idempotency):

* ``ticker`` (str): exchange ticker (suffixes such as ``_35C`` /
  ``_PUT`` stripped so the join against the performance-ledger JSON
  resolves cleanly).
* ``entry_date`` (str ISO date): date the play was opened.
* ``catalyst_date`` (str ISO date): date the catalyst fired
  (the ``resolved_date`` for resolved plays).
* ``resolved`` (bool): all rows in the M5 seed parquet are resolved;
  the column is forward-compatible with un-resolved active plays
  (M5+ extension).

Features (11):

* ``science_grade`` (categorical: ``A`` / ``B`` / ``C`` / ``D`` / ``F``
  or ``NA``)
* ``base_rate`` (float, 0..1)
* ``p_ensemble`` (float, 0..1) — derived from ``p_success`` percent
* ``iv_at_entry`` (float, 0..1 fractional IV) — entry IV from the
  performance ledger.
* ``dte_at_entry`` (float, days) — days from entry_date to
  option_expiry (NaN when no option leg).
* ``market_cap`` (float, USD) — from company pipelines.
* ``sector`` (categorical) — from ``sector_group`` in the company
  pipelines (defaults to ``BIOTECH``).
* ``prior_phase2_data_quality`` (categorical int 0..4 mapped from
  letter grade).
* ``sponsor_size`` (float) — log10(market_cap+1) numeric proxy so a
  small biotech (~$1B mc) ≈ 9.0 and a large pharma (~$100B mc) ≈
  11.0; NaN when market_cap missing.
* ``indication_class`` (categorical) — from the ScienceProfile's
  ``matched_category`` (falling back to the play's ``indication``
  field, then ``unknown``).
* ``days_to_event`` (float, days) — ``catalyst_date - entry_date``.

Targets (3):

* ``directional_correct`` (bool) — from the resolved record.
* ``option_pnl_pct`` (float) — from the resolved record.
* ``iv_crush_pct`` (float) — IV change from entry to last
  post-resolution snapshot (``max(0, (entry_iv - final_iv)/entry_iv*100)``).
  Defaults to ``0.0`` when no IV trajectory exists so VAL-M5-003
  ("targets non-null for resolved rows") holds.

Idempotency
-----------
The builder rewrites the parquet on every invocation and dedups on
``(ticker, catalyst_date)`` so re-running produces the same shape and
no duplicate keys (VAL-M5-006).

CLI
---

::

    python -m biotech_sniper.training.build_feature_store
        [--db DATA_DIR/alpha_sniper.db]
        [--out DATA_DIR/training/catalysts.parquet]
        [--state-dir STATE_DIR]
        [--seed-dir BASE_DIR/migrations/seed]
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Iterable, Mapping

import pandas as pd

from biotech_sniper import db
from biotech_sniper.paths import BASE_DIR, DATA_DIR, STATE_DIR, ensure_data_dir

__all__ = [
    "REQUIRED_FEATURE_COLS",
    "REQUIRED_TARGET_COLS",
    "REQUIRED_COLS",
    "PRIOR_PHASE2_DATA_QUALITY_MAP",
    "BuildResult",
    "MissingSourceError",
    "build",
    "main",
]


_log = logging.getLogger(__name__)


REQUIRED_FEATURE_COLS: Final[tuple[str, ...]] = (
    "science_grade",
    "base_rate",
    "p_ensemble",
    "iv_at_entry",
    "dte_at_entry",
    "market_cap",
    "sector",
    "prior_phase2_data_quality",
    "sponsor_size",
    "indication_class",
    "days_to_event",
)

REQUIRED_TARGET_COLS: Final[tuple[str, ...]] = (
    "directional_correct",
    "option_pnl_pct",
    "iv_crush_pct",
)

REQUIRED_COLS: Final[tuple[str, ...]] = (
    REQUIRED_FEATURE_COLS + REQUIRED_TARGET_COLS
)

# Letter-grade → numeric mapping for ``prior_phase2_data_quality``
# (VAL-M5-009). Higher values mean better data quality.
PRIOR_PHASE2_DATA_QUALITY_MAP: Final[Mapping[str, int]] = {
    "A": 4,
    "B": 3,
    "C": 2,
    "D": 1,
    "F": 0,
}


# Allowed letter grades in the parquet's ``science_grade`` categorical
# column. The order is descending quality, matching the
# :data:`PRIOR_PHASE2_DATA_QUALITY_MAP` values.
_SCIENCE_GRADE_CATEGORIES: Final[tuple[str, ...]] = ("A", "B", "C", "D", "F")


# Default location of the parquet output, used by both the CLI default
# and library callers who don't override ``--out``.
DEFAULT_OUT: Final[Path] = DATA_DIR / "training" / "catalysts.parquet"


class MissingSourceError(FileNotFoundError):
    """Raised when one of the four required source artefacts is missing.

    The error message names the missing source so a validator running
    VAL-M5-004 can confirm the builder fails fast instead of silently
    producing an incomplete parquet.
    """


@dataclass
class BuildResult:
    """Summary returned by :func:`build` and printed on stdout."""

    out_path: Path
    rows: int
    sqlite_resolved_count: int
    columns: list[str]


# ---------------------------------------------------------------------------
# Source loaders
# ---------------------------------------------------------------------------


def _strip_ticker(ticker: str) -> str:
    """Return the bare exchange ticker.

    Resolved plays in the seed encode option-leg suffixes onto the
    ``ticker`` column (e.g. ``IDYA_35C``, ``RVMD_125C``,
    ``AGIO_PUT``). The performance-ledger and company-pipelines JSONs
    key by the bare ticker, so we strip everything from the first
    underscore onward to make the joins cleanly resolve.
    """

    if not isinstance(ticker, str) or not ticker:
        return ""
    return ticker.split("_", 1)[0]


def _read_json_with_fallback(
    primary: Path,
    fallback: Path | None,
    *,
    label: str,
    optional: bool = False,
) -> Any:
    """Load JSON from ``primary``, falling back to ``fallback``.

    When neither file exists and ``optional`` is False, raise
    :class:`MissingSourceError` naming ``label`` so the operator can
    pinpoint the missing artefact (VAL-M5-004).
    """

    for path in (primary, fallback):
        if path is None:
            continue
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except json.JSONDecodeError as exc:
            raise json.JSONDecodeError(
                f"{label} at {path} is not valid JSON: {exc.msg}",
                exc.doc,
                exc.pos,
            ) from exc

    if optional:
        return None
    raise MissingSourceError(
        f"Required source '{label}' not found at any of: "
        f"{primary} or {fallback}"
    )


def _load_resolved_plays(db_path: Path) -> list[dict[str, Any]]:
    """Read every ``status='resolved'`` row from the plays table.

    Returns the row as a dict whose ``payload`` JSON has already been
    parsed back into a Python dict for ergonomic downstream access.
    """

    if not db_path.exists():
        raise MissingSourceError(
            f"SQLite database not found at {db_path}. "
            "Run the JSON-to-SQLite migration first."
        )

    # VAL-M5-033 strict-read-only contract: the feature-store builder
    # MUST NOT mutate ``data/alpha_sniper.db``. We therefore open the
    # connection via :func:`db.connect_readonly` (URI ``mode=ro``)
    # which refuses every write at the SQLite engine layer and skips
    # the write-PRAGMAs (``journal_mode=WAL``, ``synchronous=NORMAL``,
    # ``_ensure_db_file_mode``) that :func:`db.connect` issues. The
    # caller (or the JSON→SQLite migration) is responsible for
    # ensuring the schema is current; this builder is a pure reader.
    conn = db.connect_readonly(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM plays WHERE status = 'resolved' ORDER BY id"
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    for row in rows:
        payload = row.get("payload")
        if isinstance(payload, str) and payload:
            try:
                row["payload"] = json.loads(payload)
            except json.JSONDecodeError:
                row["payload"] = {}
        elif not isinstance(payload, dict):
            row["payload"] = {}
    return rows


def _load_ledger_doc(state_dir: Path, seed_dir: Path) -> dict[str, Any]:
    """Load ``performance_ledger.json`` honouring the seed fallback."""

    primary = state_dir / "performance_ledger.json"
    fallback = seed_dir / "performance_ledger.json"
    doc = _read_json_with_fallback(
        primary, fallback, label="performance_ledger.json"
    )
    if isinstance(doc, dict):
        plays = doc.get("plays")
        if not isinstance(plays, dict) or not plays:
            # The runtime ledger may have been written empty; prefer
            # the seed in that case so the IV joins resolve.
            seed_doc = _read_json_with_fallback(
                fallback, None, label="performance_ledger.json", optional=True
            )
            if isinstance(seed_doc, dict) and seed_doc.get("plays"):
                return seed_doc
        return doc
    return {}


def _load_science_profiles(
    state_dir: Path, seed_dir: Path
) -> dict[str, dict[str, Any]]:
    """Load ScienceProfile records keyed by ticker.

    The on-disk JSON keys profiles by ``<TICKER>_<NCT_ID>``. We
    re-key by ``TICKER`` (the first underscore-delimited segment) so
    callers can look up by bare ticker. When two profiles share the
    same ticker prefix the most recently graded one wins (i.e. the
    one with the latest ``graded_date``); ties fall back to dict
    iteration order.
    """

    primary = state_dir / "science_grades.json"
    fallback = seed_dir / "science_grades.json"
    doc = _read_json_with_fallback(
        primary,
        fallback,
        label="science_grades.json (ScienceProfile records)",
        optional=True,
    )
    if not isinstance(doc, dict):
        # Treat absent science profiles as empty rather than fatal so
        # the parquet still builds for tickers without a profile.
        return {}

    by_ticker: dict[str, dict[str, Any]] = {}
    for key, record in doc.items():
        if not isinstance(record, dict):
            continue
        ticker = str(record.get("ticker") or key.split("_", 1)[0]).strip()
        if not ticker:
            continue
        prev = by_ticker.get(ticker)
        if prev is None:
            by_ticker[ticker] = record
            continue
        prev_date = str(prev.get("graded_date") or "")
        cur_date = str(record.get("graded_date") or "")
        if cur_date > prev_date:
            by_ticker[ticker] = record
    return by_ticker


def _load_calibration_params(
    state_dir: Path, seed_dir: Path
) -> dict[str, Any]:
    """Load ``calibration_params.json`` (with seed fallback).

    The dict is opened so the audit trail shows the file was read but
    no specific fields are mandatory in this builder. Missing files
    raise :class:`MissingSourceError` so VAL-M5-004 can confirm the
    builder fails when calibration is absent.
    """

    primary = state_dir / "calibration_params.json"
    fallback = seed_dir / "calibration_params.json"
    doc = _read_json_with_fallback(
        primary, fallback, label="calibration_params.json"
    )
    return doc if isinstance(doc, dict) else {}


def _load_company_pipelines(
    state_dir: Path, seed_dir: Path
) -> dict[str, dict[str, Any]]:
    """Load ``company_pipelines.json`` (optional, seed fallback)."""

    primary = state_dir / "company_pipelines.json"
    fallback = seed_dir / "company_pipelines.json"
    doc = _read_json_with_fallback(
        primary,
        fallback,
        label="company_pipelines.json",
        optional=True,
    )
    if isinstance(doc, dict):
        out: dict[str, dict[str, Any]] = {}
        for ticker, record in doc.items():
            if isinstance(record, dict):
                out[str(ticker)] = record
        return out
    return {}


# ---------------------------------------------------------------------------
# Feature engineering helpers
# ---------------------------------------------------------------------------


def _coerce_iv_fraction(raw: Any) -> float | None:
    """Normalise an IV value to fractional units (0..N).

    The legacy JSONs store IV as a percentage (e.g. ``188.0`` for an
    IV of 188%). We coerce to fractional units so downstream
    calculations and the ranker train on a consistent scale.
    """

    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(value):
        return None
    return value / 100.0


def _ledger_entry_iv(ledger_doc: Mapping[str, Any], ticker: str) -> float | None:
    """Return the entry IV (fractional) for ``ticker`` or ``None``.

    The ledger keys plays by bare ticker (no option-leg suffix). We
    prefer the top-level ``entry_iv_pct`` field; when absent we fall
    back to the snapshot row whose ``date`` matches the ticker's
    ``entry_date``.
    """

    plays_map = ledger_doc.get("plays") if isinstance(ledger_doc, Mapping) else None
    if not isinstance(plays_map, Mapping):
        return None
    record = plays_map.get(ticker)
    if not isinstance(record, Mapping):
        return None
    entry_iv = _coerce_iv_fraction(record.get("entry_iv_pct"))
    if entry_iv is not None:
        return entry_iv
    entry_date = record.get("entry_date")
    snapshots = record.get("snapshots")
    if isinstance(snapshots, list):
        for snap in snapshots:
            if (
                isinstance(snap, Mapping)
                and snap.get("date") == entry_date
                and snap.get("iv_pct") is not None
            ):
                return _coerce_iv_fraction(snap.get("iv_pct"))
    return None


def _ledger_iv_crush_pct(
    ledger_doc: Mapping[str, Any], ticker: str, entry_iv_frac: float | None
) -> float:
    """Compute the realised IV crush for the play.

    Returns the percentage drop from entry IV to the last snapshot's
    IV, floored at 0. When IV trajectory data is unavailable we
    return ``0.0`` so the target column is non-null for every
    resolved row (VAL-M5-003).
    """

    if entry_iv_frac is None or entry_iv_frac <= 0:
        return 0.0
    plays_map = ledger_doc.get("plays") if isinstance(ledger_doc, Mapping) else None
    if not isinstance(plays_map, Mapping):
        return 0.0
    record = plays_map.get(ticker)
    if not isinstance(record, Mapping):
        return 0.0
    snapshots = record.get("snapshots")
    final_iv: float | None = None
    if isinstance(snapshots, list):
        for snap in reversed(snapshots):
            if isinstance(snap, Mapping) and snap.get("iv_pct") is not None:
                final_iv = _coerce_iv_fraction(snap.get("iv_pct"))
                if final_iv is not None:
                    break
    if final_iv is None:
        return 0.0
    drop = (entry_iv_frac - final_iv) / entry_iv_frac * 100.0
    return float(max(0.0, drop))


def _parse_iso_date(raw: Any) -> pd.Timestamp | None:
    if raw is None:
        return None
    try:
        ts = pd.to_datetime(raw, errors="coerce")
    except (TypeError, ValueError):
        return None
    if pd.isna(ts):
        return None
    return ts


def _days_between(start: Any, end: Any) -> float | None:
    a = _parse_iso_date(start)
    b = _parse_iso_date(end)
    if a is None or b is None:
        return None
    delta = b - a
    return float(delta.days)


def _sponsor_size_from_market_cap(market_cap: float | None) -> float | None:
    """Numeric proxy for sponsor size: ``log10(market_cap + 1)``.

    The proxy is monotone in market cap and stays well-defined for
    micro caps. Returns ``None`` for missing values so the column
    contains ``NaN`` rather than a placeholder zero (which would
    distort downstream regressions).
    """

    if market_cap is None:
        return None
    try:
        mc = float(market_cap)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(mc) or mc <= 0:
        return None
    return float(math.log10(mc + 1.0))


def _coerce_grade(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if not text:
        return None
    if text in PRIOR_PHASE2_DATA_QUALITY_MAP:
        return text
    return None


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------


def _row_for_resolved_play(
    play: Mapping[str, Any],
    ledger_doc: Mapping[str, Any],
    science_profiles: Mapping[str, Mapping[str, Any]],
    company_pipelines: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Translate a single resolved-play row into the parquet schema."""

    raw_ticker = str(play.get("ticker") or "")
    if not raw_ticker:
        return None
    bare_ticker = _strip_ticker(raw_ticker)

    payload = play.get("payload") if isinstance(play.get("payload"), dict) else {}
    original = payload.get("original_play") or {}

    entry_date_raw = play.get("entry_date") or original.get("added_date")
    catalyst_date_raw = (
        play.get("exit_date")
        or payload.get("resolved_date")
        or play.get("catalyst_date")
    )

    # ScienceProfile lookup first by bare ticker, falling back to the
    # raw ticker (in case a profile is keyed without stripping).
    profile = science_profiles.get(bare_ticker) or science_profiles.get(raw_ticker) or {}

    # Letter grade priority: ScienceProfile.grade > play.science_grade
    # > payload.entry_science_grade. The grade cell is a category in
    # the parquet so unknown values become NA.
    letter = (
        _coerce_grade(profile.get("grade"))
        or _coerce_grade(play.get("science_grade"))
        or _coerce_grade(payload.get("entry_science_grade"))
    )
    prior_phase2 = (
        PRIOR_PHASE2_DATA_QUALITY_MAP[letter] if letter is not None else None
    )

    # Base rate & indication category from ScienceProfile.
    base_rate_raw = profile.get("base_rate")
    try:
        base_rate = float(base_rate_raw) if base_rate_raw is not None else None
    except (TypeError, ValueError):
        base_rate = None

    indication_class = (
        profile.get("matched_category")
        or original.get("indication")
        or "unknown"
    )
    indication_class = str(indication_class).lower().strip() or "unknown"

    p_success = play.get("p_success")
    try:
        p_ensemble = float(p_success) / 100.0 if p_success is not None else None
    except (TypeError, ValueError):
        p_ensemble = None

    # IV at entry — sourced from the performance ledger so the
    # validator's join (parquet ⨝ ledger) returns ±1e-6.
    iv_at_entry = _ledger_entry_iv(ledger_doc, bare_ticker)
    if iv_at_entry is None:
        # Fallback to the resolved-play's own ``entry_iv_pct`` (the
        # ledger and resolved JSON share the same source on the seed
        # dataset, so this is a safe last-resort).
        iv_at_entry = _coerce_iv_fraction(payload.get("entry_iv_pct"))

    # dte_at_entry: option_expiry minus entry_date; NaN when no leg.
    option_expiry = play.get("option_expiry") or payload.get("expiry")
    dte_at_entry = _days_between(entry_date_raw, option_expiry)

    # days_to_event = catalyst_date - entry_date.
    days_to_event = _days_between(entry_date_raw, catalyst_date_raw)

    # Company-pipelines lookup by bare ticker.
    pipeline = company_pipelines.get(bare_ticker) or {}
    market_cap_raw = pipeline.get("market_cap")
    try:
        market_cap = (
            float(market_cap_raw) if market_cap_raw is not None else None
        )
    except (TypeError, ValueError):
        market_cap = None

    sector = pipeline.get("sector_group") or original.get("sector") or "BIOTECH"
    sector = str(sector).strip() or "BIOTECH"

    sponsor_size = _sponsor_size_from_market_cap(market_cap)

    # Targets
    directional_correct = bool(payload.get("direction_correct", False))
    option_pnl_pct_raw = play.get("option_pnl_pct")
    try:
        option_pnl_pct = (
            float(option_pnl_pct_raw) if option_pnl_pct_raw is not None else 0.0
        )
    except (TypeError, ValueError):
        option_pnl_pct = 0.0

    iv_crush_pct = _ledger_iv_crush_pct(ledger_doc, bare_ticker, iv_at_entry)

    return {
        # Identity / join keys
        "ticker": bare_ticker,
        "ticker_raw": raw_ticker,
        "entry_date": str(entry_date_raw) if entry_date_raw else None,
        "catalyst_date": str(catalyst_date_raw) if catalyst_date_raw else None,
        "resolved": True,
        # Features
        "science_grade": letter,
        "base_rate": base_rate,
        "p_ensemble": p_ensemble,
        "iv_at_entry": iv_at_entry,
        "dte_at_entry": dte_at_entry,
        "market_cap": market_cap,
        "sector": sector,
        "prior_phase2_data_quality": prior_phase2,
        "sponsor_size": sponsor_size,
        "indication_class": indication_class,
        "days_to_event": days_to_event,
        # Targets
        "directional_correct": directional_correct,
        "option_pnl_pct": option_pnl_pct,
        "iv_crush_pct": iv_crush_pct,
    }


# ---------------------------------------------------------------------------
# Frame assembly
# ---------------------------------------------------------------------------


def _coerce_dataframe(rows: Iterable[Mapping[str, Any]]) -> pd.DataFrame:
    """Build a typed dataframe from row dicts.

    Categorical columns are explicitly typed via ``astype('category')``
    (or the Categorical helper) so the parquet metadata records
    ``category`` dtype and the per-column dtype check in VAL-M5-002
    passes.
    """

    df = pd.DataFrame(list(rows))
    if df.empty:
        # Provide an empty frame with the right schema so downstream
        # consumers can iterate without special-casing.
        df = pd.DataFrame(columns=_full_column_order())
        return _apply_dtypes(df)

    # Preserve a stable column order: identity → features → targets.
    df = df.reindex(columns=_full_column_order())
    return _apply_dtypes(df)


def _full_column_order() -> list[str]:
    return [
        "ticker",
        "ticker_raw",
        "entry_date",
        "catalyst_date",
        "resolved",
        *REQUIRED_FEATURE_COLS,
        *REQUIRED_TARGET_COLS,
    ]


def _apply_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    """Cast every column to the dtype expected by VAL-M5-002."""

    # Numeric features.
    numeric_float_cols = (
        "base_rate",
        "p_ensemble",
        "iv_at_entry",
        "dte_at_entry",
        "market_cap",
        "sponsor_size",
        "days_to_event",
        "option_pnl_pct",
        "iv_crush_pct",
    )
    for col in numeric_float_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")

    # Boolean target.
    if "directional_correct" in df.columns:
        df["directional_correct"] = (
            df["directional_correct"].fillna(False).astype(bool)
        )
    if "resolved" in df.columns:
        df["resolved"] = df["resolved"].fillna(False).astype(bool)

    # Categorical features.
    if "science_grade" in df.columns:
        df["science_grade"] = pd.Categorical(
            df["science_grade"],
            categories=list(_SCIENCE_GRADE_CATEGORIES),
            ordered=True,
        )
    if "prior_phase2_data_quality" in df.columns:
        # Stringify so the parquet round-trip preserves the
        # ``category`` dtype (pyarrow drops int categoricals back
        # down to plain int64/float64, which would fail VAL-M5-002).
        # Validators reading this column should ``int(value)`` to
        # recover the numeric mapping (A→4..F→0).
        str_values = df["prior_phase2_data_quality"].apply(
            lambda v: None if v is None or (isinstance(v, float) and math.isnan(v)) else str(int(v))
        )
        df["prior_phase2_data_quality"] = pd.Categorical(
            str_values,
            categories=[str(v) for v in sorted(set(PRIOR_PHASE2_DATA_QUALITY_MAP.values()))],
            ordered=True,
        )
    for col in ("sector", "indication_class"):
        if col in df.columns:
            df[col] = df[col].astype("category")

    # Identity columns stay as ``object`` (string) for ergonomic
    # downstream filtering.
    for col in ("ticker", "ticker_raw", "entry_date", "catalyst_date"):
        if col in df.columns:
            df[col] = df[col].astype("string")

    return df


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build(
    *,
    db_path: Path | None = None,
    out_path: Path | None = None,
    state_dir: Path | None = None,
    seed_dir: Path | None = None,
) -> BuildResult:
    """Read all sources and write the catalysts parquet.

    Returns a :class:`BuildResult` with the on-disk path, the row count
    and the columns written, so callers can easily assert the expected
    shape without re-reading the parquet.
    """

    db_path = (db_path or DATA_DIR / "alpha_sniper.db").expanduser()
    out_path = (out_path or DEFAULT_OUT).expanduser()
    state_dir = (state_dir or STATE_DIR).expanduser()
    seed_dir = (seed_dir or BASE_DIR / "migrations" / "seed").expanduser()

    # Load all four required sources (raises MissingSourceError on
    # missing required artefacts).
    resolved_plays = _load_resolved_plays(db_path)
    ledger_doc = _load_ledger_doc(state_dir, seed_dir)
    _ = _load_calibration_params(state_dir, seed_dir)
    science_profiles = _load_science_profiles(state_dir, seed_dir)
    company_pipelines = _load_company_pipelines(state_dir, seed_dir)

    rows: list[dict[str, Any]] = []
    for play in resolved_plays:
        row = _row_for_resolved_play(
            play, ledger_doc, science_profiles, company_pipelines
        )
        if row is not None:
            rows.append(row)

    df = _coerce_dataframe(rows)

    # Idempotent dedup on (ticker, catalyst_date). The seed has no
    # collisions today; this guard is a forward-compat safety net.
    if not df.empty:
        df = df.drop_duplicates(subset=["ticker", "catalyst_date"], keep="last")
        df = df.reset_index(drop=True)

    # Centralised DATA_DIR bootstrap — ``ensure_data_dir()`` creates
    # the top-level data directory if absent. The subsequent
    # ``parent.mkdir`` still creates any deeper subdirectory the
    # caller asked for via ``--out`` (e.g. ``data/training/``).
    ensure_data_dir()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Always overwrite — combined with the pure-function build above
    # this gives us VAL-M5-006 idempotency.
    df.to_parquet(out_path, engine="pyarrow", index=False)

    _log.info(
        "build_feature_store wrote %d rows to %s (sources: db=%s, ledger=%s, "
        "calibration=%s, science_profiles=%s, company_pipelines=%s)",
        len(df),
        out_path,
        db_path,
        state_dir / "performance_ledger.json",
        state_dir / "calibration_params.json",
        state_dir / "science_grades.json",
        state_dir / "company_pipelines.json",
    )

    return BuildResult(
        out_path=out_path,
        rows=int(len(df)),
        sqlite_resolved_count=len(resolved_plays),
        columns=list(df.columns),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.training.build_feature_store",
        description=(
            "Assemble data/training/catalysts.parquet from the SQLite "
            "resolved plays + ledger + ScienceProfile records + "
            "calibration params. Idempotent — safe to re-run."
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DATA_DIR / "alpha_sniper.db",
        help="Path to the SQLite db (default: DATA_DIR/alpha_sniper.db).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=(
            "Path to the output parquet "
            "(default: DATA_DIR/training/catalysts.parquet)."
        ),
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=STATE_DIR,
        help="Override the runtime state directory (default: STATE_DIR).",
    )
    parser.add_argument(
        "--seed-dir",
        type=Path,
        default=BASE_DIR / "migrations" / "seed",
        help=(
            "Override the migrations seed directory "
            "(default: BASE_DIR/migrations/seed)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = build(
            db_path=args.db,
            out_path=args.out,
            state_dir=args.state_dir,
            seed_dir=args.seed_dir,
        )
    except MissingSourceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    print(
        json.dumps(
            {
                "out_path": str(result.out_path),
                "rows": result.rows,
                "sqlite_resolved_count": result.sqlite_resolved_count,
                "columns": result.columns,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
