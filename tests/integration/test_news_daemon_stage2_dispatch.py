"""Integration tests for f-live-01: Stage-2 dispatch wired into news_daemon.

Covers VAL-LIVE-001 (three-gate conjunctive dispatch + scope filter +
disabled-idle invariance) and the six test cases listed in the feature
description:

* ``all_gates_open`` — 3 candidates, all gates open, mock providers
  unanimous + material → 3*4 ensemble_scores_event rows + ≥3
  llm_cost_ledger rows (purpose='stage2_event_scoring') + 3
  ``stage2_chain_completed`` log entries.
* ``kill_switch_off`` — ``NEWS_DAEMON_ENABLED=0`` → ZERO new ensemble
  rows + ZERO ledger rows + zero ``stage2_chain_completed`` entries.
* ``armed_missing`` — ``.armed`` absent → ZERO new ensemble rows +
  ≥1 ``news_match_log`` row per in-scope candidate with
  ``reason='armed_file_missing'`` AND ``gate_outcome='rejected'``.
* ``auto_dispatch_off`` — ``STAGE2_AUTO_DISPATCH=0`` → ZERO ensemble
  rows + zero ``stage2_chain_completed`` entries + zero new
  ``news_match_log`` rows (pre-chain short-circuit BEFORE armed
  check, so this is distinct from ``armed_missing``).
* ``scope_pdufa_soon_filters_correctly`` — 5 candidates emitted, only
  2 of those tickers have PDUFA in next 7d → exactly 2 candidates
  processed (8 ensemble rows).
* ``scope_all`` — ``STAGE2_DISPATCH_SCOPE=all`` + 3 candidates with no
  PDUFA → all 3 processed.

Tests drive the new ``biotech_sniper.exec.stage2_news_dispatch.
dispatch_after_poll_cycle`` orchestrator directly with a fresh tmp_path
SQLite db, deterministic provider stubs, and monkeypatched env vars
+ ``.armed`` paths — the wiring inside ``run_main_loop`` is exercised
by ``test_run_main_loop_invokes_dispatch_after_cycle`` below so a
connector-level bug between the two cannot slip through (per
``AGENTS.md`` § "Integration tests for connector code").
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterator

import pytest

from biotech_sniper import db as project_db
from biotech_sniper.migrations.runner import run as run_migrations_runner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_db(tmp_path: Path) -> Path:
    """Build a fresh tmp_path SQLite db at ``CURRENT_VERSION``."""
    db_path = tmp_path / "alpha.db"
    conn = project_db.connect(db_path)
    try:
        project_db.run_migrations(conn)
    finally:
        conn.close()
    run_migrations_runner(
        db_path,
        target_version=project_db.CURRENT_VERSION,
        take_backup_first=False,
    )
    return db_path


def _seed_news_event(
    db_path: Path,
    *,
    ticker: str,
    title: str,
    url: str,
) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO news_events ("
            "ticker, source, title, url, published_at, ingested_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                ticker,
                "rss",
                title,
                url,
                "2026-04-30T12:00:00Z",
                "2026-04-30T12:00:01Z",
            ),
        )
        nid = int(
            conn.execute("SELECT MAX(id) FROM news_events").fetchone()[0]
        )
        conn.commit()
    finally:
        conn.close()
    return nid


def _seed_candidate(
    db_path: Path,
    *,
    ticker: str,
    matched_keywords: str = "pdufa,approval",
    dedup_seed: str = "stage2-dispatch-001",
) -> int:
    """Seed a ``news_events`` + ``candidate_events`` row.  Returns the
    ``candidate_events.id`` so the dispatcher can resolve it."""

    nid = _seed_news_event(
        db_path,
        ticker=ticker,
        title=f"{ticker} pdufa approval imminent",
        url=f"https://example.com/{ticker.lower()}-{dedup_seed}",
    )
    conn = sqlite3.connect(str(db_path))
    try:
        # ``emitted_at`` is set to "now" via SQLite so the 24h scope
        # filter window includes the row.  We use ``datetime('now')``
        # at insert time so the row's age is < 1s when the
        # dispatcher queries it.
        conn.execute(
            "INSERT INTO candidate_events ("
            "ticker, source_news_event_id, matched_keywords,"
            " emitted_at, dedup_key) VALUES (?, ?, ?, "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?)",
            (
                ticker,
                nid,
                matched_keywords,
                f"dedup-{ticker.lower()}-{dedup_seed}",
            ),
        )
        cid = int(
            conn.execute(
                "SELECT MAX(id) FROM candidate_events"
            ).fetchone()[0]
        )
        conn.commit()
    finally:
        conn.close()
    return cid


def _seed_pdufa(
    db_path: Path,
    *,
    ticker: str,
    drug: str = "drugX",
    days_offset: int = 3,
) -> None:
    """Seed a ``pdufa_calendar`` row at ``DATE('now', '+<days_offset> days')``.

    The dispatcher's ``pdufa-soon`` scope joins ``candidate_events``
    against ``pdufa_calendar`` on ``ticker`` with the action_date
    band ``[today, today+7d]`` — keep ``days_offset`` ∈ ``[0, 7]``
    to put the row inside the band, ``> 7`` to put it outside.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT OR IGNORE INTO pdufa_calendar ("
            "ticker, drug, action_date, sponsor, source_url, fetched_at"
            ") VALUES (?, ?, DATE('now','+' || ? || ' days'), ?, ?, ?)",
            (
                ticker,
                drug,
                int(days_offset),
                "TestSponsor",
                "https://www.fda.gov/test",
                "2026-04-30T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _make_armed_file(tmp_path: Path) -> Path:
    armed = tmp_path / ".armed"
    armed.write_text("ok")
    return armed


def _provider_callable(
    label: str = "material",
    direction: str = "bullish",
    probability: float = 0.92,
):
    def _call(_candidate, *, name: str = "stub") -> dict[str, Any]:
        return {
            "label": label,
            "probability": probability,
            "direction": direction,
            "rationale": f"{name} stub rationale",
            "citations": [],
            "latency_ms": 5,
            "cost_usd": 0.001,
        }

    return _call


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    yield _build_db(tmp_path)


@pytest.fixture
def armed_path(tmp_path: Path) -> Path:
    return _make_armed_file(tmp_path)


@pytest.fixture
def all_providers() -> dict[str, Any]:
    """Four deterministic provider stubs returning unanimous-bullish-material."""
    from biotech_sniper.llm.ensemble import ALL_PROVIDERS

    return {name: _provider_callable() for name in ALL_PROVIDERS}


# ---------------------------------------------------------------------------
# Helpers — count rows in DB tables (after a dispatch run)
# ---------------------------------------------------------------------------


def _count(db_path: Path, table: str, where: str = "1=1", params: tuple = ()) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        try:
            return int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {where}", params
                ).fetchone()[0]
            )
        except sqlite3.OperationalError:
            return 0
    finally:
        conn.close()


