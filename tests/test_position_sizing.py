"""Tests for position sizing + concurrency / deployed caps (f-m3-04).

Hermetic — no network. Fake :class:`AlpacaClient` doubles supply
``get_positions``, ``submit_order``, and ``get_order`` so the
:class:`PaperExecutor` paths covering VAL-M3-019 through VAL-M3-025
exercise without touching the broker.

Validation contract assertions exercised:

* VAL-M3-019 — ``size_position(play_card)`` with mid=$1.20 returns
  qty=2 (``floor(250 / 120) == 2``).
* VAL-M3-020 — ``mid = (bid + ask) / 2``; bid=ask=0 raises
  :class:`MissingQuoteData`.
* VAL-M3-021 — ``mid > $2.50`` (i.e. mid * 100 > $250) returns ``0``
  from ``size_position`` and ``execute`` raises
  :class:`ContractTooExpensive`.
* VAL-M3-022 — ``state/calibration_params.json`` ``risk_per_play_usd``
  override is honoured (175 → qty=1 on a $1.20 mid).
* VAL-M3-023 — ``len(get_positions()) >= MAX_CONCURRENT_PLAYS``
  raises :class:`ConcurrencyCapExceeded`.
* VAL-M3-024 — ``deployed + planned > MAX_DEPLOYED_USD`` raises
  :class:`DeployedCapExceeded`; ``deployed + planned <= cap``
  proceeds.
* VAL-M3-025 — caps come from ``config``; the executor body has no
  hardcoded ``3`` / ``750`` literals (verified by an in-file grep
  test below).
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper import config as biotech_config
from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.paper_executor import (
    ConcurrencyCapExceeded,
    ContractTooExpensive,
    DeployedCapExceeded,
    MissingQuoteData,
    PaperExecutor,
    UnsupportedOrderShape,
    size_position,
)


# ---------------------------------------------------------------------------
# Fake Alpaca client (mirror of the helper used in test_paper_executor.py).
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Duck-typed substitute exposing the methods PaperExecutor invokes."""

    def __init__(
        self,
        *,
        base_url: str = PAPER_BASE_URL,
        positions: list[dict[str, Any]] | None = None,
        submit_order_result: dict[str, Any] | None = None,
        submit_order_error: Exception | None = None,
    ) -> None:
        self.base_url = base_url
        self._positions = list(positions or [])
        self._submit_result = submit_order_result or {
            "id": "order-id-fixture",
            "symbol": "AXSM250620C00125000",
            "side": "buy",
            "qty": 2,
            "status": "accepted",
        }
        self._submit_error = submit_order_error
        self.submit_calls: list[Any] = []
        self.get_positions_calls: int = 0

    def get_positions(self) -> list[dict[str, Any]]:
        self.get_positions_calls += 1
        return list(self._positions)

    def submit_order(self, order_request: Any) -> dict[str, Any]:
        self.submit_calls.append(order_request)
        if self._submit_error is not None:
            raise self._submit_error
        # Echo the requested qty so caller-side assertions can read it.
        result = dict(self._submit_result)
        result.setdefault("symbol", getattr(order_request, "symbol", None))
        if hasattr(order_request, "qty"):
            result["qty"] = order_request.qty
        return result

    def get_order(self, order_id: str) -> dict[str, Any]:  # pragma: no cover
        return self._submit_result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "alpha_sniper.db"


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch) -> Path:
    """Redirect :data:`biotech_sniper.paths.STATE_DIR` to a tmp dir.

    ``config.get_risk_per_play_usd`` reads
    ``state/calibration_params.json`` on each call. Tests that exercise
    the override write into the redirected path so production state is
    untouched.
    """
    target = tmp_path / "state"
    target.mkdir(parents=True, exist_ok=True)
    from biotech_sniper import paths as paths_module

    monkeypatch.setattr(paths_module, "STATE_DIR", target)
    return target


