"""Regression tests for f-m3-25 — migration ordering for legacy ``orders`` dbs.

A production database that was last migrated to ``schema_version=5`` (the
schema shape after f-m3-09 but before f-m3-11) carries a legacy
``orders`` table whose columns predate the f-m3-11 augmentation set
(``requested_mid_at_submit``, ``purpose``, ``client_order_id``).

When :func:`biotech_sniper.db.run_migrations` runs on such a database
it must:

1. Rename ``orders`` → ``paper_orders`` (carrying its rows).
2. Add the f-m3-11 augmentation columns BEFORE the ``schema.sql``
   apply loop runs ``CREATE INDEX IF NOT EXISTS
   idx_paper_orders_purpose ON paper_orders(purpose)`` — otherwise
   that index creation raises ``no such column: purpose`` and the
   whole transaction rolls back.
3. Apply the rest of ``schema.sql`` (creating
   ``execution_events``, ``execution_fills``, ``liquidity_probes``,
   …) and bump ``schema_version`` to :data:`db.CURRENT_VERSION`.

These tests seed a synthetic schema_version=5 db with a legacy
``orders`` table populated with five fixture rows that mirror the
rows observed on the VPS, then assert that:

* :func:`run_migrations` returns successfully (no exception).
* ``schema_version`` is now :data:`db.CURRENT_VERSION` (>= 7).
* The new tables ``paper_orders``, ``execution_events``,
  ``execution_fills``, and ``liquidity_probes`` are all present.
* All five fixture rows survived the rename.

A parametrised greenfield test (empty database) confirms the same
end state to make sure the new ordering is also a no-op for fresh
installs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from biotech_sniper import db


# Mirrors the pre-f-m3-11 ``orders`` schema (columns introduced by
# f-m3-03 + f-m3-09 ``event`` CHECK), as observed on the VPS at
# schema_version=5.
_LEGACY_ORDERS_DDL = """
CREATE TABLE orders (
    id                    TEXT    NOT NULL PRIMARY KEY,
    play_card_id          TEXT,
    alpaca_order_id       TEXT,
    symbol                TEXT,
    side                  TEXT,
    qty                   INTEGER,
    status                TEXT    NOT NULL,
    reason                TEXT,
    event                 TEXT    CHECK(
        event IS NULL OR
        event IN ('open','iv_crush_exit','stop_loss','adverse_news','rotation')
    ),
    parent_play_card_id   TEXT,
    created_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

_LEGACY_INDEXES = (
    "CREATE INDEX idx_orders_play_card_id ON orders(play_card_id);",
    "CREATE INDEX idx_orders_alpaca_id    ON orders(alpaca_order_id);",
    "CREATE INDEX idx_orders_status       ON orders(status);",
    "CREATE INDEX idx_orders_event        ON orders(event);",
)

# Five fixture rows that mirror the production VPS state. The exact
# values aren't important — what matters is that the rows survive
# the rename and end up addressable as ``paper_orders``.
_FIXTURE_ROWS: tuple[tuple, ...] = (
    (
        "PFE-open-20260427",
        "pc_pfe_001",
        "alp_001",
        "PFE251219C00030000",
        "BUY",
        31,
        "accepted",
        None,
        "open",
        None,
    ),
    (
        "MRK-open-20260427",
        "pc_mrk_001",
        "alp_002",
        "MRK260117C00100000",
        "BUY",
        12,
        "filled",
        None,
        "open",
        None,
    ),
    (
        "LLY-iv_crush-20260427",
        "pc_lly_001",
        "alp_003",
        "LLY260117C00800000",
        "SELL",
        2,
        "filled",
        None,
        "iv_crush_exit",
        "pc_lly_001",
    ),
    (
        "MRNA-stop_loss-20260427",
        "pc_mrna_001",
        "alp_004",
        "MRNA260117C00050000",
        "SELL",
        4,
        "accepted",
        None,
        "stop_loss",
        "pc_mrna_001",
    ),
    (
        "BNTX-rotation-20260427",
        "pc_bntx_001",
        "alp_005",
        "BNTX260117C00100000",
        "SELL",
        3,
        "filled",
        None,
        "rotation",
        "pc_bntx_001",
    ),
)


def _seed_schema_v5_with_legacy_orders(db_path: Path) -> None:
    """Seed ``db_path`` with a synthetic schema_version=5 database.

    The seed mirrors the minimum surface relied on by f-m3-25:

    * ``schema_version`` row with ``version=5``.
    * Legacy ``orders`` table with five fixture rows.
    * The legacy ``idx_orders_*`` indexes (so the rename helper can
      drop them like it does on the VPS).

    Anything else (``plays``, ``llm_cost_ledger``, …) is unnecessary
    for this regression — :func:`run_migrations` reapplies
    ``schema.sql`` idempotently and will create whatever the legacy
    db is missing.
    """
    raw = sqlite3.connect(str(db_path))
    raw.row_factory = sqlite3.Row
    try:
        raw.execute(
            """
            CREATE TABLE schema_version (
                version       INTEGER PRIMARY KEY,
                applied_at    TEXT    NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                description   TEXT
            )
            """
        )
        raw.execute(
            "INSERT INTO schema_version (version, description) "
            "VALUES (5, 'synthetic legacy schema for f-m3-25 regression')"
        )
        raw.executescript(_LEGACY_ORDERS_DDL)
        for ddl in _LEGACY_INDEXES:
            raw.execute(ddl)
        raw.executemany(
            """
            INSERT INTO orders (
                id, play_card_id, alpaca_order_id, symbol, side, qty,
                status, reason, event, parent_play_card_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _FIXTURE_ROWS,
        )
        raw.commit()
    finally:
        raw.close()


# ---------------------------------------------------------------------------
# Helpers shared by both tests
# ---------------------------------------------------------------------------


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _assert_post_migration_invariants(conn: sqlite3.Connection) -> None:
    """Assertions that hold for BOTH legacy-rebuilt and greenfield dbs."""
    # (a) schema_version bumped to CURRENT_VERSION (>= 7 per the
    # feature spec — currently 8).
    version = db.current_schema_version(conn)
    assert version == db.CURRENT_VERSION
    assert version >= 7

    # (b) the canonical f-m3-11 / f-m3-12 tables are all present.
    for required in (
        "paper_orders",
        "execution_events",
        "execution_fills",
        "liquidity_probes",
    ):
        assert _table_exists(conn, required), (
            f"expected table {required!r} to exist after run_migrations()"
        )

    # (c) ``paper_orders`` carries the f-m3-11 augmentation columns
    # AND the canonical CHECK / NOT NULL UNIQUE constraints (the
    # recreate dance succeeded).
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(paper_orders)").fetchall()
    }
    assert {
        "requested_mid_at_submit",
        "purpose",
        "client_order_id",
    }.issubset(cols)

    # (d) ``orders`` (the legacy name) MUST be gone.
    assert not _table_exists(conn, "orders")

    # (e) the ``idx_paper_orders_purpose`` index — the one whose
    # creation triggered the original bug — exists.
    idx_row = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name = 'idx_paper_orders_purpose'"
    ).fetchone()
    assert idx_row is not None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_migrations_rebuilds_legacy_schema_v5_db_with_orders_table(
    tmp_path: Path,
) -> None:
    """Pre-f-m3-25, this test reproduced the VPS failure
    ``sqlite3.OperationalError: no such column: purpose``.

    After the fix, :func:`run_migrations` must:

    * succeed without raising,
    * end with ``schema_version == CURRENT_VERSION``,
    * carry the five fixture rows over to ``paper_orders``,
    * create the new f-m3-11 / f-m3-12 tables.
    """
    db_path = tmp_path / "legacy_v5.db"
    _seed_schema_v5_with_legacy_orders(db_path)

    # Sanity: the seed actually produced the legacy state.
    raw = sqlite3.connect(str(db_path))
    raw.row_factory = sqlite3.Row
    try:
        assert _table_exists(raw, "orders")
        assert not _table_exists(raw, "paper_orders")
        assert (
            raw.execute("SELECT MAX(version) AS v FROM schema_version")
            .fetchone()["v"]
            == 5
        )
        legacy_count = raw.execute(
            "SELECT COUNT(*) AS n FROM orders"
        ).fetchone()["n"]
        assert legacy_count == len(_FIXTURE_ROWS) == 5
    finally:
        raw.close()

    # Now run the migration through the public entry point — must
    # not raise.
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
        _assert_post_migration_invariants(conn)

        # All five fixture rows survived the rename and are now
        # addressable as ``paper_orders``.
        post_rows = conn.execute(
            "SELECT id, play_card_id, alpaca_order_id, symbol, side, "
            "qty, status, event, parent_play_card_id "
            "FROM paper_orders "
            "ORDER BY id"
        ).fetchall()
        assert len(post_rows) == len(_FIXTURE_ROWS) == 5

        # Spot-check that the data is bit-identical to the fixture
        # rows (sorted by id).
        expected_by_id = {row[0]: row for row in _FIXTURE_ROWS}
        for row in post_rows:
            fixture = expected_by_id[row["id"]]
            assert row["play_card_id"] == fixture[1]
            assert row["alpaca_order_id"] == fixture[2]
            assert row["symbol"] == fixture[3]
            assert row["side"] == fixture[4]
            assert row["qty"] == fixture[5]
            assert row["status"] == fixture[6]
            assert row["event"] == fixture[8]
            assert row["parent_play_card_id"] == fixture[9]
    finally:
        conn.close()


def test_run_migrations_idempotent_on_legacy_v5_db(tmp_path: Path) -> None:
    """Running ``run_migrations`` twice on the legacy db must be a no-op.

    This catches the case where the f-m3-25 fix accidentally re-runs a
    non-idempotent helper on an already-migrated db (e.g. duplicating
    rows or raising ``duplicate column name``).
    """
    db_path = tmp_path / "legacy_v5_twice.db"
    _seed_schema_v5_with_legacy_orders(db_path)

    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
        first_count = conn.execute(
            "SELECT COUNT(*) AS n FROM paper_orders"
        ).fetchone()["n"]
        # Second invocation must not raise and must not duplicate
        # rows.
        db.run_migrations(conn)
        _assert_post_migration_invariants(conn)
        second_count = conn.execute(
            "SELECT COUNT(*) AS n FROM paper_orders"
        ).fetchone()["n"]
        assert first_count == second_count == len(_FIXTURE_ROWS) == 5
    finally:
        conn.close()


@pytest.mark.parametrize("db_filename", ["greenfield.db"])
def test_run_migrations_on_greenfield_db_reaches_same_end_state(
    tmp_path: Path, db_filename: str
) -> None:
    """An empty database must reach the same end state as the rebuilt one.

    Confirms that the new ``_add_f_m3_11_columns_to_paper_orders``
    call placed before the schema.sql apply loop is a safe no-op
    when ``paper_orders`` does not yet exist.
    """
    db_path = tmp_path / db_filename
    # Empty file — no schema_version, no tables.
    conn = db.connect(db_path)
    try:
        # Sanity: the connection is opened against an empty db.
        assert db.current_schema_version(conn) == 0
        assert not _table_exists(conn, "orders")
        assert not _table_exists(conn, "paper_orders")

        db.run_migrations(conn)
        _assert_post_migration_invariants(conn)

        # Greenfield ``paper_orders`` is empty.
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM paper_orders"
        ).fetchone()["n"]
        assert count == 0
    finally:
        conn.close()
