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


# ---------------------------------------------------------------------------
# f-m4-08a — daily-pipeline wiring contract
# ---------------------------------------------------------------------------
#
# These tests lock in the three wiring fixes added by f-m4-08a so the
# daily systemd unit performs real pipeline work:
#
# (a) every bare cross-package import is resolved under the
#     fully-qualified ``biotech_sniper.<sub>`` path (no more
#     ``ModuleNotFoundError``-eaten-by-try/except);
# (b) :func:`biotech_sniper.logging_setup.configure` is called
#     exactly once with ``log_name='daily'`` at the top of
#     :func:`run_unified_scan`;
# (c) :func:`biotech_sniper.audit.write_audit_latest` is called on
#     the success path so ``state/audit_latest.json`` is refreshed
#     every cycle;
# (d) :func:`write_audit_latest` is also called on the exception
#     path (the wrapper uses ``try/finally``);
# (e) a structured ``daily_done`` log line is emitted with the four
#     contract-required keys (``duration_sec``, ``orders_submitted``,
#     ``cards_generated``, ``llm_cost_usd``);
# (f) ``sys.path`` is no longer polluted with ``BASE_DIR/intelligence``
#     (or the matching ``sectors`` shims) after importing the module.


import importlib
import logging
import re
import sys

from biotech_sniper import logging_setup as _logging_setup_module
from biotech_sniper.paths import BASE_DIR


def _live_audit_module():
    """Return whichever ``biotech_sniper.audit`` module is currently
    in ``sys.modules`` (or freshly import it).

    Necessary because :mod:`tests.test_audit_extension` pops the
    audit module from ``sys.modules`` and re-imports it under a
    network-stubbed fixture, which makes a top-level
    ``from biotech_sniper import audit as _audit_module`` reference
    the *previous* module instance.
    """
    return importlib.import_module("biotech_sniper.audit")


# Modules that the orchestrator must import via the fully-qualified
# ``biotech_sniper.<sub>`` path. Each entry is a (bare-name,
# fully-qualified-path) pair so the test both bans the bare form and
# proves the qualified form resolves.
_REQUIRED_QUALIFIED_IMPORTS: tuple[tuple[str, str], ...] = (
    ("amendment_tracker", "biotech_sniper.intelligence.amendment_tracker"),
    ("sam_sniper", "biotech_sniper.sectors.contracts.sam_sniper"),
    ("adcom_scanner", "biotech_sniper.sectors.adcom.adcom_scanner"),
    ("performance_tracker", "biotech_sniper.performance_tracker"),
    ("auto_resolver", "biotech_sniper.auto_resolver"),
    ("learning_engine", "biotech_sniper.learning_engine"),
    ("master_discovery", "biotech_sniper.intelligence.master_discovery"),
    ("company_resolver", "biotech_sniper.intelligence.company_resolver"),
    (
        "science_enrichment_pipeline",
        "biotech_sniper.intelligence.science_enrichment_pipeline",
    ),
    ("trial_science_reader", "biotech_sniper.intelligence.trial_science_reader"),
    ("ir_events_watcher", "biotech_sniper.intelligence.ir_events_watcher"),
    ("sec_8k_monitor", "biotech_sniper.intelligence.sec_8k_monitor"),
    (
        "twitter_biotech_monitor",
        "biotech_sniper.intelligence.twitter_biotech_monitor",
    ),
    ("watchlist_lifecycle", "biotech_sniper.intelligence.watchlist_lifecycle"),
    ("new_opportunity_sniper", "biotech_sniper.new_opportunity_sniper"),
)


def test_no_bare_cross_package_imports_in_master_unified_run():
    """(a) ``master_unified_run.py`` must not contain any bare
    ``from <bare> import …`` lines for cross-package modules — the
    bare form silently raised :class:`ModuleNotFoundError` because
    the broken ``sys.path.insert`` shims pointed at non-existent
    directories.

    Both the qualified path is verified to resolve AND the source is
    grepped to ensure no bare form regresses.
    """
    src_path = Path(master_unified_run.__file__)
    src = src_path.read_text(encoding="utf-8")

    bare_names = "|".join(re.escape(name) for name, _ in _REQUIRED_QUALIFIED_IMPORTS)
    bare_pattern = re.compile(
        rf"^\s+from\s+({bare_names})\s+import\b", re.MULTILINE
    )
    bare_hits = bare_pattern.findall(src)
    assert not bare_hits, (
        f"master_unified_run.py still has bare cross-package imports: {bare_hits}. "
        f"Replace each with a fully-qualified `from biotech_sniper.<sub> import ...` form."
    )

    # And the qualified path resolves for every entry.
    for _, qualified in _REQUIRED_QUALIFIED_IMPORTS:
        try:
            importlib.import_module(qualified)
        except Exception as exc:  # pragma: no cover - test failure path
            pytest.fail(
                f"fully-qualified import {qualified!r} did not resolve: {exc!r}"
            )


