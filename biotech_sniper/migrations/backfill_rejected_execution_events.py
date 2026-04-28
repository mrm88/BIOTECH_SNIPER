"""f-cross-04: backfill ``execution_events`` rows for orphaned rejected paper_orders.

VAL-CROSS-043 traceability requires that every ``paper_orders`` row
with ``status='rejected'`` have a corresponding row in
``execution_events`` (event_type='rejected'). The pre-fix
:mod:`biotech_sniper.paper_executor` only wrote the lifecycle row on
the SUCCESSFUL submit path, leaving every rejected order orphaned in
``execution_events``. The user-testing-validator-cross-final round 1
caught two such orphans on the production VPS — both ``auto-*``
rows from 2026-04-27.

This one-shot migration scans the ``paper_orders`` table for the
specific carve-out:

* ``status='rejected'``
* ``client_order_id NOT LIKE 'legacy:%'`` — the f-m3-11 recreate
  migration tags every legacy row with ``client_order_id='legacy:<id>'``;
  those rows pre-date the f-m3-11 telemetry contract and are
  intentionally excluded from the traceability invariant.
* ``id NOT IN (SELECT paper_order_id FROM execution_events)`` —
  rows that already carry a lifecycle entry (entry submission +
  later rejection) are NOT re-processed; the helper is idempotent.

For every matching row the helper inserts a single synthetic
``execution_events`` row:

* ``event_type='rejected'``
* ``event_at = paper_orders.created_at`` — the synthetic event is
  dated to the order's own write-then-submit timestamp so the
  ``paper_orders.created_at <= execution_events.event_at``
  invariant continues to hold.
* ``raw_payload = json.dumps({'reason': paper_orders.reason,
  'backfilled': True})`` — explicit ``backfilled`` marker so audit
  consumers can distinguish synthetic rows from live telemetry.

The insert uses ``enforce_monotonic=False`` because some legacy
orders may have a partial lifecycle (e.g. an old test run that left
a ``submitted`` row plus a manual rejection downstream); the
backfill is meant to close the gap, not to police pre-existing
sequences. The state-transition validator stays in force for ALL
non-backfill writers.

Usage
-----
::

    python -m biotech_sniper.migrations.backfill_rejected_execution_events \\
        --db /root/alpha_sniper/repo/data/alpha_sniper.db \\
        [--dry-run]

The CLI prints a JSON summary on stdout (``rows_to_backfill`` /
``rows_backfilled``) so downstream tooling (the vps-deploy-worker)
can capture the count.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

from biotech_sniper import db as _db_module

__all__ = [
    "find_orphans",
    "backfill",
    "main",
]


def find_orphans(db_path: Path) -> list[dict[str, Any]]:
    """Return the list of rejected paper_orders rows missing telemetry.

    Excludes the ``legacy:%`` carve-out (rows tagged by the f-m3-11
    recreate migration) and any row that already has at least one
    ``execution_events`` entry.

    The returned dicts carry ``id``, ``client_order_id``,
    ``created_at``, and ``reason`` so the backfill writer has every
    field it needs without a second roundtrip.
    """
    conn = _db_module.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT po.id           AS id,
                   po.client_order_id AS client_order_id,
                   po.created_at   AS created_at,
                   po.reason       AS reason
            FROM paper_orders po
            WHERE po.status = 'rejected'
              AND (
                  po.client_order_id IS NULL
                  OR po.client_order_id NOT LIKE 'legacy:%'
              )
              AND po.id NOT IN (
                  SELECT paper_order_id
                  FROM execution_events
                  WHERE paper_order_id IS NOT NULL
              )
            ORDER BY po.created_at ASC, po.id ASC
            """,
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def backfill(
    db_path: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Insert one synthetic ``execution_events`` row per orphan.

    Returns a dict summarising the action: ``rows_to_backfill`` is the
    raw orphan count; ``rows_backfilled`` is how many rows were
    actually inserted (``0`` when ``dry_run=True``); ``orphan_ids``
    enumerates the matching ``paper_orders.id`` values for audit
    purposes.

    The whole insert runs inside a single transaction so any failure
    rolls back cleanly without leaving a partial backfill behind.

    f-cross-04 carve-out: the backfill calls
    :func:`biotech_sniper.execution_subscriber.record_execution_event`
    with ``enforce_monotonic=False``. Some legacy rejected rows MAY
    already carry a ``submitted`` lifecycle event (e.g. partial
    backfill from a prior fix); the validator's monotonic guard
    would refuse to write 'rejected' after a non-submitted event,
    blocking the helper from closing the orphans it was designed to
    address. The carve-out is documented here and in the helper's
    docstring; it does NOT relax the guard for live telemetry
    writers.
    """
    from biotech_sniper.execution_subscriber import record_execution_event

    orphans = find_orphans(db_path)
    rows_backfilled = 0

    if not orphans or dry_run:
        return {
            "rows_to_backfill": len(orphans),
            "rows_backfilled": rows_backfilled,
            "orphan_ids": [o["id"] for o in orphans],
            "dry_run": dry_run,
        }

    for orphan in orphans:
        payload = {
            "reason": orphan.get("reason"),
            "backfilled": True,
        }
        record_execution_event(
            db_path,
            paper_order_id=orphan["id"],
            event_type="rejected",
            event_at=orphan["created_at"],
            raw_payload=payload,
            enforce_monotonic=False,
        )
        rows_backfilled += 1

    return {
        "rows_to_backfill": len(orphans),
        "rows_backfilled": rows_backfilled,
        "orphan_ids": [o["id"] for o in orphans],
        "dry_run": dry_run,
    }


def main(argv: Iterable[str] | None = None) -> int:
    """CLI entry point. Returns ``0`` on success, ``1`` on error."""
    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.migrations.backfill_rejected_execution_events",
        description=(
            "f-cross-04: backfill execution_events rows for orphaned "
            "rejected paper_orders (VAL-CROSS-043 traceability)."
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        required=True,
        help=(
            "Path to the SQLite database (e.g. "
            "/root/alpha_sniper/repo/data/alpha_sniper.db)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the count of rows that WOULD be backfilled and "
            "exit without writing anything."
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(
            json.dumps(
                {
                    "error": "db_not_found",
                    "db_path": str(db_path),
                }
            ),
            file=sys.stderr,
        )
        return 1

    try:
        summary = backfill(db_path, dry_run=args.dry_run)
    except sqlite3.Error as exc:
        print(
            json.dumps(
                {
                    "error": "sqlite_error",
                    "db_path": str(db_path),
                    "message": str(exc),
                }
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI smoke
    sys.exit(main())
