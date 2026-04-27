"""Lock-in tests for the M4 systemd unit files in deploy/systemd/.

These tests guard the contract for f-m4-01 / VAL-M4-001..VAL-M4-014:
- All six unit files exist (3 .service + 3 .timer)
- Each .service declares Type=oneshot, User=root, the canonical
  WorkingDirectory and EnvironmentFile, an ExecStart that uses the
  project venv `python -m biotech_sniper.<entrypoint>`.
- Each .timer carries an OnCalendar= directive whose minute field is
  staggered (not :00, :05, :07, :15, :30, :45 — the minutes already used
  by hl-edge cron / tier_refresh.py / HL grok service neighbours on the
  same VPS).
- Daily timer carries Persistent=true.
- Watchdog timer fires every 15 min.

`systemd-analyze verify` itself runs only on Linux (the VPS); these
tests provide a Python-only equivalent that runs in CI / local.
"""
from __future__ import annotations

import configparser
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"

SERVICE_NAMES = [
    "alpha-sniper.service",
    "alpha-sniper-intraday.service",
    "alpha-sniper-watchdog.service",
]
TIMER_NAMES = [
    "alpha-sniper.timer",
    "alpha-sniper-intraday.timer",
    "alpha-sniper-watchdog.timer",
]

EXPECTED_ENTRYPOINTS = {
    "alpha-sniper.service": "biotech_sniper.master_unified_run",
    "alpha-sniper-intraday.service": "biotech_sniper.intraday_scanner",
    "alpha-sniper-watchdog.service": "biotech_sniper.watchdog",
}

# Minutes already in use hourly by hl-edge cron / tier_refresh / common
# minute-0 / minute-30 schedules per AGENTS.md "Workload caps" section.
FORBIDDEN_MINUTES = {0, 5, 7, 15, 30, 45}


def _read_unit(path: Path) -> configparser.ConfigParser:
    """Parse a systemd unit file as INI. Allow duplicate keys."""
    cfg = configparser.ConfigParser(strict=False, interpolation=None)
    # systemd permits keys without a trailing space; ConfigParser is fine.
    cfg.read(path, encoding="utf-8")
    return cfg


# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", SERVICE_NAMES + TIMER_NAMES)
def test_unit_file_exists(name: str) -> None:
    path = SYSTEMD_DIR / name
    assert path.is_file(), f"missing unit file: {path}"


# ---------------------------------------------------------------------------
# Service unit contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", SERVICE_NAMES)
def test_service_unit_contract(name: str) -> None:
    cfg = _read_unit(SYSTEMD_DIR / name)
    assert cfg.has_section("Service"), f"{name}: missing [Service] section"
    svc = cfg["Service"]
    assert svc.get("Type") == "oneshot", f"{name}: Type must be oneshot"
    assert svc.get("User") == "root", f"{name}: User must be root"
    assert (
        svc.get("WorkingDirectory") == "/root/alpha_sniper/repo"
    ), f"{name}: WorkingDirectory must be /root/alpha_sniper/repo"
    assert (
        svc.get("EnvironmentFile") == "/root/alpha_sniper/.env"
    ), f"{name}: EnvironmentFile must be /root/alpha_sniper/.env"

    exec_start = svc.get("ExecStart") or ""
    assert exec_start.startswith(
        "/root/alpha_sniper/repo/.venv/bin/python "
    ), f"{name}: ExecStart must use project venv python: {exec_start!r}"
    expected_module = EXPECTED_ENTRYPOINTS[name]
    assert (
        f"-m {expected_module}" in exec_start
    ), f"{name}: ExecStart must invoke `-m {expected_module}`, got: {exec_start!r}"

    # journal/journal so journalctl picks the run output up.
    assert svc.get("StandardOutput", "").startswith(
        ("journal", "append:")
    ), f"{name}: StandardOutput must route to journal or append: file"
    assert svc.get("StandardError", "").startswith(
        ("journal", "append:")
    ), f"{name}: StandardError must route to journal or append: file"


# ---------------------------------------------------------------------------
# Timer schedule contract + stagger
# ---------------------------------------------------------------------------


def _on_calendar(name: str) -> str:
    cfg = _read_unit(SYSTEMD_DIR / name)
    assert cfg.has_section("Timer"), f"{name}: missing [Timer] section"
    on_cal = cfg["Timer"].get("OnCalendar") or ""
    assert on_cal, f"{name}: missing OnCalendar="
    return on_cal


def _extract_minutes(expr: str) -> set[int]:
    """Extract all explicit minute values referenced by an OnCalendar expr.

    Handles single minute (`14:13:00`), comma list (`14:13,22:00`), and
    step syntax (`*:8/15` -> 8, 23, 38, 53). Returns the set of minute
    integers the timer will fire on (within any given hour).
    """
    # Find HH:MM or *:MM patterns.
    minutes: set[int] = set()
    # match the minute field after the first colon following hours.
    # OnCalendar grammar examples:
    #   *-*-* 14:13:00
    #   Mon..Fri *-*-* 13..21:22:00
    #   *-*-* *:8/15
    # Strip trailing :SS if present
    parts = expr.strip().split()
    # Pick the first token that looks like a time spec (contains ':').
    # Trailing timezone tokens (e.g. 'America/Los_Angeles') contain '/'
    # but no ':', so they are skipped.
    spec = next((p for p in parts if ":" in p), "")
    if not spec:
        return minutes
    hour_part, _, rest = spec.partition(":")
    minute_part = rest.split(":")[0]  # drop seconds
    # Allow "8/15", "13", "13,22", "8/15,30"
    for chunk in minute_part.split(","):
        if "/" in chunk:
            start_s, _, step_s = chunk.partition("/")
            start = int(start_s) if start_s != "*" else 0
            step = int(step_s)
            m = start
            while m < 60:
                minutes.add(m)
                m += step
        elif chunk == "*":
            # Every minute - not used in our timers; treat as forbidden.
            minutes.update(range(60))
        else:
            minutes.add(int(chunk))
    return minutes


