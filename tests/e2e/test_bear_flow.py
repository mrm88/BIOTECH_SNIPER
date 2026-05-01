"""End-to-end Bear-flow regression test (feature ``f-m5-02-bear-e2e``).

Walks the full Reading-B happy path on a synthetic Russell-2000
biotech ticker carrying a *bearish* catalyst headline (an FDA
**Complete Response Letter** — a Tier-1 catalyst keyword that
triggers the Stage-1 matcher unambiguously).

The test mirrors :mod:`tests.e2e.test_bull_flow` step-for-step but
flips the direction-dependent legs:

1. **Stage-1** — a synthetic ``news_events`` row with a Tier-1
   catalyst keyword (``"complete response letter"``) drives
   :func:`biotech_sniper.news_daemon.emit.run_one_poll_cycle`
   and produces exactly one ``candidate_events`` row, idempotent
   on ``dedup_key`` (VAL-M5-009 background).
2. **Stage-2** — :func:`biotech_sniper.exec.stage2_dispatcher.run_stage2_chain`
   walks the cheap-first chain (cooldown → armed → cap → fan-out)
   with four stub providers returning unanimous
   ``label='material'``, ``direction='bearish'`` at probability
   ``0.85``. Exactly four ``ensemble_scores_event`` rows are
   written, all with ``direction='bearish'`` and
   ``probability ≥ 0.75`` (VAL-M5-009).
3. **Paper executor** —
   :func:`biotech_sniper.exec.stage2_paper_executor.submit_news_event_entry`
   writes the ``paper_orders`` row PRE-Alpaca submit
   (write-then-submit invariant), materialises an OTM **put** leg
   with ``strike < spot`` and **zero call legs** for the candidate
   (VAL-M5-010), and on a cassette-backed Alpaca paper accept
   records the ``execution_events`` lifecycle
   (``submitted → accepted → filled``) plus an
   ``execution_fills`` row (VAL-M5-011).
4. **Cooldown** — the successful submit upserts a
   ``ticker_cooldown`` row keyed by ``SAVA``; the row is distinct
   from any bull-flow ``VKTX`` cooldown row in the same database
   (VAL-M5-012).
5. **Audit** — :func:`biotech_sniper.reports.reading_b_audit_summary.write_reading_b_summary`
   merges the Reading-B run summary into ``state/audit_latest.json``
   such that ``news_event_entries_submitted`` increments by exactly
   1 vs the pre-bear-run snapshot, and the test exits 0
   (VAL-M5-013).

Hermeticity
-----------

The test never touches the real Alpaca paper API or any LLM
endpoint. The four ensemble providers are stubbed with
deterministic callables; the broker is a duck-typed
:class:`_FakeAlpacaClient` whose ``submit_order`` / ``get_order``
responses are sourced from
``tests/fixtures/cassettes/alpaca/order_put_roundtrip.json`` so the
order shape mirrors a recorded paper-API roundtrip.

The test is decorated with ``@pytest.mark.e2e`` so it is selected
by ``pytest -m e2e``.
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
from biotech_sniper import db as project_db
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
# Synthetic fixture data — bearish flow uses SAVA per VAL-M5-012 so the
# cooldown row is provably distinct from the bull-flow ``VKTX`` ticker.
# ---------------------------------------------------------------------------


_TICKER: str = "SAVA"
_HEADLINE: str = "SAVA receives FDA complete response letter on lead asset"
_SOURCE: str = "test_bear_flow"
_PUBLISHED_AT: str = "2026-04-30T13:00:00.000Z"
_STOCK_PRICE: float = 50.0
_BID: float = 0.80
_ASK: float = 0.90  # mid = 0.85 → qty floor($250 / 0.85 / 100) = 2
_EXPIRY: str = "2026-07-17"


# ---------------------------------------------------------------------------
# Fake Alpaca client double — duck-typed twin of the bull-flow harness.
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Duck-typed Alpaca client that walks accepted → filled deterministically."""

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
# Stub provider factory — deterministic unanimous bearish material.
# ---------------------------------------------------------------------------


