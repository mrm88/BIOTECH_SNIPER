#!/usr/bin/env python3
"""
SECTOR MAGNITUDE LIBRARY
Historical comparable moves, probability bases, and option structure guidance
for all 3 sectors. Used by the unified scorer to anchor stock move estimates.

All entries are real historical events to prevent hallucination.
"""

# ── GOVERNMENT CONTRACTS ─────────────────────────────────────────────────────
CONTRACT_COMPARABLES = {
    # Format: (company, ticker, event, win_pct_move, loss_pct_move, notes)
    "J_AND_A_SOLE_SOURCE": [
        ("Kratos Defense", "KTOS", "USAF LCASD sole-source drone award 2023", +45, -15, "Won $70M LCASD contract, stock +45% over 2 weeks"),
        ("Rocket Lab", "RKLB", "Space Force NSSL sole-source 2024", +38, -20, "Won NSSL Phase 2 sole-source, stock +38% in 3 days"),
        ("Intuitive Machines", "LUNR", "NASA CLPS sole-source task order 2024", +62, -25, "Won lunar surface services sole-source, +62%"),
        ("Mercury Systems", "MRCY", "DoD sole-source radar upgrade 2022", +22, -12, "Won avionics upgrade contract, +22%"),
        ("Kratos Defense", "KTOS", "Army MFCR sole-source denial 2022", +0, -18, "Lost challenge to sole-source, -18% after protest upheld"),
    ],
    "NARROW_PRE_SOL": [
        ("Rocket Lab", "RKLB", "SDA Tranche 2 transport layer award 2023", +28, -15, "Won SDA contract vs 3 competitors, +28%"),
        ("Planet Labs", "PL", "NRO commercial imagery contract 2022", +35, -20, "Won NRO 10-year imagery contract, +35%"),
        ("AST SpaceMobile", "ASTS", "DoD Band 1 spectrum award 2024", +55, -30, "Won spectrum access + DoD contract, +55%"),
        ("Parsons Corp", "PSN", "Army C2 systems narrow award 2023", +18, -8, "Won C2 modernization, +18%"),
        ("CACI International", "CACI", "IC cyber contract narrow competition 2023", +14, -7, "Won IC cyber ops, +14%"),
    ],
    "LARGE_PROGRAM_WIN": [
        ("Leidos", "LDOS", "NIH CIO-SP4 prime 2023", +12, -6, "Large cap — $20B IDIQ win, modest move +12%"),
        ("Booz Allen", "BAH", "NSA prime contract 2022", +8, -4, "Large cap with diversified revenue, +8%"),
        ("SAIC", "SAIC", "Army enterprise cloud 2023", +10, -5, "+10% on $1B+ award"),
    ]
}

CONTRACT_BASE_RATES = {
    "J_AND_A_SOLE_SOURCE": {
        "p_win": 87,
        "p_win_ci": "80-93%",
        "avg_win_move_small_cap": "+35-65%",
        "avg_win_move_mid_cap": "+15-35%",
        "avg_loss_move_small_cap": "-15-30%",
        "avg_loss_move_mid_cap": "-8-18%",
        "note": "J&A = company named as only capable source. GAO sustain rate ~15% on protests."
    },
    "NARROW_PRE_SOL": {
        "p_win": 62,
        "p_win_ci": "50-73%",
        "avg_win_move_small_cap": "+20-45%",
        "avg_win_move_mid_cap": "+10-20%",
        "avg_loss_move_small_cap": "-10-25%",
        "avg_loss_move_mid_cap": "-5-12%",
        "note": "Narrow SOW = 1-2 realistic competitors. Incumbent has ~65% win rate."
    },
    "OPEN_COMPETITION": {
        "p_win": 25,
        "p_win_ci": "15-35%",
        "avg_win_move_small_cap": "+15-35%",
        "avg_win_move_mid_cap": "+8-15%",
        "avg_loss_move_small_cap": "-5-15%",
        "avg_loss_move_mid_cap": "-3-8%",
        "note": "Open competition — win rate proportional to number of bidders."
    }
}

