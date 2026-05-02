"""Integration tests for f-fix-live-05: real chain_quote_fn in _build_submit_collaborators.

Pins the contract that ``_build_submit_collaborators`` returns a real
production-grade ``chain_quote_fn(candidate_row)`` (not ``None``)
that:

* reads the ticker off the candidate row,
* calls :meth:`AlpacaClient.get_latest_trade` for the underlying
  ``stock_price``,
* derives the ``catalyst_type`` from ``matched_keywords`` and the
  catalyst-specific OTM offset (re-using the canonical
  :data:`biotech_sniper.sectors.unified_scorer.DEFAULT_OTM_BY_CATALYST`
  table — never a parallel literal in news_daemon source),
* resolves the OTM call strike via
  :func:`biotech_sniper.sectors.unified_scorer.calculate_otm_strike`
  and the nearest standard-monthly expiry (3rd Friday) at least
  three weeks out,
* fetches :meth:`AlpacaClient.get_options_chain` for the resolved
  ``(ticker, expiry)`` and returns the bid/ask of the call leg
  closest to the resolved strike,
* returns the tuple shape the ``stage2_news_dispatch`` consumer
  expects: ``(bid, ask, expiry, stock_price)``,
* surfaces every Alpaca failure as a structured WARNING
  ``stage2_chain_quote_failed`` log line and returns ``None`` so
  the dispatcher's ``if not quote: continue`` guard skips the
  candidate without halting the daemon.

Test 3 drives the FULL ``run_main_loop`` entry-point (with the real
chain_quote_fn closure wired) so a connector-level bug between
``_build_submit_collaborators`` and the dispatcher could not slip
through hermetic-only coverage (per AGENTS.md § "Integration tests
for connector code").
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

from biotech_sniper import db as project_db
from biotech_sniper.alpaca_client import (
    AlpacaTransportError,
    PAPER_BASE_URL,
)
from biotech_sniper.exec.stage2_paper_executor import (
    submit_news_event_entry,
)
from biotech_sniper.migrations.runner import run as run_migrations_runner
from biotech_sniper.news_daemon.resilience import (
    ShutdownState,
    _make_chain_quote_fn,
    run_main_loop,
)
from biotech_sniper.paper_executor import PaperExecutor


# ---------------------------------------------------------------------------
# Fake Alpaca client (mirrors tests/test_stage2_paper_executor.py double).
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Duck-typed substitute exposing the surface the wiring uses."""

    def __init__(
        self,
        *,
        base_url: str = PAPER_BASE_URL,
        latest_trade_price: Optional[float] = 100.0,
        chain_rows: Optional[list[dict[str, Any]]] = None,
        latest_trade_raises: Optional[Exception] = None,
        chain_raises: Optional[Exception] = None,
    ) -> None:
        self.base_url = base_url
        self._latest_trade_price = latest_trade_price
        self._chain_rows = chain_rows or []
        self._latest_trade_raises = latest_trade_raises
        self._chain_raises = chain_raises
        self.submit_calls: list[Any] = []
        self.get_positions_calls: int = 0
        self.get_latest_trade_calls: list[str] = []
        self.get_options_chain_calls: list[tuple[str, Optional[str]]] = []

    # ------------------------------------------------------------
    # PaperExecutor surface
    # ------------------------------------------------------------

    def get_positions(self) -> list[dict[str, Any]]:
        self.get_positions_calls += 1
        return []

    def submit_order(self, order_request: Any) -> dict[str, Any]:
        self.submit_calls.append(order_request)
        return {
            "id": "fake-order-id-CHAINQUOTE",
            "symbol": getattr(order_request, "symbol", None),
            "side": "buy",
            "qty": getattr(order_request, "qty", 1),
            "status": "accepted",
        }

    def get_order(self, order_id: str) -> dict[str, Any]:
        return {"id": order_id, "status": "accepted"}

    # ------------------------------------------------------------
    # Chain-quote surface
    # ------------------------------------------------------------

    def get_latest_trade(self, ticker: str) -> Optional[float]:
        self.get_latest_trade_calls.append(ticker)
        if self._latest_trade_raises is not None:
            raise self._latest_trade_raises
        return self._latest_trade_price

    def get_options_chain(
        self,
        ticker: str,
        expiry: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        self.get_options_chain_calls.append((ticker, expiry))
        if self._chain_raises is not None:
            raise self._chain_raises
        return list(self._chain_rows)


# ---------------------------------------------------------------------------
# DB helpers (mirror tests/integration/test_news_daemon_stage2_dispatch.py).
# ---------------------------------------------------------------------------


def _build_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "alpha.db"
    conn = project_db.connect(db_path)
    try:
        project_db.run_migrations(conn)
    finally:
        conn.close()
    run_migrations_runner(
        db_path,
        target_version=project_db.CURRENT_VERSION,
        take_backup_first=False,
    )
    return db_path


def _seed_news_event(
    db_path: Path,
    *,
    ticker: str,
    title: str,
    url: str,
) -> int:
    conn = sqlite3.connect(str(db_path))
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


def _seed_candidate(
    db_path: Path,
    *,
    ticker: str,
    matched_keywords: str = "pdufa,approval",
    dedup_seed: str = "chain-quote-001",
) -> int:
    nid = _seed_news_event(
        db_path,
        ticker=ticker,
        title=f"{ticker} pdufa approval imminent",
        url=f"https://example.com/{ticker.lower()}-{dedup_seed}",
    )
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO candidate_events ("
            "ticker, source_news_event_id, matched_keywords,"
            " emitted_at, dedup_key) VALUES (?, ?, ?, "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?)",
            (
                ticker,
                nid,
                matched_keywords,
                f"dedup-{ticker.lower()}-{dedup_seed}",
            ),
        )
        cid = int(
            conn.execute(
                "SELECT MAX(id) FROM candidate_events"
            ).fetchone()[0]
        )
        conn.commit()
    finally:
        conn.close()
    return cid


