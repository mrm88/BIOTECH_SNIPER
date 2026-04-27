"""
Regression tests for f-m3-21: rotation challenger card preflight.

These tests lock the contract for the new
``incomplete_challenger_card`` skip reason that fires BEFORE the
sell leg is submitted whenever the challenger candidate is missing
the metadata required to construct a valid PaperExecutor card
(catalyst_date, play_card, option_legs[0].symbol). Without this
preflight a one-sided rotation (incumbent sold, challenger never
bought) could occur if ``executor.execute`` raises mid-flight.

Test cases:

* (a) ``test_preflight_skips_when_catalyst_date_missing`` —
  candidate with ``catalyst_date=None`` triggers
  ``skip='incomplete_challenger_card'`` and NO sell submission.
* (b) ``test_preflight_skips_when_option_legs_empty`` — candidate
  with empty / missing ``option_legs`` triggers the same skip.
* (c) ``test_full_shape_candidate_proceeds_normally`` — a
  fully-populated candidate produces exactly one sell + one buy
  (mirrors the f-m3-18 end-to-end regression).
* (d) ``test_audit_latest_json_preserves_incomplete_challenger_card`` —
  the new skip reason is merged into ``audit_latest.json`` under
  ``rotation_skipped`` and previous keys are preserved.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper import db as _db
from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.paper_executor import PaperExecutor
from biotech_sniper.rotation_engine import (
    ENTRY_EVENT,
    ROTATION_EVENT,
    VALID_SKIP_REASONS,
    _validate_challenger_card_complete,
    evaluate_rotation,
)


TODAY = datetime.date(2026, 4, 27)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeExecutor:
    """Capture ``submit_exit`` / ``execute`` calls without broker IO."""

    def __init__(self) -> None:
        self.exits: list[dict[str, Any]] = []
        self.entries: list[dict[str, Any]] = []
        self._exit_counter = 0
        self._entry_counter = 0

    def submit_exit(
        self, play, event, *, today=None, sell_qty=None
    ) -> str:
        self._exit_counter += 1
        order_id = f"sell-{self._exit_counter:04d}"
        self.exits.append(
            {
                "play": dict(play),
                "event": event,
                "today": today,
                "sell_qty": sell_qty,
                "order_id": order_id,
            }
        )
        return order_id

    def execute(self, card) -> str:
        self._entry_counter += 1
        order_id = f"buy-{self._entry_counter:04d}"
        self.entries.append({"card": dict(card), "order_id": order_id})
        return order_id


def _runner_factory(
    *, challenger_grade: str = "A", incumbent_grade: str = "C"
):
    """Return a debate runner that records every call."""

    calls: list[dict[str, Any]] = []

    def _runner(*, challenger, incumbent, today=None):
        calls.append(
            {
                "challenger": dict(challenger),
                "incumbent": dict(incumbent),
                "today": today,
            }
        )
        return {
            "challenger_grade": challenger_grade,
            "incumbent_grade": incumbent_grade,
            "rounds": 3,
            "trigger": "rotation",
        }

    _runner.calls = calls  # type: ignore[attr-defined]
    return _runner


def _active_play(ticker: str, score: float) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "ensemble_score": score,
        "catalyst_date": "2026-06-15",
        "qty": 4,
        "play_card_id": f"{ticker}-pc-001",
        "symbol": f"{ticker}260620C00050000",
        "scoring_cache_id": hash(ticker) % 100000,
    }


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit_latest.json"


# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------


def test_skip_reason_is_in_valid_skip_reasons():
    """``incomplete_challenger_card`` is the new whitelisted reason."""
    assert "incomplete_challenger_card" in VALID_SKIP_REASONS


def test_validator_accepts_full_shape_candidate():
    """A fully-populated candidate passes the preflight."""
    cand = {
        "ticker": "AAA",
        "catalyst_date": "2026-06-10",
        "play_card": {
            "play_card_id": "AAA-pc",
            "ticker": "AAA",
            "option_legs": [
                {"symbol": "AAA260620C00050000", "side": "buy", "qty": 1}
            ],
        },
        "option_legs": [
            {"symbol": "AAA260620C00050000", "side": "buy", "qty": 1}
        ],
    }
    assert _validate_challenger_card_complete(cand) is True


def test_validator_rejects_missing_catalyst_date():
    """No catalyst_date → preflight fails."""
    cand = {
        "ticker": "AAA",
        "catalyst_date": None,
        "play_card": {"option_legs": [{"symbol": "AAA260620C00050000"}]},
        "option_legs": [{"symbol": "AAA260620C00050000"}],
    }
    assert _validate_challenger_card_complete(cand) is False


def test_validator_rejects_empty_option_legs():
    """Empty ``option_legs`` → preflight fails."""
    cand = {
        "ticker": "AAA",
        "catalyst_date": "2026-06-10",
        "play_card": {"option_legs": []},
        "option_legs": [],
    }
    assert _validate_challenger_card_complete(cand) is False


def test_validator_rejects_missing_play_card():
    """No ``play_card`` → preflight fails."""
    cand = {
        "ticker": "AAA",
        "catalyst_date": "2026-06-10",
        "option_legs": [{"symbol": "AAA260620C00050000"}],
    }
    assert _validate_challenger_card_complete(cand) is False


def test_validator_rejects_missing_symbol_on_first_leg():
    """First leg missing ``symbol`` → preflight fails."""
    cand = {
        "ticker": "AAA",
        "catalyst_date": "2026-06-10",
        "play_card": {"option_legs": [{"side": "buy", "qty": 1}]},
        "option_legs": [{"side": "buy", "qty": 1}],
    }
    assert _validate_challenger_card_complete(cand) is False


# ---------------------------------------------------------------------------
# (a) candidate with None catalyst_date triggers preflight skip
# ---------------------------------------------------------------------------


def test_preflight_skips_when_catalyst_date_missing(audit_path: Path):
    """Candidate with ``catalyst_date=None`` skips with the new reason
    BEFORE any sell or debate is submitted.
    """
    runner = _runner_factory()
    executor = _FakeExecutor()

    active = [
        _active_play("AAA", 0.55),
        _active_play("BBB", 0.60),
        _active_play("CCC", 0.70),
        _active_play("DDD", 0.50),  # weakest
    ]
    candidates = [
        {
            "ticker": "EEE",
            "ensemble_score": 0.85,  # delta 0.35 — comfortably above
            "catalyst_date": None,  # ← MISSING
            "play_card": {
                "play_card_id": "EEE-pc",
                "ticker": "EEE",
                "option_legs": [
                    {
                        "symbol": "EEE260620C00075000",
                        "side": "buy",
                        "qty": 1,
                    }
                ],
            },
            "option_legs": [
                {
                    "symbol": "EEE260620C00075000",
                    "side": "buy",
                    "qty": 1,
                }
            ],
            "scoring_cache_id": 999,
        }
    ]

    result = evaluate_rotation(
        today=TODAY,
        active_plays=active,
        candidates=candidates,
        executor=executor,
        debate_runner=runner,
        max_concurrent=4,
        audit_path=audit_path,
    )

    # No rotation, exactly one skip.
    assert result["decisions"] == []
    assert len(result["skips"]) == 1
    skip = result["skips"][0]
    assert skip["reason"] == "incomplete_challenger_card"
    assert skip["challenger"] == "EEE"
    assert skip["incumbent"] == "DDD"

    # Critical invariant: NO sell, NO buy, NO debate fired.
    assert executor.exits == [], (
        "preflight must run before any sell submission"
    )
    assert executor.entries == []
    assert runner.calls == []  # type: ignore[attr-defined]

    # Audit JSON carries the new skip reason.
    audit = json.loads(audit_path.read_text())
    assert audit["rotation_skipped"]["reason"] == "incomplete_challenger_card"


# ---------------------------------------------------------------------------
# (b) candidate with empty option_legs triggers preflight skip
# ---------------------------------------------------------------------------


def test_preflight_skips_when_option_legs_empty(audit_path: Path):
    """Candidate with empty ``option_legs`` skips with the new reason
    BEFORE any sell submission.
    """
    runner = _runner_factory()
    executor = _FakeExecutor()

    active = [
        _active_play("AAA", 0.55),
        _active_play("BBB", 0.60),
        _active_play("CCC", 0.70),
        _active_play("DDD", 0.50),
    ]
    candidates = [
        {
            "ticker": "EEE",
            "ensemble_score": 0.85,
            "catalyst_date": "2026-06-15",
            "play_card": {
                "play_card_id": "EEE-pc",
                "ticker": "EEE",
                "option_legs": [],  # ← EMPTY
            },
            "option_legs": [],  # ← EMPTY
            "scoring_cache_id": 1000,
        }
    ]

    result = evaluate_rotation(
        today=TODAY,
        active_plays=active,
        candidates=candidates,
        executor=executor,
        debate_runner=runner,
        max_concurrent=4,
        audit_path=audit_path,
    )

    assert result["decisions"] == []
    assert len(result["skips"]) == 1
    assert result["skips"][0]["reason"] == "incomplete_challenger_card"
    assert result["skips"][0]["option_legs_count"] == 0

    # No order submission and no debate cost.
    assert executor.exits == []
    assert executor.entries == []
    assert runner.calls == []  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# (c) full-shape candidate proceeds normally (regression of f-m3-18 e2e)
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db_path(tmp_path: Path) -> Path:
    """Create a freshly-migrated SQLite db at ``tmp_path/alpha.db``."""
    db_path = tmp_path / "alpha.db"
    conn = _db.connect(db_path)
    try:
        _db.run_migrations(conn)
    finally:
        conn.close()
    return db_path


def _insert_play(
    db_path: Path,
    *,
    source_key: str,
    ticker: str,
    status: str = "active",
    catalyst_date: str | None = None,
    option_strike: float | None = 50.0,
    option_expiry: str | None = "2026-06-19",
    option_type: str | None = "call",
    payload: dict[str, Any] | None = None,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO plays (
                source_key, ticker, status, catalyst_date,
                option_type, option_strike, option_expiry, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_key,
                ticker,
                status,
                catalyst_date,
                option_type,
                option_strike,
                option_expiry,
                json.dumps(payload) if payload is not None else None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_scoring_cache(
    db_path: Path,
    *,
    ticker: str,
    as_of_date: str,
    ensemble_score: float,
    science_grade: str = "B",
    payload: dict[str, Any] | None = None,
) -> int:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            """
            INSERT INTO scoring_cache (
                ticker, as_of_date, ensemble_score, science_grade,
                payload
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                ticker,
                as_of_date,
                ensemble_score,
                science_grade,
                json.dumps(payload) if payload is not None else None,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _insert_paper_order(
    db_path: Path,
    *,
    play_card_id: str,
    symbol: str,
    qty: int,
    side: str = "buy",
    event: str = ENTRY_EVENT,
    status: str = "filled",
    purpose: str = "entry",
    client_order_id: str | None = None,
) -> str:
    conn = sqlite3.connect(db_path)
    try:
        order_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO paper_orders (
                id, play_card_id, alpaca_order_id, symbol, side,
                qty, status, event, purpose, client_order_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                play_card_id,
                f"alpaca-{order_id[:8]}",
                symbol,
                side,
                qty,
                status,
                event,
                purpose,
                client_order_id or f"co-{order_id[:8]}",
            ),
        )
        conn.commit()
        return order_id
    finally:
        conn.close()


