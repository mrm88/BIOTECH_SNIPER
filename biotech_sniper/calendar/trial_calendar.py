"""``trial_calendar`` merge layer (Reading-B feature f-m1-06).

This module unions three calendar sources into a single
``trial_calendar`` table keyed on ``(ticker, catalyst_date, source,
source_ref)``:

* **CT.gov** — read from the existing ``plays`` table whose
  ``catalyst_date`` column has been the canonical CT.gov anchor
  since the prior mission. Each row's ``nct_id`` (or
  ``source_key`` fallback) is recorded as ``source_ref``.
* **FDA PDUFA** — read from the ``pdufa_calendar`` table written by
  :mod:`biotech_sniper.calendar.pdufa` (f-m1-04). The ``drug``
  column is recorded as ``source_ref``.
* **EMA / CHMP** — read from the ``ema_calendar`` table written by
  :mod:`biotech_sniper.calendar.ema` (f-m1-05). One ``trial_calendar``
  row per non-null ``meeting_date``/``opinion_date`` field. Only
  rows whose ``ticker_or_sponsor`` matches the canonical US-ticker
  shape (1-6 uppercase alphanumerics, optional ``.``/``-``) are
  emitted; sponsor-only rows are silently dropped (the daemon's
  scope filter is ticker-keyed, so non-ticker rows have no
  consumer).

Per VAL-M1-035 the legacy daily-curated read path that consults
``plays.catalyst_date`` directly is **untouched** — :func:`rebuild_trial_calendar`
only ever reads from the ``plays`` table; it never writes to it.

Behavioural contract
--------------------

* **Single rebuild step.** :func:`rebuild_trial_calendar` performs
  one ``DELETE FROM trial_calendar`` followed by
  ``INSERT OR IGNORE`` of every merged row inside a single
  ``BEGIN IMMEDIATE`` transaction. Concurrent readers under SQLite
  WAL keep seeing the prior committed snapshot until COMMIT lands.
* **Composite-key idempotency.** Re-running the rebuild on an
  unchanged source set yields the same rows — the rebuild is a
  full replace and the composite UNIQUE index over
  ``(ticker, catalyst_date, source, COALESCE(source_ref,''))``
  rejects any in-batch duplicate ahead of COMMIT.
* **Earliest catalyst lookup.** :func:`get_next_catalyst` returns
  the lowest ISO-8601 ``catalyst_date`` per ticker across all three
  sources. Mirrors VAL-M1-032.
* **CT.gov regression-clean.** Reads of ``plays.catalyst_date`` are
  unchanged by the rebuild — the writer NEVER mutates ``plays``.
  Mirrors VAL-M1-035.
* **Graceful absence of source tables.** When a source table has
  not yet been created (e.g. a fresh DB without ``pdufa_calendar``),
  :func:`rebuild_trial_calendar` silently treats that source as
  empty rather than crashing. The merged result simply lacks rows
  for that source.

CLI
---

::

    python -m biotech_sniper.calendar.trial_calendar --rebuild

Flags
~~~~~

* ``--rebuild``    — perform the full DELETE / INSERT cycle.
* ``--db PATH``    — override the target SQLite file.
* ``--dry-run``    — compute the merged set but do not mutate
  the table.
* ``--emit-stats`` — print the run summary as a single JSON object
  on stdout.

Exit codes
~~~~~~~~~~

* ``0`` — success (rows written or dry-run completed).
* ``2`` — generic failure (caught :class:`Exception` re-raised by
  the orchestrator). Documented for parity with sibling calendar
  modules.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final, Iterable, Sequence

from biotech_sniper import db
from biotech_sniper.paths import DATA_DIR, ensure_data_dir

__all__ = [
    "SOURCE_CTGOV",
    "SOURCE_PDUFA",
    "SOURCE_EMA_CHMP",
    "TRIAL_CALENDAR_SOURCES",
    "EXIT_OK",
    "EXIT_FAILURE",
    "TrialCalendarRow",
    "RebuildResult",
    "default_db_path",
    "ensure_trial_calendar_table",
    "load_ctgov_rows",
    "load_pdufa_rows",
    "load_ema_rows",
    "rebuild_trial_calendar",
    "get_next_catalyst",
    "main",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


SOURCE_CTGOV: Final[str] = "ctgov"
SOURCE_PDUFA: Final[str] = "pdufa"
SOURCE_EMA_CHMP: Final[str] = "ema_chmp"

#: Stable tuple of source-enum values. Mirrors the CHECK constraint
#: in :data:`_TRIAL_CALENDAR_DDL`.
TRIAL_CALENDAR_SOURCES: Final[tuple[str, ...]] = (
    SOURCE_CTGOV,
    SOURCE_PDUFA,
    SOURCE_EMA_CHMP,
)

#: Documented exit codes (stable contract for cron + validators).
EXIT_OK: Final[int] = 0
EXIT_FAILURE: Final[int] = 2

#: Canonical US-ticker shape used when filtering EMA rows whose
#: ``ticker_or_sponsor`` may carry a free-form sponsor name. Mirrors
#: the predicate in :func:`biotech_sniper.calendar.pdufa._is_likely_ticker`.
_TICKER_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Z][A-Z0-9.\-]{0,5}[A-Z0-9]?$"
)

_ISO_DATE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$"
)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_TRIAL_CALENDAR_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS trial_calendar (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT    NOT NULL,
    catalyst_date TEXT    NOT NULL,
    source        TEXT    NOT NULL CHECK(source IN ('ctgov','pdufa','ema_chmp')),
    source_ref    TEXT,
    fetched_at    TEXT    NOT NULL
)
"""


