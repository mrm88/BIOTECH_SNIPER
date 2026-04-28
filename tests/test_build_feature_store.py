"""Tests for ``biotech_sniper.training.build_feature_store``.

Each test builds a small SQLite + JSON fixture under ``tmp_path`` and
runs :func:`build` against a per-test parquet output. No network is
touched. The fixtures mirror the seed schema (``status='resolved'`` in
the plays table, ledger plays keyed by bare ticker with snapshots,
ScienceProfile records keyed by ``<TICKER>_<NCT_ID>``).

Tests cover the M5 contract assertions VAL-M5-001..009:

* schema columns + dtypes (VAL-M5-001 / VAL-M5-002)
* targets non-null for resolved rows (VAL-M5-003)
* all 4 sources opened — missing source raises clearly (VAL-M5-004)
* row count == COUNT(*) FROM resolved_plays (VAL-M5-005)
* idempotency on re-run (VAL-M5-006)
* iv_at_entry matches ledger ±1e-6 (VAL-M5-007)
* days_to_event = (catalyst - entry).days (VAL-M5-008)
* prior_phase2_data_quality from letter mapping (VAL-M5-009)
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from biotech_sniper import db
from biotech_sniper.training import build_feature_store as bfs


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _seed_db(db_path: Path, plays: list[dict]) -> None:
    """Initialise a fresh SQLite db with schema + the given resolved plays."""

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
        for play in plays:
            payload = play.get("payload") or {}
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
                    play.get("source_key", f"resolved:{play['ticker']}"),
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
                    json.dumps(payload, sort_keys=True),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _write_state(state_dir: Path, *, ledger=None, science=None, calibration=None,
                 pipelines=None) -> None:
    """Write the four JSON sidecars under ``state_dir``."""

    state_dir.mkdir(parents=True, exist_ok=True)
    if ledger is not None:
        (state_dir / "performance_ledger.json").write_text(
            json.dumps(ledger), encoding="utf-8"
        )
    if science is not None:
        (state_dir / "science_grades.json").write_text(
            json.dumps(science), encoding="utf-8"
        )
    if calibration is not None:
        (state_dir / "calibration_params.json").write_text(
            json.dumps(calibration), encoding="utf-8"
        )
    if pipelines is not None:
        (state_dir / "company_pipelines.json").write_text(
            json.dumps(pipelines), encoding="utf-8"
        )


# Six fixtures mirroring the M5 seed dataset, kept small + deterministic.
# Tickers include suffixed forms (``IDYA_35C``) so the bare-ticker
# stripping logic is exercised by every test.
_SIX_PLAYS = [
    {
        "ticker": "LPCN",
        "direction": "EQUITY_ONLY",
        "catalyst_type": "READOUT",
        "entry_date": "2026-03-30",
        "exit_date": "2026-04-03",
        "p_success": 62,
        "science_grade": None,
        "option_pnl_pct": -100.0,
        "payload": {
            "direction_correct": False,
            "entry_iv_pct": None,
            "original_play": {"indication": "Postpartum depression"},
        },
    },
    {
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
    },
    {
        "ticker": "TVTX",
        "direction": "LONG_CALLS",
        "catalyst_type": "LABEL_EXT",
        "entry_date": "2026-03-30",
        "exit_date": "2026-04-17",
        "option_strike": 35.0,
        "option_expiry": "2026-04-17",
        "p_success": 77,
        "science_grade": "B",
        "entry_fill": 1.20,
        "option_pnl_pct": -81.0,
        "payload": {
            "direction_correct": True,
            "entry_iv_pct": 305.0,
            "expiry": "2026-04-17",
        },
    },
    {
        "ticker": "IDYA_35C",
        "direction": "LONG_CALLS",
        "catalyst_type": "READOUT",
        "entry_date": "2026-03-30",
        "exit_date": "2026-04-14",
        "option_strike": 35.0,
        "option_expiry": "2026-05-15",
        "p_success": 96,
        "science_grade": "C",
        "entry_fill": 1.82,
        "option_pnl_pct": -91.0,
        "payload": {
            "direction_correct": True,
            "entry_iv_pct": 188.0,
            "expiry": "2026-05-15",
        },
    },
    {
        "ticker": "RVMD_125C",
        "direction": "LONG_CALLS",
        "catalyst_type": "READOUT",
        "entry_date": "2026-03-30",
        "exit_date": "2026-04-14",
        "option_strike": 125.0,
        "option_expiry": "2026-06-18",
        "p_success": 61,
        "science_grade": "C",
        "entry_fill": 3.20,
        "option_pnl_pct": 193.0,
        "payload": {
            "direction_correct": True,
            "entry_iv_pct": 80.0,
            "expiry": "2026-06-18",
        },
    },
    {
        "ticker": "AGIO_PUT",
        "direction": "LONG_PUTS",
        "catalyst_type": "READOUT",
        "entry_date": "2026-04-10",
        "exit_date": "2026-04-26",
        "option_strike": 30.0,
        "option_expiry": "2026-08-21",
        "p_success": 94,
        "science_grade": "F",
        "entry_fill": 1.70,
        "option_pnl_pct": 167.0,
        "payload": {
            "direction_correct": True,
            "entry_iv_pct": 90.0,
            "expiry": "2026-08-21",
        },
    },
]


def _ledger_with_entry_iv() -> dict:
    """Build a performance_ledger with explicit entry_iv_pct + snapshots."""

    return {
        "plays": {
            "LPCN": {"ticker": "LPCN", "entry_date": "2026-03-30", "entry_iv_pct": None},
            "VRDN": {"ticker": "VRDN", "entry_date": "2026-03-30", "entry_iv_pct": 50.0,
                     "snapshots": [
                         {"date": "2026-03-30", "iv_pct": 50.0},
                         {"date": "2026-04-07", "iv_pct": 10.0},
                     ]},
            "TVTX": {"ticker": "TVTX", "entry_date": "2026-03-30", "entry_iv_pct": 305.0,
                     "snapshots": [
                         {"date": "2026-04-17", "iv_pct": 30.0},
                     ]},
            "IDYA": {"ticker": "IDYA", "entry_date": "2026-03-30", "entry_iv_pct": 188.0,
                     "snapshots": [
                         {"date": "2026-04-14", "iv_pct": 90.0},
                     ]},
            "RVMD": {"ticker": "RVMD", "entry_date": "2026-03-30", "entry_iv_pct": 80.0,
                     "snapshots": [
                         {"date": "2026-04-14", "iv_pct": 60.0},
                     ]},
            "AGIO": {"ticker": "AGIO", "entry_date": "2026-04-10", "entry_iv_pct": 90.0,
                     "snapshots": [
                         {"date": "2026-04-26", "iv_pct": 70.0},
                     ]},
        }
    }


def _science_profiles() -> dict:
    return {
        "VRDN_NCT99999990": {"ticker": "VRDN", "grade": "C", "base_rate": 0.40,
                             "matched_category": "endocrine", "graded_date": "2026-03-29"},
        "TVTX_NCT03493685": {"ticker": "TVTX", "grade": "B", "base_rate": 0.45,
                             "matched_category": "default", "graded_date": "2026-03-29"},
        "IDYA_NCT05987332": {"ticker": "IDYA", "grade": "C", "base_rate": 0.52,
                             "matched_category": "melanoma", "graded_date": "2026-03-29"},
        "RVMD_NCT06625320": {"ticker": "RVMD", "grade": "C", "base_rate": 0.15,
                             "matched_category": "pancreatic cancer", "graded_date": "2026-03-29"},
        "AGIO_NCT05490446": {"ticker": "AGIO", "grade": "F", "base_rate": 0.45,
                             "matched_category": "default", "graded_date": "2026-03-29"},
    }


def _calibration_params() -> dict:
    return {"version": 1, "n_resolved": 6, "p_min_long": 65}


def _company_pipelines() -> dict:
    return {
        "VRDN": {"ticker": "VRDN", "market_cap": 1_415_176_988, "sector_group": "Endocrine"},
        "TVTX": {"ticker": "TVTX", "market_cap": 3_740_977_386, "sector_group": "Renal"},
        "IDYA": {"ticker": "IDYA", "market_cap": 2_698_525_009, "sector_group": "Oncology"},
        "RVMD": {"ticker": "RVMD", "market_cap": 28_613_878_661, "sector_group": "Oncology"},
        "AGIO": {"ticker": "AGIO", "market_cap": 1_483_553_777, "sector_group": "Hematology"},
    }


@pytest.fixture
def six_play_environment(tmp_path: Path):
    """Stand up a fresh DB + state dir + empty seed dir with 6 plays."""

    db_path = tmp_path / "data" / "alpha_sniper.db"
    out_path = tmp_path / "data" / "training" / "catalysts.parquet"
    state_dir = tmp_path / "state"
    seed_dir = tmp_path / "migrations" / "seed"
    seed_dir.mkdir(parents=True, exist_ok=True)

    _seed_db(db_path, _SIX_PLAYS)
    _write_state(
        state_dir,
        ledger=_ledger_with_entry_iv(),
        science=_science_profiles(),
        calibration=_calibration_params(),
        pipelines=_company_pipelines(),
    )

    return {
        "db_path": db_path,
        "out_path": out_path,
        "state_dir": state_dir,
        "seed_dir": seed_dir,
    }


# ---------------------------------------------------------------------------
# VAL-M5-001 / VAL-M5-002 — schema + dtypes
# ---------------------------------------------------------------------------


def test_parquet_has_required_columns_and_dtypes(six_play_environment):
    env = six_play_environment
    bfs.build(**env)

    df = pd.read_parquet(env["out_path"])

    # VAL-M5-001: every required feature + target column is present.
    missing = set(bfs.REQUIRED_COLS) - set(df.columns)
    assert not missing, f"missing columns: {missing}"

    # VAL-M5-002: numeric float dtype.
    for col in (
        "base_rate", "p_ensemble", "iv_at_entry", "dte_at_entry",
        "market_cap", "sponsor_size", "days_to_event",
        "option_pnl_pct", "iv_crush_pct",
    ):
        assert pd.api.types.is_float_dtype(df[col]), (
            f"{col} should be float, got {df[col].dtype}"
        )

    # VAL-M5-002: bool target.
    assert pd.api.types.is_bool_dtype(df["directional_correct"])

    # VAL-M5-002: categorical / string features.
    for col in ("science_grade", "sector", "prior_phase2_data_quality",
                "indication_class"):
        assert isinstance(df[col].dtype, pd.CategoricalDtype), (
            f"{col} should be categorical, got {df[col].dtype}"
        )


def test_required_cols_constants_align_with_spec():
    # Defensive: catches a future refactor that drops a column from the
    # canonical schema list.
    assert set(bfs.REQUIRED_FEATURE_COLS) == {
        "science_grade", "base_rate", "p_ensemble", "iv_at_entry",
        "dte_at_entry", "market_cap", "sector", "prior_phase2_data_quality",
        "sponsor_size", "indication_class", "days_to_event",
    }
    assert set(bfs.REQUIRED_TARGET_COLS) == {
        "directional_correct", "option_pnl_pct", "iv_crush_pct",
    }


# ---------------------------------------------------------------------------
# VAL-M5-003 — targets non-null for resolved rows
# ---------------------------------------------------------------------------


def test_targets_non_null_for_resolved_rows(six_play_environment):
    env = six_play_environment
    bfs.build(**env)

    df = pd.read_parquet(env["out_path"])
    resolved = df[df["resolved"]]
    assert len(resolved) == len(df), "all rows in seed parquet should be resolved"

    for col in bfs.REQUIRED_TARGET_COLS:
        nulls = resolved[col].isna().sum()
        assert nulls == 0, f"{col} has {nulls} nulls in resolved rows"


# ---------------------------------------------------------------------------
# VAL-M5-004 — all sources opened; missing required source raises
# ---------------------------------------------------------------------------


def test_missing_db_raises_missing_source(tmp_path: Path):
    seed_dir = tmp_path / "migrations" / "seed"
    seed_dir.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    _write_state(state_dir, ledger={"plays": {}}, calibration={"version": 1})
    with pytest.raises(bfs.MissingSourceError, match="SQLite"):
        bfs.build(
            db_path=tmp_path / "missing.db",
            out_path=tmp_path / "out.parquet",
            state_dir=state_dir,
            seed_dir=seed_dir,
        )


def test_missing_calibration_raises(tmp_path: Path):
    db_path = tmp_path / "data" / "alpha_sniper.db"
    _seed_db(db_path, _SIX_PLAYS)
    state_dir = tmp_path / "state"
    _write_state(state_dir, ledger={"plays": {}})
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir(parents=True, exist_ok=True)

    with pytest.raises(bfs.MissingSourceError, match="calibration_params"):
        bfs.build(
            db_path=db_path,
            out_path=tmp_path / "out.parquet",
            state_dir=state_dir,
            seed_dir=seed_dir,
        )


def test_missing_ledger_raises(tmp_path: Path):
    db_path = tmp_path / "data" / "alpha_sniper.db"
    _seed_db(db_path, _SIX_PLAYS)
    state_dir = tmp_path / "state"
    _write_state(state_dir, calibration={"version": 1})
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir(parents=True, exist_ok=True)

    with pytest.raises(bfs.MissingSourceError, match="performance_ledger"):
        bfs.build(
            db_path=db_path,
            out_path=tmp_path / "out.parquet",
            state_dir=state_dir,
            seed_dir=seed_dir,
        )


# ---------------------------------------------------------------------------
# VAL-M5-005 — row count equals resolved-plays count
# ---------------------------------------------------------------------------


def test_row_count_equals_resolved_plays_count(six_play_environment):
    env = six_play_environment
    bfs.build(**env)

    df = pd.read_parquet(env["out_path"])

    conn = sqlite3.connect(str(env["db_path"]))
    try:
        sqlite_count = conn.execute(
            "SELECT COUNT(*) FROM plays WHERE status='resolved'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert len(df) == sqlite_count == 6


# ---------------------------------------------------------------------------
# VAL-M5-006 — idempotency
# ---------------------------------------------------------------------------


def test_rerun_does_not_duplicate_rows(six_play_environment):
    env = six_play_environment

    first = bfs.build(**env)
    df1 = pd.read_parquet(env["out_path"])

    second = bfs.build(**env)
    df2 = pd.read_parquet(env["out_path"])

    assert first.rows == second.rows == len(df1) == len(df2) == 6
    assert df1.shape == df2.shape

    # No duplicate (ticker, catalyst_date) keys.
    dup_count = df2.duplicated(subset=["ticker", "catalyst_date"]).sum()
    assert dup_count == 0, f"found {dup_count} duplicates after re-run"

    # Frames are equal cell-by-cell (modulo categorical ordering).
    pd.testing.assert_frame_equal(
        df1.reset_index(drop=True),
        df2.reset_index(drop=True),
        check_dtype=False,
    )


# ---------------------------------------------------------------------------
# VAL-M5-007 — iv_at_entry matches ledger ±1e-6
# ---------------------------------------------------------------------------


def test_iv_at_entry_matches_ledger(six_play_environment):
    env = six_play_environment
    bfs.build(**env)

    df = pd.read_parquet(env["out_path"])
    ledger = json.loads(
        (env["state_dir"] / "performance_ledger.json").read_text("utf-8")
    )

    expected = {
        ticker: (rec.get("entry_iv_pct") or 0) / 100.0
        for ticker, rec in ledger["plays"].items()
        if rec.get("entry_iv_pct") is not None
    }

    for _, row in df.iterrows():
        if row["ticker"] not in expected:
            continue
        assert abs(row["iv_at_entry"] - expected[row["ticker"]]) < 1e-6, (
            f"{row['ticker']} iv mismatch"
        )


# ---------------------------------------------------------------------------
# VAL-M5-008 — days_to_event = (catalyst_date - entry_date).days
# ---------------------------------------------------------------------------


def test_days_to_event_matches_date_diff(six_play_environment):
    env = six_play_environment
    bfs.build(**env)

    df = pd.read_parquet(env["out_path"])
    for _, row in df.iterrows():
        if pd.isna(row["entry_date"]) or pd.isna(row["catalyst_date"]):
            continue
        expected = (
            pd.to_datetime(row["catalyst_date"])
            - pd.to_datetime(row["entry_date"])
        ).days
        assert row["days_to_event"] == expected, (
            f"{row['ticker']}: got {row['days_to_event']}, expected {expected}"
        )


# ---------------------------------------------------------------------------
# VAL-M5-009 — prior_phase2_data_quality from letter mapping
# ---------------------------------------------------------------------------


def test_prior_phase2_data_quality_letter_mapping(six_play_environment):
    env = six_play_environment
    bfs.build(**env)

    df = pd.read_parquet(env["out_path"])
    for _, row in df.iterrows():
        grade = row["science_grade"]
        prior = row["prior_phase2_data_quality"]
        if pd.isna(grade):
            assert pd.isna(prior), f"prior should be NA when grade is NA"
            continue
        expected = bfs.PRIOR_PHASE2_DATA_QUALITY_MAP[str(grade)]
        # Stored as string-categorical to preserve the parquet ``category``
        # dtype on round-trip; the validator's mapping comparison is
        # taken on the integer interpretation.
        assert int(prior) == expected, (
            f"{row['ticker']}: grade={grade}, prior={prior}, expected={expected}"
        )


# ---------------------------------------------------------------------------
# Schema sanity: identity columns, bare-ticker normalisation
# ---------------------------------------------------------------------------


def test_ticker_is_stripped_of_option_suffix(six_play_environment):
    env = six_play_environment
    bfs.build(**env)

    df = pd.read_parquet(env["out_path"])
    bare = set(df["ticker"].tolist())
    # The seed has IDYA_35C, RVMD_125C, AGIO_PUT — bare tickers must
    # collapse to the prefix so the (ticker, entry_date) join against
    # the ledger resolves.
    assert "IDYA" in bare and "RVMD" in bare and "AGIO" in bare
    assert "IDYA_35C" not in bare


def test_dte_at_entry_uses_option_expiry(six_play_environment):
    env = six_play_environment
    bfs.build(**env)
    df = pd.read_parquet(env["out_path"]).set_index("ticker")
    # TVTX expiry 2026-04-17, entry 2026-03-30 → 18 days.
    assert df.loc["TVTX", "dte_at_entry"] == 18.0
    # AGIO expiry 2026-08-21, entry 2026-04-10 → 133 days.
    assert df.loc["AGIO", "dte_at_entry"] == 133.0
    # LPCN has no option leg → NaN.
    assert math.isnan(df.loc["LPCN", "dte_at_entry"])


def test_p_ensemble_derived_from_p_success(six_play_environment):
    env = six_play_environment
    bfs.build(**env)
    df = pd.read_parquet(env["out_path"]).set_index("ticker")
    # IDYA p_success=96 → p_ensemble=0.96.
    assert abs(df.loc["IDYA", "p_ensemble"] - 0.96) < 1e-9
    # AGIO p_success=94 → p_ensemble=0.94.
    assert abs(df.loc["AGIO", "p_ensemble"] - 0.94) < 1e-9


def test_market_cap_and_sector_from_company_pipelines(six_play_environment):
    env = six_play_environment
    bfs.build(**env)
    df = pd.read_parquet(env["out_path"]).set_index("ticker")
    # RVMD market_cap from pipeline.
    assert df.loc["RVMD", "market_cap"] == 28_613_878_661
    # Sector from sector_group.
    assert df.loc["RVMD", "sector"] == "Oncology"


def test_iv_crush_pct_uses_ledger_snapshots(six_play_environment):
    env = six_play_environment
    bfs.build(**env)
    df = pd.read_parquet(env["out_path"]).set_index("ticker")
    # IDYA: entry IV 188% → final snapshot 90% → drop = (188-90)/188 ~= 52.13%.
    assert abs(df.loc["IDYA", "iv_crush_pct"] - (188 - 90) / 188 * 100) < 1e-9
    # RVMD: 80 → 60 → 25%.
    assert abs(df.loc["RVMD", "iv_crush_pct"] - 25.0) < 1e-9
    # LPCN: no entry IV → 0.
    assert df.loc["LPCN", "iv_crush_pct"] == 0.0


def test_build_handles_zero_resolved_plays(tmp_path: Path):
    """An empty plays table still yields a valid empty parquet."""

    db_path = tmp_path / "alpha_sniper.db"
    state_dir = tmp_path / "state"
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir(parents=True, exist_ok=True)

    # Empty plays table — schema applied, no rows.
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
    finally:
        conn.close()

    _write_state(
        state_dir,
        ledger={"plays": {}},
        calibration={"version": 1},
        science={},
        pipelines={},
    )

    out_path = tmp_path / "out.parquet"
    result = bfs.build(
        db_path=db_path,
        out_path=out_path,
        state_dir=state_dir,
        seed_dir=seed_dir,
    )
    assert result.rows == 0
    df = pd.read_parquet(out_path)
    assert df.empty
    # Schema preserved even when empty.
    for col in bfs.REQUIRED_COLS:
        assert col in df.columns


def test_cli_main_writes_parquet(tmp_path: Path, capsys, six_play_environment):
    env = six_play_environment
    rc = bfs.main(
        [
            "--db", str(env["db_path"]),
            "--out", str(env["out_path"]),
            "--state-dir", str(env["state_dir"]),
            "--seed-dir", str(env["seed_dir"]),
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert payload["rows"] == 6
    assert payload["sqlite_resolved_count"] == 6
    assert env["out_path"].exists()


def test_cli_returns_2_on_missing_source(tmp_path: Path, capsys):
    rc = bfs.main(
        [
            "--db", str(tmp_path / "missing.db"),
            "--out", str(tmp_path / "out.parquet"),
            "--state-dir", str(tmp_path / "state"),
            "--seed-dir", str(tmp_path / "seed"),
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "ERROR" in err
