#!/usr/bin/env python3
"""
GOVERNMENT CONTRACT SNIPER — MULTI-SOURCE
Data sources (in priority order):

1. USASpending.gov Awards API — FREE, no auth, real FPDS data, always works
   https://api.usaspending.gov — returns actual awarded contracts
   Updated daily. Best for finding recent awards to tracked companies.

2. USASpending.gov IDV (Indefinite Delivery Vehicles) — frameworks & task orders

3. SAM.gov web search (via browser_task in cron) — J&A and pre-solicitations
   api.sam.gov requires API key (returns 404 without one)
   Cron agent uses browser_task on sam.gov search UI instead

4. Federal Register RSS — public DoD acquisition notices

Signal tiers:
  TIER 1: J&A / Sole-source filing on SAM.gov — ~87% win (found via browser)
  TIER 2: Narrow pre-solicitation — ~62% win
  TIER 3: USASpending award notice — event already occurred (tracking/scoring)
"""

import json
import logging
import requests
import datetime
import re
from pathlib import Path

from biotech_sniper.paths import BASE_DIR

log = logging.getLogger(__name__)


# f-misc-03: ``BIOTECH_SNIPER_HOME`` (the value that drives
# :data:`BASE_DIR`) varies by deploy: on the VPS it points at the repo
# root (``/root/alpha_sniper/repo``) so ``BASE_DIR/sectors/...``
# resolves correctly, but on local clones (and inside hermetic test
# fixtures) it points at the package parent so the runtime asset
# lives under ``BASE_DIR/biotech_sniper/sectors/...``. Mirror the
# candidate-path approach added to ``audit.py`` in f-m2-07: try every
# plausible layout and warn-but-continue if none is present rather
# than letting a ``FileNotFoundError`` blow up the entire scan.
TICKER_MAP_CANDIDATES = (
    BASE_DIR / "biotech_sniper/sectors/contracts/company_ticker_map.json",
    BASE_DIR / "sectors/contracts/company_ticker_map.json",
)


def _resolve_ticker_map_path() -> Path:
    """Return the first existing ``company_ticker_map.json`` candidate.

    Falls back to the first candidate when none of the paths exist so
    callers using ``open(...)`` raise the canonical ``FileNotFoundError``
    against a path that mirrors the canonical layout (matters for
    error-message readability).
    """
    for candidate in TICKER_MAP_CANDIDATES:
        if candidate.is_file():
            return candidate
    return TICKER_MAP_CANDIDATES[0]


TICKER_MAP_FILE      = _resolve_ticker_map_path()
CONTRACTS_STATE_FILE = BASE_DIR / "state/contracts_state.json"
CONTRACTS_OUTPUT     = BASE_DIR / "sectors/contracts/contracts_report.json"

HEADERS = {"User-Agent": "ContractSniperBot research@mantisvc.com"}
USA_SPENDING_BASE = "https://api.usaspending.gov/api/v2"


def load_ticker_map() -> dict:
    """Load ``company_ticker_map.json`` with graceful fallback.

    f-misc-03: if no candidate path exists (e.g. fresh checkout where
    the asset has not yet been seeded into the active tree, or
    hermetic test environments without sandbox state), log a warning
    and return an empty ticker map. Callers downstream interpret an
    empty ``companies`` block as "no tickers tracked" and short-circuit
    cleanly to a zero-match response.
    """
    path = _resolve_ticker_map_path()
    if not path.is_file():
        log.warning(
            "company_ticker_map.json not found in any candidate path "
            "(searched: %s); returning empty ticker map",
            [str(p) for p in TICKER_MAP_CANDIDATES],
        )
        return {"companies": {}}
    with open(path) as f:
        return json.load(f)

def load_state():
    if CONTRACTS_STATE_FILE.exists():
        with open(CONTRACTS_STATE_FILE) as f:
            return json.load(f)
    return {"seen_notices": [], "seen_award_ids": [], "last_run": None}

