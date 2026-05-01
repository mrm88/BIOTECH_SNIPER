"""Tests for :mod:`biotech_sniper.performance_ledger` rollup queries.

Validation contract coverage:

* **VAL-M5-032** — ``performance_ledger.roll_up_day(date, event_filter)``
  returns Reading-B-only rows when ``event_filter='news_event_entry'``,
  and daily-curated-only rows when ``event_filter='open'``. Each
  filter returns the play that was synthesised for that event.
* **VAL-M5-033** — Sum of realized P&L across the two event filters
  equals the union sum within float tolerance — i.e. the partition
  is complete (no double-counting, no missing fills).
* **VAL-M5-034** — ``paper_orders.event`` CHECK constraint is EXTENDED
  (not replaced) by the v10 migration — ``'open'``,
  ``'news_event_entry'``, and the prior exit values are all accepted;
  unknown values raise ``CHECK constraint failed``.
* **VAL-M5-048** — A ``news_event_entry`` parent + its exits roll up
  to the SAME ledger play with ``event_path='news_event_entry'``.
  Exit fills attribute back to the same play_id; 100% of P&L falls
  into the ``news_event_entry`` bucket.

Tests are hermetic — every test uses a fresh sqlite db at
``tmp_path`` migrated up to v10 via the migration runner. No
network, no real Alpaca calls.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from biotech_sniper import performance_ledger
from biotech_sniper import db as project_db
from biotech_sniper.migrations.runner import run as run_migrations_runner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Bring a fresh sqlite db up to schema v10 (Reading-B foundations)."""
    db = tmp_path / "alpha_sniper_perf_ledger.db"
    run_migrations_runner(db, target_version=project_db.CURRENT_VERSION, take_backup_first=False)
    return db


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


def _insert_paper_order(
    db_path: Path,
    *,
    paper_order_id: str,
    play_card_id: str | None,
    parent_play_card_id: str | None,
    event: str | None,
    purpose: str | None,
    side: str,
    qty: int,
    requested_mid_at_submit: float,
    created_at: str,
    symbol: str = "VKTX260619C00060000",
    client_order_id: str | None = None,
) -> None:
    """Insert one paper_orders row for a hermetic rollup test."""
    coid = client_order_id or f"client-{paper_order_id}"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO paper_orders (
                id, play_card_id, alpaca_order_id, symbol, side, qty,
                status, reason, event, parent_play_card_id,
                requested_mid_at_submit, purpose, client_order_id,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                paper_order_id,
                play_card_id,
                f"alp-{paper_order_id}",
                symbol,
                side,
                qty,
                "filled",
                None,
                event,
                parent_play_card_id,
                requested_mid_at_submit,
                purpose,
                coid,
                created_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_fill(
    db_path: Path,
    *,
    paper_order_id: str,
    filled_at: str,
    filled_price: float,
    filled_qty: int,
    requested_mid_at_submit: float,
) -> None:
    """Insert a hermetic execution_fills row.

    The slippage / time fields are stub-zero — these tests verify
    rollup math, not slippage. The schema requires NOT NULL on
    every column, so we provide defensible zero values.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO execution_fills (
                paper_order_id, filled_at, filled_price, filled_qty,
                requested_mid_at_submit, slippage_bps, slippage_usd,
                time_to_fill_ms, partial_qty_remaining
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                paper_order_id,
                filled_at,
                filled_price,
                filled_qty,
                requested_mid_at_submit,
                0.0,
                0.0,
                0,
                0,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_play(
    db_path: Path,
    *,
    event: str,
    play_card_id: str,
    entry_price: float,
    exit_price: float,
    qty: int,
    entry_date: str,
    exit_date: str,
    symbol: str = "VKTX260619C00060000",
) -> None:
    """Seed one full play (entry buy + exit sell) for a single event_path.

    Records both ``paper_orders`` rows AND their ``execution_fills``
    so the rollup query has a complete cash-flow picture.
    """
    entry_oid = f"po-entry-{play_card_id}"
    exit_oid = f"po-exit-{play_card_id}"

    # Entry order: buy at entry_price.
    _insert_paper_order(
        db_path,
        paper_order_id=entry_oid,
        play_card_id=play_card_id,
        parent_play_card_id=None,
        event=event,
        purpose="entry",
        side="buy",
        qty=qty,
        requested_mid_at_submit=entry_price,
        created_at=f"{entry_date}T15:30:00.000Z",
        symbol=symbol,
    )
    _insert_fill(
        db_path,
        paper_order_id=entry_oid,
        filled_at=f"{entry_date}T15:30:01.000Z",
        filled_price=entry_price,
        filled_qty=qty,
        requested_mid_at_submit=entry_price,
    )

    # Exit order: sell at exit_price (same day for the partition tests).
    _insert_paper_order(
        db_path,
        paper_order_id=exit_oid,
        play_card_id=f"{play_card_id}-exit",
        parent_play_card_id=play_card_id,
        event="iv_crush_exit",
        purpose="exit",
        side="sell",
        qty=qty,
        requested_mid_at_submit=exit_price,
        created_at=f"{exit_date}T15:45:00.000Z",
        symbol=symbol,
    )
    _insert_fill(
        db_path,
        paper_order_id=exit_oid,
        filled_at=f"{exit_date}T15:45:01.000Z",
        filled_price=exit_price,
        filled_qty=qty,
        requested_mid_at_submit=exit_price,
    )


# ---------------------------------------------------------------------------
# VAL-M5-034 — paper_orders.event CHECK extended (not replaced)
# ---------------------------------------------------------------------------


def test_paper_orders_event_check_accepts_news_event_entry(db_path: Path) -> None:
    """A direct insert with ``event='news_event_entry'`` succeeds — the
    v10 migration extended the CHECK enum to include the new value."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO paper_orders (
                id, side, qty, status, event, purpose, client_order_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "po-news-1",
                "buy",
                2,
                "filled",
                "news_event_entry",
                "entry",
                "client-news-1",
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT event FROM paper_orders WHERE id = 'po-news-1'"
        ).fetchone()
        assert row is not None and row[0] == "news_event_entry"
    finally:
        conn.close()


