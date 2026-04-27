#!/usr/bin/env python3
"""
COMPANY PIPELINE MANAGER
Manages data pipelines for 300-600 biotech companies simultaneously.

Per-ticker pipeline fetches:
  1. Latest 8-K filings (SEC EDGAR full-text search)
  2. Active clinical trials (ClinicalTrials.gov API v2)
  3. PDUFA / AdCom dates (from bulk_universe_scanner PDUFA_CALENDAR)
  4. IR catalyst events (composite from trials + PDUFA)
  5. Warpspeed probability (if listed in warpspeed state)

State file: state/company_pipelines.json
"""

import json
import re
import datetime
import time
import logging
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from biotech_sniper.paths import BASE_DIR

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
STATE_FILE = BASE_DIR / "state/company_pipelines.json"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# f-m4-02: ``logging.basicConfig`` is now centralised in
# ``biotech_sniper.logging_setup``. Importing it here installs the JSON
# formatter on first use without duplicating handlers.
from biotech_sniper import logging_setup  # noqa: F401 — single source of truth

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CT_API = "https://clinicaltrials.gov/api/v2/studies"
SEC_EFTS = "https://efts.sec.gov/LATEST/search-index"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

CT_HEADERS = {"User-Agent": "AlphaSniper/1.0 research@example.com"}
SEC_HEADERS = {"User-Agent": "AlphaSniper/1.0 research@example.com"}

TOPLINE_KEYWORDS = [
    "topline", "top-line", "top line", "primary endpoint", "primary efficacy",
    "data readout", "clinical trial results", "pivotal", "registrational",
    "fda", "pdufa", "nda", "bla", "inda", "breakthrough", "fast track",
    "statistically significant", "met primary", "missed primary",
    "did not meet", "positive results", "negative results", "failed to meet",
    "overall survival", "progression-free", "response rate", "hazard ratio",
    "p-value", "p<0.05", "p=0.", "efficacy", "safety", "phase 2", "phase 3",
    "advisory committee", "adcom",
]

# Catalyst event type labels
EVENT_TYPE_PDUFA    = "PDUFA"
EVENT_TYPE_TRIAL    = "TRIAL_COMPLETION"
EVENT_TYPE_ADCOM    = "ADCOM"
EVENT_TYPE_EARNINGS = "EARNINGS"

# ---------------------------------------------------------------------------
# Import PDUFA calendar from sibling module (graceful fallback)
# ---------------------------------------------------------------------------
try:
    from biotech_sniper.intelligence.bulk_universe_scanner import PDUFA_CALENDAR
except ImportError:
    try:
        import sys
        sys.path.insert(0, str(BASE_DIR))
        from biotech_sniper.intelligence.bulk_universe_scanner import (
            PDUFA_CALENDAR,
        )
    except ImportError:
        PDUFA_CALENDAR = {}

# ---------------------------------------------------------------------------
# Helpers: HTTP
# ---------------------------------------------------------------------------

def _get_json(url: str, headers: dict, timeout: int = 15) -> Optional[dict]:
    """Fetch JSON from a URL, return None on any error."""
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        log.debug("GET %s → %s", url, exc)
        return None


def _get_text(url: str, headers: dict, timeout: int = 15) -> str:
    """Fetch raw text from a URL, return empty string on error."""
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return raw.decode("utf-8", errors="replace")
    except Exception as exc:
        log.debug("GET text %s → %s", url, exc)
        return ""


# ---------------------------------------------------------------------------
# Helpers: CIK resolution (SEC EDGAR)
# ---------------------------------------------------------------------------
_cik_cache: dict = {}


def _load_cik_cache():
    """Populate _cik_cache from SEC company_tickers.json (done once)."""
    global _cik_cache
    if _cik_cache:
        return
    data = _get_json(SEC_TICKERS_URL, SEC_HEADERS)
    if not data:
        return
    for entry in data.values():
        ticker = (entry.get("ticker") or "").upper()
        cik    = str(entry.get("cik_str") or "").zfill(10)
        name   = (entry.get("title") or "").strip()
        if ticker:
            _cik_cache[ticker] = {"cik": cik, "name": name}


