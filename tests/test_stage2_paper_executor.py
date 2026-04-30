"""Stage-2 paper-executor wiring tests.

Feature: ``f-m3-10-paper-executor-wiring``.

Tests the bridge module
:mod:`biotech_sniper.exec.stage2_paper_executor` which wires
:func:`biotech_sniper.exec.stage2_dispatcher.build_play_card` →
:meth:`biotech_sniper.paper_executor.PaperExecutor.execute` for
the ``news_event_entry`` event.

Validation contract assertions exercised:

* **VAL-M3-053** — ``RISK_PER_PLAY_USD=$250`` per-play sizing;
  ``qty = floor(cap / (mid * 100))``.
* **VAL-M3-054** — Same global concurrency=3 / deployed=$750 caps
  as daily-curated entries; the wired path inherits them via
  :class:`PaperExecutor`.
* **VAL-M3-055** — Caps count OPTIONS positions only;
  pre-existing equities / ETFs ignored.
* **VAL-M3-056** — Sell-to-close orders bypass the caps (regression
  pin).
* **VAL-M3-058** — ``news_event_entry`` is distinct from ``open``
  in the persisted ``paper_orders.event`` column.
* **VAL-M3-090** — Halted / delisted underlying short-circuits
  with :class:`UnderlyingUnavailable`; no broker call; no
  ``paper_orders`` row written; cooldown left for the caller to
  skip.
* **VAL-M3-091** — ``$250`` sizing → ``qty=0`` raises
  :class:`ContractTooExpensive` (typed).
* **VAL-M3-092** — News-event entry path is single-leg only.

Per the dual-path test convention (see ``library`` / ``AGENTS.md`` /
``skills/python-worker/SKILL.md``), the canonical bodies live ONCE
in this file. Thin re-export shims at
``tests/exec/test_stage2_sizing.py`` and
``tests/exec/test_event_tagging.py`` keep the contract-form node
IDs collectible.

Hermetic — no network. Fake :class:`AlpacaClient` doubles supply
``get_positions``, ``submit_order``, and ``get_latest_trade`` so
every guardrail exercises without touching the broker.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

import pytest

from biotech_sniper.alpaca_client import (
    AlpacaClientError,
    AlpacaTransportError,
    PAPER_BASE_URL,
)
from biotech_sniper.exec.stage2_dispatcher import (
    EVENT_NEWS_ENTRY,
    UnsupportedMultiStrikeForNewsEntry,
    assert_single_leg_for_news_entry,
    build_play_card,
)
from biotech_sniper.exec.stage2_paper_executor import (
    ConcurrencyCapExceeded,
    ContractTooExpensive,
    DeployedCapExceeded,
    OrderRejected,
    UnderlyingUnavailable,
    ensure_chain_quote_on_leg,
    submit_news_event_entry,
)
from biotech_sniper.llm.ensemble import (
    ALL_PROVIDERS,
    EnsembleEventResult,
    ProviderResult,
)
from biotech_sniper.paper_executor import PaperExecutor


# ---------------------------------------------------------------------------
# Fake client double
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Duck-typed substitute exposing the surface the Stage-2 wiring uses.

    Mirrors the helpers in :mod:`tests.test_paper_executor` /
    :mod:`tests.test_paper_executor_caps_filter` so behaviour stays
    consistent with the inherited :class:`PaperExecutor` test
    ergonomics.
    """

    def __init__(
        self,
        *,
        base_url: str = PAPER_BASE_URL,
        positions: Optional[list[dict[str, Any]]] = None,
        submit_order_results: Optional[list[Any]] = None,
        submit_order_error: Optional[Exception] = None,
        latest_trade_price: Optional[float] = 100.0,
        latest_trade_error: Optional[Exception] = None,
    ) -> None:
        self.base_url = base_url
        self._positions = list(positions or [])
        self._submit_results = list(submit_order_results or [])
        self._submit_error = submit_order_error
        self._latest_trade_price = latest_trade_price
        self._latest_trade_error = latest_trade_error
        self.submit_calls: list[Any] = []
        self.get_positions_calls: int = 0
        self.get_latest_trade_calls: list[str] = []

    def get_positions(self) -> list[dict[str, Any]]:
        self.get_positions_calls += 1
        return list(self._positions)

    def submit_order(self, order_request: Any) -> dict[str, Any]:
        self.submit_calls.append(order_request)
        if self._submit_error is not None:
            raise self._submit_error
        if not self._submit_results:
            # Default-success result with broker-provided id.
            return {
                "id": "fake-order-id",
                "symbol": getattr(order_request, "symbol", None),
                "side": "buy",
                "qty": getattr(order_request, "qty", 1),
                "status": "accepted",
            }
        return self._submit_results.pop(0)

    def get_order(self, order_id: str) -> dict[str, Any]:  # pragma: no cover
        return {"id": order_id, "status": "accepted"}

    def get_latest_trade(self, ticker: str) -> Optional[float]:
        self.get_latest_trade_calls.append(ticker)
        if self._latest_trade_error is not None:
            raise self._latest_trade_error
        return self._latest_trade_price


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "alpha_sniper.db"


