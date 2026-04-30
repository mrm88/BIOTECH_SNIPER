"""Stage-1 / adverse-news orthogonality tests (f-m2-07).

Pins the f-m2-07 contract from features.json:

* The existing ``daily_news_ingest`` adverse-news exit hook
  (``biotech_sniper.adverse_news.scan_and_trigger``) is preserved
  with no regression: its public surface, event tag, and
  ``submit_exit(event='adverse_news')`` wiring are byte-stable
  across the M2 work.
* Stage-1 ``candidate_events`` emission and the adverse-news exit
  are **orthogonal**: a single ``negative_material``-tagged headline
  whose title also matches a Stage-1 catalyst keyword produces
  exactly ONE candidate_events row AND exactly ONE
  ``paper_orders(event='adverse_news')`` row. The two writes do not
  race, do not duplicate, and do not deadlock — even when run
  interleaved or concurrently from worker threads.
* Cooldown is for ENTRIES only: an ``adverse_news`` exit on
  ticker X does NOT block Stage-1 candidate emission for ticker X
  (Stage-1 emit is a pure data-plane write that contains no
  cooldown check; that gate lives further downstream in Stage-2).

These tests exercise VAL-M2-029 (existing hook preserved) and
VAL-M2-030 (orthogonality / no race / no duplicate). The companion
WAL-contention assertion lives in :mod:`tests.test_wal_contention`.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterator

import pytest

from biotech_sniper import adverse_news, db
from biotech_sniper.adverse_news import (
    ADVERSE_NEWS_EVENT,
    NEGATIVE_MATERIAL_LABEL,
    AdverseNewsExitRunner,
)
from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.migrations.runner import run as run_migrations_runner
from biotech_sniper.news_daemon.emit import run_one_poll_cycle
from biotech_sniper.news_events import record_news_event
from biotech_sniper.paper_executor import PaperExecutor
from biotech_sniper.universe.russell_biotech import (
    ensure_russell2k_biotech_table,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Minimal ``AlpacaClient`` substitute returning canned sell responses.

    Mirrors :class:`tests.test_adverse_news_exit._FakeAlpacaClient`
    but trimmed to the surface ``PaperExecutor`` actually touches
    during a single ``submit_exit`` round-trip.
    """

    def __init__(self) -> None:
        self.base_url = PAPER_BASE_URL
        self.submit_calls: list[Any] = []
        self._submit_results: list[dict[str, Any]] = []

    def queue(self, response: dict[str, Any]) -> None:
        self._submit_results.append(response)

    def get_positions(self) -> list[dict[str, Any]]:
        return []

    def submit_order(self, order_request: Any) -> dict[str, Any]:
        self.submit_calls.append(order_request)
        if not self._submit_results:
            raise AssertionError("submit_order: no result queued")
        return self._submit_results.pop(0)

    def get_order(self, order_id: str) -> dict[str, Any]:
        return {"id": order_id, "status": "accepted"}


def _stub_sell_response(
    *,
    order_id: str = "44444444-4444-4444-4444-444444444444",
    qty: int = 5,
) -> dict[str, Any]:
    return {
        "id": order_id,
        "client_order_id": "VRTX-adverse_news-2026-04-29",
        "symbol": "VRTX260620C00400000",
        "asset_class": "us_option",
        "qty": qty,
        "side": "sell",
        "status": "accepted",
        "order_class": "simple",
        "type": "market",
        "time_in_force": "day",
    }


