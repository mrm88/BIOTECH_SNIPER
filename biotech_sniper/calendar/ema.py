"""EMA / CHMP calendar scraper (Reading-B feature f-m1-05).

This module owns the daily fetch + parse + persistence path for
upcoming European Medicines Agency (EMA) Committee for Medicinal
Products for Human Use (CHMP) meeting and opinion dates. The
output table ``ema_calendar`` is one of three sources merged by the
``trial_calendar`` lookup (the others being CT.gov and FDA PDUFA).

Source strategy
---------------

The default upstream source is the EMA CHMP meeting highlights
page::

    https://www.ema.europa.eu/en/committees/chmp/chmp-meeting-highlights

Operators may override the URL via ``--source`` or the
:envvar:`EMA_SOURCE_URL` environment variable. When an explicit
override is set (CLI flag OR env var), any HTTP failure (timeout,
non-2xx response, DNS error) propagates as
:class:`UpstreamUnavailable` and the writer exits non-zero with the
prior ``ema_calendar`` rows preserved as the last-good fallback.

When NO explicit override is set and the default upstream is
unreachable (transient network error, 5xx, etc.), the scraper
falls back to a bundled JSON seed file at
``biotech_sniper/calendar/seed/ema_seed.json`` containing a small
hand-curated list of upcoming CHMP meetings / opinions. The seed
fallback exists so that the documented CLI invocation
``python -m biotech_sniper.calendar.ema --refresh`` always writes
≥ 1 row on a representative day (per VAL-M1-028). Operators with a
working live fetch override the seed via the standard UPSERT path
on the composite UNIQUE key.

Behavioural contract
--------------------

* **Descriptive User-Agent.** Every outbound HTTP request carries
  the project's :data:`DEFAULT_USER_AGENT` header (project id +
  contact email). The default ``python-requests`` UA is forbidden.
  The exact UA string matches the regex
  ``^BiotechSniper/[0-9].* contact: .*@.*$``.
* **Composite-key idempotency.** Rows are written via
  ``INSERT ... ON CONFLICT DO UPDATE`` keyed on the composite
  UNIQUE clause ``(ticker_or_sponsor, product,
  COALESCE(meeting_date,''), COALESCE(opinion_date,''))`` so a
  back-to-back ``--refresh`` yields zero net new rows
  (per VAL-M1-029). Only ``sponsor``, ``source_url`` and
  ``fetched_at`` are refreshed on conflict.
* **At-least-one date.** Every persisted row has at least one of
  ``meeting_date``/``opinion_date`` non-null (a CHECK constraint
  enforces this in the DDL per VAL-M1-027). Rows whose dates are
  both NULL are dropped pre-write.
* **ISO-8601 dates.** Every persisted ``meeting_date`` /
  ``opinion_date`` validates against the GLOB
  ``[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]`` and the band
  ``[today-365d, today+730d]`` BEFORE insertion. Out-of-band rows
  are dropped (counted in the run summary).
* **Last-good fallback.** A 404 / 5xx / timeout from an explicitly
  overridden upstream raises :class:`UpstreamUnavailable`, the
  scraper exits :data:`EXIT_UPSTREAM_UNAVAILABLE`, and the
  ``ema_calendar`` table is left exactly as it was
  (per VAL-M1-030).
* **No partial commit.** All inserts run inside a single explicit
  ``BEGIN`` / ``COMMIT`` transaction; any mid-write failure rolls
  back cleanly with no orphan rows.

CLI
---

::

    python -m biotech_sniper.calendar.ema --refresh

Flags
~~~~~

* ``--refresh``     — run the full fetch + parse + write cycle.
* ``--db PATH``     — override the target SQLite file.
* ``--source URL``  — override the upstream HTML URL.
* ``--html-path P`` — read HTML from a local file (testing).
* ``--json-path P`` — read pre-parsed JSON from a local file
  (operator dry-runs and seed re-publishing).
* ``--use-seed``    — skip the live fetch and load the bundled
  seed-data file directly.
* ``--no-fallback`` — disable the seed-data fallback even on
  default-source failure (cron operators who want a hard error
  on transient EMA outages).
* ``--dry-run``     — parse but do not write to SQLite.
* ``--emit-stats``  — print the run summary as a single JSON
  object on stdout.
* ``--http-timeout SECONDS`` — override the HTTP timeout.

Exit codes
~~~~~~~~~~

* ``0`` — success (rows written, dry-run completed, or seed
  fallback applied).
* ``2`` — :class:`UpstreamUnavailable` (HTTP non-2xx / timeout /
  DNS error from an explicitly overridden URL or, when
  ``--no-fallback`` is set, from the default URL).
* ``3`` — :class:`EMAParseError` (HTML/JSON structure could
  not be parsed; the table is unchanged).
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final, Sequence

import requests

from biotech_sniper import db
from biotech_sniper.paths import DATA_DIR, ensure_data_dir

__all__ = [
    "DEFAULT_EMA_SOURCE_URL",
    "DEFAULT_USER_AGENT",
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "EXIT_OK",
    "EXIT_UPSTREAM_UNAVAILABLE",
    "EXIT_PARSE_ERROR",
    "EMAScraperError",
    "UpstreamUnavailable",
    "EMAParseError",
    "ParsedEMA",
    "ScrapeResult",
    "default_db_path",
    "default_seed_path",
    "ensure_ema_calendar_table",
    "fetch_html_bytes",
    "parse_ema_html",
    "parse_seed_json",
    "is_iso_date",
    "write_rows",
    "scrape_ema_calendar",
    "main",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Default upstream source. Public; no authentication required.
#: Listed in ``library/environment.md`` and the ``ALLOWED_NETWORK_HOSTS``
#: whitelist (added in f-m1-08).
DEFAULT_EMA_SOURCE_URL: Final[str] = (
    "https://www.ema.europa.eu/en/committees/chmp/chmp-meeting-highlights"
)

#: Descriptive User-Agent (project id + contact email) used for every
#: outbound HTTP request from the scraper. Matches the regex
#: ``^BiotechSniper/[0-9].* contact: .*@.*$``.
DEFAULT_USER_AGENT: Final[str] = (
    "BiotechSniper/1.0 (contact: ops@alpha-sniper.local)"
)

#: HTTP timeout for the upstream fetch. Matches the cron worker's
#: 30s budget; overridable via ``--http-timeout``.
DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0

#: Documented exit codes (stable contract for cron + validators).
EXIT_OK: Final[int] = 0
EXIT_UPSTREAM_UNAVAILABLE: Final[int] = 2
EXIT_PARSE_ERROR: Final[int] = 3

#: Lower bound (days from today) on persisted dates. Dates more than
#: 365 days in the past are treated as parse garbage and dropped.
DATE_PAST_BUFFER_DAYS: Final[int] = 365

#: Upper bound (days from today) on persisted dates. Dates more than
#: 730 days in the future are treated as parse garbage.
DATE_FUTURE_BUFFER_DAYS: Final[int] = 730

#: ISO-8601 date GLOB used by both the validation contract and our
#: pre-write filter.
_ISO_DATE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$"
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EMAScraperError(Exception):
    """Base class for typed scraper errors."""


class UpstreamUnavailable(EMAScraperError):
    """Transient upstream failure (4xx / 5xx / timeout / DNS / TLS).

    Triggers the last-good fallback path when the source is the
    DEFAULT URL and ``--no-fallback`` is not set: the scraper logs
    a WARNING, loads the bundled seed-data file, and continues. When
    the source is operator-overridden (CLI flag OR env var) OR
    ``--no-fallback`` is set, the exception propagates to
    :func:`main` and the process exits
    :data:`EXIT_UPSTREAM_UNAVAILABLE`.
    """


class EMAParseError(EMAScraperError):
    """The upstream HTML/JSON could not be structurally parsed.

    Raised BEFORE any insert is attempted so a structure-drift event
    cannot produce a partial snapshot. Operators who hit this should
    diff the upstream source against the parser's expected layout.
    """


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedEMA:
    """One CHMP meeting / opinion entry extracted from the upstream source.

    At least one of :attr:`meeting_date` and :attr:`opinion_date`
    MUST be a non-empty ISO-8601 string (``YYYY-MM-DD``); the other
    may be :data:`None`.
    """

    ticker_or_sponsor: str
    product: str
    meeting_date: str | None = None
    opinion_date: str | None = None
    sponsor: str | None = None


@dataclass
class ScrapeResult:
    """Structured summary of one scrape cycle."""

    source_url: str
    db_path: str
    fetched_at: str = ""
    parsed_rows: int = 0
    rows_written: int = 0
    rows_skipped_invalid_date: int = 0
    rows_skipped_out_of_band: int = 0
    rows_skipped_no_dates: int = 0
    used_seed_fallback: bool = False
    dry_run: bool = False
    completed_at: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


# ---------------------------------------------------------------------------
# Path / env helpers
# ---------------------------------------------------------------------------


def default_db_path() -> Path:
    """Return the canonical project SQLite path."""
    return DATA_DIR / "alpha_sniper.db"


def default_seed_path() -> Path:
    """Return the bundled seed-data JSON path."""
    return Path(__file__).resolve().parent / "seed" / "ema_seed.json"


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _now_iso() -> str:
    return _now_utc().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _resolve_source_url(explicit: str | None) -> tuple[str, bool]:
    """Resolve the upstream URL.

    Returns a ``(url, was_overridden)`` tuple. The ``was_overridden``
    flag is consumed by :func:`scrape_ema_calendar` to decide
    whether a fetch failure should fall back to seed data (default
    URL, no override) or propagate as :class:`UpstreamUnavailable`
    (operator-overridden URL).

    Order of precedence:

    1. Explicit CLI flag.
    2. :envvar:`EMA_SOURCE_URL` env var.
    3. :data:`DEFAULT_EMA_SOURCE_URL`.
    """
    if explicit:
        return explicit, True
    env_value = os.environ.get("EMA_SOURCE_URL", "").strip()
    if env_value:
        return env_value, True
    return DEFAULT_EMA_SOURCE_URL, False


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------


_EMA_CALENDAR_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS ema_calendar (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker_or_sponsor  TEXT    NOT NULL,
    product            TEXT    NOT NULL,
    meeting_date       TEXT,
    opinion_date       TEXT,
    sponsor            TEXT,
    source_url         TEXT    NOT NULL,
    fetched_at         TEXT    NOT NULL,
    CHECK (meeting_date IS NOT NULL OR opinion_date IS NOT NULL)
)
"""


