"""End-to-end Bull-flow regression test (feature ``f-m5-01-bull-e2e``).

Walks the full Reading-B happy path on a synthetic Russell-2000
biotech ticker:

1. **Stage-1** — a synthetic ``news_events`` row with a Tier-1
   catalyst keyword (``"primary endpoint"``) drives
   :func:`biotech_sniper.news_daemon.emit.run_one_poll_cycle` and
   produces exactly one ``candidate_events`` row, idempotent on
   ``dedup_key`` (VAL-M5-001 / VAL-M5-002).
2. **Stage-2** — :func:`biotech_sniper.exec.stage2_dispatcher.run_stage2_chain`
   walks the cheap-first chain (cooldown → armed → cap → fan-out)
   with four stub providers returning unanimous
   ``label='material'``, ``direction='bullish'`` at probability
   ``0.85``. Exactly four ``ensemble_scores_event`` rows are
   written (VAL-M5-003).
3. **Paper executor** — :func:`biotech_sniper.exec.stage2_paper_executor.submit_news_event_entry`
   writes the ``paper_orders`` row PRE-Alpaca submit
   (write-then-submit invariant, VAL-M5-004), materialises an OTM
   **call** leg with ``strike > spot`` (VAL-M5-005), and on a
   cassette-backed Alpaca paper accept records the
   ``execution_events`` lifecycle (``submitted → accepted →
   filled``) plus an ``execution_fills`` row (VAL-M5-006).
4. **Cooldown** — the successful submit upserts a ``ticker_cooldown``
   row; a retry of ``run_stage2_chain`` within the 24h window is
   blocked PRE-LLM, leaving the ``ensemble_scores_event``,
   ``llm_cost_ledger``, and ``paper_orders`` row counts unchanged
   (VAL-M5-007).
5. **Audit** — :func:`biotech_sniper.reports.reading_b_audit_summary.write_reading_b_summary`
   merges the Reading-B run summary into ``state/audit_latest.json``
   with ``gate_pass_count == 1`` and
   ``news_event_entries_submitted == 1`` (VAL-M5-008).

Hermeticity
-----------

The test never touches the real Alpaca paper API or any LLM
endpoint. The four ensemble providers are stubbed with
deterministic callables; the broker is a duck-typed
:class:`_FakeAlpacaClient` whose ``submit_order`` / ``get_order``
responses are sourced from
``tests/fixtures/cassettes/alpaca/order_call_roundtrip.json`` so the
order shape mirrors a recorded paper-API roundtrip.

The test is decorated with ``@pytest.mark.e2e`` so it is selected
by ``pytest -m e2e``. There is no ``RUN_E2E`` skip gate inside the
test body — the test is fully hermetic and runs deterministically
even when ``RUN_E2E=1`` is unset; the marker just keeps it grouped
with the broader e2e suite.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.exec.stage2_dispatcher import (
    EVENT_NEWS_ENTRY,
    Stage2ChainResult,
    run_stage2_chain,
)
from biotech_sniper.exec.stage2_paper_executor import submit_news_event_entry
from biotech_sniper.llm.ensemble import ALL_PROVIDERS
from biotech_sniper.migrations.runner import run as run_migrations_runner
from biotech_sniper.news_daemon.emit import (
    compute_dedup_key,
    run_one_poll_cycle,
)
from biotech_sniper.paper_executor import PaperExecutor
from biotech_sniper.reports.reading_b_audit_summary import (
    write_reading_b_summary,
)


pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Cassette
# ---------------------------------------------------------------------------


_CASSETTE_DIR = (
    Path(__file__).resolve().parent.parent / "fixtures" / "cassettes" / "alpaca"
)


def _load_cassette(name: str) -> dict[str, Any]:
    return json.loads((_CASSETTE_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Synthetic fixture data
# ---------------------------------------------------------------------------


_TICKER: str = "VKTX"
_HEADLINE: str = "VKTX phase 3 readout: primary endpoint met"
_SOURCE: str = "test_bull_flow"
_PUBLISHED_AT: str = "2026-04-30T12:00:00.000Z"
_STOCK_PRICE: float = 100.0
_BID: float = 1.10
_ASK: float = 1.30  # mid = 1.20 → qty = 2 (≤ $250 cap)
_EXPIRY: str = "2026-07-17"


# ---------------------------------------------------------------------------
# Fake Alpaca client double
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Duck-typed Alpaca client that walks accepted → filled deterministically.

    The Stage-2 wiring uses ``get_latest_trade`` (underlying probe),
    ``get_positions`` (caps), and ``submit_order`` (order entry).
    The execution subscriber subsequently calls ``get_order`` to
    walk the order to a terminal ``filled`` state.

    The ``get_order_results`` queue is consumed left-to-right; the
    last entry sticks (mirroring a terminal status that re-polls
    keep returning).
    """

    def __init__(
        self,
        *,
        base_url: str = PAPER_BASE_URL,
        positions: Optional[list[dict[str, Any]]] = None,
        submit_order_result: Any = None,
        get_order_results: Optional[list[Any]] = None,
        latest_trade_price: Optional[float] = _STOCK_PRICE,
    ) -> None:
        self.base_url = base_url
        self._positions = list(positions or [])
        self._submit_result = submit_order_result
        self._get_order_results = list(get_order_results or [])
        self._latest_trade_price = latest_trade_price
        self.submit_calls: list[Any] = []
        self.get_order_calls: list[str] = []
        self.get_positions_calls: int = 0
        self.get_latest_trade_calls: list[str] = []

    # ------------------------------------------------------------------
    # AlpacaClient surface
    # ------------------------------------------------------------------

    def get_positions(self) -> list[dict[str, Any]]:
        self.get_positions_calls += 1
        return list(self._positions)

    def submit_order(self, order_request: Any) -> dict[str, Any]:
        self.submit_calls.append(order_request)
        if self._submit_result is None:
            raise AssertionError(
                "_FakeAlpacaClient.submit_order called but no result queued"
            )
        return self._submit_result

    def get_order(self, order_id: str) -> dict[str, Any]:
        self.get_order_calls.append(order_id)
        if not self._get_order_results:
            raise AssertionError(
                "_FakeAlpacaClient.get_order called but no result queued"
            )
        result = self._get_order_results[0]
        if len(self._get_order_results) > 1:
            self._get_order_results.pop(0)
        return result

    def get_latest_trade(self, ticker: str) -> Optional[float]:
        self.get_latest_trade_calls.append(ticker)
        return self._latest_trade_price