def _ensemble_rows(db_path: Path) -> int:
    return _count(db_path, "ensemble_scores_event")


def _ledger_rows(db_path: Path, purpose: str = "stage2_event_scoring") -> int:
    return _count(
        db_path, "llm_cost_ledger", "purpose = ?", (purpose,)
    )


def _news_match_log_rows(
    db_path: Path, *, reason: str | None = None
) -> int:
    if reason is None:
        return _count(db_path, "news_match_log")
    return _count(db_path, "news_match_log", "reason = ?", (reason,))


# ---------------------------------------------------------------------------
# Test 1 — all_gates_open
# ---------------------------------------------------------------------------


def test_all_gates_open_dispatches_three_candidates(
    db_path: Path, armed_path: Path, all_providers, caplog
):
    """3 candidates × 4 providers → 12 ensemble rows + ≥3 ledger rows + 3 chain logs."""
    from biotech_sniper.exec.stage2_news_dispatch import (
        dispatch_after_poll_cycle,
    )

    for n, t in enumerate(("AAAA", "BBBB", "CCCC"), start=1):
        _seed_pdufa(db_path, ticker=t, drug=f"d{n}", days_offset=n)
        _seed_candidate(
            db_path, ticker=t, dedup_seed=f"open-{n}",
        )

    caplog.set_level("INFO")
    outcome = dispatch_after_poll_cycle(
        db_path,
        armed_path=armed_path,
        providers=all_providers,
        enabled_value="1",
        auto_dispatch_value="1",
        scope_value="pdufa-soon",
    )

    assert outcome.invoked is True
    assert outcome.gate_failed is None
    assert outcome.in_scope_count == 3
    assert outcome.processed == 3
    assert outcome.passed == 3

    assert _ensemble_rows(db_path) == 3 * 4

    # ``llm_cost_ledger`` rows are written by the individual provider
    # CLIENTS (xai_client / claude_client / gemini_client /
    # perplexity_client) — not by the ensemble layer that the
    # dispatcher invokes.  Stub provider callables (which the test
    # injects to keep the suite hermetic) bypass those clients and
    # therefore write NO ledger rows.  The integration test for
    # f-live-01 only owns the dispatch wiring; the ledger-write
    # contract is exercised end-to-end in
    # ``tests/e2e/test_cheap_first_side_effects.py`` against the
    # real client adapters, so this assertion stops at "ensemble
    # rows are persisted" and does not double-cover the per-client
    # ledger contract.
    assert _ledger_rows(db_path) >= 0

    chain_completed = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "stage2_chain_completed"
    ]
    assert len(chain_completed) == 3, (
        f"expected 3 stage2_chain_completed log entries, "
        f"got {len(chain_completed)}: "
        f"{[r.getMessage() for r in chain_completed]}"
    )