def _make_ensemble_result(
    direction: Optional[str] = "bullish",
    *,
    label: str = "material",
    probability: float = 0.85,
    run_id: str = "run-stage2-paper-executor-test",
    candidate_event_id: Optional[int] = 42,
) -> EnsembleEventResult:
    """Synthesise a unanimous 4/4 EnsembleEventResult."""
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


def _candidate_event(
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
        "emitted_at": "2026-04-30T00:00:00.000000Z",
        "dedup_key": f"deadbeef{id_}",
    }


def _equity(symbol: str, qty: int = 100, avg: float = 50.0) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "qty": qty,
        "avg_entry_price": avg,
        "asset_class": "us_equity",
    }


def _option(
    symbol: str = "TESTBIO260717C00120000",
    qty: int = 1,
    avg: float = 1.20,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "qty": qty,
        "avg_entry_price": avg,
        "asset_class": "us_option",
    }


def _apply_v10_migration(db_path: Path) -> None:
    """Bring the executor-bootstrapped v9 db up to v10 (Reading-B foundations).

    The ``PaperExecutor`` constructor calls
    ``db_module.run_migrations(conn)`` which only walks up to
    :data:`db.CURRENT_VERSION` (currently 9). The ``news_event_entry``
    enum value lives in the v10 migration (010_reading_b_foundations.py),
    which is the precondition declared by the f-m1-07 feature.
    Tests that exercise the wired ``news_event_entry`` path must
    therefore apply v10 before any submit happens — mirrors the
    ``test_migration_010`` setup.
    """
    from biotech_sniper.migrations.runner import run as run_migrations_runner

    run_migrations_runner(db_path, 10, take_backup_first=False)


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
    # The constructor brought the db to v9; advance to v10 so the
    # ``paper_orders.event`` CHECK admits ``'news_event_entry'``
    # (the f-m1-07 precondition for this feature).
    _apply_v10_migration(db_path)
    return executor, fake


# ---------------------------------------------------------------------------
# VAL-M3-053 — $250 per-play sizing on news_event_entry
# ---------------------------------------------------------------------------


def test_news_event_entry_sized_at_250(db_path: Path):
    """``RISK_PER_PLAY_USD=$250`` sizing applied to news_event_entry.

    Verifies the formula ``qty = floor(cap / (mid * 100))`` for the
    matrix from VAL-M3-053:

    * mid=$1.00 → qty=2  (2 × 1.00 × 100 = $200 ≤ $250)
    * mid=$1.20 → qty=2  ($240 ≤ $250)
    * mid=$1.30 → qty=1  ($130 ≤ $250 because 2 × $260 > $250)
    """
    cases = [
        # (bid, ask) → expected qty given a $250 cap.
        ((1.00, 1.00), 2),
        ((1.10, 1.30), 2),  # mid = $1.20 → 2
        ((1.20, 1.40), 1),  # mid = $1.30 → 1
    ]
    for (bid, ask), expected_qty in cases:
        fake = _FakeAlpacaClient()
        executor, client = _make_executor(db_path, client=fake)

        order_id = submit_news_event_entry(
            executor,
            candidate_event=_candidate_event(),
            ensemble_result=_make_ensemble_result(direction="bullish"),
            stock_price=100.0,
            bid=bid,
            ask=ask,
            expiry="2026-07-17",
        )

        assert isinstance(order_id, str) and order_id
        # The broker received a single buy order with the expected qty.
        assert len(client.submit_calls) == 1
        request = client.submit_calls[0]
        assert getattr(request, "qty") == expected_qty


