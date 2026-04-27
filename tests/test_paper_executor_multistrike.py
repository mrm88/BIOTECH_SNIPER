"""f-m3-08: Multi-strike entry tests for :mod:`paper_executor`.

Validates the contract from the f-m3-08 feature description and
VAL-M3-046:

* A play card carrying **2 BUY legs** (different strikes, same
  ticker, no ``order_class != 'simple'``, no
  ``liquidity_classification == 'unfillable'``) is dispatched as a
  multi-strike entry — :meth:`PaperExecutor.execute` returns a
  ``list[str]`` of broker-assigned alpaca_order_ids and persists one
  ``orders`` row per leg, all tagged with the same
  ``play_card_id``.
* The per-play cap (``config.get_risk_per_play_usd()``) is **split
  evenly** across legs so the combined notional stays inside the
  cap. This pins the AGENTS.md "≤ $250 per play" guardrail.
* A play card with **only 1 leg** keeps the single-strike default
  — ``execute()`` returns a ``str``, exactly one orders row is
  persisted.
* A 2-leg play card carrying
  ``liquidity_classification='unfillable'`` is **demoted** to
  single-strike — :func:`_is_multi_strike` returns False so the
  underlying single-leg validator raises
  :class:`UnsupportedOrderShape` (because the card still has 2 legs
  in ``option_legs``). This is the "f-m3-12 forces single-strike"
  fallback hook.

Hermetic — no live network calls; uses the same ``_FakeAlpacaClient``
double pattern as ``tests/test_paper_executor.py``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper import config as _config
from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.paper_executor import (
    ContractTooExpensive,
    PaperExecutor,
    UnsupportedOrderShape,
    _is_multi_strike,
)


# ---------------------------------------------------------------------------
# Fakes — kept self-contained so this test file can run without
# importing test-only helpers from :mod:`tests.test_paper_executor`.
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Minimal duck-typed substitute for AlpacaClient."""

    def __init__(
        self,
        *,
        base_url: str = PAPER_BASE_URL,
        submit_order_results: list[dict[str, Any]] | None = None,
        positions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = base_url
        self._submit_results = list(submit_order_results or [])
        self._positions = list(positions or [])
        self.submit_calls: list[Any] = []

    def get_positions(self) -> list[dict[str, Any]]:
        return list(self._positions)

    def submit_order(self, order_request: Any) -> dict[str, Any]:
        self.submit_calls.append(order_request)
        if not self._submit_results:
            raise AssertionError(
                "_FakeAlpacaClient.submit_order called but no result queued"
            )
        return self._submit_results.pop(0)

    def get_order(self, order_id: str) -> dict[str, Any]:
        # Unused by the multi-strike tests — kept to satisfy the
        # PaperExecutor interface in case a test ever calls it.
        return {"id": order_id, "status": "filled"}


def _broker_response(
    *,
    order_id: str,
    symbol: str,
    qty: int,
    side: str = "buy",
    status: str = "accepted",
) -> dict[str, Any]:
    """Build a minimal broker response mimicking a paper-sandbox accept."""
    return {
        "id": order_id,
        "client_order_id": f"biotech-sniper-{symbol}",
        "symbol": symbol,
        "asset_class": "us_option",
        "qty": qty,
        "filled_qty": 0,
        "filled_avg_price": None,
        "side": side,
        "status": status,
        "order_class": "simple",
        "order_type": "limit",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": 1.50,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "alpha_sniper.db"


@pytest.fixture
def make_executor(db_path: Path):
    def _factory(
        *, client: _FakeAlpacaClient | None = None
    ) -> tuple[PaperExecutor, _FakeAlpacaClient]:
        fake = client or _FakeAlpacaClient()
        executor = PaperExecutor(
            fake,  # type: ignore[arg-type]
            db_path=db_path,
            poll_interval_seconds=0.0,
        )
        return executor, fake

    return _factory


def _multi_strike_play_card(
    *,
    play_card_id: str = "AXSM-multistrike-2026-04-27",
    leg_qty: int | None = 1,
    liquidity_classification: str | None = None,
) -> dict[str, Any]:
    """Two BUY legs at different strikes — the canonical multi-strike."""
    legs = [
        {
            "symbol": "AXSM260620C00120000",
            "side": "buy",
            "limit_price": 1.20,
            "option_type": "call",
            "strike": 120.0,
            "expiry": "2026-06-19",
        },
        {
            "symbol": "AXSM260620C00130000",
            "side": "buy",
            "limit_price": 0.95,
            "option_type": "call",
            "strike": 130.0,
            "expiry": "2026-06-19",
        },
    ]
    if leg_qty is not None:
        for leg in legs:
            leg["qty"] = leg_qty
    card: dict[str, Any] = {
        "play_card_id": play_card_id,
        "ticker": "AXSM",
        "option_legs": legs,
    }
    if liquidity_classification is not None:
        card["liquidity_classification"] = liquidity_classification
    return card


def _single_strike_play_card() -> dict[str, Any]:
    return {
        "play_card_id": "AXSM-single-2026-04-27",
        "ticker": "AXSM",
        "option_legs": [
            {
                "symbol": "AXSM260620C00125000",
                "side": "buy",
                "qty": 1,
                "limit_price": 1.50,
                "option_type": "call",
                "strike": 125.0,
                "expiry": "2026-06-19",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_is_multi_strike_detects_two_buy_legs():
    """_is_multi_strike returns True for 2 BUY legs at different strikes."""
    card = _multi_strike_play_card()
    assert _is_multi_strike(card) is True


def test_is_multi_strike_rejects_unfillable_classification():
    """liquidity_classification='unfillable' demotes to single-strike."""
    card = _multi_strike_play_card(liquidity_classification="unfillable")
    assert _is_multi_strike(card) is False


def test_is_multi_strike_rejects_single_leg():
    """A 1-leg play card is not multi-strike."""
    card = _single_strike_play_card()
    assert _is_multi_strike(card) is False


def test_is_multi_strike_rejects_buy_plus_sell_spread():
    """Spread (1 buy + 1 sell) is NOT multi-strike — that's a vertical."""
    card = _multi_strike_play_card()
    card["option_legs"][1]["side"] = "sell"
    assert _is_multi_strike(card) is False


def test_execute_multi_strike_returns_list_of_order_ids(make_executor):
    """execute() with 2 BUY legs returns a list of 2 alpaca_order_ids."""
    fake = _FakeAlpacaClient(
        submit_order_results=[
            _broker_response(
                order_id="ms-leg-0",
                symbol="AXSM260620C00120000",
                qty=1,
            ),
            _broker_response(
                order_id="ms-leg-1",
                symbol="AXSM260620C00130000",
                qty=1,
            ),
        ]
    )
    executor, _ = make_executor(client=fake)

    result = executor.execute(_multi_strike_play_card())

    assert isinstance(result, list)
    assert result == ["ms-leg-0", "ms-leg-1"]
    assert len(fake.submit_calls) == 2


def test_execute_multi_strike_persists_two_rows_same_play_id(
    make_executor, db_path: Path
):
    """Both legs persist orders rows tagged with the same play_card_id."""
    fake = _FakeAlpacaClient(
        submit_order_results=[
            _broker_response(
                order_id="ms-leg-0",
                symbol="AXSM260620C00120000",
                qty=1,
            ),
            _broker_response(
                order_id="ms-leg-1",
                symbol="AXSM260620C00130000",
                qty=1,
            ),
        ]
    )
    executor, _ = make_executor(client=fake)
    play_card_id = "AXSM-multistrike-pin-row-test"

    executor.execute(
        _multi_strike_play_card(play_card_id=play_card_id)
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT alpaca_order_id, symbol, qty, status, play_card_id "
            "FROM paper_orders WHERE play_card_id = ? ORDER BY created_at ASC",
            (play_card_id,),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 2
    # Both rows tagged with the same play_card_id (the join key for
    # downstream validators / rotation engine).
    assert {row["play_card_id"] for row in rows} == {play_card_id}
    assert [row["alpaca_order_id"] for row in rows] == [
        "ms-leg-0",
        "ms-leg-1",
    ]
    assert {row["symbol"] for row in rows} == {
        "AXSM260620C00120000",
        "AXSM260620C00130000",
    }
    assert all(row["status"] == "accepted" for row in rows)


def test_execute_multi_strike_combined_notional_within_cap(make_executor):
    """f-m3-08 + AGENTS.md: combined notional ≤ risk_per_play_usd cap.

    With the per-play cap split evenly across the 2 legs, neither
    leg's notional (qty * limit_price * 100) exceeds half the cap,
    and therefore the combined notional respects the original cap.
    """
    fake = _FakeAlpacaClient(
        submit_order_results=[
            _broker_response(
                order_id="ms-leg-0",
                symbol="AXSM260620C00120000",
                qty=1,
            ),
            _broker_response(
                order_id="ms-leg-1",
                symbol="AXSM260620C00130000",
                qty=1,
            ),
        ]
    )
    executor, _ = make_executor(client=fake)

    cap = _config.get_risk_per_play_usd()
    result = executor.execute(_multi_strike_play_card())
    assert isinstance(result, list)

    # Reconstruct the per-leg notional from the play card legs and
    # the qty PaperExecutor saw on each broker request.
    per_leg_notional: list[float] = []
    for req in fake.submit_calls:
        # alpaca-py request models expose ``qty`` and ``limit_price``.
        qty = int(getattr(req, "qty"))
        limit = float(getattr(req, "limit_price"))
        per_leg_notional.append(qty * limit * 100)

    combined = sum(per_leg_notional)
    assert combined <= cap, (
        f"combined notional ${combined:.2f} exceeds per-play cap ${cap}"
    )
    # And — equally important — neither leg alone exceeds half the cap.
    half_cap = cap / 2.0
    for n in per_leg_notional:
        assert n <= half_cap + 1.0, (
            f"per-leg notional ${n:.2f} > half-cap ${half_cap}"
        )


def test_execute_single_strike_default_returns_str(make_executor, db_path: Path):
    """A 1-leg play card returns a single str order id (back-compat)."""
    fake = _FakeAlpacaClient(
        submit_order_results=[
            _broker_response(
                order_id="ss-only",
                symbol="AXSM260620C00125000",
                qty=1,
            )
        ]
    )
    executor, _ = make_executor(client=fake)

    result = executor.execute(_single_strike_play_card())

    assert isinstance(result, str)
    assert result == "ss-only"
    assert len(fake.submit_calls) == 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT alpaca_order_id FROM paper_orders WHERE play_card_id = ?",
            ("AXSM-single-2026-04-27",),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["alpaca_order_id"] == "ss-only"


def test_execute_unfillable_2_legs_demoted_to_single_strike_raises(make_executor):
    """liquidity_classification='unfillable' on a 2-leg card forces the
    single-leg validator path → UnsupportedOrderShape (>1 leg).

    This pins the f-m3-12 hook: when the upstream liquidity probe
    decides a multi-strike entry is not realistic, the executor
    refuses the multi-strike dispatch. The play-card builder is then
    expected to either trim to 1 leg (which would succeed) or
    abandon the entry entirely.
    """
    fake = _FakeAlpacaClient()
    executor, _ = make_executor(client=fake)

    card = _multi_strike_play_card(liquidity_classification="unfillable")
    with pytest.raises(UnsupportedOrderShape, match="multi-leg"):
        executor.execute(card)

    # No broker calls were made.
    assert fake.submit_calls == []
