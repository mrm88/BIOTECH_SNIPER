"""Unit tests for :mod:`biotech_sniper.stop_loss` (f-m3-09).

The stop-loss trigger fires when a position's drawdown exceeds the
configured ``STOP_LOSS_PCT`` threshold (default ``-0.50``) and submits
an exit order tagged ``event='stop_loss'`` that closes 100% of the
position. These tests exercise:

* The pure helpers (:func:`compute_drawdown_pct`,
  :func:`should_stop_loss`).
* The runner's at-threshold / above-threshold dispatch logic.
* Idempotency: calling :meth:`StopLossRunner.run_check` twice on
  the same day for the same play does not duplicate the exit.
* :data:`config.STOP_LOSS_PCT` is exposed and equals ``-0.50``.

Validation contract assertions exercised
----------------------------------------
* **VAL-M3-049** — ``config.STOP_LOSS_PCT == -0.50``.
* **VAL-M3-050** — drawdown ≤ -0.50 fires; idempotent on
  ``client_order_id = f"{ticker}-stop_loss-{date}"``.
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper import config
from biotech_sniper import stop_loss as sl
from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.paper_executor import PaperExecutor


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    def __init__(self) -> None:
        self.base_url = PAPER_BASE_URL
        self.submit_calls: list[Any] = []
        self._submit_results: list[dict[str, Any]] = []

    def queue(self, response: dict[str, Any]) -> None:
        self._submit_results.append(response)

    def get_positions(self) -> list[dict[str, Any]]:
        return []

    def submit_order(self, order_request: Any) -> dict[str, Any]:
        self.submit_calls.append(order_request)
        if not self._submit_results:
            raise AssertionError(
                "_FakeAlpacaClient.submit_order: no result queued"
            )
        return self._submit_results.pop(0)

    def get_order(self, order_id: str) -> dict[str, Any]:
        return {"id": order_id, "status": "accepted"}


def _stub_sell_response(
    *, order_id: str = "22222222-2222-2222-2222-222222222222", qty: int = 4
) -> dict[str, Any]:
    return {
        "id": order_id,
        "client_order_id": f"AXSM-stop_loss-2025-04-27",
        "symbol": "AXSM250620C00125000",
        "asset_class": "us_option",
        "qty": qty,
        "side": "sell",
        "status": "accepted",
        "order_class": "simple",
        "type": "market",
        "time_in_force": "day",
    }


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "alpha_sniper.db"


@pytest.fixture
def make_runner(db_path: Path):
    def _factory(
        *, client: _FakeAlpacaClient | None = None, threshold: float | None = None
    ) -> tuple[sl.StopLossRunner, PaperExecutor, _FakeAlpacaClient]:
        fake = client or _FakeAlpacaClient()
        executor = PaperExecutor(
            fake,  # type: ignore[arg-type]
            db_path=db_path,
            poll_interval_seconds=0.0,
        )
        runner = sl.StopLossRunner(executor, threshold=threshold)
        return runner, executor, fake

    return _factory


def _make_position(
    *,
    ticker: str = "AXSM",
    play_card_id: str = "AXSM-2025-04-27",
    symbol: str = "AXSM250620C00125000",
    catalyst_date: str = "2025-04-30",
    qty: int = 4,
    entry_mid: float = 2.00,
    current_mid: float = 1.00,
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "play_card_id": play_card_id,
        "symbol": symbol,
        "catalyst_date": catalyst_date,
        "qty": qty,
        "entry_mid": entry_mid,
        "current_mid": current_mid,
    }


# ---------------------------------------------------------------------------
# 1) config.STOP_LOSS_PCT (VAL-M3-049).
# ---------------------------------------------------------------------------


def test_stop_loss_pct_in_config() -> None:
    """``config.STOP_LOSS_PCT`` is exposed and equals -0.50."""
    assert hasattr(config, "STOP_LOSS_PCT")
    assert config.STOP_LOSS_PCT == -0.50


# ---------------------------------------------------------------------------
# 2) Pure helpers.
# ---------------------------------------------------------------------------


def test_compute_drawdown_pct_signed() -> None:
    """Drawdown is signed and computed as ``current/entry - 1``."""
    assert sl.compute_drawdown_pct(entry_mid=2.0, current_mid=1.0) == -0.5
    assert sl.compute_drawdown_pct(entry_mid=2.0, current_mid=2.0) == 0.0
    assert sl.compute_drawdown_pct(entry_mid=2.0, current_mid=3.0) == 0.5


def test_compute_drawdown_pct_handles_bad_inputs() -> None:
    """Non-positive / unparseable inputs return None."""
    assert sl.compute_drawdown_pct(entry_mid=0.0, current_mid=1.0) is None
    assert sl.compute_drawdown_pct(entry_mid=-1.0, current_mid=1.0) is None
    assert sl.compute_drawdown_pct(entry_mid="abc", current_mid=1.0) is None  # type: ignore[arg-type]


def test_should_stop_loss_at_threshold_fires() -> None:
    """Exact -0.50 drawdown fires (boundary inclusive)."""
    assert sl.should_stop_loss(entry_mid=2.0, current_mid=1.0) is True


def test_should_stop_loss_above_threshold_does_not_fire() -> None:
    """A -0.49 drawdown is NOT enough to fire."""
    # -0.495 > -0.50 → no fire.
    assert (
        sl.should_stop_loss(entry_mid=2.0, current_mid=1.01) is False
    )


def test_should_stop_loss_disabled_for_positive_threshold() -> None:
    """A non-negative threshold disables the trigger."""
    assert (
        sl.should_stop_loss(
            entry_mid=2.0, current_mid=0.5, threshold=0.0
        )
        is False
    )


# ---------------------------------------------------------------------------
# 3) Runner: fires at -50%.
# ---------------------------------------------------------------------------


def test_stop_loss_fires_at_minus_50_percent(make_runner, db_path: Path) -> None:
    """A position at -50% drawdown produces exactly one stop_loss exit."""
    fake = _FakeAlpacaClient()
    fake.queue(_stub_sell_response(qty=4))
    runner, _, _ = make_runner(client=fake)

    pos = _make_position(entry_mid=2.0, current_mid=1.0, qty=4)
    today = datetime.date(2025, 4, 27)
    results = runner.run_check([pos], today=today)

    assert len(results) == 1
    assert results[0]["status"] == "submitted"
    assert results[0]["event"] == "stop_loss"
    assert results[0]["qty"] == 4
    assert len(fake.submit_calls) == 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT event, side, qty, parent_play_card_id FROM orders "
            "WHERE event = ?",
            ("stop_loss",),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["side"] == "sell"
    assert rows[0]["qty"] == 4
    assert rows[0]["parent_play_card_id"] == "AXSM-2025-04-27"


# ---------------------------------------------------------------------------
# 4) Runner: does NOT fire above threshold.
# ---------------------------------------------------------------------------


def test_stop_loss_does_not_fire_above_threshold(
    make_runner, db_path: Path
) -> None:
    """A position at -40% drawdown does not produce any exit."""
    fake = _FakeAlpacaClient()
    runner, _, _ = make_runner(client=fake)

    pos = _make_position(entry_mid=2.0, current_mid=1.20, qty=4)
    today = datetime.date(2025, 4, 27)
    results = runner.run_check([pos], today=today)

    assert len(results) == 1
    assert results[0]["status"] == "skipped"
    assert results[0]["reason"] == "above_threshold"
    assert fake.submit_calls == []
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE event = ?", ("stop_loss",)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0


# ---------------------------------------------------------------------------
# 5) Runner: 100% close (sell qty matches open qty).
# ---------------------------------------------------------------------------


def test_stop_loss_closes_full_position(make_runner, db_path: Path) -> None:
    """The submitted sell qty equals the open qty (100% close)."""
    fake = _FakeAlpacaClient()
    fake.queue(_stub_sell_response(qty=7))
    runner, _, _ = make_runner(client=fake)

    pos = _make_position(qty=7, entry_mid=2.0, current_mid=0.50)
    today = datetime.date(2025, 4, 27)
    results = runner.run_check([pos], today=today)

    assert results[0]["status"] == "submitted"
    assert results[0]["qty"] == 7
    request = fake.submit_calls[0]
    assert int(getattr(request, "qty")) == 7


# ---------------------------------------------------------------------------
# 6) Runner: idempotent on re-invocation.
# ---------------------------------------------------------------------------


def test_stop_loss_idempotent_on_same_day(
    make_runner, db_path: Path
) -> None:
    """Second call same day → no duplicate exit row."""
    fake = _FakeAlpacaClient()
    fake.queue(_stub_sell_response(qty=4))
    runner, _, _ = make_runner(client=fake)

    pos = _make_position(qty=4, entry_mid=2.0, current_mid=0.50)
    today = datetime.date(2025, 4, 27)

    first = runner.run_check([pos], today=today)
    second = runner.run_check([pos], today=today)

    assert len(fake.submit_calls) == 1
    assert first[0]["status"] == "submitted"
    assert second[0]["status"] == "submitted"
    assert first[0].get("order_id") == second[0].get("order_id")

    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE event = ?", ("stop_loss",)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1
