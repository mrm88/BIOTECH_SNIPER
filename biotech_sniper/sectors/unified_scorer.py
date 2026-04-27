#!/usr/bin/env python3
"""
UNIFIED DUAL-MODEL SCORER
Scores any binary catalyst — biotech, contract, AdCom — using the same
Claude Opus 4 + Gemini 2.5 Pro ensemble pipeline.

The probability prompt is sector-specific but the scoring mechanics are identical:
  - Two independent model calls
  - Average = ensemble P(success)
  - Filter: >=65% LONG | <=40% SHORT/PUT | 41-64% DROP  [CALIBRATED Apr 26 2026]
  - Model divergence >25pp → HALF SIZE flag

CALIBRATION RULES (applied Apr 26 2026 — based on 6 resolved plays):
  - Minimum P for LONG: >=65% (raised from 60% — LPCN 62% was the lesson)
  - PDUFA events (FDA decision binary): 10-25% OTM strike
  - Trial readouts (Phase 2/3 data): 5-15% OTM strike only
  - Label extensions (sBLA/sNDA on already-approved drug): ATM to 5% OTM
  - If P>=80%: recommend spread over naked call (reduce vega risk)
  - If IV > 150%: flag HALF SIZE (IV crush risk)
  - Dual-endpoint trials: use joint P for option sizing, not primary-alone P
  - Science Grade D/F on LONG play: HALF SIZE (reduce exposure)
  - Science Grade A/B + P>=80%: full size spread recommended

Also handles:
  - Finding the right option ticker + expiry + strike
  - Calculating realistic fill from live options chain
  - Computing $1k multiple on success
  - Lifecycle: adding qualifying plays to active_plays.json
"""

import json
import re
import datetime
import subprocess
from pathlib import Path

from biotech_sniper.paths import BASE_DIR

# ---------------------------------------------------------------------------
# Re-export the M2 ensemble symbols at this module's namespace so
# downstream callers (M3 selection, M5 ranker training) and validators
# (VAL-M2-044 / VAL-M2-079) can import via the historically-canonical
# location without breaking::
#
#     from biotech_sniper.sectors.unified_scorer import compute_ensemble
#
# The canonical implementation lives in :mod:`biotech_sniper.llm.ensemble`
# (per f-m2-05). This module does NOT duplicate the logic — it only
# re-exports the public surface so legacy import sites keep working.
# ---------------------------------------------------------------------------
from biotech_sniper.llm.ensemble import (  # noqa: F401 (re-exported)
    DIVERGENCE_THRESHOLD,
    EnsembleScorer,
    compute_ensemble,
    letter_grade_distance,
)

ACTIVE_PLAYS_FILE = BASE_DIR / "state/active_plays.json"
SCORING_CACHE_FILE = BASE_DIR / "state/scoring_cache.json"

def load_active_plays():
    if ACTIVE_PLAYS_FILE.exists():
        with open(ACTIVE_PLAYS_FILE) as f:
            return json.load(f)
    return {"active": {}, "monitor": {}}

def save_active_plays(plays):
    ACTIVE_PLAYS_FILE.parent.mkdir(exist_ok=True)
    with open(ACTIVE_PLAYS_FILE, "w") as f:
        json.dump(plays, f, indent=2)

def load_scoring_cache():
    if SCORING_CACHE_FILE.exists():
        with open(SCORING_CACHE_FILE) as f:
            return json.load(f)
    return {}

def save_scoring_cache(cache):
    with open(SCORING_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)

def get_third_friday(year: int, month: int) -> datetime.date:
    """Calculate the 3rd Friday of any given month/year dynamically."""
    d = datetime.date(year, month, 1)
    fridays = 0
    while True:
        if d.weekday() == 4:  # Friday
            fridays += 1
            if fridays == 3:
                return d
        d += datetime.timedelta(days=1)


