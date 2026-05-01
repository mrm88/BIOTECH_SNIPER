"""Stage-2 idempotency tests — feature ``f-m3-11-paper-roundtrip-e2e``.

Hermetic (no live network) coverage of the three idempotency
invariants enforced at the SQLite layer for the Stage-2
``news_event_entry`` path:

* **VAL-M3-068** — Ensemble scoring is idempotent on
  ``(candidate_event_id, provider, run_id)``. Two
  :func:`score_candidate_event` calls with the SAME ``run_id`` for
  the SAME ``candidate_event_id`` produce 4 ``ensemble_scores_event``
  rows total (one per provider), not 8. A FRESH ``run_id`` is the
  documented escape hatch for forced re-evaluation; that path
  produces a second 4-row score-set (8 total).
* **VAL-M3-069** — Entry is idempotent via the
  ``paper_orders.client_order_id`` ``NOT NULL UNIQUE`` constraint.
  Re-running :func:`submit_news_event_entry` with the same
  deterministic ``client_order_id`` short-circuits inside the
  executor's ``_lookup_by_client_order_id`` probe; no second
  ``paper_orders`` row is written, the broker is NOT contacted a
  second time, and the original order's ``alpaca_order_id`` is
  returned. A direct attempt to bypass the lookup and INSERT a
  duplicate row raises ``sqlite3.IntegrityError``, leaving the
  original row intact.
* **VAL-M3-070** — Cooldown UPSERT is idempotent. Two successful
  entries on the same ticker resolve to exactly ONE
  ``ticker_cooldown`` row (PRIMARY KEY is ``ticker``); the second
  call updates ``last_entry_at`` rather than inserting a duplicate.

Convention: this file holds the canonical test bodies. The
verification command for the feature is
``.venv/bin/pytest -q tests/test_stage2_idempotency.py``. A thin
shim under ``tests/llm/test_ensemble_event.py`` re-exports the
ensemble idempotency case via the
``test_idempotent_per_run_id`` node id so the validation contract
evidence command in VAL-M3-068 collects without duplicating logic.
A shim under ``tests/exec/test_stage2_idempotency.py`` re-exports
the ``test_entry_idempotent_on_client_order_id`` case for
VAL-M3-069.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

import pytest

from biotech_sniper import db as project_db
from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.exec.stage2_dispatcher import EVENT_NEWS_ENTRY
from biotech_sniper.exec.stage2_paper_executor import submit_news_event_entry
from biotech_sniper.llm.ensemble import (
    ALL_PROVIDERS,
    EnsembleEventResult,
    ProviderResult,
    score_candidate_event,
)
from biotech_sniper.llm.stage2_gates import record_cooldown_on_success
from biotech_sniper.migrations.runner import run as run_v10
from biotech_sniper.paper_executor import PaperExecutor


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Minimal duck-typed double mirroring
    ``tests.test_stage2_paper_executor._FakeAlpacaClient``.

    Each ``submit_order`` call returns a deterministic broker order
    id keyed off the request's ``client_order_id`` so the
    idempotency assertions can correlate the row against the
    submitted request.
    """

    def __init__(self, *, base_url: str = PAPER_BASE_URL) -> None:
        self.base_url = base_url
        self.submit_calls: list[Any] = []
        self.get_positions_calls: int = 0
        self.get_latest_trade_calls: list[str] = []

    def get_positions(self) -> list[dict[str, Any]]:
        self.get_positions_calls += 1
        return []

    def submit_order(self, order_request: Any) -> dict[str, Any]:
        self.submit_calls.append(order_request)
        coid = getattr(order_request, "client_order_id", None) or "no-coid"
        return {
            "id": f"alpaca-{coid}",
            "client_order_id": coid,
            "symbol": getattr(order_request, "symbol", None),
            "side": "buy",
            "qty": getattr(order_request, "qty", 1),
            "status": "accepted",
        }

    def get_order(self, order_id: str) -> dict[str, Any]:  # pragma: no cover
        return {"id": order_id, "status": "accepted"}

    def get_latest_trade(self, ticker: str) -> Optional[float]:
        self.get_latest_trade_calls.append(ticker)
        return 100.0


