#!/usr/bin/env python3
"""
SCIENCE ENRICHMENT PIPELINE
Runs during the daily cron, after discovery and before email.

For every active play + new candidate:
  1. Fetch full ClinicalTrials protocol
  2. Grade science (A-F)
  3. Adjust P(success) based on grade + indication base rate
  4. Flag new SHORT CANDIDATES (Grade D/F + currently trading as LONG)
  5. Save science_grade to active_plays.json
  6. Return enriched plays for email

Also runs NEW_SHORT_DISCOVERY:
  Looks for Grade D/F plays that are currently priced as if they'll succeed.
  These are the Shkreli short setups — market hasn't read the protocol.
"""

import json
import datetime
from pathlib import Path
from typing import Optional

from biotech_sniper.paths import BASE_DIR
ACTIVE_PLAYS_FILE = BASE_DIR / "state/active_plays.json"
SCIENCE_GRADES_FILE = BASE_DIR / "state/science_grades.json"
SCIENCE_ALERTS_FILE = BASE_DIR / "state/science_alerts.json"


def load_active_plays() -> dict:
    if ACTIVE_PLAYS_FILE.exists():
        with open(ACTIVE_PLAYS_FILE) as f:
            return json.load(f)
    return {"active": {}, "monitor": {}}


def save_active_plays(plays: dict):
    with open(ACTIVE_PLAYS_FILE, "w") as f:
        json.dump(plays, f, indent=2)


def load_science_grades() -> dict:
    if SCIENCE_GRADES_FILE.exists():
        with open(SCIENCE_GRADES_FILE) as f:
            return json.load(f)
    return {}


def save_science_grades(grades: dict):
    with open(SCIENCE_GRADES_FILE, "w") as f:
        json.dump(grades, f, indent=2)


def load_science_alerts() -> dict:
    if SCIENCE_ALERTS_FILE.exists():
        with open(SCIENCE_ALERTS_FILE) as f:
            return json.load(f)
    return {"sent": []}


def save_science_alerts(alerts: dict):
    with open(SCIENCE_ALERTS_FILE, "w") as f:
        json.dump(alerts, f, indent=2)


def run_science_enrichment() -> dict:
    """
    Main pipeline. Call from daily cron after discovery, before email.
    
    Returns:
      enriched_plays: dict of ticker -> enriched play data
      science_alerts: list of new science signals to include in email
      new_short_candidates: Grade D/F plays to add as LONG_PUTS
    """
    from intelligence.trial_science_reader import fetch_full_protocol, get_science_profile
    from intelligence.science_scorer import compute_science_grade, adjust_probability_with_science

    plays_data = load_active_plays()
    active = plays_data.get("active", {})
    science_grades = load_science_grades()
    science_alerts = load_science_alerts()
    already_alerted = set(science_alerts.get("sent", []))

    enriched = {}
    new_short_candidates = []
    new_alerts = []
    today = datetime.date.today().isoformat()

    print("\n[SCIENCE ENRICHMENT] Running Shkreli-style protocol analysis...")

    for ticker, play in active.items():
        nct_id = play.get("nct_id", "")
        if not nct_id or nct_id.startswith("UNK") or not nct_id.startswith("NCT"):
            print(f"  {ticker}: no NCT ID, skipping science enrichment")
            enriched[ticker] = play
            continue

        # Use cached grade if fresh (< 7 days old)
        cache_key = f"{ticker}_{nct_id}"
        if cache_key in science_grades:
            grade_data = science_grades[cache_key]
            age = (datetime.date.today() - datetime.date.fromisoformat(grade_data.get("graded_date", "2000-01-01"))).days
            if age < 7:
                print(f"  {ticker}: using cached grade {grade_data.get('grade','?')} (age {age}d)")
                play["science_grade"] = grade_data.get("grade")
                play["science_grade_data"] = grade_data
                play["science_adjusted_p"] = grade_data.get("adjusted_p")
                enriched[ticker] = play
                continue

        # Fresh fetch
        print(f"  {ticker} [{nct_id}]: fetching protocol...")
        candidate = {
            "ticker": ticker,
            "drug": play.get("drug", ""),
            "indication": play.get("indication", ""),
        }

        try:
            science_profile = get_science_profile(nct_id, candidate)
            if not science_profile.get("protocol"):
                print(f"  {ticker}: no protocol data, skipping")
                enriched[ticker] = play
                continue

            grade = compute_science_grade(science_profile)
            current_p = play.get("p_success", play.get("P", 50))
            indication = play.get("indication", "")
            conditions = science_profile.get("protocol", {}).get("conditions", [])
            adjustment = adjust_probability_with_science(current_p, grade, indication, conditions)

            # Store grade
            grade_data = {
                **grade,
                "adjusted_p": adjustment["adjusted_p"],
                "base_rate": adjustment["base_rate"],
                "matched_category": adjustment["matched_category"],
                "adjustment_summary": adjustment["adjustment_summary"],
                "ticker": ticker,
                "nct_id": nct_id,
                "graded_date": today,
                "science_prompt": science_profile.get("science_prompt", ""),
            }
            science_grades[cache_key] = grade_data

            # Update play with science data
            play["science_grade"] = grade["grade"]
            play["science_grade_data"] = grade_data
            play["science_adjusted_p"] = adjustment["adjusted_p"]

            # Detect MISMATCH: play is LONG but science says it should be SHORT
            direction = play.get("direction", "")
            is_long_play = direction in ("LONG_CALLS", "CALL_SPREAD")
            is_short_candidate = grade.get("is_short_candidate", False)

            alert_key = f"sci_mismatch:{ticker}:{grade['grade']}"
            if is_long_play and is_short_candidate and alert_key not in already_alerted:
                alert = {
                    "type": "SCIENCE_MISMATCH",
                    "ticker": ticker,
                    "current_direction": direction,
                    "science_grade": grade["grade"],
                    "design_score": grade["design_score"],
                    "current_p": current_p,
                    "adjusted_p": adjustment["adjusted_p"],
                    "short_thesis": grade.get("short_thesis", ""),
                    "bearish_flags": grade.get("bearish_flags", []),
                    "alert_key": alert_key,
                    "detected_date": today,
                }
                new_alerts.append(alert)
                already_alerted.add(alert_key)
                print(f"  {ticker}: MISMATCH ALERT — Grade {grade['grade']} but currently LONG")

            # Detect new SHORT candidate (Grade D/F, not already in active plays)
            if is_short_candidate and not is_long_play and adjustment["adjusted_p"] <= 40:
                # Check if we already have a PUT play for this
                existing_put = any(
                    p.get("ticker") == ticker and "PUT" in p.get("direction", "")
                    for p in active.values()
                )
                if not existing_put:
                    new_short_candidates.append({
                        "ticker": ticker,
                        "nct_id": nct_id,
                        "drug": play.get("drug", ""),
                        "indication": indication,
                        "science_grade": grade["grade"],
                        "adjusted_p": adjustment["adjusted_p"],
                        "short_thesis": grade.get("short_thesis", ""),
                        "bearish_flags": grade.get("bearish_flags", []),
                        "p_fail": 100 - adjustment["adjusted_p"],
                    })

        except Exception as e:
            print(f"  {ticker}: science enrichment error: {e}")

        enriched[ticker] = play

    # Save updated grades and alerts
    save_science_grades(science_grades)
    science_alerts["sent"] = list(already_alerted)
    save_science_alerts(science_alerts)

    # Save enriched plays back
    plays_data["active"] = enriched
    save_active_plays(plays_data)

    print(f"\n[SCIENCE ENRICHMENT] Complete: {len(enriched)} plays enriched, {len(new_alerts)} alerts, {len(new_short_candidates)} short candidates")

    return {
        "enriched_plays": enriched,
        "science_alerts": new_alerts,
        "new_short_candidates": new_short_candidates,
        "grades_summary": {
            t: {
                "grade": p.get("science_grade", "?"),
                "adjusted_p": p.get("science_adjusted_p"),
            }
            for t, p in enriched.items()
        }
    }


