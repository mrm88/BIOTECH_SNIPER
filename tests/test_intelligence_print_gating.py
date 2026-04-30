"""Tests for f-fix-m4-03a — intelligence/ print() gating.

The news_daemon adapters import :mod:`biotech_sniper.intelligence.sec_8k_monitor`
and :mod:`biotech_sniper.intelligence.ir_events_watcher` at runtime to drive
the Stage-1 RSS polling.  Those modules historically printed heading bars to
stdout in ``run_8k_monitor`` and ``run_ir_events_check``; under systemd's
``StandardOutput=append:/var/log/alpha_sniper/news.log`` directive those
prints land in ``news.log`` as plain (non-JSON) lines, breaking VAL-M4-017
("every line is structured JSON with required keys ts/level/event/module").

Fix: gate every ``print(...)`` in the two modules behind a
``__name__ == '__main__'`` check via a module-private helper, so:

* CLI-direct usage (``python -m biotech_sniper.intelligence.sec_8k_monitor``
  or running the file as a script) keeps the human-readable banners.
* Import-only usage (the news_daemon adapter path) produces zero stdout
  output even if the watcher functions are subsequently called.

These tests assert the *import-time* contract pinned by the feature
description: importing each module must capture zero bytes of stdout.
The runtime gating is a stronger guarantee on top — the watcher
``run_*`` functions are exercised end-to-end by the news-daemon
production-wiring tests, which would surface any leaked print().
"""

from __future__ import annotations

import sys

import pytest


def _purge_module(name: str) -> None:
    """Remove a module from ``sys.modules`` so a subsequent import re-runs
    its module body (fresh import side-effects)."""

    sys.modules.pop(name, None)


def test_import_sec_8k_monitor_no_stdout(capsys):
    """`import biotech_sniper.intelligence.sec_8k_monitor` is silent."""

    _purge_module("biotech_sniper.intelligence.sec_8k_monitor")
    capsys.readouterr()  # discard anything captured before this test
    import biotech_sniper.intelligence.sec_8k_monitor  # noqa: F401

    captured = capsys.readouterr()
    assert captured.out == "", (
        f"sec_8k_monitor import emitted unexpected stdout: {captured.out!r}"
    )


def test_import_ir_events_watcher_no_stdout(capsys):
    """`import biotech_sniper.intelligence.ir_events_watcher` is silent."""

    _purge_module("biotech_sniper.intelligence.ir_events_watcher")
    capsys.readouterr()  # discard anything captured before this test
    import biotech_sniper.intelligence.ir_events_watcher  # noqa: F401

    captured = capsys.readouterr()
    assert captured.out == "", (
        f"ir_events_watcher import emitted unexpected stdout: "
        f"{captured.out!r}"
    )


def test_run_8k_monitor_no_stdout_when_imported(capsys, tmp_path, monkeypatch):
    """``run_8k_monitor`` produces zero stdout when reached via an import.

    This is the runtime-gating contract: when the news_daemon adapter
    invokes the watcher (i.e. the module's ``__name__`` is
    ``biotech_sniper.intelligence.sec_8k_monitor``, NOT ``__main__``),
    the heading bars must NOT print — otherwise systemd routes them
    into ``news.log`` as non-JSON lines and VAL-M4-017 fails.

    We monkeypatch the few network/IO entry points the function reaches
    so the test stays hermetic; the assertion is on stdout, not on
    behaviour.
    """

    _purge_module("biotech_sniper.intelligence.sec_8k_monitor")
    import biotech_sniper.intelligence.sec_8k_monitor as mod

    # Empty registry → no per-CIK fan-out.
    monkeypatch.setattr(mod, "load_registry", lambda: {"watchlist": {}})
    monkeypatch.setattr(
        mod, "load_sec_state", lambda: {"seen_filings": [], "last_checked": None}
    )
    monkeypatch.setattr(mod, "save_sec_state", lambda _state: None)
    # No RSS hits.
    monkeypatch.setattr(mod, "fetch_sec_rss_recent", lambda count=40: [])
    # Avoid the persistence side-effect (writes to alpha_sniper.db).
    monkeypatch.setattr(
        mod, "_persist_sec_8k_signals_to_news_events", lambda _signals: None
    )
    # Redirect the JSON report write to the tmp path so we don't touch
    # the real intelligence/sec_8k_report.json.
    monkeypatch.setattr(
        mod, "OUTPUT_FILE", tmp_path / "sec_8k_report.json"
    )

    capsys.readouterr()  # clear
    mod.run_8k_monitor(mode="intraday")

    captured = capsys.readouterr()
    assert captured.out == "", (
        "run_8k_monitor() must not print to stdout when the module is "
        "imported (only when __name__ == '__main__'). Captured: "
        f"{captured.out!r}"
    )


def test_run_ir_events_check_no_stdout_when_imported(
    capsys, tmp_path, monkeypatch
):
    """``run_ir_events_check`` produces zero stdout when reached via an import.

    Counterpart to ``test_run_8k_monitor_no_stdout_when_imported`` for the
    IR-events watcher.
    """

    _purge_module("biotech_sniper.intelligence.ir_events_watcher")
    import biotech_sniper.intelligence.ir_events_watcher as mod

    # Empty registry so the per-ticker for-loop body never runs.
    monkeypatch.setattr(mod, "load_registry", lambda: {"watchlist": {}})
    monkeypatch.setattr(mod, "load_ir_state", lambda: {})
    monkeypatch.setattr(mod, "save_ir_state", lambda _state: None)
    monkeypatch.setattr(
        mod,
        "_persist_ir_signals_to_news_events",
        lambda _signals: None,
    )
    monkeypatch.setattr(
        mod, "OUTPUT_FILE", tmp_path / "ir_events_report.json"
    )

    capsys.readouterr()  # clear
    mod.run_ir_events_check()

    captured = capsys.readouterr()
    assert captured.out == "", (
        "run_ir_events_check() must not print to stdout when the module "
        "is imported (only when __name__ == '__main__'). Captured: "
        f"{captured.out!r}"
    )
