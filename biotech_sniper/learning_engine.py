#!/usr/bin/env python3
"""
LEARNING ENGINE
Reads all resolved trades, computes accuracy metrics, and automatically
updates the scoring system's calibration parameters.

WHAT IT LEARNS:
  1. Directional accuracy by P bucket (50-65%, 65-75%, 75-85%, 85%+)
  2. Option P&L hit rate (are we actually making money?)
  3. IV crush frequency by catalyst type
  4. Science grade predictive value (does Grade F really predict failure?)
  5. Strike selection accuracy (are we too OTM? too ATM?)
  6. Brier score (probability calibration: is P=70% right ~70% of the time?)

HOW IT RECALIBRATES:
  - Updates state/calibration_params.json with new thresholds
  - Adjusts P(success) minimum threshold if hit rate below target
  - Adjusts OTM range by catalyst type based on actual stock moves
  - Writes human-readable calibration report to reports/calibration_report.md
  - All downstream code reads calibration_params.json — changes take effect next run

DESIGN PRINCIPLE:
  The engine only adjusts parameters when it has enough data (n >= 3 per category).
  With small samples it logs findings but doesn't change parameters.
  This prevents overfitting to 1-2 data points.
"""

import json
import datetime
import math
from pathlib import Path

from biotech_sniper.paths import BASE_DIR
RESOLVED_FILE = BASE_DIR / "state/resolved_plays.json"
CALIBRATION_PARAMS_FILE = BASE_DIR / "state/calibration_params.json"
CALIBRATION_REPORT_FILE = BASE_DIR / "reports/calibration_report.md"
LEDGER_FILE = BASE_DIR / "state/performance_ledger.json"


# ── DEFAULT CALIBRATION PARAMS ───────────────────────────────────────────────
# These are overridden by calibration_params.json when enough data exists.
DEFAULT_PARAMS = {
    "version": 1,
    "last_updated": None,
    "n_resolved": 0,

    # Probability thresholds
    "p_min_long": 65,           # minimum P for LONG CALLS (was 60, raised Apr 26)
    "p_max_short": 40,          # maximum P for LONG PUTS

    # OTM ranges by catalyst type (min%, max%)
    "otm_ranges": {
        "PDUFA":     [10, 25],
        "READOUT":   [5, 15],
        "LABEL_EXT": [0, 5],
        "ADCOM":     [10, 20],
        "CONTRACT":  [15, 30],
        "DEFAULT":   [10, 20],
    },

    # Expected stock moves by catalyst type (win%, loss%)
    "expected_moves": {
        "PDUFA":     [45, 30],
        "READOUT":   [20, 25],
        "LABEL_EXT": [10, 12],
        "ADCOM":     [25, 20],
        "CONTRACT":  [15, 10],
        "DEFAULT":   [25, 20],
    },

    # IV crush threshold
    "iv_crush_halfsize_threshold": 150,   # IV% above which we reduce size

    # Science grade multipliers
    "science_multipliers": {
        "A": 1.15, "B": 1.05, "C": 1.00, "D": 0.85, "F": 0.65
    },

    # Minimum number of resolved trades to adjust a parameter
    "min_n_for_adjustment": 3,

    # Performance targets
    "target_directional_accuracy": 0.70,   # 70% direction correct
    "target_option_hit_rate": 0.50,         # 50% options profitable
    "target_brier_score": 0.20,             # Lower is better (0 = perfect)

    # History of changes for auditability
    "change_log": [],
}


def load_resolved() -> list:
    if RESOLVED_FILE.exists():
        with open(RESOLVED_FILE) as f:
            d = json.load(f)
        return d.get("resolved", [])
    return []


def load_params() -> dict:
    if CALIBRATION_PARAMS_FILE.exists():
        with open(CALIBRATION_PARAMS_FILE) as f:
            return json.load(f)
    return DEFAULT_PARAMS.copy()


