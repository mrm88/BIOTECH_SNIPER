"""Tests for ``biotech_sniper.execution_fills`` (f-m3-11).

Validation contract coverage:

* **VAL-M3-059** — ``execution_fills`` schema (table exists with the
  expected columns and FK back to ``paper_orders``).
* **VAL-M3-063** — every fill writes an ``execution_fills`` row with
  side-aware ``slippage_bps``:

      slippage_bps = ((filled - mid) / mid) * 10000 * (+1 buy / -1 sell)

  positive ⇒ worse-than-mid for the actor.

Tests are hermetic. The fill writer is exercised end-to-end (parent
``paper_orders`` row + downstream ``execution_events`` propagation)
so the integration with the subscriber's monotonic validator is
covered too.
"""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from typing import Optional

import pytest

from biotech_sniper import db as db_module
from biotech_sniper.execution_fills import (
    FillContextMissing,
    compute_slippage_bps,
    compute_slippage_usd,
    compute_time_to_fill_ms,
    record_fill,
    record_fill_event,
)
from biotech_sniper.execution_subscriber import record_execution_event


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "alpha_sniper.db"
    conn = db_module.connect(p)
    try:
        db_module.run_migrations(conn)
    finally:
        conn.close()
    return p


def _insert_paper_order(
    db_path: Path,
    *,
    paper_order_id: str = "po-1",
    side: str = "buy",
    qty: int = 2,
    requested_mid_at_submit: Optional[float] = 1.50,
    created_at: str = "2026-04-27T15:30:00.000Z",
    client_order_id: str = "client-1",
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO paper_orders (
                id, play_card_id, alpaca_order_id, symbol, side, qty,
                status, client_order_id, created_at,
                requested_mid_at_submit
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                paper_order_id,
                "PC-1",
                "alp-1",
                "AXSM250620C00125000",
                side,
                qty,
                "submitted",
                client_order_id,
                created_at,
                requested_mid_at_submit,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pure compute (VAL-M3-063 maths)
# ---------------------------------------------------------------------------


def test_compute_slippage_bps_buy_above_mid_is_positive() -> None:
    """Buy filled ABOVE mid pays MORE → positive bps."""
    bps = compute_slippage_bps(filled_price=1.52, requested_mid_at_submit=1.50, side="buy")
    # ((1.52 - 1.50)/1.50)*10000 = 133.333...
    assert bps == pytest.approx(133.3333333, rel=1e-6)


def test_compute_slippage_bps_sell_below_mid_is_positive() -> None:
    """Sell filled BELOW mid receives LESS → positive bps."""
    bps = compute_slippage_bps(filled_price=1.48, requested_mid_at_submit=1.50, side="sell")
    # raw = ((1.48-1.50)/1.50)*10000 = -133.333; sell sign flips → +133.333
    assert bps == pytest.approx(133.3333333, rel=1e-6)


def test_compute_slippage_bps_buy_below_mid_is_negative() -> None:
    """Buy filled BELOW mid is price improvement → negative bps."""
    bps = compute_slippage_bps(filled_price=1.48, requested_mid_at_submit=1.50, side="buy")
    assert bps == pytest.approx(-133.3333333, rel=1e-6)


def test_compute_slippage_bps_rejects_non_positive_mid() -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        compute_slippage_bps(filled_price=1.50, requested_mid_at_submit=0.0, side="buy")


def test_compute_slippage_bps_rejects_unknown_side() -> None:
    with pytest.raises(ValueError, match="buy"):
        compute_slippage_bps(
            filled_price=1.52, requested_mid_at_submit=1.50, side="hold"
        )


def test_compute_slippage_usd_buy_uses_100x_contract_multiplier() -> None:
    """USD slippage scales by 100 (the standard contract multiplier)."""
    usd = compute_slippage_usd(
        filled_price=1.52, requested_mid_at_submit=1.50, filled_qty=2, side="buy"
    )
    # (1.52 - 1.50) * 2 * 100 = 4.00
    assert usd == pytest.approx(4.0, rel=1e-9)


def test_compute_time_to_fill_ms_clamps_negative_drift_to_zero() -> None:
    """Broker clock drift never produces a negative latency."""
    ms = compute_time_to_fill_ms(
        submitted_at="2026-04-27T15:30:05.000Z",
        filled_at="2026-04-27T15:30:00.000Z",  # earlier!
    )
    assert ms == 0


# ---------------------------------------------------------------------------
# DB writers (VAL-M3-059 + VAL-M3-063 end-to-end)
# ---------------------------------------------------------------------------


def test_execution_fills_schema_has_all_required_columns(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(execution_fills)"
        ).fetchall()}
    finally:
        conn.close()
    expected = {
        "id",
        "paper_order_id",
        "filled_at",
        "filled_price",
        "filled_qty",
        "requested_mid_at_submit",
        "slippage_bps",
        "slippage_usd",
        "time_to_fill_ms",
        "partial_qty_remaining",
    }
    assert expected <= cols, f"missing columns: {expected - cols}"


def test_record_fill_writes_row_with_correct_slippage(db_path: Path) -> None:
    """``record_fill`` orchestrates parent lookup + slippage compute + insert."""
    _insert_paper_order(
        db_path, side="buy", requested_mid_at_submit=1.50,
        created_at="2026-04-27T15:30:00.000Z",
    )
    # Pre-record submitted+accepted so the legal-transition validator
    # accepts the downstream filled event from record_fill.
    record_execution_event(
        db_path, paper_order_id="po-1", event_type="submitted",
        event_at="2026-04-27T15:30:00.500Z",
    )
    record_execution_event(
        db_path, paper_order_id="po-1", event_type="accepted",
        event_at="2026-04-27T15:30:01.000Z",
    )
    fill_id = record_fill(
        db_path,
        paper_order_id="po-1",
        filled_price=1.52,
        filled_qty=2,
        filled_at="2026-04-27T15:30:05.000Z",
    )
    assert fill_id > 0
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM execution_fills WHERE id = ?", (fill_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["paper_order_id"] == "po-1"
    assert row["filled_price"] == pytest.approx(1.52)
    assert row["filled_qty"] == 2
    assert row["requested_mid_at_submit"] == pytest.approx(1.50)
    # Recompute and assert within 1e-6 (validator-style).
    expected_bps = ((1.52 - 1.50) / 1.50) * 10000.0
    assert math.isclose(row["slippage_bps"], expected_bps, rel_tol=0, abs_tol=1e-6)
    assert math.isclose(row["slippage_usd"], 4.0, rel_tol=0, abs_tol=1e-6)
    assert row["time_to_fill_ms"] == 5000  # 15:30:00 → 15:30:05
    assert row["partial_qty_remaining"] == 0


def test_record_fill_partial_writes_partial_qty_remaining_and_event(
    db_path: Path,
) -> None:
    """Partial fills write ``partial_fill`` events and the remaining qty."""
    _insert_paper_order(
        db_path, side="buy", requested_mid_at_submit=1.50, qty=4,
        created_at="2026-04-27T15:30:00.000Z",
    )
    record_execution_event(
        db_path, paper_order_id="po-1", event_type="submitted",
        event_at="2026-04-27T15:30:00.500Z",
    )
    record_execution_event(
        db_path, paper_order_id="po-1", event_type="accepted",
        event_at="2026-04-27T15:30:01.000Z",
    )
    record_fill(
        db_path,
        paper_order_id="po-1",
        filled_price=1.51,
        filled_qty=2,
        filled_at="2026-04-27T15:30:03.000Z",
        partial_qty_remaining=2,
    )
    conn = sqlite3.connect(db_path)
    try:
        partial_remaining = conn.execute(
            "SELECT partial_qty_remaining FROM execution_fills "
            "WHERE paper_order_id = ?",
            ("po-1",),
        ).fetchone()[0]
        events = [
            r[0] for r in conn.execute(
                "SELECT event_type FROM execution_events "
                "WHERE paper_order_id = ? ORDER BY event_at ASC",
                ("po-1",),
            ).fetchall()
        ]
    finally:
        conn.close()
    assert partial_remaining == 2
    assert events[-1] == "partial_fill"


def test_record_fill_raises_when_parent_row_missing(db_path: Path) -> None:
    """``FillContextMissing`` surfaces when the parent paper_orders row is gone."""
    with pytest.raises(FillContextMissing, match="missing"):
        record_fill(
            db_path,
            paper_order_id="ghost-1",
            filled_price=1.50,
            filled_qty=1,
            filled_at="2026-04-27T15:30:05.000Z",
        )


def test_record_fill_raises_when_parent_has_null_mid(db_path: Path) -> None:
    """Without a submit-time mid, slippage cannot be computed."""
    _insert_paper_order(
        db_path, requested_mid_at_submit=None,
    )
    with pytest.raises(FillContextMissing, match="requested_mid_at_submit"):
        record_fill(
            db_path,
            paper_order_id="po-1",
            filled_price=1.52,
            filled_qty=1,
            filled_at="2026-04-27T15:30:05.000Z",
        )


def test_record_fill_event_low_level_writer_round_trip(db_path: Path) -> None:
    """The low-level writer produces a row matching every input field."""
    _insert_paper_order(db_path)
    fill_id = record_fill_event(
        db_path,
        paper_order_id="po-1",
        filled_at="2026-04-27T15:30:05.000Z",
        filled_price=1.55,
        filled_qty=1,
        requested_mid_at_submit=1.50,
        slippage_bps=333.3333333,
        slippage_usd=5.0,
        time_to_fill_ms=5000,
        partial_qty_remaining=0,
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM execution_fills WHERE id = ?", (fill_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["filled_price"] == pytest.approx(1.55)
    assert row["slippage_bps"] == pytest.approx(333.3333333, rel=1e-6)
    assert row["slippage_usd"] == pytest.approx(5.0)
    assert row["time_to_fill_ms"] == 5000
    assert row["partial_qty_remaining"] == 0