def _build_v10_db_with_candidate(
    db_path: Path,
    *,
    ticker: str = "TESTBIO",
    matched_keywords: str = "pdufa",
) -> int:
    """Construct a fresh v10 db and insert a single candidate_events row."""
    conn = project_db.connect(db_path)
    try:
        project_db.run_migrations(conn)
    finally:
        conn.close()
    # f-misc-09: use CURRENT_VERSION so the helper stays
    # forward-compatible with future schema bumps.
    run_v10(db_path, project_db.CURRENT_VERSION, take_backup_first=False)

    conn = project_db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO news_events ("
            "ticker, source, title, url, published_at, ingested_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                ticker,
                "rss",
                f"{ticker} pdufa decision granted",
                f"https://example.com/{ticker.lower()}",
                "2026-04-30T12:00:00Z",
                "2026-04-30T12:00:01Z",
            ),
        )
        nid = conn.execute("SELECT MAX(id) FROM news_events").fetchone()[0]
        conn.execute(
            "INSERT INTO candidate_events ("
            "ticker, source_news_event_id, matched_keywords,"
            " emitted_at, dedup_key) VALUES (?,?,?,?,?)",
            (
                ticker,
                nid,
                matched_keywords,
                "2026-04-30T12:00:02Z",
                f"dedup-{ticker.lower()}-001",
            ),
        )
        cid = conn.execute(
            "SELECT MAX(id) FROM candidate_events"
        ).fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return int(cid)


def _make_provider_callable(
    label: str = "material",
    direction: str = "bullish",
    probability: float = 0.85,
):
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


