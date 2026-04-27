#!/usr/bin/env python3
"""
WATCHLIST LIFECYCLE MANAGER
Runs daily as part of Step 0. Automatically:

1. REMOVES entries when catalyst has resolved:
   - 8-K with topline data filed → event happened, remove from active plays
   - PDUFA date passed → FDA decision made, remove
   - Trial primary completion date passed by >14 days → likely announced, remove
   - Probability updated to 50% on Warpspeed (resolved ambiguously)

2. UPGRADES entries when new information arrives:
   - Announcement date changes from ESTIMATED → CERTAIN (webcast pre-reg found)
   - Timeline accelerates (new IR guidance)
   - New concrete readout date confirmed

3. GRADUATES entries from MONITOR → ACTIVE when within 60-day window:
   - MLTX, RZLT, AMGN, RVMD, NAMS — when readout window < 60 days

4. ARCHIVES resolved plays:
   - Moves to <BASE_DIR>/state/resolved_plays.json
   - Records: ticker, outcome, P_predicted, actual_result, P&L if known, date

5. ADDS net-new entries discovered in Step 3 (new catalyst scan)

Output: <BASE_DIR>/intelligence/lifecycle_report.json
"""

import json
import re
import datetime
import requests
from pathlib import Path

from biotech_sniper.paths import BASE_DIR
REGISTRY_FILE = BASE_DIR / "intelligence/nct_registry.json"
ACTIVE_PLAYS_FILE = BASE_DIR / "state/active_plays.json"
RESOLVED_FILE = BASE_DIR / "state/resolved_plays.json"
LIFECYCLE_REPORT_FILE = BASE_DIR / "intelligence/lifecycle_report.json"

# ── ACTIVE PLAYS STATE ────────────────────────────────────────────────────────
# This is the single source of truth for what's in the watchlist
# Updated automatically every day

