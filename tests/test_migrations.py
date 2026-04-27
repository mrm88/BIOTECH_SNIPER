"""Tests for ``biotech_sniper.migrations.migrate_json_to_sqlite``.

Each test builds a small JSON fixture under ``tmp_path`` and runs the
migration against a per-test SQLite db (also under ``tmp_path``). No
network is touched — the migration is purely file-driven.

The tests cover:

* Counts: 6 resolved plays / 261 NCT IDs faithfully preserved.
* Idempotency: re-running the migration produces identical row counts.
* Schema-version gating: ``schema_version`` is bumped exactly once.
* Error handling: corrupt JSON / missing source raise actionable errors
  and never partial-commit (transactional).
* Optional sources: ``scoring_cache.json`` may be absent.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from biotech_sniper import db
from biotech_sniper.migrations import migrate_json_to_sqlite as mig


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_six_resolved_plays() -> dict[str, list[dict]]:
    return {
        "resolved": [
            {
                "ticker": "LPCN",
                "resolved_date": "2026-04-03",
                "outcome": "LOSS_WRONG_DIRECTION",
                "option_pnl_pct": -100.0,
                "stock_move_pct": -78.0,
                "entry_p_success": 62,
                "entry_science_grade": None,
                "entry_catalyst_type": "READOUT",
                "entry_direction": "EQUITY_ONLY",
                "entry_fill": None,
                "entry_stock": 7.58,
                "entry_date": "2026-03-30",
            },
            {
                "ticker": "VRDN",
                "resolved_date": "2026-04-07",
                "outcome": "LOSS_WRONG_STRIKE",
                "option_pnl_pct": -65.0,
                "stock_move_pct": -34.0,
                "entry_p_success": 65,
                "entry_science_grade": "C",
                "entry_catalyst_type": "READOUT",
                "entry_direction": "LONG_CALLS",
                "entry_fill": 1.10,
                "entry_stock": 18.84,
                "entry_date": "2026-03-30",
                "strike": 27.0,
                "expiry": "2026-07-17",
            },
            {
                "ticker": "TVTX",
                "resolved_date": "2026-04-17",
                "outcome": "LOSS_WRONG_STRIKE",
                "option_pnl_pct": -81.0,
                "stock_move_pct": 6.2,
                "entry_p_success": 77,
                "entry_science_grade": "B",
                "entry_catalyst_type": "LABEL_EXT",
                "entry_direction": "LONG_CALLS",
                "entry_fill": 1.20,
                "entry_stock": 28.96,
                "entry_date": "2026-03-30",
                "strike": 35.0,
                "expiry": "2026-04-17",
            },
            {
                "ticker": "IDYA_35C",
                "resolved_date": "2026-04-14",
                "outcome": "MIXED_IV_CRUSH",
                "option_pnl_pct": -91.0,
                "stock_move_pct": 7.6,
                "entry_p_success": 96,
                "entry_science_grade": "C",
                "entry_catalyst_type": "READOUT",
                "entry_direction": "LONG_CALLS",
                "entry_fill": 1.82,
                "entry_stock": 31.0,
                "entry_date": "2026-03-30",
                "strike": 35.0,
                "expiry": "2026-05-15",
            },
            {
                "ticker": "RVMD_125C",
                "resolved_date": "2026-04-14",
                "outcome": "WIN",
                "option_pnl_pct": 193.0,
                "stock_move_pct": 41.2,
                "entry_p_success": 61,
                "entry_science_grade": "C",
                "entry_catalyst_type": "READOUT",
                "entry_direction": "LONG_CALLS",
                "entry_fill": 3.20,
                "entry_stock": 96.22,
                "entry_date": "2026-03-30",
                "strike": 125.0,
                "expiry": "2026-06-18",
            },
            {
                "ticker": "AGIO_PUT",
                "resolved_date": "2026-04-26",
                "outcome": "WIN",
                "option_pnl_pct": 167.0,
                "stock_move_pct": -23.0,
                "entry_p_success": 94,
                "entry_science_grade": "F",
                "entry_catalyst_type": "READOUT",
                "entry_direction": "LONG_PUTS",
                "entry_fill": 1.70,
                "entry_stock": 32.92,
                "entry_date": "2026-04-10",
                "strike": 30.0,
                "expiry": "2026-08-21",
            },
        ]
    }


def _make_active_plays() -> dict[str, dict]:
    return {
        "active": {
            "AAA": {
                "ticker": "AAA",
                "nct_id": "NCT11111111",
                "direction": "LONG_CALLS",
                "option_strike": 10,
                "option_expiry": "2026-07-17",
                "option_type": "C",
                "added_date": "2026-04-01",
                "p_success": 70,
                "science_grade": "B",
            },
            "BBB": {
                "ticker": "BBB",
                "nct_id": "NCT22222222",
                "direction": "LONG_PUTS",
                "option_strike": 25,
                "option_expiry": "2026-08-21",
                "option_type": "P",
                "added_date": "2026-04-05",
                "p_success": 55,
                "science_grade": "C",
            },
        },
        "monitor": {
            "CCC": {
                "ticker": "CCC",
                "nct_id": "NCT33333333",
                "direction": "LONG_CALLS",
                "option_strike": 5,
                "added_date": "2026-04-10",
                "p_success": 45,
                "science_grade": "C",
            }
        },
    }


def _make_discovery_state(nct_count: int) -> dict[str, object]:
    return {
        "seen_nct_ids": [f"NCT{i:08d}" for i in range(nct_count)],
        "last_run": "2026-04-26",
    }


def _make_performance_ledger() -> dict[str, object]:
    return {
        "plays": {
            "AAA": {
                "ticker": "AAA",
                "snapshots": [
                    {"date": "2026-04-10", "pnl_1k": 50.0},
                    {"date": "2026-04-11", "pnl_1k": 75.0},
                ],
            },
            "BBB": {
                "ticker": "BBB",
                "snapshots": [
                    {"date": "2026-04-10", "pnl_1k": -20.0},
                    {"date": "2026-04-12", "pnl_1k": 10.0},
                ],
            },
        },
        "last_updated": "2026-04-26",
        "total_plays_tracked": 2,
    }


def _make_scoring_cache() -> list[dict]:
    return [
        {
            "ticker": "AAA",
            "as_of_date": "2026-04-26",
            "grok_rank": 1,
            "grok_score": 0.81,
            "claude_grade": "B+",
            "claude_probability": 0.62,
            "gemini_grade": "A-",
            "gemini_probability": 0.71,
            "ensemble_score": 0.71,
            "divergence_flag": True,
        },
        {
            "ticker": "BBB",
            "as_of_date": "2026-04-26",
            "grok_score": 0.55,
            "ensemble_score": 0.55,
            "divergence_flag": False,
        },
    ]


@pytest.fixture
def fixtures_dir(tmp_path: Path) -> Path:
    """Build a complete migration source directory under ``tmp_path``."""
    source = tmp_path / "state"
    source.mkdir()
    _write(source / "active_plays.json", _make_active_plays())
    _write(source / "resolved_plays.json", _make_six_resolved_plays())
    _write(source / "discovery_state.json", _make_discovery_state(261))
    _write(source / "performance_ledger.json", _make_performance_ledger())
    _write(source / "scoring_cache.json", _make_scoring_cache())
    return source


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "alpha.db"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_migrate_creates_db_with_six_resolved_plays(fixtures_dir, db_path):
    result = mig.migrate(source_dir=fixtures_dir, db_path=db_path)
    assert db_path.exists()
    # 2 active + 1 monitor + 6 resolved = 9 rows total
    assert result.plays_inserted == 9

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        resolved = conn.execute(
            "SELECT COUNT(*) FROM plays WHERE status='resolved'"
        ).fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM plays WHERE status='active'"
        ).fetchone()[0]
        monitor = conn.execute(
            "SELECT COUNT(*) FROM plays WHERE status='monitor'"
        ).fetchone()[0]
        assert resolved == 6
        assert active == 2
        assert monitor == 1
    finally:
        conn.close()


def test_migrate_preserves_261_nct_ids(fixtures_dir, db_path):
    mig.migrate(source_dir=fixtures_dir, db_path=db_path)
    conn = sqlite3.connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM discovery_state").fetchone()[0]
        assert n == 261
    finally:
        conn.close()


def test_migrate_nct_id_set_matches_source(fixtures_dir, db_path):
    """The NCT-id set in SQLite must exactly equal the JSON source set."""
    mig.migrate(source_dir=fixtures_dir, db_path=db_path)
    source_doc = json.loads((fixtures_dir / "discovery_state.json").read_text())
    expected = set(source_doc["seen_nct_ids"])
    conn = sqlite3.connect(db_path)
    try:
        actual = {r[0] for r in conn.execute("SELECT nct_id FROM discovery_state")}
        assert actual == expected
    finally:
        conn.close()


def test_migrate_writes_performance_ledger_per_unique_date(fixtures_dir, db_path):
    mig.migrate(source_dir=fixtures_dir, db_path=db_path)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT as_of_date, play_count FROM performance_ledger ORDER BY as_of_date"
        ).fetchall()
    finally:
        conn.close()
    # Three distinct dates in the fixture: 2026-04-10, 04-11, 04-12.
    assert [r[0] for r in rows] == ["2026-04-10", "2026-04-11", "2026-04-12"]
    # 04-10 has snapshots from both AAA and BBB
    assert dict(rows[0:1])["2026-04-10"] == 2


def test_migrate_writes_scoring_cache_when_present(fixtures_dir, db_path):
    mig.migrate(source_dir=fixtures_dir, db_path=db_path)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT ticker, divergence_flag FROM scoring_cache ORDER BY ticker"
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == ["AAA", "BBB"]
    # divergence_flag stored as 0/1 integer
    assert rows[0][1] == 1
    assert rows[1][1] == 0


def test_migrate_bumps_schema_version(fixtures_dir, db_path):
    result = mig.migrate(source_dir=fixtures_dir, db_path=db_path)
    assert result.schema_version == db.CURRENT_VERSION
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == db.CURRENT_VERSION


def test_migrate_returns_structured_summary(fixtures_dir, db_path):
    result = mig.migrate(source_dir=fixtures_dir, db_path=db_path)
    assert result.duration_ms >= 0
    assert result.discovery_state_inserted == 261
    assert result.performance_ledger_inserted == 3
    assert result.scoring_cache_inserted == 2


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_migrate_is_idempotent_on_second_run(fixtures_dir, db_path):
    mig.migrate(source_dir=fixtures_dir, db_path=db_path)
    # snapshot row counts after first run
    conn = sqlite3.connect(db_path)
    try:
        before = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in (
                "plays",
                "discovery_state",
                "performance_ledger",
                "scoring_cache",
                "schema_version",
            )
        }
    finally:
        conn.close()

    # second run should produce identical row counts
    mig.migrate(source_dir=fixtures_dir, db_path=db_path)
    conn = sqlite3.connect(db_path)
    try:
        after = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in before
        }
    finally:
        conn.close()
    assert after == before


def test_migrate_idempotency_enforced_by_unique_constraints(fixtures_dir, db_path):
    """Even direct duplicate inserts must be rejected by SQL constraints."""
    mig.migrate(source_dir=fixtures_dir, db_path=db_path)
    conn = sqlite3.connect(db_path)
    try:
        # discovery_state: nct_id is PRIMARY KEY
        nct = conn.execute("SELECT nct_id FROM discovery_state LIMIT 1").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO discovery_state (nct_id) VALUES (?)",
                (nct,),
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_migrate_raises_on_corrupt_json(tmp_path: Path):
    source = tmp_path / "state"
    source.mkdir()
    _write(source / "active_plays.json", {"active": {}})
    _write(source / "performance_ledger.json", {"plays": {}})
    _write(source / "discovery_state.json", _make_discovery_state(2))
    # Truncated JSON → JSONDecodeError surface
    (source / "resolved_plays.json").write_text("not json {{")
    db_path = tmp_path / "alpha.db"
    with pytest.raises(json.JSONDecodeError):
        mig.migrate(source_dir=source, db_path=db_path)


def test_migrate_rolls_back_on_corrupt_partial(tmp_path: Path):
    """Partial commit must not leave plays populated when a later step fails."""
    source = tmp_path / "state"
    source.mkdir()
    _write(source / "active_plays.json", _make_active_plays())
    _write(source / "resolved_plays.json", _make_six_resolved_plays())
    _write(source / "discovery_state.json", _make_discovery_state(3))
    # Truncate the perf ledger so step 3 raises
    (source / "performance_ledger.json").write_text("{")
    db_path = tmp_path / "alpha.db"
    with pytest.raises(json.JSONDecodeError):
        mig.migrate(source_dir=source, db_path=db_path)
    # The connection commits a single transaction, so plays/ngc rows must
    # have been rolled back. ``schema_version`` is committed by
    # ``run_migrations`` which is a separate transaction (intentional —
    # the schema is allowed to exist even if data load fails).
    conn = sqlite3.connect(db_path)
    try:
        plays = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        nct = conn.execute("SELECT COUNT(*) FROM discovery_state").fetchone()[0]
    finally:
        conn.close()
    assert plays == 0
    assert nct == 0


def test_migrate_raises_on_missing_required_file(tmp_path: Path):
    source = tmp_path / "state"
    source.mkdir()
    # Only some of the required files are present
    _write(source / "active_plays.json", {"active": {}})
    db_path = tmp_path / "alpha.db"
    with pytest.raises(FileNotFoundError):
        mig.migrate(source_dir=source, db_path=db_path)


def test_migrate_raises_on_missing_source_directory(tmp_path: Path):
    missing = tmp_path / "nonexistent"
    with pytest.raises(FileNotFoundError):
        mig.migrate(source_dir=missing, db_path=tmp_path / "alpha.db")


def test_migrate_handles_optional_scoring_cache_missing(tmp_path: Path):
    source = tmp_path / "state"
    source.mkdir()
    _write(source / "active_plays.json", _make_active_plays())
    _write(source / "resolved_plays.json", _make_six_resolved_plays())
    _write(source / "discovery_state.json", _make_discovery_state(5))
    _write(source / "performance_ledger.json", _make_performance_ledger())
    # NOTE: scoring_cache.json deliberately omitted

    db_path = tmp_path / "alpha.db"
    result = mig.migrate(source_dir=source, db_path=db_path)
    assert result.scoring_cache_inserted == 0
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM scoring_cache").fetchone()[0] == 0
        # Other sections still populated.
        assert conn.execute(
            "SELECT COUNT(*) FROM plays WHERE status='resolved'"
        ).fetchone()[0] == 6
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_main_cli_dry_run_writes_summary_to_stdout(fixtures_dir, capsys, tmp_path):
    rc = mig.main(
        [
            "--source",
            str(fixtures_dir),
            "--db",
            str(tmp_path / "wont-be-touched.db"),
            "--dry-run",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out.splitlines()[-1])
    assert payload["plays_inserted"] >= 6  # at least the resolved set
    assert payload["discovery_state_inserted"] == 261
    assert payload["dry_run"] is True
    # dry-run must not create the db file
    assert not (tmp_path / "wont-be-touched.db").exists()


def test_main_cli_returns_nonzero_on_corrupt_json(tmp_path: Path, capsys):
    source = tmp_path / "state"
    source.mkdir()
    _write(source / "active_plays.json", {"active": {}})
    _write(source / "performance_ledger.json", {"plays": {}})
    _write(source / "discovery_state.json", _make_discovery_state(2))
    (source / "resolved_plays.json").write_text("not json {{")
    rc = mig.main(
        [
            "--source",
            str(source),
            "--db",
            str(tmp_path / "alpha.db"),
        ]
    )
    assert rc == 3
    err = capsys.readouterr().err
    assert "JSON" in err or "json" in err