def save_state(state):
    CONTRACTS_STATE_FILE.parent.mkdir(exist_ok=True)
    with open(CONTRACTS_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── SOURCE 1: USASpending.gov ────────────────────────────────────────────────

def search_usaspending_awards(company_names: list, days_back: int = 7) -> list:
    """
    Search USASpending.gov for recent contract awards to tracked companies.
    100% free, no auth, real FPDS-NG data.
    """
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=days_back)).isoformat()
    end   = today.isoformat()

    results = []
    for company in company_names:
        try:
            payload = {
                "filters": {
                    "time_period": [{"start_date": start, "end_date": end}],
                    "award_type_codes": ["A", "B", "C", "D"],
                    "keywords": [company]
                },
                "fields": [
                    "Award ID", "Recipient Name", "Award Amount",
                    "Awarding Agency", "Awarding Sub Agency",
                    "Description", "Period of Performance Start Date",
                    "Period of Performance Current End Date",
                    "Place of Performance City Name", "naics_code"
                ],
                "limit": 10, "page": 1,
                "sort": "Award Amount", "order": "desc"
            }
            r = requests.post(
                f"{USA_SPENDING_BASE}/search/spending_by_award/",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=20
            )
            if r.status_code == 200:
                data = r.json()
                for award in data.get("results", []):
                    award["_search_term"] = company
                    results.append(award)
        except Exception as e:
            print(f"    USASpending error for {company}: {e}")

    return results


def search_usaspending_by_keyword(keywords: list, days_back: int = 7) -> list:
    """
    Broader keyword search — catches awards where exact company name varies.
    """
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=days_back)).isoformat()
    end   = today.isoformat()

    results = []
    try:
        payload = {
            "filters": {
                "time_period": [{"start_date": start, "end_date": end}],
                "award_type_codes": ["A", "B", "C", "D"],
                "keywords": keywords
            },
            "fields": [
                "Award ID", "Recipient Name", "Award Amount",
                "Awarding Agency", "Awarding Sub Agency",
                "Description", "naics_code"
            ],
            "limit": 25, "page": 1,
            "sort": "Award Amount", "order": "desc"
        }
        r = requests.post(
            f"{USA_SPENDING_BASE}/search/spending_by_award/",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=20
        )
        if r.status_code == 200:
            results = r.json().get("results", [])
            print(f"    USASpending keyword search: {len(results)} awards")
    except Exception as e:
        print(f"    USASpending keyword error: {e}")

    return results


