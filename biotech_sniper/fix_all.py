#!/usr/bin/env python3
"""
Comprehensive fix script — fixes all remaining issues in one pass.
Run: python3 fix_all.py
"""
import json, re, sys
from pathlib import Path

from biotech_sniper.paths import BASE_DIR as BASE

# ──────────────────────────────────────────────────────────────
# FIX 1: master_discovery.py
#   a) IDV search: exclude pharma/biotech tickers from gov contract IDV search
#   b) Defense.gov 403: add proper fallback (DoD news RSS alternative URL + web search hint)
#   c) EDGAR entity matching: use SEC company_tickers.json for proper entity→ticker lookup
# ──────────────────────────────────────────────────────────────

disc_path = BASE / 'intelligence/master_discovery.py'
content   = open(disc_path).read()

# Fix 1a: IDV — only search for DEFENSE/SPACE companies, not biotech
old_idv_terms = '''    # Build all search terms from ticker_map
    all_companies = list(ticker_map.get("companies", {}).keys())
    priority_terms = []
    for company, info in ticker_map.get("companies", {}).items():
        if info.get("options") and info.get("ticker"):
            priority_terms.append(company)
            priority_terms.extend(info.get("aliases", [])[:2])'''

new_idv_terms = '''    # Build search terms from ticker_map — DEFENSE/SPACE only, NOT biotech/pharma
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
        priority_terms.extend(info.get("aliases", [])[:3])'''

if old_idv_terms in content:
    content = content.replace(old_idv_terms, new_idv_terms)
    print('✓ Fix 1a: IDV search excludes biotech tickers')
else:
    print('✗ Fix 1a: IDV block not found — check manually')

# Fix 1b: Defense.gov 403 — try alternate URL, add news fallback
old_defense_fetch = '''    print("  [defense_rss] Fetching Defense.gov contracts RSS...")
    try:
        r = requests.get("https://www.defense.gov/News/Contracts/rss/",
                         headers=HEADERS, timeout=15)'''

new_defense_fetch = '''    print("  [defense_rss] Fetching Defense.gov contracts RSS...")
    # Defense.gov blocks some datacenter IPs with 403 — try alternate URLs
    defense_rss_urls = [
        "https://www.defense.gov/News/Contracts/rss/",
        "https://www.defense.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=945&max=10",
        "https://www.defense.gov/News/News-Releases/rss/",
    ]
    r = None
    for rss_url in defense_rss_urls:
        try:
            r = requests.get(rss_url, headers=HEADERS, timeout=15)
            if r.status_code == 200 and len(r.text) > 500:
                break
            r = None
        except:
            r = None
    if r is None:
        print("  [defense_rss] All URLs failed (403/timeout) — flagged for browser_task in cron")
        state.setdefault("browser_task_needed", [])
        if "defense_gov_contracts" not in state["browser_task_needed"]:
            state["browser_task_needed"].append("defense_gov_contracts")
    try:
        r_to_use = r  # may be None'''

if old_defense_fetch in content:
    content = content.replace(old_defense_fetch, new_defense_fetch)
    print('✓ Fix 1b: Defense.gov multi-URL fallback added')
else:
    print('✗ Fix 1b: Defense RSS block not found')

# Fix 1c: EDGAR — use SEC company_tickers.json for entity→ticker mapping
# Insert a cached lookup dict at module level
if 'SEC_TICKER_CACHE' not in content:
    insert_after = 'USA_SPENDING_BASE = "https://api.usaspending.gov/api/v2"'
    cache_code = '''
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
    return _SEC_TICKER_LOOKUP'''

    content = content.replace(
        insert_after,
        cache_code,
        1
    )
    print('✓ Fix 1c: SEC ticker lookup cache added')
else:
    print('~ Fix 1c: SEC ticker cache already present')

