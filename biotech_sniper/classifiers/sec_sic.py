"""SEC EDGAR SIC classifier (Reading-B feature f-m1-02).

This module owns the ticker → SIC resolution path against the
public SEC EDGAR ``data.sec.gov`` API. It is the second stage of
the M1 universe pipeline (see ``library/architecture.md``):

    iwm_importer  -->  sec_sic (this module)  -->  russell2k_biotech writer

The downstream :mod:`biotech_sniper.universe.russell_biotech`
writer intersects the IWM holdings snapshot with the SIC-based
biotech subset (``2834``, ``2836``, ``8731``) to materialise the
``russell2k_biotech`` universe table.

Behavioural contract
--------------------

* **Two endpoints, one User-Agent.**
    1. ``https://www.sec.gov/files/company_tickers_exchange.json``
       returns the CIK→ticker map (downloaded once per process,
       lazily, and held in-memory for the lifetime of the
       :class:`SECSICClassifier` instance).
    2. ``https://data.sec.gov/submissions/CIK##########.json``
       returns the per-CIK ``sic`` + ``sicDescription`` fields.
   Every outbound request carries the project's
   :data:`DEFAULT_USER_AGENT` (project id + contact email) per
   the SEC fair-access policy.
* **≤ 8 req/s throttle.** A monotonic-clock token-bucket gate
  inside the classifier guarantees ``min_inter_request_seconds
  >= 0.125`` between any two outbound HTTP requests issued by
  the same instance.
* **Cache-first.** Every successful lookup is upserted into the
  ``cik_sic_cache`` SQLite table. The next call for the same
  ticker is answered from the cache with ZERO HTTP traffic —
  see :meth:`SECSICClassifier.resolve_ticker_sic`. A 404 from
  EDGAR (delisted CIK / never-issued CIK) writes a tombstone
  row with ``sic IS NULL`` so a subsequent lookup also hits
  the cache and returns :data:`None`.
* **Retry policy.** ``429`` and ``5xx`` responses, plus any
  transient :class:`requests.RequestException`, are retried
  with exponential backoff (``0.5s × 2.0 ± 20 % jitter``)
  capped at :data:`MAX_RETRY_ATTEMPTS = 3` attempts. After 3
  consecutive failures the classifier raises
  :class:`SECTransientError` and does NOT poison the cache.
* **404 graceful.** A plain ``404`` (delisted CIK) does NOT
  raise; the classifier logs a WARNING, returns :data:`None`,
  and writes a tombstone row.

CLI usage is intentionally minimal — the classifier is consumed
programmatically by the russell_biotech writer; an
operator-facing CLI lives in that module rather than duplicated
here.
"""

from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Mapping

import requests

from biotech_sniper import db
from biotech_sniper.paths import DATA_DIR, ensure_data_dir

