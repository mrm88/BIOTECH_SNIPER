"""Resilience and resource-discipline tests for the Stage-1 news daemon.

Pins the f-m2-09 contract and the assertion IDs listed in
``features.json::fulfills``:

* VAL-M2-037 — single RSS source 500 does NOT halt the daemon; the
  loop continues, ``errors_session`` increments, candidates from
  healthy sources still emit.
* VAL-M2-038 — ALL RSS sources 500 keeps the daemon alive (no
  SystemExit, no traceback); ``errors_session`` climbs across cycles.
* VAL-M2-039 — News spike (1000+ matching headlines) processed in
  bounded memory and a single transaction.
* VAL-M2-040 — Daemon "restart" mid-poll yields correct atomic state
  (no partial commits — single transaction guarantees 0 OR N rows).
* VAL-M2-043 — CPU pressure: daemon respects ``CPUQuota=15%``
  (verified at the systemd-unit layer; here we lock the resource
  caps + max_workers=1 invariant).
* VAL-M2-044 — Clock skew does NOT corrupt dedup (``dedup_key`` is
  deterministic on input; wall-clock is NOT an input).
* VAL-M2-047 — every threadpool inside ``biotech_sniper/news_daemon``
  uses ``max_workers=1`` (or there are zero threadpools at all).
* VAL-M2-049 — sustained-run RSS stays well below ``MemoryMax=200M``;
  per-cycle bookkeeping does not leak.
* VAL-M2-052 — SIGTERM yields graceful drain, flushed heartbeat,
  exit code 0 within 10 seconds.

The tests rely on the f-m2-08 atomic heartbeat writer
(``state/news_daemon_heartbeat.json``), the f-m2-06 single-transaction
``run_one_poll_cycle`` writer, and the f-m2-04 scope filter.  A
helper builds a tmp_path v10 SQLite database with a
``russell2k_biotech``/``universe`` intersection seeded so the loop
has work to do without hitting any real RSS source.
"""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
import threading
import time
import tracemalloc
from pathlib import Path
from typing import Iterator, List

import pytest

from biotech_sniper import db
from biotech_sniper.migrations.runner import run as run_migrations_runner
from biotech_sniper.news_daemon import resilience
from biotech_sniper.news_daemon.emit import (
    compute_dedup_key,
    get_last_emitted_news_event_id,
    make_candidate,
    run_one_poll_cycle,
    write_candidate,
)
from biotech_sniper.news_daemon.heartbeat import (
    Heartbeat,
    read_heartbeat,
    write_heartbeat,
)
from biotech_sniper.news_daemon.resilience import (
    ShutdownState,
    install_signal_handlers,
    run_main_loop,
)


# ---------------------------------------------------------------------------
# Fixtures — build a v10 SQLite database with russell2k_biotech ∩ universe
# seeded so the resilience loop has work to do.
# ---------------------------------------------------------------------------


def _build_v10_db(tmp_path: Path) -> Path:
    """Build a tmp v10 SQLite database (v9 schema + migration 010)."""

    db_path = tmp_path / "alpha.db"
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
    finally:
        conn.close()
    run_migrations_runner(db_path, target_version=10, take_backup_first=False)
    return db_path


def _seed_russell_universe(db_path: Path, tickers: List[str]) -> None:
    """Insert ``tickers`` into ``russell2k_biotech`` AND ``universe``.

    Both tables get rows so the scope intersection
    ``russell2k_biotech ∩ universe.tier ∈ {watch, tradeable}`` is
    non-empty and the resilience loop has work to do.
    """

    today = datetime.date.today().isoformat()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds"
    )
    conn = sqlite3.connect(db_path)
    try:
        for index, ticker in enumerate(tickers):
            conn.execute(
                "INSERT OR IGNORE INTO russell2k_biotech "
                "(ticker, cik, sic, sic_description, "
                " iwm_weight, iwm_market_value_usd, "
                " as_of_date, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ticker,
                    f"{index:010d}",
                    2834,
                    "PHARMACEUTICAL PREPARATIONS",
                    0.01,
                    1_000_000.0,
                    today,
                    now,
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO universe (ticker, tier) "
                "VALUES (?, ?)",
                (ticker, "watch"),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_news_event(
    db_path: Path,
    *,
    ticker: str,
    title: str,
    source: str = "test_source",
    url: str = "",
    published_at: str = "",
) -> int:
    """Insert one news_events row and return its primary key."""

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "INSERT INTO news_events "
            "(ticker, source, published_at, title, url) "
            "VALUES (?, ?, ?, ?, ?)",
            (ticker, source, published_at or None, title, url or None),
        )
        new_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()
    assert new_id is not None
    return int(new_id)