def save_params(params: dict):
    CALIBRATION_PARAMS_FILE.parent.mkdir(exist_ok=True)
    params["last_updated"] = datetime.date.today().isoformat()
    with open(CALIBRATION_PARAMS_FILE, "w") as f:
        json.dump(params, f, indent=2)


def compute_brier_score(resolved: list) -> float | None:
    """
    Brier score = mean((p_predicted - outcome)^2)
    outcome = 1 if direction correct, 0 if wrong.
    Perfect calibration = 0.0. Coin flip = 0.25.
    """
    pairs = []
    for r in resolved:
        p = r.get("entry_p_success")
        correct = r.get("direction_correct")
        if p is not None and correct is not None:
            pairs.append((p / 100, 1 if correct else 0))
    if not pairs:
        return None
    return round(sum((p - o) ** 2 for p, o in pairs) / len(pairs), 4)


def bucket_by_p(resolved: list, buckets: list) -> dict:
    """Group resolved plays into probability buckets."""
    result = {b: [] for b in buckets}
    for r in resolved:
        p = r.get("entry_p_success", 0) or 0
        for lo, hi in buckets:
            if lo <= p < hi:
                result[(lo, hi)].append(r)
                break
        else:
            result[buckets[-1]].append(r)
    return result


def analyze_iv_crush(resolved: list) -> dict:
    """How often does IV crush cause losses despite correct direction?"""
    iv_crush_cases = [r for r in resolved if r.get("iv_crush_suspected")]
    by_catalyst = {}
    for r in iv_crush_cases:
        ct = r.get("entry_catalyst_type", "UNKNOWN")
        by_catalyst.setdefault(ct, []).append(r)
    return {
        "total_iv_crush": len(iv_crush_cases),
        "by_catalyst_type": {k: len(v) for k, v in by_catalyst.items()},
        "avg_iv_at_entry": (
            sum(r.get("iv_entry", 0) or 0 for r in iv_crush_cases) / len(iv_crush_cases)
            if iv_crush_cases else None
        ),
    }


def analyze_stock_moves_by_catalyst(resolved: list) -> dict:
    """What are actual stock moves per catalyst type?"""
    by_type = {}
    for r in resolved:
        ct = r.get("entry_catalyst_type", "DEFAULT")
        if ct not in by_type:
            by_type[ct] = {"moves": [], "correct": []}
        move = r.get("stock_move_pct")
        correct = r.get("direction_correct")
        if move is not None:
            by_type[ct]["moves"].append(abs(move))
        if correct is not None:
            by_type[ct]["correct"].append(1 if correct else 0)

    result = {}
    for ct, data in by_type.items():
        moves = data["moves"]
        correct = data["correct"]
        result[ct] = {
            "n": len(moves),
            "avg_abs_move": round(sum(moves) / len(moves), 1) if moves else None,
            "max_move": round(max(moves), 1) if moves else None,
            "directional_accuracy": round(sum(correct) / len(correct), 2) if correct else None,
        }
    return result


def analyze_science_grade_accuracy(resolved: list) -> dict:
    """Does science grade predict direction accuracy?"""
    by_grade = {}
    for r in resolved:
        grade = r.get("entry_science_grade", "N/A")
        correct = r.get("direction_correct")
        if grade and correct is not None:
            by_grade.setdefault(grade, []).append(1 if correct else 0)

    return {
        grade: {
            "n": len(vals),
            "accuracy": round(sum(vals) / len(vals), 2) if vals else None,
        }
        for grade, vals in by_grade.items()
    }