def _resolve_cik(ticker: str) -> Optional[str]:
    """Return zero-padded CIK for ticker, or None."""
    _load_cik_cache()
    entry = _cik_cache.get(ticker.upper())
    return entry["cik"] if entry else None


def _resolve_company_name(ticker: str) -> str:
    """Return company name for ticker (from CIK cache or ticker itself)."""
    _load_cik_cache()
    entry = _cik_cache.get(ticker.upper())
    if entry and entry.get("name"):
        return entry["name"]
    # Try state/sec_name_to_ticker.json inverted
    nm_file = BASE_DIR / "state/sec_name_to_ticker.json"
    if nm_file.exists():
        try:
            with open(nm_file) as f:
                nm = json.load(f)
            for name, t in nm.items():
                if t == ticker.upper():
                    return name.title()
        except Exception:
            pass
    return ticker.upper()


# ---------------------------------------------------------------------------
# 1. ClinicalTrials.gov — active Phase 2/3 trials for a ticker
# ---------------------------------------------------------------------------

def _fetch_trials_for_ticker(ticker: str, company_name: str) -> list:
    """
    Query ClinicalTrials.gov API v2 for active Phase 2/3 trials.
    Uses company name as the sponsor keyword search.
    Returns list of dicts: {nct_id, phase, completion, indication, status}
    """
    results = []
    seen_ncts = set()

    # Build search terms: try company name and ticker
    search_terms = []
    # Strip common suffixes for a cleaner keyword search
    clean_name = re.sub(
        r'\b(inc\.?|corp\.?|ltd\.?|llc\.?|plc\.?|co\.?|s\.?a\.?|therapeutics|'
        r'pharmaceuticals|biosciences|biotechnology|biotech|sciences|healthcare|'
        r'medical|pharma|biopharma|biotherapeutics|oncology)\b',
        '', company_name, flags=re.IGNORECASE
    )
    clean_name = re.sub(r'\s+', ' ', clean_name).strip().rstrip(',').strip()
    if clean_name and len(clean_name) > 3:
        search_terms.append(clean_name)
    if company_name and company_name not in search_terms:
        search_terms.append(company_name)

    for term in search_terms[:2]:  # limit to 2 queries
        params = {
            "query.term": term,
            "filter.advanced": (
                "AREA[Phase](PHASE2 OR PHASE3) AND "
                "AREA[OverallStatus](ACTIVE_NOT_RECRUITING OR RECRUITING)"
            ),
            "pageSize": 50,
            "fields": "NCTId,Phase,PrimaryCompletionDate,OverallStatus,Condition,BriefTitle",
            "sort": "PrimaryCompletionDate:asc",
        }
        url = CT_API + "?" + urllib.parse.urlencode(params)
        data = _get_json(url, CT_HEADERS, timeout=20)
        time.sleep(0.1)
        if not data:
            continue
        for study in data.get("studies", []):
            ps  = study.get("protocolSection", {})
            nct = ps.get("identificationModule", {}).get("nctId", "")
            if not nct or nct in seen_ncts:
                continue
            seen_ncts.add(nct)

            phases  = ps.get("designModule", {}).get("phases", [])
            phase   = "/".join(phases) if phases else ""
            status  = ps.get("statusModule", {}).get("overallStatus", "")
            comp    = (ps.get("statusModule", {})
                         .get("primaryCompletionDateStruct", {})
                         .get("date", ""))
            conds   = ps.get("conditionsModule", {}).get("conditions", [])
            indication = (conds[0] if conds else "")[:80]

            results.append({
                "nct_id":     nct,
                "phase":      phase,
                "completion": comp,
                "indication": indication,
                "status":     status,
            })

    return results


# ---------------------------------------------------------------------------
# 2. SEC EDGAR 8-K search
# ---------------------------------------------------------------------------

