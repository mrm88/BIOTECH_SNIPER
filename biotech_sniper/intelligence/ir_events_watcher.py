#!/usr/bin/env python3
"""
IR EVENTS CALENDAR WATCHER — SELF-HEALING
Checks IR events pages for ALL companies in nct_registry.json.

Signals:
  🔴 CRITICAL: Webcast pre-registration opened = announcement date now CERTAIN
  🟠 HIGH: Quiet period language detected = readout imminent
  👁 MODERATE: IR page changed since last check

Fallback chain (tried in order per company):
  1. ir_events_url from registry (fast_info request)
  2. SEC EDGAR 8-K filings for the CIK (always works)
  3. Web search for company + "investor relations events" (cron agent runs this)

Key fix: reads ir_events_url (not ir_url) from registry.
Auto-resolves broken URLs via company_resolver.auto_resolve_missing_ir_urls()
"""

import json
import requests
import re
import datetime
import hashlib
from pathlib import Path

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

from biotech_sniper.paths import BASE_DIR
REGISTRY_FILE  = BASE_DIR / "intelligence/nct_registry.json"
IR_STATE_FILE  = BASE_DIR / "state/ir_events_state.json"
OUTPUT_FILE    = BASE_DIR / "intelligence/ir_events_report.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
SEC_HEADERS = {"User-Agent": "BioCatalystBot research@mantisvc.com"}

TOPLINE_KEYWORDS = [
    "topline", "top-line", "top line", "phase 2", "phase 3", "phase 2/3",
    "data readout", "clinical data", "trial results", "study results",
    "registrational", "pivotal", "primary endpoint", "register", "webcast",
    "optimum", "haelo", "rasolution", "duplex", "accord", "reveal",
    "affinity duchenne", "izar", "uplift", "prevail",
]

QUIET_PERIOD_KEYWORDS = [
    "quiet period", "blackout period", "trading window closed", "pre-announcement quiet"
]

REGISTRATION_KEYWORDS = ["register", "webcast", "listen live", "webinar", "join"]


def _cli_print(*args, **kwargs):
    """Print only when this module is run as the CLI ``__main__`` entry point.

    f-fix-m4-03a: the news_daemon adapter imports this module at runtime
    to drive Stage-1 RSS polling.  Under systemd's
    ``StandardOutput=append:/var/log/alpha_sniper/news.log`` directive,
    bare prints would land in ``news.log`` as non-JSON lines and break
    VAL-M4-017 ("every line is structured JSON with required keys
    ts/level/event/module").  Gating prints behind
    ``__name__ == '__main__'`` keeps CLI-direct usage (banners visible
    when run as a script) while suppressing them under any import path
    (the news_daemon adapter, smoke imports, walk_packages discovery).
    """
    if __name__ == "__main__":
        print(*args, **kwargs)


def load_registry():
    """Return the watchlist registry, or an empty stub if the file is absent.

    f-m4-09: greenfield deployments have no
    ``intelligence/nct_registry.json`` (the registry is built
    incrementally by ``company_resolver`` + ``master_discovery``).
    The legacy ``open(...)`` call raised :class:`FileNotFoundError`,
    surfaced as the WARNING line ``nct_registry.json No such file``
    by the f-m4-08 retry of ``run_ir_events_check``. Returning an
    empty ``{"watchlist": {}}`` makes the watcher a no-op until the
    registry is populated, matching the other loaders.
    """
    if not REGISTRY_FILE.exists():
        return {"watchlist": {}}
    try:
        with open(REGISTRY_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"watchlist": {}}
    if not isinstance(data, dict):
        return {"watchlist": {}}
    data.setdefault("watchlist", {})
    return data

def load_ir_state():
    if IR_STATE_FILE.exists():
        with open(IR_STATE_FILE) as f:
            return json.load(f)
    return {}