# Composite UNIQUE matches VAL-M1-034:
#   (ticker, catalyst_date, source, COALESCE(source_ref, ''))
#
# A unique index over the COALESCE expression lets a NULL
# ``source_ref`` collapse to a single sentinel so two rows whose
# only difference is "NULL vs NULL" still dedup.
_TRIAL_CALENDAR_UNIQUE_INDEX: Final[str] = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_trial_calendar_unique "
    "ON trial_calendar("
    "ticker, catalyst_date, source, COALESCE(source_ref, '')"
    ")"
)


_TRIAL_CALENDAR_INDEXES: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_trial_calendar_ticker "
    "ON trial_calendar(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_trial_calendar_catalyst_date "
    "ON trial_calendar(catalyst_date)",
    "CREATE INDEX IF NOT EXISTS idx_trial_calendar_source "
    "ON trial_calendar(source)",
)


def ensure_trial_calendar_table(conn: sqlite3.Connection) -> None:
    """Idempotently create the ``trial_calendar`` table + indexes.

    Safe against a v9 (pre-migration) or v10 (post-migration) DB —
    both flavours converge on the same DDL. The forthcoming
    ``010_reading_b_foundations.py`` migration declares an identical
    schema so calling :func:`ensure_trial_calendar_table` after the
    migration is a no-op.
    """
    conn.execute(_TRIAL_CALENDAR_DDL)
    conn.execute(_TRIAL_CALENDAR_UNIQUE_INDEX)
    for stmt in _TRIAL_CALENDAR_INDEXES:
        conn.execute(stmt)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrialCalendarRow:
    """One ``trial_calendar`` row destined for persistence.

    ``source`` is one of :data:`TRIAL_CALENDAR_SOURCES`. ``source_ref``
    is the natural per-source identifier (NCT id for CT.gov, drug
    name for PDUFA, product slug for EMA). ``catalyst_date`` is a
    strict ISO-8601 ``YYYY-MM-DD`` string.
    """

    ticker: str
    catalyst_date: str
    source: str
    source_ref: str | None = None


@dataclass
class RebuildResult:
    """Structured summary of one rebuild cycle."""

    db_path: str
    fetched_at: str = ""
    parsed_rows: int = 0
    rows_written: int = 0
    per_source: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False
    completed_at: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


# ---------------------------------------------------------------------------
# Path / time helpers
# ---------------------------------------------------------------------------


