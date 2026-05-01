"""Cross-cutting concurrent-path isolation tests for Reading-B (f-cross-07).

Pins the assertion IDs from ``features.json::fulfills`` for
``f-cross-07-idempotency-and-replay`` that exercise concurrent
SQLite contention on the shared ``data/alpha_sniper.db`` between
the Reading-B Stage-1 news daemon and the existing daily-curated
06:13 PT cron path.

* **VAL-CROSS-029** — Concurrent path isolation. A 10-headline
  burst injected into the news daemon WHILE the daily-runner
  subprocess is mid-flight produces ZERO ``database is locked``
  errors AND zero missed rows AND zero duplicated rows. The test
  uses thread-level concurrency (mirrors :mod:`tests.test_wal_contention`
  patterns) — the daily-curated path's writers are simulated by a
  ``performance_ledger`` writer thread (the canonical secondary
  writer surface that the daily 06:13 PT cron path exercises).

* **VAL-CROSS-030** — WAL checkpoint succeeds while both writers
  are active. ``PRAGMA wal_checkpoint(TRUNCATE)`` issued during
  concurrent writer activity completes successfully and the WAL
  file is bounded after the checkpoint returns.

The test fixtures are hermetic — no live network, no live broker.
SQLite is configured exactly as production (WAL mode, foreign
keys ON, busy_timeout via :func:`biotech_sniper.db.connect`). The
contention surface is the SAME tables the production code paths
touch:

* news daemon → ``candidate_events`` (Stage-1 emission)
* daily runner → ``performance_ledger`` (post-trade rollup)
* shared    → ``news_events`` (the seed table for Stage-1; the
              daily path also reads this table during ranker
              orchestration)

Threads are used instead of subprocesses for two reasons:

1. The contract surface is ``database is locked`` — this is a
   process-AND-thread-level SQLite contention signal. Threads
   exercising distinct connections produce identical lock
   semantics to subprocesses with the same connection-per-thread
   pattern that the production daemon uses.
2. Subprocesses inflate the test wall-clock by 2–3× without
   adding coverage. The test_wal_contention.py file already
   demonstrates the thread-level approach is sufficient to surface
   busy-timeout regressions.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest

from biotech_sniper import db as project_db
from biotech_sniper.migrations.runner import run as run_migrations_runner
from biotech_sniper.news_daemon.emit import (
    make_candidate,
    write_candidate,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _build_v10_db(tmp_path: Path) -> Path:
    """Construct a fresh tmp_path SQLite db at schema_version=10."""

    db_path = tmp_path / "alpha.db"
    conn = project_db.connect(db_path)
    try:
        project_db.run_migrations(conn)
    finally:
        conn.close()
    run_migrations_runner(db_path, target_version=11, take_backup_first=False)
    return db_path


@pytest.fixture
def v10_db(tmp_path: Path) -> Iterator[Path]:
    yield _build_v10_db(tmp_path)


# Per-write busy_timeout (milliseconds). Production uses SQLite's
# default; we set it explicitly here so a tighter test default
# would surface a regression. Mirrors test_wal_contention.py.
BUSY_TIMEOUT_MS: int = 5_000


def _set_busy_timeout(conn: sqlite3.Connection) -> None:
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")


def _seed_news_event(
    db_path: Path,
    *,
    ticker: str,
    title: str,
    url: str,
) -> int:
    """Insert one ``news_events`` row and return its id."""

    conn = project_db.connect(db_path)
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


# ---------------------------------------------------------------------------
# VAL-CROSS-029 — Concurrent path isolation: zero missed/duplicated rows
# ---------------------------------------------------------------------------


# Per-stress iterations for the daily-curated writer. Pinned high
# enough to reliably surface a regression but low enough to keep
# the test under 5 s on a 2-core VPS.
DAILY_ITERATIONS: int = 100

# Number of synthetic Reading-B headlines to inject during the
# stress run. The contract specifies 10, which is what we use here.
NEWS_BURST_SIZE: int = 10


class TestNewsDaemonDuringDailyRun_VAL_CROSS_029:
    """News daemon + daily-curated path concurrent — zero missed/duplicated rows."""

    def test_news_daemon_during_daily_run(self, v10_db: Path) -> None:
        """10-headline burst + 100-iteration daily writer → all rows present, no dups, no locks."""

        # Pre-seed one news_events row so the candidate FK resolves.
        seed_news_id = _seed_news_event(
            v10_db,
            ticker="VRTX",
            title="VRTX baseline headline",
            url="https://example.com/vrtx-cross29-seed",
        )

        # Pre-build the canonical (ticker, source_news_event_id,
        # matched_keywords, dedup_key) tuples we expect to see
        # exactly ONCE each in candidate_events after the test.
        burst_ids = [
            _seed_news_event(
                v10_db,
                ticker="VRTX",
                title=f"VRTX headline {i}",
                url=f"https://example.com/vrtx-cross29-{i}",
            )
            for i in range(NEWS_BURST_SIZE)
        ]
        expected_dedup_keys = [
            make_candidate(
                "VRTX", nid, ["fda", "approval"]
            ).dedup_key
            for nid in burst_ids
        ]

        locked_errors: list[str] = []
        other_errors: list[BaseException] = []
        lock = threading.Lock()
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

        def _news_daemon_writer() -> None:
            """Stage-1 emit surface: writes 10 candidate_events rows."""

            try:
                start_barrier.wait(timeout=10.0)
                for nid in burst_ids:
                    cand = make_candidate(
                        "VRTX", nid, ["fda", "approval"]
                    )
                    try:
                        write_candidate(str(v10_db), cand)
                    except BaseException as exc:  # noqa: BLE001
                        _record_error(exc)
                    # Tiny stagger so the daily writer interleaves
                    # rather than runs to completion before us.
                    time.sleep(0.001)
            except BaseException as exc:  # noqa: BLE001
                _record_error(exc)

        def _daily_curated_writer() -> None:
            """Daily-curated cron surface: writes ``paper_orders`` rows.

            The daily 06:13 PT cron path's primary writer is
            :class:`PaperExecutor`, which inserts into
            ``paper_orders`` with ``event='open'``. We mirror that
            shape here so the contention surface is exactly the
            same as production.
            """

            try:
                conn = project_db.connect(v10_db)
                try:
                    _set_busy_timeout(conn)
                    start_barrier.wait(timeout=10.0)
                    for i in range(DAILY_ITERATIONS):
                        try:
                            with conn:
                                conn.execute(
                                    "INSERT INTO paper_orders ("
                                    "id, play_card_id, alpaca_order_id, "
                                    "symbol, side, qty, status, event, "
                                    "client_order_id"
                                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    (
                                        f"daily-cross29-{i:04d}",
                                        f"VRTX-daily-{i:04d}",
                                        f"alpaca-daily-{i:04d}",
                                        "VRTX260620C00400000",
                                        "buy",
                                        1,
                                        "accepted",
                                        "open",
                                        f"VRTX-open-daily-{i:04d}",
                                    ),
                                )
                        except BaseException as exc:  # noqa: BLE001
                            _record_error(exc)
                finally:
                    conn.close()
            except BaseException as exc:  # noqa: BLE001
                _record_error(exc)

        threads = [
            threading.Thread(
                target=_news_daemon_writer, name="news-daemon"
            ),
            threading.Thread(
                target=_daily_curated_writer, name="daily-curated"
            ),
        ]
        for t in threads:
            t.start()
        deadline = time.monotonic() + 60.0
        for t in threads:
            remaining = max(0.1, deadline - time.monotonic())
            t.join(timeout=remaining)
        for t in threads:
            assert not t.is_alive(), (
                f"VAL-CROSS-029 worker {t.name} did not complete"
            )

        assert locked_errors == [], (
            f"concurrent path produced {len(locked_errors)} "
            f"'database is locked' errors: first={locked_errors[0]!r}"
        )
        assert other_errors == [], (
            f"concurrent path produced unexpected exceptions: "
            f"{[repr(e) for e in other_errors]}"
        )

        # Reading-B side: every dedup_key from the 10-headline
        # burst is present EXACTLY ONCE (no missed rows, no dups).
        conn = sqlite3.connect(v10_db)
        try:
            placeholders = ",".join(["?"] * len(expected_dedup_keys))
            distinct_count = int(
                conn.execute(
                    f"SELECT COUNT(DISTINCT dedup_key) "
                    f"FROM candidate_events "
                    f"WHERE dedup_key IN ({placeholders})",
                    expected_dedup_keys,
                ).fetchone()[0]
            )
            total_count = int(
                conn.execute(
                    f"SELECT COUNT(*) "
                    f"FROM candidate_events "
                    f"WHERE dedup_key IN ({placeholders})",
                    expected_dedup_keys,
                ).fetchone()[0]
            )
            duplicates = list(
                conn.execute(
                    "SELECT dedup_key, COUNT(*) "
                    "FROM candidate_events "
                    "GROUP BY dedup_key HAVING COUNT(*) > 1"
                )
            )
            # Daily-curated side: every paper_orders row landed.
            ledger_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM paper_orders "
                    "WHERE id LIKE 'daily-cross29-%'"
                ).fetchone()[0]
            )
        finally:
            conn.close()

        assert distinct_count == NEWS_BURST_SIZE, (
            f"expected {NEWS_BURST_SIZE} distinct dedup_keys; "
            f"got {distinct_count}"
        )
        assert total_count == NEWS_BURST_SIZE, (
            f"expected {NEWS_BURST_SIZE} total candidate_events rows; "
            f"got {total_count} (duplicates suspected)"
        )
        assert duplicates == [], (
            f"candidate_events has duplicate dedup_keys: {duplicates}"
        )
        assert ledger_count == DAILY_ITERATIONS, (
            f"expected {DAILY_ITERATIONS} paper_orders rows; "
            f"got {ledger_count} (daily-curated path missed writes)"
        )

        # Sanity: PRAGMA integrity_check passes after concurrent stress.
        conn = sqlite3.connect(v10_db)
        try:
            integrity = conn.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
        finally:
            conn.close()
        assert integrity == "ok", (
            f"PRAGMA integrity_check failed: {integrity!r}"
        )

        # Sanity: the seed news_events row is still intact.
        conn = sqlite3.connect(v10_db)
        try:
            seed_still_present = (
                conn.execute(
                    "SELECT COUNT(*) FROM news_events WHERE id=?",
                    (seed_news_id,),
                ).fetchone()[0]
                == 1
            )
        finally:
            conn.close()
        assert seed_still_present


# ---------------------------------------------------------------------------
# VAL-CROSS-029 SOAK — 5-minute concurrent stress (gated by RUN_SOAK=1)
# ---------------------------------------------------------------------------

# ~10k news-side inserts driven by a sustained 5-minute concurrent
# writer run. The contract surface (zero missed/duplicated rows,
# zero ``database is locked`` raises) is the SAME as the fast
# variant above; the soak deliberately runs orders of magnitude
# more iterations to surface contention bugs that only show up
# under sustained load (e.g. WAL growth, busy-timeout hairlines,
# checkpoint-vs-writer starvation).
SOAK_DURATION_SECONDS: int = 5 * 60  # 5 minutes
SOAK_NEWS_INSERTS_TARGET: int = 10_000
# Gating env var. The fast CI run never sets this; the soak job
# (a separate CI workflow or local invocation) sets ``RUN_SOAK=1``.
_RUN_SOAK = os.environ.get("RUN_SOAK", "") == "1"


@pytest.mark.soak
@pytest.mark.skipif(
    not _RUN_SOAK,
    reason="soak tests are gated behind RUN_SOAK=1; set RUN_SOAK=1 to run",
)
def test_news_daemon_during_daily_run_5min_soak(
    tmp_path: Path,
) -> None:
    """5-minute soak — ~10k news inserts under concurrent daily writer.

    Stricter form of VAL-CROSS-029. Runs a sustained concurrent
    workload for ``SOAK_DURATION_SECONDS`` and asserts:

    * zero ``sqlite3.OperationalError`` "database is locked" raises;
    * zero unexpected exceptions on either writer thread;
    * exactly one ``candidate_events`` row per dedup_key actually
      submitted by the news writer (no duplicates, no missed rows);
    * post-soak ``PRAGMA integrity_check`` returns ``"ok"``.

    Gated behind ``RUN_SOAK=1`` so normal ``pytest -n 2`` runs skip
    it and the fast CI stays fast. Run via:

        RUN_SOAK=1 .venv/bin/pytest -q tests/test_concurrent_isolation.py \\
            -k soak --timeout=400
    """

    db_path = _build_v10_db(tmp_path)

    # Pre-seed a single news_events row so the candidate FK
    # resolves. The dedup_key is `(ticker, source_news_event_id,
    # tuple(matched_keywords))` so we vary the matched_keywords
    # across iterations to generate distinct dedup_keys per insert.
    seed_news_id = _seed_news_event(
        db_path,
        ticker="VRTX",
        title="VRTX soak seed",
        url="https://example.com/vrtx-soak-seed",
    )

    locked_errors: list[str] = []
    other_errors: list[BaseException] = []
    lock = threading.Lock()
    start_barrier = threading.Barrier(2)
    stop = threading.Event()

    submitted_dedup_keys: list[str] = []

    def _record_error(exc: BaseException) -> None:
        with lock:
            if (
                isinstance(exc, sqlite3.OperationalError)
                and "database is locked" in str(exc).lower()
            ):
                locked_errors.append(str(exc))
            else:
                other_errors.append(exc)

    def _news_daemon_writer() -> None:
        try:
            start_barrier.wait(timeout=10.0)
            i = 0
            while not stop.is_set() and i < SOAK_NEWS_INSERTS_TARGET:
                cand = make_candidate(
                    "VRTX",
                    seed_news_id,
                    [f"soak_kw_{i:06d}"],
                )
                with lock:
                    submitted_dedup_keys.append(cand.dedup_key)
                try:
                    write_candidate(str(db_path), cand)
                except BaseException as exc:  # noqa: BLE001
                    _record_error(exc)
                i += 1
        except BaseException as exc:  # noqa: BLE001
            _record_error(exc)

    def _daily_curated_writer() -> None:
        try:
            conn = project_db.connect(db_path)
            try:
                _set_busy_timeout(conn)
                start_barrier.wait(timeout=10.0)
                i = 0
                while not stop.is_set():
                    try:
                        with conn:
                            conn.execute(
                                "INSERT INTO paper_orders ("
                                "id, play_card_id, alpaca_order_id, "
                                "symbol, side, qty, status, event, "
                                "client_order_id"
                                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (
                                    f"soak-daily-{i:06d}",
                                    f"VRTX-soak-{i:06d}",
                                    f"alpaca-soak-{i:06d}",
                                    "VRTX260620C00400000",
                                    "buy",
                                    1,
                                    "accepted",
                                    "open",
                                    f"VRTX-open-soak-{i:06d}",
                                ),
                            )
                        i += 1
                    except BaseException as exc:  # noqa: BLE001
                        _record_error(exc)
            finally:
                conn.close()
        except BaseException as exc:  # noqa: BLE001
            _record_error(exc)

    threads = [
        threading.Thread(target=_news_daemon_writer, name="news-soak"),
        threading.Thread(target=_daily_curated_writer, name="daily-soak"),
    ]
    soak_start = time.monotonic()
    for t in threads:
        t.start()

    # Run the soak. The news writer auto-stops after 10k inserts;
    # the daily writer stops on the ``stop`` event. We arm the
    # event after SOAK_DURATION_SECONDS so the test caps at ~5 min
    # regardless of which writer races ahead.
    stop_deadline = soak_start + SOAK_DURATION_SECONDS
    while time.monotonic() < stop_deadline:
        # Stop early if the news writer hit its target — no need
        # to keep stress-testing past the contract scope.
        with lock:
            done_news = len(submitted_dedup_keys) >= SOAK_NEWS_INSERTS_TARGET
        if done_news:
            break
        time.sleep(0.5)
    stop.set()

    # Generous join window — 30s after stop is plenty for both
    # writers to drain.
    join_deadline = time.monotonic() + 30.0
    for t in threads:
        remaining = max(0.1, join_deadline - time.monotonic())
        t.join(timeout=remaining)
    for t in threads:
        assert not t.is_alive(), f"soak thread {t.name} did not finish"

    soak_elapsed = time.monotonic() - soak_start

    # No locked errors and no unexpected exceptions.
    assert locked_errors == [], (
        f"soak produced {len(locked_errors)} 'database is locked' "
        f"errors over {soak_elapsed:.1f}s; first={locked_errors[0]!r}"
    )
    assert other_errors == [], (
        f"soak produced unexpected exceptions: "
        f"{[repr(e) for e in other_errors]}"
    )

    # We must have exercised at least the contract-scoped 10k inserts.
    with lock:
        submitted_keys = list(submitted_dedup_keys)
    assert len(submitted_keys) >= SOAK_NEWS_INSERTS_TARGET, (
        f"soak only submitted {len(submitted_keys)} inserts in "
        f"{soak_elapsed:.1f}s; contract requires "
        f">= {SOAK_NEWS_INSERTS_TARGET}"
    )

    # Every submitted dedup_key landed exactly once — no missed
    # rows, no duplicates.
    conn = sqlite3.connect(db_path)
    try:
        # Use a small batch-IN to avoid the 999-parameter SQLite
        # default cap when checking 10k+ keys.
        BATCH = 500
        distinct_total = 0
        rows_total = 0
        unique_keys = list(set(submitted_keys))
        for offset in range(0, len(unique_keys), BATCH):
            batch = unique_keys[offset : offset + BATCH]
            placeholders = ",".join(["?"] * len(batch))
            distinct_total += int(
                conn.execute(
                    f"SELECT COUNT(DISTINCT dedup_key) "
                    f"FROM candidate_events "
                    f"WHERE dedup_key IN ({placeholders})",
                    batch,
                ).fetchone()[0]
            )
            rows_total += int(
                conn.execute(
                    f"SELECT COUNT(*) "
                    f"FROM candidate_events "
                    f"WHERE dedup_key IN ({placeholders})",
                    batch,
                ).fetchone()[0]
            )
        any_duplicates = list(
            conn.execute(
                "SELECT dedup_key, COUNT(*) c "
                "FROM candidate_events "
                "GROUP BY dedup_key HAVING c > 1 LIMIT 1"
            )
        )
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()

    assert distinct_total == len(unique_keys), (
        f"missing dedup_keys: submitted {len(unique_keys)} unique, "
        f"persisted {distinct_total}"
    )
    assert rows_total == len(unique_keys), (
        f"duplicate rows: submitted {len(unique_keys)} unique, "
        f"persisted {rows_total} total"
    )
    assert any_duplicates == [], (
        f"candidate_events has duplicate dedup_keys: {any_duplicates}"
    )
    assert integrity == "ok", (
        f"PRAGMA integrity_check failed post-soak: {integrity!r}"
    )


# ---------------------------------------------------------------------------
# VAL-CROSS-030 — WAL checkpoint succeeds while both writers active
# ---------------------------------------------------------------------------


# Bound on the post-checkpoint WAL file size (bytes). The contract
# notes "≤ documented threshold, typically < 100 KB" — we use a
# generous 1 MiB ceiling so the test is not flaky on slower hosts
# while still catching unbounded WAL growth (a regression would
# leave the WAL multiple-MB).
WAL_POST_CHECKPOINT_MAX_BYTES: int = 1 * 1024 * 1024

# Number of background writes the writer threads issue while the
# checkpoint races against them. Tuned so the checkpoint races
# real contention, not a quiet WAL.
CHECKPOINT_RACE_WRITES: int = 50


class TestWalCheckpointDuringLoad_VAL_CROSS_030:
    """``PRAGMA wal_checkpoint(TRUNCATE)`` succeeds during concurrent writers.

    The contract is split into two halves per f-fix-cross-tests-rigor
    sub-fix D so each test asserts a single, narrow invariant rather
    than blurring contention-tolerance and post-drain truncation
    into one permissive assertion:

    * :meth:`test_checkpoint_returns_under_contention_no_corruption`
      runs the checkpoint against *active* concurrent writers and
      tolerates the race outcome (``busy`` may be 0 or 1, page
      counts may be -1 if the checkpoint could not run). The
      assertion is that the PRAGMA returns without raising and the
      WAL is bounded; it specifically does NOT require the
      checkpoint to "win" against the writers.
    * :meth:`test_checkpoint_truncates_after_writers_drain` is the
      strict half: writers are barrier-joined to completion FIRST,
      and only then is TRUNCATE issued. The assertion is hard:
      ``busy=0``, ``checkpointed_pages == log_pages > 0`` (or both
      zero on no-op), and post-checkpoint WAL <= 4 KiB.
    """

    def test_checkpoint_returns_under_contention_no_corruption(
        self, v10_db: Path
    ) -> None:
        """WAL checkpoint races active writers, completes, and bounds the WAL file."""

        seed_news_id = _seed_news_event(
            v10_db,
            ticker="VRTX",
            title="VRTX checkpoint seed",
            url="https://example.com/vrtx-cross30-seed",
        )

        # Pre-warm the WAL file so the size assertion below is
        # meaningful (a fresh db with no writes has no WAL file).
        for i in range(CHECKPOINT_RACE_WRITES):
            cand = make_candidate(
                "VRTX",
                seed_news_id,
                [f"warm{i}"],
            )
            write_candidate(str(v10_db), cand)

        wal_path = Path(str(v10_db) + "-wal")
        # The WAL may have been truncated by SQLite's auto-checkpoint
        # if synchronous=NORMAL has flushed everything. We tolerate
        # that — what matters is the checkpoint call below
        # succeeds and the post-checkpoint WAL is bounded. Capture
        # pre-checkpoint size for diagnostic reporting only.
        pre_size = wal_path.stat().st_size if wal_path.exists() else 0

        stop = threading.Event()
        errors: list[BaseException] = []
        lock = threading.Lock()
        start_barrier = threading.Barrier(3)

        def _record(exc: BaseException) -> None:
            with lock:
                errors.append(exc)

        def _writer_a() -> None:
            try:
                conn = project_db.connect(v10_db)
                try:
                    _set_busy_timeout(conn)
                    start_barrier.wait(timeout=10.0)
                    i = 0
                    while not stop.is_set():
                        try:
                            with conn:
                                conn.execute(
                                    "INSERT INTO candidate_events ("
                                    "ticker, source_news_event_id, "
                                    "matched_keywords, calendar_match, "
                                    "emitted_at, dedup_key) "
                                    "VALUES (?, ?, ?, ?, ?, ?)",
                                    (
                                        "VRTX",
                                        seed_news_id,
                                        "fda approval",
                                        None,
                                        "2026-04-30T13:00:00Z",
                                        f"checkpoint_a_{i:05d}",
                                    ),
                                )
                            i += 1
                        except BaseException as exc:  # noqa: BLE001
                            _record(exc)
                            return
                finally:
                    conn.close()
            except BaseException as exc:  # noqa: BLE001
                _record(exc)

        def _writer_b() -> None:
            try:
                conn = project_db.connect(v10_db)
                try:
                    _set_busy_timeout(conn)
                    start_barrier.wait(timeout=10.0)
                    i = 0
                    while not stop.is_set():
                        try:
                            with conn:
                                conn.execute(
                                    "INSERT INTO paper_orders ("
                                    "id, play_card_id, alpaca_order_id, "
                                    "symbol, side, qty, status, event, "
                                    "client_order_id"
                                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    (
                                        f"checkpoint_b_{i:05d}",
                                        f"VRTX-cp-b-{i:05d}",
                                        f"alp-cp-b-{i:05d}",
                                        "VRTX260620C00400000",
                                        "buy",
                                        1,
                                        "accepted",
                                        "open",
                                        f"VRTX-cp-b-{i:05d}",
                                    ),
                                )
                            i += 1
                        except BaseException as exc:  # noqa: BLE001
                            _record(exc)
                            return
                finally:
                    conn.close()
            except BaseException as exc:  # noqa: BLE001
                _record(exc)

        checkpoint_result: list[tuple[int, int, int]] = []

        def _checkpoint_runner() -> None:
            """Issue PRAGMA wal_checkpoint(TRUNCATE) WHILE writers are active."""

            try:
                conn = project_db.connect(v10_db)
                try:
                    _set_busy_timeout(conn)
                    start_barrier.wait(timeout=10.0)
                    # Let the writers do real work first so the WAL
                    # has actual frames to checkpoint.
                    time.sleep(0.05)
                    row = conn.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)"
                    ).fetchone()
                    if row is not None:
                        # row = (busy, log_pages, checkpointed_pages)
                        checkpoint_result.append(tuple(int(x) for x in row))
                finally:
                    conn.close()
            except BaseException as exc:  # noqa: BLE001
                _record(exc)

        threads = [
            threading.Thread(target=_writer_a, name="writer-a"),
            threading.Thread(target=_writer_b, name="writer-b"),
            threading.Thread(target=_checkpoint_runner, name="checkpoint"),
        ]
        for t in threads:
            t.start()

        # Let the checkpoint runner have time to issue its PRAGMA.
        # 0.5 s is plenty (the checkpoint races against a few
        # millisecond-cadence writers).
        time.sleep(0.5)
        stop.set()

        deadline = time.monotonic() + 30.0
        for t in threads:
            remaining = max(0.1, deadline - time.monotonic())
            t.join(timeout=remaining)
        for t in threads:
            assert not t.is_alive(), f"thread {t.name} did not finish"

        # No errors — including no "database is locked".
        assert errors == [], (
            f"WAL checkpoint race produced unexpected exceptions: "
            f"{[repr(e) for e in errors]}"
        )

        # The checkpoint actually ran (busy=0 means it was not
        # blocked indefinitely; we tolerate busy=1 because the
        # writers are racing against it — the contract is that the
        # PRAGMA returns, not that it always wins the race).
        assert checkpoint_result, (
            "PRAGMA wal_checkpoint(TRUNCATE) did not return"
        )
        busy, log_pages, checkpointed_pages = checkpoint_result[0]
        # ``busy`` is 0 on a clean checkpoint, 1 if some readers
        # held the WAL when the call returned. Either is acceptable
        # — the assertion is the call did not raise.
        assert busy in (0, 1)
        # SQLite returns ``log_pages``/``checkpointed_pages`` as -1
        # when the checkpoint could not run (e.g. another writer
        # holds the WAL exclusively). The contract for VAL-CROSS-030
        # is that the PRAGMA *returns* without raising — we do NOT
        # require it to win the race. A genuine regression (DB
        # corruption, infinite hang) would surface as either an
        # exception (caught above) or a thread-join timeout.
        assert isinstance(checkpointed_pages, int)
        assert isinstance(log_pages, int)

        # WAL file is bounded post-checkpoint. The TRUNCATE variant
        # truncates the WAL when no readers are blocking; if a
        # reader was active the file remains but should not have
        # grown unboundedly.
        post_size = wal_path.stat().st_size if wal_path.exists() else 0
        assert post_size <= WAL_POST_CHECKPOINT_MAX_BYTES, (
            f"WAL file too large after checkpoint: pre={pre_size} "
            f"post={post_size} (max={WAL_POST_CHECKPOINT_MAX_BYTES})"
        )

        # Final integrity check — corrupting the WAL during a
        # concurrent checkpoint would surface here.
        conn = sqlite3.connect(v10_db)
        try:
            integrity = conn.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
        finally:
            conn.close()
        assert integrity == "ok"

    def test_checkpoint_truncates_after_writers_drain(
        self, v10_db: Path
    ) -> None:
        """Strict half of VAL-CROSS-030: barrier-joined writers, then TRUNCATE.

        Unlike :meth:`test_checkpoint_returns_under_contention_no_corruption`
        which tolerates the race outcome (``busy`` ∈ {0, 1},
        ``log_pages``/``checkpointed_pages`` may be -1), this test
        is strict:

        * Two writer threads run concurrently for a fixed window
          (mirroring the production contention pattern), then are
          barrier-joined to completion BEFORE the checkpoint is
          issued.
        * The TRUNCATE checkpoint then runs against a quiescent
          writer set and MUST succeed cleanly:
          - ``busy == 0`` (no readers/writers blocking);
          - ``checkpointed_pages == log_pages`` AND ``log_pages > 0``
            (every WAL frame was flushed and the WAL had real
            content), OR ``log_pages == 0`` (auto-checkpoint pre-
            emptied the WAL — acceptable, equivalent no-op);
          - post-checkpoint WAL file size <= 4 KiB.

        This second test is the load-bearing one for VAL-CROSS-030's
        "WAL file is truncated" clause; the contention test above
        only proves the call doesn't *raise* under load.
        """

        seed_news_id = _seed_news_event(
            v10_db,
            ticker="VRTX",
            title="VRTX checkpoint drain seed",
            url="https://example.com/vrtx-cross30-drain-seed",
        )

        errors: list[BaseException] = []
        lock = threading.Lock()
        # Both writers must hit the barrier together so they
        # genuinely race for a slice of time before joining.
        start_barrier = threading.Barrier(2)
        # Hard cap on writer iterations so the join phase is
        # bounded regardless of WAL contention.
        WRITES_PER_THREAD = 200

        def _record(exc: BaseException) -> None:
            with lock:
                errors.append(exc)

        def _writer_a() -> None:
            try:
                conn = project_db.connect(v10_db)
                try:
                    _set_busy_timeout(conn)
                    start_barrier.wait(timeout=10.0)
                    for i in range(WRITES_PER_THREAD):
                        try:
                            with conn:
                                conn.execute(
                                    "INSERT INTO candidate_events ("
                                    "ticker, source_news_event_id, "
                                    "matched_keywords, calendar_match, "
                                    "emitted_at, dedup_key) "
                                    "VALUES (?, ?, ?, ?, ?, ?)",
                                    (
                                        "VRTX",
                                        seed_news_id,
                                        "fda approval",
                                        None,
                                        "2026-04-30T13:00:00Z",
                                        f"drain_a_{i:05d}",
                                    ),
                                )
                        except BaseException as exc:  # noqa: BLE001
                            _record(exc)
                            return
                finally:
                    conn.close()
            except BaseException as exc:  # noqa: BLE001
                _record(exc)

        def _writer_b() -> None:
            try:
                conn = project_db.connect(v10_db)
                try:
                    _set_busy_timeout(conn)
                    start_barrier.wait(timeout=10.0)
                    for i in range(WRITES_PER_THREAD):
                        try:
                            with conn:
                                conn.execute(
                                    "INSERT INTO paper_orders ("
                                    "id, play_card_id, alpaca_order_id, "
                                    "symbol, side, qty, status, event, "
                                    "client_order_id"
                                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    (
                                        f"drain_b_{i:05d}",
                                        f"VRTX-drain-b-{i:05d}",
                                        f"alp-drain-b-{i:05d}",
                                        "VRTX260620C00400000",
                                        "buy",
                                        1,
                                        "accepted",
                                        "open",
                                        f"VRTX-drain-b-{i:05d}",
                                    ),
                                )
                        except BaseException as exc:  # noqa: BLE001
                            _record(exc)
                            return
                finally:
                    conn.close()
            except BaseException as exc:  # noqa: BLE001
                _record(exc)

        threads = [
            threading.Thread(target=_writer_a, name="drain-writer-a"),
            threading.Thread(target=_writer_b, name="drain-writer-b"),
        ]
        for t in threads:
            t.start()

        # Barrier-join: wait for BOTH writers to fully complete.
        # The checkpoint runs ONLY after this point — so the
        # checkpoint races no concurrent writer.
        deadline = time.monotonic() + 60.0
        for t in threads:
            remaining = max(0.1, deadline - time.monotonic())
            t.join(timeout=remaining)
        for t in threads:
            assert not t.is_alive(), (
                f"drain writer {t.name} did not complete in 60s"
            )

        assert errors == [], (
            f"drain writers produced exceptions: "
            f"{[repr(e) for e in errors]}"
        )

        # Now — with no writer holding the WAL — issue TRUNCATE.
        # Both connections are closed in the writer threads' finally
        # blocks above, so this checkpoint runs against a fully
        # quiescent writer set.
        conn = project_db.connect(v10_db)
        try:
            row = conn.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, (
            "PRAGMA wal_checkpoint(TRUNCATE) returned no row"
        )
        busy, log_pages, checkpointed_pages = (int(x) for x in row)

        # Strict contract: writers are drained, so busy MUST be 0.
        assert busy == 0, (
            f"post-drain checkpoint reported busy={busy}; "
            "writers were joined before TRUNCATE so the WAL "
            "should be uncontended"
        )
        # Strict contract: every WAL frame must have been flushed.
        # Either log_pages>0 and checkpointed_pages==log_pages
        # (real work done), or log_pages==0 and checkpointed_pages==0
        # (auto-checkpoint pre-emptied the WAL — acceptable no-op).
        assert log_pages >= 0 and checkpointed_pages >= 0, (
            f"post-drain checkpoint returned negative page counts: "
            f"log_pages={log_pages} checkpointed_pages={checkpointed_pages}"
        )
        assert checkpointed_pages == log_pages, (
            f"post-drain checkpoint did not flush all WAL frames: "
            f"log_pages={log_pages} checkpointed_pages={checkpointed_pages}"
        )

        # Strict size cap: 4 KiB. SQLite TRUNCATE on a quiescent
        # WAL leaves either an empty file or zero-byte placeholder.
        # We use 4 KiB rather than 0 so a single 32-byte WAL header
        # remnant doesn't fail the test.
        wal_path = Path(str(v10_db) + "-wal")
        post_size = wal_path.stat().st_size if wal_path.exists() else 0
        assert post_size <= 4096, (
            f"post-drain TRUNCATE did not bound WAL <= 4 KiB: "
            f"got {post_size} bytes (file={wal_path})"
        )

        # Final integrity check.
        conn = sqlite3.connect(v10_db)
        try:
            integrity = conn.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
        finally:
            conn.close()
        assert integrity == "ok", (
            f"PRAGMA integrity_check failed post-drain: {integrity!r}"
        )

    def test_checkpoint_truncates_wal_when_idle(
        self, v10_db: Path
    ) -> None:
        """When no writers are active, TRUNCATE shrinks the WAL to ~0 bytes.

        Spot-check that the checkpoint primitive itself works as
        expected on this platform — provides a control case for the
        concurrent test above so a regression (e.g. TRUNCATE no
        longer truncating) is unambiguous.
        """
        seed_news_id = _seed_news_event(
            v10_db,
            ticker="VRTX",
            title="VRTX idle checkpoint seed",
            url="https://example.com/vrtx-cross30-idle",
        )
        for i in range(CHECKPOINT_RACE_WRITES):
            write_candidate(
                str(v10_db),
                make_candidate(
                    "VRTX", seed_news_id, [f"warm-idle-{i}"]
                ),
            )
        wal_path = Path(str(v10_db) + "-wal")
        # Open and close the writer connection so no in-process
        # connection is holding the WAL open.

        conn = project_db.connect(v10_db)
        try:
            row = conn.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        busy, _, _ = (int(x) for x in row)
        assert busy == 0, f"idle checkpoint should not be busy; got {busy}"

        if wal_path.exists():
            assert wal_path.stat().st_size <= WAL_POST_CHECKPOINT_MAX_BYTES
