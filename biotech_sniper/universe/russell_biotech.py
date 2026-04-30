"""``russell2k_biotech`` writer (Reading-B feature f-m1-03).

This module is the third stage of the M1 universe pipeline (see
``library/architecture.md``)::

    iwm_importer  -->  sec_sic classifier  -->  russell_biotech (this module)

It intersects the latest ``iwm_holdings_snapshot`` rows with the SEC
EDGAR SIC classifier output and materialises the ``russell2k_biotech``
table — a biotech-only subset of the Russell 2000 universe used by
the Reading-B Stage-1 news daemon.

Behavioural contract
--------------------

* **SIC allow-list.** Only tickers whose SEC EDGAR SIC code is in
  :data:`BIOTECH_SIC_CODES = {2834, 2836, 8731}` are written. Med-device
  (3841 / 3845) and diagnostics (2835) are excluded by design — the
  table also carries a ``CHECK(sic IN (2834, 2836, 8731))`` constraint
  so a future bug cannot smuggle a non-biotech row past the filter.
* **Atomic refresh.** The writer wraps every refresh inside a single
  ``BEGIN IMMEDIATE`` transaction so concurrent readers (the Stage-1
  news daemon, ad-hoc operator queries) NEVER observe a partially
  populated ``russell2k_biotech`` mid-refresh. SQLite WAL gives readers
  snapshot isolation: until ``COMMIT`` lands, every ``SELECT`` sees the
  prior committed state. The "concurrent readers never see < 100 rows"
  invariant follows directly from this.
* **25 % shrinkage refusal.** Right before COMMIT, the writer compares
  the new row count against the prior committed count. When the new
  count is less than 75 % of the prior count (and the prior count was
  itself non-zero), the transaction is ROLLBACK'd and the writer exits
  with :data:`EXIT_SHRINKAGE_REFUSED`. The prior snapshot remains
  intact — last-good fallback. Operators who genuinely WANT the
  shrunken snapshot can pass ``--allow-shrinkage``.
* **Idempotency.** Running the writer twice on the same day is a no-op
  at the row level — the writer ``DELETE``s and re-``INSERT``s every
  row inside one transaction, so any duplicate writes are absorbed by
  the PRIMARY KEY on ``ticker``. ``fetched_at`` is refreshed but no
  rows disappear or stack.
* **Refresh cadence.** ``RUSSELL_BIOTECH_REFRESH_HOURS=24`` (env or
  ``--max-age-hours``) gates the SEC fetch path: when the most recent
  ``russell2k_biotech.fetched_at`` is younger than the threshold, the
  writer logs an INFO short-circuit and exits 0 without re-classifying
  any tickers. ``--refresh`` (the documented cron flag) overrides the
  cache and forces a full refresh.

CLI
---

The module exposes a ``python -m`` entrypoint suitable for cron::

    python -m biotech_sniper.universe.russell_biotech --refresh

Flags
~~~~~

* ``--refresh`` — force a full refresh regardless of the cache.
* ``--db PATH`` — override the target SQLite file.
* ``--max-age-hours N`` — short-circuit when the latest snapshot is
  younger than ``N`` hours. ``None`` reads
  :envvar:`RUSSELL_BIOTECH_REFRESH_HOURS` from the environment.
* ``--dry-run`` — compute the new snapshot but do not write to SQLite.
* ``--allow-shrinkage`` — bypass the 25 % shrinkage guard.
* ``--emit-stats`` — print the run summary as a single JSON object on
  stdout (consumable by ``jq`` / cron-log scrapers).

Exit codes
~~~~~~~~~~

* ``0`` — success (rows written, dry-run completed, or fresh-enough
  cache hit).
* ``2`` — upstream unavailable (SEC EDGAR transport failure that the
  classifier could not recover from).
* ``4`` — :class:`ShrinkageRefusal` (new snapshot is < 75 % of prior;
  prior preserved).
* ``5`` — :class:`MissingIWMSnapshot` (no IWM snapshot present —
  caller must run ``iwm_importer`` first).
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Final, Iterable, Sequence

from biotech_sniper import db
from biotech_sniper.classifiers.sec_sic import (
    BIOTECH_SIC_CODES,
    SECClassifierError,
    SECSICClassifier,
    SECTransientError,
    SICResolution,
)
from biotech_sniper.classifiers.sec_sic import (
    ensure_cik_sic_cache_table as _ensure_cik_sic_cache_table,
)
from biotech_sniper.paths import DATA_DIR, ensure_data_dir
from biotech_sniper.universe.iwm_importer import (
    ensure_iwm_snapshot_table as _ensure_iwm_snapshot_table,
)

__all__ = [
    "BIOTECH_SIC_CODES",
    "DEFAULT_REFRESH_HOURS",
    "DEFAULT_SHRINKAGE_FLOOR_RATIO",
    "EXIT_OK",
    "EXIT_UPSTREAM_UNAVAILABLE",
    "EXIT_SHRINKAGE_REFUSED",
    "EXIT_NO_IWM_SNAPSHOT",
    "RussellBiotechError",
    "ShrinkageRefusal",
    "MissingIWMSnapshot",
    "BiotechCandidate",
    "RefreshResult",
    "default_db_path",
    "ensure_russell2k_biotech_table",
    "load_latest_iwm_snapshot",
    "classify_candidates",
    "refresh_russell2k_biotech",
    "_passes_shrinkage_floor",
    "main",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Default refresh cadence in hours. Mirrors the
#: ``RUSSELL_BIOTECH_REFRESH_HOURS`` env var documented in
#: ``library/environment.md``. Setting the env var (or
#: ``--max-age-hours``) to ``0`` disables the cache and forces a
#: re-fetch.
DEFAULT_REFRESH_HOURS: Final[int] = 24

#: Shrinkage guard: the new snapshot must contain at least this
#: fraction of the prior snapshot's row count, otherwise the writer
#: refuses to commit. ``0.75`` ⇔ "no more than 25 % shrinkage".
DEFAULT_SHRINKAGE_FLOOR_RATIO: Final[float] = 0.75

#: Documented exit codes (stable contract for cron + validators).
EXIT_OK: Final[int] = 0
EXIT_UPSTREAM_UNAVAILABLE: Final[int] = 2
EXIT_SHRINKAGE_REFUSED: Final[int] = 4
EXIT_NO_IWM_SNAPSHOT: Final[int] = 5


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RussellBiotechError(Exception):
    """Base class for typed russell_biotech errors."""


class ShrinkageRefusal(RussellBiotechError):
    """The new snapshot is < ``floor_ratio`` × prior snapshot.

    The writer rolls the transaction back; the prior snapshot is
    preserved verbatim. Cron operators see exit code
    :data:`EXIT_SHRINKAGE_REFUSED`. Suppressed by
    ``--allow-shrinkage`` when an operator deliberately wants to
    accept the shrunken result.
    """


class MissingIWMSnapshot(RussellBiotechError):
    """There is no IWM holdings snapshot to intersect against.

    Almost always means ``iwm_importer`` has not yet been run on the
    target DB. Exit code :data:`EXIT_NO_IWM_SNAPSHOT`.
    """


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_RUSSELL2K_BIOTECH_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS russell2k_biotech (
    ticker                TEXT    NOT NULL PRIMARY KEY,
    cik                   TEXT    NOT NULL,
    sic                   INTEGER NOT NULL CHECK(sic IN (2834, 2836, 8731)),
    sic_description       TEXT,
    iwm_weight            REAL,
    iwm_market_value_usd  REAL,
    as_of_date            TEXT    NOT NULL,
    fetched_at            TEXT    NOT NULL
)
"""