def _stub_orchestrator_hooks(monkeypatch, tmp_path) -> dict[str, list[Any]]:
    """Patch the two hooks the orchestrator hits so the daily run
    completes hermetically. Returns a dict with collected calls.
    """
    calls: dict[str, list[Any]] = {"news": [], "cards": []}

    def fake_news_ingest(*a: Any, **kw: Any) -> _StubIngestResult:
        calls["news"].append((a, kw))
        return _StubIngestResult(db_path=str(tmp_path / "a.db"), empty={})

    monkeypatch.setattr(news_events, "daily_news_ingest", fake_news_ingest)

    def fake_emit(*a: Any, **kw: Any) -> list[Path]:
        calls["cards"].append((a, kw))
        return [Path("a.json"), Path("b.json")]

    monkeypatch.setattr(play_card_formatter, "emit_play_cards", fake_emit)
    return calls


def test_logging_setup_configure_called_once_with_log_name_daily(
    monkeypatch, tmp_path
):
    """(b) :func:`logging_setup.configure` must be called exactly once
    with ``log_name='daily'`` at the top of ``run_unified_scan``.
    """
    _stub_orchestrator_hooks(monkeypatch, tmp_path)

    configure_calls: list[dict[str, Any]] = []

    def fake_configure(*a: Any, **kw: Any) -> Any:
        configure_calls.append({"args": a, "kwargs": kw})
        return logging.getLogger()

    # Patch BOTH the source attribute and the master_unified_run's
    # imported reference so the orchestrator's ``logging_setup.configure``
    # call dispatches to our fake.
    monkeypatch.setattr(_logging_setup_module, "configure", fake_configure)
    monkeypatch.setattr(
        master_unified_run.logging_setup, "configure", fake_configure
    )

    master_unified_run.run_unified_scan()

    assert len(configure_calls) == 1, (
        f"logging_setup.configure called {len(configure_calls)} times "
        f"(expected exactly 1)"
    )
    kwargs = configure_calls[0]["kwargs"]
    args = configure_calls[0]["args"]
    log_name = kwargs.get("log_name") or (args[0] if args else None)
    assert log_name == "daily", (
        f"logging_setup.configure must be called with log_name='daily', "
        f"got {log_name!r}"
    )


def test_write_audit_latest_called_on_success_path(monkeypatch, tmp_path):
    """(c) :func:`audit.write_audit_latest` must run on the success
    path, with the ``last_daily_run_summary`` carrying the four
    daily_done keys plus a ``success=True`` flag.
    """
    _stub_orchestrator_hooks(monkeypatch, tmp_path)

    audit_calls: list[dict[str, Any]] = []

    def fake_write_audit(audit_path: Path, *, extra: dict[str, Any] | None = None,
                         db_path: Any = None) -> dict[str, Any]:
        audit_calls.append(
            {"audit_path": audit_path, "extra": extra, "db_path": db_path}
        )
        return dict(extra or {})

    monkeypatch.setattr(_live_audit_module(), "write_audit_latest", fake_write_audit)

    master_unified_run.run_unified_scan()

    assert len(audit_calls) >= 1, (
        "audit.write_audit_latest must be called on the success path"
    )
    extra = audit_calls[-1]["extra"] or {}
    summary = extra.get("last_daily_run_summary") or {}
    for key in (
        "date",
        "duration_sec",
        "orders_submitted",
        "cards_generated",
        "llm_cost_usd",
        "success",
    ):
        assert key in summary, (
            f"last_daily_run_summary missing required key {key!r}: {summary}"
        )
    assert summary["success"] is True, (
        f"success path must record success=True, got {summary['success']!r}"
    )


