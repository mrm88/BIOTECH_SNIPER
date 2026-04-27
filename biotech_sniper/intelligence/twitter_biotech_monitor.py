#!/usr/bin/env python3
"""
TWITTER/X BIOTECH INTELLIGENCE MONITOR — v2 (Query-Based)

Architecture change from v1:
  OLD: Module made HTTP requests to DuckDuckGo HTML (unreliable, rate-limited)
  NEW: Module BUILDS query strings → cron agent runs them via web_search tool
       → module parses returned results

Two public functions:
  build_twitter_search_queries() → list of query strings for cron agent
  parse_twitter_results(results)  → list of classified signals

Watchlist terms are AUTO-BUILT from nct_registry.json + company_ticker_map.json
every run — NOT hardcoded.

Target accounts:
  BIOTECH:   BioPharmCatalyst, adamfeuerstein, bradloncar, ohsnapitsjuliee,
             JohnCarlisle_, PurpleCowBiomed, KonamiEd
  CONTRACTS: BreakingDefense, SpaceForceDoD, NASASpaceflight, DefenseOne
  ADCOM:     FDASpox, DrJohnBurke, TrialSiteNews
"""

import json
import re
import datetime
from pathlib import Path
from typing import Optional

from biotech_sniper.paths import BASE_DIR
REGISTRY_FILE      = BASE_DIR / "intelligence/nct_registry.json"
TICKER_MAP_FILE    = BASE_DIR / "sectors/contracts/company_ticker_map.json"
TWITTER_STATE_FILE = BASE_DIR / "state/twitter_state.json"
OUTPUT_FILE        = BASE_DIR / "intelligence/twitter_report.json"


# ─────────────────────────────────────────────────────────────────────────────
# MONITORED ACCOUNTS
# ─────────────────────────────────────────────────────────────────────────────

BIOTECH_ACCOUNTS = [
    {"handle": "BioPharmCatalyst",  "focus": "PDUFA dates, FDA catalyst calendar — posts every new date"},
    {"handle": "adamfeuerstein",    "focus": "breaks clinical data before 8-Ks file"},
    {"handle": "bradloncar",        "focus": "biotech catalyst calendar maintained in real time"},
    {"handle": "ohsnapitsjuliee",   "focus": "options flow on biotech events — smart money positioning"},
    {"handle": "JohnCarlisle_",     "focus": "biotech options flow — unusual activity"},
    {"handle": "PurpleCowBiomed",   "focus": "trial endpoint analysis — deep technical reads"},
    {"handle": "KonamiEd",          "focus": "biotech catalyst deep dives — comprehensive setups"},
]

CONTRACTS_ACCOUNTS = [
    {"handle": "BreakingDefense",   "focus": "defense contract news — first to report large awards"},
    {"handle": "SpaceForceDoD",     "focus": "Space Force contract awards — official"},
    {"handle": "NASASpaceflight",   "focus": "launch contract news — CLPS, CLSS, commercial crew"},
    {"handle": "DefenseOne",        "focus": "defense acquisition news — policy + awards"},
]

ADCOM_ACCOUNTS = [
    {"handle": "FDASpox",           "focus": "FDA official announcements — meeting outcomes"},
    {"handle": "DrJohnBurke",       "focus": "FDA watcher — AdCom interpretation"},
    {"handle": "TrialSiteNews",     "focus": "clinical trial news — AdCom context"},
]

ALL_ACCOUNTS = BIOTECH_ACCOUNTS + CONTRACTS_ACCOUNTS + ADCOM_ACCOUNTS

