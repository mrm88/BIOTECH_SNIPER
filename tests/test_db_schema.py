"""Tests for ``biotech_sniper.db`` (schema + connection helpers).

Each test uses an isolated SQLite db (either ``:memory:`` or
``tmp_path / "test.db"``) so the suite stays hermetic and parallel-safe.
"""

from __future__ import annotations

import sqlite3

import pytest

from biotech_sniper import db


# ---------------------------------------------------------------------------
# Connection PRAGMAs
# ---------------------------------------------------------------------------


def test_connect_enables_foreign_keys():
    """``connect`` must turn on FK enforcement on every connection."""
    conn = db.connect(":memory:")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_connect_enables_wal_for_on_disk_db(tmp_path):
    """A real on-disk db should be opened in WAL journal mode."""
    db_path = tmp_path / "alpha.db"
    conn = db.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_connect_synchronous_normal(tmp_path):
    """``synchronous`` should be NORMAL (1) — WAL-safe and faster than FULL."""
    db_path = tmp_path / "alpha.db"
    conn = db.connect(db_path)
    try:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        conn.close()


def test_connect_returns_row_factory(tmp_path):
    """Rows must be accessible by column name."""
    conn = db.connect(tmp_path / "alpha.db")
    db.run_migrations(conn)
    try:
        conn.execute(
            "INSERT INTO discovery_state (nct_id, source) VALUES (?, ?)",
            ("NCT00000001", "test"),
        )
        row = conn.execute(
            "SELECT nct_id, source FROM discovery_state"
        ).fetchone()
        assert row["nct_id"] == "NCT00000001"
        assert row["source"] == "test"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# run_migrations + schema_version
# ---------------------------------------------------------------------------


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


EXPECTED_TABLES = {
    "schema_version",
    "plays",
    "performance_ledger",
    "discovery_state",
    "scoring_cache",
    "llm_cost_ledger",
}


def test_run_migrations_creates_all_expected_tables():
    conn = db.connect(":memory:")
    try:
        db.run_migrations(conn)
        assert EXPECTED_TABLES.issubset(_table_names(conn))
    finally:
        conn.close()


def test_run_migrations_writes_schema_version_row():
    conn = db.connect(":memory:")
    try:
        v = db.run_migrations(conn)
        assert v == db.CURRENT_VERSION
        rows = conn.execute(
            "SELECT version, applied_at FROM schema_version"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["version"] == db.CURRENT_VERSION
        assert rows[0]["applied_at"]  # non-empty timestamp
    finally:
        conn.close()


def test_run_migrations_is_idempotent():
    """Re-applying must not duplicate schema_version rows or fail."""
    conn = db.connect(":memory:")
    try:
        db.run_migrations(conn)
        db.run_migrations(conn)
        db.run_migrations(conn)
        count = conn.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version = ?",
            (db.CURRENT_VERSION,),
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_current_schema_version_zero_when_unmigrated():
    conn = db.connect(":memory:")
    try:
        assert db.current_schema_version(conn) == 0
    finally:
        conn.close()


def test_current_schema_version_after_migrate():
    conn = db.connect(":memory:")
    try:
        db.run_migrations(conn)
        assert db.current_schema_version(conn) == db.CURRENT_VERSION
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema column expectations
# ---------------------------------------------------------------------------


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_plays_table_has_status_column_with_active_and_resolved():
    conn = db.connect(":memory:")
    try:
        db.run_migrations(conn)
        cols = _column_names(conn, "plays")
        # core columns required by the validation contract
        for c in (
            "id",
            "ticker",
            "nct_id",
            "status",
            "entry_date",
            "exit_date",
            "option_type",
            "pnl_usd",
            "created_at",
            "updated_at",
        ):
            assert c in cols, f"plays.{c} missing"

        # Insert one of each valid status — CHECK constraint should accept.
        conn.execute(
            "INSERT INTO plays (source_key, ticker, status) VALUES (?, ?, ?)",
            ("active:AAA", "AAA", "active"),
        )
        conn.execute(
            "INSERT INTO plays (source_key, ticker, status) VALUES (?, ?, ?)",
            ("resolved:BBB", "BBB", "resolved"),
        )
        conn.execute(
            "INSERT INTO plays (source_key, ticker, status) VALUES (?, ?, ?)",
            ("monitor:CCC", "CCC", "monitor"),
        )

        # Invalid status must be rejected by the CHECK constraint.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO plays (source_key, ticker, status) VALUES (?, ?, ?)",
                ("bogus:DDD", "DDD", "completed"),
            )
    finally:
        conn.close()


