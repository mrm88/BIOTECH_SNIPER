#!/usr/bin/env python3
"""
MASTER DISCOVERY ENGINE — Step 0b of the daily 6AM cron
Finds NEW candidates in all 3 sectors BEFORE scoring.

Sources:
  BIOTECH:
    1. Warpspeed.sh — experiments with P(success) scores
    2. ClinicalTrials.gov API — Phase 3 ACTIVE_NOT_RECRUITING
    3. SEC EDGAR full-text search — 8-Ks mentioning "topline" + "phase 3"
    4. BiopharmCatalyst FDA calendar — upcoming PDUFA dates
    5. Conference abstracts — late-breaking oral searches

  CONTRACTS:
    1. USASpending.gov awards (delegated to sam_sniper)
    2. SAM.gov J&A patterns (URL patterns for cron agent browser_task)
    3. USASpending IDV task orders — new source
    4. Defense.gov contracts RSS — new source
    5. NASA SEWP procurement signals

  ADCOM:
    1. FDA AdCom RSS feed
    2. FDA calendar HTML
    3. BiopharmCatalyst AdCom calendar
    4. FDTracker
    5. FDA briefing documents (48h window)

Returns dict with new_biotech, new_contracts, new_adcom, all_new, defense_rss, signals
"""

import json
import re
import requests
import datetime
import hashlib
import sys
from pathlib import Path
from typing import Optional

