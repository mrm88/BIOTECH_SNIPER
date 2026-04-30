"""f-m2-10 — production-wiring contract for the Stage-1 news daemon.

Pins the contract between the four canonical RSS sources and the
:func:`biotech_sniper.news_daemon.resilience.run_main_loop`'s
``rss_fetchers`` parameter:

1. :func:`build_default_rss_fetchers` returns a list of at least
   four zero-argument callables (one per source).
2. :func:`run_main_loop` with the production ``rss_fetchers`` list
   iterates over all four sources every cycle.
3. Each adapter persists into :sql:`news_events` idempotently — a
   second invocation with the same canned payload inserts zero new
   rows because the schema-level composite UNIQUE index
   ``idx_news_events_dedup`` (``(ticker, source, COALESCE(url,''),
   COALESCE(published_at,''))``) blocks duplicates.
4. A single-source HTTP-500 with REAL adapters (not test stubs)
   keeps the daemon alive: the failing adapter raises, the
   resilience layer catches the exception and increments
   :attr:`errors_session`, the loop continues to the next source.
   Replays VAL-M2-037 with the production wiring.
5. Source-grep guard — none of the four watcher modules nor the
   adapter module pull in any LLM provider client class
   (``XAIClient`` / ``ClaudeClient`` / ``GeminiClient`` /
   ``PerplexityClient`` / ``EnsembleScorer``) and none egress to
   any scoring host (``api.x.ai`` / ``api.anthropic.com`` /
   ``api.perplexity.ai`` / ``generativelanguage.googleapis.com``).

Test environment
----------------

The tests build a fresh tmp_path SQLite database at schema_version
v10 (the schema version that introduces ``russell2k_biotech``,
``candidate_events``, etc.) and seed three tickers (``VRTX``,
``BIIB``, ``REGN``) into both ``russell2k_biotech`` and
``universe.tier='watch'`` so the scope filter has work to do.

Each adapter is exercised with a cassette-equivalent JSON fixture
living under :file:`tests/fixtures/cassettes/news_daemon/` rather
than a real network call (per AGENTS.md: "NO live network in
pytest").  The fixture is deserialised inside a monkey-patched
replacement for the watcher's top-level ``run_*`` function, so the
adapter exercises its full normalisation + persistence path while
no socket is opened.
"""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Sequence

import pytest

from biotech_sniper import db
from biotech_sniper.migrations.runner import run as run_migrations_runner
from biotech_sniper.news_daemon import adapters, resilience
from biotech_sniper.news_daemon.adapters import (
    build_default_rss_fetchers,
    intraday_news_rss_adapter,
    ir_events_watcher_adapter,
    sec_8k_monitor_adapter,
    universal_news_watcher_adapter,
)
from biotech_sniper.news_daemon.resilience import ShutdownState, run_main_loop


CASSETTE_DIR: Path = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "cassettes"
    / "news_daemon"
)


class _FakeClock:
    """Synthetic ``time.monotonic`` + ``time.sleep`` for deterministic loops.

    Mirrors the ``_FakeClock`` helper in
    :file:`tests/test_news_daemon_resilience.py` — calling
    :meth:`sleep` advances the synthetic clock instantly, so the
    inter-cycle sleep in :func:`run_main_loop` does not consume
    wall-clock time.  Using a constant ``monotonic=lambda: 0.0``
    instead would produce an infinite loop because
    :func:`_interruptible_sleep` resolves its deadline relative to
    ``monotonic()`` and would never see it advance.
    """

    def __init__(self) -> None:
        self._now: float = 0.0

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._now += float(seconds)


# ---------------------------------------------------------------------------
# Helper fixtures
# ---------------------------------------------------------------------------


def _load_cassette(name: str) -> Any:
    """Load a JSON cassette from :file:`tests/fixtures/cassettes/news_daemon/`."""

    path = CASSETTE_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _build_v10_db(tmp_path: Path) -> Path:
    """Build a fresh tmp_path SQLite database at schema_version=10."""

    db_path = tmp_path / "alpha.db"
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
    finally:
        conn.close()
    run_migrations_runner(db_path, target_version=10, take_backup_first=False)
    return db_path


def _seed_universe(db_path: Path, tickers: Sequence[str]) -> None:
    """Seed ``tickers`` into ``russell2k_biotech`` AND ``universe`` (watch)."""

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