def _fetch_latest_8k(ticker: str) -> tuple:
    """
    Search SEC EDGAR full-text for recent 8-K filings mentioning the ticker.
    Returns (date_str, summary_str) or (None, None).
    
    Strategy:
      1. EDGAR full-text search (efts.sec.gov) for ticker in 8-K last 30 days
      2. If found, check filing content for topline keywords
      3. Fall back to submissions API (CIK-based) for most recent 8-K date
    """
    today        = datetime.date.today()
    thirty_ago   = (today - datetime.timedelta(days=30)).isoformat()

    # --- Strategy 1: EFTS full-text search ---
    params = {
        "q":          f'"{ticker}"',
        "forms":      "8-K",
        "dateRange":  "custom",
        "startdt":    thirty_ago,
        "enddt":      today.isoformat(),
        "_source":    "file_date,period_of_report,display_names,file_num",
        "hits.hits.total.value": 1,
        "hits.hits._source.period_of_report": 1,
    }
    efts_url = SEC_EFTS + "?" + urllib.parse.urlencode(params)
    efts_data = _get_json(efts_url, SEC_HEADERS, timeout=15)
    time.sleep(0.1)

    date_str    = None
    summary_str = None

    if efts_data:
        hits = (efts_data.get("hits") or {}).get("hits") or []
        if hits:
            src       = hits[0].get("_source", {})
            date_str  = src.get("period_of_report") or src.get("file_date")
            # Try to extract a summary from the highlighted text
            highlight = hits[0].get("highlight", {})
            snippets  = []
            for field_snippets in highlight.values():
                snippets.extend(field_snippets)
            if snippets:
                summary_str = " ... ".join(snippets[:3])[:500]

    # --- Strategy 2: CIK submissions API (always gives exact dates) ---
    if not date_str:
        cik = _resolve_cik(ticker)
        if cik:
            sub_url  = f"{SEC_SUBMISSIONS}/CIK{cik}.json"
            sub_data = _get_json(sub_url, SEC_HEADERS, timeout=15)
            time.sleep(0.1)
            if sub_data:
                recent   = sub_data.get("filings", {}).get("recent", {})
                forms    = recent.get("form", [])
                dates    = recent.get("filingDate", [])
                accns    = recent.get("accessionNumber", [])
                for i, form in enumerate(forms):
                    if form in ("8-K", "8-K/A"):
                        date_str = dates[i] if i < len(dates) else None
                        accn     = accns[i] if i < len(accns) else None
                        # Try to fetch filing index for content keywords
                        if accn and not summary_str:
                            summary_str = _peek_8k_filing(cik, accn)
                        break

    return date_str, summary_str


def _peek_8k_filing(cik: str, accn: str) -> Optional[str]:
    """
    Fetch the 8-K filing index and peek at the primary document for
    topline keywords. Returns a short summary string or None.
    """
    accn_path = accn.replace("-", "")
    idx_url   = (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{accn_path}/{accn}-index.json"
    )
    idx_data  = _get_json(idx_url, SEC_HEADERS, timeout=10)
    time.sleep(0.05)
    if not idx_data:
        return None

    doc_url = None
    for doc in idx_data.get("directory", {}).get("item", []):
        name = doc.get("name", "")
        if name.endswith(".htm") or name.endswith(".txt"):
            doc_url = (
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                f"{accn_path}/{name}"
            )
            break

    if not doc_url:
        return None

    text = _get_text(doc_url, SEC_HEADERS, timeout=10)
    time.sleep(0.05)
    if not text:
        return None

    # Strip HTML tags for keyword search
    text_clean = re.sub(r'<[^>]+>', ' ', text).lower()
    text_clean = re.sub(r'\s+', ' ', text_clean)

    found_kws = [kw for kw in TOPLINE_KEYWORDS if kw in text_clean]
    if not found_kws:
        return None

    # Extract a ~300-char snippet around the first keyword hit
    first_kw = found_kws[0]
    pos       = text_clean.find(first_kw)
    snippet   = text_clean[max(0, pos - 60): pos + 240].strip()
    return f"[{', '.join(found_kws[:5])}] {snippet}"[:500]


# ---------------------------------------------------------------------------
# 3. PDUFA date (from static calendar)
# ---------------------------------------------------------------------------