class _FakeAlpacaClient:
    """Minimal duck-typed Alpaca client for the e2e test."""

    def __init__(self) -> None:
        self.base_url = PAPER_BASE_URL
        self.submit_calls: list[Any] = []
        self._counter = 0

    def submit_order(self, order_request: Any) -> dict[str, Any]:
        self.submit_calls.append(order_request)
        self._counter += 1
        symbol = getattr(order_request, "symbol", None) or "UNKNOWN"
        side = getattr(order_request, "side", "buy")
        qty = getattr(order_request, "qty", 1)
        side_str = (
            side.value if hasattr(side, "value") else str(side)
        ).lower()
        try:
            qty_int = int(qty)
        except (TypeError, ValueError):
            qty_int = 1
        return {
            "id": f"alp-{self._counter:04d}",
            "client_order_id": getattr(
                order_request, "client_order_id", None
            ),
            "symbol": symbol,
            "side": side_str,
            "qty": qty_int,
            "status": "filled",
            "filled_qty": qty_int,
            "filled_avg_price": 1.50,
            "asset_class": "us_option",
        }

    def get_order(self, order_id: str) -> dict[str, Any]:
        return {
            "id": order_id,
            "status": "filled",
            "filled_qty": 1,
            "filled_avg_price": 1.50,
            "asset_class": "us_option",
        }

    def get_positions(self) -> list[dict[str, Any]]:
        return []


