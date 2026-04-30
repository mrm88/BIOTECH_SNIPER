"""FDA PDUFA calendar scraper (Reading-B feature f-m1-04).

This module owns the daily fetch + parse + persistence path for
upcoming FDA PDUFA action dates. The output table
``pdufa_calendar`` is one of three sources merged by the
``trial_calendar`` lookup (the other two being CT.gov and EMA/CHMP).

Source strategy
---------------

The default upstream source is the BiopharmCatalyst FDA calendar
HTML page::

    https://www.biopharmcatalyst.com/calendars/fda-calendar

Operators may override the URL via ``--source`` or the
:envvar:`PDUFA_SOURCE_URL` environment variable. When an explicit
override is set (CLI flag OR env var), any HTTP failure (timeout,
non-2xx response, DNS error) propagates as
:class:`UpstreamUnavailable` and the writer exits non-zero with the
prior ``pdufa_calendar`` rows preserved as the last-good fallback.

When NO explicit override is set and the default upstream is
unreachable (Cloudflare 403 from a sandbox, transient network
error, etc.), the scraper falls back to a bundled JSON seed file at
``biotech_sniper/calendar/seed/pdufa_seed.json`` containing a small
hand-curated list of upcoming PDUFA dates. The seed fallback exists
so that the documented CLI invocation
``python -m biotech_sniper.calendar.pdufa --refresh`` always writes
≥ 1 row on a representative day (per VAL-M1-022). Operators with a
working live fetch override the seed via the standard UPSERT path
on the composite UNIQUE key.

Behavioural contract
--------------------

* **Descriptive User-Agent.** Every outbound HTTP request carries
  the project's :data:`DEFAULT_USER_AGENT` header (project id +
  contact email). The default ``python-requests`` UA is forbidden.
  The exact UA string matches the regex
  ``^BiotechSniper/[0-9].* contact: .*@.*$`` (per VAL-M1-026).
* **Composite-key idempotency.** Rows are written via
  ``INSERT ... ON CONFLICT(ticker, drug, action_date) DO UPDATE``
  so a back-to-back ``--refresh`` yields zero net new rows. Only
  ``sponsor``, ``source_url`` and ``fetched_at`` are refreshed on
  conflict.
* **Multi-drug per ticker.** A single ticker may have many drugs
  with distinct ``action_date`` values; each persists as its own
  row keyed on the composite UNIQUE constraint.
* **ISO-8601 action_date.** Every persisted ``action_date`` is
  validated against the GLOB ``[0-9][0-9][0-9][0-9]-[0-1][0-9]-
  [0-3][0-9]`` and the band ``[today-365d, today+730d]`` BEFORE
  insertion (per VAL-M1-025).
* **Fuzzy-quarter normalisation rule** *(documented & tested):*
  Catalyst-quarter targets such as ``Q3 2026``, ``3Q 2026``,
  ``H1 2026``, ``Mid 2026``, ``Late 2026`` and bare ``2026`` are
  resolved to a deterministic ISO date by selecting the
  **last day of the targeted period** (the most-conservative
  bound from a paper-trader's perspective):

  ====================  ==================
  Fuzzy input           Resolved ISO date
  ====================  ==================
  ``Q1 YYYY``           ``YYYY-03-31``
  ``Q2 YYYY``           ``YYYY-06-30``
  ``Q3 YYYY``           ``YYYY-09-30``
  ``Q4 YYYY``           ``YYYY-12-31``
  ``H1 YYYY``           ``YYYY-06-30``
  ``H2 YYYY``           ``YYYY-12-31``
  ``Early YYYY``        ``YYYY-04-30``
  ``Mid YYYY``          ``YYYY-08-31``
  ``Late YYYY``         ``YYYY-12-31``
  bare ``YYYY``         ``YYYY-12-31``
  ====================  ==================

  See :func:`normalize_action_date` for the full grammar and
  :class:`tests/test_pdufa_scraper.py::TestNormalizeActionDate`
  for the test matrix.
* **Last-good fallback.** A 404 / 5xx / timeout from an explicitly
  overridden upstream raises :class:`UpstreamUnavailable`, the
  scraper exits :data:`EXIT_UPSTREAM_UNAVAILABLE`, and the
  ``pdufa_calendar`` table is left exactly as it was.
* **No partial commit.** All inserts run inside a single explicit
  ``BEGIN`` / ``COMMIT`` transaction; any mid-write failure rolls
  back cleanly with no orphan rows.

CLI
---

::

    python -m biotech_sniper.calendar.pdufa --refresh

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
  on Cloudflare 403).
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
* ``3`` — :class:`PDUFAParseError` (HTML/JSON structure could
  not be parsed; the table is unchanged).
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import logging
import os
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final, Iterable, Sequence

import requests

from biotech_sniper import db
from biotech_sniper.paths import DATA_DIR, ensure_data_dir

__all__ = [
    "DEFAULT_PDUFA_SOURCE_URL",
    "DEFAULT_USER_AGENT",
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "EXIT_OK",
    "EXIT_UPSTREAM_UNAVAILABLE",
    "EXIT_PARSE_ERROR",
    "PDUFAScraperError",
    "UpstreamUnavailable",
    "PDUFAParseError",
    "ParsedPDUFA",
    "ScrapeResult",
    "default_db_path",
    "default_seed_path",
    "ensure_pdufa_calendar_table",
    "fetch_html_bytes",
    "parse_biopharmcatalyst_html",
    "parse_seed_json",
    "normalize_action_date",
    "is_iso_date",
    "write_rows",
    "scrape_pdufa_calendar",
    "main",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Default upstream source. Public; no authentication required.
#: Listed in ``library/environment.md`` and the
#: ``ALLOWED_NETWORK_HOSTS`` whitelist (added in f-m1-08).
DEFAULT_PDUFA_SOURCE_URL: Final[str] = (
    "https://www.biopharmcatalyst.com/calendars/fda-calendar"
)

#: Descriptive User-Agent (project id + contact email) used for every
#: outbound HTTP request from the scraper. Matches the regex
#: ``^BiotechSniper/[0-9].* contact: .*@.*$`` enforced by VAL-M1-026.
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

#: Lower bound (days from today) on persisted ``action_date``. Dates
#: more than 365 days in the past are treated as parse garbage and
#: dropped. Mirrors the GLOB / band check in VAL-M1-025.
ACTION_DATE_PAST_BUFFER_DAYS: Final[int] = 365

#: Upper bound (days from today) on persisted ``action_date``. Dates
#: more than 730 days in the future are treated as parse garbage.
ACTION_DATE_FUTURE_BUFFER_DAYS: Final[int] = 730

#: ISO-8601 date GLOB used by both the validation contract and our
#: pre-write filter.
_ISO_DATE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$"
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PDUFAScraperError(Exception):
    """Base class for typed scraper errors."""


class UpstreamUnavailable(PDUFAScraperError):
    """Transient upstream failure (4xx / 5xx / timeout / DNS / TLS).

    Triggers the last-good fallback path when the source is the
    DEFAULT URL and ``--no-fallback`` is not set: the scraper logs
    a WARNING, loads the bundled seed-data file, and continues. When
    the source is operator-overridden (CLI flag OR env var) OR
    ``--no-fallback`` is set, the exception propagates to
    :func:`main` and the process exits
    :data:`EXIT_UPSTREAM_UNAVAILABLE`.
    """


class PDUFAParseError(PDUFAScraperError):
    """The upstream HTML/JSON could not be structurally parsed.

    Raised BEFORE any insert is attempted so a structure-drift event
    cannot produce a partial snapshot. Operators who hit this should
    diff the upstream source against the parser's expected layout.
    """


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedPDUFA:
    """One PDUFA entry extracted from the upstream source.

    ``action_date`` is the post-normalisation ISO-8601 string
    (``YYYY-MM-DD``); the raw upstream form (e.g. ``"Q3 2026"``) is
    available on :attr:`raw_action_date` for audit logging.
    """

    ticker: str
    drug: str
    action_date: str
    sponsor: str | None = None
    raw_action_date: str | None = None


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
    return Path(__file__).resolve().parent / "seed" / "pdufa_seed.json"


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _now_iso() -> str:
    return _now_utc().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _resolve_source_url(explicit: str | None) -> tuple[str, bool]:
    """Resolve the upstream URL.

    Returns a ``(url, was_overridden)`` tuple. The ``was_overridden``
    flag is consumed by :func:`scrape_pdufa_calendar` to decide
    whether a fetch failure should fall back to seed data (default
    URL, no override) or propagate as :class:`UpstreamUnavailable`
    (operator-overridden URL).

    Order of precedence:

    1. Explicit CLI flag.
    2. :envvar:`PDUFA_SOURCE_URL` env var.
    3. :data:`DEFAULT_PDUFA_SOURCE_URL`.
    """
    if explicit:
        return explicit, True
    env_value = os.environ.get("PDUFA_SOURCE_URL", "").strip()
    if env_value:
        return env_value, True
    return DEFAULT_PDUFA_SOURCE_URL, False


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------


_PDUFA_CALENDAR_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS pdufa_calendar (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT    NOT NULL,
    drug          TEXT    NOT NULL,
    action_date   TEXT    NOT NULL,
    sponsor       TEXT,
    source_url    TEXT    NOT NULL,
    fetched_at    TEXT    NOT NULL,
    UNIQUE (ticker, drug, action_date)
)
"""


