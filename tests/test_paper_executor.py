"""Tests for :mod:`biotech_sniper.paper_executor`.

Hermetic — replays cassettes from ``tests/fixtures/cassettes/alpaca/``
through fake :class:`AlpacaClient` doubles. Production network calls
never fire. Mirrors the cassette pattern established in
:mod:`tests.test_alpaca_client`.

Validation contract assertions exercised:

* VAL-M3-014 — single-leg long call submission roundtrip.
* VAL-M3-015 — single-leg long put submission roundtrip
  (``side='buy'``, ``qty>=1``, ``order_class='simple'``).
* VAL-M3-016 — sandbox poll loop walks ``accepted → filled`` within
  the bounded 30 s timeout.
* VAL-M3-017 — multi-leg play cards raise
  :class:`UnsupportedOrderShape` and submit no order.
* VAL-M3-018 — failed submission raises :class:`OrderRejected`,
  persists ``status='rejected'`` + ``reason`` row, never persists a
  ``status='submitted'`` row.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper import db as db_module
from biotech_sniper.alpaca_client import (
    AlpacaAuthError,
    AlpacaClient,
    AlpacaClientError,
    AlpacaTransportError,
    LIVE_BASE_URL,
    PAPER_BASE_URL,
)
from biotech_sniper.paper_executor import (
    OrderRejected,
    PaperExecutor,
    PaperExecutorError,
    PaperOnlyViolation,
    UnsupportedOrderShape,
)


CASSETTE_DIR = Path(__file__).parent / "fixtures" / "cassettes" / "alpaca"


# ---------------------------------------------------------------------------
# Cassette helpers
# ---------------------------------------------------------------------------


def _load_cassette(name: str) -> dict[str, Any]:
    return json.loads((CASSETTE_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Fake AlpacaClient double — exposes the surface PaperExecutor uses.
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Duck-typed substitute for :class:`AlpacaClient` for unit tests."""

    def __init__(
        self,
        *,
        base_url: str = PAPER_BASE_URL,
        submit_order_results: list[Any] | None = None,
        submit_order_error: Exception | None = None,
        get_order_results: list[Any] | None = None,
        positions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = base_url
        self._submit_results = list(submit_order_results or [])
        self._submit_error = submit_order_error
        self._get_order_results = list(get_order_results or [])
        self._positions = list(positions or [])
        self.submit_calls: list[Any] = []
        self.get_order_calls: list[str] = []
        self.get_positions_calls: int = 0

    def get_positions(self) -> list[dict[str, Any]]:
        self.get_positions_calls += 1
        return list(self._positions)

    def submit_order(self, order_request: Any) -> dict[str, Any]:
        self.submit_calls.append(order_request)
        if self._submit_error is not None:
            raise self._submit_error
        if not self._submit_results:
            raise AssertionError(
                "_FakeAlpacaClient.submit_order called but no result queued"
            )
        return self._submit_results.pop(0)

    def get_order(self, order_id: str) -> dict[str, Any]:
        self.get_order_calls.append(order_id)
        if not self._get_order_results:
            raise AssertionError(
                "_FakeAlpacaClient.get_order called but no result queued"
            )
        result = self._get_order_results[0]
        # Last queued result sticks (mirrors a terminal-status sandbox
        # state where re-polling keeps returning the same row).
        if len(self._get_order_results) > 1:
            self._get_order_results.pop(0)
        return result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Fresh SQLite db for each test (orders table is auto-created)."""
    return tmp_path / "alpha_sniper.db"


@pytest.fixture
def make_executor(db_path: Path):
    """Factory: build a PaperExecutor with an injected fake client."""

    def _factory(
        *,
        client: _FakeAlpacaClient | None = None,
    ) -> tuple[PaperExecutor, _FakeAlpacaClient]:
        fake = client or _FakeAlpacaClient()
        executor = PaperExecutor(
            fake,  # type: ignore[arg-type]
            db_path=db_path,
            poll_interval_seconds=0.0,
        )
        return executor, fake

    return _factory


def _call_play_card() -> dict[str, Any]:
    return {
        "play_card_id": "AXSM-2025-04-27",
        "ticker": "AXSM",
        "option_legs": [
            {
                "symbol": "AXSM250620C00125000",
                "side": "buy",
                "qty": 2,
                "limit_price": 1.50,
                "option_type": "call",
                "strike": 125.0,
                "expiry": "2025-06-20",
                "client_order_id": "biotech-sniper-AXSM-call-2025-04-27",
            }
        ],
    }


def _put_play_card() -> dict[str, Any]:
    return {
        "play_card_id": "NTLA-2025-04-27",
        "ticker": "NTLA",
        "option_legs": [
            {
                "symbol": "NTLA250620P00010000",
                "side": "buy",
                "qty": 3,
                "limit_price": 0.85,
                "option_type": "put",
                "strike": 10.0,
                "expiry": "2025-06-20",
                "client_order_id": "biotech-sniper-NTLA-put-2025-04-27",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Constructor: paper-only enforcement (VAL-M3-008).
# ---------------------------------------------------------------------------


def test_constructor_refuses_non_paper_client(db_path: Path):
    """PaperOnlyViolation is raised when client.base_url is not paper."""
    fake = _FakeAlpacaClient(base_url=LIVE_BASE_URL)
    with pytest.raises(PaperOnlyViolation, match="paper"):
        PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]


def test_constructor_refuses_arbitrary_url(db_path: Path):
    """Even non-Alpaca URLs trip the paper-only assertion."""
    fake = _FakeAlpacaClient(base_url="https://example.com")
    with pytest.raises(PaperOnlyViolation):
        PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]


def test_constructor_accepts_paper_client(db_path: Path):
    """Paper URL constructs successfully and creates the orders table."""
    fake = _FakeAlpacaClient(base_url=PAPER_BASE_URL)
    executor = PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]
    assert executor.client is fake
    assert executor.db_path == db_path

    # Schema migration ran → paper_orders table exists.
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='paper_orders'"
        ).fetchall()
        assert rows, "paper_orders table missing after PaperExecutor construction"
    finally:
        conn.close()


def test_runtime_paper_only_check_during_execute(db_path: Path):
    """Drifting client.base_url after construction also raises."""
    fake = _FakeAlpacaClient(base_url=PAPER_BASE_URL)
    executor = PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]
    fake.base_url = LIVE_BASE_URL
    with pytest.raises(PaperOnlyViolation):
        executor.execute(_call_play_card())


# ---------------------------------------------------------------------------
# VAL-M3-014: Single-leg long call submission roundtrip.
# ---------------------------------------------------------------------------


def test_execute_call_returns_alpaca_order_id(make_executor):
    """execute() returns the broker-assigned order id as a string."""
    cassette = _load_cassette("order_call_roundtrip.json")
    fake = _FakeAlpacaClient(submit_order_results=[cassette["submit"]])
    executor, _ = make_executor(client=fake)

    order_id = executor.execute(_call_play_card())

    assert isinstance(order_id, str)
    assert order_id == "22222222-2222-2222-2222-222222222222"
    assert len(fake.submit_calls) == 1


def test_execute_call_persists_orders_row(make_executor, db_path: Path):
    """Successful submission writes an orders row keyed on play_card_id."""
    cassette = _load_cassette("order_call_roundtrip.json")
    fake = _FakeAlpacaClient(submit_order_results=[cassette["submit"]])
    executor, _ = make_executor(client=fake)

    executor.execute(_call_play_card())

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM paper_orders WHERE play_card_id = ?",
            ("AXSM-2025-04-27",),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["alpaca_order_id"] == "22222222-2222-2222-2222-222222222222"
    assert row["symbol"] == "AXSM250620C00125000"
    assert row["side"] == "buy"
    assert row["qty"] == 2
    assert row["status"] == "accepted"
    assert row["reason"] is None


def test_get_order_returns_symbol_matching_request(make_executor):
    """After execute(), get_order(id)['symbol'] matches the OCC symbol."""
    cassette = _load_cassette("order_call_roundtrip.json")
    fake = _FakeAlpacaClient(
        submit_order_results=[cassette["submit"]],
        get_order_results=[cassette["submit"]],
    )
    executor, _ = make_executor(client=fake)

    order_id = executor.execute(_call_play_card())
    fetched = executor.get_order(order_id)

    assert fetched["symbol"] == "AXSM250620C00125000"
    assert fetched["id"] == order_id


# ---------------------------------------------------------------------------
# VAL-M3-015: Single-leg long put submission roundtrip.
# ---------------------------------------------------------------------------


def test_execute_put_side_qty_order_class(make_executor, db_path: Path):
    """Put order: side=buy, qty>=1, order_class=simple in the response."""
    cassette = _load_cassette("order_put_roundtrip.json")
    fake = _FakeAlpacaClient(submit_order_results=[cassette["submit"]])
    executor, _ = make_executor(client=fake)

    order_id = executor.execute(_put_play_card())
    assert order_id == "33333333-3333-3333-3333-333333333333"

    submitted = cassette["submit"]
    assert submitted["side"] == "buy"
    assert submitted["qty"] >= 1
    assert submitted["order_class"] == "simple"

    # Verify the persisted orders row reflects the put.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM paper_orders WHERE play_card_id = ?",
            ("NTLA-2025-04-27",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["symbol"] == "NTLA250620P00010000"
    assert row["side"] == "buy"
    assert row["qty"] == 3


# ---------------------------------------------------------------------------
# VAL-M3-016: Sandbox poll loop walks accepted → filled within 30s.
# ---------------------------------------------------------------------------


def test_wait_for_fill_walks_accepted_to_filled(make_executor, db_path: Path):
    """The poll loop terminates on 'filled' and persists the transition."""
    cassette = _load_cassette("order_call_roundtrip.json")
    fake = _FakeAlpacaClient(
        submit_order_results=[cassette["submit"]],
        get_order_results=[
            cassette["poll_intermediate"],
            cassette["poll_filled"],
        ],
    )
    executor, _ = make_executor(client=fake)

    order_id = executor.execute(_call_play_card())
    final = executor.wait_for_fill(
        order_id, timeout_seconds=30.0, poll_interval_seconds=0.0
    )

    assert final["status"] == "filled"
    assert final["filled_qty"] == 2
    # We polled at least twice (intermediate + filled).
    assert len(fake.get_order_calls) >= 2

    # Persisted row should now reflect the terminal status.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status FROM paper_orders WHERE alpaca_order_id = ?",
            (order_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["status"] == "filled"


def test_wait_for_fill_respects_timeout(make_executor):
    """When status never reaches terminal, the loop returns by deadline."""
    cassette = _load_cassette("order_call_roundtrip.json")
    accepted = cassette["poll_intermediate"]
    fake = _FakeAlpacaClient(
        submit_order_results=[cassette["submit"]],
        get_order_results=[accepted],  # last result sticks
    )
    executor, _ = make_executor(client=fake)

    order_id = executor.execute(_call_play_card())
    # 0s timeout + 0s poll interval — loop polls once then bails.
    final = executor.wait_for_fill(
        order_id, timeout_seconds=0.0, poll_interval_seconds=0.0
    )
    assert final["status"] == "accepted"
    assert len(fake.get_order_calls) >= 1


# ---------------------------------------------------------------------------
# VAL-M3-017: Multi-leg / spread orders rejected with no submission.
# ---------------------------------------------------------------------------


def test_multi_leg_play_card_raises_unsupported_order_shape(
    make_executor, db_path: Path
):
    """Two-leg play card raises UnsupportedOrderShape; no order submitted."""
    fake = _FakeAlpacaClient()
    executor, _ = make_executor(client=fake)

    multi_leg = {
        "play_card_id": "MULTI-2025-04-27",
        "option_legs": [
            {
                "symbol": "AXSM250620C00125000",
                "side": "buy",
                "qty": 1,
                "option_type": "call",
            },
            {
                "symbol": "AXSM250620C00130000",
                "side": "sell",
                "qty": 1,
                "option_type": "call",
            },
        ],
    }
    with pytest.raises(UnsupportedOrderShape, match="multi-leg"):
        executor.execute(multi_leg)

    # No call into the broker.
    assert fake.submit_calls == []
    # No row persisted (rejection from shape validation is pre-persistence).
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_mleg_order_class_raises_unsupported_order_shape(make_executor):
    """Explicit order_class='mleg' on a single-leg card still rejects."""
    fake = _FakeAlpacaClient()
    executor, _ = make_executor(client=fake)
    play_card = _call_play_card()
    play_card["order_class"] = "mleg"
    with pytest.raises(UnsupportedOrderShape, match="order_class"):
        executor.execute(play_card)
    assert fake.submit_calls == []


def test_empty_legs_raises(make_executor):
    fake = _FakeAlpacaClient()
    executor, _ = make_executor(client=fake)
    with pytest.raises(UnsupportedOrderShape, match="empty"):
        executor.execute({"play_card_id": "x", "option_legs": []})


def test_missing_legs_key_raises(make_executor):
    fake = _FakeAlpacaClient()
    executor, _ = make_executor(client=fake)
    with pytest.raises(UnsupportedOrderShape):
        executor.execute({"play_card_id": "x"})


def test_invalid_qty_raises(make_executor):
    fake = _FakeAlpacaClient()
    executor, _ = make_executor(client=fake)
    bad = _call_play_card()
    bad["option_legs"][0]["qty"] = 0
    with pytest.raises(UnsupportedOrderShape, match=">= 1"):
        executor.execute(bad)


def test_invalid_option_type_raises(make_executor):
    fake = _FakeAlpacaClient()
    executor, _ = make_executor(client=fake)
    bad = _call_play_card()
    bad["option_legs"][0]["option_type"] = "straddle"
    with pytest.raises(UnsupportedOrderShape, match="option_type"):
        executor.execute(bad)


# ---------------------------------------------------------------------------
# VAL-M3-018: Failed submission leaves clean state.
# ---------------------------------------------------------------------------


def test_rejection_raises_order_rejected(make_executor):
    """Broker 4xx surfaces as OrderRejected (subclass of PaperExecutorError)."""
    fake = _FakeAlpacaClient(
        submit_order_error=AlpacaClientError(
            "Alpaca API error: invalid symbol AXSM250620C00125000"
        )
    )
    executor, _ = make_executor(client=fake)

    with pytest.raises(OrderRejected, match="invalid symbol") as excinfo:
        executor.execute(_call_play_card())
    assert isinstance(excinfo.value, PaperExecutorError)


def test_rejection_persists_status_rejected_with_reason(
    make_executor, db_path: Path
):
    """Rejection writes a row with status='rejected' + broker reason."""
    fake = _FakeAlpacaClient(
        submit_order_error=AlpacaClientError(
            "Alpaca API error: invalid symbol AXSM250620C00125000"
        )
    )
    executor, _ = make_executor(client=fake)

    with pytest.raises(OrderRejected):
        executor.execute(_call_play_card())

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT status, reason FROM paper_orders WHERE play_card_id = ?",
            ("AXSM-2025-04-27",),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["status"] == "rejected"
    assert rows[0]["reason"] is not None
    assert "invalid symbol" in rows[0]["reason"]


def test_rejection_does_not_persist_submitted_row(make_executor, db_path: Path):
    """Crucial VAL-M3-018 invariant: no row with status='submitted' on rejection."""
    fake = _FakeAlpacaClient(
        submit_order_error=AlpacaAuthError("HTTP 422: invalid symbol")
    )
    executor, _ = make_executor(client=fake)

    with pytest.raises(OrderRejected):
        executor.execute(_call_play_card())

    conn = sqlite3.connect(db_path)
    try:
        # No 'submitted' or 'accepted' row should exist for this play.
        count = conn.execute(
            "SELECT COUNT(*) FROM paper_orders "
            "WHERE play_card_id = ? AND status IN ('submitted','accepted')",
            ("AXSM-2025-04-27",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_rejection_logs_structured_warning(make_executor, caplog):
    """A WARNING-level log line carries the broker reason."""
    fake = _FakeAlpacaClient(
        submit_order_error=AlpacaTransportError("HTTP 503: service unavailable")
    )
    executor, _ = make_executor(client=fake)
    caplog.set_level(logging.WARNING, logger="biotech_sniper.paper_executor")

    with pytest.raises(OrderRejected):
        executor.execute(_call_play_card())

    rejected_lines = [
        r for r in caplog.records if "order_rejected" in r.getMessage()
    ]
    assert rejected_lines, "expected paper_executor.order_rejected log line"
    assert any(
        "503" in r.getMessage() for r in rejected_lines
    ), "broker reason missing from log line"


# ---------------------------------------------------------------------------
# get_orders_for_play helper.
# ---------------------------------------------------------------------------


def test_get_orders_for_play_returns_chronological_rows(
    make_executor, db_path: Path
):
    """Helper returns rows in created_at ASC order.

    Under f-m3-11 the executor short-circuits on duplicate
    ``client_order_id`` derived from the play_card. To exercise
    chronological ordering we submit two *distinct* legs (entry +
    exit) under the same ``play_card_id`` — both rows map to the
    same play but each derives a unique ``client_order_id`` (the
    purpose differs).
    """
    cassette = _load_cassette("order_call_roundtrip.json")
    submit_two = dict(cassette["submit"])
    submit_two = dict(submit_two)
    submit_two["id"] = "44444444-4444-4444-4444-444444444444"
    fake = _FakeAlpacaClient(
        submit_order_results=[cassette["submit"], submit_two]
    )
    executor, _ = make_executor(client=fake)

    entry = _call_play_card()
    executor.execute(entry)

    # Submit an exit-purpose order under the same play_card_id; the
    # f-m3-11 client_order_id derivation includes ``event`` and
    # ``parent_play_card_id`` so this row gets a distinct
    # client_order_id and is NOT short-circuited.
    exit_play = dict(entry)
    exit_play["event"] = "iv_crush_exit"
    exit_play["parent_play_card_id"] = "AXSM-2025-04-27"
    exit_leg = dict(entry["option_legs"][0])
    exit_leg["side"] = "sell"
    # The entry leg ships an explicit client_order_id; override it
    # on the exit leg so the f-m3-11 idempotency lookup does NOT
    # short-circuit the second submission.
    exit_leg["client_order_id"] = "test-exit-AXSM-call-2025-04-27"
    exit_play["option_legs"] = [exit_leg]
    executor.execute(exit_play)

    rows = executor.get_orders_for_play("AXSM-2025-04-27")
    assert len(rows) == 2
    assert rows[0]["created_at"] <= rows[1]["created_at"]


def test_get_orders_for_play_empty_for_unknown_id(make_executor):
    executor, _ = make_executor()
    assert executor.get_orders_for_play("NONEXISTENT") == []


# ---------------------------------------------------------------------------
# Schema sanity (orders table exists at the documented version).
# ---------------------------------------------------------------------------


def test_orders_table_columns_match_spec(db_path: Path):
    """The orders table exposes the columns the f-m3-03 contract names."""
    fake = _FakeAlpacaClient()
    PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]
    conn = sqlite3.connect(db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_orders)").fetchall()}
    finally:
        conn.close()
    expected = {
        "id",
        "play_card_id",
        "alpaca_order_id",
        "symbol",
        "side",
        "qty",
        "status",
        "reason",
        "event",
        "parent_play_card_id",
        "created_at",
    }
    missing = expected - cols
    assert not missing, f"missing columns in orders table: {missing}"


def test_schema_version_bumped(db_path: Path):
    """f-m3-03 bumps CURRENT_VERSION to 5 (sanity floor)."""
    fake = _FakeAlpacaClient()
    PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]
    assert db_module.CURRENT_VERSION >= 5
    conn = sqlite3.connect(db_path)
    try:
        version = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
    finally:
        conn.close()
    assert version == db_module.CURRENT_VERSION
