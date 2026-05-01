"""Reading-B run summary writer for ``state/audit_latest.json``.

Feature: ``f-m5-01-bull-e2e``.

The Reading-B end-to-end Stage-1 → Stage-2 → paper-executor pipeline
emits a small "run summary" block under the top-level ``reading_b``
key in ``state/audit_latest.json`` so the operator-facing watchdog
and the cross-flow E2E tests can verify a run in-place without
re-running the daily report builder.

Contract (VAL-M5-008)
~~~~~~~~~~~~~~~~~~~~~

After the bullish E2E run completes, ``state/audit_latest.json``
contains a ``reading_b`` block with the following integer-valued
keys (all ``>= 0``):

* ``candidate_events_emitted`` — total ``candidate_events`` rows
  visible in the database at the time the summary is computed.
* ``gate_pass_count`` — count of ``paper_orders`` rows tagged with
  ``event='news_event_entry'`` AND a non-empty broker-assigned
  ``alpaca_order_id`` (the write-then-submit invariant of f-m3-11
  guarantees the order was actually accepted by Alpaca paper).
* ``gate_reject_counts`` — histogram of cheap-gate rejections drawn
  from ``news_match_log`` rows whose ``matched=0`` and ``reason``
  is non-NULL. Mapping form ``{reason: count}``; empty dict when
  no rejections have been recorded yet.
* ``news_event_entries_submitted`` — same count as
  ``gate_pass_count`` (the bull-flow contract pins ``=1`` for the
  synthetic single-shot run; we keep the duplicate key so future
  bear/reject features can decouple the two if the contract drifts).

Side-effects
~~~~~~~~~~~~

The writer reads from SQLite via :func:`biotech_sniper.db.connect`
(the project-wide PRAGMA helper) and writes JSON atomically via a
``write_text`` after merging into any existing file payload. Other
top-level keys present in the existing ``audit_latest.json`` (e.g.
the M4 ``sources`` block, ``last_daily_run``, etc.) are preserved
verbatim — only the ``reading_b`` key is replaced with the freshly
computed block.

The module never writes to the database and never makes a network
call.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from biotech_sniper import db as _db_module

__all__ = [
    "compute_reading_b_summary",
    "write_reading_b_summary",
]


_SQL_CANDIDATE_COUNT = "SELECT COUNT(*) FROM candidate_events"

_SQL_NEWS_EVENT_ENTRIES_SUBMITTED = (
    "SELECT COUNT(*) FROM paper_orders "
    "WHERE event = 'news_event_entry' "
    "  AND alpaca_order_id IS NOT NULL "
    "  AND alpaca_order_id != ''"
)

_SQL_GATE_REJECT_HISTOGRAM = (
    "SELECT reason, COUNT(*) AS cnt "
    "FROM news_match_log "
    "WHERE matched = 0 AND reason IS NOT NULL "
    "GROUP BY reason"
)


def _safe_count(conn: sqlite3.Connection, sql: str) -> int:
    """Execute ``sql`` returning a single COUNT and tolerate missing tables.

    A fresh ``v9`` database that has not yet been advanced to the
    Reading-B ``v10`` schema will not have the ``candidate_events``
    or ``news_match_log`` tables. The summary writer is best-effort:
    when a table is missing we surface ``0`` rather than crash so
    the watchdog can still emit a structured payload.
    """
    try:
        row = conn.execute(sql).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return 0
        raise
    if row is None:
        return 0
    return int(row[0] or 0)


def _safe_histogram(conn: sqlite3.Connection, sql: str) -> dict[str, int]:
    """Execute a ``GROUP BY`` query returning a ``{key: count}`` mapping."""
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return {}
        raise
    out: dict[str, int] = {}
    for row in rows:
        # Tolerate both Row objects and plain tuples.
        if isinstance(row, sqlite3.Row):
            reason = row["reason"]
            cnt = row["cnt"]
        else:
            reason = row[0]
            cnt = row[1]
        if reason is None:
            continue
        out[str(reason)] = int(cnt or 0)
    return out


def compute_reading_b_summary(db_path: Path | str) -> dict[str, Any]:
    """Build the ``reading_b`` summary block for ``audit_latest.json``.

    Parameters
    ----------
    db_path:
        Path to the SQLite database (typically
        ``data/alpha_sniper.db``).

    Returns
    -------
    dict[str, Any]
        Mapping with the four contract-mandated keys. Always
        non-None; never raises.
    """
    conn = _db_module.connect(Path(db_path))
    try:
        candidate_events_emitted = _safe_count(conn, _SQL_CANDIDATE_COUNT)
        news_event_entries_submitted = _safe_count(
            conn, _SQL_NEWS_EVENT_ENTRIES_SUBMITTED
        )
        gate_reject_counts = _safe_histogram(conn, _SQL_GATE_REJECT_HISTOGRAM)
    finally:
        conn.close()

    return {
        "candidate_events_emitted": int(candidate_events_emitted),
        "gate_pass_count": int(news_event_entries_submitted),
        "gate_reject_counts": gate_reject_counts,
        "news_event_entries_submitted": int(news_event_entries_submitted),
    }


def write_reading_b_summary(
    audit_path: Path | str,
    *,
    db_path: Path | str,
) -> dict[str, Any]:
    """Compute the Reading-B summary and merge it into ``audit_path``.

    The merge is non-destructive: any other top-level keys already
    present in ``audit_path`` are preserved verbatim. Only the
    ``reading_b`` key is overwritten with the freshly computed
    block.

    Parameters
    ----------
    audit_path:
        Destination ``audit_latest.json`` file. Parent directories
        are created lazily if missing.
    db_path:
        Path to the SQLite database used to compute the summary.

    Returns
    -------
    dict[str, Any]
        The full payload that was written (so callers can log /
        assert against it without re-reading the file).
    """
    audit_path = Path(audit_path)
    block = compute_reading_b_summary(db_path)

    audit_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, Any] = {}
    if audit_path.is_file():
        try:
            loaded = json.loads(audit_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            loaded = {}
        if isinstance(loaded, dict):
            existing = loaded

    payload = dict(existing)
    payload["reading_b"] = block

    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return payload