def get_option_expiry_for_announcement(announcement_date_str, certainty="ESTIMATED"):
    """
    Given an announcement date/range, return the correct option expiry.
    Standard monthly expiries = 3rd Friday of each month.
    FULLY DYNAMIC — works for any year, no hardcoded expiry tables.
    """
    today = datetime.date.today()
    
    # Parse announcement date — handles ISO, month names, Q1-Q4, H1/H2
    ann_date = None
    if announcement_date_str:
        text = announcement_date_str.lower().strip()
        try:
            ann_date = datetime.date.fromisoformat(announcement_date_str)
        except:
            # Q1/Q2/Q3/Q4 YYYY format
            q_match = re.search(r'q([1-4])\s*(20\d\d)', text)
            if q_match:
                quarter = int(q_match.group(1))
                year    = int(q_match.group(2))
                month   = {1: 2, 2: 5, 3: 8, 4: 11}[quarter]  # mid-quarter
                ann_date = datetime.date(year, month, 15)

            # H1/H2 YYYY format
            if not ann_date:
                h_match = re.search(r'h([12])\s*(20\d\d)', text)
                if h_match:
                    half  = int(h_match.group(1))
                    year  = int(h_match.group(2))
                    month = 3 if half == 1 else 9  # mid-half
                    ann_date = datetime.date(year, month, 15)

            # Month name(s) YYYY — e.g. "April 30 2026", "May - June 2026"
            if not ann_date:
                month_map = {
                    "january": 1, "february": 2, "march": 3, "april": 4,
                    "may": 5, "june": 6, "july": 7, "august": 8,
                    "september": 9, "october": 10, "november": 11, "december": 12
                }
                year_match = re.search(r'20\d\d', text)
                year = int(year_match.group()) if year_match else today.year
                found_months = []
                for mname, mnum in month_map.items():
                    if mname in text:
                        found_months.append(mnum)
                if found_months:
                    day_match = re.search(r'\b([12]?\d)\b', text)
                    day = int(day_match.group(1)) if day_match and int(day_match.group(1)) <= 31 else 15
                    try:
                        ann_date = datetime.date(year, min(found_months), day)
                    except:
                        ann_date = datetime.date(year, min(found_months), 15)

    if not ann_date:
        # Default: 2 months from now
        m = today.month + 2
        y = today.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        ann_date = datetime.date(y, m, 15)
    
    # For CERTAIN dates: use nearest expiry with min 2-3 trading day buffer
    # For ESTIMATED dates: add 3-4 week buffer on top of estimate
    buffer_days = 3 if certainty == "CERTAIN" else 21  # 3 weeks buffer for uncertain timing
    target = ann_date + datetime.timedelta(days=buffer_days)
    
    # Search up to 18 months forward dynamically — no hardcoded table
    search_start = max(today, target)
    year, month = search_start.year, search_start.month
    for _ in range(18):  # max 18 months
        tf = get_third_friday(year, month)
        if tf >= target and tf > today:
            return tf.isoformat(), (ann_date - today).days
        # Advance to next month
        month += 1
        if month > 12:
            month = 1
            year += 1
    
    # Fallback: 6 months out from today
    m = today.month + 6
    y = today.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    return get_third_friday(y, m).isoformat(), 180

# ── CATALYST TYPE DETECTION ──────────────────────────────────────────────────

DEFAULT_OTM_BY_CATALYST = {
    "PDUFA":      (0.10, 0.25),  # 10-25% OTM for FDA approval binary (large gap-up expected)
    "READOUT":    (0.05, 0.15),  # 5-15% OTM for Phase 2/3 data (magnitude variable)
    "LABEL_EXT":  (0.00, 0.05),  # ATM to 5% OTM for sBLA/sNDA label extension (modest move)
    "ADCOM":      (0.10, 0.20),  # 10-20% OTM for AdCom vote (can gap large on yes)
    "CONTRACT":   (0.15, 0.30),  # 15-30% OTM for contract awards (usually modest)
    "DEFAULT":    (0.10, 0.20),  # Fallback if type unknown
}