def _get_pdufa_date(ticker: str) -> Optional[str]:
    """Return PDUFA date string for ticker if in PDUFA_CALENDAR, else None."""
    entry = PDUFA_CALENDAR.get(ticker.upper())
    if entry:
        return entry[0]  # (date, description)
    return None


def _get_pdufa_description(ticker: str) -> Optional[str]:
    entry = PDUFA_CALENDAR.get(ticker.upper())
    if entry:
        return entry[1]
    return None


# ---------------------------------------------------------------------------
# 4. Warpspeed probability (from check_warpspeed or state)
# ---------------------------------------------------------------------------

def _get_warpspeed_p(ticker: str) -> Optional[float]:
    """
    Check if ticker has a Warpspeed probability stored in state.
    Looks for state/warpspeed_state.json or universe_watchlist.json.
    Returns float [0,1] or None.
    """
    # Check warpspeed_state.json if it exists
    ws_file = BASE_DIR / "state/warpspeed_state.json"
    if ws_file.exists():
        try:
            with open(ws_file) as f:
                ws = json.load(f)
            entry = ws.get(ticker.upper()) or ws.get(ticker.lower())
            if entry:
                p = entry.get("probability") or entry.get("p") or entry.get("warpspeed_p")
                if p is not None:
                    return float(p)
        except Exception:
            pass

    # Check universe_watchlist.json
    uw_file = BASE_DIR / "state/universe_watchlist.json"
    if uw_file.exists():
        try:
            with open(uw_file) as f:
                uw = json.load(f)
            candidates = uw.get("candidates", uw)
            entry = candidates.get(ticker.upper())
            if entry:
                p = entry.get("warpspeed_p")
                if p is not None:
                    return float(p)
        except Exception:
            pass

    return None


# ---------------------------------------------------------------------------
# 5. Build catalyst_events list
# ---------------------------------------------------------------------------

def _build_catalyst_events(
    active_trials: list,
    pdufa_date: Optional[str],
    pdufa_desc: Optional[str],
) -> list:
    """
    Combine trial completion dates and PDUFA date into a catalyst event list.
    Each event: {date, event_type, description}
    Sorted by date ascending.
    """
    events = []

    # PDUFA date
    if pdufa_date:
        events.append({
            "date":        pdufa_date,
            "event_type":  EVENT_TYPE_PDUFA,
            "description": pdufa_desc or "FDA action date",
        })

    # Trial completion dates
    for trial in active_trials:
        comp = trial.get("completion")
        if not comp:
            continue
        phase = trial.get("phase", "")
        ind   = trial.get("indication", "")
        nct   = trial.get("nct_id", "")
        events.append({
            "date":        comp,
            "event_type":  EVENT_TYPE_TRIAL,
            "description": f"{phase} {ind} completion ({nct})".strip(),
        })

    # Sort by date (None-safe)
    events.sort(key=lambda e: e["date"] or "9999-12-31")
    return events


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _compute_pre_score(
    active_trials: list,
    pdufa_date: Optional[str],
    latest_8k_date: Optional[str],
    warpspeed_p: Optional[float],
) -> int:
    """Compute a rough pre_score 0-100 used for tiering."""
    today = datetime.date.today()
    score = 0

    # PDUFA is high value
    if pdufa_date:
        score += 25
        try:
            days = (datetime.date.fromisoformat(pdufa_date[:10]) - today).days
            if 0 < days <= 90:  score += 20
            elif days <= 180:   score += 12
            elif days <= 365:   score += 6
        except Exception:
            pass

    # Phase 3 trial completing soon
    for trial in active_trials:
        ph   = trial.get("phase", "")
        comp = trial.get("completion", "")
        if "3" in ph:
            score += 10
        elif "2" in ph:
            score += 5
        if comp:
            try:
                days = (datetime.date.fromisoformat(comp[:10]) - today).days
                if 0 < days <= 90:  score += 15
                elif days <= 180:   score += 8
                elif days <= 365:   score += 4
            except Exception:
                pass
        break  # only bonus for nearest trial

    # Recent 8-K
    if latest_8k_date:
        try:
            days_ago = (today - datetime.date.fromisoformat(latest_8k_date[:10])).days
            if days_ago <= 7:  score += 10
            elif days_ago <= 30: score += 5
        except Exception:
            pass

    # Warpspeed boost
    if warpspeed_p is not None:
        score += int(warpspeed_p * 10)

    return min(score, 100)


