"""Backtest harness for the Biotech Sniper learning engine (M5).

The harness replays resolved plays and the CT.gov amendment timeline through
:func:`biotech_sniper.learning_engine.process_event` over a closed
``[--from, --to]`` window, computes window-wide Brier score, directional
accuracy, and per-play P&L, and writes a JSON report to
``reports/backtest_<from>_<to>.json``.

The harness is deterministic — no random numbers, no wall-clock-dependent
ordering — and intentionally narrow in side effects:

* It does **not** modify ``state/active_plays.json``,
  ``state/resolved_plays.json``, ``state/performance_ledger.json``, or
  ``state/discovery_state.json``. Those are read-only inputs.
* When ``--apply-calibration`` is in effect *and* at least one probability
  bucket has ``n >= 3`` resolved plays in the window, the existing
  ``state/calibration_params.json`` (if any) is copied to
  ``state/calibration_params_backup_<UTC-timestamp>.json`` *before* any
  mutation. The backup's SHA-256 matches the pre-run file's SHA-256
  (VAL-M5-015).
* When ``--no-apply-calibration`` is passed (the seed-window
  reproduction path used by VAL-M5-017) the harness does NOT touch
  ``calibration_params.json`` at all, even if buckets would otherwise
  qualify.

Usage::

    python -m biotech_sniper.backtest \
        --from 2025-10-01 --to 2026-04-25 \
        [--no-apply-calibration]

Source data resolution order for the inputs (resolved plays, CT.gov
amendment timeline, calibration params):

    1. ``state/<file>.json`` if present — the live runtime state.
    2. ``migrations/seed/<file>.json`` — the seed checkpoint that the
       repo ships with so a fresh clone with an empty ``state/`` can
       still reproduce the baseline metrics.

"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from biotech_sniper import learning_engine
from biotech_sniper.paths import BASE_DIR, REPORTS_DIR, STATE_DIR

__all__ = [
    "BacktestResult",
    "main",
    "run_backtest",
]


_log = logging.getLogger(__name__)


SEED_DIR_DEFAULT: Path = BASE_DIR / "migrations" / "seed"


# Minimum |delta| to actually mutate calibration params. Smaller residuals
# are recorded in the report (so operators can see them) but skipped to
# avoid noise-driven nudges.
CALIBRATION_DELTA_EPSILON: float = 0.005


@dataclass
class BacktestResult:
    """Structured summary of a backtest run."""

    window_from: dt.date
    window_to: dt.date
    metrics: dict[str, Any]
    plays: list[dict[str, Any]]
    buckets: dict[str, dict[str, Any]]
    events_processed: int
    calibration: dict[str, Any]
    report_path: Path


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------


def _parse_iso_date(raw: str) -> dt.date:
    """Parse ``YYYY-MM-DD`` into a :class:`datetime.date`.

    Raises :class:`argparse.ArgumentTypeError` so argparse surfaces a
    clean error for malformed inputs instead of a Python traceback.
    """

    try:
        return dt.date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid ISO date '{raw}'; expected YYYY-MM-DD"
        ) from exc


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.backtest",
        description="Replay resolved plays + CT.gov amendments through "
                    "learning_engine.process_event and emit Brier / "
                    "directional-accuracy / per-play P&L metrics.",
    )
    parser.add_argument(
        "--from",
        dest="frm",
        required=True,
        type=_parse_iso_date,
        help="Window start date (inclusive), YYYY-MM-DD.",
    )
    parser.add_argument(
        "--to",
        dest="to",
        required=True,
        type=_parse_iso_date,
        help="Window end date (inclusive), YYYY-MM-DD.",
    )
    apply_group = parser.add_mutually_exclusive_group()
    apply_group.add_argument(
        "--apply-calibration",
        dest="apply_calibration",
        action="store_true",
        default=True,
        help="Apply calibration deltas where n>=3 per bucket (default).",
    )
    apply_group.add_argument(
        "--no-apply-calibration",
        dest="apply_calibration",
        action="store_false",
        help="Skip calibration mutation entirely (used for baseline "
             "reproduction; VAL-M5-017).",
    )
    parser.add_argument(
        "--state-dir",
        dest="state_dir",
        type=Path,
        default=None,
        help="Override path to the runtime state directory (defaults to "
             "BASE_DIR/state).",
    )
    parser.add_argument(
        "--seed-dir",
        dest="seed_dir",
        type=Path,
        default=None,
        help="Override path to the migrations seed directory.",
    )
    parser.add_argument(
        "--reports-dir",
        dest="reports_dir",
        type=Path,
        default=None,
        help="Override path to the reports directory (defaults to "
             "BASE_DIR/reports).",
    )
    return parser


# ---------------------------------------------------------------------------
# Source loaders
# ---------------------------------------------------------------------------


def _load_json_with_fallback(
    primary: Path, fallback: Path, *, label: str
) -> Any:
    """Read JSON from ``primary`` then ``fallback``; return ``None`` if
    neither exists.

    ``label`` is included in any decode error so operators can pinpoint
    the offending file.
    """

    for path in (primary, fallback):
        if path is None or not path.exists():
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
    return None


def _load_resolved_plays(state_dir: Path, seed_dir: Path) -> list[dict[str, Any]]:
    """Return the list of resolved-play dicts, falling back to seed."""

    doc = _load_json_with_fallback(
        state_dir / "resolved_plays.json",
        seed_dir / "resolved_plays.json",
        label="resolved_plays.json",
    )
    if isinstance(doc, dict):
        plays = doc.get("resolved")
        if isinstance(plays, list):
            return [p for p in plays if isinstance(p, dict)]
    return []


def _load_amendments(state_dir: Path, seed_dir: Path) -> dict[str, Any]:
    """Return the CT.gov amendment timeline dict (NCT_ID → record)."""

    doc = _load_json_with_fallback(
        state_dir / "amendment_state.json",
        seed_dir / "amendment_state.json",
        label="amendment_state.json",
    )
    if isinstance(doc, dict):
        return doc
    return {}


# ---------------------------------------------------------------------------
# Window filtering
# ---------------------------------------------------------------------------


def _safe_date(value: Any) -> dt.date | None:
    """Best-effort ISO-date parse that returns ``None`` on failure."""

    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def _resolved_in_window(
    plays: list[dict[str, Any]], frm: dt.date, to: dt.date
) -> list[dict[str, Any]]:
    """Filter plays to those whose entry date sits inside ``[frm, to]``.

    Plays without a parseable ``entry_date`` fall back to
    ``resolved_date``. Plays without either are dropped (they cannot be
    deterministically attributed to a window). The output is sorted
    deterministically so downstream consumers (and the report JSON)
    have stable ordering.
    """

    in_window: list[tuple[str, dict[str, Any]]] = []
    for play in plays:
        date = _safe_date(play.get("entry_date")) or _safe_date(
            play.get("resolved_date")
        )
        if date is None:
            continue
        if frm <= date <= to:
            in_window.append((date.isoformat(), play))

    in_window.sort(
        key=lambda item: (
            item[0],
            str(item[1].get("ticker") or ""),
            str(item[1].get("resolved_date") or ""),
        )
    )
    return [play for _, play in in_window]


def _amendments_in_window(
    amendments: dict[str, Any], frm: dt.date, to: dt.date
) -> list[tuple[str, dict[str, Any]]]:
    """Return ``(nct_id, record)`` pairs whose ``last_update`` falls in
    the window. Sorted by ``(last_update, nct_id)`` for determinism.
    """

    selected: list[tuple[str, str, dict[str, Any]]] = []
    for nct, record in amendments.items():
        if not isinstance(record, dict):
            continue
        status = record.get("status") or {}
        last_update = _safe_date(status.get("last_update"))
        if last_update is None:
            continue
        if frm <= last_update <= to:
            selected.append((last_update.isoformat(), str(nct), record))

    selected.sort(key=lambda item: (item[0], item[1]))
    return [(nct, record) for _, nct, record in selected]


# ---------------------------------------------------------------------------
# Calibration backup
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    """Return the hex SHA-256 of ``path``'s bytes."""

    hasher = hashlib.sha256()
    hasher.update(path.read_bytes())
    return hasher.hexdigest()


