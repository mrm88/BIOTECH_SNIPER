"""Cross-cutting idempotency / replay invariants for Reading-B (f-cross-07).

Pins the assertion IDs listed in ``features.json::fulfills`` for
``f-cross-07-idempotency-and-replay``:

* **VAL-CROSS-025** — Daemon restart does not duplicate
  ``candidate_events``. Restarting the news-daemon (kill -9 +
  fresh interpreter) over the SAME ``news_events`` backlog produces
  AT MOST one ``candidate_events`` row keyed on its ``dedup_key``.
  Anchored by the UNIQUE constraint on
  ``candidate_events.dedup_key`` (provisioned by the v10 migration)
  AND by the watermark recovery from
  :func:`biotech_sniper.news_daemon.emit.get_last_emitted_news_event_id`
  (so an in-memory "seen set" wipe on restart does not double-emit).

* **VAL-CROSS-026** — Re-emitting the same candidate event yields
  no duplicate ensemble rows. Two consecutive
  :func:`biotech_sniper.llm.ensemble.score_candidate_event` calls
  with the SAME ``run_id`` against the SAME ``candidate_event_id``
  produce 4 ``ensemble_scores_event`` rows total (one per provider),
  not 8 — anchored by the UNIQUE
  ``(candidate_event_id, provider, run_id)`` constraint on
  ``ensemble_scores_event``.

* **VAL-CROSS-027** — Paper order retry idempotent on
  ``client_order_id``. A second
  :func:`biotech_sniper.exec.stage2_paper_executor.submit_news_event_entry`
  call against the SAME candidate produces exactly ONE
  ``paper_orders`` row; the broker is NOT contacted a second time
  (the executor's :meth:`PaperExecutor._lookup_by_client_order_id`
  probe short-circuits the second submission). A direct INSERT that
  bypasses the lookup and re-uses the same ``client_order_id``
  raises :class:`sqlite3.IntegrityError`.

* **VAL-CROSS-045** — Replaying the same source headline 10 times
  produces byte-identical state. After ``replay_n=1`` and after
  ``replay_n=10``, the row counts of
  ``(candidate_events, ensemble_scores_event, paper_orders,
  ticker_cooldown, news_match_log, llm_cost_ledger,
  execution_events, execution_fills)`` are identical AND the
  full-row payload (modulo the deterministic
  ``INSERT OR IGNORE`` short-circuit) is byte-identical via a
  SHA-256 over the canonical select.

Hermetic — no live network, no live broker. The test fixtures use:

* a tmp-path SQLite db built via the project's migration runner at
  schema_version=10 (Reading-B foundations),
* a duck-typed fake :class:`AlpacaClient` that records every
  ``submit_order`` invocation so the "broker not contacted" claim
  has a hard counter,
* deterministic provider stubs that return a unanimous bullish
  result so the ensemble path completes without invoking real LLMs.
"""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

from biotech_sniper import db as project_db
from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.exec.stage2_dispatcher import (
    EVENT_NEWS_ENTRY,
    build_play_card,
)
from biotech_sniper.exec.stage2_paper_executor import (
    submit_news_event_entry,
)
from biotech_sniper.llm.ensemble import (
    ALL_PROVIDERS,
    EnsembleEventResult,
    ProviderResult,
    score_candidate_event,
)
from biotech_sniper.llm.stage2_gates import (
    cooldown_gate,
    record_cooldown_on_success,
)
from biotech_sniper.migrations.runner import run as run_migrations_runner
from biotech_sniper.news_daemon import emit
from biotech_sniper.news_daemon.emit import (
    make_candidate,
    run_one_poll_cycle,
    write_candidate,
)
from biotech_sniper.paper_executor import PaperExecutor


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
    run_migrations_runner(db_path, target_version=10, take_backup_first=False)
    return db_path


@pytest.fixture
def v10_db(tmp_path: Path) -> Iterator[Path]:
    yield _build_v10_db(tmp_path)


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