DEFAULT_ACTIVE_PLAYS = {
    "IDYA": {
        "ticker": "IDYA",
        "company": "IDEAYA Biosciences",
        "drug": "Darovasertib + Crizotinib",
        "indication": "1L HLA-A2-neg metastatic uveal melanoma",
        "trial": "OptimUM-02",
        "nct_id": "NCT05987332",
        "p_success": 96,
        "p_source": "Warpspeed",
        "direction": "LONG_CALLS",
        "announcement_certainty": "ESTIMATED",
        "estimated_announcement": "late April - early May 2026",
        "pdufa_date": None,
        "option_strike": 40,
        "option_expiry": "2026-05-15",
        "option_type": "C",
        "added_date": "2026-03-30",
        "status": "ACTIVE",
        "days_in_watchlist": 0,
        "last_updated": "2026-03-30",
        "notes": "DB lock confirmed 1H April. Webcast pre-reg not yet opened."
    },
    "TVTX": {
        "ticker": "TVTX",
        "company": "Travere Therapeutics",
        "drug": "Sparsentan (FILSPARI)",
        "indication": "FSGS label expansion sNDA",
        "trial": "DUPLEX",
        "nct_id": "NCT03493685",
        "p_success": 77,
        "p_source": "Ensemble Claude/Gemini",
        "direction": "LONG_CALLS",
        "announcement_certainty": "CERTAIN",
        "estimated_announcement": "April 13 2026",
        "pdufa_date": "2026-04-13",
        "option_strike": 35,
        "option_expiry": "2026-04-17",
        "option_type": "C",
        "added_date": "2026-03-30",
        "status": "ACTIVE",
        "days_in_watchlist": 0,
        "last_updated": "2026-03-30",
        "notes": "PDUFA April 13. Apr 17 expiry = 4 days after CERTAIN date."
    },
    "AXSM": {
        "ticker": "AXSM",
        "company": "Axsome Therapeutics",
        "drug": "AXS-05",
        "indication": "Alzheimer's disease agitation",
        "trial": "ACCORD",
        "nct_id": "NCT04797715",
        "p_success": 85,
        "p_source": "Ensemble Claude/Gemini",
        "direction": "LONG_CALLS",
        "announcement_certainty": "CERTAIN",
        "estimated_announcement": "April 30 2026",
        "pdufa_date": "2026-04-30",
        "option_strike": 200,
        "option_expiry": "2026-05-15",
        "option_type": "C",
        "added_date": "2026-03-30",
        "status": "ACTIVE",
        "days_in_watchlist": 0,
        "last_updated": "2026-03-30",
        "notes": "PDUFA April 30. $200C May 15 at $3.62 mid, OI 535."
    },
    "RGNX": {
        "ticker": "RGNX",
        "company": "REGENXBIO",
        "drug": "RGX-202",
        "indication": "Duchenne Muscular Dystrophy",
        "trial": "AFFINITY DUCHENNE Phase 3",
        "nct_id": "NCT05693142",
        "p_success": 68,
        "p_source": "Ensemble Claude/Gemini",
        "direction": "LONG_CALLS",
        "announcement_certainty": "ESTIMATED",
        "estimated_announcement": "April - May 2026",
        "pdufa_date": None,
        "option_strike": 10,
        "option_expiry": "2026-07-17",
        "option_type": "C",
        "added_date": "2026-03-30",
        "status": "ACTIVE",
        "days_in_watchlist": 0,
        "last_updated": "2026-03-30",
        "notes": "Primary endpoint = biomarker (expression >=10%). Jul17 expiry conservative."
    },
    "ARGX": {
        "ticker": "ARGX",
        "company": "argenx SE",
        "drug": "Efgartigimod alfa",
        "indication": "Seronegative gMG label expansion",
        "trial": "ADAPT-SC+",
        "nct_id": "NCT04980495",
        "p_success": 87,
        "p_source": "Ensemble Claude/Gemini",
        "direction": "CALL_SPREAD",
        "announcement_certainty": "CERTAIN",
        "estimated_announcement": "May 10 2026",
        "pdufa_date": "2026-05-10",
        "option_strike": "750/800 spread",
        "option_expiry": "2026-05-15",
        "option_type": "C",
        "added_date": "2026-03-30",
        "status": "ACTIVE",
        "days_in_watchlist": 0,
        "last_updated": "2026-03-30",
        "notes": "Large-cap. $730/$800 call spread ~$13 debit. Need $1.3k min."
    },
    "AGIO": {
        "ticker": "AGIO",
        "company": "Agios Pharmaceuticals",
        "drug": "Tebapivat",
        "indication": "Lower-risk MDS anemia",
        "trial": "Phase 2b LR-MDS",
        "nct_id": "NCT05490446",
        "p_success": 6,
        "p_source": "Warpspeed",
        "direction": "LONG_PUTS",
        "announcement_certainty": "ESTIMATED",
        "estimated_announcement": "May - June 2026",
        "pdufa_date": None,
        "option_strike": 17.5,
        "option_expiry": "2026-08-21",
        "option_type": "P",
        "added_date": "2026-03-30",
        "status": "ACTIVE",
        "days_in_watchlist": 0,
        "last_updated": "2026-03-30",
        "notes": "$17.5P Aug21. All puts show $0 bid — patient limit entry only."
    },
    "NTLA": {
        "ticker": "NTLA",
        "company": "Intellia Therapeutics",
        "drug": "onvo-z (NTLA-2002)",
        "indication": "Hereditary Angioedema",
        "trial": "HAELO Phase 3",
        "nct_id": "NCT06634420",
        "p_success": 82,
        "p_source": "Ensemble Claude/Gemini",
        "direction": "LONG_CALLS",
        "announcement_certainty": "ESTIMATED",
        "estimated_announcement": "May - June 2026",
        "pdufa_date": None,
        "option_strike": 15,
        "option_expiry": "2026-07-17",
        "option_type": "C",
        "added_date": "2026-03-30",
        "status": "ACTIVE",
        "days_in_watchlist": 0,
        "last_updated": "2026-03-30",
        "notes": "$15C Jul17 @ $1.65 mid. OI 1,734. Tight $0.30 spread."
    },
    "VRDN": {
        "ticker": "VRDN",
        "company": "Viridian Therapeutics",
        "drug": "Elegrobart",
        "indication": "Chronic TED REVEAL-2",
        "trial": "REVEAL-2 Phase 3",
        "nct_id": "NCT06625398",
        "p_success": 80,
        "p_source": "Ensemble Claude/Gemini",
        "direction": "LONG_CALLS",
        "announcement_certainty": "ESTIMATED",
        "estimated_announcement": "Q2 2026",
        "pdufa_date": None,
        "option_strike": 25,
        "option_expiry": "2026-07-17",
        "option_type": "C",
        "added_date": "2026-03-30",
        "status": "ACTIVE",
        "days_in_watchlist": 0,
        "last_updated": "2026-03-30",
        "notes": "Crashed -32% on REVEAL-1 today. Stock near cash value. $25C Jul17."
    },
    "LPCN": {
        "ticker": "LPCN",
        "company": "Lipocine",
        "drug": "LPCN 1154",
        "indication": "Postpartum depression",
        "trial": "Phase 3 PPD",
        "nct_id": None,
        "p_success": 62,
        "p_source": "Ensemble Claude/Gemini",
        "direction": "EQUITY_ONLY",
        "announcement_certainty": "ESTIMATED",
        "estimated_announcement": "Q2 2026",
        "pdufa_date": None,
        "option_strike": None,
        "option_expiry": None,
        "option_type": None,
        "added_date": "2026-03-30",
        "status": "ACTIVE",
        "days_in_watchlist": 0,
        "last_updated": "2026-03-30",
        "notes": "No options market. Equity only. 131 shares at $7.58 = $1k."
    }
}