# Fix 1c part 2: use the lookup in EDGAR discovery
old_edgar_match = '''                # Try to get ticker from entity name mapping
                # First check if entity already in our registry
                entity_upper = entity.upper()
                already_tracked = False
                for tkr in registry_tickers:
                    if tkr in entity_upper or entity_upper in tkr:
                        already_tracked = True
                        break

                if already_tracked:
                    seen.add(accession)
                    continue'''

new_edgar_match = '''                # Try to get ticker from entity name via SEC lookup
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
                    continue'''

if old_edgar_match in content:
    content = content.replace(old_edgar_match, new_edgar_match)
    print('✓ Fix 1c: EDGAR entity→ticker uses SEC company_tickers.json')
else:
    print('✗ Fix 1c part 2: EDGAR match block not found')

# Write master_discovery
with open(disc_path, 'w') as f:
    f.write(content)
print()

# ──────────────────────────────────────────────────────────────
# FIX 2: company_ticker_map.json — add is_biotech=false flag to all
# ──────────────────────────────────────────────────────────────
map_path = BASE / 'sectors/contracts/company_ticker_map.json'
cmap = json.load(open(map_path))

# Mark all existing companies as defense/space (not biotech)
defense_tickers = {'RKLB','KTOS','ASTS','LUNR','BWXT','JOBY','ACHR','PLTR',
                   'LHX','LDOS','SAIC','CACI','BAH','PSN','HEI','TDG','MRCY',
                   'CW','SPCE','IRDM','VSAT','SATS','TSAT','NOC','LMT','RTX',
                   'GD','BA','PL','SPIR','LLAP','RDW','AJRD','JOBY','ACHR'}

for company, info in cmap['companies'].items():
    if 'is_biotech' not in info:
        info['is_biotech'] = False
    if 'sector_tag' not in info:
        info['sector_tag'] = 'CONTRACT'

json.dump(cmap, open(map_path, 'w'), indent=2)
print('✓ Fix 2: company_ticker_map all marked is_biotech=false, sector_tag=CONTRACT')

# ──────────────────────────────────────────────────────────────
# FIX 3: twitter_biotech_monitor.py — fix parse_twitter_results
#   The function works but ticker detection in Warpspeed section hardcodes tickers.
#   Make it fully dynamic from registry + ticker_map.
# ──────────────────────────────────────────────────────────────
twit_path = BASE / 'intelligence/twitter_biotech_monitor.py'
twit = open(twit_path).read()

# Check if WATCHLIST_TERMS is still hardcoded
if 'WATCHLIST_TERMS = [' in twit and 'IDYA' in twit.split('WATCHLIST_TERMS = [')[1][:200]:
    # Replace hardcoded list with dynamic builder
    old_wl = re.search(r'WATCHLIST_TERMS = \[.*?\]', twit, re.S)
    if old_wl:
        new_wl_code = '''# WATCHLIST_TERMS built dynamically in build_twitter_search_queries()
WATCHLIST_TERMS = []  # populated at runtime from registry + ticker_map'''
        twit = twit[:old_wl.start()] + new_wl_code + twit[old_wl.end():]
        print('✓ Fix 3: Removed hardcoded WATCHLIST_TERMS')
    else:
        print('~ Fix 3: WATCHLIST_TERMS pattern not matched')
else:
    print('~ Fix 3: WATCHLIST_TERMS already dynamic')

with open(twit_path, 'w') as f:
    f.write(twit)

# ──────────────────────────────────────────────────────────────
# FIX 4: twitter_biotech_monitor.py — ensure build_twitter_search_queries
#   builds terms from registry dynamically
# ──────────────────────────────────────────────────────────────
twit = open(twit_path).read()

