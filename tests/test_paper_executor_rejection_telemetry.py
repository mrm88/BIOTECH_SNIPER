"""Regression tests for f-cross-04 rejection telemetry (VAL-CROSS-043).

The user-testing-validator-cross-final round 1 found two
``paper_orders`` rows on the production VPS with ``status='rejected'``
that had ZERO corresponding ``execution_events`` rows
(``auto-6910da3e*`` and ``auto-26d885fa*``, both PFE260501C00027xxx,
both from 2026-04-27). Root cause: the rejection paths in
:mod:`biotech_sniper.paper_executor` updated ``paper_orders`` but did
NOT emit the matching ``execution_events`` row — only the SUCCESSFUL
submit path did so. Pre-existing rejections were thus orphaned in the
telemetry stream.

This module asserts the post-fix invariant: every
``paper_orders.status='rejected'`` row written by the executor MUST
have at least one ``execution_events`` row with
``event_type='rejected'`` for that ``paper_order_id``.

Three regression cases (matching the f-cross-04 spec):

1. Broker raises :class:`AlpacaClientError` during submit — the
   ``paper_orders`` row is updated to status='rejected' AND a
   matching ``execution_events`` row exists.
2. Broker returns a response missing the ``id`` field — the
   ``paper_orders`` row reflects the rejection AND a matching
   ``execution_events`` row exists.
3. Telemetry write itself raises (mock
   ``record_execution_event`` to raise) — the
   :class:`OrderRejected` exception still propagates and the
   underlying paper_orders row is intact (telemetry is best-effort).

The tests also cover the pre-submit cap-exceeded and
ContractTooExpensive paths to round out the regression matrix.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper.alpaca_client import (
    AlpacaClientError,
    PAPER_BASE_URL,
)
from biotech_sniper.paper_executor import (
    ConcurrencyCapExceeded,
    ContractTooExpensive,
    DeployedCapExceeded,
    OrderRejected,
    PaperExecutor,
)


# ---------------------------------------------------------------------------
# Local fake AlpacaClient — mirrors the double in tests/test_paper_executor.py
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Duck-typed AlpacaClient stub that returns canned submit/poll data."""

    def __init__(
        self,
        *,
        base_url: str = PAPER_BASE_URL,
        submit_order_results: list[dict[str, Any]] | None = None,
        submit_order_error: Exception | None = None,
        positions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = base_url
        self._submit_results = list(submit_order_results or [])
        self._submit_error = submit_order_error
        self._positions = list(positions or [])

    def get_positions(self) -> list[dict[str, Any]]:
        return list(self._positions)

    def submit_order(self, order_request: Any) -> dict[str, Any]:
        if self._submit_error is not None:
            raise self._submit_error
        if not self._submit_results:
            raise AssertionError(
                "submit_order called with no result queued"
            )
        return self._submit_results.pop(0)

    def get_order(self, order_id: str) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("get_order not expected in rejection tests")


def _call_play_card() -> dict[str, Any]:
    """Minimal valid single-leg long-call play card."""
    return {
        "play_card_id": "AXSM-2026-04-28",
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
                "client_order_id": "biotech-sniper-AXSM-call-2026-04-28",
            }
        ],
    }


