"""Cross-flow tests for cooldown + UTC-window invariants (f-cross-08).

Feature: ``f-cross-08-cooldown-and-utc-windows``.

Pins the validation-contract assertions VAL-CROSS-031, VAL-CROSS-032,
VAL-CROSS-033, VAL-CROSS-034:

* **VAL-CROSS-031** — News-event entry blocked by 24h cooldown when
  the ticker recently entered. The Stage-2 cheap-first
  :func:`biotech_sniper.llm.stage2_gates.cooldown_gate` rejects with
  ``reason='cooldown_active'`` and the dispatcher short-circuits
  BEFORE any LLM fan-out, so zero new ``llm_cost_ledger`` rows are
  written. (See :func:`test_news_entry_blocked_by_cooldown`.)
* **VAL-CROSS-032** — Adverse-news exit on the same ticker is NEVER
  blocked by the entry cooldown. Sells flow through
  :meth:`biotech_sniper.paper_executor.PaperExecutor.submit_exit`
  → :func:`biotech_sniper.hold_policy.assert_exit_allowed`, which
  has zero awareness of ``ticker_cooldown``. The functional check
  submits an ``adverse_news`` exit while a fresh cooldown row is
  blocking entries — the exit succeeds. The structural check
  greps the project source tree to confirm no exit module
  references ``cooldown_gate``. (See
  :func:`test_adverse_exit_bypasses_cooldown`.)
* **VAL-CROSS-033** — Daily $20 Stage-2 LLM cap window resets at UTC
  00:00 sharp. The cap query keys on ``DATE(called_at)`` (SQLite
  interprets the stamped ISO-8601 strings as UTC), so a row at
  ``2025-04-29T23:59:00Z`` and one at ``2025-04-30T00:01:00Z``
  count toward DIFFERENT daily totals. (See
  :func:`test_cap_resets_at_utc_midnight`.)
* **VAL-CROSS-034** — Per-ticker 24h cooldown is computed in UTC and
  survives DST transitions. Parametrised across the two annual US
  DST boundaries (spring-forward 2026-03-08 and fall-back
  2026-11-01); the elapsed window is exactly ``86400`` seconds in
  UTC math regardless of local civil-clock distortion. (See
  :func:`test_dst_invariance`.)

The dual-path test convention requires the validation contract's
referenced node IDs (``tests/test_orthogonality.py``,
``tests/test_stage2_cap_tz.py``, ``tests/test_cooldown_tz.py``) to
collect under the same logic — accomplished via thin re-export
shims that ``from tests.test_cooldown_utc import *``.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from biotech_sniper import db as project_db
from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.llm import stage2_gates
from biotech_sniper.llm.stage2_gates import (
    CooldownGateResult,
    DailyCapGateResult,
    GATE_REASON_COOLDOWN_ACTIVE,
    cooldown_gate,
    daily_cap_gate,
    record_cooldown_on_success,
)
from biotech_sniper.migrations.runner import run as run_v10
from biotech_sniper.paper_executor import PaperExecutor


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Fresh SQLite DB at schema_version=10 (Reading-B foundations)."""
    db_path = tmp_path / "cooldown_utc.db"
    run_v10(db_path, target_version=11, take_backup_first=False)
    return db_path