def get_company_award_history(recipient_name: str, years_back: int = 3) -> dict:
    """
    Get award history for a company — useful for estimating win probability.
    """
    try:
        start = (datetime.date.today() - datetime.timedelta(days=365 * years_back)).isoformat()
        end   = datetime.date.today().isoformat()
        payload = {
            "filters": {
                "time_period": [{"start_date": start, "end_date": end}],
                "recipient_search_text": [recipient_name]
            },
            "category": "recipient",
            "limit": 1, "page": 1
        }
        r = requests.post(
            f"{USA_SPENDING_BASE}/search/spending_by_category/recipient/",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            results = data.get("results", [])
            if results:
                return {
                    "total_awards": results[0].get("amount", 0),
                    "name": results[0].get("name", ""),
                }
    except:
        pass
    return {}


# ── SOURCE 2: Defense.gov RSS ───────────────────────────────────────────────

def fetch_defense_gov_contracts(days_back: int = 3) -> list:
    """
    Fetch Defense.gov daily contract announcements RSS.
    DoD publishes EVERY contract awarded same day at:
    https://www.defense.gov/News/Contracts/rss/
    This is the authoritative source for DoD awards.

    Args:
        days_back: How many days back to look (default 3)

    Returns:
        list of matched award dicts with ticker, amount, agency, description
    """
    ticker_map = load_ticker_map()
    state      = load_state()
    seen_ids   = set(state.get("seen_award_ids", []))
    today      = datetime.date.today()
    cutoff     = today - datetime.timedelta(days=days_back)

    # Build all match terms: term_lower → (company_name, ticker, mkt_cap_tier, has_options)
    match_terms = {}
    for company, info in ticker_map.get("companies", {}).items():
        if not info.get("ticker"):
            continue
        for name in [company] + info.get("aliases", []):
            if name and len(name) > 3:
                match_terms[name.lower()] = {
                    "company": company,
                    "ticker": info["ticker"],
                    "mkt_cap_tier": info.get("mkt_cap_tier", "small"),
                    "has_options": info.get("options", False),
                }

    results = []
    print(f"  Fetching Defense.gov contracts RSS (last {days_back} days)...")

    try:
        # Try feedparser first
        feed_entries = []
        try:
            import feedparser
            feed = feedparser.parse("https://www.defense.gov/News/Contracts/rss/")
            feed_entries = feed.entries
        except ImportError:
            import email.utils
            r = requests.get(
                "https://www.defense.gov/News/Contracts/rss/",
                headers=HEADERS, timeout=25
            )
            if r.status_code != 200:
                print(f"  Defense.gov RSS: HTTP {r.status_code}")
                return []

            # Manual RSS parse
            items = re.findall(r'<item>(.*?)</item>', r.text, re.S)
            for item in items:
                class _FeedEntry:
                    pass
                e = _FeedEntry()

                title_m   = re.search(r'<title>(?:<!\.\[CDATA\[)?(.*?)(?:\]\]>)?</title>', item, re.S) or \
                            re.search(r'<title>(.*?)</title>', item, re.S)
                desc_m    = re.search(r'<description>(?:<!\.\[CDATA\[)?(.*?)(?:\]\]>)?</description>', item, re.S) or \
                            re.search(r'<description>(.*?)</description>', item, re.S)
                link_m    = re.search(r'<link>(.*?)</link>', item, re.S)
                pub_m     = re.search(r'<pubDate>(.*?)</pubDate>', item, re.S)

                e.title   = re.sub(r'<[^>]+>', '', title_m.group(1)).strip() if title_m else ""
                e.summary = re.sub(r'<[^>]+>', ' ', desc_m.group(1)).strip() if desc_m else ""
                e.link    = link_m.group(1).strip() if link_m else ""

                pub_str = pub_m.group(1).strip() if pub_m else ""
                try:
                    pt = email.utils.parsedate(pub_str)
                    e.published_parsed = pt
                except:
                    e.published_parsed = None
                feed_entries.append(e)

        print(f"  Defense.gov RSS: {len(feed_entries)} entries")

        for entry in feed_entries:
            # Parse publish date
            pub = getattr(entry, "published_parsed", None)
            pub_date = None
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

            # Clean HTML from summary
            summary_clean = re.sub(r'<[^>]+>', ' ', summary)
            full_text     = f"{title} {summary_clean}".lower()

            # Extract dollar amount from text
            amount_raw = 0.0
            amount_str = ""
            # Pattern: $XXX,XXX or $XXX million or $X.X billion
            amt_patterns = [
                r'\$([\d,]+(?:\.\d+)?)\s*(billion|million|thousand)',
                r'\$(\d{1,3}(?:,\d{3})+)',
                r'valued\s+at\s+\$([\d,]+(?:\.\d+)?)\s*(billion|million|thousand)?',
            ]
            for pattern in amt_patterns:
                m = re.search(pattern, summary_clean, re.I)
                if m:
                    try:
                        num = float(m.group(1).replace(',', ''))
                        mult = (m.group(2) or "").lower() if len(m.groups()) >= 2 else ""
                        if "billion" in mult:  num *= 1e9
                        elif "million" in mult: num *= 1e6
                        elif "thousand" in mult: num *= 1e3
                        amount_raw = num
                        amount_str = f"${num/1e6:.1f}M" if num >= 1e6 else f"${num:,.0f}"
                    except:
                        pass
                    break

            # Extract agency name (DoD sub-agency from title)
            agency = "Department of Defense"
            agency_patterns = [
                r'(?:Army|Navy|Air Force|Space Force|Marine Corps|DARPA|MDA|DLA|'  
                r'SOCOM|TRANSCOM|CENTCOM|INDOPACOM|EUCOM|AFRICOM|CYBERCOM)',
            ]
            for pattern in agency_patterns:
                am = re.search(pattern, f"{title} {summary_clean}", re.I)
                if am:
                    agency = f"DoD / {am.group(0)}"
                    break

            # Generate stable entry ID
            id_source = link or f"{title[:60]}{pub_date or ''}"
            import hashlib as _hl
            entry_id = f"dod_{_hl.md5(id_source.encode()).hexdigest()[:12]}"

            if entry_id in seen_ids:
                continue

            # Match to tracked tickers
            matched = False
            for term, info in match_terms.items():
                if term in full_text:
                    award = {
                        "source":       "defense_gov_rss",
                        "entry_id":     entry_id,
                        "ticker":       info["ticker"],
                        "company":      info["company"],
                        "mkt_cap_tier": info["mkt_cap_tier"],
                        "has_options":  info["has_options"],
                        "title":        title[:200],
                        "description":  summary_clean[:400],
                        "amount":       amount_str,
                        "amount_raw":   amount_raw,
                        "agency":       agency,
                        "publish_date": pub_date.isoformat() if pub_date else "",
                        "link":         link,
                        "sector":       "CONTRACT",
                        "detected_date": today.isoformat(),
                    }
                    results.append(award)
                    seen_ids.add(entry_id)
                    matched = True
                    print(
                        f"  [defense_rss] MATCH: {info['ticker']} | {amount_str} | "
                        f"{agency} | {title[:60]}"
                    )
                    break  # Only first match per entry (avoid duplicates)

            if not matched:
                # Still return unmatched entries with no ticker for RSS awareness
                results.append({
                    "source":       "defense_gov_rss",
                    "entry_id":     entry_id,
                    "ticker":       "",
                    "company":      "",
                    "title":        title[:200],
                    "description":  summary_clean[:300],
                    "amount":       amount_str,
                    "amount_raw":   amount_raw,
                    "agency":       agency,
                    "publish_date": pub_date.isoformat() if pub_date else "",
                    "link":         link,
                    "sector":       "CONTRACT",
                    "detected_date": today.isoformat(),
                })
                seen_ids.add(entry_id)

    except Exception as e:
        print(f"  Defense.gov RSS error: {e}")

    # Update state
    state["seen_award_ids"] = list(seen_ids)[-3000:]
    save_state(state)

    matched_count = sum(1 for r in results if r.get("ticker"))
    print(f"  Defense.gov RSS: {len(results)} entries ({matched_count} ticker matches)")
    return results


# ── SOURCE 3: Federal Register RSS ───────────────────────────────────────────

def fetch_federal_register_defense(days_back: int = 3) -> list:
    """Federal Register DoD procurement notices — free RSS, always works."""
    try:
        import feedparser
        url = "https://www.federalregister.gov/documents/search.rss?conditions[type]=NOTICE&conditions[agencies][]=defense-department&conditions[agencies][]=space-force&conditions[agencies][]=nasa"
        feed = feedparser.parse(url)
        cutoff = datetime.date.today() - datetime.timedelta(days=days_back)
        results = []
        for entry in feed.entries[:50]:
            pub = entry.get("published_parsed")
            if pub:
                pub_date = datetime.date(*pub[:3])
                if pub_date >= cutoff:
                    results.append({
                        "title": entry.get("title", ""),
                        "summary": entry.get("summary", "")[:500],
                        "link": entry.get("link", ""),
                        "published": pub_date.isoformat(),
                        "source": "federal_register"
                    })
        print(f"    Federal Register: {len(results)} recent defense notices")
        return results
    except Exception as e:
        print(f"    Federal Register error: {e}")
        return []


# ── MATCHING & SCORING ───────────────────────────────────────────────────────

def match_award_to_ticker(award: dict, ticker_map: dict) -> list:
    """Match an award's recipient name to tracked public company tickers."""
    recipient = award.get("Recipient Name", "").lower()
    description = award.get("Description", "").lower()
    full_text = f"{recipient} {description}"

    matches = []
    for company_name, info in ticker_map.get("companies", {}).items():
        if not info.get("ticker"):
            continue
        # Try company name and all aliases
        check_names = [company_name.lower()] + [a.lower() for a in info.get("aliases", [])]
        for name in check_names:
            if name and len(name) > 3 and name in full_text:
                matches.append({
                    "company": company_name,
                    "ticker": info["ticker"],
                    "mkt_cap_tier": info.get("mkt_cap_tier", "small"),
                    "has_options": info.get("options", True)
                })
                break

    # Deduplicate by ticker
    seen = set()
    out = []
    for m in matches:
        if m["ticker"] not in seen:
            seen.add(m["ticker"])
            out.append(m)
    return out


def score_award_signal(award: dict, match: dict, source: str) -> dict:
    """Assess signal quality and stock impact for a contract award."""
    amount = award.get("Award Amount", 0) or 0
    try:
        amount = float(amount)
    except:
        amount = 0

    agency  = award.get("Awarding Agency", "")
    sub_agency = award.get("Awarding Sub Agency", "")
    desc    = award.get("Description", "")
    ticker  = match["ticker"]
    tier    = match.get("mkt_cap_tier", "small")

    # Signal quality
    is_defense = any(kw in f"{agency} {sub_agency}".lower()
                     for kw in ["defense", "air force", "army", "navy", "space force", "darpa", "nasa"])

    # Impact estimates by market cap tier
    impacts = {
        "small": {"win_pct": "+35-70%", "loss_pct": "-15-30%", "multiplier": 7},
        "mid":   {"win_pct": "+15-30%", "loss_pct": "-8-15%",  "multiplier": 5},
        "large": {"win_pct": "+5-15%",  "loss_pct": "-3-8%",   "multiplier": 3},
    }
    impact = impacts.get(tier, impacts["small"])

    # Severity: big defense award to small cap = CRITICAL
    severity = "LOW"
    if amount > 1e8 and is_defense and tier == "small":
        severity = "CRITICAL"
    elif amount > 5e7 and is_defense:
        severity = "HIGH"
    elif amount > 1e7:
        severity = "MODERATE"
    elif is_defense:
        severity = "MODERATE"

    icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MODERATE": "🟡", "LOW": "⚪"}.get(severity, "⚪")

    return {
        "sector":       "CONTRACT",
        "ticker":       ticker,
        "company":      match["company"],
        "type":         f"AWARD_{source.upper()}",
        "severity":     severity,
        "icon":         icon,
        "title":        desc[:150] if desc else award.get("Award ID", ""),
        "agency":       f"{agency} / {sub_agency}".strip(" /"),
        "award_amount": f"${amount/1e6:.1f}M" if amount >= 1e6 else f"${amount:,.0f}",
        "win_impact":   impact["win_pct"],
        "loss_impact":  impact["loss_pct"],
        "multiplier":   impact["multiplier"],
        "award_id":     award.get("Award ID", ""),
        "detected_date": datetime.date.today().isoformat(),
        "scoring_prompt": build_scoring_prompt(award, match)
    }


def build_scoring_prompt(award: dict, match: dict) -> str:
    ticker  = match["ticker"]
    company = match["company"]
    amount  = award.get("Award Amount", "unknown")
    agency  = award.get("Awarding Agency", "")
    desc    = award.get("Description", "")[:500]

    return f"""You are a government contract award probability analyst.

AWARD: {company} ({ticker})
Agency: {agency}
Amount: ${amount}
Description: {desc}

Score:
1. P(this is the start of a larger contract stream): X%
2. Expected stock move on announcement: +X% to +X%
3. Is this a binary catalyst (win/lose) or incremental?
4. Options recommendation: strike % OTM, expiry, structure

Output: P=XX% | Move: +XX% win / -XX% miss | [notes]"""


# ── MAIN SCAN ────────────────────────────────────────────────────────────────

def run_contract_scan(days_back: int = 7) -> dict:
    """
    Full contract scan across all sources.
    Returns signals dict for the unified report.
    """
    ticker_map = load_ticker_map()
    state      = load_state()
    seen_ids   = set(state.get("seen_award_ids", []))
    today      = datetime.date.today().isoformat()

    print(f"\n{'='*70}")
    print(f"CONTRACT SNIPER — {today}")
    print(f"{'='*70}")

    all_signals = []

    # Get all company names + aliases for searching
    all_companies = list(ticker_map.get("companies", {}).keys())
    all_aliases   = []
    for info in ticker_map.get("companies", {}).values():
        all_aliases.extend(info.get("aliases", []))

    # ── SOURCE 1: USASpending by company name ───────────────────────────
    print(f"\n1. USASpending.gov — company name search ({len(all_companies)} companies):")
    awards = search_usaspending_awards(
        company_names=all_companies[:20],  # batch
        days_back=days_back
    )
    print(f"   Found {len(awards)} awards")

    # ── SOURCE 2: USASpending keyword search (broad) ─────────────────────
    print(f"\n2. USASpending.gov — keyword search:")
    priority_keywords = [
        "Rocket Lab", "Kratos", "AST SpaceMobile", "Intuitive Machines",
        "Mercury Systems", "Joby Aviation", "Archer Aviation",
        "L3Harris", "Palantir", "Anduril", "Shield AI",
        "reusable launch", "autonomous systems", "directed energy",
        "counter-UAS", "hypersonic", "satellite"
    ]
    keyword_awards = search_usaspending_by_keyword(priority_keywords, days_back=days_back)
    awards.extend(keyword_awards)

    # ── SOURCE 2b: Defense.gov Contracts RSS (new) ─────────────────────
    print(f"\n2b. Defense.gov Contracts RSS (authoritative DoD source):")
    defense_rss = fetch_defense_gov_contracts(days_back=days_back)
    defense_matched = [r for r in defense_rss if r.get("ticker") and r.get("has_options")]
    print(f"   Defense.gov: {len(defense_rss)} total | {len(defense_matched)} optionable ticker matches")

    for award in defense_matched:
        award_id = award.get("entry_id", "")
        if award_id in seen_ids:
            continue
        amount_raw = award.get("amount_raw", 0)
        tier = award.get("mkt_cap_tier", "small")
        if amount_raw > 1e8 and tier == "small":
            severity = "CRITICAL"
        elif amount_raw > 5e7:
            severity = "HIGH"
        elif amount_raw > 1e7:
            severity = "MODERATE"
        else:
            severity = "MODERATE"
        icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MODERATE": "🟡", "LOW": "⚪"}.get(severity, "⚪")
        signal = {
            "sector": "CONTRACT", "ticker": award["ticker"],
            "company": award["company"], "type": "AWARD_DEFENSE_GOV_RSS",
            "severity": severity, "icon": icon,
            "title": award.get("title", "")[:150],
            "agency": award.get("agency", ""),
            "award_amount": award.get("amount", ""),
            "description_preview": award.get("description", "")[:200],
            "award_id": award_id, "source_url": award.get("link", ""),
            "detected_date": award.get("detected_date", ""),
            "scoring_prompt": build_scoring_prompt(
                {"Recipient Name": award.get("company",""),
                 "Award Amount": award.get("amount_raw", 0),
                 "Awarding Agency": award.get("agency",""),
                 "Description": award.get("title","")},
                {"ticker": award["ticker"], "company": award["company"]}
            ),
        }
        if severity in ("CRITICAL", "HIGH", "MODERATE"):
            all_signals.append(signal)
            seen_ids.add(award_id)
            print(f"  {icon} [{severity}] {award['ticker']}: DoD RSS — {award.get('amount','')} | {award.get('title','')[:60]}")

    # ── SOURCE 3: Federal Register RSS ──────────────────────────────────
    print(f"\n3. Federal Register defense notices:")
    fed_reg = fetch_federal_register_defense(days_back=days_back)

    # ── PROCESS AWARDS ───────────────────────────────────────────────────
    print(f"\nProcessing {len(awards)} total awards...")
    processed_ids = set()

    for award in awards:
        award_id = award.get("Award ID", str(hash(award.get("Description", "")[:50])))

        if award_id in seen_ids or award_id in processed_ids:
            continue
        processed_ids.add(award_id)

        matches = match_award_to_ticker(award, ticker_map)
        if not matches:
            continue

        for match in matches:
            if not match.get("has_options", True):
                continue  # Skip non-optionable companies

            signal = score_award_signal(award, match, "usaspending")
            signal["award_id"] = award_id

            # Only surface MODERATE+ signals
            if signal["severity"] in ("CRITICAL", "HIGH", "MODERATE"):
                all_signals.append(signal)
                seen_ids.add(award_id)
                print(f"  {signal['icon']} [{signal['severity']}] {signal['ticker']}: "
                      f"{signal['award_amount']} from {signal['agency'][:40]}")

    # ── PROCESS FEDERAL REGISTER ─────────────────────────────────────────
    for notice in fed_reg:
        title   = notice.get("title", "")
        summary = notice.get("summary", "")
        full    = f"{title} {summary}".lower()

        for company_name, info in ticker_map.get("companies", {}).items():
            if not info.get("ticker") or not info.get("options"):
                continue
            check_names = [company_name.lower()] + [a.lower() for a in info.get("aliases", [])]
            if any(n in full for n in check_names if n):
                signal_id = f"fedreg_{notice.get('published','')[:10]}_{info['ticker']}"
                if signal_id not in seen_ids:
                    seen_ids.add(signal_id)
                    all_signals.append({
                        "sector": "CONTRACT", "ticker": info["ticker"],
                        "company": company_name, "type": "FEDERAL_REGISTER_NOTICE",
                        "severity": "MODERATE", "icon": "🟡",
                        "title": title[:150], "agency": "DoD / Federal Register",
                        "description_preview": summary[:200],
                        "notice_url": notice.get("link", ""),
                        "detected_date": today
                    })
                    print(f"  🟡 [MODERATE] {info['ticker']}: Federal Register — {title[:60]}")

    # ── SAM.GOV NOTE FOR CRON ─────────────────────────────────────────────
    # SAM.gov API requires an API key (returns 404 without one).
    # The cron agent handles this via browser_task on:
    #   https://sam.gov/search/?index=opp&pageSize=25&sort=-modifiedDate&sfm[noticeType][0]=Justification
    #   https://sam.gov/search/?index=opp&pageSize=25&sort=-modifiedDate&sfm[noticeType][0]=Presolicitation
    # J&A filings found there get added manually to all_signals by the cron agent.
    print(f"\n  SAM.gov J&A: checked by cron agent via browser (requires JS rendering)")

    # ── SAVE ─────────────────────────────────────────────────────────────
    state["seen_award_ids"] = list(seen_ids)[-3000:]
    state["last_run"] = today
    save_state(state)

    report = {
        "run_date": today,
        "signals": all_signals,
        "critical": [s for s in all_signals if s["severity"] == "CRITICAL"],
        "high":     [s for s in all_signals if s["severity"] == "HIGH"],
        "total":    len(all_signals),
        "needs_scoring": [s for s in all_signals if s["severity"] in ("CRITICAL", "HIGH")]
    }

    with open(CONTRACTS_OUTPUT, "w") as f:
        json.dump(report, f, indent=2)

    print(f"\n{'='*70}")
    print(f"CONTRACTS SUMMARY: {len(all_signals)} signals")
    if report["critical"]:
        print(f"  🔴 CRITICAL: {len(report['critical'])}")
    if report["high"]:
        print(f"  🟠 HIGH: {len(report['high'])}")
    if not all_signals:
        print(f"  ✓ No qualifying contract signals (last {days_back} days)")
    print(f"{'='*70}\n")

    return report


if __name__ == "__main__":
    run_contract_scan(days_back=14)
