"""Backfill the legacy JSON state files into the SQLite database.

Usage
-----
    python -m biotech_sniper.migrations.migrate_json_to_sqlite \
        --source /path/to/state \
        --db    /path/to/alpha_sniper.db
    [--dry-run]

The migration is **idempotent**: running it twice over the same source
directory produces the same row counts (no duplicates, no deletions).
Idempotency is enforced both at the SQL level via
``UNIQUE``/``PRIMARY KEY`` constraints and inside this script via
``INSERT OR REPLACE`` / ``INSERT OR IGNORE`` statements.

Source files
~~~~~~~~~~~~
* ``active_plays.json``       — top-level dict with ``active`` and
  optional ``monitor`` sub-dicts keyed by ticker.
* ``resolved_plays.json``     — top-level dict with a ``resolved`` list
  of resolved-play records.
* ``performance_ledger.json`` — top-level dict with a ``plays`` map
  (ticker → record). Each record carries a ``snapshots`` list whose
  rows are aggregated per ``date`` into the ``performance_ledger``
  SQLite table.
* ``discovery_state.json``    — top-level dict with a ``seen_nct_ids``
  list (the 261 deduped NCT IDs that survived the f-m1-03 reorg).
* ``scoring_cache.json``      — optional. When present it is expected
  to be a list of ``{ticker, as_of_date, ...}`` records or a dict
  keyed by ticker. Missing file is allowed (no-op for that section).

The whole import runs inside a single transaction so a failure midway
rolls back cleanly. On success a structured JSON summary is printed to
stdout for downstream tooling.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from biotech_sniper import db
from biotech_sniper.paths import DATA_DIR, STATE_DIR

__all__ = [
    "MigrationResult",
    "main",
    "migrate",
    "CURRENT_VERSION",
]


# Re-exported so callers know which schema version this migration
# brings the database to. Tracks ``biotech_sniper.db.CURRENT_VERSION``.
CURRENT_VERSION: int = db.CURRENT_VERSION


# Files we consume. Optional files raise nothing when absent.
_REQUIRED_SOURCES = (
    "active_plays.json",
    "resolved_plays.json",
    "performance_ledger.json",
    "discovery_state.json",
)
_OPTIONAL_SOURCES = ("scoring_cache.json",)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class MigrationResult:
    """Summary returned by :func:`migrate` and printed on stdout."""

    plays_inserted: int = 0
    discovery_state_inserted: int = 0
    performance_ledger_inserted: int = 0
    scoring_cache_inserted: int = 0
    duration_ms: int = 0
    schema_version: int = CURRENT_VERSION
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_json(path: Path, *, optional: bool = False) -> Any:
    """Load JSON from ``path`` with actionable error messages.

    Missing files raise ``FileNotFoundError`` (unless ``optional``) so
    the migration fails fast and never partially commits.
    """

    if not path.exists():
        if optional:
            return None
        raise FileNotFoundError(
            f"Required source file not found: {path}"
        )
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        # Surface the offending file so operators can find it.
        raise json.JSONDecodeError(
            f"Failed to parse JSON file {path}: {exc.msg}",
            exc.doc,
            exc.pos,
        ) from exc


def _coerce_strike(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _coerce_int(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _coerce_pnl_usd(record: dict[str, Any]) -> float | None:
    """Best-effort extraction of P&L in USD from a resolved-play record.

    Resolved-play records use a mix of ``option_pnl_pct`` and an
    ``entry_fill`` price. The original JSONs do not store a USD figure
    directly. Compute approx USD using the per-play notional ($250)
    and the option_pnl_pct (mission risk default), so the column is
    populated and downstream aggregations remain stable.
    """

    pct = record.get("option_pnl_pct")
    if pct is None:
        return None
    try:
        return round(float(pct) / 100.0 * 250.0, 2)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Section: plays (active + monitor + resolved)
# ---------------------------------------------------------------------------


_PLAYS_UPSERT_SQL = """
INSERT INTO plays (
    source_key, ticker, nct_id, status, direction, catalyst_type,
    catalyst_date, entry_date, exit_date, option_type, option_strike,
    option_expiry, p_success, science_grade, entry_stock, exit_stock,
    entry_fill, pnl_usd, option_pnl_pct, stock_move_pct, payload,
    updated_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
ON CONFLICT(source_key) DO UPDATE SET
    ticker=excluded.ticker,
    nct_id=excluded.nct_id,
    status=excluded.status,
    direction=excluded.direction,
    catalyst_type=excluded.catalyst_type,
    catalyst_date=excluded.catalyst_date,
    entry_date=excluded.entry_date,
    exit_date=excluded.exit_date,
    option_type=excluded.option_type,
    option_strike=excluded.option_strike,
    option_expiry=excluded.option_expiry,
    p_success=excluded.p_success,
    science_grade=excluded.science_grade,
    entry_stock=excluded.entry_stock,
    exit_stock=excluded.exit_stock,
    entry_fill=excluded.entry_fill,
    pnl_usd=excluded.pnl_usd,
    option_pnl_pct=excluded.option_pnl_pct,
    stock_move_pct=excluded.stock_move_pct,
    payload=excluded.payload,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
"""


def _iter_active_play_rows(active_doc: Any) -> Iterable[tuple[Any, ...]]:
    """Yield SQL parameter tuples from ``active_plays.json``."""
    if not isinstance(active_doc, dict):
        return
    for status_label in ("active", "monitor"):
        bucket = active_doc.get(status_label)
        if not isinstance(bucket, dict):
            continue
        for ticker_key, record in bucket.items():
            if not isinstance(record, dict):
                continue
            ticker = str(record.get("ticker") or ticker_key)
            source_key = f"{status_label}:{ticker_key}"
            yield (
                source_key,
                ticker,
                record.get("nct_id"),
                status_label,
                record.get("direction"),
                record.get("catalyst_type"),
                record.get("estimated_announcement"),
                record.get("added_date") or record.get("entry_date"),
                None,  # exit_date — none for active
                record.get("option_type"),
                _coerce_strike(record.get("option_strike")),
                record.get("option_expiry"),
                _coerce_int(record.get("p_success")),
                record.get("science_grade"),
                _coerce_strike(record.get("entry_stock")),
                None,
                _coerce_strike(record.get("entry_fill")),
                None,
                None,
                None,
                json.dumps(record, sort_keys=True),
            )


def _iter_resolved_play_rows(resolved_doc: Any) -> Iterable[tuple[Any, ...]]:
    """Yield SQL parameter tuples from ``resolved_plays.json``."""
    if not isinstance(resolved_doc, dict):
        return
    items = resolved_doc.get("resolved")
    if not isinstance(items, list):
        return
    for record in items:
        if not isinstance(record, dict):
            continue
        ticker = str(record.get("ticker") or "")
        # ``ticker`` is sometimes encoded with strike ("IDYA_35C"). We
        # store the raw string in the ``ticker`` column for
        # round-tripping but also surface the original ticker prefix
        # via the JSON payload.
        original = record.get("original_play") or {}
        source_key = (
            f"resolved:{ticker}:{record.get('resolved_date') or ''}:"
            f"{record.get('strike') or original.get('option_strike') or ''}"
        )
        yield (
            source_key,
            ticker,
            original.get("nct_id"),
            "resolved",
            record.get("entry_direction") or original.get("direction"),
            record.get("entry_catalyst_type") or original.get("catalyst_type"),
            original.get("estimated_announcement"),
            record.get("entry_date") or original.get("added_date"),
            record.get("resolved_date"),
            original.get("option_type"),
            _coerce_strike(record.get("strike") or original.get("option_strike")),
            record.get("expiry") or original.get("option_expiry"),
            _coerce_int(record.get("entry_p_success") or original.get("p_success")),
            record.get("entry_science_grade") or original.get("science_grade"),
            _coerce_strike(record.get("entry_stock")),
            _coerce_strike(record.get("exit_price") or record.get("stock_at_exit")),
            _coerce_strike(record.get("entry_fill") or record.get("option_at_entry")),
            _coerce_pnl_usd(record),
            _coerce_strike(record.get("option_pnl_pct")),
            _coerce_strike(record.get("stock_move_pct")),
            json.dumps(record, sort_keys=True),
        )


def _migrate_plays(
    conn: sqlite3.Connection,
    source_dir: Path,
) -> int:
    """Upsert active + monitor + resolved plays. Returns total rows touched."""

    active_doc = _read_json(source_dir / "active_plays.json")
    resolved_doc = _read_json(source_dir / "resolved_plays.json")

    rows = list(_iter_active_play_rows(active_doc)) + list(
        _iter_resolved_play_rows(resolved_doc)
    )
    if rows:
        conn.executemany(_PLAYS_UPSERT_SQL, rows)
    return len(rows)


# ---------------------------------------------------------------------------
# Section: discovery_state
# ---------------------------------------------------------------------------


def _migrate_discovery_state(conn: sqlite3.Connection, source_dir: Path) -> int:
    """Backfill the deduped NCT-id seen-set."""

    doc = _read_json(source_dir / "discovery_state.json")
    if not isinstance(doc, dict):
        return 0
    nct_ids = doc.get("seen_nct_ids") or []
    if not isinstance(nct_ids, list):
        return 0
    last_run = doc.get("last_run") or None
    rows = [(str(nct_id), last_run, last_run, "seed") for nct_id in nct_ids if nct_id]
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO discovery_state (nct_id, first_seen_at, last_seen_at, source)
        VALUES (?, COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                   COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ','now')), ?)
        ON CONFLICT(nct_id) DO UPDATE SET
            last_seen_at = COALESCE(excluded.last_seen_at, discovery_state.last_seen_at),
            source       = COALESCE(discovery_state.source, excluded.source)
        """,
        rows,
    )
    return len(rows)