def test_daily_timer_schedule() -> None:
    on_cal = _on_calendar("alpha-sniper.timer")
    # Must reference 6 AM PT — either explicitly via timezone or via the
    # equivalent UTC literal (13:NN PDT or 14:NN PST).
    matches_pt = bool(
        re.search(r"06:\d{2}:00\s+America/Los_Angeles", on_cal)
    )
    matches_utc_pst = bool(re.search(r"\b14:\d{2}(?::00)?\b", on_cal))
    matches_utc_pdt = bool(re.search(r"\b13:\d{2}(?::00)?\b", on_cal))
    assert (
        matches_pt or matches_utc_pst or matches_utc_pdt
    ), f"daily OnCalendar must encode 6 AM PT (got {on_cal!r})"

    minutes = _extract_minutes(on_cal)
    assert minutes, f"daily OnCalendar yielded no minute (got {on_cal!r})"
    assert minutes.isdisjoint(
        FORBIDDEN_MINUTES
    ), f"daily timer minute(s) {minutes} collide with forbidden {FORBIDDEN_MINUTES}"

    # Persistent=true required for missed-run catchup.
    cfg = _read_unit(SYSTEMD_DIR / "alpha-sniper.timer")
    assert (
        cfg["Timer"].get("Persistent", "").strip().lower() == "true"
    ), "alpha-sniper.timer must set Persistent=true"


def test_intraday_timer_schedule() -> None:
    on_cal = _on_calendar("alpha-sniper-intraday.timer")
    # Must restrict to weekdays Mon..Fri.
    assert re.search(r"\bMon\.\.Fri\b", on_cal), (
        f"intraday OnCalendar must restrict to Mon..Fri (got {on_cal!r})"
    )
    # Must cover US market hours window 13..21 UTC.
    assert re.search(r"\b13\.\.21:\d", on_cal), (
        f"intraday OnCalendar must span hours 13..21 UTC (got {on_cal!r})"
    )

    minutes = _extract_minutes(on_cal)
    assert minutes, f"intraday OnCalendar yielded no minute (got {on_cal!r})"
    assert minutes.isdisjoint(FORBIDDEN_MINUTES), (
        f"intraday timer minute(s) {minutes} collide with forbidden "
        f"{FORBIDDEN_MINUTES}"
    )


def test_watchdog_timer_schedule() -> None:
    on_cal = _on_calendar("alpha-sniper-watchdog.timer")
    minutes = _extract_minutes(on_cal)
    # Every 15 min => exactly 4 elapses per hour.
    assert (
        len(minutes) == 4
    ), f"watchdog must fire 4 times per hour, got minutes={minutes}"
    # Spacing: sorted differences are all 15.
    sorted_m = sorted(minutes)
    diffs = [b - a for a, b in zip(sorted_m, sorted_m[1:])]
    assert diffs == [15, 15, 15], (
        f"watchdog minutes must be 15min apart, got {sorted_m}"
    )
    assert minutes.isdisjoint(FORBIDDEN_MINUTES), (
        f"watchdog timer minute(s) {minutes} collide with forbidden "
        f"{FORBIDDEN_MINUTES}"
    )


# ---------------------------------------------------------------------------
# Cross-timer stagger: no two alpha-sniper timers share a minute
# ---------------------------------------------------------------------------


def test_no_two_alpha_sniper_timers_share_a_minute() -> None:
    daily_minutes = _extract_minutes(_on_calendar("alpha-sniper.timer"))
    intraday_minutes = _extract_minutes(
        _on_calendar("alpha-sniper-intraday.timer")
    )
    watchdog_minutes = _extract_minutes(
        _on_calendar("alpha-sniper-watchdog.timer")
    )

    # Daily/intraday are single-minute schedules; check they don't collide.
    assert daily_minutes.isdisjoint(intraday_minutes), (
        f"daily {daily_minutes} and intraday {intraday_minutes} share a minute"
    )

    # Watchdog runs every 15 min; daily and intraday must not pick a minute
    # that lands on a watchdog firing minute (would cause same-minute
    # collision once per day / once per hour).
    assert daily_minutes.isdisjoint(watchdog_minutes), (
        f"daily {daily_minutes} collides with watchdog {watchdog_minutes}"
    )
    assert intraday_minutes.isdisjoint(watchdog_minutes), (
        f"intraday {intraday_minutes} collides with watchdog {watchdog_minutes}"
    )


# ---------------------------------------------------------------------------
# Timer Install section (so `systemctl enable` works)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", TIMER_NAMES)
def test_timer_has_install_section(name: str) -> None:
    cfg = _read_unit(SYSTEMD_DIR / name)
    assert cfg.has_section("Install"), f"{name}: missing [Install] section"
    wanted = cfg["Install"].get("WantedBy", "")
    assert "timers.target" in wanted, (
        f"{name}: [Install] WantedBy must include timers.target (got {wanted!r})"
    )


@pytest.mark.parametrize("name", TIMER_NAMES)
def test_timer_unit_pairs_to_correct_service(name: str) -> None:
    cfg = _read_unit(SYSTEMD_DIR / name)
    timer_section = cfg["Timer"]
    explicit_unit = timer_section.get("Unit")
    expected_service = name.replace(".timer", ".service")
    if explicit_unit is not None:
        assert explicit_unit == expected_service, (
            f"{name}: [Timer] Unit must point to {expected_service}, "
            f"got {explicit_unit!r}"
        )
    # If Unit is omitted, systemd defaults to <basename>.service which
    # equals expected_service — still correct.