from biotech_sniper.paths import BASE_DIR
REGISTRY_FILE   = BASE_DIR / "intelligence/nct_registry.json"
TICKER_MAP_FILE = BASE_DIR / "sectors/contracts/company_ticker_map.json"
ACTIVE_PLAYS    = BASE_DIR / "state/active_plays.json"
DISCOVERY_STATE = BASE_DIR / "state/discovery_state.json"
REPORTS_DIR     = BASE_DIR / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/html, */*",
}
SEC_HEADERS = {"User-Agent": "BioCatalystBot research@mantisvc.com"}
CT_HEADERS  = {"User-Agent": "BioCatalystBot research@mantisvc.com", "Accept": "application/json"}


USA_SPENDING_BASE = "https://api.usaspending.gov/api/v2"

# Cached SEC company ticker lookup — loaded once, used for entity→ticker resolution
_SEC_TICKER_LOOKUP: dict = {}  # entity_name_lower -> ticker

def get_sec_ticker_lookup() -> dict:
    """Load SEC company_tickers.json (10k+ companies) for fast entity→ticker resolution."""
    global _SEC_TICKER_LOOKUP
    if _SEC_TICKER_LOOKUP:
        return _SEC_TICKER_LOOKUP
    try:
        r = requests.get("https://www.sec.gov/files/company_tickers.json",
                         headers={"User-Agent": "BioCatalystBot research@mantisvc.com"}, timeout=15)
        if r.status_code == 200:
            data = r.json()
            for entry in data.values():
                name   = entry.get("title", "").lower().strip()
                ticker = entry.get("ticker", "").upper()
                cik    = str(entry.get("cik_str", "")).zfill(10)
                if name and ticker:
                    _SEC_TICKER_LOOKUP[name] = {"ticker": ticker, "cik": cik}
                    # Also index first word and short forms
                    words = name.split()
                    if words and len(words[0]) > 3:
                        if words[0] not in _SEC_TICKER_LOOKUP:
                            _SEC_TICKER_LOOKUP[words[0]] = {"ticker": ticker, "cik": cik}
    except Exception as e:
        print(f"  [sec_lookup] Could not load ticker cache: {e}")
    return _SEC_TICKER_LOOKUP


# ─────────────────────────────────────────────────────────────────────────────
# STATE MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if DISCOVERY_STATE.exists():
        with open(DISCOVERY_STATE) as f:
            return json.load(f)
    return {
        "seen_nct_ids":      [],
        "seen_8k_accessions":[],
        "seen_award_ids":    [],
        "seen_adcom_ids":    [],
        "seen_warpspeed_ids":[],
        "last_run":          None,
    }

def save_state(state: dict):
    DISCOVERY_STATE.parent.mkdir(exist_ok=True)
    with open(DISCOVERY_STATE, "w") as f:
        json.dump(state, f, indent=2)

def load_registry() -> dict:
    if REGISTRY_FILE.exists():
        with open(REGISTRY_FILE) as f:
            return json.load(f)
    return {"watchlist": {}}

def load_active_plays() -> dict:
    if ACTIVE_PLAYS.exists():
        with open(ACTIVE_PLAYS) as f:
            return json.load(f)
    return {"active": {}, "monitor": {}}

def load_ticker_map() -> dict:
    if TICKER_MAP_FILE.exists():
        with open(TICKER_MAP_FILE) as f:
            return json.load(f)
    return {"companies": {}}

def is_already_known(ticker_or_id: str, registry: dict, active_plays: dict) -> bool:
    """Check if a ticker/NCT ID is already in our watchlist or active plays."""
    watchlist = registry.get("watchlist", {})
    active    = active_plays.get("active", {})
    monitor   = active_plays.get("monitor", {})

    key = ticker_or_id.upper()
    if key in watchlist or key in active or key in monitor:
        return True
    # Also check by NCT ID in watchlist values
    for info in watchlist.values():
        if info.get("nct_id") == ticker_or_id:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# COMPANY RESOLVER (lazy import to avoid circular deps)
# ─────────────────────────────────────────────────────────────────────────────

def resolve_and_register(ticker: str, company_name: str = "",
                          drug: str = "", trial: str = "",
                          nct_id: str = "", estimated_announcement: str = "") -> dict:
    """
    Auto-resolve IR URL, CIK, options and write to registry.
    Wraps company_resolver so discovery doesn't need to call it directly.
    Returns the profile dict.
    """
    try:
        sys.path.insert(0, str(BASE_DIR / "intelligence"))
        from company_resolver import resolve_company, update_registry_with_company
        profile = resolve_company(
            ticker=ticker,
            company_name=company_name,
            drug=drug,
            trial=trial,
            nct_id=nct_id,
            estimated_announcement=estimated_announcement,
        )
        update_registry_with_company(profile)
        return profile
    except Exception as e:
        print(f"    [resolver] Error for {ticker}: {e}")
        return {
            "ticker": ticker,
            "company": company_name,
            "drug": drug,
            "trial": trial,
            "nct_id": nct_id,
            "estimated_announcement": estimated_announcement,
            "resolver_error": str(e),
        }


# ─────────────────────────────────────────────────────────────────────────────
# BIOTECH DISCOVERY — SOURCE 1: Warpspeed.sh
# ─────────────────────────────────────────────────────────────────────────────

def discover_warpspeed(state: dict, registry: dict, active_plays: dict) -> list:
    """
    Fetch Warpspeed open experiments via MARKDOWN endpoint (not JS-rendered homepage).
    Warpspeed fetches /forecasts/home-open.md and /forecasts/home-resolved.md client-side.
    These markdown files contain all experiments with P(success) and full thesis text.
    """
    seen = set(state.get("seen_warpspeed_ids", []))
    results = []
    today = datetime.date.today().isoformat()

    print("  [warpspeed] Fetching warpspeed.sh markdown data files...")

    for md_path, is_resolved in [
        ("/forecasts/home-open.md", False),
        ("/forecasts/home-resolved.md", True),
    ]:
        try:
            r = requests.get(f"https://warpspeed.sh{md_path}", headers=HEADERS, timeout=15)
            if r.status_code != 200:
                print(f"  [warpspeed] HTTP {r.status_code} for {md_path}")
                continue

            md = r.text
            print(f"  [warpspeed] {md_path}: {len(md):,} chars")

            # Parse markdown sections: ## [Trial Name](url)
            # Each section starts with ## [Title](/forecast/exp-XXXX)
            sections = re.split(r"(?=^## )", md, flags=re.M)

            for section in sections:
                if not section.strip() or not section.startswith("## ["):
                    continue

                # Extract title and experiment slug
                header_m = re.match(r"## \[([^\]]+)\]\(/forecast/([^)]+)\)", section)
                if not header_m:
                    continue

                title    = header_m.group(1).strip()
                exp_slug = header_m.group(2).strip()  # e.g. exp-idya

                # Extract ticker hint from slug (exp-idya -> IDYA)
                ticker_hint = exp_slug.replace("exp-", "").upper() if exp_slug.startswith("exp-") else ""

                # Extract probability
                prob = None
                prob_m = re.search(r"(\~?(\d{1,3})%|probability of success)", section, re.I)
                if prob_m:
                    digits = re.search(r"(\d{1,3})", prob_m.group(0))
                    if digits:
                        prob = int(digits.group(1))

                # Extract drug name (usually in title before "in")
                drug_m = re.match(r"[^:]+:\s*([^in]+?)\s+in\s+", title, re.I)
                drug = drug_m.group(1).strip() if drug_m else title.split(":")[-1].strip()[:60]

                # Extract indication (after "in")
                ind_m = re.search(r"in\s+(.+)", title, re.I)
                indication = ind_m.group(1).strip()[:80] if ind_m else ""

                # Extract company/ticker from thesis if possible
                company_m = re.search(r"(?:stock|ticker|(IDYA|AGIO|NTLA|RGNX|VRDN|TVTX|AXSM|RVMD|MLTX|RZLT|NAMS|TECX|CYBN|XENE|GOSS))", section)
                ticker = company_m.group(1) if company_m and company_m.group(1) else ticker_hint

                # Use exp_slug as stable ID
                entry_id = exp_slug

                # Skip if already known
                if entry_id in seen:
                    continue

                # Check against registry by ticker
                if ticker and is_already_known(ticker, registry, active_plays):
                    seen.add(entry_id)  # Mark as seen so we don't re-check
                    print(f"  [warpspeed] Already tracked: {ticker} ({title[:50]})")
                    # Still update P(success) if it changed for existing plays
                    if prob and ticker in load_active_plays().get("active", {}):
                        plays = load_active_plays()
                        existing_p = plays["active"][ticker].get("p_success", 0)
                        if abs(existing_p - prob) > 3:  # P changed by more than 3pp
                            plays["active"][ticker]["p_success"] = prob
                            plays["active"][ticker]["last_updated"] = today
                            plays["active"][ticker]["notes"] = plays["active"][ticker].get("notes","") + f" | Warpspeed P updated {prob}% on {today}"
                            with open(ACTIVE_PLAYS, "w") as f2:
                                json.dump(plays, f2, indent=2)
                            print(f"  [warpspeed] P-update: {ticker} {existing_p}% -> {prob}%")
                    continue

                # New experiment — add to results
                seen.add(entry_id)
                candidate = {
                    "source":              "warpspeed",
                    "exp_slug":            exp_slug,
                    "ticker":              ticker,
                    "ticker_hint":         ticker_hint,
                    "drug":                drug,
                    "indication":          indication,
                    "trial_name":          title.split(":")[0].strip()[:60],
                    "p_success_warpspeed": prob,
                    "sector":              "BIOTECH",
                    "resolved":            is_resolved,
                    "full_thesis":         section[:800],
                    "needs_scoring":       prob is None,
                    "needs_ticker_resolution": not bool(ticker),
                    "detected_date":       today,
                }
                results.append(candidate)

                status = "RESOLVED" if is_resolved else "OPEN"
                print(f"  [warpspeed] NEW [{status}]: {exp_slug} | {drug[:30]} | P={prob}%")

        except Exception as e:
            print(f"  [warpspeed] Error fetching {md_path}: {e}")

    state["seen_warpspeed_ids"] = list(seen)[-2000:]
    print(f"  [warpspeed] Found {len(results)} new experiments")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# BIOTECH DISCOVERY — SOURCE 2: ClinicalTrials.gov API v2
# ─────────────────────────────────────────────────────────────────────────────

CLINICALTRIALS_CATEGORIES = [
    "oncology", "immunology", "neurology", "rare disease"
]

def discover_clinicaltrials(state: dict, registry: dict, active_plays: dict) -> list:
    """
    Query ClinicalTrials.gov API v2 for recently updated Phase 3 trials
    with status ACTIVE_NOT_RECRUITING (enrollment done, data coming).
    Filter: lastUpdatePostDate within 90 days.
    """
    seen = set(state.get("seen_nct_ids", []))
    results = []
    today = datetime.date.today()
    cutoff_date = (today - datetime.timedelta(days=90)).isoformat()

    print("  [clinicaltrials] Querying ClinicalTrials.gov API v2...")

    # Build watchlist NCT IDs to exclude
    registry_ncts = {
        info.get("nct_id", "") for info in load_registry().get("watchlist", {}).values()
    }
    registry_ncts.discard("")

    base_url = "https://clinicaltrials.gov/api/v2/studies"
    params = {
        "filter.advanced": "AREA[Phase]PHASE3 AND AREA[OverallStatus]ACTIVE_NOT_RECRUITING",
        "pageSize": 50,
        "sort": "LastUpdatePostDate:desc",
        "countTotal": "true",
    }

    try:
        r = requests.get(base_url, params=params, headers=CT_HEADERS, timeout=25)
        if r.status_code != 200:
            print(f"  [clinicaltrials] HTTP {r.status_code}: {r.text[:200]}")
            return results

        data = r.json()
        studies = data.get("studies", [])
        print(f"  [clinicaltrials] Got {len(studies)} Phase 3 ACTIVE_NOT_RECRUITING studies")

        for study in studies:
            ps = study.get("protocolSection", {})
            id_mod      = ps.get("identificationModule", {})
            status_mod  = ps.get("statusModule", {})
            design_mod  = ps.get("designModule", {})
            sponsor_mod = ps.get("sponsorCollaboratorsModule", {})
            conditions  = ps.get("conditionsModule", {})
            interv_mod  = ps.get("armsInterventionsModule", {})

            nct_id      = id_mod.get("nctId", "")
            brief_title = id_mod.get("briefTitle", "")
            sponsor     = sponsor_mod.get("leadSponsor", {}).get("name", "")
            status      = status_mod.get("overallStatus", "")
            last_update = status_mod.get("lastUpdatePostDateStruct", {}).get("date", "")
            completion  = status_mod.get("primaryCompletionDateStruct", {}).get("date", "")

            # Only keep if updated within 90 days
            if last_update and last_update < cutoff_date:
                continue

            if not nct_id or nct_id in seen or nct_id in registry_ncts:
                continue

            if is_already_known(nct_id, registry, active_plays):
                continue

            # Extract drug names from interventions
            interventions = interv_mod.get("interventions", [])
            drug_names = [
                iv.get("name", "") for iv in interventions
                if iv.get("type", "").upper() in ("DRUG", "BIOLOGICAL", "COMBINATION_PRODUCT", "GENETIC")
            ]
            drug_str = "; ".join(drug_names[:3]) if drug_names else ""

            condition_list = conditions.get("conditions", [])
            condition_str  = "; ".join(condition_list[:2]) if condition_list else ""

            # Immediately try to resolve ticker from SEC lookup (fast, no network call after first load)
            resolved_ticker = ""
            sec_lut = load_sec_ticker_lookup()
            sponsor_lower = sponsor.strip().lower()
            # Try exact match then first-word match
            if sponsor_lower in sec_lut:
                resolved_ticker = sec_lut[sponsor_lower]["ticker"]
            else:
                # Try first significant word
                words = [w for w in sponsor_lower.split() if len(w) > 3 and w not in ("inc", "corp", "ltd", "llc", "the", "pharma", "therapeutics", "biosciences")]
                for word in words[:2]:
                    if word in sec_lut:
                        resolved_ticker = sec_lut[word]["ticker"]
                        break

            # Validate it's a real US-listed equity with options if resolved.
            # M3 update (f-m3-02): chain probe is now Alpaca-backed via
            # biotech_sniper.options_chains.pull_options.pull_chain, replacing
            # the legacy vendor's ``Ticker.options`` lookup.
            has_options = False
            if resolved_ticker:
                try:
                    from biotech_sniper.options_chains.pull_options import (
                        pull_chain,
                    )

                    chain = pull_chain(resolved_ticker)
                    has_options = bool(chain)
                    if not has_options:
                        resolved_ticker = ""  # No options = no tradeable play
                except Exception:
                    resolved_ticker = ""

            candidate = {
                "source": "clinicaltrials_gov",
                "nct_id": nct_id,
                "trial_name": brief_title[:120],
                "company_hint": sponsor,
                "drug": drug_str[:100],
                "indication": condition_str[:80],
                "status": status,
                "last_update": last_update,
                "primary_completion": completion,
                "ticker": resolved_ticker,
                "has_options": has_options,
                "sector": "BIOTECH",
                "needs_ticker_resolution": not bool(resolved_ticker),
                "detected_date": datetime.date.today().isoformat(),
                "search_query": f'"{sponsor}" biotech stock ticker SEC',
            }
            results.append(candidate)
            seen.add(nct_id)
            ticker_tag = f"ticker={resolved_ticker}" if resolved_ticker else "NO TICKER"
            print(f"  [clinicaltrials] NEW: {nct_id} | {sponsor[:40]} | {drug_str[:30]} | {ticker_tag}")

    except Exception as e:
        print(f"  [clinicaltrials] Error: {e}")

    state["seen_nct_ids"] = list(seen)[-3000:]
    print(f"  [clinicaltrials] Found {len(results)} new Phase 3 candidates")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# BIOTECH DISCOVERY — SOURCE 3: SEC EDGAR 8-K Full-text Search
# ─────────────────────────────────────────────────────────────────────────────

def discover_sec_8k_topline(state: dict, registry: dict, active_plays: dict) -> list:
    """
    Search SEC EDGAR full-text for 8-Ks mentioning 'topline' + 'phase 3'
    filed in the last 7 days. Finds companies not in our registry.
    """
    seen = set(state.get("seen_8k_accessions", []))
    results = []
    today = datetime.date.today()
    start_dt = (today - datetime.timedelta(days=7)).isoformat()

    # Build existing tickers to skip
    registry_tickers = set(load_registry().get("watchlist", {}).keys())

    print("  [sec_edgar] Searching EDGAR for topline Phase 3 8-Ks...")

    queries = [
        '"topline" "phase 3"',
        '"top-line" "phase 3"',
        '"primary endpoint" "phase 3" "results"',
    ]

    for query in queries:
        try:
            url = (
                f"https://efts.sec.gov/LATEST/search-index"
                f"?q={requests.utils.quote(query)}"
                f"&dateRange=custom&startdt={start_dt}&enddt={today.isoformat()}"
                f"&forms=8-K"
            )
            r = requests.get(url, headers=SEC_HEADERS, timeout=20)
            if r.status_code != 200:
                print(f"  [sec_edgar] HTTP {r.status_code} for query: {query}")
                continue

            data = r.json()
            hits = data.get("hits", {}).get("hits", [])
            print(f"  [sec_edgar] Query '{query[:40]}': {len(hits)} hits")

            for hit in hits:
                src = hit.get("_source", {})
                accession = src.get("accession_no", "") or hit.get("_id", "")
                entity    = src.get("entity_name", "") or src.get("display_names", [""])[0] if src.get("display_names") else ""
                ticker_raw = src.get("file_num", "")
                filing_date = src.get("period_of_report", "") or src.get("file_date", "")
                cik = src.get("_id", "").split("/")[0] if "/" in src.get("_id", "") else ""

                if not accession or accession in seen:
                    continue

                # Try to get ticker from entity name via SEC lookup
                entity_lower = entity.lower().strip()
                entity_upper = entity.upper()

                # Check registry first (fast)
                already_tracked = any(
                    tkr.upper() in entity_upper or entity_upper.startswith(tkr.upper())
                    for tkr in registry_tickers
                )
                if not already_tracked:
                    # Check SEC company_tickers.json
                    lookup = get_sec_ticker_lookup()
                    sec_match = lookup.get(entity_lower, {})
                    if not sec_match:
                        # Try partial match on first meaningful word
                        first_word = entity_lower.split()[0] if entity_lower.split() else ""
                        if len(first_word) > 4:
                            sec_match = lookup.get(first_word, {})
                    ticker_from_sec = sec_match.get("ticker", "")
                    if ticker_from_sec:
                        already_tracked = is_already_known(ticker_from_sec, registry, active_plays)
                        if not already_tracked:
                            # New company from SEC — populate ticker field
                            cik = sec_match.get("cik", cik)
                else:
                    ticker_from_sec = ""

                if already_tracked:
                    seen.add(accession)
                    continue

                # This is a new company filing a topline 8-K
                candidate = {
                    "source": "sec_edgar_8k",
                    "accession": accession,
                    "entity_name": entity[:100],
                    "filing_date": filing_date,
                    "cik": cik,
                    "ticker": "",
                    "sector": "BIOTECH",
                    "needs_ticker_resolution": True,
                    "search_query": f'"{entity}" stock ticker biotech pharmaceutical',
                    "edgar_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=8-K&count=5",
                    "detected_date": today.isoformat(),
                }
                results.append(candidate)
                seen.add(accession)
                print(f"  [sec_edgar] NEW 8-K: {entity[:50]} | {filing_date}")

        except Exception as e:
            print(f"  [sec_edgar] Error for query '{query}': {e}")

    state["seen_8k_accessions"] = list(seen)[-3000:]
    print(f"  [sec_edgar] Found {len(results)} new 8-K topline candidates")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# BIOTECH DISCOVERY — SOURCE 4: BiopharmCatalyst PDUFA Calendar
# ─────────────────────────────────────────────────────────────────────────────

def discover_biopharmcatalyst_pdufa(state: dict, registry: dict, active_plays: dict) -> list:
    """
    Fetch https://www.biopharmcatalyst.com/calendars/fda-calendar
    Extract upcoming PDUFA dates and catalyst events.
    """
    results = []
    registry_tickers = set(load_registry().get("watchlist", {}).keys())
    seen_adcom = set(state.get("seen_adcom_ids", []))

    print("  [biopharmcatalyst] Fetching FDA calendar...")
    try:
        r = requests.get(
            "https://www.biopharmcatalyst.com/calendars/fda-calendar",
            headers={**HEADERS, "Referer": "https://www.biopharmcatalyst.com/"},
            timeout=20
        )
        if r.status_code != 200:
            print(f"  [biopharmcatalyst] HTTP {r.status_code}")
            return results

        html = r.text

        # Try to extract JSON data embedded in the page (BiopharmCatalyst uses React)
        json_match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', html, re.S)
        if json_match:
            try:
                page_data = json.loads(json_match.group(1))
                # Traverse to find calendar entries
                catalysts = page_data.get("catalysts", page_data.get("calendar", []))
                for entry in (catalysts if isinstance(catalysts, list) else []):
                    ticker = entry.get("ticker", "").upper()
                    if not ticker or ticker in registry_tickers:
                        continue
                    drug  = entry.get("drug", "") or entry.get("catalyst", "")
                    date  = entry.get("date", "") or entry.get("pdufa_date", "")
                    company = entry.get("company", "") or entry.get("name", "")
                    entry_id = f"bpc_{ticker}_{date}"
                    if entry_id in seen_adcom:
                        continue
                    if is_already_known(ticker, registry, active_plays):
                        continue
                    candidate = {
                        "source": "biopharmcatalyst_pdufa",
                        "ticker": ticker,
                        "company": company,
                        "drug": drug,
                        "pdufa_date": date,
                        "sector": "BIOTECH",
                        "needs_ticker_resolution": False,
                        "detected_date": datetime.date.today().isoformat(),
                    }
                    results.append(candidate)
                    seen_adcom.add(entry_id)
                    print(f"  [biopharmcatalyst] NEW PDUFA: {ticker} | {drug[:40]} | {date}")
            except Exception as e:
                print(f"  [biopharmcatalyst] JSON parse error: {e}")

        # Fallback: regex extraction from HTML
        if not results:
            # Look for ticker symbols followed by drug names and dates
            ticker_pattern = re.compile(
                r'(?:ticker|symbol)["\s:]+([A-Z]{2,6})["\s,]+.*?'
                r'(?:pdufa|date|catalyst)["\s:]+([0-9]{4}-[0-9]{2}-[0-9]{2})',
                re.I | re.S
            )
            for m in ticker_pattern.finditer(html):
                ticker = m.group(1).upper()
                date   = m.group(2)
                if ticker in registry_tickers:
                    continue
                if is_already_known(ticker, registry, active_plays):
                    continue
                entry_id = f"bpc_{ticker}_{date}"
                if entry_id in seen_adcom:
                    continue
                candidate = {
                    "source": "biopharmcatalyst_pdufa",
                    "ticker": ticker,
                    "company": "",
                    "drug": "",
                    "pdufa_date": date,
                    "sector": "BIOTECH",
                    "needs_ticker_resolution": False,
                    "detected_date": datetime.date.today().isoformat(),
                }
                results.append(candidate)
                seen_adcom.add(entry_id)
                print(f"  [biopharmcatalyst] NEW (regex): {ticker} | PDUFA {date}")

    except Exception as e:
        print(f"  [biopharmcatalyst] Error: {e}")

    state["seen_adcom_ids"] = list(seen_adcom)[-2000:]
    print(f"  [biopharmcatalyst] Found {len(results)} new PDUFA candidates")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# BIOTECH DISCOVERY — SOURCE 5: Conference Late-Breaking Abstracts
# ─────────────────────────────────────────────────────────────────────────────

CONFERENCE_SEARCH_QUERIES = [
    "late-breaking oral abstract ASCO 2026 phase 3 results",
    "late-breaking abstract AACR 2026 phase 3 clinical trial",
    "late-breaking clinical trial NEJM 2026 phase 3",
    "late-breaking ASH 2026 hematology phase 3",
    "late breaking ESC 2026 cardiovascular trial results",
    "ASCO 2026 presidential symposium phase 3 positive",
    "AACR 2026 plenary session phase 3 efficacy",
]

def build_conference_search_queries() -> list:
    """
    Returns list of search queries the cron agent should run for
    conference late-breaking abstract discovery.
    Late-breaking oral = pre-announced positive abstract = STRONG BULLISH SIGNAL.
    """
    today = datetime.date.today()
    year  = today.year
    return [q.replace("2026", str(year)).replace("2025", str(year)) for q in CONFERENCE_SEARCH_QUERIES]


def parse_conference_results(search_results: list, registry: dict, active_plays: dict) -> list:
    """
    Parse web search results for conference late-breaking abstract signals.
    Called by cron agent after running build_conference_search_queries().
    """
    signals = []
    registry_tickers = set(load_registry().get("watchlist", {}).keys())
    today = datetime.date.today().isoformat()

    late_breaking_kws = [
        "late-breaking oral", "late-breaking abstract", "late breaking",
        "plenary session", "presidential symposium", "lba",
    ]
    positive_kws = [
        "positive", "met primary", "statistically significant", "improvement",
        "survival benefit", "superior", "meaningful", "efficacy",
    ]

    for result in search_results:
        title   = result.get("title", "")
        snippet = result.get("snippet", "") or result.get("body", "")
        url     = result.get("url", "") or result.get("href", "")
        text    = f"{title} {snippet}".lower()

        has_lb   = any(kw in text for kw in late_breaking_kws)
        has_pos  = any(kw in text for kw in positive_kws)

        if not has_lb:
            continue

        # Try to extract ticker from text
        ticker_match = re.search(r'\b([A-Z]{2,6})\b', title)
        ticker = ticker_match.group(1) if ticker_match else ""

        # Filter known tickers (stock-like pattern only)
        if ticker and len(ticker) > 1 and ticker not in {"THE", "FOR", "AND", "FDA", "CEO", "USA", "RCT", "OS", "PFS"}:
            if ticker in registry_tickers:
                continue  # Already tracked

        signals.append({
            "source": "conference_abstract_search",
            "type": "LATE_BREAKING_ABSTRACT",
            "severity": "HIGH" if has_pos else "MODERATE",
            "ticker": ticker,
            "title": title[:200],
            "snippet": snippet[:300],
            "url": url,
            "has_positive_signal": has_pos,
            "detected_date": today,
            "needs_ticker_resolution": bool(ticker),
            "sector": "BIOTECH",
        })

    return signals


# ─────────────────────────────────────────────────────────────────────────────
# CONTRACTS DISCOVERY — SOURCE 3: USASpending IDV Task Orders
# ─────────────────────────────────────────────────────────────────────────────

IDV_AWARD_TYPE_CODES = ["IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E"]

def discover_usaspending_idv(state: dict, ticker_map: dict, days_back: int = 7) -> list:
    """
    IDV (Indefinite Delivery Vehicles) task orders — often unreported alpha.
    Modifications to existing contracts, large value updates.
    """
    results = []
    seen = set(state.get("seen_award_ids", []))
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=days_back)).isoformat()

    # Build search terms from ticker_map — DEFENSE/SPACE only, NOT biotech/pharma
    # Biotech companies like AGIO, IDYA etc. appear in VA/NIH grants — not tradeable signals
    DEFENSE_SECTORS = {"defense", "space", "aerospace", "satellite", "launch", "autonomous",
                       "surveillance", "intelligence", "cyber", "hypersonic", "drone"}
    priority_terms = []
    for company, info in ticker_map.get("companies", {}).items():
        if not info.get("options") or not info.get("ticker"):
            continue
        sector_tag = info.get("sector_tag", "").lower()
        mkt_tier   = info.get("mkt_cap_tier", "")
        # Skip pure pharma/biotech — their "contracts" are NIH/VA grants, not tradeable
        if info.get("is_biotech") or info.get("sector_tag", "").upper() == "BIOTECH":
            continue
        priority_terms.append(company)
        priority_terms.extend(info.get("aliases", [])[:3])

    # Batch into groups of 15 for API calls
    search_terms = list(set(priority_terms))[:30]

    print(f"  [usaspending_idv] Searching IDV task orders ({len(search_terms)} terms)...")

    try:
        payload = {
            "filters": {
                "time_period": [{"start_date": start, "end_date": today.isoformat()}],
                "award_type_codes": IDV_AWARD_TYPE_CODES,
                "keywords": search_terms[:15],  # API limit
            },
            "fields": [
                "Award ID", "Recipient Name", "Award Amount",
                "Awarding Agency", "Awarding Sub Agency",
                "Description", "Period of Performance Start Date",
                "Period of Performance Current End Date",
                "generated_internal_id",
            ],
            "limit": 25, "page": 1,
            "sort": "Award Amount", "order": "desc"
        }
        r = requests.post(
            f"{USA_SPENDING_BASE}/search/spending_by_award/",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=25
        )
        if r.status_code == 200:
            awards = r.json().get("results", [])
            print(f"  [usaspending_idv] Got {len(awards)} IDV awards")

            for award in awards:
                award_id  = award.get("Award ID") or award.get("generated_internal_id", "")
                if award_id and award_id in seen:
                    continue

                recipient = award.get("Recipient Name", "").lower()
                desc      = award.get("Description", "").lower()
                full_text = f"{recipient} {desc}"

                # Match to ticker map
                for company, info in ticker_map.get("companies", {}).items():
                    if not info.get("ticker") or not info.get("options"):
                        continue
                    check = [company.lower()] + [a.lower() for a in info.get("aliases", [])]
                    if any(n and len(n) > 3 and n in full_text for n in check):
                        amount = float(award.get("Award Amount") or 0)
                        result_item = {
                            "source": "usaspending_idv",
                            "ticker": info["ticker"],
                            "company": company,
                            "award_id": award_id,
                            "award_amount": f"${amount/1e6:.1f}M" if amount >= 1e6 else f"${amount:,.0f}",
                            "amount_raw": amount,
                            "agency": award.get("Awarding Agency", ""),
                            "description": award.get("Description", "")[:200],
                            "award_type": "IDV_TASK_ORDER",
                            "sector": "CONTRACT",
                            "severity": "HIGH" if amount > 5e7 else "MODERATE",
                            "detected_date": today.isoformat(),
                        }
                        results.append(result_item)
                        if award_id:
                            seen.add(award_id)
                        print(f"  [usaspending_idv] MATCH: {info['ticker']} | {result_item['award_amount']} | {award.get('Awarding Agency','')[:40]}")
                        break
        else:
            print(f"  [usaspending_idv] HTTP {r.status_code}")

    except Exception as e:
        print(f"  [usaspending_idv] Error: {e}")

    state["seen_award_ids"] = list(seen)[-3000:]
    print(f"  [usaspending_idv] Found {len(results)} IDV matches")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# CONTRACTS DISCOVERY — SOURCE 4: Defense.gov RSS
# ─────────────────────────────────────────────────────────────────────────────

def discover_defense_gov_rss(state: dict, ticker_map: dict, days_back: int = 3) -> list:
    """
    Defense Department publishes EVERY contract awarded same day at:
    https://www.defense.gov/News/Contracts/rss/
    Parse for tracked tickers.
    """
    results = []
    seen = set(state.get("seen_award_ids", []))
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=days_back)

    # Build match terms
    match_terms = {}  # term_lower -> (company, ticker, info)
    for company, info in ticker_map.get("companies", {}).items():
        if not info.get("ticker"):
            continue
        for name in [company] + info.get("aliases", []):
            if name and len(name) > 3:
                match_terms[name.lower()] = (company, info["ticker"], info)

    print("  [defense_rss] Fetching Defense.gov contracts RSS...")
    # Defense.gov blocks datacenter IPs with 403 — try multiple URLs and fallback
    DEFENSE_RSS_URLS = [
        "https://www.defense.gov/News/Contracts/rss/",
        "https://www.defense.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=945&max=10",
    ]
    try:
        # Try feedparser with multiple URLs + user agents
        try:
            import feedparser
            entries = []
            for _rss_url in DEFENSE_RSS_URLS:
                for _ua in [
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
                ]:
                    _r = requests.get(_rss_url, headers={"User-Agent": _ua}, timeout=15)
                    if _r.status_code == 200 and len(_r.text) > 200:
                        feed = feedparser.parse(_r.text)
                        entries = feed.entries
                        if entries:
                            print(f"  [defense_rss] Got {len(entries)} entries from {_rss_url}")
                            break
                if entries:
                    break
            if not entries:
                print("  [defense_rss] All RSS URLs returned 403/empty — flagging for browser_task")
                state.setdefault("browser_task_needed", [])
                if "defense_gov_contracts" not in state.get("browser_task_needed", []):
                    state["browser_task_needed"] = state.get("browser_task_needed", []) + ["defense_gov_contracts"]
        except ImportError:
            r = requests.get(
                "https://www.defense.gov/News/Contracts/rss/",
                headers=HEADERS, timeout=20
            )
            # Parse RSS manually with regex
            entries = []
            items = re.findall(r'<item>(.*?)</item>', r.text, re.S)
            for item in items:
                title_m   = re.search(r'<title>(.*?)</title>', item, re.S)
                summary_m = re.search(r'<description>(.*?)</description>', item, re.S)
                link_m    = re.search(r'<link>(.*?)</link>', item, re.S)
                pub_m     = re.search(r'<pubDate>(.*?)</pubDate>', item, re.S)
                # Parse pubDate
                pub_date_str = pub_m.group(1).strip() if pub_m else ""
                parsed_date = None
                for fmt in ["%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"]:
                    try:
                        import email.utils
                        parsed_tuple = email.utils.parsedate(pub_date_str)
                        if parsed_tuple:
                            parsed_date = datetime.date(*parsed_tuple[:3])
                            break
                    except:
                        pass

                class FakeEntry:
                    pass
                e = FakeEntry()
                e.title   = title_m.group(1).strip() if title_m else ""
                e.summary = re.sub(r'<[^>]+>', '', summary_m.group(1)).strip() if summary_m else ""
                e.link    = link_m.group(1).strip() if link_m else ""
                e.published_parsed = parsed_date.timetuple()[:9] if parsed_date else None
                entries.append(e)

        print(f"  [defense_rss] Got {len(entries)} RSS entries")

        for entry in entries:
            # Parse date
            pub = getattr(entry, "published_parsed", None)
            if pub:
                try:
                    pub_date = datetime.date(*pub[:3])
                    if pub_date < cutoff:
                        continue
                except:
                    pass

            title   = getattr(entry, "title", "") or ""
            summary = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
            link    = getattr(entry, "link", "") or ""

            full_text = f"{title} {summary}".lower()
            # Clean HTML tags
            full_text = re.sub(r'<[^>]+>', ' ', full_text)

            # Extract dollar amount
            amount_match = re.search(
                r'\$\s*([\d,]+(?:\.\d+)?)\s*(million|billion|thousand)?',
                f"{title} {summary}", re.I
            )
            amount_str = ""
            if amount_match:
                num_str = amount_match.group(1).replace(",", "")
                mult_str = (amount_match.group(2) or "").lower()
                try:
                    num = float(num_str)
                    if "billion" in mult_str:
                        num *= 1e9
                    elif "million" in mult_str:
                        num *= 1e6
                    amount_str = f"${num/1e6:.1f}M" if num >= 1e6 else f"${num:,.0f}"
                except:
                    amount_str = amount_match.group(0)

            # Generate entry ID
            entry_id = f"dod_{hashlib.md5(link.encode()).hexdigest()[:10]}" if link else \
                       f"dod_{hashlib.md5(title.encode()).hexdigest()[:10]}"

            if entry_id in seen:
                continue

            # Match to ticker map
            matched = False
            for term, (company, ticker, info) in match_terms.items():
                if term in full_text:
                    results.append({
                        "source": "defense_gov_rss",
                        "ticker": ticker,
                        "company": company,
                        "entry_id": entry_id,
                        "title": title[:200],
                        "description": summary[:400],
                        "amount": amount_str,
                        "link": link,
                        "sector": "CONTRACT",
                        "award_type": "DOD_CONTRACT",
                        "severity": "HIGH" if amount_str else "MODERATE",
                        "detected_date": today.isoformat(),
                    })
                    seen.add(entry_id)
                    matched = True
                    print(f"  [defense_rss] MATCH: {ticker} | {amount_str} | {title[:60]}")
                    break

            # Even unmatched entries are returned for awareness (no ticker)
            if not matched:
                results.append({
                    "source": "defense_gov_rss",
                    "ticker": "",
                    "company": "",
                    "entry_id": entry_id,
                    "title": title[:200],
                    "description": summary[:300],
                    "amount": amount_str,
                    "link": link,
                    "sector": "CONTRACT",
                    "award_type": "DOD_CONTRACT_UNMATCHED",
                    "severity": "LOW",
                    "detected_date": today.isoformat(),
                })
                seen.add(entry_id)

    except Exception as e:
        print(f"  [defense_rss] Error: {e}")

    state["seen_award_ids"] = list(seen)[-3000:]
    matched_count = sum(1 for r in results if r.get("ticker"))
    print(f"  [defense_rss] Found {len(results)} entries ({matched_count} ticker matches)")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# CONTRACTS DISCOVERY — SOURCE 5: NASA SEWP / Procurement
# ─────────────────────────────────────────────────────────────────────────────

def build_nasa_search_queries(ticker_map: dict) -> list:
    """
    Build NASA procurement search queries for the cron agent to run.
    Returns list of query strings.
    """
    today = datetime.date.today()
    month = today.strftime("%B")
    year  = today.year

    # Focus on space/launch companies
    space_tickers = [
        company for company, info in ticker_map.get("companies", {}).items()
        if info.get("ticker") and any(kw in company.lower() for kw in
           ["rocket", "space", "satellite", "launch", "orbital", "astro"])
    ]

    queries = []
    for company in space_tickers[:8]:
        queries.append(f"nasa.gov contract award {month} {year} \"{company}\"")
        queries.append(f"site:nasa.gov \"{company}\" contract award {year}")

    # Generic NASA procurement queries
    queries += [
        f"NASA SEWP contract award {month} {year} launch services",
        f"NASA commercial lunar payload services CLPS award {year}",
        f"NASA space launch system contract {year} award",
    ]
    return queries


# ─────────────────────────────────────────────────────────────────────────────
# CONTRACTS DISCOVERY — SAM.gov J&A URL Patterns
# ─────────────────────────────────────────────────────────────────────────────

def build_sam_gov_search_patterns() -> dict:
    """
    Returns URL patterns and search terms for the cron agent to use
    via browser_task on SAM.gov (requires JS rendering).
    """
    return {
        "justification_url": (
            "https://sam.gov/search/?index=opp&pageSize=25&sort=-modifiedDate"
            "&sfm[noticeType][0]=Justification"
        ),
        "presolicitation_url": (
            "https://sam.gov/search/?index=opp&pageSize=25&sort=-modifiedDate"
            "&sfm[noticeType][0]=Presolicitation"
        ),
        "sole_source_url": (
            "https://sam.gov/search/?index=opp&pageSize=25&sort=-modifiedDate"
            "&sfm[noticeType][0]=Justification&sfm[naics][0]=336414"
        ),
        "search_terms": [
            "rocket lab", "kratos", "ast spacemobile", "intuitive machines",
            "mercury systems", "joby aviation", "archer aviation", "palantir",
            "directed energy", "autonomous systems", "hypersonic",
            "counter-uas", "satellite communications", "reusable launch"
        ],
        "instructions": (
            "For each URL, load the page, wait 3s for JS, then extract "
            "notice titles + dates + companies. Match companies to tickers. "
            "J&A = sole-source filing = ~87% win probability. HIGH signal."
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# ADCOM DISCOVERY — Multi-source
# ─────────────────────────────────────────────────────────────────────────────

def discover_adcom_rss(state: dict, registry: dict, active_plays: dict) -> list:
    """
    Fetch FDA AdCom RSS feed — always works, parse XML.
    https://www.fda.gov/feeds/advisory-committee-meetings-coming-soon.rss
    """
    results = []
    seen = set(state.get("seen_adcom_ids", []))
    today = datetime.date.today()

    print("  [adcom_rss] Fetching FDA AdCom RSS...")
    try:
        try:
            import feedparser
            feed = feedparser.parse(
                "https://www.fda.gov/feeds/advisory-committee-meetings-coming-soon.rss"
            )
            entries = feed.entries
        except ImportError:
            r = requests.get(
                "https://www.fda.gov/feeds/advisory-committee-meetings-coming-soon.rss",
                headers=HEADERS, timeout=20
            )
            entries = []
            items = re.findall(r'<item>(.*?)</item>', r.text, re.S)
            for item in items:
                title_m   = re.search(r'<title><!\[CDATA\[(.*?)\]\]></title>', item, re.S) or \
                            re.search(r'<title>(.*?)</title>', item, re.S)
                summary_m = re.search(r'<description><!\[CDATA\[(.*?)\]\]></description>', item, re.S) or \
                            re.search(r'<description>(.*?)</description>', item, re.S)
                link_m    = re.search(r'<link>(.*?)</link>', item, re.S)
                pub_m     = re.search(r'<pubDate>(.*?)</pubDate>', item, re.S)

                class FakeEntry:
                    pass
                e = FakeEntry()
                e.title   = title_m.group(1).strip() if title_m else ""
                e.summary = re.sub(r'<[^>]+>', '', summary_m.group(1)).strip() if summary_m else ""
                e.link    = link_m.group(1).strip() if link_m else ""
                pub_str   = pub_m.group(1).strip() if pub_m else ""
                try:
                    import email.utils
                    pt = email.utils.parsedate(pub_str)
                    e.published_parsed = pt
                except:
                    e.published_parsed = None
                entries.append(e)

        print(f"  [adcom_rss] Got {len(entries)} AdCom RSS entries")

        # Import drug-ticker map from adcom_scanner
        try:
            sys.path.insert(0, str(BASE_DIR / "sectors/adcom"))
            from adcom_scanner import build_drug_ticker_map
            drug_map = build_drug_ticker_map()
        except Exception as e:
            print(f"  [adcom_rss] Could not load drug_ticker_map: {e}")
            drug_map = {}

        for entry in entries:
            title   = getattr(entry, "title", "") or ""
            summary = getattr(entry, "summary", "") or ""
            link    = getattr(entry, "link", "") or ""

            entry_id = f"adcom_{hashlib.md5((title + link).encode()).hexdigest()[:12]}"
            if entry_id in seen:
                continue

            # Extract date from title or summary
            date_match = re.search(
                r'(\w+ \d{1,2},?\s*\d{4}|\d{4}-\d{2}-\d{2})',
                f"{title} {summary}"
            )
            meeting_date_str = date_match.group(1) if date_match else ""

            # Parse date
            meeting_date = None
            for fmt in ["%B %d, %Y", "%B %d %Y", "%Y-%m-%d", "%b %d, %Y"]:
                try:
                    meeting_date = datetime.datetime.strptime(
                        meeting_date_str.strip(), fmt
                    ).date()
                    break
                except:
                    continue

            days_out = (meeting_date - today).days if meeting_date else None

            # Map drug → ticker
            full_text = f"{title} {summary}".lower()
            ticker = ""
            drug_name = ""
            for drug_key, drug_info in drug_map.items():
                if drug_key.lower() in full_text:
                    ticker    = drug_info.get("ticker", "")
                    drug_name = drug_key
                    break

            if not ticker:
                # Flag for cron agent to resolve
                drug_match = re.search(r'\b([A-Z][a-z]+(?:mab|nib|zib|tinib|ciclib|umab|imab|ximab|zumab|lumab|tumab|mumab|olomab))\b', title)
                drug_name  = drug_match.group(1) if drug_match else ""

            already_in_registry = ticker and is_already_known(ticker, registry, active_plays)

            candidate = {
                "source": "fda_adcom_rss",
                "entry_id": entry_id,
                "title": title[:200],
                "summary": summary[:400],
                "link": link,
                "meeting_date": meeting_date.isoformat() if meeting_date else "",
                "days_out": days_out,
                "ticker": ticker,
                "drug_name": drug_name,
                "sector": "ADCOM",
                "severity": "HIGH" if days_out is not None and days_out <= 14 else "MODERATE",
                "already_tracked": already_in_registry,
                "needs_ticker_resolution": not bool(ticker),
                "search_query": f'"{drug_name}" company stock ticker FDA' if drug_name and not ticker else "",
                "detected_date": today.isoformat(),
            }
            results.append(candidate)
            seen.add(entry_id)

            status = "TRACKED" if already_in_registry else "NEW"
            print(f"  [adcom_rss] {status}: {ticker or '???'} | {title[:60]} | {meeting_date}")

    except Exception as e:
        print(f"  [adcom_rss] Error: {e}")

    state["seen_adcom_ids"] = list(seen)[-2000:]
    new_count = sum(1 for r in results if not r.get("already_tracked"))
    print(f"  [adcom_rss] Found {len(results)} AdCom entries ({new_count} new)")
    return results


def discover_adcom_biopharmcatalyst(state: dict, registry: dict, active_plays: dict) -> list:
    """
    Fetch BiopharmCatalyst AdCom calendar.
    https://www.biopharmcatalyst.com/calendars/adcom-calendar
    """
    results = []
    seen = set(state.get("seen_adcom_ids", []))
    today = datetime.date.today()

    print("  [adcom_bpc] Fetching BiopharmCatalyst AdCom calendar...")
    try:
        r = requests.get(
            "https://www.biopharmcatalyst.com/calendars/adcom-calendar",
            headers={**HEADERS, "Referer": "https://www.biopharmcatalyst.com/"},
            timeout=20
        )
        if r.status_code != 200:
            print(f"  [adcom_bpc] HTTP {r.status_code}")
            return results

        html = r.text

        # BiopharmCatalyst embeds data in various JS structures
        # Try to find table rows with ticker + date
        ticker_date_pattern = re.compile(
            r'(?:<td[^>]*>|"ticker":")["\s]*([A-Z]{2,6})["\s]*(?:</td>|",?)'
            r'.*?(?:<td[^>]*>|"date":")["\s]*(\d{4}-\d{2}-\d{2})["\s]*(?:</td>|",?)',
            re.S | re.I
        )
        for m in ticker_date_pattern.finditer(html):
            ticker = m.group(1).upper()
            date   = m.group(2)
            entry_id = f"adcom_bpc_{ticker}_{date}"
            if entry_id in seen:
                continue
            if is_already_known(ticker, registry, active_plays):
                seen.add(entry_id)
                continue

            try:
                meeting_date = datetime.date.fromisoformat(date)
                days_out = (meeting_date - today).days
            except:
                days_out = None

            candidate = {
                "source": "biopharmcatalyst_adcom",
                "ticker": ticker,
                "meeting_date": date,
                "days_out": days_out,
                "sector": "ADCOM",
                "severity": "HIGH" if days_out is not None and days_out <= 14 else "MODERATE",
                "needs_ticker_resolution": False,
                "detected_date": today.isoformat(),
            }
            results.append(candidate)
            seen.add(entry_id)
            print(f"  [adcom_bpc] NEW: {ticker} | AdCom {date} ({days_out} days)")

    except Exception as e:
        print(f"  [adcom_bpc] Error: {e}")

    state["seen_adcom_ids"] = list(seen)[-2000:]
    return results


def discover_adcom_fdatracker(state: dict, registry: dict, active_plays: dict) -> list:
    """
    Fetch FDTracker FDA calendar.
    https://www.fdatracker.com/fda-calendar/
    """
    results = []
    seen = set(state.get("seen_adcom_ids", []))
    today = datetime.date.today()

    print("  [adcom_fdatracker] Fetching FDTracker calendar...")
    try:
        r = requests.get(
            "https://www.fdatracker.com/fda-calendar/",
            headers=HEADERS,
            timeout=20
        )
        if r.status_code != 200:
            print(f"  [adcom_fdatracker] HTTP {r.status_code} — flagging for browser_task")
            return [{"source": "fdatracker", "needs_browser_task": True,
                     "browser_url": "https://www.fdatracker.com/fda-calendar/",
                     "detected_date": today.isoformat()}]

        html = r.text
        # Try to extract company/drug/date combos
        try:
            sys.path.insert(0, str(BASE_DIR / "sectors/adcom"))
            from adcom_scanner import build_drug_ticker_map
            drug_map = build_drug_ticker_map()
        except:
            drug_map = {}

        # Look for date patterns followed by drug/company info
        date_blocks = re.finditer(
            r'(\d{4}-\d{2}-\d{2}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2},?\s*\d{4})',
            html
        )
        for dm in date_blocks:
            date_str = dm.group(1)
            try:
                for fmt in ["%Y-%m-%d", "%B %d, %Y", "%B %d %Y", "%b %d, %Y"]:
                    try:
                        meeting_date = datetime.datetime.strptime(date_str.strip(), fmt).date()
                        break
                    except:
                        meeting_date = None

                if not meeting_date or meeting_date < today:
                    continue

                # Context around date
                start = max(0, dm.start() - 100)
                end   = min(len(html), dm.end() + 300)
                context = html[start:end].lower()
                context_clean = re.sub(r'<[^>]+>', ' ', context)

                # Find matching drug
                ticker = ""
                drug   = ""
                for drug_key, drug_info in drug_map.items():
                    if drug_key.lower() in context_clean:
                        ticker = drug_info.get("ticker", "")
                        drug   = drug_key
                        break

                if not ticker:
                    continue

                entry_id = f"fdatracker_{ticker}_{meeting_date.isoformat()}"
                if entry_id in seen:
                    continue
                if is_already_known(ticker, registry, active_plays):
                    seen.add(entry_id)
                    continue

                days_out = (meeting_date - today).days
                candidate = {
                    "source": "fdatracker",
                    "ticker": ticker,
                    "drug": drug,
                    "meeting_date": meeting_date.isoformat(),
                    "days_out": days_out,
                    "sector": "ADCOM",
                    "severity": "HIGH" if days_out <= 14 else "MODERATE",
                    "needs_ticker_resolution": False,
                    "detected_date": today.isoformat(),
                }
                results.append(candidate)
                seen.add(entry_id)
                print(f"  [adcom_fdatracker] NEW: {ticker} | {drug} | {meeting_date}")

            except Exception:
                continue

    except Exception as e:
        print(f"  [adcom_fdatracker] Error: {e}")

    state["seen_adcom_ids"] = list(seen)[-2000:]
    return results


def check_fda_briefing_docs_48h(adcom_entries: list) -> list:
    """
    For any AdCom within 48 hours, fetch briefing docs and run keyword scoring.
    Returns list of briefing analysis results with sentiment -10 to +10.
    """
    results = []
    today = datetime.date.today()

    BEARISH = [
        "the committee should consider whether", "the applicant has not demonstrated",
        "the agency is concerned", "questions remain regarding",
        "the data do not support", "the primary endpoint was not met",
        "whether the totality of evidence", "discuss the uncertainties",
        "what additional", "are the data adequate", "unresolved questions",
        "major concerns", "the fda notes that",
    ]
    BULLISH = [
        "substantial evidence of effectiveness", "the data support",
        "clinically meaningful", "the benefit-risk is favorable",
        "unprecedented efficacy", "breakthrough",
        "the agency agrees with the applicant",
        "statistically significant and clinically meaningful",
        "no new safety signals", "adequate and well-controlled",
        "the totality of evidence supports",
    ]

    for entry in adcom_entries:
        days_out = entry.get("days_out")
        if days_out is None or days_out > 2 or days_out < 0:
            continue

        link = entry.get("link", "")
        if not link:
            continue

        ticker = entry.get("ticker", "")
        print(f"  [briefing_docs] Checking briefing docs for {ticker} (meeting in {days_out}d)...")

        try:
            r = requests.get(link, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                continue

            # Find PDF links in the meeting page
            pdf_links = re.findall(
                r'href=["\']([^"\']*\.pdf[^"\']*)["\']',
                r.text, re.I
            )
            # Also look for briefing document links
            briefing_links = [
                url for url in pdf_links
                if any(kw in url.lower() for kw in ["briefing", "background", "sponsor"])
            ]
            briefing_links = [
                ("https://www.fda.gov" + url if url.startswith("/") else url)
                for url in briefing_links[:3]
            ]

            if not briefing_links:
                results.append({
                    "ticker": ticker,
                    "meeting_date": entry.get("meeting_date", ""),
                    "days_out": days_out,
                    "briefing_docs_found": False,
                    "sentiment_score": 0,
                    "note": "No briefing docs found yet (usually posted 24-48h before)",
                })
                continue

            # Fetch and score first briefing doc (FDA analysis is most important)
            all_text = ""
            for pdf_url in briefing_links[:1]:
                try:
                    pr = requests.get(pdf_url, headers=HEADERS, timeout=20, stream=True)
                    # Can't parse PDF in pure Python without pdfminer, but we can
                    # get the raw bytes and search for text patterns
                    content = pr.content
                    # Try to extract text from PDF bytes (basic approach)
                    text_raw = content.decode("latin-1", errors="replace").lower()
                    all_text += text_raw
                except Exception as e:
                    print(f"    briefing doc fetch error: {e}")

            if all_text:
                bearish_count = sum(1 for p in BEARISH if p in all_text)
                bullish_count = sum(1 for p in BULLISH if p in all_text)
                question_count = all_text.count("?")
                net = bullish_count - bearish_count
                sentiment = max(-10, min(10, net * 2))

                key_quotes = []
                for phrase in BEARISH[:3]:
                    idx = all_text.find(phrase)
                    if idx >= 0:
                        key_quotes.append(all_text[max(0,idx-50):idx+len(phrase)+100].strip()[:200])

                results.append({
                    "ticker": ticker,
                    "meeting_date": entry.get("meeting_date", ""),
                    "days_out": days_out,
                    "briefing_docs_found": True,
                    "briefing_urls": briefing_links,
                    "sentiment_score": sentiment,
                    "bearish_count": bearish_count,
                    "bullish_count": bullish_count,
                    "question_count": question_count,
                    "key_quotes": key_quotes[:3],
                    "recommendation": (
                        "BEARISH_SIGNAL" if sentiment <= -3 else
                        "BULLISH_SIGNAL" if sentiment >= 3 else
                        "NEUTRAL"
                    ),
                    "detected_date": today.isoformat(),
                })
                print(f"  [briefing_docs] {ticker}: sentiment={sentiment} ({bullish_count}B/{bearish_count}Be/{question_count}?)")

        except Exception as e:
            print(f"  [briefing_docs] Error for {ticker}: {e}")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# MAIN DISCOVERY FUNCTION
# ─────────────────────────────────────────────────────────────────────────────

def run_discovery() -> dict:
    """
    Unified discovery pipeline. Runs as Step 0b of the daily 6AM cron.
    Finds NEW candidates in all 3 sectors NOT already in active_plays.json.

    Returns:
    {
        "new_biotech":    [...],   # new candidates not yet scored
        "new_contracts":  [...],   # new contract opportunities
        "new_adcom":      [...],   # new AdCom meetings found
        "all_new":        [...],   # combined list for scoring queue
        "defense_rss":    [...],   # defense.gov contract announcements
        "signals":        [...],   # breaking signals found during discovery
        "run_date":       "...",
        "sam_gov_urls":   {...},   # URL patterns for cron agent
        "nasa_queries":   [...],   # NASA search queries for cron agent
        "conference_queries": [...],  # Conference abstract queries
        "briefing_analysis": [...],  # FDA briefing doc results (48h window)
    }
    """
    today = datetime.date.today().isoformat()
    state       = load_state()
    registry    = load_registry()
    active_plays = load_active_plays()
    ticker_map  = load_ticker_map()

    print(f"\n{'='*70}")
    print(f"MASTER DISCOVERY ENGINE — {today}")
    print(f"Registry: {len(registry.get('watchlist',{}))} companies | "
          f"Active plays: {len(active_plays.get('active',{}))}")
    print(f"{'='*70}")

    all_new_biotech   = []
    all_new_contracts = []
    all_new_adcom     = []
    all_defense_rss   = []
    all_signals       = []

    # ── BIOTECH DISCOVERY ────────────────────────────────────────────────────
    print(f"\n[BIOTECH DISCOVERY]")

    # Source 1: Warpspeed.sh
    ws_candidates = discover_warpspeed(state, registry, active_plays)
    all_new_biotech.extend(ws_candidates)

    # Source 2: ClinicalTrials.gov
    ct_candidates = discover_clinicaltrials(state, registry, active_plays)
    all_new_biotech.extend(ct_candidates)

    # Source 3: SEC EDGAR 8-K topline
    sec_candidates = discover_sec_8k_topline(state, registry, active_plays)
    all_new_biotech.extend(sec_candidates)

    # Source 4: BiopharmCatalyst PDUFA
    bpc_candidates = discover_biopharmcatalyst_pdufa(state, registry, active_plays)
    all_new_biotech.extend(bpc_candidates)

    # Source 5: ClinicalTrials IMMINENT completions (primary endpoint in next 60 days)
    # This is the earliest possible signal — finds trials right before topline data exists
    ct_imminent = discover_clinicaltrials_imminent(state, registry, active_plays)
    all_new_biotech.extend(ct_imminent)

    # Source 6: News RSS — Endpoints/STAT/GlobeNewswire/PRNewswire
    # Breaks topline data 15-60 min before SEC 8-K filing
    news_signals = discover_news_rss(state, registry, active_plays)
    for ns in news_signals:
        if ns.get("is_topline"):
            all_signals.append(ns)   # immediate alert signal
        elif ns.get("needs_ticker_resolution") and not ns.get("is_watchlist"):
            all_new_biotech.append(ns)   # new company, needs scoring

    # Source 7: Conference abstract queries (agent will run these)
    conference_queries = build_conference_search_queries()

    # ── CONTRACTS DISCOVERY ──────────────────────────────────────────────────
    print(f"\n[CONTRACTS DISCOVERY]")

    # Source 1: USASpending IDV task orders
    idv_awards = discover_usaspending_idv(state, ticker_map, days_back=7)
    all_new_contracts.extend(idv_awards)

    # Source 2: Defense.gov RSS (new source)
    defense_rss = discover_defense_gov_rss(state, ticker_map, days_back=3)
    all_defense_rss.extend(defense_rss)
    # Matched defense items are also new contract signals
    matched_defense = [r for r in defense_rss if r.get("ticker")]
    all_new_contracts.extend(matched_defense)

    # Source 3: SAM.gov J&A patterns for cron agent
    sam_gov_patterns = build_sam_gov_search_patterns()

    # Source 4: NASA search queries for cron agent
    nasa_queries = build_nasa_search_queries(ticker_map)

    # ── ADCOM DISCOVERY ──────────────────────────────────────────────────────
    print(f"\n[ADCOM DISCOVERY]")

    # Source 1: FDA AdCom RSS
    adcom_rss = discover_adcom_rss(state, registry, active_plays)
    all_new_adcom.extend([e for e in adcom_rss if not e.get("already_tracked")])

    # Source 2: BiopharmCatalyst AdCom calendar
    adcom_bpc = discover_adcom_biopharmcatalyst(state, registry, active_plays)
    all_new_adcom.extend(adcom_bpc)

    # Source 3: FDTracker
    adcom_fdt = discover_adcom_fdatracker(state, registry, active_plays)
    all_new_adcom.extend([e for e in adcom_fdt if not e.get("needs_browser_task")])

    # Source 4: Federal Register FDA notices (always works — JSON API)
    try:
        sys.path.insert(0, str(BASE_DIR / "sectors/adcom"))
        from adcom_scanner import fetch_federal_register_adcom, build_drug_ticker_map
        fed_reg_meetings = fetch_federal_register_adcom()
        drug_map_for_fr  = build_drug_ticker_map()
        seen_adcom_ids   = set(state.get("seen_adcom_ids", []))
        for mtg in fed_reg_meetings:
            title    = mtg.get("committee", "")
            summary  = mtg.get("topic", "")
            full_txt = f"{title} {summary}".lower()
            ticker   = ""
            drug_n   = ""
            for dk, dv in drug_map_for_fr.items():
                if dk.lower() in full_txt:
                    ticker = dv.get("ticker", "")
                    drug_n = dk
                    break
            entry_id = f"fr_{hashlib.md5((title+mtg.get('link','')).encode()).hexdigest()[:10]}"
            if entry_id in seen_adcom_ids:
                continue
            if ticker and is_already_known(ticker, registry, active_plays):
                seen_adcom_ids.add(entry_id)
                continue
            candidate = {
                "source": "federal_register_fda",
                "entry_id": entry_id,
                "title": title[:200],
                "summary": summary[:300],
                "link": mtg.get("link", ""),
                "date_text": mtg.get("date_text", ""),
                "ticker": ticker,
                "drug_name": drug_n,
                "sector": "ADCOM",
                "severity": "MODERATE",
                "needs_ticker_resolution": not bool(ticker),
                "detected_date": datetime.date.today().isoformat(),
            }
            all_new_adcom.append(candidate)
            seen_adcom_ids.add(entry_id)
        state["seen_adcom_ids"] = list(seen_adcom_ids)[-2000:]
    except Exception as e:
        print(f"  [fed_reg_adcom] Error: {e}")

    # Source 4: Briefing documents for AdComs within 48 hours
    all_adcom_for_briefing = adcom_rss + adcom_bpc
    briefing_results = check_fda_briefing_docs_48h(all_adcom_for_briefing)

    # ── AUTO-RESOLVE NEW COMPANIES ───────────────────────────────────────────
    print(f"\n[AUTO-RESOLVING NEW COMPANIES]")

    # Resolve any biotech candidates that already have a ticker
    for candidate in all_new_biotech:
        ticker = candidate.get("ticker", "")
        if ticker and not candidate.get("needs_ticker_resolution"):
            print(f"  Resolving {ticker}...")
            profile = resolve_and_register(
                ticker=ticker,
                company_name=candidate.get("company_hint", "") or candidate.get("company", ""),
                drug=candidate.get("drug", ""),
                trial=candidate.get("trial_name", "") or candidate.get("trial", ""),
                nct_id=candidate.get("nct_id", ""),
                estimated_announcement=candidate.get("primary_completion", ""),
            )
            candidate["resolved_profile"] = profile

    # Resolve new AdCom companies (those with tickers)
    for candidate in all_new_adcom:
        ticker = candidate.get("ticker", "")
        if ticker and not candidate.get("needs_ticker_resolution"):
            profile = resolve_and_register(
                ticker=ticker,
                company_name=candidate.get("company", ""),
                drug=candidate.get("drug_name", "") or candidate.get("drug", ""),
                estimated_announcement=candidate.get("meeting_date", ""),
            )
            candidate["resolved_profile"] = profile

    # ── BUILD SIGNALS FROM BRIEFING DOCS ────────────────────────────────────
    for br in briefing_results:
        if br.get("sentiment_score", 0) != 0:
            severity = "HIGH" if abs(br["sentiment_score"]) >= 5 else "MODERATE"
            all_signals.append({
                "source": "fda_briefing_doc",
                "type": "BRIEFING_DOC_SENTIMENT",
                "ticker": br.get("ticker", ""),
                "severity": severity,
                "sentiment_score": br["sentiment_score"],
                "recommendation": br.get("recommendation", ""),
                "meeting_date": br.get("meeting_date", ""),
                "days_out": br.get("days_out"),
                "detected_date": today,
            })

    # ── COMBINE ALL NEW ──────────────────────────────────────────────────────
    all_new = all_new_biotech + all_new_contracts + all_new_adcom

    # De-duplicate by ticker+source
    seen_combo = set()
    deduped_new = []
    for item in all_new:
        key = f"{item.get('ticker','?')}_{item.get('source','?')}_{item.get('nct_id','')}"
        if key not in seen_combo:
            seen_combo.add(key)
            deduped_new.append(item)

    # ── SAVE STATE ───────────────────────────────────────────────────────────
    save_state(state)

    # ── SAVE DISCOVERY REPORT ────────────────────────────────────────────────
    result = {
        "run_date": today,
        "new_biotech":      all_new_biotech,
        "new_contracts":    [r for r in all_new_contracts if r.get("ticker")],
        "new_adcom":        all_new_adcom,
        "all_new":          deduped_new,
        "defense_rss":      all_defense_rss,
        "signals":          all_signals,
        "briefing_analysis": briefing_results,
        "sam_gov_urls":     sam_gov_patterns,
        "nasa_queries":     nasa_queries,
        "conference_queries": conference_queries,
        "summary": {
            "total_new_biotech":   len(all_new_biotech),
            "total_new_contracts": len([r for r in all_new_contracts if r.get("ticker")]),
            "total_new_adcom":     len(all_new_adcom),
            "total_defense_rss":   len(all_defense_rss),
            "defense_rss_matched": len([r for r in all_defense_rss if r.get("ticker")]),
            "total_signals":       len(all_signals),
            "needs_ticker_resolution": sum(1 for x in deduped_new if x.get("needs_ticker_resolution")),
        }
    }

    report_file = REPORTS_DIR / f"discovery_{today}.json"
    with open(report_file, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\n{'='*70}")
    print(f"DISCOVERY COMPLETE — {today}")
    print(f"  New biotech:   {result['summary']['total_new_biotech']}")
    print(f"  New contracts: {result['summary']['total_new_contracts']}")
    print(f"  New AdCom:     {result['summary']['total_new_adcom']}")
    print(f"  Defense RSS:   {result['summary']['total_defense_rss']} entries ({result['summary']['defense_rss_matched']} matched)")
    print(f"  Signals:       {result['summary']['total_signals']}")
    print(f"  Saved: {report_file}")
    print(f"{'='*70}\n")

    return result


if __name__ == "__main__":
    result = run_discovery()
    print(json.dumps(result["summary"], indent=2))


# ─────────────────────────────────────────────────────────────────────────────
# BIOTECH DISCOVERY — SOURCE 5: ClinicalTrials "primary completion next 60 days"
# NEW HIGH-ALPHA SOURCE — finds trials right before data drops
# ─────────────────────────────────────────────────────────────────────────────

def discover_clinicaltrials_imminent(state: dict, registry: dict, active_plays: dict) -> list:
    """
    Query ClinicalTrials for trials where PRIMARY COMPLETION DATE is in the next 60 days.
    This is the earliest possible signal — finds trials right before topline data exists.
    Status must be ACTIVE_NOT_RECRUITING (enrollment done, observing outcomes).
    
    This is fundamentally different from our existing CT query which uses a 90-day update filter.
    This directly targets "data about to drop" trials regardless of last update date.
    """
    seen = set(state.get("seen_nct_ids", []))
    results = []
    today = datetime.date.today()
    end_date = (today + datetime.timedelta(days=60)).isoformat()

    # Build registry NCT IDs to skip
    registry_ncts = {
        info.get("nct_id", "") 
        for info in load_registry().get("watchlist", {}).values()
    }
    registry_ncts.discard("")

    print("  [ct_imminent] Querying ClinicalTrials for primary completions in next 60 days...")

    # Skip known academic/non-public sponsors
    SKIP_SPONSORS = [
        'university', 'hospital', 'institute', 'college', 'center', 'centre',
        'national cancer', 'nci ', 'mayo clinic', 'memorial', 'stanford', 'harvard',
        'brigham', 'cedars', 'kaiser', 'veterans', 'va ', 'nih ', 'niaid',
        'sichuan', 'suzhou', 'wuhan', 'beijing', 'jiangsu', 'shanghai', 'guangdong',
        'hospices', 'hershey', 'icahn', 'sinai',
    ]

    try:
        r = requests.get(
            "https://clinicaltrials.gov/api/v2/studies",
            params={
                "filter.advanced": f"AREA[Phase]PHASE3 AND AREA[OverallStatus]ACTIVE_NOT_RECRUITING AND AREA[PrimaryCompletionDate]RANGE[{today.isoformat()},{end_date}]",
                "pageSize": 100,
                "sort": "PrimaryCompletionDate",
                "fields": "NCTId,BriefTitle,LeadSponsorName,PrimaryCompletionDate,Condition,InterventionName,OverallStatus",
            },
            headers=CT_HEADERS, timeout=20
        )
        if r.status_code != 200:
            print(f"  [ct_imminent] HTTP {r.status_code}")
            return results

        studies = r.json().get("studies", [])
        print(f"  [ct_imminent] Found {len(studies)} trials completing primary endpoint in 60 days")

        for study in studies:
            p = study.get("protocolSection", {})
            ident    = p.get("identificationModule", {})
            stat_mod = p.get("statusModule", {})
            sponsor  = p.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name", "")
            nct_id   = ident.get("nctId", "")
            title    = ident.get("briefTitle", "")
            cond     = p.get("conditionsModule", {}).get("conditions", [""])[0]
            drug     = p.get("interventionsModule", {}).get("interventions", [{}])[0].get("name", "") if p.get("interventionsModule", {}).get("interventions") else ""
            completion = stat_mod.get("primaryCompletionDateStruct", {}).get("date", "")

            if not nct_id or nct_id in seen:
                continue
            if nct_id in registry_ncts:
                seen.add(nct_id)
                continue
            if is_already_known(nct_id, registry, active_plays):
                seen.add(nct_id)
                continue

            # Skip academic/gov/Chinese sponsors — no options
            sponsor_lower = sponsor.lower()
            if any(skip in sponsor_lower for skip in SKIP_SPONSORS):
                continue

            # Flag for resolution + scoring
            seen.add(nct_id)
            results.append({
                "source":      "clinicaltrials_imminent",
                "nct_id":      nct_id,
                "trial_name":  title[:100],
                "company_hint": sponsor,
                "drug":        drug[:80],
                "indication":  cond[:60],
                "primary_completion": completion,
                "estimated_announcement": completion,  # completion ≠ announcement but close
                "sector":      "BIOTECH",
                "needs_ticker_resolution": True,
                "needs_scoring": True,
                "detected_date": today.isoformat(),
            })
            print(f"  [ct_imminent] NEW: {nct_id} | {sponsor[:30]} | {cond[:35]} | completes {completion}")

    except Exception as e:
        print(f"  [ct_imminent] Error: {e}")

    state["seen_nct_ids"] = list(seen)[-3000:]
    print(f"  [ct_imminent] Found {len(results)} new imminent-completion candidates")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# BIOTECH DISCOVERY — SOURCE 6: News RSS Feeds
# Endpoints News + STAT News + GlobeNewswire + PRNewswire
# Catches topline data 15-60 minutes before SEC 8-K filing
# ─────────────────────────────────────────────────────────────────────────────

NEWS_RSS_FEEDS = [
    ("Endpoints News",   "https://endpts.com/feed/"),
    ("STAT News",        "https://www.statnews.com/feed/"),
    ("GlobeNewswire",    "https://www.globenewswire.com/RssFeed/subjectcode/15-Pharmaceutical"),
    ("PRNewswire",       "https://www.prnewswire.com/rss/news-releases-list.rss?category=pharma-biotech"),
]

TOPLINE_NEWS_KWS = [
    "topline", "top-line", "phase 3", "phase iii", "primary endpoint",
    "met primary", "missed primary", "failed to meet", "statistically significant",
    "positive results", "negative results", "complete response letter", "crl",
    "pdufa", "fda approved", "fda rejected", "fda declined", "fda accepts",
    "breakthrough therapy", "fast track", "priority review",
    "late-breaking", "plenary", "oral presentation",
]

def discover_news_rss(state: dict, registry: dict, active_plays: dict) -> list:
    """
    Monitor Endpoints News, STAT News, GlobeNewswire, PRNewswire for topline signals.
    These sources break clinical trial data 15-60 minutes before SEC 8-K filings.
    Catches both watchlist companies AND new companies with phase 3 results.
    """
    seen_urls = set(state.get("seen_news_urls", []))
    results = []
    today = datetime.date.today().isoformat()

    # Build watchlist terms from registry for entity matching
    registry = load_registry()
    active_plays_data = load_active_plays()
    watchlist_terms = set()
    for ticker, info in registry.get("watchlist", {}).items():
        watchlist_terms.add(ticker.lower())
        watchlist_terms.add(info.get("company", "").lower().split()[0]) if info.get("company") else None
        watchlist_terms.add(info.get("drug", "").lower()[:10]) if info.get("drug") else None

    print("  [news_rss] Checking news RSS feeds...")

    try:
        import feedparser
    except ImportError:
        print("  [news_rss] feedparser not available")
        return results

    for feed_name, feed_url in NEWS_RSS_FEEDS:
        try:
            r = requests.get(feed_url, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                print(f"  [news_rss] {feed_name}: HTTP {r.status_code}")
                continue

            feed = feedparser.parse(r.text)
            new_signals = 0

            for entry in feed.entries[:30]:
                url   = entry.get("link", "")
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                full  = f"{title} {summary}".lower()

                if url in seen_urls:
                    continue

                # Check for topline keywords
                has_topline = any(kw in full for kw in TOPLINE_NEWS_KWS)
                if not has_topline:
                    continue

                seen_urls.add(url)

                # Check if it's about a watchlist company
                matched_ticker = None
                for term in watchlist_terms:
                    if term and len(term) > 3 and term in full:
                        # Find the ticker
                        for tkr, info in registry.get("watchlist", {}).items():
                            if (info.get("company", "").lower()[:10] in full or
                                info.get("drug", "").lower()[:8] in full or
                                tkr.lower() in full):
                                matched_ticker = tkr
                                break
                        if matched_ticker:
                            break

                # Classify signal type
                is_breaking  = any(kw in full for kw in ["topline", "top-line", "met primary", "missed", "fda approved", "crl"])
                is_new_date  = any(kw in full for kw in ["pdufa", "fda accepts", "priority review", "breakthrough"])
                is_conference = any(kw in full for kw in ["late-breaking", "plenary", "oral presentation", "asco", "aacr", "nejm", "lancet", "nejm"])

                severity = "CRITICAL" if is_breaking else ("HIGH" if is_new_date else "MODERATE")

                result = {
                    "source":       f"news_rss_{feed_name.lower().replace(' ','_')}",
                    "feed":         feed_name,
                    "ticker":       matched_ticker or "",
                    "title":        title[:150],
                    "url":          url,
                    "summary":      summary[:300],
                    "type":         "BREAKING_DATA" if is_breaking else ("NEW_DATE" if is_new_date else "CONFERENCE"),
                    "severity":     severity,
                    "is_topline":   is_breaking,
                    "is_watchlist": bool(matched_ticker),
                    "needs_ticker_resolution": not bool(matched_ticker),
                    "sector":       "BIOTECH",
                    "detected_date": today,
                }
                results.append(result)
                new_signals += 1

                flag = f"[{matched_ticker}]" if matched_ticker else "[NEW]"
                print(f"  [news_rss] {severity} {flag} {feed_name}: {title[:70]}")

            print(f"  [news_rss] {feed_name}: {len(feed.entries)} entries, {new_signals} new signals")

        except Exception as e:
            print(f"  [news_rss] {feed_name}: Error — {e}")

    state["seen_news_urls"] = list(seen_urls)[-2000:]
    print(f"  [news_rss] Total new news signals: {len(results)}")
    return results