# ---------------------------------------------------------------------------
# Section: performance_ledger (per-day aggregation)
# ---------------------------------------------------------------------------


def _migrate_performance_ledger(conn: sqlite3.Connection, source_dir: Path) -> int:
    """Backfill per-day P&L attribution rows.

    The legacy JSON stores a per-ticker dict where each play has a
    list of ``snapshots`` keyed by ``date``. We aggregate across all
    plays on each unique date into a single row per date so the
    ``as_of_date`` PRIMARY KEY constraint holds and idempotency is
    SQL-enforced.
    """

    doc = _read_json(source_dir / "performance_ledger.json")
    if not isinstance(doc, dict):
        return 0
    plays_map = doc.get("plays") or {}
    if not isinstance(plays_map, dict):
        return 0

    per_date: dict[str, dict[str, float]] = {}
    for record in plays_map.values():
        if not isinstance(record, dict):
            continue
        is_resolved = bool(record.get("resolved")) or record.get("status") == "RESOLVED"
        for snap in record.get("snapshots") or []:
            if not isinstance(snap, dict):
                continue
            date = snap.get("date")
            if not date:
                continue
            pnl = snap.get("pnl_1k")
            try:
                pnl_usd = float(pnl) if pnl is not None else 0.0
            except (TypeError, ValueError):
                pnl_usd = 0.0
            slot = per_date.setdefault(
                date,
                {"realized": 0.0, "unrealized": 0.0, "play_count": 0},
            )
            if is_resolved:
                slot["realized"] += pnl_usd
            else:
                slot["unrealized"] += pnl_usd
            slot["play_count"] += 1

    if not per_date:
        return 0

    rows = [
        (
            date,
            round(slot["realized"], 4),
            round(slot["unrealized"], 4),
            int(slot["play_count"]),
            None,
        )
        for date, slot in sorted(per_date.items())
    ]
    conn.executemany(
        """
        INSERT INTO performance_ledger (
            as_of_date, realized_pnl_usd, unrealized_pnl_usd, play_count, notes
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(as_of_date) DO UPDATE SET
            realized_pnl_usd   = excluded.realized_pnl_usd,
            unrealized_pnl_usd = excluded.unrealized_pnl_usd,
            play_count         = excluded.play_count,
            notes              = excluded.notes
        """,
        rows,
    )
    return len(rows)