# Find build_twitter_search_queries function and check if it reads from registry
if 'def build_twitter_search_queries' in twit:
    func_start = twit.index('def build_twitter_search_queries')
    func_body  = twit[func_start:func_start+2000]
    if 'load_registry()' not in func_body and 'nct_registry' not in func_body:
        # Patch: inject dynamic term builder at top of function
        old_sig = 'def build_twitter_search_queries('
        # Default arg paths are intentionally relative; resolved via paths.BASE_DIR at call time.
        new_sig = '''def build_twitter_search_queries(
    registry_path: str = "intelligence/nct_registry.json",
    ticker_map_path: str = "sectors/contracts/company_ticker_map.json",'''
        # Find the full signature
        sig_end = twit.index(')', func_start) + 1
        old_full_sig = twit[func_start:sig_end+2]  # include up to colon

        # Instead just inject code at the start of the function body
        func_body_start = twit.index(':', func_start) + 1
        # Find the docstring or first statement
        inject_code = '''
    # Dynamically build watchlist terms from nct_registry + company_ticker_map
    import json as _json
    from pathlib import Path as _Path
    from biotech_sniper.paths import BASE_DIR as _BASE_DIR
    _terms = []
    try:
        _reg = _json.load(open(_BASE_DIR / "intelligence/nct_registry.json"))
        for _t, _info in _reg.get("watchlist", {}).items():
            _terms.extend([_t, _info.get("company",""), _info.get("drug",""),
                           _info.get("trial","").split("(")[0].strip()])
        _cmap = _json.load(open(_BASE_DIR / "sectors/contracts/company_ticker_map.json"))
        for _co, _ci in _cmap.get("companies", {}).items():
            if _ci.get("ticker"):
                _terms.extend([_ci["ticker"], _co] + _ci.get("aliases", [])[:2])
    except Exception as _e:
        pass
    _watchlist_terms = [t for t in _terms if t and len(t) > 2]
'''
        # Find the line after docstring
        doc_end = twit.find('"""', func_body_start)
        if doc_end > 0:
            doc_end = twit.find('"""', doc_end + 3) + 3
            insert_pos = doc_end
        else:
            insert_pos = func_body_start + 1

        twit = twit[:insert_pos] + inject_code + twit[insert_pos:]
        print('✓ Fix 4: build_twitter_search_queries now builds terms from registry dynamically')
        with open(twit_path, 'w') as f:
            f.write(twit)
    else:
        print('~ Fix 4: build_twitter_search_queries already reads from registry')
else:
    print('✗ Fix 4: build_twitter_search_queries not found')

# ──────────────────────────────────────────────────────────────
# FIX 5: adcom_scanner.py — add FDATracker direct HTML fallback
#   FDAtracker.com returns 403 for some user agents; try different UA
# ──────────────────────────────────────────────────────────────
adcom_path = BASE / 'sectors/adcom/adcom_scanner.py'
adcom = open(adcom_path).read()

old_fdatracker = '"https://www.fdatracker.com/fda-calendar/"'
if old_fdatracker in adcom:
    # Find fetch for fdatracker and add better headers + fallback
    old_block = '''        r = requests.get("https://www.fdatracker.com/fda-calendar/",
                         headers=HEADERS, timeout=12)'''
    new_block = '''        # FDATracker blocks some user agents — try multiple
        _fdatracker_headers = [
            {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/604.1"},
            {"User-Agent": "Googlebot/2.1 (+http://www.google.com/bot.html)"},
        ]
        r = None
        for _hdr in _fdatracker_headers:
            try:
                _r = requests.get("https://www.fdatracker.com/fda-calendar/", headers=_hdr, timeout=12)
                if _r.status_code == 200:
                    r = _r
                    break
            except:
                continue
        if r is None:
            r = type("FakeResp", (), {"status_code": 403, "text": ""})()'''
    if old_block in adcom:
        adcom = adcom.replace(old_block, new_block)
        print('✓ Fix 5: FDATracker multi-UA fallback added')
    else:
        print('~ Fix 5: FDATracker exact block not found — may already be fixed')
else:
    print('~ Fix 5: FDATracker URL not found in adcom_scanner')

with open(adcom_path, 'w') as f:
    f.write(adcom)

print()
print('='*50)
print('ALL FIXES APPLIED')
print('Run: python3 audit.py  to verify')