def _play_card(
    *,
    bid: float | None = 1.00,
    ask: float | None = 1.40,
    qty: int | None = None,
    play_card_id: str = "AXSM-2025-04-27",
) -> dict[str, Any]:
    leg: dict[str, Any] = {
        "symbol": "AXSM250620C00125000",
        "side": "buy",
        "limit_price": 1.50,
        "option_type": "call",
    }
    if bid is not None:
        leg["bid"] = bid
    if ask is not None:
        leg["ask"] = ask
    if qty is not None:
        leg["qty"] = qty
    return {
        "play_card_id": play_card_id,
        "ticker": "AXSM",
        "option_legs": [leg],
    }


# ---------------------------------------------------------------------------
# VAL-M3-019: $250 default per play.
# ---------------------------------------------------------------------------


def test_size_position_mid_120_returns_two_contracts():
    """mid=$1.20 → 100*1.20=120 per contract → floor(250/120) == 2."""
    play = _play_card(bid=1.00, ask=1.40)
    assert size_position(play) == 2


def test_size_position_mid_120_dollar_value_under_cap():
    """$240 (2 * $120) is the deployed dollar value, under the $250 cap."""
    qty = size_position(_play_card(bid=1.00, ask=1.40))
    mid = (1.00 + 1.40) / 2.0
    assert qty * mid * 100 <= biotech_config.RISK_PER_PLAY_USD


def test_size_position_default_uses_config_risk_per_play_usd():
    """Default cap is sourced from config.RISK_PER_PLAY_USD (== 250)."""
    assert biotech_config.RISK_PER_PLAY_USD == 250


# ---------------------------------------------------------------------------
# VAL-M3-020: mid = (bid+ask)/2; missing data → MissingQuoteData.
# ---------------------------------------------------------------------------


def test_size_position_mid_is_average_of_bid_and_ask():
    """Asymmetric quote: bid=0.80 ask=1.60 → mid=1.20 → qty=2."""
    play = _play_card(bid=0.80, ask=1.60)
    assert size_position(play) == 2


def test_size_position_raises_missing_quote_data_when_zero_zero():
    play = _play_card(bid=0, ask=0)
    with pytest.raises(MissingQuoteData):
        size_position(play)


def test_size_position_raises_missing_quote_data_when_none_none():
    play = _play_card(bid=None, ask=None)
    with pytest.raises(MissingQuoteData):
        size_position(play)


# ---------------------------------------------------------------------------
# VAL-M3-021: mid > cap → returns 0; execute raises ContractTooExpensive.
# ---------------------------------------------------------------------------


def test_size_position_returns_zero_when_mid_exceeds_cap():
    """mid=$3.50 → $350 per contract > $250 cap → qty == 0."""
    play = _play_card(bid=3.40, ask=3.60)
    assert size_position(play) == 0


def test_execute_raises_contract_too_expensive_on_zero_size(
    db_path: Path,
):
    fake = _FakeAlpacaClient()
    executor = PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]
    play = _play_card(bid=3.40, ask=3.60)
    with pytest.raises(ContractTooExpensive):
        executor.execute(play)
    # Defensive: no order was sent to the broker.
    assert fake.submit_calls == []


def test_contract_too_expensive_persists_rejection_row(db_path: Path):
    fake = _FakeAlpacaClient()
    executor = PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]
    with pytest.raises(ContractTooExpensive):
        executor.execute(_play_card(bid=3.40, ask=3.60))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT status, reason FROM orders WHERE play_card_id = ?",
            ("AXSM-2025-04-27",),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["status"] == "rejected"
    assert "ContractTooExpensive" in (rows[0]["reason"] or "")


# ---------------------------------------------------------------------------
# VAL-M3-022: calibration_params.json override.
# ---------------------------------------------------------------------------


def test_calibration_override_175_produces_one_contract(state_dir: Path):
    """risk_per_play_usd=175 on a $1.20 mid → floor(175/120) == 1."""
    (state_dir / "calibration_params.json").write_text(
        json.dumps({"risk_per_play_usd": 175}), encoding="utf-8"
    )
    play = _play_card(bid=1.00, ask=1.40)
    assert size_position(play) == 1


