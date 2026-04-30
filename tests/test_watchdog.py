"""Tests for :mod:`biotech_sniper.watchdog`.

Covers the f-m4-04 contract surface (VAL-M4-039 .. VAL-M4-042 plus
VAL-M4-043's "doesn't spawn the heavy pipeline" invariant by direct
import inspection):

* :func:`check_health` returns ``[]`` on a healthy audit_latest.json.
* :func:`check_health` flags ``daily_run_stale`` when
  ``last_daily_run`` is older than 26h or missing.
* :func:`check_health` flags ``intraday_run_stale`` only during
  market hours; outside market hours stale/missing intraday is silent.
* :func:`check_health` flags ``audit_stale`` when the file's mtime is
  older than 26h, the file is missing, or the JSON is unparseable.
* :func:`main` exits 0 on healthy state with a single ``watchdog_ok``
  INFO line and exits 1 on degraded state with WARN/ERROR lines.
* The watchdog module does not import the heavy pipeline modules.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from biotech_sniper import logging_setup, watchdog


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_logging(tmp_path, monkeypatch):
    """Reset structured logging into a tmp file per test."""
    monkeypatch.delenv("ALPHA_SNIPER_LOG_PATH", raising=False)
    monkeypatch.delenv("ALPHA_SNIPER_LOG_DIR", raising=False)
    log_path = tmp_path / "watchdog.log"
    monkeypatch.setenv("ALPHA_SNIPER_LOG_PATH", str(log_path))

    importlib.reload(logging_setup)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    yield log_path

    for handler in list(root.handlers):
        root.removeHandler(handler)


def _write_audit(
    path: Path,
    *,
    last_daily_run: str | None,
    last_intraday_run: str | None = None,
    extra: dict | None = None,
    mtime: datetime | None = None,
) -> Path:
    """Write a synthetic audit_latest.json and pin its mtime.

    ``mtime`` defaults to "fresh" relative to whatever ``now`` the
    caller pins on :func:`watchdog.check_health`; when None the file
    keeps its current real-clock mtime which would make every test
    appear audit-stale because the pinned ``NOW_*`` constants live in
    the future relative to the wall clock.
    """
    payload: dict = {
        "as_of_date": "2026-04-27",
        "last_daily_run": last_daily_run,
        "last_intraday_run": last_intraday_run,
        "sources": {},
    }
    if extra:
        payload.update(extra)
    path.write_text(json.dumps(payload), encoding="utf-8")
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(path, (ts, ts))
    return path


def _read_log_lines(log_path: Path) -> list[dict]:
    if not log_path.is_file():
        return []
    text = log_path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines()]


def _iso(now: datetime) -> str:
    return now.isoformat().replace("+00:00", "Z")


# Pinned timestamps used across tests. Wednesday 2026-04-29 16:00 UTC
# sits inside the market band (13:30-21:00 UTC). 06:00 UTC is outside.
NOW_MARKET = datetime(2026, 4, 29, 16, 0, 0, tzinfo=timezone.utc)
NOW_OFFHOURS = datetime(2026, 4, 29, 6, 0, 0, tzinfo=timezone.utc)
NOW_WEEKEND = datetime(2026, 5, 2, 16, 0, 0, tzinfo=timezone.utc)  # Saturday


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_module_exports_expected_symbols():
    for symbol in (
        "check_health",
        "main",
        "is_market_hours_utc",
        "DAILY_STALE_AFTER",
        "INTRADAY_STALE_AFTER",
        "AUDIT_STALE_AFTER",
    ):
        assert hasattr(watchdog, symbol), f"missing public symbol {symbol}"


def test_thresholds_match_spec():
    assert watchdog.DAILY_STALE_AFTER == timedelta(hours=26)
    assert watchdog.INTRADAY_STALE_AFTER == timedelta(minutes=70)
    assert watchdog.AUDIT_STALE_AFTER == timedelta(hours=26)


def test_module_does_not_import_heavy_pipeline():
    """Per VAL-M4-043 the watchdog must not pull in the heavy modules.

    Run in a fresh subprocess so we measure ``watchdog``'s actual
    import graph rather than the union of every module already loaded
    by sibling tests in this pytest worker.
    """
    import subprocess

    forbidden = [
        "biotech_sniper.master_unified_run",
        "biotech_sniper.intraday_scanner",
        "biotech_sniper.paper_executor",
        "biotech_sniper.bulk_universe_scanner",
        "biotech_sniper.rotation_engine",
    ]
    code = (
        "import sys, importlib, json\n"
        "importlib.import_module('biotech_sniper.watchdog')\n"
        f"forbidden = {forbidden!r}\n"
        "leaked = [m for m in forbidden if m in sys.modules]\n"
        "print(json.dumps(leaked))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    leaked = json.loads(result.stdout.strip().splitlines()[-1])
    assert not leaked, f"watchdog leaked heavy-pipeline imports: {leaked}"


# ---------------------------------------------------------------------------
# is_market_hours_utc
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "moment, expected",
    [
        (datetime(2026, 4, 29, 13, 30, tzinfo=timezone.utc), True),  # open boundary
        (datetime(2026, 4, 29, 16, 0, tzinfo=timezone.utc), True),
        (datetime(2026, 4, 29, 21, 0, tzinfo=timezone.utc), True),  # close boundary
        (datetime(2026, 4, 29, 13, 29, tzinfo=timezone.utc), False),
        (datetime(2026, 4, 29, 21, 1, tzinfo=timezone.utc), False),
        (datetime(2026, 4, 29, 6, 0, tzinfo=timezone.utc), False),
        (datetime(2026, 5, 2, 16, 0, tzinfo=timezone.utc), False),  # Saturday
        (datetime(2026, 5, 3, 16, 0, tzinfo=timezone.utc), False),  # Sunday
    ],
)
def test_is_market_hours_utc(moment, expected):
    assert watchdog.is_market_hours_utc(moment) is expected


def test_is_market_hours_utc_naive_datetime_is_treated_as_utc():
    naive = datetime(2026, 4, 29, 16, 0)
    assert watchdog.is_market_hours_utc(naive) is True


# ---------------------------------------------------------------------------
# check_health — healthy
# ---------------------------------------------------------------------------


def test_check_health_returns_empty_when_everything_fresh(tmp_path):
    audit = _write_audit(
        tmp_path / "audit_latest.json",
        last_daily_run=_iso(NOW_MARKET - timedelta(hours=2)),
        last_intraday_run=_iso(NOW_MARKET - timedelta(minutes=10)),
        mtime=NOW_MARKET - timedelta(minutes=1),
    )
    assert watchdog.check_health(audit, now=NOW_MARKET) == []


def test_check_health_silent_outside_market_hours_when_intraday_missing(tmp_path):
    audit = _write_audit(
        tmp_path / "audit_latest.json",
        last_daily_run=_iso(NOW_OFFHOURS - timedelta(hours=2)),
        last_intraday_run=None,
        mtime=NOW_OFFHOURS - timedelta(minutes=1),
    )
    assert watchdog.check_health(audit, now=NOW_OFFHOURS) == []


def test_check_health_silent_on_weekends_when_intraday_missing(tmp_path):
    audit = _write_audit(
        tmp_path / "audit_latest.json",
        last_daily_run=_iso(NOW_WEEKEND - timedelta(hours=2)),
        last_intraday_run=None,
        mtime=NOW_WEEKEND - timedelta(minutes=1),
    )
    assert watchdog.check_health(audit, now=NOW_WEEKEND) == []


# ---------------------------------------------------------------------------
# check_health — daily_run_stale
# ---------------------------------------------------------------------------


def test_daily_run_stale_when_older_than_26h(tmp_path):
    audit = _write_audit(
        tmp_path / "audit_latest.json",
        last_daily_run=_iso(NOW_OFFHOURS - timedelta(hours=27)),
        mtime=NOW_OFFHOURS - timedelta(minutes=1),
    )
    findings = watchdog.check_health(audit, now=NOW_OFFHOURS)
    events = [f["event"] for f in findings]
    assert "daily_run_stale" in events
    finding = next(f for f in findings if f["event"] == "daily_run_stale")
    assert finding["level"] in {"WARNING", "WARN", "ERROR"}
    assert finding["age_seconds"] >= 26 * 3600


def test_daily_run_stale_when_missing(tmp_path):
    audit = _write_audit(
        tmp_path / "audit_latest.json",
        last_daily_run=None,
        mtime=NOW_OFFHOURS - timedelta(minutes=1),
    )
    findings = watchdog.check_health(audit, now=NOW_OFFHOURS)
    events = [f["event"] for f in findings]
    assert "daily_run_stale" in events


def test_daily_run_fresh_at_25h59m_is_healthy(tmp_path):
    audit = _write_audit(
        tmp_path / "audit_latest.json",
        last_daily_run=_iso(NOW_OFFHOURS - timedelta(hours=25, minutes=59)),
        mtime=NOW_OFFHOURS - timedelta(minutes=1),
    )
    events = [f["event"] for f in watchdog.check_health(audit, now=NOW_OFFHOURS)]
    assert "daily_run_stale" not in events


# ---------------------------------------------------------------------------
# check_health — intraday_run_stale
# ---------------------------------------------------------------------------


def test_intraday_run_stale_during_market_hours(tmp_path):
    audit = _write_audit(
        tmp_path / "audit_latest.json",
        last_daily_run=_iso(NOW_MARKET - timedelta(hours=2)),
        last_intraday_run=_iso(NOW_MARKET - timedelta(minutes=80)),
        mtime=NOW_MARKET - timedelta(minutes=1),
    )
    findings = watchdog.check_health(audit, now=NOW_MARKET)
    events = [f["event"] for f in findings]
    assert "intraday_run_stale" in events
    finding = next(f for f in findings if f["event"] == "intraday_run_stale")
    assert finding["age_seconds"] >= 70 * 60


def test_intraday_run_stale_silent_outside_market_hours(tmp_path):
    audit = _write_audit(
        tmp_path / "audit_latest.json",
        last_daily_run=_iso(NOW_OFFHOURS - timedelta(hours=2)),
        last_intraday_run=_iso(NOW_OFFHOURS - timedelta(hours=10)),
        mtime=NOW_OFFHOURS - timedelta(minutes=1),
    )
    events = [f["event"] for f in watchdog.check_health(audit, now=NOW_OFFHOURS)]
    assert "intraday_run_stale" not in events


def test_intraday_missing_during_market_hours_is_stale(tmp_path):
    audit = _write_audit(
        tmp_path / "audit_latest.json",
        last_daily_run=_iso(NOW_MARKET - timedelta(hours=2)),
        last_intraday_run=None,
        mtime=NOW_MARKET - timedelta(minutes=1),
    )
    events = [f["event"] for f in watchdog.check_health(audit, now=NOW_MARKET)]
    assert "intraday_run_stale" in events


def test_intraday_fresh_within_window_is_healthy(tmp_path):
    audit = _write_audit(
        tmp_path / "audit_latest.json",
        last_daily_run=_iso(NOW_MARKET - timedelta(hours=2)),
        last_intraday_run=_iso(NOW_MARKET - timedelta(minutes=60)),
        mtime=NOW_MARKET - timedelta(minutes=1),
    )
    events = [f["event"] for f in watchdog.check_health(audit, now=NOW_MARKET)]
    assert "intraday_run_stale" not in events


# ---------------------------------------------------------------------------
# check_health — audit_stale
# ---------------------------------------------------------------------------


def test_audit_stale_when_file_mtime_older_than_26h(tmp_path):
    audit = _write_audit(
        tmp_path / "audit_latest.json",
        last_daily_run=_iso(NOW_OFFHOURS - timedelta(hours=2)),
    )
    # Backdate file mtime to 27 hours ago.
    old = (NOW_OFFHOURS - timedelta(hours=27)).timestamp()
    os.utime(audit, (old, old))
    findings = watchdog.check_health(audit, now=NOW_OFFHOURS)
    events = [f["event"] for f in findings]
    assert "audit_stale" in events
    finding = next(f for f in findings if f["event"] == "audit_stale")
    assert finding["level"] in {"WARNING", "WARN", "ERROR"}


def test_audit_stale_when_file_missing(tmp_path):
    findings = watchdog.check_health(
        tmp_path / "audit_latest.json", now=NOW_OFFHOURS
    )
    events = [f["event"] for f in findings]
    assert events == ["audit_stale"]
    assert findings[0]["level"] == "ERROR"
    assert findings[0]["reason"] == "audit_missing"


def test_audit_stale_when_file_unreadable_json(tmp_path):
    audit = tmp_path / "audit_latest.json"
    audit.write_text("not json {{", encoding="utf-8")
    fresh = (NOW_OFFHOURS - timedelta(minutes=1)).timestamp()
    os.utime(audit, (fresh, fresh))
    findings = watchdog.check_health(audit, now=NOW_OFFHOURS)
    events = [f["event"] for f in findings]
    assert "audit_stale" in events
    finding = next(f for f in findings if f["event"] == "audit_stale")
    assert finding["level"] == "ERROR"
    assert "unreadable" in finding["reason"]


# ---------------------------------------------------------------------------
# main() — exit codes & log output
# ---------------------------------------------------------------------------


def test_main_returns_zero_on_healthy_state(tmp_path, fresh_logging):
    audit = _write_audit(
        tmp_path / "audit_latest.json",
        last_daily_run=_iso(NOW_MARKET - timedelta(hours=2)),
        last_intraday_run=_iso(NOW_MARKET - timedelta(minutes=10)),
        mtime=NOW_MARKET - timedelta(minutes=1),
    )
    hb = tmp_path / "news_daemon_heartbeat.json"
    hb.write_text("{}", encoding="utf-8")
    fresh = (NOW_MARKET - timedelta(seconds=30)).timestamp()
    os.utime(hb, (fresh, fresh))
    rc = watchdog.main(
        argv=[], audit_path=audit, heartbeat_path=hb,
        now=NOW_MARKET, is_active_fn=lambda: True,
    )
    assert rc == 0

    for handler in logging.getLogger().handlers:
        handler.flush()
    entries = _read_log_lines(fresh_logging)
    # VAL-M4-042: only INFO-level lines on a healthy run.
    levels = {entry["level"] for entry in entries}
    assert levels <= {"INFO"}, entries
    assert any(entry["event"] == "watchdog_ok" for entry in entries)


def test_main_returns_one_on_stale_daily(tmp_path, fresh_logging):
    audit = _write_audit(
        tmp_path / "audit_latest.json",
        last_daily_run=_iso(NOW_OFFHOURS - timedelta(hours=27)),
        mtime=NOW_OFFHOURS - timedelta(minutes=1),
    )
    rc = watchdog.main(argv=[], audit_path=audit, now=NOW_OFFHOURS)
    assert rc == 1

    for handler in logging.getLogger().handlers:
        handler.flush()
    entries = _read_log_lines(fresh_logging)
    events = {entry["event"] for entry in entries}
    assert "daily_run_stale" in events
    # VAL-M4-039: WARN/WARNING/ERROR level for the stale finding.
    stale = next(e for e in entries if e["event"] == "daily_run_stale")
    assert stale["level"] in {"WARNING", "WARN", "ERROR"}


def test_main_returns_one_on_stale_intraday_during_market_hours(
    tmp_path, fresh_logging
):
    audit = _write_audit(
        tmp_path / "audit_latest.json",
        last_daily_run=_iso(NOW_MARKET - timedelta(hours=2)),
        last_intraday_run=_iso(NOW_MARKET - timedelta(minutes=90)),
        mtime=NOW_MARKET - timedelta(minutes=1),
    )
    rc = watchdog.main(argv=[], audit_path=audit, now=NOW_MARKET)
    assert rc == 1

    for handler in logging.getLogger().handlers:
        handler.flush()
    entries = _read_log_lines(fresh_logging)
    events = {entry["event"] for entry in entries}
    assert "intraday_run_stale" in events
    stale = next(e for e in entries if e["event"] == "intraday_run_stale")
    assert stale["level"] in {"WARNING", "WARN", "ERROR"}


def test_main_returns_one_on_stale_audit_file(tmp_path, fresh_logging):
    audit = _write_audit(
        tmp_path / "audit_latest.json",
        last_daily_run=_iso(NOW_OFFHOURS - timedelta(hours=2)),
    )
    old = (NOW_OFFHOURS - timedelta(hours=27)).timestamp()
    os.utime(audit, (old, old))
    rc = watchdog.main(argv=[], audit_path=audit, now=NOW_OFFHOURS)
    assert rc == 1

    for handler in logging.getLogger().handlers:
        handler.flush()
    entries = _read_log_lines(fresh_logging)
    events = {entry["event"] for entry in entries}
    assert "audit_stale" in events


def test_main_returns_one_on_missing_audit_file(tmp_path, fresh_logging):
    rc = watchdog.main(
        argv=[],
        audit_path=tmp_path / "audit_latest.json",
        now=NOW_OFFHOURS,
    )
    assert rc == 1

    for handler in logging.getLogger().handlers:
        handler.flush()
    entries = _read_log_lines(fresh_logging)
    events = {entry["event"] for entry in entries}
    assert "audit_stale" in events
    audit_finding = next(e for e in entries if e["event"] == "audit_stale")
    assert audit_finding["level"] == "ERROR"


def test_main_log_line_carries_required_json_keys(tmp_path, fresh_logging):
    """Every emitted line is a JSON object with ts/level/event/module."""
    audit = _write_audit(
        tmp_path / "audit_latest.json",
        last_daily_run=_iso(NOW_OFFHOURS - timedelta(hours=27)),
        mtime=NOW_OFFHOURS - timedelta(minutes=1),
    )
    watchdog.main(argv=[], audit_path=audit, now=NOW_OFFHOURS)

    for handler in logging.getLogger().handlers:
        handler.flush()
    entries = _read_log_lines(fresh_logging)
    assert entries, "expected at least one structured log line"
    for entry in entries:
        for required in ("ts", "level", "event", "module"):
            assert required in entry, entry


def test_main_accepts_audit_path_cli_flag(tmp_path, fresh_logging):
    """CLI passthrough: ``--audit-path`` overrides the default."""
    audit = _write_audit(
        tmp_path / "audit_latest.json",
        last_daily_run=_iso(NOW_MARKET - timedelta(hours=2)),
        last_intraday_run=_iso(NOW_MARKET - timedelta(minutes=10)),
        mtime=NOW_MARKET - timedelta(minutes=1),
    )
    hb = tmp_path / "news_daemon_heartbeat.json"
    hb.write_text("{}", encoding="utf-8")
    fresh = (NOW_MARKET - timedelta(seconds=30)).timestamp()
    os.utime(hb, (fresh, fresh))
    rc = watchdog.main(
        argv=["--audit-path", str(audit)], heartbeat_path=hb,
        now=NOW_MARKET, is_active_fn=lambda: True,
    )
    assert rc == 0


def test_main_returns_one_when_multiple_conditions_degraded(
    tmp_path, fresh_logging
):
    audit = _write_audit(
        tmp_path / "audit_latest.json",
        last_daily_run=_iso(NOW_MARKET - timedelta(hours=30)),
        last_intraday_run=_iso(NOW_MARKET - timedelta(minutes=120)),
        mtime=NOW_MARKET - timedelta(minutes=1),
    )
    rc = watchdog.main(argv=[], audit_path=audit, now=NOW_MARKET)
    assert rc == 1

    for handler in logging.getLogger().handlers:
        handler.flush()
    entries = _read_log_lines(fresh_logging)
    events = {entry["event"] for entry in entries}
    assert {"daily_run_stale", "intraday_run_stale"} <= events


# ---------------------------------------------------------------------------
# Reading-B M4: news daemon health (VAL-M4-021..024)
# ---------------------------------------------------------------------------


def test_check_news_daemon_health_healthy_returns_empty(tmp_path):
    hb = tmp_path / "news_daemon_heartbeat.json"
    hb.write_text("{}", encoding="utf-8")
    fresh = (NOW_MARKET - timedelta(seconds=30)).timestamp()
    os.utime(hb, (fresh, fresh))
    findings = watchdog.check_news_daemon_health(
        hb, now=NOW_MARKET, is_active_fn=lambda: True,
    )
    assert findings == []


def test_check_news_daemon_health_inactive_emits_news_service_inactive(
    tmp_path,
):
    hb = tmp_path / "news_daemon_heartbeat.json"
    hb.write_text("{}", encoding="utf-8")
    fresh = (NOW_MARKET - timedelta(seconds=30)).timestamp()
    os.utime(hb, (fresh, fresh))
    findings = watchdog.check_news_daemon_health(
        hb, now=NOW_MARKET, is_active_fn=lambda: False,
    )
    events = [f["event"] for f in findings]
    assert "news_service_inactive" in events
    inactive = next(f for f in findings if f["event"] == "news_service_inactive")
    assert inactive["level"] == "ERROR"
    # VAL-CROSS-040: 'news_daemon_inactive' substring must appear in the
    # log line (validator greps the journalctl output).
    assert inactive.get("legacy_event") == "news_daemon_inactive"


def test_check_news_daemon_health_stale_heartbeat_emits_warning(tmp_path):
    hb = tmp_path / "news_daemon_heartbeat.json"
    hb.write_text("{}", encoding="utf-8")
    old = (NOW_MARKET - timedelta(minutes=10)).timestamp()
    os.utime(hb, (old, old))
    findings = watchdog.check_news_daemon_health(
        hb, now=NOW_MARKET, is_active_fn=lambda: True,
    )
    stale = next(f for f in findings if f["event"] == "news_daemon_heartbeat_stale")
    assert stale["level"] in {"WARNING", "WARN", "ERROR"}
    assert stale["age_seconds"] >= 300


def test_check_news_daemon_health_missing_heartbeat_flags_stale(tmp_path):
    findings = watchdog.check_news_daemon_health(
        tmp_path / "no_such_hb.json",
        now=NOW_MARKET,
        is_active_fn=lambda: True,
    )
    assert any(
        f["event"] == "news_daemon_heartbeat_stale"
        and f.get("reason") == "heartbeat_missing"
        for f in findings
    )


def test_main_emits_news_service_inactive_when_daemon_stopped(
    tmp_path, fresh_logging
):
    audit = _write_audit(
        tmp_path / "audit_latest.json",
        last_daily_run=_iso(NOW_MARKET - timedelta(hours=2)),
        last_intraday_run=_iso(NOW_MARKET - timedelta(minutes=10)),
        mtime=NOW_MARKET - timedelta(minutes=1),
    )
    hb = tmp_path / "news_daemon_heartbeat.json"
    hb.write_text("{}", encoding="utf-8")
    fresh = (NOW_MARKET - timedelta(seconds=30)).timestamp()
    os.utime(hb, (fresh, fresh))
    rc = watchdog.main(
        argv=[], audit_path=audit, heartbeat_path=hb,
        now=NOW_MARKET, is_active_fn=lambda: False,
    )
    assert rc == 1
    for handler in logging.getLogger().handlers:
        handler.flush()
    entries = _read_log_lines(fresh_logging)
    events = {e["event"] for e in entries}
    assert "news_service_inactive" in events


def test_main_emits_heartbeat_stale_when_old(tmp_path, fresh_logging):
    audit = _write_audit(
        tmp_path / "audit_latest.json",
        last_daily_run=_iso(NOW_MARKET - timedelta(hours=2)),
        last_intraday_run=_iso(NOW_MARKET - timedelta(minutes=10)),
        mtime=NOW_MARKET - timedelta(minutes=1),
    )
    hb = tmp_path / "news_daemon_heartbeat.json"
    hb.write_text("{}", encoding="utf-8")
    old = (NOW_MARKET - timedelta(minutes=10)).timestamp()
    os.utime(hb, (old, old))
    rc = watchdog.main(
        argv=[], audit_path=audit, heartbeat_path=hb,
        now=NOW_MARKET, is_active_fn=lambda: True,
    )
    assert rc == 1
    for handler in logging.getLogger().handlers:
        handler.flush()
    entries = _read_log_lines(fresh_logging)
    events = {e["event"] for e in entries}
    assert "news_daemon_heartbeat_stale" in events
