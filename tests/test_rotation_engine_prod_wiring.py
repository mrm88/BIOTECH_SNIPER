"""Tests for f-m3-18 — rotation_engine production wiring fixes.

Three surgical fixes layered on top of f-m3-10:

1. **EXECUTOR INJECTED IN PROD** — ``intraday_scanner.run_intraday_scan``
   JOB 4 now constructs a :class:`PaperExecutor` (gated by
   ``BIOTECH_SNIPER_PAPER_EXECUTE``) and passes it into
   :func:`evaluate_rotation`. With the gate closed (the default for
   the dev / CI suite) the engine still runs but with
   ``executor=None`` so dry-run cycles record audit decisions
   without contacting the broker.
2. **load_active_plays_from_db full surface** — returns
   ``scoring_cache_id``, ``symbol``, ``qty``, ``ensemble_score``,
   ``catalyst_date`` for each active play. Without these fields the
   default debate runner skips and ``_submit_rotation_sell`` skips
   when ``qty<1``.
3. **load_today_candidates_from_db full surface** — returns
   ``catalyst_date`` AND a buy-card shape (``option_legs`` /
   ``play_card``) for each challenger so the 24h guard works on
   challengers AND :meth:`PaperExecutor.execute` receives a valid
   card.

Test cases mirror the f-m3-18 spec's required suite:

* (a) ``run_intraday_scan`` with mocked PaperExecutor invokes
  ``evaluate_rotation`` with ``executor=that_mock``;
* (b) ``load_active_plays_from_db`` on a fixtured DB returns rows
  with all 5 required fields populated;
* (c) ``load_today_candidates_from_db`` on a fixtured DB returns
  rows with non-None ``catalyst_date`` and ``option_legs``;
* (d) end-to-end: a fixtured DB where challenger > incumbent + 0.10,
  both >24h from catalyst, returns one rotation with exactly one
  sell + one buy in ``paper_orders``.
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
from biotech_sniper import rotation_engine as re_mod
from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.paper_executor import PaperExecutor
from biotech_sniper.rotation_engine import (
    ENTRY_EVENT,
    ROTATION_EVENT,
    evaluate_rotation,
    load_active_plays_from_db,
    load_today_candidates_from_db,
)


TODAY = datetime.date(2026, 4, 27)


# ---------------------------------------------------------------------------
# Fixtures
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


# ---------------------------------------------------------------------------
# (a) intraday_scanner.run_intraday_scan with mocked PaperExecutor
# ---------------------------------------------------------------------------


def test_intraday_scan_passes_executor_when_paper_execute_flag_enabled(
    monkeypatch,
):
    """JOB 4 must inject a real PaperExecutor when the gate is open.

    With ``BIOTECH_SNIPER_PAPER_EXECUTE=1`` the scanner constructs a
    :class:`PaperExecutor` and passes it into
    ``evaluate_rotation(executor=...)``. The test patches
    ``_build_rotation_executor`` to return a sentinel so we don't need
    real Alpaca credentials, then asserts the sentinel landed on the
    rotation call.
    """
    from biotech_sniper import intraday_scanner as scanner

    sentinel_executor = object()
    monkeypatch.setattr(
        scanner, "_build_rotation_executor", lambda: sentinel_executor
    )

    captured: dict[str, Any] = {}

    def _fake_evaluate_rotation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {
            "decisions": [],
            "skips": [],
            "active_count": 0,
            "capacity": 0,
        }

    # JOB 4 imports evaluate_rotation lazily inside the try-block.
    # Patching the source module covers that import path.
    from biotech_sniper import rotation_engine

    monkeypatch.setattr(
        rotation_engine, "evaluate_rotation", _fake_evaluate_rotation
    )

    # Stub out everything else so run_intraday_scan completes fast.
    monkeypatch.setattr(scanner, "scan_news_rss", lambda *_a, **_k: [])
    monkeypatch.setattr(scanner, "scan_sec_rss", lambda *_a, **_k: [])
    monkeypatch.setattr(scanner, "scan_ir_pages_quick", lambda *_a, **_k: [])
    monkeypatch.setattr(
        scanner, "scan_usaspending_intraday", lambda *_a, **_k: []
    )
    monkeypatch.setattr(scanner, "scan_fda_rss", lambda *_a, **_k: [])
    monkeypatch.setattr(scanner, "check_removals", lambda *_a, **_k: [])
    monkeypatch.setattr(scanner, "check_upgrades", lambda *_a, **_k: [])
    monkeypatch.setattr(scanner, "load_active", lambda: {"active": {}})
    monkeypatch.setattr(
        scanner,
        "load_log",
        lambda: {
            "seen_urls": [],
            "seen_award_ids": [],
            "last_scan": None,
            "alerts_sent": [],
        },
    )
    monkeypatch.setattr(scanner, "save_log", lambda _l: None)
    monkeypatch.setattr(
        scanner, "_NEW_OPP_SNIPER_AVAILABLE", False, raising=False
    )

    # Stub iv_crush JOB 5 so we don't drag in the broker on this test.
    from biotech_sniper import iv_crush_exit_rules as ic

    monkeypatch.setattr(
        ic,
        "run_intraday_iv_crush_exit_job",
        lambda *_a, **_k: {
            "date": "2026-04-27",
            "considered": 0,
            "exited": 0,
            "errors": 0,
        },
    )

    scanner.run_intraday_scan()

    assert "kwargs" in captured, "evaluate_rotation was never called"
    assert captured["kwargs"].get("executor") is sentinel_executor


def test_paper_execute_flag_default_off_returns_none(monkeypatch):
    """The default (flag unset) keeps tests hermetic by injecting None."""
    from biotech_sniper import intraday_scanner as scanner

    monkeypatch.delenv("BIOTECH_SNIPER_PAPER_EXECUTE", raising=False)
    assert scanner._paper_execute_enabled() is False
    assert scanner._build_rotation_executor() is None


def test_paper_execute_flag_truthy_values(monkeypatch):
    """The flag honours ``1`` / ``true`` / ``yes`` / ``on``."""
    from biotech_sniper import intraday_scanner as scanner

    for val in ("1", "true", "TRUE", "Yes", "on"):
        monkeypatch.setenv("BIOTECH_SNIPER_PAPER_EXECUTE", val)
        assert scanner._paper_execute_enabled() is True, val


def test_build_rotation_executor_swallows_construction_error(monkeypatch):
    """Missing creds → executor is None (the cycle never crashes)."""
    from biotech_sniper import intraday_scanner as scanner

    monkeypatch.setenv("BIOTECH_SNIPER_PAPER_EXECUTE", "1")
    monkeypatch.delenv("ALPACA_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    assert scanner._build_rotation_executor() is None


# ---------------------------------------------------------------------------
# (b) load_active_plays_from_db returns all 5 required fields
# ---------------------------------------------------------------------------


def test_load_active_plays_from_db_returns_all_five_required_fields(
    fresh_db_path: Path,
):
    """Every active row must carry scoring_cache_id, symbol, qty,
    ensemble_score, catalyst_date — the f-m3-18 spec contract."""
    _insert_play(
        fresh_db_path,
        source_key="active:AXSM",
        ticker="AXSM",
        catalyst_date="2026-05-15",
        option_strike=125.0,
        option_expiry="2026-06-20",
        option_type="call",
        payload={
            "play_card_id": "AXSM-2026-04-27",
            "option_symbol": "AXSM260620C00125000",
        },
    )
    _insert_scoring_cache(
        fresh_db_path,
        ticker="AXSM",
        as_of_date="2026-04-27",
        ensemble_score=0.78,
    )
    _insert_paper_order(
        fresh_db_path,
        play_card_id="AXSM-2026-04-27",
        symbol="AXSM260620C00125000",
        qty=4,
        status="filled",
    )

    out = load_active_plays_from_db(db_path=fresh_db_path)
    assert len(out) == 1
    record = out[0]

    # All 5 required fields populated.
    assert record["scoring_cache_id"] is not None
    assert record["symbol"] == "AXSM260620C00125000"
    assert record["qty"] == 4
    assert record["ensemble_score"] == pytest.approx(0.78)
    assert record["catalyst_date"] == "2026-05-15"


def test_load_active_plays_constructs_symbol_when_payload_lacks_one(
    fresh_db_path: Path,
):
    """When no payload symbol exists, build OCC from strike+expiry+type."""
    _insert_play(
        fresh_db_path,
        source_key="active:VRDN",
        ticker="VRDN",
        catalyst_date="2026-05-20",
        option_strike=20.0,
        option_expiry="2026-06-19",
        option_type="call",
    )
    _insert_scoring_cache(
        fresh_db_path,
        ticker="VRDN",
        as_of_date="2026-04-27",
        ensemble_score=0.55,
    )

    out = load_active_plays_from_db(db_path=fresh_db_path)
    assert len(out) == 1
    # OCC: VRDN + 260619 + C + 00020000 (= $20 * 1000)
    assert out[0]["symbol"] == "VRDN260619C00020000"


def test_load_active_plays_falls_back_qty_to_payload_when_no_orders(
    fresh_db_path: Path,
):
    """No filled buy in paper_orders → qty falls back to payload contracts."""
    _insert_play(
        fresh_db_path,
        source_key="active:NTLA",
        ticker="NTLA",
        catalyst_date="2026-05-15",
        option_strike=10.0,
        option_expiry="2026-06-19",
        option_type="put",
        payload={
            "play_card_id": "NTLA-2026-04-27",
            "contracts": 3,
        },
    )
    _insert_scoring_cache(
        fresh_db_path,
        ticker="NTLA",
        as_of_date="2026-04-27",
        ensemble_score=0.62,
    )

    out = load_active_plays_from_db(db_path=fresh_db_path)
    assert len(out) == 1
    assert out[0]["qty"] == 3  # from payload, not paper_orders


def test_load_active_plays_skips_resolved_status(fresh_db_path: Path):
    """Only ``status='active'`` rows come back."""
    _insert_play(
        fresh_db_path,
        source_key="active:AAA",
        ticker="AAA",
        catalyst_date="2026-05-15",
    )
    _insert_play(
        fresh_db_path,
        source_key="resolved:BBB",
        ticker="BBB",
        status="resolved",
        catalyst_date="2024-01-01",
    )
    out = load_active_plays_from_db(db_path=fresh_db_path)
    assert sorted(p["ticker"] for p in out) == ["AAA"]


# ---------------------------------------------------------------------------
# (c) load_today_candidates_from_db returns catalyst_date + option_legs
# ---------------------------------------------------------------------------


def test_load_today_candidates_returns_catalyst_date_and_option_legs(
    fresh_db_path: Path,
):
    """Each candidate must carry catalyst_date AND option_legs."""
    # Seed a plays row so the loader can source catalyst metadata.
    _insert_play(
        fresh_db_path,
        source_key="active:AXSM",
        ticker="AXSM",
        catalyst_date="2026-05-15",
        option_strike=125.0,
        option_expiry="2026-06-20",
        option_type="call",
    )
    _insert_scoring_cache(
        fresh_db_path,
        ticker="AXSM",
        as_of_date="2026-04-27",
        ensemble_score=0.85,
    )
    out = load_today_candidates_from_db(today=TODAY, db_path=fresh_db_path)
    assert len(out) == 1
    cand = out[0]
    assert cand["catalyst_date"] == "2026-05-15"
    legs = cand["option_legs"]
    assert isinstance(legs, list) and len(legs) == 1
    leg = legs[0]
    assert leg["side"] == "buy"
    assert leg["qty"] == 1
    assert leg["symbol"] == "AXSM260620C00125000"
    # play_card mirrors option_legs and is ready for PaperExecutor.execute.
    assert cand["play_card"]["option_legs"] == legs


def test_load_today_candidates_orders_by_ensemble_score_desc(
    fresh_db_path: Path,
):
    """Multiple candidates are returned in score-desc order."""
    for tk, score in [("AAA", 0.50), ("BBB", 0.92), ("CCC", 0.71)]:
        _insert_play(
            fresh_db_path,
            source_key=f"active:{tk}",
            ticker=tk,
            catalyst_date="2026-06-01",
        )
        _insert_scoring_cache(
            fresh_db_path,
            ticker=tk,
            as_of_date="2026-04-27",
            ensemble_score=score,
        )
    out = load_today_candidates_from_db(today=TODAY, db_path=fresh_db_path)
    assert [c["ticker"] for c in out] == ["BBB", "CCC", "AAA"]


def test_load_today_candidates_returns_empty_when_db_missing(tmp_path: Path):
    """Absent DB returns ``[]``."""
    out = load_today_candidates_from_db(
        today=TODAY, db_path=tmp_path / "nope.db"
    )
    assert out == []


# ---------------------------------------------------------------------------
# (d) End-to-end: rotation produces one sell + one buy in paper_orders.
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Minimal duck-typed Alpaca client for the end-to-end test."""

    def __init__(self) -> None:
        self.base_url = PAPER_BASE_URL
        self.submit_calls: list[Any] = []
        self.get_order_calls: list[str] = []
        self._counter = 0

    def submit_order(self, order_request: Any) -> dict[str, Any]:
        self.submit_calls.append(order_request)
        self._counter += 1
        symbol = getattr(order_request, "symbol", None) or "UNKNOWN"
        side = getattr(order_request, "side", "buy")
        qty = getattr(order_request, "qty", 1)
        # Some order request objects expose an enum for side/qty;
        # coerce to plain values so the persistence helpers can
        # persist them.
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
        self.get_order_calls.append(order_id)
        return {
            "id": order_id,
            "status": "filled",
            "filled_qty": 1,
            "filled_avg_price": 1.50,
            "asset_class": "us_option",
        }

    def get_positions(self) -> list[dict[str, Any]]:
        return []