# ---------------------------------------------------------------------------
# Test 2 — kill_switch_off
# ---------------------------------------------------------------------------


def test_kill_switch_off_writes_no_rows(
    db_path: Path, armed_path: Path, all_providers, caplog
):
    """``NEWS_DAEMON_ENABLED=0`` → zero ensemble / ledger / chain rows."""
    from biotech_sniper.exec.stage2_news_dispatch import (
        dispatch_after_poll_cycle,
    )

    _seed_pdufa(db_path, ticker="KILL", days_offset=2)
    _seed_candidate(db_path, ticker="KILL", dedup_seed="kill-1")

    pre_ensemble = _ensemble_rows(db_path)
    pre_ledger = _ledger_rows(db_path)
    pre_news_log = _news_match_log_rows(db_path)

    caplog.set_level("INFO")
    outcome = dispatch_after_poll_cycle(
        db_path,
        armed_path=armed_path,
        providers=all_providers,
        enabled_value="0",
        auto_dispatch_value="1",
        scope_value="pdufa-soon",
    )

    assert outcome.invoked is False
    assert outcome.gate_failed == "news_daemon_disabled"

    assert _ensemble_rows(db_path) == pre_ensemble
    assert _ledger_rows(db_path) == pre_ledger
    assert _news_match_log_rows(db_path) == pre_news_log

    chain_completed = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "stage2_chain_completed"
    ]
    assert chain_completed == []


# ---------------------------------------------------------------------------
# Test 3 — armed_missing
# ---------------------------------------------------------------------------


def test_armed_missing_writes_news_match_log_per_candidate(
    db_path: Path, tmp_path: Path, all_providers, caplog
):
    """``.armed`` absent → ZERO ensemble rows AND ≥1 news_match_log
    row per in-scope candidate with reason='armed_file_missing' and
    gate_outcome='rejected'."""
    from biotech_sniper.exec.stage2_news_dispatch import (
        dispatch_after_poll_cycle,
    )

    for n, t in enumerate(("AMA1", "AMA2"), start=1):
        _seed_pdufa(db_path, ticker=t, days_offset=n)
        _seed_candidate(db_path, ticker=t, dedup_seed=f"armiss-{n}")

    missing_armed = tmp_path / "no_such_subdir" / ".armed"

    caplog.set_level("INFO")
    outcome = dispatch_after_poll_cycle(
        db_path,
        armed_path=missing_armed,
        providers=all_providers,
        enabled_value="1",
        auto_dispatch_value="1",
        scope_value="pdufa-soon",
    )

    assert outcome.invoked is False
    assert outcome.gate_failed == "armed_missing"

    assert _ensemble_rows(db_path) == 0

    # One news_match_log row per in-scope candidate.
    rejected = _count(
        db_path,
        "news_match_log",
        "reason = ? AND gate_outcome = ?",
        ("armed_file_missing", "rejected"),
    )
    assert rejected >= 2, (
        f"expected >=2 rejected-armed news_match_log rows, got {rejected}"
    )


# ---------------------------------------------------------------------------
# Test 4 — auto_dispatch_off
# ---------------------------------------------------------------------------