def default_db_path() -> Path:
    """Return the canonical project SQLite path."""
    return DATA_DIR / "alpha_sniper.db"


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _now_iso() -> str:
    return _now_utc().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _is_iso_date(value: str | None) -> bool:
    """Return ``True`` when ``value`` matches strict ISO-8601 ``YYYY-MM-DD``."""
    if not isinstance(value, str):
        return False
    if not _ISO_DATE_RE.match(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_likely_ticker(value: str | None) -> bool:
    """Return ``True`` when ``value`` looks like a US biotech ticker."""
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if not (1 <= len(candidate) <= 6):
        return False
    if not candidate.isupper() and not _TICKER_RE.match(candidate):
        # Relax: allow already-canonical mixed-case if it matches the
        # uppercase-only regex on .upper(). EMA seeds always emit
        # uppercase, but be defensive.
        candidate = candidate.upper()
    return bool(_TICKER_RE.match(candidate)) and " " not in candidate


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Source loaders
# ---------------------------------------------------------------------------


def load_ctgov_rows(conn: sqlite3.Connection) -> list[TrialCalendarRow]:
    """Load CT.gov-derived catalysts from the existing ``plays`` table.

    Reads ``ticker``, ``catalyst_date`` and ``nct_id`` from
    ``plays`` where ``catalyst_date`` is non-NULL and ``ticker`` is
    non-empty. Falls back to ``source_key`` for ``source_ref`` when
    ``nct_id`` is NULL (the legacy intraday rotation path occasionally
    persists rows without an NCT id but with a meaningful source_key
    such as ``"intraday:VRTX_2026-05-30"``).

    Rows whose ``catalyst_date`` is not strict ISO-8601 are dropped
    silently — the legacy ``plays`` table predates the strict-ISO
    contract and may carry the occasional ``"Q3 2026"`` style stub.
    Such rows simply don't make it into the merged calendar; the
    daily-curated path that reads ``plays.catalyst_date`` directly
    is untouched (per VAL-M1-035).
    """
    if not _table_exists(conn, "plays"):
        return []
    rows: list[TrialCalendarRow] = []
    seen: set[tuple[str, str, str | None]] = set()
    cursor = conn.execute(
        "SELECT ticker, catalyst_date, nct_id, source_key "
        "FROM plays "
        "WHERE catalyst_date IS NOT NULL AND ticker IS NOT NULL "
        "AND TRIM(catalyst_date) <> '' AND TRIM(ticker) <> ''"
    )
    for raw_ticker, raw_date, nct_id, source_key in cursor.fetchall():
        ticker = (raw_ticker or "").strip().upper()
        catalyst_date = (raw_date or "").strip()
        if not ticker or not catalyst_date:
            continue
        if not _is_iso_date(catalyst_date):
            # Strict ISO-only — fuzzy stubs (Q3 2026 etc.) do not
            # propagate into the merged table.
            continue
        ref: str | None
        if nct_id and str(nct_id).strip():
            ref = str(nct_id).strip()
        elif source_key and str(source_key).strip():
            ref = str(source_key).strip()
        else:
            ref = None
        key = (ticker, catalyst_date, ref)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            TrialCalendarRow(
                ticker=ticker,
                catalyst_date=catalyst_date,
                source=SOURCE_CTGOV,
                source_ref=ref,
            )
        )
    return rows


def load_pdufa_rows(conn: sqlite3.Connection) -> list[TrialCalendarRow]:
    """Load PDUFA catalysts from ``pdufa_calendar``.

    Each row produces one :class:`TrialCalendarRow` keyed on
    ``(ticker, action_date, drug)``. Rows whose ``action_date`` is
    not strict ISO-8601 are dropped (the
    :mod:`biotech_sniper.calendar.pdufa` writer enforces this on
    insert, so the filter is a defence-in-depth check).
    """
    if not _table_exists(conn, "pdufa_calendar"):
        return []
    rows: list[TrialCalendarRow] = []
    seen: set[tuple[str, str, str | None]] = set()
    cursor = conn.execute(
        "SELECT ticker, action_date, drug FROM pdufa_calendar"
    )
    for raw_ticker, raw_date, drug in cursor.fetchall():
        ticker = (raw_ticker or "").strip().upper()
        catalyst_date = (raw_date or "").strip()
        if not ticker or not _is_iso_date(catalyst_date):
            continue
        ref = (str(drug).strip() or None) if drug is not None else None
        key = (ticker, catalyst_date, ref)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            TrialCalendarRow(
                ticker=ticker,
                catalyst_date=catalyst_date,
                source=SOURCE_PDUFA,
                source_ref=ref,
            )
        )
    return rows