def test_news_event_entry_persists_event_tag(db_path: Path):
    """Persisted ``paper_orders.event`` is ``'news_event_entry'`` (not 'open')."""
    fake = _FakeAlpacaClient()
    executor, _ = _make_executor(db_path, client=fake)

    order_id = submit_news_event_entry(
        executor,
        candidate_event=_candidate_event(),
        ensemble_result=_make_ensemble_result(direction="bullish"),
        stock_price=100.0,
        bid=1.00,
        ask=1.00,
        expiry="2026-07-17",
    )
    assert order_id

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT event, side, status FROM paper_orders "
            "WHERE alpaca_order_id = ?",
            (order_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["event"] == EVENT_NEWS_ENTRY == "news_event_entry"
    assert row["side"] == "buy"


# ---------------------------------------------------------------------------
# VAL-M3-054 — Concurrency cap inherited (3 active option positions blocks)
# ---------------------------------------------------------------------------


def test_news_event_entry_inherits_concurrency_cap(db_path: Path):
    """3 active OPTIONS positions → ConcurrencyCapExceeded on news_event_entry."""
    fake = _FakeAlpacaClient(
        positions=[
            _option("OPT_A"),
            _option("OPT_B"),
            _option("OPT_C"),
        ]
    )
    executor, client = _make_executor(db_path, client=fake)

    with pytest.raises(ConcurrencyCapExceeded):
        submit_news_event_entry(
            executor,
            candidate_event=_candidate_event(),
            ensemble_result=_make_ensemble_result(direction="bullish"),
            stock_price=100.0,
            bid=1.00,
            ask=1.00,
            expiry="2026-07-17",
        )

    # No submission to the broker.
    assert client.submit_calls == []


def test_news_event_entry_inherits_deployed_cap(db_path: Path):
    """Sum of deployed + planned > $750 → DeployedCapExceeded."""
    fake = _FakeAlpacaClient(
        positions=[
            _option("OPT_A", qty=2, avg=3.00),  # 2 × 300 = $600
            _option("OPT_B", qty=1, avg=1.50),  # 1 × 150 = $150
        ]
    )
    executor, client = _make_executor(db_path, client=fake)

    # Deployed = $750. Planned for mid=$1.00 qty=2 = $200 → $950 > $750.
    with pytest.raises(DeployedCapExceeded):
        submit_news_event_entry(
            executor,
            candidate_event=_candidate_event(),
            ensemble_result=_make_ensemble_result(direction="bullish"),
            stock_price=100.0,
            bid=0.80,
            ask=1.20,
            expiry="2026-07-17",
        )
    assert client.submit_calls == []


# ---------------------------------------------------------------------------
# VAL-M3-055 — Caps count OPTIONS positions only
# ---------------------------------------------------------------------------


def test_caps_filter_options_only(db_path: Path):
    """5 equity holdings + 2 option positions → 3rd news_event_entry succeeds.

    The 5 equity positions inflate raw position count to 7 (would
    trip 3-cap if unfiltered) and would inflate the deployed sum
    into the millions if the equity ``qty * avg * 100`` math ran.
    With the f-m3-07b filter applied, only the 2 option positions
    count → entry proceeds (option count 2 → 3, within cap;
    deployed-options-only sum stays well under $750).
    """
    fake = _FakeAlpacaClient(
        positions=[
            _option("OPT_A", qty=1, avg=1.20),
            _option("OPT_B", qty=1, avg=1.20),
            _equity("AMDL", qty=10, avg=12.0),
            _equity("AMZZ", qty=10, avg=15.0),
            _equity("CONL", qty=5, avg=8.0),
            _equity("LMT", qty=2, avg=460.0),
            _equity("NVDL", qty=20, avg=85.0),
        ]
    )
    executor, client = _make_executor(db_path, client=fake)

    order_id = submit_news_event_entry(
        executor,
        candidate_event=_candidate_event(),
        ensemble_result=_make_ensemble_result(direction="bullish"),
        stock_price=100.0,
        bid=1.00,
        ask=1.00,
        expiry="2026-07-17",
    )

    assert isinstance(order_id, str) and order_id
    assert len(client.submit_calls) == 1


# ---------------------------------------------------------------------------
# VAL-M3-056 — Sell-to-close bypasses caps (regression pin)
# ---------------------------------------------------------------------------


def test_exit_orders_bypass_caps(db_path: Path):
    """Sell-to-close orders bypass concurrency / deployed caps.

    The Stage-2 wiring itself only ever produces BUY entries — but
    the downstream :class:`PaperExecutor` is the SAME entry point
    that handles sell-to-close exits triggered by IV-crush /
    stop-loss / adverse-news / rotation. We pin the inherited
    bypass behaviour by submitting an exit play_card directly
    through ``PaperExecutor.execute`` while caps are deliberately
    saturated; the exit MUST still go through.
    """
    fake = _FakeAlpacaClient(
        # 5 active option positions — caps would block any BUY.
        positions=[
            _option(f"OPT_{i}", qty=1, avg=2.40) for i in range(5)
        ]
    )
    executor, client = _make_executor(db_path, client=fake)

    exit_card = {
        "play_card_id": "exit-news-1",
        "ticker": "TESTBIO",
        "event": "iv_crush_exit",
        "purpose": "exit",
        "parent_play_card_id": "news-1-run",
        "option_legs": [
            {
                "symbol": "TESTBIO260717C00120000",
                "side": "sell",
                "qty": 1,
                "limit_price": 1.50,
                "option_type": "call",
                "client_order_id": "test-exit-stage2-bypass",
            }
        ],
    }

    order_id = executor.execute(exit_card)
    assert isinstance(order_id, str) and order_id
    assert len(client.submit_calls) == 1
    # get_positions is NEVER consulted for sells — caps probe skipped.
    assert client.get_positions_calls == 0


# ---------------------------------------------------------------------------
# VAL-M3-058 — news_event_entry distinct from open in paper_orders
# ---------------------------------------------------------------------------


def test_news_event_vs_open_distinct(db_path: Path):
    """Running both paths in the same fixture day yields distinct event tags.

    Asserts ``SELECT event, COUNT(*) FROM paper_orders ... GROUP BY
    event`` returns rows for BOTH ``'open'`` and
    ``'news_event_entry'`` with no overlap (per VAL-M3-058 evidence).
    """
    fake = _FakeAlpacaClient()
    executor, client = _make_executor(db_path, client=fake)

    # 1) Daily-curated 'open' entry — built directly (no Stage-2 dispatcher).
    open_card = {
        "play_card_id": "open-1",
        "ticker": "DAILYBIO",
        "event": "open",
        "purpose": "entry",
        "option_legs": [
            {
                "symbol": "DAILYBIO260717C00100000",
                "side": "buy",
                "bid": 1.00,
                "ask": 1.00,
                "limit_price": 1.00,
                "option_type": "call",
                "client_order_id": "daily-curated-1",
            }
        ],
    }
    open_id = executor.execute(open_card)
    assert open_id

    # 2) Stage-2 news_event_entry — through the wiring.
    news_id = submit_news_event_entry(
        executor,
        candidate_event=_candidate_event(),
        ensemble_result=_make_ensemble_result(direction="bullish"),
        stock_price=100.0,
        bid=1.00,
        ask=1.00,
        expiry="2026-07-17",
    )
    assert news_id

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT event, COUNT(*) FROM paper_orders "
            "WHERE status NOT IN ('rejected') "
            "GROUP BY event ORDER BY event"
        ).fetchall()
    finally:
        conn.close()
    by_event = dict(rows)
    assert "open" in by_event, by_event
    assert "news_event_entry" in by_event, by_event
    assert by_event["open"] == 1
    assert by_event["news_event_entry"] == 1
    # Guarantee no overlap — the two tags are distinct rows.
    assert "open" != "news_event_entry"