def _ensemble_result(
    *,
    candidate_event_id: Optional[int] = 42,
    run_id: str = "run-idempotency",
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


def _candidate(
    *,
    id_: int = 42,
    ticker: str = "TESTBIO",
    matched_keywords: str = "pdufa",
) -> dict[str, Any]:
    return {
        "id": id_,
        "ticker": ticker,
        "source_news_event_id": id_ * 10,
        "matched_keywords": matched_keywords,
        "calendar_match": None,
        "emitted_at": "2026-04-30T12:00:00.000000Z",
        "dedup_key": f"dedup-{id_}",
    }


def _make_executor(
    db_path: Path,
) -> tuple[PaperExecutor, _FakeAlpacaClient]:
    fake = _FakeAlpacaClient()
    executor = PaperExecutor(
        fake,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )
    # f-misc-09: track the active CURRENT_VERSION so the helper
    # stays forward-compatible with future schema bumps. Re-asserts
    # the v10/v11 floor (idempotent on a db the executor already
    # bootstrapped via ``db.run_migrations``).
    run_v10(db_path, project_db.CURRENT_VERSION, take_backup_first=False)
    return executor, fake


# ---------------------------------------------------------------------------
# VAL-M3-068 — Ensemble idempotent on (candidate_event_id, provider, run_id)
# ---------------------------------------------------------------------------


def test_idempotent_per_run_id(tmp_path: Path):
    """Two ``score_candidate_event`` calls with the SAME ``run_id`` for
    the same candidate produce 4 ``ensemble_scores_event`` rows total.
    """
    db_path = tmp_path / "alpha.db"
    cand_id = _build_v10_db_with_candidate(db_path, ticker="TESTU")

    providers = {
        name: _make_provider_callable() for name in ALL_PROVIDERS
    }

    # First call.
    er1 = score_candidate_event(
        {"id": cand_id, "ticker": "TESTU"},
        run_id="run-idem-stable",
        db_path=db_path,
        providers=providers,
    )
    assert er1.successful_providers == list(ALL_PROVIDERS)
    assert er1.failed_providers == []

    # Second call with the SAME run_id — must NOT duplicate rows.
    er2 = score_candidate_event(
        {"id": cand_id, "ticker": "TESTU"},
        run_id="run-idem-stable",
        db_path=db_path,
        providers=providers,
    )
    assert er2.successful_providers == list(ALL_PROVIDERS)

    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM ensemble_scores_event "
            "WHERE candidate_event_id=?",
            (cand_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == len(ALL_PROVIDERS) == 4


def test_idempotent_fresh_run_id_creates_new_rows(tmp_path: Path):
    """A FRESH ``run_id`` produces a second 4-row score-set (8 total).

    Documented escape hatch from VAL-M3-068 — operator-driven forced
    re-evaluation under a new ``run_id`` is allowed and produces a
    fully independent row group.
    """
    db_path = tmp_path / "alpha.db"
    cand_id = _build_v10_db_with_candidate(db_path, ticker="TESTU")

    providers = {
        name: _make_provider_callable() for name in ALL_PROVIDERS
    }

    score_candidate_event(
        {"id": cand_id, "ticker": "TESTU"},
        run_id="run-A",
        db_path=db_path,
        providers=providers,
    )
    score_candidate_event(
        {"id": cand_id, "ticker": "TESTU"},
        run_id="run-B",
        db_path=db_path,
        providers=providers,
    )

    conn = sqlite3.connect(db_path)
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM ensemble_scores_event "
            "WHERE candidate_event_id=?",
            (cand_id,),
        ).fetchone()[0]
        per_run = conn.execute(
            "SELECT run_id, COUNT(*) FROM ensemble_scores_event "
            "WHERE candidate_event_id=? GROUP BY run_id",
            (cand_id,),
        ).fetchall()
    finally:
        conn.close()
    assert total == 8
    assert dict(per_run) == {"run-A": 4, "run-B": 4}


# ---------------------------------------------------------------------------
# VAL-M3-069 — Entry idempotent via paper_orders.client_order_id UNIQUE
# ---------------------------------------------------------------------------


def _stable_client_order_id(
    *, ticker: str, candidate_event_id: int, date_iso: str = "2026-04-30"
) -> str:
    """Pattern documented in VAL-M3-069:
    ``f"{ticker}-news_event_entry-{candidate_event_id}-{date}"``.
    """
    return (
        f"{ticker.upper()}-news_event_entry-{candidate_event_id}-{date_iso}"
    )


def test_entry_idempotent_on_client_order_id(tmp_path: Path):
    """Re-running the entry path on the same ``candidate_event_id``
    produces exactly 1 ``paper_orders`` row.

    The deterministic ``client_order_id`` is stitched onto the
    play_card BEFORE submission so the executor's
    ``_lookup_by_client_order_id`` probe short-circuits the second
    call. The broker is NOT contacted a second time.
    """
    db_path = tmp_path / "alpha.db"
    executor, fake = _make_executor(db_path)

    cand = _candidate(id_=42, ticker="TESTBIO")
    er = _ensemble_result(candidate_event_id=42)
    coid = _stable_client_order_id(
        ticker="TESTBIO", candidate_event_id=42
    )

    # First submission.
    play_card_first = {"client_order_id": coid}
    # The wiring builds the play_card internally, so we patch the
    # client_order_id by attaching it to the candidate event mapping
    # via the leg layer — easiest path is to call the executor via
    # the wiring with check_underlying_tradable=False AND attach the
    # client_order_id by post-processing the build. Instead, use a
    # deterministic seed by setting ``client_order_id`` directly on
    # the leg via the chain-quote helper (the
    # ``_derive_client_order_id`` helper accepts both).
    # The simplest path: monkeypatch the executor.execute() return
    # so the same coid is used on both. Use a play_card that already
    # carries client_order_id by manually constructing one.
    from biotech_sniper.exec.stage2_dispatcher import build_play_card

    res = build_play_card(
        candidate_event=cand,
        ensemble_result=er,
        stock_price=100.0,
        expiry="2026-07-17",
    )
    pc = dict(res.play_card)
    leg = dict(pc["option_legs"][0])
    leg["bid"] = 1.00
    leg["ask"] = 1.00
    leg["limit_price"] = 1.00
    pc["option_legs"] = [leg]
    pc["client_order_id"] = coid

    order_id_first = executor.execute(pc)
    assert order_id_first
    assert len(fake.submit_calls) == 1

    # Second submission — same client_order_id.
    order_id_second = executor.execute(pc)
    # The executor's idempotency short-circuit returns the existing
    # ``alpaca_order_id`` (or empty when no broker id was assigned —
    # both are non-error outcomes).
    assert order_id_second == order_id_first

    # Critically: the broker was NOT contacted a second time.
    assert len(fake.submit_calls) == 1, (
        "broker must NOT be re-submitted on idempotent re-call"
    )

    # Exactly 1 paper_orders row carrying the deterministic coid.
    conn = sqlite3.connect(db_path)
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


def test_direct_duplicate_insert_raises_integrity_error(tmp_path: Path):
    """A direct INSERT bypassing ``_lookup_by_client_order_id`` raises
    ``sqlite3.IntegrityError``; the original row remains intact.

    Pin for the SQL-level UNIQUE invariant (VAL-M3-069). The
    executor's normal path uses the lookup short-circuit so this
    error is never surfaced in production — but the underlying
    constraint MUST still be present so any future caller that
    bypasses the lookup is rejected at the storage layer.
    """
    db_path = tmp_path / "alpha.db"
    executor, _ = _make_executor(db_path)

    cand = _candidate(id_=99, ticker="DUPE")
    er = _ensemble_result(candidate_event_id=99)
    coid = _stable_client_order_id(
        ticker="DUPE", candidate_event_id=99
    )

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

    conn = sqlite3.connect(db_path)
    try:
        original = conn.execute(
            "SELECT id, alpaca_order_id, status, event "
            "FROM paper_orders WHERE event=?",
            (EVENT_NEWS_ENTRY,),
        ).fetchone()
        assert original is not None
        original_internal_id, original_alpaca_id, original_status, _ = (
            original
        )
        # Read its real client_order_id (auto-generated by the
        # executor when the play_card omits one) and try to insert
        # a duplicate row that re-uses the SAME client_order_id.
        coid_persisted = conn.execute(
            "SELECT client_order_id FROM paper_orders WHERE id=?",
            (original_internal_id,),
        ).fetchone()[0]
        assert coid_persisted is not None and coid_persisted.strip()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO paper_orders ("
                "id, play_card_id, alpaca_order_id, symbol, side, qty, "
                "status, reason, event, parent_play_card_id, "
                "requested_mid_at_submit, purpose, client_order_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "second-internal-id",
                    "pc-2",
                    "alpaca-2",
                    "TESTSYM",
                    "buy",
                    1,
                    "accepted",
                    None,
                    EVENT_NEWS_ENTRY,
                    None,
                    1.0,
                    "entry",
                    coid_persisted,  # duplicate -> IntegrityError
                ),
            )

        # Original row UNCHANGED.
        after = conn.execute(
            "SELECT alpaca_order_id, status FROM paper_orders WHERE id=?",
            (original_internal_id,),
        ).fetchone()
        assert after == (original_alpaca_id, original_status)

        # Total row count is still exactly 1 — the failed INSERT was
        # rolled back by the IntegrityError.
        count = conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE client_order_id=?",
            (coid_persisted,),
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# VAL-M3-070 — Cooldown UPSERT idempotent
# ---------------------------------------------------------------------------