def save_ir_state(state):
    IR_STATE_FILE.parent.mkdir(exist_ok=True)
    with open(IR_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fetch_page(url: str, timeout: int = 8) -> dict:
    """Fetch a page and return text + links."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code != 200 or len(r.text) < 500:
            return {"error": f"HTTP {r.status_code}", "text": "", "links": []}

        text_lower = r.text.lower()

        links = []
        if HAS_BS4:
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a.get("href", "")
                anchor = a.get_text(strip=True)
                if href.startswith("/"):
                    base = "/".join(url.split("/")[:3])
                    href = base + href
                if anchor and href:
                    links.append({"text": anchor.lower(), "url": href})
        else:
            # Regex fallback
            for m in re.finditer(r'href=["\']([^"\']+)["\'][^>]*>([^<]{3,80})<', r.text, re.I):
                links.append({"url": m.group(1), "text": m.group(2).lower().strip()})

        return {
            "text": text_lower,
            "links": links,
            "raw_length": len(r.text),
            "content_hash": hashlib.md5(r.text.encode()).hexdigest()[:12]
        }
    except Exception as e:
        return {"error": str(e), "text": "", "links": []}


def fetch_sec_recent_8k(cik: str, days_back: int = 3) -> dict:
    """Fetch recent 8-K filings from SEC EDGAR for a CIK. Always works."""
    try:
        cik_padded = cik.strip().lstrip("0").zfill(10)
        url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
        r = requests.get(url, headers=SEC_HEADERS, timeout=12)
        if r.status_code != 200:
            return {"error": f"EDGAR {r.status_code}", "filings": []}

        data = r.json()
        recent = data.get("filings", {}).get("recent", {})
        forms   = recent.get("form", [])
        dates   = recent.get("filingDate", [])
        accnums = recent.get("accessionNumber", [])
        docs    = recent.get("primaryDocument", [])

        cutoff = (datetime.date.today() - datetime.timedelta(days=days_back)).isoformat()
        filings = []
        for form, date, acc, doc in zip(forms, dates, accnums, docs):
            if date < cutoff:
                break  # EDGAR returns newest first
            if form in ("8-K", "8-K/A", "6-K"):
                cik_num = cik.lstrip("0") or "0"
                filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{acc.replace('-','')}/{doc}"
                filings.append({
                    "form": form, "date": date, "accession": acc,
                    "url": filing_url,
                    "edgar_page": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_padded}&type=8-K&count=5"
                })

        return {"filings": filings, "company_name": data.get("name", "")}
    except Exception as e:
        return {"error": str(e), "filings": []}


def analyze_page(page: dict, ticker: str) -> list:
    """Extract signals from a fetched IR page."""
    signals = []
    today = datetime.date.today().isoformat()
    text  = page.get("text", "")
    links = page.get("links", [])

    found_topline   = [kw for kw in TOPLINE_KEYWORDS if kw in text]
    found_quiet     = [kw for kw in QUIET_PERIOD_KEYWORDS if kw in text]

    # Suppress false positive: empty event calendars with "no events to display" boilerplate
    no_events = any(phrase in text for phrase in [
        "no events to display", "no upcoming events", "no events found", "loading events"
    ])

    # Registration links near topline content
    # Require: the link text contains a specific action CTA ("register", "listen live", "join")
    # AND the link OR its nearby context contains topline keywords
    # Exclude generic archive/boilerplate mentions of "webcast"
    STRONG_REG_KEYWORDS = ["register", "listen live", "join webcast", "pre-register",
                           "register now", "register here", "click to register", "sign up"]
    reg_links = []
    if not no_events:
        reg_links = [
            lnk for lnk in links
            if any(rk in lnk["text"] for rk in STRONG_REG_KEYWORDS)
            and any(tk in text for tk in ["topline", "top-line", "phase 3", "data readout",
                                          "clinical results", "trial results", "study results"])
        ]

    if reg_links:
        signals.append({
            "ticker": ticker, "type": "WEBCAST_PREREGISTRATION_FOUND",
            "severity": "CRITICAL", "icon": "🔴",
            "detail": f"Topline webcast pre-registration link found on IR page",
            "registration_links": reg_links[:3],
            "implication": "Pre-reg open = announcement date is now CERTAIN. Update expiry immediately.",
            "detected_date": today
        })

    elif found_quiet:
        signals.append({
            "ticker": ticker, "type": "QUIET_PERIOD_DETECTED",
            "severity": "HIGH", "icon": "🟠",
            "detail": f"Quiet period language on {ticker} IR page: {found_quiet[0]}",
            "implication": "Quiet period = data readout imminent. Days away.",
            "detected_date": today
        })

    elif found_topline and not reg_links:
        # Page mentions topline stuff but no reg link yet — watch
        signals.append({
            "ticker": ticker, "type": "TOPLINE_EVENT_MENTIONED",
            "severity": "MODERATE", "icon": "👁",
            "detail": f"IR page mentions: {', '.join(found_topline[:3])}",
            "implication": "IR page has topline keywords but no pre-reg yet.",
            "detected_date": today
        })

    return signals


def analyze_sec_filings(filings_data: dict, ticker: str) -> list:
    """Check new 8-K filings for topline data signals."""
    signals = []
    today = datetime.date.today().isoformat()
    filings = filings_data.get("filings", [])

    TOPLINE_8K = [
        "topline", "top-line", "primary endpoint", "phase 3", "phase 2",
        "met primary", "missed", "statistically significant", "efficacy",
        "overall survival", "progression-free"
    ]

    for filing in filings:
        # Try to fetch and scan the actual 8-K document
        try:
            r = requests.get(filing["url"], headers=SEC_HEADERS, timeout=8)
            text = r.text.lower() if r.status_code == 200 else ""
        except:
            text = ""

        has_topline = any(kw in text for kw in TOPLINE_8K)
        if has_topline:
            signals.append({
                "ticker": ticker, "type": "TOPLINE_8K_VIA_CIK",
                "severity": "CRITICAL", "icon": "🔴",
                "detail": f"8-K filed {filing['date']} — topline data keywords detected",
                "filing_url": filing["url"],
                "implication": "TOPLINE DATA DROPPING — check immediately.",
                "detected_date": today
            })
        elif filing["form"] in ("8-K", "6-K"):
            # New 8-K but not obviously topline — flag as watch
            signals.append({
                "ticker": ticker, "type": "NEW_8K_VIA_CIK",
                "severity": "LOW", "icon": "📄",
                "detail": f"New {filing['form']} filed {filing['date']}",
                "filing_url": filing["url"],
                "implication": "Review for clinical data.",
                "detected_date": today
            })

    return signals


def run_ir_events_check():
    """
    Main IR events check.
    For each company in nct_registry:
      1. Try IR events page (ir_events_url)
      2. Always also check SEC EDGAR via CIK (bulletproof fallback)
      3. Flag page changes, pre-reg links, quiet periods
    """
    registry   = load_registry()
    ir_state   = load_ir_state()
    all_signals = []
    new_state  = {}
    today      = datetime.date.today().isoformat()

    _cli_print(f"\n{'='*70}")
    _cli_print(f"IR EVENTS WATCHER — {today}")
    _cli_print(f"Companies: {len(registry['watchlist'])}")
    _cli_print(f"{'='*70}")

    # Step 0: Auto-fix any broken IR URLs before checking
    try:
        # f-misc-03: replaced the legacy
        # ``sys.path.insert(BASE_DIR/'intelligence')`` +
        # ``from company_resolver import ...`` pattern with a
        # canonical absolute import. The legacy form mutated
        # ``sys.path`` for every caller of this function — it was a
        # holdover from when ``intelligence/`` was a sibling
        # directory rather than a package.
        from biotech_sniper.intelligence.company_resolver import (
            auto_resolve_missing_ir_urls,
        )
        auto_resolve_missing_ir_urls()
        # Reload registry after fixes
        registry = load_registry()
    except Exception as e:
        _cli_print(f"  Auto-resolver: {e}")

    for ticker, info in registry["watchlist"].items():
        # Skip monitor plays that are far out (>180 days)
        est = info.get("estimated_announcement", "")
        if "2027" in est or "2028" in est:
            _cli_print(f"  ⏭  {ticker}: far-dated ({est}) — skipping IR check")
            continue

        ir_url = info.get("ir_events_url", "")  # CORRECT KEY
        cik    = info.get("sec_cik", "")
        company = info.get("company", ticker)

        _cli_print(f"\n  {ticker} ({company})")
        signals = []

        # ── SOURCE 1: IR EVENTS PAGE ────────────────────────────────────
        if ir_url:
            page = fetch_page(ir_url)
            if "error" not in page:
                # Detect page changes
                prev_hash = ir_state.get(ticker, {}).get("content_hash", "")
                curr_hash = page.get("content_hash", "")
                changed   = prev_hash and prev_hash != curr_hash

                page_signals = analyze_page(page, ticker)
                signals.extend(page_signals)

                if changed and not page_signals:
                    signals.append({
                        "ticker": ticker, "type": "IR_PAGE_CHANGED",
                        "severity": "MODERATE", "icon": "👁",
                        "detail": f"IR events page changed since last check",
                        "implication": f"New event posted. Check: {ir_url}",
                        "detected_date": today
                    })

                new_state[ticker] = {
                    "ir_url": ir_url, "content_hash": curr_hash,
                    "last_checked": today,
                    "page_length": page.get("raw_length", 0)
                }
                status = "CHANGED" if changed else "unchanged"
                _cli_print(f"    IR page: {page.get('raw_length',0):,} chars | {status}")
            else:
                _cli_print(f"    IR page: ERROR — {page['error'][:60]}")
                # Mark for URL refresh
                registry["watchlist"][ticker]["needs_ir_url_refresh"] = True
        else:
            _cli_print(f"    IR page: no URL — will use SEC CIK only")

        # ── SOURCE 2: SEC EDGAR CIK (always run) ───────────────────────
        if cik:
            sec_data = fetch_sec_recent_8k(cik, days_back=2)
            if "error" not in sec_data:
                n = len(sec_data.get("filings", []))
                if n > 0:
                    _cli_print(f"    SEC EDGAR: {n} new 8-K(s) in last 48h")
                    sec_signals = analyze_sec_filings(sec_data, ticker)
                    # Don't double-count with SEC RSS monitor
                    for s in sec_signals:
                        if s["severity"] == "CRITICAL":
                            signals.append(s)
                else:
                    _cli_print(f"    SEC EDGAR: ✓ no new filings")
            else:
                _cli_print(f"    SEC EDGAR: {sec_data['error'][:50]}")
        else:
            _cli_print(f"    SEC CIK: not set — run company_resolver to fix")

        # Report
        if signals:
            for s in signals:
                _cli_print(f"    {s['icon']} [{s['severity']}] {s['type']}: {s['detail'][:80]}")
            all_signals.extend(signals)
        else:
            _cli_print(f"    ✓ No new signals")

    # Save state
    save_ir_state(new_state)

    # Save report
    report = {
        "run_date": today,
        "signals": all_signals,
        "critical": [s for s in all_signals if s["severity"] == "CRITICAL"],
        "high":     [s for s in all_signals if s["severity"] == "HIGH"],
        "tickers_checked": list(registry["watchlist"].keys()),
        "total_signals": len(all_signals)
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(report, f, indent=2)

    # Persist into the SQLite news_events table per VAL-M2-075.
    try:
        _persist_ir_signals_to_news_events(all_signals)
    except Exception as e:  # pragma: no cover - defensive
        _cli_print(f"  [ir_events] news_events persistence failed: {e}")

    _cli_print(f"\n{'='*70}")
    _cli_print(f"IR EVENTS SUMMARY: {len(all_signals)} signals")
    if report["critical"]:
        _cli_print(f"  🔴 CRITICAL: {len(report['critical'])} — ACTION REQUIRED")
        for s in report["critical"]:
            _cli_print(f"     {s['ticker']}: {s['detail']}")
    if report["high"]:
        _cli_print(f"  🟠 HIGH: {len(report['high'])}")
    if not all_signals:
        _cli_print(f"  ✓ All clear")
    _cli_print(f"{'='*70}\n")

    return report


def _persist_ir_signals_to_news_events(signals: list) -> None:
    """Mirror IR-events signals into the SQLite news_events table."""
    if not signals:
        return

    from biotech_sniper import db as _db
    from biotech_sniper.news_events import (
        NewsEvent,
        SOURCE_IR_EVENTS,
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
                source=SOURCE_IR_EVENTS,
                title=str(signal.get("detail") or signal.get("type") or "IR signal"),
                url=signal.get("filing_url") or signal.get("ir_url") or None,
                published_at=signal.get("detected_date") or None,
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
    run_ir_events_check()
