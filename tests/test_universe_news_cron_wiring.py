"""f-m4-09 wiring tests.

Locks in three contract points the milestone validator relies on:

1. ``master_unified_run.run_unified_scan`` calls
   :func:`biotech_sniper.bulk_universe_scanner.build_universe` exactly
   once per cycle so ``last_chain_check_at`` is refreshed on every
   universe row each day (≥ 90% rows per the f-m4-09 description).

2. ``RSS_FEEDS`` in ``intelligence.universal_news_watcher`` no longer
   contains the dead Reuters Health endpoint, but still meets the
   "≥ 4 feeds" guardrail.

3. ``run_8k_monitor`` and ``run_ir_events_check`` no longer raise
   :class:`FileNotFoundError` when ``intelligence/nct_registry.json``
   is absent — the WARNING log surfaced by f-m4-08 retry must be
   gone.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from biotech_sniper import master_unified_run, news_events, play_card_formatter
from biotech_sniper import bulk_universe_scanner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubBuildResult:
    """Stand-in for :class:`bulk_universe_scanner.BuildResult`."""

    def __init__(self) -> None:
        self.watch_count = 600
        self.tradeable_count = 150
        self.probe_calls = 600
        self.completed_at = "2026-04-27T13:14:15.123456Z"
        self.db_path = "/tmp/x.db"


class _StubIngestResult:
    """Stand-in for ``DailyIngestResult`` exposing ``to_dict``."""

    def __init__(self) -> None:
        self.db_path = "/tmp/x.db"
        self.rows_inserted = 0
        self.tickers_attempted = 0
        self.empty_feed_reasons: dict[str, str] = {}
        self.completed_at = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_path": self.db_path,
            "rows_inserted": self.rows_inserted,
            "tickers_attempted": self.tickers_attempted,
            "empty_feed_reasons": dict(self.empty_feed_reasons),
        }


# ---------------------------------------------------------------------------
# 1) build_universe is wired into the daily run
# ---------------------------------------------------------------------------


def test_build_universe_called_exactly_once_per_daily_cycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """f-m4-09 (daily): ``build_universe`` runs every cycle BEFORE news ingest."""
    build_calls: list[dict[str, Any]] = []
    news_calls: list[dict[str, Any]] = []
    order: list[str] = []

    def fake_build_universe(*args: Any, **kwargs: Any) -> _StubBuildResult:
        order.append("build_universe")
        build_calls.append({"args": args, "kwargs": kwargs})
        return _StubBuildResult()

    def fake_news_ingest(*args: Any, **kwargs: Any) -> _StubIngestResult:
        order.append("news_ingest")
        news_calls.append({"args": args, "kwargs": kwargs})
        return _StubIngestResult()

    monkeypatch.setattr(
        bulk_universe_scanner, "build_universe", fake_build_universe
    )
    monkeypatch.setattr(news_events, "daily_news_ingest", fake_news_ingest)
    monkeypatch.setattr(
        play_card_formatter,
        "emit_play_cards",
        lambda *a, **kw: [],
    )

    master_unified_run.run_unified_scan()

    assert len(build_calls) == 1, (
        f"build_universe must be called exactly once per cycle "
        f"(got {len(build_calls)})"
    )
    assert len(news_calls) >= 1, "daily_news_ingest must still be called"
    # Universe refresh runs BEFORE news ingest.
    build_idx = order.index("build_universe")
    news_idx = order.index("news_ingest")
    assert build_idx < news_idx, (
        f"build_universe must precede news_ingest (got order={order})"
    )


def test_master_unified_run_source_references_build_universe() -> None:
    """Source-level grep so future drift is loud."""
    import inspect

    src = inspect.getsource(master_unified_run)
    assert "build_universe" in src, (
        "master_unified_run.py must invoke bulk_universe_scanner.build_universe"
    )
    assert "universe_refresh" in src, (
        "master_unified_run.py must record a 'universe_refresh' event"
    )


# ---------------------------------------------------------------------------
# 2) RSS_FEEDS no longer contains the dead Reuters Health endpoint
# ---------------------------------------------------------------------------


def test_rss_feeds_drops_reuters_health() -> None:
    """f-m4-09 (a): replace or remove the dead reuters health RSS source."""
    from biotech_sniper.intelligence import universal_news_watcher as unw

    urls = [feed["url"] for feed in unw.RSS_FEEDS]
    assert not any("feeds.reuters.com" in url for url in urls), (
        f"RSS_FEEDS still references feeds.reuters.com: {urls}"
    )


def test_rss_feeds_meets_minimum_count() -> None:
    """f-m4-09 (a): keep total feed count >= 4."""
    from biotech_sniper.intelligence import universal_news_watcher as unw

    assert len(unw.RSS_FEEDS) >= 4, (
        f"RSS_FEEDS must contain >= 4 entries, got {len(unw.RSS_FEEDS)}"
    )


# ---------------------------------------------------------------------------
# 3) Registry loaders no-op gracefully when nct_registry.json is absent
# ---------------------------------------------------------------------------


def test_sec_8k_load_registry_handles_missing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """f-m4-09 (b): ``run_8k_monitor`` no longer fails on missing registry."""
    from biotech_sniper.intelligence import sec_8k_monitor as sm

    missing = tmp_path / "no-such-file.json"
    monkeypatch.setattr(sm, "REGISTRY_FILE", missing)

    registry = sm.load_registry()
    assert registry == {"watchlist": {}}


def test_ir_events_load_registry_handles_missing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """f-m4-09 (b): ``run_ir_events_check`` no longer fails on missing registry."""
    from biotech_sniper.intelligence import ir_events_watcher as iew

    missing = tmp_path / "no-such-file.json"
    monkeypatch.setattr(iew, "REGISTRY_FILE", missing)

    registry = iew.load_registry()
    assert registry == {"watchlist": {}}


def test_sec_8k_load_registry_handles_corrupt_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from biotech_sniper.intelligence import sec_8k_monitor as sm

    bad = tmp_path / "registry.json"
    bad.write_text("not json {{{")
    monkeypatch.setattr(sm, "REGISTRY_FILE", bad)

    registry = sm.load_registry()
    assert registry == {"watchlist": {}}


def test_ir_events_load_registry_handles_corrupt_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from biotech_sniper.intelligence import ir_events_watcher as iew

    bad = tmp_path / "registry.json"
    bad.write_text("not json {{{")
    monkeypatch.setattr(iew, "REGISTRY_FILE", bad)

    registry = iew.load_registry()
    assert registry == {"watchlist": {}}


def test_sec_8k_load_registry_preserves_existing_watchlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the file IS present, the loader returns its content unchanged."""
    from biotech_sniper.intelligence import sec_8k_monitor as sm

    seed = tmp_path / "registry.json"
    seed.write_text(
        json.dumps({"watchlist": {"AAA": {"company": "Alpha Inc"}}})
    )
    monkeypatch.setattr(sm, "REGISTRY_FILE", seed)

    registry = sm.load_registry()
    assert registry["watchlist"]["AAA"]["company"] == "Alpha Inc"
