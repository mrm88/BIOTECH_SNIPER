"""Tests for the f-m3-05 IV-crush exit autotrigger.

These exercise :class:`biotech_sniper.iv_crush_exit_rules.IVCrushExitRunner`
and the module-level :func:`run_on_open` convenience wrapper. All tests
are hermetic — they use a duck-typed fake :class:`AlpacaClient` so no
network calls are made and the SQLite ``orders`` table is materialised
under :func:`tmp_path`.

Validation contract assertions exercised:

* **VAL-M3-026** — :func:`run_on_open` selects positions with
  ``catalyst_date == today`` and skips off-catalyst dates.
* **VAL-M3-027** — sells ``floor(N/2)`` contracts (N=4→2, N=1→skip
  with ``minimum_size`` log line).
* **VAL-M3-028** — sell order id logged + persisted to ``orders``
  with ``event='iv_crush_exit'`` and ``parent_play_card_id`` linking
  to the entry play card.
* **VAL-M3-029** — second invocation same trading day → no duplicate;
  emits ``already_iv_crush_exited`` log line.
* **VAL-M3-030** — runner inherits the paper-only guardrail; a
  drifted ``client.base_url`` raises :class:`PaperOnlyViolation`
  before any submission.
"""

from __future__ import annotations

import datetime
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper.alpaca_client import (
    AlpacaClientError,
    LIVE_BASE_URL,
    PAPER_BASE_URL,
)
from biotech_sniper.iv_crush_exit_rules import (
    IVCrushExitRunner,
    IV_CRUSH_EXIT_EVENT,
    run_on_open,
)
from biotech_sniper.paper_executor import (
    PaperExecutor,
    PaperOnlyViolation,
)


