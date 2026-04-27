"""SQLite database access for the biotech_sniper package.

This module is the canonical entrypoint for opening connections to the
project database (``data/alpha_sniper.db``) and applying the schema.

Design goals
------------
* Single-source schema in :data:`SCHEMA_PATH` (``schema.sql``) so the
  layout is human-readable and reviewable in one file.
* All connections opened via :func:`connect` enable
  ``PRAGMA foreign_keys=ON`` and ``PRAGMA journal_mode=WAL`` so cron
  units can read concurrently without deadlocks.
* Schema migrations are idempotent and gated by the ``schema_version``
  table so re-running :func:`run_migrations` is a no-op once the
  current version has been applied.

Public API
----------
* :data:`SCHEMA_PATH` — absolute path to ``schema.sql``.
* :data:`CURRENT_VERSION` — integer version this codebase ships.
* :func:`connect` — open a connection with the project's PRAGMAs set.
* :func:`run_migrations` — apply ``schema.sql`` if not already applied
  and bump the ``schema_version`` row.
* :func:`current_schema_version` — read the latest applied version (or
  ``0`` when no migration has run yet).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Final, Union

__all__ = [
    "SCHEMA_PATH",
    "CURRENT_VERSION",
    "connect",
    "run_migrations",
    "current_schema_version",
    "split_sql_statements",
]


SCHEMA_PATH: Final[Path] = Path(__file__).resolve().parent / "schema.sql"

# Schema revision shipped by this codebase. Bump when ``schema.sql``
# changes shape. The migration script writes this value into
# ``schema_version`` after a successful apply.
#
# Versioning policy (post-f-m2-13):
#   * CURRENT_VERSION MUST be bumped any time ``schema.sql`` gains a
#     new ``CREATE TABLE`` or ``CREATE INDEX``. The unit test
#     ``tests/test_db_schema.py::test_current_version_matches_table_count``
#     locks this in by counting the CREATE-TABLE lines in
#     ``schema.sql`` and asserting CURRENT_VERSION matches.
#   * The migration applies ``schema.sql`` idempotently on every
#     connect (every CREATE statement uses ``IF NOT EXISTS``), so even
#     if a db drifts to a stale version row, re-running
#     :func:`run_migrations` will restore any missing tables.
CURRENT_VERSION: Final[int] = 4


PathLike = Union[str, Path]


def connect(db_path: PathLike) -> sqlite3.Connection:
    """Open a SQLite connection with project-standard PRAGMAs.

    Parameters
    ----------
    db_path:
        Filesystem path to the database file. The special value
        ``":memory:"`` is honoured (used by the smoke import in
        the validation contract).

    The returned connection has:

    * ``PRAGMA foreign_keys=ON`` (FK enforcement),
    * ``PRAGMA journal_mode=WAL`` for on-disk databases (skipped for
      in-memory databases since WAL is not supported there),
    * ``PRAGMA synchronous=NORMAL`` (WAL-safe durability),
    * ``row_factory = sqlite3.Row`` so callers can access columns by
      name in addition to positional indexing.

    The caller owns closing the connection.
    """

    # ``str()`` of a ``Path`` is the literal filesystem path; sqlite3
    # accepts both str and Path on Python 3.10+ but we normalise here
    # so debug logging is consistent.
    target = str(db_path)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON;")
    if target != ":memory:":
        # ``PRAGMA journal_mode=WAL`` is a no-op for ``:memory:`` and
        # spuriously prints ``memory`` instead — skip it there to keep
        # the smoke import quiet.
        conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def current_schema_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied schema version, or ``0`` if none.

    Returns ``0`` when the ``schema_version`` table does not yet
    exist (i.e. before the first :func:`run_migrations` call) so
    callers can treat "uninitialised db" and "version 0" the same.
    """

    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return 0
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    if row is None or row["v"] is None:
        return 0
    return int(row["v"])