def _compute_tier(pre_score: int) -> int:
    if pre_score >= 40: return 1
    if pre_score >= 20: return 2
    return 3


# ---------------------------------------------------------------------------
# Core pipeline function
# ---------------------------------------------------------------------------

def run_pipeline_for_ticker(ticker: str) -> dict:
    """
    Run full data pipeline for a single ticker.

    Returns dict with:
      ticker, company_name, active_trials, pdufa_date, latest_8k_date,
      latest_8k_summary, catalyst_events, warpspeed_p, last_updated,
      pre_score, tier
    """
    ticker = ticker.upper().strip()
    today  = datetime.date.today().isoformat()
    log.debug("Running pipeline: %s", ticker)

    # Company name
    company_name = _resolve_company_name(ticker)

    # 1. Active clinical trials
    try:
        active_trials = _fetch_trials_for_ticker(ticker, company_name)
    except Exception as exc:
        log.warning("%s: trials fetch failed: %s", ticker, exc)
        active_trials = []

    # 2. Latest 8-K
    try:
        latest_8k_date, latest_8k_summary = _fetch_latest_8k(ticker)
    except Exception as exc:
        log.warning("%s: 8-K fetch failed: %s", ticker, exc)
        latest_8k_date    = None
        latest_8k_summary = None

    # 3. PDUFA date
    pdufa_date = _get_pdufa_date(ticker)
    pdufa_desc = _get_pdufa_description(ticker)

    # 4. Warpspeed probability
    try:
        warpspeed_p = _get_warpspeed_p(ticker)
    except Exception as exc:
        log.warning("%s: warpspeed fetch failed: %s", ticker, exc)
        warpspeed_p = None

    # 5. Catalyst events
    catalyst_events = _build_catalyst_events(active_trials, pdufa_date, pdufa_desc)

    # Scoring / tiering
    pre_score = _compute_pre_score(active_trials, pdufa_date, latest_8k_date, warpspeed_p)
    tier      = _compute_tier(pre_score)

    return {
        "ticker":            ticker,
        "company_name":      company_name,
        "active_trials":     active_trials,
        "pdufa_date":        pdufa_date,
        "latest_8k_date":    latest_8k_date,
        "latest_8k_summary": latest_8k_summary,
        "catalyst_events":   catalyst_events,
        "warpspeed_p":       warpspeed_p,
        "last_updated":      today,
        "pre_score":         pre_score,
        "tier":              tier,
    }


# ---------------------------------------------------------------------------
# Batch pipeline
# ---------------------------------------------------------------------------

def run_pipeline_batch(tickers: list, max_workers: int = 10) -> dict:
    """
    Run pipeline for multiple tickers in parallel using ThreadPoolExecutor.
    Returns {ticker: result_dict}.
    Workers sleep 0.1s between API calls to respect rate limits.
    """
    results = {}
    errors  = {}

    def _worker(t: str) -> tuple:
        try:
            res = run_pipeline_for_ticker(t)
            time.sleep(0.1)
            return t, res, None
        except Exception as exc:
            log.error("Pipeline error for %s: %s", t, exc)
            time.sleep(0.1)
            return t, None, str(exc)

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="pipeline") as pool:
        futures = {pool.submit(_worker, t): t for t in tickers}
        for fut in as_completed(futures):
            t, res, err = fut.result()
            if err:
                errors[t] = err
            else:
                results[t] = res

    if errors:
        log.warning("Batch errors (%d): %s", len(errors), list(errors.keys())[:10])

    return results


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def save_pipeline_state(results: dict):
    """
    Merge results into state/company_pipelines.json.
    New results overwrite existing ticker entries; others are preserved.
    """
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = load_pipeline_state()
    existing.update(results)
    with open(STATE_FILE, "w") as f:
        json.dump(existing, f, indent=2)
    log.info("Saved pipeline state: %d tickers total", len(existing))