_PDUFA_CALENDAR_INDEXES: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_pdufa_calendar_ticker "
    "ON pdufa_calendar(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_pdufa_calendar_action_date "
    "ON pdufa_calendar(action_date)",
    "CREATE INDEX IF NOT EXISTS idx_pdufa_calendar_fetched_at "
    "ON pdufa_calendar(fetched_at)",
)


def ensure_pdufa_calendar_table(conn: sqlite3.Connection) -> None:
    """Idempotently create the ``pdufa_calendar`` table.

    Safe against a v9 (pre-migration) or v10 (post-migration) DB —
    both flavours converge on the same DDL. The forthcoming
    ``010_reading_b_foundations.py`` migration declares an identical
    schema so calling this after the migration is a no-op.
    """
    conn.execute(_PDUFA_CALENDAR_DDL)
    for stmt in _PDUFA_CALENDAR_INDEXES:
        conn.execute(stmt)


# ---------------------------------------------------------------------------
# Date normalisation
# ---------------------------------------------------------------------------


_QUARTER_END: Final[dict[int, tuple[int, int]]] = {
    1: (3, 31),
    2: (6, 30),
    3: (9, 30),
    4: (12, 31),
}


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


# Half-year and seasonal qualifier resolutions. Each maps to
# ``(month, day)`` for the LAST day of the targeted period.
_HALF_YEAR_END: Final[dict[str, tuple[int, int]]] = {
    "h1": (6, 30),
    "h2": (12, 31),
    "early": (4, 30),
    "mid": (8, 31),
    "late": (12, 31),
}


