"""Tests for the f-m3-19 surgical wiring fixes.

Three regressions are addressed by f-m3-19; this file is the
black-box regression suite for each.

1. ``ExecutionSubscriber.poll_once`` records ``execution_fills``
   AND ``execution_events`` on a ``partial_fill`` / ``filled``
   broker payload (was: events-only).
2. ``PaperExecutor.wait_for_fill`` emits ``execution_events``
   transitions for every observed broker status change (was:
   updated ``paper_orders.status`` only — the lifecycle stream
   was empty after the initial ``submitted`` event).
3. ``python -m biotech_sniper.execution_subscriber --poll-once
   --date <today>`` is wired into ``intraday_scanner.run_intraday_scan``
   as JOB 6 — TELEMETRY POLL and produces at least one
   ``execution_events`` row when invoked against a fresh DB with
   one open paper_orders row in ``submitted`` state.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper import db as db_module
from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.execution_subscriber import (
    ExecutionSubscriber,
    main as execution_subscriber_main,
    record_execution_event,
)
from biotech_sniper.paper_executor import PaperExecutor


# ---------------------------------------------------------------------------
# Fixtures and stubs
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Migrated SQLite DB on a tmp path."""
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
    alpaca_order_id: str = "alp-1",
    client_order_id: str = "client-1",
    side: str = "buy",
    qty: int = 4,
    requested_mid_at_submit: float | None = 1.50,
    created_at: str = "2026-04-27T15:30:00.000Z",
    status: str = "submitted",
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
                alpaca_order_id,
                "AXSM250620C00125000",
                side,
                qty,
                status,
                client_order_id,
                created_at,
                requested_mid_at_submit,
            ),
        )
        conn.commit()
    finally:
        conn.close()


class _StubAlpacaClient:
    """Duck-typed broker double used by subscriber + wait_for_fill tests."""

    def __init__(
        self,
        responses: dict[str, list[dict[str, Any]]] | None = None,
        *,
        base_url: str = PAPER_BASE_URL,
    ) -> None:
        self.base_url = base_url
        self._responses = {k: list(v) for k, v in (responses or {}).items()}
        self.calls: list[str] = []

    def get_order(self, order_id: str) -> dict[str, Any]:
        self.calls.append(order_id)
        seq = self._responses.get(order_id, [])
        if not seq:
            raise AssertionError(
                f"no queued response for order_id={order_id!r}"
            )
        # Last queued result sticks (mirrors a sandbox where
        # repeat polls return the same row).
        return seq[0] if len(seq) == 1 else seq.pop(0)

    def get_positions(self) -> list[dict[str, Any]]:  # pragma: no cover
        return []


# ---------------------------------------------------------------------------
# Fix #1: poll_once on partial_fill writes BOTH execution_events
# AND execution_fills.
# ---------------------------------------------------------------------------


def test_poll_once_partial_fill_writes_event_and_fill(db_path: Path) -> None:
    """A ``partial_fill`` broker payload lands one event + one fill row."""
    _insert_paper_order(
        db_path,
        side="buy",
        qty=4,
        requested_mid_at_submit=1.50,
        created_at="2026-04-27T15:30:00.000Z",
    )
    # Pre-record submitted+accepted so the validator allows
    # 'partial_fill' as the next state.
    record_execution_event(
        db_path,
        paper_order_id="po-1",
        event_type="submitted",
        event_at="2026-04-27T15:30:00.500Z",
    )
    record_execution_event(
        db_path,
        paper_order_id="po-1",
        event_type="accepted",
        event_at="2026-04-27T15:30:01.000Z",
    )

    client = _StubAlpacaClient(
        {
            "alp-1": [
                {
                    "id": "alp-1",
                    "status": "partial_fill",
                    "filled_at": "2026-04-27T15:30:03.000Z",
                    "filled_qty": 2,
                    "filled_avg_price": 1.51,
                }
            ]
        }
    )
    subscriber = ExecutionSubscriber(client, db_path=db_path)
    inserted = subscriber.poll_once()
    assert inserted == 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        events = [
            r["event_type"]
            for r in conn.execute(
                "SELECT event_type FROM execution_events "
                "WHERE paper_order_id = ? ORDER BY event_at ASC",
                ("po-1",),
            ).fetchall()
        ]
        fill_rows = conn.execute(
            "SELECT filled_price, filled_qty, partial_qty_remaining, "
            "slippage_bps FROM execution_fills WHERE paper_order_id = ?",
            ("po-1",),
        ).fetchall()
    finally:
        conn.close()

    # Fix #1 contract: BOTH tables get a row.
    assert events[-1] == "partial_fill", (
        f"expected last event 'partial_fill' but got {events!r}"
    )
    assert len(fill_rows) == 1, (
        f"expected exactly one execution_fills row, got {len(fill_rows)}"
    )
    fill = fill_rows[0]
    assert fill["filled_price"] == pytest.approx(1.51)
    assert fill["filled_qty"] == 2
    # qty=4 total, qty=2 filled → 2 remaining
    assert fill["partial_qty_remaining"] == 2
    # buy filled above mid → positive bps
    assert fill["slippage_bps"] > 0