def _seed_pdufa(
    db_path: Path,
    *,
    ticker: str,
    drug: str = "drugX",
    days_offset: int = 3,
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT OR IGNORE INTO pdufa_calendar ("
            "ticker, drug, action_date, sponsor, source_url, fetched_at"
            ") VALUES (?, ?, DATE('now','+' || ? || ' days'), ?, ?, ?)",
            (
                ticker,
                drug,
                int(days_offset),
                "TestSponsor",
                "https://www.fda.gov/test",
                "2026-04-30T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _make_armed_file(tmp_path: Path) -> Path:
    armed = tmp_path / ".armed"
    armed.write_text("ok")
    return armed


def _provider_callable(
    label: str = "material",
    direction: str = "bullish",
    probability: float = 0.92,
):
    def _call(_candidate, *, name: str = "stub") -> dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    yield _build_db(tmp_path)


@pytest.fixture
def armed_path(tmp_path: Path) -> Path:
    return _make_armed_file(tmp_path)


@pytest.fixture
def all_providers() -> dict[str, Any]:
    from biotech_sniper.llm.ensemble import ALL_PROVIDERS

    return {name: _provider_callable() for name in ALL_PROVIDERS}


# ---------------------------------------------------------------------------
# Test 1 — chain_quote_fn returns the expected tuple shape
# ---------------------------------------------------------------------------


def test_chain_quote_fn_resolves_real_quote(caplog) -> None:
    """A wired chain_quote_fn returns ``(bid, ask, expiry, stock_price)``.

    Stock price = 100, catalyst_type='PDUFA' (midpoint OTM = 17.5%) →
    raw_strike = 117.5 → rounded to 120 (since 100 ≤ s < 200 round to
    nearest $5). Chain advertises strikes 115/120/125 calls; the
    closest match is 120 → returns its bid/ask.
    """
    chain_rows = [
        {"strike": 115.0, "type": "call", "bid": 1.40, "ask": 1.60,
         "expiry": "2026-07-17"},
        {"strike": 120.0, "type": "call", "bid": 1.10, "ask": 1.30,
         "expiry": "2026-07-17"},
        {"strike": 125.0, "type": "call", "bid": 0.80, "ask": 1.00,
         "expiry": "2026-07-17"},
        # Put rows of the same strikes — must be ignored on the call
        # branch.
        {"strike": 120.0, "type": "put", "bid": 5.00, "ask": 5.40,
         "expiry": "2026-07-17"},
    ]
    fake_client = _FakeAlpacaClient(
        latest_trade_price=100.0,
        chain_rows=chain_rows,
    )

    log = logging.getLogger("test.chain_quote.resolves")
    chain_quote_fn = _make_chain_quote_fn(fake_client, log)

    candidate_row = {
        "id": 42,
        "ticker": "WIRE",
        "matched_keywords": "pdufa,approval",
    }

    caplog.set_level("WARNING")
    quote = chain_quote_fn(candidate_row)

    assert quote is not None, "expected a non-None quote tuple"
    assert isinstance(quote, tuple) and len(quote) == 4, (
        f"expected a 4-tuple, got {quote!r}"
    )
    bid, ask, expiry, stock_price = quote
    assert isinstance(bid, float) and bid > 0
    assert isinstance(ask, float) and ask > 0
    assert ask >= bid
    assert isinstance(expiry, str) and len(expiry) == 10
    assert isinstance(stock_price, float) and stock_price == 100.0

    # Assert the call leg at strike 120 (the closest OTM match) was
    # selected — bid=1.10, ask=1.30.
    assert bid == pytest.approx(1.10)
    assert ask == pytest.approx(1.30)

    # Per VAL spec: ticker → get_latest_trade was called once and
    # get_options_chain was called once.
    assert fake_client.get_latest_trade_calls == ["WIRE"]
    assert len(fake_client.get_options_chain_calls) == 1
    chain_ticker, chain_expiry = fake_client.get_options_chain_calls[0]
    assert chain_ticker == "WIRE"
    assert chain_expiry == expiry

    # No WARNING was emitted on the success path.
    failure_logs = [
        r for r in caplog.records
        if getattr(r, "event", None) == "stage2_chain_quote_failed"
    ]
    assert failure_logs == []


# ---------------------------------------------------------------------------
# Test 2 — Alpaca failure is swallowed: returns None + WARNING logged
# ---------------------------------------------------------------------------


def test_chain_quote_fn_handles_alpaca_failure(caplog) -> None:
    """get_latest_trade raises → chain_quote_fn returns None + WARNING."""
    fake_client = _FakeAlpacaClient(
        latest_trade_raises=AlpacaTransportError("simulated 503"),
    )

    log = logging.getLogger("test.chain_quote.failure")
    chain_quote_fn = _make_chain_quote_fn(fake_client, log)

    candidate_row = {
        "id": 11,
        "ticker": "BOOM",
        "matched_keywords": "pdufa,approval",
    }

    caplog.set_level("WARNING")
    quote = chain_quote_fn(candidate_row)

    assert quote is None, f"expected None on Alpaca failure, got {quote!r}"

    failure_logs = [
        r for r in caplog.records
        if getattr(r, "event", None) == "stage2_chain_quote_failed"
    ]
    assert len(failure_logs) >= 1, (
        f"expected >=1 stage2_chain_quote_failed WARNING, got "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    # Reason must surface the typed exception class name (no secret /
    # PII leakage of the exception args).
    rec = failure_logs[0]
    assert rec.levelname == "WARNING"
    reason = getattr(rec, "reason", None)
    assert reason == "AlpacaTransportError", (
        f"expected reason='AlpacaTransportError', got {reason!r}"
    )

    # Even on failure the wiring must not have invoked
    # get_options_chain — the chain quote MUST short-circuit on the
    # latest_trade failure (no wasted chain pull).
    assert fake_client.get_options_chain_calls == []


# ---------------------------------------------------------------------------
# Test 3 — full run_main_loop with a real chain_quote_fn closure wired
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        self.t += 0.001
        return self.t

    def sleep(self, _s: float) -> None:
        return


def _count(
    db_path: Path,
    table: str,
    where: str = "1=1",
    params: tuple = (),
) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        try:
            return int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {where}", params
                ).fetchone()[0]
            )
        except sqlite3.OperationalError:
            return 0
    finally:
        conn.close()


def test_full_dispatch_with_real_chain_quote(
    db_path: Path,
    armed_path: Path,
    all_providers: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog,
) -> None:
    """End-to-end: gates open + real chain_quote_fn → 1 paper_orders row.

    Verifies that the chain_quote_fn produced by
    :func:`_make_chain_quote_fn` plugs into the dispatcher transparently
    AND the resulting ``paper_orders`` row carries a non-null
    ``requested_mid_at_submit`` matching the fake-chain quote mid.
    """
    _seed_pdufa(db_path, ticker="REAL", days_offset=2)
    _seed_candidate(db_path, ticker="REAL", dedup_seed="real-chain-1")

    chain_rows = [
        {"strike": 120.0, "type": "call", "bid": 1.10, "ask": 1.30,
         "expiry": "2026-07-17"},
        {"strike": 125.0, "type": "call", "bid": 0.80, "ask": 1.00,
         "expiry": "2026-07-17"},
    ]
    fake_client = _FakeAlpacaClient(
        latest_trade_price=100.0,
        chain_rows=chain_rows,
    )
    executor = PaperExecutor(
        fake_client,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )

    log = logging.getLogger("test.chain_quote.full")
    real_chain_quote_fn = _make_chain_quote_fn(fake_client, log)

    def _market_open() -> bool:
        return True

    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "1")
    monkeypatch.setenv("STAGE2_AUTO_DISPATCH", "1")
    monkeypatch.setenv("STAGE2_DISPATCH_SCOPE", "pdufa-soon")

    import biotech_sniper.news_daemon.resilience as resilience_module

    monkeypatch.setattr(
        resilience_module,
        "_STAGE2_DISPATCH_OVERRIDES_FOR_TESTS",
        {
            "armed_path": armed_path,
            "providers": all_providers,
            "submit_fn": submit_news_event_entry,
            "market_open_check": _market_open,
            "chain_quote_fn": real_chain_quote_fn,
            "paper_executor": executor,
        },
        raising=False,
    )

    state = ShutdownState()
    fc = _FakeClock()
    caplog.set_level("INFO")
    rc = run_main_loop(
        str(db_path),
        poll_seconds=1,
        max_cycles=1,
        rss_fetchers=(),
        state=state,
        install_handlers=False,
        sleep_func=fc.sleep,
        monotonic=fc.monotonic,
        heartbeat_path=Path(db_path).parent / "hb.json",
        version_sha="e" * 40,
    )
    assert rc == 0
    assert state.cycles_completed == 1

    rows = sqlite3.connect(str(db_path)).execute(
        "SELECT event, alpaca_order_id, requested_mid_at_submit "
        "FROM paper_orders"
    ).fetchall()
    assert len(rows) == 1, (
        f"expected exactly 1 paper_orders row, got {rows!r}"
    )
    event, alpaca_order_id, requested_mid_at_submit = rows[0]
    assert event == "news_event_entry"
    assert alpaca_order_id, (
        f"alpaca_order_id must be non-null, got {alpaca_order_id!r}"
    )
    # mid = (1.10 + 1.30) / 2 = 1.20
    assert requested_mid_at_submit == pytest.approx(1.20), (
        f"expected requested_mid_at_submit ~ 1.20 from chain quote, "
        f"got {requested_mid_at_submit!r}"
    )

    # The chain_quote_fn must have been invoked and the underlying
    # latest_trade probe AND chain pull happened on the wired path.
    assert "REAL" in fake_client.get_latest_trade_calls
    assert any(
        ticker == "REAL" for ticker, _expiry in fake_client.get_options_chain_calls
    )

    submitted = [
        r for r in caplog.records
        if getattr(r, "event", None) == "stage2_order_submitted"
    ]
    assert len(submitted) >= 1, (
        f"expected >=1 stage2_order_submitted log entry, got "
        f"{[r.getMessage() for r in submitted]}"
    )
