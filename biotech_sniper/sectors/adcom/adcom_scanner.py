#!/usr/bin/env python3
"""
FDA ADVISORY COMMITTEE MEETING SCANNER — v2
Multi-source with drug→ticker mapper + briefing doc NLP + auto-resolver

Sources (with fallbacks):
  a. FDA AdCom RSS feed  — always works, parse XML
  b. BiopharmCatalyst AdCom page — HTML parse
  c. FDTracker calendar — HTML parse
  d. flags for browser_task if JS-rendered pages fail

Drug→Ticker: drug_ticker_map covers top 50+ 2026 AdCom drugs.
  - Brand name + INN + code name → ticker + company + indication
  - Unknown drugs flagged for cron agent to resolve via web search
  - New companies auto-resolved via company_resolver

For meetings within 48 hours:
  - Download FDA briefing documents
  - Run keyword scoring: -10 to +10 sentiment
  - Output key quotes + recommendation

State:
  - seen_adcom_ids prevents reprocessing
  - new meetings auto-resolve company via company_resolver
  - P>=60% or P<=40% → add to active_plays.json
"""

import json
import re
import requests
import datetime
import hashlib
from pathlib import Path

from biotech_sniper.paths import BASE_DIR
ADCOM_STATE_FILE = BASE_DIR / "state/adcom_state.json"
ADCOM_OUTPUT     = BASE_DIR / "sectors/adcom/adcom_report.json"
ACTIVE_PLAYS     = BASE_DIR / "state/active_plays.json"

# f-misc-09: removed module-level ``sys.path.insert(BASE_DIR /
# 'intelligence')``. The bare ``from company_resolver import ...``
# inside ``run_adcom_scan`` is replaced with the canonical
# ``biotech_sniper.intelligence.company_resolver`` absolute import,
# matching the f-misc-03 cleanup pattern.

HEADERS     = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
SEC_HEADERS = {"User-Agent": "BioCatalystBot research@mantisvc.com"}

FDA_ADCOM_RSS      = "https://www.fda.gov/feeds/advisory-committee-meetings-coming-soon.rss"
FDA_ADCOM_CALENDAR = "https://www.fda.gov/advisory-committees/advisory-committee-calendar"
FEDERAL_REGISTER_FDA_NOTICES = (
    "https://www.federalregister.gov/api/v1/documents.json"
    "?conditions[agencies][]=food-and-drug-administration"
    "&conditions[type]=NOTICE"
    "&conditions[term]=advisory+committee"
    "&per_page=20"
    "&order=newest"
)


# ─────────────────────────────────────────────────────────────────────────────
# DRUG → TICKER MAP  (50+ entries for 2026 AdCom pipeline)
# ─────────────────────────────────────────────────────────────────────────────