# Signal keyword classification
SIGNAL_KEYWORDS = {
    "TOPLINE_POSITIVE": [
        "positive", "met primary", "succeeded", "statistically significant",
        "p<0.05", "p < 0.05", "strong efficacy", "achieved primary",
        "hit primary", "met its primary", "met the primary", "significant improvement",
        "overall survival benefit", "pfs benefit", "response rate", "approved",
        "approval", "complete response", "remarkable efficacy",
    ],
    "TOPLINE_NEGATIVE": [
        "failed", "missed", "did not meet", "no statistical significance",
        "negative results", "did not achieve", "futility", "discontinued",
        "halted", "terminated early", "no significant difference", "rejected",
        "complete response letter", "crl", "not approvable",
    ],
    "DATE_CONFIRMED": [
        "pdufa date", "adcom scheduled", "readout date confirmed",
        "meeting set for", "topline expected", "results expected by",
        "q1 2026", "q2 2026", "q3 2026", "q4 2026",
        "january 2026", "february 2026", "march 2026", "april 2026",
        "may 2026", "june 2026", "july 2026", "august 2026",
        "september 2026", "october 2026", "november 2026", "december 2026",
    ],
    "LATE_BREAKING_ABSTRACT": [
        "late-breaking oral", "late-breaking abstract", "late breaking",
        "lba", "plenary session", "presidential symposium",
        "asco 2026", "aacr 2026", "ash 2026", "esc 2026",
        "nejm 2026", "new england journal", "lancet 2026",
    ],
    "OPTIONS_FLOW_UNUSUAL": [
        "unusual call", "unusual put", "call sweep", "put sweep",
        "large block", "smart money", "institutional flow", "dark pool",
        "massive call", "massive put", "whale activity", "unusual activity",
        "abnormal volume", "flow alert",
    ],
    "CONTRACT_AWARD": [
        "contract awarded", "contract award", "wins contract", "awarded contract",
        "receives contract", "selected for", "task order", "billions",
        "millions contract", "dod award", "army contract", "navy contract",
        "air force contract", "space force contract", "darpa award",
        "nasa selects", "nasa awards",
    ],
    "ADCOM_RESULT": [
        "adcom voted", "advisory committee voted", "voted to recommend",
        "yes vote", "no vote", "panel recommended", "committee recommended",
        "fda advisory", "adcom result",
    ],
    "DATE_MENTION": [
        "readout", "data expected", "topline", "catalyst", "binary event",
        "pivotal", "registrational",
    ],
}

# Severity mapping
SIGNAL_SEVERITY = {
    "TOPLINE_POSITIVE":      "CRITICAL",
    "TOPLINE_NEGATIVE":      "CRITICAL",
    "ADCOM_RESULT":          "CRITICAL",
    "DATE_CONFIRMED":        "HIGH",
    "LATE_BREAKING_ABSTRACT":"HIGH",
    "OPTIONS_FLOW_UNUSUAL":  "HIGH",
    "CONTRACT_AWARD":        "HIGH",
    "DATE_MENTION":          "MODERATE",
}

SIGNAL_ICONS = {
    "TOPLINE_POSITIVE":      "🟢",
    "TOPLINE_NEGATIVE":      "🔴",
    "ADCOM_RESULT":          "🔴",
    "DATE_CONFIRMED":        "📅",
    "LATE_BREAKING_ABSTRACT":"🟠",
    "OPTIONS_FLOW_UNUSUAL":  "💰",
    "CONTRACT_AWARD":        "🏛️",
    "DATE_MENTION":          "👁",
}


# ─────────────────────────────────────────────────────────────────────────────
# WATCHLIST BUILDER (dynamic — never hardcoded)
# ─────────────────────────────────────────────────────────────────────────────

def build_watchlist_terms() -> dict:
    """
    Build watchlist terms dynamically from nct_registry.json and company_ticker_map.json.
    Returns dict mapping search terms to metadata.
    Never hardcoded — always derived from live data files.
    """
    terms = {}  # term_lower -> {ticker, company, sector, drug}

    # From nct_registry watchlist
    if REGISTRY_FILE.exists():
        try:
            with open(REGISTRY_FILE) as f:
                registry = json.load(f)
            for ticker, info in registry.get("watchlist", {}).items():
                company = info.get("company", "")
                drug    = info.get("drug", "")
                trial   = info.get("trial", "")

                # Add ticker
                terms[ticker.lower()] = {"ticker": ticker, "company": company, "sector": "BIOTECH", "drug": drug}

                # Add company name variations
                if company:
                    terms[company.lower()] = {"ticker": ticker, "company": company, "sector": "BIOTECH"}
                    # Short version (first word)
                    first_word = company.split()[0].lower()
                    if len(first_word) > 3:
                        terms[first_word] = {"ticker": ticker, "company": company, "sector": "BIOTECH"}

                # Add drug name (most specific signal)
                if drug:
                    # Handle "Drug A + Drug B" combos
                    for d in re.split(r'[/+;,]', drug):
                        d = d.strip()
                        if d and len(d) > 3:
                            terms[d.lower()] = {"ticker": ticker, "company": company, "sector": "BIOTECH", "drug": d}

                # Add trial name keywords
                if trial:
                    trial_words = re.findall(r'[A-Z][A-Z0-9\-]{3,}', trial)
                    for word in trial_words:
                        if word not in ("PHASE", "NCT", "IGA"):
                            terms[word.lower()] = {"ticker": ticker, "company": company, "sector": "BIOTECH"}

        except Exception as e:
            print(f"  [watchlist] Registry load error: {e}")

    # From company_ticker_map (contracts sector)
    if TICKER_MAP_FILE.exists():
        try:
            with open(TICKER_MAP_FILE) as f:
                ticker_map = json.load(f)
            for company, info in ticker_map.get("companies", {}).items():
                ticker = info.get("ticker")
                if not ticker:
                    continue
                terms[company.lower()] = {"ticker": ticker, "company": company, "sector": "CONTRACT"}
                terms[ticker.lower()]  = {"ticker": ticker, "company": company, "sector": "CONTRACT"}
                for alias in info.get("aliases", []):
                    if alias and len(alias) > 3:
                        terms[alias.lower()] = {"ticker": ticker, "company": company, "sector": "CONTRACT"}
        except Exception as e:
            print(f"  [watchlist] Ticker map load error: {e}")

    return terms


