#!/usr/bin/env python3
"""
SCIENCE SCORER
Shkreli-style conviction scoring layer.

Sits between trial_science_reader.py and unified_scorer.py.
Takes the ScienceProfile and builds a SCIENCE GRADE + adjusted P(success).

The core idea:
  - Most scoring systems use "Phase 3 in cancer: base rate 40%"
  - We go deeper: trial design quality, endpoint validity, MoA plausibility, prior data
  - This lets us find PUT setups (bad science, inflated market expectations)
    AND find stronger long setups (robust design that market is underpricing)

SCIENCE GRADE: A / B / C / D / F
  A = Randomized, blinded, hard endpoint, well-powered, strong Phase 2 data
  B = Solid design, minor concerns
  C = Adequate but notable weaknesses (open-label, surrogate endpoint, etc.)
  D = Multiple red flags — not necessarily wrong but science is weak
  F = Protocol is fundamentally flawed — Shkreli short territory

CONVICTION MULTIPLIER on base P(success):
  A: 1.15x | B: 1.05x | C: 1.0x | D: 0.85x | F: 0.65x

This is applied BEFORE the LLM dual-model scoring, which then refines further.
"""

import json
import datetime
from pathlib import Path
from typing import Optional

from biotech_sniper.paths import BASE_DIR
SCIENCE_GRADES_FILE = BASE_DIR / "state/science_grades.json"


def load_science_grades() -> dict:
    if SCIENCE_GRADES_FILE.exists():
        with open(SCIENCE_GRADES_FILE) as f:
            return json.load(f)
    return {}


def save_science_grades(grades: dict):
    SCIENCE_GRADES_FILE.parent.mkdir(exist_ok=True)
    with open(SCIENCE_GRADES_FILE, "w") as f:
        json.dump(grades, f, indent=2)


# ── HISTORICAL BASE RATES BY INDICATION ──────────────────────────────────────
# Source: BIO/Citeline 2023 Clinical Development Success Rate Report
BASE_RATES = {
    "alzheimer amyloid": 0.03,    # Amyloid-targeting in Alzheimer's — historically catastrophic
    "alzheimer agitation": 0.28,  # Behavioral/symptomatic Alzheimer's trials — much higher success
    "alzheimer": 0.18,            # General Alzheimer (catch-all; symptomatic trials do better)
    "dementia": 0.20,
    "psychiatric": 0.10,
    "depression": 0.18,
    "schizophrenia": 0.07,
    "cns": 0.10,
    "glioblastoma": 0.08,
    "brain tumor": 0.08,
    "pancreatic": 0.15,
    "pancreatic cancer": 0.15,
    "lung cancer": 0.40,
    "nsclc": 0.42,
    "sclc": 0.25,
    "breast cancer": 0.48,
    "colorectal": 0.35,
    "melanoma": 0.52,
    "lymphoma": 0.55,
    "leukemia": 0.55,
    "multiple myeloma": 0.58,
    "hematologic": 0.55,
    "oncology": 0.42,
    "immunology": 0.50,
    "autoimmune": 0.52,
    "rheumatoid arthritis": 0.58,
    "lupus": 0.30,
    "rare disease": 0.65,
    "orphan": 0.70,
    "cardiovascular": 0.45,
    "heart failure": 0.40,
    "infectious": 0.65,
    "infectious disease": 0.65,
    "respiratory": 0.50,
    "copd": 0.45,
    "idiopathic pulmonary fibrosis": 0.35,
    "metabolic": 0.55,
    "diabetes": 0.60,
    "nash": 0.25,
    "mash": 0.25,
    "fatty liver": 0.25,
    "kidney": 0.50,
    "renal": 0.50,
    "ophthalmic": 0.55,
    "gene therapy": 0.58,
    "cell therapy": 0.52,
    "default": 0.45,  # Unknown indication
}