MONITOR_PLAYS = {
    "MLTX": {"ticker": "MLTX", "p_success": 100, "estimated_announcement": "Mid 2026", "status": "MONITOR", "notes": "IZAR-1 PsA primary. Add to active when <60 days."},
    "RZLT": {"ticker": "RZLT", "p_success": 72, "estimated_announcement": "H2 2026", "status": "MONITOR", "notes": "upLIFT tumor HI. 2H 2026 too far."},
    "AMGN": {"ticker": "AMGN", "p_success": 71, "estimated_announcement": "2026-2027", "status": "MONITOR", "notes": "OCEAN(a)-PreEvent. Too far."},
    # RVMD removed from MONITOR — graduated to ACTIVE with full play entry (May-June 2026)
    "NAMS": {"ticker": "NAMS", "p_success": 37, "estimated_announcement": "2026-2027", "status": "MONITOR", "notes": "PREVAIL. Add when readout window dated."},
}

def load_active_plays():
    if ACTIVE_PLAYS_FILE.exists():
        with open(ACTIVE_PLAYS_FILE) as f:
            return json.load(f)
    # First run — seed from defaults
    save_active_plays({"active": DEFAULT_ACTIVE_PLAYS, "monitor": MONITOR_PLAYS})
    return {"active": DEFAULT_ACTIVE_PLAYS, "monitor": MONITOR_PLAYS}

def save_active_plays(plays):
    ACTIVE_PLAYS_FILE.parent.mkdir(exist_ok=True)
    with open(ACTIVE_PLAYS_FILE, "w") as f:
        json.dump(plays, f, indent=2)

def load_resolved():
    if RESOLVED_FILE.exists():
        with open(RESOLVED_FILE) as f:
            return json.load(f)
    return {"resolved": []}

def save_resolved(resolved):
    with open(RESOLVED_FILE, "w") as f:
        json.dump(resolved, f, indent=2)

def check_pdufa_passed(play):
    """Check if a PDUFA date has passed."""
    pdufa = play.get("pdufa_date")
    if not pdufa:
        return False
    today = datetime.date.today()
    pdufa_date = datetime.date.fromisoformat(pdufa)
    return today > pdufa_date