def test_news_event_entry_is_not_open_in_persistence(db_path: Path):
    """Persisted ``paper_orders.event`` for the wired path is NEVER 'open'."""
    fake = _FakeAlpacaClient()
    executor, _ = _make_executor(db_path, client=fake)

    submit_news_event_entry(
        executor,
        candidate_event=_candidate_event(),
        ensemble_result=_make_ensemble_result(direction="bullish"),
        stock_price=100.0,
        bid=1.00,
        ask=1.00,
        expiry="2026-07-17",
    )

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT event FROM paper_orders"
        ).fetchall()
    finally:
        conn.close()
    distinct_events = {r[0] for r in rows}
    assert "news_event_entry" in distinct_events
    assert "open" not in distinct_events


# ---------------------------------------------------------------------------
# VAL-M3-090 — Halted / delisted underlying → typed UnderlyingUnavailable
# ---------------------------------------------------------------------------


def test_halted_underlying_rejects_cleanly(tmp_path: Path):
    """Halted underlying short-circuits with typed exception, no broker call.

    Uses ``tmp_path`` directly (not the local ``db_path`` fixture) so
    the dual-path shim at
    ``tests/exec/test_stage2_direction_routing.py`` can re-export the
    test by name without dragging in module-local fixtures.
    """
    db_path = tmp_path / "alpha_sniper.db"
    fake = _FakeAlpacaClient(
        latest_trade_error=AlpacaTransportError(
            "Alpaca server error (HTTP 503): asset is not active for trading"
        ),
    )
    executor, client = _make_executor(db_path, client=fake)

    with pytest.raises(UnderlyingUnavailable) as excinfo:
        submit_news_event_entry(
            executor,
            candidate_event=_candidate_event(ticker="HALTBIO"),
            ensemble_result=_make_ensemble_result(direction="bullish"),
            stock_price=100.0,
            bid=1.00,
            ask=1.00,
            expiry="2026-07-17",
        )
    # Reason mentions the ticker for audit.
    assert "HALTBIO" in str(excinfo.value)

    # No broker submission AT ALL.
    assert client.submit_calls == []
    assert client.get_positions_calls == 0
    # And no paper_orders row was ever written.
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM paper_orders"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0, "paper_orders count must be 0 on halted underlying"