def test_missing_calibration_file_falls_back_to_default(state_dir: Path):
    # No file written; STATE_DIR is the tmp path → default applies.
    play = _play_card(bid=1.00, ask=1.40)
    assert size_position(play) == 2


def test_unparseable_calibration_file_falls_back_with_warning(
    state_dir: Path, caplog
):
    (state_dir / "calibration_params.json").write_text(
        "{not json", encoding="utf-8"
    )
    caplog.set_level("WARNING", logger="biotech_sniper.config")
    play = _play_card(bid=1.00, ask=1.40)
    assert size_position(play) == 2
    assert any(
        "calibration_params.json" in r.getMessage() for r in caplog.records
    )


def test_explicit_risk_per_play_usd_override_argument():
    """Caller can pass risk_per_play_usd=… directly to short-circuit config."""
    play = _play_card(bid=1.00, ask=1.40)
    assert size_position(play, risk_per_play_usd=175) == 1
    assert size_position(play, risk_per_play_usd=500) == 4


# ---------------------------------------------------------------------------
# VAL-M3-023: Concurrency cap (3 active → 4th blocked).
# ---------------------------------------------------------------------------


def test_three_active_positions_block_fourth_submission(db_path: Path):
    positions = [
        {"symbol": "X1", "qty": 1, "avg_entry_price": 1.0},
        {"symbol": "X2", "qty": 1, "avg_entry_price": 1.0},
        {"symbol": "X3", "qty": 1, "avg_entry_price": 1.0},
    ]
    fake = _FakeAlpacaClient(positions=positions)
    executor = PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]
    with pytest.raises(ConcurrencyCapExceeded):
        executor.execute(_play_card(bid=1.00, ask=1.40))
    assert fake.submit_calls == []


def test_two_active_positions_allow_third_submission(db_path: Path):
    positions = [
        {"symbol": "X1", "qty": 1, "avg_entry_price": 1.0},
        {"symbol": "X2", "qty": 1, "avg_entry_price": 1.0},
    ]
    fake = _FakeAlpacaClient(positions=positions)
    executor = PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]
    order_id = executor.execute(_play_card(bid=1.00, ask=1.40))
    assert order_id == "order-id-fixture"
    assert len(fake.submit_calls) == 1


def test_concurrency_cap_persists_rejection_row(db_path: Path):
    fake = _FakeAlpacaClient(
        positions=[
            {"symbol": "X1", "qty": 1, "avg_entry_price": 1.0},
            {"symbol": "X2", "qty": 1, "avg_entry_price": 1.0},
            {"symbol": "X3", "qty": 1, "avg_entry_price": 1.0},
        ]
    )
    executor = PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]
    with pytest.raises(ConcurrencyCapExceeded):
        executor.execute(_play_card(bid=1.00, ask=1.40))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status, reason FROM orders WHERE play_card_id = ?",
            ("AXSM-2025-04-27",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["status"] == "rejected"
    assert "ConcurrencyCapExceeded" in (row["reason"] or "")


# ---------------------------------------------------------------------------
# VAL-M3-024: Deployed-capital cap.
# ---------------------------------------------------------------------------


def _positions_totaling_620() -> list[dict[str, Any]]:
    """Return two synthetic positions whose deployed capital sums to $620."""
    # 2 contracts at $2.10 mid + 2 contracts at $1.00 mid = $420 + $200 = $620.
    return [
        {"symbol": "Y1", "qty": 2, "avg_entry_price": 2.10},
        {"symbol": "Y2", "qty": 2, "avg_entry_price": 1.00},
    ]


def test_deployed_cap_blocks_when_planned_pushes_over_cap(db_path: Path):
    """$620 deployed + $200 planned = $820 > $750 → DeployedCapExceeded."""
    positions = _positions_totaling_620()
    fake = _FakeAlpacaClient(positions=positions)
    executor = PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]
    # mid=$1.00 (bid 0.80, ask 1.20) on $250 cap → qty=2 → $200 planned.
    play = _play_card(bid=0.80, ask=1.20)
    with pytest.raises(DeployedCapExceeded):
        executor.execute(play)
    assert fake.submit_calls == []