# ---------------------------------------------------------------------------
# Stub provider factory — deterministic unanimous bullish material
# ---------------------------------------------------------------------------


class _StubProviderTracker:
    """Tracks provider invocations so we can prove cooldown short-circuits."""

    def __init__(self) -> None:
        self.call_counts: dict[str, int] = {p: 0 for p in ALL_PROVIDERS}

    def make_providers(
        self,
        *,
        label: str = "material",
        direction: str = "bullish",
        probability: float = 0.85,
    ) -> dict[str, Any]:
        """Return a ``{provider_name: callable}`` mapping for the ensemble."""

        def _factory(provider_name: str):
            def _stub(
                _candidate_row: Any, *, name: str = provider_name
            ) -> dict[str, Any]:
                # Each Stage-2 provider callable is invoked by the
                # ensemble fan-out as ``callable_(candidate, name=name)``
                # and MUST return a plain mapping; the ensemble then
                # normalises the dict into a :class:`ProviderResult`
                # via :func:`_normalise_provider_payload`. Returning a
                # pre-built ``ProviderResult`` here would trip the
                # ``isinstance(raw, Mapping)`` guard inside
                # :func:`_run_one_provider` and the worker would record
                # ``error='TypeError: ...'`` for every leg.
                self.call_counts[name] += 1
                return {
                    "label": label,
                    "probability": probability,
                    "direction": direction,
                    "rationale": f"{name}-stub-bullish",
                    "citations": [],
                    "latency_ms": 10,
                    "cost_usd": 0.001,
                }

            return _stub

        return {p: _factory(p) for p in ALL_PROVIDERS}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Bring a fresh sqlite db up to schema v10 (Reading-B foundations)."""
    db = tmp_path / "alpha_sniper_e2e.db"
    run_migrations_runner(db, target_version=11, take_backup_first=False)
    return db


@pytest.fixture
def armed_path(tmp_path: Path) -> Path:
    """Create a dummy ``.armed`` marker so the armed gate passes."""
    p = tmp_path / ".armed"
    p.write_text("e2e-bull-flow", encoding="utf-8")
    return p


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    """Path used for the ``audit_latest.json`` Reading-B summary write."""
    return tmp_path / "state" / "audit_latest.json"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _seed_synthetic_news_event(db_path: Path) -> int:
    """Insert one synthetic news_events row and return its id."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            """
            INSERT INTO news_events (
                ticker, source, published_at, title, url, raw_payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _TICKER,
                _SOURCE,
                _PUBLISHED_AT,
                _HEADLINE,
                "https://example.com/vktx-readout",
                _HEADLINE,  # body == headline for matcher purposes
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _candidate_event_row(db_path: Path, news_event_id: int) -> dict[str, Any]:
    """Return the candidate_events row keyed by source_news_event_id."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT id, ticker, source_news_event_id, matched_keywords,
                   calendar_match, emitted_at, dedup_key
            FROM candidate_events
            WHERE source_news_event_id = ?
            """,
            (news_event_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "candidate_events row missing"
    return dict(row)


def _count(conn_or_path: Any, sql: str, params: tuple = ()) -> int:
    if isinstance(conn_or_path, sqlite3.Connection):
        return int(conn_or_path.execute(sql, params).fetchone()[0])
    conn = sqlite3.connect(str(conn_or_path))
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The single end-to-end test (one orchestration covers VAL-M5-001..008)
# ---------------------------------------------------------------------------


def test_bull_flow_end_to_end(
    db_path: Path,
    armed_path: Path,
    audit_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full Reading-B Bull happy-path regression.

    Walks every M5 contract assertion (``VAL-M5-001`` ..
    ``VAL-M5-008``) inline so the end-to-end contract is provable
    in a single orchestration without sharing mutable state across
    test functions.
    """

    # Capture wall-clock test-start for the VAL-M5-002 emitted_at
    # freshness assertion further down (the contract requires
    # ``emitted_at`` parseable as ISO-8601 within 60s of the test
    # start, not just a trailing-Z sanity check). Uses the
    # timezone-aware ``datetime.now(timezone.utc)`` per project
    # hygiene rule in ``tests/test_no_utcnow.py``.
    _test_started_at = _dt.datetime.now(_dt.timezone.utc)

    # ---------- VAL-M5-001 — Stage-1 emits exactly one candidate_events row.
    news_event_id = _seed_synthetic_news_event(db_path)
    assert news_event_id > 0

    polled_tickers = [_TICKER]
    scanned_first, inserted_first = run_one_poll_cycle(
        str(db_path),
        polled_tickers=polled_tickers,
        after_id=0,
    )
    assert scanned_first == 1, "first poll cycle should scan the seed row"
    assert inserted_first == 1, "first poll cycle should insert one candidate"

    candidate = _candidate_event_row(db_path, news_event_id)
    assert candidate["ticker"] == _TICKER
    matched_keywords = candidate["matched_keywords"]
    # Stage-1 stores matched_keywords as a sorted, comma-joined CSV
    # of normalised keywords (NOT a JSON array — see
    # :func:`biotech_sniper.news_daemon.emit.make_candidate`). The
    # contract requires a non-empty match set.
    assert isinstance(matched_keywords, str) and matched_keywords.strip(), (
        f"candidate_events.matched_keywords must be non-empty; got {matched_keywords!r}"
    )
    matched_list = [kw for kw in matched_keywords.split(",") if kw]
    assert "primary endpoint" in matched_list, (
        f"matched_keywords must contain 'primary endpoint'; got {matched_list!r}"
    )

    # Verify the dedup_key matches the documented hash of the
    # canonical (ticker, news_event_id, sorted-CSV-keywords) triple.
    expected_dedup = compute_dedup_key(_TICKER, news_event_id, matched_list)
    assert candidate["dedup_key"] == expected_dedup

    # Re-running Stage-1 against the same news_events row inserts
    # zero additional candidate_events rows (idempotent on
    # dedup_key, VAL-M5-001).
    scanned_second, inserted_second = run_one_poll_cycle(
        str(db_path),
        polled_tickers=polled_tickers,
        after_id=0,  # force a re-scan — emitting the same dedup_key is a no-op
    )
    assert scanned_second == 1
    assert inserted_second == 0
    assert _count(db_path, "SELECT COUNT(*) FROM candidate_events") == 1

    # ---------- VAL-M5-002 — matched_keywords + calendar_match metadata.
    # ``calendar_match`` is a nullable TEXT field on the v10 schema
    # (NULL when no trial_calendar row matched). The synthetic row
    # has no calendar entry, so we expect NULL — the contract just
    # requires the column to be queryable and to carry an
    # ISO-8601 emitted_at within 60 seconds of the test, which we
    # assert below.
    assert candidate["calendar_match"] is None
    assert candidate["emitted_at"] and candidate["emitted_at"].endswith("Z")
    # VAL-M5-002 (freshness): parse emitted_at as ISO-8601 and
    # assert its age in seconds against ``_test_started_at`` is
    # within ``[0, 60]``. The trailing-Z check above is necessary
    # but not sufficient — the contract requires the timestamp to
    # be fresh wall-clock (within ~60s of test start), not merely
    # well-formed.
    _emitted_at_raw = candidate["emitted_at"]
    # ``datetime.fromisoformat`` only learned to accept the literal
    # ``Z`` suffix in 3.11; to keep parity with the project's
    # 3.10-on-VPS floor we strip the trailing ``Z`` and parse,
    # then attach ``timezone.utc`` so the subtraction below is
    # tz-aware on both sides (``_test_started_at`` is tz-aware via
    # ``datetime.now(timezone.utc)``).
    _emitted_at_parsed = _dt.datetime.fromisoformat(
        _emitted_at_raw[:-1] if _emitted_at_raw.endswith("Z") else _emitted_at_raw
    )
    if _emitted_at_parsed.tzinfo is None:
        _emitted_at_parsed = _emitted_at_parsed.replace(tzinfo=_dt.timezone.utc)
    _age_seconds = (_emitted_at_parsed - _test_started_at).total_seconds()
    assert 0.0 <= _age_seconds <= 60.0, (
        f"VAL-M5-002: emitted_at must be within 60s of test start; "
        f"emitted_at={_emitted_at_raw!r} test_started_at={_test_started_at} "
        f"age_seconds={_age_seconds}"
    )

    # ---------- VAL-M5-003 — Stage-2 ensemble runs all 4 providers.
    tracker = _StubProviderTracker()
    providers = tracker.make_providers()

    chain_result: Stage2ChainResult = run_stage2_chain(
        candidate_event_row=candidate,
        db_path=db_path,
        armed_path=armed_path,
        providers=providers,
    )
    assert chain_result.passed is True
    assert chain_result.gate is None
    assert chain_result.reason is None

    ensemble = chain_result.ensemble_result
    assert ensemble is not None
    # Four-provider unanimous bullish material with avg p == 0.85.
    assert ensemble.label == "material"
    assert ensemble.direction == "bullish"
    assert ensemble.mean_probability >= 0.75
    assert sorted(ensemble.successful_providers) == sorted(ALL_PROVIDERS)
    assert ensemble.failed_providers == []

    # Each provider stub fired exactly once.
    assert all(tracker.call_counts[p] == 1 for p in ALL_PROVIDERS), (
        tracker.call_counts
    )

    # Exactly four ensemble_scores_event rows persisted.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT provider, label, direction, probability "
            "FROM ensemble_scores_event "
            "WHERE candidate_event_id = ? "
            "ORDER BY provider",
            (candidate["id"],),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 4
    persisted_providers = {r["provider"] for r in rows}
    assert persisted_providers == set(ALL_PROVIDERS)
    for r in rows:
        assert r["label"] == "material"
        assert r["direction"] == "bullish"
        assert 0.0 <= float(r["probability"]) <= 1.0

    # ---------- VAL-M5-004 + VAL-M5-005 — paper_orders + OTM call leg.
    cassette = _load_cassette("order_call_roundtrip.json")

    # Re-stamp the cassette timestamps with strictly-increasing
    # wall-clock instants ahead of the executor's "submitted"
    # write, otherwise the state-machine validator (which orders
    # ``execution_events`` by ``event_at DESC``) would see the
    # cassette-vintage 2025 timestamp as STALE and reject the
    # ``submitted -> accepted`` transition.
    now = _dt.datetime.now(_dt.timezone.utc)

    def _iso(offset_seconds: int) -> str:
        ts = now + _dt.timedelta(seconds=offset_seconds)
        return (
            ts.strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{ts.microsecond // 1000:03d}Z"
        )

    submit_payload = dict(cassette["submit"])
    submit_payload["created_at"] = _iso(0)
    submit_payload["updated_at"] = _iso(0)
    submit_payload["submitted_at"] = _iso(0)

    poll_intermediate_payload = dict(cassette["poll_intermediate"])
    poll_intermediate_payload["updated_at"] = _iso(60)
    poll_intermediate_payload["submitted_at"] = _iso(0)

    poll_filled_payload = dict(cassette["poll_filled"])
    poll_filled_payload["updated_at"] = _iso(120)
    poll_filled_payload["filled_at"] = _iso(120)
    poll_filled_payload["submitted_at"] = _iso(0)

    fake_client = _FakeAlpacaClient(
        submit_order_result=submit_payload,
        get_order_results=[
            # First poll → still accepted (advances submitted → accepted).
            poll_intermediate_payload,
            # Second poll → filled (advances accepted → filled,
            # writes execution_fills row).
            poll_filled_payload,
        ],
    )

    executor = PaperExecutor(
        fake_client,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )

    paper_orders_pre = _count(db_path, "SELECT COUNT(*) FROM paper_orders")

    alpaca_order_id = submit_news_event_entry(
        executor,
        candidate_event=candidate,
        ensemble_result=ensemble,
        stock_price=_STOCK_PRICE,
        bid=_BID,
        ask=_ASK,
        expiry=_EXPIRY,
    )
    assert isinstance(alpaca_order_id, str) and alpaca_order_id

    paper_orders_post = _count(db_path, "SELECT COUNT(*) FROM paper_orders")
    assert paper_orders_post == paper_orders_pre + 1, (
        "exactly one new paper_orders row must be written"
    )

    # paper_orders row carries event='news_event_entry', purpose='entry',
    # non-null client_order_id and requested_mid_at_submit (write-then-submit
    # invariant — the row is INSERTed BEFORE the broker call, so by the
    # time submit_news_event_entry returns the row is durable on disk).
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        po_row = conn.execute(
            "SELECT event, purpose, side, qty, status, symbol, "
            "       client_order_id, requested_mid_at_submit "
            "FROM paper_orders WHERE alpaca_order_id = ?",
            (alpaca_order_id,),
        ).fetchone()
    finally:
        conn.close()
    assert po_row is not None
    assert po_row["event"] == EVENT_NEWS_ENTRY == "news_event_entry"
    assert po_row["purpose"] == "entry"
    assert po_row["side"] == "buy"
    assert int(po_row["qty"]) >= 1
    assert po_row["client_order_id"] is not None
    assert (po_row["client_order_id"] or "").strip() != ""
    assert po_row["requested_mid_at_submit"] is not None
    assert float(po_row["requested_mid_at_submit"]) > 0.0

    # OTM call leg: the broker submit request must carry an option
    # symbol whose option-type tag is 'C' and whose embedded strike
    # is strictly above the underlying spot at submit time.
    assert len(fake_client.submit_calls) == 1
    submit_request = fake_client.submit_calls[0]
    submitted_symbol = getattr(submit_request, "symbol", None)
    assert submitted_symbol, "submitted order must carry an OCC symbol"
    # OCC option symbol form: ROOT + YYMMDD + (C|P) + strike8.
    assert "C" in submitted_symbol[-9:-8] or submitted_symbol[-9] == "C", (
        f"bullish flow must produce a CALL leg; got {submitted_symbol!r}"
    )
    # Parse strike from the OCC symbol's last 8 chars (millis precision).
    strike_int_millis = int(submitted_symbol[-8:])
    strike_dollars = strike_int_millis / 1000.0
    assert strike_dollars > _STOCK_PRICE, (
        f"OTM call must have strike > spot; got strike={strike_dollars} "
        f"spot={_STOCK_PRICE}"
    )

    # No put-flavored paper_orders row was written.
    assert (
        _count(
            db_path,
            "SELECT COUNT(*) FROM paper_orders "
            "WHERE event = 'news_event_entry' AND symbol LIKE '%P%' "
            "AND symbol NOT LIKE '%C%'",
        )
        == 0
    )

    # ---------- VAL-M5-006 — execution_events + execution_fills written.
    # The executor itself wrote a 'submitted' lifecycle row; the
    # execution subscriber walks the broker state through
    # accepted → filled by polling twice.
    from biotech_sniper.execution_subscriber import ExecutionSubscriber

    subscriber = ExecutionSubscriber(fake_client, db_path=db_path)

    # First poll: broker reports 'accepted' → records 'accepted'.
    n_first = subscriber.poll_once()
    assert n_first == 1
    # Second poll: broker reports 'filled' → records 'filled' AND
    # writes an execution_fills row via record_fill.
    n_second = subscriber.poll_once()
    assert n_second == 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        events = conn.execute(
            "SELECT event_type FROM execution_events "
            "WHERE paper_order_id = (SELECT id FROM paper_orders "
            "                        WHERE alpaca_order_id = ?) "
            "ORDER BY event_at ASC, id ASC",
            (alpaca_order_id,),
        ).fetchall()
        fills = conn.execute(
            "SELECT filled_qty, filled_price, filled_at "
            "FROM execution_fills "
            "WHERE paper_order_id = (SELECT id FROM paper_orders "
            "                        WHERE alpaca_order_id = ?)",
            (alpaca_order_id,),
        ).fetchall()
    finally:
        conn.close()

    event_types = [row["event_type"] for row in events]
    # Sequence must include 'submitted' (executor write), 'accepted'
    # and 'filled' (subscriber writes) in that monotonic order.
    assert event_types == ["submitted", "accepted", "filled"], event_types
    assert len(fills) == 1
    assert int(fills[0]["filled_qty"]) > 0
    assert float(fills[0]["filled_price"]) > 0.0
    assert (fills[0]["filled_at"] or "").endswith("Z")

    # ---------- VAL-M5-007 — 24h cooldown row + retry blocks PRE-call.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cd_rows = conn.execute(
            "SELECT ticker, last_entry_at, cooldown_hours "
            "FROM ticker_cooldown WHERE ticker = ?",
            (_TICKER,),
        ).fetchall()
    finally:
        conn.close()
    assert len(cd_rows) == 1, cd_rows
    cd = cd_rows[0]
    assert cd["ticker"] == _TICKER
    assert cd["last_entry_at"]
    assert int(cd["cooldown_hours"]) == 24

    # Snapshot pre-retry counts.
    pre_paper_orders = _count(db_path, "SELECT COUNT(*) FROM paper_orders")
    pre_ensemble_rows = _count(
        db_path, "SELECT COUNT(*) FROM ensemble_scores_event"
    )
    pre_llm_cost = _count(
        db_path, "SELECT COUNT(*) FROM llm_cost_ledger"
    )
    # VAL-M5-007 (cooldown retry execution-side invariants):
    # snapshot the execution_events / execution_fills row counts
    # so the post-retry assertion below can prove the cooldown
    # short-circuit leaves them strictly unchanged. The cooldown
    # gate fires PRE-LLM and PRE-broker, so neither table should
    # gain a row on the retry; without these snapshots the prior
    # version of the test only checked paper_orders /
    # ensemble_scores_event / llm_cost_ledger and silently lost
    # coverage of the broker-side side-effect invariant.
    pre_exec_events = _count(
        db_path, "SELECT COUNT(*) FROM execution_events"
    )
    pre_exec_fills = _count(
        db_path, "SELECT COUNT(*) FROM execution_fills"
    )
    pre_call_counts = dict(tracker.call_counts)
    pre_alpaca_calls = len(fake_client.submit_calls)

    # Retry: same candidate_event row → cooldown_gate fires first
    # in the cheap-first chain → run_stage2_chain returns
    # passed=False with reason='cooldown_active' BEFORE any LLM
    # provider stub is invoked (VAL-M3-061 / VAL-M3-070 / VAL-M5-007).
    retry_result = run_stage2_chain(
        candidate_event_row=candidate,
        db_path=db_path,
        armed_path=armed_path,
        providers=providers,
    )
    assert retry_result.passed is False
    assert retry_result.gate == "cooldown"
    assert retry_result.reason == "cooldown_active"
    assert retry_result.ensemble_result is None

    # Zero new rows of any kind, zero new provider calls, zero new
    # broker calls.
    assert _count(db_path, "SELECT COUNT(*) FROM paper_orders") == pre_paper_orders
    assert (
        _count(db_path, "SELECT COUNT(*) FROM ensemble_scores_event")
        == pre_ensemble_rows
    )
    assert _count(db_path, "SELECT COUNT(*) FROM llm_cost_ledger") == pre_llm_cost
    # VAL-M5-007 (cooldown retry execution-side invariants): the
    # cooldown gate fires PRE-broker, so neither execution_events
    # nor execution_fills may gain a row on the retry.
    assert (
        _count(db_path, "SELECT COUNT(*) FROM execution_events")
        == pre_exec_events
    ), (
        "VAL-M5-007: execution_events row count must be unchanged "
        "across the cooldown-rejected retry"
    )
    assert (
        _count(db_path, "SELECT COUNT(*) FROM execution_fills")
        == pre_exec_fills
    ), (
        "VAL-M5-007: execution_fills row count must be unchanged "
        "across the cooldown-rejected retry"
    )
    assert tracker.call_counts == pre_call_counts
    assert len(fake_client.submit_calls) == pre_alpaca_calls

    # ---------- VAL-M5-008 — audit_latest.json reading_b summary.
    payload = write_reading_b_summary(audit_path, db_path=db_path)
    assert audit_path.is_file()

    rb = payload.get("reading_b")
    assert isinstance(rb, dict), payload
    assert isinstance(rb["candidate_events_emitted"], int)
    assert rb["candidate_events_emitted"] >= 1
    assert rb["gate_pass_count"] == 1
    assert rb["news_event_entries_submitted"] == 1
    assert isinstance(rb["gate_reject_counts"], dict)
    for reason, cnt in rb["gate_reject_counts"].items():
        assert isinstance(reason, str) and reason
        assert isinstance(cnt, int) and cnt >= 0

    # The on-disk JSON must round-trip byte-for-byte to the returned
    # payload (no silent dropped keys).
    loaded = json.loads(audit_path.read_text(encoding="utf-8"))
    assert loaded == payload
    assert loaded["reading_b"]["gate_pass_count"] == 1
    assert loaded["reading_b"]["news_event_entries_submitted"] == 1


# ---------------------------------------------------------------------------
# Smoke import — keeps `pytest --collect-only` green even when the
# heavy fixtures are not exercised (no-op on import).
# ---------------------------------------------------------------------------


def test_module_imports_cleanly() -> None:
    """Defensive smoke test: every named import above resolves at module load."""
    assert "tests.e2e.test_bull_flow" in sys.modules or __name__.endswith(
        "test_bull_flow"
    )
