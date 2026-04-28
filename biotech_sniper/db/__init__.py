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

import os
import sqlite3
import stat
import urllib.parse
from pathlib import Path
from typing import Final, Union

__all__ = [
    "SCHEMA_PATH",
    "CURRENT_VERSION",
    "DB_FILE_MODE",
    "connect",
    "connect_readonly",
    "run_migrations",
    "current_schema_version",
    "split_sql_statements",
    "cleanup_scoring_cache_chain_gate_violations",
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
CURRENT_VERSION: Final[int] = 9


# File mode applied to the on-disk SQLite database after every
# :func:`connect` call. The DB contains play data, LLM-debate
# transcripts, and configuration, so we lock down world access.
# ``0o640`` (rw- r-- ---) leaves the file readable by the owner's
# group so future systemd group access (e.g. a reporting unit running
# under a sibling user that shares the ``alpha-sniper`` group) does
# not require ``sudo``. See VAL-M2-001 for the contract assertion.
DB_FILE_MODE: Final[int] = 0o640


PathLike = Union[str, Path]


def _ensure_db_file_mode(path: Path, mode: int = DB_FILE_MODE) -> None:
    """Idempotently chmod ``path`` to ``mode`` if it differs from current.

    Called by :func:`connect` after the connection has been opened (and
    the file therefore exists on disk). The ``stat()`` is cheap; the
    ``chmod()`` is skipped entirely when the file already has the
    desired mode so we do not generate spurious filesystem writes on
    every connect (which happens many times per cron run).

    Silently returns when ``path`` does not exist (e.g. the in-memory
    database) or when ``stat()``/``chmod()`` raises ``OSError`` (e.g.
    a read-only filesystem). The DB-mode invariant is best-effort and
    must never crash a connect call.
    """
    try:
        current = stat.S_IMODE(path.stat().st_mode) & 0o777
    except (FileNotFoundError, OSError):
        return
    if current == mode:
        return
    try:
        os.chmod(path, mode)
    except OSError:
        # Filesystem may be read-only or owned by another user. We
        # surface neither — the caller should not care about chmod
        # failures when a connection succeeded.
        pass


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
        # Lock the on-disk file to ``DB_FILE_MODE`` (0o640). The first
        # connect creates the file (sqlite3 inherits the process
        # umask, which is 0o022 on most Linux installs → 0o644). We
        # tighten the permissions here so subsequent connects find
        # the file already at 0o640 and skip the chmod entirely. See
        # VAL-M2-001 (DB file mode contract assertion).
        _ensure_db_file_mode(Path(target))
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def connect_readonly(db_path: PathLike) -> sqlite3.Connection:
    """Open a strict read-only SQLite connection that does NOT mutate the file.

    Unlike :func:`connect`, this helper opens the database via the SQLite
    URI mode ``file:<path>?mode=ro``. The ``mode=ro`` flag tells the
    SQLite engine to refuse every write at the engine layer — any
    ``INSERT``/``UPDATE``/``DELETE``/``CREATE``/``DROP`` raises
    :class:`sqlite3.OperationalError` ("attempt to write a readonly
    database") instead of silently mutating the file.

    Critically, this helper does NOT issue any of the write-PRAGMAs that
    :func:`connect` does:

    * ``PRAGMA journal_mode=WAL`` — would write the WAL marker into the
      main db file's header.
    * ``PRAGMA synchronous=NORMAL`` — connection-level only, but bundled
      with the WAL switch so we omit it here for parity.
    * :func:`_ensure_db_file_mode` — ``os.chmod`` would update the file's
      ctime even on a no-op match.

    The only PRAGMA we do issue is ``foreign_keys=ON``, which is a
    purely connection-level setting (it never writes to disk), and
    we tolerate any ``OperationalError`` from it so a stricter SQLite
    build cannot break read-only callers.

    Use this helper when the caller only needs ``SELECT`` access and
    must guarantee the on-disk database file is byte-for-byte
    unchanged after the connection is closed (e.g. the M5 feature-store
    builder, per VAL-M5-033).

    Parameters
    ----------
    db_path:
        Filesystem path to an existing SQLite db file. The special
        token ``":memory:"`` is rejected because an empty in-memory
        database has nothing to read.

    Returns
    -------
    sqlite3.Connection
        A connection where every write attempt raises
        :class:`sqlite3.OperationalError` and ``row_factory`` is set
        to :class:`sqlite3.Row`.

    Raises
    ------
    ValueError
        If ``db_path`` is the ``":memory:"`` sentinel.
    """

    target = str(db_path)
    if target == ":memory:":
        raise ValueError(
            "connect_readonly does not support ':memory:'; "
            "the URI mode=ro requires an existing database file on disk."
        )
    # SQLite URIs are URL-encoded. ``urllib.parse.quote`` keeps the path
    # separator (``/``) unencoded by default, which is the form SQLite
    # expects for absolute paths.
    encoded = urllib.parse.quote(target)
    uri = f"file:{encoded}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # ``foreign_keys`` is a per-connection flag; it does not cause
        # the engine to write anything to the on-disk file, so it's
        # safe on a read-only connection. Tolerate the unlikely
        # OperationalError from a SQLite build that disallows it on
        # readonly connections — read-only mode itself already blocks
        # all writes, so the FK PRAGMA is best-effort.
        conn.execute("PRAGMA foreign_keys = ON;")
    except sqlite3.OperationalError:
        pass
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
    # f-m3-09: ``enrichment_label`` carries the LLM-enrichment tag
    # written by the news pipeline (e.g. ``'negative_material'``).
    # Used by the adverse-news exit hook to detect headlines that
    # should auto-close active plays.
    ("news_events", "enrichment_label", "enrichment_label TEXT"),
)