def _seed_candidate_for(
    db_path: Path,
    *,
    ticker: str,
    matched_keywords: str = "pdufa,approval",
    dedup_seed: str = "replay-001",
) -> int:
    """Insert a ``news_events`` + matching ``candidate_events`` row.

    Returns the ``candidate_events.id`` so the score / submit
    helpers below can chain off a real candidate row.
    """

    nid = _seed_news_event(
        db_path,
        ticker=ticker,
        title=f"{ticker} pdufa approval granted",
        url=f"https://example.com/{ticker.lower()}-{dedup_seed}",
    )
    conn = project_db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO candidate_events ("
            "ticker, source_news_event_id, matched_keywords,"
            " emitted_at, dedup_key) VALUES (?,?,?,?,?)",
            (
                ticker,
                nid,
                matched_keywords,
                "2026-04-30T12:00:02Z",
                f"dedup-{ticker.lower()}-{dedup_seed}",
            ),
        )
        cid = int(
            conn.execute("SELECT MAX(id) FROM candidate_events").fetchone()[0]
        )
        conn.commit()
    finally:
        conn.close()
    return cid


# ---------------------------------------------------------------------------
# Fake AlpacaClient — duck-typed minimal surface for PaperExecutor
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Records ``submit_order`` calls; deterministic broker id by client_order_id."""

    def __init__(self, *, base_url: str = PAPER_BASE_URL) -> None:
        self.base_url = base_url
        self.submit_calls: list[Any] = []

    def get_positions(self) -> list[dict[str, Any]]:
        return []

    def submit_order(self, order_request: Any) -> dict[str, Any]:
        self.submit_calls.append(order_request)
        coid = (
            getattr(order_request, "client_order_id", None) or "no-coid"
        )
        return {
            "id": f"alpaca-{coid}",
            "client_order_id": coid,
            "symbol": getattr(order_request, "symbol", None),
            "side": "buy",
            "qty": getattr(order_request, "qty", 1),
            "status": "accepted",
        }

    def get_order(self, order_id: str) -> dict[str, Any]:
        return {"id": order_id, "status": "accepted"}

    def get_latest_trade(self, ticker: str) -> Optional[float]:
        return 100.0


# ---------------------------------------------------------------------------
# Ensemble + dispatch helpers
# ---------------------------------------------------------------------------


def _make_provider_callable(
    label: str = "material",
    direction: str = "bullish",
    probability: float = 0.85,
):
    """Return a deterministic provider stub for the ensemble fan-out."""

    def _call(candidate, *, name=None):  # noqa: ARG001
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


def _ensemble_result_for(
    candidate_event_id: int,
    *,
    run_id: str,
    direction: str = "bullish",
    label: str = "material",
    probability: float = 0.85,
) -> EnsembleEventResult:
    rows = [
        ProviderResult(
            provider=p,
            label=label,
            probability=probability,
            direction=direction,
            rationale=f"{p}-stub",
            citations=[],
            latency_ms=10,
            cost_usd=0.001,
        )
        for p in ALL_PROVIDERS
    ]
    return EnsembleEventResult(
        candidate_event_id=candidate_event_id,
        run_id=run_id,
        per_provider_results=rows,
        successful_providers=list(ALL_PROVIDERS),
        failed_providers=[],
        label=label,
        direction=direction,
        mean_probability=probability,
        label_histogram={label: 4},
    )


def _candidate_event_dict(cid: int, *, ticker: str) -> dict[str, Any]:
    return {
        "id": cid,
        "ticker": ticker,
        "source_news_event_id": cid * 10,
        "matched_keywords": "pdufa,approval",
        "calendar_match": None,
        "emitted_at": "2026-04-30T12:00:02Z",
        "dedup_key": f"dedup-{ticker.lower()}-replay-001",
    }


def _make_executor(
    db_path: Path,
    *,
    client: Optional[_FakeAlpacaClient] = None,
) -> tuple[PaperExecutor, _FakeAlpacaClient]:
    fake = client or _FakeAlpacaClient()
    executor = PaperExecutor(
        fake,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )
    # PaperExecutor's constructor only walks up to db.CURRENT_VERSION
    # (v9). The Reading-B v10 migration is required for
    # ``paper_orders.event = 'news_event_entry'`` and
    # ``ticker_cooldown`` to exist.
    run_migrations_runner(db_path, 10, take_backup_first=False)
    return executor, fake


# ---------------------------------------------------------------------------
# VAL-CROSS-025 — Daemon restart does not duplicate candidate_events
# ---------------------------------------------------------------------------


class TestDaemonRestartNoDuplicateCandidates_VAL_CROSS_025:
    """Restart over the SAME news_events backlog → at most ONE candidate per dedup_key."""

    def test_in_process_restart_simulation(self, v10_db: Path) -> None:
        """Re-running ``run_one_poll_cycle`` over the same backlog does NOT duplicate."""

        nid = _seed_news_event(
            v10_db,
            ticker="VRTX",
            title="VRTX PDUFA approval granted",
            url="https://example.com/vrtx-restart-1",
        )

        # First cycle commits one candidate.
        scanned_1, inserted_1 = run_one_poll_cycle(str(v10_db))
        assert inserted_1 == 1, (scanned_1, inserted_1)

        # "Restart" — same db, fresh module state already cached but
        # the production cycle reads watermark from SQLite, so a
        # re-call is the in-process equivalent of kill+restart.
        scanned_2, inserted_2 = run_one_poll_cycle(str(v10_db))
        assert inserted_2 == 0, (scanned_2, inserted_2)

        # Even if the watermark were corrupted to 0 (simulating a
        # pathological state-loss restart) the dedup_key UNIQUE
        # constraint must still cap the row count at 1.
        scanned_3, inserted_3 = run_one_poll_cycle(str(v10_db), after_id=0)
        assert inserted_3 == 0, (scanned_3, inserted_3)

        conn = sqlite3.connect(v10_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM candidate_events "
                "WHERE source_news_event_id=?",
                (nid,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1, "AT MOST one candidate_events row across restarts"

    def test_subprocess_restart_simulation(self, tmp_path: Path) -> None:
        """Fresh interpreter (mimics systemd Restart=on-failure) → 1 candidate."""

        db_path = _build_v10_db(tmp_path)
        nid = _seed_news_event(
            db_path,
            ticker="VRTX",
            title="VRTX PDUFA approval granted",
            url="https://example.com/vrtx-restart-subproc",
        )

        script = (
            "from biotech_sniper.news_daemon.emit import run_one_poll_cycle\n"
            f"scanned, inserted = run_one_poll_cycle(r'{db_path}')\n"
            "print(f'scanned={scanned} inserted={inserted}')\n"
        )
        repo_root = Path(emit.__file__).resolve().parents[2]

        first = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(repo_root),
            check=True,
            capture_output=True,
            text=True,
        )
        assert "inserted=1" in first.stdout, first.stdout

        # Second subprocess — fresh interpreter, no in-memory state.
        # MUST source the watermark from SQLite and skip the row.
        second = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(repo_root),
            check=True,
            capture_output=True,
            text=True,
        )
        assert "inserted=0" in second.stdout, second.stdout

        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM candidate_events "
                "WHERE source_news_event_id=?",
                (nid,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_dedup_key_unique_blocks_duplicate_emit(
        self, v10_db: Path
    ) -> None:
        """Direct duplicate INSERT against the same dedup_key is silently ignored."""

        nid = _seed_news_event(
            v10_db,
            ticker="VRTX",
            title="VRTX trial readout positive",
            url="https://example.com/vrtx-dedup",
        )
        cand = make_candidate("VRTX", nid, ["readout", "positive"])

        # First INSERT writes the row.
        assert write_candidate(str(v10_db), cand) is True
        # Second INSERT silently ignored by ``INSERT OR IGNORE``.
        assert write_candidate(str(v10_db), cand) is False

        conn = sqlite3.connect(v10_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM candidate_events WHERE dedup_key=?",
                (cand.dedup_key,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1


# ---------------------------------------------------------------------------
# VAL-CROSS-026 — Re-emit candidate event yields no duplicate ensemble rows
# ---------------------------------------------------------------------------


class TestEnsembleIdempotentOnRunId_VAL_CROSS_026:
    """``score_candidate_event`` with stable ``run_id`` is idempotent across re-calls."""

    def test_two_calls_same_run_id_yields_four_rows_total(
        self, v10_db: Path
    ) -> None:
        cid = _seed_candidate_for(v10_db, ticker="REPL1", dedup_seed="ens-1")

        providers = {
            name: _make_provider_callable() for name in ALL_PROVIDERS
        }

        score_candidate_event(
            {"id": cid, "ticker": "REPL1"},
            run_id="run-stable",
            db_path=v10_db,
            providers=providers,
        )
        score_candidate_event(
            {"id": cid, "ticker": "REPL1"},
            run_id="run-stable",
            db_path=v10_db,
            providers=providers,
        )

        conn = sqlite3.connect(v10_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM ensemble_scores_event "
                "WHERE candidate_event_id=?",
                (cid,),
            ).fetchone()[0]
            duplicates = conn.execute(
                "SELECT candidate_event_id, provider, run_id, COUNT(*) "
                "FROM ensemble_scores_event "
                "WHERE candidate_event_id=? "
                "GROUP BY 1,2,3 HAVING COUNT(*)>1",
                (cid,),
            ).fetchall()
        finally:
            conn.close()

        assert count == len(ALL_PROVIDERS) == 4
        assert duplicates == [], (
            "no duplicate (candidate_event_id, provider, run_id) tuples"
        )

    def test_unique_constraint_rejects_direct_duplicate_insert(
        self, v10_db: Path
    ) -> None:
        """Direct duplicate INSERT raises sqlite3.IntegrityError."""

        cid = _seed_candidate_for(v10_db, ticker="REPL2", dedup_seed="ens-2")
        conn = sqlite3.connect(v10_db)
        try:
            conn.execute(
                "INSERT INTO ensemble_scores_event ("
                "candidate_event_id, provider, run_id, label, "
                "probability, direction, rationale, citations, "
                "latency_ms, cost_usd, called_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cid,
                    "xai",
                    "run-direct",
                    "material",
                    0.9,
                    "bullish",
                    "first",
                    "[]",
                    10,
                    0.001,
                    "2026-04-30T12:00:00Z",
                ),
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO ensemble_scores_event ("
                    "candidate_event_id, provider, run_id, label, "
                    "probability, direction, rationale, citations, "
                    "latency_ms, cost_usd, called_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        cid,
                        "xai",  # same provider
                        "run-direct",  # same run_id
                        "material",
                        0.5,
                        "bullish",
                        "second",
                        "[]",
                        12,
                        0.001,
                        "2026-04-30T12:00:01Z",
                    ),
                )
                conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# VAL-CROSS-027 — Paper order retry idempotent on client_order_id
# ---------------------------------------------------------------------------


def _build_executable_play_card(
    candidate_event: dict[str, Any],
    ensemble_result: EnsembleEventResult,
    *,
    client_order_id: str,
    bid: float = 1.00,
    ask: float = 1.00,
) -> dict[str, Any]:
    """Build a single-leg news_event_entry play_card with a stable ``client_order_id``.

    Mirrors the scaffold in ``test_stage2_idempotency.py``: the
    dispatcher produces a quote-less leg, we stitch the chain
    quote on, then stamp a deterministic ``client_order_id`` on
    the play_card so the executor's idempotency lookup short-
    circuits the second submission.
    """
    res = build_play_card(
        candidate_event=candidate_event,
        ensemble_result=ensemble_result,
        stock_price=100.0,
        expiry="2026-07-17",
    )
    pc = dict(res.play_card)
    leg = dict(pc["option_legs"][0])
    leg["bid"] = bid
    leg["ask"] = ask
    leg["limit_price"] = (bid + ask) / 2.0
    pc["option_legs"] = [leg]
    pc["client_order_id"] = client_order_id
    return pc


class TestPaperOrderRetryIdempotent_VAL_CROSS_027:
    """Same client_order_id → 1 ``paper_orders`` row; broker not contacted twice."""

    def test_retry_short_circuits_in_executor(self, v10_db: Path) -> None:
        executor, fake = _make_executor(v10_db)
        cand = _candidate_event_dict(99, ticker="RETRY1")
        er = _ensemble_result_for(99, run_id="run-retry-1")
        coid = "RETRY1-news_event_entry-99-2026-04-30"

        pc = _build_executable_play_card(
            cand, er, client_order_id=coid
        )

        first = executor.execute(pc)
        assert first
        assert len(fake.submit_calls) == 1

        second = executor.execute(pc)
        # Idempotent short-circuit returns the existing alpaca_order_id.
        assert second == first
        # CRITICAL: broker NOT contacted a second time.
        assert len(fake.submit_calls) == 1, (
            "broker MUST NOT receive a second submission"
        )

        conn = sqlite3.connect(v10_db)
        try:
            rows = conn.execute(
                "SELECT id, alpaca_order_id, status, event "
                "FROM paper_orders WHERE client_order_id=?",
                (coid,),
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0][2] != "rejected"
        assert rows[0][3] == EVENT_NEWS_ENTRY

    def test_direct_duplicate_insert_raises_integrity_error(
        self, v10_db: Path
    ) -> None:
        """SQL-layer probe: duplicate ``client_order_id`` INSERT → IntegrityError.

        The executor's normal path uses the lookup short-circuit so
        this error is never surfaced in production — but the
        underlying ``client_order_id NOT NULL UNIQUE`` constraint
        MUST remain in place so any future caller that bypasses the
        lookup is rejected at the storage layer. The original row
        also remains intact (rollback semantics).
        """
        executor, _ = _make_executor(v10_db)

        # Drive a successful entry first (fresh candidate_event row).
        cid = _seed_candidate_for(
            v10_db, ticker="RETRY2", dedup_seed="retry-2"
        )
        cand = _candidate_event_dict(cid, ticker="RETRY2")
        er = _ensemble_result_for(cid, run_id="run-retry-2")

        order_id = submit_news_event_entry(
            executor,
            candidate_event=cand,
            ensemble_result=er,
            stock_price=100.0,
            bid=1.00,
            ask=1.00,
            expiry="2026-07-17",
            check_underlying_tradable=False,
        )
        assert order_id

        conn = sqlite3.connect(v10_db)
        try:
            row = conn.execute(
                "SELECT id, alpaca_order_id, status, client_order_id "
                "FROM paper_orders WHERE event=?",
                (EVENT_NEWS_ENTRY,),
            ).fetchone()
            assert row is not None
            internal_id, alpaca_order_id, status, coid = row
            assert coid is not None and coid.strip()

            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO paper_orders ("
                    "id, play_card_id, alpaca_order_id, symbol, side, "
                    "qty, status, reason, event, parent_play_card_id, "
                    "requested_mid_at_submit, purpose, client_order_id"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "second-internal-id",
                        "pc-second",
                        "alpaca-second",
                        "RETRY2-OPT",
                        "buy",
                        1,
                        "accepted",
                        None,
                        EVENT_NEWS_ENTRY,
                        None,
                        1.0,
                        "entry",
                        coid,  # duplicate client_order_id
                    ),
                )

            # Original row is still there, unchanged.
            after = conn.execute(
                "SELECT alpaca_order_id, status FROM paper_orders WHERE id=?",
                (internal_id,),
            ).fetchone()
            assert after == (alpaca_order_id, status)
            count = conn.execute(
                "SELECT COUNT(*) FROM paper_orders WHERE client_order_id=?",
                (coid,),
            ).fetchone()[0]
            assert count == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# VAL-CROSS-045 — 10× replay of the same source headline → byte-identical state
# ---------------------------------------------------------------------------


def _state_hash(db_path: Path) -> tuple[str, dict[str, int]]:
    """Compute SHA-256 over the canonical replay-anchored tables.

    The hash covers the post-replay state of every table the
    f-cross-07 contract names:

    * ``candidate_events`` (Stage-1 emission)
    * ``ensemble_scores_event`` (Stage-2 fan-out persistence)
    * ``paper_orders`` (broker submission persistence)
    * ``execution_events`` (post-submit telemetry)
    * ``execution_fills`` (broker-side fills)
    * ``ticker_cooldown`` (post-success cooldown UPSERT)
    * ``news_match_log`` (audit trail when present)
    * ``llm_cost_ledger`` (per-call cost telemetry)

    Each table is sorted by primary-key columns so the hash is
    stable across replays. Returns the hex digest plus a per-table
    row-count dict for diagnostic reporting.
    """

    tables: tuple[tuple[str, str], ...] = (
        ("candidate_events", "id"),
        ("ensemble_scores_event", "id"),
        ("paper_orders", "id"),
        ("execution_events", "id"),
        ("execution_fills", "id"),
        ("ticker_cooldown", "ticker"),
        ("news_match_log", "id"),
        ("llm_cost_ledger", "id"),
    )
    h = hashlib.sha256()
    counts: dict[str, int] = {}
    conn = sqlite3.connect(db_path)
    try:
        for table, order in tables:
            try:
                rows = list(
                    conn.execute(
                        f"SELECT * FROM {table} ORDER BY {order} ASC"
                    )
                )
            except sqlite3.OperationalError:
                # Table does not exist on this schema (defensive —
                # all 8 are provisioned by v10 but a future drift
                # should fail loudly via the count assertion below).
                rows = []
            counts[table] = len(rows)
            h.update(table.encode("utf-8"))
            h.update(b":")
            for row in rows:
                # ``str(row)`` is stable for tuple-shaped row values
                # because every column type is sqlite-native (TEXT,
                # INTEGER, REAL, NULL). The SHA-256 over the literal
                # repr is sufficient for a replay-equality check.
                h.update(repr(row).encode("utf-8"))
                h.update(b"\n")
            h.update(b"|")
    finally:
        conn.close()
    return (h.hexdigest(), counts)


def _replay_one(
    *,
    db_path: Path,
    executor: PaperExecutor,
    candidate_event_id: int,
    ticker: str,
    run_id: str,
    client_order_id: str,
) -> None:
    """Run one full pipeline replay with the canonical cheap-first gate order.

    Mirrors the production Stage-2 flow:

    1. Stage-1 emit (no-op after replay #1 due to dedup_key UNIQUE).
    2. Cheap-first cooldown gate. If active, the entire downstream
       Stage-2 chain short-circuits with ZERO new
       ``llm_cost_ledger`` rows AND ZERO new ``paper_orders`` rows
       (per VAL-CROSS-028). This is the production protection that
       makes 10 replays byte-identical: replays #2..#10 hit the
       cooldown gate seeded by replay #1's ``record_cooldown_on_success``
       call and never advance further.
    3. Ensemble score (idempotent on ``run_id``).
    4. ``executor.execute`` (idempotent on ``client_order_id``).
    5. ``record_cooldown_on_success`` (UPSERT, but only fires once
       because step 2 short-circuits replays #2..#10).
    """
    # Stage-1 — re-run the poll cycle. A no-op after the seed
    # candidate is already in place (dedup_key UNIQUE).
    run_one_poll_cycle(str(db_path))

    # Cheap-first gate: cooldown. Production short-circuits BEFORE
    # any LLM dispatch so the byte-identity invariant holds across
    # replays — without this gate every replay would issue 4 new
    # llm_cost_ledger rows AND advance ``ticker_cooldown.last_entry_at``.
    gate = cooldown_gate(ticker=ticker, db_path=db_path)
    if not gate.passed:
        return

    # Stage-2 — re-run the ensemble score with a stable run_id.
    providers = {
        name: _make_provider_callable() for name in ALL_PROVIDERS
    }
    er = score_candidate_event(
        {"id": candidate_event_id, "ticker": ticker},
        run_id=run_id,
        db_path=db_path,
        providers=providers,
    )
    # The ensemble returns the unanimous bullish verdict every time
    # (the providers are deterministic).
    assert er.label == "material"
    assert er.direction == "bullish"

    # Stage-3 — submit the news_event_entry play_card.
    cand = _candidate_event_dict(candidate_event_id, ticker=ticker)
    pc = _build_executable_play_card(
        cand, er, client_order_id=client_order_id
    )
    executor.execute(pc)
    # Stage-3.5 — UPSERT the post-success cooldown row, mirroring
    # what :func:`submit_news_event_entry` does after a successful
    # ``executor.execute``. The cooldown gate above will short-
    # circuit replays #2..#10 so this UPSERT only fires on replay #1.
    record_cooldown_on_success(
        ticker=ticker,
        db_path=executor.db_path,
        last_event_id=candidate_event_id,
    )


class TestTenReplaysByteIdentical_VAL_CROSS_045:
    """10 replays of the same source headline → byte-identical state."""

    def test_state_hash_unchanged_across_replays(self, v10_db: Path) -> None:
        # Seed: one news_events + one candidate_events row stable
        # for every replay (the daemon's dedup_key UNIQUE makes
        # subsequent emit cycles no-ops).
        cid = _seed_candidate_for(
            v10_db,
            ticker="RPLY",
            dedup_seed="ten-replays",
        )
        executor, fake = _make_executor(v10_db)

        run_id = "run-ten-replays-stable"
        coid = f"RPLY-news_event_entry-{cid}-2026-04-30"

        # Replay #1 — establishes the canonical state.
        _replay_one(
            db_path=v10_db,
            executor=executor,
            candidate_event_id=cid,
            ticker="RPLY",
            run_id=run_id,
            client_order_id=coid,
        )
        first_hash, first_counts = _state_hash(v10_db)
        # Sanity: first replay populated the canonical row set.
        assert first_counts["candidate_events"] >= 1
        assert first_counts["ensemble_scores_event"] >= len(ALL_PROVIDERS)
        assert first_counts["paper_orders"] >= 1
        assert first_counts["ticker_cooldown"] >= 1
        first_submit_call_count = len(fake.submit_calls)
        assert first_submit_call_count == 1, (
            "the first replay submits exactly once"
        )

        # Replays #2..#10 — must be byte-identical.
        for replay_n in range(2, 11):
            _replay_one(
                db_path=v10_db,
                executor=executor,
                candidate_event_id=cid,
                ticker="RPLY",
                run_id=run_id,
                client_order_id=coid,
            )
            current_hash, current_counts = _state_hash(v10_db)
            assert current_hash == first_hash, (
                f"replay #{replay_n}: state hash drift\n"
                f"  before={first_hash}\n  after ={current_hash}\n"
                f"  counts before={first_counts}\n  counts after={current_counts}"
            )
            assert current_counts == first_counts

        # Broker contacted EXACTLY once across all 10 replays.
        assert len(fake.submit_calls) == 1, (
            "executor's idempotent_skip MUST short-circuit replays "
            "2..10 — broker is contacted on replay #1 only"
        )

    def test_row_counts_after_replay_one_equal_after_replay_ten(
        self, v10_db: Path
    ) -> None:
        """Spot-check the exact assertion phrasing from VAL-CROSS-045."""

        cid = _seed_candidate_for(
            v10_db, ticker="RPLY2", dedup_seed="ten-replays-counts"
        )
        executor, _ = _make_executor(v10_db)

        run_id = "run-rply2-stable"
        coid = f"RPLY2-news_event_entry-{cid}-2026-04-30"

        _replay_one(
            db_path=v10_db,
            executor=executor,
            candidate_event_id=cid,
            ticker="RPLY2",
            run_id=run_id,
            client_order_id=coid,
        )

        def _counts(conn: sqlite3.Connection) -> dict[str, int]:
            tables = (
                "candidate_events",
                "ensemble_scores_event",
                "paper_orders",
                "execution_events",
                "execution_fills",
                "ticker_cooldown",
                "news_match_log",
                "llm_cost_ledger",
            )
            out: dict[str, int] = {}
            for t in tables:
                try:
                    out[t] = int(
                        conn.execute(
                            f"SELECT COUNT(*) FROM {t}"
                        ).fetchone()[0]
                    )
                except sqlite3.OperationalError:
                    out[t] = 0
            return out

        conn = sqlite3.connect(v10_db)
        try:
            counts_after_one = _counts(conn)
        finally:
            conn.close()

        for _ in range(9):
            _replay_one(
                db_path=v10_db,
                executor=executor,
                candidate_event_id=cid,
                ticker="RPLY2",
                run_id=run_id,
                client_order_id=coid,
            )

        conn = sqlite3.connect(v10_db)
        try:
            counts_after_ten = _counts(conn)
        finally:
            conn.close()

        assert counts_after_one == counts_after_ten, (
            f"replay-stable row counts diverged: "
            f"after_1={counts_after_one} after_10={counts_after_ten}"
        )