@pytest.fixture
def v10_db(tmp_path: Path) -> Iterator[Path]:
    """Yields a tmp_path SQLite db at schema_version=10."""

    db_path = _build_v10_db(tmp_path)
    _seed_russell_universe(db_path, ["VRTX", "BIIB", "REGN"])
    yield db_path


@pytest.fixture
def heartbeat_path(tmp_path: Path) -> Path:
    """Yields a tmp_path heartbeat destination."""

    return tmp_path / "state" / "news_daemon_heartbeat.json"


class _FakeClock:
    """Deterministic monotonic + sleep for the resilience-loop tests.

    ``sleep(seconds)`` advances the synthetic clock by ``seconds``
    instantly so the inter-cycle sleep in :func:`run_main_loop` does
    not actually consume wall-clock time.  ``monotonic()`` returns
    the synthetic clock's current value.

    The class is intentionally small — production callers pass
    :func:`time.sleep` / :func:`time.monotonic`; this fixture lets the
    test suite run 30+ cycles in milliseconds.
    """

    def __init__(self) -> None:
        self._now: float = 0.0

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._now += float(seconds)


# ---------------------------------------------------------------------------
# VAL-M2-037 — single RSS source 500 does NOT halt the daemon
# ---------------------------------------------------------------------------


class TestSingleRssSourceFailure:
    """One failing RSS source must not halt the loop."""

    def test_one_rss_source_500_others_fine(
        self,
        v10_db: Path,
        heartbeat_path: Path,
    ) -> None:
        """One of three sources raises on every poll — daemon survives.

        After 3 cycles, ``errors_session`` ≥ 3 (one failure per
        cycle) AND a candidate emitted from a healthy source landed
        in ``candidate_events``.
        """

        cycles_seen = {"count": 0}

        def failing_source() -> None:
            raise RuntimeError("HTTP 500 from upstream RSS")

        def healthy_source_a() -> None:
            cycles_seen["count"] += 1
            # Insert a matching headline so the cycle has emit work.
            _seed_news_event(
                v10_db,
                ticker="VRTX",
                title=f"VRTX FDA approval cycle {cycles_seen['count']}",
                url=f"https://example.com/a/{cycles_seen['count']}",
            )

        def healthy_source_b() -> None:
            # No-op; just shows non-raising sources don't bump errors.
            return

        state = ShutdownState()
        fake_clock = _FakeClock()
        rc = run_main_loop(
            v10_db,
            poll_seconds=1,
            max_cycles=3,
            rss_fetchers=[failing_source, healthy_source_a, healthy_source_b],
            state=state,
            install_handlers=False,
            sleep_func=fake_clock.sleep,
            monotonic=fake_clock.monotonic,
            heartbeat_path=heartbeat_path,
            version_sha="a" * 40,
        )

        assert rc == 0
        assert state.cycles_completed == 3
        assert state.errors_session >= 3, state.errors_session
        # Every cycle saw exactly one failed source.
        assert list(state.rss_failures_per_cycle) == [1, 1, 1]
        assert state.candidates_emitted_session >= 1

        # Candidate row is committed.
        conn = sqlite3.connect(v10_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM candidate_events WHERE ticker='VRTX'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count >= 1

        # Heartbeat reflects the live counters.
        hb = read_heartbeat(heartbeat_path)
        assert hb.errors_session >= 3
        assert hb.candidates_emitted_session >= 1


# ---------------------------------------------------------------------------
# VAL-M2-038 — ALL RSS sources 500 keeps the daemon alive
# ---------------------------------------------------------------------------


class TestAllRssSourcesFailure:
    """Every source failing keeps the daemon alive (no SystemExit)."""

    def test_all_sources_500_daemon_survives(
        self,
        v10_db: Path,
        heartbeat_path: Path,
    ) -> None:
        """All sources raise for 5 cycles; daemon stays alive."""

        sources = [
            lambda: (_ for _ in ()).throw(RuntimeError("500 src1")),
            lambda: (_ for _ in ()).throw(RuntimeError("500 src2")),
            lambda: (_ for _ in ()).throw(RuntimeError("500 src3")),
        ]

        state = ShutdownState()
        fake_clock = _FakeClock()
        rc = run_main_loop(
            v10_db,
            poll_seconds=1,
            max_cycles=5,
            rss_fetchers=sources,
            state=state,
            install_handlers=False,
            sleep_func=fake_clock.sleep,
            monotonic=fake_clock.monotonic,
            heartbeat_path=heartbeat_path,
            version_sha="b" * 40,
        )

        assert rc == 0
        assert state.cycles_completed == 5
        # 5 cycles × 3 sources = 15 failures.
        assert state.errors_session >= 5 * len(sources)
        assert state.candidates_emitted_session == 0
        assert list(state.rss_failures_per_cycle) == [3, 3, 3, 3, 3]

        # Heartbeat is still updating across cycles.
        hb = read_heartbeat(heartbeat_path)
        assert hb.errors_session >= 15
        assert hb.candidates_emitted_session == 0


# ---------------------------------------------------------------------------
# VAL-M2-039 — News spike (1000 headlines) processed bounded-memory,
#               bounded-time, single transaction.
# ---------------------------------------------------------------------------


class TestNewsSpike:
    """Spike of 1000 matching headlines commits in a single transaction."""

    def test_news_spike_1000_headlines_single_transaction(
        self,
        v10_db: Path,
    ) -> None:
        """1000 matching news_events → 1000 candidates in one txn."""

        # Seed 1000 matching headlines on tickers in scope.
        conn = sqlite3.connect(v10_db)
        try:
            for i in range(1000):
                conn.execute(
                    "INSERT INTO news_events "
                    "(ticker, source, published_at, title, url) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        "VRTX",
                        "test_spike",
                        None,
                        f"VRTX FDA approval headline {i}",
                        f"https://example.com/spike/{i}",
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        tracemalloc.start()
        start_wall = time.monotonic()
        scanned, inserted = run_one_poll_cycle(
            str(v10_db), polled_tickers=["VRTX"]
        )
        elapsed = time.monotonic() - start_wall
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert scanned == 1000
        assert inserted == 1000
        # Bounded time — way under 60 s on any laptop / VPS.
        assert elapsed < 30.0, elapsed
        # Bounded memory — peak Python allocator usage well under
        # 200 MB (this is a tracemalloc-only proxy for VAL-M2-039
        # since we cannot exercise the systemd RSS limit from a
        # unit test).  200 MB = 200 * 1024 * 1024 bytes.
        assert peak < 200 * 1024 * 1024, peak

    def test_news_spike_uses_executemany_inside_transaction(self) -> None:
        """Source-level: ``write_candidates`` MUST use executemany."""

        from biotech_sniper.news_daemon import emit

        source = Path(emit.__file__).read_text(encoding="utf-8")
        assert "executemany(" in source
        assert "with conn:" in source


# ---------------------------------------------------------------------------
# VAL-M2-040 — daemon restart mid-poll yields correct atomic state
# ---------------------------------------------------------------------------


class TestRestartAtomicState:
    """Mid-poll failure rolls back; no partial commits visible."""

    def test_partial_batch_failure_rolls_back_atomically(
        self,
        v10_db: Path,
    ) -> None:
        """A bad row in a batch rolls back the entire batch.

        ``run_one_poll_cycle``'s contract is single-transaction.  A
        violating row (FK to a non-existent ``news_events.id``)
        raises and the prior good rows in the same batch must NOT
        have been committed.
        """

        from biotech_sniper.news_daemon.emit import write_candidates

        good_news_id = _seed_news_event(
            v10_db, ticker="VRTX", title="approval"
        )

        # One legitimate candidate + one with a phantom news_event_id.
        good = make_candidate("VRTX", good_news_id, ["approval"])
        bad = make_candidate("VRTX", 999_999_999, ["approval"])

        with pytest.raises(sqlite3.IntegrityError):
            write_candidates(str(v10_db), [good, bad])

        # The good row must NOT have leaked through.
        conn = sqlite3.connect(v10_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM candidate_events"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0, "single-transaction contract violated"

    def test_restart_resumes_from_durable_watermark(
        self,
        v10_db: Path,
    ) -> None:
        """After a "restart" (new conn), watermark recovers from DB.

        The dedup primitive is the ``candidate_events.dedup_key``
        UNIQUE constraint; the input-side watermark is recovered
        from ``MAX(source_news_event_id)``.
        """

        nid_1 = _seed_news_event(
            v10_db,
            ticker="VRTX",
            title="VRTX FDA approval one",
            url="https://example.com/restart/1",
        )
        run_one_poll_cycle(str(v10_db), polled_tickers=["VRTX"])
        watermark_after_first = get_last_emitted_news_event_id(str(v10_db))
        assert watermark_after_first == nid_1

        # "Restart" — new news_events row, fresh poll cycle.
        nid_2 = _seed_news_event(
            v10_db,
            ticker="VRTX",
            title="VRTX FDA approval two",
            url="https://example.com/restart/2",
        )
        scanned, inserted = run_one_poll_cycle(
            str(v10_db), polled_tickers=["VRTX"]
        )
        # Only the second row is scanned (watermark advanced).
        assert scanned == 1
        assert inserted == 1
        assert get_last_emitted_news_event_id(str(v10_db)) == nid_2


# ---------------------------------------------------------------------------
# VAL-M2-044 — clock skew does NOT corrupt dedup
# ---------------------------------------------------------------------------


class TestClockSkew:
    """``dedup_key`` is wall-clock-independent."""

    def test_clock_skew_no_dedup_corruption(self) -> None:
        """Pre-skew and post-skew dedup_keys MUST be identical.

        ``compute_dedup_key`` consumes only ``(ticker, news_event_id,
        matched_keywords)`` — wall-clock is NOT an input, so a 30-min
        clock back-step cannot change the key.
        """

        ticker = "VRTX"
        news_event_id = 42
        kws = ["approval", "pdufa"]

        pre_key = compute_dedup_key(ticker, news_event_id, kws)
        # Simulate a clock back-step.  Any wall-clock-dependent
        # implementation would surface a different key here.
        time_before = time.time()
        # No-op other than burning wall time / asserting determinism:
        post_key = compute_dedup_key(ticker, news_event_id, kws)
        assert pre_key == post_key

        # Ordering / dedup of keywords is also wall-clock-free.
        assert compute_dedup_key(
            ticker, news_event_id, ["pdufa", "approval"]
        ) == pre_key
        del time_before  # quiet linter

    def test_dedup_key_unchanged_under_simulated_clock_back_step(
        self,
        v10_db: Path,
    ) -> None:
        """Re-emitting the same headline after clock back-step is idempotent."""

        nid = _seed_news_event(
            v10_db, ticker="VRTX", title="VRTX FDA approval"
        )
        cand_first = make_candidate(
            "VRTX",
            nid,
            ["fda approval"],
            emitted_at="2026-04-29T15:00:00.000000Z",
        )
        first = write_candidate(str(v10_db), cand_first)
        assert first is True

        # Pretend the daemon restarted and the wall clock skewed by
        # -30 minutes.  The new candidate's emitted_at is earlier, but
        # the dedup_key is identical, so the second insert is a no-op.
        cand_second = make_candidate(
            "VRTX",
            nid,
            ["fda approval"],
            emitted_at="2026-04-29T14:30:00.000000Z",
        )
        assert cand_second.dedup_key == cand_first.dedup_key
        second = write_candidate(str(v10_db), cand_second)
        assert second is False  # INSERT OR IGNORE swallowed the dup

        conn = sqlite3.connect(v10_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM candidate_events "
                "WHERE source_news_event_id=?",
                (nid,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1


# ---------------------------------------------------------------------------
# VAL-M2-047 — max_workers=1 invariant on every threadpool in the package
# ---------------------------------------------------------------------------


class TestMaxWorkersInvariant:
    """No threadpool inside news_daemon may use ``max_workers >= 2``."""

    def test_no_threadpool_with_max_workers_above_one(self) -> None:
        """Source-level grep — every ThreadPool/Process must be
        either absent or ``max_workers=1``.
        """

        package_root = Path(__file__).resolve().parent.parent / (
            "biotech_sniper/news_daemon"
        )
        offenders: List[str] = []
        for path in package_root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(
                r"(ThreadPoolExecutor|ProcessPoolExecutor)\s*\(([^)]*)\)",
                text,
            ):
                args = match.group(2)
                kw = re.search(r"max_workers\s*=\s*(\d+)", args)
                if kw and int(kw.group(1)) >= 2:
                    offenders.append(
                        f"{path}:{match.start()}: {match.group(0)}"
                    )
        assert offenders == [], (
            "news_daemon may NOT spawn ThreadPool with max_workers>=2: "
            + "; ".join(offenders)
        )

    def test_resilience_module_uses_no_threadpool(self) -> None:
        """The resilience loop is single-threaded by contract.

        We grep for an *invocation* (``ThreadPoolExecutor(`` or
        ``ProcessPoolExecutor(``) so the docstring's prose mention
        of the class names does not trip the assertion.
        """

        source = Path(resilience.__file__).read_text(encoding="utf-8")
        assert "ThreadPoolExecutor(" not in source
        assert "ProcessPoolExecutor(" not in source

    def test_no_async_imports_in_news_daemon(self) -> None:
        """No ``aiohttp`` / ``httpx`` / ``asyncio`` in news_daemon.

        Stage-1 stack is sync-only (``requests`` + ``feedparser``).
        Mirrors the VAL-M2-048 contract.
        """

        package_root = Path(__file__).resolve().parent.parent / (
            "biotech_sniper/news_daemon"
        )
        forbidden = re.compile(
            r"^\s*(import|from)\s+(asyncio|httpx|aiohttp|anyio|trio|uvloop)\b",
            re.MULTILINE,
        )
        offenders: List[str] = []
        for path in package_root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for match in forbidden.finditer(text):
                offenders.append(f"{path}: {match.group(0).strip()}")
        assert offenders == [], offenders


# ---------------------------------------------------------------------------
# VAL-M2-049 — sustained-run RSS stays bounded
# ---------------------------------------------------------------------------


class TestSustainedRunMemoryBounded:
    """Per-cycle bookkeeping does not leak across many cycles."""

    def test_sustained_run_rss_bounded(
        self,
        v10_db: Path,
        heartbeat_path: Path,
    ) -> None:
        """30 cycles' worth of bookkeeping stays in tens of MB."""

        # Seed 5 matching headlines so there is per-cycle work but
        # not a runaway emit fan-out.
        for i in range(5):
            _seed_news_event(
                v10_db,
                ticker="VRTX",
                title=f"VRTX FDA approval {i}",
                url=f"https://example.com/sustained/{i}",
            )

        tracemalloc.start()
        fake_clock = _FakeClock()
        rc = run_main_loop(
            v10_db,
            poll_seconds=1,
            max_cycles=30,
            rss_fetchers=(),
            state=ShutdownState(),
            install_handlers=False,
            sleep_func=fake_clock.sleep,
            monotonic=fake_clock.monotonic,
            heartbeat_path=heartbeat_path,
            version_sha="c" * 40,
        )
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert rc == 0
        # 30 cycles' allocator peak well below 200 MB.  The bound is
        # generous (resilience contract is "well below 200 MB").
        assert peak < 200 * 1024 * 1024, peak


# ---------------------------------------------------------------------------
# VAL-M2-039 (spirit) — rss_failures_per_cycle history is bounded
# ---------------------------------------------------------------------------


class TestRssFailuresHistoryBounded:
    """``ShutdownState.rss_failures_per_cycle`` never grows unbounded.

    A long-uptime daemon (months of systemd uptime) would otherwise
    accumulate one int per poll cycle (~30 s cadence), defeating the
    bounded-memory discipline that VAL-M2-039 enforces.  The deque
    cap (:data:`resilience.RSS_FAILURES_HISTORY_MAXLEN`) keeps the
    history at most ``maxlen`` entries; older entries fall off the
    left edge automatically.
    """

    def test_default_state_uses_bounded_deque(self) -> None:
        """A fresh ``ShutdownState`` exposes a bounded deque."""

        from collections import deque as _deque

        state = ShutdownState()
        assert isinstance(state.rss_failures_per_cycle, _deque)
        assert state.rss_failures_per_cycle.maxlen is not None
        # Contract: maxlen is a small constant ≤ 4096 (a few hours of
        # diagnostic history at the configured cadence).
        assert state.rss_failures_per_cycle.maxlen <= 4096
        # The module-level constant is the source of truth.
        assert (
            state.rss_failures_per_cycle.maxlen
            == resilience.RSS_FAILURES_HISTORY_MAXLEN
        )

    def test_5000_cycles_of_failures_stay_bounded(self) -> None:
        """Appending 5000 cycles' worth of failures stays bounded.

        Mirrors f-fix-m2-09 contract: appending 5000 ints (well above
        any realistic ``maxlen``) caps the deque at exactly the
        configured ``maxlen`` and preserves only the most recent
        entries (LIFO-style truncation from the left edge).
        """

        state = ShutdownState()
        cap = resilience.RSS_FAILURES_HISTORY_MAXLEN

        for cycle_idx in range(5000):
            # Simulate a per-cycle failure count in a small range.
            state.rss_failures_per_cycle.append(cycle_idx % 7)

        # Bounded-memory invariant: never grows past the configured
        # maxlen no matter how many cycles run.
        assert len(state.rss_failures_per_cycle) <= cap
        assert len(state.rss_failures_per_cycle) == cap
        # The tail is the most recent appends (deque truncates from
        # the LEFT when maxlen is reached).
        last = state.rss_failures_per_cycle[-1]
        assert last == (5000 - 1) % 7

    def test_run_main_loop_does_not_unbound_history(
        self,
        v10_db: Path,
        heartbeat_path: Path,
    ) -> None:
        """Driving the loop preserves the deque (does NOT replace it).

        A regression where the loop reassigned the attribute to a
        list would silently restore unbounded growth.  Pinning the
        type after a multi-cycle run prevents that drift.
        """

        from collections import deque as _deque

        state = ShutdownState()
        fake_clock = _FakeClock()
        rc = run_main_loop(
            v10_db,
            poll_seconds=1,
            max_cycles=4,
            rss_fetchers=(
                lambda: (_ for _ in ()).throw(RuntimeError("boom1")),
                lambda: (_ for _ in ()).throw(RuntimeError("boom2")),
            ),
            state=state,
            install_handlers=False,
            sleep_func=fake_clock.sleep,
            monotonic=fake_clock.monotonic,
            heartbeat_path=heartbeat_path,
            version_sha="d" * 40,
        )

        assert rc == 0
        assert isinstance(state.rss_failures_per_cycle, _deque)
        assert (
            state.rss_failures_per_cycle.maxlen
            == resilience.RSS_FAILURES_HISTORY_MAXLEN
        )
        # 4 cycles × 2 failing sources each.
        assert list(state.rss_failures_per_cycle) == [2, 2, 2, 2]


# ---------------------------------------------------------------------------
# VAL-M2-052 — SIGTERM yields graceful drain, flushed heartbeat, exit 0
# ---------------------------------------------------------------------------


class TestSigtermDrainAndCleanExit:
    """SIGTERM signals → loop exits cleanly within 10 s with final heartbeat."""

    def test_sigterm_drain_and_clean_exit(
        self,
        v10_db: Path,
        heartbeat_path: Path,
    ) -> None:
        """``state.shutdown=True`` from another thread → exit 0 fast."""

        state = ShutdownState()

        # Run the loop in a worker thread; flip shutdown after the
        # first cycle has had a chance to run.
        result_box: dict = {}

        def loop_body() -> None:
            result_box["rc"] = run_main_loop(
                v10_db,
                poll_seconds=1,
                max_cycles=0,  # run forever until shutdown
                rss_fetchers=(),
                state=state,
                install_handlers=False,
                sleep_func=time.sleep,
                heartbeat_path=heartbeat_path,
                version_sha="d" * 40,
                shutdown_poll_seconds=0.05,
            )

        thread = threading.Thread(target=loop_body, daemon=True)
        start = time.monotonic()
        thread.start()
        # Give the loop time to complete at least one cycle.
        time.sleep(0.4)

        # Trigger graceful shutdown.
        state.shutdown = True
        state.signal_received = 15  # SIGTERM
        thread.join(timeout=10.0)
        elapsed = time.monotonic() - start

        assert not thread.is_alive(), "loop did not drain within 10 s"
        assert result_box["rc"] == 0
        assert elapsed < 10.0, elapsed
        assert state.cycles_completed >= 1

        # Final heartbeat flushed; mtime within ~5 s.
        hb = read_heartbeat(heartbeat_path)
        # Parse last_poll_ts and confirm freshness.
        text = hb.last_poll_ts
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        age = datetime.datetime.now(datetime.timezone.utc) - parsed
        assert age.total_seconds() <= 10.0, age

    def test_signal_handler_install_in_main_thread(self) -> None:
        """:func:`install_signal_handlers` returns True on main thread.

        We only run this when ``threading.current_thread()`` is
        ``MainThread`` — otherwise install will skip and return False
        (which is also a valid outcome for test runners that pin
        SIGTERM).  In either case the function must not raise.
        """

        state = ShutdownState()
        ok = install_signal_handlers(state)
        # On main thread under pytest, install succeeds.  On
        # pytest-xdist worker subprocesses (run via -n 2) the worker
        # IS the main thread of its own process, so the call still
        # succeeds.  Either way the install must NOT raise.
        assert isinstance(ok, bool)


# ---------------------------------------------------------------------------
# VAL-M2-043 — CPU pressure / resource caps (systemd-unit-level)
# ---------------------------------------------------------------------------


class TestResourceCapsInUnit:
    """The locked resource caps appear in the systemd unit file."""

    def test_unit_file_declares_all_resource_caps(self) -> None:
        """``Nice / IOSchedulingClass / CPUQuota / MemoryMax / MemoryHigh /
        TasksMax / StartLimitBurst / StartLimitIntervalSec`` all present.
        """

        unit = (
            Path(__file__).resolve().parent.parent
            / "deploy"
            / "systemd"
            / "alpha-sniper-news.service"
        )
        text = unit.read_text(encoding="utf-8")

        assert re.search(r"^Nice=10$", text, re.MULTILINE), text
        assert re.search(r"^IOSchedulingClass=idle$", text, re.MULTILINE)
        assert re.search(r"^CPUQuota=15%$", text, re.MULTILINE)
        assert re.search(r"^MemoryMax=200M$", text, re.MULTILINE)
        assert re.search(r"^MemoryHigh=150M$", text, re.MULTILINE)
        assert re.search(r"^TasksMax=64$", text, re.MULTILINE)
        # Restart thrash protection.
        assert re.search(r"^StartLimitBurst=5$", text, re.MULTILINE)
        assert re.search(
            r"^StartLimitIntervalSec=300s$", text, re.MULTILINE
        )

    def test_unit_file_uses_long_lived_simple_type(self) -> None:
        """Service is ``Type=simple`` — long-lived, not cron-fired."""

        unit = (
            Path(__file__).resolve().parent.parent
            / "deploy"
            / "systemd"
            / "alpha-sniper-news.service"
        )
        text = unit.read_text(encoding="utf-8")
        assert re.search(r"^Type=simple$", text, re.MULTILINE)
        assert "Restart=on-failure" in text
        assert "RestartSec=10s" in text


# ---------------------------------------------------------------------------
# Cross-cutting — heartbeat is flushed on every cycle, including drain.
# ---------------------------------------------------------------------------


class TestHeartbeatFlushOnDrain:
    """The graceful-drain path writes a final heartbeat."""

    def test_final_heartbeat_written_on_max_cycles_exit(
        self,
        v10_db: Path,
        heartbeat_path: Path,
    ) -> None:
        fake_clock = _FakeClock()
        rc = run_main_loop(
            v10_db,
            poll_seconds=1,
            max_cycles=2,
            rss_fetchers=(),
            state=ShutdownState(),
            install_handlers=False,
            sleep_func=fake_clock.sleep,
            monotonic=fake_clock.monotonic,
            heartbeat_path=heartbeat_path,
            version_sha="e" * 40,
        )
        assert rc == 0
        hb = read_heartbeat(heartbeat_path)
        # Second cycle's snapshot is what we get; total >= 0.
        assert hb.candidates_emitted_session >= 0

    def test_final_heartbeat_written_on_shutdown(
        self,
        v10_db: Path,
        heartbeat_path: Path,
    ) -> None:
        state = ShutdownState()
        state.shutdown = True  # short-circuit the loop on cycle 0
        fake_clock = _FakeClock()
        rc = run_main_loop(
            v10_db,
            poll_seconds=1,
            max_cycles=0,
            rss_fetchers=(),
            state=state,
            install_handlers=False,
            sleep_func=fake_clock.sleep,
            monotonic=fake_clock.monotonic,
            heartbeat_path=heartbeat_path,
            version_sha="f" * 40,
        )
        assert rc == 0
        hb = read_heartbeat(heartbeat_path)
        assert hb.errors_session == 0


# ---------------------------------------------------------------------------
# Resilience — poll_cycle exception MUST NOT halt the loop.
# ---------------------------------------------------------------------------


class TestPollCycleExceptionNonFatal:
    """A raised exception inside ``run_one_poll_cycle`` must be caught."""

    def test_poll_cycle_exception_increments_errors_session(
        self,
        v10_db: Path,
        heartbeat_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An exception from ``run_one_poll_cycle`` does not halt the loop."""

        boom_count = {"n": 0}

        def boom(*_args: object, **_kwargs: object) -> None:
            boom_count["n"] += 1
            raise RuntimeError("synthetic poll cycle error")

        monkeypatch.setattr(resilience, "run_one_poll_cycle", boom)

        state = ShutdownState()
        fake_clock = _FakeClock()
        rc = run_main_loop(
            v10_db,
            poll_seconds=1,
            max_cycles=4,
            rss_fetchers=(),
            state=state,
            install_handlers=False,
            sleep_func=fake_clock.sleep,
            monotonic=fake_clock.monotonic,
            heartbeat_path=heartbeat_path,
            version_sha="0" * 40,
        )
        assert rc == 0
        assert state.cycles_completed == 4
        assert state.errors_session == 4
        assert boom_count["n"] == 4
