"""Regression tests for VAL-M5-033 — strict-read-only feature-store db access.

The user-testing validator round 1 surfaced that
``biotech_sniper.training.build_feature_store`` was mutating
``data/alpha_sniper.db`` across isolated reruns: the file's sha256 hash
and mtime changed every invocation. The root cause was that
``biotech_sniper.db.connect`` issues ``PRAGMA journal_mode=WAL``,
``PRAGMA synchronous=NORMAL`` and ``_ensure_db_file_mode`` on every
connect — all write operations that touch the file's WAL header /
metadata even for read-only callers.

These tests lock in the fix from f-m5-01a:

* ``connect_readonly`` opens the database with the URI ``mode=ro`` flag
  so the SQLite engine itself rejects any write attempt.
* The connection still supports SELECT queries.
* ``build_feature_store.build`` invocation leaves the on-disk database
  byte-for-byte unchanged (sha256 + mtime identical pre vs post).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from biotech_sniper import db
from biotech_sniper.training import build_feature_store as bfs


# ---------------------------------------------------------------------------
# Fixture helpers — minimal copy of the patterns in test_build_feature_store.py
# ---------------------------------------------------------------------------


_PLAY_FIXTURE = {
    "ticker": "VRDN",
    "direction": "LONG_CALLS",
    "catalyst_type": "READOUT",
    "entry_date": "2026-03-30",
    "exit_date": "2026-04-07",
    "option_strike": 27.0,
    "option_expiry": "2026-07-17",
    "p_success": 65,
    "science_grade": "C",
    "entry_fill": 1.10,
    "option_pnl_pct": -65.0,
    "payload": {
        "direction_correct": True,
        "entry_iv_pct": None,
        "expiry": "2026-07-17",
    },
}


def _seed_db(db_path: Path) -> None:
    """Seed a tmp db with one resolved play.

    Uses ``db.connect`` (the writable helper) and ``db.run_migrations``
    so the schema lands. After this call returns, every subsequent
    open MUST be via ``db.connect_readonly`` for the test to validate
    the strict-read-only contract.
    """

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
        play = _PLAY_FIXTURE
        conn.execute(
            """
            INSERT INTO plays (
                source_key, ticker, status, direction, catalyst_type,
                catalyst_date, entry_date, exit_date, option_type,
                option_strike, option_expiry, p_success, science_grade,
                entry_stock, exit_stock, entry_fill, pnl_usd,
                option_pnl_pct, stock_move_pct, payload
            )
            VALUES (?, ?, 'resolved', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"resolved:{play['ticker']}",
                play["ticker"],
                play.get("direction"),
                play.get("catalyst_type"),
                play.get("catalyst_date"),
                play.get("entry_date"),
                play.get("exit_date"),
                play.get("option_type"),
                play.get("option_strike"),
                play.get("option_expiry"),
                play.get("p_success"),
                play.get("science_grade"),
                play.get("entry_stock"),
                play.get("exit_stock"),
                play.get("entry_fill"),
                play.get("pnl_usd"),
                play.get("option_pnl_pct"),
                play.get("stock_move_pct"),
                json.dumps(play["payload"], sort_keys=True),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _write_state_sidecars(state_dir: Path) -> None:
    """Write the four JSON sidecars expected by ``build_feature_store.build``."""

    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "performance_ledger.json").write_text(
        json.dumps(
            {
                "plays": {
                    "VRDN": {
                        "ticker": "VRDN",
                        "entry_date": "2026-03-30",
                        "entry_iv_pct": 50.0,
                        "snapshots": [
                            {"date": "2026-03-30", "iv_pct": 50.0},
                            {"date": "2026-04-07", "iv_pct": 10.0},
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "science_grades.json").write_text(
        json.dumps(
            {
                "VRDN_NCT99999990": {
                    "ticker": "VRDN",
                    "grade": "C",
                    "base_rate": 0.40,
                    "matched_category": "endocrine",
                    "graded_date": "2026-03-29",
                }
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "calibration_params.json").write_text(
        json.dumps({"version": 1, "n_resolved": 1, "p_min_long": 65}),
        encoding="utf-8",
    )
    (state_dir / "company_pipelines.json").write_text(
        json.dumps(
            {
                "VRDN": {
                    "ticker": "VRDN",
                    "market_cap": 1_415_176_988,
                    "sector_group": "Endocrine",
                }
            }
        ),
        encoding="utf-8",
    )


def _checkpoint_and_quiesce(db_path: Path) -> None:
    """Ensure WAL is fully merged into the main db file and remove WAL/SHM.

    The ``_seed_db`` step above opens the database via ``db.connect``,
    which puts it in WAL mode. WAL mode keeps a separate ``-wal`` file
    that may not be fully merged on close. To get a stable
    sha256+mtime baseline before testing the read-only path, we:

    1. Run ``PRAGMA wal_checkpoint(TRUNCATE)`` to flush + truncate the
       WAL file into the main db.
    2. Delete the leftover ``-wal``/``-shm`` files so the read-only
       open does not interact with them at all.

    This isolates the test to the main database file, matching the
    user-testing validator's contract (which hashes only
    ``data/alpha_sniper.db``).
    """

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        # Switch back to the default rollback journal so the file
        # header itself is no longer marked WAL — that way a
        # subsequent connect_readonly does not need to read WAL
        # bookkeeping at all.
        conn.execute("PRAGMA journal_mode = DELETE;")
        conn.commit()
    finally:
        conn.close()
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = db_path.with_name(db_path.name + suffix)
        try:
            sidecar.unlink()
        except FileNotFoundError:
            pass


def _hash_and_mtime(path: Path) -> tuple[str, float]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    mtime = path.stat().st_mtime
    return digest, mtime


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_connect_readonly_supports_select(tmp_path: Path) -> None:
    """connect_readonly opens an existing db and SELECT queries succeed."""

    db_path = tmp_path / "data" / "alpha_sniper.db"
    _seed_db(db_path)

    conn = db.connect_readonly(db_path)
    try:
        rows = conn.execute(
            "SELECT ticker, status FROM plays WHERE status='resolved'"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0]["ticker"] == "VRDN"
    assert rows[0]["status"] == "resolved"


def test_connect_readonly_blocks_writes(tmp_path: Path) -> None:
    """INSERT/UPDATE/DELETE/CREATE through connect_readonly raise OperationalError.

    The ``mode=ro`` URI flag tells the SQLite engine itself to reject
    every write — the rejection comes back as
    ``sqlite3.OperationalError: attempt to write a readonly database``
    (or equivalent wording in older SQLite builds).
    """

    db_path = tmp_path / "data" / "alpha_sniper.db"
    _seed_db(db_path)

    conn = db.connect_readonly(db_path)
    try:
        # INSERT
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO plays (source_key, ticker, status) "
                "VALUES ('rw_attempt', 'XXXX', 'resolved')"
            )
        # UPDATE
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE plays SET status='active' WHERE ticker='VRDN'")
        # DELETE
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM plays WHERE ticker='VRDN'")
        # CREATE TABLE
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE rw_attempt (id INTEGER PRIMARY KEY)")
    finally:
        conn.close()


def test_connect_readonly_rejects_in_memory(tmp_path: Path) -> None:
    """``:memory:`` is rejected because the URI mode=ro requires a real file."""

    with pytest.raises(ValueError, match="memory"):
        db.connect_readonly(":memory:")


def test_build_feature_store_does_not_mutate_db(tmp_path: Path) -> None:
    """sha256 + mtime of the SQLite db are unchanged after build_feature_store.

    This is the canonical regression for VAL-M5-033 / the user-testing
    finding that motivated f-m5-01a. We snapshot the db file bytes and
    mtime, run :func:`bfs.build` (which exercises the full read path),
    and assert both are unchanged.

    A small ``time.sleep`` is NOT used here — we instead rely on the
    bytes-equality check (sha256) as the primary guarantee, with
    mtime as a redundant invariant. mtime granularity on macOS is
    nanosecond, so even a fast no-op chmod by the legacy code path
    would be detectable.
    """

    db_path = tmp_path / "data" / "alpha_sniper.db"
    out_path = tmp_path / "data" / "training" / "catalysts.parquet"
    state_dir = tmp_path / "state"
    seed_dir = tmp_path / "migrations" / "seed"
    seed_dir.mkdir(parents=True, exist_ok=True)

    _seed_db(db_path)
    _checkpoint_and_quiesce(db_path)
    _write_state_sidecars(state_dir)

    pre_hash, pre_mtime = _hash_and_mtime(db_path)

    result = bfs.build(
        db_path=db_path,
        out_path=out_path,
        state_dir=state_dir,
        seed_dir=seed_dir,
    )
    assert result.rows == 1, (
        "feature-store build expected to emit exactly one row from the "
        "single seeded play"
    )
    assert out_path.exists(), "parquet output should be written"

    post_hash, post_mtime = _hash_and_mtime(db_path)

    assert pre_hash == post_hash, (
        f"data/alpha_sniper.db sha256 changed across build_feature_store "
        f"invocation. pre={pre_hash[:16]} post={post_hash[:16]}. "
        "VAL-M5-033 violated — the feature-store builder must not mutate "
        "the state database."
    )
    assert pre_mtime == post_mtime, (
        f"data/alpha_sniper.db mtime changed across build_feature_store "
        f"invocation. pre={pre_mtime} post={post_mtime}."
    )

    # Sanity: WAL/SHM sidecars must also not have been re-created. The
    # only allowed side-effect of build_feature_store is the parquet
    # output under ``data/training/``.
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = db_path.with_name(db_path.name + suffix)
        assert not sidecar.exists(), (
            f"build_feature_store unexpectedly created sidecar {sidecar.name}; "
            "the connection should be strictly read-only with no WAL/SHM "
            "interaction"
        )


def test_build_feature_store_idempotent_under_readonly(tmp_path: Path) -> None:
    """Re-running build_feature_store twice keeps the db sha256 stable.

    Idempotency on the source db (in addition to the parquet output)
    is a strict subset of VAL-M5-033 but worth a dedicated check —
    a regression here would surface as flaky validator behavior on
    the second user-testing pass.
    """

    db_path = tmp_path / "data" / "alpha_sniper.db"
    out_path = tmp_path / "data" / "training" / "catalysts.parquet"
    state_dir = tmp_path / "state"
    seed_dir = tmp_path / "migrations" / "seed"
    seed_dir.mkdir(parents=True, exist_ok=True)

    _seed_db(db_path)
    _checkpoint_and_quiesce(db_path)
    _write_state_sidecars(state_dir)

    pre_hash, pre_mtime = _hash_and_mtime(db_path)

    bfs.build(
        db_path=db_path,
        out_path=out_path,
        state_dir=state_dir,
        seed_dir=seed_dir,
    )
    mid_hash, mid_mtime = _hash_and_mtime(db_path)
    bfs.build(
        db_path=db_path,
        out_path=out_path,
        state_dir=state_dir,
        seed_dir=seed_dir,
    )
    post_hash, post_mtime = _hash_and_mtime(db_path)

    assert pre_hash == mid_hash == post_hash, (
        "db sha256 drifted across repeat build_feature_store invocations: "
        f"pre={pre_hash[:8]} mid={mid_hash[:8]} post={post_hash[:8]}"
    )
    assert pre_mtime == mid_mtime == post_mtime, (
        "db mtime drifted across repeat build_feature_store invocations"
    )