def get_indication_base_rate(indication: str, conditions: list) -> tuple[float, str]:
    """Returns (base_rate, matched_category) for the indication."""
    search_text = (indication + " " + " ".join(conditions)).lower()

    best_match = ("default", BASE_RATES["default"])
    for kw, rate in BASE_RATES.items():
        if kw in search_text and kw != "default":
            # Prefer more specific matches
            if len(kw) > len(best_match[0]):
                best_match = (kw, rate)

    return best_match[1], best_match[0]


def compute_science_grade(science_profile: dict) -> dict:
    """
    Given a ScienceProfile from trial_science_reader, compute:
    - Letter grade (A-F)
    - Conviction multiplier
    - Short explanation
    - Identified short thesis (if grade D/F)
    """
    protocol = science_profile.get("protocol", {})
    flags = protocol.get("science_flags", {})
    design_score = flags.get("design_score", 0)
    bearish_flags = flags.get("bearish_flags", [])
    bullish_flags = flags.get("bullish_flags", [])
    endpoint_type = flags.get("endpoint_type", "UNKNOWN")
    has_pubmed = science_profile.get("has_pubmed_data", False)

    # Base grade from design_score
    if design_score >= 6:
        grade = "A"
        multiplier = 1.15
    elif design_score >= 3:
        grade = "B"
        multiplier = 1.05
    elif design_score >= 0:
        grade = "C"
        multiplier = 1.00
    elif design_score >= -3:
        grade = "D"
        multiplier = 0.85
    else:
        grade = "F"
        multiplier = 0.65

    # Override for critical failures
    is_open_label = protocol.get("is_open_label", False)
    allocation = protocol.get("allocation", "")
    endpoint_type = flags.get("endpoint_type", "")

    # Open-label + subjective endpoint = automatic D minimum
    subjective_endpoints = ["quality of life", "qol", "patient-reported", "score", "questionnaire", "functional"]
    primary_text = " ".join([po.get("measure", "").lower() for po in protocol.get("primary_outcomes", [])])
    is_subjective_endpoint = any(kw in primary_text for kw in subjective_endpoints)

    if is_open_label and is_subjective_endpoint:
        if grade not in ("D", "F"):
            grade = "D"
            multiplier = min(multiplier, 0.85)
        bearish_flags.append("CRITICAL: Open-label + subjective endpoint = cannot distinguish placebo effect from treatment")

    # Non-randomized Phase 3 = automatic F
    if allocation.upper() == "NON_RANDOMIZED":
        grade = "F"
        multiplier = 0.65
        bearish_flags.append("CRITICAL: Non-randomized Phase 3 = uninterpretable results expected")

    # Alzheimer / amyloid in CNS = automatic D minimum
    conditions_text = " ".join(protocol.get("conditions", [])).lower()
    drug_text = " ".join(protocol.get("drug_names", [])).lower()
    if "alzheimer" in conditions_text or "amyloid" in drug_text:
        if grade not in ("D", "F"):
            grade = "D"
            multiplier = min(multiplier, 0.85)

    # Very small N in Phase 3
    n = protocol.get("enrollment_count")
    try:
        n = int(n)
        if n < 50:
            grade = "F"
            multiplier = 0.65
        elif n < 100 and grade == "A":
            grade = "B"
            multiplier = min(multiplier, 1.05)
    except (TypeError, ValueError):
        n = None

    # Grade description
    grade_descriptions = {
        "A": "Robust design — RCT, blinded, hard endpoint, well-powered. High-confidence science.",
        "B": "Solid design with minor concerns. Science supports the thesis.",
        "C": "Adequate but notable weaknesses. Market may be appropriately pricing risk.",
        "D": "Multiple red flags. Science is weak — market may be overpricing odds.",
        "F": "Fundamentally flawed protocol. High-conviction SHORT territory.",
    }

    # Short thesis for D/F grades (put setup)
    short_thesis = None
    if grade in ("D", "F"):
        short_thesis = _build_short_thesis(protocol, flags, bearish_flags)

    # Generate the one-liner
    top_flag = bearish_flags[0] if bearish_flags else (bullish_flags[0] if bullish_flags else "No notable flags")
    one_liner = f"Grade {grade} ({design_score:+d}): {top_flag}"

    result = {
        "grade": grade,
        "multiplier": multiplier,
        "design_score": design_score,
        "description": grade_descriptions.get(grade, ""),
        "one_liner": one_liner,
        "bearish_flags": bearish_flags,
        "bullish_flags": bullish_flags,
        "endpoint_type": endpoint_type,
        "n": n,
        "is_open_label": is_open_label,
        "has_pubmed_data": has_pubmed,
        "short_thesis": short_thesis,
        "is_short_candidate": grade in ("D", "F"),
        "graded_date": datetime.date.today().isoformat(),
    }

    return result