# ---------------------------------------------------------------------------
# Section: scoring_cache (optional)
# ---------------------------------------------------------------------------


def _iter_scoring_cache_rows(doc: Any) -> Iterable[tuple[Any, ...]]:
    """Yield SQL parameter tuples for the ``scoring_cache`` table.

    Accepts two shapes for the source JSON:
      * a dict keyed by ticker → record, OR
      * a list of records (each with ``ticker`` + ``as_of_date``).
    Records lacking a ``ticker`` or ``as_of_date`` are skipped.
    """

    def _row_for(record: dict[str, Any], ticker: str | None = None) -> tuple | None:
        ticker = str(record.get("ticker") or ticker or "").strip()
        as_of_date = record.get("as_of_date") or record.get("date")
        if not ticker or not as_of_date:
            return None
        return (
            ticker,
            str(as_of_date),
            _coerce_int(record.get("grok_rank")),
            _coerce_strike(record.get("grok_score")),
            record.get("claude_grade"),
            _coerce_strike(record.get("claude_probability")),
            record.get("gemini_grade"),
            _coerce_strike(record.get("gemini_probability")),
            record.get("science_grade"),
            _coerce_strike(record.get("ensemble_score")),
            1 if record.get("divergence_flag") else 0,
            json.dumps(record, sort_keys=True),
        )

    if isinstance(doc, dict):
        for ticker, record in doc.items():
            if isinstance(record, dict):
                row = _row_for(record, ticker=str(ticker))
                if row is not None:
                    yield row
    elif isinstance(doc, list):
        for record in doc:
            if isinstance(record, dict):
                row = _row_for(record)
                if row is not None:
                    yield row


