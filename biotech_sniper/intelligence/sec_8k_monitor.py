#!/usr/bin/env python3
"""
SEC EDGAR 8-K REAL-TIME MONITOR
Watches for 8-K filings from watchlist companies.
Topline data is almost always filed as an 8-K the same morning as the PR.

Two modes:
1. INTRADAY (run every 30 min during market hours) — catches filings within minutes
2. DAILY (run at 6 AM) — catches overnight filings

Signals:
  🔴 BREAKING: 8-K filed by watchlist company — topline data may be dropping NOW
  🟡 WATCH: 8-K filed but appears to be routine (earnings, governance, etc.)
"""

import json
import requests
import datetime
import re
import feedparser
from pathlib import Path

from biotech_sniper.paths import BASE_DIR
REGISTRY_FILE = BASE_DIR / "intelligence/nct_registry.json"
SEC_STATE_FILE = BASE_DIR / "state/sec_8k_state.json"
OUTPUT_FILE = BASE_DIR / "intelligence/sec_8k_report.json"

# Keywords in 8-K that suggest topline data (check item type)
TOPLINE_8K_ITEMS = [
    "item 8.01",  # "Other Events" — most topline data PRs
    "item 7.01",  # "Regulation FD Disclosure"
]

TOPLINE_CONTENT_KEYWORDS = [
    "topline", "top-line", "top line", "primary endpoint", "phase 3", "phase 2",
    "clinical trial results", "efficacy", "progression-free survival", "overall survival",
    "response rate", "hazard ratio", "statistically significant", "met primary",
    "missed primary", "did not meet", "positive results", "negative results",
    "failed", "succeeded", "p-value", "p<0.05", "p=0."
]

ROUTINE_8K_ITEMS = [
    "item 5.02",  # Departure/election of directors
    "item 5.03",  # Amendments to articles
    "item 9.01",  # Financial statements
    "item 2.02",  # Results of operations (earnings)
]

def load_registry():
    with open(REGISTRY_FILE) as f:
        return json.load(f)

def load_sec_state():
    if SEC_STATE_FILE.exists():
        with open(SEC_STATE_FILE) as f:
            return json.load(f)
    return {"seen_filings": [], "last_checked": None}