class _StubProviderTracker:
    """Tracks provider invocations so we can prove cooldown short-circuits."""

    def __init__(self) -> None:
        self.call_counts: dict[str, int] = {p: 0 for p in ALL_PROVIDERS}

    def make_providers(
        self,
        *,
        label: str = "material",
        direction: str = "bearish",
        probability: float = 0.85,
    ) -> dict[str, Any]:
        """Return a ``{provider_name: callable}`` mapping for the ensemble."""

        def _factory(provider_name: str):
            def _stub(
                _candidate_row: Any, *, name: str = provider_name
            ) -> dict[str, Any]:
                self.call_counts[name] += 1
                return {
                    "label": label,
                    "probability": probability,
                    "direction": direction,
                    "rationale": f"{name}-stub-bearish",
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
    run_migrations_runner(db, target_version=project_db.CURRENT_VERSION, take_backup_first=False)
    return db


@pytest.fixture
def armed_path(tmp_path: Path) -> Path:
    """Create a dummy ``.armed`` marker so the armed gate passes."""
    p = tmp_path / ".armed"
    p.write_text("e2e-bear-flow", encoding="utf-8")
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
                "https://example.com/sava-crl",
                _HEADLINE,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _candidate_event_row(db_path: Path, news_event_id: int) -> dict[str, Any]:
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
# The single end-to-end test (one orchestration covers VAL-M5-009..013)
# ---------------------------------------------------------------------------


def test_bear_flow_end_to_end(
    db_path: Path,
    armed_path: Path,
    audit_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full Reading-B Bear happy-path regression.

    Walks every M5 contract assertion in the bear range
    (``VAL-M5-009`` .. ``VAL-M5-013``) inline so the end-to-end
    contract is provable in a single orchestration without
    sharing mutable state across test functions.
    """

    # ---------- Stage-1 — synthetic bearish news → one candidate_events row.
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
    assert isinstance(matched_keywords, str) and matched_keywords.strip(), (
        f"candidate_events.matched_keywords must be non-empty; got {matched_keywords!r}"
    )
    matched_list = [kw for kw in matched_keywords.split(",") if kw]
    assert "complete response letter" in matched_list, (
        f"matched_keywords must contain 'complete response letter'; "
        f"got {matched_list!r}"
    )

    expected_dedup = compute_dedup_key(_TICKER, news_event_id, matched_list)
    assert candidate["dedup_key"] == expected_dedup

    # Idempotency on dedup_key: re-running the cycle inserts zero rows.
    scanned_second, inserted_second = run_one_poll_cycle(
        str(db_path),
        polled_tickers=polled_tickers,
        after_id=0,
    )
    assert scanned_second == 1
    assert inserted_second == 0
    assert _count(db_path, "SELECT COUNT(*) FROM candidate_events") == 1

    # ---------- VAL-M5-009 — Stage-2 ensemble runs all 4 providers, bearish, p ≥ 0.75.
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
    assert ensemble.label == "material"
    assert ensemble.direction == "bearish"
    assert ensemble.mean_probability >= 0.75
    assert sorted(ensemble.successful_providers) == sorted(ALL_PROVIDERS)
    assert ensemble.failed_providers == []

    assert all(tracker.call_counts[p] == 1 for p in ALL_PROVIDERS), (
        tracker.call_counts
    )

    # Exactly four ensemble_scores_event rows persisted, all bearish.
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
        assert r["direction"] == "bearish"
        assert 0.0 <= float(r["probability"]) <= 1.0

    avg_p = sum(float(r["probability"]) for r in rows) / len(rows)
    assert avg_p >= 0.75

    # ---------- VAL-M5-010 — paper_orders + OTM put leg, no call leg.
    cassette = _load_cassette("order_put_roundtrip.json")

    # The put cassette only ships ``submit`` + ``poll_filled``; the
    # bull flow harness expects an additional ``poll_intermediate``
    # state so the ExecutionSubscriber can advance
    # ``submitted → accepted → filled`` in two polls. Synthesise an
    # ``accepted`` intermediate from the cassette's ``submit`` row
    # (status='accepted' already); we re-stamp timestamps below.
    intermediate_template = dict(cassette["submit"])

    # Re-stamp the cassette timestamps with strictly-increasing
    # wall-clock instants (see :mod:`tests.e2e.test_bull_flow`).
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

    poll_intermediate_payload = dict(intermediate_template)
    poll_intermediate_payload["created_at"] = _iso(0)
    poll_intermediate_payload["updated_at"] = _iso(60)
    poll_intermediate_payload["submitted_at"] = _iso(0)

    poll_filled_payload = dict(cassette["poll_filled"])
    poll_filled_payload["created_at"] = _iso(0)
    poll_filled_payload["updated_at"] = _iso(120)
    poll_filled_payload["filled_at"] = _iso(120)
    poll_filled_payload["submitted_at"] = _iso(0)

    fake_client = _FakeAlpacaClient(
        submit_order_result=submit_payload,
        get_order_results=[
            poll_intermediate_payload,
            poll_filled_payload,
        ],
    )

    executor = PaperExecutor(
        fake_client,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )

    # Snapshot for VAL-M5-013 increment assertion: write the
    # Reading-B summary BEFORE the bear flow submits its order so
    # we can later assert news_event_entries_submitted increments
    # by exactly +1 vs this pre-snapshot.
    pre_snapshot = write_reading_b_summary(audit_path, db_path=db_path)
    pre_news_event_entries = int(
        pre_snapshot["reading_b"]["news_event_entries_submitted"]
    )
    assert pre_news_event_entries == 0, (
        "pre-bear-flow snapshot must report zero submitted entries"
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
    # write-then-submit invariant honoured (row exists by the time
    # submit_news_event_entry returns).
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

    # OTM put leg: the broker submit request must carry an option
    # symbol whose option-type tag is 'P' and whose embedded strike
    # is strictly BELOW the underlying spot at submit time.
    assert len(fake_client.submit_calls) == 1
    submit_request = fake_client.submit_calls[0]
    submitted_symbol = getattr(submit_request, "symbol", None)
    assert submitted_symbol, "submitted order must carry an OCC symbol"
    # OCC option symbol form: ROOT + YYMMDD + (C|P) + strike8.
    assert submitted_symbol[-9] == "P", (
        f"bearish flow must produce a PUT leg; got {submitted_symbol!r}"
    )
    # Parse strike from the OCC symbol's last 8 chars (millis precision).
    strike_int_millis = int(submitted_symbol[-8:])
    strike_dollars = strike_int_millis / 1000.0
    assert strike_dollars < _STOCK_PRICE, (
        f"OTM put must have strike < spot; got strike={strike_dollars} "
        f"spot={_STOCK_PRICE}"
    )

    # VAL-M5-010 explicit: zero CALL legs for the bearish candidate.
    # Detect call legs via the OCC symbol form: chars [-9] == 'C'.
    conn = sqlite3.connect(str(db_path))
    try:
        all_symbols = [
            row[0]
            for row in conn.execute(
                "SELECT symbol FROM paper_orders "
                "WHERE event = 'news_event_entry'"
            ).fetchall()
        ]
    finally:
        conn.close()
    call_legs = [s for s in all_symbols if s and len(s) >= 9 and s[-9] == "C"]
    assert call_legs == [], (
        f"bearish flow must produce zero call legs; got {call_legs!r}"
    )
    put_legs = [s for s in all_symbols if s and len(s) >= 9 and s[-9] == "P"]
    assert len(put_legs) == 1, put_legs

    # ---------- VAL-M5-011 — execution_events + execution_fills written.
    from biotech_sniper.execution_subscriber import ExecutionSubscriber

    subscriber = ExecutionSubscriber(fake_client, db_path=db_path)

    n_first = subscriber.poll_once()
    assert n_first == 1
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
    assert event_types == ["submitted", "accepted", "filled"], event_types
    assert len(fills) == 1
    assert int(fills[0]["filled_qty"]) > 0
    assert float(fills[0]["filled_price"]) > 0.0
    assert (fills[0]["filled_at"] or "").endswith("Z")

    # ---------- VAL-M5-012 — bear flow inserts cooldown for SAVA.
    # The bull-flow contract pins the cooldown ticker to VKTX; the
    # bear-flow ticker SAVA is provably distinct. The bear-flow
    # test runs in its own tmp_path db, so the ``ticker_cooldown``
    # table contains exactly one row keyed by SAVA, and zero rows
    # keyed by VKTX (the bull-flow ticker), proving the ticker
    # space is non-overlapping.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cd_rows = conn.execute(
            "SELECT ticker, last_entry_at, cooldown_hours "
            "FROM ticker_cooldown ORDER BY ticker"
        ).fetchall()
    finally:
        conn.close()
    assert len(cd_rows) == 1, cd_rows
    cd = cd_rows[0]
    assert cd["ticker"] == _TICKER == "SAVA"
    assert cd["last_entry_at"]
    assert int(cd["cooldown_hours"]) == 24
    bull_ticker_cooldown = [r for r in cd_rows if r["ticker"] == "VKTX"]
    assert bull_ticker_cooldown == [], (
        "bear flow must NOT touch the bull-flow VKTX cooldown row"
    )

    # ---------- VAL-M5-013 — exit code 0 + audit_latest.json reading_b counts increment.
    payload = write_reading_b_summary(audit_path, db_path=db_path)
    assert audit_path.is_file()

    rb = payload.get("reading_b")
    assert isinstance(rb, dict), payload
    assert isinstance(rb["candidate_events_emitted"], int)
    assert rb["candidate_events_emitted"] >= 1
    assert rb["gate_pass_count"] == 1
    assert rb["news_event_entries_submitted"] == 1
    # +1 increment vs the pre-bear-run snapshot per VAL-M5-013.
    assert (
        rb["news_event_entries_submitted"] - pre_news_event_entries == 1
    ), (
        f"news_event_entries_submitted must increment by exactly 1 "
        f"(pre={pre_news_event_entries}, "
        f"post={rb['news_event_entries_submitted']})"
    )
    assert isinstance(rb["gate_reject_counts"], dict)

    loaded = json.loads(audit_path.read_text(encoding="utf-8"))
    assert loaded == payload
    assert loaded["reading_b"]["news_event_entries_submitted"] == 1


# ---------------------------------------------------------------------------
# Smoke import — keeps ``pytest --collect-only`` green even when the
# heavy fixtures are not exercised.
# ---------------------------------------------------------------------------


def test_module_imports_cleanly() -> None:
    """Defensive smoke test: every named import above resolves at module load."""
    assert "tests.e2e.test_bear_flow" in sys.modules or __name__.endswith(
        "test_bear_flow"
    )
