#!/usr/bin/env python3
"""
UNIVERSAL NEWS & IR WATCHER — 500+ BIOTECH TICKERS
====================================================
Monitors SEC EDGAR, RSS news feeds, and catalyst sites for signals
across the entire tracked biotech universe.

Designed for hourly intraday cron execution.

Output:  <BASE_DIR>/intelligence/universal_news_report.json
Dedup:   <BASE_DIR>/state/seen_8k_accessions.json

Signal tiers
  TIER 1 (immediate email)  — topline data, PDUFA dates, FDA approval/rejection
  TIER 2 (daily digest)     — partnerships, interim data, milestone payments
"""

import json
import re
import time
import datetime
import hashlib
import logging
from pathlib import Path
from typing import Optional

import requests
import feedparser

# ── Paths ────────────────────────────────────────────────────────────────────
from biotech_sniper.paths import BASE_DIR, STATE_DIR  # noqa: E402
INTEL_DIR       = BASE_DIR / "intelligence"
SEEN_ACCESSIONS = STATE_DIR / "seen_8k_accessions.json"
NAME_TO_TICKER  = STATE_DIR / "sec_name_to_ticker.json"
OUTPUT_FILE     = INTEL_DIR / "universal_news_report.json"

# ── HTTP headers ─────────────────────────────────────────────────────────────
SEC_HEADERS  = {"User-Agent": "BioCatalystBot research@mantisvc.com"}
NEWS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}

# ── Signal keywords ───────────────────────────────────────────────────────────
TIER_1_SIGNALS = [
    "topline", "top-line", "primary endpoint", "phase 3 results",
    "phase iii results", "pivotal trial", "nda submission", "bla submission",
    "pdufa", "fda approval", "fda approved", "complete response letter",
    "breakthrough therapy", "fast track", "priority review",
    "met its primary", "met primary endpoint", "missed primary",
    "did not meet", "statistically significant", "statistically superior",
    "overall survival", "progression-free survival", "objective response rate",
    "fda action date", "fda decision", "approvable",
]

TIER_2_SIGNALS = [
    "partnership", "collaboration", "license agreement", "milestone",
    "interim data", "phase 2 results", "proof of concept",
    "data readout", "clinical data", "trial results", "efficacy data",
    "safety data", "phase 1 results", "phase i results",
    "accelerated approval", "orphan drug", "rare pediatric",
    "acquisition", "merger", "buyout", "tender offer",
]

# Medical conferences — presence near a date = catalyst signal
CONFERENCE_NAMES = [
    "asco", "ash", "aacr", "aha", "acc", "esmo", "ema", "easl",
    "eha", "croi", "idc", "ssiem", "siope", "sio", "eurohiv",
    "chest", "ers", "ats", "ada", "endo", "aasld", "ueg week",
    "ean", "ectrims", "aan", "sns", "sgna",
]

# Routine 8-K items (lower signal)
ROUTINE_8K_ITEMS = [
    "item 5.02",  # director departure/election
    "item 5.03",  # amendments to articles
    "item 9.01",  # financial statements
    "item 2.02",  # results of operations (earnings)
    "item 8.01 other events",  # generic — still check content
]

# RSS feeds to scan.
#
# f-m4-09: ``feeds.reuters.com/reuters/healthNews`` is dead — the
# Reuters health RSS endpoint started returning ``NameResolutionError``
# / ``ServerNotFound`` from the VPS once Reuters retired its public
# RSS infrastructure mid-2024 (the Reuters site no longer publishes
# section-level RSS at all). The dead entry generated repeated WARNING
# logs from ``run_hourly_news_scan`` on every intraday cycle without
# yielding any rows. We dropped it; the remaining 5 feeds satisfy the
# mission's "≥ 4 feeds" guardrail.
RSS_FEEDS = [
    {
        "name": "STAT News",
        "url": "https://feeds.feedburner.com/statnews/rss",
    },
    {
        "name": "Endpoints News",
        "url": "https://endpts.com/feed/",
    },
    {
        "name": "Fierce Biotech",
        "url": "https://www.fiercebiotech.com/rss/xml",
    },
    {
        "name": "BioPharma Dive",
        "url": "https://www.biopharmadive.com/feeds/news/",
    },
    {
        "name": "MedPage Today",
        "url": "https://www.medpagetoday.com/rss/headlines.xml",
    },
]

# ── Logging ───────────────────────────────────────────────────────────────────
# f-m4-02: log configuration is owned exclusively by
# ``biotech_sniper.logging_setup``; this module no longer calls
# ``logging.basicConfig`` directly.
from biotech_sniper import logging_setup  # noqa: F401 — installs JSON formatter on import

log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# STATE HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _load_seen_accessions() -> set:
    """Load the dedup set of already-processed 8-K accession numbers."""
    if SEEN_ACCESSIONS.exists():
        try:
            with open(SEEN_ACCESSIONS) as f:
                data = json.load(f)
            return set(data.get("accessions", []))
        except Exception:
            return set()
    return set()