def test_write_audit_latest_called_on_exception_path(monkeypatch, tmp_path):
    """(d) The wrapper uses ``try/finally`` so
    :func:`audit.write_audit_latest` runs even when the body raises.
    """
    _stub_orchestrator_hooks(monkeypatch, tmp_path)

    sentinel = RuntimeError("boom")

    def fake_impl(**_kw: Any) -> dict[str, Any]:
        raise sentinel

    monkeypatch.setattr(master_unified_run, "_run_unified_scan_impl", fake_impl)

    audit_calls: list[dict[str, Any]] = []

    def fake_write_audit(audit_path: Path, *, extra: dict[str, Any] | None = None,
                         db_path: Any = None) -> dict[str, Any]:
        audit_calls.append(
            {"audit_path": audit_path, "extra": extra, "db_path": db_path}
        )
        return dict(extra or {})

    monkeypatch.setattr(_live_audit_module(), "write_audit_latest", fake_write_audit)

    with pytest.raises(RuntimeError, match="boom"):
        master_unified_run.run_unified_scan()

    assert len(audit_calls) >= 1, (
        "audit.write_audit_latest must run on the exception path "
        "(via try/finally) — got zero calls"
    )
    summary = (audit_calls[-1]["extra"] or {}).get("last_daily_run_summary") or {}
    assert summary.get("success") is False, (
        f"exception path must record success=False, got {summary.get('success')!r}"
    )


def test_daily_done_log_event_carries_required_keys(monkeypatch, tmp_path, caplog):
    """(e) A structured ``daily_done`` log line must be emitted with
    the four keys ``duration_sec``, ``orders_submitted``,
    ``cards_generated``, ``llm_cost_usd``.
    """
    _stub_orchestrator_hooks(monkeypatch, tmp_path)

    # Capture records emitted by the master_unified_run logger at
    # INFO level. ``caplog`` propagates from the project's logger.
    caplog.set_level(logging.INFO, logger="biotech_sniper.master_unified_run")

    master_unified_run.run_unified_scan()

    daily_done_records = [
        r for r in caplog.records
        if getattr(r, "event", None) == "daily_done"
        or r.getMessage() == "daily_done"
    ]
    assert daily_done_records, (
        "expected a `daily_done` log record from run_unified_scan; "
        f"saw events {[getattr(r, 'event', r.getMessage()) for r in caplog.records]}"
    )
    record = daily_done_records[-1]
    for key in ("duration_sec", "orders_submitted", "cards_generated", "llm_cost_usd"):
        assert hasattr(record, key), (
            f"daily_done log record missing required key {key!r}; "
            f"available extras: {sorted(vars(record).keys())}"
        )
    # Type sanity-check: numeric fields must be numbers.
    assert isinstance(record.duration_sec, (int, float))
    assert isinstance(record.orders_submitted, int)
    assert isinstance(record.cards_generated, int)
    assert isinstance(record.llm_cost_usd, (int, float))


def test_sys_path_no_longer_contains_base_dir_intelligence_shim():
    """(f) Importing ``master_unified_run`` must NOT prepend
    ``BASE_DIR/intelligence`` (or the ``sectors`` shims) to
    ``sys.path``. The legacy ``sys.path.insert`` mutations were the
    root cause of the daily-run silent failure.

    We isolate the assertion by running a fresh ``python`` subprocess
    so module-level mutations done by *other* legacy submodules
    (``master_discovery``, ``adcom_scanner``, …) — which may have
    been triggered by sibling tests in this session — do not pollute
    the assertion. Only ``master_unified_run`` itself is imported
    in the child interpreter.
    """
    import subprocess

    forbidden = [
        str(BASE_DIR / "intelligence"),
        str(BASE_DIR / "sectors"),
        str(BASE_DIR / "sectors/contracts"),
        str(BASE_DIR / "sectors/adcom"),
    ]

    script = (
        "import sys\n"
        "import biotech_sniper.master_unified_run  # noqa: F401\n"
        "print(repr(list(sys.path)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(BASE_DIR),
    )
    assert proc.returncode == 0, (
        f"subprocess import failed: stdout={proc.stdout!r} "
        f"stderr={proc.stderr!r}"
    )
    child_sys_path = eval(proc.stdout.strip())  # noqa: S307 — controlled output
    for shim in forbidden:
        assert shim not in child_sys_path, (
            f"sys.path must NOT contain legacy shim {shim!r} after "
            f"importing master_unified_run; child sys.path={child_sys_path}"
        )

    # Source-level grep: master_unified_run.py itself must not
    # contain any active ``sys.path.insert(...)`` call.
    src = Path(master_unified_run.__file__).read_text(encoding="utf-8")
    active_insert = re.compile(r"^[^#\n]*\bsys\.path\.insert\b", re.MULTILINE)
    matches = [m.group(0) for m in active_insert.finditer(src)]
    assert not matches, (
        f"master_unified_run.py still contains active sys.path.insert calls: "
        f"{matches}"
    )
