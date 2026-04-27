"""Tests for ``biotech_sniper.execution_subscriber`` (f-m3-11).

Validation contract coverage:

* **VAL-M3-058** — ``execution_events`` schema (table exists with the
  expected columns and CHECK constraint enum).
* **VAL-M3-062** — every ``submitted`` paper_orders row has exactly
  one ``execution_events`` row of ``event_type='submitted'``.
* **VAL-M3-069** — write-then-submit invariant: the
  ``paper_orders.created_at`` is ``<=`` the corresponding
  ``execution_events.event_at`` for the ``submitted`` row.
* **VAL-M3-070** — DB is the source of truth; structured-log lines
  reference DB-resident ``client_order_id`` / ``paper_order_id``.

Tests are hermetic: no Alpaca network calls. Broker responses are
either inline dicts (for the subscriber's poll loop) or replayed
from the existing ``order_call_roundtrip.json`` cassette.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

import pytest

from biotech_sniper import db as db_module
from biotech_sniper.execution_subscriber import (
    EVENT_TYPES,
    ExecutionSubscriber,
    IllegalStateTransition,
    LEGAL_TRANSITIONS,
    TERMINAL_EVENT_TYPES,
    WriteThenSubmitViolation,
    record_execution_event,
)


CASSETTE_DIR = Path(__file__).parent / "fixtures" / "cassettes" / "alpaca"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Migrated SQLite database path for the test."""
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
    client_order_id: str = "client-1",
    status: str = "submitted",
    created_at: str = "2026-04-27T15:30:00.000Z",
    alpaca_order_id: Optional[str] = "alp-1",
    play_card_id: str = "PC-1",
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO paper_orders (
                id, play_card_id, alpaca_order_id, symbol, side, qty,
                status, client_order_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                paper_order_id,
                play_card_id,
                alpaca_order_id,
                "AXSM250620C00125000",
                "buy",
                2,
                status,
                client_order_id,
                created_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


class _StubAlpacaClient:
    """Duck-typed broker double used by ExecutionSubscriber tests."""

    def __init__(self, responses: dict[str, list[dict[str, Any]]]):
        self._responses = {k: list(v) for k, v in responses.items()}
        self.calls: list[str] = []

    def get_order(self, order_id: str) -> dict[str, Any]:
        self.calls.append(order_id)
        seq = self._responses.get(order_id, [])
        if not seq:
            raise AssertionError(
                f"no queued response for order_id={order_id!r}"
            )
        return seq[0] if len(seq) == 1 else seq.pop(0)


# ---------------------------------------------------------------------------
# Schema (VAL-M3-058)
# ---------------------------------------------------------------------------


def test_execution_events_table_schema_columns(db_path: Path) -> None:
    """``execution_events`` exposes every column f-m3-11 names."""
    conn = sqlite3.connect(db_path)
    try:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(execution_events)"
        ).fetchall()}
    finally:
        conn.close()
    expected = {
        "id",
        "paper_order_id",
        "event_type",
        "event_at",
        "raw_payload",
    }
    assert expected <= cols, f"missing columns: {expected - cols}"


def test_execution_events_event_type_check_rejects_unknown(
    db_path: Path,
) -> None:
    """The CHECK constraint blocks any event_type outside the enum."""
    _insert_paper_order(db_path)
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO execution_events "
                "(paper_order_id, event_type, event_at) "
                "VALUES (?, ?, ?)",
                ("po-1", "not_a_real_status", "2026-04-27T15:30:01Z"),
            )
            conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Low-level writer + write-then-submit invariant (VAL-M3-069)
# ---------------------------------------------------------------------------


def test_record_execution_event_inserts_row_and_returns_id(
    db_path: Path,
) -> None:
    """``record_execution_event`` writes one row and returns its rowid."""
    _insert_paper_order(db_path)
    new_id = record_execution_event(
        db_path,
        paper_order_id="po-1",
        event_type="submitted",
        event_at="2026-04-27T15:30:00.500Z",
        raw_payload={"id": "alp-1", "status": "submitted"},
    )
    assert new_id > 0
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM execution_events WHERE id = ?", (new_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["paper_order_id"] == "po-1"
    assert row["event_type"] == "submitted"
    payload = json.loads(row["raw_payload"])
    assert payload["status"] == "submitted"


def test_write_then_submit_violation_when_paper_orders_row_missing(
    db_path: Path,
) -> None:
    """A ``submitted`` event without a paper_orders row raises the invariant."""
    with pytest.raises(WriteThenSubmitViolation, match="no paper_orders row"):
        record_execution_event(
            db_path,
            paper_order_id="ghost-1",
            event_type="submitted",
            event_at="2026-04-27T15:30:00.500Z",
        )


def test_write_then_submit_violation_when_event_at_precedes_created_at(
    db_path: Path,
) -> None:
    """``event_at`` < ``paper_orders.created_at`` is the canonical violation."""
    _insert_paper_order(
        db_path,
        created_at="2026-04-27T16:00:00.000Z",  # AFTER the event_at below
    )
    with pytest.raises(WriteThenSubmitViolation, match="created_at="):
        record_execution_event(
            db_path,
            paper_order_id="po-1",
            event_type="submitted",
            event_at="2026-04-27T15:00:00.000Z",
        )


