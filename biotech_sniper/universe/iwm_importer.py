"""iShares IWM holdings CSV importer (Reading-B feature f-m1-01).

This module owns the daily fetch + parse + persistence path for the
iShares Russell 2000 ETF (IWM) holdings CSV. Every successful run
writes one ``iwm_holdings_snapshot`` row per ``(as_of_date, ticker)``
tuple, carrying the source URL and ``fetched_at`` provenance the
downstream ``russell2k_biotech`` writer relies on.

The importer is invoked from cron via the documented CLI::

    python -m biotech_sniper.universe.iwm_importer --refresh

and is the FIRST stage of the M1 universe pipeline (see
``library/architecture.md``). It is intentionally self-sufficient —
the table schema is created idempotently with ``CREATE TABLE IF NOT
EXISTS`` so the importer works against either a v9 (pre-migration)
or v10 (post-migration) database. The forthcoming
``010_reading_b_foundations.py`` migration declares the same DDL so
no schema drift can develop.

Behavioural contract
--------------------

* **Descriptive User-Agent.** Every outbound HTTP request carries
  the project's :data:`DEFAULT_USER_AGENT` header (project id +
  contact email). The default Python-requests UA is never used.
* **iShares preamble skipped.** The CSV opens with ~9 metadata
  rows ("Fund Holdings as of", "Inception Date", etc.). The parser
  scans for the canonical header row (must contain the columns
  ``Ticker`` and ``Asset Class``) and discards every preceding row.
* **UTF-8 BOM tolerated.** A leading ``\ufeff`` is stripped before
  parsing.
* **Fail-loud on schema drift.** Missing or renamed required
  columns raise :class:`IWMSchemaError` with the offending column
  name in the message. NO partial rows are written.
* **Last-good fallback on transient upstream failures.** Any
  HTTP 4xx/5xx response (including the iShares/Akamai 403 the
  edge sometimes returns) and any ``requests.RequestException``
  (timeout, DNS, TLS, etc.) is treated as an
  ``UpstreamUnavailable`` failure. The importer logs an ``ERROR``
  line, exits non-zero (``EXIT_UPSTREAM_UNAVAILABLE = 2``), and
  leaves the previous successful snapshot row count unchanged.
* **No partial commit.** All snapshot inserts run inside a single
  transaction; a network timeout, a parse error, or a schema
  violation rolls back cleanly with no orphan rows.

CLI flags
---------

* ``--refresh`` — perform the full HTTP fetch + parse + write
  cycle. (Equivalent to running the module without flags; explicit
  flag form is the documented cron entrypoint.)
* ``--db PATH`` — override the target SQLite file (default
  :func:`biotech_sniper.universe.iwm_importer.default_db_path`).
* ``--source URL`` / ``--csv-path PATH`` — override the upstream
  source. ``--source`` accepts an HTTPS URL; ``--csv-path``
  accepts a local file path (used by tests and operator dry-runs).
* ``--dry-run`` — parse the CSV but do not write to SQLite.
  Combine with ``--emit-stats`` to print a JSON summary suitable
  for piping to ``jq``.
* ``--emit-stats`` — emit the run summary (row counts, null
  counters, source URL) as a single JSON object to stdout.
* ``--max-age-hours N`` — short-circuit the fetch when the most
  recent snapshot row is younger than ``N`` hours. Mirrors the
  ``RUSSELL_BIOTECH_REFRESH_HOURS`` env-var override.
* ``--http-timeout SECONDS`` — override the HTTP timeout for the
  iShares fetch (default :data:`DEFAULT_HTTP_TIMEOUT_SECONDS`).
* ``--allow-shrinkage`` — accepted as a no-op here (the
  shrinkage guard lives in the russell_biotech writer); included
  so cron operators may pass through the same flag set.

Exit codes
----------

* ``0`` — success (rows written or fresh-enough cache hit).
* ``2`` — :class:`UpstreamUnavailable` (HTTP 4xx/5xx, timeout,
  DNS error, TLS error, or any other transport failure).
* ``3`` — :class:`IWMSchemaError` (missing or renamed required
  column).
"""

from __future__ import annotations