def detect_catalyst_type(notes: str, drug_or_topic: str = "", indication: str = "",
                          sector: str = "BIOTECH") -> str:
    """
    Detect whether the catalyst is a PDUFA, READOUT, LABEL_EXT, ADCOM, or CONTRACT.
    Returns a catalyst_type string used to select the correct OTM strike range.

    CALIBRATION NOTE (Apr 26 2026):
      Label extensions (sNDA/sBLA) produce only 5-15% stock moves on approval.
      Trial readouts are binary but magnitude is limited — 10-25% typical.
      PDUFA on first-ever approval can gap 30-60%.
      Using the wrong category was the TVTX $35C lesson.
    """
    if sector == "CONTRACT":
        return "CONTRACT"
    if sector == "ADCOM":
        return "ADCOM"

    combined = (notes + " " + drug_or_topic + " " + indication).lower()

    # Label extension signals (drug already approved, adding new indication)
    label_ext_signals = ["label extension", "snda", "sbla", "supplemental nda", "supplemental bla",
                         "label expansion", "new indication", "already approved", "approved for"]
    # PDUFA = FDA action on a BLA/NDA filing
    pdufa_signals = ["pdufa", "nda filing", "bla filing", "fda decision", "fda action date",
                     "fda approval", "pdufa date", "nda", "bla"]
    # Trial readout signals
    readout_signals = ["phase 3", "phase 2", "phase 2b", "phase 3a", "data readout", "trial results",
                       "primary endpoint", "pivotal trial", "pivotal data", "clinical readout",
                       "topline", "top-line", "data drop"]

    # Check label extension first (most specific — subset of PDUFA territory)
    if any(sig in combined for sig in label_ext_signals):
        return "LABEL_EXT"
    if any(sig in combined for sig in pdufa_signals):
        return "PDUFA"
    if any(sig in combined for sig in readout_signals):
        return "READOUT"
    return "DEFAULT"


def get_otm_range_for_catalyst(catalyst_type: str) -> tuple:
    """Returns (min_otm_pct, max_otm_pct) for the given catalyst type."""
    return DEFAULT_OTM_BY_CATALYST.get(catalyst_type, DEFAULT_OTM_BY_CATALYST["DEFAULT"])


def calculate_otm_strike(stock_price, direction, otm_pct=0.15,
                          catalyst_type: str = "DEFAULT") -> float:
    """
    Calculate OTM strike based on catalyst type.
    
    CALIBRATION (Apr 26 2026): OTM % is now catalyst-type-specific.
      PDUFA: 10-25% OTM (use midpoint 17.5%)
      READOUT: 5-15% OTM (use midpoint 10%)
      LABEL_EXT: 0-5% OTM (use 2.5% = nearly ATM)
      ADCOM: 10-20% OTM (use midpoint 15%)
      Old default 35% OTM was too aggressive for most non-PDUFA plays.
    """
    # Use catalyst-type-specific OTM range if not explicitly provided
    if catalyst_type != "DEFAULT" or otm_pct == 0.15:  # default sentinel
        min_otm, max_otm = get_otm_range_for_catalyst(catalyst_type)
        otm_pct = (min_otm + max_otm) / 2  # Use midpoint of the range

    if direction in ("LONG_CALLS", "CALL_SPREAD"):
        raw_strike = stock_price * (1 + otm_pct)
    else:  # LONG_PUTS
        raw_strike = stock_price * (1 - otm_pct)
    
    # Round to standard increments
    if raw_strike < 10:
        return round(raw_strike * 2) / 2  # $0.50 increments
    elif raw_strike < 25:
        return round(raw_strike)  # $1 increments
    elif raw_strike < 100:
        return round(raw_strike / 2.5) * 2.5  # $2.50 increments
    elif raw_strike < 200:
        return round(raw_strike / 5) * 5  # $5 increments
    else:
        return round(raw_strike / 10) * 10  # $10 increments


def check_iv_crush_risk(iv_pct: float) -> dict:
    """
    Check if implied volatility is high enough to trigger IV crush warning.

    CALIBRATION NOTE (Apr 26 2026):
      IDYA was trading at IV=188%. Even though the drug worked (+7.6% stock),
      the option lost 91% because IV collapsed from 188% to ~40% post-announcement.
      Rule: if IV > 150%, flag HALF SIZE and add crush warning to the card.
    """
    if iv_pct is None:
        return {"crush_risk": False, "warning": None, "half_size": False}

    if iv_pct > 200:
        return {
            "crush_risk": True,
            "warning": f"EXTREME IV CRUSH RISK: IV={iv_pct:.0f}%. Option likely loses 50-80% even if stock rises. Use HALF SIZE max.",
            "half_size": True,
            "severity": "EXTREME",
        }
    elif iv_pct > 150:
        return {
            "crush_risk": True,
            "warning": f"HIGH IV CRUSH RISK: IV={iv_pct:.0f}%. Option may lose 30-60% post-announcement even if directionally correct. Reduce size.",
            "half_size": True,
            "severity": "HIGH",
        }
    elif iv_pct > 100:
        return {
            "crush_risk": True,
            "warning": f"MODERATE IV CRUSH RISK: IV={iv_pct:.0f}%. Size down slightly — IV expansion already priced in.",
            "half_size": False,
            "severity": "MODERATE",
        }
    else:
        return {"crush_risk": False, "warning": None, "half_size": False}