def test_record_execution_event_logs_ordered_chronologically(
    db_path: Path,
) -> None:
    """Sequential events on one order are queryable in chronological order."""
    _insert_paper_order(db_path)
    for et, t in [
        ("submitted", "2026-04-27T15:30:00.100Z"),
        ("accepted", "2026-04-27T15:30:00.200Z"),
        ("filled", "2026-04-27T15:30:05.000Z"),
    ]:
        record_execution_event(
            db_path,
            paper_order_id="po-1",
            event_type=et,
            event_at=t,
        )
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT event_type, event_at FROM execution_events "
            "WHERE paper_order_id = ? ORDER BY event_at ASC",
            ("po-1",),
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == ["submitted", "accepted", "filled"]


# ---------------------------------------------------------------------------
# Subscriber poll loop (VAL-M3-062)
# ---------------------------------------------------------------------------


def test_subscriber_records_filled_event_on_first_poll(db_path: Path) -> None:
    """Subscriber sees broker filled status → writes accepted+filled events.

    ``submitted`` is already recorded by the executor write-then-submit
    path; the subscriber covers the gap between accepted/partial_fill
    and the terminal state.
    """
    _insert_paper_order(db_path)
    record_execution_event(
        db_path, paper_order_id="po-1", event_type="submitted",
        event_at="2026-04-27T15:30:00.500Z",
    )
    record_execution_event(
        db_path, paper_order_id="po-1", event_type="accepted",
        event_at="2026-04-27T15:30:01.000Z",
    )
    client = _StubAlpacaClient({
        "alp-1": [{
            "id": "alp-1",
            "status": "filled",
            "filled_at": "2026-04-27T15:30:05.000Z",
            "filled_qty": 2,
            "filled_avg_price": 1.52,
        }]
    })
    subscriber = ExecutionSubscriber(client, db_path=db_path)
    inserted = subscriber.poll_once()
    assert inserted == 1
    conn = sqlite3.connect(db_path)
    try:
        types = [
            r[0] for r in conn.execute(
                "SELECT event_type FROM execution_events "
                "WHERE paper_order_id = ? ORDER BY event_at ASC",
                ("po-1",),
            ).fetchall()
        ]
    finally:
        conn.close()
    assert types == ["submitted", "accepted", "filled"]


def test_subscriber_skips_terminal_orders(db_path: Path) -> None:
    """Orders already in a terminal event state are not re-polled."""
    _insert_paper_order(db_path)
    for et, t in [
        ("submitted", "2026-04-27T15:30:00.100Z"),
        ("accepted", "2026-04-27T15:30:00.200Z"),
        ("filled", "2026-04-27T15:30:05.000Z"),
    ]:
        record_execution_event(
            db_path, paper_order_id="po-1", event_type=et, event_at=t
        )
    # Empty responses — if the subscriber tries to call get_order
    # the stub raises, so 0 inserts == 0 broker calls.
    client = _StubAlpacaClient({})
    subscriber = ExecutionSubscriber(client, db_path=db_path)
    inserted = subscriber.poll_once()
    assert inserted == 0
    assert client.calls == []


def test_subscriber_dedups_same_state_on_repeat_poll(
    db_path: Path,
) -> None:
    """Polling twice with the same broker state inserts at most once."""
    _insert_paper_order(db_path)
    record_execution_event(
        db_path, paper_order_id="po-1", event_type="submitted",
        event_at="2026-04-27T15:30:00.500Z",
    )
    accepted = {
        "id": "alp-1",
        "status": "accepted",
        "updated_at": "2026-04-27T15:30:01.000Z",
    }
    client = _StubAlpacaClient({"alp-1": [accepted, accepted]})
    subscriber = ExecutionSubscriber(client, db_path=db_path)
    first = subscriber.poll_once()
    second = subscriber.poll_once()
    assert first == 1
    assert second == 0
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM execution_events "
            "WHERE paper_order_id = ? AND event_type = 'accepted'",
            ("po-1",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


# ---------------------------------------------------------------------------
# Logging & legal-transition wiring (VAL-M3-070 sanity)
# ---------------------------------------------------------------------------


def test_record_execution_event_emits_debug_log_referencing_db(
    db_path: Path, caplog
) -> None:
    """The logger line names paper_order_id; DB stays the source of truth."""
    _insert_paper_order(db_path)
    caplog.set_level(logging.DEBUG, logger="biotech_sniper.execution_subscriber")
    record_execution_event(
        db_path,
        paper_order_id="po-1",
        event_type="submitted",
        event_at="2026-04-27T15:30:00.500Z",
    )
    msgs = [r.getMessage() for r in caplog.records]
    assert any("paper_order_id=po-1" in m and "event_recorded" in m for m in msgs)


def test_legal_transitions_table_covers_every_event_type() -> None:
    """Every event type is reachable as either source or destination."""
    sources = set(LEGAL_TRANSITIONS.keys())
    destinations: set[str] = set()
    for nexts in LEGAL_TRANSITIONS.values():
        destinations |= set(nexts)
    # Every EVENT_TYPES member appears as a source or destination.
    assert EVENT_TYPES <= (sources | destinations)
    # Every terminal state has an empty next-set.
    for term in TERMINAL_EVENT_TYPES:
        assert LEGAL_TRANSITIONS[term] == frozenset()
