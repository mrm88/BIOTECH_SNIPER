"""Tests for the M5 backtest harness (``biotech_sniper.backtest``).

The harness is exercised end-to-end against per-test seed and state
directories built under ``tmp_path``. The fixtures mirror the shape of
the real seed data (six resolved plays, a CT.gov amendment timeline)
so the tests cover the same code paths the production CLI takes when
invoked against ``BASE_DIR/migrations/seed/``.

Tests cover the M5 backtest contract assertions:

* VAL-M5-010 — CLI accepts ``--from`` / ``--to`` and replays events.
* VAL-M5-011 — Report includes Brier, directional accuracy, and
  per-play P&L.
* VAL-M5-012 — Report path is ``reports/backtest_<from>_<to>.json``.
* VAL-M5-013 — Two runs produce identical metrics (deterministic).
* VAL-M5-014 — Calibration deltas applied only when ``n>=3`` per bucket.
* VAL-M5-015 — Old ``calibration_params.json`` preserved as
  timestamped backup with matching SHA-256.
* VAL-M5-016 — Active / resolved / performance / discovery state files
  unchanged after a backtest run.
* VAL-M5-017 — Reproduces Brier ≈0.1195 ±0.001 and dir-acc ≈83% ±0.5pp
  over the seed window with ``--no-apply-calibration``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import pytest

from biotech_sniper import backtest, learning_engine


# ---------------------------------------------------------------------------
# Fixture helpers — six resolved plays mirroring the seed dataset.
# ---------------------------------------------------------------------------


def _seed_resolved() -> list[dict]:
    """Return the six-play seed dataset used to validate baselines.

    The values are taken verbatim from
    ``migrations/seed/resolved_plays.json`` so the test reproduces the
    canonical Brier ≈ 0.1195 and dir-acc ≈ 83% baseline.
    """

    return [
        {
            "ticker": "LPCN",
            "entry_date": "2026-03-30",
            "resolved_date": "2026-04-03",
            "entry_p_success": 62,
            "direction_correct": False,
            "option_pnl_pct": -100.0,
            "entry_catalyst_type": "READOUT",
        },
        {
            "ticker": "VRDN",
            "entry_date": "2026-03-30",
            "resolved_date": "2026-04-07",
            "entry_p_success": 65,
            "direction_correct": True,
            "option_pnl_pct": -65.0,
            "entry_catalyst_type": "READOUT",
        },
        {
            "ticker": "TVTX",
            "entry_date": "2026-03-30",
            "resolved_date": "2026-04-17",
            "entry_p_success": 77,
            "direction_correct": True,
            "option_pnl_pct": -81.0,
            "entry_catalyst_type": "LABEL_EXT",
        },
        {
            "ticker": "IDYA_35C",
            "entry_date": "2026-03-30",
            "resolved_date": "2026-04-14",
            "entry_p_success": 96,
            "direction_correct": True,
            "option_pnl_pct": -91.0,
            "entry_catalyst_type": "READOUT",
        },
        {
            "ticker": "RVMD_125C",
            "entry_date": "2026-03-30",
            "resolved_date": "2026-04-14",
            "entry_p_success": 61,
            "direction_correct": True,
            "option_pnl_pct": 193.0,
            "entry_catalyst_type": "READOUT",
        },
        {
            "ticker": "AGIO_PUT",
            "entry_date": "2026-04-10",
            "resolved_date": "2026-04-26",
            "entry_p_success": 94,
            "direction_correct": True,
            "option_pnl_pct": 167.0,
            "entry_catalyst_type": "READOUT",
        },
    ]


def _seed_amendments() -> dict:
    return {
        "NCT05987332": {
            "ticker": "IDYA",
            "status": {"overall": "ACTIVE_NOT_RECRUITING", "last_update": "2026-02-17"},
        },
        "NCT05490446": {
            "ticker": "AGIO",
            "status": {"overall": "ACTIVE_NOT_RECRUITING", "last_update": "2026-03-30"},
        },
        "NCT_OUT_OF_WINDOW": {
            "ticker": "NONE",
            "status": {"overall": "COMPLETED", "last_update": "2024-09-05"},
        },
    }


def _seed_calibration() -> dict:
    return {
        "version": 1,
        "n_resolved": 6,
        "p_min_long": 65,
        "p_max_short": 40,
    }


def _make_env(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Stand up a per-test (state_dir, seed_dir, reports_dir) trio."""

    state_dir = tmp_path / "state"
    seed_dir = tmp_path / "migrations" / "seed"
    reports_dir = tmp_path / "reports"
    state_dir.mkdir(parents=True, exist_ok=True)
    seed_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    (seed_dir / "resolved_plays.json").write_text(
        json.dumps({"resolved": _seed_resolved()}), encoding="utf-8"
    )
    (seed_dir / "amendment_state.json").write_text(
        json.dumps(_seed_amendments()), encoding="utf-8"
    )
    return state_dir, seed_dir, reports_dir