# f-m3-09: required CHECK clause on ``paper_orders.event``. New
# databases get this from ``schema.sql``; production databases that
# pre-date f-m3-09 need a recreate migration because SQLite cannot
# add a CHECK constraint via ``ALTER TABLE``.
_PAPER_ORDERS_EVENT_CHECK_FRAGMENT: Final[str] = (
    "event IN ('open','iv_crush_exit','stop_loss','adverse_news','rotation')"
)

# f-m3-11: required CHECK clause on ``paper_orders.purpose``. Same
# pattern — new dbs get it from schema.sql, legacy dbs (which had
# only the f-m3-03/-09 column set) need the recreate migration.
_PAPER_ORDERS_PURPOSE_CHECK_FRAGMENT: Final[str] = (
    "purpose IN ('entry','exit','liquidity_probe')"
)

# f-m3-11: required NOT NULL UNIQUE on ``paper_orders.client_order_id``.
_PAPER_ORDERS_CLIENT_ORDER_ID_FRAGMENT: Final[str] = (
    "client_order_id TEXT    NOT NULL UNIQUE"
)


def _legacy_orders_table_exists(conn: sqlite3.Connection) -> bool:
    """Return ``True`` when the pre-f-m3-11 ``orders`` table is present."""
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='orders'"
    ).fetchone()
    return row is not None


def _paper_orders_table_exists(conn: sqlite3.Connection) -> bool:
    """Return ``True`` when the f-m3-11 ``paper_orders`` table is present."""
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='paper_orders'"
    ).fetchone()
    return row is not None


def _rename_legacy_orders_to_paper_orders(conn: sqlite3.Connection) -> None:
    """Rename a legacy ``orders`` table to ``paper_orders`` if needed.

    Called BEFORE :data:`SCHEMA_PATH` is applied so the idempotent
    ``CREATE TABLE IF NOT EXISTS paper_orders`` statement in
    ``schema.sql`` becomes a no-op for production dbs (which arrive
    with ``orders``) and a fresh-create for greenfield dbs.

    Idempotent — only runs when ``orders`` exists AND ``paper_orders``
    does not. SQLite preserves any indexes/foreign keys that
    referenced ``orders`` after the rename (since SQLite 3.25), so
    we only need to drop the old explicitly-named indexes; the
    ``CREATE INDEX IF NOT EXISTS`` statements in ``schema.sql``
    will re-create them under the new naming convention.
    """
    if not _legacy_orders_table_exists(conn):
        return
    if _paper_orders_table_exists(conn):
        # Both tables exist — production drift we should not touch.
        # Leave both in place; subsequent ALTER/recreate steps target
        # ``paper_orders`` and the ``orders`` table is left as-is.
        return
    conn.execute("ALTER TABLE orders RENAME TO paper_orders")
    for legacy_index in (
        "idx_orders_play_card_id",
        "idx_orders_alpaca_id",
        "idx_orders_status",
        "idx_orders_event",
    ):
        try:
            conn.execute(f"DROP INDEX IF EXISTS {legacy_index}")
        except sqlite3.OperationalError:
            # Best-effort cleanup; the new indexes will be created
            # by the schema.sql apply step regardless.
            pass