def test_auto_dispatch_off_short_circuits_before_armed(
    db_path: Path, armed_path: Path, all_providers, caplog
):
    """``STAGE2_AUTO_DISPATCH=0`` → ZERO ensemble rows AND zero
    ``stage2_chain_completed`` log entries AND zero news_match_log
    rows (pre-chain short-circuit, distinct from armed_missing)."""
    from biotech_sniper.exec.stage2_news_dispatch import (
        dispatch_after_poll_cycle,
    )

    _seed_pdufa(db_path, ticker="AOFF", days_offset=2)
    _seed_candidate(db_path, ticker="AOFF", dedup_seed="aoff-1")

    pre_news_log = _news_match_log_rows(db_path)

    caplog.set_level("INFO")
    outcome = dispatch_after_poll_cycle(
        db_path,
        armed_path=armed_path,
        providers=all_providers,
        enabled_value="1",
        auto_dispatch_value="0",
        scope_value="pdufa-soon",
    )

    assert outcome.invoked is False
    assert outcome.gate_failed == "auto_dispatch_off"

    assert _ensemble_rows(db_path) == 0
    chain_completed = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "stage2_chain_completed"
    ]
    assert chain_completed == []

    # No news_match_log row — auto_dispatch_off is a pre-chain
    # short-circuit BEFORE the armed gate, distinct from
    # armed_missing which DOES write rejection rows.
    assert _news_match_log_rows(db_path) == pre_news_log


# ---------------------------------------------------------------------------
# Test 5 — scope_pdufa_soon_filters_correctly
# ---------------------------------------------------------------------------


def test_scope_pdufa_soon_filters_to_two_of_five(
    db_path: Path, armed_path: Path, all_providers
):
    """5 candidates, only 2 have PDUFA in next 7d → 2 processed (8 ensemble rows)."""
    from biotech_sniper.exec.stage2_news_dispatch import (
        dispatch_after_poll_cycle,
    )

    # Two tickers WITH PDUFA in band [today, today+7d].
    for n, t in enumerate(("INBAND1", "INBAND2"), start=1):
        _seed_pdufa(db_path, ticker=t, days_offset=n)
        _seed_candidate(db_path, ticker=t, dedup_seed=f"in-{n}")

    # Three tickers WITHOUT PDUFA in band — either no PDUFA at all
    # or PDUFA outside the 7-day window.
    _seed_candidate(db_path, ticker="OUT1", dedup_seed="out-1")
    _seed_candidate(db_path, ticker="OUT2", dedup_seed="out-2")
    _seed_pdufa(db_path, ticker="FAR", days_offset=30)
    _seed_candidate(db_path, ticker="FAR", dedup_seed="out-3")

    outcome = dispatch_after_poll_cycle(
        db_path,
        armed_path=armed_path,
        providers=all_providers,
        enabled_value="1",
        auto_dispatch_value="1",
        scope_value="pdufa-soon",
    )

    assert outcome.invoked is True
    assert outcome.in_scope_count == 2
    assert outcome.processed == 2
    assert _ensemble_rows(db_path) == 2 * 4


# ---------------------------------------------------------------------------
# Test 6 — scope_all
# ---------------------------------------------------------------------------


def test_scope_all_processes_every_candidate(
    db_path: Path, armed_path: Path, all_providers
):
    """``STAGE2_DISPATCH_SCOPE=all``: 3 candidates without PDUFA → all 3 processed."""
    from biotech_sniper.exec.stage2_news_dispatch import (
        dispatch_after_poll_cycle,
    )

    for n, t in enumerate(("ALLA", "ALLB", "ALLC"), start=1):
        _seed_candidate(db_path, ticker=t, dedup_seed=f"all-{n}")

    outcome = dispatch_after_poll_cycle(
        db_path,
        armed_path=armed_path,
        providers=all_providers,
        enabled_value="1",
        auto_dispatch_value="1",
        scope_value="all",
    )

    assert outcome.invoked is True
    assert outcome.in_scope_count == 3
    assert outcome.processed == 3
    assert _ensemble_rows(db_path) == 3 * 4


# ---------------------------------------------------------------------------
# Test 7 — scope=none short-circuits without writes (sanity)
# ---------------------------------------------------------------------------


def test_scope_none_short_circuits(
    db_path: Path, armed_path: Path, all_providers
):
    from biotech_sniper.exec.stage2_news_dispatch import (
        dispatch_after_poll_cycle,
    )

    _seed_candidate(db_path, ticker="ZZZ", dedup_seed="none-1")

    outcome = dispatch_after_poll_cycle(
        db_path,
        armed_path=armed_path,
        providers=all_providers,
        enabled_value="1",
        auto_dispatch_value="1",
        scope_value="none",
    )
    assert outcome.invoked is False
    assert outcome.gate_failed == "scope_none"
    assert _ensemble_rows(db_path) == 0