def save_sec_state(state):
    SEC_STATE_FILE.parent.mkdir(exist_ok=True)
    with open(SEC_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def get_company_cik(ticker):
    """Get CIK for a company from SEC EDGAR."""
    url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=&CIK={ticker}&type=8-K&dateb=&owner=include&count=5&search_text=&action=getcompany"
    # Use the pre-configured CIK from registry instead
    return None

def fetch_company_8k_filings(cik, days_back=2):
    """Fetch recent 8-K filings for a company using their CIK."""
    # EDGAR company search by CIK
    url = f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
    headers = {"User-Agent": "BioCatalystBot research@mantisvc.com"}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        
        filings = data.get("filings", {}).get("recent", {})
        forms = filings.get("form", [])
        dates = filings.get("filingDate", [])
        accessions = filings.get("accessionNumber", [])
        descriptions = filings.get("primaryDocument", [])
        
        cutoff = (datetime.date.today() - datetime.timedelta(days=days_back)).isoformat()
        
        recent_8ks = []
        for i, (form, date, acc, desc) in enumerate(zip(forms, dates, accessions, descriptions)):
            if form == "8-K" and date >= cutoff:
                recent_8ks.append({
                    "form": form,
                    "date": date,
                    "accession": acc,
                    "primary_doc": desc,
                    "filing_url": f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/{acc.replace('-','')}/{desc}"
                })
        
        return recent_8ks
    except Exception as e:
        return [{"error": str(e)}]

def fetch_sec_rss_recent(count=40):
    """Fetch latest 8-K filings from SEC EDGAR RSS feed."""
    url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count={count}&search_text=&output=atom"
    headers = {"User-Agent": "BioCatalystBot research@mantisvc.com"}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        feed = feedparser.parse(r.text)
        filings = []
        for entry in feed.entries:
            filings.append({
                "title": entry.get("title", ""),
                "company": entry.get("companysearch", entry.get("title", "").split(" - ")[0] if " - " in entry.get("title","") else ""),
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
                "summary": entry.get("summary", "")[:500]
            })
        return filings
    except Exception as e:
        return [{"error": str(e)}]

def classify_8k(filing_text, ticker):
    """Classify an 8-K as topline data vs routine."""
    text_lower = filing_text.lower()
    
    found_topline = any(kw in text_lower for kw in TOPLINE_CONTENT_KEYWORDS)
    found_topline_items = any(item in text_lower for item in TOPLINE_8K_ITEMS)
    found_routine = any(item in text_lower for item in ROUTINE_8K_ITEMS)
    
    if found_topline and found_topline_items and not found_routine:
        return "TOPLINE_DATA", "CRITICAL"
    elif found_topline:
        return "POSSIBLE_TOPLINE", "HIGH"
    elif found_routine:
        return "ROUTINE", "LOW"
    else:
        return "UNKNOWN", "MODERATE"

def run_8k_monitor(mode="daily"):
    """
    mode: "daily" (full check) or "intraday" (quick RSS check only)
    """
    registry = load_registry()
    state = load_sec_state()
    seen = set(state.get("seen_filings", []))
    all_signals = []
    
    print(f"\n{'='*70}")
    print(f"SEC 8-K MONITOR [{mode.upper()}] — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M ET')}")
    print(f"{'='*70}")
    
    # Build lookup: company name variations → ticker
    company_to_ticker = {}
    for ticker, info in registry["watchlist"].items():
        company = info.get("company", "").lower()
        company_to_ticker[company] = ticker
        # Also index shortened versions
        words = company.split()
        if words:
            company_to_ticker[words[0].lower()] = ticker
    
    today = datetime.date.today().isoformat()
    
    # ── MODE 1: INTRADAY — Quick RSS scan ────────────────────────────────
    print("\nChecking SEC EDGAR RSS feed for recent 8-K filings...")
    rss_filings = fetch_sec_rss_recent(40)
    
    new_filings = []
    for filing in rss_filings:
        if "error" in filing:
            print(f"  RSS ERROR: {filing['error']}")
            continue
        
        title = filing.get("title", "").lower()
        link = filing.get("link", "")
        
        # Check if this matches any watchlist company
        matched_ticker = None
        for company_key, ticker in company_to_ticker.items():
            if company_key and len(company_key) > 3 and company_key in title:
                matched_ticker = ticker
                break
        
        if matched_ticker and link not in seen:
            seen.add(link)
            new_filings.append({
                "ticker": matched_ticker,
                "filing": filing
            })
    
    for item in new_filings:
        ticker = item["ticker"]
        filing = item["filing"]
        filing_type, severity = classify_8k(filing.get("summary", "") + filing.get("title", ""), ticker)
        
        icon = "🔴" if filing_type == "TOPLINE_DATA" else ("🟡" if filing_type == "POSSIBLE_TOPLINE" else "⚪")
        sentiment = "BREAKING_DATA" if filing_type == "TOPLINE_DATA" else ("WATCH" if filing_type == "POSSIBLE_TOPLINE" else "ROUTINE")
        
        signal = {
            "ticker": ticker,
            "type": f"8K_FILED_{filing_type}",
            "sentiment": sentiment,
            "severity": severity,
            "icon": icon,
            "detail": f"8-K filed: {filing.get('title', '')[:100]}",
            "filing_url": filing.get("link", ""),
            "implication": "TOPLINE DATA MAY BE DROPPING — check immediately and prepare orders" if filing_type == "TOPLINE_DATA" else "New 8-K filed — review for clinical data",
            "detected_date": today,
            "published": filing.get("published", "")
        }
        
        all_signals.append(signal)
        print(f"  {icon} [{ticker}] {filing_type}: {filing.get('title','')[:80]}")
    
    if not new_filings:
        print(f"  ✓ No new 8-K filings from watchlist companies since last check")
    
    # ── MODE 2: DAILY — Per-company CIK check ───────────────────────────
    if mode == "daily":
        print("\nRunning per-company CIK check (last 2 days)...")
        for ticker, info in registry["watchlist"].items():
            cik = info.get("sec_cik", "").lstrip("0")
            if not cik:
                continue
            
            filings = fetch_company_8k_filings(cik, days_back=2)
            for f in filings:
                if "error" in f:
                    continue
                acc = f.get("accession", "")
                if acc and acc not in seen:
                    seen.add(acc)
                    print(f"  📄 {ticker}: New 8-K on {f['date']} — {f.get('primary_doc','')}")
                    all_signals.append({
                        "ticker": ticker,
                        "type": "8K_FROM_CIK_CHECK",
                        "sentiment": "WATCH",
                        "severity": "MODERATE",
                        "icon": "📄",
                        "detail": f"8-K filed {f['date']}",
                        "filing_url": f.get("filing_url", ""),
                        "implication": "Review 8-K for clinical data content",
                        "detected_date": today
                    })
    
    # Save state
    state["seen_filings"] = list(seen)[-500:]  # Keep last 500
    state["last_checked"] = datetime.datetime.now().isoformat()
    save_sec_state(state)
    
    # Save report
    report = {
        "run_date": today,
        "mode": mode,
        "signals": all_signals,
        "breaking": [s for s in all_signals if s["sentiment"] == "BREAKING_DATA"],
        "total": len(all_signals)
    }
    
    with open(OUTPUT_FILE, "w") as f:
        json.dump(report, f, indent=2)

    # Persist into news_events SQLite table per VAL-M2-075.
    try:
        _persist_sec_8k_signals_to_news_events(all_signals)
    except Exception as e:  # pragma: no cover - defensive
        print(f"  [sec_8k] news_events persistence failed: {e}")

    print(f"\n{'='*70}")
    breaking = report["breaking"]
    if breaking:
        print(f"  🔴 BREAKING: {len(breaking)} potential topline data filings!")
        for s in breaking:
            print(f"     {s['ticker']}: {s['detail']}")
            print(f"     URL: {s['filing_url']}")
    else:
        print(f"  ✓ No breaking topline data 8-Ks detected")
    print(f"{'='*70}\n")
    
    return report


def _persist_sec_8k_signals_to_news_events(signals: list) -> None:
    """Mirror SEC 8-K signals into the SQLite news_events table.

    Best-effort: failures here must not break the legacy JSON output
    or short-circuit the run; we log and continue.
    """
    if not signals:
        return

    from biotech_sniper import db as _db
    from biotech_sniper.news_events import (
        NewsEvent,
        SOURCE_SEC_8K,
        default_db_path,
        record_news_events,
    )

    events: list[NewsEvent] = []
    for signal in signals:
        ticker = signal.get("ticker")
        if not ticker:
            continue
        events.append(
            NewsEvent(
                ticker=str(ticker),
                source=SOURCE_SEC_8K,
                title=str(signal.get("detail") or signal.get("type") or "8-K signal"),
                url=signal.get("filing_url") or None,
                published_at=signal.get("published") or signal.get("detected_date") or None,
                raw_payload=signal,
            )
        )

    if not events:
        return

    target = default_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = _db.connect(target)
    try:
        _db.run_migrations(conn)
        record_news_events(conn, events)
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    run_8k_monitor(mode)