def _paper_orders_table_has_event_check(conn: sqlite3.Connection) -> bool:
    """Return ``True`` when ``paper_orders`` has the f-m3-09 event CHECK."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name='paper_orders'"
    ).fetchone()
    if row is None:
        return False
    sql = row["sql"] if isinstance(row, sqlite3.Row) else row[0]
    if not isinstance(sql, str):
        return False
    return _PAPER_ORDERS_EVENT_CHECK_FRAGMENT in sql


def _paper_orders_table_has_f_m3_11_constraints(
    conn: sqlite3.Connection,
) -> bool:
    """Return ``True`` when ``paper_orders`` already enforces all f-m3-11 constraints.

    The check is textual and looks for the canonical fragments emitted
    by :data:`SCHEMA_PATH` after collapsing all runs of whitespace to
    a single space (so the matcher is robust to the multi-space
    column-alignment used in ``schema.sql``):

    * ``client_order_id TEXT NOT NULL UNIQUE``
    * ``purpose IN ('entry','exit','liquidity_probe')``

    A stricter SQL parser is unnecessary because the migration
    always emits these exact fragments — substring matching against
    normalised whitespace is sufficient for self-healing detection.
    """
    import re as _re

    row = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name='paper_orders'"
    ).fetchone()
    if row is None:
        return False
    sql = row["sql"] if isinstance(row, sqlite3.Row) else row[0]
    if not isinstance(sql, str):
        return False
    normalised = _re.sub(r"\s+", " ", sql)
    return (
        "client_order_id TEXT NOT NULL UNIQUE" in normalised
        and "purpose IN ('entry','exit','liquidity_probe')" in normalised
    )


def _add_f_m3_11_columns_to_paper_orders(conn: sqlite3.Connection) -> None:
    """ALTER TABLE ADD COLUMN for the f-m3-11 augmentation columns.

    Each column is added only when missing (PRAGMA pre-check). The
    columns are nullable at this stage; the recreate dance below
    promotes ``client_order_id`` to ``NOT NULL UNIQUE`` after
    backfilling sentinel values for any legacy rows.

    Skipping the helper entirely when ``paper_orders`` does not yet
    exist (e.g. brand-new db before ``schema.sql`` has been applied)
    keeps it idempotent.
    """
    if not _paper_orders_table_exists(conn):
        return
    cols = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(paper_orders)"
        ).fetchall()
    }
    if "requested_mid_at_submit" not in cols:
        try:
            conn.execute(
                "ALTER TABLE paper_orders ADD COLUMN "
                "requested_mid_at_submit REAL"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    if "purpose" not in cols:
        try:
            conn.execute(
                "ALTER TABLE paper_orders ADD COLUMN purpose TEXT"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    if "client_order_id" not in cols:
        try:
            conn.execute(
                "ALTER TABLE paper_orders ADD COLUMN client_order_id TEXT"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
        # Backfill NULL values with a deterministic sentinel so the
        # later UNIQUE recreate doesn't trip over duplicates. Legacy
        # rows are tagged ``legacy:<id>`` so operators can audit them.
        conn.execute(
            "UPDATE paper_orders "
            "SET client_order_id = 'legacy:' || id "
            "WHERE client_order_id IS NULL OR client_order_id = ''"
        )


def _recreate_paper_orders_with_full_constraints(
    conn: sqlite3.Connection,
) -> None:
    """Re-create ``paper_orders`` with the canonical f-m3-11 schema.

    SQLite supports column-level CHECK / NOT NULL UNIQUE only at
    table creation time; promoting an existing table requires the
    CREATE-COPY-DROP-RENAME pattern (mirrors the f-m3-09 helper for
    the ``event`` CHECK constraint).

    1. Create ``paper_orders__new`` with the canonical schema
       (matches ``schema.sql`` exactly).
    2. Copy every row from ``paper_orders`` into ``paper_orders__new``.
       Rows with NULL/empty ``client_order_id`` get the sentinel
       ``'legacy:<id>'``. Rows with an unrecognised ``purpose``
       (defensive) are coerced to NULL.
    3. ``DROP TABLE paper_orders`` and rename the new table.
    4. Re-create the index set declared in ``schema.sql``.

    The whole sequence runs inside the caller's transaction so a
    failure rolls back cleanly.
    """
    import logging as _logging

    log = _logging.getLogger(__name__)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_orders__new (
            id                          TEXT    NOT NULL PRIMARY KEY,
            play_card_id                TEXT,
            alpaca_order_id             TEXT,
            symbol                      TEXT,
            side                        TEXT,
            qty                         INTEGER,
            status                      TEXT    NOT NULL,
            reason                      TEXT,
            event                       TEXT    CHECK(
                event IS NULL OR
                event IN ('open','iv_crush_exit','stop_loss',
                          'adverse_news','rotation')
            ),
            parent_play_card_id         TEXT,
            requested_mid_at_submit     REAL,
            purpose                     TEXT    CHECK(
                purpose IS NULL OR
                purpose IN ('entry','exit','liquidity_probe')
            ),
            client_order_id             TEXT    NOT NULL UNIQUE,
            created_at                  TEXT    NOT NULL DEFAULT
                (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )

    allowed_events = (
        "open",
        "iv_crush_exit",
        "stop_loss",
        "adverse_news",
        "rotation",
    )
    allowed_purposes = ("entry", "exit", "liquidity_probe")

    # Audit-log any legacy event/purpose values that will be coerced
    # to NULL by the CASE expressions below — operator visibility for
    # forensic reviews.
    coerced_events = list(
        conn.execute(
            "SELECT id, event FROM paper_orders "
            "WHERE event IS NOT NULL AND event NOT IN "
            f"({', '.join('?' * len(allowed_events))})",
            allowed_events,
        ).fetchall()
    )
    for row in coerced_events:
        rid = row["id"] if isinstance(row, sqlite3.Row) else row[0]
        rev = row["event"] if isinstance(row, sqlite3.Row) else row[1]
        log.warning(
            "paper_orders.event_coerced_to_null id=%s legacy_event=%s "
            "reason=f-m3-11_recreate",
            rid,
            rev,
        )

    conn.execute(
        f"""
        INSERT INTO paper_orders__new (
            id, play_card_id, alpaca_order_id, symbol, side, qty,
            status, reason, event, parent_play_card_id,
            requested_mid_at_submit, purpose, client_order_id,
            created_at
        )
        SELECT id, play_card_id, alpaca_order_id, symbol, side, qty,
               status, reason,
               CASE
                   WHEN event IS NULL THEN NULL
                   WHEN event IN ({', '.join('?' * len(allowed_events))})
                       THEN event
                   ELSE NULL
               END AS event,
               parent_play_card_id,
               requested_mid_at_submit,
               CASE
                   WHEN purpose IS NULL THEN NULL
                   WHEN purpose IN ({', '.join('?' * len(allowed_purposes))})
                       THEN purpose
                   ELSE NULL
               END AS purpose,
               CASE
                   WHEN client_order_id IS NULL OR client_order_id = ''
                       THEN 'legacy:' || id
                   ELSE client_order_id
               END AS client_order_id,
               created_at
        FROM paper_orders
        """,
        (*allowed_events, *allowed_purposes),
    )

    conn.execute("DROP TABLE paper_orders")
    conn.execute("ALTER TABLE paper_orders__new RENAME TO paper_orders")

    # Restore the index set declared by ``schema.sql``.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_orders_play_card_id   "
        "ON paper_orders(play_card_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_orders_alpaca_id      "
        "ON paper_orders(alpaca_order_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_orders_status         "
        "ON paper_orders(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_orders_event          "
        "ON paper_orders(event)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_orders_purpose        "
        "ON paper_orders(purpose)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_orders_client_order_id "
        "ON paper_orders(client_order_id)"
    )


def _cleanup_scoring_cache_chain_gate_violations(
    conn: sqlite3.Connection,
) -> int:
    """Delete ``scoring_cache`` rows that violate the chain-gate invariant.

    Per VAL-M3-045 + f-m3-08 + f-m3-16, every ``scoring_cache`` row
    MUST join to a ``universe`` row with ``has_options_chain=1``.
    Rows that pre-date the chain-gate enforcement (e.g. injected by
    the f-m2-19 / f-m2-22 smoke runs that synthesised ``SMOK1..5``
    before the gate landed) leak through if the DB carries them
    forward, even though no production code path can re-create them
    today. This helper deletes those stale rows so the
    contract-enforcing JOIN

        ``SELECT COUNT(*) FROM scoring_cache sc
              LEFT JOIN universe u ON sc.ticker=u.ticker
            WHERE u.has_options_chain IS NULL
               OR u.has_options_chain = 0;``

    returns ``0`` after every migration apply.

    Idempotent: when no violations exist the DELETE matches zero
    rows and is a no-op. Wired into :func:`run_migrations` as a
    one-shot — gated on ``pre_migration_version < 9`` — so it
    runs exactly once per legacy database (after which
    ``schema_version=9`` is recorded and subsequent connects skip
    the cleanup so test fixtures that legitimately seed
    ``scoring_cache`` without ``universe`` are left alone).
    Whenever a non-zero number of rows is deleted the function logs
    a WARNING so operators notice the bypass actually fired
    (rather than silently swallowing the issue).

    Returns the number of rows deleted (so callers and tests can
    verify the cleanup ran).

    Skipped silently when either ``scoring_cache`` or ``universe``
    is absent — e.g. a partial/legacy db whose schema is being
    rebuilt by an earlier step in the migration. The
    ``CREATE TABLE IF NOT EXISTS`` statements in ``schema.sql`` run
    BEFORE this helper, so the skip path is reached only when the
    db is structurally broken in some other way.
    """
    import logging as _logging

    log = _logging.getLogger(__name__)

    def _table_exists(name: str) -> bool:
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return row is not None

    if not (_table_exists("scoring_cache") and _table_exists("universe")):
        return 0

    # Targets:
    #   * ticker absent from ``universe`` entirely (LEFT JOIN gives
    #     NULL has_options_chain) — strict reading of f-m3-16's
    #     "no chain status means reject" rule.
    #   * ticker present with ``has_options_chain=0`` — watch-only
    #     row that should never have been scored.
    #
    # The subquery uses ``NOT IN (SELECT ticker FROM universe WHERE
    # has_options_chain=1)`` so both buckets are caught with a
    # single, index-friendly DELETE.
    #
    # Cascade: ``llm_debate.scoring_cache_id`` is a FOREIGN KEY back
    # into ``scoring_cache(id)``. With ``PRAGMA foreign_keys=ON``
    # (set by :func:`connect`) the parent DELETE would raise
    # ``IntegrityError`` whenever a violator has an associated
    # debate transcript — the f-m2-19 smoke run created exactly
    # this case (SMOK1 has 3 debate rounds, SMOK2 has the synthetic
    # over-cap row). We therefore delete the dependent
    # ``llm_debate`` rows in the same transaction *before* the
    # parent rows. This is intentional, idempotent, and audited via
    # the WARNING log lines below.
    deleted_debates = 0
    if _table_exists("llm_debate"):
        debate_cursor = conn.execute(
            """
            DELETE FROM llm_debate
            WHERE scoring_cache_id IN (
                SELECT id FROM scoring_cache
                WHERE ticker NOT IN (
                    SELECT ticker FROM universe
                    WHERE has_options_chain = 1
                )
            )
            """
        )
        deleted_debates = debate_cursor.rowcount or 0

    cursor = conn.execute(
        """
        DELETE FROM scoring_cache
        WHERE ticker NOT IN (
            SELECT ticker FROM universe WHERE has_options_chain = 1
        )
        """
    )
    deleted = cursor.rowcount or 0
    if deleted > 0 or deleted_debates > 0:
        log.warning(
            "scoring_cache.chain_gate_cleanup deleted=%d "
            "llm_debate_cascaded=%d "
            "reason=stale_pre_chain_gate_rows "
            "(VAL-M3-045 self-heal — see f-m3-24)",
            deleted,
            deleted_debates,
        )
    return deleted


# Public alias so tests and operator scripts can invoke the cleanup
# helper without poking at the underscore-prefixed implementation.
# The helper is idempotent and safe to call against any db that has
# both ``scoring_cache`` and ``universe`` tables.
cleanup_scoring_cache_chain_gate_violations = (
    _cleanup_scoring_cache_chain_gate_violations
)


def _apply_pending_alter_table_migrations(conn: sqlite3.Connection) -> None:
    """Run any ``ALTER TABLE ... ADD COLUMN`` migrations that are missing.

    Idempotent — checks each column with ``PRAGMA table_info`` before
    issuing the ALTER. The "duplicate column name" ``OperationalError``
    that SQLite raises when two writers race on the same migration is
    caught locally so it does not propagate out and roll back the
    enclosing migration transaction. Any other ``OperationalError``
    (e.g. malformed DDL, missing table) is re-raised so callers can
    roll back.

    f-m3-09 also runs the orders-table recreate here so production
    databases that pre-date the CHECK constraint pick it up on the
    next connect. The f-m3-24 ``scoring_cache`` chain-gate cleanup
    is NOT applied here — it lives directly in :func:`run_migrations`
    so it can be gated on the pre-migration ``schema_version`` and
    thus run exactly once per legacy database.
    """
    for table, column, ddl in _ALTER_TABLE_ADD_COLUMNS:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column in cols:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        except sqlite3.OperationalError as exc:
            # Only swallow the well-known "column already exists" race;
            # everything else must propagate so the outer transaction
            # rolls back.
            if "duplicate column name" not in str(exc).lower():
                raise

    # f-m3-11: add the augmentation columns (``requested_mid_at_submit``,
    # ``purpose``, ``client_order_id``) to ``paper_orders`` if missing.
    # Brand-new dbs already have them via ``schema.sql``; legacy dbs
    # (pre-f-m3-11) gain them here as nullable columns and then have
    # ``client_order_id`` promoted to NOT NULL UNIQUE in the
    # subsequent recreate dance.
    _add_f_m3_11_columns_to_paper_orders(conn)

    # f-m3-09 + f-m3-11: ensure ``paper_orders`` carries every CHECK
    # constraint and the NOT NULL UNIQUE on ``client_order_id``. New
    # databases get this from ``schema.sql`` directly; older
    # production databases need the recreate dance because SQLite
    # cannot ADD a CHECK / NOT NULL UNIQUE via ALTER TABLE.
    try:
        if _paper_orders_table_exists(conn) and not (
            _paper_orders_table_has_event_check(conn)
            and _paper_orders_table_has_f_m3_11_constraints(conn)
        ):
            _recreate_paper_orders_with_full_constraints(conn)
    except sqlite3.OperationalError:
        # Surface to the caller's transaction so the migration rolls
        # back atomically — never silently leave the table partially
        # rebuilt.
        raise


def run_migrations(conn: sqlite3.Connection, target_version: int = CURRENT_VERSION) -> int:
    """Apply ``schema.sql`` and ALTER-TABLE migrations atomically.

    Behaviour (post-f-m2-14):

    1. Read ``schema.sql`` and split it into individual statements
       (see :func:`split_sql_statements`).
    2. Open an EXPLICIT transaction (``BEGIN``) and execute every
       statement (each ``CREATE TABLE``/``CREATE INDEX`` uses
       ``IF NOT EXISTS``, so re-running is a no-op).
    3. Run any pending ``ALTER TABLE ... ADD COLUMN`` migrations
       declared in :data:`_ALTER_TABLE_ADD_COLUMNS`.
    4. Upsert the ``schema_version`` row with ``version =
       target_version`` and ``COMMIT``.
    5. On any exception, ``ROLLBACK`` and re-raise so the caller sees
       the original error and the database is left untouched.

    Idempotency
    -----------
    Re-applying ``schema.sql`` on every connect is **intentional**: a
    database whose ``schema_version`` row drifted (e.g. an earlier
    feature forgot to bump ``CURRENT_VERSION``) self-heals on the
    next connect. Version drift can never cause silent
    table-skipping.

    Atomicity (the f-m2-14 fix)
    ---------------------------
    Earlier implementations relied on ``conn.executescript(schema_sql)``
    or just ``with conn:`` to bundle the DDL. Both are unsafe for
    SQLite DDL:

    * ``executescript`` issues an implicit ``COMMIT`` before running
      the script.
    * ``with conn:`` only opens an implicit transaction the first
      time a DML statement is executed via the connection's
      ``isolation_level``. ``CREATE TABLE``/``ALTER TABLE`` are NOT
      DML and Python's sqlite3 driver does **not** auto-BEGIN before
      them — so each DDL statement effectively auto-commits.

    The fix is to (a) set ``conn.isolation_level = None`` so the
    driver does not interfere, (b) issue an explicit ``BEGIN`` BEFORE
    any DDL, and (c) ``COMMIT``/``ROLLBACK`` ourselves. With this
    pattern, an ``OperationalError`` raised mid-script reverts the
    earlier ``CREATE TABLE`` statements as well — the database is
    either fully migrated or fully untouched.

    See ``library/architecture.md`` ("SQLite transactions" section)
    for the broader rationale and recipe.
    """

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    statements = split_sql_statements(schema_sql)

    # Snapshot the pre-migration schema version BEFORE we open the
    # transaction so the f-m3-24 one-time cleanup can decide whether
    # to fire. ``current_schema_version`` reads ``schema_version``
    # outside the BEGIN/COMMIT block (and tolerates a missing table
    # by returning ``0``), so it never interferes with the DDL
    # transaction below.
    pre_migration_version = current_schema_version(conn)

    # Take over transaction management from Python's sqlite3 driver.
    # We restore the previous isolation_level on the way out so the
    # caller's connection state is unchanged on success or failure.
    previous_isolation_level = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN")
        try:
            # f-m3-11: rename a legacy ``orders`` table to
            # ``paper_orders`` BEFORE applying ``schema.sql`` so the
            # idempotent ``CREATE TABLE IF NOT EXISTS paper_orders``
            # statement is a no-op for production dbs (which arrive
            # with ``orders``) and a fresh-create for greenfield
            # dbs. Idempotent: skipped when ``orders`` is absent or
            # ``paper_orders`` already exists.
            _rename_legacy_orders_to_paper_orders(conn)
            # f-m3-25: add the f-m3-11 augmentation columns
            # (``requested_mid_at_submit``, ``purpose``,
            # ``client_order_id``) to the just-renamed
            # ``paper_orders`` BEFORE the ``schema.sql`` apply loop.
            # Otherwise the ``CREATE INDEX IF NOT EXISTS
            # idx_paper_orders_purpose ON paper_orders(purpose)``
            # statement in ``schema.sql`` would fail with
            # ``no such column: purpose`` on legacy databases whose
            # ``orders`` table predates the f-m3-11 columns, rolling
            # back the entire migration. Idempotent: the helper
            # short-circuits cleanly when ``paper_orders`` does not
            # yet exist (greenfield dbs whose ``paper_orders`` is
            # created by the schema.sql apply below).
            _add_f_m3_11_columns_to_paper_orders(conn)
            for stmt in statements:
                conn.execute(stmt)
            _apply_pending_alter_table_migrations(conn)
            # f-m3-24: one-shot cleanup of stale ``scoring_cache``
            # rows that violate the chain-gate invariant (per
            # VAL-M3-045 + f-m3-08 + f-m3-16). The fix is gated on
            # ``pre_migration_version < 9`` so it runs exactly once
            # per database — enough to purge the SMOK1..5 stale
            # rows from the production VPS db without disturbing
            # tests or smoke runs that legitimately seed
            # ``scoring_cache`` without populating ``universe``.
            #
            # The chain-gate code paths (``filter_chain_gated_tickers``,
            # ``EnsembleScorer``) already prevent NEW violations
            # from being inserted by production callers, so this
            # one-time pass is sufficient — re-running on every
            # connect would fight legitimate test fixtures that
            # don't bother to seed the universe table.
            if pre_migration_version < 9:
                _cleanup_scoring_cache_chain_gate_violations(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, description) "
                "VALUES (?, ?)",
                (target_version, f"biotech_sniper schema v{target_version}"),
            )
            conn.execute("COMMIT")
        except Exception:
            # ROLLBACK is best-effort: if the connection is already
            # in a state where rollback fails we still want to
            # surface the original exception, not the rollback's.
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    finally:
        conn.isolation_level = previous_isolation_level
    return target_version