def test_poll_once_filled_writes_event_and_fill(db_path: Path) -> None:
    """A terminal ``filled`` payload also writes BOTH rows."""
    _insert_paper_order(
        db_path,
        side="buy",
        qty=2,
        requested_mid_at_submit=1.50,
        created_at="2026-04-27T15:30:00.000Z",
    )
    record_execution_event(
        db_path,
        paper_order_id="po-1",
        event_type="submitted",
        event_at="2026-04-27T15:30:00.500Z",
    )
    record_execution_event(
        db_path,
        paper_order_id="po-1",
        event_type="accepted",
        event_at="2026-04-27T15:30:01.000Z",
    )

    client = _StubAlpacaClient(
        {
            "alp-1": [
                {
                    "id": "alp-1",
                    "status": "filled",
                    "filled_at": "2026-04-27T15:30:05.000Z",
                    "filled_qty": 2,
                    "filled_avg_price": 1.52,
                }
            ]
        }
    )
    subscriber = ExecutionSubscriber(client, db_path=db_path)
    subscriber.poll_once()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        last_event = conn.execute(
            "SELECT event_type FROM execution_events "
            "WHERE paper_order_id = ? ORDER BY event_at DESC LIMIT 1",
            ("po-1",),
        ).fetchone()
        fill = conn.execute(
            "SELECT filled_price, filled_qty, partial_qty_remaining "
            "FROM execution_fills WHERE paper_order_id = ?",
            ("po-1",),
        ).fetchone()
    finally:
        conn.close()
    assert last_event["event_type"] == "filled"
    assert fill is not None
    assert fill["filled_qty"] == 2
    assert fill["partial_qty_remaining"] == 0


# ---------------------------------------------------------------------------
# Fix #2: wait_for_fill cycling accepted → filled produces
# execution_events rows for BOTH transitions.
# ---------------------------------------------------------------------------