def _insert_candidate_row(
    db_path: Path,
    *,
    ticker: str,
    matched_keywords: str = "pdufa",
    dedup_seed: str = "wired",
) -> int:
    """Insert a real ``candidate_events`` row so the cooldown
    ``last_event_id`` FK resolves on UPSERT.

    The ``record_cooldown_on_success`` UPSERT references
    ``candidate_events(id)`` via the ``last_event_id`` FK; if the
    candidate doesn't exist, SQLite raises a swallowed
    ``FOREIGN KEY constraint failed`` warning and the row is NOT
    written. Tests that go through the wired path therefore MUST
    insert a real candidate row first.
    """
    conn = project_db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO news_events ("
            "ticker, source, title, url, published_at, ingested_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                ticker,
                "rss",
                f"{ticker} pdufa decision granted",
                f"https://example.com/{ticker.lower()}-{dedup_seed}",
                "2026-04-30T12:00:00Z",
                "2026-04-30T12:00:01Z",
            ),
        )
        nid = conn.execute("SELECT MAX(id) FROM news_events").fetchone()[0]
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
        cid = conn.execute(
            "SELECT MAX(id) FROM candidate_events"
        ).fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return int(cid)


def test_cooldown_upsert_idempotent_via_wired_path(tmp_path: Path):
    """Two successful entries on the same ticker → exactly 1
    ``ticker_cooldown`` row.

    Goes through the full wired ``submit_news_event_entry`` path so
    the cooldown UPSERT side-effect is exercised end-to-end (the
    second call uses a different candidate_event_id so the executor's
    own client_order_id-idempotency does NOT short-circuit it).
    """
    db_path = tmp_path / "alpha.db"
    executor, _ = _make_executor(db_path)

    cand_id_1 = _insert_candidate_row(
        db_path, ticker="MRNA", dedup_seed="wired-1"
    )
    cand_id_2 = _insert_candidate_row(
        db_path, ticker="MRNA", dedup_seed="wired-2"
    )

    # First entry.
    submit_news_event_entry(
        executor,
        candidate_event=_candidate(id_=cand_id_1, ticker="MRNA"),
        ensemble_result=_ensemble_result(
            candidate_event_id=cand_id_1, run_id="run-mrna-1"
        ),
        stock_price=100.0,
        bid=1.00,
        ask=1.00,
        expiry="2026-07-17",
        check_underlying_tradable=False,
    )

    conn = sqlite3.connect(db_path)
    try:
        first = conn.execute(
            "SELECT ticker, last_event_id, last_entry_at "
            "FROM ticker_cooldown WHERE ticker=?",
            ("MRNA",),
        ).fetchone()
    finally:
        conn.close()
    assert first is not None
    assert first[0] == "MRNA"
    assert first[1] == cand_id_1
    first_last_entry_at = first[2]

    # Second entry — same ticker, fresh candidate.
    submit_news_event_entry(
        executor,
        candidate_event=_candidate(id_=cand_id_2, ticker="MRNA"),
        ensemble_result=_ensemble_result(
            candidate_event_id=cand_id_2, run_id="run-mrna-2"
        ),
        stock_price=100.0,
        bid=1.00,
        ask=1.00,
        expiry="2026-07-17",
        check_underlying_tradable=False,
    )

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT ticker, last_event_id, last_entry_at "
            "FROM ticker_cooldown WHERE ticker=?",
            ("MRNA",),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, "ticker_cooldown PK collapses to ONE row"
    second = rows[0]
    assert second[0] == "MRNA"
    assert second[1] == cand_id_2  # advanced to the latest candidate
    # last_entry_at advanced (not strictly required to be greater
    # because the second UPSERT could land on the same millisecond,
    # but inequality covers the canonical case).
    assert second[2] >= first_last_entry_at


def test_cooldown_upsert_idempotent_direct(tmp_path: Path):
    """Direct ``record_cooldown_on_success`` on the same ticker
    resolves to 1 row (PRIMARY KEY collapse, VAL-M3-070).
    """
    db_path = tmp_path / "alpha.db"
    _build_v10_db_with_candidate(db_path, ticker="NVAX")

    record_cooldown_on_success(
        ticker="NVAX", db_path=db_path, last_event_id=None
    )
    record_cooldown_on_success(
        ticker="NVAX", db_path=db_path, last_event_id=None
    )

    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM ticker_cooldown WHERE ticker=?",
            ("NVAX",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1
