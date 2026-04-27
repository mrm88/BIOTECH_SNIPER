#!/usr/bin/env python3
"""
TRIAL SCIENCE READER
Shkreli-style deep protocol analysis.

For every candidate trial, pulls the full protocol from ClinicalTrials.gov API v2:
  - Primary/secondary endpoints (what does success actually require?)
  - Study design (randomized? placebo-controlled? crossover? open-label?)
  - Sample size + power calculation (powered for what effect size?)
  - Mechanism of action context
  - Historical Phase 2 data (if disclosed)
  - Comparator arm (best supportive care? SoC competitor? placebo?)
  - Patient population eligibility criteria

Also fetches:
  - PubMed for Phase 1/2 results on same drug/target
  - Recent 10-K/8-K text for management statements on trial design

Returns a structured ScienceProfile that the scorer uses to ask:
  "Does this trial ACTUALLY make scientific sense?"

This is the moat. Most retail money trades the calendar, not the science.
"""

import json
import re
import requests
import datetime
from pathlib import Path
from typing import Optional

from biotech_sniper.paths import BASE_DIR
CACHE_FILE = BASE_DIR / "state/science_cache.json"
CT_HEADERS = {"User-Agent": "BioCatalystBot research@mantisvc.com", "Accept": "application/json"}
PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
NCBI_HEADERS = {"User-Agent": "BioCatalystBot research@mantisvc.com"}

CACHE_MAX_AGE_DAYS = 30  # Science data doesn't change often


def load_cache() -> dict:
    if CACHE_FILE.exists():
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}