# ─────────────────────────────────────────────────────────────────────────────
# QUERY BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_twitter_search_queries() -> list:
    """
    Build list of web search query strings for the cron agent to run.
    The cron agent runs each via its web_search tool, then passes
    results back to parse_twitter_results().

    Returns list of query strings.
    Queries are structured to work via standard web search
    (site:twitter.com OR site:x.com prefix pattern).
    """
    # Dynamically build watchlist terms from nct_registry + company_ticker_map
    import json as _json
    from pathlib import Path as _Path
    _terms = []
    try:
        _reg = _json.load(open(BASE_DIR / "intelligence/nct_registry.json"))
        for _t, _info in _reg.get("watchlist", {}).items():
            _terms.extend([_t, _info.get("company",""), _info.get("drug",""),
                           _info.get("trial","").split("(")[0].strip()])
        _cmap = _json.load(open(BASE_DIR / "sectors/contracts/company_ticker_map.json"))
        for _co, _ci in _cmap.get("companies", {}).items():
            if _ci.get("ticker"):
                _terms.extend([_ci["ticker"], _co] + _ci.get("aliases", [])[:2])
    except Exception as _e:
        pass
    _watchlist_terms = [t for t in _terms if t and len(t) > 2]

    today      = datetime.date.today()
    year       = today.year
    last_7days = (today - datetime.timedelta(days=7)).strftime("%Y-%m-%d")

    watchlist = build_watchlist_terms()

    # Extract high-value specific terms (drugs, not just tickers)
    drug_terms = [
        term for term, meta in watchlist.items()
        if meta.get("drug") and len(term) > 4 and " " not in term
    ]
    ticker_terms = [
        meta["ticker"] for meta in watchlist.values()
        if meta.get("ticker")
    ]
    company_short = [
        term for term, meta in watchlist.items()
        if " " not in term and len(term) > 4
        and meta.get("company", "").lower().split()
        and term == meta.get("company", "").lower().split()[0]
    ]

    queries = []

    # ── BIOTECH ACCOUNT QUERIES ──────────────────────────────────────────────
    # Account-specific high-signal queries (these don't need ticker filter —
    # these accounts only tweet about biotech catalysts anyway)
    queries += [
        f"site:twitter.com OR site:x.com from:BioPharmCatalyst PDUFA OR topline OR readout after:{last_7days}",
        f"site:twitter.com OR site:x.com from:adamfeuerstein topline OR results OR failed OR succeeded after:{last_7days}",
        f"site:twitter.com OR site:x.com from:bradloncar catalyst OR readout OR PDUFA after:{last_7days}",
        f"site:twitter.com OR site:x.com from:ohsnapitsjuliee calls OR puts OR flow OR unusual biotech after:{last_7days}",
        f"site:twitter.com OR site:x.com from:JohnCarlisle_ calls OR puts OR flow unusual biotech after:{last_7days}",
        f"site:twitter.com OR site:x.com from:PurpleCowBiomed phase 3 OR endpoint OR trial after:{last_7days}",
        f"site:twitter.com OR site:x.com from:KonamiEd catalyst OR readout OR PDUFA OR binary after:{last_7days}",
    ]

    # ── DRUG-SPECIFIC QUERIES (most alpha-generating) ────────────────────────
    # Batch drugs 3-4 per query to cover watchlist
    drug_batch = sorted(set(drug_terms))[:20]
    for i in range(0, len(drug_batch), 4):
        batch = drug_batch[i:i+4]
        query_str = " OR ".join(f'"{d}"' for d in batch)
        queries.append(
            f"({query_str}) topline OR failed OR succeeded OR results after:{last_7days}"
        )
        # Twitter-specific
        queries.append(
            f"site:twitter.com OR site:x.com ({query_str}) after:{last_7days}"
        )

    # ── TICKER + CATALYST QUERIES ────────────────────────────────────────────
    ticker_batch = sorted(set(ticker_terms))[:16]
    for i in range(0, len(ticker_batch), 4):
        batch = ticker_batch[i:i+4]
        query_str = " OR ".join(f'${t}' for t in batch)  # $ prefix for stock tickers
        queries.append(
            f"({query_str}) phase 3 OR topline OR PDUFA OR catalyst after:{last_7days}"
        )

    # ── DEFENSE/CONTRACT QUERIES ─────────────────────────────────────────────
    queries += [
        f"site:twitter.com OR site:x.com from:BreakingDefense contract award millions OR billions after:{last_7days}",
        f"site:twitter.com OR site:x.com from:SpaceForceDoD contract OR award after:{last_7days}",
        f"site:twitter.com OR site:x.com from:NASASpaceflight contract OR award OR selected after:{last_7days}",
        f"site:twitter.com OR site:x.com from:DefenseOne contract award billions after:{last_7days}",
        # Defense contract keywords
        f"rocket lab OR kratos OR \"intuitive machines\" OR \"AST SpaceMobile\" contract award {year}",
    ]

    # ── ADCOM QUERIES ────────────────────────────────────────────────────────
    queries += [
        f"site:twitter.com OR site:x.com from:FDASpox advisory committee OR adcom OR vote after:{last_7days}",
        f"site:twitter.com OR site:x.com from:DrJohnBurke FDA advisory OR adcom after:{last_7days}",
        f"site:twitter.com OR site:x.com from:TrialSiteNews advisory committee after:{last_7days}",
        f"FDA advisory committee vote {year} site:twitter.com OR site:x.com",
    ]

    # ── LATE-BREAKING CONFERENCE ABSTRACTS ──────────────────────────────────
    queries += [
        f"site:twitter.com OR site:x.com late-breaking oral ASCO {year} phase 3",
        f"site:twitter.com OR site:x.com late-breaking abstract AACR {year} phase 3",
        f"site:twitter.com OR site:x.com late breaking ASH {year} hematology results",
        f"late-breaking oral abstract ASCO {year} phase 3 positive",
        f"\"late-breaking\" clinical trial NEJM {year} OR Lancet {year}",
    ]

    # ── OPTIONS FLOW QUERIES ──────────────────────────────────────────────────
    queries += [
        f"site:twitter.com OR site:x.com from:ohsnapitsjuliee unusual activity biotech after:{last_7days}",
        f"biotech unusual options activity flow {' OR '.join('$'+t for t in ticker_batch[:8])} after:{last_7days}",
    ]

    # Deduplicate
    seen_q = set()
    unique_queries = []
    for q in queries:
        q_clean = q.strip()
        if q_clean not in seen_q:
            seen_q.add(q_clean)
            unique_queries.append(q_clean)

    return unique_queries