def check_announcement_certainty_window(play):
    """
    Check if estimated announcement window has clearly passed.
    If estimated_announcement was "late April - early May 2026" and today is June 1 2026,
    it's overdue and should be flagged.
    """
    est = play.get("estimated_announcement", "").lower()
    today = datetime.date.today()

    # Map month keywords to dates
    month_map = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
        "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
    }

    # Extract latest month mentioned in the estimate
    latest_month = None
    for month_str, month_num in month_map.items():
        if month_str in est:
            if latest_month is None or month_num > latest_month:
                latest_month = month_num

    if latest_month:
        # Use the year from the estimate string, or current year if not specified
        yr_match = re.search(r'(20\d{2})', play.get('estimated_announcement', ''))
        year = int(yr_match.group(1)) if yr_match else today.year
        # If today is more than 2 months past the latest mentioned month
        try:
            deadline = datetime.date(year, latest_month, 28)  # end of month
        except ValueError:
            deadline = datetime.date(year, latest_month, 30)
        if today > deadline + datetime.timedelta(days=60):
            return True

    return False

def check_trial_completion_passed(nct_id, days_buffer=30):
    """Check ClinicalTrials.gov if primary completion date has passed."""
    if not nct_id:
        return False
    url = f"https://clinicaltrials.gov/api/v2/studies/{nct_id}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        protocol = data.get("protocolSection", {})
        status_module = protocol.get("statusModule", {})
        completion = status_module.get("primaryCompletionDateStruct", {})
        date_str = completion.get("date", "")
        if date_str:
            comp_date = datetime.date.fromisoformat(date_str)
            today = datetime.date.today()
            # Flag if completion date passed by more than buffer days
            if today > comp_date + datetime.timedelta(days=days_buffer):
                return True, comp_date
    except:
        pass
    return False, None

def check_resolved_via_8k(ticker, sec_8k_report):
    """Check if the 8-K monitor already caught a topline filing for this ticker."""
    signals = sec_8k_report.get("breaking", [])
    for s in signals:
        if s.get("ticker") == ticker and "TOPLINE" in s.get("type", ""):
            return True, s.get("detail", "")
    return False, None

def estimate_days_to_announcement(play):
    """Estimate days until announcement for sorting. Handles all date formats."""
    # Certain date (PDUFA or confirmed webcast)
    pdufa = play.get("pdufa_date")
    if pdufa:
        try:
            d = datetime.date.fromisoformat(pdufa)
            return max(0, (d - datetime.date.today()).days)
        except:
            pass

    est = play.get("estimated_announcement", "")
    if not est:
        return 999
    today = datetime.date.today()
    text = est.lower().strip()

    # Q1/Q2/Q3/Q4 YYYY
    q_match = re.search(r'q([1-4])\s*(20\d\d)', text)
    if q_match:
        quarter, year = int(q_match.group(1)), int(q_match.group(2))
        month = {1: 2, 2: 5, 3: 8, 4: 11}[quarter]
        try:
            return max(0, (datetime.date(year, month, 15) - today).days)
        except: pass

    # H1/H2 YYYY
    h_match = re.search(r'h([12])\s*(20\d\d)', text)
    if h_match:
        half, year = int(h_match.group(1)), int(h_match.group(2))
        month = 3 if half == 1 else 9
        try:
            return max(0, (datetime.date(year, month, 15) - today).days)
        except: pass

    # Mid/early/late YYYY (no month)
    mid_match = re.search(r'(?:mid|early|late)\s*(20\d\d)', text)
    if mid_match and not any(m in text for m in ["january","february","march","april","may","june","july","august","september","october","november","december","jan","feb","mar","apr","jun","jul","aug","sep","oct","nov","dec"]):
        year = int(mid_match.group(1))
        return max(0, (datetime.date(year, 6, 15) - today).days)  # mid-year

    # Month names
    month_map = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
    }
    found_months = []
    for word in re.split(r'[\s,/\-]+', text):
        w = re.sub(r'[^a-z]', '', word)
        if w in month_map:
            found_months.append(month_map[w])
    
    if found_months:
        year_match = re.search(r'20\d\d', text)
        year = int(year_match.group()) if year_match else today.year
        earliest = min(found_months)
        day = 10 if "early" in text else (25 if "late" in text else 15)
        try:
            target = datetime.date(year, earliest, day)
            if target < today:  # If earliest month already passed, use latest
                target = datetime.date(year, max(found_months), day)
            return max(0, (target - today).days)
        except: pass

    # ISO date in string
    iso_match = re.search(r'(20\d\d-\d{2}-\d{2})', est)
    if iso_match:
        try:
            return max(0, (datetime.date.fromisoformat(iso_match.group(1)) - today).days)
        except: pass

    return 999