def _save_seen_accessions(seen: set) -> None:
    """Persist dedup set, keeping only the most recent 5 000 entries."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Trim to last 5 000 to prevent unbounded growth
    trimmed = sorted(seen)[-5000:]
    with open(SEEN_ACCESSIONS, "w") as f:
        json.dump({"accessions": trimmed, "updated": datetime.datetime.now(datetime.timezone.utc).isoformat()}, f, indent=2)


# ════════════════════════════════════════════════════════════════════════════
# CIK LOOKUP  — builds {ticker: cik_str} from SEC company_tickers.json
# ════════════════════════════════════════════════════════════════════════════

_CIK_CACHE: dict = {}  # module-level cache so we only fetch once per process


def _build_cik_map(force: bool = False) -> dict:
    """
    Return {ticker (upper): zero-padded CIK string} for all SEC-registered companies.

    First tries the local sec_name_to_ticker.json (inverted to ticker→CIK).
    Falls back to fetching https://www.sec.gov/files/company_tickers.json.
    """
    global _CIK_CACHE
    if _CIK_CACHE and not force:
        return _CIK_CACHE

    ticker_to_cik: dict = {}

    # ── Try live SEC source (most complete) ──────────────────────────────
    try:
        r = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=SEC_HEADERS,
            timeout=20,
        )
        if r.status_code == 200:
            data = r.json()
            for _, entry in data.items():
                tkr = str(entry.get("ticker", "")).upper().strip()
                cik = str(entry.get("cik_str", "")).zfill(10)
                if tkr and cik:
                    ticker_to_cik[tkr] = cik
            log.info(f"CIK map built from SEC: {len(ticker_to_cik):,} tickers")
    except Exception as e:
        log.warning(f"Could not fetch company_tickers.json: {e}")

    # ── Also load local name-to-ticker for reverse lookup augmentation ───
    if NAME_TO_TICKER.exists():
        try:
            with open(NAME_TO_TICKER) as f:
                name_to_tkr = json.load(f)
            # name_to_ticker is {company_name_lower: TICKER}
            # We can't get CIK from this directly, but we can confirm tickers
            log.info(f"Local name-to-ticker loaded: {len(name_to_tkr):,} entries")
        except Exception:
            pass

    _CIK_CACHE = ticker_to_cik
    return ticker_to_cik


def _build_cik_to_ticker(tickers: list) -> dict:
    """
    Return {cik_str: ticker} for the supplied ticker list.
    Used when filtering SEC RSS feed entries.
    """
    cik_map = _build_cik_map()
    result: dict = {}
    ticker_set = {t.upper() for t in tickers}
    for tkr in ticker_set:
        cik = cik_map.get(tkr)
        if cik:
            result[cik] = tkr
            # Also store without leading zeros for matching flexibility
            result[cik.lstrip("0") or "0"] = tkr
    return result


# ════════════════════════════════════════════════════════════════════════════
# SIGNAL SCORING
# ════════════════════════════════════════════════════════════════════════════

def _score_text(text: str) -> tuple[bool, str, str]:
    """
    Returns (is_high_signal, signal_tier, matched_keyword).
    Checks TIER_1 first, then TIER_2.
    """
    lower = text.lower()
    for kw in TIER_1_SIGNALS:
        if kw in lower:
            return True, "TIER_1", kw
    for kw in TIER_2_SIGNALS:
        if kw in lower:
            return False, "TIER_2", kw
    return False, "NONE", ""


def _extract_summary(text: str, max_chars: int = 400) -> str:
    """Extract a clean plain-text summary from HTML or raw text."""
    # Strip HTML tags
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:max_chars]


# ════════════════════════════════════════════════════════════════════════════
# FUNCTION 1: scan_new_8ks
# ════════════════════════════════════════════════════════════════════════════

def scan_new_8ks(tickers: list, since_hours: int = 24) -> list:
    """
    Scan SEC EDGAR for new 8-K filings from any ticker in list.

    Strategy:
      1. Pull the SEC EDGAR 8-K RSS feed (most recent 100 entries).
      2. Match each entry's CIK to the supplied ticker list.
      3. For matched filings not yet seen, fetch the filing index and
         scan the document text for high-signal keywords.
      4. Also run the EDGAR full-text search API for TIER_1 keywords
         restricted to the last `since_hours`.

    Returns list of:
      {ticker, company, filed_date, form_url, is_high_signal,
       signal_tier, signal_type, matched_keyword, summary, accession}
    """
    seen_accessions = _load_seen_accessions()
    cik_to_ticker   = _build_cik_to_ticker(tickers)
    ticker_set      = {t.upper() for t in tickers}

    results: list = []
    new_accessions: set = set()

    cutoff_dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=since_hours)
    cutoff_date = cutoff_dt.strftime("%Y-%m-%d")

    # ── Step 1: Scan SEC 8-K RSS feed ────────────────────────────────────
    log.info(f"Fetching SEC EDGAR 8-K RSS (last 100)...")
    rss_url = (
        "https://www.sec.gov/cgi-bin/browse-edgar"
        "?action=getcurrent&type=8-K&dateb=&owner=include"
        "&count=100&search_text=&output=atom"
    )
    try:
        r = requests.get(rss_url, headers=SEC_HEADERS, timeout=20)
        feed = feedparser.parse(r.text)

        for entry in feed.entries:
            raw_title  = entry.get("title", "")
            link       = entry.get("link", "")
            published  = entry.get("published", "")
            summary    = entry.get("summary", "")

            # Extract CIK from the EDGAR filing URL
            # Typical link: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001234567&type=8-K&...
            cik_match = re.search(r"CIK=(\d+)", link, re.I)
            if not cik_match:
                # Alternative: parse from entry id
                cik_match = re.search(r"/(\d{10})/", link)
            
            cik_raw = cik_match.group(1) if cik_match else ""
            cik_padded = cik_raw.zfill(10) if cik_raw else ""

            # Match CIK → ticker
            matched_ticker = (
                cik_to_ticker.get(cik_padded)
                or cik_to_ticker.get(cik_raw.lstrip("0") or "0")
            )

            # Also try company-name match against ticker set (fallback)
            if not matched_ticker:
                title_upper = raw_title.upper()
                for tkr in ticker_set:
                    # Look for "TICKER - 8-K" or "TICKER:" patterns
                    if re.search(r'\b' + re.escape(tkr) + r'\b', title_upper):
                        matched_ticker = tkr
                        break

            if not matched_ticker:
                continue

            # Extract accession number from the EDGAR filing index link
            acc_match = re.search(r"(\d{10}-\d{2}-\d{6})", link)
            accession = acc_match.group(1) if acc_match else hashlib.md5(link.encode()).hexdigest()[:18]

            if accession in seen_accessions:
                continue  # already processed

            # Fetch the filing index to get the actual document URL
            doc_text  = ""
            form_url  = link
            company_name = raw_title.split(" - 8")[0].strip() if " - 8" in raw_title else raw_title

            filing_index_url = _resolve_filing_index(cik_raw, accession)
            if filing_index_url:
                doc_url, doc_text = _fetch_8k_document(filing_index_url, cik_raw)
                if doc_url:
                    form_url = doc_url

            # Score
            combined_text = f"{raw_title} {summary} {doc_text}"
            is_high, tier, keyword = _score_text(combined_text)

            entry_result = {
                "ticker":          matched_ticker,
                "company":         company_name,
                "filed_date":      published[:10] if published else cutoff_date,
                "form_url":        form_url,
                "is_high_signal":  is_high,
                "signal_tier":     tier,
                "signal_type":     keyword if keyword else "ROUTINE",
                "matched_keyword": keyword,
                "summary":         _extract_summary(f"{raw_title}. {summary}"),
                "accession":       accession,
                "source":          "SEC_RSS",
            }
            results.append(entry_result)
            new_accessions.add(accession)

        log.info(f"SEC RSS: {len(results)} new 8-K(s) for tracked tickers")

    except Exception as e:
        log.error(f"SEC RSS scan failed: {e}")

    # ── Step 2: EDGAR Full-Text Search for TIER_1 keywords ──────────────
    # Use EFTS to find 8-Ks mentioning high-signal phrases from last N hours
    tier1_scan_results = _efts_tier1_scan(ticker_set, cutoff_date, seen_accessions)
    for item in tier1_scan_results:
        if item["accession"] not in seen_accessions and item["accession"] not in new_accessions:
            results.append(item)
            new_accessions.add(item["accession"])

    # ── Step 3: Per-ticker CIK-based check for important tickers ─────────
    # For tickers not caught by RSS (feed only returns 100), check EDGAR directly
    # Limit to tickers that haven't appeared in RSS results
    found_tickers = {r["ticker"] for r in results}
    missing_tickers = [t for t in tickers if t.upper() not in found_tickers]

    if missing_tickers and len(missing_tickers) <= 50:
        # Only do CIK-based check for smaller lists (expensive per-ticker)
        cik_map = _build_cik_map()
        for ticker in missing_tickers[:50]:
            cik = cik_map.get(ticker.upper(), "")
            if not cik:
                continue
            cik_results = _fetch_cik_8ks(ticker, cik, cutoff_date, seen_accessions)
            for item in cik_results:
                if item["accession"] not in new_accessions:
                    results.append(item)
                    new_accessions.add(item["accession"])
            time.sleep(0.1)  # Be polite to EDGAR

    # ── Persist dedup state ───────────────────────────────────────────────
    seen_accessions.update(new_accessions)
    _save_seen_accessions(seen_accessions)

    log.info(f"scan_new_8ks: {len(results)} total new filings found")
    return results


def _resolve_filing_index(cik: str, accession: str) -> str:
    """Build the EDGAR filing index URL from CIK and accession number."""
    if not cik or not accession:
        return ""
    cik_num = cik.lstrip("0") or "0"
    acc_nodash = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{acc_nodash}/{accession}-index.htm"


def _fetch_8k_document(index_url: str, cik: str, timeout: int = 10) -> tuple[str, str]:
    """
    Fetch the 8-K filing index page and return (primary_doc_url, document_text).
    Returns ("", "") on failure.
    """
    try:
        r = requests.get(index_url, headers=SEC_HEADERS, timeout=timeout)
        if r.status_code != 200:
            return "", ""

        # Find the primary document link (usually .htm)
        doc_match = re.search(
            r'href="([^"]+\.htm)"[^>]*>\s*(?:8-K|Complete submission)',
            r.text, re.I
        )
        if not doc_match:
            # Fallback: first .htm link that isn't the index itself
            doc_match = re.search(r'href="(/Archives/edgar/data/[^"]+\.htm)"', r.text, re.I)

        if not doc_match:
            return index_url, _extract_summary(r.text, 300)

        doc_path = doc_match.group(1)
        if doc_path.startswith("/"):
            doc_url = "https://www.sec.gov" + doc_path
        elif doc_path.startswith("http"):
            doc_url = doc_path
        else:
            cik_num = cik.lstrip("0") or "0"
            doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{doc_path}"

        # Fetch document text (first 8 KB is enough for signal keywords)
        dr = requests.get(doc_url, headers=SEC_HEADERS, timeout=timeout)
        if dr.status_code == 200:
            return doc_url, dr.text[:8000]
        return doc_url, ""

    except Exception:
        return "", ""


def _efts_tier1_scan(ticker_set: set, start_date: str, seen: set) -> list:
    """
    Use SEC EDGAR full-text search (EFTS) to find TIER_1 keyword mentions
    in 8-K filings since start_date.

    Only runs a handful of high-value keyword queries to avoid over-requesting.
    """
    results = []
    # Top 5 TIER_1 keywords worth scanning broadly
    EFTS_KEYWORDS = ["pdufa", "topline", "fda approved", "nda submission", "pivotal trial"]
    cik_map = _build_cik_map()
    # Invert: cik → ticker for our universe
    universe_cik_to_ticker = {v: k for k, v in cik_map.items() if k in ticker_set}

    for kw in EFTS_KEYWORDS:
        url = (
            f"https://efts.sec.gov/LATEST/search-index"
            f"?q=%22{requests.utils.quote(kw)}%22"
            f"&forms=8-K"
            f"&dateRange=custom&startdt={start_date}"
        )
        try:
            r = requests.get(url, headers=SEC_HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            data = r.json()
            hits = data.get("hits", {}).get("hits", [])
            for hit in hits:
                src      = hit.get("_source", {})
                accession = src.get("accession_no", "").replace("-", "")
                if not accession or accession in seen:
                    continue
                entity_id = str(src.get("entity_id", "")).zfill(10)
                ticker = universe_cik_to_ticker.get(entity_id, "")
                if not ticker:
                    # Try raw entity_id without leading zeros
                    ticker = universe_cik_to_ticker.get(entity_id.lstrip("0") or "0", "")
                if not ticker:
                    continue

                form_url = (
                    f"https://www.sec.gov/Archives/edgar/data/"
                    f"{entity_id.lstrip('0') or '0'}/{accession}/"
                    f"{src.get('file_date','')}"
                )
                results.append({
                    "ticker":          ticker,
                    "company":         src.get("display_names", [{"name": ticker}])[0].get("name", ticker),
                    "filed_date":      src.get("file_date", start_date),
                    "form_url":        src.get("file_date", ""),
                    "is_high_signal":  True,
                    "signal_tier":     "TIER_1",
                    "signal_type":     kw.upper().replace(" ", "_"),
                    "matched_keyword": kw,
                    "summary":         _extract_summary(src.get("period_of_report", "") + " " + kw),
                    "accession":       accession,
                    "source":          "EFTS_SEARCH",
                })
            time.sleep(0.3)  # EDGAR rate limit courtesy
        except Exception as e:
            log.debug(f"EFTS search for '{kw}' failed: {e}")

    return results


def _fetch_cik_8ks(ticker: str, cik: str, since_date: str, seen: set) -> list:
    """Fetch 8-Ks directly from EDGAR submissions API for a single CIK."""
    results = []
    try:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        r = requests.get(url, headers=SEC_HEADERS, timeout=15)
        if r.status_code != 200:
            return []
        data = r.json()
        recent = data.get("filings", {}).get("recent", {})
        forms    = recent.get("form", [])
        dates    = recent.get("filingDate", [])
        accnums  = recent.get("accessionNumber", [])
        docs     = recent.get("primaryDocument", [])
        company  = data.get("name", ticker)

        cik_num = cik.lstrip("0") or "0"
        for form, date, acc, doc in zip(forms, dates, accnums, docs):
            if date < since_date:
                break  # EDGAR returns newest-first
            if form not in ("8-K", "8-K/A"):
                continue
            acc_clean = acc.replace("-", "")
            if acc_clean in seen:
                continue

            doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{acc_clean}/{doc}"
            # Fetch & score
            doc_text = ""
            try:
                dr = requests.get(doc_url, headers=SEC_HEADERS, timeout=8)
                if dr.status_code == 200:
                    doc_text = dr.text[:8000]
            except Exception:
                pass

            is_high, tier, keyword = _score_text(doc_text or acc or form)

            results.append({
                "ticker":          ticker.upper(),
                "company":         company,
                "filed_date":      date,
                "form_url":        doc_url,
                "is_high_signal":  is_high,
                "signal_tier":     tier,
                "signal_type":     keyword if keyword else "ROUTINE",
                "matched_keyword": keyword,
                "summary":         _extract_summary(doc_text, 300),
                "accession":       acc_clean,
                "source":          "CIK_DIRECT",
            })

    except Exception as e:
        log.debug(f"CIK fetch failed for {ticker}: {e}")

    return results


# ════════════════════════════════════════════════════════════════════════════
# FUNCTION 2: scan_biotech_news_rss
# ════════════════════════════════════════════════════════════════════════════

def scan_biotech_news_rss(tickers: list) -> list:
    """
    Scan RSS news feeds for articles mentioning any tracked ticker.

    Checks:
      - STAT News
      - Endpoints News
      - Fierce Biotech
      - Reuters Health
      - BioPharma Dive
      - MedPage Today

    Returns list of:
      {ticker, headline, url, published, source, signal_type, is_high_signal, summary}
    """
    ticker_set     = {t.upper() for t in tickers}
    ticker_pattern = re.compile(
        r'\b(' + '|'.join(re.escape(t) for t in sorted(ticker_set, key=len, reverse=True)) + r')\b'
    )
    results: list = []

    for feed_info in RSS_FEEDS:
        feed_name = feed_info["name"]
        feed_url  = feed_info["url"]
        log.info(f"Scanning RSS: {feed_name}")

        try:
            r = requests.get(feed_url, headers=NEWS_HEADERS, timeout=15)
            feed = feedparser.parse(r.text if r.status_code == 200 else feed_url)

            for entry in feed.entries:
                headline  = entry.get("title", "")
                url       = entry.get("link", "")
                published = entry.get("published", entry.get("updated", ""))
                summary   = entry.get("summary", entry.get("description", ""))

                combined = f"{headline} {summary}"

                # Find any mentioned tickers
                matches = ticker_pattern.findall(combined.upper())
                if not matches:
                    continue

                # Deduplicate matched tickers per article
                for ticker in set(matches):
                    if ticker not in ticker_set:
                        continue
                    is_high, tier, keyword = _score_text(combined)
                    results.append({
                        "ticker":          ticker,
                        "headline":        headline,
                        "url":             url,
                        "published":       published,
                        "source":          feed_name,
                        "signal_type":     keyword if keyword else "NEWS_MENTION",
                        "signal_tier":     tier,
                        "is_high_signal":  is_high,
                        "summary":         _extract_summary(summary, 300),
                    })

        except Exception as e:
            log.warning(f"RSS scan failed for {feed_name}: {e}")

    log.info(f"scan_biotech_news_rss: {len(results)} news hits across {len(RSS_FEEDS)} feeds")
    return results


# ════════════════════════════════════════════════════════════════════════════
# FUNCTION 3: detect_new_catalyst_dates
# ════════════════════════════════════════════════════════════════════════════

# Date pattern captures: "Q3 2025", "2025", "March 2025", "first half 2025",
#                        "mid-2025", "H2 2025", "2025 Q1", "March 15, 2025"
_DATE_PATTERNS = [
    # Full date: "March 15, 2025" / "15 March 2025"
    (r'\b(\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+20\d{2})\b', "full_date"),
    (r'\b((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+20\d{2})\b', "full_date"),
    # Quarter: "Q3 2025" / "first quarter 2025"
    (r'\b([qQ][1-4]\s+20\d{2})\b', "quarter"),
    (r'\b((?:first|second|third|fourth)\s+quarter\s+of\s+20\d{2})\b', "quarter"),
    (r'\b(20\d{2}\s+[qQ][1-4])\b', "quarter"),
    # Half: "H1 2025" / "first half 2025"
    (r'\b([Hh][12]\s+20\d{2})\b', "half_year"),
    (r'\b((?:first|second)\s+half\s+(?:of\s+)?20\d{2})\b', "half_year"),
    # Mid-year: "mid-2025"
    (r'\b(mid-?20\d{2})\b', "mid_year"),
    # Year only: "2025"
    (r'\b(20\d{2})\b', "year"),
    # Month + Year: "March 2025"
    (r'\b((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+20\d{2})\b', "month_year"),
]

# Context windows that signal specific catalyst types
_PDUFA_CONTEXT = [
    "pdufa", "fda action date", "fda decision", "fda review", "prescription drug user fee",
    "fda target action", "fda goal date", "user fee date",
    "nda submission", "bla submission", "nda filing", "bla filing",
    "regulatory submission", "fda approval", "fda approved", "approvable",
    "complete response letter", "crl", "adcom", "advisory committee",
]

_TRIAL_COMPLETION_CONTEXT = [
    "topline", "top-line", "primary completion", "data readout", "data read-out",
    "efficacy results", "trial results", "study results", "phase 3", "phase iii",
    "pivotal", "registrational", "interim analysis", "interim data",
]

_CONFERENCE_CONTEXT = CONFERENCE_NAMES  # reuse conference list


def detect_new_catalyst_dates(ticker: str, text: str) -> Optional[dict]:
    """
    Parse text (8-K body or news article) for upcoming catalyst date mentions.

    Returns the HIGHEST-CONFIDENCE catalyst found, or None.

    Return schema:
      {ticker, catalyst_type, date_str, confidence, context_snippet, detected_from}
    """
    if not text:
        return None

    lower = text.lower()

    # ── Find all date occurrences ─────────────────────────────────────────
    date_hits: list[dict] = []
    for pattern, date_type in _DATE_PATTERNS:
        for m in re.finditer(pattern, lower, re.I):
            date_hits.append({
                "date_str":  m.group(1),
                "date_type": date_type,
                "pos":       m.start(),
            })

    if not date_hits:
        return None

    # ── Score each date hit by surrounding context (±250 chars) ──────────
    best: Optional[dict] = None
    best_score = 0

    for hit in date_hits:
        pos   = hit["pos"]
        ctx   = lower[max(0, pos - 250): pos + 250]

        # Classify catalyst type + score
        score        = 0
        catalyst_type = "UNKNOWN"

        pdufa_hits = sum(1 for kw in _PDUFA_CONTEXT if kw in ctx)
        trial_hits = sum(1 for kw in _TRIAL_COMPLETION_CONTEXT if kw in ctx)
        conf_hits  = sum(1 for kw in _CONFERENCE_CONTEXT if kw in ctx)

        if pdufa_hits > 0:
            score        = 90 + pdufa_hits * 5
            catalyst_type = "PDUFA"
        elif trial_hits >= 2:
            score        = 80 + trial_hits * 3
            catalyst_type = "DATA_READOUT"
        elif trial_hits == 1:
            score        = 60
            catalyst_type = "DATA_READOUT"
        elif conf_hits > 0:
            score        = 50 + conf_hits * 5
            catalyst_type = "CONFERENCE_PRESENTATION"
        else:
            score = 20

        # Boost for more precise dates
        precision_boost = {
            "full_date":   20,
            "month_year":  15,
            "quarter":     10,
            "half_year":    8,
            "mid_year":     6,
            "year":         0,
        }
        score += precision_boost.get(hit["date_type"], 0)

        # Cap confidence at 99
        confidence = min(score, 99)

        if confidence > best_score:
            best_score = confidence
            # Extract a human-readable context snippet
            snippet_start = max(0, pos - 80)
            snippet_end   = min(len(text), pos + 80)
            ctx_snippet   = text[snippet_start:snippet_end].strip()
            ctx_snippet   = re.sub(r"\s+", " ", ctx_snippet)

            best = {
                "ticker":          ticker.upper(),
                "catalyst_type":   catalyst_type,
                "date_str":        hit["date_str"],
                "date_precision":  hit["date_type"],
                "confidence":      confidence,
                "context_snippet": ctx_snippet,
                "detected_from":   "text_parse",
            }

    # Only return if minimum confidence threshold met
    if best and best["confidence"] >= 40:
        return best
    return None


# ════════════════════════════════════════════════════════════════════════════
# FUNCTION 4: run_hourly_news_scan (main entry point)
# ════════════════════════════════════════════════════════════════════════════

def run_hourly_news_scan(tickers: list) -> dict:
    """
    Main entry point for intraday cron execution.

    Runs:
      1. SEC EDGAR 8-K scan (last 24h by default)
      2. RSS news feed scan
      3. Catalyst date extraction from all new text
      4. High-signal alert formatting

    Returns:
      {
        new_8ks:             list of 8-K filing dicts,
        news_hits:           list of news article dicts,
        new_catalyst_dates:  list of detected catalyst date dicts,
        high_signal_alerts:  list of TIER_1 alert dicts,
        run_ts:              ISO timestamp,
        summary:             human-readable summary string,
      }
    """
    run_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    log.info(f"=== HOURLY NEWS SCAN — {run_ts} | {len(tickers)} tickers ===")

    # ── 1. Scan 8-Ks ─────────────────────────────────────────────────────
    log.info("Step 1/3: Scanning SEC EDGAR 8-Ks...")
    new_8ks = scan_new_8ks(tickers, since_hours=24)

    # ── 2. Scan RSS news feeds ───────────────────────────────────────────
    log.info("Step 2/3: Scanning RSS news feeds...")
    news_hits = scan_biotech_news_rss(tickers)

    # ── 3. Extract catalyst dates from all new text ──────────────────────
    log.info("Step 3/3: Extracting catalyst dates...")
    new_catalyst_dates: list = []
    seen_date_keys: set = set()  # avoid duplicates

    # From 8-Ks
    for filing in new_8ks:
        cat = detect_new_catalyst_dates(filing["ticker"], filing.get("summary", ""))
        if cat:
            key = f"{cat['ticker']}|{cat['catalyst_type']}|{cat['date_str']}"
            if key not in seen_date_keys:
                cat["source"] = "8K"
                cat["source_url"] = filing.get("form_url", "")
                new_catalyst_dates.append(cat)
                seen_date_keys.add(key)

    # From news
    for article in news_hits:
        cat = detect_new_catalyst_dates(
            article["ticker"],
            f"{article.get('headline','')} {article.get('summary','')}",
        )
        if cat:
            key = f"{cat['ticker']}|{cat['catalyst_type']}|{cat['date_str']}"
            if key not in seen_date_keys:
                cat["source"] = article.get("source", "RSS")
                cat["source_url"] = article.get("url", "")
                new_catalyst_dates.append(cat)
                seen_date_keys.add(key)

    # ── 4. Build high-signal alert list ──────────────────────────────────
    high_signal_alerts: list = []

    for filing in new_8ks:
        if filing.get("is_high_signal") or filing.get("signal_tier") == "TIER_1":
            high_signal_alerts.append({
                "type":       "8K_HIGH_SIGNAL",
                "ticker":     filing["ticker"],
                "company":    filing["company"],
                "keyword":    filing.get("matched_keyword", ""),
                "signal_tier": filing.get("signal_tier", "TIER_1"),
                "url":        filing["form_url"],
                "filed_date": filing["filed_date"],
                "summary":    filing["summary"],
                "priority":   1 if filing.get("signal_tier") == "TIER_1" else 2,
            })

    for article in news_hits:
        if article.get("is_high_signal"):
            high_signal_alerts.append({
                "type":       "NEWS_HIGH_SIGNAL",
                "ticker":     article["ticker"],
                "company":    article["ticker"],  # No company name in RSS
                "keyword":    article.get("signal_type", ""),
                "signal_tier": article.get("signal_tier", "TIER_1"),
                "url":        article["url"],
                "published":  article["published"],
                "headline":   article["headline"],
                "summary":    article["summary"],
                "source":     article["source"],
                "priority":   1 if article.get("signal_tier") == "TIER_1" else 2,
            })

    # Sort: TIER_1 first
    high_signal_alerts.sort(key=lambda x: x.get("priority", 99))

    # ── 5. Build result dict ──────────────────────────────────────────────
    result = {
        "new_8ks":            new_8ks,
        "news_hits":          news_hits,
        "new_catalyst_dates": new_catalyst_dates,
        "high_signal_alerts": high_signal_alerts,
        "run_ts":             run_ts,
        "tickers_scanned":    len(tickers),
        "summary": (
            f"Scan complete: {len(new_8ks)} new 8-K(s), "
            f"{len(news_hits)} news mention(s), "
            f"{len(new_catalyst_dates)} catalyst date(s) detected, "
            f"{len(high_signal_alerts)} high-signal alert(s)."
        ),
    }

    # ── 6. Persist output ────────────────────────────────────────────────
    INTEL_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(result, f, indent=2, default=str)
    log.info(f"Report saved → {OUTPUT_FILE}")

    # ── 6b. Persist per-headline rows into the SQLite news_events table ──
    # Per VAL-M2-075 every watcher writes into the canonical
    # ``news_events`` table in addition to its legacy JSON state.
    # Failures here MUST NOT abort the scan (the JSON report above
    # remains the authoritative legacy artefact); we log and move on.
    try:
        _persist_news_events_to_db(result)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(f"news_events persistence failed: {exc}")

    # ── 7. Print summary ──────────────────────────────────────────────────
    _print_scan_summary(result)

    return result


def _persist_news_events_to_db(result: dict) -> None:
    """Persist scan rows into the SQLite ``news_events`` table.

    This is a thin shim around
    :func:`biotech_sniper.news_events.record_news_events` that mirrors
    the legacy JSON output into the M2 SQLite layer. It is imported
    lazily so unit tests for the watcher's network path do not pay
    the cost of opening the database.
    """

    from biotech_sniper import db as _db
    from biotech_sniper.news_events import (
        NewsEvent,
        SOURCE_UNIVERSAL,
        default_db_path,
        record_news_events,
    )

    events: list[NewsEvent] = []
    for filing in result.get("new_8ks", []) or []:
        ticker = filing.get("ticker")
        if not ticker:
            continue
        events.append(
            NewsEvent(
                ticker=str(ticker),
                source=SOURCE_UNIVERSAL,
                title=str(
                    filing.get("summary")
                    or filing.get("company")
                    or "8-K filing"
                ),
                url=filing.get("form_url") or None,
                published_at=filing.get("filed_date") or None,
                raw_payload=filing,
            )
        )
    for article in result.get("news_hits", []) or []:
        ticker = article.get("ticker")
        if not ticker:
            continue
        events.append(
            NewsEvent(
                ticker=str(ticker),
                source=SOURCE_UNIVERSAL,
                title=str(
                    article.get("headline")
                    or article.get("summary")
                    or "news"
                ),
                url=article.get("url") or None,
                published_at=article.get("published") or None,
                raw_payload=article,
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


def _print_scan_summary(result: dict) -> None:
    """Emit a structured summary of scan results.

    f-m4-02a: routes the scan summary through the project's
    structured JSON logger instead of bare ``print`` calls so the
    output lands in ``/var/log/alpha_sniper/intraday.log`` with
    secret-redaction + ts/level/event/module fields applied.
    """
    alerts = result.get("high_signal_alerts", []) or []
    cats = result.get("new_catalyst_dates", []) or []
    n8k = len(result.get("new_8ks", []) or [])
    nws = len(result.get("news_hits", []) or [])
    log.info(
        "universal_news_scan_summary",
        extra={
            "event": "universal_news_scan_summary",
            "run_ts": result.get("run_ts"),
            "tickers_scanned": result.get("tickers_scanned", 0),
            "high_signal_alerts_count": len(alerts),
            "high_signal_alerts": [
                {
                    "type": a.get("type"),
                    "ticker": a.get("ticker"),
                    "keyword": a.get("keyword", ""),
                    "summary": (a.get("summary") or a.get("headline") or "")[:120],
                    "url": a.get("url", ""),
                }
                for a in alerts
            ],
            "new_catalyst_dates_count": len(cats),
            "new_catalyst_dates": [
                {
                    "ticker": c.get("ticker"),
                    "catalyst_type": c.get("catalyst_type"),
                    "date_str": c.get("date_str"),
                    "confidence": c.get("confidence"),
                }
                for c in cats
            ],
            "new_8ks_count": n8k,
            "news_hits_count": nws,
        },
    )


# ════════════════════════════════════════════════════════════════════════════
# FUNCTION 5: format_news_alert_email
# ════════════════════════════════════════════════════════════════════════════

def format_news_alert_email(alerts: list) -> str:
    """
    Format high-signal news/8-K alerts into a plain-text email body.

    Sections:
      1. BREAKING (TIER_1 8-Ks) — topline data, PDUFA, FDA approval
      2. HIGH PRIORITY (TIER_1 news)
      3. WATCH LIST (TIER_2)

    Designed to be pasted directly into an email or sent via SMTP.
    """
    if not alerts:
        return (
            "BIOTECH SNIPER — NEWS ALERT\n"
            "=" * 50 + "\n"
            "No high-signal events detected in this scan.\n"
        )

    now_str  = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tier1_8k = [a for a in alerts if a["type"] == "8K_HIGH_SIGNAL" and a.get("signal_tier") == "TIER_1"]
    tier1_nw = [a for a in alerts if a["type"] == "NEWS_HIGH_SIGNAL" and a.get("signal_tier") == "TIER_1"]
    tier2    = [a for a in alerts if a.get("signal_tier") == "TIER_2"]

    lines = []
    lines.append("=" * 60)
    lines.append("🚨 BIOTECH SNIPER — HIGH-SIGNAL NEWS ALERT")
    lines.append(f"   {now_str}")
    lines.append(f"   {len(alerts)} alert(s) detected")
    lines.append("=" * 60)

    # ── BREAKING 8-Ks ────────────────────────────────────────────────────
    if tier1_8k:
        lines.append("")
        lines.append("🔴 BREAKING — 8-K HIGH-SIGNAL FILINGS")
        lines.append("-" * 50)
        for a in tier1_8k:
            lines.append(f"TICKER:   {a['ticker']} — {a.get('company', '')}")
            lines.append(f"KEYWORD:  {a.get('keyword', '').upper()}")
            lines.append(f"FILED:    {a.get('filed_date', '')}")
            lines.append(f"SUMMARY:  {a.get('summary', '')[:200]}")
            lines.append(f"URL:      {a.get('url', '')}")
            lines.append("")

    # ── TIER_1 news ───────────────────────────────────────────────────────
    if tier1_nw:
        lines.append("")
        lines.append("🟠 HIGH PRIORITY — NEWS ARTICLES")
        lines.append("-" * 50)
        for a in tier1_nw:
            lines.append(f"TICKER:   {a['ticker']}")
            lines.append(f"SOURCE:   {a.get('source', '')}")
            lines.append(f"HEADLINE: {a.get('headline', '')}")
            lines.append(f"KEYWORD:  {a.get('keyword', '').upper()}")
            lines.append(f"SUMMARY:  {a.get('summary', '')[:200]}")
            lines.append(f"URL:      {a.get('url', '')}")
            lines.append("")

    # ── TIER_2 watch list ─────────────────────────────────────────────────
    if tier2:
        lines.append("")
        lines.append("🟡 WATCH LIST — TIER_2 SIGNALS")
        lines.append("-" * 50)
        for a in tier2:
            tag = "8-K" if a["type"] == "8K_HIGH_SIGNAL" else "NEWS"
            lines.append(
                f"[{tag}] {a['ticker']} | {a.get('keyword','').upper()} | "
                f"{a.get('filed_date', a.get('published',''))[:10]}"
            )
            lines.append(f"  {a.get('summary', a.get('headline',''))[:150]}")
            lines.append(f"  {a.get('url', '')}")
            lines.append("")

    lines.append("=" * 60)
    lines.append("This alert was generated by Biotech Sniper universal_news_watcher.")
    lines.append("High-signal events warrant immediate review before market open.")
    lines.append("=" * 60)

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
# BIOPHARMCATALYST INTEGRATION
# ════════════════════════════════════════════════════════════════════════════

def check_biopharmcatalyst(ticker: str, timeout: int = 10) -> list:
    """
    Scrape BiopharmCatalyst for catalyst entries for a given ticker.

    Returns list of:
      {ticker, drug, catalyst_type, date_str, note, source_url}

    Note: BiopharmCatalyst does not have a public API. This fetches the
    search results page and parses the table HTML.
    """
    url = f"https://www.biopharmcatalyst.com/calendars/fda-calendar?q={ticker}"
    results = []
    try:
        r = requests.get(url, headers=NEWS_HEADERS, timeout=timeout)
        if r.status_code != 200:
            return []

        # Parse table rows from HTML
        # BPC renders a table with columns: Date, Company, Drug, Catalyst
        rows = re.findall(
            r'<tr[^>]*>(.*?)</tr>',
            r.text,
            re.S | re.I,
        )
        for row in rows:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S | re.I)
            if len(cells) < 3:
                continue
            # Strip HTML tags from each cell
            clean_cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
            if not any(ticker.upper() in c.upper() for c in clean_cells):
                continue
            # Best-effort mapping: first col = date, subsequent = drug, catalyst
            date_str     = clean_cells[0] if clean_cells else ""
            catalyst_note = " | ".join(clean_cells[1:4])
            results.append({
                "ticker":       ticker.upper(),
                "date_str":     date_str,
                "catalyst_note": catalyst_note,
                "source_url":   url,
                "source":       "BiopharmCatalyst",
            })
    except Exception as e:
        log.debug(f"BiopharmCatalyst fetch failed for {ticker}: {e}")

    return results


def scan_biopharmcatalyst_batch(tickers: list, max_tickers: int = 20) -> list:
    """
    Check BiopharmCatalyst for a batch of tickers.
    Limited to max_tickers to avoid overloading the site.

    Returns combined list from check_biopharmcatalyst() for each ticker.
    """
    all_results = []
    for ticker in tickers[:max_tickers]:
        hits = check_biopharmcatalyst(ticker)
        all_results.extend(hits)
        time.sleep(0.5)  # Polite delay
    log.info(f"BiopharmCatalyst: {len(all_results)} catalyst entries for {min(len(tickers), max_tickers)} tickers")
    return all_results


# ════════════════════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════

def _load_tracked_tickers() -> list:
    """
    Load all tracked tickers from the biotech sniper universe.

    Priority order:
      1. /state/universe_watchlist.json — tier1/tier2 candidates
      2. /intelligence/nct_registry.json — watchlist tickers
      3. Fallback: empty list (will still scan RSS broadly)
    """
    tickers = set()

    # Source 1: universe_watchlist.json
    uw_path = STATE_DIR / "universe_watchlist.json"
    if uw_path.exists():
        try:
            with open(uw_path) as f:
                uw = json.load(f)
            for key in ("tier1_tickers", "tier2_tickers", "candidates"):
                tickers.update(uw.get(key, []) if isinstance(uw.get(key), list) else [])
        except Exception:
            pass

    # Source 2: nct_registry.json watchlist
    reg_path = INTEL_DIR / "nct_registry.json"
    if reg_path.exists():
        try:
            with open(reg_path) as f:
                reg = json.load(f)
            tickers.update(reg.get("watchlist", {}).keys())
        except Exception:
            pass

    return [t.upper() for t in tickers if t and len(t) <= 6]


if __name__ == "__main__":
    import sys

    # f-m4-02a: configure the structured JSON logger before any code
    # path emits output so the CLI run produces ts/level/event/module
    # JSON lines (and respects the secret-redaction contract).
    from biotech_sniper.logging_setup import configure

    configure(log_name="universal_news_watcher")

    tickers = _load_tracked_tickers()

    if not tickers:
        # Fallback demo set
        tickers = [
            "MRNA", "BNTX", "REGN", "BIIB", "VRTX", "ALNY", "BMRN",
            "RARE", "BLUE", "SGEN", "EXEL", "INCY", "ALXN", "HALO",
            "SRPT", "PTCT", "ACAD", "SAGE", "ARWR", "NTLA", "CRSP",
        ]

    if len(sys.argv) > 1 and sys.argv[1] == "email-test":
        # Test email formatter with dummy alerts
        dummy_alerts = [
            {
                "type": "8K_HIGH_SIGNAL",
                "ticker": "MRNA",
                "company": "Moderna Inc",
                "keyword": "pdufa",
                "signal_tier": "TIER_1",
                "url": "https://www.sec.gov/example",
                "filed_date": datetime.date.today().isoformat(),
                "summary": "PDUFA date announced for mRNA-1283. FDA action date set for Q3 2025.",
            }
        ]
        log.info(
            "universal_news_email_draft",
            extra={
                "event": "universal_news_email_draft",
                "mode": "email-test",
                "body": format_news_alert_email(dummy_alerts),
            },
        )
        sys.exit(0)

    result = run_hourly_news_scan(tickers)
    log.info(
        "universal_news_scan_result",
        extra={
            "event": "universal_news_scan_result",
            "summary": result.get("summary"),
        },
    )

    if result.get("high_signal_alerts"):
        log.info(
            "universal_news_email_draft",
            extra={
                "event": "universal_news_email_draft",
                "mode": "live",
                "body": format_news_alert_email(result["high_signal_alerts"]),
            },
        )
