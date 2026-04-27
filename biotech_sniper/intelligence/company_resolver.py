#!/usr/bin/env python3
"""
COMPANY RESOLVER — AUTO-DISCOVERY
When a new company/ticker is found (from Warpspeed, ClinicalTrials, SAM.gov, AdCom),
this module automatically:

1. Finds the correct IR events URL by trying common patterns + web search fallback
2. Looks up the SEC CIK from EDGAR
3. Checks if the stock has tradeable options (via Alpaca options chain)
4. Gets the current stock price
5. Returns a fully-populated company profile ready to add to nct_registry.json

This runs every time a new play is scored and added to active_plays.json.
Zero manual intervention needed.
"""

import json
import re
import requests
import datetime
from pathlib import Path

from biotech_sniper.paths import BASE_DIR as BASE
REGISTRY_FILE = BASE / "intelligence/nct_registry.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
SEC_HEADERS = {"User-Agent": "BioCatalystBot research@mantisvc.com"}


# ── IR URL DISCOVERY ─────────────────────────────────────────────────────────

# Common IR events URL patterns (tried in order)
IR_URL_PATTERNS = [
    "https://ir.{domain}/events",
    "https://ir.{domain}/investor-relations/events",
    "https://ir.{domain}/news-events/events",
    "https://investors.{domain}/events",
    "https://investor.{domain}/events",
    "https://www.{domain}/investors/events",
    "https://www.{domain}/investor-relations/events",
    "https://www.{domain}/investors",
    "https://ir.{domain}/investor-relations",
    "https://ir.{domain}",
]

# Known domain overrides (when ticker → domain isn't obvious)
KNOWN_DOMAINS = {
    "IDYA":  "ideayabio.com",
    "AGIO":  "agios.com",
    "MLTX":  "moonlaketx.com",
    "RZLT":  "rezolutebio.com",
    "RVMD":  "revmed.com",
    "TVTX":  "travere.com",
    "AXSM":  "axsome.com",
    "RGNX":  "regenxbio.com",
    "ARGX":  "argenx.com",
    "NTLA":  "intelliatx.com",
    "VRDN":  "viridiantherapeutics.com",
    "NAMS":  "newamsterdampharma.com",
    # Contracts sector
    "RKLB":  "rocketlabusa.com",
    "KTOS":  "kratosdefense.com",
    "ASTS":  "ast-science.com",
    "LUNR":  "intuitivemachines.com",
    "MRCY":  "mrcy.com",
}


def find_ir_url(ticker: str, company_name: str = "") -> str:
    """
    Auto-discover working IR events URL for a company.
    Tries URL patterns first, falls back to web search construction.
    Returns the best working URL or empty string.
    """
    domain = KNOWN_DOMAINS.get(ticker.upper(), "")

    # If no known domain, derive from company name
    if not domain and company_name:
        # Clean company name to get likely domain
        name = re.sub(r'\s+(inc|corp|llc|ltd|plc|se|therapeutics|biosciences|pharmaceuticals|pharma|sciences|technologies|systems|defense)\s*$',
                      '', company_name.lower().strip(), flags=re.I)
        name = re.sub(r'[^a-z0-9]', '', name)
        domain = f"{name}.com"

    if not domain:
        return _search_ir_url(ticker, company_name)

    # Try patterns
    for pattern in IR_URL_PATTERNS:
        # Try both with/without www
        base_domain = domain.replace("www.", "")
        url = pattern.format(domain=base_domain)
        try:
            r = requests.get(url, headers=HEADERS, timeout=6, allow_redirects=True)
            if r.status_code == 200 and len(r.text) > 2000:
                print(f"    IR URL found: {url}")
                return url
        except Exception:
            continue

    # All patterns failed — use web search-based discovery
    return _search_ir_url(ticker, company_name)


def _search_ir_url(ticker: str, company_name: str) -> str:
    """
    When URL patterns all fail, construct a search query the cron agent can use.
    Returns a search instruction string prefixed with SEARCH: so the cron knows
    to run a web search and extract the IR URL from results.
    """
    query = f"{company_name or ticker} investor relations events calendar site"
    return f"SEARCH:{query}"