def load_ema_rows(conn: sqlite3.Connection) -> list[TrialCalendarRow]:
    """Load CHMP catalysts from ``ema_calendar``.

    Each ``ema_calendar`` row may have both ``meeting_date`` and
    ``opinion_date`` populated; we emit one :class:`TrialCalendarRow`
    per non-null date so the daemon can react to either trigger.

    Rows whose ``ticker_or_sponsor`` does not match the canonical
    US-ticker shape (``^[A-Z][A-Z0-9.\\-]{0,5}[A-Z0-9]?$``) are
    silently dropped — the daemon's scope filter is ticker-keyed
    and free-form sponsor names have no consumer in the merged
    table.
    """
    if not _table_exists(conn, "ema_calendar"):
        return []
    rows: list[TrialCalendarRow] = []
    seen: set[tuple[str, str, str | None]] = set()
    cursor = conn.execute(
        "SELECT ticker_or_sponsor, product, meeting_date, opinion_date "
        "FROM ema_calendar"
    )
    for raw_ticker, raw_product, meeting_date, opinion_date in cursor.fetchall():
        ticker = (raw_ticker or "").strip().upper()
        if not _is_likely_ticker(ticker):
            continue
        product = (raw_product or "").strip() or None
        for date_value in (meeting_date, opinion_date):
            if not date_value:
                continue
            iso = str(date_value).strip()
            if not _is_iso_date(iso):
                continue
            key = (ticker, iso, product)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                TrialCalendarRow(
                    ticker=ticker,
                    catalyst_date=iso,
                    source=SOURCE_EMA_CHMP,
                    source_ref=product,
                )
            )
    return rows


# ---------------------------------------------------------------------------
# Atomic rebuild
# ---------------------------------------------------------------------------