def test_paper_orders_event_check_retains_open(db_path: Path) -> None:
    """Existing ``event='open'`` value is still accepted — the migration
    EXTENDS the CHECK rather than replacing it."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO paper_orders (
                id, side, qty, status, event, purpose, client_order_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "po-open-1",
                "buy",
                2,
                "filled",
                "open",
                "entry",
                "client-open-1",
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT event FROM paper_orders WHERE id = 'po-open-1'"
        ).fetchone()
        assert row is not None and row[0] == "open"
    finally:
        conn.close()


def test_paper_orders_event_check_retains_legacy_exit_events(
    db_path: Path,
) -> None:
    """Each pre-existing exit event token (iv_crush_exit, stop_loss,
    adverse_news, rotation) is still accepted post-migration."""
    legacy_events = (
        "iv_crush_exit",
        "stop_loss",
        "adverse_news",
        "rotation",
    )
    conn = sqlite3.connect(db_path)
    try:
        for ev in legacy_events:
            conn.execute(
                """
                INSERT INTO paper_orders (
                    id, side, qty, status, event, purpose, client_order_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"po-legacy-{ev}",
                    "sell",
                    2,
                    "filled",
                    ev,
                    "exit",
                    f"client-legacy-{ev}",
                ),
            )
        conn.commit()
        rows = {
            r[0]
            for r in conn.execute(
                "SELECT event FROM paper_orders WHERE id LIKE 'po-legacy-%'"
            ).fetchall()
        }
        assert rows == set(legacy_events)
    finally:
        conn.close()