import argparse
import csv
import datetime
import io
import json
import logging
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final, Iterable, Sequence

import requests

from biotech_sniper import db
from biotech_sniper.paths import DATA_DIR, ensure_data_dir

__all__ = [
    "DEFAULT_IWM_HOLDINGS_URL",
    "DEFAULT_USER_AGENT",
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "DEFAULT_REFRESH_HOURS",
    "REQUIRED_COLUMNS",
    "EXIT_OK",
    "EXIT_UPSTREAM_UNAVAILABLE",
    "EXIT_SCHEMA_ERROR",
    "IWMImporterError",
    "UpstreamUnavailable",
    "IWMSchemaError",
    "ParsedRow",
    "ImportResult",
    "default_db_path",
    "ensure_iwm_snapshot_table",
    "fetch_csv_bytes",
    "parse_iwm_csv",
    "write_snapshot",
    "import_iwm_holdings",
    "main",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Canonical iShares Russell 2000 ETF holdings CSV URL. Public; no
#: authentication required. The URL is also referenced in
#: ``library/environment.md`` and the ``ALLOWED_NETWORK_HOSTS``
#: whitelist.
DEFAULT_IWM_HOLDINGS_URL: Final[str] = (
    "https://www.ishares.com/us/products/239710/"
    "ishares-russell-2000-etf/1467271812596.ajax"
    "?fileType=csv&fileName=IWM_holdings&dataType=fund"
)

#: Descriptive User-Agent (project id + contact email) used for
#: every outbound HTTP request. iShares/Akamai actively rejects
#: the default ``python-requests/x.y`` UA with HTTP 403, and the
#: SEC fair-access policy explicitly requires a contact email,
#: so we share the same UA across both producers.
DEFAULT_USER_AGENT: Final[str] = (
    "BiotechSniper/1.0 (contact: ops@alpha-sniper.local)"
)

#: HTTP timeout for the holdings fetch. Matches the cron worker's
#: 30s budget; overridable via ``--http-timeout``.
DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0

#: Default refresh cadence in hours. Reads
#: ``RUSSELL_BIOTECH_REFRESH_HOURS`` from the environment when set
#: (consumed by :func:`_resolve_refresh_hours`); otherwise the
#: importer always re-fetches.
DEFAULT_REFRESH_HOURS: Final[int] = 24

#: Header columns the parser MUST find in the CSV. Renamed or
#: missing columns trigger :class:`IWMSchemaError`. The order does
#: not matter — we look up by header name.
REQUIRED_COLUMNS: Final[tuple[str, ...]] = (
    "Ticker",
    "Name",
    "Asset Class",
    "Weight (%)",
)

#: Optional columns we attempt to parse when present. Missing
#: optional columns are tolerated and persisted as NULL.
_OPTIONAL_COLUMNS: Final[tuple[str, ...]] = (
    "Sector",
    "Market Value",
    "Notional Value",
    "Quantity",
    "Price",
    "Location",
    "Exchange",
)

#: Sentinel asset-class string used by iShares for cash / receivables
#: lines. Equity rows always have ``Asset Class == 'Equity'``.
EQUITY_ASSET_CLASS: Final[str] = "Equity"

#: Documented exit codes (stable contract for cron + validators).
EXIT_OK: Final[int] = 0
EXIT_UPSTREAM_UNAVAILABLE: Final[int] = 2
EXIT_SCHEMA_ERROR: Final[int] = 3


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class IWMImporterError(Exception):
    """Base class for typed importer errors."""


class UpstreamUnavailable(IWMImporterError):
    """Transient upstream failure (4xx, 5xx, timeout, DNS, TLS).

    Triggers the last-good fallback path: the importer logs an
    ``ERROR`` line, leaves the prior snapshot rows untouched, and
    exits ``EXIT_UPSTREAM_UNAVAILABLE``.
    """


class IWMSchemaError(IWMImporterError):
    """Required column missing or renamed in the upstream CSV.

    Raised BEFORE any insert is attempted so a schema-drift event
    cannot produce a partial snapshot. The error message MUST
    name the offending column so operators can diff against the
    iShares CSV header.
    """


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedRow:
    """One equity row extracted from the iShares CSV."""

    ticker: str
    name: str
    asset_class: str
    weight: float
    sector: str | None = None
    market_value_usd: float | None = None
    notional_value_usd: float | None = None
    quantity: float | None = None
    price: float | None = None
    location: str | None = None
    exchange: str | None = None


@dataclass
class ImportResult:
    """Structured summary of one importer run.

    Suitable for stdout JSON emission via ``--emit-stats`` and for
    cron-log consumption. Field names mirror the validation
    contract evidence (e.g. ``equity_rows`` matches VAL-M1-003).
    """

    source_url: str
    db_path: str
    as_of_date: str = ""
    fetched_at: str = ""
    equity_rows: int = 0
    other_rows: int = 0
    null_ticker_count: int = 0
    null_weight_count: int = 0
    rows_written: int = 0
    dry_run: bool = False
    refresh_skipped: bool = False
    skip_reason: str | None = None
    completed_at: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


# ---------------------------------------------------------------------------
# Path / env helpers
# ---------------------------------------------------------------------------


def default_db_path() -> Path:
    """Return the canonical project SQLite path."""
    return DATA_DIR / "alpha_sniper.db"


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _now_iso() -> str:
    return _now_utc().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _today_iso() -> str:
    return _now_utc().date().isoformat()


def _resolve_source_url(explicit: str | None) -> str:
    """Resolve the iShares CSV URL from CLI flag, env, or default."""
    if explicit:
        return explicit
    env_value = os.environ.get("IWM_HOLDINGS_URL", "").strip()
    if env_value:
        return env_value
    return DEFAULT_IWM_HOLDINGS_URL


def _resolve_refresh_hours(explicit: int | None) -> int:
    """Resolve ``--max-age-hours`` from CLI flag or env.

    Order of precedence:

    1. Explicit CLI flag.
    2. ``RUSSELL_BIOTECH_REFRESH_HOURS`` env var (integer).
    3. :data:`DEFAULT_REFRESH_HOURS`.

    Non-integer env values fall back to the default with a
    WARNING log so cron operators notice the typo without
    crashing the run.
    """
    if explicit is not None:
        return max(0, int(explicit))
    raw = os.environ.get("RUSSELL_BIOTECH_REFRESH_HOURS", "").strip()
    if not raw:
        return DEFAULT_REFRESH_HOURS
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "RUSSELL_BIOTECH_REFRESH_HOURS=%r is not an integer; "
            "falling back to %d",
            raw,
            DEFAULT_REFRESH_HOURS,
        )
        return DEFAULT_REFRESH_HOURS


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------


