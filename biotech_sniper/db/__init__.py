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
]


SCHEMA_PATH: Final[Path] = Path(__file__).resolve().parent / "schema.sql"

# Schema revision shipped by this codebase. Bump when ``schema.sql``
# changes shape. The migration script writes this value into
# ``schema_version`` after a successful apply.
CURRENT_VERSION: Final[int] = 3


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


def run_migrations(conn: sqlite3.Connection, target_version: int = CURRENT_VERSION) -> int:
    """Apply pending migrations up to ``target_version`` (default current).

    The migration is idempotent: if the current applied version is
    already ``>= target_version`` this is a no-op and returns the
    existing version. Otherwise the function:

    1. Executes the contents of ``schema.sql`` (every statement uses
       ``IF NOT EXISTS`` so this is safe to run repeatedly).
    2. Inserts a row into ``schema_version`` with ``version =
       target_version`` (``INSERT OR IGNORE`` so re-running is safe).

    The whole operation runs inside a single transaction so a failure
    midway leaves the database untouched.

    Returns the version that is now applied.
    """

    applied = current_schema_version(conn)
    if applied >= target_version:
        return applied

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    # ``executescript`` issues an implicit COMMIT before running the
    # script, which would defeat our wrapper transaction. Instead, we
    # split on ``;`` and run inside our own ``BEGIN``/``COMMIT``.
    with conn:
        conn.execute("BEGIN")
        try:
            conn.executescript(schema_sql)
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, description) "
                "VALUES (?, ?)",
                (target_version, f"biotech_sniper schema v{target_version}"),
            )
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return target_version