def compute_adjusted_otm_ranges(resolved: list, current_params: dict) -> dict:
    """
    Based on actual stock moves, adjust OTM ranges per catalyst type.
    Target: strike should be within 1 standard deviation of the actual move.
    Only adjust if n >= 3 per category.
    """
    moves_by_type = analyze_stock_moves_by_catalyst(resolved)
    new_ranges = dict(current_params.get("otm_ranges", {}))
    changes = []
    min_n = current_params.get("min_n_for_adjustment", 3)

    for cat_type, stats in moves_by_type.items():
        n = stats.get("n", 0)
        avg_move = stats.get("avg_abs_move")
        if n < min_n or avg_move is None or cat_type not in new_ranges:
            continue

        # Current range
        curr_lo, curr_hi = new_ranges[cat_type]
        curr_mid = (curr_lo + curr_hi) / 2

        # Target: midpoint should be ~50-70% of average stock move
        # (to be OTM but within range of likely outcome)
        target_mid = avg_move * 0.60
        target_lo = max(0, round(target_mid * 0.60))
        target_hi = round(target_mid * 1.40)

        # Only change if significantly different (>5pp shift)
        if abs(target_mid - curr_mid) > 5:
            new_ranges[cat_type] = [target_lo, target_hi]
            changes.append(f"{cat_type}: [{curr_lo},{curr_hi}] -> [{target_lo},{target_hi}] (avg_move={avg_move}%, n={n})")

    return new_ranges, changes


def compute_p_threshold_adjustment(resolved: list, current_params: dict) -> tuple:
    """
    If LONG plays with P=65-70% are losing >60% of the time,
    raise the minimum P threshold.
    """
    min_n = current_params.get("min_n_for_adjustment", 3)
    current_min = current_params.get("p_min_long", 65)

    # Look at low-P longs (65-75%)
    low_p_longs = [
        r for r in resolved
        if r.get("entry_direction") == "LONG_CALLS"
        and 65 <= (r.get("entry_p_success") or 0) < 75
    ]

    if len(low_p_longs) < min_n:
        return current_min, None

    accuracy = sum(1 for r in low_p_longs if r.get("direction_correct")) / len(low_p_longs)
    option_hit_rate = sum(1 for r in low_p_longs if (r.get("option_pnl_pct") or 0) > 0) / len(low_p_longs)

    # If direction accuracy <60% or option hit rate <40% in the 65-75% bucket,
    # raise the minimum threshold
    if accuracy < 0.60 or option_hit_rate < 0.40:
        new_min = min(current_min + 5, 80)  # cap at 80%
        change = f"p_min_long: {current_min}% -> {new_min}% (65-75% bucket: dir_acc={accuracy:.0%}, opt_hit={option_hit_rate:.0%}, n={len(low_p_longs)})"
        return new_min, change
    elif accuracy > 0.80 and option_hit_rate > 0.65 and current_min > 60:
        # If low-P plays are actually working well, we could lower the threshold
        # but be conservative — only lower if very clear signal
        return current_min, None

    return current_min, None