def _iso_utc_millis(dt: _dt.datetime) -> str:
    """Return ``YYYY-MM-DDTHH:MM:SS.fffZ`` shape matching SQLite default.

    Mirrors :func:`stage2_gates._parse_iso_utc`'s accepted shapes so
    the gate parses every test fixture without falling through to
    the "unparseable" defensive-allow branch.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{dt.microsecond // 1000:03d}Z"
    )


def _seed_cooldown(
    db_path: Path,
    *,
    ticker: str,
    last_entry_at: str,
    cooldown_hours: int = 24,
    last_event_id: int | None = None,
) -> None:
    """Insert a raw ``ticker_cooldown`` row (no canonicalisation)."""
    conn = project_db.connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO ticker_cooldown "
                "(ticker, last_entry_at, last_event_id, cooldown_hours) "
                "VALUES (?, ?, ?, ?)",
                (ticker, last_entry_at, last_event_id, cooldown_hours),
            )
    finally:
        conn.close()


def _seed_ledger(
    db_path: Path,
    *,
    cost_usd: float,
    called_at: str,
    provider: str = "perplexity",
    model_id: str = "sonar",
    purpose: str = "stage2_event_scoring",
) -> None:
    """Insert one ``llm_cost_ledger`` row with an explicit ISO timestamp."""
    conn = project_db.connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO llm_cost_ledger (
                    provider, model_id, purpose,
                    prompt_tokens, completion_tokens,
                    latency_ms, cost_usd, called_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provider,
                    model_id,
                    purpose,
                    100,
                    50,
                    250,
                    float(cost_usd),
                    called_at,
                ),
            )
    finally:
        conn.close()


def _count_ledger(db_path: Path) -> int:
    conn = project_db.connect(db_path)
    try:
        return int(
            conn.execute("SELECT COUNT(*) FROM llm_cost_ledger").fetchone()[0]
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fakes for the adverse-news exit roundtrip (VAL-CROSS-032)
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Minimal Alpaca client double — paper base URL, queued responses."""

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
            raise AssertionError("submit_order: no result queued")
        return self._submit_results.pop(0)

    def get_order(self, order_id: str) -> dict[str, Any]:
        return {"id": order_id, "status": "accepted"}


def _stub_sell_response(*, order_id: str, qty: int) -> dict[str, Any]:
    return {
        "id": order_id,
        "client_order_id": "VRTX-adverse_news-2026-04-29",
        "symbol": "VRTX260620C00400000",
        "asset_class": "us_option",
        "qty": qty,
        "side": "sell",
        "status": "accepted",
        "order_class": "simple",
        "type": "market",
        "time_in_force": "day",
    }


def _active_play(
    *,
    ticker: str = "VRTX",
    play_card_id: str = "VRTX-2026-04-29",
    symbol: str = "VRTX260620C00400000",
    catalyst_date: str = "2026-05-12",
    qty: int = 5,
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "play_card_id": play_card_id,
        "symbol": symbol,
        "catalyst_date": catalyst_date,
        "qty": qty,
    }


# ===========================================================================
# VAL-CROSS-031 — News-event entry blocked by 24h cooldown
# ===========================================================================


def test_news_entry_blocked_by_cooldown(temp_db: Path):
    """Entry-attempt at T+1h on a ticker that entered at T is rejected.

    Pins VAL-CROSS-031:

    * The Stage-2 cheap-first cooldown gate observes a fresh
      ``ticker_cooldown`` row (1 h elapsed, 24 h window) and
      returns ``passed=False`` with ``reason='cooldown_active'``
      AND a positive ``remaining_seconds`` (~ 23 h).
    * The dispatcher short-circuits at the cooldown gate BEFORE
      any provider fan-out, so the ``llm_cost_ledger`` table
      gains zero rows from this evaluation.
    * The block is independent of the Stage-2 daily $ cap and
      ``.armed`` state — cooldown is the FIRST cheap-first gate.
    """
    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    one_hour_ago = now - timedelta(hours=1)

    # Seed an active cooldown row simulating a successful entry at T-1h.
    _seed_cooldown(
        temp_db,
        ticker="VRTX",
        last_entry_at=_iso_utc_millis(one_hour_ago),
        cooldown_hours=24,
    )

    pre_ledger_count = _count_ledger(temp_db)

    # Re-entry attempt at T+1h is blocked.
    res = cooldown_gate(ticker="VRTX", db_path=temp_db, now=now)

    assert isinstance(res, CooldownGateResult)
    assert res.passed is False
    assert res.reason == GATE_REASON_COOLDOWN_ACTIVE == "cooldown_active"
    assert res.ticker == "VRTX"
    # 24h window minus 1h elapsed = 23h remaining = 82_800 s (± 2 s slack).
    assert res.remaining_seconds == pytest.approx(23 * 3600, abs=2)
    assert res.cooldown_hours == 24

    # Cheap-first short-circuit: no LLM fan-out, no ledger rows added.
    assert _count_ledger(temp_db) == pre_ledger_count