def should_use_spread(p_success: float, catalyst_type: str, science_grade: str = "C") -> dict:
    """
    Recommend spread vs naked call based on calibration rules.

    CALIBRATION NOTE (Apr 26 2026):
      ARGX $800/$850 spread worked better than naked calls at P=87%.
      When probability is very high, a spread caps the upside but massively
      reduces vega risk. The binary gap still profits, just with a ceiling.

    Returns recommendation dict.
    """
    use_spread = False
    reason = None

    if p_success >= 80 and catalyst_type in ("PDUFA", "ADCOM"):
        use_spread = True
        reason = f"P={p_success}% is high-conviction for a PDUFA/AdCom binary. Spread reduces vega exposure while preserving gap-up upside."
    elif p_success >= 85 and science_grade in ("A", "B"):
        use_spread = True
        reason = f"P={p_success}% + Grade {science_grade} science = highest conviction. Use spread to protect against IV crush."

    return {
        "use_spread": use_spread,
        "reason": reason,
        "spread_type": "CALL_SPREAD" if use_spread else None,
    }


def apply_science_size_modifier(science_grade: str, direction: str,
                                 p_success: float) -> dict:
    """
    Adjust position size based on science grade.

    CALIBRATION NOTE (Apr 26 2026):
      Grade D/F LONG plays have weak science. The model direction may be correct
      but the thesis is shaky. We want half size to manage risk.
      Grade F LONG plays additionally get a MISMATCH ALERT in the email.

    Returns:
      size_multiplier: 1.0 = full, 0.5 = half
      half_size: bool
      mismatch_alert: bool (Grade D/F on a LONG play = flag for user)
    """
    if not science_grade:
        return {"size_multiplier": 1.0, "half_size": False, "mismatch_alert": False}

    is_naked_long = direction == "LONG_CALLS"    # Worst exposure: unhedged
    is_spread = direction == "CALL_SPREAD"         # Already hedged — less alarming
    is_long = direction in ("LONG_CALLS", "CALL_SPREAD")
    is_short = direction == "LONG_PUTS"

    if science_grade == "F":
        if is_naked_long:
            return {
                "size_multiplier": 0.5,
                "half_size": True,
                "mismatch_alert": True,  # Full MISMATCH ALERT for naked long
                "note": "Grade F science on LONG CALLS — HALF SIZE. Science fundamentally flawed. High conviction SHORT territory instead.",
            }
        elif is_spread:
            return {
                "size_multiplier": 0.75,
                "half_size": False,
                "mismatch_alert": True,  # Still flag, but spread already mitigates
                "note": "Grade F science on CALL SPREAD — spread structure is appropriate. Note science is fundamentally flawed.",
            }
        else:  # SHORT / PUT on Grade F = highest conviction
            return {
                "size_multiplier": 1.0,
                "half_size": False,
                "mismatch_alert": False,
                "note": "Grade F science + PUT = highest conviction setup (Shkreli strategy).",
            }
    elif science_grade == "D":
        if is_naked_long:
            return {
                "size_multiplier": 0.5,
                "half_size": True,
                "mismatch_alert": True,
                "note": "Grade D science on LONG CALLS — HALF SIZE. Multiple red flags. Science is weak.",
            }
        elif is_spread:
            return {
                "size_multiplier": 1.0,
                "half_size": False,
                "mismatch_alert": False,  # Spread already hedges; no alarm needed
                "note": "Grade D science on CALL SPREAD — spread structure appropriately manages risk.",
            }
        else:
            return {
                "size_multiplier": 1.0,
                "half_size": False,
                "mismatch_alert": False,
                "note": "Grade D science + PUT = strong conviction setup.",
            }
    elif science_grade == "A":
        if is_long and p_success >= 80:
            return {
                "size_multiplier": 1.0,
                "half_size": False,
                "mismatch_alert": False,
                "note": "Grade A science + high P = premium setup. Consider spread to lock in conviction.",
            }
    # Grades B and C: normal sizing
    return {"size_multiplier": 1.0, "half_size": False, "mismatch_alert": False}