def _migrate_scoring_cache(conn: sqlite3.Connection, source_dir: Path) -> int:
    """Optional: backfill scoring_cache from JSON when present."""
    doc = _read_json(source_dir / "scoring_cache.json", optional=True)
    if doc is None:
        return 0
    rows = list(_iter_scoring_cache_rows(doc))
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO scoring_cache (
            ticker, as_of_date, grok_rank, grok_score, claude_grade,
            claude_probability, gemini_grade, gemini_probability,
            science_grade, ensemble_score, divergence_flag, payload
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, as_of_date) DO UPDATE SET
            grok_rank          = excluded.grok_rank,
            grok_score         = excluded.grok_score,
            claude_grade       = excluded.claude_grade,
            claude_probability = excluded.claude_probability,
            gemini_grade       = excluded.gemini_grade,
            gemini_probability = excluded.gemini_probability,
            science_grade      = excluded.science_grade,
            ensemble_score     = excluded.ensemble_score,
            divergence_flag    = excluded.divergence_flag,
            payload            = excluded.payload
        """,
        rows,
    )
    return len(rows)


# ---------------------------------------------------------------------------
# Top-level migrate()
# ---------------------------------------------------------------------------


def migrate(
    *,
    source_dir: Path,
    db_path: Path,
    dry_run: bool = False,
) -> MigrationResult:
    """Run all migration sections inside a single transaction.

    On ``dry_run`` the database is opened in-memory so the schema and
    inserts are exercised but no on-disk file is created/modified.
    """

    started = time.monotonic()
    if not source_dir.exists():
        raise FileNotFoundError(
            f"Migration source directory does not exist: {source_dir}"
        )

    target = ":memory:" if dry_run else str(db_path)
    if not dry_run:
        db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = db.connect(target)
    try:
        db.run_migrations(conn)
        with conn:
            conn.execute("BEGIN")
            try:
                plays = _migrate_plays(conn, source_dir)
                discovery = _migrate_discovery_state(conn, source_dir)
                ledger = _migrate_performance_ledger(conn, source_dir)
                scoring = _migrate_scoring_cache(conn, source_dir)
            except Exception:
                conn.execute("ROLLBACK")
                raise
        result = MigrationResult(
            plays_inserted=plays,
            discovery_state_inserted=discovery,
            performance_ledger_inserted=ledger,
            scoring_cache_inserted=scoring,
            duration_ms=int((time.monotonic() - started) * 1000),
            schema_version=db.current_schema_version(conn),
            dry_run=dry_run,
        )
    finally:
        conn.close()
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.migrations.migrate_json_to_sqlite",
        description="Backfill the legacy biotech_sniper JSON state files into "
        "the SQLite database. Idempotent: safe to re-run.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=STATE_DIR,
        help="Directory containing the legacy state JSON files. "
        "Defaults to STATE_DIR (the project ``state/`` directory).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DATA_DIR / "alpha_sniper.db",
        help="Path to the SQLite database to write. Defaults to "
        "DATA_DIR/alpha_sniper.db.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the migration in-memory without touching --db.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        result = migrate(
            source_dir=args.source.expanduser(),
            db_path=args.db.expanduser(),
            dry_run=args.dry_run,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