# ─────────────────────────────────────────────────────────────────────────────
# RESULT PARSER
# ─────────────────────────────────────────────────────────────────────────────

def classify_result_signal(text: str, url: str,
                            watchlist_terms: dict) -> Optional[dict]:
    """
    Classify a search result for signal type and ticker relevance.

    Args:
        text: Combined title + snippet
        url: Source URL
        watchlist_terms: dict from build_watchlist_terms()

    Returns signal dict or None if no relevant signal.
    """
    text_lower = text.lower()

    # Find matching tickers from watchlist
    matched_tickers = []
    matched_sector  = "BIOTECH"
    for term, meta in watchlist_terms.items():
        if term and len(term) >= 3 and term in text_lower:
            t = meta.get("ticker", "")
            if t and t not in matched_tickers:
                matched_tickers.append(t)
                matched_sector = meta.get("sector", matched_sector)

    # Also try $ prefix ticker matches
    dollar_tickers = re.findall(r'\$([A-Z]{2,6})\b', text)
    for dt in dollar_tickers:
        if dt not in matched_tickers:
            # Check if in watchlist
            for meta in watchlist_terms.values():
                if meta.get("ticker") == dt:
                    matched_tickers.append(dt)
                    break

    # Classify signal type (check in priority order — CRITICAL first)
    signal_type = None
    for stype in [
        "TOPLINE_POSITIVE", "TOPLINE_NEGATIVE", "ADCOM_RESULT",
        "LATE_BREAKING_ABSTRACT", "OPTIONS_FLOW_UNUSUAL", "CONTRACT_AWARD",
        "DATE_CONFIRMED", "DATE_MENTION",
    ]:
        keywords = SIGNAL_KEYWORDS.get(stype, [])
        if any(kw.lower() in text_lower for kw in keywords):
            signal_type = stype
            break

    if not signal_type:
        return None

    # For lower-severity signals, require ticker relevance
    if signal_type in ("DATE_MENTION",) and not matched_tickers:
        return None

    # Extract source account if from twitter
    account = ""
    acct_match = re.search(r'(?:twitter\.com|x\.com)/([A-Za-z0-9_]+)/', url)
    if acct_match:
        account = acct_match.group(1)
    # Also check title for "from @handle" patterns
    from_match = re.search(r'@([A-Za-z0-9_]+)', text)
    if from_match and not account:
        account = from_match.group(1)

    # Extract date/time hint if present
    date_hint = ""
    date_match = re.search(
        r'(?:Q[1-4]\s*202[0-9]|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4})',
        text, re.I
    )
    if date_match:
        date_hint = date_match.group(0)

    return {
        "signal_type": signal_type,
        "severity":    SIGNAL_SEVERITY.get(signal_type, "LOW"),
        "icon":        SIGNAL_ICONS.get(signal_type, "👁"),
        "matched_tickers": matched_tickers,
        "primary_ticker":  matched_tickers[0] if matched_tickers else "UNKNOWN",
        "sector":      matched_sector,
        "account":     account,
        "date_hint":   date_hint,
        "text_preview": text[:300],
        "url":         url,
    }