def split_sql_statements(sql: str) -> list[str]:
    """Split a multi-statement SQL script into individual statements.

    The splitter is purpose-built for ``schema.sql``: it handles
    ``--`` line comments and single-quoted string literals (with
    ``''`` escapes). Multi-line C-style ``/* ... */`` comments are
    NOT supported because ``schema.sql`` does not use them — adding
    one would require updating this splitter.

    Empty statements (whitespace only after stripping comments) are
    dropped so the caller can blindly iterate the result and run
    each entry through :py:meth:`sqlite3.Connection.execute`.

    The returned strings are stripped of trailing semicolons and
    surrounding whitespace.
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)
    in_string = False
    while i < n:
        ch = sql[i]
        if in_string:
            buf.append(ch)
            if ch == "'":
                # SQLite escapes a single quote inside a string by
                # doubling it; consume the second quote and stay in
                # the string.
                if i + 1 < n and sql[i + 1] == "'":
                    buf.append(sql[i + 1])
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        # Outside a string literal: handle comments + statement boundary.
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            # Skip to end of line.
            j = sql.find("\n", i)
            if j == -1:
                break
            i = j + 1
            buf.append("\n")
            continue
        if ch == "'":
            in_string = True
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


# ALTER-TABLE migrations applied AFTER the idempotent schema.sql apply.
# Each tuple is ``(table, column, ddl_fragment)``. The migration step
# checks ``PRAGMA table_info(<table>)`` and only runs ``ALTER TABLE
# <table> ADD COLUMN <ddl_fragment>`` when the column is missing. This
# is the canonical pattern for adding nullable columns to existing
# databases without dropping data.
_ALTER_TABLE_ADD_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # f-m2-13 fix #2: ``note`` carries a free-form annotation when an
    # LLM call is logged but the body could not be parsed (e.g.
    # ``note='unparseable_response'``).
    ("llm_cost_ledger", "note", "note TEXT"),
)


def _apply_pending_alter_table_migrations(conn: sqlite3.Connection) -> None:
    """Run any ``ALTER TABLE ... ADD COLUMN`` migrations that are missing.

    Idempotent — checks each column with ``PRAGMA table_info`` before
    issuing the ALTER. Safe to call inside the same transaction as
    the ``schema.sql`` apply because none of these ALTERs commit
    implicitly.
    """
    for table, column, ddl in _ALTER_TABLE_ADD_COLUMNS:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column in cols:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def run_migrations(conn: sqlite3.Connection, target_version: int = CURRENT_VERSION) -> int:
    """Apply ``schema.sql`` and ALTER-TABLE migrations atomically.

    Behaviour (post-f-m2-13):

    1. Read ``schema.sql`` and split it into individual statements
       (see :func:`split_sql_statements`).
    2. Inside a single explicit transaction, execute every statement
       (each ``CREATE TABLE``/``CREATE INDEX`` uses ``IF NOT EXISTS``,
       so re-running is a no-op) and then run any pending
       ``ALTER TABLE ... ADD COLUMN`` migrations declared in
       :data:`_ALTER_TABLE_ADD_COLUMNS`.
    3. Insert/upsert the ``schema_version`` row with ``version =
       target_version``.

    Idempotency
    -----------
    Re-applying ``schema.sql`` on every connect is **intentional**: it
    means a database whose ``schema_version`` row drifted (e.g. earlier
    versions that forgot to bump ``CURRENT_VERSION`` when they added a
    table) self-heals on the next connect. Version drift can never
    cause silent table-skipping again.

    Atomicity
    ---------
    The earlier implementation called ``conn.executescript(schema_sql)``,
    which issues an implicit ``COMMIT`` before executing the script —
    if a later step (e.g. the ``schema_version`` insert) failed, the
    DDL was already committed. The new implementation runs every
    statement via ``conn.execute()`` inside ``with conn:`` so a
    failure rolls back the entire migration cleanly.
    """

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    statements = split_sql_statements(schema_sql)

    with conn:
        for stmt in statements:
            conn.execute(stmt)
        _apply_pending_alter_table_migrations(conn)
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, description) "
            "VALUES (?, ?)",
            (target_version, f"biotech_sniper schema v{target_version}"),
        )
    return target_version