def test_end_to_end_rotation_produces_one_sell_and_one_buy(
    fresh_db_path: Path, tmp_path: Path
):
    """End-to-end: challenger > incumbent + 0.10, both >24h from catalyst.

    Expect exactly one sell + one buy in ``paper_orders`` after the
    rotation engine fires.
    """
    # ── INCUMBENT ─────────────────────────────────────────────────
    # Active play with a far-away catalyst date so the 24h guard
    # is not triggered.
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
    # Same far-future catalyst so 24h guard is irrelevant; ensemble
    # score 0.85 > 0.50 + 0.10.
    _insert_play(
        fresh_db_path,
        source_key="active:STRONG",  # carrier of option metadata only
        ticker="STRONG",
        status="resolved",  # NOT active; we only need the leg metadata
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

    # ── EXECUTOR ─────────────────────────────────────────────────
    fake_client = _FakeAlpacaClient()
    executor = PaperExecutor(
        fake_client,  # type: ignore[arg-type]
        db_path=fresh_db_path,
        poll_interval_seconds=0.0,
    )

    # ── DEBATE RUNNER (force outranking) ─────────────────────────
    def _runner(*, challenger, incumbent, today=None):
        return {
            "challenger_grade": "A",
            "incumbent_grade": "C",
            "rounds": 3,
            "trigger": "rotation",
        }

    # ── EVALUATE ─────────────────────────────────────────────────
    # max_concurrent=1 so the single incumbent fills capacity and
    # the challenger triggers a rotation.
    audit_path = tmp_path / "audit_latest.json"
    result = evaluate_rotation(
        today=TODAY,
        executor=executor,
        debate_runner=_runner,
        max_concurrent=1,
        db_path=fresh_db_path,
        audit_path=audit_path,
    )

    # One rotation decision, no skips.
    assert len(result["decisions"]) == 1, result
    decision = result["decisions"][0]
    assert decision["challenger"] == "STRONG"
    assert decision["incumbent"] == "WEAK"
    assert decision["sell_order_id"] is not None
    assert decision["buy_order_id"] is not None

    # Inspect paper_orders directly: we expect EXACTLY one sell with
    # event='rotation' and one buy with event='open' that the
    # rotation engine just submitted (in addition to the seed buy
    # that established the incumbent).
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

    assert len(rotation_sells) == 1, (
        f"expected exactly one rotation sell, got {len(rotation_sells)}: "
        f"{[dict(r) for r in rotation_sells]}"
    )
    assert rotation_sells[0]["symbol"] == "WEAK260717C00050000"
    assert rotation_sells[0]["qty"] == 2

    assert len(rotation_buys) == 1, (
        f"expected exactly one rotation buy, got {len(rotation_buys)}: "
        f"{[dict(r) for r in rotation_buys]}"
    )
    assert rotation_buys[0]["symbol"] == "STRONG260717C00075000"
    assert rotation_buys[0]["side"] == "buy"