def test_deployed_cap_proceeds_when_within_cap(db_path: Path):
    """$620 deployed + $100 planned = $720 ≤ $750 → submission proceeds."""
    positions = _positions_totaling_620()
    fake = _FakeAlpacaClient(positions=positions)
    executor = PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]
    # mid=$0.50 → cost per contract $50 → qty=floor(250/50)=5 → $250 planned.
    # Need a planned cost of exactly $100. Override via explicit qty path:
    # use limit_price-only entry with explicit qty=2 and no chain quote so
    # _planned_cost_usd uses limit_price ($0.50) → $100.
    play = {
        "play_card_id": "AXSM-2025-04-27",
        "ticker": "AXSM",
        "option_legs": [
            {
                "symbol": "AXSM250620C00125000",
                "side": "buy",
                "qty": 2,
                "limit_price": 0.50,
                "option_type": "call",
            }
        ],
    }
    order_id = executor.execute(play)
    assert order_id == "order-id-fixture"


def test_deployed_capital_helper_accuracy(db_path: Path):
    """The internal cap calculation matches qty * avg * 100 over positions."""
    from biotech_sniper.paper_executor import _deployed_capital_usd

    positions = _positions_totaling_620()
    assert _deployed_capital_usd(positions) == pytest.approx(620.0)


def test_deployed_cap_persists_rejection_row(db_path: Path):
    fake = _FakeAlpacaClient(positions=_positions_totaling_620())
    executor = PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]
    with pytest.raises(DeployedCapExceeded):
        executor.execute(_play_card(bid=0.80, ask=1.20))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status, reason FROM orders WHERE play_card_id = ?",
            ("AXSM-2025-04-27",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["status"] == "rejected"
    assert "DeployedCapExceeded" in (row["reason"] or "")


# ---------------------------------------------------------------------------
# VAL-M3-025: caps sourced from config (no hardcoded literals in executor).
# ---------------------------------------------------------------------------


def test_max_concurrent_plays_constant_value():
    assert biotech_config.MAX_CONCURRENT_PLAYS == 3


def test_max_deployed_usd_constant_value():
    assert biotech_config.MAX_DEPLOYED_USD == 750


def test_paper_executor_has_no_hardcoded_cap_literals():
    """Greppable invariant from the f-m3-04 verification step."""
    text = (
        Path(__file__)
        .resolve()
        .parent.parent
        / "biotech_sniper"
        / "paper_executor.py"
    ).read_text(encoding="utf-8")
    pattern = re.compile(r"(== ?3|>= ?3|== ?750|>= ?750)")
    matches = pattern.findall(text)
    assert matches == [], (
        f"paper_executor.py has hardcoded cap literals: {matches}. "
        "Caps must come from config.MAX_CONCURRENT_PLAYS / "
        "config.MAX_DEPLOYED_USD."
    )


def test_caps_are_referenced_via_config_module(db_path: Path):
    """Live monkey-patch: changing config caps changes executor behaviour."""
    fake = _FakeAlpacaClient(
        positions=[{"symbol": "Z", "qty": 1, "avg_entry_price": 1.0}]
    )
    executor = PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]
    # Lower the concurrency cap to 1; with 1 active position, the next
    # submission must trip ConcurrencyCapExceeded.
    import biotech_sniper.config as cfg

    original_cap = cfg.MAX_CONCURRENT_PLAYS
    try:
        cfg.MAX_CONCURRENT_PLAYS = 1  # type: ignore[misc]
        with pytest.raises(ConcurrencyCapExceeded):
            executor.execute(_play_card(bid=1.00, ask=1.40))
    finally:
        cfg.MAX_CONCURRENT_PLAYS = original_cap  # type: ignore[misc]