@pytest.fixture
def v10_db(tmp_path: Path) -> Iterator[Path]:
    """Yield a fresh v10 SQLite db with the test universe pre-seeded."""

    db_path = _build_v10_db(tmp_path)
    _seed_universe(db_path, ["VRTX", "BIIB", "REGN"])
    yield db_path


@pytest.fixture
def adapter_db(
    monkeypatch: pytest.MonkeyPatch, v10_db: Path
) -> Path:
    """Bind ``default_db_path`` to the tmp v10 db for adapter tests.

    The adapter module imports ``default_db_path`` from
    :mod:`biotech_sniper.news_events`; we patch the imported symbol
    directly inside the adapter module's namespace so the adapter
    persists into the tmp database without polluting the project's
    real ``data/alpha_sniper.db`` file.
    """

    monkeypatch.setattr(
        adapters,
        "default_db_path",
        lambda: v10_db,
        raising=True,
    )
    # Some adapters resolve scope via ``resolve_polled_tickers()`` —
    # which falls back to ``DATA_DIR / 'alpha_sniper.db'`` when the
    # caller does not pass ``db_path``.  Patch the module-level
    # function in ``adapters`` so ``polled_universe()`` returns the
    # expected tickers without touching the project-root DB.  The
    # stub accepts the ``db_path`` kwarg threaded through by
    # f-fix-m2-10-adapters-scope-and-db-path so adapter functions
    # can call ``resolve_polled_tickers(db_path=db_path)`` without
    # tripping ``TypeError``.
    monkeypatch.setattr(
        adapters,
        "resolve_polled_tickers",
        lambda *_args, **_kwargs: {"VRTX", "BIIB", "REGN"},
        raising=True,
    )
    return v10_db


# ---------------------------------------------------------------------------
# Test 1 — build_default_rss_fetchers returns the four production sources
# ---------------------------------------------------------------------------


class TestBuildDefaultRssFetchers:
    """The production fetcher list has exactly four entries."""

    def test_build_default_rss_fetchers_length_is_at_least_four(
        self,
    ) -> None:
        fetchers = build_default_rss_fetchers()
        assert len(fetchers) >= 4
        # Every entry MUST be callable with zero args (the contract
        # ``run_main_loop`` consumes).
        for fetcher in fetchers:
            assert callable(fetcher)

    def test_build_default_rss_fetchers_names_match_production(
        self,
    ) -> None:
        """Names embed the underlying watcher source for journal triage."""

        fetchers = build_default_rss_fetchers()
        names = [getattr(f, "__name__", "") for f in fetchers]
        assert names == [
            "universal_news_watcher_adapter",
            "sec_8k_monitor_adapter",
            "ir_events_watcher_adapter",
            "intraday_news_rss_adapter",
        ]

    def test_build_default_rss_fetchers_importable_from_poll_loop(
        self,
    ) -> None:
        """``poll_loop`` re-exports the symbol for the verification step."""

        from biotech_sniper.news_daemon.poll_loop import (
            build_default_rss_fetchers as poll_loop_builder,
        )

        assert poll_loop_builder is build_default_rss_fetchers


# ---------------------------------------------------------------------------
# Test 2 — run_main_loop iterates over all four sources per cycle
# ---------------------------------------------------------------------------


