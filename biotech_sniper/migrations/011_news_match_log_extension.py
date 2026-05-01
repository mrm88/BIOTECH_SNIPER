"""Schema-version-11 migration: extend ``news_match_log`` with forensic columns.

Feature ``f-misc-09-news-match-log-v11-extension`` (surfaced 2026-05-01
from f-m5-03 audit-JSON workaround). The v10 ``news_match_log`` table
ships as a skeleton ``(ticker, news_event_id, matched, reason,
logged_at)`` form. Validation contract evidence at VAL-M5-018 /
VAL-M5-024 / VAL-M5-025 / VAL-M5-027 SELECTs forensic columns
(``candidate_event_id``, ``gate_outcome``, ``avg_probability``,
``cooldown_remaining_seconds``, ``today_total_usd``) that the v10
schema does not provide; f-m5-03 stashed those values on
``state/audit_latest.json::stage2_skipped[]`` as a parallel
persistence surface.

This v11 migration:

1. Adds the five forensic columns to ``news_match_log`` via
   ``ALTER TABLE ... ADD COLUMN`` (SQLite supports adding nullable
   columns without a table rebuild).
2. Each column has explicit ``DEFAULT NULL`` so existing rows
   remain valid post-migration (NULL on the legacy
   ``ticker / news_event_id / matched / reason / logged_at``
   skeleton).
3. Is idempotent: re-running on a v11 db is a no-op (each ALTER is
   guarded by a ``PRAGMA table_info`` membership check).

Out of scope (per AGENTS.md "Schema / Query Naming Map"): backfilling
the audit JSON forensic fields back into the SQL surface. The audit
JSON path stays as a parallel/legacy persistence surface for any
downstream tooling that already consumes it.

Usage
-----

::

    python -m biotech_sniper.migrations.runner --db data/alpha_sniper.db --target 11

The :func:`apply` function is also callable directly::

    from biotech_sniper.migrations._migration_v11 import apply  # via load_migration
    conn.execute("BEGIN")
    apply(conn)
    conn.execute("COMMIT")
"""

from __future__ import annotations

import sqlite3
from typing import Final

__all__ = [
    "FROM_VERSION",
    "TO_VERSION",
    "DESCRIPTION",
    "FORENSIC_COLUMNS",
    "apply",
]


# Version markers — module attributes the runner inspects via
# :func:`biotech_sniper.migrations.runner.load_migration`. Keep
# explicit so VAL-style greps for ``FROM_VERSION = 10`` /
# ``TO_VERSION = 11`` succeed without parsing.
FROM_VERSION: Final[int] = 10
TO_VERSION: Final[int] = 11
DESCRIPTION: Final[str] = (
    "news_match_log forensic columns "
    "(candidate_event_id, gate_outcome, avg_probability, "
    "cooldown_remaining_seconds, today_total_usd)"
)


# Five forensic columns added to ``news_match_log`` by this migration.
# Each tuple is ``(column_name, sqlite_type)``. All are nullable with
# explicit ``DEFAULT NULL`` so existing rows remain valid.
#
# * ``candidate_event_id`` — TEXT (kept TEXT rather than INTEGER FK so
#   audit consumers can stash composite ids / tokens without a schema
#   change; matches the contract evidence SELECT shape).
# * ``gate_outcome`` — TEXT, audit label of the gate decision
#   (``'rejected'`` for every ``record_stage2_skip`` write).
# * ``avg_probability`` — REAL, mean of the four provider
#   probabilities at probability-gate rejection time.
# * ``cooldown_remaining_seconds`` — REAL, seconds until the
#   ``ticker_cooldown`` window lifts at cooldown-gate rejection time.
# * ``today_total_usd`` — REAL, snapshotted Stage-2 ledger sum at
#   cap-gate rejection time.
FORENSIC_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("candidate_event_id", "TEXT"),
    ("gate_outcome", "TEXT"),
    ("avg_probability", "REAL"),
    ("cooldown_remaining_seconds", "REAL"),
    ("today_total_usd", "REAL"),
)


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the set of column names currently on ``table``.

    Used to gate ``ALTER TABLE ADD COLUMN`` on idempotent re-runs:
    the second invocation observes every column already present and
    short-circuits without issuing a duplicate ALTER (which SQLite
    would reject with ``duplicate column name``).
    """
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def apply(conn: sqlite3.Connection) -> None:
    """Apply the v10 → v11 forensic-column extension to ``news_match_log``.

    Idempotent: each column is added only when missing from the
    table's current ``PRAGMA table_info`` snapshot. The caller
    (``biotech_sniper.migrations.runner.run``) wraps this function in
    an explicit ``BEGIN IMMEDIATE`` / ``COMMIT`` envelope, so a
    SQLite error mid-loop rolls back atomically and leaves
    ``schema_version`` at 10.

    SQLite supports ``ALTER TABLE ... ADD COLUMN <name> <type>
    DEFAULT NULL`` on existing tables without a rebuild — the
    forensic columns are nullable, have NO ``NOT NULL`` constraint,
    and have NO ``CHECK`` constraint, so the ALTER is in-place and
    the existing row(s) are simply backfilled with NULL. This means
    the migration is O(1) on the ``news_match_log`` row count
    rather than O(N) — important for VPS deploys where the table
    may carry months of audit-trail rows.

    Side effects (on success):

    * Each of :data:`FORENSIC_COLUMNS` is present on
      ``news_match_log`` post-call.
    * Existing rows have ``NULL`` in every newly-added column.

    Re-running on a v11 db is a NO-OP — every ALTER is guarded.

    Raises
    ------
    sqlite3.Error
        Any SQLite-level error from a malformed DDL is re-raised so
        the caller's transaction rolls back atomically. The ALTER
        is gated on a column-presence check so the canonical
        ``duplicate column name`` race that occurs when two writers
        race the same migration cannot fire from this function.
    """
    existing = _existing_columns(conn, "news_match_log")
    for column_name, sqlite_type in FORENSIC_COLUMNS:
        if column_name in existing:
            # Idempotent re-run: the column already exists from a
            # prior apply, so skip the ALTER. PRAGMA membership is
            # the canonical check; the ``duplicate column name``
            # OperationalError swallow used in
            # :mod:`biotech_sniper.db._apply_pending_alter_table_migrations`
            # is unnecessary here because the runner's outer
            # transaction already serialises writers.
            continue
        # Each ALTER explicitly stamps ``DEFAULT NULL`` so existing
        # rows are backfilled deterministically. The default is
        # also what PRAGMA table_info reports for the column going
        # forward — useful for the validator's column-shape check.
        conn.execute(
            f"ALTER TABLE news_match_log "
            f"ADD COLUMN {column_name} {sqlite_type} DEFAULT NULL"
        )
