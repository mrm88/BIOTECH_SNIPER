"""WAL contention stress test for the Stage-1 / daily-curated split (f-m2-07).

The Stage-1 news daemon and the existing daily-curated path share
the same SQLite database (``data/alpha_sniper.db``). With WAL mode
enabled by :func:`biotech_sniper.db.connect`, concurrent readers and
writers must NOT collide with ``OperationalError("database is locked")``
when they target different tables.

This module pins the f-m2-07 contract:

* **Concurrent WAL stress**: zero ``database is locked`` errors over
  100 iterations across at least three concurrent worker threads
  hitting the three hot tables (``candidate_events``, ``news_events``,
  ``paper_orders``) — the same tables that Stage-1 emit and the
  adverse-news exit hook would touch in production.

The test deliberately keeps the per-iteration work small (one INSERT
per worker, one cross-table SELECT) so the suite runs in
deterministic sub-second time on the local laptop and CI hosts. The
goal is to exercise the WAL writer-lock + reader handshake, NOT to
benchmark throughput.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest

from biotech_sniper import db
from biotech_sniper.migrations.runner import run as run_migrations_runner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_v10_db(tmp_path: Path) -> Path:
    """Create a tmp-path SQLite db at schema_version=10 (WAL mode on)."""

    db_path = tmp_path / "alpha.db"
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
    finally:
        conn.close()
    run_migrations_runner(db_path, target_version=11, take_backup_first=False)
    return db_path


@pytest.fixture
def v10_db(tmp_path: Path) -> Iterator[Path]:
    yield _build_v10_db(tmp_path)


# ---------------------------------------------------------------------------
# WAL mode sanity check
# ---------------------------------------------------------------------------


def test_db_connect_enables_wal(v10_db: Path) -> None:
    """The project ``connect()`` opens the db in WAL journal mode.

    WAL is the precondition for non-blocking reader/writer
    concurrency — without it, a single writer would serialise every
    read on the same connection. The contention test below would
    silently succeed on a stricter journal mode but for the wrong
    reasons; pin the journal_mode here so a regression is loud.
    """

    conn = db.connect(v10_db)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert str(mode).lower() == "wal"


# ---------------------------------------------------------------------------
# 100-iteration concurrency stress
# ---------------------------------------------------------------------------


# Number of full iterations the contention test runs. Pinned at 100
# per the f-m2-07 expectedBehavior contract.
ITERATIONS: int = 100

# Per-write busy_timeout (milliseconds). WAL still serialises
# writers, so a small busy_timeout lets a slow worker queue rather
# than fail outright on a transient writer-lock collision. Production
# uses SQLite's default which yields the same behaviour; we set it
# explicitly here so a tighter test default would surface a
# regression.
BUSY_TIMEOUT_MS: int = 5_000


def _write_busy_timeout(conn: sqlite3.Connection) -> None:
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")


def _seed_baseline(db_path: Path) -> None:
    """Pre-populate one news_events row that workers can FK against.

    ``candidate_events.source_news_event_id`` references
    ``news_events(id)`` so the writer worker needs at least one
    parent row to insert against. We stage exactly one so the
    contention surface stays narrow (each worker writes to its own
    table; the parent row is read-only thereafter).
    """

    conn = db.connect(db_path)
    try:
        _write_busy_timeout(conn)
        conn.execute(
            "INSERT INTO news_events "
            "(ticker, source, published_at, title, url, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "VRTX",
                "wal_stress_seed",
                "2026-04-29T08:00:00.000000Z",
                "VRTX baseline headline (FDA approval)",
                "https://example.com/wal-seed",
                "2026-04-29T08:00:00.000000Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_no_database_locked_under_100_iteration_concurrency(
    v10_db: Path,
) -> None:
    """100 iterations × 3 concurrent writers → zero ``database is locked``.

    Three workers run in parallel for ITERATIONS rounds, each
    targeting a different hot table:

    * Worker A — ``candidate_events`` writer (Stage-1 emit surface).
    * Worker B — ``news_events`` writer (daily-curated ingest +
      adverse-news enrichment surface).
    * Worker C — ``paper_orders`` writer (executor / adverse-news
      exit surface).

    Each worker uses its own short-lived connection, sets
    ``PRAGMA busy_timeout`` to a tolerant value, and records every
    raised :class:`sqlite3.OperationalError` whose message contains
    ``database is locked``. The test asserts the count is zero
    after the run completes.
    """

    _seed_baseline(v10_db)

    # Resolve the seed news_events.id once outside the workers so
    # the FK lookup never collides with the writers above.
    conn = db.connect(v10_db)
    try:
        seed_news_event_id = int(
            conn.execute(
                "SELECT id FROM news_events WHERE source='wal_stress_seed' "
                "ORDER BY id ASC LIMIT 1"
            ).fetchone()[0]
        )
    finally:
        conn.close()

    locked_errors: list[str] = []
    other_errors: list[BaseException] = []
    lock = threading.Lock()
    start_barrier = threading.Barrier(3)

    def _record_error(exc: BaseException) -> None:
        with lock:
            if (
                isinstance(exc, sqlite3.OperationalError)
                and "database is locked" in str(exc).lower()
            ):
                locked_errors.append(str(exc))
            else:
                other_errors.append(exc)

    def _candidate_writer() -> None:
        """Insert a fresh ``candidate_events`` row per iteration.

        Each row's ``dedup_key`` is unique by suffix so the UNIQUE
        constraint never short-circuits the INSERT.
        """

        try:
            conn = db.connect(v10_db)
            try:
                _write_busy_timeout(conn)
                start_barrier.wait(timeout=10.0)
                for i in range(ITERATIONS):
                    try:
                        with conn:
                            conn.execute(
                                "INSERT INTO candidate_events "
                                "(ticker, source_news_event_id, "
                                "matched_keywords, calendar_match, "
                                "emitted_at, dedup_key) "
                                "VALUES (?, ?, ?, ?, ?, ?)",
                                (
                                    "VRTX",
                                    seed_news_event_id,
                                    "fda approval",
                                    None,
                                    "2026-04-29T09:00:00.000000Z",
                                    f"wal_stress_cand_{i:04d}",
                                ),
                            )
                    except BaseException as exc:  # noqa: BLE001
                        _record_error(exc)
            finally:
                conn.close()
        except BaseException as exc:  # noqa: BLE001
            _record_error(exc)

    def _news_writer() -> None:
        """Insert a fresh ``news_events`` row per iteration."""

        try:
            conn = db.connect(v10_db)
            try:
                _write_busy_timeout(conn)
                start_barrier.wait(timeout=10.0)
                for i in range(ITERATIONS):
                    try:
                        with conn:
                            conn.execute(
                                "INSERT INTO news_events "
                                "(ticker, source, published_at, title, url, "
                                "ingested_at) "
                                "VALUES (?, ?, ?, ?, ?, ?)",
                                (
                                    "VRTX",
                                    "wal_stress_news",
                                    "2026-04-29T10:00:00.000000Z",
                                    f"VRTX stress headline {i}",
                                    f"https://example.com/wal-stress-n-{i}",
                                    "2026-04-29T10:00:00.000000Z",
                                ),
                            )
                    except BaseException as exc:  # noqa: BLE001
                        _record_error(exc)
            finally:
                conn.close()
        except BaseException as exc:  # noqa: BLE001
            _record_error(exc)

    def _paper_orders_writer() -> None:
        """Insert a fresh ``paper_orders`` row per iteration.

        Uses ``event='adverse_news'`` to mirror the actual
        contention surface the Reading-B M2 split introduces:
        Stage-1 writes ``candidate_events`` while the adverse-news
        exit hook writes ``paper_orders(event='adverse_news')``.
        """

        try:
            conn = db.connect(v10_db)
            try:
                _write_busy_timeout(conn)
                start_barrier.wait(timeout=10.0)
                for i in range(ITERATIONS):
                    try:
                        with conn:
                            conn.execute(
                                "INSERT INTO paper_orders "
                                "(id, play_card_id, alpaca_order_id, symbol, "
                                "side, qty, status, event, parent_play_card_id, "
                                "client_order_id) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (
                                    f"wal_stress_order_{i:04d}",
                                    f"VRTX-stress-{i:04d}",
                                    f"alp_{i:04d}",
                                    "VRTX260620C00400000",
                                    "sell",
                                    1,
                                    "accepted",
                                    "adverse_news",
                                    "VRTX-2026-04-29",
                                    f"VRTX-adverse_news-stress-{i:04d}",
                                ),
                            )
                    except BaseException as exc:  # noqa: BLE001
                        _record_error(exc)
            finally:
                conn.close()
        except BaseException as exc:  # noqa: BLE001
            _record_error(exc)

    workers = [
        threading.Thread(target=_candidate_writer, name="cand"),
        threading.Thread(target=_news_writer, name="news"),
        threading.Thread(target=_paper_orders_writer, name="orders"),
    ]
    for w in workers:
        w.start()
    deadline = time.monotonic() + 60.0
    for w in workers:
        remaining = max(0.1, deadline - time.monotonic())
        w.join(timeout=remaining)
    for w in workers:
        assert not w.is_alive(), f"WAL stress worker {w.name} did not finish"

    # f-m2-07 contract: zero "database is locked" errors over 100
    # iterations across the three writer surfaces.
    assert locked_errors == [], (
        f"WAL contention produced {len(locked_errors)} 'database is "
        f"locked' errors: first={locked_errors[0]!r}"
    )
    # Any OTHER unexpected exception is a hard failure too — this
    # path catches schema regressions (CHECK violations, UNIQUE
    # collisions) that would otherwise be masked by the locked-only
    # filter above.
    assert other_errors == [], (
        f"WAL stress produced unexpected non-locked exceptions: "
        f"{[repr(e) for e in other_errors]}"
    )

    # Sanity check: every writer landed exactly ITERATIONS rows so
    # the test has actually exercised the lock surface (rather than
    # racing on the barrier and exiting cleanly).
    conn = sqlite3.connect(v10_db)
    try:
        cand_count = conn.execute(
            "SELECT COUNT(*) FROM candidate_events "
            "WHERE dedup_key LIKE 'wal_stress_cand_%'"
        ).fetchone()[0]
        news_count = conn.execute(
            "SELECT COUNT(*) FROM news_events WHERE source='wal_stress_news'"
        ).fetchone()[0]
        order_count = conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE id LIKE 'wal_stress_order_%'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert cand_count == ITERATIONS
    assert news_count == ITERATIONS
    assert order_count == ITERATIONS


def test_concurrent_readers_observe_writes_without_locking(
    v10_db: Path,
) -> None:
    """A reader thread polling COUNT(*) must NEVER see ``database is locked``.

    Tightens the writer-only stress above with a fourth worker that
    spins on `SELECT COUNT(*)` while the writer threads commit.
    Demonstrates that WAL readers do not block writers (and vice
    versa) on the same connection-per-thread pattern the news
    daemon uses in production.
    """

    _seed_baseline(v10_db)
    conn = db.connect(v10_db)
    try:
        seed_news_event_id = int(
            conn.execute(
                "SELECT id FROM news_events WHERE source='wal_stress_seed' "
                "ORDER BY id ASC LIMIT 1"
            ).fetchone()[0]
        )
    finally:
        conn.close()

    locked_errors: list[str] = []
    other_errors: list[BaseException] = []
    lock = threading.Lock()
    stop_event = threading.Event()
    start_barrier = threading.Barrier(2)

    def _record_error(exc: BaseException) -> None:
        with lock:
            if (
                isinstance(exc, sqlite3.OperationalError)
                and "database is locked" in str(exc).lower()
            ):
                locked_errors.append(str(exc))
            else:
                other_errors.append(exc)

    def _writer() -> None:
        try:
            conn = db.connect(v10_db)
            try:
                _write_busy_timeout(conn)
                start_barrier.wait(timeout=10.0)
                for i in range(ITERATIONS):
                    try:
                        with conn:
                            conn.execute(
                                "INSERT INTO candidate_events "
                                "(ticker, source_news_event_id, "
                                "matched_keywords, calendar_match, "
                                "emitted_at, dedup_key) "
                                "VALUES (?, ?, ?, ?, ?, ?)",
                                (
                                    "VRTX",
                                    seed_news_event_id,
                                    "fda approval",
                                    None,
                                    "2026-04-29T09:00:00.000000Z",
                                    f"wal_reader_cand_{i:04d}",
                                ),
                            )
                    except BaseException as exc:  # noqa: BLE001
                        _record_error(exc)
            finally:
                conn.close()
        finally:
            stop_event.set()

    def _reader() -> None:
        try:
            conn = sqlite3.connect(v10_db)
            try:
                _write_busy_timeout(conn)
                start_barrier.wait(timeout=10.0)
                while not stop_event.is_set():
                    try:
                        conn.execute(
                            "SELECT COUNT(*) FROM candidate_events"
                        ).fetchone()
                        conn.execute(
                            "SELECT COUNT(*) FROM news_events"
                        ).fetchone()
                        conn.execute(
                            "SELECT COUNT(*) FROM paper_orders"
                        ).fetchone()
                    except BaseException as exc:  # noqa: BLE001
                        _record_error(exc)
            finally:
                conn.close()
        except BaseException as exc:  # noqa: BLE001
            _record_error(exc)

    writer_thread = threading.Thread(target=_writer, name="writer")
    reader_thread = threading.Thread(target=_reader, name="reader")

    writer_thread.start()
    reader_thread.start()
    writer_thread.join(timeout=30.0)
    stop_event.set()
    reader_thread.join(timeout=10.0)

    assert not writer_thread.is_alive()
    assert not reader_thread.is_alive()
    assert locked_errors == [], (
        f"reader/writer WAL contention produced "
        f"{len(locked_errors)} 'database is locked' errors"
    )
    assert other_errors == [], (
        f"reader/writer stress produced unexpected exceptions: "
        f"{[repr(e) for e in other_errors]}"
    )