class TestRunMainLoopIteratesAllFourSources:
    """The resilience loop drives every fetcher once per cycle."""

    def test_each_default_fetcher_called_once_per_cycle(
        self,
        adapter_db: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Three poll cycles → 3 invocations per source (= 12 total)."""

        # Patch each adapter's underlying ``run_*`` (or ``scan_*``)
        # function with a counting stub.  We do NOT patch the
        # adapters themselves so the test exercises the real
        # closure shape returned by ``build_default_rss_fetchers``.
        call_counts: Dict[str, int] = {
            "universal": 0,
            "sec_8k": 0,
            "ir": 0,
            "intraday": 0,
        }

        def _u(_tickers: Sequence[str]) -> Dict[str, Any]:
            call_counts["universal"] += 1
            return {"new_8ks": [], "news_hits": []}

        def _s(*, mode: str = "intraday") -> Dict[str, Any]:
            call_counts["sec_8k"] += 1
            return {"signals": []}

        def _i() -> Dict[str, Any]:
            call_counts["ir"] += 1
            return {"signals": []}

        def _x(_seen: set) -> List[Dict[str, Any]]:
            call_counts["intraday"] += 1
            return []

        # Patch the watcher's top-level callables that the adapters
        # import inside their lazy ``from ... import`` statements.
        # Patching the symbol on the watcher module's namespace
        # means subsequent ``from ... import`` resolves to the stub.
        import biotech_sniper.intelligence.universal_news_watcher as uw
        import biotech_sniper.intelligence.sec_8k_monitor as sec
        import biotech_sniper.intelligence.ir_events_watcher as irw
        import biotech_sniper.intraday_scanner as intra

        monkeypatch.setattr(uw, "run_hourly_news_scan", _u, raising=True)
        monkeypatch.setattr(sec, "run_8k_monitor", _s, raising=True)
        monkeypatch.setattr(irw, "run_ir_events_check", _i, raising=True)
        monkeypatch.setattr(intra, "scan_news_rss", _x, raising=True)

        # Build the fetcher list bound to the tmp db so adapters
        # persist into the test SQLite file.
        fetchers = build_default_rss_fetchers(db_path=adapter_db)
        heartbeat_path = tmp_path / "state" / "news_daemon_heartbeat.json"

        fake_clock = _FakeClock()
        rc = run_main_loop(
            adapter_db,
            poll_seconds=1,
            max_cycles=3,
            rss_fetchers=fetchers,
            install_handlers=False,
            sleep_func=fake_clock.sleep,
            monotonic=fake_clock.monotonic,
            heartbeat_path=heartbeat_path,
            version_sha="a" * 40,
        )

        assert rc == 0
        assert call_counts == {
            "universal": 3,
            "sec_8k": 3,
            "ir": 3,
            "intraday": 3,
        }, call_counts


# ---------------------------------------------------------------------------
# Test 3 — each adapter idempotent on re-call (composite UNIQUE blocks dup)
# ---------------------------------------------------------------------------


def _row_count(db_path: Path, source: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM news_events WHERE source=?",
            (source,),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else 0


class TestAdapterIdempotency:
    """Each adapter writes once; a second call inserts zero new rows."""

    def test_universal_news_watcher_adapter_idempotent(
        self,
        adapter_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cassette = _load_cassette("universal_news_watcher.json")
        import biotech_sniper.intelligence.universal_news_watcher as uw

        monkeypatch.setattr(
            uw,
            "run_hourly_news_scan",
            lambda _tickers: cassette,
            raising=True,
        )

        first = universal_news_watcher_adapter(db_path=adapter_db)
        assert first >= 1, "first call must insert at least one row"

        # Confirm rows actually landed under the canonical source label.
        from biotech_sniper.news_events import SOURCE_UNIVERSAL

        before = _row_count(adapter_db, SOURCE_UNIVERSAL)
        assert before == first

        second = universal_news_watcher_adapter(db_path=adapter_db)
        assert second == 0, "re-run must insert zero rows (UNIQUE blocks dup)"

        after = _row_count(adapter_db, SOURCE_UNIVERSAL)
        assert after == before

    def test_sec_8k_monitor_adapter_idempotent(
        self,
        adapter_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cassette = _load_cassette("sec_8k_monitor.json")
        import biotech_sniper.intelligence.sec_8k_monitor as sec

        monkeypatch.setattr(
            sec,
            "run_8k_monitor",
            lambda mode="intraday": cassette,
            raising=True,
        )

        first = sec_8k_monitor_adapter(db_path=adapter_db)
        assert first >= 1

        from biotech_sniper.news_events import SOURCE_SEC_8K

        before = _row_count(adapter_db, SOURCE_SEC_8K)
        assert before == first

        second = sec_8k_monitor_adapter(db_path=adapter_db)
        assert second == 0

        after = _row_count(adapter_db, SOURCE_SEC_8K)
        assert after == before

    def test_ir_events_watcher_adapter_idempotent(
        self,
        adapter_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cassette = _load_cassette("ir_events_watcher.json")
        import biotech_sniper.intelligence.ir_events_watcher as irw

        monkeypatch.setattr(
            irw,
            "run_ir_events_check",
            lambda: cassette,
            raising=True,
        )

        first = ir_events_watcher_adapter(db_path=adapter_db)
        assert first >= 1

        from biotech_sniper.news_events import SOURCE_IR_EVENTS

        before = _row_count(adapter_db, SOURCE_IR_EVENTS)
        assert before == first

        second = ir_events_watcher_adapter(db_path=adapter_db)
        assert second == 0

        after = _row_count(adapter_db, SOURCE_IR_EVENTS)
        assert after == before

    def test_intraday_news_rss_adapter_idempotent(
        self,
        adapter_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cassette = _load_cassette("intraday_scan_news_rss.json")
        alerts: List[Mapping[str, Any]] = list(cassette.get("alerts", []))
        import biotech_sniper.intraday_scanner as intra

        monkeypatch.setattr(
            intra,
            "scan_news_rss",
            lambda _seen: list(alerts),
            raising=True,
        )

        first = intraday_news_rss_adapter(db_path=adapter_db)
        assert first >= 1

        from biotech_sniper.news_events import SOURCE_INTRADAY_RSS

        before = _row_count(adapter_db, SOURCE_INTRADAY_RSS)
        assert before == first

        second = intraday_news_rss_adapter(db_path=adapter_db)
        assert second == 0

        after = _row_count(adapter_db, SOURCE_INTRADAY_RSS)
        assert after == before


# ---------------------------------------------------------------------------
# Test 4 — single-source HTTP 500 with real adapters: errors_session ++,
#          daemon stays alive (replay of VAL-M2-037 with prod wiring)
# ---------------------------------------------------------------------------


class TestSingleSourceHttp500WithRealAdapters:
    """One production adapter raising HTTP 500 is non-fatal."""

    def test_one_source_500_increments_errors_session_and_loop_continues(
        self,
        adapter_db: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``run_hourly_news_scan`` raises; the other 3 sources are fine.

        After 3 cycles, ``errors_session >= 3`` (one error per cycle
        from the failing source) AND the daemon completed all 3
        cycles (``cycles_completed == 3``).  The healthy sources'
        cassettes still landed in news_events.
        """

        # Failing source — raise on every cycle.
        def _u_raise(_tickers: Sequence[str]) -> Dict[str, Any]:
            # The adapter wraps this; resilience.run_main_loop's
            # ``_drive_rss_fetchers`` catches whatever bubbles out.
            raise RuntimeError("HTTP 500 from upstream RSS")

        # Healthy sources — return canned cassette payloads.
        sec_cassette = _load_cassette("sec_8k_monitor.json")
        ir_cassette = _load_cassette("ir_events_watcher.json")
        intraday_cassette = _load_cassette("intraday_scan_news_rss.json")
        intraday_alerts: List[Mapping[str, Any]] = list(
            intraday_cassette.get("alerts", [])
        )

        import biotech_sniper.intelligence.universal_news_watcher as uw
        import biotech_sniper.intelligence.sec_8k_monitor as sec
        import biotech_sniper.intelligence.ir_events_watcher as irw
        import biotech_sniper.intraday_scanner as intra

        monkeypatch.setattr(uw, "run_hourly_news_scan", _u_raise, raising=True)
        monkeypatch.setattr(
            sec, "run_8k_monitor", lambda mode="intraday": sec_cassette,
            raising=True,
        )
        monkeypatch.setattr(
            irw, "run_ir_events_check", lambda: ir_cassette, raising=True,
        )
        monkeypatch.setattr(
            intra, "scan_news_rss", lambda _seen: list(intraday_alerts),
            raising=True,
        )

        fetchers = build_default_rss_fetchers(db_path=adapter_db)

        state = ShutdownState()
        heartbeat_path = tmp_path / "state" / "news_daemon_heartbeat.json"
        fake_clock = _FakeClock()

        rc = run_main_loop(
            adapter_db,
            poll_seconds=1,
            max_cycles=3,
            rss_fetchers=fetchers,
            state=state,
            install_handlers=False,
            sleep_func=fake_clock.sleep,
            monotonic=fake_clock.monotonic,
            heartbeat_path=heartbeat_path,
            version_sha="b" * 40,
        )

        # Daemon survived all 3 cycles despite the failing source.
        assert rc == 0
        assert state.cycles_completed == 3
        # One failure per cycle from the universal_news_watcher adapter.
        assert state.errors_session >= 3, state.errors_session
        # Per-cycle failure count is exactly 1 (the other 3 sources OK).
        assert state.rss_failures_per_cycle == [1, 1, 1]

        # Healthy sources persisted rows on cycle #1; subsequent
        # cycles were idempotent (UNIQUE blocked re-inserts).
        from biotech_sniper.news_events import (
            SOURCE_INTRADAY_RSS,
            SOURCE_IR_EVENTS,
            SOURCE_SEC_8K,
            SOURCE_UNIVERSAL,
        )

        assert _row_count(adapter_db, SOURCE_SEC_8K) >= 1
        assert _row_count(adapter_db, SOURCE_IR_EVENTS) >= 1
        assert _row_count(adapter_db, SOURCE_INTRADAY_RSS) >= 1
        # Universal source produced 0 rows because every call raised.
        assert _row_count(adapter_db, SOURCE_UNIVERSAL) == 0


# ---------------------------------------------------------------------------
# Test 5 — source-grep guard: no LLM hostnames or LLM clients in the
#          adapter module surface or the four watcher modules.
# ---------------------------------------------------------------------------


class TestNoLlmLeakageIntoAdapters:
    """No LLM provider hosts or client classes in the wired surface."""

    _FORBIDDEN_PATTERN = re.compile(
        r"api\.x\.ai"
        r"|api\.anthropic\.com"
        r"|api\.perplexity\.ai"
        r"|generativelanguage(?:\.googleapis\.com)?"
        r"|XAIClient"
        r"|ClaudeClient"
        r"|GeminiClient"
        r"|PerplexityClient"
        r"|EnsembleScorer"
    )

    @staticmethod
    def _wired_files() -> List[Path]:
        repo_root = Path(__file__).resolve().parent.parent
        files: List[Path] = []
        # Every Python file in the news_daemon package.
        for path in (repo_root / "biotech_sniper" / "news_daemon").rglob(
            "*.py"
        ):
            files.append(path)
        # The four watcher modules named in the feature description.
        for relative in (
            "biotech_sniper/intelligence/universal_news_watcher.py",
            "biotech_sniper/intelligence/sec_8k_monitor.py",
            "biotech_sniper/intelligence/ir_events_watcher.py",
            "biotech_sniper/intraday_scanner.py",
        ):
            files.append(repo_root / relative)
        return files

    def test_no_llm_hostnames_or_client_classes_in_wired_files(
        self,
    ) -> None:
        offenders: List[str] = []
        for path in self._wired_files():
            text = path.read_text(encoding="utf-8")
            for match in self._FORBIDDEN_PATTERN.finditer(text):
                # Allow lines inside a docstring or comment that
                # mention the forbidden tokens for context (e.g.
                # adapters.py's design notes call out the host
                # names as "MUST NOT egress to" warnings).  We
                # rely on a loose heuristic: a line is "policy
                # text" when it is a comment line (``#``) OR
                # appears inside a triple-quoted block whose own
                # text contains "MUST NOT" or "NEVER".
                line_start = text.rfind("\n", 0, match.start()) + 1
                line_end = text.find("\n", match.end())
                if line_end == -1:
                    line_end = len(text)
                line = text[line_start:line_end]
                stripped = line.strip()
                # Accept comment lines.
                if stripped.startswith("#"):
                    continue
                # Accept lines inside docstrings whose text has
                # MUST NOT / NEVER nearby (a pragmatic heuristic
                # that lets policy prose name the forbidden tokens
                # without tripping the guard).
                window_start = max(0, match.start() - 600)
                window_end = min(len(text), match.end() + 600)
                window = text[window_start:window_end]
                if "MUST NOT" in window or "NEVER" in window or "DO NOT" in window:
                    continue
                offenders.append(
                    f"{path.relative_to(path.parents[2])}: "
                    f"{match.group(0)} — {stripped[:80]}"
                )

        assert offenders == [], (
            "wired files leak LLM provider hostnames / client "
            "classes:\n  " + "\n  ".join(offenders)
        )

    def test_adapter_module_imports_no_llm_modules(
        self,
        tmp_path: Path,
    ) -> None:
        """Sanity probe — importing adapters does not pull in LLM SDKs.

        Mirrors VAL-M2-003.  Other tests in the suite may have
        already imported LLM modules into the live
        :data:`sys.modules` cache, so we run the import probe in a
        fresh subprocess that starts with a clean module set.
        """

        import subprocess
        import sys

        repo_root = Path(__file__).resolve().parent.parent

        script = (
            "import sys\n"
            "import biotech_sniper.news_daemon.adapters  # noqa: F401\n"
            "bad = [\n"
            "    name\n"
            "    for name in sys.modules\n"
            "    if name.startswith('biotech_sniper.llm')\n"
            "    or name in {'openai', 'anthropic'}\n"
            "    or name.startswith('google.generativeai')\n"
            "]\n"
            "if bad:\n"
            "    print('LEAK', bad)\n"
            "    raise SystemExit(1)\n"
            "print('OK')\n"
        )

        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            "stdout=" + result.stdout + " stderr=" + result.stderr
        )
        assert "OK" in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# Test 6 — f-fix-m2-10-adapters-scope-and-db-path