def save_cache(cache: dict):
    CACHE_FILE.parent.mkdir(exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def _cache_key(nct_id: str) -> str:
    return f"ct_{nct_id}"


def fetch_full_protocol(nct_id: str) -> dict:
    """
    Fetch the complete protocol from ClinicalTrials.gov API v2.
    Returns all fields needed for science analysis.
    """
    cache = load_cache()
    ck = _cache_key(nct_id)
    if ck in cache:
        age = (datetime.date.today() - datetime.date.fromisoformat(cache[ck]["fetched_date"])).days
        if age < CACHE_MAX_AGE_DAYS:
            return cache[ck]["data"]

    url = f"https://clinicaltrials.gov/api/v2/studies/{nct_id}"
    # Fetch full protocol — no fields filter needed, v2 API returns all modules by default

    try:
        r = requests.get(url, headers=CT_HEADERS, timeout=20)
        if r.status_code != 200:
            print(f"  [science_reader] {nct_id}: HTTP {r.status_code}")
            return {}
        data = r.json()
        ps = data.get("protocolSection", {})
        parsed = _parse_protocol(nct_id, ps)

        cache[ck] = {"fetched_date": datetime.date.today().isoformat(), "data": parsed}
        save_cache(cache)
        return parsed

    except Exception as e:
        print(f"  [science_reader] {nct_id}: {e}")
        return {}


def _parse_protocol(nct_id: str, ps: dict) -> dict:
    """Parse the protocolSection into a structured ScienceProfile."""

    # ── IDENTIFICATION ────────────────────────────────────────────────────────
    id_mod = ps.get("identificationModule", {})
    brief_title = id_mod.get("briefTitle", "")
    official_title = id_mod.get("officialTitle", "")

    # ── STATUS ────────────────────────────────────────────────────────────────
    status_mod = ps.get("statusModule", {})
    primary_completion = status_mod.get("primaryCompletionDateStruct", {}).get("date", "")

    # ── DESIGN ────────────────────────────────────────────────────────────────
    design_mod = ps.get("designModule", {})
    design_info = design_mod.get("designInfo", {})
    allocation = design_info.get("allocation", "")          # RANDOMIZED | NON_RANDOMIZED | N/A
    masking_info = design_info.get("maskingInfo", {})
    masking = masking_info.get("masking", "")               # NONE (Open Label) | SINGLE | DOUBLE | QUADRUPLE
    model = design_info.get("interventionModel", "")        # PARALLEL | CROSSOVER | FACTORIAL
    primary_purpose = design_info.get("primaryPurpose", "")
    enrollment_count = design_mod.get("enrollmentInfo", {}).get("count", None)
    enrollment_type = design_mod.get("enrollmentInfo", {}).get("type", "")  # ESTIMATED | ACTUAL

    n_arms = len(ps.get("armsInterventionsModule", {}).get("armGroups", []))

    # ── ARMS ──────────────────────────────────────────────────────────────────
    arms_mod = ps.get("armsInterventionsModule", {})
    arms = []
    for arm in arms_mod.get("armGroups", []):
        arms.append({
            "label": arm.get("label", ""),
            "type": arm.get("type", ""),          # EXPERIMENTAL | ACTIVE_COMPARATOR | PLACEBO_COMPARATOR | NO_INTERVENTION
            "description": arm.get("description", "")[:300],
        })

    # Determine if placebo-controlled
    arm_types = [a["type"].upper() for a in arms]
    has_placebo = any("PLACEBO" in t for t in arm_types)
    has_active_comparator = any("ACTIVE_COMPARATOR" in t for t in arm_types)
    is_open_label = masking.upper() in ("NONE", "OPEN LABEL", "")

    # ── INTERVENTIONS ─────────────────────────────────────────────────────────
    interventions = []
    for iv in arms_mod.get("interventions", []):
        interventions.append({
            "name": iv.get("name", ""),
            "type": iv.get("type", ""),
            "description": iv.get("description", "")[:400],
        })

    drug_names = [iv["name"] for iv in interventions
                  if iv["type"].upper() in ("DRUG", "BIOLOGICAL", "COMBINATION_PRODUCT", "GENETIC", "RADIATION")]

    # ── PRIMARY ENDPOINTS ─────────────────────────────────────────────────────
    outcomes_mod = ps.get("outcomesModule", {})
    primary_outcomes = []
    for po in outcomes_mod.get("primaryOutcomes", []):
        primary_outcomes.append({
            "measure": po.get("measure", ""),
            "description": po.get("description", "")[:400],
            "time_frame": po.get("timeFrame", ""),
        })

    secondary_outcomes = []
    for so in outcomes_mod.get("secondaryOutcomes", [])[:5]:
        secondary_outcomes.append({
            "measure": so.get("measure", ""),
            "time_frame": so.get("timeFrame", ""),
        })

    # ── ELIGIBILITY ───────────────────────────────────────────────────────────
    elig_mod = ps.get("eligibilityModule", {})
    eligibility_text = elig_mod.get("eligibilityCriteria", "")[:2000]
    min_age = elig_mod.get("minimumAge", "")
    max_age = elig_mod.get("maximumAge", "")

    # ── CONDITIONS ────────────────────────────────────────────────────────────
    cond_mod = ps.get("conditionsModule", {})
    conditions = cond_mod.get("conditions", [])
    keywords = cond_mod.get("keywords", [])

    # ── DESCRIPTION ───────────────────────────────────────────────────────────
    desc_mod = ps.get("descriptionModule", {})
    brief_summary = desc_mod.get("briefSummary", "")[:800]
    detailed_description = desc_mod.get("detailedDescription", "")[:1200]

    # ── REFERENCES (prior Phase 1/2 publications) ─────────────────────────────
    refs_mod = ps.get("referencesModule", {})
    references = []
    for ref in refs_mod.get("references", [])[:10]:
        references.append({
            "type": ref.get("type", ""),   # RESULT | DERIVED | BACKGROUND
            "citation": ref.get("citation", "")[:200],
            "pmid": ref.get("pmid", ""),
        })

    # PMIDs of result references → fetch abstracts
    result_pmids = [r["pmid"] for r in references if r.get("type", "").upper() == "RESULT" and r.get("pmid")]

    # ── SCIENCE FLAGS (heuristics before LLM scoring) ─────────────────────────
    flags = _compute_science_flags(
        allocation=allocation,
        masking=masking,
        has_placebo=has_placebo,
        is_open_label=is_open_label,
        enrollment_count=enrollment_count,
        primary_outcomes=primary_outcomes,
        arms=arms,
        eligibility_text=eligibility_text,
        brief_summary=brief_summary,
        drug_names=drug_names,
    )

    return {
        "nct_id": nct_id,
        "brief_title": brief_title,
        "official_title": official_title,
        "primary_completion": primary_completion,
        # Design quality signals
        "allocation": allocation,
        "masking": masking,
        "model": model,
        "primary_purpose": primary_purpose,
        "has_placebo": has_placebo,
        "has_active_comparator": has_active_comparator,
        "is_open_label": is_open_label,
        "enrollment_count": enrollment_count,
        "enrollment_type": enrollment_type,
        "n_arms": n_arms,
        # Endpoints
        "primary_outcomes": primary_outcomes,
        "secondary_outcomes": secondary_outcomes,
        # Drugs / MoA
        "drug_names": drug_names,
        "interventions": interventions,
        "arms": arms,
        # Patient population
        "conditions": conditions,
        "keywords": keywords,
        "eligibility_summary": eligibility_text[:600],
        "min_age": min_age,
        "max_age": max_age,
        # Descriptions
        "brief_summary": brief_summary,
        "detailed_description": detailed_description,
        # Prior study publications
        "references": references,
        "result_pmids": result_pmids,
        # Pre-computed flags
        "science_flags": flags,
        # Fetched date
        "fetched_date": datetime.date.today().isoformat(),
    }


def _compute_science_flags(
    allocation: str,
    masking: str,
    has_placebo: bool,
    is_open_label: bool,
    enrollment_count,
    primary_outcomes: list,
    arms: list,
    eligibility_text: str,
    brief_summary: str,
    drug_names: list,
) -> dict:
    """
    Compute hard science flags — high-conviction signals before LLM call.
    These are the BEARISH / BULLISH signals Shkreli would have caught reading the protocol.
    """
    flags = {
        "design_quality": [],   # List of flag strings
        "bearish_flags": [],    # Red flags → push P(success) DOWN
        "bullish_flags": [],    # Positive signals → push P(success) UP
        "endpoint_type": "",    # SURROGATE | CLINICAL | COMPOSITE | FUNCTIONAL
        "is_well_powered": None,
        "design_score": 0,      # -10 to +10, negative = sloppy trial
    }

    score = 0

    # ── DESIGN FLAGS ─────────────────────────────────────────────────────────

    # Placebo-controlled RCT = gold standard
    if has_placebo and allocation.upper() == "RANDOMIZED":
        flags["bullish_flags"].append("Randomized placebo-controlled (gold standard design)")
        score += 2

    # Open-label = weaker evidence, more endpoint manipulation risk
    if is_open_label:
        flags["bearish_flags"].append("Open-label design (no blinding — endpoint bias risk)")
        score -= 2

    # Non-randomized = serious red flag in Phase 3
    if allocation.upper() == "NON_RANDOMIZED":
        flags["bearish_flags"].append("NON-RANDOMIZED Phase 3 — very unusual, high failure risk")
        score -= 3

    # ── ENROLLMENT ───────────────────────────────────────────────────────────

    if enrollment_count:
        try:
            n = int(enrollment_count)
            if n < 100:
                flags["bearish_flags"].append(f"Very small N={n} for Phase 3 (underpowered?)")
                score -= 2
            elif n >= 500:
                flags["bullish_flags"].append(f"Large enrollment N={n} (well-powered)")
                score += 1
        except (ValueError, TypeError):
            pass

    # ── ENDPOINT ANALYSIS ────────────────────────────────────────────────────

    if primary_outcomes:
        po_text = " ".join([p.get("measure", "") + " " + p.get("description", "") for p in primary_outcomes]).lower()
        time_frames = [p.get("time_frame", "").lower() for p in primary_outcomes]

        # Clinical endpoints (hardest to manipulate)
        clinical_kw = ["overall survival", "os ", "event-free survival", "efs", "progression-free",
                       "pfs", "death", "hospitalization", "major adverse", "mace", "transplant"]
        surrogate_kw = ["biomarker", "lab value", "level", "concentration",
                        "orr", "ctrough", "pharmacokinetic", "pk ", "tumor shrinkage",
                        "complete response", "partial response"]
        # Validated composite clinical endpoints — NOT surrogates despite using 'response rate'
        validated_composite_kw = ["acr50", "acr 50", "das28", "pasi", "cdai", "haqs", "haq ",
                                   "hiscr", "cdeis", "mayo score", "adas-cog", "mmse"]
        is_validated_composite = any(kw in po_text for kw in validated_composite_kw)
        functional_kw = ["quality of life", "qol", "functional", "score", "questionnaire",
                         "6-minute walk", "hamd", "phq", "patient-reported"]

        if any(kw in po_text for kw in clinical_kw):
            flags["endpoint_type"] = "CLINICAL"
            flags["bullish_flags"].append("Hard clinical endpoint (OS/PFS/EFS) — hardest to fake")
            score += 2
        elif is_validated_composite:
            flags["endpoint_type"] = "COMPOSITE"
            flags["bullish_flags"].append("Validated composite clinical endpoint (ACR/PASI/HiSCR — FDA-required standard for this indication)")
            score += 1
        elif any(kw in po_text for kw in functional_kw):
            flags["endpoint_type"] = "FUNCTIONAL"
            flags["bullish_flags"].append("Functional/PRO endpoint (valid but softer than OS)")
            score += 1
        elif any(kw in po_text for kw in surrogate_kw):
            flags["endpoint_type"] = "SURROGATE"
            flags["bearish_flags"].append("Surrogate endpoint — historically weak FDA track record")
            score -= 1

        # Very short time frames can be red flag in slow diseases
        short_timeframe = any(
            re.search(r"\b[1-4]\s*weeks?\b|\b[1-3]\s*months?\b", tf)
            for tf in time_frames
        )
        if short_timeframe:
            flags["bearish_flags"].append("Very short primary endpoint time frame (may not capture durable response)")
            score -= 1

    # ── ELIGIBILITY / PATIENT POPULATION ─────────────────────────────────────

    elig_lower = eligibility_text.lower()

    # Enrichment strategies — often helpful but also signal mechanistic uncertainty
    if "biomarker" in elig_lower and ("positive" in elig_lower or "negative" in elig_lower):
        flags["bullish_flags"].append("Biomarker-enriched population (targeted approach)")
        score += 1

    # Heavily restricted population = small addressable market + possible cherry-picking
    restriction_count = sum([
        "prior therapy" in elig_lower,
        "failed" in elig_lower,
        "refractory" in elig_lower,
        "relapsed" in elig_lower,
        "≥3" in elig_lower or ">=3" in elig_lower or "3 prior" in elig_lower,
    ])
    if restriction_count >= 3:
        flags["bearish_flags"].append("Heavily pre-treated/refractory population — harder to show benefit, smaller market")
        score -= 1

    # ── DRUG / MoA FLAGS ─────────────────────────────────────────────────────

    drug_text = " ".join(drug_names).lower()

    # Common red flags
    # Only flag amyloid/tau as extremely bearish — NOT all CNS or behavior targets
    if any(kw in drug_text for kw in ["amyloid", "tau", "aducanumab", "lecanemab", "donanemab"]):
        flags["bearish_flags"].append("Amyloid/tau targeting drug — historical failure rate >97%")
        score -= 2

    if any(kw in drug_text for kw in ["antisense", "aso", "rna interference", "sirna", "rnai"]):
        flags["bullish_flags"].append("RNA-targeting modality (antisense/siRNA) — modern MoA with recent approvals")
        score += 1

    if any(kw in drug_text for kw in ["gene therapy", "aav", "lentiviral"]):
        flags["bullish_flags"].append("Gene therapy — high risk/reward, but mechanism is direct and measurable")

    # Check summary for MoA clarity
    summary_lower = brief_summary.lower()
    if "mechanism" in summary_lower or "inhibit" in summary_lower or "block" in summary_lower:
        flags["bullish_flags"].append("Clear mechanistic rationale stated in protocol")
        score += 1

    flags["design_score"] = max(-10, min(10, score))
    return flags


def fetch_pubmed_phase2_results(drug_name: str, indication: str, max_results: int = 5) -> list:
    """
    Search PubMed for Phase 1/2 results for this drug + indication.
    Returns list of abstract summaries — the prior clinical evidence that either
    supports or undermines the Phase 3 thesis.
    """
    if not drug_name:
        return []

    search_term = f'"{drug_name}" AND ("phase 1" OR "phase 2" OR "phase I" OR "phase II") AND ("clinical trial" OR "results")'
    if indication:
        short_indication = indication.split(";")[0].strip()[:50]
        search_term += f' AND ("{short_indication}")'

    try:
        # Step 1: Search
        search_url = f"{PUBMED_BASE}/esearch.fcgi"
        search_params = {
            "db": "pubmed",
            "term": search_term,
            "retmax": max_results,
            "sort": "pub_date",
            "retmode": "json",
        }
        r = requests.get(search_url, params=search_params, headers=NCBI_HEADERS, timeout=15)
        if r.status_code != 200:
            return []
        pmids = r.json().get("esearchresult", {}).get("idlist", [])

        if not pmids:
            return []

        # Step 2: Fetch abstracts
        fetch_url = f"{PUBMED_BASE}/efetch.fcgi"
        fetch_params = {
            "db": "pubmed",
            "id": ",".join(pmids),
            "rettype": "abstract",
            "retmode": "text",
        }
        r2 = requests.get(fetch_url, params=fetch_params, headers=NCBI_HEADERS, timeout=20)
        if r2.status_code != 200:
            return []

        # Parse out key info from each abstract
        abstracts = r2.text.split("\n\n\n")
        results = []
        for abs_text in abstracts[:max_results]:
            if len(abs_text) < 100:
                continue
            results.append({
                "text": abs_text[:1200],
                "source": "pubmed",
            })
        return results

    except Exception as e:
        print(f"  [pubmed] Error searching '{drug_name}': {e}")
        return []


def build_science_enriched_prompt(candidate: dict, protocol: dict, pubmed_abstracts: list) -> str:
    """
    Build the scoring prompt with full science context.
    This is what gets sent to Claude Opus + Gemini — 10x richer than basic metadata.

    The Shkreli edge: the models now read the actual science, not just "Phase 3 drug for cancer".
    """
    ticker = candidate.get("ticker", "UNKNOWN")
    drug = candidate.get("drug", protocol.get("drug_names", [""])[0] if protocol.get("drug_names") else "")
    indication = candidate.get("indication", "; ".join(protocol.get("conditions", [])))

    flags = protocol.get("science_flags", {})
    bearish = flags.get("bearish_flags", [])
    bullish = flags.get("bullish_flags", [])
    design_score = flags.get("design_score", 0)
    endpoint_type = flags.get("endpoint_type", "UNKNOWN")

    # Format primary endpoints
    po_text = ""
    for po in protocol.get("primary_outcomes", [])[:2]:
        po_text += f"  - {po.get('measure', '')}: {po.get('time_frame', '')}\n"
        if po.get("description"):
            po_text += f"    Detail: {po['description'][:200]}\n"

    # Format arms
    arms_text = ""
    for arm in protocol.get("arms", []):
        arms_text += f"  [{arm.get('type', '?')}] {arm.get('label', '')} — {arm.get('description', '')[:150]}\n"

    # Format PubMed prior evidence
    pubmed_text = ""
    if pubmed_abstracts:
        pubmed_text = "\nPRIOR CLINICAL EVIDENCE (Phase 1/2 PubMed results):\n"
        for i, ab in enumerate(pubmed_abstracts[:3]):
            pubmed_text += f"--- Abstract {i+1} ---\n{ab['text'][:600]}\n\n"
    else:
        pubmed_text = "\nPRIOR CLINICAL EVIDENCE: None found on PubMed (no published Phase 1/2 data).\n"

    prompt = f"""You are a clinical trial analyst conducting a Shkreli-style science review.
Your job: assess whether this Phase 3 trial has SCIENTIFIC merit, not just a date on the calendar.
Most retail investors don't read the protocol. You do. That's the edge.

TRIAL: {protocol.get('brief_title', candidate.get('trial_name', 'Unknown'))}
NCT ID: {protocol.get('nct_id', '')}
TICKER: {ticker}
DRUG: {drug}
INDICATION: {indication}
ENROLLMENT: {protocol.get('enrollment_count', 'unknown')} patients ({protocol.get('enrollment_type', '')})
COMPLETION: {protocol.get('primary_completion', 'unknown')}

━━━ STUDY DESIGN ━━━
Allocation: {protocol.get('allocation', 'unknown')}
Masking: {protocol.get('masking', 'unknown')} | Open-label: {protocol.get('is_open_label', '?')}
Model: {protocol.get('model', 'unknown')}
Arms: {protocol.get('n_arms', '?')} total
Has placebo control: {protocol.get('has_placebo', '?')}
Has active comparator: {protocol.get('has_active_comparator', '?')}

━━━ ARMS ━━━
{arms_text or 'Not available'}

━━━ PRIMARY ENDPOINTS ━━━
{po_text or 'Not available'}
Endpoint type classification: {endpoint_type}

━━━ SECONDARY ENDPOINTS ━━━
{chr(10).join(['  - ' + so.get('measure', '') + ' (' + so.get('time_frame', '') + ')' for so in protocol.get('secondary_outcomes', [])[:5]]) or 'Not available'}

━━━ TRIAL SUMMARY ━━━
{protocol.get('brief_summary', 'Not available')}

━━━ ELIGIBILITY CRITERIA (excerpt) ━━━
{protocol.get('eligibility_summary', 'Not available')}

━━━ PRE-COMPUTED SCIENCE FLAGS ━━━
Design score: {design_score:+d}/10 (negative = more bearish)
BEARISH flags: {chr(10).join(['  ⚠ ' + f for f in bearish]) if bearish else '  (none)'}
BULLISH flags: {chr(10).join(['  ✓ ' + f for f in bullish]) if bullish else '  (none)'}

{pubmed_text}

━━━ YOUR ANALYSIS TASK ━━━

Step 1 — ENDPOINT VALIDITY
Is the primary endpoint measuring something the FDA cares about?
Is the time frame long enough to capture a meaningful treatment effect?
Is this a surrogate that has failed to translate in similar drugs before?

Step 2 — MECHANISTIC PLAUSIBILITY
Does the drug's mechanism of action actually make scientific sense for this indication?
Is this a well-validated target (e.g. PD-1 in oncology) or a speculative one (e.g. amyloid in Alzheimer's)?

Step 3 — STATISTICAL POWER
Is N={protocol.get('enrollment_count', '?')} sufficient for the endpoint and expected effect size?
Is the enrollment type ACTUAL (locked in) or still ESTIMATED?

Step 4 — DESIGN QUALITY
Randomized and blinded? Any critical flaws (open-label in a subjective endpoint, wrong comparator, cherry-picked population)?

Step 5 — PRIOR EVIDENCE
What do the Phase 1/2 results suggest? Does the Phase 3 dose match the effective Phase 2 dose?
Are there red flags in the prior data (e.g. effect only at extreme doses, only in subgroups)?

Step 6 — BASE RATE COMPARISON
This is Phase 3 in {indication}. What is the historical Phase 3 success rate in this indication?
Key base rates: Oncology ~40-50% | CNS ~30% | Rare disease ~60% | Alzheimer's <5% | Infectious disease ~65%

━━━ FINAL ANSWER ━━━
Given ONLY the science (not the market hype, not the stock price, not press releases):

P(SUCCESS) = [integer 0-100]%

Then provide:
DIRECTION: LONG_CALLS | LONG_PUTS | DROP (if 41-59%)
CONFIDENCE: HIGH | MEDIUM | LOW (how sure are you given available data)
ONE_LINE_THESIS: <one sentence — tweet-style — explaining the key risk or opportunity>
SHORT_THESIS: <if P<=40%: why does the science FAIL here? Shkreli-style, 2 sentences max>
LONG_THESIS: <if P>=60%: what specifically makes this trial likely to succeed?>

Be ruthless. The market overpays for hope. Your edge is the science.
"""

    return prompt


def get_science_profile(nct_id: str, candidate: dict) -> dict:
    """
    Main entry point. Fetch full protocol + PubMed data for a candidate.
    Returns a ScienceProfile ready for scoring.
    """
    print(f"  [science_reader] Fetching full protocol for {nct_id}...")
    protocol = fetch_full_protocol(nct_id)

    if not protocol:
        print(f"  [science_reader] No protocol data for {nct_id}, falling back to basic metadata")
        return {"nct_id": nct_id, "protocol": {}, "pubmed": [], "science_prompt": None}

    # Get primary drug name for PubMed search
    drug_names = protocol.get("drug_names", [])
    drug_name = drug_names[0] if drug_names else candidate.get("drug", "")
    indication = candidate.get("indication", "; ".join(protocol.get("conditions", [])))

    print(f"  [science_reader] Searching PubMed for prior {drug_name} results...")
    pubmed_abstracts = fetch_pubmed_phase2_results(drug_name, indication)

    science_prompt = build_science_enriched_prompt(candidate, protocol, pubmed_abstracts)

    return {
        "nct_id": nct_id,
        "protocol": protocol,
        "pubmed": pubmed_abstracts,
        "science_prompt": science_prompt,
        "design_score": protocol.get("science_flags", {}).get("design_score", 0),
        "bearish_flags": protocol.get("science_flags", {}).get("bearish_flags", []),
        "bullish_flags": protocol.get("science_flags", {}).get("bullish_flags", []),
        "endpoint_type": protocol.get("science_flags", {}).get("endpoint_type", "UNKNOWN"),
        "has_pubmed_data": len(pubmed_abstracts) > 0,
    }


def get_science_profile_for_existing_play(play: dict) -> Optional[dict]:
    """
    Enrich an existing active play with science data.
    Used in the daily cron to retroactively add science grades to existing plays.
    """
    nct_id = play.get("nct_id", "")
    if not nct_id or nct_id.startswith("UNK"):
        return None

    candidate = {
        "ticker": play.get("ticker", ""),
        "drug": play.get("drug", ""),
        "indication": play.get("indication", ""),
    }

    return get_science_profile(nct_id, candidate)


if __name__ == "__main__":
    # Quick test on a known trial
    test_nct = "NCT04767139"  # Example Phase 3
    print(f"Testing science reader on {test_nct}...")
    profile = fetch_full_protocol(test_nct)
    if profile:
        print(f"Title: {profile.get('brief_title', 'n/a')}")
        print(f"Design score: {profile.get('science_flags', {}).get('design_score', 'n/a')}")
        print(f"Bearish: {profile.get('science_flags', {}).get('bearish_flags', [])}")
        print(f"Bullish: {profile.get('science_flags', {}).get('bullish_flags', [])}")
    else:
        print("No data returned")