def test_paper_orders_event_check_rejects_bogus(db_path: Path) -> None:
    """An unknown ``event`` value raises ``CHECK constraint failed``."""
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError) as ei:
            conn.execute(
                """
                INSERT INTO paper_orders (
                    id, side, qty, status, event, purpose, client_order_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "po-bogus-1",
                    "buy",
                    2,
                    "filled",
                    "bogus_event",
                    "entry",
                    "client-bogus-1",
                ),
            )
            conn.commit()
        assert "CHECK constraint" in str(ei.value)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# VAL-M5-032 — event_filter isolates Reading-B vs daily-curated rows
# ---------------------------------------------------------------------------


def test_event_filter_isolates_news_event_entry(db_path: Path) -> None:
    """Two plays — one ``open`` (daily-curated) and one
    ``news_event_entry`` (Reading-B) — both fill on the same day.
    ``roll_up_day(d, event_filter='news_event_entry')`` returns ONLY
    the Reading-B play; ``event_filter='open'`` returns ONLY the
    daily-curated play. Every filter result is internally consistent
    (1 play, 1 entry + 1 exit attributed to the same play_id).
    """
    as_of_date = "2030-05-15"

    # Daily-curated play — buy @ 1.00 → sell @ 1.50  (P&L = +$0.50/share)
    _seed_play(
        db_path,
        event="open",
        play_card_id="PC-DAILY-1",
        entry_price=1.00,
        exit_price=1.50,
        qty=3,
        entry_date=as_of_date,
        exit_date=as_of_date,
        symbol="ABCD260619C00010000",
    )

    # Reading-B play — buy @ 2.00 → sell @ 1.20  (P&L = -$0.80/share)
    _seed_play(
        db_path,
        event="news_event_entry",
        play_card_id="PC-NEWS-1",
        entry_price=2.00,
        exit_price=1.20,
        qty=2,
        entry_date=as_of_date,
        exit_date=as_of_date,
        symbol="EFGH260619C00020000",
    )

    news_only = performance_ledger.roll_up_day(
        as_of_date, event_filter="news_event_entry", db_path=db_path
    )
    daily_only = performance_ledger.roll_up_day(
        as_of_date, event_filter="open", db_path=db_path
    )

    assert news_only.play_count == 1
    assert daily_only.play_count == 1
    news_play_ids = {p.play_id for p in news_only.plays}
    daily_play_ids = {p.play_id for p in daily_only.plays}
    assert news_play_ids == {"PC-NEWS-1"}
    assert daily_play_ids == {"PC-DAILY-1"}
    # Reading-B realized P&L = (1.20 - 2.00) * 2 * 100 = -160.00
    assert news_only.realized_pnl == pytest.approx(-160.00, abs=1e-6)
    # Daily-curated realized P&L = (1.50 - 1.00) * 3 * 100 = +150.00
    assert daily_only.realized_pnl == pytest.approx(150.00, abs=1e-6)
    # No cross-contamination: each filter's plays carry the right
    # event_path label.
    assert all(p.event_path == "news_event_entry" for p in news_only.plays)
    assert all(p.event_path == "open" for p in daily_only.plays)


# ---------------------------------------------------------------------------
# VAL-M5-033 — partition equality (no double-count, no miss)
# ---------------------------------------------------------------------------


def test_event_filter_partition_is_complete(db_path: Path) -> None:
    """``sum(roll_up_day('open')) + sum(roll_up_day('news_event_entry'))
    == sum(roll_up_day(event_filter=None))`` within $0.01 tolerance.
    Demonstrates the partition has no double-counting and no missing
    fills (every fill is attributed to exactly one event_path).
    """
    as_of_date = "2030-05-15"
    # Daily-curated play
    _seed_play(
        db_path,
        event="open",
        play_card_id="PC-DAILY-1",
        entry_price=1.00,
        exit_price=1.50,
        qty=3,
        entry_date=as_of_date,
        exit_date=as_of_date,
        symbol="ABCD260619C00010000",
    )
    # Reading-B play
    _seed_play(
        db_path,
        event="news_event_entry",
        play_card_id="PC-NEWS-1",
        entry_price=2.00,
        exit_price=1.20,
        qty=2,
        entry_date=as_of_date,
        exit_date=as_of_date,
        symbol="EFGH260619C00020000",
    )

    union = performance_ledger.roll_up_day(
        as_of_date, event_filter=None, db_path=db_path
    )
    daily = performance_ledger.roll_up_day(
        as_of_date, event_filter="open", db_path=db_path
    )
    news = performance_ledger.roll_up_day(
        as_of_date, event_filter="news_event_entry", db_path=db_path
    )

    assert union.play_count == 2
    assert union.realized_pnl == pytest.approx(
        daily.realized_pnl + news.realized_pnl, abs=0.01
    )
    # Expected: -160 + 150 = -10.00 union total.
    assert union.realized_pnl == pytest.approx(-10.00, abs=1e-6)


# ---------------------------------------------------------------------------
# VAL-M5-048 — news_event_entry parent + multiple exits roll up to same play
# ---------------------------------------------------------------------------


def test_news_event_entry_with_exits_single_parent(db_path: Path) -> None:
    """A ``news_event_entry`` entry that fills then exits via multiple
    exit events ({iv_crush_exit, stop_loss}) produces exactly ONE
    play row with ``event_path='news_event_entry'``. Every exit fill
    links back to the same play_id; 100% of P&L falls into the
    Reading-B bucket.
    """
    as_of_date = "2030-05-15"
    play_card_id = "PC-NEWS-PARENT-1"
    symbol = "JJJJ260619C00050000"

    # Single entry (buy 4 @ 1.00).
    _insert_paper_order(
        db_path,
        paper_order_id=f"po-entry-{play_card_id}",
        play_card_id=play_card_id,
        parent_play_card_id=None,
        event="news_event_entry",
        purpose="entry",
        side="buy",
        qty=4,
        requested_mid_at_submit=1.00,
        created_at=f"{as_of_date}T15:30:00.000Z",
        symbol=symbol,
    )
    _insert_fill(
        db_path,
        paper_order_id=f"po-entry-{play_card_id}",
        filled_at=f"{as_of_date}T15:30:01.000Z",
        filled_price=1.00,
        filled_qty=4,
        requested_mid_at_submit=1.00,
    )

    # Two distinct exit orders against the same parent.
    exits = (
        ("iv_crush_exit", 2, 1.40),
        ("stop_loss", 2, 0.80),
    )
    for idx, (exit_event, exit_qty, exit_price) in enumerate(exits):
        exit_oid = f"po-exit-{idx}-{play_card_id}"
        _insert_paper_order(
            db_path,
            paper_order_id=exit_oid,
            play_card_id=f"{play_card_id}-exit-{idx}",
            parent_play_card_id=play_card_id,
            event=exit_event,
            purpose="exit",
            side="sell",
            qty=exit_qty,
            requested_mid_at_submit=exit_price,
            created_at=f"{as_of_date}T16:0{idx}:00.000Z",
            symbol=symbol,
        )
        _insert_fill(
            db_path,
            paper_order_id=exit_oid,
            filled_at=f"{as_of_date}T16:0{idx}:01.000Z",
            filled_price=exit_price,
            filled_qty=exit_qty,
            requested_mid_at_submit=exit_price,
        )

    news = performance_ledger.roll_up_day(
        as_of_date, event_filter="news_event_entry", db_path=db_path
    )

    # All exits roll up to the SAME parent play.
    assert news.play_count == 1
    assert {p.play_id for p in news.plays} == {play_card_id}
    assert {p.event_path for p in news.plays} == {"news_event_entry"}

    # 100% of P&L bucketed under news_event_entry. No leak into
    # iv_crush_exit / stop_loss buckets.
    iv_crush_only = performance_ledger.roll_up_day(
        as_of_date, event_filter="iv_crush_exit", db_path=db_path
    )
    stop_loss_only = performance_ledger.roll_up_day(
        as_of_date, event_filter="stop_loss", db_path=db_path
    )
    assert iv_crush_only.play_count == 0
    assert stop_loss_only.play_count == 0
    assert iv_crush_only.realized_pnl == pytest.approx(0.0, abs=1e-9)
    assert stop_loss_only.realized_pnl == pytest.approx(0.0, abs=1e-9)

    # Total realized P&L:
    #   entry: -1.00 * 4 * 100  = -400.00
    #   exit1: +1.40 * 2 * 100  = +280.00
    #   exit2: +0.80 * 2 * 100  = +160.00
    #   total = +40.00
    assert news.realized_pnl == pytest.approx(40.00, abs=1e-6)

    # Partition still holds: filter=None matches the news bucket
    # (since this play is the only thing seeded today).
    union = performance_ledger.roll_up_day(
        as_of_date, event_filter=None, db_path=db_path
    )
    assert union.realized_pnl == pytest.approx(news.realized_pnl, abs=0.01)
    assert union.play_count == 1


# ---------------------------------------------------------------------------
# Empty-day robustness
# ---------------------------------------------------------------------------


def test_roll_up_day_empty_returns_zero(db_path: Path) -> None:
    """A date with no fills returns a zero-pnl, zero-play result —
    never raises, never returns NaN."""
    result = performance_ledger.roll_up_day(
        "2099-01-01", event_filter=None, db_path=db_path
    )
    assert result.play_count == 0
    assert result.plays == []
    assert result.realized_pnl == pytest.approx(0.0, abs=1e-9)


def test_roll_up_day_accepts_existing_connection(db_path: Path) -> None:
    """Callers may pass an open ``sqlite3.Connection`` instead of a
    db path — useful when composing rollups inside larger queries."""
    as_of_date = "2030-05-15"
    _seed_play(
        db_path,
        event="news_event_entry",
        play_card_id="PC-CONN-1",
        entry_price=1.00,
        exit_price=1.30,
        qty=1,
        entry_date=as_of_date,
        exit_date=as_of_date,
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = performance_ledger.roll_up_day(
            as_of_date, event_filter="news_event_entry", conn=conn
        )
    finally:
        conn.close()
    assert result.play_count == 1
    # +0.30 * 1 * 100 = +30.00
    assert result.realized_pnl == pytest.approx(30.00, abs=1e-6)


# ---------------------------------------------------------------------------
# f-fix-m5-05-rollup-double-count — non-unique play_card_id regression tests
#
# These tests pin VAL-M5-033 against a class of bugs surfaced by cross-flow
# scrutiny round 1: ``paper_orders.play_card_id`` is NOT unique per entry
# order. Multi-strike entries (paper_executor multi-leg path around lines
# 1810-1850) and rotation retries (rotation_engine.py:391-405) can write
# multiple rows that share the same ``play_card_id`` AND
# ``purpose='entry'``. The previous _ROLLUP_SQL_BASE LEFT JOIN
# (``parent.play_card_id = po.parent_play_card_id AND parent.purpose='entry'``)
# matched ALL of those rows, multiplying every exit fill's contribution by
# the number of entry rows sharing the play_card_id — a silent
# double-count. The fix disambiguates the parent join to exactly one row
# per (play_card_id, purpose='entry') by selecting MIN(id).
# ---------------------------------------------------------------------------


def test_rollup_no_double_count_with_rejected_retry(db_path: Path) -> None:
    """Reproduction of the f-fix-m5-05 bug: one filled entry + one
    rejected retry entry sharing the same ``play_card_id`` + one
    exit fill must roll up to the naive sum, NOT double-count the
    exit because two ``purpose='entry'`` rows share the play_card_id.

    Setup:
      * Entry #1 — purpose='entry', status='filled', fills 1 @ $1.00 → -$100
      * Entry #2 — purpose='entry', status='rejected' (no fill row),
        SAME play_card_id (rotation retry pattern from
        rotation_engine.py:391-405).
      * Exit    — purpose='exit', parent_play_card_id=PC, sells
        1 @ $1.50 → +$150

    Naive realized_pnl = -100 + 150 = $50.
    Pre-fix (buggy) realized_pnl = -100 + 2 * 150 = $200 (the exit
    fill is matched against BOTH entry rows in the LEFT JOIN).

    Per VAL-M5-033 the partition must be lossless — neither
    double-counting nor missing fills is acceptable.
    """
    as_of_date = "2030-05-15"
    play_card_id = "PC-NEWS-RETRY-1"
    symbol = "AAAA260619C00010000"

    # Filled entry.
    entry_filled_oid = f"po-entry-filled-{play_card_id}"
    _insert_paper_order(
        db_path,
        paper_order_id=entry_filled_oid,
        play_card_id=play_card_id,
        parent_play_card_id=None,
        event="news_event_entry",
        purpose="entry",
        side="buy",
        qty=1,
        requested_mid_at_submit=1.00,
        created_at=f"{as_of_date}T15:30:00.000Z",
        symbol=symbol,
        client_order_id=f"client-{entry_filled_oid}",
    )
    _insert_fill(
        db_path,
        paper_order_id=entry_filled_oid,
        filled_at=f"{as_of_date}T15:30:01.000Z",
        filled_price=1.00,
        filled_qty=1,
        requested_mid_at_submit=1.00,
    )

    # Rejected retry entry — SAME play_card_id, no fill row.
    # Mirrors the rotation_engine retry pattern that re-uses the
    # parent's play_card_id to record the rejection in
    # paper_orders.
    entry_rejected_oid = f"po-entry-rejected-{play_card_id}"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO paper_orders (
                id, play_card_id, alpaca_order_id, symbol, side, qty,
                status, reason, event, parent_play_card_id,
                requested_mid_at_submit, purpose, client_order_id,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry_rejected_oid,
                play_card_id,
                None,  # rejection — no broker order
                symbol,
                "buy",
                0,  # rejected before sizing
                "rejected",
                "ContractTooExpensive (retry)",
                "news_event_entry",
                None,
                None,
                "entry",
                f"client-{entry_rejected_oid}",
                f"{as_of_date}T15:31:00.000Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # Single exit fill against the parent.
    exit_oid = f"po-exit-{play_card_id}"
    _insert_paper_order(
        db_path,
        paper_order_id=exit_oid,
        play_card_id=f"{play_card_id}-exit",
        parent_play_card_id=play_card_id,
        event="iv_crush_exit",
        purpose="exit",
        side="sell",
        qty=1,
        requested_mid_at_submit=1.50,
        created_at=f"{as_of_date}T16:00:00.000Z",
        symbol=symbol,
        client_order_id=f"client-{exit_oid}",
    )
    _insert_fill(
        db_path,
        paper_order_id=exit_oid,
        filled_at=f"{as_of_date}T16:00:01.000Z",
        filled_price=1.50,
        filled_qty=1,
        requested_mid_at_submit=1.50,
    )

    news = performance_ledger.roll_up_day(
        as_of_date, event_filter="news_event_entry", db_path=db_path
    )

    # Exactly ONE play row, attributed to the parent play_card_id.
    assert news.play_count == 1
    assert {p.play_id for p in news.plays} == {play_card_id}
    assert {p.event_path for p in news.plays} == {"news_event_entry"}

    # Naive arithmetic: -1.00*1*100 + 1.50*1*100 = 50.00.
    # Pre-fix bug yielded 200.00 (the exit was double-counted).
    assert news.realized_pnl == pytest.approx(50.00, abs=1e-6)

    # Fill count: 1 entry fill + 1 exit fill = 2. Pre-fix bug
    # reported 3 because the exit fill was counted twice.
    assert sum(p.fill_count for p in news.plays) == 2

    # Partition holds — union sum equals the news bucket (this is
    # the only play seeded today).
    union = performance_ledger.roll_up_day(
        as_of_date, event_filter=None, db_path=db_path
    )
    assert union.realized_pnl == pytest.approx(50.00, abs=1e-6)
    assert union.play_count == 1


def test_rollup_no_double_count_multi_strike(db_path: Path) -> None:
    """Multi-strike entry plays write TWO ``paper_orders`` rows with
    purpose='entry' that share the SAME ``play_card_id`` (one per
    leg, see paper_executor multi-leg dispatch around lines
    1810-1850). Each leg has its own exit. The rollup must sum each
    leg's pnl exactly once; pre-fix it doubled both exits because
    the parent LEFT JOIN matched both leg rows for each exit.

    Setup (single play_card_id ``PC-MULTI-STRIKE-1``):
      * Leg A entry — buy 1 @ $1.00 → -$100
      * Leg B entry — buy 1 @ $2.00 → -$200
      * Leg A exit  — sell 1 @ $1.50 → +$150
      * Leg B exit  — sell 1 @ $2.50 → +$250

    Naive realized_pnl = -100 - 200 + 150 + 250 = +$100.
    Pre-fix (buggy) realized_pnl = -100 - 200 + 2*150 + 2*250 = +$500.
    """
    as_of_date = "2030-05-15"
    play_card_id = "PC-MULTI-STRIKE-1"
    leg_a_symbol = "ZZZA260619C00010000"
    leg_b_symbol = "ZZZB260619C00020000"

    # Leg A entry + fill.
    entry_a_oid = f"po-entry-A-{play_card_id}"
    _insert_paper_order(
        db_path,
        paper_order_id=entry_a_oid,
        play_card_id=play_card_id,
        parent_play_card_id=None,
        event="news_event_entry",
        purpose="entry",
        side="buy",
        qty=1,
        requested_mid_at_submit=1.00,
        created_at=f"{as_of_date}T15:30:00.000Z",
        symbol=leg_a_symbol,
        client_order_id=f"client-{entry_a_oid}",
    )
    _insert_fill(
        db_path,
        paper_order_id=entry_a_oid,
        filled_at=f"{as_of_date}T15:30:01.000Z",
        filled_price=1.00,
        filled_qty=1,
        requested_mid_at_submit=1.00,
    )

    # Leg B entry + fill — SAME play_card_id, different symbol.
    entry_b_oid = f"po-entry-B-{play_card_id}"
    _insert_paper_order(
        db_path,
        paper_order_id=entry_b_oid,
        play_card_id=play_card_id,
        parent_play_card_id=None,
        event="news_event_entry",
        purpose="entry",
        side="buy",
        qty=1,
        requested_mid_at_submit=2.00,
        created_at=f"{as_of_date}T15:30:02.000Z",
        symbol=leg_b_symbol,
        client_order_id=f"client-{entry_b_oid}",
    )
    _insert_fill(
        db_path,
        paper_order_id=entry_b_oid,
        filled_at=f"{as_of_date}T15:30:03.000Z",
        filled_price=2.00,
        filled_qty=1,
        requested_mid_at_submit=2.00,
    )

    # Leg A exit + fill.
    exit_a_oid = f"po-exit-A-{play_card_id}"
    _insert_paper_order(
        db_path,
        paper_order_id=exit_a_oid,
        play_card_id=f"{play_card_id}-exit-A",
        parent_play_card_id=play_card_id,
        event="iv_crush_exit",
        purpose="exit",
        side="sell",
        qty=1,
        requested_mid_at_submit=1.50,
        created_at=f"{as_of_date}T16:00:00.000Z",
        symbol=leg_a_symbol,
        client_order_id=f"client-{exit_a_oid}",
    )
    _insert_fill(
        db_path,
        paper_order_id=exit_a_oid,
        filled_at=f"{as_of_date}T16:00:01.000Z",
        filled_price=1.50,
        filled_qty=1,
        requested_mid_at_submit=1.50,
    )

    # Leg B exit + fill.
    exit_b_oid = f"po-exit-B-{play_card_id}"
    _insert_paper_order(
        db_path,
        paper_order_id=exit_b_oid,
        play_card_id=f"{play_card_id}-exit-B",
        parent_play_card_id=play_card_id,
        event="iv_crush_exit",
        purpose="exit",
        side="sell",
        qty=1,
        requested_mid_at_submit=2.50,
        created_at=f"{as_of_date}T16:01:00.000Z",
        symbol=leg_b_symbol,
        client_order_id=f"client-{exit_b_oid}",
    )
    _insert_fill(
        db_path,
        paper_order_id=exit_b_oid,
        filled_at=f"{as_of_date}T16:01:01.000Z",
        filled_price=2.50,
        filled_qty=1,
        requested_mid_at_submit=2.50,
    )

    news = performance_ledger.roll_up_day(
        as_of_date, event_filter="news_event_entry", db_path=db_path
    )

    # Exactly one play row (the multi-strike legs collapse to one
    # play_id under the ``COALESCE(parent_play_card_id, play_card_id)``
    # rule).
    assert news.play_count == 1
    assert {p.play_id for p in news.plays} == {play_card_id}
    assert {p.event_path for p in news.plays} == {"news_event_entry"}

    # Naive sum: -100 - 200 + 150 + 250 = +100.
    # Pre-fix bug yielded +500 (each exit double-counted).
    assert news.realized_pnl == pytest.approx(100.00, abs=1e-6)

    # 4 fills total — pre-fix bug reported 6.
    assert sum(p.fill_count for p in news.plays) == 4

    # Partition stays clean.
    union = performance_ledger.roll_up_day(
        as_of_date, event_filter=None, db_path=db_path
    )
    assert union.realized_pnl == pytest.approx(100.00, abs=1e-6)
    assert union.play_count == 1