# ---------------------------------------------------------------------------
# learning_engine.process_event tests
# ---------------------------------------------------------------------------


def test_process_event_resolved_play_derives_bucket_and_brier():
    rec = learning_engine.process_event(
        {
            "type": "resolved_play",
            "data": {
                "ticker": "X",
                "entry_p_success": 70,
                "direction_correct": True,
                "option_pnl_pct": 12.0,
            },
        }
    )
    assert rec["bucket"] == "P65-75"
    assert rec["directional_correct"] is True
    # (0.70 - 1.0)^2 = 0.09
    assert pytest.approx(rec["brier_contribution"], abs=1e-9) == 0.09


def test_process_event_amendment_normalises_record():
    rec = learning_engine.process_event(
        {
            "type": "ctgov_amendment",
            "nct_id": "NCT12345",
            "data": {
                "ticker": "ZZZ",
                "status": {"overall": "ACTIVE", "last_update": "2026-02-17"},
            },
        }
    )
    assert rec["nct_id"] == "NCT12345"
    assert rec["last_update"] == "2026-02-17"
    assert rec["normalized"] is True


def test_process_event_unknown_type_is_ignored():
    rec = learning_engine.process_event({"type": "mystery"})
    assert rec.get("ignored") is True


# ---------------------------------------------------------------------------
# CLI / harness tests
# ---------------------------------------------------------------------------


def test_cli_writes_report_at_canonical_path(tmp_path):
    state_dir, seed_dir, reports_dir = _make_env(tmp_path)

    rc = backtest.main(
        [
            "--from",
            "2025-10-01",
            "--to",
            "2026-04-25",
            "--no-apply-calibration",
            "--state-dir",
            str(state_dir),
            "--seed-dir",
            str(seed_dir),
            "--reports-dir",
            str(reports_dir),
        ]
    )
    assert rc == 0

    report_path = reports_dir / "backtest_2025-10-01_2026-04-25.json"
    assert report_path.exists(), "VAL-M5-012 report path"
    payload = json.loads(report_path.read_text())

    # VAL-M5-011: required metric keys + plays array.
    assert "metrics" in payload and "plays" in payload
    assert {"brier", "directional_accuracy", "n"} <= set(payload["metrics"].keys())
    assert isinstance(payload["plays"], list) and payload["plays"]
    for play in payload["plays"]:
        assert "ticker" in play
        assert "entry_date" in play
        assert "option_pnl_pct" in play
        assert "directional_correct" in play

    # VAL-M5-010: replay invokes process_event at least once per event.
    assert payload["events_processed"] >= len(payload["plays"])


def test_baseline_metrics_reproduced(tmp_path):
    """VAL-M5-017: ``--no-apply-calibration`` reproduces seed baselines."""

    state_dir, seed_dir, reports_dir = _make_env(tmp_path)

    result = backtest.run_backtest(
        frm=dt.date(2025, 10, 1),
        to=dt.date(2026, 4, 25),
        apply_calibration=False,
        state_dir=state_dir,
        seed_dir=seed_dir,
        reports_dir=reports_dir,
    )

    assert result.metrics["n"] == 6
    assert abs(result.metrics["brier"] - 0.1195) <= 0.001
    assert abs(result.metrics["directional_accuracy"] - 0.8333) <= 0.005


def test_run_is_deterministic(tmp_path):
    """VAL-M5-013: same window twice → identical metrics + plays."""

    state_dir, seed_dir, reports_dir = _make_env(tmp_path)

    first = backtest.run_backtest(
        frm=dt.date(2025, 10, 1),
        to=dt.date(2026, 4, 25),
        apply_calibration=False,
        state_dir=state_dir,
        seed_dir=seed_dir,
        reports_dir=reports_dir,
    )
    second = backtest.run_backtest(
        frm=dt.date(2025, 10, 1),
        to=dt.date(2026, 4, 25),
        apply_calibration=False,
        state_dir=state_dir,
        seed_dir=seed_dir,
        reports_dir=reports_dir,
    )

    assert first.metrics == second.metrics
    assert first.plays == second.plays
    assert first.events_processed == second.events_processed


def test_calibration_skipped_when_no_bucket_has_three(tmp_path):
    """VAL-M5-014: with the seed dataset, no bucket has n>=3 so no
    calibration delta is applied even with ``--apply-calibration``."""

    state_dir, seed_dir, reports_dir = _make_env(tmp_path)
    (state_dir / "calibration_params.json").write_text(
        json.dumps(_seed_calibration()), encoding="utf-8"
    )

    result = backtest.run_backtest(
        frm=dt.date(2025, 10, 1),
        to=dt.date(2026, 4, 25),
        apply_calibration=True,
        state_dir=state_dir,
        seed_dir=seed_dir,
        reports_dir=reports_dir,
    )

    for label, info in result.buckets.items():
        if info["n"] < 3:
            assert info["applied"] is False, f"bucket {label} mistakenly applied"
    assert result.calibration["applied"] is False
    assert result.calibration["backup_path"] is None