def run_learning_cycle() -> dict:
    """
    Main entry point. Reads all resolved plays, updates calibration params,
    writes the calibration report.
    """
    today = datetime.date.today().isoformat()
    resolved = load_resolved()
    params = load_params()

    print(f"\n[learning_engine] Running — {today}")
    print(f"  Resolved plays: {len(resolved)}")

    if not resolved:
        return {"summary": "No resolved plays yet — learning will begin after first resolution", "n": 0}

    changes_made = []

    # ── CORE METRICS ─────────────────────────────────────────────────────────
    n = len(resolved)
    direction_correct = [r for r in resolved if r.get("direction_correct")]
    option_winners = [r for r in resolved if (r.get("option_pnl_pct") or 0) > 0]

    dir_accuracy = len(direction_correct) / n
    opt_hit_rate = len(option_winners) / n
    brier = compute_brier_score(resolved)

    print(f"  Directional accuracy: {dir_accuracy:.0%} ({len(direction_correct)}/{n})")
    print(f"  Option hit rate:      {opt_hit_rate:.0%} ({len(option_winners)}/{n})")
    if brier is not None:
        print(f"  Brier score:          {brier:.3f} (target <{params.get('target_brier_score', 0.20):.2f})")

    # ── P BUCKET ANALYSIS ────────────────────────────────────────────────────
    buckets = [(50, 65), (65, 75), (75, 85), (85, 101)]
    bucketed = bucket_by_p(resolved, buckets)
    bucket_stats = {}
    for (lo, hi), plays in bucketed.items():
        if plays:
            acc = sum(1 for p in plays if p.get("direction_correct")) / len(plays)
            opt_rate = sum(1 for p in plays if (p.get("option_pnl_pct") or 0) > 0) / len(plays)
            bucket_stats[f"P{lo}-{hi}"] = {
                "n": len(plays), "dir_accuracy": round(acc, 2), "opt_hit_rate": round(opt_rate, 2)
            }

    # ── IV CRUSH ANALYSIS ────────────────────────────────────────────────────
    iv_analysis = analyze_iv_crush(resolved)

    # If >50% of PDUFA plays are IV-crushed, lower the IV threshold
    if iv_analysis["by_catalyst_type"].get("PDUFA", 0) >= 2:
        n_pdufa = len([r for r in resolved if r.get("entry_catalyst_type") == "PDUFA"])
        iv_crush_rate = iv_analysis["by_catalyst_type"]["PDUFA"] / n_pdufa if n_pdufa > 0 else 0
        if iv_crush_rate > 0.5:
            current_threshold = params.get("iv_crush_halfsize_threshold", 150)
            new_threshold = max(100, current_threshold - 25)
            if new_threshold != current_threshold:
                params["iv_crush_halfsize_threshold"] = new_threshold
                changes_made.append(f"iv_crush_halfsize_threshold: {current_threshold}% -> {new_threshold}% (PDUFA crush rate {iv_crush_rate:.0%})")

    # ── SCIENCE GRADE ACCURACY ───────────────────────────────────────────────
    grade_accuracy = analyze_science_grade_accuracy(resolved)

    # Adjust science multipliers if grades are strongly predictive
    min_n = params.get("min_n_for_adjustment", 3)
    for grade, stats in grade_accuracy.items():
        if stats["n"] < min_n:
            continue
        acc = stats["accuracy"]
        current_mult = params["science_multipliers"].get(grade, 1.0)

        # If Grade F is actually 0% accurate (direction), reinforce the 0.65x multiplier
        if grade == "F" and acc < 0.20 and current_mult > 0.60:
            params["science_multipliers"]["F"] = max(0.50, current_mult - 0.05)
            changes_made.append(f"science_mult[F]: {current_mult} -> {params['science_multipliers']['F']} (accuracy={acc:.0%}, n={stats['n']})")

        # If Grade A is consistently right, reinforce 1.15x
        if grade == "A" and acc > 0.85 and current_mult < 1.20:
            params["science_multipliers"]["A"] = min(1.25, current_mult + 0.05)
            changes_made.append(f"science_mult[A]: {current_mult} -> {params['science_multipliers']['A']} (accuracy={acc:.0%}, n={stats['n']})")

    # ── OTM RANGE ADJUSTMENT ────────────────────────────────────────────────
    new_otm_ranges, otm_changes = compute_adjusted_otm_ranges(resolved, params)
    if otm_changes:
        params["otm_ranges"] = new_otm_ranges
        changes_made.extend(otm_changes)

    # ── P THRESHOLD ADJUSTMENT ───────────────────────────────────────────────
    new_p_min, p_change = compute_p_threshold_adjustment(resolved, params)
    if p_change:
        params["p_min_long"] = new_p_min
        changes_made.append(p_change)

    # ── EXPECTED MOVE CALIBRATION ────────────────────────────────────────────
    actual_moves = analyze_stock_moves_by_catalyst(resolved)
    new_expected_moves = dict(params.get("expected_moves", {}))
    for ct, stats in actual_moves.items():
        if stats["n"] < min_n or stats.get("avg_abs_move") is None:
            continue
        avg = stats["avg_abs_move"]
        if ct in new_expected_moves:
            curr_win, curr_loss = new_expected_moves[ct]
            # Blend 70% old, 30% new data (conservative update)
            new_win = round(curr_win * 0.70 + avg * 0.30)
            if abs(new_win - curr_win) >= 5:
                new_expected_moves[ct] = [new_win, curr_loss]
                changes_made.append(f"expected_moves[{ct}] win: {curr_win}% -> {new_win}% (avg_actual={avg}%, n={stats['n']})")

    params["expected_moves"] = new_expected_moves

    # ── SAVE UPDATED PARAMS ──────────────────────────────────────────────────
    params["n_resolved"] = n
    if changes_made:
        params.setdefault("change_log", []).append({
            "date": today,
            "n_resolved": n,
            "changes": changes_made,
        })
        print(f"  Parameters updated: {len(changes_made)} changes")
        for c in changes_made:
            print(f"    - {c}")
    else:
        print(f"  No parameter changes needed")

    save_params(params)

    # ── WRITE CALIBRATION REPORT ─────────────────────────────────────────────
    report = _build_calibration_report(
        resolved, params, dir_accuracy, opt_hit_rate, brier,
        bucket_stats, grade_accuracy, iv_analysis, actual_moves, changes_made
    )
    CALIBRATION_REPORT_FILE.parent.mkdir(exist_ok=True)
    with open(CALIBRATION_REPORT_FILE, "w") as f:
        f.write(report)

    brier_str = f"{brier:.3f}" if brier is not None else "N/A"
    summary = (
        f"{n} resolved | dir_acc={dir_accuracy:.0%} | opt_hit={opt_hit_rate:.0%} | "
        f"brier={brier_str} | {len(changes_made)} param changes"
    )
    return {
        "summary": summary,
        "n": n,
        "dir_accuracy": dir_accuracy,
        "opt_hit_rate": opt_hit_rate,
        "brier_score": brier,
        "changes_made": changes_made,
        "bucket_stats": bucket_stats,
    }