def build_drug_ticker_map() -> dict:
    """
    Comprehensive drug→ticker mapping for FDA AdCom meetings.
    Covers brand name, INN, code name, company name → ticker.
    Dynamically merged with nct_registry.json data.
    """
    # Hardcoded known 2025-2026 AdCom drugs
    hardcoded = {
        # BIOTECH / PHARMA — CNS
        "auvelity":              {"ticker": "AXSM", "company": "Axsome Therapeutics", "indication": "Alzheimer's agitation"},
        "axs-05":                {"ticker": "AXSM", "company": "Axsome Therapeutics", "indication": "Alzheimer's agitation"},
        "dextromethorphan":      {"ticker": "AXSM", "company": "Axsome Therapeutics", "indication": "Alzheimer's agitation"},
        "axsome":                {"ticker": "AXSM", "company": "Axsome Therapeutics", "indication": ""},
        "caplyta":               {"ticker": "ITCI", "company": "Intra-Cellular Therapies", "indication": "MDD/Bipolar"},
        "lumateperone":          {"ticker": "ITCI", "company": "Intra-Cellular Therapies", "indication": "MDD/Bipolar"},
        "intra-cellular":        {"ticker": "ITCI", "company": "Intra-Cellular Therapies", "indication": ""},
        "pimavanserin":          {"ticker": "ACAD", "company": "ACADIA Pharmaceuticals", "indication": "Parkinson's psychosis"},
        "nuplazid":              {"ticker": "ACAD", "company": "ACADIA Pharmaceuticals", "indication": "Parkinson's psychosis"},
        "acadia":                {"ticker": "ACAD", "company": "ACADIA Pharmaceuticals", "indication": ""},
        "brexanolone":           {"ticker": "SAGE", "company": "Sage Therapeutics", "indication": "Depression"},
        "zuranolone":            {"ticker": "SAGE", "company": "Sage Therapeutics", "indication": "Depression"},
        "sage":                  {"ticker": "SAGE", "company": "Sage Therapeutics", "indication": ""},
        "psilocybin":            {"ticker": "CMPS", "company": "COMPASS Pathways", "indication": "TRD/PTSD"},
        "comp360":               {"ticker": "CMPS", "company": "COMPASS Pathways", "indication": "TRD"},
        "compass pathways":      {"ticker": "CMPS", "company": "COMPASS Pathways", "indication": ""},
        "ingrezza":              {"ticker": "NBIX", "company": "Neurocrine Biosciences", "indication": "Tardive dyskinesia"},
        "valbenazine":           {"ticker": "NBIX", "company": "Neurocrine Biosciences", "indication": "Tardive dyskinesia"},
        "neurocrine":            {"ticker": "NBIX", "company": "Neurocrine Biosciences", "indication": ""},

        # BIOTECH — ONCOLOGY
        "darovasertib":          {"ticker": "IDYA", "company": "IDEAYA Biosciences", "indication": "Uveal melanoma"},
        "ide196":                {"ticker": "IDYA", "company": "IDEAYA Biosciences", "indication": "Uveal melanoma"},
        "ideaya":                {"ticker": "IDYA", "company": "IDEAYA Biosciences", "indication": ""},
        "vusolimogene":          {"ticker": "REPL", "company": "Replimune", "indication": "Melanoma"},
        "rp1":                   {"ticker": "REPL", "company": "Replimune", "indication": "Melanoma"},
        "replimune":             {"ticker": "REPL", "company": "Replimune", "indication": ""},
        "daraxonrasib":          {"ticker": "RVMD", "company": "Revolution Medicines", "indication": "KRAS cancer"},
        "rmc-6236":              {"ticker": "RVMD", "company": "Revolution Medicines", "indication": "KRAS cancer"},
        "zidesamtinib":          {"ticker": "RVMD", "company": "Revolution Medicines", "indication": "KRAS/PDAC"},
        "revolution medicines":  {"ticker": "RVMD", "company": "Revolution Medicines", "indication": ""},
        "selpercatinib":         {"ticker": "LILLY", "company": "Eli Lilly", "indication": "RET+ cancer"},
        "retevmo":               {"ticker": "LILLY", "company": "Eli Lilly", "indication": "RET+ cancer"},
        "adagrasib":             {"ticker": "MRNA", "company": "Mirati (acquired)", "indication": "KRAS G12C"},
        "krazati":               {"ticker": "BMY", "company": "Bristol-Myers Squibb", "indication": "KRAS G12C"},
        "nivolumab":             {"ticker": "BMY",  "company": "Bristol-Myers Squibb", "indication": "Oncology IO"},
        "opdivo":                {"ticker": "BMY",  "company": "Bristol-Myers Squibb", "indication": "Oncology IO"},
        "pembrolizumab":         {"ticker": "MRK",  "company": "Merck", "indication": "Oncology IO"},
        "keytruda":              {"ticker": "MRK",  "company": "Merck", "indication": "Oncology IO"},

        # BIOTECH — RARE DISEASE
        "rgx-202":               {"ticker": "RGNX", "company": "REGENXBIO", "indication": "Duchenne MD"},
        "regenxbio":             {"ticker": "RGNX", "company": "REGENXBIO", "indication": ""},
        "elegrobart":            {"ticker": "VRDN", "company": "Viridian Therapeutics", "indication": "TED"},
        "viridian":              {"ticker": "VRDN", "company": "Viridian Therapeutics", "indication": ""},
        "ntla-2002":             {"ticker": "NTLA", "company": "Intellia Therapeutics", "indication": "HAE"},
        "onvo-z":                {"ticker": "NTLA", "company": "Intellia Therapeutics", "indication": "HAE"},
        "intellia":              {"ticker": "NTLA", "company": "Intellia Therapeutics", "indication": ""},
        "fitusiran":             {"ticker": "ALNY", "company": "Alnylam Pharmaceuticals", "indication": "Hemophilia"},
        "inclisiran":            {"ticker": "NVS",  "company": "Novartis", "indication": "LDL-C"},
        "leqvio":                {"ticker": "NVS",  "company": "Novartis", "indication": "LDL-C"},
        "obicetrapib":           {"ticker": "NAMS", "company": "NewAmsterdam Pharma", "indication": "LDL-C"},
        "newamsterdam":          {"ticker": "NAMS", "company": "NewAmsterdam Pharma", "indication": ""},
        "sparsentan":            {"ticker": "TVTX", "company": "Travere Therapeutics", "indication": "FSGS/IgAN"},
        "filspari":              {"ticker": "TVTX", "company": "Travere Therapeutics", "indication": "FSGS/IgAN"},
        "travere":               {"ticker": "TVTX", "company": "Travere Therapeutics", "indication": ""},
        "ersodetug":             {"ticker": "RZLT", "company": "Rezolute", "indication": "Tumor HI"},
        "rezolute":              {"ticker": "RZLT", "company": "Rezolute", "indication": ""},

        # BIOTECH — IMMUNOLOGY
        "efgartigimod":          {"ticker": "ARGX", "company": "argenx", "indication": "gMG/MG"},
        "vyvgart":               {"ticker": "ARGX", "company": "argenx", "indication": "gMG"},
        "argenx":                {"ticker": "ARGX", "company": "argenx", "indication": ""},
        "sonelokimab":           {"ticker": "MLTX", "company": "MoonLake Immunotherapeutics", "indication": "PsA/PsO"},
        "moonlake":              {"ticker": "MLTX", "company": "MoonLake Immunotherapeutics", "indication": ""},
        "tebapivat":             {"ticker": "AGIO", "company": "Agios Pharmaceuticals", "indication": "LR-MDS"},
        "ag-946":                {"ticker": "AGIO", "company": "Agios Pharmaceuticals", "indication": "LR-MDS"},
        "agios":                 {"ticker": "AGIO", "company": "Agios Pharmaceuticals", "indication": ""},
        "ublituximab":           {"ticker": "TGTX", "company": "TG Therapeutics", "indication": "MS"},
        "briumvi":               {"ticker": "TGTX", "company": "TG Therapeutics", "indication": "MS"},
        "tg therapeutics":       {"ticker": "TGTX", "company": "TG Therapeutics", "indication": ""},
        "rozanolixizumab":       {"ticker": "UCB",  "company": "UCB", "indication": "gMG"},
        "inebilizumab":          {"ticker": "MORF", "company": "Morphic Therapeutic", "indication": ""},
        "imetelstat":            {"ticker": "GERN", "company": "Geron Corporation", "indication": "MDS/MF"},
        "geron":                 {"ticker": "GERN", "company": "Geron", "indication": ""},
        "navitoclax":            {"ticker": "ABBV", "company": "AbbVie", "indication": "MF"},

        # GENE THERAPY / GENE EDITING
        "casgevy":               {"ticker": "CRSP", "company": "CRISPR Therapeutics", "indication": "Sickle cell"},
        "exa-cel":               {"ticker": "CRSP", "company": "CRISPR Therapeutics", "indication": "Sickle cell"},
        "crispr therapeutics":   {"ticker": "CRSP", "company": "CRISPR Therapeutics", "indication": ""},
        "delandistrogene":       {"ticker": "SRPT", "company": "Sarepta Therapeutics", "indication": "DMD"},
        "elevidys":              {"ticker": "SRPT", "company": "Sarepta Therapeutics", "indication": "DMD"},
        "sarepta":               {"ticker": "SRPT", "company": "Sarepta Therapeutics", "indication": ""},

        # CARDIOVASCULAR
        "inclisiran":            {"ticker": "NVS",  "company": "Novartis", "indication": "LDL-C"},
        "zilebesiran":           {"ticker": "ARWR", "company": "Arrowhead Pharmaceuticals", "indication": "Hypertension"},
        "olpasiran":             {"ticker": "AMGN", "company": "Amgen", "indication": "Lp(a)"},
        "muvalaplin":            {"ticker": "LLY",  "company": "Eli Lilly", "indication": "Lp(a)"},

        # METABOLIC / OBESITY
        "tirzepatide":           {"ticker": "LLY",  "company": "Eli Lilly", "indication": "T2D/Obesity"},
        "mounjaro":              {"ticker": "LLY",  "company": "Eli Lilly", "indication": "T2D/Obesity"},
        "zepbound":              {"ticker": "LLY",  "company": "Eli Lilly", "indication": "Obesity"},
        "semaglutide":           {"ticker": "NVO",  "company": "Novo Nordisk", "indication": "T2D/Obesity"},
        "ozempic":               {"ticker": "NVO",  "company": "Novo Nordisk", "indication": "T2D"},
        "wegovy":                {"ticker": "NVO",  "company": "Novo Nordisk", "indication": "Obesity"},
        "cagrilintide":          {"ticker": "NVO",  "company": "Novo Nordisk", "indication": "Obesity"},
        "retatrutide":           {"ticker": "LLY",  "company": "Eli Lilly", "indication": "Obesity"},
    }

    # Dynamically merge registry watchlist data
    registry_file = BASE_DIR / "intelligence/nct_registry.json"
    if registry_file.exists():
        try:
            with open(registry_file) as f:
                registry = json.load(f)
            for ticker, info in registry.get("watchlist", {}).items():
                drug = info.get("drug", "")
                company = info.get("company", "")
                indication = info.get("indication", "") or info.get("trial", "")

                # Add drug (and variations)
                if drug:
                    for d in re.split(r'[/+;,]', drug):
                        d = d.strip().lower()
                        if d and len(d) > 3:
                            if d not in hardcoded:
                                hardcoded[d] = {
                                    "ticker": ticker,
                                    "company": company,
                                    "indication": indication,
                                }
                # Add company shortname
                if company:
                    short = company.lower().split()[0]
                    if len(short) > 3 and short not in hardcoded:
                        hardcoded[short] = {
                            "ticker": ticker,
                            "company": company,
                            "indication": indication,
                        }
        except Exception as e:
            print(f"  [drug_map] Registry merge error: {e}")

    return hardcoded


