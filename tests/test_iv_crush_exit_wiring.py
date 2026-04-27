"""Tests for the f-m3-15 IV-crush exit production wiring.

Covers two surgical fixes layered on top of f-m3-05:

1. **DB SOURCE OF TRUTH** — :func:`run_on_open` now defaults to
   :func:`load_active_plays_from_db` (queries the SQLite ``plays``
   table), no longer the legacy ``state/active_plays.json`` loader.
   The JSON-backed :func:`load_active_plays` remains importable as
   an explicit injection for tests / email-alert helpers.

2. **PRODUCTION WIRING** — a new top-level CLI
   ``python -m biotech_sniper.iv_crush_exit_rules --run-on-open
   --date YYYY-MM-DD [--dry-run]`` is the canonical scheduler
   entrypoint, and :func:`run_intraday_iv_crush_exit_job` is invoked
   by :func:`intraday_scanner.run_intraday_scan` as JOB 5.

Test cases mirror the feature spec's three required tests:
* (a) the CLI exit code is 0 on a fresh DB with no active plays
* (b) the JSON summary keys are stable
* (c) ``intraday_scanner.run_intraday_scan()`` invokes the new
  entry-point exactly once when LIVE-mode and paper-execute flags
  allow.
"""

from __future__ import annotations

import json
import subprocess
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper import db as _db
from biotech_sniper import iv_crush_exit_rules as ic
from biotech_sniper.iv_crush_exit_rules import (
    _cli_main,
    _CLI_SUMMARY_KEYS,
    load_active_plays_from_db,
    run_intraday_iv_crush_exit_job,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db_path(tmp_path: Path) -> Path:
    """Create a freshly-migrated SQLite db at ``tmp_path/alpha.db``."""
    db_path = tmp_path / "alpha.db"
    conn = _db.connect(db_path)
    try:
        _db.run_migrations(conn)
    finally:
        conn.close()
    return db_path


def _insert_active_play(
    db_path: Path,
    *,
    source_key: str,
    ticker: str,
    catalyst_date: str | None,
    payload: dict[str, Any] | None = None,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO plays (
                source_key, ticker, status, catalyst_date, option_type,
                option_strike, option_expiry, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_key,
                ticker,
                "active",
                catalyst_date,
                "call",
                125.0,
                "2025-06-20",
                json.dumps(payload) if payload is not None else None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# load_active_plays_from_db
# ---------------------------------------------------------------------------


def test_load_active_plays_from_db_returns_empty_when_db_missing(
    tmp_path: Path,
):
    """A non-existent db file returns ``[]`` (no crash)."""
    out = load_active_plays_from_db(db_path=tmp_path / "does_not_exist.db")
    assert out == []


def test_load_active_plays_from_db_filters_status_and_catalyst_date(
    fresh_db_path: Path,
):
    """Only ``status='active'`` rows with non-null catalyst_date come back."""
    _insert_active_play(
        fresh_db_path,
        source_key="active:AXSM",
        ticker="AXSM",
        catalyst_date="2026-04-27",
    )
    # Insert a row with NULL catalyst_date — must be filtered out.
    conn = sqlite3.connect(fresh_db_path)
    try:
        conn.execute(
            """
            INSERT INTO plays (source_key, ticker, status, catalyst_date)
            VALUES (?, ?, ?, ?)
            """,
            ("active:NULLDATE", "ZZZZ", "active", None),
        )
        # Insert a resolved row — must be filtered out.
        conn.execute(
            """
            INSERT INTO plays (source_key, ticker, status, catalyst_date)
            VALUES (?, ?, ?, ?)
            """,
            ("resolved:OLDIE", "OLDIE", "resolved", "2024-01-01"),
        )
        conn.commit()
    finally:
        conn.close()

    out = load_active_plays_from_db(db_path=fresh_db_path)
    tickers = sorted(p["ticker"] for p in out)
    assert tickers == ["AXSM"]


def test_load_active_plays_from_db_merges_payload_legacy_keys(
    fresh_db_path: Path,
):
    """Payload JSON fields are merged onto the row (option_symbol, contracts)."""
    _insert_active_play(
        fresh_db_path,
        source_key="active:AXSM",
        ticker="AXSM",
        catalyst_date="2026-04-27",
        payload={
            "option_symbol": "AXSM250620C00125000",
            "contracts": 4,
            "play_card_id": "AXSM-2026-04-27",
        },
    )
    out = load_active_plays_from_db(db_path=fresh_db_path)
    assert len(out) == 1
    record = out[0]
    assert record["ticker"] == "AXSM"
    assert record["option_symbol"] == "AXSM250620C00125000"
    assert record["contracts"] == 4
    assert record["play_card_id"] == "AXSM-2026-04-27"
    assert record["catalyst_date"] == "2026-04-27"


def test_load_active_plays_from_db_falls_back_play_card_id_to_source_key(
    fresh_db_path: Path,
):
    """Missing play_card_id in payload defaults to source_key."""
    _insert_active_play(
        fresh_db_path,
        source_key="active:AXSM",
        ticker="AXSM",
        catalyst_date="2026-04-27",
        payload=None,
    )
    out = load_active_plays_from_db(db_path=fresh_db_path)
    assert out[0]["play_card_id"] == "active:AXSM"


# ---------------------------------------------------------------------------
# CLI — feature spec tests (a) and (b)
# ---------------------------------------------------------------------------


def test_cli_exit_zero_on_fresh_db_with_no_active_plays(
    fresh_db_path: Path, capsys, monkeypatch
):
    """(a) CLI exits 0 when the DB has no active plays."""
    # Deliberately do NOT inject Alpaca creds — the CLI must not even
    # attempt to construct an Alpaca client when there are zero
    # candidates (otherwise it would crash with AlpacaAuthError).
    monkeypatch.delenv("ALPACA_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    rc = _cli_main(
        [
            "--run-on-open",
            "--date",
            "2026-04-27",
            "--db-path",
            str(fresh_db_path),
        ]
    )
    assert rc == 0

    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    # Stable keys — feature spec test (b).
    for key in _CLI_SUMMARY_KEYS:
        assert key in payload, f"missing key {key!r} in summary {payload!r}"
    assert payload["considered"] == 0
    assert payload["exited"] == 0
    assert payload["errors"] == 0


def test_cli_dry_run_exit_zero_on_fresh_db(
    fresh_db_path: Path, capsys, monkeypatch
):
    """``--dry-run`` short-circuits without any broker construction."""
    monkeypatch.delenv("ALPACA_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    rc = _cli_main(
        [
            "--run-on-open",
            "--date",
            "2026-04-27",
            "--dry-run",
            "--db-path",
            str(fresh_db_path),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["dry_run"] is True
    for key in _CLI_SUMMARY_KEYS:
        assert key in payload


def test_cli_summary_keys_stable_with_active_play(
    fresh_db_path: Path, capsys, monkeypatch
):
    """(b) Summary keys are stable even when candidates are present.

    Use ``--dry-run`` so we don't need real Alpaca creds; the
    candidate is counted via the synthetic floor(N/2) >= 1 estimator.
    """
    _insert_active_play(
        fresh_db_path,
        source_key="active:AXSM",
        ticker="AXSM",
        catalyst_date="2026-04-27",
        payload={
            "option_symbol": "AXSM250620C00125000",
            "contracts": 4,
            "play_card_id": "AXSM-2026-04-27",
        },
    )
    monkeypatch.delenv("ALPACA_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    rc = _cli_main(
        [
            "--run-on-open",
            "--date",
            "2026-04-27",
            "--dry-run",
            "--db-path",
            str(fresh_db_path),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    for key in _CLI_SUMMARY_KEYS:
        assert key in payload
    assert payload["date"] == "2026-04-27"
    assert payload["considered"] == 1
    assert payload["exited"] == 1  # synthetic floor(4/2)=2 >= 1
    assert payload["errors"] == 0
    assert payload["dry_run"] is True


def test_cli_subprocess_invocation_returns_zero(
    fresh_db_path: Path, monkeypatch
):
    """End-to-end: invoking the module via ``python -m`` returns 0.

    This pins the verificationSteps from the feature spec:
    ``.venv/bin/python -m biotech_sniper.iv_crush_exit_rules
    --run-on-open --date 2026-04-27 --dry-run``.
    """
    repo_root = Path(__file__).resolve().parent.parent
    env_overrides = {
        "BIOTECH_SNIPER_HOME": str(repo_root),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "biotech_sniper.iv_crush_exit_rules",
            "--run-on-open",
            "--date",
            "2026-04-27",
            "--dry-run",
            "--db-path",
            str(fresh_db_path),
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        env={**__import__("os").environ, **env_overrides},
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"CLI exited {proc.returncode}: stdout={proc.stdout!r} "
        f"stderr={proc.stderr!r}"
    )
    # Stdout's last non-empty line is the JSON summary.
    last = [ln for ln in proc.stdout.splitlines() if ln.strip()][-1]
    payload = json.loads(last)
    for key in _CLI_SUMMARY_KEYS:
        assert key in payload


def test_cli_rejects_malformed_date(capsys):
    """An invalid ``--date`` value yields exit 1 + JSON error message."""
    rc = _cli_main(["--run-on-open", "--date", "2026/04/27", "--dry-run"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert "error" in payload
    assert "invalid --date" in payload["error"].lower()


# ---------------------------------------------------------------------------
# run_intraday_iv_crush_exit_job
# ---------------------------------------------------------------------------


def test_run_intraday_iv_crush_exit_job_no_plays(
    fresh_db_path: Path,
):
    """Empty DB → considered=0, no broker construction attempted."""
    out = run_intraday_iv_crush_exit_job(
        today="2026-04-27", db_path=fresh_db_path
    )
    assert out["considered"] == 0
    assert out["exited"] == 0
    assert out["errors"] == 0
    assert out["date"] == "2026-04-27"


def test_run_intraday_iv_crush_exit_job_returns_error_on_missing_creds(
    fresh_db_path: Path, monkeypatch
):
    """A live candidate without Alpaca creds → errors >= 1, no raise."""
    _insert_active_play(
        fresh_db_path,
        source_key="active:AXSM",
        ticker="AXSM",
        catalyst_date="2026-04-27",
        payload={
            "option_symbol": "AXSM250620C00125000",
            "contracts": 4,
            "play_card_id": "AXSM-2026-04-27",
        },
    )
    monkeypatch.delenv("ALPACA_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    out = run_intraday_iv_crush_exit_job(
        today="2026-04-27", db_path=fresh_db_path
    )
    assert out["considered"] == 1
    assert out["exited"] == 0
    assert out["errors"] >= 1
    assert "error" in out
    # Stable summary keys.
    for key in _CLI_SUMMARY_KEYS:
        assert key in out


# ---------------------------------------------------------------------------
# Feature spec test (c): intraday_scanner.run_intraday_scan() invokes the
# new entry-point exactly once.
# ---------------------------------------------------------------------------


def test_intraday_scan_invokes_iv_crush_exit_exactly_once(monkeypatch):
    """(c) ``run_intraday_scan`` calls the helper exactly once per cycle."""
    from biotech_sniper import intraday_scanner

    # Track invocations of the IV-crush exit job. We patch the
    # symbol on ``iv_crush_exit_rules`` (the import site inside the
    # scanner's JOB-5 try block) so the dispatch is observable
    # regardless of how ``intraday_scanner`` chose to bind the name.
    calls: list[dict[str, Any]] = []

    def _fake_helper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"args": args, "kwargs": kwargs})
        return {
            "date": "2026-04-27",
            "considered": 0,
            "exited": 0,
            "errors": 0,
        }

    monkeypatch.setattr(
        ic, "run_intraday_iv_crush_exit_job", _fake_helper
    )

    # Stub out every IO-heavy scan helper so run_intraday_scan
    # completes quickly and deterministically. Each scan_* function
    # already returns a list, so we mock with no-op equivalents.
    monkeypatch.setattr(intraday_scanner, "scan_news_rss", lambda *_a, **_k: [])
    monkeypatch.setattr(intraday_scanner, "scan_sec_rss", lambda *_a, **_k: [])
    monkeypatch.setattr(
        intraday_scanner, "scan_ir_pages_quick", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        intraday_scanner, "scan_usaspending_intraday", lambda *_a, **_k: []
    )
    monkeypatch.setattr(intraday_scanner, "scan_fda_rss", lambda *_a, **_k: [])
    monkeypatch.setattr(
        intraday_scanner, "check_removals", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        intraday_scanner, "check_upgrades", lambda *_a, **_k: []
    )
    monkeypatch.setattr(intraday_scanner, "load_active", lambda: {"active": {}})
    monkeypatch.setattr(
        intraday_scanner,
        "load_log",
        lambda: {
            "seen_urls": [],
            "seen_award_ids": [],
            "last_scan": None,
            "alerts_sent": [],
        },
    )
    monkeypatch.setattr(intraday_scanner, "save_log", lambda _l: None)

    # Disable the rotation engine cleanly so JOB 4 short-circuits.
    from biotech_sniper import rotation_engine

    monkeypatch.setattr(
        rotation_engine,
        "evaluate_rotation",
        lambda *_a, **_k: {
            "active_count": 0,
            "capacity": 0,
            "decisions": [],
            "skips": [],
        },
    )

    # Disable the new-opportunity sniper short-circuit.
    monkeypatch.setattr(
        intraday_scanner, "_NEW_OPP_SNIPER_AVAILABLE", False, raising=False
    )

    result = intraday_scanner.run_intraday_scan()

    assert len(calls) == 1, (
        f"run_intraday_iv_crush_exit_job expected 1 call, got "
        f"{len(calls)}: {calls!r}"
    )
    # The scan returns a dict (legacy shape) — make sure the function
    # at least completed without raising.
    assert isinstance(result, dict)


def test_intraday_scanner_source_references_iv_crush_helper():
    """Lock the wiring in via a source-level grep so future drift is loud."""
    import inspect

    from biotech_sniper import intraday_scanner

    src = inspect.getsource(intraday_scanner.run_intraday_scan)
    assert "run_intraday_iv_crush_exit_job" in src, (
        "run_intraday_scan must invoke run_intraday_iv_crush_exit_job"
    )
    assert "JOB 5" in src, "JOB 5 banner missing from run_intraday_scan"


# ---------------------------------------------------------------------------
# Default loader regression: run_on_open() falls through to SQLite, not JSON
# ---------------------------------------------------------------------------


def test_run_on_open_default_loader_is_db_not_json(monkeypatch):
    """``_coerce_active_plays(None)`` calls the DB loader, not the JSON one."""
    from biotech_sniper.iv_crush_exit_rules import _coerce_active_plays

    db_calls: list[None] = []
    json_calls: list[None] = []

    def _fake_db_loader(**kwargs: Any) -> list[dict[str, Any]]:
        db_calls.append(None)
        return []

    def _fake_json_loader() -> dict[str, Any]:
        json_calls.append(None)
        return {}

    monkeypatch.setattr(ic, "load_active_plays_from_db", _fake_db_loader)
    monkeypatch.setattr(ic, "load_active_plays", _fake_json_loader)

    out = _coerce_active_plays(None)
    assert out == []
    assert len(db_calls) == 1, "DB loader must be called for None input"
    assert len(json_calls) == 0, (
        "Legacy JSON loader must NOT be called by default — only when "
        "explicitly injected"
    )