_RUSSELL2K_BIOTECH_INDEXES: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_russell2k_biotech_sic "
    "ON russell2k_biotech(sic)",
    "CREATE INDEX IF NOT EXISTS idx_russell2k_biotech_fetched_at "
    "ON russell2k_biotech(fetched_at)",
    "CREATE INDEX IF NOT EXISTS idx_russell2k_biotech_as_of_date "
    "ON russell2k_biotech(as_of_date)",
)


def ensure_russell2k_biotech_table(conn: sqlite3.Connection) -> None:
    """Idempotently create the ``russell2k_biotech`` table.

    Safe to call against a v9 (pre-migration) or v10 (post-migration)
    database — both flavours converge on the same DDL. The forthcoming
    ``010_reading_b_foundations.py`` migration declares an identical
    schema so calling :func:`ensure_russell2k_biotech_table` after the
    migration is a no-op.
    """
    conn.execute(_RUSSELL2K_BIOTECH_DDL)
    for stmt in _RUSSELL2K_BIOTECH_INDEXES:
        conn.execute(stmt)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BiotechCandidate:
    """One candidate row destined for ``russell2k_biotech``.

    Combines an IWM holdings row with its SEC EDGAR SIC resolution.
    The ``sic`` field is guaranteed to be in :data:`BIOTECH_SIC_CODES`
    by construction — non-biotech tickers are filtered out before a
    :class:`BiotechCandidate` is instantiated.
    """

    ticker: str
    cik: str
    sic: int
    sic_description: str | None
    iwm_weight: float | None
    iwm_market_value_usd: float | None
    as_of_date: str