def _quoted_play_card(*, bid: float, ask: float) -> dict[str, Any]:
    """Single-leg play card with bid/ask quotes (no explicit qty).

    Used by the ContractTooExpensive regression: the executor sees
    a quote without an explicit ``qty`` and runs ``size_position``
    which derives the contract count from the cap. A wide quote
    will yield ``qty=0`` and trip the rejection.
    """
    return {
        "play_card_id": "AXSM-2026-04-28-cte",
        "ticker": "AXSM",
        "option_legs": [
            {
                "symbol": "AXSM250620C00125000",
                "side": "buy",
                "bid": bid,
                "ask": ask,
                "option_type": "call",
                "client_order_id": "biotech-sniper-AXSM-cte-2026-04-28",
            }
        ],
    }


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Fresh SQLite db per test."""
    return tmp_path / "alpha_sniper.db"


@pytest.fixture
def executor(db_path: Path) -> PaperExecutor:
    """Default paper executor with no submit results queued."""
    fake = _FakeAlpacaClient()
    return PaperExecutor(fake, db_path=db_path, poll_interval_seconds=0.0)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _select_rejected_paper_orders(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT id, status, reason, client_order_id "
            "FROM paper_orders WHERE status='rejected'"
        ).fetchall()
    finally:
        conn.close()


def _select_rejected_events_for(
    db_path: Path, paper_order_id: str
) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT id, paper_order_id, event_type, event_at, raw_payload "
            "FROM execution_events "
            "WHERE paper_order_id = ? AND event_type = 'rejected'",
            (paper_order_id,),
        ).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Case 1: AlpacaClientError during submit → rejection telemetry written
# ---------------------------------------------------------------------------


def test_alpaca_apierror_writes_paper_orders_and_execution_events(
    db_path: Path,
):
    """AlpacaClientError on submit results in matched paper_orders + events.

    Before f-cross-04: paper_orders had status='rejected' but
    execution_events was empty (orphan). After f-cross-04: both
    rows exist.
    """
    fake = _FakeAlpacaClient(
        submit_order_error=AlpacaClientError(
            "Alpaca API error: invalid symbol AXSM250620C00125000"
        )
    )
    exe = PaperExecutor(fake, db_path=db_path, poll_interval_seconds=0.0)  # type: ignore[arg-type]

    with pytest.raises(OrderRejected, match="invalid symbol"):
        exe.execute(_call_play_card())

    rejected_orders = _select_rejected_paper_orders(db_path)
    assert len(rejected_orders) == 1
    paper_order_id = rejected_orders[0]["id"]
    assert "invalid symbol" in (rejected_orders[0]["reason"] or "")

    events = _select_rejected_events_for(db_path, paper_order_id)
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "rejected"
    assert event["paper_order_id"] == paper_order_id
    payload = json.loads(event["raw_payload"]) if event["raw_payload"] else {}
    assert "invalid symbol" in payload.get("reason", "")


# ---------------------------------------------------------------------------
# Case 2: broker response missing 'id' → rejection telemetry written
# ---------------------------------------------------------------------------


def test_missing_broker_order_id_writes_paper_orders_and_execution_events(
    db_path: Path,
):
    """A successful HTTP response that lacks ``id`` is treated as a rejection.

    The post-submit code path at ~line 1589 calls
    ``_update_order_after_submit(status='rejected')`` with reason
    ``'broker response missing order id'``. The execution_events
    row must mirror this.
    """
    fake = _FakeAlpacaClient(
        submit_order_results=[
            {
                # No "id" field — defensive guard in execute() should
                # surface this as OrderRejected.
                "status": "accepted",
                "qty": 2,
                "symbol": "AXSM250620C00125000",
            }
        ]
    )
    exe = PaperExecutor(fake, db_path=db_path, poll_interval_seconds=0.0)  # type: ignore[arg-type]

    with pytest.raises(OrderRejected, match="missing order id"):
        exe.execute(_call_play_card())

    rejected_orders = _select_rejected_paper_orders(db_path)
    assert len(rejected_orders) == 1
    paper_order_id = rejected_orders[0]["id"]
    assert (rejected_orders[0]["reason"] or "").lower().startswith(
        "broker response missing order id"
    )

    events = _select_rejected_events_for(db_path, paper_order_id)
    assert len(events) == 1
    payload = json.loads(events[0]["raw_payload"]) if events[0]["raw_payload"] else {}
    assert "missing order id" in payload.get("reason", "")


# ---------------------------------------------------------------------------
# Case 3: telemetry write itself raises → OrderRejected still propagates
# ---------------------------------------------------------------------------


def test_telemetry_failure_does_not_break_rejection_path(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Mock record_execution_event to raise; OrderRejected still surfaces.

    The f-cross-04 contract is explicit: telemetry writes are
    best-effort. A failed insert into ``execution_events`` MUST NOT
    swallow the original :class:`OrderRejected` (or hide the broker
    reason). The paper_orders row should still be written with
    status='rejected'.
    """
    import biotech_sniper.execution_subscriber as _subscriber

    def _exploding_record(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("simulated telemetry failure")

    monkeypatch.setattr(
        _subscriber, "record_execution_event", _exploding_record
    )

    fake = _FakeAlpacaClient(
        submit_order_error=AlpacaClientError("Alpaca API error: HTTP 503")
    )
    exe = PaperExecutor(fake, db_path=db_path, poll_interval_seconds=0.0)  # type: ignore[arg-type]

    with pytest.raises(OrderRejected, match="HTTP 503"):
        exe.execute(_call_play_card())

    # paper_orders still reflects the rejection — telemetry failure
    # does not roll back the persistence write.
    rejected_orders = _select_rejected_paper_orders(db_path)
    assert len(rejected_orders) == 1
    assert "HTTP 503" in (rejected_orders[0]["reason"] or "")


# ---------------------------------------------------------------------------
# Bonus regression: pre-submit caps also emit telemetry.
# ---------------------------------------------------------------------------


def test_concurrency_cap_exceeded_emits_telemetry(
    db_path: Path,
):
    """ConcurrencyCapExceeded path also writes a rejected execution_events row.

    Pre-submit rejections (cap exceeded, contract too expensive)
    are even harder to trace than broker rejections because no
    network call is made. The fix must cover them too.
    """
    # Force the cap by mocking get_positions to return >= 3 options
    # positions.
    fake_positions = [
        {"symbol": f"FOO{i}", "qty": "1", "avg_entry_price": "1.00",
         "asset_class": "us_option"}
        for i in range(5)
    ]
    fake = _FakeAlpacaClient(positions=fake_positions)
    exe = PaperExecutor(fake, db_path=db_path, poll_interval_seconds=0.0)  # type: ignore[arg-type]

    with pytest.raises(ConcurrencyCapExceeded):
        exe.execute(_call_play_card())

    rejected_orders = _select_rejected_paper_orders(db_path)
    assert len(rejected_orders) == 1
    paper_order_id = rejected_orders[0]["id"]
    events = _select_rejected_events_for(db_path, paper_order_id)
    assert len(events) == 1


def test_contract_too_expensive_emits_telemetry(
    db_path: Path,
):
    """ContractTooExpensive path also writes a rejected execution_events row.

    A wide bid/ask whose mid * 100 exceeds the per-play cap forces
    ``size_position`` to return 0; the executor persists a
    rejection row and raises :class:`ContractTooExpensive`.
    """
    fake = _FakeAlpacaClient()
    exe = PaperExecutor(fake, db_path=db_path, poll_interval_seconds=0.0)  # type: ignore[arg-type]

    # bid=$5, ask=$5 → mid=$5 → cost-per-contract = $500 > $250 cap.
    with pytest.raises(ContractTooExpensive):
        exe.execute(_quoted_play_card(bid=5.0, ask=5.0))

    rejected_orders = _select_rejected_paper_orders(db_path)
    assert len(rejected_orders) == 1
    paper_order_id = rejected_orders[0]["id"]
    events = _select_rejected_events_for(db_path, paper_order_id)
    assert len(events) == 1


# ---------------------------------------------------------------------------
# Backfill helper smoke test.
# ---------------------------------------------------------------------------


def test_backfill_helper_inserts_synthetic_rejected_events(
    db_path: Path,
):
    """Backfill scans for orphans and inserts one event per row.

    Simulates the production state on the VPS pre-fix: a rejected
    paper_orders row with no execution_events. The backfill helper
    must close the gap.
    """
    from biotech_sniper import db as db_module
    from biotech_sniper.migrations import (
        backfill_rejected_execution_events as backfill_mod,
    )

    # Initialise the schema.
    conn = db_module.connect(db_path)
    try:
        db_module.run_migrations(conn)
    finally:
        conn.close()

    # Insert an orphan row directly (mimics a pre-fix VPS row).
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO paper_orders (
                id, play_card_id, alpaca_order_id, symbol, side, qty,
                status, reason, client_order_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "auto-6910da3e",
                "PFE-2026-04-27",
                None,
                "PFE260501C00027000",
                "buy",
                2,
                "rejected",
                "ConcurrencyCapExceeded: 3 active positions",
                "auto-6910da3e",
                "2026-04-27T15:30:00.000Z",
            ),
        )
        # Add a legacy:* row that should NOT be backfilled.
        conn.execute(
            """
            INSERT INTO paper_orders (
                id, play_card_id, alpaca_order_id, symbol, side, qty,
                status, reason, client_order_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-1",
                None,
                None,
                "FOOBAR",
                "buy",
                1,
                "rejected",
                "old reason",
                "legacy:1",
                "2026-04-26T15:30:00.000Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # Dry run: counts orphans but does not write.
    summary_dry = backfill_mod.backfill(db_path, dry_run=True)
    assert summary_dry["rows_to_backfill"] == 1
    assert summary_dry["rows_backfilled"] == 0
    assert summary_dry["orphan_ids"] == ["auto-6910da3e"]

    # Real run: writes the synthetic event.
    summary = backfill_mod.backfill(db_path, dry_run=False)
    assert summary["rows_to_backfill"] == 1
    assert summary["rows_backfilled"] == 1

    # Verify the event landed with backfilled=True marker.
    events = _select_rejected_events_for(db_path, "auto-6910da3e")
    assert len(events) == 1
    payload = json.loads(events[0]["raw_payload"]) if events[0]["raw_payload"] else {}
    assert payload.get("backfilled") is True
    assert "ConcurrencyCapExceeded" in payload.get("reason", "")
    # event_at should match paper_orders.created_at exactly.
    assert events[0]["event_at"] == "2026-04-27T15:30:00.000Z"

    # Re-running is idempotent (no orphans left).
    summary_again = backfill_mod.backfill(db_path, dry_run=False)
    assert summary_again["rows_to_backfill"] == 0
    assert summary_again["rows_backfilled"] == 0

    # Legacy row never gets backfilled.
    legacy_events = _select_rejected_events_for(db_path, "legacy-1")
    assert legacy_events == []


def test_backfill_cli_dry_run_prints_count(
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """The CLI prints a JSON summary on dry-run."""
    from biotech_sniper import db as db_module
    from biotech_sniper.migrations import (
        backfill_rejected_execution_events as backfill_mod,
    )

    conn = db_module.connect(db_path)
    try:
        db_module.run_migrations(conn)
    finally:
        conn.close()

    rc = backfill_mod.main(["--db", str(db_path), "--dry-run"])
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["rows_to_backfill"] == 0
    assert data["rows_backfilled"] == 0
    assert data["dry_run"] is True