def parse_twitter_results(results: list) -> list:
    """
    Parse web search results returned by the cron agent.

    Args:
        results: list of dicts with keys: title, snippet/body/description, url/href

    Returns:
        list of classified signal dicts
    """
    watchlist_terms = build_watchlist_terms()
    state = _load_twitter_state()
    today = datetime.date.today().isoformat()

    seen_urls = set(state.get("seen_posts", []))
    signals   = []

    for result in results:
        # Normalize result dict
        title   = result.get("title", "") or ""
        snippet = (
            result.get("snippet") or result.get("body") or
            result.get("description") or result.get("summary") or ""
        )
        url     = result.get("url") or result.get("href") or ""

        # Skip already seen
        url_key = url[:200] if url else hashlib.md5_fake(title)
        if url and url in seen_urls:
            continue

        text = f"{title} {snippet}"
        if len(text.strip()) < 10:
            continue

        classification = classify_result_signal(text, url, watchlist_terms)
        if not classification:
            continue

        signal = {
            "source":          f"@{classification['account']}" if classification['account'] else "web_search",
            "ticker":          classification["primary_ticker"],
            "all_tickers":     classification["matched_tickers"],
            "type":            classification["signal_type"],
            "severity":        classification["severity"],
            "icon":            classification["icon"],
            "sector":          classification["sector"],
            "detail":          text[:300],
            "url":             url,
            "date_hint":       classification["date_hint"],
            "account":         classification["account"],
            "detected_date":   today,
        }

        signals.append(signal)
        if url:
            seen_urls.add(url)
        print(
            f"  {signal['icon']} [{signal['severity']}] @{signal['account'] or '?'} "
            f"[{signal['ticker']}] {signal['type']}: {text[:80]}"
        )

    # Save state
    state["seen_posts"]   = list(seen_urls)[-2000:]
    state["last_checked"] = datetime.datetime.now().isoformat()
    _save_twitter_state(state)

    # Save output report
    _save_twitter_report(signals, today)

    # Print summary
    critical = [s for s in signals if s["severity"] == "CRITICAL"]
    high     = [s for s in signals if s["severity"] == "HIGH"]
    print(f"\n  Twitter signals: {len(signals)} total | {len(critical)} CRITICAL | {len(high)} HIGH")

    return signals