__all__ = [
    "DEFAULT_USER_AGENT",
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "DEFAULT_MIN_INTER_REQUEST_SECONDS",
    "DEFAULT_BACKOFF_BASE_SECONDS",
    "DEFAULT_BACKOFF_FACTOR",
    "DEFAULT_BACKOFF_JITTER",
    "MAX_RETRY_ATTEMPTS",
    "BIOTECH_SIC_CODES",
    "CIK_TICKERS_URL",
    "SUBMISSIONS_URL_TEMPLATE",
    "SECClassifierError",
    "SECTransientError",
    "SECSchemaError",
    "SICResolution",
    "SECSICClassifier",
    "default_db_path",
    "ensure_cik_sic_cache_table",
    "format_cik",
    "resolve_ticker_sic",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Public CIK → ticker / exchange map. Documented as the canonical
#: source for ticker → CIK resolution; updated daily by SEC.
CIK_TICKERS_URL: Final[str] = (
    "https://www.sec.gov/files/company_tickers_exchange.json"
)

#: Per-CIK submissions endpoint. The CIK is zero-padded to 10
#: digits and prefixed with ``CIK``.
SUBMISSIONS_URL_TEMPLATE: Final[str] = (
    "https://data.sec.gov/submissions/CIK{cik}.json"
)

#: Descriptive User-Agent (project id + contact email) used for
#: every outbound HTTP request. The SEC fair-access policy
#: requires a contact email; the default ``python-requests/x.y``
#: UA is rejected at the edge.
DEFAULT_USER_AGENT: Final[str] = (
    "BiotechSniper/1.0 (contact: ops@alpha-sniper.local)"
)

#: HTTP timeout for every SEC request. SEC endpoints are
#: low-latency so 15 s is generous.
DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 15.0

#: SEC fair-access policy caps clients at 10 req/s burst, 8 req/s
#: sustained. We adopt 8 req/s as the hard ceiling — i.e. the
#: minimum inter-request gap is ``1 / 8 = 0.125`` seconds.
DEFAULT_MIN_INTER_REQUEST_SECONDS: Final[float] = 0.125

#: Exponential-backoff base (seconds). First retry sleeps
#: ``base * factor^0 ± jitter`` = ``0.5 ± 0.1`` seconds; second
#: sleeps ``1.0 ± 0.2``; third sleeps ``2.0 ± 0.4``.
DEFAULT_BACKOFF_BASE_SECONDS: Final[float] = 0.5

#: Exponential-backoff factor (multiplier between retries).
DEFAULT_BACKOFF_FACTOR: Final[float] = 2.0

#: Backoff jitter as a fraction of the current sleep target.
#: ``0.2`` means ±20 % of the deterministic value.
DEFAULT_BACKOFF_JITTER: Final[float] = 0.2

#: Maximum retry attempts (the 1st attempt + (MAX-1) retries).
#: After exhausting all attempts the classifier raises
#: :class:`SECTransientError` rather than poisoning the cache.
MAX_RETRY_ATTEMPTS: Final[int] = 3

#: SIC codes considered "biotech" for Reading-B's
#: ``russell2k_biotech`` writer. Excludes med-device 3841/3845
#: and diagnostics 2835 by design — see ``mission.md``.
BIOTECH_SIC_CODES: Final[frozenset[int]] = frozenset({2834, 2836, 8731})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SECClassifierError(Exception):
    """Base class for SEC classifier errors."""


class SECTransientError(SECClassifierError):
    """Persistent transport failure (3 consecutive retries failed).

    Wraps the most recent underlying exception (HTTP 5xx, 429,
    timeout, DNS, TLS). Callers should treat this as retryable
    on the NEXT cron tick — the cache is NOT poisoned.
    """


class SECSchemaError(SECClassifierError):
    """The SEC API returned a 200 with an unexpected JSON shape.

    Raised when the CIK→ticker map or the submissions document
    cannot be parsed (missing ``fields`` / ``data`` keys, missing
    ``sic`` field, etc.). Callers should treat this as a bug
    against an upstream contract change, not a transient issue.
    """


# ---------------------------------------------------------------------------
# DB schema
# ---------------------------------------------------------------------------


_CIK_SIC_CACHE_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS cik_sic_cache (
    cik               TEXT    NOT NULL PRIMARY KEY,
    ticker            TEXT    NOT NULL,
    sic               INTEGER,
    sic_description   TEXT,
    fetched_at        TEXT    NOT NULL
)
"""


# UNIQUE index on ticker enforces 1:1 ticker→cik mapping (matches
# SEC's CIK→ticker map shape) while leaving cik as the canonical
# row identity. ``sic`` and ``fetched_at`` indexes are non-unique
# helpers for the russell_biotech writer + ops-side queries.
_CIK_SIC_CACHE_INDEXES: Final[tuple[str, ...]] = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_cik_sic_cache_ticker "
    "ON cik_sic_cache(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_cik_sic_cache_sic "
    "ON cik_sic_cache(sic)",
    "CREATE INDEX IF NOT EXISTS idx_cik_sic_cache_fetched_at "
    "ON cik_sic_cache(fetched_at)",
)


def _detect_legacy_cik_sic_cache(conn: sqlite3.Connection) -> bool:
    """Return ``True`` when ``cik_sic_cache`` is in the legacy ticker-PK shape.

    The legacy shape (pre-feature-f-m1-02b) had ``ticker`` as
    PRIMARY KEY and ``cik`` as a UNIQUE-indexed non-PK column.
    The corrected shape (per VAL-M1-012) has ``cik`` as PRIMARY
    KEY and ``ticker`` as a UNIQUE-indexed non-PK column.
    """
    table = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='cik_sic_cache'"
    ).fetchone()
    if table is None:
        return False
    cols = conn.execute("PRAGMA table_info(cik_sic_cache)").fetchall()
    # PRAGMA table_info column[5] is ``pk`` (0 = not part of PK,
    # otherwise 1-indexed PK column rank).
    ticker_is_pk = any(c[1] == "ticker" and c[5] >= 1 for c in cols)
    cik_is_pk = any(c[1] == "cik" and c[5] >= 1 for c in cols)
    return ticker_is_pk and not cik_is_pk


def _migrate_legacy_cik_sic_cache(conn: sqlite3.Connection) -> None:
    """Forward-only migration: legacy ticker-PK shape → cik-PK shape.

    The migration runs inside a single transaction and is a
    no-op when the table is already in the new shape. Rows are
    copied verbatim — every legacy row has both a ``cik`` and a
    ``ticker`` value, so the column-renaming is a pure key swap
    (no data transformation). The legacy table is dropped on
    success; on failure, the transaction rolls back and the
    legacy table is preserved.
    """
    nested = conn.in_transaction
    if not nested:
        conn.execute("BEGIN")
    try:
        # Sidestep the temporary-table renaming dance with a
        # fresh-named scratch table: copy rows in, then atomic
        # drop+rename.
        conn.execute("ALTER TABLE cik_sic_cache RENAME TO cik_sic_cache_legacy")
        conn.execute(_CIK_SIC_CACHE_DDL)
        conn.execute(
            "INSERT INTO cik_sic_cache "
            "(cik, ticker, sic, sic_description, fetched_at) "
            "SELECT cik, ticker, sic, sic_description, fetched_at "
            "FROM cik_sic_cache_legacy"
        )
        conn.execute("DROP TABLE cik_sic_cache_legacy")
        for stmt in _CIK_SIC_CACHE_INDEXES:
            conn.execute(stmt)
        if not nested:
            conn.execute("COMMIT")
    except Exception:
        if not nested:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise


def ensure_cik_sic_cache_table(conn: sqlite3.Connection) -> None:
    """Idempotently create the ``cik_sic_cache`` table.

    Safe to call against either a v9 (pre-migration) or v10
    (post-migration) database — both flavours converge on the
    corrected shape (cik PRIMARY KEY, UNIQUE index on ticker,
    sic INTEGER nullable for 404 tombstones).

    If the table is detected in the legacy ticker-PK shape (a
    pre-existing artefact of the f-m1-02 implementation), this
    function migrates it forward inside a single transaction —
    rows are preserved, the legacy table is dropped, and the
    new shape with its UNIQUE-ticker index is created in place.

    Mirrors the pattern used by
    :func:`biotech_sniper.universe.iwm_importer.ensure_iwm_snapshot_table`.
    """
    if _detect_legacy_cik_sic_cache(conn):
        _migrate_legacy_cik_sic_cache(conn)
        return
    conn.execute(_CIK_SIC_CACHE_DDL)
    for stmt in _CIK_SIC_CACHE_INDEXES:
        conn.execute(stmt)


def default_db_path() -> Path:
    """Return the canonical project SQLite path."""
    return DATA_DIR / "alpha_sniper.db"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_CIK_NUMERIC_RE: Final[re.Pattern[str]] = re.compile(r"^\d{1,10}$")


def format_cik(cik: int | str) -> str:
    """Return ``cik`` as a 10-digit zero-padded string.

    Accepts an :class:`int` or a stringified integer (with or
    without leading zeros / ``"CIK"`` prefix). Rejects garbage
    so the caller cannot accidentally hit a malformed URL.

    >>> format_cik(875320)
    '0000875320'
    >>> format_cik("0000875320")
    '0000875320'
    >>> format_cik("CIK0000875320")
    '0000875320'
    """
    if isinstance(cik, int):
        if cik < 0:
            raise ValueError(f"CIK must be non-negative, got {cik}")
        if cik > 9_999_999_999:
            raise ValueError(f"CIK exceeds 10 digits: {cik}")
        return f"{cik:010d}"
    raw = str(cik).strip()
    if raw.upper().startswith("CIK"):
        raw = raw[3:]
    if not raw or not raw.isdigit():
        raise ValueError(f"CIK is not numeric: {cik!r}")
    if len(raw) > 10:
        raise ValueError(f"CIK exceeds 10 digits: {cik!r}")
    return f"{int(raw):010d}"


def _now_iso() -> str:
    # Mirror the existing iwm_importer / db module timestamp
    # format so the cache rows compare cleanly with the rest of
    # the schema.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SICResolution:
    """One ticker → SIC resolution."""

    ticker: str
    cik: str
    sic: int | None
    sic_description: str | None
    fetched_at: str
    cached: bool = False


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


class SECSICClassifier:
    """Resolve tickers → SIC codes against SEC EDGAR.

    The classifier is the canonical entrypoint for downstream
    callers. A single instance owns:

    * The CIK → ticker map (lazy-loaded on first use).
    * A monotonic-clock throttle (≤ 8 req/s).
    * A SQLite-backed cache (``cik_sic_cache``).

    Multiple instances against the same database are safe — the
    cache writes are idempotent UPSERTs. The throttle is
    per-instance; callers issuing concurrent lookups across
    many instances should serialise on a shared classifier.

    Parameters
    ----------
    db_path:
        Path to the SQLite database. Defaults to
        :func:`default_db_path`.
    user_agent:
        ``User-Agent`` header for every outbound request.
    timeout:
        Per-request HTTP timeout (seconds).
    min_inter_request_seconds:
        Minimum gap between two outbound requests issued by this
        instance. Defaults to :data:`DEFAULT_MIN_INTER_REQUEST_SECONDS`
        (0.125 s = 8 req/s).
    backoff_base_seconds / backoff_factor / backoff_jitter:
        Exponential-backoff knobs for the 429 / 5xx retry path.
    max_retry_attempts:
        Hard cap on the number of attempts per request.
    session:
        Optional :class:`requests.Session` (used by tests for
        cassette interception).
    sleep:
        Optional callable matching :func:`time.sleep` (used by
        tests to record / fast-forward backoff sleeps).
    monotonic:
        Optional callable matching :func:`time.monotonic` (used
        by tests to drive the throttle deterministically).
    """

    def __init__(
        self,
        *,
        db_path: Path | str | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        min_inter_request_seconds: float = DEFAULT_MIN_INTER_REQUEST_SECONDS,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
        backoff_jitter: float = DEFAULT_BACKOFF_JITTER,
        max_retry_attempts: int = MAX_RETRY_ATTEMPTS,
        session: requests.Session | None = None,
        sleep: "callable[[float], None] | None" = None,
        monotonic: "callable[[], float] | None" = None,
    ) -> None:
        self._db_path = (
            Path(db_path) if db_path is not None else default_db_path()
        )
        self._user_agent = user_agent
        self._timeout = timeout
        self._min_inter_request_seconds = float(min_inter_request_seconds)
        self._backoff_base_seconds = float(backoff_base_seconds)
        self._backoff_factor = float(backoff_factor)
        self._backoff_jitter = float(backoff_jitter)
        self._max_retry_attempts = int(max_retry_attempts)
        self._session = session
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic

        self._lock = threading.Lock()
        self._last_request_monotonic: float | None = None
        # ``_ticker_to_cik`` is populated lazily on the first
        # ``resolve_ticker_sic`` call that misses the cache.
        self._ticker_to_cik: dict[str, str] | None = None

        # Diagnostics — tests assert these counters to avoid
        # over-reaching with mock-call recorders.
        self._http_call_count: int = 0
        self._inter_request_gaps: list[float] = []

    # -- public ---------------------------------------------------------

    @property
    def http_call_count(self) -> int:
        """Number of outbound HTTP requests issued by this instance."""
        return self._http_call_count

    @property
    def inter_request_gaps(self) -> list[float]:
        """Recorded inter-request gaps (seconds) — for throttle assertions."""
        # Defensive copy; callers must not mutate the internal
        # bookkeeping list.
        return list(self._inter_request_gaps)

    def resolve_ticker_sic(self, ticker: str) -> SICResolution | None:
        """Resolve ``ticker`` → :class:`SICResolution`.

        Returns :data:`None` when the ticker maps to a CIK that
        EDGAR rejects with HTTP 404 (delisted / never-issued
        CIK). Returns :data:`None` for tickers that are not in
        the CIK→ticker map at all.

        Cache semantics:

        * Hot path: a previously-resolved ticker is answered from
          the ``cik_sic_cache`` table with ZERO HTTP traffic.
        * Cold path: the CIK→ticker map is fetched once per
          process (HTTP #1), then the per-CIK submissions
          endpoint is fetched (HTTP #2). Both fetches obey the
          ≤ 8 req/s throttle and the 429 / 5xx retry policy.
        * 404 path: a tombstone row (``sic IS NULL``) is written
          to the cache so subsequent lookups also hit the cache.
        """
        norm = (ticker or "").strip().upper()
        if not norm:
            raise ValueError("ticker must be a non-empty string")

        # Cache hit short-circuit — no HTTP, no map load.
        cached = self._cache_lookup(norm)
        if cached is not None:
            # Tombstone row (sic IS NULL) → return None to mirror
            # the live-404 path. Operators can still inspect the
            # tombstone in the ``cik_sic_cache`` table directly.
            if cached.sic is None:
                return None
            return cached

        # Cache miss — resolve the CIK from the ticker map.
        ticker_to_cik = self._load_ticker_map()
        cik_raw = ticker_to_cik.get(norm)
        if cik_raw is None:
            # Ticker not present in the SEC's CIK→ticker map —
            # almost certainly an OTC / preferred / private
            # security. We do NOT tombstone here because we have
            # no CIK to anchor the row on; the caller can re-try
            # tomorrow when the SEC refreshes the map.
            logger.info(
                "sec_sic.no_cik ticker=%s reason=not_in_cik_ticker_map",
                norm,
            )
            return None
        cik = format_cik(cik_raw)

        # Fetch the submissions document.
        try:
            submission = self._fetch_submission(cik)
        except _NotFound:
            # Delisted / never-issued CIK — write a tombstone so
            # we don't re-issue the request next cron tick.
            now = _now_iso()
            self._cache_upsert(
                ticker=norm,
                cik=cik,
                sic=None,
                sic_description=None,
                fetched_at=now,
            )
            logger.warning(
                "sec_sic.delisted ticker=%s cik=%s reason=submissions_404",
                norm,
                cik,
            )
            return None

        sic, sic_description = self._parse_submission(submission, cik=cik)

        now = _now_iso()
        self._cache_upsert(
            ticker=norm,
            cik=cik,
            sic=sic,
            sic_description=sic_description,
            fetched_at=now,
        )
        return SICResolution(
            ticker=norm,
            cik=cik,
            sic=sic,
            sic_description=sic_description,
            fetched_at=now,
            cached=False,
        )

    # -- internals ------------------------------------------------------

    def _load_ticker_map(self) -> Mapping[str, str]:
        """Fetch (or return the cached) CIK→ticker map."""
        if self._ticker_to_cik is not None:
            return self._ticker_to_cik
        try:
            payload = self._http_get_json(CIK_TICKERS_URL)
        except _NotFound as exc:
            # The map endpoint should never return 404 in
            # practice; if it does we treat it as a schema-level
            # contract break.
            raise SECSchemaError(
                f"company_tickers_exchange.json returned 404: {exc}"
            ) from exc

        fields = payload.get("fields") if isinstance(payload, dict) else None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(fields, list) or not isinstance(data, list):
            raise SECSchemaError(
                "company_tickers_exchange.json: missing 'fields' / 'data'"
            )
        # Normalise field labels — the documented order is
        # ['cik', 'name', 'ticker', 'exchange'] but we look up
        # by name to be defensive against re-ordering.
        try:
            cik_idx = fields.index("cik")
            ticker_idx = fields.index("ticker")
        except ValueError as exc:
            raise SECSchemaError(
                "company_tickers_exchange.json: required field "
                f"missing ({exc})"
            ) from exc

        mapping: dict[str, str] = {}
        for row in data:
            if not isinstance(row, list) or len(row) <= max(cik_idx, ticker_idx):
                continue
            ticker_val = row[ticker_idx]
            cik_val = row[cik_idx]
            if not ticker_val or cik_val is None:
                continue
            mapping[str(ticker_val).strip().upper()] = format_cik(cik_val)

        self._ticker_to_cik = mapping
        return mapping

    def _fetch_submission(self, cik: str) -> dict:
        """Fetch the per-CIK submissions document.

        Raises :class:`_NotFound` on HTTP 404 so callers can
        write a tombstone row.
        """
        url = SUBMISSIONS_URL_TEMPLATE.format(cik=cik)
        return self._http_get_json(url)

    @staticmethod
    def _parse_submission(
        payload: dict, *, cik: str
    ) -> tuple[int | None, str | None]:
        """Extract ``(sic, sicDescription)`` from a submissions document."""
        if not isinstance(payload, dict):
            raise SECSchemaError(
                f"submissions document for CIK {cik} is not a JSON object"
            )
        raw_sic = payload.get("sic")
        sic_description = payload.get("sicDescription")
        if raw_sic in (None, ""):
            return None, sic_description if sic_description else None
        try:
            sic = int(str(raw_sic).strip())
        except (TypeError, ValueError) as exc:
            raise SECSchemaError(
                f"submissions document for CIK {cik} has non-integer "
                f"sic={raw_sic!r}"
            ) from exc
        return sic, str(sic_description) if sic_description else None

    # -- HTTP layer -----------------------------------------------------

    def _http_get_json(self, url: str) -> dict:
        """GET ``url`` and return the parsed JSON body.

        Applies the throttle, the User-Agent, and the
        429/5xx-retry policy. Maps HTTP 404 to
        :class:`_NotFound` so callers can write a tombstone row
        without confusing the retry loop.
        """
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retry_attempts + 1):
            self._wait_for_throttle()
            try:
                response = self._send_request(url)
            except requests.RequestException as exc:
                last_exc = exc
                logger.info(
                    "sec_sic.http_transient attempt=%d url=%s exc=%s",
                    attempt,
                    url,
                    exc.__class__.__name__,
                )
                if attempt >= self._max_retry_attempts:
                    break
                self._backoff_sleep(attempt)
                continue

            status = int(getattr(response, "status_code", 0) or 0)
            if status == 404:
                raise _NotFound(f"HTTP 404 for {url}")
            if status == 429 or 500 <= status < 600:
                last_exc = SECTransientError(
                    f"HTTP {status} for {url}"
                )
                logger.info(
                    "sec_sic.http_retryable attempt=%d url=%s status=%d",
                    attempt,
                    url,
                    status,
                )
                if attempt >= self._max_retry_attempts:
                    break
                self._backoff_sleep(attempt)
                continue
            if not 200 <= status < 300:
                raise SECClassifierError(
                    f"unexpected HTTP {status} for {url}"
                )

            body = getattr(response, "content", None)
            if body is None:
                # Some test doubles only set ``.text``.
                text = getattr(response, "text", "") or ""
                body = text.encode("utf-8")
            if not body:
                raise SECSchemaError(f"empty body for {url}")
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise SECSchemaError(
                    f"invalid JSON for {url}: {exc}"
                ) from exc

        # Exhausted retries.
        if isinstance(last_exc, SECTransientError):
            raise last_exc
        raise SECTransientError(
            f"transport failure for {url}: "
            f"{last_exc.__class__.__name__ if last_exc else 'unknown'}: "
            f"{last_exc}"
        ) from last_exc

    def _send_request(self, url: str) -> requests.Response:
        headers = {
            "User-Agent": self._user_agent,
            "Accept": "application/json",
        }
        getter = (
            self._session.get
            if self._session is not None
            else requests.get
        )
        self._http_call_count += 1
        return getter(url, headers=headers, timeout=self._timeout)

    def _wait_for_throttle(self) -> None:
        """Block until the next request would respect the rate limit."""
        gap_target = self._min_inter_request_seconds
        if gap_target <= 0:
            return
        with self._lock:
            now = self._monotonic()
            last = self._last_request_monotonic
            if last is None:
                self._last_request_monotonic = now
                return
            elapsed = now - last
            if elapsed >= gap_target:
                self._inter_request_gaps.append(elapsed)
                self._last_request_monotonic = now
                return
            sleep_for = gap_target - elapsed
        # Release the lock around the sleep so a second thread
        # waiting for the same slot does not stall the entire
        # process — the throttle is best-effort for concurrency
        # but strict in single-threaded use (which is the
        # supported call pattern).
        self._sleep(sleep_for)
        with self._lock:
            new_now = self._monotonic()
            prior = self._last_request_monotonic
            if prior is not None:
                self._inter_request_gaps.append(new_now - prior)
            self._last_request_monotonic = new_now

    def _backoff_sleep(self, attempt: int) -> None:
        """Sleep for the exponential-backoff duration of ``attempt``.

        ``attempt`` is 1-indexed; ``attempt=1`` corresponds to
        ``base * factor^0`` ± jitter.
        """
        base = self._backoff_base_seconds * (self._backoff_factor ** (attempt - 1))
        jitter_amplitude = base * self._backoff_jitter
        # Use a deterministic jitter source so test recorders see
        # the bounded range; ``random.uniform`` works for both
        # production and tests because the upper bound is the
        # only thing the contract pins.
        jitter = random.uniform(-jitter_amplitude, jitter_amplitude)
        delay = max(0.0, base + jitter)
        self._sleep(delay)

    # -- cache layer ----------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        ensure_data_dir()
        conn = db.connect(self._db_path)
        ensure_cik_sic_cache_table(conn)
        return conn

    def _cache_lookup(self, ticker: str) -> SICResolution | None:
        try:
            conn = self._connect()
        except sqlite3.OperationalError:
            return None
        try:
            row = conn.execute(
                "SELECT ticker, cik, sic, sic_description, fetched_at "
                "FROM cik_sic_cache WHERE ticker = ?",
                (ticker,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        # ``sqlite3.Row`` supports both name and index access.
        return SICResolution(
            ticker=row["ticker"],
            cik=row["cik"],
            sic=int(row["sic"]) if row["sic"] is not None else None,
            sic_description=row["sic_description"],
            fetched_at=row["fetched_at"],
            cached=True,
        )

    def _cache_upsert(
        self,
        *,
        ticker: str,
        cik: str,
        sic: int | None,
        sic_description: str | None,
        fetched_at: str,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            try:
                # ``cik`` is the PRIMARY KEY (per VAL-M1-012);
                # the ON CONFLICT target is ``cik`` so re-fetching
                # the same CIK refreshes the cached SIC fields
                # (and the ticker, if SEC ever re-maps a CIK).
                conn.execute(
                    "INSERT INTO cik_sic_cache "
                    "(cik, ticker, sic, sic_description, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(cik) DO UPDATE SET "
                    "ticker=excluded.ticker, "
                    "sic=excluded.sic, "
                    "sic_description=excluded.sic_description, "
                    "fetched_at=excluded.fetched_at",
                    (cik, ticker, sic, sic_description, fetched_at),
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


# ---------------------------------------------------------------------------
# Internal sentinel
# ---------------------------------------------------------------------------


class _NotFound(Exception):
    """Internal: HTTP 404 from EDGAR (delisted / never-issued CIK)."""


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


def resolve_ticker_sic(
    ticker: str,
    *,
    db_path: Path | str | None = None,
) -> SICResolution | None:
    """One-shot convenience wrapper around :class:`SECSICClassifier`.

    Use the class form for batch resolution (it amortises the
    CIK→ticker map fetch across many tickers); use this helper
    for single-ticker scripts and ad-hoc resolution.
    """
    return SECSICClassifier(db_path=db_path).resolve_ticker_sic(ticker)