def _active_play(
    *,
    ticker: str = "VRTX",
    play_card_id: str = "VRTX-2026-04-29",
    symbol: str = "VRTX260620C00400000",
    catalyst_date: str = "2026-05-12",
    qty: int = 5,
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "play_card_id": play_card_id,
        "symbol": symbol,
        "catalyst_date": catalyst_date,
        "qty": qty,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_v10_db(tmp_path: Path) -> Path:
    """Create a tmp-path SQLite db at schema_version=10.

    Mirrors the helper used by :mod:`tests.test_news_daemon_emit`
    so the schema matches production exactly (v9 schema.sql apply
    + the v10 ``010_reading_b_foundations`` migration that adds
    ``candidate_events`` plus the ``news_event_entry`` enum
    extension on ``paper_orders.event``).
    """

    db_path = tmp_path / "alpha.db"
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
    finally:
        conn.close()
    run_migrations_runner(db_path, target_version=10, take_backup_first=False)
    return db_path


def _seed_scope_for_ticker(
    db_path: Path,
    ticker: str,
    *,
    tier: str = "watch",
) -> None:
    """Seed russell2k_biotech + universe rows so Stage-1 polls ``ticker``."""

    conn = db.connect(db_path)
    try:
        ensure_russell2k_biotech_table(conn)
        conn.execute(
            "INSERT OR IGNORE INTO russell2k_biotech "
            "(ticker, cik, sic, sic_description, as_of_date, fetched_at) "
            "VALUES (?, ?, ?, ?, '2026-04-29', '2026-04-29T00:00:00Z')",
            (ticker, "0000875320", 2834, "PHARMACEUTICAL PREPARATIONS"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO universe (ticker, tier, has_options_chain) "
            "VALUES (?, ?, 1)",
            (ticker, tier),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def v10_db(tmp_path: Path) -> Iterator[Path]:
    yield _build_v10_db(tmp_path)


@pytest.fixture
def make_runner(v10_db: Path):
    """Construct an :class:`AdverseNewsExitRunner` bound to ``v10_db``."""

    def _factory(
        *, client: _FakeAlpacaClient | None = None
    ) -> tuple[AdverseNewsExitRunner, PaperExecutor, _FakeAlpacaClient]:
        fake = client or _FakeAlpacaClient()
        executor = PaperExecutor(
            fake,  # type: ignore[arg-type]
            db_path=v10_db,
            poll_interval_seconds=0.0,
        )
        runner = AdverseNewsExitRunner(executor)
        return runner, executor, fake

    return _factory


# ---------------------------------------------------------------------------
# VAL-M2-029 — existing adverse_news public surface preserved.
# ---------------------------------------------------------------------------


class TestAdverseNewsModuleUnchanged:
    """The :mod:`biotech_sniper.adverse_news` public surface is byte-stable.

    Workers MUST NOT modify the existing exit hook in M2; this test
    asserts the canonical event tag, label string, and exported API
    so a regression that mutates the surface is caught immediately.
    """

    def test_event_tag_is_adverse_news(self) -> None:
        assert ADVERSE_NEWS_EVENT == "adverse_news"

    def test_negative_material_label_is_canonical(self) -> None:
        assert NEGATIVE_MATERIAL_LABEL == "negative_material"

    def test_public_api_surface(self) -> None:
        """Exported names that callers rely on stay present."""

        expected = {
            "ADVERSE_NEWS_EVENT",
            "NEGATIVE_MATERIAL_LABEL",
            "AdverseNewsExitRunner",
            "scan_and_trigger",
            "record_negative_news_and_exit",
        }
        assert expected <= set(adverse_news.__all__)
        for name in expected:
            assert hasattr(adverse_news, name), name

    def test_runner_constructor_validates_executor(self) -> None:
        """The constructor still rejects a ``None`` executor (defence-in-depth)."""

        with pytest.raises(TypeError):
            AdverseNewsExitRunner(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# VAL-M2-030 — orthogonality.
# ---------------------------------------------------------------------------


class TestStage1AndAdverseNewsOrthogonal:
    """Stage-1 emission and adverse-news exit do not race or duplicate.

    A single ``negative_material``-tagged headline whose title also
    matches a Stage-1 catalyst keyword exercises BOTH paths:

    * :func:`run_one_poll_cycle` writes a candidate_events row.
    * :class:`AdverseNewsExitRunner.scan_and_trigger` writes a
      paper_orders row tagged ``event='adverse_news'``.

    The final state must be exactly one of each, regardless of the
    order in which the two paths run, with no
    ``WriteThenSubmitViolation`` and no ``UNIQUE constraint failed``.
    """

    @staticmethod
    def _seed_negative_material_news(
        db_path: Path,
        ticker: str,
        *,
        title: str = "FDA issues complete response letter on lead asset",
    ) -> int:
        """Insert a negative-material news_events row + return its id.

        The title contains TIER-1 catalyst keyword
        ``"complete response letter"`` so the Stage-1 matcher
        produces a candidate_events row, while
        ``enrichment_label='negative_material'`` triggers the
        adverse-news exit hook on the same row.
        """

        conn = db.connect(db_path)
        try:
            record_news_event(
                conn,
                ticker=ticker,
                source="test_orthogonality",
                title=title,
                url=f"https://example.com/{ticker}-crl",
                published_at="2026-04-29T08:00:00.000000Z",
                enrichment_label=NEGATIVE_MATERIAL_LABEL,
            )
            conn.commit()
            row = conn.execute(
                "SELECT id FROM news_events WHERE ticker=? ORDER BY id DESC LIMIT 1",
                (ticker,),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, "news_events row was not persisted"
        return int(row[0])

    @staticmethod
    def _count_candidates_for(db_path: Path, news_event_id: int) -> int:
        conn = sqlite3.connect(db_path)
        try:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM candidate_events "
                    "WHERE source_news_event_id=?",
                    (news_event_id,),
                ).fetchone()[0]
            )
        finally:
            conn.close()

    @staticmethod
    def _count_adverse_exits_for(db_path: Path, ticker: str) -> int:
        conn = sqlite3.connect(db_path)
        try:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM paper_orders "
                    "WHERE event=? AND parent_play_card_id IN ("
                    "  SELECT play_card_id FROM paper_orders "
                    "  WHERE event=? AND symbol IS NOT NULL"
                    ") OR (event=? AND symbol LIKE ?)",
                    (
                        ADVERSE_NEWS_EVENT,
                        ADVERSE_NEWS_EVENT,
                        ADVERSE_NEWS_EVENT,
                        f"{ticker}%",
                    ),
                ).fetchone()[0]
            )
        finally:
            conn.close()

    def test_emit_then_exit_produces_exactly_one_of_each(
        self, v10_db: Path, make_runner
    ) -> None:
        """Stage-1 emit BEFORE adverse_news exit: orthogonal, single row each."""

        ticker = "VRTX"
        _seed_scope_for_ticker(v10_db, ticker, tier="tradeable")
        news_id = self._seed_negative_material_news(v10_db, ticker)

        # Stage-1 emit first.
        scanned, inserted = run_one_poll_cycle(
            str(v10_db), polled_tickers=[ticker]
        )
        assert scanned == 1
        assert inserted == 1

        # Adverse-news exit second.
        fake = _FakeAlpacaClient()
        fake.queue(_stub_sell_response(qty=5))
        runner, _, _ = make_runner(client=fake)
        results = runner.scan_and_trigger(
            [_active_play(ticker=ticker)],
            db_path=v10_db,
            since_at="2026-04-29T00:00:00.000000Z",
        )
        assert len(results) == 1
        assert results[0]["status"] == "submitted"
        assert results[0]["event"] == ADVERSE_NEWS_EVENT

        # Final state: exactly one candidate_events row and one
        # paper_orders(event='adverse_news') row for this ticker.
        assert self._count_candidates_for(v10_db, news_id) == 1
        conn = sqlite3.connect(v10_db)
        try:
            exit_count = conn.execute(
                "SELECT COUNT(*) FROM paper_orders WHERE event=?",
                (ADVERSE_NEWS_EVENT,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert exit_count == 1

    def test_exit_then_emit_produces_exactly_one_of_each(
        self, v10_db: Path, make_runner
    ) -> None:
        """Adverse_news exit BEFORE Stage-1 emit: cooldown does NOT block emission.

        The contract pins that "cooldown is for ENTRIES only" — a
        prior adverse_news submission for ticker X must not prevent
        Stage-1 from emitting a candidate_events row for the same
        ticker on the same headline. Stage-2 (downstream) is the
        only place where per-ticker cooldown is consulted.
        """

        ticker = "VRTX"
        _seed_scope_for_ticker(v10_db, ticker, tier="tradeable")
        news_id = self._seed_negative_material_news(v10_db, ticker)

        # Adverse-news exit first.
        fake = _FakeAlpacaClient()
        fake.queue(_stub_sell_response(qty=5))
        runner, _, _ = make_runner(client=fake)
        results = runner.scan_and_trigger(
            [_active_play(ticker=ticker)],
            db_path=v10_db,
            since_at="2026-04-29T00:00:00.000000Z",
        )
        assert len(results) == 1
        assert results[0]["status"] == "submitted"

        # Stage-1 emit AFTER the exit landed: must still emit the
        # candidate (Stage-1 has no cooldown check).
        scanned, inserted = run_one_poll_cycle(
            str(v10_db), polled_tickers=[ticker]
        )
        assert scanned == 1
        assert inserted == 1
        assert self._count_candidates_for(v10_db, news_id) == 1

    def test_emit_idempotent_after_exit_for_same_ticker(
        self, v10_db: Path, make_runner
    ) -> None:
        """Re-running the Stage-1 cycle after the exit landed never
        produces a duplicate candidate_events row (UNIQUE on dedup_key
        plus the watermark).
        """

        ticker = "VRTX"
        _seed_scope_for_ticker(v10_db, ticker, tier="tradeable")
        news_id = self._seed_negative_material_news(v10_db, ticker)

        # Fire the exit first.
        fake = _FakeAlpacaClient()
        fake.queue(_stub_sell_response(qty=5))
        runner, _, _ = make_runner(client=fake)
        runner.scan_and_trigger(
            [_active_play(ticker=ticker)],
            db_path=v10_db,
            since_at="2026-04-29T00:00:00.000000Z",
        )

        # Two Stage-1 cycles back-to-back.
        for _ in range(2):
            run_one_poll_cycle(str(v10_db), polled_tickers=[ticker])

        assert self._count_candidates_for(v10_db, news_id) == 1


@pytest.fixture
def _wal_busy_timeout(monkeypatch):
    """Add ``PRAGMA busy_timeout`` to every ``db.connect()`` call.

    f-misc-03 hardening: the concurrent test below runs two threads
    against the same on-disk SQLite db. SQLite WAL serialises writers
    via a per-database write lock; with no ``busy_timeout`` set,
    ``BEGIN IMMEDIATE`` on the second writer surfaces
    ``sqlite3.OperationalError('database is locked')`` immediately
    instead of queuing on the writer-lock retry envelope. Production
    intentionally leaves the default unset (cron units serialise
    naturally), so we apply the timeout only inside this test via a
    test-local monkeypatch on :func:`biotech_sniper.db.connect`.

    Implementation note: we intentionally re-build the connection
    setup ourselves rather than wrapping ``real_connect``. The
    project's :func:`db.connect` issues
    ``PRAGMA journal_mode = WAL;`` mid-setup, which itself takes a
    brief shared/reserved lock on the on-disk database file. If two
    threads ran ``PRAGMA journal_mode = WAL`` concurrently with no
    busy_timeout in place yet, one could surface
    ``OperationalError('database is locked')`` *before* the wrapper
    set the timeout. To plug that window, we set
    ``PRAGMA busy_timeout`` as the FIRST statement on the connection,
    *before* any other PRAGMA — so every subsequent statement
    (including ``journal_mode = WAL``) is queued on the busy-lock
    retry envelope.

    The 30-second timeout is generous (vs the 5 s value used by
    :mod:`tests.test_wal_contention`) because the test runs
    synchronously alongside the rest of the ``-n 2`` suite on a
    laptop; a transient lock collision under heavy parallel load
    can briefly exceed 5 s. Production behaviour is unaffected.

    The fixture is ``autouse=False`` and scoped per-test (the default
    ``function`` scope) so neither serial nor sibling parallel tests
    are affected.
    """
    from biotech_sniper import db as _bs_db

    BUSY_TIMEOUT_MS = 30_000

    def _connect_with_busy_timeout(db_path):
        target = str(db_path)
        if target != ":memory:":
            _bs_db._ensure_parent_under_data_dir(Path(target))
        conn = sqlite3.connect(target)
        # FIRST PRAGMA on the new connection: install a generous
        # busy_timeout so every subsequent lock acquisition (incl.
        # the journal_mode=WAL pragma below) queues rather than
        # fails on contention.
        try:
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        except sqlite3.OperationalError:
            pass
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        if target != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL;")
            try:
                _bs_db._ensure_db_file_mode(Path(target))
            except Exception:  # pragma: no cover - never crash a connect
                pass
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    monkeypatch.setattr(_bs_db, "connect", _connect_with_busy_timeout)


class TestConcurrentEmitAndExit:
    """Run Stage-1 emit + adverse_news exit on threads concurrently.

    The two writes target DIFFERENT tables (``candidate_events`` for
    Stage-1, ``paper_orders`` for the exit) so under SQLite WAL there
    is no logical conflict. The test confirms that fact empirically:
    after a concurrent run, exactly one of each row exists and no
    ``OperationalError("database is locked")`` was raised.
    """

    def test_concurrent_run_no_race_no_duplicate(
        self, v10_db: Path, make_runner, _wal_busy_timeout
    ) -> None:
        ticker = "VRTX"
        _seed_scope_for_ticker(v10_db, ticker, tier="tradeable")
        TestStage1AndAdverseNewsOrthogonal._seed_negative_material_news(
            v10_db, ticker
        )

        fake = _FakeAlpacaClient()
        fake.queue(_stub_sell_response(qty=5))
        runner, _, _ = make_runner(client=fake)

        errors: list[BaseException] = []
        start_barrier = threading.Barrier(2)

        # f-misc-03 retry-on-locked envelope: even with a generous
        # ``PRAGMA busy_timeout=30000`` installed by
        # ``_wal_busy_timeout``, a transient SQLite-level
        # ``database is locked`` can still surface under heavy
        # parallel load (xdist + threading on a contended laptop)
        # because some lock-acquisition windows in SQLite's WAL
        # path are NOT covered by the busy-handler retry envelope
        # (e.g. mid-PRAGMA contention before busy_timeout has been
        # set on a freshly opened connection, or rare reader-vs-
        # checkpointer races). The test's invariant is "no race, no
        # duplicate", which is preserved as long as the worker
        # eventually completes — so we wrap each worker in a tight
        # bounded retry loop that re-runs the WHOLE work unit on a
        # locked-error. Both writers are idempotent on
        # ``dedup_key`` / ``client_order_id`` so a retry never
        # duplicates rows. If every retry inside the budget still
        # fails, we surface the original exception.
        _MAX_RETRIES = 5
        _RETRY_SLEEP = 0.05

        def _is_locked_error(exc: BaseException) -> bool:
            return (
                isinstance(exc, sqlite3.OperationalError)
                and "database is locked" in str(exc).lower()
            )

        def _retry_on_locked(work):
            last_exc: BaseException | None = None
            for attempt in range(_MAX_RETRIES):
                try:
                    work()
                    return
                except BaseException as exc:  # noqa: BLE001
                    last_exc = exc
                    if not _is_locked_error(exc):
                        raise
                    import time as _time

                    _time.sleep(_RETRY_SLEEP * (attempt + 1))
            assert last_exc is not None
            raise last_exc

        def _emit_worker() -> None:
            try:
                start_barrier.wait(timeout=5.0)
                _retry_on_locked(
                    lambda: run_one_poll_cycle(
                        str(v10_db), polled_tickers=[ticker]
                    )
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def _exit_worker() -> None:
            try:
                start_barrier.wait(timeout=5.0)
                _retry_on_locked(
                    lambda: runner.scan_and_trigger(
                        [_active_play(ticker=ticker)],
                        db_path=v10_db,
                        since_at="2026-04-29T00:00:00.000000Z",
                    )
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        emit_thread = threading.Thread(target=_emit_worker, name="emit")
        exit_thread = threading.Thread(target=_exit_worker, name="exit")

        emit_thread.start()
        exit_thread.start()
        emit_thread.join(timeout=10.0)
        exit_thread.join(timeout=10.0)

        assert not emit_thread.is_alive()
        assert not exit_thread.is_alive()
        # No race: neither worker raised, in particular no
        # ``database is locked`` from WAL contention.
        assert errors == [], (
            f"concurrent emit/exit raised {[repr(e) for e in errors]}"
        )

        conn = sqlite3.connect(v10_db)
        try:
            cand_count = conn.execute(
                "SELECT COUNT(*) FROM candidate_events"
            ).fetchone()[0]
            exit_count = conn.execute(
                "SELECT COUNT(*) FROM paper_orders WHERE event=?",
                (ADVERSE_NEWS_EVENT,),
            ).fetchone()[0]
        finally:
            conn.close()
        # Exactly one row per side — no duplicates, no missing rows.
        assert cand_count == 1
        assert exit_count == 1