def test_full_shape_candidate_proceeds_normally(
    fresh_db_path: Path, tmp_path: Path
):
    """A complete challenger (catalyst_date + option_legs[0].symbol +
    play_card) MUST still produce exactly one sell + one buy after
    the f-m3-21 preflight is in place. Mirrors the f-m3-18
    end-to-end regression so we confirm the new gate does NOT
    introduce a regression on the happy path.
    """
    # ── INCUMBENT ─────────────────────────────────────────────────
    _insert_play(
        fresh_db_path,
        source_key="active:WEAK",
        ticker="WEAK",
        catalyst_date="2026-06-15",
        option_strike=50.0,
        option_expiry="2026-07-17",
        option_type="call",
        payload={
            "play_card_id": "WEAK-pc-001",
            "option_symbol": "WEAK260717C00050000",
        },
    )
    _insert_scoring_cache(
        fresh_db_path,
        ticker="WEAK",
        as_of_date="2026-04-27",
        ensemble_score=0.50,
    )
    _insert_paper_order(
        fresh_db_path,
        play_card_id="WEAK-pc-001",
        symbol="WEAK260717C00050000",
        qty=2,
        status="filled",
    )

    # ── CHALLENGER ───────────────────────────────────────────────
    _insert_play(
        fresh_db_path,
        source_key="active:STRONG",
        ticker="STRONG",
        status="resolved",
        catalyst_date="2026-06-20",
        option_strike=75.0,
        option_expiry="2026-07-17",
        option_type="call",
    )
    _insert_scoring_cache(
        fresh_db_path,
        ticker="STRONG",
        as_of_date="2026-04-27",
        ensemble_score=0.85,
    )

    fake_client = _FakeAlpacaClient()
    executor = PaperExecutor(
        fake_client,  # type: ignore[arg-type]
        db_path=fresh_db_path,
        poll_interval_seconds=0.0,
    )

    def _runner(*, challenger, incumbent, today=None):
        return {
            "challenger_grade": "A",
            "incumbent_grade": "C",
            "rounds": 3,
            "trigger": "rotation",
        }

    audit = tmp_path / "audit_latest.json"
    result = evaluate_rotation(
        today=TODAY,
        executor=executor,
        debate_runner=_runner,
        max_concurrent=1,
        db_path=fresh_db_path,
        audit_path=audit,
    )

    assert len(result["decisions"]) == 1, result
    decision = result["decisions"][0]
    assert decision["challenger"] == "STRONG"
    assert decision["incumbent"] == "WEAK"
    assert decision["sell_order_id"] is not None
    assert decision["buy_order_id"] is not None

    # Exactly one rotation sell + one rotation buy in paper_orders.
    conn = sqlite3.connect(fresh_db_path)
    conn.row_factory = sqlite3.Row
    try:
        rotation_sells = conn.execute(
            "SELECT * FROM paper_orders "
            "WHERE event = ? AND side = 'sell'",
            (ROTATION_EVENT,),
        ).fetchall()
        rotation_buys = conn.execute(
            "SELECT * FROM paper_orders "
            "WHERE event = 'open' AND side = 'buy' "
            "  AND symbol LIKE 'STRONG%'",
        ).fetchall()
    finally:
        conn.close()

    assert len(rotation_sells) == 1
    assert rotation_sells[0]["symbol"] == "WEAK260717C00050000"
    assert len(rotation_buys) == 1
    assert rotation_buys[0]["symbol"] == "STRONG260717C00075000"

    # The audit JSON should NOT carry an incomplete_challenger_card
    # skip on the happy path.
    audit_data = json.loads(audit.read_text()) if audit.is_file() else {}
    skip = audit_data.get("rotation_skipped") or {}
    assert skip.get("reason") != "incomplete_challenger_card"