def _backup_calibration(state_dir: Path, *, now: dt.datetime) -> dict[str, Any]:
    """Copy ``state_dir/calibration_params.json`` to a timestamped backup.

    Returns a dict describing the backup outcome:

    * ``backup_path`` — path to the new backup file or ``None`` when no
      pre-existing calibration file existed.
    * ``backup_sha256`` — SHA-256 of the backup file (matches
      ``pre_run_sha256`` by construction).
    * ``pre_run_sha256`` — SHA-256 of the pre-run calibration file.
    """

    calib_path = state_dir / "calibration_params.json"
    if not calib_path.exists():
        return {"backup_path": None, "backup_sha256": None, "pre_run_sha256": None}

    pre_run_sha = _sha256(calib_path)
    timestamp = now.strftime("%Y%m%dT%H%M%S")
    backup_path = state_dir / f"calibration_params_backup_{timestamp}.json"
    backup_path.write_bytes(calib_path.read_bytes())
    backup_sha = _sha256(backup_path)

    return {
        "backup_path": str(backup_path),
        "backup_sha256": backup_sha,
        "pre_run_sha256": pre_run_sha,
    }


# ---------------------------------------------------------------------------
# Metric aggregation
# ---------------------------------------------------------------------------


def _aggregate(
    processed_resolved: list[dict[str, Any]],
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, float]],
]:
    """Aggregate the processed-event records into report metrics.

    Returns ``(metrics, plays, buckets, bucket_stats)``. ``metrics``
    always contains ``brier``, ``directional_accuracy``, and ``n``; the
    values are ``None`` when no usable rows are present. ``bucket_stats``
    accumulates per-bucket sums of the predicted probability and the
    realised directional outcome so the caller can compute calibration
    deltas (mean_actual_outcome - mean_p_predicted) when the n>=3 gate
    fires.
    """

    plays: list[dict[str, Any]] = []
    brier_terms: list[float] = []
    correct = 0
    total_with_outcome = 0
    bucket_counts: dict[str, int] = {}
    bucket_stats: dict[str, dict[str, float]] = {}

    for rec in processed_resolved:
        ticker = rec.get("ticker")
        plays.append(
            {
                "ticker": ticker,
                "entry_date": rec.get("entry_date"),
                "resolved_date": rec.get("resolved_date"),
                "option_pnl_pct": rec.get("option_pnl_pct"),
                "directional_correct": rec.get("directional_correct"),
                "bucket": rec.get("bucket"),
                "entry_p_success": rec.get("entry_p_success"),
            }
        )

        bucket = rec.get("bucket")
        if bucket:
            bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
            stats = bucket_stats.setdefault(
                bucket,
                {"n": 0, "p_sum": 0.0, "outcome_sum": 0.0, "n_with_outcome": 0},
            )
            stats["n"] += 1
            p_pct = rec.get("entry_p_success")
            if p_pct is not None:
                try:
                    stats["p_sum"] += float(p_pct) / 100.0
                except (TypeError, ValueError):
                    pass
            outcome_val = rec.get("directional_correct")
            if outcome_val is not None:
                stats["n_with_outcome"] += 1
                stats["outcome_sum"] += 1.0 if outcome_val else 0.0

        contribution = rec.get("brier_contribution")
        if contribution is not None:
            brier_terms.append(float(contribution))

        outcome = rec.get("directional_correct")
        if outcome is not None:
            total_with_outcome += 1
            if outcome:
                correct += 1

    if brier_terms:
        # round to 6dp so tiny float drift doesn't show in the report;
        # the determinism contract (VAL-M5-013) is satisfied by the
        # sorted input ordering and the absence of any randomness.
        brier = round(sum(brier_terms) / len(brier_terms), 6)
    else:
        brier = None

    if total_with_outcome:
        directional_accuracy = round(correct / total_with_outcome, 6)
    else:
        directional_accuracy = None

    metrics = {
        "brier": brier,
        "directional_accuracy": directional_accuracy,
        "n": len(plays),
        "n_with_outcome": total_with_outcome,
    }

    # Materialise the standard four buckets so the report is shape-stable
    # even when one of them has zero plays (validators may grep for
    # ``"P50-65"`` regardless of population).
    buckets: dict[str, dict[str, Any]] = {}
    for lo, hi in learning_engine.P_BUCKETS:
        label = f"P{lo}-{hi}"
        buckets[label] = {
            "n": bucket_counts.get(label, 0),
            "applied": False,  # filled in by the caller after the n>=3 gate.
        }

    return metrics, plays, buckets, bucket_stats


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def run_backtest(
    *,
    frm: dt.date,
    to: dt.date,
    apply_calibration: bool = True,
    state_dir: Path | None = None,
    seed_dir: Path | None = None,
    reports_dir: Path | None = None,
    now: dt.datetime | None = None,
) -> BacktestResult:
    """Replay events through ``learning_engine.process_event`` and write
    a backtest report. Returns a :class:`BacktestResult`.

    ``now`` is exposed for tests; the production CLI uses
    :func:`datetime.datetime.now` with :data:`datetime.timezone.utc` so
    the calibration backup carries a stable UTC timestamp.
    """

    if to < frm:
        raise ValueError(
            f"--to ({to.isoformat()}) must be on or after --from "
            f"({frm.isoformat()})"
        )

    state_dir = (state_dir or STATE_DIR).resolve()
    seed_dir = (seed_dir or SEED_DIR_DEFAULT).resolve()
    reports_dir = (reports_dir or REPORTS_DIR).resolve()
    now = now or dt.datetime.now(dt.timezone.utc)

    print(
        f"Backtest window: {frm.isoformat()} \u2192 {to.isoformat()} "
        f"(apply_calibration={apply_calibration})"
    )

    resolved = _load_resolved_plays(state_dir, seed_dir)
    amendments = _load_amendments(state_dir, seed_dir)

    in_window_plays = _resolved_in_window(resolved, frm, to)
    in_window_amendments = _amendments_in_window(amendments, frm, to)

    # ── Replay through learning_engine.process_event ─────────────────────
    processed_resolved: list[dict[str, Any]] = []
    events_processed = 0
    for play in in_window_plays:
        rec = learning_engine.process_event(
            {"type": "resolved_play", "data": play}
        )
        processed_resolved.append(rec)
        events_processed += 1

    for nct, record in in_window_amendments:
        learning_engine.process_event(
            {"type": "ctgov_amendment", "nct_id": nct, "data": record}
        )
        events_processed += 1

    metrics, plays, buckets, bucket_stats = _aggregate(processed_resolved)

    # ── Apply n>=3 gate per bucket (VAL-M5-014) ──────────────────────────
    bucket_lines: list[str] = []
    any_qualifying_bucket = False
    for label, info in buckets.items():
        n = info["n"]
        applied = bool(apply_calibration and n >= 3)
        info["applied"] = applied
        if applied:
            any_qualifying_bucket = True
        bucket_lines.append(
            f"Bucket {label}: n={n}, applied={'true' if applied else 'false'}"
        )
        print(bucket_lines[-1])

    # ── Calibration backup + per-bucket delta application ────────────────
    # Backup is taken BEFORE any mutation so the pre-run SHA-256 invariant
    # (VAL-M5-015) holds even when delta application rewrites the file.
    calibration_summary: dict[str, Any] = {
        "applied": False,
        "any_qualifying_bucket": any_qualifying_bucket,
        "backup_path": None,
        "backup_sha256": None,
        "pre_run_sha256": None,
        "deltas": {},
    }
    if apply_calibration and any_qualifying_bucket:
        calibration_summary.update(_backup_calibration(state_dir, now=now))
        calibration_summary["applied"] = True

        # Compute and apply per-bucket calibration deltas. The delta is
        # the calibration residual: mean_actual_outcome - mean_p_predicted.
        # We only apply when |delta| >= CALIBRATION_DELTA_EPSILON to avoid
        # noise-driven nudges; the report records the raw delta either way
        # so operators can audit which buckets were below the floor.
        calib_path = state_dir / "calibration_params.json"
        deltas: dict[str, dict[str, Any]] = {}
        for label, info in buckets.items():
            if not info["applied"]:
                continue
            stats = bucket_stats.get(label)
            if not stats or stats["n_with_outcome"] == 0 or stats["n"] == 0:
                continue
            mean_p = stats["p_sum"] / stats["n"]
            mean_actual = stats["outcome_sum"] / stats["n_with_outcome"]
            delta = round(mean_actual - mean_p, 6)
            entry: dict[str, Any] = {
                "delta": delta,
                "mean_p_predicted": round(mean_p, 6),
                "mean_actual_outcome": round(mean_actual, 6),
                "n": stats["n"],
                "applied": False,
            }
            if abs(delta) >= CALIBRATION_DELTA_EPSILON:
                learning_engine.apply_calibration_delta(
                    label,
                    delta,
                    reason=(
                        f"backtest:{frm.isoformat()}_{to.isoformat()}"
                    ),
                    path=calib_path,
                )
                entry["applied"] = True
            deltas[label] = entry
        calibration_summary["deltas"] = deltas

    # ── Write report ─────────────────────────────────────────────────────
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / (
        f"backtest_{frm.isoformat()}_{to.isoformat()}.json"
    )
    report = {
        "window": {"from": frm.isoformat(), "to": to.isoformat()},
        "metrics": metrics,
        "plays": plays,
        "buckets": buckets,
        "events_processed": events_processed,
        "calibration": calibration_summary,
        "apply_calibration_flag": apply_calibration,
    }
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )

    brier_str = (
        f"{metrics['brier']:.4f}"
        if metrics["brier"] is not None
        else "n/a"
    )
    dir_str = (
        f"{metrics['directional_accuracy']:.4f}"
        if metrics["directional_accuracy"] is not None
        else "n/a"
    )
    print(
        f"Brier: {brier_str}  Dir-acc: {dir_str}  "
        f"plays_in_window={metrics['n']}  events_processed={events_processed}"
    )
    print(f"Report: {report_path}")

    return BacktestResult(
        window_from=frm,
        window_to=to,
        metrics=metrics,
        plays=plays,
        buckets=buckets,
        events_processed=events_processed,
        calibration=calibration_summary,
        report_path=report_path,
    )


def main(argv: Iterable[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""

    parser = _build_argparser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        run_backtest(
            frm=args.frm,
            to=args.to,
            apply_calibration=args.apply_calibration,
            state_dir=args.state_dir,
            seed_dir=args.seed_dir,
            reports_dir=args.reports_dir,
        )
    except ValueError as exc:
        parser.error(str(exc))
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - direct CLI entry
    sys.exit(main())