_IWM_SNAPSHOT_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS iwm_holdings_snapshot (
    as_of_date            TEXT    NOT NULL,
    ticker                TEXT    NOT NULL,
    name                  TEXT,
    asset_class           TEXT    NOT NULL,
    weight                REAL    NOT NULL,
    sector                TEXT,
    market_value_usd      REAL,
    notional_value_usd    REAL,
    quantity              REAL,
    price                 REAL,
    location              TEXT,
    exchange              TEXT,
    source_url            TEXT    NOT NULL,
    fetched_at            TEXT    NOT NULL,
    PRIMARY KEY (as_of_date, ticker)
)
"""


_IWM_SNAPSHOT_INDEXES: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_iwm_holdings_snapshot_ticker "
    "ON iwm_holdings_snapshot(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_iwm_holdings_snapshot_fetched_at "
    "ON iwm_holdings_snapshot(fetched_at)",
    "CREATE INDEX IF NOT EXISTS idx_iwm_holdings_snapshot_asset_class "
    "ON iwm_holdings_snapshot(asset_class)",
)


def ensure_iwm_snapshot_table(conn: sqlite3.Connection) -> None:
    """Idempotently create the ``iwm_holdings_snapshot`` table.

    Safe to call against a v9 (pre-migration) or v10 (post-migration)
    database — both flavours converge on the same DDL. The forthcoming
    ``010_reading_b_foundations.py`` migration declares an identical
    schema so calling :func:`ensure_iwm_snapshot_table` after the
    migration is a no-op.
    """
    conn.execute(_IWM_SNAPSHOT_DDL)
    for stmt in _IWM_SNAPSHOT_INDEXES:
        conn.execute(stmt)


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------


def fetch_csv_bytes(
    url: str,
    *,
    timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    session: requests.Session | None = None,
) -> bytes:
    """Download the IWM holdings CSV.

    Any HTTP 4xx/5xx response (including iShares/Akamai 403) and
    any :class:`requests.RequestException` (timeout, DNS, TLS,
    connection reset, etc.) is wrapped in :class:`UpstreamUnavailable`
    so callers can apply the last-good fallback uniformly.

    Parameters
    ----------
    url:
        Fully-qualified HTTPS URL.
    timeout:
        Request timeout in seconds. The same value is applied to
        the connect AND read phases (``timeout=(t, t)`` semantics
        in the requests library).
    user_agent:
        Outbound ``User-Agent`` header. Defaults to
        :data:`DEFAULT_USER_AGENT`.
    session:
        Optional :class:`requests.Session` (used by tests so a
        cassette / mock layer can intercept ``session.get``).
    """
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/csv,application/octet-stream;q=0.9,*/*;q=0.5",
    }
    getter = session.get if session is not None else requests.get
    try:
        response = getter(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:  # network / TLS / DNS
        raise UpstreamUnavailable(
            f"iShares fetch failed: {exc.__class__.__name__}: {exc}"
        ) from exc

    status = getattr(response, "status_code", None)
    if status is None or not (200 <= int(status) < 300):
        # Akamai returns 403 from the edge for default-UA requests;
        # iShares occasionally returns 404 on URL renames. Treat
        # every non-2xx as a transient upstream failure (no
        # poisoning of the snapshot table).
        raise UpstreamUnavailable(
            f"iShares returned HTTP {status} for {url!r}"
        )

    body = response.content
    if not isinstance(body, (bytes, bytearray)):
        raise UpstreamUnavailable(
            f"iShares returned non-bytes body type={type(body).__name__}"
        )
    if not body:
        raise UpstreamUnavailable("iShares returned empty body")
    return bytes(body)


# ---------------------------------------------------------------------------
# CSV parse
# ---------------------------------------------------------------------------


def _decode_csv(body: bytes) -> str:
    """Decode the CSV body and strip any leading UTF-8 BOM.

    iShares serves UTF-8; the BOM is sometimes present and confuses
    naive ``csv.reader`` consumers (the BOM ends up appended to the
    first column header). Strip it before parsing.
    """
    text = body.decode("utf-8-sig", errors="replace")
    return text


#: Column-label tokens that strongly identify an IWM header row.
#: The iShares preamble rows are short metadata pairs ("Fund
#: Holdings as of", "Inception Date", etc.) that NEVER contain any
#: of these strings; the canonical header row contains every one
#: of them. Requiring at least three tokens out of the family is
#: enough to lock onto the right row even if one column has been
#: renamed (e.g. ``Ticker`` → ``Symbol``), so the parser can fail
#: with the precise :class:`IWMSchemaError` rather than the
#: generic "header row not found" error.
_HEADER_TOKEN_FAMILY: Final[frozenset[str]] = frozenset(
    {
        "ticker",
        "symbol",
        "name",
        "sector",
        "asset class",
        "weight (%)",
        "market value",
        "notional value",
        "quantity",
        "price",
    }
)


def _looks_like_header(row: Sequence[str]) -> bool:
    """Return ``True`` when ``row`` is the canonical IWM header row.

    The iShares preamble lines are short, mostly-empty metadata
    pairs that contain none of the column-label tokens listed in
    :data:`_HEADER_TOKEN_FAMILY`. Requiring at least three matches
    locks onto the real header row even when ONE column has been
    renamed upstream — so the parser can later fail with a precise
    :class:`IWMSchemaError` naming the offending column.
    """
    seen = {(cell or "").strip().lower() for cell in row}
    matches = seen & _HEADER_TOKEN_FAMILY
    return len(matches) >= 3


def _normalise_header(row: Sequence[str]) -> list[str]:
    """Return ``row`` with cells stripped and BOM removed."""
    return [cell.strip().lstrip("\ufeff") for cell in row]


def _validate_required_columns(header: Sequence[str]) -> None:
    """Raise :class:`IWMSchemaError` if any required column is missing.

    The error message names the FIRST missing column literally so
    operators can diff against the upstream header. Listing every
    missing column at once would be friendlier, but VAL-M1-055
    asserts the message contains the literal name of the
    REMOVED column — naming all of them simultaneously also passes
    that assertion.
    """
    present = {cell for cell in header}
    missing = [col for col in REQUIRED_COLUMNS if col not in present]
    if missing:
        raise IWMSchemaError(
            "iShares CSV header is missing required column(s): "
            + ", ".join(repr(col) for col in missing)
            + f". Got header: {list(header)!r}"
        )


def _parse_float(raw: str | None) -> float | None:
    """Parse iShares-formatted numeric strings.

    The CSV uses thousand-separated US-formatted numbers like
    ``"1,234.56"`` and ``"-"`` for unset values. Returns ``None``
    on any unparseable input — the caller decides how to log it.
    """
    if raw is None:
        return None
    cleaned = raw.strip().strip('"')
    if not cleaned or cleaned in {"-", "--", "N/A", "NA"}:
        return None
    cleaned = cleaned.replace(",", "")
    # Some rows have trailing percent signs on weight columns.
    if cleaned.endswith("%"):
        cleaned = cleaned[:-1]
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_iwm_csv(body: bytes) -> tuple[list[ParsedRow], dict[str, int]]:
    """Parse the IWM holdings CSV body.

    Returns
    -------
    rows:
        List of equity rows (``asset_class == 'Equity'``).
    stats:
        Counter dict with keys ``equity_rows``, ``other_rows``,
        ``null_ticker_count``, ``null_weight_count``.

    Raises
    ------
    IWMSchemaError
        When the header row cannot be located or a required
        column is missing.
    """
    text = _decode_csv(body)
    reader = csv.reader(io.StringIO(text))

    header_row: list[str] | None = None
    preamble_skipped = 0
    for raw_row in reader:
        if _looks_like_header(raw_row):
            header_row = _normalise_header(raw_row)
            break
        preamble_skipped += 1

    if header_row is None:
        raise IWMSchemaError(
            "iShares CSV header row not found "
            "(scanned {n} preamble rows looking for 'Ticker' + "
            "'Asset Class')".format(n=preamble_skipped)
        )

    _validate_required_columns(header_row)

    # Build a positional index map for cheap column access.
    idx = {col: i for i, col in enumerate(header_row)}

    def _cell(row: Sequence[str], col: str) -> str | None:
        i = idx.get(col)
        if i is None or i >= len(row):
            return None
        cell = row[i]
        if cell is None:
            return None
        return cell.strip()

    rows: list[ParsedRow] = []
    null_ticker = 0
    null_weight = 0
    other_rows = 0

    for raw_row in reader:
        if not raw_row or all((c is None or not c.strip()) for c in raw_row):
            # Blank row (iShares appends one between equities and
            # the cash/derivative summary). Skip silently.
            continue
        ticker = _cell(raw_row, "Ticker")
        asset_class = _cell(raw_row, "Asset Class")
        if not ticker:
            null_ticker += 1
            continue
        if asset_class != EQUITY_ASSET_CLASS:
            # Non-equity holdings (cash, FX hedge, swap) are
            # tracked for the ``other_rows`` counter but excluded
            # from the snapshot.
            other_rows += 1
            continue
        weight = _parse_float(_cell(raw_row, "Weight (%)"))
        if weight is None:
            null_weight += 1
            continue

        name = _cell(raw_row, "Name") or ""
        sector = _cell(raw_row, "Sector")
        market_value = _parse_float(_cell(raw_row, "Market Value"))
        notional_value = _parse_float(_cell(raw_row, "Notional Value"))
        quantity = _parse_float(_cell(raw_row, "Quantity"))
        price = _parse_float(_cell(raw_row, "Price"))
        location = _cell(raw_row, "Location")
        exchange = _cell(raw_row, "Exchange")

        # The Asset Class column is the equity discriminator and is
        # always literally ``"Equity"`` on equity rows; we still
        # echo it back so downstream consumers can audit by-row.
        rows.append(
            ParsedRow(
                ticker=ticker.upper(),
                name=name,
                asset_class=asset_class,
                weight=weight,
                sector=sector,
                market_value_usd=market_value,
                notional_value_usd=notional_value,
                quantity=quantity,
                price=price,
                location=location,
                exchange=exchange,
            )
        )

    stats = {
        "equity_rows": len(rows),
        "other_rows": other_rows,
        "null_ticker_count": null_ticker,
        "null_weight_count": null_weight,
        "preamble_skipped": preamble_skipped,
    }
    return rows, stats


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def write_snapshot(
    conn: sqlite3.Connection,
    rows: Sequence[ParsedRow],
    *,
    as_of_date: str,
    source_url: str,
    fetched_at: str,
) -> int:
    """Write ``rows`` to ``iwm_holdings_snapshot`` atomically.

    Uses a single explicit transaction (BEGIN / COMMIT) so a mid-write
    failure rolls back cleanly with no orphan rows. Returns the number
    of rows persisted.

    Re-running the importer on the same ``as_of_date`` upserts each
    ``(as_of_date, ticker)`` row in place — the PRIMARY KEY constraint
    is the SQL-level idempotency guard.
    """
    if not rows:
        return 0
    payload = [
        (
            as_of_date,
            row.ticker,
            row.name,
            row.asset_class,
            row.weight,
            row.sector,
            row.market_value_usd,
            row.notional_value_usd,
            row.quantity,
            row.price,
            row.location,
            row.exchange,
            source_url,
            fetched_at,
        )
        for row in rows
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO iwm_holdings_snapshot ("
        "as_of_date, ticker, name, asset_class, weight, sector, "
        "market_value_usd, notional_value_usd, quantity, price, "
        "location, exchange, source_url, fetched_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        payload,
    )
    return len(payload)


def _latest_snapshot_age_hours(
    conn: sqlite3.Connection,
    *,
    now: datetime.datetime,
) -> float | None:
    """Return the age (in hours) of the most recent snapshot, or ``None``.

    ``None`` indicates the table is empty (no snapshot yet) or the
    table is missing entirely (legacy db not migrated to v10 and the
    importer has not yet bootstrapped its table).
    """
    try:
        row = conn.execute(
            "SELECT MAX(fetched_at) AS latest FROM iwm_holdings_snapshot"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    latest = row[0] if not isinstance(row, sqlite3.Row) else row["latest"]
    if not latest:
        return None
    try:
        # Stored ISO is ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` — replace the
        # trailing Z with +00:00 so :py:meth:`datetime.fromisoformat`
        # parses it on Python 3.10/3.12.
        parsed = datetime.datetime.fromisoformat(
            str(latest).replace("Z", "+00:00")
        )
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    delta = now - parsed
    return delta.total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def import_iwm_holdings(
    *,
    db_path: Path | str | None = None,
    source_url: str | None = None,
    csv_path: Path | str | None = None,
    dry_run: bool = False,
    max_age_hours: int | None = None,
    http_timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    session: requests.Session | None = None,
    now: datetime.datetime | None = None,
) -> ImportResult:
    """Run one importer cycle: fetch / parse / persist.

    Parameters
    ----------
    db_path:
        Override the target SQLite path. ``None`` resolves to
        :func:`default_db_path`.
    source_url:
        Override the upstream URL. ``None`` resolves to the
        ``IWM_HOLDINGS_URL`` env var or
        :data:`DEFAULT_IWM_HOLDINGS_URL`.
    csv_path:
        Read the CSV from disk instead of the network. Mutually
        exclusive with ``source_url`` when supplied; takes
        precedence when both are passed.
    dry_run:
        Parse the CSV but skip the SQLite write.
    max_age_hours:
        Short-circuit the fetch when the most recent snapshot is
        younger than this many hours. ``None`` reads
        :data:`RUSSELL_BIOTECH_REFRESH_HOURS` from the environment.
        ``0`` disables the cache and forces a re-fetch.
    http_timeout:
        Per-request timeout in seconds.
    user_agent:
        Outbound ``User-Agent`` header.
    session:
        Optional :class:`requests.Session` (used by tests).
    now:
        Override the current UTC time (used by tests for the
        ``fetched_at`` / ``as_of_date`` deterministic stamps).

    Returns
    -------
    ImportResult
        Structured summary of the run.

    Raises
    ------
    UpstreamUnavailable
        Transient upstream failure (4xx/5xx, timeout, DNS, TLS).
        Translates to ``EXIT_UPSTREAM_UNAVAILABLE`` in
        :func:`main`.
    IWMSchemaError
        Required column missing or renamed in the upstream CSV.
        Translates to ``EXIT_SCHEMA_ERROR`` in :func:`main`.
    """
    target_db = Path(db_path) if db_path is not None else default_db_path()
    resolved_url = (
        f"file://{Path(csv_path).resolve()}"
        if csv_path is not None
        else _resolve_source_url(source_url)
    )
    refresh_hours = _resolve_refresh_hours(max_age_hours)
    now = now or _now_utc()
    fetched_at = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    as_of_date = now.date().isoformat()

    result = ImportResult(
        source_url=resolved_url,
        db_path=str(target_db),
        as_of_date=as_of_date,
        fetched_at=fetched_at,
        dry_run=dry_run,
    )

    # --- bootstrap the snapshot table ---------------------------------
    # Done up-front and unconditionally so every code path below
    # observes the schema. Cheap (idempotent IF NOT EXISTS) and the
    # "fresh-enough" cache check + post-failure validation queries
    # all rely on the table being present even when the fetch step
    # raises (timeout, schema error, upstream failure).
    ensure_data_dir()
    bootstrap_conn = db.connect(target_db)
    try:
        ensure_iwm_snapshot_table(bootstrap_conn)
        bootstrap_conn.commit()
        # Cache short-circuit only applies to real network fetches:
        # ``--dry-run`` and ``--csv-path`` are operator overrides
        # whose intent is to bypass the cache.
        if not dry_run and csv_path is None and refresh_hours > 0:
            age = _latest_snapshot_age_hours(bootstrap_conn, now=now)
            if age is not None and age < refresh_hours:
                logger.info(
                    "iwm_importer.skip reason=fresh_enough age_hours=%.2f "
                    "max_age_hours=%d",
                    age,
                    refresh_hours,
                )
                result.refresh_skipped = True
                result.skip_reason = (
                    f"fresh_enough age_hours={age:.2f} "
                    f"max_age_hours={refresh_hours}"
                )
                result.completed_at = _now_iso()
                return result
    finally:
        bootstrap_conn.close()

    # --- fetch -------------------------------------------------------
    if csv_path is not None:
        body = Path(csv_path).read_bytes()
    else:
        body = fetch_csv_bytes(
            resolved_url,
            timeout=http_timeout,
            user_agent=user_agent,
            session=session,
        )

    # --- parse -------------------------------------------------------
    parsed_rows, stats = parse_iwm_csv(body)
    result.equity_rows = stats["equity_rows"]
    result.other_rows = stats["other_rows"]
    result.null_ticker_count = stats["null_ticker_count"]
    result.null_weight_count = stats["null_weight_count"]

    # --- persist -----------------------------------------------------
    if dry_run:
        logger.info(
            "iwm_importer.dry_run equity_rows=%d other_rows=%d "
            "null_ticker=%d null_weight=%d",
            stats["equity_rows"],
            stats["other_rows"],
            stats["null_ticker_count"],
            stats["null_weight_count"],
        )
        result.completed_at = _now_iso()
        return result

    conn = db.connect(target_db)
    try:
        # Schema is already bootstrapped above; the explicit BEGIN
        # below frames the per-row writes in a single transaction.
        # ``conn.isolation_level`` is left at its Python-default
        # "deferred" so the BEGIN / executemany / COMMIT pattern
        # rolls back cleanly on any mid-write failure.
        conn.execute("BEGIN")
        try:
            written = write_snapshot(
                conn,
                parsed_rows,
                as_of_date=as_of_date,
                source_url=resolved_url,
                fetched_at=fetched_at,
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
    result.completed_at = _now_iso()
    logger.info(
        "iwm_importer.complete rows_written=%d as_of_date=%s "
        "source=%s",
        written,
        as_of_date,
        resolved_url,
    )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.universe.iwm_importer",
        description=(
            "Download the iShares IWM holdings CSV, parse it, and "
            "persist one row per (as_of_date, ticker) into the "
            "iwm_holdings_snapshot table. Last-good fallback on "
            "iShares 4xx/5xx; fail-loud on schema drift."
        ),
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "Run the full fetch + parse + write cycle (default "
            "behaviour; explicit flag for cron entrypoint)."
        ),
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help=(
            "Override the target SQLite database path "
            "(default: data/alpha_sniper.db)."
        ),
    )
    parser.add_argument(
        "--source",
        type=str,
        default=None,
        help=(
            "Override the upstream iShares CSV URL. Takes "
            "precedence over the IWM_HOLDINGS_URL env var. "
            "Mutually exclusive with --csv-path."
        ),
    )
    parser.add_argument(
        "--csv-path",
        type=str,
        default=None,
        help=(
            "Read the CSV from a local file instead of the "
            "network (operator dry-run + test fixtures)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse the CSV but do not write to SQLite.",
    )
    parser.add_argument(
        "--emit-stats",
        action="store_true",
        help="Print the run summary as a single JSON object on stdout.",
    )
    parser.add_argument(
        "--max-age-hours",
        type=int,
        default=None,
        help=(
            "Short-circuit the fetch when the latest snapshot is "
            "younger than N hours. Defaults to "
            "RUSSELL_BIOTECH_REFRESH_HOURS or 24."
        ),
    )
    parser.add_argument(
        "--http-timeout",
        type=float,
        default=DEFAULT_HTTP_TIMEOUT_SECONDS,
        help=(
            f"HTTP timeout (seconds) for the iShares fetch "
            f"(default: {DEFAULT_HTTP_TIMEOUT_SECONDS}). Values "
            f"≤ 0 still apply but will fail-fast on every fetch."
        ),
    )
    parser.add_argument(
        "--allow-shrinkage",
        action="store_true",
        help=(
            "No-op here (the shrinkage guard lives in the "
            "russell_biotech writer); accepted so cron can pass "
            "through the same flag set without quoting tricks."
        ),
    )
    return parser


def _emit(stats: bool, result: ImportResult) -> None:
    if stats:
        sys.stdout.write(result.to_json() + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint. See :mod:`biotech_sniper.universe.iwm_importer`."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    # ``--refresh`` is the documented cron flag, but we treat the
    # bare invocation the same way so operators do not have to
    # remember it. ``--csv-path`` always implies a local read.
    if args.source and args.csv_path:
        parser.error("--source and --csv-path are mutually exclusive")
        return EXIT_UPSTREAM_UNAVAILABLE  # pragma: no cover

    db_path = Path(args.db) if args.db else default_db_path()

    # ``--refresh`` is the documented cron flag and ALSO the way an
    # operator forces a re-fetch even when a fresh-enough snapshot
    # exists. Setting ``max_age_hours=0`` here disables the
    # cache-hit short-circuit for this invocation only (env vars
    # are not mutated).
    effective_max_age = args.max_age_hours
    if args.refresh and effective_max_age is None:
        effective_max_age = 0

    try:
        result = import_iwm_holdings(
            db_path=db_path,
            source_url=args.source,
            csv_path=args.csv_path,
            dry_run=args.dry_run,
            max_age_hours=effective_max_age,
            http_timeout=args.http_timeout,
        )
    except UpstreamUnavailable as exc:
        logger.error("iwm_importer.upstream_unavailable %s", exc)
        sys.stderr.write(f"ERROR: iwm_importer upstream unavailable: {exc}\n")
        return EXIT_UPSTREAM_UNAVAILABLE
    except IWMSchemaError as exc:
        logger.error("iwm_importer.schema_error %s", exc)
        sys.stderr.write(f"ERROR: iwm_importer schema error: {exc}\n")
        return EXIT_SCHEMA_ERROR

    _emit(args.emit_stats, result)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