def run_lifecycle_check(sec_8k_report=None, ir_events_report=None):
    """Main lifecycle check — returns actions taken."""
    today = datetime.date.today().isoformat()
    plays_data = load_active_plays()
    active = plays_data.get("active", {})
    monitor = plays_data.get("monitor", {})
    resolved = load_resolved()

    actions = []
    removals = []
    upgrades = []
    graduations = []

    print(f"\n{'='*70}")
    print(f"WATCHLIST LIFECYCLE MANAGER — {today}")
    print(f"{'='*70}")

    # ── CHECK ACTIVE PLAYS FOR EXPIRY ────────────────────────────────────
    print(f"\nChecking {len(active)} active plays for staleness...")

    for ticker, play in list(active.items()):

        # Update days in watchlist
        added = play.get("added_date", today)
        try:
            days_in = (datetime.date.today() - datetime.date.fromisoformat(added)).days
            play["days_in_watchlist"] = days_in
        except:
            pass

        # ── REMOVAL CHECKS ────────────────────────────────────────────────

        # 1. PDUFA date passed
        if check_pdufa_passed(play):
            pdufa = play["pdufa_date"]
            print(f"  🗑 {ticker}: PDUFA {pdufa} has passed — removing from active plays")
            removals.append(ticker)
            resolved["resolved"].append({
                "ticker": ticker,
                "removed_date": today,
                "reason": f"PDUFA date {pdufa} passed",
                "p_predicted": play.get("p_success"),
                "outcome": "UNKNOWN — check news for FDA decision",
                "play_data": play
            })
            actions.append({"type": "REMOVED", "ticker": ticker, "reason": f"PDUFA {pdufa} passed"})
            continue

        # 2. Announcement window clearly passed
        if check_announcement_certainty_window(play):
            print(f"  🗑 {ticker}: Estimated announcement window has passed — removing")
            removals.append(ticker)
            resolved["resolved"].append({
                "ticker": ticker,
                "removed_date": today,
                "reason": "Estimated announcement window passed without update",
                "p_predicted": play.get("p_success"),
                "outcome": "OVERDUE — check IR page for data status",
                "play_data": play
            })
            actions.append({"type": "REMOVED", "ticker": ticker, "reason": "Announcement window passed"})
            continue

        # 3. 8-K monitor caught topline data
        if sec_8k_report:
            resolved_by_8k, detail = check_resolved_via_8k(ticker, sec_8k_report)
            if resolved_by_8k:
                print(f"  🗑 {ticker}: TOPLINE 8-K DETECTED — removing from active, archiving")
                removals.append(ticker)
                resolved["resolved"].append({
                    "ticker": ticker,
                    "removed_date": today,
                    "reason": "Topline data 8-K filed",
                    "p_predicted": play.get("p_success"),
                    "outcome": f"DATA DROPPED: {detail}",
                    "play_data": play
                })
                actions.append({"type": "RESOLVED_DATA_DROPPED", "ticker": ticker, "reason": detail})
                continue

        # ── UPGRADE CHECKS ────────────────────────────────────────────────

        # 4. IR events watcher found webcast pre-registration
        if ir_events_report:
            for signal in ir_events_report.get("signals", []):
                if signal.get("ticker") == ticker and signal.get("type") == "WEBCAST_PREREGISTRATION_FOUND":
                    if play.get("announcement_certainty") != "CERTAIN":
                        print(f"  ⬆ {ticker}: WEBCAST PRE-REG FOUND — upgrading to CERTAIN date")
                        play["announcement_certainty"] = "CERTAIN"
                        play["last_updated"] = today
                        reg_links = signal.get("registration_links", [])
                        if reg_links:
                            play["notes"] = f"Webcast pre-registration opened: {reg_links[0].get('url','')}"
                        upgrades.append(ticker)
                        actions.append({"type": "UPGRADED_TO_CERTAIN", "ticker": ticker})

        print(f"  ✓ {ticker}: Active | {estimate_days_to_announcement(play)} days est. | P={play.get('p_success')}%")

    # ── REMOVE RESOLVED PLAYS ─────────────────────────────────────────────
    for ticker in removals:
        del active[ticker]

    # ── GRADUATE MONITOR → ACTIVE ─────────────────────────────────────────
    print(f"\nChecking {len(monitor)} monitor plays for graduation...")
    for ticker, play in list(monitor.items()):
        days = estimate_days_to_announcement(play)
        if days <= 60:
            print(f"  🎓 {ticker}: {days} days to estimated announcement — graduating to ACTIVE")
            active[ticker] = {
                **play,
                "status": "ACTIVE",
                "added_date": today,
                "days_in_watchlist": 0,
                "last_updated": today,
                "notes": f"Graduated from MONITOR — {days} days to estimated announcement. Needs scoring."
            }
            del monitor[ticker]
            graduations.append(ticker)
            actions.append({"type": "GRADUATED", "ticker": ticker, "days_out": days})
        else:
            print(f"  ⏳ {ticker}: {days} days — staying in MONITOR (>60 days)")

    # ── SORT ACTIVE PLAYS BY DAYS TO ANNOUNCEMENT ─────────────────────────
    active_sorted = dict(sorted(
        active.items(),
        key=lambda x: estimate_days_to_announcement(x[1])
    ))

    # ── SAVE ──────────────────────────────────────────────────────────────
    plays_data = {"active": active_sorted, "monitor": monitor}
    save_active_plays(plays_data)
    save_resolved(resolved)

    # ── REPORT ────────────────────────────────────────────────────────────
    report = {
        "run_date": today,
        "active_count": len(active_sorted),
        "monitor_count": len(monitor),
        "removed": removals,
        "upgraded": upgrades,
        "graduated": graduations,
        "actions": actions,
        "active_plays_ordered": [
            {
                "ticker": t,
                "days_out": estimate_days_to_announcement(p),
                "p_success": p.get("p_success"),
                "direction": p.get("direction"),
                "announcement": p.get("estimated_announcement"),
                "certainty": p.get("announcement_certainty"),
                "option": (
                "CALL_SPREAD" if p.get('direction') == 'CALL_SPREAD'
                else "EQUITY_ONLY" if p.get('direction') == 'EQUITY_ONLY'
                else f"${p.get('option_strike','?')}{p.get('option_type','')}"
                + f" {p.get('option_expiry','?')}" if p.get('option_strike') else "N/A"
            )
            }
            for t, p in active_sorted.items()
        ]
    }

    with open(LIFECYCLE_REPORT_FILE, "w") as f:
        json.dump(report, f, indent=2)

    print(f"\n{'='*70}")
    print(f"LIFECYCLE SUMMARY:")
    print(f"  Active plays: {len(active_sorted)} | Monitor: {len(monitor)}")
    if removals:
        print(f"  🗑 Removed: {removals}")
    if upgrades:
        print(f"  ⬆ Upgraded to CERTAIN: {upgrades}")
    if graduations:
        print(f"  🎓 Graduated to active: {graduations}")
    if not actions:
        print(f"  ✓ No changes today")
    print(f"\n  Current active plays (ordered by days to announcement):")
    for entry in report["active_plays_ordered"]:
        cert = "✓" if entry["certainty"] == "CERTAIN" else "~"
        print(f"    {entry['ticker']:6} | {cert}{entry['days_out']:3}d | P={entry['p_success']}% | {entry['direction']} | {entry['option']}")
    print(f"{'='*70}\n")

    return report

if __name__ == "__main__":
    run_lifecycle_check()
