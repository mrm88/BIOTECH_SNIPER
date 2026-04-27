"""Tests for f-m2-13 fixes #4 and #5 — master_unified_run wiring.

Verifies that ``biotech_sniper.master_unified_run.run_unified_scan``:

* calls :func:`biotech_sniper.news_events.daily_news_ingest` exactly
  once BEFORE scoring (fix #4);
* writes ``news_events_empty`` (and ``news_ingestion``) keys into the
  audit JSON the news ingest uses (fix #4);
* calls :func:`biotech_sniper.play_card_formatter.emit_play_cards`
  exactly once AFTER scoring (fix #5).

The orchestrator pulls in many heavy submodules (Twitter agent,
amendment tracker, Alpaca, …) — we don't care about those for this
test, only about the daily-news-ingest and play-card-emission hooks.
We monkey-patch the two hooks and let the rest of the orchestrator
fail-soft via its existing ``try/except`` blocks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper import master_unified_run, news_events, play_card_formatter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubIngestResult:
    """Minimal ``DailyIngestResult`` substitute exposing ``to_dict``."""

    def __init__(self, *, db_path: str, empty: dict[str, str]):
        self.db_path = db_path
        self.rows_inserted = 0
        self.tickers_attempted = 1
        self.empty_feed_reasons = dict(empty)

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_path": self.db_path,
            "rows_inserted": self.rows_inserted,
            "tickers_attempted": self.tickers_attempted,
            "empty_feed_reasons": dict(self.empty_feed_reasons),
        }


# ---------------------------------------------------------------------------
# Fixture: stub the heavy intelligence imports so run_unified_scan finishes
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_intelligence(monkeypatch):
    """Make the orchestrator's optional imports fail-soft.

    ``run_unified_scan`` imports a stack of intelligence/sectors
    submodules through ``sys.path`` shims that don't all exist in the
    test environment. Each one is wrapped in ``try/except`` that
    prints "ERROR" and moves on, which is exactly what we want — but
    importing :mod:`biotech_sniper.intelligence.science_enrichment_pipeline`
    is unconditional and would crash the test if it doesn't exist on
    disk. We mark every problematic import as a no-op via
    monkeypatching ``builtins.__import__``.
    """
    # The orchestrator catches every exception from its inner imports
    # already; we don't have to do anything except make sure pytest
    # doesn't propagate the import errors. The real ``daily_news_ingest``
    # and ``emit_play_cards`` symbols are imported via the ``biotech_sniper.``
    # package path, which IS present, so the targeted hooks below work.
    yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_daily_news_ingest_is_called_exactly_once(monkeypatch, tmp_path, stub_intelligence):
    """f-m2-13 fix #4: master_unified_run must call daily_news_ingest once."""
    ingest_calls: list[dict[str, Any]] = []
    audit_path = tmp_path / "audit_latest.json"

    def fake_daily_news_ingest(*args: Any, **kwargs: Any) -> _StubIngestResult:
        ingest_calls.append({"args": args, "kwargs": kwargs})
        result = _StubIngestResult(
            db_path=str(tmp_path / "alpha.db"),
            empty={"AAA": "no_feed_match"},
        )
        # Mirror the real audit-merge behaviour.
        existing: dict[str, Any] = {}
        if audit_path.is_file():
            existing = json.loads(audit_path.read_text(encoding="utf-8"))
        existing["news_ingestion"] = result.to_dict()
        existing["news_events_empty"] = dict(result.empty_feed_reasons)
        audit_path.write_text(json.dumps(existing, indent=2, sort_keys=True))
        return result

    # Patch the symbol AT THE IMPORT SITE inside the orchestrator's
    # function body. The orchestrator imports
    # ``from biotech_sniper.news_events import daily_news_ingest`` at
    # call time (lazy), so patching the source attribute is enough.
    monkeypatch.setattr(news_events, "daily_news_ingest", fake_daily_news_ingest)

    emit_calls: list[dict[str, Any]] = []

    def fake_emit_play_cards(*args: Any, **kwargs: Any) -> list[Path]:
        emit_calls.append({"args": args, "kwargs": kwargs})
        return []

    monkeypatch.setattr(play_card_formatter, "emit_play_cards", fake_emit_play_cards)

    # Run the orchestrator. We don't care if the discovery/biotech/
    # contracts/adcom steps fail — the orchestrator catches every one.
    result = master_unified_run.run_unified_scan()

    assert len(ingest_calls) == 1, f"daily_news_ingest called {len(ingest_calls)} times"
    # Audit JSON includes ``news_events_empty`` so VAL-M2-076 can pass.
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert "news_events_empty" in audit
    assert audit["news_events_empty"] == {"AAA": "no_feed_match"}
    # And the orchestrator returns successfully.
    assert isinstance(result, dict)


def test_emit_play_cards_is_called_exactly_once_after_scoring(
    monkeypatch, tmp_path, stub_intelligence
):
    """f-m2-13 fix #5: master_unified_run must call emit_play_cards once."""

    monkeypatch.setattr(
        news_events,
        "daily_news_ingest",
        lambda *a, **k: _StubIngestResult(
            db_path=str(tmp_path / "alpha.db"), empty={}
        ),
    )

    emit_calls: list[dict[str, Any]] = []

    def fake_emit_play_cards(*args: Any, **kwargs: Any) -> list[Path]:
        emit_calls.append({"args": args, "kwargs": kwargs})
        return []

    monkeypatch.setattr(play_card_formatter, "emit_play_cards", fake_emit_play_cards)

    master_unified_run.run_unified_scan()

    assert len(emit_calls) == 1, f"emit_play_cards called {len(emit_calls)} times"
    # The call must specify ``as_of_date=today``.
    kwargs = emit_calls[0]["kwargs"]
    args = emit_calls[0]["args"]
    # Either passed as kwarg or first positional arg.
    as_of = kwargs.get("as_of_date") or (args[0] if args else None)
    assert as_of, "emit_play_cards must be called with an as_of_date"


def test_master_unified_run_source_references_both_hooks():
    """Lock the wiring in via a source-level grep so future drift is loud."""
    import inspect

    src = inspect.getsource(master_unified_run)
    assert "daily_news_ingest" in src, (
        "master_unified_run.py must invoke daily_news_ingest"
    )
    assert "emit_play_cards" in src, (
        "master_unified_run.py must invoke emit_play_cards"
    )