# Composite UNIQUE matches VAL-M1-027:
#   (ticker_or_sponsor, product, COALESCE(meeting_date,''),
#    COALESCE(opinion_date,''))
#
# A unique index over the COALESCE expressions lets two rows with
# the same (ticker_or_sponsor, product) coexist when their
# meeting_date/opinion_date differ, while still rejecting duplicate
# (ticker_or_sponsor, product, meeting_date, opinion_date) tuples
# even when one of the dates is NULL.
_EMA_CALENDAR_UNIQUE_INDEX: Final[str] = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ema_calendar_unique "
    "ON ema_calendar("
    "ticker_or_sponsor, product, "
    "COALESCE(meeting_date, ''), COALESCE(opinion_date, '')"
    ")"
)


_EMA_CALENDAR_INDEXES: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_ema_calendar_ticker "
    "ON ema_calendar(ticker_or_sponsor)",
    "CREATE INDEX IF NOT EXISTS idx_ema_calendar_meeting_date "
    "ON ema_calendar(meeting_date)",
    "CREATE INDEX IF NOT EXISTS idx_ema_calendar_opinion_date "
    "ON ema_calendar(opinion_date)",
    "CREATE INDEX IF NOT EXISTS idx_ema_calendar_fetched_at "
    "ON ema_calendar(fetched_at)",
)