# ─────────────────────────────────────────────────────────────────────────────
# BRIEFING DOCUMENT ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

BEARISH_ADCOM_PHRASES = [
    "the committee should consider whether",
    "the applicant has not demonstrated",
    "the agency is concerned",
    "questions remain regarding",
    "the data do not support",
    "the primary endpoint was not met",
    "the benefit-risk profile",
    "whether the totality of evidence",
    "discuss the uncertainties",
    "the agency requests the committee address",
    "whether the available data are sufficient",
    "unresolved questions",
    "the following questions",
    "major concerns",
    "the fda notes that",
    "the agency is asking the committee",
    "what additional studies",
    "are the data adequate",
    "does the committee believe",
    "the applicant failed to",
    "insufficient evidence",
]

BULLISH_ADCOM_PHRASES = [
    "substantial evidence of effectiveness",
    "the data support",
    "clinically meaningful",
    "the benefit-risk is favorable",
    "unprecedented efficacy",
    "breakthrough",
    "the agency agrees with the applicant",
    "statistically significant and clinically meaningful",
    "no new safety signals",
    "consistent with",
    "the totality of evidence supports",
    "adequate and well-controlled",
    "compelling efficacy",
    "favorable benefit-risk",
    "the agency concurs",
    "well-established safety profile",
]

