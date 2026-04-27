#!/usr/bin/env python3
"""
INTRADAY OPPORTUNITY SCANNER — FULL COVERAGE
Runs every hour during market hours (Mon-Fri 7:27 AM - 2:27 PM PT).

THREE JOBS every run:

JOB 1 — SCAN ALL SOURCES FOR NEW OPPORTUNITIES
  Biotech:  SEC 8-K RSS + individual CIK checks + IR page changes
  Contracts: USASpending.gov new awards + Federal Register
  AdCom:    FDA calendar RSS for newly announced meetings

JOB 2 — CHECK ACTIVE PLAYS FOR REMOVAL TRIGGERS
  PDUFA/award/vote date passed
  Topline 8-K filed (BREAKING)
  Announcement window overdue
  Trial suspended/cancelled

JOB 3 — CHECK FOR UPGRADES
  ESTIMATED → CERTAIN via webcast pre-reg
  New exact date confirmed on IR page or PR

EMAIL RULES: Only send if something actually changed.
"""

import json
import logging
import os
import re
import datetime
import time

try:
    # f-m4-08a: bare ``from new_opportunity_sniper import …`` used to
    # rely on the broken sys.path shim in master_unified_run; the
    # canonical path is the fully-qualified ``biotech_sniper.``
    # module.
    from biotech_sniper.new_opportunity_sniper import (
        scan_for_new_opportunities,
        format_new_opp_email,
    )
    _NEW_OPP_SNIPER_AVAILABLE = True
except Exception as _e:
    print(f'  [intraday] new_opp_sniper not available: {_e}')
    _NEW_OPP_SNIPER_AVAILABLE = False
import requests

from biotech_sniper import logging_setup
from biotech_sniper.paths import BASE_DIR as BASE

# f-m4-08a: module-level logger. Renamed to ``_logger`` (rather than
# ``log``) to avoid collision with ``run_intraday_scan``'s local
# ``log`` variable that holds the JSON dict loaded from
# ``state/intraday_log.json``.
_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# f-m3-18 — Paper-executor injection for JOB 4 rotation engine
# ---------------------------------------------------------------------------


def _paper_execute_enabled() -> bool:
    """Return ``True`` when JOB 4 should inject a real PaperExecutor.

    The gate is intentionally OPT-IN so unit tests + dry-run cron
    invocations stay hermetic by default. Production cron sets the
    flag in the systemd unit's ``Environment=`` block once the
    operator is happy with paper-trading wiring.

    Truthy values: ``1``, ``true``, ``yes`` (case-insensitive). Any
    other value (or unset) returns ``False``.
    """
    val = os.environ.get("BIOTECH_SNIPER_PAPER_EXECUTE", "0")
    return val.strip().lower() in ("1", "true", "yes", "on")


def _build_rotation_executor():
    """Return a :class:`PaperExecutor` for JOB 4, or ``None`` to dry-run.

    Construction is best-effort: when paper-execute is disabled, the
    Alpaca SDK is missing, credentials are absent, or the broker
    rejects the constructor (e.g. a misconfigured ``ALPACA_BASE_URL``),
    we fall through to ``None`` so the rotation engine still records
    its audit decisions instead of crashing the intraday cycle.

    Wiring contract for f-m3-18:

    * Default (``BIOTECH_SNIPER_PAPER_EXECUTE`` unset / ``0``) →
      ``None``. Rotation engine runs in dry-run; tests stay hermetic.
    * Flag enabled + creds present → real :class:`PaperExecutor`
      pointed at ``https://paper-api.alpaca.markets`` (the
      :class:`AlpacaClient` constructor enforces this).
    * Flag enabled + creds missing / broker error → ``None`` with a
      WARNING log line so operators can spot the misconfig.
    """
    if not _paper_execute_enabled():
        return None
    try:
        from biotech_sniper.alpaca_client import AlpacaClient
        from biotech_sniper.paper_executor import PaperExecutor

        client = AlpacaClient()
        return PaperExecutor(client)
    except Exception as exc:  # noqa: BLE001 — graceful degradation
        print(
            f"  [intraday] rotation_executor: disabled "
            f"({type(exc).__name__}: {exc})"
        )
        return None
ACTIVE_FILE   = BASE / "state/active_plays.json"
RESOLVED_FILE = BASE / "state/resolved_plays.json"
SEC_STATE     = BASE / "state/sec_8k_state.json"
INTRADAY_LOG  = BASE / "state/intraday_log.json"
REGISTRY_FILE = BASE / "intelligence/nct_registry.json"