# ---------------------------------------------------------------------------
# (d) audit_latest.json merge preserves the new skip reason
# ---------------------------------------------------------------------------


def test_audit_latest_json_preserves_incomplete_challenger_card(
    audit_path: Path,
):
    """The new skip reason is merged into ``rotation_skipped`` and any
    pre-existing top-level keys in ``audit_latest.json`` are preserved
    verbatim by the audit-merge helper.
    """
    # Seed an existing audit JSON with sibling keys + a different
    # rotation_skipped block to confirm the merger overwrites only
    # ``rotation_skipped`` and leaves siblings intact.
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            {
                "active_plays_summary": {"count": 4},
                "rotation_skipped": {
                    "reason": "below_threshold",
                    "stale": True,
                },
                "iv_crush_summary": {"considered": 2},
            }
        )
    )

    runner = _runner_factory()
    executor = _FakeExecutor()
    active = [
        _active_play("AAA", 0.55),
        _active_play("BBB", 0.60),
        _active_play("CCC", 0.70),
        _active_play("DDD", 0.50),
    ]
    # Candidate is incomplete (None catalyst_date) so the preflight
    # fires.
    candidates = [
        {
            "ticker": "EEE",
            "ensemble_score": 0.85,
            "catalyst_date": None,
            "play_card": {
                "play_card_id": "EEE-pc",
                "option_legs": [
                    {"symbol": "EEE260620C00075000", "side": "buy", "qty": 1}
                ],
            },
            "option_legs": [
                {"symbol": "EEE260620C00075000", "side": "buy", "qty": 1}
            ],
        }
    ]

    result = evaluate_rotation(
        today=TODAY,
        active_plays=active,
        candidates=candidates,
        executor=executor,
        debate_runner=runner,
        max_concurrent=4,
        audit_path=audit_path,
    )

    assert result["decisions"] == []
    assert result["skips"][0]["reason"] == "incomplete_challenger_card"

    audit = json.loads(audit_path.read_text())
    # New skip reason replaced the old rotation_skipped block.
    assert audit["rotation_skipped"]["reason"] == "incomplete_challenger_card"
    assert audit["rotation_skipped"]["challenger"] == "EEE"
    # The earlier "stale: True" key from the seeded block is gone
    # because the merger overwrites the whole rotation_skipped block.
    assert "stale" not in audit["rotation_skipped"]
    # Sibling keys are preserved verbatim.
    assert audit["active_plays_summary"] == {"count": 4}
    assert audit["iv_crush_summary"] == {"considered": 2}