def analyze_briefing_text(text: str) -> dict:
    """
    Analyze FDA briefing document text for sentiment.
    Returns sentiment score -10 to +10, key quotes, recommendation.
    """
    text_lower = text.lower()
    bearish_count = sum(1 for p in BEARISH_ADCOM_PHRASES if p in text_lower)
    bullish_count = sum(1 for p in BULLISH_ADCOM_PHRASES if p in text_lower)
    question_count = text_lower.count("?")

    net = bullish_count - bearish_count
    # Clamp to -10..+10
    score = max(-10, min(10, net * 1.5 + (0.5 if bullish_count > 0 else 0) - (0.5 if bearish_count > 0 else 0)))
    score = round(score, 1)

    # Extract key bearish quotes
    key_quotes = []
    for phrase in BEARISH_ADCOM_PHRASES:
        idx = text_lower.find(phrase)
        if idx >= 0:
            start = max(0, idx - 50)
            end   = min(len(text), idx + len(phrase) + 150)
            key_quotes.append(text[start:end].strip()[:300])
            if len(key_quotes) >= 3:
                break

    if score >= 5:
        recommendation = "BULLISH_BRIEFING"
    elif score >= 2:
        recommendation = "MILDLY_BULLISH_BRIEFING"
    elif score <= -5:
        recommendation = "BEARISH_BRIEFING"
    elif score <= -2:
        recommendation = "MILDLY_BEARISH_BRIEFING"
    else:
        recommendation = "NEUTRAL_BRIEFING"

    return {
        "sentiment_score": score,
        "bearish_count":   bearish_count,
        "bullish_count":   bullish_count,
        "question_count":  question_count,
        "key_quotes":      key_quotes,
        "recommendation":  recommendation,
    }


def fetch_briefing_documents(meeting_link: str) -> list:
    """
    Try to fetch FDA briefing docs for an AdCom meeting.
    Posted ~48h before meeting.
    Returns list of {text, url, name} dicts.
    """
    if not meeting_link:
        return []

    try:
        r = requests.get(meeting_link, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return []

        # Find PDF links
        pdf_links = re.findall(
            r'href=["\']([^"\']*(?:brief|background|sponsor|information-package)[^"\']*\.pdf[^"\']*)["\']',
            r.text, re.I
        )
        # Broader PDF search
        if not pdf_links:
            pdf_links = re.findall(r'href=["\']([^"\']*\.pdf[^"\']*)["\']', r.text, re.I)

        full_links = []
        for link in pdf_links[:5]:
            if not link.startswith("http"):
                link = "https://www.fda.gov" + link
            full_links.append(link)

        return [{"url": url, "name": url.split("/")[-1]} for url in full_links]
    except Exception as e:
        print(f"  [briefing] Fetch error: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE A: FDA AdCom RSS
# ─────────────────────────────────────────────────────────────────────────────

def fetch_fda_adcom_rss() -> list:
    """Fetch FDA AdCom RSS — most reliable source, always works."""
    meetings = []
    try:
        try:
            import feedparser
            feed = feedparser.parse(FDA_ADCOM_RSS)
            entries = feed.entries
        except ImportError:
            r = requests.get(FDA_ADCOM_RSS, headers=HEADERS, timeout=20)
            entries = []
            for item_m in re.finditer(r'<item>(.*?)</item>', r.text, re.S):
                item = item_m.group(1)
                title_m   = re.search(r'<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>', item, re.S)
                desc_m    = re.search(r'<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>', item, re.S)
                link_m    = re.search(r'<link>(.*?)</link>', item, re.S)

                class _E:
                    pass
                e = _E()
                e.title   = re.sub(r'<[^>]+>', '', title_m.group(1)).strip() if title_m else ""
                e.summary = re.sub(r'<[^>]+>', '', desc_m.group(1)).strip() if desc_m else ""
                e.link    = link_m.group(1).strip() if link_m else ""
                entries.append(e)

        for entry in entries:
            title   = getattr(entry, "title", "") or ""
            summary = getattr(entry, "summary", "") or ""
            link    = getattr(entry, "link", "") or ""

            # Extract date from title or summary
            date_match = re.search(
                r'(\w+ \d{1,2},?\s*\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})',
                f"{title} {summary}"
            )
            date_str = date_match.group(1) if date_match else ""

            meetings.append({
                "date_text": date_str,
                "committee": title,
                "topic": summary,
                "link": link,
                "source": "fda_rss",
            })

        print(f"  [adcom] FDA RSS: {len(meetings)} entries")

    except Exception as e:
        print(f"  [adcom] FDA RSS error: {e}")

    return meetings


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE B: BiopharmCatalyst AdCom Calendar
# ─────────────────────────────────────────────────────────────────────────────

def fetch_biopharmcatalyst_adcom() -> list:
    """Fetch BiopharmCatalyst AdCom calendar — HTML parse."""
    meetings = []
    try:
        r = requests.get(
            "https://www.biopharmcatalyst.com/calendars/adcom-calendar",
            headers={**HEADERS, "Referer": "https://www.biopharmcatalyst.com/"},
            timeout=20
        )
        if r.status_code != 200:
            print(f"  [adcom] BPC HTTP {r.status_code}")
            return meetings

        html = r.text
        # Try JSON in page first
        json_match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', html, re.S)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                catalysts = data.get("adcoms", data.get("meetings", data.get("calendar", [])))
                for entry in (catalysts if isinstance(catalysts, list) else []):
                    ticker  = entry.get("ticker", "").upper()
                    date    = entry.get("date", "") or entry.get("adcom_date", "")
                    drug    = entry.get("drug", "") or entry.get("catalyst", "")
                    company = entry.get("company", "") or entry.get("name", "")
                    meetings.append({
                        "date_text": date,
                        "committee": company,
                        "topic": drug,
                        "link": "",
                        "ticker_hint": ticker,
                        "source": "biopharmcatalyst",
                    })
            except Exception as e:
                print(f"  [adcom] BPC JSON error: {e}")

        # HTML table fallback
        if not meetings:
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.S | re.I)
            for row in rows:
                cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S | re.I)
                if len(cells) >= 2:
                    date_text = re.sub(r'<[^>]+>', '', cells[0]).strip()
                    topic     = re.sub(r'<[^>]+>', '', cells[1]).strip()
                    if date_text and topic:
                        meetings.append({
                            "date_text": date_text,
                            "committee": "",
                            "topic": topic,
                            "link": "",
                            "source": "biopharmcatalyst",
                        })

        print(f"  [adcom] BPC calendar: {len(meetings)} entries")

    except Exception as e:
        print(f"  [adcom] BPC error: {e}")
        meetings.append({
            "needs_browser_task": True,
            "browser_url": "https://www.biopharmcatalyst.com/calendars/adcom-calendar",
            "source": "biopharmcatalyst_browser_required",
        })

    return meetings


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE C: FDTracker
# ─────────────────────────────────────────────────────────────────────────────