def test_calibration_backup_on_qualifying_bucket(tmp_path):
    """VAL-M5-015: when at least one bucket qualifies, a timestamped
    backup is written before any mutation and its SHA-256 matches the
    pre-run file."""

    state_dir, seed_dir, reports_dir = _make_env(tmp_path)
    # Inflate the P75-85 bucket to n>=3 by appending two synthetic plays
    # so the n>=3 gate fires and the harness performs a backup.
    extra = list(_seed_resolved())
    for i in range(2):
        extra.append(
            {
                "ticker": f"SYNTH{i}",
                "entry_date": "2026-04-01",
                "resolved_date": "2026-04-02",
                "entry_p_success": 80,
                "direction_correct": True,
                "option_pnl_pct": 10.0,
                "entry_catalyst_type": "READOUT",
            }
        )
    (seed_dir / "resolved_plays.json").write_text(
        json.dumps({"resolved": extra}), encoding="utf-8"
    )

    calib_path = state_dir / "calibration_params.json"
    calib_payload = _seed_calibration()
    calib_path.write_text(json.dumps(calib_payload), encoding="utf-8")
    pre_run_sha = hashlib.sha256(calib_path.read_bytes()).hexdigest()

    result = backtest.run_backtest(
        frm=dt.date(2025, 10, 1),
        to=dt.date(2026, 4, 25),
        apply_calibration=True,
        state_dir=state_dir,
        seed_dir=seed_dir,
        reports_dir=reports_dir,
    )

    assert result.buckets["P75-85"]["n"] >= 3
    assert result.buckets["P75-85"]["applied"] is True
    assert result.calibration["applied"] is True
    backup_path = Path(result.calibration["backup_path"])
    assert backup_path.exists()
    assert backup_path.name.startswith("calibration_params_backup_")
    assert backup_path.name.endswith(".json")
    backup_sha = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    assert backup_sha == pre_run_sha
    assert result.calibration["backup_sha256"] == pre_run_sha
    assert result.calibration["pre_run_sha256"] == pre_run_sha


def test_state_files_untouched_after_run(tmp_path):
    """VAL-M5-016: backtest does not modify active/resolved/perf/discovery state."""

    state_dir, seed_dir, reports_dir = _make_env(tmp_path)
    # Create the state files the contract names. The harness is supposed
    # to leave them alone (they're inputs at most).
    state_files = {
        "active_plays.json": {"active": []},
        "resolved_plays.json": {"resolved": []},
        "performance_ledger.json": {"plays": {}},
        "discovery_state.json": {"nct_ids": []},
    }
    for name, payload in state_files.items():
        (state_dir / name).write_text(json.dumps(payload), encoding="utf-8")

    snapshots = {
        name: hashlib.sha256((state_dir / name).read_bytes()).hexdigest()
        for name in state_files
    }

    backtest.run_backtest(
        frm=dt.date(2025, 10, 1),
        to=dt.date(2026, 4, 25),
        apply_calibration=True,  # even with apply enabled — no qualifying bucket here
        state_dir=state_dir,
        seed_dir=seed_dir,
        reports_dir=reports_dir,
    )

    for name, expected_sha in snapshots.items():
        actual_sha = hashlib.sha256((state_dir / name).read_bytes()).hexdigest()
        assert actual_sha == expected_sha, f"{name} was modified by backtest"


def test_invalid_window_rejected(tmp_path):
    state_dir, seed_dir, reports_dir = _make_env(tmp_path)
    with pytest.raises(SystemExit):
        # ``--to`` before ``--from`` triggers parser.error → SystemExit(2).
        backtest.main(
            [
                "--from",
                "2026-04-25",
                "--to",
                "2025-10-01",
                "--state-dir",
                str(state_dir),
                "--seed-dir",
                str(seed_dir),
                "--reports-dir",
                str(reports_dir),
            ]
        )


def test_malformed_date_rejected(tmp_path, capsys):
    state_dir, seed_dir, reports_dir = _make_env(tmp_path)
    with pytest.raises(SystemExit):
        backtest.main(
            [
                "--from",
                "not-a-date",
                "--to",
                "2026-04-25",
                "--state-dir",
                str(state_dir),
                "--seed-dir",
                str(seed_dir),
                "--reports-dir",
                str(reports_dir),
            ]
        )
    err = capsys.readouterr().err
    assert "YYYY-MM-DD" in err or "invalid ISO date" in err


def test_does_not_call_alpaca_or_paper_executor():
    """VAL-M5-036: backtest does not import paper_executor / alpaca_client."""

    src = Path(backtest.__file__).read_text(encoding="utf-8")
    assert "alpaca" not in src.lower()
    assert "paper_executor" not in src
