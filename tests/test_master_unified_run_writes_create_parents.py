"""f-m2-16 fix #2 regression — output-write paths auto-create parents.

A clean clone of the repo (and a clean VPS deploy) does not include
the ``intelligence/`` directory in the git tree. The legacy
``master_unified_run.py`` opened ``intelligence/unified_master_signals.json``
without creating the parent — yielding ``FileNotFoundError`` on the
very first run after deploy.

This test pins the fix: with a fresh ``BASE_DIR`` that contains
*nothing* but the seed venv directories, the orchestrator's write
step must create the missing parent directory and persist the JSON
report.

The test is fully hermetic — it stubs the heavy intelligence imports
via ``daily_news_ingest`` / ``emit_play_cards`` monkeypatches and
points ``BIOTECH_SNIPER_HOME`` at a tmp directory so nothing in the
real repo is touched.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest


def _reload_paths_and_orchestrator(monkeypatch, base_dir: Path):
    """Point ``BIOTECH_SNIPER_HOME`` at ``base_dir`` and reload modules.

    ``master_unified_run`` resolves ``OUTPUT_FILE`` at import time from
    ``biotech_sniper.paths.BASE_DIR`` so we must reload both modules
    after the env override is set.
    """
    monkeypatch.setenv("BIOTECH_SNIPER_HOME", str(base_dir))
    from biotech_sniper import paths as _paths

    importlib.reload(_paths)
    from biotech_sniper import master_unified_run as _muri

    importlib.reload(_muri)
    return _muri


class _StubIngestResult:
    db_path = ""
    rows_inserted = 0
    tickers_attempted = 0
    empty_feed_reasons: dict[str, str] = {}

    def to_dict(self) -> dict[str, Any]:
        return {"rows_inserted": 0, "tickers_attempted": 0, "empty_feed_reasons": {}}


def test_run_unified_scan_creates_intelligence_dir_when_missing(
    monkeypatch, tmp_path
):
    """Fresh BASE_DIR with no ``intelligence/`` subdir — the orchestrator
    must auto-create it and write the unified-master-signals JSON.
    """
    # Sanity: ``intelligence/`` must NOT exist in our fresh tmp BASE_DIR.
    assert not (tmp_path / "intelligence").exists()

    muri = _reload_paths_and_orchestrator(monkeypatch, tmp_path)

    # The recomputed ``OUTPUT_FILE`` is rooted at the fresh tmp BASE_DIR.
    expected_output = tmp_path / "intelligence" / "unified_master_signals.json"
    assert muri.OUTPUT_FILE == expected_output, (
        f"OUTPUT_FILE did not pick up the BIOTECH_SNIPER_HOME override: "
        f"{muri.OUTPUT_FILE!r} vs {expected_output!r}"
    )

    # Stub the two hooks so the run completes deterministically.
    from biotech_sniper import news_events, play_card_formatter

    monkeypatch.setattr(
        news_events,
        "daily_news_ingest",
        lambda *a, **k: _StubIngestResult(),
    )
    monkeypatch.setattr(
        play_card_formatter,
        "emit_play_cards",
        lambda *a, **k: [],
    )

    # Run. All other steps fail-soft via the orchestrator's try/excepts.
    result = muri.run_unified_scan()

    # The output file must be on disk after the run.
    assert expected_output.is_file(), (
        f"expected output file at {expected_output!r} but it was not created; "
        f"intelligence/ exists={expected_output.parent.is_dir()}"
    )
    # And the file content is the JSON dict the orchestrator returned.
    assert isinstance(result, dict)
    assert result.get("run_date")


def test_output_file_parent_is_created_idempotently(monkeypatch, tmp_path):
    """Pre-creating the parent directory must not break the run.

    The orchestrator uses ``mkdir(parents=True, exist_ok=True)`` so a
    second run (with the dir already in place) must succeed too.
    """
    # Pre-create the intelligence/ dir.
    (tmp_path / "intelligence").mkdir()
    muri = _reload_paths_and_orchestrator(monkeypatch, tmp_path)

    from biotech_sniper import news_events, play_card_formatter

    monkeypatch.setattr(
        news_events,
        "daily_news_ingest",
        lambda *a, **k: _StubIngestResult(),
    )
    monkeypatch.setattr(
        play_card_formatter,
        "emit_play_cards",
        lambda *a, **k: [],
    )

    muri.run_unified_scan()
    expected_output = tmp_path / "intelligence" / "unified_master_signals.json"
    assert expected_output.is_file()


@pytest.fixture(autouse=True)
def _restore_paths_module():
    """Reload paths + master_unified_run with default env after each test
    so subsequent tests in the suite see the real repo BASE_DIR.
    """
    yield
    import os

    os.environ.pop("BIOTECH_SNIPER_HOME", None)
    from biotech_sniper import paths as _paths

    importlib.reload(_paths)
    from biotech_sniper import master_unified_run as _muri

    importlib.reload(_muri)