def fetch_fdatracker_adcom() -> list:
    """Fetch FDTracker FDA calendar — HTML parse."""
    meetings = []
    try:
        r = requests.get("https://www.fdatracker.com/fda-calendar/", headers=HEADERS, timeout=20)
        if r.status_code != 200:
            print(f"  [adcom] FDTracker HTTP {r.status_code}")
            return [{"needs_browser_task": True, "browser_url": "https://www.fdatracker.com/fda-calendar/"}]

        html = r.text
        # FDTracker uses table layout
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            tables = soup.find_all("table")
            for table in tables:
                rows = table.find_all("tr")
                for row in rows[1:]:
                    cols = row.find_all(["td", "th"])
                    if len(cols) >= 2:
                        date_text = cols[0].get_text(strip=True)
                        topic     = cols[-1].get_text(strip=True)
                        link_tag  = row.find("a", href=True)
                        link      = link_tag["href"] if link_tag else ""
                        if not link.startswith("http") and link:
                            link = "https://www.fdatracker.com" + link
                        if date_text:
                            meetings.append({
                                "date_text": date_text,
                                "committee": cols[1].get_text(strip=True) if len(cols) > 2 else "",
                                "topic": topic,
                                "link": link,
                                "source": "fdatracker",
                            })
        except ImportError:
            # Regex fallback
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.S | re.I)
            for row in rows:
                cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S | re.I)
                if len(cells) >= 2:
                    date_text = re.sub(r'<[^>]+>', '', cells[0]).strip()
                    topic = re.sub(r'<[^>]+>', '', cells[-1]).strip()
                    if date_text and topic:
                        meetings.append({
                            "date_text": date_text,
                            "committee": "",
                            "topic": topic,
                            "link": "",
                            "source": "fdatracker",
                        })

        print(f"  [adcom] FDTracker: {len(meetings)} entries")

    except Exception as e:
        print(f"  [adcom] FDTracker error: {e}")
        meetings.append({
            "needs_browser_task": True,
            "browser_url": "https://www.fdatracker.com/fda-calendar/",
            "source": "fdatracker_browser_required",
        })

    return meetings


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE D: Federal Register FDA AdCom Notices
# ─────────────────────────────────────────────────────────────────────────────

def fetch_federal_register_adcom() -> list:
    """Fetch FDA advisory committee notices from Federal Register JSON API (no JS needed)."""
    meetings = []
    try:
        r = requests.get(FEDERAL_REGISTER_FDA_NOTICES, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"  [adcom] Federal Register: HTTP {r.status_code}")
            return meetings
        data = r.json()
        docs = data.get("results", [])
        print(f"  [adcom] Federal Register FDA notices: {len(docs)}")
        for doc in docs:
            title    = doc.get("title", "")
            pub_date = doc.get("publication_date", "")
            abstract = doc.get("abstract", "") or ""
            url      = doc.get("html_url", "") or doc.get("pdf_url", "")
            full_text = f"{title} {abstract}".lower()
            if not any(kw in full_text
                       for kw in ["advisory committee", "adcom", "advisory panel"]):
                continue
            meetings.append({
                "date_text": pub_date,
                "committee": title[:150],
                "topic":     abstract[:300],
                "link":      url,
                "source":    "federal_register_fda",
            })
    except Exception as e:
        print(f"  [adcom] Federal Register error: {e}")
    return meetings


