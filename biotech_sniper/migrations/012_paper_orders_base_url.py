"""Schema-version-12 migration: add ``base_url`` column to ``paper_orders``.

Feature ``f-misc-10-paper-orders-base-url-column`` (surfaced 2026-05-01
from f-cross-02 discoveredIssue #1). The v11 ``paper_orders`` table
lacks a ``base_url`` column, which makes the SQL evidence form

    WHERE base_url NOT LIKE '%paper-api%'

(referenced in VAL-CROSS-009 evidence text and similar forensic
audit queries) non-runnable against the live DB. The paper-only
invariant is still satisfied operationally — the ``LIVE_MODE``
two-flag gate raises :class:`LiveTradingBlockedError` before SDK
construction; ``ALPACA_BASE_URL`` is pinned to the paper endpoint;
0 live-host POSTs across 30 days of journals — but adding the
column makes future audit queries directly runnable without
journal grepping.

This v12 migration:

1. Adds ``base_url TEXT DEFAULT 'https://paper-api.alpaca.markets'``
   to ``paper_orders`` via ``ALTER TABLE ... ADD COLUMN``. SQLite
   supports adding nullable columns with a literal ``DEFAULT`` value
   without a table rebuild — every existing row is backfilled with
   the default at ALTER time.
2. Is idempotent: re-running on a v12 db is a no-op (the ALTER is
   guarded by a ``PRAGMA table_info`` membership check).

Out of scope (per the feature description): backfilling historical
rows with environment-derived values. The default literal
``'https://paper-api.alpaca.markets'`` suffices because (a) it is
what every operational journal records and (b) the ``LIVE_MODE``
gate guarantees no other host could have been used.

Usage
-----

::

    python -m biotech_sniper.migrations.runner --db data/alpha_sniper.db --target 12

The :func:`apply` function is also callable directly through the
runner's ``load_migration`` dispatcher; legacy databases at v11
gain the column atomically inside the runner's ``BEGIN
IMMEDIATE`` / ``COMMIT`` envelope.
"""

from __future__ import annotations

import sqlite3
from typing import Final

__all__ = [
    "FROM_VERSION",
    "TO_VERSION",
    "DESCRIPTION",
    "DEFAULT_BASE_URL",
    "apply",
]


# Version markers — module attributes the runner inspects via
# :func:`biotech_sniper.migrations.runner.load_migration`.
FROM_VERSION: Final[int] = 11
TO_VERSION: Final[int] = 12
DESCRIPTION: Final[str] = (
    "paper_orders.base_url column with default "
    "'https://paper-api.alpaca.markets' (paper-only invariant)"
)


# The literal default value applied to every existing row at ALTER
# time AND to any future INSERT that omits the column. Tied to
# :data:`biotech_sniper.alpaca_client.PAPER_BASE_URL` by intent (we
# avoid importing it here to keep the migration module dependency-
# free — migrations should only touch ``sqlite3`` and stdlib).
DEFAULT_BASE_URL: Final[str] = "https://paper-api.alpaca.markets"


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the set of column names currently on ``table``.

    Used to gate ``ALTER TABLE ADD COLUMN`` on idempotent re-runs:
    the second invocation observes the column already present and
    short-circuits without issuing a duplicate ALTER (which SQLite
    would reject with ``duplicate column name``).
    """
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def apply(conn: sqlite3.Connection) -> None:
    """Apply the v11 → v12 ``paper_orders.base_url`` extension.

    Idempotent: the ALTER is guarded by a column-presence check so
    re-running the migration on a v12 db is a no-op. The caller
    (``biotech_sniper.migrations.runner.run`` or
    :func:`biotech_sniper.db.run_migrations` dispatcher) wraps this
    function in an explicit ``BEGIN IMMEDIATE`` / ``COMMIT`` envelope
    so a SQLite error rolls back atomically and leaves
    ``schema_version`` at 11.

    SQLite supports ``ALTER TABLE ... ADD COLUMN <name> <type>
    DEFAULT '<literal>'`` on existing tables without a rebuild — the
    new column is nullable, has NO ``NOT NULL`` constraint, and has
    NO ``CHECK`` constraint, so the ALTER is in-place and existing
    rows are backfilled with the literal default. This makes the
    migration O(1) on the ``paper_orders`` row count rather than
    O(N) — important for VPS deploys where the table may carry
    months of paper-trade history.

    Side effects (on success):

    * ``base_url`` column present on ``paper_orders`` post-call.
    * Existing rows have :data:`DEFAULT_BASE_URL` in the new column
      (SQLite's ``ALTER TABLE ADD COLUMN ... DEFAULT '<literal>'``
      semantics).

    Re-running on a v12 db is a NO-OP — the ALTER is guarded.

    Raises
    ------
    sqlite3.Error
        Any SQLite-level error from a malformed DDL is re-raised so
        the caller's transaction rolls back atomically.
    """
    if "base_url" in _existing_columns(conn, "paper_orders"):
        # Idempotent re-run: the column already exists from a prior
        # apply, so skip the ALTER. PRAGMA membership is the
        # canonical check; no need to swallow ``duplicate column
        # name`` from sqlite3 because the runner's outer transaction
        # already serialises writers.
        return
    # The literal default is single-quoted in the DDL — SQLite
    # stores the value verbatim (including the quotes' content) and
    # PRAGMA table_info reports it back wrapped in the same quotes.
    # The default is what existing rows get backfilled with at
    # ALTER time and what future INSERTs receive when they omit the
    # column.
    conn.execute(
        "ALTER TABLE paper_orders "
        f"ADD COLUMN base_url TEXT DEFAULT '{DEFAULT_BASE_URL}'"
    )