HEADERS     = {"User-Agent": "Mozilla/5.0 (compatible; research purposes)"}
SEC_HEADERS = {"User-Agent": "BioCatalystBot research@mantisvc.com"}


def load_active():
    return json.load(open(ACTIVE_FILE)) if ACTIVE_FILE.exists() else {"active": {}, "monitor": {}}

def load_registry():
    return json.load(open(REGISTRY_FILE)) if REGISTRY_FILE.exists() else {"watchlist": {}}

def load_log():
    if INTRADAY_LOG.exists():
        return json.load(open(INTRADAY_LOG))
    return {"seen_urls": [], "seen_award_ids": [], "last_scan": None, "alerts_sent": []}

def save_log(log):
    with open(INTRADAY_LOG, "w") as f:
        json.dump(log, f, indent=2)

def load_sec_state():
    if SEC_STATE.exists():
        return json.load(open(SEC_STATE))
    return {"seen_filings": [], "last_checked": None}

def save_sec_state(state):
    with open(SEC_STATE, "w") as f:
        json.dump(state, f, indent=2)


# ══════════════════════════════════════════════════════
# JOB 1 — SCAN FOR NEW OPPORTUNITIES
# ══════════════════════════════════════════════════════

def scan_news_rss(seen_urls: set) -> list:
    """
    Check Endpoints News, STAT News, GlobeNewswire, PRNewswire for breaking topline signals.
    These break data 15-60 min before SEC 8-K. Run every hour.
    """
    alerts = []
    registry = load_registry()
    
    watchlist_terms = {}
    for ticker, info in registry.get("watchlist", {}).items():
        for term in [ticker.lower(), info.get("company","").lower()[:12], info.get("drug","").lower()[:10]]:
            if term and len(term) > 3:
                watchlist_terms[term] = ticker

    TOPLINE_KWS = [
        "topline", "top-line", "phase 3", "primary endpoint", "met primary",
        "missed", "failed to meet", "fda approved", "crl", "complete response",
        "statistically significant", "late-breaking", "plenary"
    ]

    NEWS_FEEDS = [
        ("Endpoints", "https://endpts.com/feed/"),
        ("STAT",      "https://www.statnews.com/feed/"),
        ("GNW",       "https://www.globenewswire.com/RssFeed/subjectcode/15-Pharmaceutical"),
        ("PRN",       "https://www.prnewswire.com/rss/news-releases-list.rss?category=pharma-biotech"),
    ]

    try:
        import feedparser
        for feed_name, feed_url in NEWS_FEEDS:
            try:
                r = requests.get(feed_url, headers=HEADERS, timeout=8)
                if r.status_code != 200:
                    continue
                feed = feedparser.parse(r.text)
                for entry in feed.entries[:20]:
                    url   = entry.get("link", "")
                    title = entry.get("title", "")
                    full  = f"{title} {entry.get('summary','')}".lower()
                    if url in seen_urls:
                        continue
                    if not any(kw in full for kw in TOPLINE_KWS):
                        continue
                    seen_urls.add(url)
                    matched = next((t for term, t in watchlist_terms.items() if term in full), None)
                    is_breaking = any(kw in full for kw in ["topline","top-line","met primary","missed","fda approved","crl"])
                    alerts.append({
                        "ticker": matched or "",
                        "type": "BREAKING_NEWS" if is_breaking else "NEWS_SIGNAL",
                        "severity": "CRITICAL" if (is_breaking and matched) else "HIGH" if is_breaking else "MODERATE",
                        "title": title[:120],
                        "url": url,
                        "feed": feed_name,
                        "is_topline": is_breaking,
                        "is_watchlist": bool(matched),
                    })
            except:
                continue
    except ImportError:
        pass

    # Persist alerts into the SQLite news_events table per VAL-M2-075.
    # Best-effort: failures must not block the legacy in-memory return
    # value, which other intraday code still relies on.
    try:
        _persist_intraday_alerts_to_news_events(alerts)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"  [intraday] news_events persistence failed: {exc}")

    return alerts