# ─────────────────────────────────────────────────────────────────────────────
# DATE PARSING
# ─────────────────────────────────────────────────────────────────────────────

def parse_meeting_date(date_text: str):
    """Parse various FDA date formats. Returns date or None."""
    if not date_text:
        return None

    # Clean HTML
    date_text = re.sub(r'<[^>]+>', '', date_text).strip()

    formats = [
        "%B %d, %Y",    # April 15, 2026
        "%m/%d/%Y",     # 04/15/2026
        "%Y-%m-%d",     # 2026-04-15
        "%b %d, %Y",    # Apr 15, 2026
        "%B %d %Y",     # April 15 2026
        "%b %d %Y",     # Apr 15 2026
        "%m/%d/%y",     # 04/15/26
    ]

    for fmt in formats:
        try:
            return datetime.datetime.strptime(date_text.strip(), fmt).date()
        except:
            continue

    # Try to extract date from messy text
    match = re.search(r'(\w+ \d{1,2},?\s*\d{4})', date_text)
    if match:
        for fmt in ["%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"]:
            try:
                return datetime.datetime.strptime(match.group(1).strip(), fmt).date()
            except:
                continue

    # Try numeric date extraction
    match = re.search(r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})', date_text)
    if match:
        try:
            m, d, y = int(match.group(1)), int(match.group(2)), int(match.group(3))
            if y < 100:
                y += 2000
            return datetime.date(y, m, d)
        except:
            pass

    return None


# ─────────────────────────────────────────────────────────────────────────────
# TICKER MAPPER
# ─────────────────────────────────────────────────────────────────────────────

def find_ticker_for_meeting(topic: str, committee: str,
                             drug_map: dict) -> tuple:
    """
    Map AdCom meeting to a public company ticker.

    Returns (ticker, drug_name, search_query_if_unknown)
    """
    full_text = f"{topic} {committee}".lower()

    # Check drug_ticker_map
    for drug_key, info in drug_map.items():
        if drug_key.lower() in full_text:
            return info.get("ticker", ""), drug_key, ""

    # Try to extract a drug name (INN suffix patterns)
    inn_match = re.search(
        r'\b([A-Z][a-z]+(?:mab|nib|zib|tinib|ciclib|umab|imab|ximab|zumab|lumab|tumab|mumab|'
        r'olomab|rafenib|tuzumab|cilimab|amide|olimus|fostat|stat|vir|parin|cetib))\b',
        f"{topic} {committee}"
    )
    if inn_match:
        drug_name = inn_match.group(1)
        search_q  = f'"{drug_name}" company stock ticker FDA approval'
        return "", drug_name, search_q

    # Generic company search
    search_q = f'"{topic[:50]}" FDA advisory committee company stock ticker'
    return "", "", search_q


# ─────────────────────────────────────────────────────────────────────────────
# AdCom SCORING PROMPT
# ─────────────────────────────────────────────────────────────────────────────

def build_adcom_probability_prompt(meeting_date, committee, topic, drug_name,
                                    company, ticker, indication, background=""):
    """Build Aletheia-style prompt for AdCom dual-model scoring."""
    return f"""You are Warpspeed v2 — FDA regulatory probability specialist.
Predict P(FDA Advisory Committee will vote YES / favorable) for this meeting.

MEETING DETAILS:
Date: {meeting_date}
Committee: {committee}
Topic/Drug: {topic}
Drug name: {drug_name}
Company: {company} (ticker: {ticker})
Indication: {indication}
Background: {background[:1000]}

SCORING FRAMEWORK:
1. BASE RATES:
   - Overall FDA AdCom positive vote rate: ~67% historically
   - After Priority Review: ~75%
   - After Breakthrough Therapy Designation: ~82%
   - In oncology (strong unmet need): ~72%
   - In CNS (historically harder): ~58%
   - With single-arm trial only: ~55%
   - With positive Phase 3 RCT: ~78%
   - After CRL resubmission: ~60%

2. FACTOR ANALYSIS (weight each):
   a) Clinical trial design quality (RCT vs single-arm) — 25%
   b) Unmet medical need in indication — 20%
   c) Safety profile — 20%
   d) Effect size vs clinical significance — 20%
   e) Regulatory history — 15%

3. MONTE CARLO: Bull/Base/Bear scenarios summing to 1.0

4. OUTPUT FORMAT (exact):
   P(positive AdCom vote): XX% [80% CI: XX%-XX%]
   Confidence: Low/Medium/High
   Key upside: [3 bullets]
   Key downside: [3 bullets]
   Expected move YES: +X% to +X%
   Expected move NO: -X% to -X%
   Options structure: [strike, expiry, type recommendation]
   Note: AdCom vote is NOT FDA approval. FDA follows AdCom ~75% of time."""