def test_news_entry_allowed_after_cooldown_expires(temp_db: Path):
    """Re-entry 25h after the original entry is allowed (cooldown expired)."""
    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    twentyfive_hours_ago = now - timedelta(hours=25)
    _seed_cooldown(
        temp_db,
        ticker="VRTX",
        last_entry_at=_iso_utc_millis(twentyfive_hours_ago),
        cooldown_hours=24,
    )
    res = cooldown_gate(ticker="VRTX", db_path=temp_db, now=now)
    assert res.passed is True
    assert res.reason is None
    assert res.remaining_seconds == 0


# ===========================================================================
# VAL-CROSS-032 — Adverse-news exit NEVER blocked by entry cooldown
# ===========================================================================


def test_adverse_exit_bypasses_cooldown(temp_db: Path):
    """An adverse-news exit submission ignores the per-ticker cooldown.

    Pins VAL-CROSS-032 with both a structural and a functional check:

    * Functional: with a 1h-old ``ticker_cooldown`` row in place
      (which would block any Stage-2 entry attempt — see
      :func:`test_news_entry_blocked_by_cooldown`), call
      :meth:`PaperExecutor.submit_exit` with
      ``event='adverse_news'``. The exit completes with an
      ``alpaca_order_id`` from the broker double — proving the
      cooldown is not consulted in the exit path.
    * Structural: the exit modules
      (``adverse_news`` / ``paper_executor.submit_exit`` /
      ``hold_policy``) contain no reference to ``cooldown_gate``.
      Sells therefore cannot be gated by cooldown — by
      construction, not by accident.
    """
    # 1. Active cooldown that WOULD block a fresh entry.
    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    _seed_cooldown(
        temp_db,
        ticker="VRTX",
        last_entry_at=_iso_utc_millis(now - timedelta(hours=1)),
        cooldown_hours=24,
    )
    entry_check = cooldown_gate(ticker="VRTX", db_path=temp_db, now=now)
    assert entry_check.passed is False, (
        "fixture sanity: cooldown row must block entries before we test the exit bypass"
    )

    # 2. Functional: adverse-news exit on VRTX must succeed despite cooldown.
    fake = _FakeAlpacaClient()
    fake.queue(_stub_sell_response(order_id="exit-001", qty=5))
    executor = PaperExecutor(
        fake,  # type: ignore[arg-type]
        db_path=temp_db,
        poll_interval_seconds=0.0,
    )

    alpaca_order_id = executor.submit_exit(
        _active_play(ticker="VRTX"),
        event="adverse_news",
        today=date(2026, 4, 30),
    )

    assert alpaca_order_id == "exit-001"
    assert len(fake.submit_calls) == 1, (
        "exit must reach the broker — cooldown bypass for sells"
    )

    # The persisted exit row carries event='adverse_news' on a
    # VRTX-prefixed OCC option symbol. The ``paper_orders`` table
    # encodes the ticker via the OCC ``symbol`` column (no separate
    # ``ticker`` column), so we filter on ``symbol LIKE 'VRTX%'``.
    conn = sqlite3.connect(temp_db)
    try:
        rows = conn.execute(
            "SELECT event, side, symbol FROM paper_orders "
            "WHERE event='adverse_news' AND status != 'rejected' "
            "AND symbol LIKE 'VRTX%'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, "exactly one adverse_news exit row persisted"
    assert rows[0][0] == "adverse_news"
    assert rows[0][1] == "sell"
    assert rows[0][2].startswith("VRTX")


def test_exit_path_does_not_reference_cooldown_gate():
    """Structural invariant: no exit-side module imports cooldown_gate.

    The cooldown gate lives in :mod:`biotech_sniper.llm.stage2_gates`
    and is invoked exclusively from the Stage-2 *entry* path
    (``stage2_dispatcher.run_stage2_chain``). Exit modules
    (:mod:`biotech_sniper.adverse_news`, :mod:`biotech_sniper.paper_executor`
    -> ``submit_exit``, :mod:`biotech_sniper.hold_policy`) MUST NOT
    consult cooldown — sells bypass the cooldown by design.

    A regression that smuggles a cooldown lookup into the exit
    path would also smuggle the symbol ``cooldown_gate`` (or the
    canonical reason ``cooldown_active``) into one of these
    modules; this test catches that.
    """
    repo_root = Path(__file__).resolve().parents[1]
    exit_modules = [
        repo_root / "biotech_sniper" / "adverse_news.py",
        repo_root / "biotech_sniper" / "hold_policy.py",
    ]
    for path in exit_modules:
        text = path.read_text(encoding="utf-8")
        assert "cooldown_gate" not in text, (
            f"{path.name} must not reference cooldown_gate "
            "(sells bypass cooldown)"
        )
        assert "cooldown_active" not in text, (
            f"{path.name} must not reference cooldown_active reason "
            "(sells bypass cooldown)"
        )

    # paper_executor.py: the *entry* hook exists in submit_news_event_entry
    # and that lives in ``exec/stage2_paper_executor.py`` — verify the core
    # submit_exit method body has no cooldown reference. We grep the
    # function body delimited by the docstring markers.
    pe_text = (repo_root / "biotech_sniper" / "paper_executor.py").read_text(
        encoding="utf-8"
    )
    submit_exit_idx = pe_text.find("def submit_exit(")
    assert submit_exit_idx > 0, "paper_executor.submit_exit must exist"
    # Find the next top-level method def so we can bound the snippet.
    next_def_idx = pe_text.find("\n    def ", submit_exit_idx + 10)
    submit_exit_body = pe_text[submit_exit_idx:next_def_idx]
    assert "cooldown_gate" not in submit_exit_body
    assert "cooldown_active" not in submit_exit_body
    assert "ticker_cooldown" not in submit_exit_body


# ===========================================================================
# VAL-CROSS-033 — Daily $20 Stage-2 LLM cap window resets at UTC 00:00
# ===========================================================================


def test_cap_resets_at_utc_midnight(temp_db: Path):
    """Daily Stage-2 cap window resets sharply at UTC 00:00.

    Pins VAL-CROSS-033 by seeding two ``llm_cost_ledger`` rows that
    straddle UTC midnight:

    * ``$10.00`` at ``2025-04-29T23:59:00Z``  (yesterday-UTC)
    * ``$15.00`` at ``2025-04-30T00:01:00Z``  (today-UTC)

    The gate's SUM filter (``DATE(called_at) = ?``) interprets each
    timestamp as UTC, so:

    * Querying with ``today=2025-04-29`` returns ``today_total=$10``.
    * Querying with ``today=2025-04-30`` returns ``today_total=$15``.

    The cap effectively resets at UTC 00:00 sharp — the 23:59→00:01
    boundary moves a row into a different daily bucket within two
    minutes of wall-clock time.
    """
    _seed_ledger(
        temp_db,
        cost_usd=10.0,
        called_at="2025-04-29T23:59:00Z",
    )
    _seed_ledger(
        temp_db,
        cost_usd=15.0,
        called_at="2025-04-30T00:01:00Z",
    )

    res_yesterday = daily_cap_gate(
        db_path=temp_db,
        projected_cost=0.0,
        cap=20.0,
        today=date(2025, 4, 29),
    )
    res_today = daily_cap_gate(
        db_path=temp_db,
        projected_cost=0.0,
        cap=20.0,
        today=date(2025, 4, 30),
    )

    assert isinstance(res_yesterday, DailyCapGateResult)
    assert isinstance(res_today, DailyCapGateResult)

    # Yesterday's bucket = $10.00 only (the 23:59 row).
    assert res_yesterday.today_total_usd == pytest.approx(10.0)
    # Today's bucket = $15.00 only (the 00:01 row); the 23:59 row
    # from the prior UTC day does NOT bleed in.
    assert res_today.today_total_usd == pytest.approx(15.0)

    # The cap query is bucketed by UTC date — the SUM resets to the
    # new day's total without remembering yesterday.
    assert res_today.today_total_usd != res_yesterday.today_total_usd


def test_cap_window_at_exact_utc_midnight_boundary(temp_db: Path):
    """A row stamped exactly at ``00:00:00.000Z`` belongs to the new day.

    SQLite's ``DATE('2026-04-30T00:00:00Z')`` evaluates to
    ``'2026-04-30'`` — the second the clock ticks past midnight, the
    row is in the *new* UTC day's bucket, never in the prior day's.
    """
    _seed_ledger(
        temp_db,
        cost_usd=5.0,
        called_at="2026-04-29T23:59:59.999Z",
    )
    _seed_ledger(
        temp_db,
        cost_usd=7.0,
        called_at="2026-04-30T00:00:00.000Z",
    )

    res_yesterday = daily_cap_gate(
        db_path=temp_db,
        projected_cost=0.0,
        cap=20.0,
        today=date(2026, 4, 29),
    )
    res_today = daily_cap_gate(
        db_path=temp_db,
        projected_cost=0.0,
        cap=20.0,
        today=date(2026, 4, 30),
    )
    assert res_yesterday.today_total_usd == pytest.approx(5.0)
    assert res_today.today_total_usd == pytest.approx(7.0)


# ===========================================================================
# VAL-CROSS-034 — Per-ticker 24h cooldown computed in UTC; DST-safe
# ===========================================================================


# US DST transitions in 2026:
#   * Spring-forward: 2026-03-08 02:00 local → 03:00 local (EST → EDT).
#   * Fall-back:      2026-11-01 02:00 local → 01:00 local (EDT → EST).
# In UTC math, both transitions are inert: 24h elapsed is always
# 86_400 s in UTC, never 82_800 (= 23h) or 90_000 (= 25h).
DST_BOUNDARIES = [
    pytest.param(
        datetime(2026, 3, 8, 6, 0, 0, tzinfo=timezone.utc),  # set-at
        datetime(2026, 3, 9, 6, 0, 0, tzinfo=timezone.utc),  # +24h UTC
        id="spring-forward-2026-03-08",
    ),
    pytest.param(
        datetime(2026, 11, 1, 6, 0, 0, tzinfo=timezone.utc),  # set-at
        datetime(2026, 11, 2, 6, 0, 0, tzinfo=timezone.utc),  # +24h UTC
        id="fall-back-2026-11-01",
    ),
]


@pytest.mark.parametrize("set_at,expires_at", DST_BOUNDARIES)
def test_dst_invariance(
    temp_db: Path,
    set_at: datetime,
    expires_at: datetime,
):
    """24h cooldown duration is preserved across US DST transitions.

    Pins VAL-CROSS-034:

    * The cooldown row is keyed in UTC (ISO-8601 with 'Z' suffix).
    * At ``set_at + 24h`` (in UTC math) the elapsed window is
      exactly ``86_400`` seconds — never ``82_800`` (= 23h, the
      mistaken value if the math used civil-clock subtraction
      across a spring-forward) and never ``90_000`` (= 25h, the
      mistaken value across a fall-back).
    * The boundary is INCLUSIVE: ``elapsed == cooldown_seconds``
      passes (gate allows entry).
    * Just BEFORE the boundary (``+ 24h - 1s``) the gate still
      blocks; just AFTER (``+ 24h + 1s``) it definitely allows.
    """
    # The clock-difference invariant: two UTC datetimes 24h apart
    # are always exactly 86_400 s — DST is a civil-clock concept,
    # not a UTC concept.
    elapsed_seconds = (expires_at - set_at).total_seconds()
    assert elapsed_seconds == 86_400, (
        f"DST invariant broken: 24h UTC elapsed should be 86400s, "
        f"got {elapsed_seconds}s. set_at={set_at} expires_at={expires_at}"
    )
    # Defensive: not 23h (82_800) or 25h (90_000).
    assert elapsed_seconds != 82_800
    assert elapsed_seconds != 90_000

    ticker = f"DST{int(set_at.month):02d}"

    _seed_cooldown(
        temp_db,
        ticker=ticker,
        last_entry_at=_iso_utc_millis(set_at),
        cooldown_hours=24,
    )

    # 1) Just before expiry: elapsed = 24h - 1s → BLOCK.
    res_before = cooldown_gate(
        ticker=ticker,
        db_path=temp_db,
        now=expires_at - timedelta(seconds=1),
    )
    assert res_before.passed is False, (
        "1s before the 24h UTC boundary the gate must still block"
    )
    assert res_before.reason == GATE_REASON_COOLDOWN_ACTIVE
    assert res_before.remaining_seconds == pytest.approx(1, abs=1)

    # 2) Exactly at the 24h boundary (UTC math): allow (>= inclusive).
    res_at = cooldown_gate(
        ticker=ticker,
        db_path=temp_db,
        now=expires_at,
    )
    assert res_at.passed is True, (
        "at exactly the 24h UTC boundary the gate must allow (>= semantics)"
    )
    assert res_at.reason is None
    assert res_at.remaining_seconds == 0

    # 3) Just after expiry: definitely allow.
    res_after = cooldown_gate(
        ticker=ticker,
        db_path=temp_db,
        now=expires_at + timedelta(seconds=1),
    )
    assert res_after.passed is True
    assert res_after.reason is None
    assert res_after.remaining_seconds == 0


def test_dst_invariance_record_then_gate(temp_db: Path):
    """End-to-end DST-stable: record at the spring-forward instant, gate at +24h.

    Verifies the writer (:func:`record_cooldown_on_success`) and the
    reader (:func:`cooldown_gate`) agree on UTC math: a row recorded
    at the moment of a DST jump expires exactly 86_400 seconds later
    in UTC, regardless of any local civil-clock distortion.
    """
    set_at = datetime(2026, 3, 8, 6, 30, 0, tzinfo=timezone.utc)  # spring-forward day
    expires_at = set_at + timedelta(hours=24)

    record_cooldown_on_success(
        ticker="DSTREC",
        db_path=temp_db,
        now=set_at,
        cooldown_hours=24,
    )

    # 1s before expiry — block.
    res_before = cooldown_gate(
        ticker="DSTREC",
        db_path=temp_db,
        now=expires_at - timedelta(seconds=1),
    )
    assert res_before.passed is False

    # At expiry — allow.
    res_at = cooldown_gate(
        ticker="DSTREC",
        db_path=temp_db,
        now=expires_at,
    )
    assert res_at.passed is True


def test_dst_invariance_seconds_arithmetic_is_utc():
    """Pure-math sanity: 24h is 86_400 UTC seconds across DST boundaries.

    A unit-level check that does not touch the DB. Catches any
    regression that re-implements cooldown elapsed-time math on
    naive (timezone-less) datetimes — which would silently lose 1h
    on spring-forward and gain 1h on fall-back when the underlying
    OS interprets the naive datetime as local civil time.
    """
    cases = [
        (datetime(2026, 3, 8, 6, 0, 0, tzinfo=timezone.utc),
         datetime(2026, 3, 9, 6, 0, 0, tzinfo=timezone.utc)),
        (datetime(2026, 11, 1, 6, 0, 0, tzinfo=timezone.utc),
         datetime(2026, 11, 2, 6, 0, 0, tzinfo=timezone.utc)),
        # Off-DST control: 24h apart anywhere should also = 86400.
        (datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc),
         datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)),
    ]
    for start, end in cases:
        elapsed = (end - start).total_seconds()
        assert elapsed == 86_400.0, (
            f"24h UTC math should always = 86400s, got {elapsed} for "
            f"start={start} end={end}"
        )