def _build_calibration_report(resolved, params, dir_acc, opt_hit, brier,
                                bucket_stats, grade_acc, iv_analysis,
                                actual_moves, changes_made) -> str:
    today = datetime.date.today().strftime("%B %d, %Y")
    n = len(resolved)

    lines = [
        f"# Alpha Sniper — Calibration Report",
        f"Generated: {today} | {n} resolved trades",
        "",
        "## Overall Performance",
        f"- Directional accuracy: {dir_acc:.0%} ({sum(1 for r in resolved if r.get('direction_correct'))}/{n})",
        f"- Option hit rate: {opt_hit:.0%} ({sum(1 for r in resolved if (r.get('option_pnl_pct') or 0) > 0)}/{n})",
        f"- Brier score: {brier:.4f}" if brier else "- Brier score: N/A (need more data)",
        "",
        "## Performance by Probability Bucket",
        "| Bucket | N | Dir Acc | Opt Hit |",
        "|--------|---|---------|---------|",
    ]
    for bucket, stats in bucket_stats.items():
        lines.append(f"| {bucket} | {stats['n']} | {stats['dir_accuracy']:.0%} | {stats['opt_hit_rate']:.0%} |")

    lines += [
        "",
        "## Actual Stock Moves by Catalyst Type",
        "| Catalyst Type | N | Avg Move | Dir Acc |",
        "|---------------|---|----------|---------|",
    ]
    for ct, stats in actual_moves.items():
        move = f"{stats['avg_abs_move']:.1f}%" if stats.get("avg_abs_move") else "N/A"
        acc = f"{stats['dir_accuracy']:.0%}" if stats.get("dir_accuracy") else "N/A"
        lines.append(f"| {ct} | {stats['n']} | {move} | {acc} |")

    lines += [
        "",
        "## Science Grade Predictive Accuracy",
        "| Grade | N | Dir Accuracy |",
        "|-------|---|--------------|",
    ]
    for grade in ["A", "B", "C", "D", "F"]:
        stats = grade_acc.get(grade, {})
        if stats.get("n", 0) > 0:
            lines.append(f"| {grade} | {stats['n']} | {stats['accuracy']:.0%} |")

    lines += [
        "",
        "## IV Crush Analysis",
        f"- Total IV crush cases: {iv_analysis['total_iv_crush']}",
        f"- By catalyst type: {iv_analysis['by_catalyst_type']}",
        f"- Avg IV at entry when crushed: {iv_analysis.get('avg_iv_at_entry', 'N/A')}%",
        "",
        "## Current Calibration Parameters",
        f"- P minimum LONG: {params.get('p_min_long')}%",
        f"- P maximum SHORT: {params.get('p_max_short')}%",
        f"- IV crush half-size threshold: {params.get('iv_crush_halfsize_threshold')}%",
        "",
        "### OTM Ranges by Catalyst Type",
    ]
    for ct, rng in params.get("otm_ranges", {}).items():
        lines.append(f"- {ct}: {rng[0]}-{rng[1]}%")

    lines += ["", "### Expected Stock Moves"]
    for ct, moves in params.get("expected_moves", {}).items():
        lines.append(f"- {ct}: win +{moves[0]}%, loss -{moves[1]}%")

    if changes_made:
        lines += ["", "## Parameter Changes This Run"]
        for c in changes_made:
            lines.append(f"- {c}")

    log = params.get("change_log", [])
    if log:
        lines += ["", "## Change History"]
        for entry in reversed(log[-5:]):  # Last 5 runs
            lines.append(f"### {entry['date']} (n={entry['n_resolved']})")
            for c in entry["changes"]:
                lines.append(f"- {c}")

    lines += [
        "",
        "## Resolved Trade Log",
        "| Ticker | Date | Outcome | Dir | Opt P&L | Stock Move | P% | Science |",
        "|--------|------|---------|-----|---------|------------|----|---------|\n",
    ]
    for r in sorted(resolved, key=lambda x: x.get("resolved_date", ""), reverse=True):
        lines.append(
            f"| {r['ticker']} | {r.get('resolved_date','?')} | {r.get('outcome','?')} | "
            f"{'Y' if r.get('direction_correct') else 'N'} | "
            f"{r.get('option_pnl_pct', '?'):+.0f}%" if isinstance(r.get('option_pnl_pct'), (int, float)) else f"| {r['ticker']} | {r.get('resolved_date','?')} | {r.get('outcome','?')} | {'Y' if r.get('direction_correct') else 'N'} | N/A"
            + f" | {r.get('stock_move_pct', '?'):+.1f}%" if isinstance(r.get('stock_move_pct'), (int, float)) else " | N/A"
            + f" | {r.get('entry_p_success','?')}% | {r.get('entry_science_grade','?')} |"
        )

    return "\n".join(lines)