# ─────────────────────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────────────────────

def load_adcom_state() -> dict:
    if ADCOM_STATE_FILE.exists():
        with open(ADCOM_STATE_FILE) as f:
            return json.load(f)
    return {"upcoming_meetings": {}, "briefings_analyzed": [], "last_run": None}

def save_adcom_state(state: dict):
    ADCOM_STATE_FILE.parent.mkdir(exist_ok=True)
    with open(ADCOM_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def load_active_plays() -> dict:
    if ACTIVE_PLAYS.exists():
        with open(ACTIVE_PLAYS) as f:
            return json.load(f)
    return {"active": {}, "monitor": {}}


# ─────────────────────────────────────────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────────────────────────────────────────

def run_adcom_scan() -> dict:
    """
    Main AdCom scan — multi-source, with drug→ticker mapper.
    For each new meeting:
      1. Map drug → ticker via drug_ticker_map
      2. Auto-resolve company via company_resolver
      3. Score via dual-model if P unknown
      4. Add to active_plays if P>=60% or P<=40%
      5. Check briefing docs if meeting within 48h
    """
    state        = load_adcom_state()
    active_plays = load_active_plays()
    today        = datetime.date.today()
    today_str    = today.isoformat()
    cutoff       = today + datetime.timedelta(days=90)  # 90-day lookahead
    drug_map     = build_drug_ticker_map()

    all_signals     = []
    new_plays       = []
    briefing_alerts = []
    browser_tasks   = []

    print(f"\n{'='*70}")
    print(f"FDA ADCOM SCANNER v2 — {today_str}")
    print(f"Drug map entries: {len(drug_map)} | Sources: FDA RSS + BPC + FDTracker")
    print(f"{'='*70}")

    # ── COLLECT MEETINGS FROM ALL SOURCES ────────────────────────────────────
    all_meetings = []

    # Source A: FDA RSS (most reliable)
    print("\nFetching FDA AdCom RSS...")
    rss_meetings = fetch_fda_adcom_rss()
    all_meetings.extend(rss_meetings)

    # Source B: BiopharmCatalyst
    print("Fetching BiopharmCatalyst AdCom calendar...")
    bpc_meetings = fetch_biopharmcatalyst_adcom()
    for m in bpc_meetings:
        if m.get("needs_browser_task"):
            browser_tasks.append(m)
        else:
            all_meetings.append(m)

    # Source C: FDTracker
    print("Fetching FDTracker calendar...")
    fdt_meetings = fetch_fdatracker_adcom()
    for m in fdt_meetings:
        if m.get("needs_browser_task"):
            browser_tasks.append(m)
        else:
            all_meetings.append(m)

    # Source D: Federal Register FDA notices (reliable JSON API, no JS)
    print("Fetching Federal Register FDA AdCom notices...")
    fed_reg_meetings = fetch_federal_register_adcom()
    all_meetings.extend(fed_reg_meetings)

    print(f"\n  Total meeting entries: {len(all_meetings)} "
          f"(+ {len(browser_tasks)} need browser_task)")

    # ── PROCESS EACH MEETING ─────────────────────────────────────────────────
    seen_meeting_ids = set(state.get("upcoming_meetings", {}).keys())
    processed = {}

    for meeting in all_meetings:
        # Skip browser_task flags
        if meeting.get("needs_browser_task"):
            continue

        date_text = meeting.get("date_text", "")
        committee = meeting.get("committee", "")
        topic     = meeting.get("topic", "")
        link      = meeting.get("link", "")

        # Parse date
        meeting_date = parse_meeting_date(date_text)
        if not meeting_date:
            continue

        # Only within lookahead window
        if meeting_date < today or meeting_date > cutoff:
            continue

        days_out = (meeting_date - today).days

        # Find ticker
        ticker_hint = meeting.get("ticker_hint", "")
        if ticker_hint:
            ticker, drug_name, search_q = ticker_hint, "", ""
        else:
            ticker, drug_name, search_q = find_ticker_for_meeting(topic, committee, drug_map)

        # Build meeting ID
        date_key = meeting_date.isoformat()
        ticker_key = ticker or drug_name[:8] if (ticker or drug_name) else hashlib.md5(
            f"{date_text}{committee}{topic}".encode()
        ).hexdigest()[:8]
        meeting_id = f"{date_key}_{ticker_key}"

        # Deduplicate by meeting_id
        if meeting_id in processed:
            continue
        processed[meeting_id] = True

        # Check if already in active plays (tracked)
        already_tracked = (
            ticker in active_plays.get("active", {}) or
            ticker in active_plays.get("monitor", {})
        ) if ticker else False

        # Check if new
        is_new = meeting_id not in seen_meeting_ids

        print(
            f"  {'NEW' if is_new else 'KNOWN'} | {ticker or '???'} | "
            f"{meeting_date} ({days_out}d) | {committee[:40]}"
        )

        if is_new or days_out <= 2:
            # Auto-resolve company if ticker known and not already tracked
            resolved_profile = {}
            if ticker and not already_tracked:
                try:
                    # f-misc-09: replaced bare ``from company_resolver
                    # import ...`` (which only resolved when the legacy
                    # module-level ``sys.path.insert`` mutated sys.path)
                    # with the canonical absolute import.
                    from biotech_sniper.intelligence.company_resolver import (
                        resolve_company,
                        update_registry_with_company,
                    )
                    resolved_profile = resolve_company(
                        ticker=ticker,
                        company_name=next(
                            (v["company"] for k, v in drug_map.items()
                             if v.get("ticker") == ticker), ""
                        ),
                        drug=drug_name,
                        estimated_announcement=date_key,
                    )
                    update_registry_with_company(resolved_profile)
                except Exception as e:
                    print(f"    [resolver] {ticker}: {e}")

            signal = {
                "sector":       "ADCOM",
                "ticker":       ticker,
                "drug_name":    drug_name,
                "type":         "UPCOMING_ADCOM",
                "severity":     "HIGH" if days_out <= 14 else "MODERATE",
                "icon":         "🟠" if days_out <= 14 else "🟡",
                "meeting_date": date_key,
                "days_out":     days_out,
                "committee":    committee[:100],
                "topic":        topic[:200],
                "link":         link,
                "source":       meeting.get("source", ""),
                "already_tracked": already_tracked,
                "detected_date": today_str,
                "needs_dual_model_scoring": is_new and not already_tracked,
                "search_query_for_ticker": search_q,
                "scoring_prompt": build_adcom_probability_prompt(
                    date_key, committee, topic, drug_name,
                    next((v["company"] for k, v in drug_map.items() if v.get("ticker") == ticker), ""),
                    ticker, next((v["indication"] for k, v in drug_map.items() if v.get("ticker") == ticker), ""),
                ) if is_new and ticker else "",
            }

            if is_new:
                all_signals.append(signal)
                state.setdefault("upcoming_meetings", {})[meeting_id] = {
                    "date": date_key,
                    "ticker": ticker,
                    "committee": committee[:100],
                    "topic": topic[:100],
                    "added_date": today_str,
                    "days_out_when_added": days_out,
                }

        # Briefing document window (48 hours)
        if days_out <= 2 and meeting_id not in state.get("briefings_analyzed", []):
            print(f"    BRIEFING WINDOW ({days_out}d): fetching docs for {ticker}...")
            doc_links = fetch_briefing_documents(link) if link else []

            briefing_entry = {
                "ticker":       ticker,
                "meeting_date": date_key,
                "days_out":     days_out,
                "briefing_docs": doc_links,
            }

            if doc_links:
                print(f"    Found {len(doc_links)} briefing PDFs")
                # Try to score first doc
                try:
                    br = requests.get(doc_links[0]["url"], headers=HEADERS, timeout=20, stream=True)
                    if br.status_code == 200:
                        raw_text = br.content.decode("latin-1", errors="replace")
                        analysis = analyze_briefing_text(raw_text)
                        briefing_entry.update(analysis)
                        print(
                            f"    Briefing sentiment: {analysis['sentiment_score']} "
                            f"({analysis['recommendation']})"
                        )
                except Exception as e:
                    print(f"    Briefing doc error: {e}")

                state.setdefault("briefings_analyzed", []).append(meeting_id)
            else:
                print(f"    No briefing docs found yet (usually posted 24-48h before)")

            briefing_alerts.append(briefing_entry)

    # ── CLEAN UP PAST MEETINGS ────────────────────────────────────────────────
    for mid in list(state.get("upcoming_meetings", {}).keys()):
        md = state["upcoming_meetings"][mid]
        try:
            mdate = datetime.date.fromisoformat(md["date"])
            if mdate < today - datetime.timedelta(days=3):
                print(f"  Archiving past AdCom: {mid}")
                del state["upcoming_meetings"][mid]
        except:
            pass

    state["last_run"] = today_str
    save_adcom_state(state)

    # ── REPORT ────────────────────────────────────────────────────────────────
    report = {
        "run_date":       today_str,
        "signals":        all_signals,
        "briefing_alerts": briefing_alerts,
        "browser_tasks":  browser_tasks,
        "upcoming_count": len(state.get("upcoming_meetings", {})),
        "needs_scoring":  [s for s in all_signals if s.get("needs_dual_model_scoring")],
        "drug_map_size":  len(drug_map),
    }

    ADCOM_OUTPUT.parent.mkdir(exist_ok=True)
    with open(ADCOM_OUTPUT, "w") as f:
        json.dump(report, f, indent=2)

    print(f"\n{'='*70}")
    print(f"ADCOM SUMMARY: {len(all_signals)} new | {report['upcoming_count']} upcoming | {len(briefing_alerts)} briefing alerts")
    if briefing_alerts:
        for a in briefing_alerts:
            score = a.get("sentiment_score", "?")
            rec   = a.get("recommendation", "")
            print(f"  {a['ticker']}: AdCom in {a['days_out']}d | briefing={score} ({rec})")
    if browser_tasks:
        print(f"  Browser tasks needed: {len(browser_tasks)}")
        for bt in browser_tasks:
            print(f"    {bt.get('browser_url', '')}")
    print(f"{'='*70}\n")

    return report


if __name__ == "__main__":
    run_adcom_scan()