def test_delisted_underlying_no_recent_trade_rejects(db_path: Path):
    """``get_latest_trade`` returning ``None`` (no recent trade) trips the gate."""
    fake = _FakeAlpacaClient(latest_trade_price=None)
    executor, client = _make_executor(db_path, client=fake)

    with pytest.raises(UnderlyingUnavailable, match="DELISTED"):
        submit_news_event_entry(
            executor,
            candidate_event=_candidate_event(ticker="DELISTED"),
            ensemble_result=_make_ensemble_result(direction="bullish"),
            stock_price=100.0,
            bid=1.00,
            ask=1.00,
            expiry="2026-07-17",
        )
    assert client.submit_calls == []
    assert client.get_latest_trade_calls == ["DELISTED"]


def test_underlying_check_can_be_disabled(db_path: Path):
    """``check_underlying_tradable=False`` skips the pre-check entirely.

    Lets tests with a chain quote already known to be valid bypass
    the live-trade fetch — the pre-check is best-effort and must
    not be mandatory for unit-level coverage.
    """
    fake = _FakeAlpacaClient(latest_trade_price=None)
    executor, client = _make_executor(db_path, client=fake)

    order_id = submit_news_event_entry(
        executor,
        candidate_event=_candidate_event(),
        ensemble_result=_make_ensemble_result(direction="bullish"),
        stock_price=100.0,
        bid=1.00,
        ask=1.00,
        expiry="2026-07-17",
        check_underlying_tradable=False,
    )
    assert isinstance(order_id, str) and order_id
    # Pre-check skipped — get_latest_trade was never called.
    assert client.get_latest_trade_calls == []


# ---------------------------------------------------------------------------
# VAL-M3-091 — qty=0 path raises ContractTooExpensive
# ---------------------------------------------------------------------------


def test_qty_zero_rejects_contract_too_expensive(db_path: Path):
    """``mid * 100 > $250`` → :class:`ContractTooExpensive` raised.

    With mid=$5.00 (bid+ask both $5.00), per-contract cost = $500 >
    $250 cap → :func:`size_position` returns 0; the executor
    persists a ``status='rejected'`` row and raises
    :class:`ContractTooExpensive`. The wiring propagates the typed
    exception so Stage-2 can soft-skip without advancing
    cooldown.
    """
    fake = _FakeAlpacaClient()
    executor, client = _make_executor(db_path, client=fake)

    with pytest.raises(ContractTooExpensive):
        submit_news_event_entry(
            executor,
            candidate_event=_candidate_event(),
            ensemble_result=_make_ensemble_result(direction="bullish"),
            stock_price=100.0,
            bid=5.00,
            ask=5.00,
            expiry="2026-07-17",
        )
    # The broker was NEVER contacted — sizing rejection short-circuits
    # before submission.
    assert client.submit_calls == []
    # A rejection row WAS persisted (per existing PaperExecutor f-m3-04
    # contract — the wiring does not suppress that).
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT status, qty, event FROM paper_orders"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["status"] == "rejected"
    assert rows[0]["qty"] == 0
    assert rows[0]["event"] == "news_event_entry"


# ---------------------------------------------------------------------------
# VAL-M3-092 — Single-leg only (no multi-strike, no spreads)
# ---------------------------------------------------------------------------