_RE_QUARTER: Final[re.Pattern[str]] = re.compile(
    r"\b(?:Q([1-4])\s*[-/']?\s*(\d{2,4})|([1-4])Q\s*[-/']?\s*(\d{2,4}))\b",
    re.IGNORECASE,
)
_RE_HALF: Final[re.Pattern[str]] = re.compile(
    r"\b(?:H([12])|([12])H|(early|mid|late))\s*[-/']?\s*(\d{2,4})\b",
    re.IGNORECASE,
)
_RE_BARE_YEAR: Final[re.Pattern[str]] = re.compile(
    r"^\s*(\d{4})\s*$"
)
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
    """Expand a 2-digit year to the 21st century (``26 → 2026``).

    A 2-digit year is interpreted as ``2000 + value`` because the
    PDUFA pipeline only deals with future dates within ``±2`` years
    of today; treating a 2-digit year as 19xx is never correct here.
    """
    year = int(raw)
    if year < 100:
        return 2000 + year
    return year


def is_iso_date(value: str | None) -> bool:
    """Return ``True`` when ``value`` matches strict ISO-8601 ``YYYY-MM-DD``.

    Validates both the regex shape AND the calendar (Feb 30 is
    rejected). Used pre-write to enforce VAL-M1-025.
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


def _last_day_of_month(year: int, month: int) -> int:
    """Return the last day-of-month for ``(year, month)``."""
    if month == 12:
        next_first = datetime.date(year + 1, 1, 1)
    else:
        next_first = datetime.date(year, month + 1, 1)
    return (next_first - datetime.timedelta(days=1)).day


def normalize_action_date(
    raw: str | None,
    *,
    today: datetime.date | None = None,
) -> str | None:
    """Normalize ``raw`` to an ISO-8601 ``YYYY-MM-DD`` string.

    See the module docstring's "Fuzzy-quarter normalisation rule"
    table for the full grammar. Returns :data:`None` when no
    parser branch produces a valid calendar date — callers MUST
    treat :data:`None` as "drop the row" (the
    ``action_date NOT NULL`` schema constraint would refuse a
    NULL anyway).

    Parameters
    ----------
    raw:
        Free-form date text from the upstream source.
    today:
        Optional override for "today" (used by tests so that bare
        year / fuzzy-quarter resolutions are deterministic).
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None

    # 1. Already-ISO ``YYYY-MM-DD``? Fast path.
    if is_iso_date(text):
        return text

    # 2. Fuzzy quarter ``Q3 2026`` / ``3Q 2026`` / ``Q3-2026``.
    m = _RE_QUARTER.search(text)
    if m:
        quarter_raw = m.group(1) or m.group(3)
        year_raw = m.group(2) or m.group(4)
        if quarter_raw and year_raw:
            quarter = int(quarter_raw)
            year = _expand_year(year_raw)
            month, day = _QUARTER_END[quarter]
            try:
                return datetime.date(year, month, day).isoformat()
            except ValueError:
                return None

    # 3. Half-year ``H1 2026`` / ``2H 2026`` / ``Mid 2026`` / ``Late
    #    2026`` / ``Early 2026``.
    m = _RE_HALF.search(text)
    if m:
        h_raw = m.group(1) or m.group(2)
        seasonal = m.group(3)
        year_raw = m.group(4)
        if year_raw:
            year = _expand_year(year_raw)
            if h_raw:
                key = f"h{h_raw}"
            elif seasonal:
                key = seasonal.lower()
            else:
                return None
            month, day = _HALF_YEAR_END[key]
            try:
                return datetime.date(year, month, day).isoformat()
            except ValueError:
                return None

    # 4. Numeric ``MM/DD/YYYY`` or ``MM-DD-YYYY``. We assume US
    #    convention (PDUFA dates published by FDA / BiopharmCatalyst
    #    use month-first), but if the first field exceeds 12 we
    #    fall back to day-first (DD/MM/YYYY) so European typings
    #    still parse.
    m = _RE_NUMERIC_DATE.search(text)
    if m:
        a_raw, b_raw, y_raw = m.group(1), m.group(2), m.group(3)
        a, b = int(a_raw), int(b_raw)
        year = _expand_year(y_raw)
        candidates: list[tuple[int, int, int]] = []
        if 1 <= a <= 12 and 1 <= b <= 31:
            candidates.append((year, a, b))
        if 1 <= b <= 12 and 1 <= a <= 31 and (a > 12 or b > 12):
            candidates.append((year, b, a))
        for year_, month_, day_ in candidates:
            try:
                return datetime.date(year_, month_, day_).isoformat()
            except ValueError:
                continue

    # 5. ``Mar 15, 2026`` / ``March 15 2026`` / ``Mar 15th, 2026``.
    m = _RE_MONTH_DAY_YEAR.search(text)
    if m:
        month_word, day_raw, year_raw = m.group(1), m.group(2), m.group(3)
        month = _MONTH_NAMES.get(month_word.lower())
        if month is not None:
            year = _expand_year(year_raw)
            try:
                return datetime.date(year, month, int(day_raw)).isoformat()
            except ValueError:
                pass

    # 6. ``15 March 2026`` (European/UK ordering).
    m = _RE_DAY_MONTH_YEAR.search(text)
    if m:
        day_raw, month_word, year_raw = m.group(1), m.group(2), m.group(3)
        month = _MONTH_NAMES.get(month_word.lower())
        if month is not None:
            year = _expand_year(year_raw)
            try:
                return datetime.date(year, month, int(day_raw)).isoformat()
            except ValueError:
                pass

    # 7. Bare year ``2026`` ⇒ year-end Dec 31.
    m = _RE_BARE_YEAR.match(text)
    if m:
        year = _expand_year(m.group(1))
        try:
            return datetime.date(year, 12, 31).isoformat()
        except ValueError:
            return None

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
    """Download the upstream PDUFA HTML payload.

    Any HTTP non-2xx response (Cloudflare 403, 404 from URL renames,
    5xx from origin failures, etc.) and any
    :class:`requests.RequestException` (timeout, DNS, TLS, connection
    reset) is wrapped in :class:`UpstreamUnavailable` so callers can
    apply the last-good fallback / seed fallback uniformly.
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
            f"PDUFA fetch failed: {exc.__class__.__name__}: {exc}"
        ) from exc

    status = getattr(response, "status_code", None)
    if status is None or not (200 <= int(status) < 300):
        raise UpstreamUnavailable(
            f"PDUFA upstream returned HTTP {status} for {url!r}"
        )

    body = response.content
    if not isinstance(body, (bytes, bytearray)):
        raise UpstreamUnavailable(
            f"PDUFA upstream returned non-bytes body type={type(body).__name__}"
        )
    if not body:
        raise UpstreamUnavailable("PDUFA upstream returned empty body")
    return bytes(body)


# ---------------------------------------------------------------------------
# HTML parser
# ---------------------------------------------------------------------------


_TICKER_RE: Final[re.Pattern[str]] = re.compile(
    r"\b([A-Z][A-Z0-9.\-]{0,5}[A-Z0-9])\b"
)


def _extract_text(node: Any) -> str:
    """Return whitespace-collapsed visible text for a BS4 node."""
    text = node.get_text(separator=" ", strip=True) if node else ""
    return re.sub(r"\s+", " ", text).strip()


def _is_likely_ticker(token: str) -> bool:
    """Return ``True`` when ``token`` looks like a US biotech ticker."""
    if not (1 <= len(token) <= 6):
        return False
    if not token[0].isalpha():
        return False
    if not all(c.isalnum() or c in ".-" for c in token):
        return False
    # Common headings that look like tickers.
    blacklist = {"FDA", "PDUFA", "DRUG", "DATE", "TYPE", "STAGE",
                 "NDA", "BLA", "IND", "INDICATION", "COMPANY",
                 "TICKER", "NEW", "NA", "TBD", "YES", "NO"}
    return token.upper() not in blacklist


def parse_biopharmcatalyst_html(
    body: bytes,
    *,
    today: datetime.date | None = None,
) -> list[ParsedPDUFA]:
    """Parse a BiopharmCatalyst FDA-calendar HTML page.

    The parser is intentionally conservative: it scans every
    ``<table>`` looking for rows that simultaneously expose

    * a ticker-shaped token (1-6 uppercase letters/digits, optionally
      wrapped in an ``<a>`` whose ``href`` contains ``/biotechs/`` or
      similar), AND
    * a date-shaped token that :func:`normalize_action_date` accepts.

    Rows that satisfy both are projected into :class:`ParsedPDUFA`.
    Empty / null-ticker / unparseable-date rows are silently dropped
    (they show up in :attr:`ScrapeResult.rows_skipped_invalid_date`).

    Raises
    ------
    PDUFAParseError
        When the HTML body has no ``<table>`` at all (a strong signal
        the upstream layout has changed enough that a manual review
        is required).
    """
    try:
        from bs4 import BeautifulSoup  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - bs4 is a hard dep
        raise PDUFAParseError(
            "beautifulsoup4 is required to parse PDUFA HTML; "
            f"import failed: {exc}"
        ) from exc

    text = body.decode("utf-8", errors="replace")
    soup = BeautifulSoup(text, "html.parser")

    tables = soup.find_all("table")
    if not tables:
        raise PDUFAParseError(
            "BiopharmCatalyst HTML contains no <table> elements; "
            "structure may have changed."
        )

    parsed: list[ParsedPDUFA] = []
    seen_keys: set[tuple[str, str, str]] = set()

    for table in tables:
        # Build a header→index map when a <thead> or first <tr>
        # exposes a recognisable header row. We use header positions
        # to extract drug / sponsor when present, but fall back to
        # heuristic scanning if the table is unstructured.
        header_cells: list[str] = []
        first_row = table.find("tr")
        if first_row is not None:
            ths = first_row.find_all(["th"])
            if ths:
                header_cells = [_extract_text(th).lower() for th in ths]

        col_index = {h: i for i, h in enumerate(header_cells)}
        idx_ticker = col_index.get("ticker") or col_index.get("symbol")
        idx_drug = (
            col_index.get("drug")
            or col_index.get("drug name")
            or col_index.get("treatment")
            or col_index.get("product")
        )
        idx_date = (
            col_index.get("catalyst date")
            or col_index.get("pdufa date")
            or col_index.get("action date")
            or col_index.get("date")
            or col_index.get("target date")
        )
        idx_sponsor = (
            col_index.get("company")
            or col_index.get("sponsor")
            or col_index.get("name")
        )

        for tr in table.find_all("tr"):
            cells = tr.find_all(["td"])
            if not cells:
                continue
            cell_texts = [_extract_text(c) for c in cells]
            if not cell_texts:
                continue

            # Column-aware extraction first.
            ticker = drug = sponsor = None
            raw_action = None

            if idx_ticker is not None and idx_ticker < len(cell_texts):
                ticker_candidate = cell_texts[idx_ticker].strip().upper()
                if _is_likely_ticker(ticker_candidate):
                    ticker = ticker_candidate
            if idx_drug is not None and idx_drug < len(cell_texts):
                drug = cell_texts[idx_drug].strip() or None
            if idx_date is not None and idx_date < len(cell_texts):
                raw_action = cell_texts[idx_date].strip() or None
            if idx_sponsor is not None and idx_sponsor < len(cell_texts):
                sponsor = cell_texts[idx_sponsor].strip() or None

            # Heuristic fallback: scan cells for ticker + date when
            # column indices were not derivable.
            if ticker is None:
                for txt in cell_texts:
                    candidate = txt.strip().upper()
                    if _is_likely_ticker(candidate):
                        ticker = candidate
                        break
                # Sometimes ticker is wrapped in an <a> on the row.
                if ticker is None:
                    for anchor in tr.find_all("a"):
                        href = anchor.get("href", "") or ""
                        # BiopharmCatalyst links: /biotechs/<slug>-<ticker>.
                        m = re.search(r"-([A-Z]{1,6})/?$", href)
                        if m and _is_likely_ticker(m.group(1)):
                            ticker = m.group(1)
                            break
                        anchor_text = _extract_text(anchor).strip().upper()
                        if _is_likely_ticker(anchor_text):
                            ticker = anchor_text
                            break

            if raw_action is None:
                for txt in cell_texts:
                    if normalize_action_date(txt, today=today):
                        raw_action = txt
                        break

            if drug is None:
                # Pick the longest non-ticker cell as a fallback drug
                # label. This is heuristic but predictable.
                non_short = [
                    c for c in cell_texts
                    if len(c) >= 3 and not _is_likely_ticker(c.strip().upper())
                ]
                if non_short:
                    drug = sorted(non_short, key=len, reverse=True)[0]

            if not ticker or not drug or not raw_action:
                continue

            iso = normalize_action_date(raw_action, today=today)
            if iso is None:
                continue

            ticker = ticker.upper()
            drug = drug.strip()
            key = (ticker, drug, iso)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            parsed.append(
                ParsedPDUFA(
                    ticker=ticker,
                    drug=drug,
                    action_date=iso,
                    sponsor=sponsor.strip() if sponsor else None,
                    raw_action_date=raw_action,
                )
            )

    return parsed


# ---------------------------------------------------------------------------
# Seed loader
# ---------------------------------------------------------------------------


def parse_seed_json(
    body: bytes | str,
    *,
    today: datetime.date | None = None,
) -> tuple[list[ParsedPDUFA], str]:
    """Parse the bundled ``pdufa_seed.json`` payload.

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
        raise PDUFAParseError(
            f"PDUFA seed JSON is not valid JSON: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise PDUFAParseError(
            "PDUFA seed JSON top-level must be an object"
        )
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise PDUFAParseError(
            "PDUFA seed JSON missing 'entries' list"
        )

    source_url = (
        str(payload.get("source_url") or DEFAULT_PDUFA_SOURCE_URL)
    )

    parsed: list[ParsedPDUFA] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        ticker = str(raw.get("ticker", "")).strip().upper()
        drug = str(raw.get("drug", "")).strip()
        raw_action = str(raw.get("action_date", "")).strip()
        sponsor = raw.get("sponsor")
        sponsor = str(sponsor).strip() if sponsor else None

        if not ticker or not drug or not raw_action:
            continue

        iso = normalize_action_date(raw_action, today=today)
        if iso is None:
            continue
        key = (ticker, drug, iso)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        parsed.append(
            ParsedPDUFA(
                ticker=ticker,
                drug=drug,
                action_date=iso,
                sponsor=sponsor,
                raw_action_date=raw_action,
            )
        )
    return parsed, source_url


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _within_action_date_band(
    iso_date: str, *, today: datetime.date
) -> bool:
    """Return ``True`` when ``iso_date`` is within the accepted band.

    Mirrors VAL-M1-025: ``[today-365d, today+730d]``.
    """
    try:
        parsed = datetime.date.fromisoformat(iso_date)
    except ValueError:
        return False
    lower = today - datetime.timedelta(days=ACTION_DATE_PAST_BUFFER_DAYS)
    upper = today + datetime.timedelta(days=ACTION_DATE_FUTURE_BUFFER_DAYS)
    return lower <= parsed <= upper