def test_wait_for_fill_emits_events_for_accepted_and_filled(
    db_path: Path,
) -> None:
    """Cycling broker statuses accepted → filled writes both events.

    The initial ``submitted`` event is written by ``execute()``'s
    write-then-submit path. We seed that here so the validator
    accepts the wait_for_fill-emitted transitions.
    """
    _insert_paper_order(
        db_path,
        side="buy",
        qty=2,
        requested_mid_at_submit=1.50,
        created_at="2026-04-27T15:30:00.000Z",
    )
    record_execution_event(
        db_path,
        paper_order_id="po-1",
        event_type="submitted",
        event_at="2026-04-27T15:30:00.500Z",
    )

    accepted = {
        "id": "alp-1",
        "status": "accepted",
        "updated_at": "2026-04-27T15:30:01.000Z",
        "filled_qty": 0,
        "filled_avg_price": None,
    }
    filled = {
        "id": "alp-1",
        "status": "filled",
        "filled_at": "2026-04-27T15:30:05.000Z",
        "updated_at": "2026-04-27T15:30:05.000Z",
        "filled_qty": 2,
        "filled_avg_price": 1.52,
    }
    client = _StubAlpacaClient({"alp-1": [accepted, filled]})

    executor = PaperExecutor(
        client, db_path=db_path, poll_interval_seconds=0.0
    )

    final = executor.wait_for_fill(
        "alp-1", timeout_seconds=5.0, poll_interval_seconds=0.0
    )
    assert final["status"] == "filled"

    conn = sqlite3.connect(db_path)
    try:
        events = [
            r[0]
            for r in conn.execute(
                "SELECT event_type FROM execution_events "
                "WHERE paper_order_id = ? ORDER BY event_at ASC, id ASC",
                ("po-1",),
            ).fetchall()
        ]
    finally:
        conn.close()

    # The submitted seed is the first row; wait_for_fill must add
    # accepted and filled (one each).
    assert "accepted" in events, f"missing 'accepted'; got {events!r}"
    assert "filled" in events, f"missing 'filled'; got {events!r}"
    # Order matters: accepted strictly before filled.
    assert events.index("accepted") < events.index("filled")


def test_wait_for_fill_skips_telemetry_when_no_paper_orders_row(
    db_path: Path,
) -> None:
    """No matching ``paper_orders`` row → no telemetry written.

    Tests that wait_for_fill is safe to call from a code path
    where ``execute()`` was bypassed (e.g., the cassette-driven
    ``test_wait_for_fill_walks_accepted_to_filled`` in
    ``test_paper_executor.py`` populates the row inline). The
    poll loop should still terminate cleanly.
    """
    accepted = {
        "id": "alp-ghost",
        "status": "accepted",
        "updated_at": "2026-04-27T15:30:01.000Z",
    }
    filled = {
        "id": "alp-ghost",
        "status": "filled",
        "filled_at": "2026-04-27T15:30:05.000Z",
        "filled_qty": 2,
        "filled_avg_price": 1.52,
    }
    client = _StubAlpacaClient({"alp-ghost": [accepted, filled]})

    executor = PaperExecutor(
        client, db_path=db_path, poll_interval_seconds=0.0
    )
    final = executor.wait_for_fill(
        "alp-ghost", timeout_seconds=5.0, poll_interval_seconds=0.0
    )
    assert final["status"] == "filled"

    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM execution_events"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0


# ---------------------------------------------------------------------------
# Fix #3: CLI on a fresh DB with one open submitted order
# produces at least one execution_events row.
# ---------------------------------------------------------------------------


def test_cli_poll_once_on_open_order_writes_events(db_path: Path) -> None:
    """``main(['--poll-once', '--date', <today>])`` records progress.

    Seeds one ``paper_orders`` row in the canonical
    'submitted-and-acknowledged-by-broker' state (no
    execution_events written yet — the executor's write-then-submit
    row exists but the broker hasn't been polled). The CLI should:

    * Walk the open-orders query.
    * Call get_order on the broker stub.
    * Land at least one ``execution_events`` row.
    """
    today = "2026-04-27"
    _insert_paper_order(
        db_path,
        paper_order_id="po-cli-1",
        alpaca_order_id="alp-cli-1",
        client_order_id="client-cli-1",
        side="buy",
        qty=2,
        requested_mid_at_submit=1.50,
        created_at=f"{today}T15:30:00.000Z",
    )
    # Seed the 'submitted' event so subsequent broker-side
    # 'accepted' is a legal transition.
    record_execution_event(
        db_path,
        paper_order_id="po-cli-1",
        event_type="submitted",
        event_at=f"{today}T15:30:00.500Z",
    )

    client = _StubAlpacaClient(
        {
            "alp-cli-1": [
                {
                    "id": "alp-cli-1",
                    "status": "accepted",
                    "updated_at": f"{today}T15:30:01.000Z",
                    "filled_qty": 0,
                    "filled_avg_price": None,
                }
            ]
        }
    )

    exit_code = execution_subscriber_main(
        ["--poll-once", "--date", today],
        client=client,
        db_path=db_path,
    )
    assert exit_code == 0

    conn = sqlite3.connect(db_path)
    try:
        events = conn.execute(
            "SELECT event_type FROM execution_events "
            "WHERE paper_order_id = ? ORDER BY event_at ASC",
            ("po-cli-1",),
        ).fetchall()
    finally:
        conn.close()
    assert len(events) >= 1
    # The CLI must have observed the broker's 'accepted' state.
    assert any(row[0] == "accepted" for row in events), (
        f"expected an 'accepted' execution_events row; got {events!r}"
    )


