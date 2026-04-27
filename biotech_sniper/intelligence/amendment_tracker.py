#!/usr/bin/env python3
"""
CLINICALTRIALS.GOV AMENDMENT TRACKER
Checks every watchlist trial for:
1. Protocol amendments in the last 90 days
2. Primary outcome measure changes (endpoint switching - huge red flag)
3. Enrollment changes (expansion = confidence, reduction = concern)
4. Study status changes

Signals:
  🚨 BEARISH: Primary endpoint changed, sample size reduced, study suspended
  🟢 BULLISH: Enrollment expanded, new sites added, accelerated timeline
  ⚪ NEUTRAL: Administrative/formatting changes only
"""

import json
import requests
import datetime
from pathlib import Path

from biotech_sniper.paths import BASE_DIR
REGISTRY_FILE = BASE_DIR / "intelligence/nct_registry.json"
STATE_FILE = BASE_DIR / "state/amendment_state.json"
OUTPUT_FILE = BASE_DIR / "intelligence/amendment_report.json"

def load_registry():
    with open(REGISTRY_FILE) as f:
        return json.load(f)

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    STATE_FILE.parent.mkdir(exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def fetch_trial_current(nct_id):
    """Fetch current trial data from ClinicalTrials.gov v2 API."""
    url = f"https://clinicaltrials.gov/api/v2/studies/{nct_id}"
    params = {
        "fields": "NCTId,BriefTitle,OverallStatus,Phase,EnrollmentInfo,"
                  "PrimaryOutcomeMeasure,PrimaryOutcomeDescription,"
                  "SecondaryOutcomeMeasure,StudyFirstPostDate,"
                  "LastUpdatePostDate,StatusVerifiedDate,"
                  "DesignInfo,EligibilityCriteria,LeadSponsorName"
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def fetch_trial_history(nct_id):
    """Fetch version history from ClinicalTrials.gov."""
    url = f"https://clinicaltrials.gov/api/v2/studies/{nct_id}/history"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def extract_primary_outcomes(study_data):
    """Extract primary outcome measures from study data."""
    outcomes = []
    try:
        protocol = study_data.get("protocolSection", {})
        outcomes_module = protocol.get("outcomesModule", {})
        primary = outcomes_module.get("primaryOutcomes", [])
        for o in primary:
            outcomes.append({
                "measure": o.get("measure", ""),
                "description": o.get("description", ""),
                "timeFrame": o.get("timeFrame", "")
            })
    except:
        pass
    return outcomes

def extract_enrollment(study_data):
    """Extract enrollment info."""
    try:
        protocol = study_data.get("protocolSection", {})
        design = protocol.get("designModule", {})
        enrollment = design.get("enrollmentInfo", {})
        return {
            "count": enrollment.get("count"),
            "type": enrollment.get("type", "")  # ACTUAL or ESTIMATED
        }
    except:
        return {"count": None, "type": ""}

def extract_status(study_data):
    """Extract overall status."""
    try:
        protocol = study_data.get("protocolSection", {})
        status = protocol.get("statusModule", {})
        return {
            "overall": status.get("overallStatus", ""),
            "last_update": status.get("lastUpdatePostDateStruct", {}).get("date", ""),
            "verified_date": status.get("statusVerifiedDate", "")
        }
    except:
        return {}

def classify_amendment(old_state, new_data, nct_id, ticker):
    """
    Compare current trial data to saved state.
    Returns list of signals with sentiment classification.
    """
    signals = []
    today = datetime.date.today().isoformat()
    cutoff_date = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()

    new_outcomes = extract_primary_outcomes(new_data)
    new_enrollment = extract_enrollment(new_data)
    new_status = extract_status(new_data)
    last_update = new_status.get("last_update", "")

    # Only flag amendments in the last 90 days
    if last_update < cutoff_date:
        return signals  # No recent amendments

    old = old_state.get(nct_id, {})
    old_outcomes = old.get("primary_outcomes", [])
    old_enrollment = old.get("enrollment", {})
    old_overall_status = old.get("status", {}).get("overall", "")

    # ── PRIMARY ENDPOINT CHANGE ──────────────────────────────────────────
    if old_outcomes and new_outcomes:
        old_measures = [o["measure"].lower() for o in old_outcomes]
        new_measures = [o["measure"].lower() for o in new_outcomes]
        if old_measures != new_measures:
            signals.append({
                "ticker": ticker,
                "nct_id": nct_id,
                "type": "PRIMARY_ENDPOINT_CHANGED",
                "sentiment": "BEARISH",
                "severity": "CRITICAL",
                "icon": "🚨",
                "detail": f"Primary endpoint changed from '{old_measures}' → '{new_measures}'",
                "implication": "Endpoint switching almost always signals sponsor concern about hitting original endpoint. Probability should be DOWNGRADED significantly.",
                "detected_date": today,
                "last_update": last_update
            })

    # ── ENROLLMENT CHANGE ───────────────────────────────────────────────
    old_count = old_enrollment.get("count")
    new_count = new_enrollment.get("count")
    if old_count and new_count and old_count != new_count:
        change_pct = ((new_count - old_count) / old_count) * 100
        if new_count > old_count:
            signals.append({
                "ticker": ticker,
                "nct_id": nct_id,
                "type": "ENROLLMENT_EXPANDED",
                "sentiment": "BULLISH",
                "severity": "MODERATE",
                "icon": "🟢",
                "detail": f"Enrollment expanded {old_count} → {new_count} (+{change_pct:.1f}%)",
                "implication": "Enrollment expansion typically signals sponsor confidence in drug effect size and regulatory pathway.",
                "detected_date": today,
                "last_update": last_update
            })
        elif new_count < old_count and change_pct < -10:
            signals.append({
                "ticker": ticker,
                "nct_id": nct_id,
                "type": "ENROLLMENT_REDUCED",
                "sentiment": "BEARISH",
                "severity": "MODERATE",
                "icon": "🟡",
                "detail": f"Enrollment reduced {old_count} → {new_count} ({change_pct:.1f}%)",
                "implication": "Could indicate interim analysis showed larger-than-expected effect (positive) OR study re-powered downward due to weaker signal (negative). Investigate.",
                "detected_date": today,
                "last_update": last_update
            })

    # ── STATUS CHANGE ────────────────────────────────────────────────────
    if old_overall_status and old_overall_status != new_status.get("overall", ""):
        new_overall = new_status.get("overall", "")
        is_bad = any(x in new_overall.upper() for x in ["SUSPEND", "TERMINAT", "WITHDRAWN"])
        is_good = any(x in new_overall.upper() for x in ["COMPLET", "ACTIVE"])
        signals.append({
            "ticker": ticker,
            "nct_id": nct_id,
            "type": "STATUS_CHANGED",
            "sentiment": "BEARISH" if is_bad else ("BULLISH" if is_good else "NEUTRAL"),
            "severity": "CRITICAL" if is_bad else "MODERATE",
            "icon": "🚨" if is_bad else ("🟢" if is_good else "⚪"),
            "detail": f"Status changed: '{old_overall_status}' → '{new_overall}'",
            "implication": f"Study status change detected. {'IMMEDIATE concern — investigate.' if is_bad else 'Monitor.'}",
            "detected_date": today,
            "last_update": last_update
        })

    # ── RECENT UPDATE (no prior state — first time checking) ─────────────
    if not old and last_update >= cutoff_date:
        signals.append({
            "ticker": ticker,
            "nct_id": nct_id,
            "type": "RECENT_AMENDMENT_DETECTED",
            "sentiment": "WATCH",
            "severity": "LOW",
            "icon": "👁",
            "detail": f"Trial was last updated {last_update} (within 90-day pre-readout window). Baseline captured — will monitor for further changes.",
            "implication": "First check — no prior state to compare. Will detect changes from tomorrow.",
            "detected_date": today,
            "last_update": last_update
        })

    return signals

def run_amendment_check():
    registry = load_registry()
    state = load_state()
    
    all_signals = []
    new_state = {}
    
    print(f"\n{'='*70}")
    print(f"CLINICALTRIALS.GOV AMENDMENT TRACKER — {datetime.date.today()}")
    print(f"{'='*70}")
    
    for ticker, info in registry["watchlist"].items():
        nct_id = info.get("nct_id")
        if not nct_id:
            continue
            
        print(f"\nChecking {ticker} ({nct_id})...")
        
        # Fetch current data
        current = fetch_trial_current(nct_id)
        if "error" in current:
            print(f"  ERROR: {current['error']}")
            continue
        
        # Extract state
        outcomes = extract_primary_outcomes(current)
        enrollment = extract_enrollment(current)
        status = extract_status(current)
        
        # Save to new state
        new_state[nct_id] = {
            "ticker": ticker,
            "primary_outcomes": outcomes,
            "enrollment": enrollment,
            "status": status,
            "last_checked": datetime.date.today().isoformat()
        }
        
        # Detect amendments
        signals = classify_amendment(state, current, nct_id, ticker)
        
        if signals:
            for s in signals:
                print(f"  {s['icon']} [{s['sentiment']}] {s['type']}: {s['detail']}")
            all_signals.extend(signals)
        else:
            last_update = status.get("last_update", "unknown")
            enrollment_count = enrollment.get("count", "unknown")
            print(f"  ✓ No amendments detected | Last update: {last_update} | Enrollment: {enrollment_count}")
            print(f"    Primary endpoints: {[o['measure'][:60] for o in outcomes]}")
    
    # Save updated state
    save_state(new_state)
    
    # Save report
    report = {
        "run_date": datetime.date.today().isoformat(),
        "signals": all_signals,
        "tickers_checked": list(registry["watchlist"].keys()),
        "total_signals": len(all_signals),
        "bearish_signals": [s for s in all_signals if s["sentiment"] == "BEARISH"],
        "bullish_signals": [s for s in all_signals if s["sentiment"] == "BULLISH"]
    }
    
    with open(OUTPUT_FILE, "w") as f:
        json.dump(report, f, indent=2)
    
    print(f"\n{'='*70}")
    print(f"SUMMARY: {len(all_signals)} signals detected")
    if report["bearish_signals"]:
        print(f"  🚨 BEARISH: {len(report['bearish_signals'])} — REVIEW IMMEDIATELY")
        for s in report["bearish_signals"]:
            print(f"     {s['ticker']}: {s['detail']}")
    if report["bullish_signals"]:
        print(f"  🟢 BULLISH: {len(report['bullish_signals'])}")
    print(f"Report saved: {OUTPUT_FILE}")
    print(f"{'='*70}\n")
    
    return report

if __name__ == "__main__":
    run_amendment_check()