# ─────────────────────────────────────────────────────────────────────────────
# STATE & REPORT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _load_twitter_state() -> dict:
    if TWITTER_STATE_FILE.exists():
        with open(TWITTER_STATE_FILE) as f:
            return json.load(f)
    return {"seen_posts": [], "last_checked": None}

def _save_twitter_state(state: dict):
    TWITTER_STATE_FILE.parent.mkdir(exist_ok=True)
    with open(TWITTER_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def _save_twitter_report(signals: list, today: str):
    OUTPUT_FILE.parent.mkdir(exist_ok=True)
    report = {
        "run_date": today,
        "signals":  signals,
        "critical": [s for s in signals if s["severity"] == "CRITICAL"],
        "high":     [s for s in signals if s["severity"] == "HIGH"],
        "total":    len(signals),
        "accounts_monitored": [a["handle"] for a in ALL_ACCOUNTS],
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(report, f, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# BACKWARD COMPATIBILITY — legacy run_twitter_monitor()
# ─────────────────────────────────────────────────────────────────────────────

def run_twitter_monitor():
    """
    Legacy entry point. Builds queries and returns them for the cron agent.
    Does NOT make HTTP requests itself — agent-side execution only.
    Returns the query list and an empty signals list (cron fills this).
    """
    today = datetime.date.today().isoformat()
    print(f"\n{'='*70}")
    print(f"TWITTER/X INTELLIGENCE MONITOR v2 — {today}")
    print(f"{'='*70}")

    queries = build_twitter_search_queries()
    watchlist = build_watchlist_terms()

    print(f"\n  Watchlist terms: {len(watchlist)}")
    print(f"  Queries built: {len(queries)}")
    print(f"\n  QUERIES FOR CRON AGENT (run via web_search tool):")
    for i, q in enumerate(queries[:10], 1):
        print(f"    {i}. {q[:100]}")
    if len(queries) > 10:
        print(f"    ... and {len(queries)-10} more")

    print(f"\n  Note: Cron agent runs these via web_search, then calls parse_twitter_results()")
    print(f"{'='*70}\n")

    return {
        "run_date": today,
        "queries": queries,
        "watchlist_term_count": len(watchlist),
        "accounts_monitored": [a["handle"] for a in ALL_ACCOUNTS],
        "signals": [],  # Filled by cron after running queries
    }


# Allow direct hash computation without importing hashlib at module level
try:
    import hashlib as _hashlib
    def _url_hash(s: str) -> str:
        return _hashlib.md5(s.encode()).hexdigest()[:12]
except:
    def _url_hash(s: str) -> str:
        return str(abs(hash(s)))[:12]

# Patch the fake md5 reference used in parse_twitter_results
class _FakeHashlib:
    @staticmethod
    def md5_fake(s: str) -> str:
        return _url_hash(s)

import sys as _sys
# patch module-level reference
_current_module = _sys.modules[__name__]
setattr(_current_module, 'hashlib', _FakeHashlib)


if __name__ == "__main__":
    # Test query building
    queries = build_twitter_search_queries()
    print(f"Built {len(queries)} queries")
    for q in queries[:5]:
        print(f"  {q}")

    # Test watchlist building
    wl = build_watchlist_terms()
    print(f"\nWatchlist terms: {len(wl)}")

    # Test result parsing with mock results
    mock_results = [
        {
            "title": "@adamfeuerstein: IDYA darovasertib topline positive phase 3 met primary endpoint",
            "snippet": "IDEAYA Biosciences $IDYA OptimUM-02 data positive — statistically significant improvement",
            "url": "https://twitter.com/adamfeuerstein/status/123456789",
        },
        {
            "title": "Rocket Lab wins $100M Space Force contract for satellite launch",
            "snippet": "RKLB Rocket Lab USA awarded contract by US Space Force for responsive launch",
            "url": "https://twitter.com/BreakingDefense/status/987654321",
        },
    ]
    signals = parse_twitter_results(mock_results)
    print(f"\nParsed {len(signals)} signals from mock results")