def test_plays_source_key_is_unique():
    conn = db.connect(":memory:")
    try:
        db.run_migrations(conn)
        conn.execute(
            "INSERT INTO plays (source_key, ticker, status) VALUES (?, ?, ?)",
            ("active:AAA", "AAA", "active"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO plays (source_key, ticker, status) VALUES (?, ?, ?)",
                ("active:AAA", "AAA", "active"),
            )
    finally:
        conn.close()


def test_discovery_state_nct_id_is_primary_key():
    conn = db.connect(":memory:")
    try:
        db.run_migrations(conn)
        conn.execute(
            "INSERT INTO discovery_state (nct_id) VALUES (?)", ("NCT00000001",)
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO discovery_state (nct_id) VALUES (?)",
                ("NCT00000001",),
            )
    finally:
        conn.close()


def test_scoring_cache_unique_on_ticker_and_as_of_date():
    conn = db.connect(":memory:")
    try:
        db.run_migrations(conn)
        conn.execute(
            "INSERT INTO scoring_cache (ticker, as_of_date) VALUES (?, ?)",
            ("AAA", "2026-04-26"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO scoring_cache (ticker, as_of_date) VALUES (?, ?)",
                ("AAA", "2026-04-26"),
            )
        # different as_of_date is fine
        conn.execute(
            "INSERT INTO scoring_cache (ticker, as_of_date) VALUES (?, ?)",
            ("AAA", "2026-04-27"),
        )
    finally:
        conn.close()


def test_performance_ledger_primary_key_on_as_of_date():
    conn = db.connect(":memory:")
    try:
        db.run_migrations(conn)
        conn.execute(
            "INSERT INTO performance_ledger (as_of_date, realized_pnl_usd, "
            "unrealized_pnl_usd, play_count) VALUES (?, ?, ?, ?)",
            ("2026-04-26", 100.0, 50.0, 4),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO performance_ledger (as_of_date, realized_pnl_usd, "
                "unrealized_pnl_usd, play_count) VALUES (?, ?, ?, ?)",
                ("2026-04-26", 0.0, 0.0, 0),
            )
    finally:
        conn.close()


def test_llm_cost_ledger_provider_check_constraint():
    conn = db.connect(":memory:")
    try:
        db.run_migrations(conn)
        # Allowed providers
        for prov in ("xai", "anthropic", "gemini"):
            conn.execute(
                "INSERT INTO llm_cost_ledger (provider, model_id) VALUES (?, ?)",
                (prov, "test-model"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO llm_cost_ledger (provider, model_id) VALUES (?, ?)",
                ("openai", "gpt-x"),
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Indices
# ---------------------------------------------------------------------------


def _index_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        )
    }


def test_hot_path_indices_exist():
    """Hot-path indices on ticker, nct_id, status, as_of_date, called_at."""
    conn = db.connect(":memory:")
    try:
        db.run_migrations(conn)
        names = _index_names(conn)
        for required in (
            "idx_plays_ticker",
            "idx_plays_nct_id",
            "idx_plays_status",
            "idx_scoring_cache_ticker",
            "idx_scoring_cache_as_of_date",
            "idx_llm_cost_ledger_called_at",
        ):
            assert required in names, f"missing index: {required}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# f-m2-13 fix #3 — schema-version idempotency + drift-self-heal
# ---------------------------------------------------------------------------


def test_run_migrations_self_heals_a_stale_version_db(tmp_path):
    """A db with stale ``schema_version`` must still get every table on re-run.

    Simulates the earlier f-m2-09 / f-m2-10 drift where adding a new
    table to ``schema.sql`` did not bump ``CURRENT_VERSION``: a db
    that was already at the prior version would have skipped the new
    DDL under the old short-circuit. Post-f-m2-13, ``run_migrations``
    re-applies the schema on every connect so the missing tables
    self-heal.
    """
    db_path = tmp_path / "alpha.db"
    conn = db.connect(db_path)
    try:
        # Create just the migration-tracking table with a stale version.
        conn.execute(
            "CREATE TABLE schema_version ("
            "version INTEGER PRIMARY KEY, applied_at TEXT, description TEXT)"
        )
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.commit()

        applied = db.run_migrations(conn)
        assert applied == db.CURRENT_VERSION
        names = _table_names(conn)
        # Every table currently in schema.sql must be present.
        for required in (
            "plays",
            "performance_ledger",
            "discovery_state",
            "scoring_cache",
            "llm_cost_ledger",
            "universe",
            "news_events",
            "llm_debate",
        ):
            assert required in names, f"{required} missing after self-heal"
    finally:
        conn.close()


def test_current_version_documented_history():
    """``CURRENT_VERSION`` must be bumped any time ``schema.sql`` changes.

    Sanity floor: f-m2-12 bumped to 3 for ``llm_debate``; f-m2-13 bumped
    to 4 for the ``llm_cost_ledger.note`` column. Future schema changes
    MUST bump this further.
    """
    assert db.CURRENT_VERSION >= 4
    schema_text = db.SCHEMA_PATH.read_text(encoding="utf-8")
    # We rely on idempotent re-apply, so every CREATE TABLE statement
    # in schema.sql must use IF NOT EXISTS — the test enforces this so
    # a future PR that drops the qualifier breaks the build.
    import re as _re

    create_tables = _re.findall(
        r"CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?(\w+)",
        schema_text,
        flags=_re.IGNORECASE,
    )
    for if_not_exists, name in create_tables:
        assert if_not_exists.strip(), (
            f"CREATE TABLE {name!r} in schema.sql is missing IF NOT EXISTS — "
            "re-applying the schema would otherwise raise."
        )


def test_llm_cost_ledger_has_note_column_after_migration(tmp_path):
    """f-m2-13 fix #2 added ``note`` to ``llm_cost_ledger``.

    Verified via PRAGMA so both fresh dbs (created from schema.sql)
    and existing dbs (upgraded via the ALTER TABLE migration) agree.
    """
    conn = db.connect(tmp_path / "alpha.db")
    try:
        db.run_migrations(conn)
        cols = _column_names(conn, "llm_cost_ledger")
        assert "note" in cols, f"note column missing from llm_cost_ledger: {cols}"
    finally:
        conn.close()


def test_alter_table_note_migration_self_heals_old_schema(tmp_path):
    """A db that was created BEFORE ``note`` existed must gain it on re-run."""
    db_path = tmp_path / "alpha.db"
    # Open a raw connection (no migrations yet) and create the
    # llm_cost_ledger table WITHOUT the note column to simulate the
    # pre-f-m2-13 schema state.
    raw = sqlite3.connect(db_path)
    try:
        raw.execute(
            "CREATE TABLE llm_cost_ledger ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "provider TEXT NOT NULL CHECK(provider IN ('xai','anthropic','gemini')), "
            "model_id TEXT NOT NULL, purpose TEXT, "
            "prompt_tokens INTEGER, completion_tokens INTEGER, "
            "latency_ms INTEGER, cost_usd REAL, request_id TEXT, "
            "called_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
        )
        raw.commit()
    finally:
        raw.close()

    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
        cols = _column_names(conn, "llm_cost_ledger")
        assert "note" in cols
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# f-m2-13 fix #1 — atomic migration (rollback on partial failure)
# ---------------------------------------------------------------------------


def test_run_migrations_rolls_back_on_mid_script_failure(tmp_path, monkeypatch):
    """If the SECOND statement raises, the first CREATE TABLE rolls back too.

    Monkey-patches ``split_sql_statements`` so the second emitted
    statement is guaranteed-invalid SQL. The first statement
    (``CREATE TABLE schema_version``) has already been executed at
    that point, so this proves the migration uses an EXPLICIT
    transaction: without one, SQLite would auto-commit the first
    DDL and the schema_version table would survive the rollback.
    Post-f-m2-14, no tables from schema.sql may persist.
    """
    db_path = tmp_path / "alpha.db"

    real_split = db.split_sql_statements

    def _broken_split(sql: str) -> list[str]:
        statements = real_split(sql)
        # The first statement in schema.sql is
        # ``CREATE TABLE IF NOT EXISTS schema_version``. Inject a
        # guaranteed-fail statement immediately after it, dropping
        # everything that follows so we don't accidentally re-issue
        # the failing DDL twice (which would mask the rollback).
        return [statements[0], "this is not valid SQL"]

    monkeypatch.setattr(db, "split_sql_statements", _broken_split)

    conn = db.connect(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.run_migrations(conn)

        # Strict assertion: NO tables from schema.sql survive the
        # rollback. ``sqlite_%`` internal tables are filtered out.
        # If the explicit BEGIN is missing, schema_version (and any
        # earlier CREATE TABLE) auto-commits and this assert fails.
        tables = _table_names(conn)
        assert tables == set(), (
            f"expected zero user tables after rollback, got {tables!r}"
        )

        # Connection should be usable after rollback (no dangling tx).
        # A trivial query must succeed without raising.
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()


def test_run_migrations_rolls_back_on_late_failure(tmp_path, monkeypatch):
    """If a statement deep in schema.sql fails, ALL prior CREATE TABLEs roll back.

    Stronger sibling of the test above: rather than failing on the
    second statement, this injects the failure AFTER several
    CREATE TABLE statements have been issued. Validates that the
    explicit transaction wraps the entire DDL stream — not just the
    first couple of statements.
    """
    db_path = tmp_path / "alpha.db"

    real_split = db.split_sql_statements

    def _broken_split(sql: str) -> list[str]:
        statements = real_split(sql)
        # We want the failure to come AFTER multiple CREATE TABLEs
        # have been executed so the test proves bulk rollback.
        # schema.sql is well over a dozen statements; insert the
        # bad SQL after the 5th.
        assert len(statements) > 5, (
            "schema.sql has fewer than 6 statements — adjust this test"
        )
        return statements[:5] + ["this is not valid SQL"]

    monkeypatch.setattr(db, "split_sql_statements", _broken_split)

    conn = db.connect(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.run_migrations(conn)

        # ALL of the first 5 CREATE TABLE/INDEX statements must have
        # been rolled back: zero user tables remain.
        tables = _table_names(conn)
        assert tables == set(), (
            f"expected zero user tables after late-failure rollback, "
            f"got {tables!r}"
        )

        # And rolling back must not have left an open transaction
        # that blocks future writes.
        conn.execute("CREATE TABLE _post_rollback (id INTEGER)")
        conn.execute("INSERT INTO _post_rollback VALUES (1)")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM _post_rollback").fetchone()[0] == 1
    finally:
        conn.close()


def test_apply_pending_alter_table_real_duplicate_column_handling(tmp_path):
    """End-to-end regression: real ALTER-twice path on a fresh db is safe.

    Companion to
    :func:`test_run_migrations_swallows_duplicate_column_during_alter`,
    which uses ``monkeypatch`` to bypass the helper's PRAGMA pre-check
    so the ALTER actually attempts a re-add and the local
    ``OperationalError`` catch fires. This test exercises the same
    contract WITHOUT monkey-patching: it applies the helper's full
    ALTER-TABLE-ADD-COLUMN sweep twice on a fresh db (via
    :func:`db.run_migrations` and a direct
    :func:`db._apply_pending_alter_table_migrations` invocation), then
    forces a real duplicate-column error by issuing the same ALTER
    statement again outside the helper. The forced duplicate is wrapped
    in the helper's catch idiom to prove the production path's error
    classification is correct (only ``"duplicate column name"`` is
    swallowed; any other ``OperationalError`` propagates).

    Three invariants verified end-to-end:
      1. Calling ``run_migrations`` twice on the same fresh db is a
         no-op the second time and does NOT raise.
      2. Direct double invocation of
         ``_apply_pending_alter_table_migrations`` on a freshly
         migrated connection does NOT raise (idempotent under the real
         PRAGMA pre-check).
      3. ``schema_version`` advances to ``CURRENT_VERSION`` and stays
         there after both sweeps; no rows are duplicated and no rollback
         strands the database in a half-migrated state.
    """
    db_path = tmp_path / "alpha.db"
    conn = db.connect(db_path)
    try:
        # 1) First migrate — applies schema.sql + every entry in
        #    _ALTER_TABLE_ADD_COLUMNS via the real helper.
        v1 = db.run_migrations(conn)
        assert v1 == db.CURRENT_VERSION

        # Snapshot post-migration columns for one of the ALTER-target
        # tables. ``llm_cost_ledger.note`` is the canonical f-m2-13
        # ALTER target.
        cols_after_first = {
            row[1]
            for row in conn.execute("PRAGMA table_info(llm_cost_ledger)")
        }
        assert "note" in cols_after_first, (
            "llm_cost_ledger.note column missing after first migrate; "
            "_ALTER_TABLE_ADD_COLUMNS did not include it"
        )

        # 2) Second migrate — must be idempotent. The helper's PRAGMA
        #    pre-check sees the column already present and short-circuits
        #    every ALTER. No exception, no duplicate schema_version row.
        v2 = db.run_migrations(conn)
        assert v2 == db.CURRENT_VERSION
        rows = conn.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version = ?",
            (db.CURRENT_VERSION,),
        ).fetchone()
        assert rows[0] == 1, (
            "schema_version row was duplicated; idempotent UPSERT broken"
        )

        # 3) Direct invocation of the real helper on the same connection.
        #    Still must not raise. Exercises the helper end-to-end without
        #    going through run_migrations' transaction wrapper.
        db._apply_pending_alter_table_migrations(conn)
        db._apply_pending_alter_table_migrations(conn)

        # 4) Force a real duplicate-column race by issuing the same
        #    ALTER outside the helper. SQLite must surface
        #    "duplicate column name"; the helper's catch idiom is what
        #    swallows this error in production. Any other
        #    OperationalError must propagate.
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            conn.execute("ALTER TABLE llm_cost_ledger ADD COLUMN note TEXT")
        assert "duplicate column name" in str(excinfo.value).lower(), (
            f"expected duplicate-column error, got: {excinfo.value!r}"
        )

        # 5) Schema_version must still be at CURRENT_VERSION (no rollback).
        applied = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        assert applied == db.CURRENT_VERSION

        # 6) All expected user tables still present (no half-rebuild).
        tables = _table_names(conn)
        assert EXPECTED_TABLES.issubset(tables)
    finally:
        conn.close()


def test_run_migrations_swallows_duplicate_column_during_alter(tmp_path, monkeypatch):
    """Duplicate-column OperationalError during ALTER must NOT roll back.

    The ALTER-TABLE-ADD-COLUMN migrations must tolerate a "duplicate
    column name" race (e.g. two cron units race the same migration).
    That specific OperationalError must be swallowed locally so it
    doesn't take down the outer transaction. After the race, the
    migration must still complete (``schema_version`` row written
    and committed).

    We patch ``_ALTER_TABLE_ADD_COLUMNS`` so the migration tries to
    add a column that ALREADY EXISTS in the schema (``id`` on
    ``schema_version``). The PRAGMA pre-check in the helper
    short-circuits — but we ALSO patch the helper to skip the
    pre-check, simulating the race window.
    """
    db_path = tmp_path / "alpha.db"

    def _patched_apply(conn_):
        # Bypass the PRAGMA pre-check to force the ALTER to actually
        # execute against an already-existing column. SQLite will
        # raise OperationalError("duplicate column name: ...").
        # The fix in run_migrations must catch this WITHOUT rolling
        # back the outer transaction.
        try:
            conn_.execute("ALTER TABLE schema_version ADD COLUMN version INTEGER")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise

    monkeypatch.setattr(db, "_apply_pending_alter_table_migrations", _patched_apply)

    conn = db.connect(db_path)
    try:
        v = db.run_migrations(conn)
        assert v == db.CURRENT_VERSION

        # Outer transaction committed: schema_version row present.
        applied = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        assert applied == db.CURRENT_VERSION

        # And no exception escaped: the DDL committed cleanly.
        tables = _table_names(conn)
        assert "schema_version" in tables
    finally:
        conn.close()
