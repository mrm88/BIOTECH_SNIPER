#!/usr/bin/env python3
"""
MASTER INTELLIGENCE RUNNER
Runs all 4 intelligence modules and produces a consolidated signal report.
Called at the start of every daily 6 AM run BEFORE the main catalyst report.

Output: <BASE_DIR>/intelligence/master_signals.json

Any CRITICAL signals automatically get flagged in the daily email with 🚨 ALERT header.
"""

import json
import datetime
import sys
from pathlib import Path

from biotech_sniper.paths import BASE_DIR
OUTPUT_FILE = BASE_DIR / "intelligence/master_signals.json"

def run_all_modules():
    today = datetime.date.today().isoformat()
    now = datetime.datetime.now().isoformat()
    all_signals = []
    module_results = {}

    print(f"\n{'#'*70}")
    print(f"# BIOTECH SNIPER INTELLIGENCE SUITE — {today}")
    print(f"{'#'*70}")

    # ── MODULE 1: ClinicalTrials.gov Amendment Tracker ───────────────────
    try:
        from amendment_tracker import run_amendment_check
        print("\n[1/4] Running ClinicalTrials.gov amendment tracker...")
        result = run_amendment_check()
        module_results["amendment_tracker"] = result
        all_signals.extend(result.get("signals", []))
        print(f"  → {len(result.get('signals', []))} signals")
    except Exception as e:
        print(f"  → ERROR: {e}")
        module_results["amendment_tracker"] = {"error": str(e)}

    # ── MODULE 2: IR Events Calendar Watcher ─────────────────────────────
    try:
        from ir_events_watcher import run_ir_events_check
        print("\n[2/4] Running IR events calendar watcher...")
        result = run_ir_events_check()
        module_results["ir_events"] = result
        all_signals.extend(result.get("signals", []))
        print(f"  → {len(result.get('signals', []))} signals")
    except Exception as e:
        print(f"  → ERROR: {e}")
        module_results["ir_events"] = {"error": str(e)}

    # ── MODULE 3: SEC 8-K Monitor ─────────────────────────────────────────
    try:
        from sec_8k_monitor import run_8k_monitor
        print("\n[3/4] Running SEC 8-K monitor...")
        result = run_8k_monitor(mode="daily")
        module_results["sec_8k"] = result
        all_signals.extend(result.get("signals", []))
        print(f"  → {len(result.get('signals', []))} signals")
    except Exception as e:
        print(f"  → ERROR: {e}")
        module_results["sec_8k"] = {"error": str(e)}

    # ── MODULE 4: Twitter Biotech Monitor ────────────────────────────────
    try:
        from twitter_biotech_monitor import run_twitter_monitor
        print("\n[4/4] Running Twitter biotech monitor...")
        result = run_twitter_monitor()
        module_results["twitter"] = result
        all_signals.extend(result.get("signals", []))
        print(f"  → {len(result.get('signals', []))} signals")
    except Exception as e:
        print(f"  → ERROR: {e}")
        module_results["twitter"] = {"error": str(e)}

    # ── CONSOLIDATE ───────────────────────────────────────────────────────
    critical = [s for s in all_signals if s.get("severity") in ("CRITICAL",)]
    high = [s for s in all_signals if s.get("severity") == "HIGH"]
    moderate = [s for s in all_signals if s.get("severity") == "MODERATE"]

    # Deduplicate by ticker+type
    seen = set()
    deduped = []
    for s in all_signals:
        key = f"{s.get('ticker','')}-{s.get('type','')}"
        if key not in seen:
            seen.add(key)
            deduped.append(s)

    master = {
        "run_date": today,
        "run_time": now,
        "all_signals": deduped,
        "critical": critical,
        "high": high,
        "moderate": moderate,
        "total": len(deduped),
        "module_results": module_results,
        "summary_for_email": format_email_summary(deduped, critical, high)
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(master, f, indent=2)

    print(f"\n{'#'*70}")
    print(f"INTELLIGENCE SUITE COMPLETE")
    print(f"  Critical: {len(critical)} | High: {len(high)} | Moderate: {len(moderate)}")
    if critical:
        print(f"\n  🚨 CRITICAL SIGNALS — REVIEW BEFORE TRADING:")
        for s in critical:
            print(f"     [{s.get('ticker','?')}] {s.get('type','')}: {s.get('detail','')[:80]}")
    print(f"  Saved: {OUTPUT_FILE}")
    print(f"{'#'*70}\n")

    return master

def format_email_summary(all_signals, critical, high):
    """Format signal summary for inclusion in the daily email."""
    if not all_signals:
        return "INTELLIGENCE SUITE: No signals today. All watchlist trials stable."

    lines = []
    lines.append("=" * 60)
    lines.append("INTELLIGENCE SIGNALS (ClinicalTrials + IR + SEC + Twitter)")
    lines.append("=" * 60)

    if critical:
        lines.append(f"\n🚨 CRITICAL ({len(critical)}) — REVIEW BEFORE TRADING:")
        for s in critical:
            lines.append(f"  [{s.get('ticker','?')}] {s.get('icon','')} {s.get('type','')}")
            lines.append(f"    {s.get('detail','')[:120]}")
            if s.get("implication"):
                lines.append(f"    → {s.get('implication','')[:120]}")

    if high:
        lines.append(f"\n🟠 HIGH PRIORITY ({len(high)}):")
        for s in high:
            lines.append(f"  [{s.get('ticker','?')}] {s.get('icon','')} {s.get('type','')}: {s.get('detail','')[:100]}")

    if not critical and not high:
        lines.append("\n✓ No critical or high-priority signals. All systems normal.")

    return "\n".join(lines)

if __name__ == "__main__":
    # Add intelligence dir to path
    sys.path.insert(0, str(BASE_DIR / "intelligence"))
    run_all_modules()

def run_all_with_lifecycle():
    """Full run including lifecycle management."""
    import sys
    sys.path.insert(0, str(BASE_DIR / "intelligence"))

    # Step 1: Run intelligence modules
    today = datetime.date.today().isoformat()
    module_results = {}
    all_signals = []

    print(f"\n{'#'*70}")
    print(f"# BIOTECH SNIPER INTELLIGENCE SUITE — {today}")
    print(f"{'#'*70}")

    # Run amendment tracker
    try:
        from amendment_tracker import run_amendment_check
        result = run_amendment_check()
        module_results["amendment"] = result
        all_signals.extend(result.get("signals", []))
    except Exception as e:
        module_results["amendment"] = {"error": str(e)}

    # Run IR events watcher
    try:
        from ir_events_watcher import run_ir_events_check
        result = run_ir_events_check()
        module_results["ir_events"] = result
        all_signals.extend(result.get("signals", []))
    except Exception as e:
        module_results["ir_events"] = {"error": str(e)}

    # Run 8-K monitor
    try:
        from sec_8k_monitor import run_8k_monitor
        result = run_8k_monitor(mode="daily")
        module_results["sec_8k"] = result
        all_signals.extend(result.get("signals", []))
    except Exception as e:
        module_results["sec_8k"] = {"error": str(e)}

    # Run Twitter monitor
    try:
        from twitter_biotech_monitor import run_twitter_monitor
        result = run_twitter_monitor()
        module_results["twitter"] = result
        all_signals.extend(result.get("signals", []))
    except Exception as e:
        module_results["twitter"] = {"error": str(e)}

    # Step 2: Run lifecycle manager with signal inputs
    try:
        from watchlist_lifecycle import run_lifecycle_check
        lifecycle = run_lifecycle_check(
            sec_8k_report=module_results.get("sec_8k"),
            ir_events_report=module_results.get("ir_events")
        )
        module_results["lifecycle"] = lifecycle
    except Exception as e:
        module_results["lifecycle"] = {"error": str(e)}

    # Step 3: Save master output
    critical = [s for s in all_signals if s.get("severity") == "CRITICAL"]
    high = [s for s in all_signals if s.get("severity") == "HIGH"]

    master = {
        "run_date": today,
        "all_signals": all_signals,
        "critical": critical,
        "high": high,
        "total": len(all_signals),
        "module_results": module_results,
        "summary_for_email": format_email_summary(all_signals, critical, high),
        "active_plays_ordered": module_results.get("lifecycle", {}).get("active_plays_ordered", []),
        "removed_today": module_results.get("lifecycle", {}).get("removed", []),
        "graduated_today": module_results.get("lifecycle", {}).get("graduated", []),
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(master, f, indent=2)

    print(f"\n{'#'*70}")
    print(f"SUITE COMPLETE | {len(critical)} critical | {len(high)} high | {len(all_signals)} total signals")
    print(f"Active plays: {len(master['active_plays_ordered'])} | Removed today: {master['removed_today']}")
    print(f"{'#'*70}\n")

    return master

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(BASE_DIR / "intelligence"))
    run_all_with_lifecycle()
