"""Regression tests for :mod:`biotech_sniper.intraday_scanner`.

f-misc-07-intraday-save-log-mkdir
---------------------------------
``save_log()`` writes the per-cycle intraday dedup state to
``state/intraday_log.json``. On production VPS deploys the parent
``state/`` directory is created up-front by the M1 deploy worker, but on
fresh checkouts (dev workstations, ephemeral CI runs, brand-new
``BIOTECH_SNIPER_HOME`` pointed at a tmp directory) that parent does NOT
exist yet — the legacy implementation crashed with
``FileNotFoundError: state/intraday_log.json``.

This test pins the defensive ``mkdir(parents=True, exist_ok=True)``
inside ``save_log()`` so the intraday cycle is resilient to a brand new
``state/`` ancestry.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


def _reload_intraday_scanner_with_home(
    monkeypatch: pytest.MonkeyPatch, base_dir: Path
):
    """Point ``BIOTECH_SNIPER_HOME`` at ``base_dir`` and reload modules.

    ``intraday_scanner`` resolves ``INTRADAY_LOG`` at import time from
    ``biotech_sniper.paths.BASE_DIR`` so we must reload both modules
    after the env override is set.
    """
    monkeypatch.setenv("BIOTECH_SNIPER_HOME", str(base_dir))
    from biotech_sniper import paths as _paths

    importlib.reload(_paths)
    from biotech_sniper import intraday_scanner as _scanner

    importlib.reload(_scanner)
    return _scanner


def test_save_log_creates_state_dir_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A fresh ``BIOTECH_SNIPER_HOME`` with no ``state/`` directory must
    not crash ``save_log()`` — the helper auto-creates the parent.
    """
    # Sanity: state/ MUST NOT exist in the fresh tmp BASE_DIR.
    assert not (tmp_path / "state").exists()

    scanner = _reload_intraday_scanner_with_home(monkeypatch, tmp_path)

    # The recomputed INTRADAY_LOG is rooted at the fresh tmp BASE_DIR.
    expected_log = tmp_path / "state" / "intraday_log.json"
    assert scanner.INTRADAY_LOG == expected_log, (
        f"INTRADAY_LOG did not pick up the BIOTECH_SNIPER_HOME override: "
        f"{scanner.INTRADAY_LOG!r} vs {expected_log!r}"
    )

    payload = {
        "seen_urls": ["https://example.com/topline"],
        "seen_award_ids": ["abc123"],
        "last_scan": "2026-04-29 12:00 PT",
        "alerts_sent": [{"key": "ir:VRDN:webcast_prereg_detected",
                         "ts": "2026-04-29 12:00 PT"}],
    }

    # Pre-condition again: parent dir really is missing.
    assert not expected_log.parent.exists()

    # The call under test — must not raise.
    scanner.save_log(payload)

    # Post-condition: the parent dir was auto-created and the JSON
    # round-trips with the same payload we wrote.
    assert expected_log.parent.is_dir()
    assert expected_log.is_file()
    assert json.loads(expected_log.read_text()) == payload


def test_save_log_idempotent_when_state_dir_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A pre-existing ``state/`` directory must not break ``save_log()``.

    The defensive ``mkdir(parents=True, exist_ok=True)`` is idempotent;
    a second invocation (with the dir already in place) must succeed
    and overwrite the prior payload.
    """
    (tmp_path / "state").mkdir()
    scanner = _reload_intraday_scanner_with_home(monkeypatch, tmp_path)

    expected_log = tmp_path / "state" / "intraday_log.json"

    scanner.save_log({"seen_urls": [], "alerts_sent": []})
    scanner.save_log(
        {"seen_urls": ["https://example.com/x"], "alerts_sent": []}
    )

    assert expected_log.is_file()
    payload = json.loads(expected_log.read_text())
    assert payload["seen_urls"] == ["https://example.com/x"]


@pytest.fixture(autouse=True)
def _restore_paths_module():
    """Reload paths + intraday_scanner with default env after each test
    so subsequent tests in the suite see the real repo BASE_DIR.
    """
    yield
    import os

    os.environ.pop("BIOTECH_SNIPER_HOME", None)
    from biotech_sniper import paths as _paths

    importlib.reload(_paths)
    from biotech_sniper import intraday_scanner as _scanner

    importlib.reload(_scanner)