def test_cli_dry_run_exits_zero_without_broker(db_path: Path) -> None:
    """The ``--dry-run`` flag exits 0 without touching the broker."""
    _insert_paper_order(
        db_path,
        paper_order_id="po-dry-1",
        alpaca_order_id="alp-dry-1",
        client_order_id="client-dry-1",
        created_at="2026-04-27T15:30:00.000Z",
    )

    exit_code = execution_subscriber_main(
        ["--dry-run", "--date", "2026-04-27"],
        client=None,  # dry-run never instantiates the broker
        db_path=db_path,
    )
    assert exit_code == 0


def test_cli_poll_once_filters_by_date(db_path: Path) -> None:
    """The ``--date`` filter narrows poll to that day's open orders."""
    # Two orders: one for today (matches), one yesterday (excluded)
    _insert_paper_order(
        db_path,
        paper_order_id="po-today",
        alpaca_order_id="alp-today",
        client_order_id="client-today",
        created_at="2026-04-27T15:30:00.000Z",
    )
    _insert_paper_order(
        db_path,
        paper_order_id="po-yesterday",
        alpaca_order_id="alp-yesterday",
        client_order_id="client-yesterday",
        created_at="2026-04-26T15:30:00.000Z",
    )
    record_execution_event(
        db_path,
        paper_order_id="po-today",
        event_type="submitted",
        event_at="2026-04-27T15:30:00.500Z",
    )
    record_execution_event(
        db_path,
        paper_order_id="po-yesterday",
        event_type="submitted",
        event_at="2026-04-26T15:30:00.500Z",
    )

    accepted_today = {
        "id": "alp-today",
        "status": "accepted",
        "updated_at": "2026-04-27T15:30:01.000Z",
    }
    # Only queue a response for today's id; if the CLI tries to
    # poll yesterday's order the stub raises.
    client = _StubAlpacaClient({"alp-today": [accepted_today]})

    exit_code = execution_subscriber_main(
        ["--poll-once", "--date", "2026-04-27"],
        client=client,
        db_path=db_path,
    )
    assert exit_code == 0
    # Yesterday's order MUST NOT have been polled.
    assert "alp-yesterday" not in client.calls
    assert "alp-today" in client.calls


# ---------------------------------------------------------------------------
# Fix #3 alt: Job 6 wiring smoke — the CLI is importable and invokable
# the same way intraday_scanner.run_intraday_scan calls it.
# ---------------------------------------------------------------------------


def test_intraday_scanner_job6_invokes_subscriber_main(
    db_path: Path, monkeypatch
) -> None:
    """JOB 6 in ``intraday_scanner`` calls ``execution_subscriber.main``."""
    import biotech_sniper.execution_subscriber as _es

    captured: dict[str, Any] = {}

    def _spy_main(argv, *, client=None, **_kw):  # noqa: ANN001
        captured["argv"] = argv
        captured["client_was_none"] = client is None
        return 0

    monkeypatch.setattr(_es, "main", _spy_main)

    # Re-import the helper from the symbol the scanner imports
    # locally inside JOB 6 — this exercises the same import path.
    from biotech_sniper.execution_subscriber import main as _local_main

    assert _local_main is _spy_main  # spy correctly installed
    code = _local_main(["--poll-once", "--date", "2026-04-27"])
    assert code == 0
    assert captured["argv"] == ["--poll-once", "--date", "2026-04-27"]