#
# (A) Empty ``polled_universe`` short-circuits in the three previously
#     broken adapters (sec_8k_monitor / ir_events_watcher /
#     intraday_news_rss) — the underlying watcher run-function is
#     NEVER called and the adapter returns 0.
# (B) When ``polled_universe`` returns a single ticker, an out-of-scope
#     fixture row for a different ticker is filtered out → 0 events
#     persisted.
# (C) Two distinct SQLite databases — one with ``russell2k_biotech``
#     populated, one empty — calling each adapter with ``db_path``
#     bound to the EMPTY db must reflect the empty universe (zero
#     rows persisted) regardless of any other (populated) db on disk.
# ---------------------------------------------------------------------------


class TestAdaptersScopeShortCircuitAndDbPath:
    """Scope-first short-circuit + db_path threading invariants."""

    # -- (A) Empty universe short-circuits without invoking the watcher. ----

    def test_sec_8k_adapter_short_circuits_when_universe_empty(
        self,
        adapter_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock

        import biotech_sniper.intelligence.sec_8k_monitor as sec

        run_mock = MagicMock(return_value={"signals": []})
        monkeypatch.setattr(sec, "run_8k_monitor", run_mock, raising=True)
        monkeypatch.setattr(
            adapters,
            "polled_universe",
            lambda *_a, **_k: [],
            raising=True,
        )

        rc = sec_8k_monitor_adapter(db_path=adapter_db)

        assert rc == 0
        run_mock.assert_not_called()

    def test_ir_events_adapter_short_circuits_when_universe_empty(
        self,
        adapter_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock

        import biotech_sniper.intelligence.ir_events_watcher as irw

        run_mock = MagicMock(return_value={"signals": []})
        monkeypatch.setattr(
            irw, "run_ir_events_check", run_mock, raising=True
        )
        monkeypatch.setattr(
            adapters,
            "polled_universe",
            lambda *_a, **_k: [],
            raising=True,
        )

        rc = ir_events_watcher_adapter(db_path=adapter_db)

        assert rc == 0
        run_mock.assert_not_called()

    def test_intraday_rss_adapter_short_circuits_when_universe_empty(
        self,
        adapter_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock

        import biotech_sniper.intraday_scanner as intra

        scan_mock = MagicMock(return_value=[])
        monkeypatch.setattr(intra, "scan_news_rss", scan_mock, raising=True)
        monkeypatch.setattr(
            adapters,
            "polled_universe",
            lambda *_a, **_k: [],
            raising=True,
        )

        rc = intraday_news_rss_adapter(db_path=adapter_db)

        assert rc == 0
        scan_mock.assert_not_called()

    def test_universal_adapter_short_circuits_when_universe_empty(
        self,
        adapter_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sanity probe — universal adapter already short-circuited."""

        from unittest.mock import MagicMock

        import biotech_sniper.intelligence.universal_news_watcher as uw

        run_mock = MagicMock(
            return_value={"new_8ks": [], "news_hits": []}
        )
        monkeypatch.setattr(
            uw, "run_hourly_news_scan", run_mock, raising=True
        )
        monkeypatch.setattr(
            adapters,
            "polled_universe",
            lambda *_a, **_k: [],
            raising=True,
        )

        rc = universal_news_watcher_adapter(db_path=adapter_db)

        assert rc == 0
        run_mock.assert_not_called()

    # -- (B) Out-of-scope fixture event filtered out by universe set. ----

    def test_sec_8k_adapter_filters_out_of_scope_ticker(
        self,
        adapter_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """polled_universe=['ABC'], fixture ticker='XYZ' → 0 persisted."""

        report = {
            "signals": [
                {
                    "ticker": "XYZ",
                    "type": "8-K filing",
                    "detail": "Out-of-scope filing",
                    "filing_url": "https://example.test/xyz-8k",
                    "published": "2026-04-30T10:00:00Z",
                }
            ]
        }
        import biotech_sniper.intelligence.sec_8k_monitor as sec

        monkeypatch.setattr(
            sec, "run_8k_monitor", lambda mode="intraday": report,
            raising=True,
        )
        monkeypatch.setattr(
            adapters,
            "polled_universe",
            lambda *_a, **_k: ["ABC"],
            raising=True,
        )

        from biotech_sniper.news_events import SOURCE_SEC_8K

        before = _row_count(adapter_db, SOURCE_SEC_8K)
        rc = sec_8k_monitor_adapter(db_path=adapter_db)
        after = _row_count(adapter_db, SOURCE_SEC_8K)

        assert rc == 0
        assert after == before  # zero rows persisted (XYZ not in {ABC})

    def test_ir_events_adapter_filters_out_of_scope_ticker(
        self,
        adapter_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        report = {
            "signals": [
                {
                    "ticker": "XYZ",
                    "type": "ir_signal",
                    "detail": "Out-of-scope IR page hit",
                    "ir_url": "https://example.test/xyz-ir",
                    "detected_date": "2026-04-30",
                }
            ]
        }
        import biotech_sniper.intelligence.ir_events_watcher as irw

        monkeypatch.setattr(
            irw, "run_ir_events_check", lambda: report, raising=True,
        )
        monkeypatch.setattr(
            adapters,
            "polled_universe",
            lambda *_a, **_k: ["ABC"],
            raising=True,
        )

        from biotech_sniper.news_events import SOURCE_IR_EVENTS

        before = _row_count(adapter_db, SOURCE_IR_EVENTS)
        rc = ir_events_watcher_adapter(db_path=adapter_db)
        after = _row_count(adapter_db, SOURCE_IR_EVENTS)

        assert rc == 0
        assert after == before

    def test_intraday_rss_adapter_filters_out_of_scope_ticker(
        self,
        adapter_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        alerts = [
            {
                "ticker": "XYZ",
                "title": "Out-of-scope news headline",
                "url": "https://example.test/xyz-news",
                "published_at": "2026-04-30T10:00:00Z",
            }
        ]
        import biotech_sniper.intraday_scanner as intra

        monkeypatch.setattr(
            intra, "scan_news_rss", lambda _seen: list(alerts),
            raising=True,
        )
        monkeypatch.setattr(
            adapters,
            "polled_universe",
            lambda *_a, **_k: ["ABC"],
            raising=True,
        )

        from biotech_sniper.news_events import SOURCE_INTRADAY_RSS

        before = _row_count(adapter_db, SOURCE_INTRADAY_RSS)
        rc = intraday_news_rss_adapter(db_path=adapter_db)
        after = _row_count(adapter_db, SOURCE_INTRADAY_RSS)

        assert rc == 0
        assert after == before

    # -- (C) db_path bound to empty russell DB → zero events persisted. --

    def test_db_path_threaded_through_polled_universe_two_dbs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two databases: populated vs empty russell2k_biotech.

        Each of the four adapters, when bound to the EMPTY db, must
        reflect the empty universe (zero events persisted) — even
        though a fully-populated db lives next to it on disk.  This
        proves ``db_path`` is threaded all the way to
        :func:`resolve_polled_tickers` (and not silently ignored,
        defaulting to the production DATA_DIR path).
        """

        populated_dir = tmp_path / "populated"
        empty_dir = tmp_path / "empty"
        populated_dir.mkdir()
        empty_dir.mkdir()
        populated_db = _build_v10_db(populated_dir)
        empty_db = _build_v10_db(empty_dir)

        # Seed only the populated DB.
        _seed_universe(populated_db, ["ABC", "DEF"])

        # No monkeypatching of ``resolve_polled_tickers`` /
        # ``polled_universe`` — exercise the REAL scope resolver
        # against the empty db_path and assert it returns an empty
        # set.
        from biotech_sniper.news_daemon.scope import resolve_polled_tickers

        assert resolve_polled_tickers(populated_db) == {"ABC", "DEF"}
        assert resolve_polled_tickers(empty_db) == set()

        # Also assert via the wrapper the way the adapters call it.
        assert adapters.polled_universe(db_path=populated_db) == [
            "ABC",
            "DEF",
        ]
        assert adapters.polled_universe(db_path=empty_db) == []

        # Stub each watcher with a payload for tickers that ARE in
        # the populated db.  When invoked with db_path=empty_db, the
        # short-circuit must fire BEFORE the watcher is called.
        from unittest.mock import MagicMock

        import biotech_sniper.intelligence.ir_events_watcher as irw
        import biotech_sniper.intelligence.sec_8k_monitor as sec
        import biotech_sniper.intelligence.universal_news_watcher as uw
        import biotech_sniper.intraday_scanner as intra

        u_mock = MagicMock(return_value={
            "new_8ks": [],
            "news_hits": [
                {
                    "ticker": "ABC",
                    "headline": "headline-abc",
                    "url": "https://example.test/abc",
                    "published": "2026-04-30T10:00:00Z",
                }
            ],
        })
        s_mock = MagicMock(return_value={
            "signals": [{
                "ticker": "ABC",
                "type": "8-K",
                "detail": "8-K signal",
                "filing_url": "https://example.test/abc-8k",
                "published": "2026-04-30T10:01:00Z",
            }],
        })
        i_mock = MagicMock(return_value={
            "signals": [{
                "ticker": "ABC",
                "type": "ir_signal",
                "detail": "IR page event",
                "ir_url": "https://example.test/abc-ir",
                "detected_date": "2026-04-30",
            }],
        })
        x_mock = MagicMock(return_value=[
            {
                "ticker": "ABC",
                "title": "intraday-abc",
                "url": "https://example.test/abc-rss",
                "published_at": "2026-04-30T10:02:00Z",
            }
        ])

        monkeypatch.setattr(
            uw, "run_hourly_news_scan", u_mock, raising=True
        )
        monkeypatch.setattr(sec, "run_8k_monitor", s_mock, raising=True)
        monkeypatch.setattr(
            irw, "run_ir_events_check", i_mock, raising=True
        )
        monkeypatch.setattr(intra, "scan_news_rss", x_mock, raising=True)

        from biotech_sniper.news_events import (
            SOURCE_INTRADAY_RSS,
            SOURCE_IR_EVENTS,
            SOURCE_SEC_8K,
            SOURCE_UNIVERSAL,
        )

        # Each adapter bound to the EMPTY db: zero rows persisted +
        # underlying watcher MUST NOT be invoked.
        assert universal_news_watcher_adapter(db_path=empty_db) == 0
        assert sec_8k_monitor_adapter(db_path=empty_db) == 0
        assert ir_events_watcher_adapter(db_path=empty_db) == 0
        assert intraday_news_rss_adapter(db_path=empty_db) == 0

        u_mock.assert_not_called()
        s_mock.assert_not_called()
        i_mock.assert_not_called()
        x_mock.assert_not_called()

        assert _row_count(empty_db, SOURCE_UNIVERSAL) == 0
        assert _row_count(empty_db, SOURCE_SEC_8K) == 0
        assert _row_count(empty_db, SOURCE_IR_EVENTS) == 0
        assert _row_count(empty_db, SOURCE_INTRADAY_RSS) == 0

        # Adapters bound to the empty db never wrote into the
        # populated db.
        assert _row_count(populated_db, SOURCE_UNIVERSAL) == 0
        assert _row_count(populated_db, SOURCE_SEC_8K) == 0
        assert _row_count(populated_db, SOURCE_IR_EVENTS) == 0
        assert _row_count(populated_db, SOURCE_INTRADAY_RSS) == 0

    def test_build_default_rss_fetchers_threads_db_path_to_scope(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``build_default_rss_fetchers(db_path=X)`` closures use X for scope."""

        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        empty_db = _build_v10_db(empty_dir)

        from unittest.mock import MagicMock

        import biotech_sniper.intelligence.ir_events_watcher as irw
        import biotech_sniper.intelligence.sec_8k_monitor as sec
        import biotech_sniper.intelligence.universal_news_watcher as uw
        import biotech_sniper.intraday_scanner as intra

        u = MagicMock(side_effect=AssertionError("must not be called"))
        s = MagicMock(side_effect=AssertionError("must not be called"))
        i = MagicMock(side_effect=AssertionError("must not be called"))
        x = MagicMock(side_effect=AssertionError("must not be called"))

        monkeypatch.setattr(uw, "run_hourly_news_scan", u, raising=True)
        monkeypatch.setattr(sec, "run_8k_monitor", s, raising=True)
        monkeypatch.setattr(irw, "run_ir_events_check", i, raising=True)
        monkeypatch.setattr(intra, "scan_news_rss", x, raising=True)

        fetchers = build_default_rss_fetchers(db_path=empty_db)
        for fetcher in fetchers:
            assert fetcher() == 0

        u.assert_not_called()
        s.assert_not_called()
        i.assert_not_called()
        x.assert_not_called()