def get_science_grade_line(play: dict) -> str:
    """
    Returns a one-liner science grade for the email/card.
    e.g. "Science: Grade C | Open-label RCT, hard OS endpoint | Base rate 42%"
    """
    grade_data = play.get("science_grade_data", {})
    if not grade_data:
        return ""

    grade = grade_data.get("grade", "?")
    design_score = grade_data.get("design_score", 0)
    endpoint_type = grade_data.get("endpoint_type", "")
    base_rate = grade_data.get("base_rate", 0)
    bearish = grade_data.get("bearish_flags", [])
    bullish = grade_data.get("bullish_flags", [])

    top_signal = bearish[0] if bearish else (bullish[0] if bullish else "")
    ep_tag = f" | {endpoint_type} endpoint" if endpoint_type and endpoint_type != "UNKNOWN" else ""
    br_tag = f" | historical base {base_rate*100:.0f}%" if base_rate else ""

    is_short = grade_data.get("is_short_candidate", False)
    short_tag = " [SHORT SETUP]" if is_short else ""

    return f"Science Grade {grade} ({design_score:+d}){ep_tag}{br_tag}{short_tag} — {top_signal}"


def format_science_section_for_email(enriched_plays: dict) -> str:
    """
    Build the science grades section for the daily email.
    Groups by grade: A/B (confirmed longs), C (neutral), D/F (short setups).
    """
    lines = ["", "SCIENCE GRADES (Shkreli-style protocol analysis)", "=" * 50]

    grade_order = ["A", "B", "C", "D", "F"]
    by_grade = {g: [] for g in grade_order}

    for ticker, play in enriched_plays.items():
        grade = play.get("science_grade", "?")
        if grade in by_grade:
            by_grade[grade].append((ticker, play))

    for grade in grade_order:
        plays_in_grade = by_grade[grade]
        if not plays_in_grade:
            continue

        grade_label = {
            "A": "GRADE A — High-conviction science",
            "B": "GRADE B — Solid design",
            "C": "GRADE C — Adequate, some concerns",
            "D": "GRADE D — Weak science [review long thesis]",
            "F": "GRADE F — Flawed protocol [short setup]",
        }.get(grade, f"GRADE {grade}")

        lines.append(f"\n{grade_label}:")
        for ticker, play in plays_in_grade:
            line = get_science_grade_line(play)
            adjusted_p = play.get("science_adjusted_p", play.get("p_success", play.get("P", "?")))
            direction = play.get("direction", "?")
            short_thesis = play.get("science_grade_data", {}).get("short_thesis", "")
            lines.append(f"  {ticker} ({direction}) | P={adjusted_p}% | {line}")
            if short_thesis and grade in ("D", "F"):
                lines.append(f"    SHORT THESIS: {short_thesis}")

    return "\n".join(lines)


if __name__ == "__main__":
    result = run_science_enrichment()
    print("\nGRADES SUMMARY:")
    for ticker, g in result["grades_summary"].items():
        print(f"  {ticker}: {g['grade']} | adjusted P={g.get('adjusted_p','?')}%")

    if result["science_alerts"]:
        print("\nSCIENCE ALERTS:")
        for alert in result["science_alerts"]:
            print(f"  {alert['ticker']}: {alert['type']} — {alert.get('short_thesis','')}")

    print("\nSCIENCE EMAIL SECTION:")
    plays = load_active_plays().get("active", {})
    print(format_science_section_for_email(plays))
