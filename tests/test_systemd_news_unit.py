"""Lock-in tests for the alpha-sniper-news.service unit file (Reading B M2).

Guards the contract for f-m2-02-systemd-unit-file and the validation
assertions VAL-M2-004 / VAL-M2-005 / VAL-M2-006 / VAL-M2-007 / VAL-M2-045 /
VAL-M2-046 / VAL-M4-002 / VAL-M4-051 / VAL-M4-052.

The Reading-B mission introduces ONE new long-lived service:
``alpha-sniper-news.service`` (Type=simple). This file is the source of
truth checked into the repo and copied to ``/etc/systemd/system/`` on the
VPS by the M4 deploy worker. Reading-B does NOT introduce a companion
``.timer`` — the daemon is long-lived (Restart=on-failure).
"""
from __future__ import annotations

import configparser
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
NEWS_UNIT = SYSTEMD_DIR / "alpha-sniper-news.service"
NEWS_TIMER = SYSTEMD_DIR / "alpha-sniper-news.timer"


def _read_unit(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(strict=False, interpolation=None)
    cfg.read(path, encoding="utf-8")
    return cfg


# ---------------------------------------------------------------------------
# File existence + no companion timer
# ---------------------------------------------------------------------------


def test_news_service_unit_exists() -> None:
    """VAL-M2-004 (local mirror): unit file checked into repo."""
    assert NEWS_UNIT.is_file(), f"missing: {NEWS_UNIT}"


def test_news_service_has_no_companion_timer() -> None:
    """VAL-M2-006: long-lived service — never introduce a .timer."""
    assert not NEWS_TIMER.exists(), (
        f"alpha-sniper-news must NOT have a companion .timer "
        f"(found: {NEWS_TIMER})"
    )


# ---------------------------------------------------------------------------
# [Service] section — type, exec, environment, user
# ---------------------------------------------------------------------------


def test_news_service_type_is_simple() -> None:
    """VAL-M2-005: long-lived service — Type=simple, no oneshot."""
    cfg = _read_unit(NEWS_UNIT)
    assert cfg.has_section("Service"), "[Service] section missing"
    assert cfg["Service"].get("Type") == "simple", (
        f"Type must be simple (got {cfg['Service'].get('Type')!r})"
    )
    # Must not declare RemainAfterExit on a long-lived service.
    assert "RemainAfterExit" not in cfg["Service"], (
        "Type=simple long-lived service must not declare RemainAfterExit"
    )


def test_news_service_exec_start_uses_canonical_venv_and_module() -> None:
    """VAL-M2-007 / VAL-M4-002: ExecStart references project venv + module."""
    cfg = _read_unit(NEWS_UNIT)
    exec_start = cfg["Service"].get("ExecStart") or ""
    assert exec_start == (
        "/root/alpha_sniper/repo/.venv/bin/python -m biotech_sniper.news_daemon"
    ), f"ExecStart mismatch: {exec_start!r}"


def test_news_service_environment_file_and_workdir() -> None:
    """VAL-M2-007 / VAL-M4-002: EnvironmentFile + WorkingDirectory."""
    cfg = _read_unit(NEWS_UNIT)
    svc = cfg["Service"]
    assert svc.get("EnvironmentFile") == "/root/alpha_sniper/.env"
    assert svc.get("WorkingDirectory") == "/root/alpha_sniper/repo"
    assert svc.get("User") == "root"


# ---------------------------------------------------------------------------
# Restart policy — VAL-M2-005, VAL-M2-045
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,expected",
    [
        ("Restart", "on-failure"),
        ("RestartSec", "10s"),
        ("StartLimitBurst", "5"),
        ("StartLimitIntervalSec", "300s"),
    ],
)
def test_news_service_restart_policy(key: str, expected: str) -> None:
    cfg = _read_unit(NEWS_UNIT)
    # systemd accepts StartLimit* in either [Unit] or [Service]; we follow
    # AGENTS.md and put them in [Service]. Accept either section so the
    # test does not lock the section choice.
    found = cfg["Service"].get(key) if cfg.has_section("Service") else None
    if found is None and cfg.has_section("Unit"):
        found = cfg["Unit"].get(key)
    assert found == expected, (
        f"{key} expected {expected!r}, got {found!r}"
    )