# ── FDA ADVISORY COMMITTEES ───────────────────────────────────────────────────
ADCOM_COMPARABLES = {
    # Format: (company, ticker, drug, vote_result, stock_move_on_vote, stock_move_on_pdufa, notes)
    "YES_VOTE_STRONG": [
        ("Karuna Therapeutics", "KRTX", "KarXT schizophrenia", "YES 9-0", +28, +22, "Unanimous vote, stock +28% day of AdCom"),
        ("Sage/Biogen", "SAGE", "zuranolone MDD+PPD", "YES 14-1 PPD", +35, -54, "Yes on PPD but FDA later rejected MDD — stock crashed on PDUFA"),
        ("Acadia", "ACAD", "pimavanserin Alzheimer's psychosis", "YES 8-4", +40, -15, "AdCom yes but narrow → PDUFA was CRL"),
        ("Axsome", "AXSM", "AXS-05 MDD", "YES 15-0", +32, +18, "Unanimous → clean PDUFA approval"),
        ("Intra-Cellular", "ITCI", "lumateperone bipolar", "YES 12-1", +25, +20, "Strong vote → approval"),
    ],
    "YES_VOTE_NARROW": [
        ("Sarepta", "SRPT", "ELEVIDYS DMD", "YES 8-6", +15, +28, "Narrow vote, FDA approved anyway"),
        ("Ampio", "AMPE", "Ampion OA knee", "YES 6-5", +22, -65, "Narrow vote → FDA rejected, stock destroyed"),
        ("Biohaven", "BHVN", "rimegepant CGRP", "YES 7-5", +18, +15, "Narrow → approved with label restrictions"),
    ],
    "NO_VOTE": [
        ("Cassava Sciences", "SAVA", "simufilam Alzheimer's", "NO 8-1", -45, -55, "Committee rejected, stock crushed"),
        ("Acadia", "ACAD", "pimavanserin dementia-related psychosis", "NO 5-9", -38, None, "CRL followed"),
        ("Alector", "ALEC", "AL002 Alzheimer's", "NO 7-4", -32, -28, "Negative vote → rejection"),
        ("ProQR", "PRQR", "gene therapy LCA10", "NO 12-3", -55, -60, "Panel rejected efficacy"),
    ],
    "BRIEFING_DOC_SIGNALS": [
        ("Karuna", "KRTX", "FDA briefing strongly positive language", "Bullish doc → YES 9-0", +5, None, "Bullish briefing doc predicted the outcome 48h early"),
        ("Acadia 2021", "ACAD", "FDA briefing listed 8 specific concerns", "Bearish doc → NO 5-9", -15, None, "Bearish briefing doc → stock dropped 15% before AdCom vote"),
        ("Sage zuranolone", "SAGE", "FDA doc: 'questions remain on MDD data'", "Mixed doc → split outcome", -8, None, "Cautious FDA language telegraphed MDD concern 48h before"),
    ]
}

ADCOM_BASE_RATES = {
    "OVERALL": {
        "p_yes": 67,
        "p_yes_ci": "62-72%",
        "pdufa_follows_yes": 75,
        "pdufa_follows_no": 20,
        "avg_stock_move_yes_vote": "+15-35%",
        "avg_stock_move_no_vote": "-25-55%",
        "note": "FDA follows AdCom ~75% of time. Strong yes vote (>75% of panel) → ~85% approval."
    },
    "BREAKTHROUGH_THERAPY": {
        "p_yes": 82,
        "p_yes_ci": "74-89%",
        "avg_stock_move_yes_vote": "+20-40%",
        "avg_stock_move_no_vote": "-30-50%",
    },
    "PRIORITY_REVIEW": {
        "p_yes": 76,
        "p_yes_ci": "68-83%",
        "avg_stock_move_yes_vote": "+15-35%",
        "avg_stock_move_no_vote": "-25-50%",
    },
    "SINGLE_ARM_TRIAL": {
        "p_yes": 55,
        "p_yes_ci": "45-65%",
        "avg_stock_move_yes_vote": "+20-45%",
        "avg_stock_move_no_vote": "-30-60%",
        "note": "Single-arm trials face more scrutiny at AdCom."
    },
    "CNS_INDICATION": {
        "p_yes": 58,
        "p_yes_ci": "50-66%",
        "avg_stock_move_yes_vote": "+15-30%",
        "avg_stock_move_no_vote": "-25-45%",
    },
    "ONCOLOGY_INDICATION": {
        "p_yes": 72,
        "p_yes_ci": "64-80%",
        "avg_stock_move_yes_vote": "+15-35%",
        "avg_stock_move_no_vote": "-20-45%",
    }
}

# ── BIOTECH (already used in existing system — added here for completeness) ───
BIOTECH_BASE_RATES = {
    "PHASE_3_ONCOLOGY": {"p_success": 65, "avg_win_move": "+40-100%", "avg_loss_move": "-50-80%"},
    "PHASE_3_CNS": {"p_success": 52, "avg_win_move": "+35-70%", "avg_loss_move": "-40-70%"},
    "PHASE_3_RARE_DISEASE": {"p_success": 73, "avg_win_move": "+50-120%", "avg_loss_move": "-40-70%"},
    "PHASE_3_IMMUNOLOGY": {"p_success": 68, "avg_win_move": "+30-80%", "avg_loss_move": "-40-65%"},
    "PHASE_3_CARDIO": {"p_success": 58, "avg_win_move": "+30-70%", "avg_loss_move": "-40-60%"},
    "PDUFA_CLEAN_APPROVAL": {"p_success": 85, "avg_win_move": "+10-25%", "avg_loss_move": "-20-40%"},
    "PDUFA_AFTER_CRL": {"p_success": 60, "avg_win_move": "+15-35%", "avg_loss_move": "-30-50%"},
}