def test_news_event_entry_is_single_leg_only(db_path: Path):
    """Positive control: a happy-path bullish entry produces a 1-leg order.

    Companion to the multi-strike defensive validator coverage in
    :mod:`tests.test_stage2_dispatcher`. Verifies that the wiring
    layer ALSO produces a single-leg play_card on the happy path —
    the broker is never asked to handle a 2-leg shape under
    ``event='news_event_entry'``.
    """
    fake = _FakeAlpacaClient()
    executor, client = _make_executor(db_path, client=fake)

    order_id = submit_news_event_entry(
        executor,
        candidate_event=_candidate_event(),
        ensemble_result=_make_ensemble_result(direction="bullish"),
        stock_price=100.0,
        bid=1.00,
        ask=1.00,
        expiry="2026-07-17",
    )
    assert order_id

    # Exactly one alpaca-py request was constructed.
    assert len(client.submit_calls) == 1


def test_ensure_chain_quote_on_leg_rejects_multi_strike():
    """Defensive helper: a multi-strike-shaped play_card raises."""
    multi = {
        "play_card_id": "pc-multi",
        "ticker": "TESTBIO",
        "event": "news_event_entry",
        "order_class": "simple",
        "side": "buy",
        "purpose": "entry",
        "option_legs": [
            {"symbol": "TESTBIO260717C00120000", "side": "buy"},
            {"symbol": "TESTBIO260717C00150000", "side": "buy"},
        ],
    }
    with pytest.raises(UnsupportedMultiStrikeForNewsEntry):
        ensure_chain_quote_on_leg(multi, bid=1.0, ask=1.0)


def test_dispatcher_output_is_single_leg_through_wiring():
    """End-to-end: dispatcher output flows through wiring with 1 leg."""
    er = _make_ensemble_result(direction="bullish")
    cand = _candidate_event(matched_keywords="pdufa")
    res = build_play_card(
        candidate_event=cand,
        ensemble_result=er,
        stock_price=100.0,
        expiry="2026-07-17",
    )
    pc = ensure_chain_quote_on_leg(res.play_card, bid=1.0, ask=1.0)
    assert isinstance(pc["option_legs"], list)
    assert len(pc["option_legs"]) == 1
    assert pc["event"] == EVENT_NEWS_ENTRY
    # Defensive validator passes.
    leg = assert_single_leg_for_news_entry(pc)
    assert leg is pc["option_legs"][0]
    assert leg["bid"] == 1.0
    assert leg["ask"] == 1.0


# ---------------------------------------------------------------------------
# Wiring guardrails (paper-only check inherited)
# ---------------------------------------------------------------------------


def test_wiring_inherits_paper_only_guardrail(db_path: Path):
    """A non-paper client at the executor level still trips PaperOnlyViolation.

    The wiring does not bypass the underlying executor's paper-only
    invariant — confirmed by drifting the fake client's ``base_url``
    after construction.
    """
    from biotech_sniper.alpaca_client import LIVE_BASE_URL
    from biotech_sniper.paper_executor import PaperOnlyViolation

    fake = _FakeAlpacaClient()
    executor, client = _make_executor(db_path, client=fake)
    client.base_url = LIVE_BASE_URL

    with pytest.raises(PaperOnlyViolation):
        submit_news_event_entry(
            executor,
            candidate_event=_candidate_event(),
            ensemble_result=_make_ensemble_result(direction="bullish"),
            stock_price=100.0,
            bid=1.00,
            ask=1.00,
            expiry="2026-07-17",
            check_underlying_tradable=False,
        )


def test_wiring_propagates_broker_rejection(db_path: Path):
    """A broker 4xx from a NON-halt cause surfaces as :class:`OrderRejected`.

    The halt/delist check is a separate pre-check; broker
    rejections that don't match the halted-asset signal flow
    through the executor's normal ``OrderRejected`` path so
    Stage-2 can distinguish "underlying unavailable" from
    "broker-side rejection".
    """
    fake = _FakeAlpacaClient(
        submit_order_error=AlpacaClientError(
            "Alpaca API error: invalid OCC symbol TESTBIO260717C12345678"
        )
    )
    executor, _ = _make_executor(db_path, client=fake)

    with pytest.raises(OrderRejected, match="invalid OCC symbol"):
        submit_news_event_entry(
            executor,
            candidate_event=_candidate_event(),
            ensemble_result=_make_ensemble_result(direction="bullish"),
            stock_price=100.0,
            bid=1.00,
            ask=1.00,
            expiry="2026-07-17",
        )