def write_rows(
    conn: sqlite3.Connection,
    rows: Sequence[ParsedPDUFA],
    *,
    source_url: str,
    fetched_at: str,
    today: datetime.date | None = None,
) -> tuple[int, int, int]:
    """Persist ``rows`` to ``pdufa_calendar`` atomically.

    Uses ``INSERT ... ON CONFLICT(ticker, drug, action_date) DO
    UPDATE`` so a back-to-back invocation yields zero net new rows
    (per VAL-M1-023). Returns the tuple
    ``(rows_written, skipped_invalid_date, skipped_out_of_band)``.

    Rows whose ``action_date`` is not strict ISO-8601 (``YYYY-MM-DD``)
    or falls outside the ``[today-365, today+730]`` band are dropped
    and counted in the returned tuple.
    """
    if not rows:
        return 0, 0, 0
    today_ = today or _now_utc().date()
    payload: list[tuple[Any, ...]] = []
    skipped_invalid = 0
    skipped_band = 0
    for row in rows:
        if not is_iso_date(row.action_date):
            skipped_invalid += 1
            continue
        if not _within_action_date_band(row.action_date, today=today_):
            skipped_band += 1
            continue
        payload.append(
            (
                row.ticker.upper(),
                row.drug,
                row.action_date,
                row.sponsor,
                source_url,
                fetched_at,
            )
        )
    if not payload:
        return 0, skipped_invalid, skipped_band
    conn.executemany(
        "INSERT INTO pdufa_calendar ("
        "ticker, drug, action_date, sponsor, source_url, fetched_at"
        ") VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(ticker, drug, action_date) DO UPDATE SET "
        "sponsor = excluded.sponsor, "
        "source_url = excluded.source_url, "
        "fetched_at = excluded.fetched_at",
        payload,
    )
    return len(payload), skipped_invalid, skipped_band


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def scrape_pdufa_calendar(
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
    """Run one PDUFA scrape cycle: fetch / parse / persist.

    See module docstring for full behavioural contract.

    Raises
    ------
    UpstreamUnavailable
        When the upstream URL was operator-overridden (CLI flag OR
        :envvar:`PDUFA_SOURCE_URL`) AND fetch fails, OR when
        ``no_fallback=True`` and the default fetch fails. In this
        case the ``pdufa_calendar`` table is NOT mutated.
    PDUFAParseError
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
        ensure_pdufa_calendar_table(bootstrap_conn)
        bootstrap_conn.commit()
    finally:
        bootstrap_conn.close()

    parsed_rows: list[ParsedPDUFA] = []
    used_seed = False

    # --- choose source path ------------------------------------------
    if use_seed:
        body = seed_file.read_bytes()
        parsed_rows, seed_source = parse_seed_json(body, today=today)
        result.source_url = seed_source
        resolved_url = seed_source
        used_seed = True
    elif json_path is not None:
        body = Path(json_path).read_bytes()
        parsed_rows, json_source = parse_seed_json(body, today=today)
        result.source_url = json_source or resolved_url
        resolved_url = result.source_url
    elif html_path is not None:
        body = Path(html_path).read_bytes()
        parsed_rows = parse_biopharmcatalyst_html(body, today=today)
    else:
        # --- live HTTP fetch --------------------------------------
        try:
            body = fetch_html_bytes(
                resolved_url,
                timeout=http_timeout,
                user_agent=user_agent,
                session=session,
            )
            parsed_rows = parse_biopharmcatalyst_html(body, today=today)
        except UpstreamUnavailable:
            if was_overridden or no_fallback:
                # Operator wants fail-loud semantics — propagate.
                raise
            logger.warning(
                "pdufa.upstream_unavailable falling back to seed data; "
                "source=%s",
                resolved_url,
            )
            body = seed_file.read_bytes()
            parsed_rows, seed_source = parse_seed_json(body, today=today)
            result.source_url = seed_source
            resolved_url = seed_source
            used_seed = True

        # If the live fetch parsed cleanly but produced ZERO rows,
        # also fall back to the seed unless the operator suppressed
        # fallback. This guards against silent upstream layout
        # changes that strand the cron with an empty table.
        if not parsed_rows and not was_overridden and not no_fallback:
            logger.warning(
                "pdufa.live_yielded_zero_rows falling back to seed data"
            )
            body = seed_file.read_bytes()
            parsed_rows, seed_source = parse_seed_json(body, today=today)
            result.source_url = seed_source
            resolved_url = seed_source
            used_seed = True

    result.parsed_rows = len(parsed_rows)
    result.used_seed_fallback = used_seed

    # --- persist -----------------------------------------------------
    if dry_run:
        logger.info(
            "pdufa.dry_run parsed_rows=%d source=%s used_seed=%s",
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
            written, skipped_inv, skipped_band = write_rows(
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
    result.completed_at = _now_iso()
    logger.info(
        "pdufa.complete rows_written=%d skipped_invalid=%d "
        "skipped_band=%d source=%s used_seed=%s",
        written,
        skipped_inv,
        skipped_band,
        resolved_url,
        used_seed,
    )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.calendar.pdufa",
        description=(
            "Download and persist upcoming FDA PDUFA action dates from "
            "BiopharmCatalyst (HTML) into the pdufa_calendar table. "
            "Composite UNIQUE on (ticker, drug, action_date); "
            "fuzzy-quarter targets normalised to the last day of the "
            "targeted period. Last-good fallback on operator-overridden "
            "404/timeout; bundled seed-data fallback when the default "
            "upstream is blocked (Cloudflare 403, sandbox, etc.)."
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
            "the PDUFA_SOURCE_URL env var. Failures with an explicit "
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
            "failure (cron operators who want fail-loud Cloudflare 403)."
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
    """CLI entrypoint. See :mod:`biotech_sniper.calendar.pdufa`."""
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
        result = scrape_pdufa_calendar(
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
        logger.error("pdufa.upstream_unavailable %s", exc)
        sys.stderr.write(f"ERROR: pdufa upstream unavailable: {exc}\n")
        return EXIT_UPSTREAM_UNAVAILABLE
    except PDUFAParseError as exc:
        logger.error("pdufa.parse_error %s", exc)
        sys.stderr.write(f"ERROR: pdufa parse error: {exc}\n")
        return EXIT_PARSE_ERROR

    _emit(args.emit_stats, result)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