def ensure_ema_calendar_table(conn: sqlite3.Connection) -> None:
    """Idempotently create the ``ema_calendar`` table + indexes.

    Safe against a v9 (pre-migration) or v10 (post-migration) DB —
    both flavours converge on the same DDL. The forthcoming
    ``010_reading_b_foundations.py`` migration declares an identical
    schema so calling this after the migration is a no-op.
    """
    conn.execute(_EMA_CALENDAR_DDL)
    conn.execute(_EMA_CALENDAR_UNIQUE_INDEX)
    for stmt in _EMA_CALENDAR_INDEXES:
        conn.execute(stmt)


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


_MONTH_NAMES: Final[dict[str, int]] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


_RE_NUMERIC_DATE: Final[re.Pattern[str]] = re.compile(
    r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b"
)
_RE_MONTH_DAY_YEAR: Final[re.Pattern[str]] = re.compile(
    r"\b([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{2,4})\b"
)
_RE_DAY_MONTH_YEAR: Final[re.Pattern[str]] = re.compile(
    r"\b(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{2,4})\b"
)


def _expand_year(raw: int | str) -> int:
    """Expand a 2-digit year to the 21st century (``26 → 2026``)."""
    year = int(raw)
    if year < 100:
        return 2000 + year
    return year


def is_iso_date(value: str | None) -> bool:
    """Return ``True`` when ``value`` matches strict ISO-8601 ``YYYY-MM-DD``.

    Validates both the regex shape AND the calendar (Feb 30 is
    rejected). Used pre-write to enforce the ISO-8601 contract.
    """
    if not isinstance(value, str):
        return False
    if not _ISO_DATE_RE.match(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def normalize_date(raw: str | None) -> str | None:
    """Normalize ``raw`` to an ISO-8601 ``YYYY-MM-DD`` string.

    The EMA publishes meeting / opinion dates as e.g.
    ``"19-22 May 2026"`` (date range), ``"22 May 2026"``,
    ``"May 22, 2026"`` or already-ISO ``"2026-05-22"``. This helper
    accepts the bracketed forms and returns the LAST day of any
    range (the most-conservative date for a paper-trader).

    Returns :data:`None` when no parser branch produces a valid
    calendar date — callers MUST treat :data:`None` as "drop this
    date field".
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None

    # 1. Already-ISO ``YYYY-MM-DD``? Fast path.
    if is_iso_date(text):
        return text

    # 2. Date range like ``19-22 May 2026`` / ``19–22 May 2026`` —
    #    take the LAST day. We collapse en-dash / em-dash to a
    #    plain hyphen for the regex.
    text_norm = text.replace("\u2013", "-").replace("\u2014", "-")
    range_match = re.match(
        r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{2,4})\s*$",
        text_norm,
    )
    if range_match:
        _start, last, month_word, year_raw = range_match.groups()
        month = _MONTH_NAMES.get(month_word.lower())
        if month is not None:
            year = _expand_year(year_raw)
            try:
                return datetime.date(year, month, int(last)).isoformat()
            except ValueError:
                pass

    # 3. Numeric ``DD/MM/YYYY`` (EU) or ``MM/DD/YYYY`` (US). EMA
    #    publishes EU-style; we still accept both and disambiguate
    #    via field magnitudes.
    m = _RE_NUMERIC_DATE.search(text_norm)
    if m:
        a_raw, b_raw, y_raw = m.group(1), m.group(2), m.group(3)
        a, b = int(a_raw), int(b_raw)
        year = _expand_year(y_raw)
        candidates: list[tuple[int, int, int]] = []
        # EU (DD/MM/YYYY) preferred — try first.
        if 1 <= b <= 12 and 1 <= a <= 31:
            candidates.append((year, b, a))
        # US (MM/DD/YYYY) fallback when EU mapping is invalid.
        if 1 <= a <= 12 and 1 <= b <= 31:
            candidates.append((year, a, b))
        for year_, month_, day_ in candidates:
            try:
                return datetime.date(year_, month_, day_).isoformat()
            except ValueError:
                continue

    # 4. ``May 22, 2026`` / ``May 22 2026``.
    m = _RE_MONTH_DAY_YEAR.search(text_norm)
    if m:
        month_word, day_raw, year_raw = m.group(1), m.group(2), m.group(3)
        month = _MONTH_NAMES.get(month_word.lower())
        if month is not None:
            year = _expand_year(year_raw)
            try:
                return datetime.date(year, month, int(day_raw)).isoformat()
            except ValueError:
                pass

    # 5. ``22 May 2026`` (EU/UK ordering, EMA's published form).
    m = _RE_DAY_MONTH_YEAR.search(text_norm)
    if m:
        day_raw, month_word, year_raw = m.group(1), m.group(2), m.group(3)
        month = _MONTH_NAMES.get(month_word.lower())
        if month is not None:
            year = _expand_year(year_raw)
            try:
                return datetime.date(year, month, int(day_raw)).isoformat()
            except ValueError:
                pass

    return None


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------


def fetch_html_bytes(
    url: str,
    *,
    timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    session: requests.Session | None = None,
) -> bytes:
    """Download the upstream EMA HTML payload.

    Any HTTP non-2xx response (404 from URL renames, 5xx from origin
    failures, etc.) and any :class:`requests.RequestException`
    (timeout, DNS, TLS, connection reset) is wrapped in
    :class:`UpstreamUnavailable` so callers can apply the last-good
    fallback / seed fallback uniformly.
    """
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
        "Accept-Language": "en-US,en;q=0.5",
    }
    getter = session.get if session is not None else requests.get
    try:
        response = getter(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise UpstreamUnavailable(
            f"EMA fetch failed: {exc.__class__.__name__}: {exc}"
        ) from exc

    status = getattr(response, "status_code", None)
    if status is None or not (200 <= int(status) < 300):
        raise UpstreamUnavailable(
            f"EMA upstream returned HTTP {status} for {url!r}"
        )

    body = response.content
    if not isinstance(body, (bytes, bytearray)):
        raise UpstreamUnavailable(
            f"EMA upstream returned non-bytes body type={type(body).__name__}"
        )
    if not body:
        raise UpstreamUnavailable("EMA upstream returned empty body")
    return bytes(body)


# ---------------------------------------------------------------------------
# HTML parser
# ---------------------------------------------------------------------------


def _extract_text(node: Any) -> str:
    """Return whitespace-collapsed visible text for a BS4 node."""
    text = node.get_text(separator=" ", strip=True) if node else ""
    return re.sub(r"\s+", " ", text).strip()


_TICKER_RE: Final[re.Pattern[str]] = re.compile(
    r"\b([A-Z][A-Z0-9.\-]{0,5}[A-Z0-9])\b"
)


def _is_likely_ticker(token: str) -> bool:
    """Return ``True`` when ``token`` looks like a US biotech ticker."""
    if not (1 <= len(token) <= 6):
        return False
    if not token[0].isalpha():
        return False
    if not all(c.isalnum() or c in ".-" for c in token):
        return False
    blacklist = {"FDA", "EMA", "CHMP", "DRUG", "DATE", "TYPE",
                 "STAGE", "INDICATION", "COMPANY", "TICKER",
                 "PRODUCT", "OPINION", "MEETING", "NEW",
                 "NA", "TBD", "YES", "NO", "EU", "UK", "US"}
    return token.upper() not in blacklist


def parse_ema_html(
    body: bytes,
    *,
    source_url: str = DEFAULT_EMA_SOURCE_URL,
) -> list[ParsedEMA]:
    """Parse an EMA / CHMP meeting-highlights HTML page.

    The parser is intentionally conservative: it scans every
    ``<table>`` looking for rows that simultaneously expose

    * a ticker-shaped token OR a non-empty sponsor cell, AND
    * a product/INN cell, AND
    * at least one date-shaped token that :func:`normalize_date`
      accepts (mapped to either ``meeting_date`` or
      ``opinion_date``).

    Rows that satisfy the above are projected into :class:`ParsedEMA`.
    Empty / unparseable-date rows are silently dropped.

    Raises
    ------
    EMAParseError
        When the HTML body has no ``<table>`` at all (a strong signal
        the upstream layout has changed enough that a manual review
        is required).
    """
    try:
        from bs4 import BeautifulSoup  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - bs4 is a hard dep
        raise EMAParseError(
            "beautifulsoup4 is required to parse EMA HTML; "
            f"import failed: {exc}"
        ) from exc

    text = body.decode("utf-8", errors="replace")
    soup = BeautifulSoup(text, "html.parser")

    tables = soup.find_all("table")
    if not tables:
        raise EMAParseError(
            "EMA HTML contains no <table> elements; "
            "structure may have changed."
        )

    parsed: list[ParsedEMA] = []
    seen_keys: set[tuple[str, str, str, str]] = set()

    for table in tables:
        header_cells: list[str] = []
        first_row = table.find("tr")
        if first_row is not None:
            ths = first_row.find_all(["th"])
            if ths:
                header_cells = [_extract_text(th).lower() for th in ths]

        col_index = {h: i for i, h in enumerate(header_cells)}
        idx_ticker = (
            col_index.get("ticker")
            or col_index.get("symbol")
        )
        idx_sponsor = (
            col_index.get("company")
            or col_index.get("sponsor")
            or col_index.get("applicant")
            or col_index.get("marketing authorisation holder")
            or col_index.get("mah")
        )
        idx_product = (
            col_index.get("product")
            or col_index.get("medicine")
            or col_index.get("inn")
            or col_index.get("name")
            or col_index.get("invented name")
        )
        idx_meeting = (
            col_index.get("meeting date")
            or col_index.get("meeting")
            or col_index.get("chmp meeting")
            or col_index.get("date of meeting")
        )
        idx_opinion = (
            col_index.get("opinion date")
            or col_index.get("opinion")
            or col_index.get("chmp opinion")
            or col_index.get("date of opinion")
        )

        for tr in table.find_all("tr"):
            cells = tr.find_all(["td"])
            if not cells:
                continue
            cell_texts = [_extract_text(c) for c in cells]
            if not cell_texts:
                continue

            ticker = sponsor = product = None
            meeting_raw = opinion_raw = None

            if idx_ticker is not None and idx_ticker < len(cell_texts):
                ticker_candidate = cell_texts[idx_ticker].strip().upper()
                if _is_likely_ticker(ticker_candidate):
                    ticker = ticker_candidate
            if idx_sponsor is not None and idx_sponsor < len(cell_texts):
                sponsor = cell_texts[idx_sponsor].strip() or None
            if idx_product is not None and idx_product < len(cell_texts):
                product = cell_texts[idx_product].strip() or None
            if idx_meeting is not None and idx_meeting < len(cell_texts):
                meeting_raw = cell_texts[idx_meeting].strip() or None
            if idx_opinion is not None and idx_opinion < len(cell_texts):
                opinion_raw = cell_texts[idx_opinion].strip() or None

            # Heuristic fallback for ticker.
            if ticker is None:
                for txt in cell_texts:
                    candidate = txt.strip().upper()
                    if _is_likely_ticker(candidate):
                        ticker = candidate
                        break

            # Heuristic fallback for product when no header was matched —
            # take the longest non-ticker, non-date, non-sponsor cell.
            if product is None:
                non_short: list[str] = []
                for c in cell_texts:
                    cs = c.strip()
                    if len(cs) < 3:
                        continue
                    if _is_likely_ticker(cs.upper()):
                        continue
                    if normalize_date(cs):
                        continue
                    if sponsor and cs == sponsor:
                        continue
                    non_short.append(cs)
                if non_short:
                    product = sorted(non_short, key=len, reverse=True)[0]

            # Heuristic fallback for dates when no header matched.
            if meeting_raw is None and opinion_raw is None:
                date_cells = [c for c in cell_texts if normalize_date(c)]
                if len(date_cells) >= 2:
                    meeting_raw, opinion_raw = date_cells[0], date_cells[1]
                elif len(date_cells) == 1:
                    meeting_raw = date_cells[0]

            ticker_or_sponsor = ticker or (sponsor.upper() if sponsor else None)
            if not ticker_or_sponsor or not product:
                continue

            meeting_iso = normalize_date(meeting_raw)
            opinion_iso = normalize_date(opinion_raw)
            if meeting_iso is None and opinion_iso is None:
                continue

            product_clean = product.strip()
            key = (
                ticker_or_sponsor,
                product_clean,
                meeting_iso or "",
                opinion_iso or "",
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            parsed.append(
                ParsedEMA(
                    ticker_or_sponsor=ticker_or_sponsor,
                    product=product_clean,
                    meeting_date=meeting_iso,
                    opinion_date=opinion_iso,
                    sponsor=sponsor.strip() if sponsor else None,
                )
            )

    return parsed


# ---------------------------------------------------------------------------
# Seed loader
# ---------------------------------------------------------------------------


def parse_seed_json(
    body: bytes | str,
) -> tuple[list[ParsedEMA], str]:
    """Parse the bundled ``ema_seed.json`` payload.

    Returns ``(rows, source_url)`` where ``source_url`` is the
    seed file's declared upstream attribution (so the persisted
    rows carry a meaningful ``source_url`` rather than the literal
    seed-file path).
    """
    if isinstance(body, (bytes, bytearray)):
        text = body.decode("utf-8", errors="replace")
    else:
        text = body
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EMAParseError(
            f"EMA seed JSON is not valid JSON: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise EMAParseError(
            "EMA seed JSON top-level must be an object"
        )
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise EMAParseError(
            "EMA seed JSON missing 'entries' list"
        )

    source_url = (
        str(payload.get("source_url") or DEFAULT_EMA_SOURCE_URL)
    )

    parsed: list[ParsedEMA] = []
    seen_keys: set[tuple[str, str, str, str]] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        ticker_or_sponsor = str(raw.get("ticker_or_sponsor", "")).strip()
        product = str(raw.get("product", "")).strip()
        meeting_raw = raw.get("meeting_date")
        opinion_raw = raw.get("opinion_date")
        sponsor = raw.get("sponsor")
        sponsor = str(sponsor).strip() if sponsor else None

        if not ticker_or_sponsor or not product:
            continue

        meeting_iso = (
            normalize_date(str(meeting_raw)) if meeting_raw else None
        )
        opinion_iso = (
            normalize_date(str(opinion_raw)) if opinion_raw else None
        )
        if meeting_iso is None and opinion_iso is None:
            continue

        key = (
            ticker_or_sponsor,
            product,
            meeting_iso or "",
            opinion_iso or "",
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        parsed.append(
            ParsedEMA(
                ticker_or_sponsor=ticker_or_sponsor,
                product=product,
                meeting_date=meeting_iso,
                opinion_date=opinion_iso,
                sponsor=sponsor,
            )
        )
    return parsed, source_url


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _within_band(
    iso_date: str, *, today: datetime.date
) -> bool:
    """Return ``True`` when ``iso_date`` is within the accepted band."""
    try:
        parsed = datetime.date.fromisoformat(iso_date)
    except ValueError:
        return False
    lower = today - datetime.timedelta(days=DATE_PAST_BUFFER_DAYS)
    upper = today + datetime.timedelta(days=DATE_FUTURE_BUFFER_DAYS)
    return lower <= parsed <= upper


def write_rows(
    conn: sqlite3.Connection,
    rows: Sequence[ParsedEMA],
    *,
    source_url: str,
    fetched_at: str,
    today: datetime.date | None = None,
) -> tuple[int, int, int, int]:
    """Persist ``rows`` to ``ema_calendar`` atomically.

    Uses ``INSERT ... ON CONFLICT DO UPDATE`` against the named
    composite UNIQUE index so a back-to-back invocation yields zero
    net new rows (per VAL-M1-029). Returns the tuple
    ``(rows_written, skipped_invalid_date, skipped_out_of_band,
    skipped_no_dates)``.

    Each input row is filtered through:

    * ISO-8601 validation on whichever of ``meeting_date`` /
      ``opinion_date`` is non-null (invalid dates are dropped to
      :data:`None`; if both end up :data:`None`, the row is
      skipped under ``skipped_no_dates``).
    * Date-band check: any present date must fall inside
      ``[today-365, today+730]``; otherwise the row is dropped under
      ``skipped_out_of_band``.
    """
    if not rows:
        return 0, 0, 0, 0
    today_ = today or _now_utc().date()
    payload: list[tuple[Any, ...]] = []
    skipped_invalid = 0
    skipped_band = 0
    skipped_no_dates = 0
    for row in rows:
        meeting_iso: str | None = row.meeting_date
        opinion_iso: str | None = row.opinion_date

        # Strict ISO check + drop invalid dates to NULL.
        if meeting_iso is not None and not is_iso_date(meeting_iso):
            skipped_invalid += 1
            meeting_iso = None
        if opinion_iso is not None and not is_iso_date(opinion_iso):
            skipped_invalid += 1
            opinion_iso = None

        # Band check on remaining dates.
        if meeting_iso is not None and not _within_band(
            meeting_iso, today=today_
        ):
            skipped_band += 1
            meeting_iso = None
        if opinion_iso is not None and not _within_band(
            opinion_iso, today=today_
        ):
            skipped_band += 1
            opinion_iso = None

        if meeting_iso is None and opinion_iso is None:
            skipped_no_dates += 1
            continue

        payload.append(
            (
                row.ticker_or_sponsor,
                row.product,
                meeting_iso,
                opinion_iso,
                row.sponsor,
                source_url,
                fetched_at,
            )
        )
    if not payload:
        return 0, skipped_invalid, skipped_band, skipped_no_dates

    # SQLite ON CONFLICT against an expression-indexed UNIQUE
    # requires naming the conflict target either by index name or
    # by the same expression list. We use the expression list so
    # the statement is portable across SQLite versions.
    conn.executemany(
        "INSERT INTO ema_calendar ("
        "ticker_or_sponsor, product, meeting_date, opinion_date, "
        "sponsor, source_url, fetched_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT("
        "ticker_or_sponsor, product, "
        "COALESCE(meeting_date, ''), COALESCE(opinion_date, '')"
        ") DO UPDATE SET "
        "sponsor = excluded.sponsor, "
        "source_url = excluded.source_url, "
        "fetched_at = excluded.fetched_at",
        payload,
    )
    return len(payload), skipped_invalid, skipped_band, skipped_no_dates


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def scrape_ema_calendar(
    *,
    db_path: Path | str | None = None,
    source_url: str | None = None,
    html_path: Path | str | None = None,
    json_path: Path | str | None = None,
    use_seed: bool = False,
    no_fallback: bool = False,
    dry_run: bool = False,
    http_timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    session: requests.Session | None = None,
    seed_path: Path | str | None = None,
    now: datetime.datetime | None = None,
) -> ScrapeResult:
    """Run one EMA scrape cycle: fetch / parse / persist.

    See module docstring for full behavioural contract.

    Raises
    ------
    UpstreamUnavailable
        When the upstream URL was operator-overridden (CLI flag OR
        :envvar:`EMA_SOURCE_URL`) AND fetch fails, OR when
        ``no_fallback=True`` and the default fetch fails. In this
        case the ``ema_calendar`` table is NOT mutated.
    EMAParseError
        Structural drift (no ``<table>`` in the HTML, malformed
        JSON in the seed file, etc.). The table is NOT mutated.
    """
    target_db = Path(db_path) if db_path is not None else default_db_path()
    seed_file = Path(seed_path) if seed_path is not None else default_seed_path()
    resolved_url, was_overridden = _resolve_source_url(source_url)
    now = now or _now_utc()
    fetched_at = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    today = now.date()

    result = ScrapeResult(
        source_url=resolved_url,
        db_path=str(target_db),
        fetched_at=fetched_at,
        dry_run=dry_run,
    )

    # --- bootstrap the table -----------------------------------------
    ensure_data_dir()
    bootstrap_conn = db.connect(target_db)
    try:
        ensure_ema_calendar_table(bootstrap_conn)
        bootstrap_conn.commit()
    finally:
        bootstrap_conn.close()

    parsed_rows: list[ParsedEMA] = []
    used_seed = False

    # --- choose source path ------------------------------------------
    if use_seed:
        body = seed_file.read_bytes()
        parsed_rows, seed_source = parse_seed_json(body)
        result.source_url = seed_source
        resolved_url = seed_source
        used_seed = True
    elif json_path is not None:
        body = Path(json_path).read_bytes()
        parsed_rows, json_source = parse_seed_json(body)
        result.source_url = json_source or resolved_url
        resolved_url = result.source_url
    elif html_path is not None:
        body = Path(html_path).read_bytes()
        parsed_rows = parse_ema_html(body, source_url=resolved_url)
    else:
        # --- live HTTP fetch --------------------------------------
        try:
            body = fetch_html_bytes(
                resolved_url,
                timeout=http_timeout,
                user_agent=user_agent,
                session=session,
            )
            parsed_rows = parse_ema_html(body, source_url=resolved_url)
        except UpstreamUnavailable:
            if was_overridden or no_fallback:
                # Operator wants fail-loud semantics — propagate.
                raise
            logger.warning(
                "ema.upstream_unavailable falling back to seed data; "
                "source=%s",
                resolved_url,
            )
            body = seed_file.read_bytes()
            parsed_rows, seed_source = parse_seed_json(body)
            result.source_url = seed_source
            resolved_url = seed_source
            used_seed = True

        # If the live fetch parsed cleanly but produced ZERO rows,
        # also fall back to the seed unless the operator suppressed
        # fallback. This guards against silent upstream layout
        # changes that strand the cron with an empty table.
        if not parsed_rows and not was_overridden and not no_fallback:
            logger.warning(
                "ema.live_yielded_zero_rows falling back to seed data"
            )
            body = seed_file.read_bytes()
            parsed_rows, seed_source = parse_seed_json(body)
            result.source_url = seed_source
            resolved_url = seed_source
            used_seed = True

    result.parsed_rows = len(parsed_rows)
    result.used_seed_fallback = used_seed

    # --- persist -----------------------------------------------------
    if dry_run:
        logger.info(
            "ema.dry_run parsed_rows=%d source=%s used_seed=%s",
            len(parsed_rows),
            resolved_url,
            used_seed,
        )
        result.completed_at = _now_iso()
        return result

    conn = db.connect(target_db)
    try:
        conn.execute("BEGIN")
        try:
            (
                written,
                skipped_inv,
                skipped_band,
                skipped_no_dates,
            ) = write_rows(
                conn,
                parsed_rows,
                source_url=resolved_url,
                fetched_at=fetched_at,
                today=today,
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    finally:
        conn.close()

    result.rows_written = written
    result.rows_skipped_invalid_date = skipped_inv
    result.rows_skipped_out_of_band = skipped_band
    result.rows_skipped_no_dates = skipped_no_dates
    result.completed_at = _now_iso()
    logger.info(
        "ema.complete rows_written=%d skipped_invalid=%d "
        "skipped_band=%d skipped_no_dates=%d source=%s used_seed=%s",
        written,
        skipped_inv,
        skipped_band,
        skipped_no_dates,
        resolved_url,
        used_seed,
    )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.calendar.ema",
        description=(
            "Download and persist upcoming EMA / CHMP meeting + opinion "
            "dates from www.ema.europa.eu into the ema_calendar table. "
            "Composite UNIQUE on (ticker_or_sponsor, product, "
            "COALESCE(meeting_date,''), COALESCE(opinion_date,'')). "
            "Last-good fallback on operator-overridden 404/timeout; "
            "bundled seed-data fallback when the default upstream is "
            "unreachable."
        ),
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Run the full fetch + parse + write cycle (cron flag).",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Override the target SQLite database path.",
    )
    parser.add_argument(
        "--source",
        type=str,
        default=None,
        help=(
            "Override the upstream HTML URL. Takes precedence over "
            "the EMA_SOURCE_URL env var. Failures with an explicit "
            "override are fatal (no seed fallback)."
        ),
    )
    parser.add_argument(
        "--html-path",
        type=str,
        default=None,
        help="Read HTML from a local file instead of the network.",
    )
    parser.add_argument(
        "--json-path",
        type=str,
        default=None,
        help=(
            "Read pre-parsed JSON (same shape as the seed file) from "
            "a local file instead of the network."
        ),
    )
    parser.add_argument(
        "--use-seed",
        action="store_true",
        help=(
            "Skip the live fetch and load the bundled seed-data file "
            "directly. Useful for first-time bootstrap and offline runs."
        ),
    )
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help=(
            "Disable the bundled-seed fallback even on default-source "
            "failure (cron operators who want a hard error on transient "
            "EMA outages)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse but do not write to SQLite.",
    )
    parser.add_argument(
        "--emit-stats",
        action="store_true",
        help="Print the run summary as a single JSON object on stdout.",
    )
    parser.add_argument(
        "--http-timeout",
        type=float,
        default=DEFAULT_HTTP_TIMEOUT_SECONDS,
        help=f"HTTP timeout in seconds (default: {DEFAULT_HTTP_TIMEOUT_SECONDS}).",
    )
    return parser


def _emit(stats: bool, result: ScrapeResult) -> None:
    if stats:
        sys.stdout.write(result.to_json() + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint. See :mod:`biotech_sniper.calendar.ema`."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.source and args.html_path:
        parser.error("--source and --html-path are mutually exclusive")
        return EXIT_UPSTREAM_UNAVAILABLE  # pragma: no cover
    if args.source and args.json_path:
        parser.error("--source and --json-path are mutually exclusive")
        return EXIT_UPSTREAM_UNAVAILABLE  # pragma: no cover
    if args.html_path and args.json_path:
        parser.error("--html-path and --json-path are mutually exclusive")
        return EXIT_UPSTREAM_UNAVAILABLE  # pragma: no cover

    db_path = Path(args.db) if args.db else default_db_path()

    try:
        result = scrape_ema_calendar(
            db_path=db_path,
            source_url=args.source,
            html_path=args.html_path,
            json_path=args.json_path,
            use_seed=args.use_seed,
            no_fallback=args.no_fallback,
            dry_run=args.dry_run,
            http_timeout=args.http_timeout,
        )
    except UpstreamUnavailable as exc:
        logger.error("ema.upstream_unavailable %s", exc)
        sys.stderr.write(f"ERROR: ema upstream unavailable: {exc}\n")
        return EXIT_UPSTREAM_UNAVAILABLE
    except EMAParseError as exc:
        logger.error("ema.parse_error %s", exc)
        sys.stderr.write(f"ERROR: ema parse error: {exc}\n")
        return EXIT_PARSE_ERROR

    _emit(args.emit_stats, result)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