def get_sec_cik(ticker: str) -> str:
    """Look up SEC CIK for a ticker via EDGAR company search."""
    url = f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt=2020-01-01&forms=8-K"
    try:
        # Use EDGAR full-text search to find CIK
        r = requests.get(
            f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&forms=8-K&hits.hits._source=period_of_report,entity_name,file_num,period_of_report",
            headers=SEC_HEADERS, timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            hits = data.get("hits", {}).get("hits", [])
            if hits:
                # Extract entity name and search by name
                pass
    except:
        pass

    # Direct EDGAR ticker lookup (most reliable)
    try:
        r = requests.get(
            f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=&CIK={ticker}&type=8-K&dateb=&owner=include&count=5&search_text=&output=atom",
            headers=SEC_HEADERS, timeout=10
        )
        # Extract CIK from response
        match = re.search(r'CIK=(\d+)', r.text)
        if match:
            return match.group(1).zfill(10)
    except:
        pass

    # Try EDGAR company facts API
    try:
        r = requests.get(
            "https://efts.sec.gov/LATEST/search-index?q=%22" + ticker + "%22&forms=10-K,10-Q",
            headers=SEC_HEADERS, timeout=10
        )
    except:
        pass

    return ""


def get_sec_cik_direct(ticker: str) -> str:
    """Get CIK via SEC EDGAR company tickers JSON (most reliable method)."""
    try:
        r = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=SEC_HEADERS, timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            for key, info in data.items():
                if info.get("ticker", "").upper() == ticker.upper():
                    cik = str(info["cik_str"]).zfill(10)
                    print(f"    SEC CIK: {cik} ({info.get('title','')})")
                    return cik
    except Exception as e:
        print(f"    CIK lookup error: {e}")
    return ""


def check_options_available(ticker: str) -> dict:
    """Check if stock has tradeable options via the Alpaca options chain.

    M3 update (f-m3-02): the legacy vendor lookup is replaced by
    :func:`biotech_sniper.options_chains.pull_options.pull_chain`
    (Alpaca-backed). ``current_price`` is no longer fetched here —
    the M3 paper executor and selection layer derive prices from the
    chain row's bid/ask snapshot or from ``calibration_params.json``.
    """
    try:
        from biotech_sniper.options_chains.pull_options import pull_chain

        chain = pull_chain(ticker)
        expiries = sorted(
            {row.get("expiry") for row in chain if row.get("expiry")}
        )
        has_options = bool(chain)
        return {
            "has_options": has_options,
            "expiries": list(expiries[:6]),
            "current_price": None,
        }
    except Exception as e:
        return {"has_options": None, "expiries": [], "current_price": None, "error": str(e)}


def resolve_company(ticker: str, company_name: str = "", drug: str = "",
                    trial: str = "", nct_id: str = "",
                    estimated_announcement: str = "") -> dict:
    """
    Full company resolution pipeline.
    Returns a registry-ready profile dict.
    Called whenever a new ticker is discovered.
    """
    print(f"\n  Resolving {ticker} ({company_name})...")

    # 1. Find IR URL
    ir_url = find_ir_url(ticker, company_name)

    # 2. Get SEC CIK
    cik = get_sec_cik_direct(ticker)

    # 3. Check options
    options_data = check_options_available(ticker)

    profile = {
        "ticker": ticker,
        "company": company_name,
        "drug": drug,
        "trial": trial,
        "nct_id": nct_id,
        "ir_events_url": ir_url if not ir_url.startswith("SEARCH:") else "",
        "ir_search_fallback": ir_url if ir_url.startswith("SEARCH:") else "",
        "sec_cik": cik,
        "has_options": options_data.get("has_options"),
        "current_price": options_data.get("current_price"),
        "available_expiries": options_data.get("expiries", []),
        "estimated_announcement": estimated_announcement,
        "resolved_date": datetime.date.today().isoformat(),
        "needs_ir_url_refresh": ir_url.startswith("SEARCH:"),
    }

    return profile


def update_registry_with_company(profile: dict) -> bool:
    """
    Add or update a company profile in nct_registry.json.
    Returns True if registry was updated.
    """
    registry = json.load(open(REGISTRY_FILE)) if REGISTRY_FILE.exists() else {"watchlist": {}}
    ticker = profile["ticker"]

    existing = registry["watchlist"].get(ticker, {})

    # Only update fields that are newly discovered (don't overwrite existing good data)
    updated = dict(existing)
    for key, val in profile.items():
        if val and (key not in existing or not existing[key]):
            updated[key] = val

    # Always update these
    for key in ["current_price", "available_expiries", "resolved_date"]:
        if profile.get(key):
            updated[key] = profile[key]

    if updated != existing:
        registry["watchlist"][ticker] = updated
        with open(REGISTRY_FILE, "w") as f:
            json.dump(registry, f, indent=2)
        print(f"  ✓ Registry updated for {ticker}")
        return True

    return False


def auto_resolve_missing_ir_urls():
    """
    Scan nct_registry.json for companies with missing/broken IR URLs.
    Attempt to resolve each one automatically.
    Called in the daily 6AM run as part of Step 0.
    """
    registry = json.load(open(REGISTRY_FILE))
    updated_count = 0

    print(f"\n{'='*60}")
    print("AUTO-RESOLVING MISSING IR URLS")
    print(f"{'='*60}")

    for ticker, info in registry["watchlist"].items():
        ir_url = info.get("ir_events_url", "")
        needs_refresh = info.get("needs_ir_url_refresh", False)

        if ir_url and not needs_refresh:
            # Verify it still works
            try:
                r = requests.get(ir_url, headers=HEADERS, timeout=6)
                if r.status_code == 200 and len(r.text) > 1000:
                    continue  # Still good
                else:
                    print(f"  {ticker}: IR URL broken ({r.status_code}) — re-resolving")
            except:
                print(f"  {ticker}: IR URL unreachable — re-resolving")

        # Try to find/fix the URL
        company = info.get("company", ticker)
        new_url = find_ir_url(ticker, company)

        if new_url and not new_url.startswith("SEARCH:"):
            registry["watchlist"][ticker]["ir_events_url"] = new_url
            registry["watchlist"][ticker]["needs_ir_url_refresh"] = False
            print(f"  ✓ {ticker}: found IR URL → {new_url}")
            updated_count += 1
        elif not ir_url:
            # Mark for cron agent to web-search
            registry["watchlist"][ticker]["needs_ir_url_refresh"] = True
            query = f'{company} investor relations events calendar'
            registry["watchlist"][ticker]["ir_search_fallback"] = f"SEARCH:{query}"
            print(f"  ~ {ticker}: needs web search → {query}")

    if updated_count > 0:
        with open(REGISTRY_FILE, "w") as f:
            json.dump(registry, f, indent=2)
        print(f"\n  ✓ Updated {updated_count} IR URLs in registry")
    else:
        print(f"  ✓ All IR URLs validated")

    return updated_count


if __name__ == "__main__":
    # Run auto-resolution on all companies in registry
    auto_resolve_missing_ir_urls()

    print("\n\nTesting full resolution for a new company:")
    profile = resolve_company(
        ticker="EXEL",
        company_name="Exelixis",
        drug="Cabozantinib",
        trial="STELLAR-303",
        estimated_announcement="Q3 2026"
    )
    print(json.dumps(profile, indent=2))
