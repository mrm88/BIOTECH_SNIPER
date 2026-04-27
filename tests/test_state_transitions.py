"""Tests for the f-m3-11 monotonic state-transition validator.

Validation contract coverage:

* **VAL-M3-064** — order state transitions are monotonic and complete.
  The validator rejects any sequence that is NOT one of the legal
  paths::

      submitted → accepted → [partial_fill]* → filled
      submitted → accepted → canceled
      submitted → accepted → expired
      submitted → rejected

The tests exercise both the in-memory pure validator
(:func:`biotech_sniper.execution_subscriber.validate_state_transition`)
AND the runtime guard inside
:func:`record_execution_event` so the contract is enforced from
both layers.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from biotech_sniper import db as db_module
from biotech_sniper.execution_subscriber import (
    EVENT_TYPES,
    IllegalStateTransition,
    LEGAL_TRANSITIONS,
    TERMINAL_EVENT_TYPES,
    record_execution_event,
    validate_state_transition,
)


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
    client_order_id: str = "client-1",
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
                "PC-1",
                "alp-1",
                "AXSM250620C00125000",
                "buy",
                2,
                "submitted",
                client_order_id,
                "2026-04-27T15:30:00.000Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Legal-path coverage.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sequence",
    [
        ("submitted", "accepted", "filled"),
        ("submitted", "accepted", "partial_fill", "filled"),
        ("submitted", "accepted", "partial_fill", "partial_fill", "filled"),
        ("submitted", "accepted", "canceled"),
        ("submitted", "accepted", "expired"),
        ("submitted", "rejected"),
    ],
    ids=[
        "fast_fill",
        "single_partial_then_fill",
        "multi_partial_then_fill",
        "accepted_then_canceled",
        "accepted_then_expired",
        "submitted_then_rejected",
    ],
)
def test_legal_sequences_pass_validator(sequence) -> None:
    """Every canonical sequence walks cleanly through the validator."""
    prev = None
    for step in sequence:
        # Should not raise.
        validate_state_transition(prev, step)
        prev = step


def test_legal_full_sequence_persists_in_db(db_path: Path) -> None:
    """Recording the canonical full sequence end-to-end leaves N rows."""
    _insert_paper_order(db_path)
    sequence = [
        ("submitted", "2026-04-27T15:30:00.500Z"),
        ("accepted", "2026-04-27T15:30:01.000Z"),
        ("partial_fill", "2026-04-27T15:30:03.000Z"),
        ("filled", "2026-04-27T15:30:05.000Z"),
    ]
    for et, t in sequence:
        record_execution_event(
            db_path, paper_order_id="po-1", event_type=et, event_at=t
        )
    conn = sqlite3.connect(db_path)
    try:
        rows = [
            r[0]
            for r in conn.execute(
                "SELECT event_type FROM execution_events "
                "WHERE paper_order_id = ? ORDER BY event_at ASC",
                ("po-1",),
            ).fetchall()
        ]
    finally:
        conn.close()
    assert rows == ["submitted", "accepted", "partial_fill", "filled"]


# ---------------------------------------------------------------------------
# Illegal sequences (the contract's anti-cases).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "previous, next_",
    [
        (None, "filled"),       # first event must be 'submitted'
        (None, "accepted"),     # first event must be 'submitted'
        ("submitted", "filled"),    # skips 'accepted'
        ("submitted", "partial_fill"),  # skips 'accepted'
        ("accepted", "submitted"),  # backwards
        ("accepted", "rejected"),   # rejected only allowed from submitted
        ("filled", "filled"),       # terminal — no further events
        ("rejected", "accepted"),   # terminal — no further events
        ("canceled", "filled"),     # terminal — no further events
    ],
)
def test_illegal_transitions_are_rejected(previous, next_) -> None:
    """The validator surfaces every illegal transition as IllegalStateTransition."""
    with pytest.raises(IllegalStateTransition):
        validate_state_transition(previous, next_)


def test_unknown_event_type_is_rejected_by_validator() -> None:
    """An out-of-enum event_type is rejected before any DB call."""
    with pytest.raises(IllegalStateTransition, match="unknown event_type"):
        validate_state_transition("submitted", "halted")


def test_runtime_guard_rejects_skip_from_submitted_to_filled(
    db_path: Path,
) -> None:
    """The DB writer enforces the same graph as the pure validator."""
    _insert_paper_order(db_path)
    record_execution_event(
        db_path, paper_order_id="po-1", event_type="submitted",
        event_at="2026-04-27T15:30:00.500Z",
    )
    with pytest.raises(IllegalStateTransition, match="illegal transition"):
        record_execution_event(
            db_path, paper_order_id="po-1", event_type="filled",
            event_at="2026-04-27T15:30:05.000Z",
        )


def test_terminal_state_blocks_further_events(db_path: Path) -> None:
    """Once an order is ``filled`` (terminal), no further events land."""
    _insert_paper_order(db_path)
    record_execution_event(
        db_path, paper_order_id="po-1", event_type="submitted",
        event_at="2026-04-27T15:30:00.500Z",
    )
    record_execution_event(
        db_path, paper_order_id="po-1", event_type="accepted",
        event_at="2026-04-27T15:30:01.000Z",
    )
    record_execution_event(
        db_path, paper_order_id="po-1", event_type="filled",
        event_at="2026-04-27T15:30:05.000Z",
    )
    with pytest.raises(IllegalStateTransition):
        record_execution_event(
            db_path, paper_order_id="po-1", event_type="canceled",
            event_at="2026-04-27T15:30:10.000Z",
        )


# ---------------------------------------------------------------------------
# Sanity: graph constants stay in sync with the SQL CHECK enum.
# ---------------------------------------------------------------------------


def test_event_types_match_terminal_subset() -> None:
    """``TERMINAL_EVENT_TYPES`` must be a subset of ``EVENT_TYPES``."""
    assert TERMINAL_EVENT_TYPES <= EVENT_TYPES


def test_legal_transitions_only_emit_known_event_types() -> None:
    """Every value in ``LEGAL_TRANSITIONS`` is a valid event type."""
    for nexts in LEGAL_TRANSITIONS.values():
        assert set(nexts) <= EVENT_TYPES