def _build_short_thesis(protocol: dict, flags: dict, bearish_flags: list) -> str:
    """
    Shkreli-style short thesis: 2 sentences max.
    Why does the science fail here? What does the market not understand?
    """
    drug = (protocol.get("drug_names") or ["?"])[0]
    indication = (protocol.get("conditions") or ["unknown"])[0]
    allocation = protocol.get("allocation", "")
    is_open_label = protocol.get("is_open_label", False)
    endpoint_type = flags.get("endpoint_type", "UNKNOWN")
    n = protocol.get("enrollment_count", "?")

    thesis_parts = []

    if allocation.upper() == "NON_RANDOMIZED":
        thesis_parts.append(f"{drug} Phase 3 is non-randomized — any results are statistically uninterpretable")
    elif is_open_label:
        thesis_parts.append(f"Open-label design means investigators and patients know the treatment — subjective endpoint bias is guaranteed")

    if endpoint_type == "SURROGATE":
        thesis_parts.append("Primary endpoint is a surrogate biomarker with poor historical FDA track record — approval is not guaranteed even if endpoint is met")

    # Check for Alzheimer — but only flag amyloid hypothesis if that's the MoA
    cond_text = " ".join(protocol.get("conditions", [])).lower()
    drug_lower = " ".join(protocol.get("drug_names", [])).lower()
    has_amyloid_drug = any(kw in drug_lower for kw in ["amyloid", "tau", "aducanumab", "lecanemab", "donanemab"])
    if "alzheimer" in cond_text and has_amyloid_drug:
        thesis_parts.append("Alzheimer amyloid-targeting drug — >97% historical failure rate; FDA approvals have been highly contested")
    elif "alzheimer" in cond_text:
        # Alzheimer CNS trial (not amyloid) — still high risk but different reason
        thesis_parts.append(f"CNS drug in Alzheimer population — high base failure rate (~20-30% for symptomatic trials); any improvement must exceed placebo on validated scale")

    if not thesis_parts and bearish_flags:
        thesis_parts.append(bearish_flags[0])
        if len(bearish_flags) > 1:
            thesis_parts.append(bearish_flags[1])

    return ". ".join(thesis_parts[:2]) + "." if thesis_parts else ""


def adjust_probability_with_science(
    raw_p: float,
    science_grade: dict,
    indication: str,
    conditions: list,
) -> dict:
    """
    Apply science grade multiplier to adjust probability.
    Also factors in indication base rate.

    Returns:
      adjusted_p: final P(success) after science adjustment
      base_rate: historical base rate for this indication
      adjustment_summary: human-readable explanation of the adjustment
    """
    base_rate, matched_category = get_indication_base_rate(indication, conditions)
    multiplier = science_grade.get("multiplier", 1.0)
    grade = science_grade.get("grade", "C")

    # Apply multiplier to raw_p (from basic metadata scoring)
    adjusted_p = min(95, max(5, round(raw_p * multiplier)))

    # If no prior P available, anchor to base rate adjusted by grade
    if raw_p is None:
        base_adjusted = base_rate * 100 * multiplier
        adjusted_p = min(90, max(5, round(base_adjusted)))

    summary = (
        f"Base rate ({matched_category}): {base_rate*100:.0f}% | "
        f"Science grade: {grade} ({multiplier:.2f}x) | "
        f"Adjusted P: {adjusted_p}%"
    )

    return {
        "adjusted_p": adjusted_p,
        "base_rate": base_rate,
        "matched_category": matched_category,
        "multiplier": multiplier,
        "adjustment_summary": summary,
    }