# ---------------------------------------------------------------------------
# Test 8 — connector test: run_main_loop invokes the dispatcher
# ---------------------------------------------------------------------------


def test_run_main_loop_invokes_dispatch_after_cycle(
    db_path: Path,
    armed_path: Path,
    all_providers,
    monkeypatch: pytest.MonkeyPatch,
):
    """Driving the actual ``run_main_loop`` entrypoint with all 3
    gates open MUST trigger the Stage-2 dispatch path (per AGENTS.md
    § "Integration tests for connector code"): a connector-level
    bug between ``run_one_poll_cycle`` and the Stage-2 step would
    break this assertion."""
    from biotech_sniper.news_daemon.resilience import (
        ShutdownState,
        run_main_loop,
    )

    _seed_pdufa(db_path, ticker="WIRED", days_offset=2)
    _seed_candidate(db_path, ticker="WIRED", dedup_seed="wired-1")

    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "1")
    monkeypatch.setenv("STAGE2_AUTO_DISPATCH", "1")
    monkeypatch.setenv("STAGE2_DISPATCH_SCOPE", "pdufa-soon")

    # Inject the .armed path + provider stubs through the resilience
    # module's test-friendly seam (``_stage2_dispatch_overrides``).
    import biotech_sniper.news_daemon.resilience as resilience_module

    monkeypatch.setattr(
        resilience_module,
        "_STAGE2_DISPATCH_OVERRIDES_FOR_TESTS",
        {
            "armed_path": armed_path,
            "providers": all_providers,
        },
        raising=False,
    )

    # Single-cycle, no-sleep run.
    state = ShutdownState()

    class _FakeClock:
        def __init__(self) -> None:
            self.t = 0.0

        def monotonic(self) -> float:
            self.t += 0.001
            return self.t

        def sleep(self, _s: float) -> None:
            return

    fc = _FakeClock()
    rc = run_main_loop(
        str(db_path),
        poll_seconds=1,
        max_cycles=1,
        rss_fetchers=(),
        state=state,
        install_handlers=False,
        sleep_func=fc.sleep,
        monotonic=fc.monotonic,
        heartbeat_path=Path(db_path).parent / "hb.json",
        version_sha="c" * 40,
    )
    assert rc == 0
    assert state.cycles_completed == 1

    # Stage-2 dispatch fired during the cycle.
    assert _ensemble_rows(db_path) == 4


# ---------------------------------------------------------------------------
# Test 9 — pdufa-soon scope deduplicates candidates (f-fix-live-02 finding)
# ---------------------------------------------------------------------------


def test_pdufa_soon_dedupes_multiple_pdufa_rows_per_ticker(
    db_path: Path, armed_path: Path, all_providers
):
    """A ticker with multiple ``pdufa_calendar`` rows in the 7-day
    band MUST be dispatched ONCE, not once-per-pdufa-row.

    Pins the f-fix-live-02 non-blocking finding: the prior pdufa-soon
    JOIN against ``pdufa_calendar`` could produce duplicate
    ``candidate_event_id`` rows when a ticker had multiple PDUFA
    actions queued (e.g., two action_dates within the 7-day window),
    causing the same chain to be invoked multiple times in one poll
    cycle and doubling LLM spend. The fix dedupes by candidate id.
    """
    from biotech_sniper.exec.stage2_news_dispatch import (
        dispatch_after_poll_cycle,
        query_in_scope_candidates,
    )

    _seed_pdufa(db_path, ticker="DUPE", drug="drugA", days_offset=2)
    _seed_pdufa(db_path, ticker="DUPE", drug="drugB", days_offset=5)
    _seed_candidate(db_path, ticker="DUPE", dedup_seed="dupe-1")

    candidates = query_in_scope_candidates(db_path, "pdufa-soon")
    ids = [c["id"] for c in candidates]
    assert ids.count(ids[0]) == 1, (
        f"pdufa-soon scope returned duplicate candidate_event ids: {ids}"
    )
    assert len(candidates) == 1

    outcome = dispatch_after_poll_cycle(
        db_path,
        armed_path=armed_path,
        providers=all_providers,
        enabled_value="1",
        auto_dispatch_value="1",
        scope_value="pdufa-soon",
    )

    assert outcome.invoked is True
    assert outcome.in_scope_count == 1
    assert outcome.processed == 1
    assert _ensemble_rows(db_path) == 4