@dataclass
class RefreshResult:
    """Structured summary of one refresh cycle."""

    db_path: str
    as_of_date: str = ""
    fetched_at: str = ""
    iwm_tickers_considered: int = 0
    sic_resolved: int = 0
    sic_unresolved: int = 0
    sic_transient_errors: int = 0
    biotech_matches: int = 0
    rows_written: int = 0
    prior_row_count: int = 0
    new_row_count: int = 0
    shrinkage_ratio: float | None = None
    refresh_skipped: bool = False
    skip_reason: str | None = None
    shrinkage_refused: bool = False
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


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _now_iso() -> str:
    return _now_utc().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _resolve_refresh_hours(explicit: int | None) -> int:
    """Resolve ``--max-age-hours`` from CLI flag or env.

    Order of precedence:

    1. Explicit CLI flag.
    2. :envvar:`RUSSELL_BIOTECH_REFRESH_HOURS` env var (integer).
    3. :data:`DEFAULT_REFRESH_HOURS`.

    Non-integer env values fall back to the default with a WARNING
    log so cron operators notice the typo without crashing the run.
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


def _latest_snapshot_age_hours(
    conn: sqlite3.Connection, *, now: datetime.datetime
) -> float | None:
    """Return age (hours) of the most recent ``russell2k_biotech`` row.

    Returns ``None`` when the table is empty (no prior snapshot) so
    callers force a fetch.
    """
    try:
        row = conn.execute(
            "SELECT MAX(fetched_at) AS latest FROM russell2k_biotech"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    latest = row[0] if not isinstance(row, sqlite3.Row) else row["latest"]
    if not latest:
        return None
    try:
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
# IWM snapshot loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _IWMRow:
    ticker: str
    weight: float | None
    market_value_usd: float | None
    as_of_date: str


def load_latest_iwm_snapshot(conn: sqlite3.Connection) -> list[_IWMRow]:
    """Return the most recent ``iwm_holdings_snapshot`` rows.

    "Most recent" is keyed on ``MAX(as_of_date)`` so a stale partial
    write from an earlier day cannot pollute the result. Returns an
    empty list when the snapshot is missing entirely.
    """
    try:
        max_row = conn.execute(
            "SELECT MAX(as_of_date) FROM iwm_holdings_snapshot"
        ).fetchone()
    except sqlite3.OperationalError:
        return []
    if max_row is None:
        return []
    latest_date = max_row[0]
    if not latest_date:
        return []
    cursor = conn.execute(
        "SELECT ticker, weight, market_value_usd, as_of_date "
        "FROM iwm_holdings_snapshot "
        "WHERE as_of_date = ? AND asset_class = 'Equity' "
        "ORDER BY ticker",
        (latest_date,),
    )
    return [
        _IWMRow(
            ticker=row[0],
            weight=row[1],
            market_value_usd=row[2],
            as_of_date=row[3],
        )
        for row in cursor.fetchall()
    ]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_candidates(
    iwm_rows: Sequence[_IWMRow],
    classifier: SECSICClassifier,
    *,
    max_transient_errors: int = 25,
) -> tuple[list[BiotechCandidate], dict[str, int]]:
    """Walk ``iwm_rows`` and return biotech candidates.

    Each ticker is resolved through ``classifier``. Tickers whose SIC
    is in :data:`BIOTECH_SIC_CODES` become :class:`BiotechCandidate`
    rows; all others are silently dropped.

    ``SECTransientError`` is tolerated up to ``max_transient_errors``
    times — beyond that the function re-raises the most recent
    transient so the caller can short-circuit and try again on the
    next cron tick (no partial commit). Schema-level errors
    (``SECSchemaError``) bubble up immediately because they indicate
    an upstream contract break.
    """
    matches: list[BiotechCandidate] = []
    stats = {
        "iwm_tickers_considered": len(iwm_rows),
        "sic_resolved": 0,
        "sic_unresolved": 0,
        "sic_transient_errors": 0,
        "biotech_matches": 0,
    }
    last_transient: SECTransientError | None = None
    for row in iwm_rows:
        try:
            resolution = classifier.resolve_ticker_sic(row.ticker)
        except SECTransientError as exc:
            stats["sic_transient_errors"] += 1
            last_transient = exc
            if stats["sic_transient_errors"] > max_transient_errors:
                # Too many in a row — bail out so we don't burn the
                # SEC fair-access budget on a clearly broken endpoint.
                raise
            continue
        if resolution is None or resolution.sic is None:
            stats["sic_unresolved"] += 1
            continue
        stats["sic_resolved"] += 1
        if resolution.sic not in BIOTECH_SIC_CODES:
            continue
        matches.append(
            BiotechCandidate(
                ticker=resolution.ticker,
                cik=resolution.cik,
                sic=resolution.sic,
                sic_description=resolution.sic_description,
                iwm_weight=row.weight,
                iwm_market_value_usd=row.market_value_usd,
                as_of_date=row.as_of_date,
            )
        )
    stats["biotech_matches"] = len(matches)
    return matches, stats


# ---------------------------------------------------------------------------
# Shrinkage predicate
# ---------------------------------------------------------------------------


def _passes_shrinkage_floor(
    prior_count: int, new_count: int, floor_ratio: float
) -> bool:
    """Return ``True`` iff ``new_count`` is at or above the shrinkage floor.

    The predicate is ``new_count / prior_count >= floor_ratio`` — i.e. the
    new snapshot must retain at least ``floor_ratio`` × prior rows. The
    comparison is performed in float space (``new_count * 1.0 >=
    prior_count * floor_ratio``) so the boundary is the EXACT real-valued
    cutoff rather than a truncated integer threshold. (The earlier
    implementation used ``new_count < int(prior_count * floor_ratio)``,
    which truncated the threshold and admitted forbidden shrinkage on
    non-divisible priors — e.g. ``prior=101, ratio=0.75`` truncated to
    ``75`` and admitted ``new=75`` (a 25.74 % shrink). See feature
    ``f-fix-m1-03-russell-shrinkage-precision`` / VAL-M1-057.)

    A ``prior_count`` of ``0`` is treated as "no prior baseline", which
    cannot be shrunk — the predicate returns ``True`` unconditionally so
    a fresh first-ever load is never refused.
    """
    if prior_count <= 0:
        return True
    # Float comparison — symmetric, no truncation. Equivalent (modulo
    # float rounding for representable ratios) to:
    #   new_count * 100 >= prior_count * int(floor_ratio * 100)
    # for the documented ``floor_ratio = 0.75`` case.
    return float(new_count) >= float(prior_count) * float(floor_ratio)


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def _atomic_replace(
    conn: sqlite3.Connection,
    candidates: Sequence[BiotechCandidate],
    *,
    fetched_at: str,
    floor_ratio: float,
    allow_shrinkage: bool,
) -> tuple[int, int, float | None]:
    """Replace every ``russell2k_biotech`` row inside one transaction.

    Returns ``(prior_count, new_count, shrinkage_ratio)``. Raises
    :class:`ShrinkageRefusal` (after rolling the transaction back)
    when the new count is less than ``floor_ratio`` × prior count and
    ``allow_shrinkage`` is ``False``. ``shrinkage_ratio`` is
    ``new_count / prior_count`` (``None`` when ``prior_count == 0``).

    Uses ``BEGIN IMMEDIATE`` so the writer holds the SQLite "RESERVED"
    lock for the duration of the transaction. Concurrent readers
    continue to see the prior committed snapshot until COMMIT lands —
    this is the durability anchor for the
    "concurrent readers never see < N rows" invariant.
    """
    # Snapshot the prior row count BEFORE the BEGIN IMMEDIATE so a
    # failed BEGIN cannot mask a real shrinkage. Reads outside the
    # transaction are cheap and consistent with the pre-write state.
    (prior_count,) = conn.execute(
        "SELECT COUNT(*) FROM russell2k_biotech"
    ).fetchone()

    new_count = len(candidates)
    shrinkage_ratio: float | None
    if prior_count > 0:
        shrinkage_ratio = new_count / float(prior_count)
    else:
        shrinkage_ratio = None

    if (
        prior_count > 0
        and not allow_shrinkage
        and not _passes_shrinkage_floor(prior_count, new_count, floor_ratio)
    ):
        raise ShrinkageRefusal(
            f"new snapshot has {new_count} rows vs prior {prior_count} "
            f"(ratio={shrinkage_ratio:.3f} < floor={floor_ratio:.3f}); "
            "refusing to commit. Pass --allow-shrinkage to override."
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM russell2k_biotech")
        if candidates:
            payload = [
                (
                    c.ticker,
                    c.cik,
                    c.sic,
                    c.sic_description,
                    c.iwm_weight,
                    c.iwm_market_value_usd,
                    c.as_of_date,
                    fetched_at,
                )
                for c in candidates
            ]
            conn.executemany(
                "INSERT INTO russell2k_biotech ("
                "ticker, cik, sic, sic_description, iwm_weight, "
                "iwm_market_value_usd, as_of_date, fetched_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise

    return prior_count, new_count, shrinkage_ratio


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def refresh_russell2k_biotech(
    *,
    db_path: Path | str | None = None,
    classifier: SECSICClassifier | None = None,
    max_age_hours: int | None = None,
    dry_run: bool = False,
    allow_shrinkage: bool = False,
    floor_ratio: float = DEFAULT_SHRINKAGE_FLOOR_RATIO,
    now: datetime.datetime | None = None,
) -> RefreshResult:
    """Run one refresh cycle: load IWM → classify → atomic write.

    Parameters
    ----------
    db_path:
        Override the target SQLite path. ``None`` resolves to
        :func:`default_db_path`.
    classifier:
        Optional :class:`SECSICClassifier` instance (used by tests
        to inject a fake). When ``None`` a fresh classifier is
        constructed against ``db_path``.
    max_age_hours:
        Short-circuit the refresh when the most recent
        ``russell2k_biotech`` row is younger than this many hours.
        ``None`` reads :envvar:`RUSSELL_BIOTECH_REFRESH_HOURS`.
        ``0`` disables the cache.
    dry_run:
        Compute the new snapshot but do not write to SQLite.
    allow_shrinkage:
        Bypass the 25 % shrinkage guard.
    floor_ratio:
        Override the shrinkage floor ratio (defaults to
        :data:`DEFAULT_SHRINKAGE_FLOOR_RATIO`).
    now:
        Override the current UTC time (used by tests for
        deterministic ``fetched_at`` / ``as_of_date`` stamps).

    Returns
    -------
    RefreshResult
        Structured summary of the run.

    Raises
    ------
    MissingIWMSnapshot
        No IWM holdings snapshot present — caller must run
        :mod:`biotech_sniper.universe.iwm_importer` first.
    ShrinkageRefusal
        New snapshot has < ``floor_ratio`` × prior row count.
    SECTransientError
        Persistent SEC EDGAR transport failure (bubbled up from
        the classifier after :func:`classify_candidates` exhausts
        its tolerance).
    """
    target_db = Path(db_path) if db_path is not None else default_db_path()
    refresh_hours = _resolve_refresh_hours(max_age_hours)
    now = now or _now_utc()
    fetched_at = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    result = RefreshResult(
        db_path=str(target_db),
        fetched_at=fetched_at,
        dry_run=dry_run,
    )

    ensure_data_dir()

    # --- bootstrap dependent tables -----------------------------------
    # Idempotent CREATE-IF-NOT-EXISTS so we never trip on a v9 db.
    bootstrap_conn = db.connect(target_db)
    try:
        _ensure_iwm_snapshot_table(bootstrap_conn)
        _ensure_cik_sic_cache_table(bootstrap_conn)
        ensure_russell2k_biotech_table(bootstrap_conn)
        bootstrap_conn.commit()

        # Cache short-circuit only on real refreshes (dry-run is an
        # operator override whose intent is to bypass the cache).
        if not dry_run and refresh_hours > 0:
            age = _latest_snapshot_age_hours(bootstrap_conn, now=now)
            if age is not None and age < refresh_hours:
                logger.info(
                    "russell_biotech.skip reason=fresh_enough "
                    "age_hours=%.2f max_age_hours=%d",
                    age,
                    refresh_hours,
                )
                result.refresh_skipped = True
                result.skip_reason = (
                    f"fresh_enough age_hours={age:.2f} "
                    f"max_age_hours={refresh_hours}"
                )
                (prior_count,) = bootstrap_conn.execute(
                    "SELECT COUNT(*) FROM russell2k_biotech"
                ).fetchone()
                result.prior_row_count = prior_count
                result.new_row_count = prior_count
                result.completed_at = _now_iso()
                return result
    finally:
        bootstrap_conn.close()

    # --- load IWM snapshot --------------------------------------------
    read_conn = db.connect(target_db)
    try:
        iwm_rows = load_latest_iwm_snapshot(read_conn)
    finally:
        read_conn.close()

    if not iwm_rows:
        raise MissingIWMSnapshot(
            f"iwm_holdings_snapshot is empty in {target_db!s}; "
            "run biotech_sniper.universe.iwm_importer first."
        )

    result.iwm_tickers_considered = len(iwm_rows)
    result.as_of_date = iwm_rows[0].as_of_date

    # --- classify -----------------------------------------------------
    if classifier is None:
        classifier = SECSICClassifier(db_path=target_db)
    candidates, stats = classify_candidates(iwm_rows, classifier)
    result.sic_resolved = stats["sic_resolved"]
    result.sic_unresolved = stats["sic_unresolved"]
    result.sic_transient_errors = stats["sic_transient_errors"]
    result.biotech_matches = stats["biotech_matches"]

    # --- write --------------------------------------------------------
    if dry_run:
        # We still want to report prior_row_count for operator
        # visibility — open a read-only cursor to fetch it.
        ro_conn = db.connect(target_db)
        try:
            (prior_count,) = ro_conn.execute(
                "SELECT COUNT(*) FROM russell2k_biotech"
            ).fetchone()
        finally:
            ro_conn.close()
        result.prior_row_count = prior_count
        result.new_row_count = len(candidates)
        if prior_count > 0:
            result.shrinkage_ratio = len(candidates) / float(prior_count)
        result.completed_at = _now_iso()
        logger.info(
            "russell_biotech.dry_run iwm=%d biotech_matches=%d "
            "prior=%d",
            len(iwm_rows),
            len(candidates),
            prior_count,
        )
        return result

    write_conn = db.connect(target_db)
    try:
        try:
            prior_count, new_count, ratio = _atomic_replace(
                write_conn,
                candidates,
                fetched_at=fetched_at,
                floor_ratio=floor_ratio,
                allow_shrinkage=allow_shrinkage,
            )
        except ShrinkageRefusal as exc:
            (prior_count,) = write_conn.execute(
                "SELECT COUNT(*) FROM russell2k_biotech"
            ).fetchone()
            result.prior_row_count = prior_count
            result.new_row_count = len(candidates)
            if prior_count > 0:
                result.shrinkage_ratio = len(candidates) / float(prior_count)
            result.shrinkage_refused = True
            result.completed_at = _now_iso()
            logger.error("russell_biotech.shrinkage_refused %s", exc)
            raise
    finally:
        write_conn.close()

    result.prior_row_count = prior_count
    result.new_row_count = new_count
    result.rows_written = new_count
    result.shrinkage_ratio = ratio
    result.completed_at = _now_iso()
    logger.info(
        "russell_biotech.complete rows_written=%d prior=%d ratio=%s",
        new_count,
        prior_count,
        f"{ratio:.3f}" if ratio is not None else "n/a",
    )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.universe.russell_biotech",
        description=(
            "Intersect the latest iwm_holdings_snapshot with the SEC "
            "EDGAR SIC classifier and write biotech-only rows to "
            "russell2k_biotech (atomic refresh; 25%% shrinkage guard; "
            "RUSSELL_BIOTECH_REFRESH_HOURS=24 cadence)."
        ),
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "Force a full refresh regardless of the cache (cron "
            "entrypoint). Equivalent to --max-age-hours 0."
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
        "--max-age-hours",
        type=int,
        default=None,
        help=(
            "Short-circuit when the latest russell2k_biotech row is "
            "younger than N hours. Defaults to "
            "RUSSELL_BIOTECH_REFRESH_HOURS or 24."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Compute the new snapshot but do not write to SQLite. "
            "Implies a fresh classification (cache bypass)."
        ),
    )
    parser.add_argument(
        "--allow-shrinkage",
        action="store_true",
        help=(
            "Bypass the 25%% shrinkage guard. Use only when an "
            "operator deliberately wants to accept a smaller "
            "snapshot (e.g. during a documented universe correction)."
        ),
    )
    parser.add_argument(
        "--emit-stats",
        action="store_true",
        help="Print the run summary as a single JSON object on stdout.",
    )
    return parser


def _emit(stats: bool, result: RefreshResult) -> None:
    if stats:
        sys.stdout.write(result.to_json() + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint. See :mod:`biotech_sniper.universe.russell_biotech`."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else default_db_path()

    # ``--refresh`` is the documented cron flag and ALSO the way an
    # operator forces a re-run when a fresh snapshot exists. Setting
    # ``max_age_hours=0`` here disables the cache short-circuit for
    # this invocation only (env vars are not mutated).
    effective_max_age = args.max_age_hours
    if args.refresh and effective_max_age is None:
        effective_max_age = 0

    try:
        result = refresh_russell2k_biotech(
            db_path=db_path,
            max_age_hours=effective_max_age,
            dry_run=args.dry_run,
            allow_shrinkage=args.allow_shrinkage,
        )
    except MissingIWMSnapshot as exc:
        logger.error("russell_biotech.no_iwm_snapshot %s", exc)
        sys.stderr.write(
            f"ERROR: russell_biotech: no IWM snapshot present: {exc}\n"
        )
        return EXIT_NO_IWM_SNAPSHOT
    except ShrinkageRefusal as exc:
        sys.stderr.write(
            f"ERROR: russell_biotech: shrinkage refusal: {exc}\n"
        )
        return EXIT_SHRINKAGE_REFUSED
    except SECTransientError as exc:
        logger.error("russell_biotech.sec_transient %s", exc)
        sys.stderr.write(
            f"ERROR: russell_biotech: SEC transport failure: {exc}\n"
        )
        return EXIT_UPSTREAM_UNAVAILABLE
    except SECClassifierError as exc:
        logger.error("russell_biotech.sec_classifier_error %s", exc)
        sys.stderr.write(
            f"ERROR: russell_biotech: SEC classifier error: {exc}\n"
        )
        return EXIT_UPSTREAM_UNAVAILABLE

    _emit(args.emit_stats, result)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