def get_current_params() -> dict:
    """Quick accessor for other modules to get current calibration params."""
    return load_params()


def format_learning_summary_for_email(learning_result: dict) -> str:
    """One-section summary for the daily email."""
    if not learning_result or learning_result.get("n", 0) == 0:
        return ""

    n = learning_result.get("n", 0)
    dir_acc = learning_result.get("dir_accuracy", 0)
    opt_hit = learning_result.get("opt_hit_rate", 0)
    brier = learning_result.get("brier_score")
    changes = learning_result.get("changes_made", [])
    bucket_stats = learning_result.get("bucket_stats", {})

    lines = [
        "─" * 65,
        f"SYSTEM CALIBRATION ({n} resolved trades)",
        "─" * 65,
        f"  Directional accuracy: {dir_acc:.0%}  |  Option hit rate: {opt_hit:.0%}  |  Brier: {brier:.3f}" if brier else f"  Directional accuracy: {dir_acc:.0%}  |  Option hit rate: {opt_hit:.0%}",
    ]

    if bucket_stats:
        lines.append("  By P bucket:")
        for bucket, stats in bucket_stats.items():
            lines.append(f"    {bucket}: {stats['n']}x trades | dir={stats['dir_accuracy']:.0%} | opt={stats['opt_hit_rate']:.0%}")

    if changes:
        lines.append(f"  Parameters auto-updated: {len(changes)} change(s) this run")
        for c in changes[:3]:
            lines.append(f"    - {c}")
        if len(changes) > 3:
            lines.append(f"    ... +{len(changes)-3} more")

    return "\n".join(lines)


if __name__ == "__main__":
    result = run_learning_cycle()
    print(f"\n{result['summary']}")