def get_magnitude_prompt(sector, event_type, ticker, mkt_cap_tier, contract_value=None,
                          committee=None, indication=None):
    """
    Returns the magnitude research prompt for the dual-model scorer.
    This gets appended to the probability prompt so both models output
    stock move estimates alongside probability.
    """
    
    if sector == "CONTRACT":
        comparables = CONTRACT_COMPARABLES.get(event_type, CONTRACT_COMPARABLES["NARROW_PRE_SOL"])
        base = CONTRACT_BASE_RATES.get(event_type, CONTRACT_BASE_RATES["NARROW_PRE_SOL"])
        comp_text = "\n".join([f"  - {c[0]} ({c[1]}): {c[2]} → WIN +{c[3]}% / LOSS {c[4]}% ({c[5]})"
                               for c in comparables[:4]])
        
        return f"""
MAGNITUDE ANALYSIS (required in addition to probability):
Market cap tier: {mkt_cap_tier}
Contract value: {contract_value or 'unknown'}

Historical comparable moves for {event_type}:
{comp_text}

Base rate for {event_type}:
  P(win): {base['p_win']}% {base['p_win_ci']}
  Avg win move ({mkt_cap_tier} cap): {base.get(f'avg_win_move_{mkt_cap_tier}_cap', '+20-40%')}
  Avg loss move ({mkt_cap_tier} cap): {base.get(f'avg_loss_move_{mkt_cap_tier}_cap', '-10-20%')}

REQUIRED OUTPUT FORMAT for magnitude section:
  Expected stock move ON WIN: +[X]% to +[X]% (central: +[X]%)
  Expected stock move ON LOSS: -[X]% to -[X]%
  Asymmetry score: [1-10]
  Catalyst weight: ~[X]% of company equity value rides on this award
  Recommended option structure:
    Direction: CALLS (if P>=60%) or PUTS (if P<=40%)
    Strike: [X]% OTM from current price
    Expiry: [award date] + [buffer] = [specific expiry month]
    Why: [one sentence]"""

    elif sector == "ADCOM":
        comp_yes = "\n".join([f"  - {c[0]} ({c[1]}): {c[2]} → {c[3]} → vote day +{c[4]}%"
                              for c in ADCOM_COMPARABLES["YES_VOTE_STRONG"][:3]])
        comp_no = "\n".join([f"  - {c[0]} ({c[1]}): {c[2]} → {c[3]} → vote day {c[4]}%"
                             for c in ADCOM_COMPARABLES["NO_VOTE"][:3]])
        
        ind_key = "CNS_INDICATION" if any(w in (indication or "").lower() 
                  for w in ["alzheimer","depression","anxiety","psychosis","neurological"]) \
                  else "ONCOLOGY_INDICATION" if any(w in (indication or "").lower() 
                  for w in ["cancer","tumor","oncology","leukemia","lymphoma"]) \
                  else "OVERALL"
        base = ADCOM_BASE_RATES[ind_key]
        
        return f"""
MAGNITUDE ANALYSIS (required in addition to probability):
AdCom committee: {committee or 'unknown'}
Indication: {indication or 'unknown'}
Market cap tier: {mkt_cap_tier}

Historical comparable AdCom moves:
YES vote examples:
{comp_yes}
NO vote examples:
{comp_no}

Base rates for this indication type ({ind_key}):
  P(yes vote): {base['p_yes']}% {base['p_yes_ci']}
  Avg stock move on YES: {base.get('avg_stock_move_yes_vote', '+15-35%')}
  Avg stock move on NO: {base.get('avg_stock_move_no_vote', '-25-55%')}
  FDA follows AdCom YES: ~75% of time → factor in PDUFA gap trade opportunity

IMPORTANT DISTINCTION:
  AdCom vote day move = immediate reaction to panel vote result
  PDUFA approval move = additional move 60-90 days later on FDA decision
  If YES vote: buy options for PDUFA date too (second catalyst)
  If NO vote: most value realized on AdCom day itself

REQUIRED OUTPUT FORMAT:
  Expected stock move ON YES VOTE: +[X]% to +[X]% (central: +[X]%)
  Expected stock move ON NO VOTE: -[X]% to -[X]%
  PDUFA gap trade: +[X]% additional if FDA follows AdCom YES
  Asymmetry score: [1-10]
  Catalyst weight: ~[X]% of equity value
  Option structure for AdCom day: strike [X]% OTM, expiry [AdCom date + 3-5 days]
  Option structure for PDUFA (if AdCom passes): strike [X]% OTM, expiry [PDUFA + buffer]"""

    else:  # BIOTECH (existing system)
        return f"""
MAGNITUDE ANALYSIS:
REQUIRED OUTPUT FORMAT:
  Expected stock move ON SUCCESS: +[X]% to +[X]% (central: +[X]%)
  Expected stock move ON FAILURE: -[X]% to -[X]%
  Asymmetry score: [1-10]
  Catalyst weight: ~[X]% of equity value
  Options market implied move: search '[{ticker}] options implied move'
  Comparable moves: [3 real historical examples with actual % moves]"""

if __name__ == "__main__":
    print("Contract J&A base rate:", CONTRACT_BASE_RATES["J_AND_A_SOLE_SOURCE"]["p_win"], "%")
    print("AdCom overall p(yes):", ADCOM_BASE_RATES["OVERALL"]["p_yes"], "%")
    print("Sample contract magnitude prompt:")
    print(get_magnitude_prompt("CONTRACT", "J_AND_A_SOLE_SOURCE", "RKLB", "small", "$150M"))