# ---------------------------------------------------------------------------
# Fake Alpaca client (mirrors test_paper_executor.py).
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Duck-typed substitute exposing the methods PaperExecutor invokes."""

    def __init__(
        self,
        *,
        base_url: str = PAPER_BASE_URL,
        submit_order_results: list[Any] | None = None,
        submit_order_error: Exception | None = None,
        positions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = base_url
        self._submit_results = list(submit_order_results or [])
        self._submit_error = submit_order_error
        self._positions = list(positions or [])
        self.submit_calls: list[Any] = []
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
        # IV-crush exit autotrigger does not poll for fills; provide a
        # reasonable fallback so any future test that exercises a poll
        # loop does not crash.
        return {"id": order_id, "status": "accepted"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_sell_response(
    *,
    order_id: str = "55555555-5555-5555-5555-555555555555",
    symbol: str = "AXSM250620C00125000",
    qty: int = 2,
) -> dict[str, Any]:
    """Build a minimal Alpaca-shaped order response for a sell submit."""
    return {
        "id": order_id,
        "client_order_id": f"biotech-sniper-{symbol}-iv_crush_exit",
        "symbol": symbol,
        "asset_class": "us_option",
        "qty": qty,
        "filled_qty": 0,
        "filled_avg_price": None,
        "side": "sell",
        "status": "accepted",
        "order_class": "simple",
        "order_type": "market",
        "type": "market",
        "time_in_force": "day",
        "limit_price": None,
        "stop_price": None,
        "created_at": "2025-04-27T13:30:00Z",
        "updated_at": "2025-04-27T13:30:00Z",
        "submitted_at": "2025-04-27T13:30:00Z",
        "filled_at": None,
        "canceled_at": None,
    }


def _make_position(
    *,
    ticker: str = "AXSM",
    play_card_id: str = "AXSM-2025-04-27",
    symbol: str = "AXSM250620C00125000",
    catalyst_date: str = "2025-04-27",
    qty: int = 4,
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "play_card_id": play_card_id,
        "symbol": symbol,
        "catalyst_date": catalyst_date,
        "qty": qty,
    }


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "alpha_sniper.db"


@pytest.fixture
def make_runner(db_path: Path):
    """Factory returning ``(runner, executor, fake_client)``."""

    def _factory(
        *,
        client: _FakeAlpacaClient | None = None,
    ) -> tuple[IVCrushExitRunner, PaperExecutor, _FakeAlpacaClient]:
        fake = client or _FakeAlpacaClient()
        executor = PaperExecutor(
            fake,  # type: ignore[arg-type]
            db_path=db_path,
            poll_interval_seconds=0.0,
        )
        runner = IVCrushExitRunner(executor)
        return runner, executor, fake

    return _factory


# ---------------------------------------------------------------------------
# VAL-M3-026: catalyst-day selection.
# ---------------------------------------------------------------------------


def test_run_on_open_selects_position_when_catalyst_today(make_runner):
    """A position whose catalyst_date is today is selected for exit."""
    today = datetime.date(2025, 4, 27)
    fake = _FakeAlpacaClient(
        submit_order_results=[_stub_sell_response()]
    )
    runner, _, _ = make_runner(client=fake)

    results = runner.run_on_open(
        active_plays=[_make_position(catalyst_date=today.isoformat(), qty=4)],
        today=today,
    )

    assert len(results) == 1
    assert results[0]["status"] == "submitted"
    assert results[0]["qty"] == 2
    assert len(fake.submit_calls) == 1


def test_run_on_open_skips_position_when_catalyst_tomorrow(make_runner):
    """A position whose catalyst_date is tomorrow is skipped (no order)."""
    today = datetime.date(2025, 4, 27)
    tomorrow = today + datetime.timedelta(days=1)
    fake = _FakeAlpacaClient()
    runner, _, _ = make_runner(client=fake)

    results = runner.run_on_open(
        active_plays=[
            _make_position(catalyst_date=tomorrow.isoformat(), qty=4)
        ],
        today=today,
    )

    assert len(results) == 1
    assert results[0]["status"] == "skipped"
    assert results[0]["reason"] == "not_catalyst_date"
    assert fake.submit_calls == []


def test_run_on_open_iterates_zero_orders_for_no_catalyst_today(
    make_runner, db_path: Path
):
    """No positions match today → zero orders, zero rows in orders table."""
    today = datetime.date(2025, 4, 27)
    fake = _FakeAlpacaClient()
    runner, _, _ = make_runner(client=fake)

    results = runner.run_on_open(
        active_plays=[
            _make_position(catalyst_date="2025-04-30", qty=4),
            _make_position(
                ticker="ZZZZ",
                play_card_id="ZZZZ-2025-04-25",
                symbol="ZZZZ250620C00050000",
                catalyst_date="2025-05-15",
                qty=2,
            ),
        ],
        today=today,
    )

    assert all(r["status"] == "skipped" for r in results)
    assert fake.submit_calls == []
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_run_on_open_legacy_pdufa_date_key_is_honoured(make_runner):
    """The legacy ``pdufa_date`` key is treated as catalyst_date."""
    today = datetime.date(2025, 4, 27)
    fake = _FakeAlpacaClient(
        submit_order_results=[_stub_sell_response()]
    )
    runner, _, _ = make_runner(client=fake)

    legacy_play = {
        "ticker": "AXSM",
        "play_card_id": "AXSM-LEGACY-2025-04-27",
        "option_symbol": "AXSM250620C00125000",
        "pdufa_date": today.isoformat(),
        "contracts": 4,
    }
    results = runner.run_on_open(active_plays=[legacy_play], today=today)
    assert len(results) == 1
    assert results[0]["status"] == "submitted"
    assert results[0]["qty"] == 2


# ---------------------------------------------------------------------------
# VAL-M3-027: floor(N/2) sizing + N=1 minimum_size skip.
# ---------------------------------------------------------------------------


def test_run_on_open_sells_floor_n_over_2_for_n_equal_4(make_runner):
    """N=4 → exactly one sell order with qty=2."""
    today = datetime.date(2025, 4, 27)
    fake = _FakeAlpacaClient(
        submit_order_results=[_stub_sell_response(qty=2)]
    )
    runner, _, _ = make_runner(client=fake)

    results = runner.run_on_open(
        active_plays=[_make_position(catalyst_date=today.isoformat(), qty=4)],
        today=today,
    )
    assert len(fake.submit_calls) == 1
    request = fake.submit_calls[0]
    assert int(getattr(request, "qty")) == 2
    assert results[0]["qty"] == 2


def test_run_on_open_sells_floor_n_over_2_for_n_equal_5(make_runner):
    """N=5 → floor(5/2) = 2 contracts."""
    today = datetime.date(2025, 4, 27)
    fake = _FakeAlpacaClient(
        submit_order_results=[_stub_sell_response(qty=2)]
    )
    runner, _, _ = make_runner(client=fake)
    runner.run_on_open(
        active_plays=[_make_position(catalyst_date=today.isoformat(), qty=5)],
        today=today,
    )
    assert int(getattr(fake.submit_calls[0], "qty")) == 2


def test_run_on_open_skips_n_equal_1_with_minimum_size_log(
    make_runner, caplog, db_path: Path
):
    """N=1 → no order submitted, structured ``minimum_size`` log line."""
    today = datetime.date(2025, 4, 27)
    fake = _FakeAlpacaClient()
    runner, _, _ = make_runner(client=fake)
    caplog.set_level(logging.INFO, logger="biotech_sniper.iv_crush_exit_rules")

    results = runner.run_on_open(
        active_plays=[_make_position(catalyst_date=today.isoformat(), qty=1)],
        today=today,
    )

    # No broker call, no orders row.
    assert fake.submit_calls == []
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]
    finally:
        conn.close()
    assert count == 0

    # Result entry + structured log.
    assert len(results) == 1
    assert results[0]["status"] == "skipped"
    assert results[0]["reason"] == "minimum_size"

    skipped_lines = [
        r for r in caplog.records if "iv_crush_exit.skipped" in r.getMessage()
    ]
    assert any(
        "minimum_size" in r.getMessage() for r in skipped_lines
    ), f"expected minimum_size log line, got: {[r.getMessage() for r in skipped_lines]}"


# ---------------------------------------------------------------------------
# VAL-M3-028: persistence + structured submit log.
# ---------------------------------------------------------------------------


def test_run_on_open_persists_orders_row_with_event_and_parent_link(
    make_runner, db_path: Path
):
    """Persisted row has event='iv_crush_exit' + parent_play_card_id link."""
    today = datetime.date(2025, 4, 27)
    fake = _FakeAlpacaClient(
        submit_order_results=[
            _stub_sell_response(
                order_id="55555555-5555-5555-5555-555555555555",
                qty=2,
            )
        ]
    )
    runner, _, _ = make_runner(client=fake)

    runner.run_on_open(
        active_plays=[
            _make_position(
                play_card_id="AXSM-2025-04-27",
                catalyst_date=today.isoformat(),
                qty=4,
            )
        ],
        today=today,
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, alpaca_order_id, event, parent_play_card_id, "
            "qty, side, status FROM paper_orders WHERE event = ?",
            (IV_CRUSH_EXIT_EVENT,),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == "iv_crush_exit"
    assert row["parent_play_card_id"] == "AXSM-2025-04-27"
    assert row["qty"] == 2
    assert row["side"] == "sell"
    assert row["status"] == "accepted"
    assert row["alpaca_order_id"] == "55555555-5555-5555-5555-555555555555"


def test_run_on_open_logs_structured_event_on_submit(
    make_runner, caplog
):
    """Submit log carries event, order_id, qty, parent_play_card_id."""
    today = datetime.date(2025, 4, 27)
    fake = _FakeAlpacaClient(
        submit_order_results=[
            _stub_sell_response(
                order_id="55555555-5555-5555-5555-555555555555",
                qty=2,
            )
        ]
    )
    runner, _, _ = make_runner(client=fake)
    caplog.set_level(logging.INFO, logger="biotech_sniper.iv_crush_exit_rules")

    runner.run_on_open(
        active_plays=[
            _make_position(
                play_card_id="AXSM-2025-04-27",
                catalyst_date=today.isoformat(),
                qty=4,
            )
        ],
        today=today,
    )

    submit_lines = [
        r for r in caplog.records if "iv_crush_exit.exit_submitted" in r.getMessage()
    ]
    assert submit_lines, (
        f"expected exit_submitted log line, got "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    msg = submit_lines[0].getMessage()
    assert "event=iv_crush_exit" in msg
    assert "order_id=55555555-5555-5555-5555-555555555555" in msg
    assert "qty=2" in msg
    assert "parent_play_card_id=AXSM-2025-04-27" in msg


def test_run_on_open_select_query_returns_event_iv_crush_exit(
    make_runner, db_path: Path
):
    """VAL-M3-028 SQL: ``SELECT id FROM paper_orders WHERE event='iv_crush_exit'``
    returns the new sell row."""
    today = datetime.date(2025, 4, 27)
    fake = _FakeAlpacaClient(
        submit_order_results=[_stub_sell_response()]
    )
    runner, _, _ = make_runner(client=fake)
    runner.run_on_open(
        active_plays=[_make_position(catalyst_date=today.isoformat(), qty=4)],
        today=today,
    )

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM paper_orders WHERE event='iv_crush_exit'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# VAL-M3-029: idempotent on re-invocation.
# ---------------------------------------------------------------------------


def test_run_on_open_idempotent_second_call_same_day(
    make_runner, caplog, db_path: Path
):
    """Re-invocation in same trading day → no duplicate; ``already_iv_crush_exited``."""
    today = datetime.date(2025, 4, 27)
    fake = _FakeAlpacaClient(
        # Only ONE submit response queued. A second submit would raise
        # AssertionError from the fake; the runner must short-circuit
        # before calling submit_order again.
        submit_order_results=[_stub_sell_response()]
    )
    runner, _, _ = make_runner(client=fake)

    play = _make_position(catalyst_date=today.isoformat(), qty=4)

    # First call submits.
    first = runner.run_on_open(active_plays=[play], today=today)
    assert first[0]["status"] == "submitted"
    assert len(fake.submit_calls) == 1

    # Second call observes the persisted exit row and short-circuits.
    caplog.clear()
    caplog.set_level(logging.INFO, logger="biotech_sniper.iv_crush_exit_rules")
    second = runner.run_on_open(active_plays=[play], today=today)

    assert len(fake.submit_calls) == 1, "executor.submit_order called twice"
    assert second[0]["status"] == "skipped"
    assert second[0]["reason"] == "already_iv_crush_exited"

    skipped_lines = [
        r for r in caplog.records if "iv_crush_exit.skipped" in r.getMessage()
    ]
    assert any(
        "already_iv_crush_exited" in r.getMessage() for r in skipped_lines
    ), f"expected already_iv_crush_exited log line, got: {[r.getMessage() for r in skipped_lines]}"

    # Exactly one IV-crush exit row in the DB.
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE event=?",
            (IV_CRUSH_EXIT_EVENT,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


# ---------------------------------------------------------------------------
# VAL-M3-030: paper-only enforcement.
# ---------------------------------------------------------------------------


def test_run_on_open_raises_paper_only_violation_on_live_url(
    db_path: Path, tmp_path: Path
):
    """Live-URL drift on the wrapped client raises before any submission."""
    # Construct PaperExecutor against paper, then mutate base_url so the
    # IVCrushExitRunner re-check trips. (This mirrors the validator
    # contract: ``IVCrushExitRunner constructed with a mock executor
    # whose client.base_url is the live URL``.)
    fake = _FakeAlpacaClient(base_url=PAPER_BASE_URL)
    executor = PaperExecutor(
        fake,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )
    runner = IVCrushExitRunner(executor)

    fake.base_url = LIVE_BASE_URL

    today = datetime.date(2025, 4, 27)
    with pytest.raises(PaperOnlyViolation, match="paper"):
        runner.run_on_open(
            active_plays=[
                _make_position(catalyst_date=today.isoformat(), qty=4)
            ],
            today=today,
        )

    # No order submitted before the guardrail tripped.
    assert fake.submit_calls == []


def test_run_on_open_paper_only_violation_message_names_base_url(
    db_path: Path,
):
    """Error message references the paper base URL so operators can debug."""
    fake = _FakeAlpacaClient(base_url=PAPER_BASE_URL)
    executor = PaperExecutor(
        fake,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )
    runner = IVCrushExitRunner(executor)
    fake.base_url = "https://example.com"

    with pytest.raises(PaperOnlyViolation) as excinfo:
        runner.run_on_open(active_plays=[_make_position()], today="2025-04-27")
    msg = str(excinfo.value)
    assert "paper" in msg.lower()
    assert "example.com" in msg


# ---------------------------------------------------------------------------
# Module-level convenience wrapper.
# ---------------------------------------------------------------------------


def test_module_level_run_on_open_delegates_to_runner(make_runner):
    """``run_on_open(executor, ...)`` matches the class-based API."""
    today = datetime.date(2025, 4, 27)
    fake = _FakeAlpacaClient(
        submit_order_results=[_stub_sell_response()]
    )
    _, executor, _ = make_runner(client=fake)

    results = run_on_open(
        executor,
        [_make_position(catalyst_date=today.isoformat(), qty=4)],
        today=today,
    )

    assert len(results) == 1
    assert results[0]["status"] == "submitted"


# ---------------------------------------------------------------------------
# Bonus: caps don't block sells (regression guard for the f-m3-04 cap fix).
# ---------------------------------------------------------------------------


def test_run_on_open_succeeds_when_concurrency_cap_already_full(
    make_runner,
):
    """f-m3-05 cap-skip: 3 active positions does NOT block IV-crush sells.

    The PaperExecutor's MAX_CONCURRENT_PLAYS=3 cap was originally
    raised on every ``execute()`` regardless of side. f-m3-05's exit
    autotrigger requires sells to bypass that cap (the fully-deployed
    state is precisely when exits matter most). This test pins the
    behaviour.
    """
    today = datetime.date(2025, 4, 27)
    fake = _FakeAlpacaClient(
        submit_order_results=[_stub_sell_response()],
        # 4 broker positions — comfortably above MAX_CONCURRENT_PLAYS=3.
        positions=[
            {"qty": "1", "avg_entry_price": "1.20"},
            {"qty": "1", "avg_entry_price": "1.20"},
            {"qty": "1", "avg_entry_price": "1.20"},
            {"qty": "1", "avg_entry_price": "1.20"},
        ],
    )
    runner, _, _ = make_runner(client=fake)

    results = runner.run_on_open(
        active_plays=[_make_position(catalyst_date=today.isoformat(), qty=4)],
        today=today,
    )
    assert len(results) == 1
    assert results[0]["status"] == "submitted"
    # The fake's get_positions should NOT be called for sells.
    assert fake.get_positions_calls == 0