def compute_joint_probability(p_primary: float, p_secondary: float,
                               correlation: float = 0.7) -> float:
    """
    Compute joint probability for dual-endpoint trials.

    CALIBRATION NOTE (Apr 26 2026):
      IDYA had P(PFS)=90% but P(PFS+OS)=~42%. We used P=96% (primary alone).
      The option was sized as if the HIGHER bar was 96% likely.
      It was not. The market priced in joint probability.
      
      Formula: P(joint) = P(primary) * P(secondary | primary met)
      With correlation ~0.7 for co-primary endpoints in the same trial.
      
    Args:
      p_primary: P(primary endpoint met) as 0-100
      p_secondary: P(secondary endpoint met) as 0-100
      correlation: Pearson correlation between endpoints (default 0.7 for related endpoints)
    """
    # Bayesian approach: P(A and B) = P(A) * P(B|A)
    # P(B|A) = P(B) + correlation * sqrt(P(B)*(1-P(B))/P(A)*(1-P(A))) ... simplified:
    # For correlated endpoints: P(joint) is between P(primary)*P(secondary) and min(P(primary), P(secondary))
    p1 = p_primary / 100
    p2 = p_secondary / 100
    # Weighted between independent and fully correlated
    p_independent = p1 * p2
    p_correlated = min(p1, p2)
    joint = p_independent * (1 - correlation) + p_correlated * correlation
    return round(joint * 100, 1)


def classify_play_direction(p_success):
    """Determine trade direction from probability.
    
    CALIBRATION NOTE (Apr 26 2026):
      Raised LONG threshold from 60% to 65%.
      LPCN at 62% showed that 60-64% is too close to a coin flip for options.
      Puts remain at <=40% (that threshold has been well-calibrated).
    """
    if p_success >= 65:
        return "LONG_CALLS"
    elif p_success <= 40:
        return "LONG_PUTS"
    else:
        return "FILTERED_OUT"


