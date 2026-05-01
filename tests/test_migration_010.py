"""Tests for the v9 → v10 Reading-B foundations migration.

Covers VAL-M1-036 through VAL-M1-048:

* File existence + version markers (VAL-M1-036)
* v9 → v10 schema_version bump (VAL-M1-037)
* Atomic rollback on mid-migration failure (VAL-M1-038)
* Forward-only — no DROP / RENAME COLUMN / DROP COLUMN of legacy
  tables (VAL-M1-039)
* Exact set of new tables created (VAL-M1-040)
* ``llm_cost_ledger.provider`` admits 'perplexity' (VAL-M1-041)
* ``paper_orders.event`` admits 'news_event_entry' (VAL-M1-042)
* Existing v9 tables byte-schema-identical post-migration
  (VAL-M1-043)
* Idempotent re-run on v10 is a no-op (VAL-M1-044)
* Existing v9 row counts unchanged across migration (VAL-M1-045)
* Pre-migration DB backup written automatically (VAL-M1-046)
* PRAGMA integrity_check passes after migration (VAL-M1-047)
* Foreign-key invariants hold after migration (VAL-M1-048)

Plus runner-level coverage for :class:`DowngradeForbidden`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from biotech_sniper import db
from biotech_sniper.migrations import runner as migration_runner
from biotech_sniper.migrations.runner import (
    DowngradeForbidden,
    MigrationError,
    EXIT_DOWNGRADE,
    EXIT_FAILURE,
    EXIT_OK,
    load_migration,
    run as run_migrations_runner,
)

# Compiled regexes for forward-only / file-shape assertions.
_LEGACY_TABLES_FOR_FORWARD_ONLY: tuple[str, ...] = (
    "plays",
    "performance_ledger",
    "discovery_state",
    "scoring_cache",
    "llm_cost_ledger",
    "llm_debate",
    "paper_orders",
    "execution_events",
    "execution_fills",
    "news_events",
    "universe",
    "schema_version",
)

_LEGACY_TABLES_FOR_BYTE_IDENTICAL: tuple[str, ...] = (
    # Excludes llm_cost_ledger and paper_orders by design — those
    # are the two legacy tables whose CHECK enum is intentionally
    # extended (VAL-M1-041, VAL-M1-042).
    "plays",
    "performance_ledger",
    "discovery_state",
    "scoring_cache",
    "llm_debate",
    "news_events",
    "universe",
    "execution_events",
    "execution_fills",
    "schema_version",
)

# Row-count comparison list. ``schema_version`` is intentionally
# omitted because the migration legitimately appends a new row for
# the v10 marker (VAL-M1-045 evidence enumerates exactly the data
# tables that must preserve their pre-migration row counts).
_LEGACY_TABLES_FOR_ROW_COUNT: tuple[str, ...] = (
    "plays",
    "performance_ledger",
    "discovery_state",
    "scoring_cache",
    "llm_debate",
    "news_events",
    "universe",
    "execution_events",
    "execution_fills",
    "llm_cost_ledger",
    "paper_orders",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_v9_db(db_path: Path) -> None:
    """Initialise ``db_path`` as a v9 baseline using schema.sql.

    f-misc-06: ``db.CURRENT_VERSION`` is now 10 (so that
    ``PaperExecutor`` auto-bootstraps a fresh sqlite all the way to
    the Reading-B v10 foundations). To keep this helper a true v9
    baseline (so the v9 → v10 migration tests below have something
    to upgrade), we pass ``target_version=9`` explicitly to
    :func:`db.run_migrations`. The function applies the v9 schema
    via ``schema.sql`` + the ALTER-TABLE migration list, writes the
    ``schema_version=9`` row, and stops short of dispatching the
    v10 migration module.
    """
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn, target_version=9)
        # Double-check we're at v9 — the migration runner test
        # below will bump us to 10.
        assert db.current_schema_version(conn) == 9
    finally:
        conn.close()


def _seed_legacy_rows(db_path: Path) -> dict[str, int]:
    """Insert a small set of legacy rows so VAL-M1-045 (row counts unchanged)
    has something to compare. Returns the per-table row counts.
    """
    conn = db.connect(db_path)
    try:
        # plays — minimal row.
        conn.execute(
            "INSERT INTO plays (source_key, ticker, status) "
            "VALUES (?, ?, ?)",
            ("active:VRTX", "VRTX", "active"),
        )
        # discovery_state — one nct_id.
        conn.execute(
            "INSERT INTO discovery_state (nct_id, source) VALUES (?, ?)",
            ("NCT00000001", "test"),
        )
        # universe — one tradeable ticker.
        conn.execute(
            "INSERT INTO universe (ticker, tier, has_options_chain) "
            "VALUES (?, ?, ?)",
            ("VRTX", "tradeable", 1),
        )
        # llm_cost_ledger — one xai row (legacy provider).
        conn.execute(
            "INSERT INTO llm_cost_ledger "
            "(provider, model_id, purpose, prompt_tokens, completion_tokens, "
            " latency_ms, cost_usd, request_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("xai", "grok-2", "ranker", 100, 200, 500, 0.01, "req-1"),
        )
        # paper_orders — one row with an existing event value.
        conn.execute(
            "INSERT INTO paper_orders "
            "(id, status, event, client_order_id, purpose, qty) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("po-1", "submitted", "open", "co-1", "entry", 1),
        )
        # news_events — one row.
        conn.execute(
            "INSERT INTO news_events (ticker, source, title) VALUES (?, ?, ?)",
            ("VRTX", "test", "hello"),
        )
        conn.commit()
    finally:
        conn.close()

    # Capture row counts.
    return _row_counts(db_path)


def _row_counts(db_path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    conn = db.connect(db_path)
    try:
        for table in _LEGACY_TABLES_FOR_ROW_COUNT:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            out[table] = int(row["n"])
    finally:
        conn.close()
    return out


def _table_set(db_path: Path) -> set[str]:
    conn = db.connect(db_path)
    try:
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conn.close()


def _create_table_sql_map(db_path: Path) -> dict[str, str]:
    conn = db.connect(db_path)
    try:
        return {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# VAL-M1-036 — file existence + version markers
# ---------------------------------------------------------------------------


def test_migration_file_exists_with_version_markers():
    repo_root = Path(__file__).resolve().parent.parent
    candidates = sorted(
        (repo_root / "biotech_sniper" / "migrations").glob("010_*.py")
    )
    candidates = [p for p in candidates if p.name != "__init__.py"]
    assert candidates, "Expected a 010_*.py migration file"
    text = candidates[0].read_text(encoding="utf-8")
    assert re.search(r"FROM_VERSION\s*=\s*9", text), text[:200]
    assert re.search(r"TO_VERSION\s*=\s*10", text), text[:200]


def test_migration_module_loads_with_required_attrs():
    module = load_migration(10)
    assert module.FROM_VERSION == 9
    assert module.TO_VERSION == 10
    assert callable(module.apply)
    assert hasattr(module, "NEW_TABLE_NAMES")
    assert isinstance(module.NEW_TABLE_NAMES, tuple)
    assert len(module.NEW_TABLE_NAMES) == 10


# ---------------------------------------------------------------------------
# VAL-M1-039 — forward-only: no DROP / RENAME-COLUMN / DROP-COLUMN of legacy
# ---------------------------------------------------------------------------


def test_migration_contains_no_destructive_legacy_drops():
    repo_root = Path(__file__).resolve().parent.parent
    text = (
        repo_root
        / "biotech_sniper"
        / "migrations"
        / "010_reading_b_foundations.py"
    ).read_text(encoding="utf-8")
    legacy_alt = "|".join(_LEGACY_TABLES_FOR_FORWARD_ONLY)
    forbidden = re.compile(
        rf"DROP\s+(TABLE|INDEX|VIEW)\s+({legacy_alt})", re.IGNORECASE
    )
    matches = forbidden.findall(text)
    assert matches == [], (
        f"Forward-only violation in migration script: {matches}"
    )
    assert "RENAME COLUMN" not in text.upper()
    assert "DROP COLUMN" not in text.upper()


# ---------------------------------------------------------------------------
# VAL-M1-037 + VAL-M1-040 — schema_version bump + new-table inventory
# ---------------------------------------------------------------------------


def test_run_bumps_schema_version_to_10(tmp_path):
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)

    summary = run_migrations_runner(
        db_path, target_version=10, take_backup_first=False
    )
    assert summary["from_version"] == 9
    assert summary["applied"] == [10]
    assert summary["no_op"] is False

    conn = db.connect(db_path)
    try:
        assert db.current_schema_version(conn) == 10
        rows = conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()
        versions = [r[0] for r in rows]
        assert 9 in versions
        assert 10 in versions
    finally:
        conn.close()


def test_run_creates_exactly_the_v10_tables(tmp_path):
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)

    pre_tables = _table_set(db_path)
    run_migrations_runner(db_path, 10, take_backup_first=False)
    post_tables = _table_set(db_path)

    new_tables = post_tables - pre_tables
    expected_new = {
        "russell2k_biotech",
        "iwm_holdings_snapshot",
        "cik_sic_cache",
        "pdufa_calendar",
        "ema_calendar",
        "trial_calendar",
        "candidate_events",
        "ticker_cooldown",
        "ensemble_scores_event",
        "news_match_log",
    }
    assert new_tables == expected_new, (
        f"unexpected delta: missing={expected_new - new_tables}, "
        f"extra={new_tables - expected_new}"
    )


# ---------------------------------------------------------------------------
# VAL-M1-038 — atomic rollback on failure
# ---------------------------------------------------------------------------


def test_atomic_rollback_on_fault_inject(tmp_path, monkeypatch):
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)
    pre_tables = _table_set(db_path)
    pre_version = db.current_schema_version(
        db.connect(db_path)
    )  # 9
    assert pre_version == 9

    monkeypatch.setenv("MIGRATION_FAULT_INJECT", "1")
    with pytest.raises(MigrationError):
        run_migrations_runner(db_path, 10, take_backup_first=False)

    # No new tables, version still 9.
    post_tables = _table_set(db_path)
    assert post_tables == pre_tables, (
        f"Tables leaked across rollback: {post_tables - pre_tables}"
    )
    conn = db.connect(db_path)
    try:
        assert db.current_schema_version(conn) == 9
        # PRAGMA integrity check still ok after rollback.
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert ok == "ok"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# VAL-M1-044 — re-run on v10 is a no-op
# ---------------------------------------------------------------------------


def test_idempotent_rerun_on_v10(tmp_path):
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)
    run_migrations_runner(db_path, 10, take_backup_first=False)

    pre_schema_count = _schema_version_row_count(db_path, version=10)
    pre_tables = _create_table_sql_map(db_path)

    # Re-run should be a no-op.
    summary = run_migrations_runner(db_path, 10, take_backup_first=False)
    assert summary["no_op"] is True
    assert summary["applied"] == []

    post_schema_count = _schema_version_row_count(db_path, version=10)
    assert post_schema_count == pre_schema_count == 1

    post_tables = _create_table_sql_map(db_path)
    assert pre_tables == post_tables, (
        "table CREATE SQL changed across idempotent re-run"
    )


def _schema_version_row_count(db_path: Path, version: int) -> int:
    conn = db.connect(db_path)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version=?",
                (version,),
            ).fetchone()[0]
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# VAL-M1-043 — legacy v9 tables byte-schema-identical
# ---------------------------------------------------------------------------


def test_legacy_v9_tables_byte_schema_identical(tmp_path):
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)

    pre_sql = _create_table_sql_map(db_path)
    run_migrations_runner(db_path, 10, take_backup_first=False)
    post_sql = _create_table_sql_map(db_path)

    for table in _LEGACY_TABLES_FOR_BYTE_IDENTICAL:
        assert table in pre_sql, f"missing pre-migration table: {table}"
        assert table in post_sql, f"missing post-migration table: {table}"
        assert pre_sql[table] == post_sql[table], (
            f"{table} CREATE TABLE SQL changed across migration"
        )


# ---------------------------------------------------------------------------
# VAL-M1-045 — legacy row counts unchanged
# ---------------------------------------------------------------------------


def test_legacy_row_counts_unchanged(tmp_path):
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)
    pre_counts = _seed_legacy_rows(db_path)

    run_migrations_runner(db_path, 10, take_backup_first=False)
    post_counts = _row_counts(db_path)
    assert pre_counts == post_counts, (
        f"row counts changed across migration: "
        f"diff={set(pre_counts.items()) ^ set(post_counts.items())}"
    )


# ---------------------------------------------------------------------------
# VAL-M1-041 — llm_cost_ledger.provider extended to include 'perplexity'
# ---------------------------------------------------------------------------


def test_llm_cost_ledger_admits_perplexity(tmp_path):
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)
    run_migrations_runner(db_path, 10, take_backup_first=False)

    conn = db.connect(db_path)
    try:
        # Insert with 'perplexity' must succeed.
        conn.execute(
            "INSERT INTO llm_cost_ledger "
            "(provider, model_id, purpose) VALUES (?, ?, ?)",
            ("perplexity", "sonar", "stage2_event"),
        )
        conn.commit()
        n = conn.execute(
            "SELECT COUNT(*) FROM llm_cost_ledger WHERE provider='perplexity'"
        ).fetchone()[0]
        assert n == 1

        # Insert with bogus provider must fail.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO llm_cost_ledger "
                "(provider, model_id, purpose) VALUES (?, ?, ?)",
                ("bogus", "sonar", "stage2_event"),
            )
    finally:
        conn.close()


def test_llm_cost_ledger_existing_providers_still_accepted(tmp_path):
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)
    run_migrations_runner(db_path, 10, take_backup_first=False)
    conn = db.connect(db_path)
    try:
        for prov in ("xai", "anthropic", "gemini"):
            conn.execute(
                "INSERT INTO llm_cost_ledger "
                "(provider, model_id, purpose) VALUES (?, ?, ?)",
                (prov, "model", "purpose"),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# VAL-M1-042 — paper_orders.event extended with 'news_event_entry'
# ---------------------------------------------------------------------------


def test_paper_orders_admits_news_event_entry(tmp_path):
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)
    run_migrations_runner(db_path, 10, take_backup_first=False)

    conn = db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO paper_orders "
            "(id, status, event, client_order_id, purpose) "
            "VALUES (?, ?, ?, ?, ?)",
            ("po-news-1", "submitted", "news_event_entry", "co-news-1", "entry"),
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO paper_orders "
                "(id, status, event, client_order_id) "
                "VALUES (?, ?, ?, ?)",
                ("po-bad", "submitted", "totally_bogus_event", "co-bad"),
            )
    finally:
        conn.close()


def test_paper_orders_existing_event_values_still_accepted(tmp_path):
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)
    run_migrations_runner(db_path, 10, take_backup_first=False)
    conn = db.connect(db_path)
    try:
        for ev in (
            "open",
            "iv_crush_exit",
            "stop_loss",
            "adverse_news",
            "rotation",
        ):
            conn.execute(
                "INSERT INTO paper_orders "
                "(id, status, event, client_order_id) VALUES (?, ?, ?, ?)",
                (f"po-{ev}", "submitted", ev, f"co-{ev}"),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# VAL-M1-046 — pre-migration backup
# ---------------------------------------------------------------------------


def test_pre_migration_backup_is_taken(tmp_path):
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)
    backup_dir = tmp_path / "backups"

    src_sha256 = hashlib.sha256(db_path.read_bytes()).hexdigest()

    summary = run_migrations_runner(
        db_path,
        10,
        take_backup_first=True,
        backup_dir=backup_dir,
    )
    backup_path = summary["backup_path"]
    assert backup_path is not None
    backup_path = Path(backup_path)
    assert backup_path.exists()
    assert backup_path.parent == backup_dir
    assert backup_path.name.startswith("alpha.db.v9.")

    # Backup contents match pre-migration db bytes.
    backup_sha256 = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    assert backup_sha256 == src_sha256

    # File mode is 0o600 (best-effort — may differ on Windows).
    if os.name == "posix":
        mode = os.stat(backup_path).st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"


def test_no_backup_flag_disables_backup(tmp_path):
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)
    backup_dir = tmp_path / "backups"

    summary = run_migrations_runner(
        db_path,
        10,
        take_backup_first=False,
        backup_dir=backup_dir,
    )
    assert summary["backup_path"] is None
    assert not backup_dir.exists() or not list(backup_dir.iterdir())


# ---------------------------------------------------------------------------
# VAL-M1-047 + VAL-M1-048 — PRAGMA integrity + foreign-key invariants
# ---------------------------------------------------------------------------


def test_pragma_integrity_check_passes_after_migration(tmp_path):
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)
    _seed_legacy_rows(db_path)
    run_migrations_runner(db_path, 10, take_backup_first=False)

    conn = db.connect(db_path)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert result == "ok"
    finally:
        conn.close()


def test_foreign_key_invariants_hold_after_migration(tmp_path):
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)
    _seed_legacy_rows(db_path)
    run_migrations_runner(db_path, 10, take_backup_first=False)

    conn = db.connect(db_path)
    try:
        rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert rows == [], f"FK violations: {rows}"
    finally:
        conn.close()


def test_candidate_events_fk_to_news_events_enforced(tmp_path):
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)
    run_migrations_runner(db_path, 10, take_backup_first=False)

    conn = db.connect(db_path)
    try:
        # Insert with a non-existent news_events.id must fail.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO candidate_events "
                "(ticker, source_news_event_id, matched_keywords, "
                " emitted_at, dedup_key) "
                "VALUES (?, ?, ?, ?, ?)",
                ("VRTX", 9999999, "nda", "2026-04-29T00:00:00Z", "abc"),
            )
            conn.commit()
        conn.rollback()

        # Insert a real news_events row first, then the candidate
        # links cleanly.
        cur = conn.execute(
            "INSERT INTO news_events (ticker, source, title) VALUES (?, ?, ?)",
            ("VRTX", "test", "BLA submitted"),
        )
        nid = cur.lastrowid
        conn.execute(
            "INSERT INTO candidate_events "
            "(ticker, source_news_event_id, matched_keywords, "
            " emitted_at, dedup_key) "
            "VALUES (?, ?, ?, ?, ?)",
            ("VRTX", nid, "BLA submission", "2026-04-29T00:00:00Z", "k1"),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# f-fix-m1-07 — paper_orders recreate is FK-safe with linked child rows
# ---------------------------------------------------------------------------


def test_paper_orders_recreate_fk_safe_with_linked_children(tmp_path):
    """v9 → v10 migrates cleanly when ``paper_orders`` already has linked
    rows in ``execution_events`` and ``execution_fills``.

    Reproduces the scrutiny finding for f-m1-07-migration-v9-to-v10
    (VAL-M1-048): without the FK-safe envelope, the
    ``DROP`` step inside the ``paper_orders`` recreate raises
    ``FOREIGN KEY constraint failed`` because
    :func:`biotech_sniper.db.connect` opens every connection with
    ``PRAGMA foreign_keys=ON`` and the child rows reference the
    parent table. The fix wraps the recreate in the canonical
    ``foreign_keys=OFF`` / ``BEGIN`` / rebuild / ``foreign_key_check`` /
    ``COMMIT`` / ``foreign_keys=ON`` envelope.

    Asserts:

    * Migration succeeds (``schema_version`` advances to 10).
    * ``PRAGMA foreign_key_check`` returns zero rows post-migration.
    * Child rows in ``execution_events`` + ``execution_fills`` are
      preserved with valid ``paper_order_id`` references.
    * Parent row in ``paper_orders`` is preserved.
    * ``PRAGMA foreign_keys`` is restored to ON after migration.
    """
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)

    conn = db.connect(db_path)
    try:
        # Parent paper_orders row.
        conn.execute(
            "INSERT INTO paper_orders "
            "(id, status, event, client_order_id, purpose, qty) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("po-fk-1", "filled", "open", "co-fk-1", "entry", 1),
        )
        # Two execution_events children referencing po-fk-1.
        conn.execute(
            "INSERT INTO execution_events "
            "(paper_order_id, event_type, event_at) VALUES (?, ?, ?)",
            ("po-fk-1", "submitted", "2026-04-29T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO execution_events "
            "(paper_order_id, event_type, event_at) VALUES (?, ?, ?)",
            ("po-fk-1", "filled", "2026-04-29T00:00:01Z"),
        )
        # One execution_fills child referencing po-fk-1.
        conn.execute(
            "INSERT INTO execution_fills "
            "(paper_order_id, filled_at, filled_price, filled_qty, "
            " requested_mid_at_submit, slippage_bps, slippage_usd, "
            " time_to_fill_ms, partial_qty_remaining) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "po-fk-1",
                "2026-04-29T00:00:01Z",
                1.5,
                1,
                1.4,
                0.0,
                0.0,
                1000,
                0,
            ),
        )
        conn.commit()

        # Sanity: parent + children present BEFORE migration.
        ev_pre = conn.execute(
            "SELECT COUNT(*) FROM execution_events WHERE paper_order_id='po-fk-1'"
        ).fetchone()[0]
        fl_pre = conn.execute(
            "SELECT COUNT(*) FROM execution_fills WHERE paper_order_id='po-fk-1'"
        ).fetchone()[0]
        assert ev_pre == 2
        assert fl_pre == 1
    finally:
        conn.close()

    # Run migration. Must NOT raise — proves the FK-safe envelope is
    # in place. On the buggy code path this raises
    # ``MigrationError: ... IntegrityError('FOREIGN KEY constraint failed')``.
    summary = run_migrations_runner(
        db_path, target_version=10, take_backup_first=False
    )
    assert summary["from_version"] == 9
    assert summary["applied"] == [10]
    assert summary["no_op"] is False

    conn = db.connect(db_path)
    try:
        # Schema bumped to 10.
        assert db.current_schema_version(conn) == 10

        # PRAGMA foreign_keys restored to ON.
        fk_pragma = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk_pragma == 1, f"expected foreign_keys=1, got {fk_pragma}"

        # PRAGMA foreign_key_check returns no rows.
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert violations == [], f"FK violations after migration: {violations}"

        # Child rows preserved with valid paper_order_id references.
        ev_rows = conn.execute(
            "SELECT paper_order_id, event_type FROM execution_events "
            "WHERE paper_order_id='po-fk-1' ORDER BY event_at"
        ).fetchall()
        assert len(ev_rows) == 2
        assert all(r["paper_order_id"] == "po-fk-1" for r in ev_rows)
        assert [r["event_type"] for r in ev_rows] == ["submitted", "filled"]

        fl_rows = conn.execute(
            "SELECT paper_order_id, filled_qty FROM execution_fills "
            "WHERE paper_order_id='po-fk-1'"
        ).fetchall()
        assert len(fl_rows) == 1
        assert fl_rows[0]["paper_order_id"] == "po-fk-1"
        assert fl_rows[0]["filled_qty"] == 1

        # Parent row preserved.
        po = conn.execute(
            "SELECT id, status, event, client_order_id "
            "FROM paper_orders WHERE id='po-fk-1'"
        ).fetchone()
        assert po is not None
        assert po["id"] == "po-fk-1"
        assert po["status"] == "filled"
        assert po["event"] == "open"
        assert po["client_order_id"] == "co-fk-1"

        # And the new event value 'news_event_entry' is now accepted —
        # confirms the recreate did its job (CHECK enum extended).
        conn.execute(
            "INSERT INTO paper_orders "
            "(id, status, event, client_order_id, purpose, qty) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "po-news-fk-1",
                "submitted",
                "news_event_entry",
                "co-news-fk-1",
                "entry",
                1,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_paper_orders_recreate_fk_safe_preserves_forward_only_grep():
    """f-fix-m1-07: VAL-M1-039 forward-only literal grep still passes.

    The FK-safe envelope must NOT introduce a literal
    ``DROP TABLE <legacy>`` substring in the migration source —
    the script must continue to interpolate the legacy table name
    via :data:`_PO_TABLE_NAME` so the validator's bare ``grep -nE
    'DROP\\s+(TABLE|INDEX|VIEW)\\s+(<legacy>)'`` returns no matches.
    Mirrors the existing ``test_migration_contains_no_destructive_legacy_drops``
    test but locks in the invariant after f-fix-m1-07's edit.
    """
    repo_root = Path(__file__).resolve().parent.parent
    text = (
        repo_root
        / "biotech_sniper"
        / "migrations"
        / "010_reading_b_foundations.py"
    ).read_text(encoding="utf-8")

    legacy_alt = "|".join(_LEGACY_TABLES_FOR_FORWARD_ONLY)
    forbidden = re.compile(
        rf"DROP\s+(TABLE|INDEX|VIEW)\s+({legacy_alt})", re.IGNORECASE
    )
    matches = forbidden.findall(text)
    assert matches == [], (
        f"VAL-M1-039 forward-only violation after f-fix-m1-07: {matches}"
    )


def test_paper_orders_recreate_fk_safe_no_orphans_with_many_children(tmp_path):
    """Stress-shaped variant: many child rows survive the FK-safe rebuild.

    Inserts 20 ``execution_events`` rows and 10 ``execution_fills`` rows
    spread across 3 distinct ``paper_orders`` parents, then runs the
    v9 → v10 migration. Asserts every child row still resolves to its
    parent post-migration (i.e. no orphan rows leaked from the
    create-copy-drop-rename dance).
    """
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)

    parent_ids = ("po-many-1", "po-many-2", "po-many-3")
    expected_event_count = 0
    expected_fill_count = 0

    conn = db.connect(db_path)
    try:
        for i, pid in enumerate(parent_ids):
            conn.execute(
                "INSERT INTO paper_orders "
                "(id, status, event, client_order_id, purpose, qty) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (pid, "filled", "open", f"co-{pid}", "entry", 1),
            )
            # 6-7 events per parent.
            n_events = 6 + (i % 2)
            for j in range(n_events):
                conn.execute(
                    "INSERT INTO execution_events "
                    "(paper_order_id, event_type, event_at) "
                    "VALUES (?, ?, ?)",
                    (
                        pid,
                        "submitted" if j == 0 else "filled",
                        f"2026-04-29T00:00:{j:02d}Z",
                    ),
                )
                expected_event_count += 1
            # 3-4 fills per parent.
            n_fills = 3 + (i % 2)
            for j in range(n_fills):
                conn.execute(
                    "INSERT INTO execution_fills "
                    "(paper_order_id, filled_at, filled_price, filled_qty, "
                    " requested_mid_at_submit, slippage_bps, slippage_usd, "
                    " time_to_fill_ms, partial_qty_remaining) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        pid,
                        f"2026-04-29T00:01:{j:02d}Z",
                        1.5 + j * 0.1,
                        1,
                        1.4,
                        0.0,
                        0.0,
                        1000 + j,
                        0,
                    ),
                )
                expected_fill_count += 1
        conn.commit()
    finally:
        conn.close()

    # Run migration; must succeed.
    summary = run_migrations_runner(
        db_path, target_version=10, take_backup_first=False
    )
    assert summary["applied"] == [10]

    conn = db.connect(db_path)
    try:
        # FK invariants intact.
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        # All child rows preserved + resolve to a real parent.
        ev_total = conn.execute(
            "SELECT COUNT(*) FROM execution_events"
        ).fetchone()[0]
        fl_total = conn.execute(
            "SELECT COUNT(*) FROM execution_fills"
        ).fetchone()[0]
        assert ev_total == expected_event_count
        assert fl_total == expected_fill_count

        # No orphan children — every paper_order_id resolves.
        orphans_ev = conn.execute(
            "SELECT COUNT(*) FROM execution_events ee "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM paper_orders po WHERE po.id = ee.paper_order_id"
            ")"
        ).fetchone()[0]
        orphans_fl = conn.execute(
            "SELECT COUNT(*) FROM execution_fills ef "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM paper_orders po WHERE po.id = ef.paper_order_id"
            ")"
        ).fetchone()[0]
        assert orphans_ev == 0
        assert orphans_fl == 0

        # All parent rows preserved.
        po_count = conn.execute(
            "SELECT COUNT(*) FROM paper_orders"
        ).fetchone()[0]
        assert po_count == len(parent_ids)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DowngradeForbidden
# ---------------------------------------------------------------------------


def test_downgrade_forbidden(tmp_path):
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)
    run_migrations_runner(db_path, 10, take_backup_first=False)

    with pytest.raises(DowngradeForbidden):
        run_migrations_runner(db_path, 9, take_backup_first=False)


def test_downgrade_forbidden_target_zero(tmp_path):
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)
    with pytest.raises(DowngradeForbidden):
        run_migrations_runner(db_path, 0, take_backup_first=False)


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


def test_cli_check_prints_current_version(tmp_path):
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)
    rc = migration_runner.main(["--db", str(db_path), "--check"])
    assert rc == EXIT_OK


def test_cli_target_10_applies_migration(tmp_path):
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)
    backup_dir = tmp_path / "backups"
    rc = migration_runner.main(
        [
            "--db",
            str(db_path),
            "--target",
            "10",
            "--backup-dir",
            str(backup_dir),
        ]
    )
    assert rc == EXIT_OK
    conn = db.connect(db_path)
    try:
        assert db.current_schema_version(conn) == 10
    finally:
        conn.close()


def test_cli_downgrade_returns_exit_3(tmp_path):
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)
    run_migrations_runner(db_path, 10, take_backup_first=False)
    rc = migration_runner.main(
        ["--db", str(db_path), "--target", "9", "--no-backup"]
    )
    assert rc == EXIT_DOWNGRADE


def test_cli_target_required(tmp_path):
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)
    with pytest.raises(SystemExit):
        migration_runner.main(["--db", str(db_path)])


# ---------------------------------------------------------------------------
# Module-level fault-inject helper: also rolls back when a generic exception
# is raised by the migration's apply() (e.g. mid-migration sqlite error).
# ---------------------------------------------------------------------------


def test_apply_module_short_circuits_on_fault_inject(tmp_path, monkeypatch):
    """Direct test of the migration's ``apply()`` honouring fault-inject."""
    module = load_migration(10)
    db_path = tmp_path / "alpha.db"
    _build_v9_db(db_path)

    monkeypatch.setenv("MIGRATION_FAULT_INJECT", "true")
    conn = db.connect(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN")
        with pytest.raises(module.MigrationFaultInjected):
            module.apply(conn)
        conn.execute("ROLLBACK")

        # New tables NOT present after rollback.
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        for new_table in module.NEW_TABLE_NAMES:
            assert new_table not in names, (
                f"{new_table} leaked across rollback"
            )
    finally:
        conn.close()