def load_pipeline_state() -> dict:
    """Load state/company_pipelines.json. Returns {} if file missing."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as exc:
            log.error("Failed to load pipeline state: %s", exc)
    return {}


# ---------------------------------------------------------------------------
# Staleness / update scheduling helpers
# ---------------------------------------------------------------------------

def get_tickers_needing_update(max_age_days: int = 7) -> list:
    """
    Returns list of tickers whose pipeline data is older than max_age_days,
    or tickers with no pipeline data at all.

    Pulls the full ticker universe from:
      1. state/universe_watchlist.json (primary)
      2. state/company_pipelines.json already loaded entries (secondary)
    """
    today = datetime.date.today()
    state = load_pipeline_state()

    # Build universe from watchlist
    universe = set(state.keys())
    uw_file  = BASE_DIR / "state/universe_watchlist.json"
    if uw_file.exists():
        try:
            with open(uw_file) as f:
                uw = json.load(f)
            candidates = uw.get("candidates", uw)
            if isinstance(candidates, dict):
                universe.update(candidates.keys())
        except Exception:
            pass

    stale = []
    for ticker in universe:
        entry = state.get(ticker)
        if not entry:
            stale.append(ticker)
            continue
        last = entry.get("last_updated")
        if not last:
            stale.append(ticker)
            continue
        try:
            age = (today - datetime.date.fromisoformat(last)).days
            if age >= max_age_days:
                stale.append(ticker)
        except Exception:
            stale.append(ticker)

    return stale


# ---------------------------------------------------------------------------
# Catalyst discovery (alerts)
# ---------------------------------------------------------------------------

def get_new_catalyst_discoveries(since_date: str) -> list:
    """
    Returns any NEW catalyst events (pdufa_date changes or new trial completions)
    discovered since since_date (ISO format string, e.g. "2026-04-20").

    Compares current pipeline state against the since_date cutoff:
      - Any catalyst_event whose 'date' >= since_date AND the ticker's
        last_updated >= since_date is considered a "new discovery".
    Returns list of dicts: {ticker, company_name, event_type, date, description}
    """
    try:
        cutoff = datetime.date.fromisoformat(since_date)
    except Exception:
        log.error("Invalid since_date: %s", since_date)
        return []

    state       = load_pipeline_state()
    discoveries = []

    for ticker, entry in state.items():
        last_updated = entry.get("last_updated", "")
        try:
            lu_date = datetime.date.fromisoformat(last_updated)
        except Exception:
            continue

        if lu_date < cutoff:
            continue  # this ticker wasn't updated recently enough

        for event in entry.get("catalyst_events", []):
            try:
                ev_date = datetime.date.fromisoformat(event["date"][:10])
            except Exception:
                continue

            if ev_date >= cutoff:
                discoveries.append({
                    "ticker":       ticker,
                    "company_name": entry.get("company_name", ticker),
                    "event_type":   event.get("event_type", "UNKNOWN"),
                    "date":         event["date"],
                    "description":  event.get("description", ""),
                })

    # Sort by event date ascending
    discoveries.sort(key=lambda d: d["date"])
    return discoveries


# ---------------------------------------------------------------------------
# CLI / standalone usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["AXSM", "SRPT", "NMRA"]
    print(f"Running pipeline for: {tickers}")

    if len(tickers) == 1:
        result = run_pipeline_for_ticker(tickers[0])
        print(json.dumps(result, indent=2))
    else:
        results = run_pipeline_batch(tickers, max_workers=5)
        save_pipeline_state(results)
        print(f"Saved {len(results)} ticker pipelines to {STATE_FILE}")
        for t, r in results.items():
            trials_n = len(r.get("active_trials", []))
            pdufa    = r.get("pdufa_date", "—")
            score    = r.get("pre_score", 0)
            tier     = r.get("tier", 3)
            print(f"  {t:8s} | trials={trials_n} | PDUFA={pdufa} | score={score} | T{tier}")