def generate_order_ladder(ticker, expiry, strike, option_type, fill_price, 
                           total_risk=1000, half_size=False):
    """Generate decoy + ladder limit orders."""
    if half_size:
        total_risk = total_risk // 2
    
    contracts = max(1, int(total_risk / (fill_price * 100)))
    
    orders = []
    # 2 decoy probes
    orders.append(f"DECOY 1:  BUY TO OPEN {ticker} {expiry} ${strike}{option_type} | LIMIT ${fill_price:.2f}      | 1 contract  | GTC")
    orders.append(f"DECOY 2:  BUY TO OPEN {ticker} {expiry} ${strike}{option_type} | LIMIT ${fill_price-0.03:.2f}  | 1 contract  | GTC")
    
    # 4-6 ladder levels
    bulk_contracts = max(1, contracts // 4)
    for i, offset in enumerate([0.05, 0.10, 0.15, 0.20], 1):
        price = max(0.05, fill_price - offset)
        orders.append(f"LADDER {i}: BUY TO OPEN {ticker} {expiry} ${strike}{option_type} | LIMIT ${price:.2f}      | {bulk_contracts} contract(s) | GTC")
    
    total_invested = contracts * fill_price * 100
    return orders, contracts, total_invested

def build_play_entry(ticker, sector, direction, p_success, p_source,
                     drug_or_topic, indication, announcement_date,
                     announcement_certainty, option_expiry, option_strike,
                     option_type, notes="", model_divergence=False):
    """Create a standardized play entry for active_plays.json."""
    today = datetime.date.today().isoformat()
    
    return {
        "ticker": ticker,
        "sector": sector,  # "BIOTECH" | "CONTRACT" | "ADCOM"
        "drug_or_topic": drug_or_topic,
        "indication": indication,
        "p_success": p_success,
        "p_source": p_source,
        "direction": direction,
        "announcement_certainty": announcement_certainty,
        "estimated_announcement": announcement_date,
        "pdufa_date": announcement_date if announcement_certainty == "CERTAIN" else None,
        "option_strike": option_strike,
        "option_expiry": option_expiry,
        "option_type": option_type,
        "half_size": model_divergence,
        "added_date": today,
        "last_updated": today,
        "status": "ACTIVE",
        "days_in_watchlist": 0,
        "notes": notes
    }

def add_play_if_qualifies(ticker, sector, p_success, p_source, direction,
                           drug_or_topic, indication, announcement_date,
                           announcement_certainty, stock_price=None,
                           notes="", model_divergence=False):
    """
    Add a new play to active_plays.json if it qualifies (>=65% LONG or <=40% SHORT/PUT).
    [CALIBRATED Apr 26 2026: raised from >=60% to >=65% for LONG]
    Returns True if added, False if filtered or already exists.
    """
    if direction == "FILTERED_OUT":
        return False
    
    plays = load_active_plays()
    active = plays.get("active", {})
    
    # Already tracked?
    if ticker in active:
        # Update probability if changed
        if abs(active[ticker].get("p_success", 0) - p_success) > 5:
            active[ticker]["p_success"] = p_success
            active[ticker]["last_updated"] = datetime.date.today().isoformat()
            active[ticker]["notes"] += f" | P updated to {p_success}% on {datetime.date.today()}"
            save_active_plays({"active": active, "monitor": plays.get("monitor", {})})
            print(f"  📊 {ticker}: Updated P(success) to {p_success}%")
        return False
    
    # Detect catalyst type for proper strike selection (CALIBRATED Apr 26 2026)
    catalyst_type = detect_catalyst_type(notes, drug_or_topic, indication, sector)
    
    # Calculate expiry and strike
    expiry, days_out = get_option_expiry_for_announcement(announcement_date, announcement_certainty)
    strike = calculate_otm_strike(stock_price or 20, direction,
                                   catalyst_type=catalyst_type) if stock_price else None
    option_type = "C" if direction in ("LONG_CALLS", "CALL_SPREAD") else "P"
    
    # Check if spread is recommended (P>=80% PDUFA/AdCom)
    spread_rec = should_use_spread(p_success, catalyst_type)
    if spread_rec["use_spread"] and direction == "LONG_CALLS":
        direction = "CALL_SPREAD"
        spread_note = f" | SPREAD RECOMMENDED: {spread_rec['reason']}"
    else:
        spread_note = ""
    
    play = build_play_entry(
        ticker=ticker, sector=sector, direction=direction,
        p_success=p_success, p_source=p_source,
        drug_or_topic=drug_or_topic, indication=indication,
        announcement_date=announcement_date,
        announcement_certainty=announcement_certainty,
        option_expiry=expiry, option_strike=strike,
        option_type=option_type, notes=notes + spread_note,
        model_divergence=model_divergence
    )
    play["catalyst_type"] = catalyst_type
    
    active[ticker] = play
    save_active_plays({"active": active, "monitor": plays.get("monitor", {})})
    
    print(f"  ADDED to active plays: {ticker} ({sector}) | P={p_success}% | {direction} | expiry {expiry} | catalyst_type={catalyst_type}")
    return True

def _read_scoring_cache_row(ticker: str, as_of_date: str):
    """Return the persisted ``scoring_cache`` row for ``(ticker, as_of_date)``.

    Returns ``None`` when the SQLite database does not exist yet (fresh
    checkouts), when the row is absent, or when the connection fails
    for any reason. The helper is intentionally forgiving so that
    callers can treat a missing cache as a cold-start and proceed to
    score via the ensemble.
    """
    # Imports are kept local so importing this module does not require
    # the SQLite layer to be set up yet (e.g. during smoke imports on
    # a fresh checkout where ``data/alpha_sniper.db`` is absent).
    from biotech_sniper import db as _db
    from biotech_sniper.paths import DATA_DIR

    db_path = DATA_DIR / "alpha_sniper.db"
    if not db_path.exists():
        return None
    try:
        conn = _db.connect(db_path)
    except Exception:
        return None
    try:
        try:
            _db.run_migrations(conn)
        except Exception:
            # If migrations cannot be applied (schema lock, mid-write,
            # permissions), treat the cache as a miss rather than
            # crashing the scorer.
            return None
        try:
            row = conn.execute(
                "SELECT claude_probability, gemini_probability, "
                "ensemble_score, divergence_flag "
                "FROM scoring_cache WHERE ticker=? AND as_of_date=?",
                (ticker, as_of_date),
            ).fetchone()
        except Exception:
            return None
        return dict(row) if row is not None else None
    finally:
        conn.close()


def score_candidate_with_models(
    prompt,
    ticker,
    cache_key=None,
    *,
    as_of_date=None,
    fast_context=None,
    science_profile=None,
    full_context=None,
    scorer=None,
):
    """Score a binary catalyst via the M2 :class:`EnsembleScorer`.

    Replaces the legacy cron-agent file-based stub (which left the
    actual model calls to the external ``cron-agent`` and read the
    results from per-ticker text files dropped under the legacy
    ``scores/`` directory) with a direct in-process call to the
    :class:`biotech_sniper.llm.ensemble.EnsembleScorer`. The ensemble
    persists every successful score into the SQLite ``scoring_cache``
    table keyed on ``(ticker, as_of_date)``; re-running on the same
    date is idempotent (UPSERT on the unique index).

    Returns:
        ``(claude_probability, gemini_probability, ensemble_score,
        divergence_flag)``. Each numeric component is a float in
        ``[0, 1]`` (or ``None`` when the corresponding provider is
        disabled / unavailable). ``divergence_flag`` is a bool.

    Args:
        prompt: Free-form prompt text. Forwarded into the fast and
            full-context payloads when callers do not supply
            structured contexts.
        ticker: Ticker the candidate is being scored against.
        cache_key: Legacy parameter retained for compatibility; when
            present, it is forwarded into the contexts so callers can
            tag a logical scoring round.
        as_of_date: Optional ISO date used for the SQLite cache
            primary key. Defaults to ``datetime.date.today()``.
        fast_context, science_profile, full_context: Optional
            structured payloads passed to the per-tier clients. When
            omitted the helper falls back to ``{"prompt": prompt}``.
        scorer: Optional pre-built :class:`EnsembleScorer` (used by
            tests to inject fake clients). When ``None``, the function
            calls :meth:`EnsembleScorer.from_config` which gracefully
            skips providers without API keys.
    """
    today = as_of_date or datetime.date.today().isoformat()

    cached = _read_scoring_cache_row(ticker, today)
    if cached is not None:
        print(f"  📋 scoring_cache hit for {ticker} ({today})")
        return (
            cached.get("claude_probability"),
            cached.get("gemini_probability"),
            cached.get("ensemble_score"),
            bool(cached.get("divergence_flag")),
        )

    if scorer is None:
        scorer = EnsembleScorer.from_config()

    candidate_payload = {
        "ticker": ticker,
        "as_of_date": today,
        "fast_context": fast_context
        or {"prompt": prompt or "", "cache_key": cache_key},
        "science_profile": science_profile or {},
        "full_context": full_context
        or {"prompt": prompt or "", "cache_key": cache_key},
    }
    result = scorer.score(candidate_payload)
    return (
        result.get("claude_probability"),
        result.get("gemini_probability"),
        result.get("ensemble_score"),
        bool(result.get("divergence_flag")),
    )

def format_scoring_instructions(signals_needing_scoring):
    """
    Format instructions for the cron agent to score new candidates.
    Returns a structured prompt for dual-model scoring.
    """
    instructions = []
    for signal in signals_needing_scoring:
        ticker = signal.get("ticker")
        sector = signal.get("sector")
        prompt = signal.get("scoring_prompt", "")
        
        instructions.append(f"""
SCORE THIS CANDIDATE — {ticker} ({sector})
Run BOTH models in parallel:
  Model A: claude_opus_4_6
  Model B: gemini_3_1_pro

Prompt for each (identical):
{prompt[:3000]}

Extract from each response: P(success) as a single integer percentage.
Average = ensemble. 
If ensemble >= 65: direction = LONG_CALLS  [CALIBRATED Apr 26 2026: raised from 60%]
If ensemble <= 40: direction = LONG_PUTS  
If 41-64: DROP (do not add to active plays)
If |claude - gemini| > 25pp AND qualifies: flag as HALF SIZE

After scoring, call add_play_if_qualifies() with the results.
Save P scores to: <BASE_DIR>/state/scoring_cache.json
""")
    
    return "\n".join(instructions)

if __name__ == "__main__":
    print("Unified scorer loaded. Use via master_unified_run.py")
    print(f"Active plays: {len(load_active_plays().get('active', {}))}")