# ---------------------------------------------------------------------------
# Graceful shutdown — VAL-M4-052
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,expected",
    [
        ("KillSignal", "SIGTERM"),
        ("TimeoutStopSec", "30s"),
        ("KillMode", "control-group"),
    ],
)
def test_news_service_graceful_shutdown(key: str, expected: str) -> None:
    cfg = _read_unit(NEWS_UNIT)
    assert cfg["Service"].get(key) == expected, (
        f"{key} expected {expected!r}, got {cfg['Service'].get(key)!r}"
    )


# ---------------------------------------------------------------------------
# Resource caps — VAL-M2-046 / VAL-M4-002 / VAL-M4-006
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,expected",
    [
        ("Nice", "10"),
        ("IOSchedulingClass", "idle"),
        ("CPUQuota", "15%"),
        ("MemoryMax", "200M"),
        ("MemoryHigh", "150M"),
        ("TasksMax", "64"),
    ],
)
def test_news_service_resource_caps_exact(key: str, expected: str) -> None:
    """Spec uses human-friendly units (200M / 150M); reject byte alternatives."""
    cfg = _read_unit(NEWS_UNIT)
    actual = cfg["Service"].get(key)
    assert actual == expected, (
        f"{key} must be {expected!r} (locked spec; no alt units), got {actual!r}"
    )


# ---------------------------------------------------------------------------
# Logging sink — file-based, rotation delegated to logrotate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["StandardOutput", "StandardError"])
def test_news_service_log_sink_is_canonical_file(key: str) -> None:
    cfg = _read_unit(NEWS_UNIT)
    val = cfg["Service"].get(key)
    assert val == "append:/var/log/alpha_sniper/news.log", (
        f"{key} must append to /var/log/alpha_sniper/news.log, got {val!r}"
    )


# ---------------------------------------------------------------------------
# [Unit] — network-online ordering — VAL-M4-051
# ---------------------------------------------------------------------------


def test_news_service_unit_orders_after_network_online() -> None:
    cfg = _read_unit(NEWS_UNIT)
    assert cfg.has_section("Unit"), "[Unit] section missing"
    assert "network-online.target" in (cfg["Unit"].get("After") or ""), (
        "After= must include network-online.target"
    )
    assert "network-online.target" in (cfg["Unit"].get("Wants") or ""), (
        "Wants= must include network-online.target"
    )


# ---------------------------------------------------------------------------
# [Install] — needed so `systemctl enable` works
# ---------------------------------------------------------------------------


def test_news_service_install_section_present() -> None:
    cfg = _read_unit(NEWS_UNIT)
    assert cfg.has_section("Install"), "[Install] section missing"
    assert "multi-user.target" in (cfg["Install"].get("WantedBy") or ""), (
        "[Install] WantedBy must include multi-user.target"
    )


# ---------------------------------------------------------------------------
# AGENTS.md verification grep contract (the literal feature acceptance grep)
# ---------------------------------------------------------------------------


def test_news_service_grep_contract() -> None:
    """The feature acceptance grep MUST match all required directive keys.

    Mirrors the verification command from the feature contract:
        grep -E '^(Type|Restart|RestartSec|StartLimitBurst|Nice|
                   IOSchedulingClass|CPUQuota|MemoryMax|MemoryHigh|
                   TasksMax|KillSignal|TimeoutStopSec)' \\
            deploy/systemd/alpha-sniper-news.service
    """
    text = NEWS_UNIT.read_text(encoding="utf-8")
    required_keys = [
        "Type",
        "Restart",
        "RestartSec",
        "StartLimitBurst",
        "Nice",
        "IOSchedulingClass",
        "CPUQuota",
        "MemoryMax",
        "MemoryHigh",
        "TasksMax",
        "KillSignal",
        "TimeoutStopSec",
    ]
    lines = text.splitlines()
    for key in required_keys:
        prefix = f"{key}="
        assert any(line.startswith(prefix) for line in lines), (
            f"unit file missing line starting with {prefix!r}"
        )