def _persist_intraday_alerts_to_news_events(alerts: list) -> None:
    """Mirror ``scan_news_rss`` alerts into the news_events SQLite table."""
    if not alerts:
        return

    from biotech_sniper import db as _db
    from biotech_sniper.news_events import (
        NewsEvent,
        SOURCE_INTRADAY_RSS,
        default_db_path,
        record_news_events,
    )

    events: list[NewsEvent] = []
    for alert in alerts:
        ticker = alert.get("ticker")
        if not ticker:
            continue
        events.append(
            NewsEvent(
                ticker=str(ticker),
                source=SOURCE_INTRADAY_RSS,
                title=str(alert.get("title") or "intraday news"),
                url=alert.get("url") or None,
                published_at=None,
                raw_payload=alert,
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


def scan_sec_rss(seen_urls: set) -> list:
    """
    Check SEC EDGAR RSS for 8-K filings from watchlist companies.
    Fast, no auth, updated in near-real-time during market hours.
    """
    alerts = []
    registry = load_registry()

    # Build company → ticker lookup
    company_to_ticker = {}
    for ticker, info in registry["watchlist"].items():
        name = info.get("company", "").lower()
        if name:
            company_to_ticker[name] = ticker
            # First word (e.g. "ideaya" from "ideaya biosciences")
            first = name.split()[0]
            if len(first) > 4:
                company_to_ticker[first] = ticker

    TOPLINE_KWS = [
        "topline", "top-line", "primary endpoint", "phase 3", "phase 2",
        "met primary", "missed", "statistically significant", "efficacy",
        "overall survival", "progression-free", "positive results", "failed to meet"
    ]

    try:
        import feedparser
        url = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=40&search_text=&output=atom"
        r = requests.get(url, headers=SEC_HEADERS, timeout=12)
        feed = feedparser.parse(r.text)

        for entry in feed.entries:
            link = entry.get("link", "")
            if link in seen_urls:
                continue

            title   = entry.get("title", "").lower()
            summary = entry.get("summary", "").lower()
            full    = f"{title} {summary}"

            matched_ticker = next(
                (t for n, t in company_to_ticker.items() if n and len(n) > 3 and n in full),
                None
            )
            if not matched_ticker:
                continue

            seen_urls.add(link)
            is_topline = any(kw in full for kw in TOPLINE_KWS)
            alerts.append({
                "ticker":     matched_ticker,
                "type":       "BREAKING_8K" if is_topline else "NEW_8K",
                "severity":   "CRITICAL" if is_topline else "MODERATE",
                "title":      entry.get("title", "")[:100],
                "url":        link,
                "is_topline": is_topline,
                "published":  entry.get("published", "")
            })
    except Exception as e:
        print(f"  SEC RSS error: {e}")

    return alerts


def scan_ir_pages_quick(active_plays: dict, sent_alert_keys: set) -> list:
    """
    Quick IR page check for CERTAIN-date confirmation signals.
    Only fires once per ticker per signal type — deduped via sent_alert_keys.

    Dedup key format: "ir:{TICKER}:{SIGNAL_TYPE}"
    e.g. "ir:VRDN:webcast_prereg_detected"

    Once an alert key is in sent_alert_keys it will NOT fire again UNLESS the
    page now shows an EXACT DATE (e.g. "April 22" in the registration context),
    in which case the key upgrades to "ir:VRDN:exact_date_YYYYMMDD" and fires once.
    """
    alerts = []
    registry = load_registry()

    for ticker, play in active_plays.items():
        if play.get("announcement_certainty") == "CERTAIN":
            continue  # Already confirmed, no need to watch

        info   = registry["watchlist"].get(ticker, {})
        ir_url = info.get("ir_events_url", "")
        if not ir_url:
            continue

        try:
            r = requests.get(ir_url, headers=HEADERS, timeout=6, allow_redirects=True)
            if r.status_code != 200:
                continue

            text_lower = r.text.lower()

            # Suppress false positive: empty event calendar boilerplate
            if any(p in text_lower for p in ["no events to display", "loading events", "no upcoming events"]):
                continue

            # Require a strong registration CTA (not just generic "webcast" in page boilerplate)
            STRONG_REG = ["register", "listen live", "join webcast", "pre-register",
                          "register now", "register here", "sign up"]
            has_reg     = any(kw in text_lower for kw in STRONG_REG)
            has_topline = any(kw in text_lower for kw in ["topline", "top-line", "phase 3 results",
                                                          "phase 2 results", "data readout",
                                                          "clinical results"])

            if not (has_reg and has_topline):
                continue

            # Try to extract an exact date from the registration context
            import re
            exact_date = None
            date_patterns = [
                r'(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},?\s*20\d{2}',
                r'\d{1,2}/\d{1,2}/20\d{2}',
                r'20\d{2}-\d{2}-\d{2}',
            ]
            for pat in date_patterns:
                m = re.search(pat, text_lower)
                if m:
                    exact_date = m.group(0).strip()
                    break

            # Build dedup key — exact date gets a unique key, so it fires once when date first appears
            if exact_date:
                date_slug = re.sub(r'[^a-z0-9]', '', exact_date)
                alert_key = f"ir:{ticker}:exact_date_{date_slug}"
                signal_type = "WEBCAST_DATE_CONFIRMED"
                detail = f"Webcast date confirmed on {ticker} IR page: {exact_date}"
            else:
                alert_key = f"ir:{ticker}:webcast_prereg_detected"
                signal_type = "WEBCAST_PREREGISTRATION_FOUND"
                detail = f"Webcast pre-registration on {ticker} IR page (no exact date yet)"

            # DEDUP CHECK — only fire if this exact key has never been sent
            if alert_key in sent_alert_keys:
                continue  # Already alerted on this, suppress

            sent_alert_keys.add(alert_key)
            alerts.append({
                "ticker":     ticker,
                "type":       signal_type,
                "severity":   "CRITICAL",
                "detail":     detail,
                "ir_url":     ir_url,
                "alert_key":  alert_key,
                "exact_date": exact_date,
            })
        except:
            continue

    return alerts


def scan_usaspending_intraday(seen_award_ids: set) -> list:
    """
    Quick USASpending check for new awards in last 24h.
    """
    alerts = []
    try:
        # f-m4-08a: replace the bare ``from sectors.contracts.sam_sniper``
        # / ``from sam_sniper`` imports + sys.path mutation with a
        # single fully-qualified import. The legacy bare import paths
        # never resolved in this layout and the function silently
        # ate every call.
        from biotech_sniper.sectors.contracts.sam_sniper import (
            load_ticker_map,
            match_award_to_ticker,
            score_award_signal,
            search_usaspending_by_keyword,
        )

        ticker_map = load_ticker_map()
        awards = search_usaspending_by_keyword(
            ["Rocket Lab", "Kratos", "AST SpaceMobile", "Intuitive Machines", "Mercury Systems"],
            days_back=1  # Last 24h only for intraday
        )
        for award in awards:
            award_id = award.get("Award ID", "")
            if not award_id or award_id in seen_award_ids:
                continue
            matches = match_award_to_ticker(award, ticker_map)
            for match in matches:
                signal = score_award_signal(award, match, "intraday")
                if signal["severity"] in ("CRITICAL", "HIGH"):
                    seen_award_ids.add(award_id)
                    alerts.append(signal)
    except Exception as e:
        print(f"  USASpending intraday: {e}")

    return alerts


def scan_fda_rss() -> list:
    """
    Check FDA AdCom RSS for any newly announced meetings.
    """
    alerts = []
    try:
        import feedparser
        url = "https://www.fda.gov/feeds/advisory-committee-meetings-coming-soon.rss"
        feed = feedparser.parse(url)
        cutoff = datetime.date.today() - datetime.timedelta(days=1)
        for entry in feed.entries[:20]:
            pub = entry.get("published_parsed")
            if pub and datetime.date(*pub[:3]) >= cutoff:
                title = entry.get("title", "")
                alerts.append({
                    "type": "NEW_ADCOM_ANNOUNCED", "severity": "HIGH",
                    "title": title[:150], "url": entry.get("link", ""),
                    "published": entry.get("published", "")
                })
    except Exception as e:
        print(f"  FDA RSS error: {e}")

    return alerts


# ══════════════════════════════════════════════════════
# JOB 2 — CHECK FOR REMOVALS
# ══════════════════════════════════════════════════════

def check_removals(active_plays: dict) -> list:
    """
    Scan active plays for removal triggers.
    Returns list of (ticker, reason, detail) tuples.
    """
    today     = datetime.date.today()
    removals  = []

    for ticker, play in active_plays.items():
        # 1. PDUFA / certain date passed
        pdufa = play.get("pdufa_date")
        if pdufa:
            try:
                if datetime.date.fromisoformat(pdufa) < today:
                    removals.append((ticker, "PDUFA_PASSED",
                                     f"PDUFA {pdufa} passed — FDA decision should be out"))
                    continue
            except:
                pass

        # 2. Announcement window overdue
        est = play.get("estimated_announcement", "").lower()
        if est:
            # Find latest month mentioned
            month_map = {
                "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
                "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
                "jan":1,"feb":2,"mar":3,"apr":4,"jun":6,"jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12
            }
            months = [v for k, v in month_map.items() if k in est]
            yr_m = re.search(r'(20\d{2})', est)
            year = int(yr_m.group(1)) if yr_m else today.year
            if months:
                latest_m = max(months)
                try:
                    deadline = datetime.date(year, latest_m, 28)
                    if today > deadline + datetime.timedelta(days=60):
                        removals.append((ticker, "WINDOW_EXPIRED",
                                         f"Est. announcement '{play.get('estimated_announcement')}' overdue by 60d+"))
                except:
                    pass

    return removals


# ══════════════════════════════════════════════════════
# JOB 3 — CHECK FOR UPGRADES
# ══════════════════════════════════════════════════════

def check_upgrades(active_plays: dict, new_sec_alerts: list) -> list:
    """
    Check if any ESTIMATED plays have become CERTAIN.
    Sources: IR page pre-reg (already in scan_ir_pages_quick) + SEC 8-K date announcements
    """
    upgrades = []
    for alert in new_sec_alerts:
        if alert.get("type") == "WEBCAST_PREREGISTRATION_FOUND":
            upgrades.append({
                "ticker": alert["ticker"],
                "type":   "UPGRADED_TO_CERTAIN",
                "detail": alert.get("detail", "")
            })
    return upgrades


# ══════════════════════════════════════════════════════
# EMAIL FORMATTER
# ══════════════════════════════════════════════════════

def _build_intraday_subject(breaking, new_plays, removals, upgrades, now):
    """Build a descriptive subject line for the intraday alert email."""
    parts = []
    if breaking:
        tickers = ", ".join(a.get("ticker", "?") for a in breaking[:2])
        parts.append(f"BREAKING: {tickers}")
    if removals:
        tickers = ", ".join(r[0] if isinstance(r, (list, tuple)) else r.get("ticker","?") for r in removals[:2])
        parts.append(f"REMOVED: {tickers}")
    if upgrades:
        tickers = ", ".join(u.get("ticker","?") for u in upgrades[:2])
        parts.append(f"CONFIRMED: {tickers}")
    if new_plays:
        parts.append(f"{len(new_plays)} new play(s)")
    label = " | ".join(parts) if parts else "update"
    return f"Alpha Sniper Intraday -- {now} | {label}"


def read_pending_intraday_email():
    """
    Read a pending intraday email that was saved before alerts were marked seen.
    Returns the pending dict (with email_body, subject, etc.) or None.
    Used by the cron agent to recover the email body without re-running the scan.
    """
    pending_path = BASE / "state/pending_intraday_email.json"
    if pending_path.exists():
        import json as _j
        data = _j.load(open(pending_path))
        if data.get("should_send"):
            return data
    return None


def clear_pending_intraday_email():
    """Mark the pending email as sent so it is not re-sent on next run."""
    pending_path = BASE / "state/pending_intraday_email.json"
    if pending_path.exists():
        import json as _j
        data = _j.load(open(pending_path))
        data["should_send"] = False
        _j.dump(data, open(pending_path, "w"), indent=2)


def format_intraday_email(breaking: list, new_plays: list, removals: list,
                           upgrades: list, adcom_alerts: list, scan_time: str) -> str | None:
    """Format the intraday alert email. Returns None if nothing to send."""
    if not breaking and not new_plays and not removals and not upgrades and not adcom_alerts:
        return None

    lines = []
    lines.append(f"ALPHA SNIPER — INTRADAY ALERT")
    lines.append(f"Scan: {scan_time}")
    lines.append("")

    # Breaking data first — always most urgent
    if breaking:
        lines.append("━" * 50)
        lines.append("BREAKING -- DATA FILING DETECTED")
        lines.append("━" * 50)
        for a in breaking:
            lines.append(f"  {a['ticker']}: {a['title'][:80]}")
            lines.append(f"  SEC: {a['url']}")
            lines.append(f"  → CHECK IMMEDIATELY. If position is open, consider exit before close.")
        lines.append("")

    if upgrades:
        lines.append("━" * 50)
        lines.append("DATE CONFIRMED")
        lines.append("━" * 50)
        for u in upgrades:
            lines.append(f"  {u['ticker']}: {u.get('detail', 'ESTIMATED → CERTAIN')}")
            lines.append(f"  → Check option expiry — may need to roll to earlier date.")
        lines.append("")

    if new_plays:
        lines.append("━" * 50)
        lines.append(f"NEW PLAYS ({len(new_plays)})")
        lines.append("━" * 50)
        for p in new_plays:
            ticker = p.get("ticker", "?")
            sector = p.get("sector", "?")
            sev    = p.get("severity", "")
            lines.append(f"  {ticker} [{sector}]: {p.get('title', p.get('detail',''))[:80]}")
            lines.append(f"  → Full scoring + card in tomorrow 6 AM report.")
        lines.append("")

    if adcom_alerts:
        lines.append("━" * 50)
        lines.append(f"NEW FDA ADCOM ANNOUNCED ({len(adcom_alerts)})")
        lines.append("━" * 50)
        for a in adcom_alerts:
            lines.append(f"  {a['title'][:100]}")
            lines.append(f"  → Run scoring now. Full play card in 6 AM report.")
        lines.append("")

    if removals:
        lines.append("━" * 50)
        lines.append(f"PLAYS REMOVED ({len(removals)})")
        lines.append("━" * 50)
        for ticker, reason, detail in removals:
            lines.append(f"  {ticker}: {detail}")
            lines.append(f"  → Close any open {ticker} positions immediately.")
        lines.append("")

    lines.append("─" * 50)
    lines.append(f"Next scan: 1 hour. Full report: Tomorrow 6:00 AM PT.")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════

def run_intraday_scan():
    """Main entry point — runs all 3 jobs, emails if anything changed."""
    # f-m4-08a: configure structured JSON logging for the intraday
    # cycle so log lines land in ``/var/log/alpha_sniper/intraday.log``
    # (or the env-overridden destination). Idempotent on subsequent
    # calls within the same process.
    logging_setup.configure(log_name="intraday")
    started_at = time.monotonic()

    now  = datetime.datetime.now().strftime("%Y-%m-%d %H:%M PT")
    _logger.info("intraday_start", extra={"event": "intraday_start", "ts": now})
    data = load_active()
    log  = load_log()

    active         = data.get("active", {})
    seen_urls      = set(log.get("seen_urls", []))
    seen_award_ids = set(log.get("seen_award_ids", []))
    # sent_alert_keys: persistent dedup set for IR signals, stored in log
    # Format: "ir:TICKER:signal_slug" — never cleared, accumulates across all runs
    sent_alert_keys = set(
        a["key"] if isinstance(a, dict) else a
        for a in log.get("alerts_sent", [])
    )

    print(f"\n{'='*60}")
    print(f"INTRADAY SCAN — {now}")
    print(f"Active plays: {len(active)}")
    print(f"{'='*60}")

    # ── JOB 1: SCAN ──────────────────────────────────────────────────
    print("\nJOB 1 — SCANNING ALL SOURCES")

    sec_alerts      = scan_sec_rss(seen_urls)
    news_alerts     = scan_news_rss(seen_urls)                          # Endpoints/STAT/GNW/PRN
    ir_alerts       = scan_ir_pages_quick(active, sent_alert_keys)      # deduped via sent_alert_keys
    contract_alerts = scan_usaspending_intraday(seen_award_ids)
    adcom_alerts    = scan_fda_rss()

    # ── NEW OPPORTUNITY DISCOVERY (real-time) ─────────────────────────────
    new_opportunities = []
    if _NEW_OPP_SNIPER_AVAILABLE:
        try:
            new_opportunities = scan_for_new_opportunities(seen_urls)
            if new_opportunities:
                print(f"  *** NEW OPPORTUNITIES FOUND: {len(new_opportunities)} ***")
                for opp in new_opportunities:
                    print(f"      {opp['ticker']} {opp['multiple']}x P={opp['p_success']}% | {opp['catalyst_date']}")
        except Exception as _ne:
            print(f"  [new_opp] Error: {_ne}")
            import traceback; traceback.print_exc()

    all_new_alerts = sec_alerts + news_alerts + ir_alerts + contract_alerts

    breaking  = [a for a in all_new_alerts if a.get("severity") == "CRITICAL" and ("8K" in a.get("type","") or "NEWS" in a.get("type",""))]
    new_plays = [a for a in all_new_alerts if a.get("severity") in ("CRITICAL","HIGH") and "8K" not in a.get("type","") and "WEBCAST" not in a.get("type","") and "NEWS" not in a.get("type","")]

    print(f"  SEC RSS: {len(sec_alerts)} | IR pages: {len(ir_alerts)} | Contracts: {len(contract_alerts)} | AdCom: {len(adcom_alerts)}")
    print(f"  Breaking: {len(breaking)} | New plays: {len(new_plays)}")

    # ── JOB 2: REMOVALS ──────────────────────────────────────────────
    print("\nJOB 2 — CHECKING REMOVALS")
    raw_removals = check_removals(active)

    # Dedup: only fire each removal alert ONCE via alerts_sent
    # Also actually remove from active_plays.json so it doesn't re-trigger every hour
    removals = []
    for ticker, reason, detail in raw_removals:
        alert_key = f"removal:{ticker}:{reason}"
        if alert_key not in existing_sent:
            removals.append((ticker, reason, detail))
            existing_sent[alert_key] = {"key": alert_key, "ts": now}
            print(f"  REMOVED {ticker}: {detail}")

            # Actually remove from active_plays.json → resolved_plays.json
            try:
                _plays = load_active_plays()
                _play  = _plays.get("active", {}).pop(ticker, {})
                if _play:
                    _play["resolved_date"]   = now
                    _play["resolved_reason"] = reason
                    _play["resolved_detail"] = detail
                    _resolved = json.load(open(RESOLVED_FILE)) if RESOLVED_FILE.exists() else {}
                    _resolved[ticker] = _play
                    with open(RESOLVED_FILE, "w") as _f:
                        json.dump(_resolved, _f, indent=2)
                    with open(ACTIVE_FILE, "w") as _f:
                        json.dump(_plays, _f, indent=2)
                    print(f"    → Moved {ticker} to resolved_plays.json")
            except Exception as _re:
                print(f"    → Could not update plays file: {_re}")
        else:
            print(f"  SKIP {ticker}: already alerted for {reason}")

    if not raw_removals:
        print("  ✓ No removals")

    # ── JOB 3: UPGRADES ──────────────────────────────────────────────
    print("\nJOB 3 — CHECKING UPGRADES")
    upgrades = check_upgrades(active, ir_alerts)
    if upgrades:
        for u in upgrades:
            print(f"  CONFIRMED {u['ticker']}: {u.get('detail','')}")
    else:
        print("  ✓ No upgrades")

    # ── JOB 4: ROTATION (f-m3-10) ──────────────────────────────────────
    # Best-effort rotation evaluation. Failures here MUST NOT break the
    # rest of the intraday cycle (the rotation engine is gated by paper
    # credentials + scoring_cache freshness; in dev or without an
    # executor it short-circuits to a dry-run summary).
    #
    # f-m3-18: gate the executor injection behind LIVE_MODE and a
    # ``BIOTECH_SNIPER_PAPER_EXECUTE`` flag so the dev / test suite
    # stays hermetic. When the gate is closed (the default) we still
    # call evaluate_rotation but pass ``executor=None`` so the engine
    # records audit decisions without contacting the broker. When the
    # gate is open we instantiate :class:`PaperExecutor` once per
    # intraday cycle so the same-run sell+buy execution path actually
    # fires through a real Alpaca paper client.
    print("\nJOB 4 — ROTATION CHECK")
    try:
        from biotech_sniper.rotation_engine import evaluate_rotation
        rotation_executor = _build_rotation_executor()
        rotation_result = evaluate_rotation(executor=rotation_executor)
        print(
            f"  active={rotation_result.get('active_count')} / "
            f"cap={rotation_result.get('capacity')} | "
            f"rotations={len(rotation_result.get('decisions', []))} | "
            f"skips={len(rotation_result.get('skips', []))} | "
            f"executor={'paper' if rotation_executor is not None else 'dry-run'}"
        )
    except Exception as _re:  # pragma: no cover - defensive
        print(f"  rotation_engine: skipped ({type(_re).__name__}: {_re})")
        rotation_result = {"decisions": [], "skips": []}

    # ── JOB 5: IV CRUSH EXIT (f-m3-15) ─────────────────────────────────
    # Best-effort IV-crush exit job. The 50% sell on a position's
    # catalyst-day open is normally driven by the systemd timer that
    # invokes ``python -m biotech_sniper.iv_crush_exit_rules``
    # directly; the intraday hook here is a defence-in-depth path
    # that runs the same logic on every hourly cycle so a missed
    # systemd unit never causes a missed exit. Failures (missing
    # Alpaca creds in dev, db not yet migrated, etc.) MUST NOT break
    # the rest of the cycle — the helper itself catches every
    # exception and returns a populated ``errors`` field.
    print("\nJOB 5 — IV CRUSH EXIT")
    try:
        from biotech_sniper.iv_crush_exit_rules import (
            run_intraday_iv_crush_exit_job,
        )
        iv_crush_result = run_intraday_iv_crush_exit_job()
        print(
            f"  considered={iv_crush_result.get('considered')} | "
            f"exited={iv_crush_result.get('exited')} | "
            f"errors={iv_crush_result.get('errors')}"
        )
    except Exception as _ie:  # pragma: no cover - defensive
        print(
            f"  iv_crush_exit: skipped ({type(_ie).__name__}: {_ie})"
        )
        iv_crush_result = {
            "date": now.split()[0] if isinstance(now, str) else "",
            "considered": 0,
            "exited": 0,
            "errors": 1,
        }

    # ── JOB 6: TELEMETRY POLL (f-m3-19) ─────────────────────────────────
    # Best-effort execution telemetry poll. Walks every open
    # ``paper_orders`` row created today and records any broker-side
    # state change to ``execution_events`` / ``execution_fills``. The
    # production CLI (``python -m
    # biotech_sniper.execution_subscriber --poll-once --date <today>``)
    # is the canonical entry point; we invoke it in-process so an
    # intraday tick keeps slippage / time-to-fill telemetry fresh
    # between watchdog runs. Failures (missing Alpaca creds, DB not
    # yet migrated, broker outage) MUST NOT break the rest of the
    # cycle — the helper logs and returns a non-zero exit code which
    # we surface but do not raise on.
    print("\nJOB 6 — TELEMETRY POLL")
    try:
        from biotech_sniper.execution_subscriber import (
            main as _execution_subscriber_main,
        )
        import datetime as _dt
        _today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        _exit_code = _execution_subscriber_main(
            ["--poll-once", "--date", _today]
        )
        print(
            f"  date={_today} | exit_code={_exit_code} | "
            f"status={'ok' if _exit_code == 0 else 'errored'}"
        )
        telemetry_result = {"date": _today, "exit_code": int(_exit_code)}
    except Exception as _te:  # pragma: no cover - defensive
        print(
            f"  telemetry_poll: skipped ({type(_te).__name__}: {_te})"
        )
        telemetry_result = {
            "date": now.split()[0] if isinstance(now, str) else "",
            "exit_code": 1,
        }

    # ── EMAIL ─────────────────────────────────────────────────────────
    # New opportunities get their OWN immediate email (higher priority)
    if new_opportunities:
        new_opp_body = format_new_opp_email(new_opportunities, now)
        # Send new opp email immediately
        try:
            import sys; sys.path.insert(0, '.')
            from list_external_tools import list_tools
        except Exception:
            pass
        # Store for caller to send
        result_new_opps = new_opportunities
    else:
        result_new_opps = []

    email_body = format_intraday_email(
        breaking, new_plays, removals, upgrades, adcom_alerts, now
    )

    # ── SAVE STATE ────────────────────────────────────────────────────
    log["seen_urls"]      = list(seen_urls)[-1000:]
    log["seen_award_ids"] = list(seen_award_ids)[-2000:]
    log["last_scan"]      = now

    # Persist alert keys — write back as list of dicts with key + timestamp
    existing_sent = {a["key"]: a for a in log.get("alerts_sent", []) if isinstance(a, dict)}
    for key in sent_alert_keys:
        if key not in existing_sent:
            existing_sent[key] = {"key": key, "ts": now}
    log["alerts_sent"] = list(existing_sent.values())[-500:]  # keep last 500, never grows unbounded

    save_log(log)

    new_opps = result_new_opps if 'result_new_opps' in dir() else []
    should_email = email_body is not None or bool(new_opps)

    # f-m4-08a: structured ``intraday_done`` log line replaces the
    # legacy ad-hoc print summary. Carries cycle-level counts so
    # operators can grep the JSON log for SLO tracking.
    elapsed = time.monotonic() - started_at
    _logger.info(
        "intraday_done",
        extra={
            "event": "intraday_done",
            "duration_sec": elapsed,
            "scan_time": now,
            "breaking": len(breaking),
            "new_plays": len(new_plays),
            "removals": len(removals),
            "upgrades": len(upgrades),
            "adcom_alerts": len(adcom_alerts),
            "new_opportunities": len(new_opps),
            "should_email": bool(should_email),
        },
    )

    return {
        "breaking":          breaking,
        "new_plays":         new_plays,
        "removals":          removals,
        "upgrades":          upgrades,
        "adcom_alerts":      adcom_alerts,
        "new_opportunities": new_opps,
        "should_email":      should_email,
        "email_body":        email_body,
        "scan_time":         now,
    }


if __name__ == "__main__":
    result = run_intraday_scan()
    print(f"\nShould email: {result['should_email']}")
    if result["email_body"]:
        print("\n" + result["email_body"])