def score_with_science(nct_id: str, candidate: dict, existing_p: Optional[float] = None) -> dict:
    """
    Full pipeline: fetch protocol → grade science → adjust P → return enriched candidate.
    
    This is the main function called from master_discovery or daily cron
    when enriching a new candidate with science data.
    """
    from intelligence.trial_science_reader import get_science_profile

    # Fetch and analyze protocol
    science_profile = get_science_profile(nct_id, candidate)
    if not science_profile.get("protocol"):
        return {
            "nct_id": nct_id,
            "science_grade": None,
            "adjusted_p": existing_p,
            "science_prompt": None,
            "error": "No protocol data available",
        }

    # Grade the science
    science_grade = compute_science_grade(science_profile)

    # Adjust probability
    indication = candidate.get("indication", "")
    conditions = science_profile.get("protocol", {}).get("conditions", [])
    p_adjustment = adjust_probability_with_science(existing_p or 45.0, science_grade, indication, conditions)

    # Cache the grade
    grades = load_science_grades()
    grades[nct_id] = {
        **science_grade,
        "adjusted_p": p_adjustment["adjusted_p"],
        "base_rate": p_adjustment["base_rate"],
        "adjustment_summary": p_adjustment["adjustment_summary"],
        "ticker": candidate.get("ticker", ""),
        "nct_id": nct_id,
    }
    save_science_grades(grades)

    return {
        "nct_id": nct_id,
        "science_grade": science_grade,
        "science_prompt": science_profile.get("science_prompt"),
        "adjusted_p": p_adjustment["adjusted_p"],
        "base_rate": p_adjustment["base_rate"],
        "adjustment_summary": p_adjustment["adjustment_summary"],
        "design_score": science_grade.get("design_score", 0),
        "is_short_candidate": science_grade.get("is_short_candidate", False),
        "short_thesis": science_grade.get("short_thesis"),
    }


def get_all_science_grades() -> dict:
    """Load all cached science grades."""
    return load_science_grades()


def format_science_grade_for_email(ticker: str, science_grade: dict) -> str:
    """
    Format science grade for email output.
    One line: Grade, key flag, endpoint type.
    """
    if not science_grade:
        return ""
    grade = science_grade.get("grade", "?")
    design_score = science_grade.get("design_score", 0)
    endpoint = science_grade.get("endpoint_type", "")
    one_liner = science_grade.get("one_liner", "")
    is_short = science_grade.get("is_short_candidate", False)

    short_tag = " [SHORT CANDIDATE]" if is_short else ""
    endpoint_tag = f" | {endpoint}" if endpoint and endpoint != "UNKNOWN" else ""
    return f"Science: Grade {grade} ({design_score:+d}){endpoint_tag}{short_tag} — {one_liner}"


if __name__ == "__main__":
    # Test with a known NCT
    import sys
    nct = sys.argv[1] if len(sys.argv) > 1 else "NCT04767139"
    print(f"Testing science scorer on {nct}...")

    candidate = {
        "ticker": "TEST",
        "drug": "test drug",
        "indication": "oncology",
    }

    result = score_with_science(nct, candidate, existing_p=50.0)
    print(f"\nGrade: {result.get('science_grade', {}).get('grade', '?')}")
    print(f"Adjusted P: {result.get('adjusted_p', '?')}%")
    print(f"Adjustment: {result.get('adjustment_summary', '?')}")
    print(f"Short candidate: {result.get('is_short_candidate', False)}")
    if result.get("short_thesis"):
        print(f"Short thesis: {result['short_thesis']}")