def _atomic_replace(
    conn: sqlite3.Connection,
    rows: Sequence[TrialCalendarRow],
    *,
    fetched_at: str,
) -> int:
    """Replace every ``trial_calendar`` row inside one transaction.

    Uses ``BEGIN IMMEDIATE`` so the writer holds the SQLite
    "RESERVED" lock for the duration of the transaction — concurrent
    readers under WAL keep seeing the prior committed snapshot
    until COMMIT lands. ``INSERT OR IGNORE`` lets accidentally
    duplicated tuples (e.g. CT.gov + PDUFA both pointing at the
    same drug name) collapse to one row instead of raising.

    Returns the number of rows actually written (post-IGNORE).
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM trial_calendar")
        if rows:
            payload = [
                (
                    r.ticker,
                    r.catalyst_date,
                    r.source,
                    r.source_ref,
                    fetched_at,
                )
                for r in rows
            ]
            conn.executemany(
                "INSERT OR IGNORE INTO trial_calendar ("
                "ticker, catalyst_date, source, source_ref, fetched_at"
                ") VALUES (?, ?, ?, ?, ?)",
                payload,
            )
        (written,) = conn.execute(
            "SELECT COUNT(*) FROM trial_calendar"
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return int(written)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def rebuild_trial_calendar(
    *,
    db_path: Path | str | None = None,
    dry_run: bool = False,
    now: datetime.datetime | None = None,
) -> RebuildResult:
    """Run one rebuild cycle: load three sources → atomic write.

    See module docstring for the full behavioural contract.

    Parameters
    ----------
    db_path:
        Override the target SQLite path. ``None`` resolves to
        :func:`default_db_path`.
    dry_run:
        Compute the merged set but do not mutate ``trial_calendar``.
    now:
        Override the current UTC time (used by tests for
        deterministic ``fetched_at`` stamps).

    Returns
    -------
    RebuildResult
        Structured summary of the run.
    """
    target_db = Path(db_path) if db_path is not None else default_db_path()
    now = now or _now_utc()
    fetched_at = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    result = RebuildResult(
        db_path=str(target_db),
        fetched_at=fetched_at,
        dry_run=dry_run,
        per_source={src: 0 for src in TRIAL_CALENDAR_SOURCES},
    )

    ensure_data_dir()

    # --- bootstrap target table ---------------------------------------
    bootstrap_conn = db.connect(target_db)
    try:
        ensure_trial_calendar_table(bootstrap_conn)
        bootstrap_conn.commit()
    finally:
        bootstrap_conn.close()

    # --- load sources -------------------------------------------------
    read_conn = db.connect(target_db)
    try:
        # Reference each loader through the module so test monkey-
        # patches (see ``test_rebuild_atomic_replace``) take effect
        # without bypassing the module's own callable resolution.
        import biotech_sniper.calendar.trial_calendar as _self

        ctgov = _self.load_ctgov_rows(read_conn)
        pdufa = _self.load_pdufa_rows(read_conn)
        ema = _self.load_ema_rows(read_conn)
    finally:
        read_conn.close()

    merged: list[TrialCalendarRow] = [*ctgov, *pdufa, *ema]
    # In-batch dedup on the composite key — INSERT OR IGNORE will
    # also enforce this at the DB level, but pre-dedup keeps the
    # parsed_rows count meaningful for operator stats.
    seen: set[tuple[str, str, str, str]] = set()
    deduped: list[TrialCalendarRow] = []
    for row in merged:
        key = (row.ticker, row.catalyst_date, row.source, row.source_ref or "")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    result.parsed_rows = len(deduped)
    for r in deduped:
        result.per_source[r.source] = result.per_source.get(r.source, 0) + 1

    # --- write --------------------------------------------------------
    if dry_run:
        result.completed_at = _now_iso()
        logger.info(
            "trial_calendar.dry_run parsed_rows=%d ctgov=%d pdufa=%d "
            "ema_chmp=%d",
            result.parsed_rows,
            result.per_source.get("ctgov", 0),
            result.per_source.get("pdufa", 0),
            result.per_source.get("ema_chmp", 0),
        )
        return result

    write_conn = db.connect(target_db)
    try:
        written = _atomic_replace(
            write_conn, deduped, fetched_at=fetched_at
        )
    finally:
        write_conn.close()

    result.rows_written = written
    result.completed_at = _now_iso()
    logger.info(
        "trial_calendar.complete rows_written=%d ctgov=%d pdufa=%d "
        "ema_chmp=%d",
        written,
        result.per_source.get("ctgov", 0),
        result.per_source.get("pdufa", 0),
        result.per_source.get("ema_chmp", 0),
    )
    return result


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def get_next_catalyst(
    ticker: str,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> str | None:
    """Return the EARLIEST ``catalyst_date`` known for ``ticker``.

    Looks up ``MIN(catalyst_date)`` in ``trial_calendar`` for the
    given ticker (case-insensitive). Returns :data:`None` when no
    row exists or when the ``trial_calendar`` table itself is
    absent (e.g. the rebuild has not yet been run on a fresh DB).

    Parameters
    ----------
    ticker:
        Ticker symbol (case-insensitive — the merged table stores
        canonical uppercase).
    db_path:
        Optional SQLite path. Ignored when ``conn`` is provided.
    conn:
        Optional open SQLite connection. When supplied the helper
        leaves the connection open so callers can batch lookups.
    """
    if not ticker or not isinstance(ticker, str):
        return None
    canonical = ticker.strip().upper()
    if not canonical:
        return None

    own_conn = False
    if conn is None:
        target_db = (
            Path(db_path) if db_path is not None else default_db_path()
        )
        if not Path(target_db).exists():
            return None
        conn = db.connect(target_db)
        own_conn = True
    try:
        if not _table_exists(conn, "trial_calendar"):
            return None
        row = conn.execute(
            "SELECT MIN(catalyst_date) FROM trial_calendar "
            "WHERE ticker = ?",
            (canonical,),
        ).fetchone()
    finally:
        if own_conn:
            conn.close()
    if row is None:
        return None
    value = row[0] if not isinstance(row, sqlite3.Row) else row["MIN(catalyst_date)"]
    if not value:
        return None
    return str(value)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.calendar.trial_calendar",
        description=(
            "Merge CT.gov (plays.catalyst_date) ∪ pdufa_calendar ∪ "
            "ema_calendar into a single trial_calendar lookup table. "
            "Atomic replace; composite UNIQUE on (ticker, catalyst_date, "
            "source, COALESCE(source_ref,'')); legacy plays.catalyst_date "
            "reads remain untouched."
        ),
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Perform the full DELETE / INSERT cycle (cron entrypoint).",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Override the target SQLite database path.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the merged set but do not mutate the table.",
    )
    parser.add_argument(
        "--emit-stats",
        action="store_true",
        help="Print the run summary as a single JSON object on stdout.",
    )
    return parser


def _emit(stats: bool, result: RebuildResult) -> None:
    if stats:
        sys.stdout.write(result.to_json() + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint. See :mod:`biotech_sniper.calendar.trial_calendar`."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else default_db_path()

    try:
        result = rebuild_trial_calendar(
            db_path=db_path,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # pragma: no cover - defensive cron guard
        logger.error("trial_calendar.failure %s", exc)
        sys.stderr.write(f"ERROR: trial_calendar failure: {exc}\n")
        return EXIT_FAILURE

    _emit(args.emit_stats, result)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
