"""Reading-B CLI report — final mission-end coverage (f-m5-06).

This module locks down the **final** Reading-B CLI behaviour against
the M5 cross-flow validation contract. It supplements (does NOT
replace) ``tests/test_reading_b_report.py`` which already covers the
M4 surface.

Validation contract coverage
----------------------------

* **VAL-M5-035** — CLI exit code 0 on a populated DB with valid JSON.
* **VAL-M5-036** — ``candidate_events_today`` matches DB ground truth.
* **VAL-M5-037** — ``gate_pass_rate`` = entries / candidates with
  empty-day & zero-pass edge cases (never NaN).
* **VAL-M5-038** — ``stage2_dollars_today`` within $0.01 of
  ``llm_cost_ledger`` perplexity sum.
* **VAL-M5-039** — Σ ``per_source_emit_counts.values()`` ==
  ``candidate_events_today``; bucket keys are documented Stage-1
  source identifiers (``universal_news_watcher``,
  ``sec_8k_monitor``, ``ir_events_watcher``, ``intraday_scanner``,
  ``trial_calendar_match``).
* **VAL-M5-048** — ``news_event_entry`` parent + multiple exit fills
  ({iv_crush_exit, stop_loss}) all roll up to ONE ledger play with
  ``event_path='news_event_entry'``; 100% of P&L attributed to the
  Reading-B bucket.
* **VAL-M5-049** — ``--since`` honours partial-day windows: accepts
  ISO 8601 datetime AND short durations (``2h``, ``30m``, ``1d``,
  ``45s``); rejects garbage with stderr containing ``invalid --since``.
* **VAL-M5-050** — ``--by-source`` mode and empty-day output are
  well-formed (exits 0, no NaN, JSON parses, all required keys
  present).

All tests are hermetic: each constructs a fresh sqlite db at
``tmp_path``, applies the v10 migration, and seeds rows directly.
No network, no external services, no Alpaca mocking required.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from biotech_sniper import db as _db
from biotech_sniper import performance_ledger
from biotech_sniper.migrations import runner as _migrations_runner
from biotech_sniper.reports import reading_b_report


# ---------------------------------------------------------------------------
# DB / seed helpers
# ---------------------------------------------------------------------------


def _seed_v10_db(db_path: Path) -> None:
    """Bring a fresh db file up to schema v10 (Reading-B foundations)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _db.connect(db_path)
    try:
        _db.run_migrations(conn)
    finally:
        conn.close()
    rc = _migrations_runner.main(
        [
            "--db",
            str(db_path),
            "--target",
            str(_db.CURRENT_VERSION),
            "--no-backup",
        ]
    )
    assert rc == 0, f"migration runner returned {rc}"


def _insert_news_event(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    source: str,
    title: str,
    ingested_at: str,
    url: str,
    published_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO news_events (ticker, source, title, ingested_at, url, published_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (ticker, source, title, ingested_at, url, published_at),
    )
    return int(cur.lastrowid)


def _insert_candidate(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    news_event_id: int,
    matched: str,
    emitted_at: str,
    dedup_key: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO candidate_events (
            ticker, source_news_event_id, matched_keywords,
            calendar_match, emitted_at, dedup_key
        ) VALUES (?, ?, ?, NULL, ?, ?)
        """,
        (ticker, news_event_id, matched, emitted_at, dedup_key),
    )
    return int(cur.lastrowid)


def _insert_perplexity_cost(
    conn: sqlite3.Connection,
    *,
    cost_usd: float,
    called_at: str,
    purpose: str = "stage2_event_scoring",
) -> None:
    conn.execute(
        """
        INSERT INTO llm_cost_ledger (
            provider, model_id, purpose,
            prompt_tokens, completion_tokens,
            latency_ms, cost_usd, called_at
        ) VALUES ('perplexity', 'sonar', ?, 100, 100, 200, ?, ?)
        """,
        (purpose, cost_usd, called_at),
    )


def _insert_paper_order(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    client_order_id: str,
    event: str,
    created_at: str,
    status: str = "filled",
) -> None:
    conn.execute(
        """
        INSERT INTO paper_orders (
            id, status, event, client_order_id, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (order_id, status, event, client_order_id, created_at),
    )


# A consistent test "today" so we don't have to dance around real
# UTC clock shifts. The fixtures that need now-relative durations
# instead use ``datetime.now(timezone.utc)`` directly.
_TODAY = "2026-04-29"


@pytest.fixture()
def populated_db(tmp_path: Path) -> Path:
    """Seed a v10 db with deterministic Reading-B rows on ``_TODAY``.

    Layout:

    * 4 candidate_events (ABCD x2, EFGH, IJKL) on _TODAY.
    * 1 paper_orders row (event='news_event_entry') on _TODAY →
      gate_pass_rate = 1/4 = 0.25.
    * 2 perplexity ledger rows summing to $5.10.
    * 1 same-ticker (ZZZZ) candidate on the prior day —
      `--since 2026-04-29` MUST exclude it.
    """
    db_path = tmp_path / "alpha_sniper.db"
    _seed_v10_db(db_path)
    conn = _db.connect(db_path)
    try:
        # _TODAY rows
        n1 = _insert_news_event(
            conn,
            ticker="ABCD",
            source="universal_news_watcher",
            title="ABCD reports phase 3 readout",
            ingested_at=f"{_TODAY}T13:00:00.000Z",
            url="https://example.com/abcd/1",
            published_at=f"{_TODAY}T13:00:00.000Z",
        )
        n2 = _insert_news_event(
            conn,
            ticker="ABCD",
            source="universal_news_watcher",
            title="ABCD secondary headline",
            ingested_at=f"{_TODAY}T13:30:00.000Z",
            url="https://example.com/abcd/2",
            published_at=f"{_TODAY}T13:30:00.000Z",
        )
        n3 = _insert_news_event(
            conn,
            ticker="EFGH",
            source="sec_8k_monitor",
            title="EFGH 8-K material",
            ingested_at=f"{_TODAY}T14:00:00.000Z",
            url="https://example.com/efgh/1",
            published_at=f"{_TODAY}T14:00:00.000Z",
        )
        n4 = _insert_news_event(
            conn,
            ticker="IJKL",
            source="ir_events_watcher",
            title="IJKL partnership",
            ingested_at=f"{_TODAY}T15:00:00.000Z",
            url="https://example.com/ijkl/1",
            published_at=f"{_TODAY}T15:00:00.000Z",
        )

        _insert_candidate(
            conn,
            ticker="ABCD",
            news_event_id=n1,
            matched="readout",
            emitted_at=f"{_TODAY}T13:01:00.000Z",
            dedup_key="dk-abcd-1",
        )
        _insert_candidate(
            conn,
            ticker="ABCD",
            news_event_id=n2,
            matched="readout",
            emitted_at=f"{_TODAY}T13:31:00.000Z",
            dedup_key="dk-abcd-2",
        )
        _insert_candidate(
            conn,
            ticker="EFGH",
            news_event_id=n3,
            matched="material",
            emitted_at=f"{_TODAY}T14:01:00.000Z",
            dedup_key="dk-efgh-1",
        )
        _insert_candidate(
            conn,
            ticker="IJKL",
            news_event_id=n4,
            matched="partnership",
            emitted_at=f"{_TODAY}T15:01:00.000Z",
            dedup_key="dk-ijkl-1",
        )

        _insert_perplexity_cost(
            conn,
            cost_usd=2.10,
            called_at=f"{_TODAY}T13:05:00.000Z",
        )
        _insert_perplexity_cost(
            conn,
            cost_usd=3.00,
            called_at=f"{_TODAY}T14:05:00.000Z",
        )

        _insert_paper_order(
            conn,
            order_id="po-news-1",
            client_order_id="coid-1",
            event="news_event_entry",
            created_at=f"{_TODAY}T13:10:00.000Z",
        )

        # Prior-day rows that MUST NOT be counted by --since _TODAY.
        prior = "2026-04-28"
        n_prior = _insert_news_event(
            conn,
            ticker="ZZZZ",
            source="universal_news_watcher",
            title="ZZZZ pre-window",
            ingested_at=f"{prior}T10:00:00.000Z",
            url="https://example.com/zzzz/prev",
            published_at=f"{prior}T10:00:00.000Z",
        )
        _insert_candidate(
            conn,
            ticker="ZZZZ",
            news_event_id=n_prior,
            matched="readout",
            emitted_at=f"{prior}T10:01:00.000Z",
            dedup_key="dk-zzzz-prev",
        )
        _insert_perplexity_cost(
            conn,
            cost_usd=99.0,
            called_at=f"{prior}T10:05:00.000Z",
        )

        conn.commit()
    finally:
        conn.close()
    return db_path


def _run_json(db_path: Path, *extra: str) -> dict:
    """Invoke ``main(...)`` and return the parsed JSON payload.

    Captures stdout via ``capsys`` is awkward when chained — we
    instead invoke as a subprocess so the test reads the same
    bytes a real shell pipeline would.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "biotech_sniper.reports.reading_b_report",
            "--db",
            str(db_path),
            "--json",
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"CLI exited {proc.returncode}; stderr={proc.stderr!r}"
    )
    last = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    assert last, f"empty stdout; full output={proc.stdout!r}"
    return json.loads(last[-1])


# ===========================================================================
# VAL-M5-035 — CLI exit 0 on populated DB
# ===========================================================================


def test_val_m5_035_cli_exit_zero_on_populated_db(populated_db: Path) -> None:
    """``--date <today> --json`` exits 0 and emits parseable JSON."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "biotech_sniper.reports.reading_b_report",
            "--date",
            _TODAY,
            "--db",
            str(populated_db),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    # Exact-day match: 4 candidates seeded on _TODAY.
    assert payload["candidate_events_today"] == 4


def test_val_m5_035_cli_exit_zero_with_since_alias(populated_db: Path) -> None:
    """``--since <YYYY-MM-DD>`` (existing alias) also exits 0."""
    payload = _run_json(populated_db, "--since", _TODAY)
    assert payload["candidate_events_today"] == 4


# ===========================================================================
# VAL-M5-036 — candidate_events_today matches DB ground truth
# ===========================================================================


def test_val_m5_036_candidate_events_today_matches_db(populated_db: Path) -> None:
    """JSON's ``candidate_events_today`` ≡ DB ``COUNT(*)`` for the day."""
    payload = _run_json(populated_db, "--since", _TODAY)
    conn = _db.connect(populated_db)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM candidate_events "
            "WHERE DATE(emitted_at) = DATE(?)",
            (_TODAY,),
        ).fetchone()
        ground_truth = int(row["n"])
    finally:
        conn.close()
    assert payload["candidate_events_today"] == ground_truth


# ===========================================================================
# VAL-M5-037 — gate_pass_rate computed correctly
# ===========================================================================


def test_val_m5_037_gate_pass_rate_typical_day(populated_db: Path) -> None:
    """Typical day: 1 entry / 4 candidates = 0.25."""
    payload = _run_json(populated_db, "--since", _TODAY)
    rate = payload["gate_pass_rate"]
    assert rate is not None
    assert isinstance(rate, float)
    assert 0.0 <= rate <= 1.0
    assert rate == pytest.approx(0.25, abs=1e-9)


def test_val_m5_037_gate_pass_rate_zero_emit_day(tmp_path: Path) -> None:
    """Zero-emit day: gate_pass_rate is null OR 0.0 (never NaN)."""
    db_path = tmp_path / "empty.db"
    _seed_v10_db(db_path)
    payload = _run_json(db_path, "--since", _TODAY)
    rate = payload["gate_pass_rate"]
    # Spec allows null OR 0.0 (VAL-M5-037 + VAL-M5-050 both
    # acceptable); never NaN, never raises.
    assert rate is None or rate == 0.0
    # The whole field must round-trip through JSON without
    # introducing NaN.
    assert payload["candidate_events_today"] == 0


def test_val_m5_037_gate_pass_rate_zero_pass_day(tmp_path: Path) -> None:
    """Candidates emitted but no entries → gate_pass_rate == 0.0."""
    db_path = tmp_path / "zero_pass.db"
    _seed_v10_db(db_path)
    conn = _db.connect(db_path)
    try:
        n = _insert_news_event(
            conn,
            ticker="MMMM",
            source="universal_news_watcher",
            title="MMMM headline",
            ingested_at=f"{_TODAY}T13:00:00.000Z",
            url="https://example.com/mmmm/1",
            published_at=f"{_TODAY}T13:00:00.000Z",
        )
        _insert_candidate(
            conn,
            ticker="MMMM",
            news_event_id=n,
            matched="readout",
            emitted_at=f"{_TODAY}T13:01:00.000Z",
            dedup_key="dk-mmmm-1",
        )
        conn.commit()
    finally:
        conn.close()
    payload = _run_json(db_path, "--since", _TODAY)
    assert payload["candidate_events_today"] == 1
    assert payload["gate_pass_rate"] == 0.0


# ===========================================================================
# VAL-M5-038 — stage2_dollars_today within $0.01 of ledger SUM
# ===========================================================================


def test_val_m5_038_stage2_dollars_within_one_cent(populated_db: Path) -> None:
    """Reported spend matches ``SUM(cost_usd)`` within $0.01."""
    payload = _run_json(populated_db, "--since", _TODAY)
    conn = _db.connect(populated_db)
    try:
        row = conn.execute(
            "SELECT COALESCE(ROUND(SUM(cost_usd), 4), 0) AS total "
            "FROM llm_cost_ledger "
            "WHERE provider='perplexity' AND DATE(called_at) = DATE(?)",
            (_TODAY,),
        ).fetchone()
        ground_truth = float(row["total"])
    finally:
        conn.close()
    assert abs(payload["stage2_dollars_today"] - ground_truth) < 0.01
    # Sanity: exactly the seeded sum.
    assert payload["stage2_dollars_today"] == pytest.approx(5.10, abs=0.01)


# ===========================================================================
# VAL-M5-039 — Σ per_source_emit_counts == candidate_events_today
# ===========================================================================


def test_val_m5_039_per_source_sum_equals_total(populated_db: Path) -> None:
    """Σ values equals total candidates (no double-counting/miss)."""
    payload = _run_json(populated_db, "--since", _TODAY)
    per_src = payload["per_source_emit_counts"]
    assert sum(per_src.values()) == payload["candidate_events_today"]


def test_val_m5_039_per_source_keys_documented_subset(populated_db: Path) -> None:
    """Bucket keys are a subset of the documented Stage-1 sources."""
    documented = {
        "universal_news_watcher",
        "sec_8k_monitor",
        "ir_events_watcher",
        "intraday_scanner",
        "trial_calendar_match",
    }
    payload = _run_json(populated_db, "--since", _TODAY)
    per_src = payload["per_source_emit_counts"]
    assert set(per_src.keys()) <= documented, per_src
    # Spot-check the seeded counts for each documented source.
    assert per_src["universal_news_watcher"] == 2
    assert per_src["sec_8k_monitor"] == 1
    assert per_src["ir_events_watcher"] == 1


# ===========================================================================
# VAL-M5-048 — news_event_entry parent + exits roll up to ONE play
# ===========================================================================


def _seed_news_entry_with_exits(db_path: Path, *, as_of_date: str) -> str:
    """Seed a single news_event_entry parent + 2 distinct exit fills."""
    play_card_id = "PC-NEWS-FINAL-1"
    symbol = "NEEN260619C00050000"

    conn = sqlite3.connect(db_path)
    try:
        # Entry buy.
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
                "po-entry",
                play_card_id,
                "alp-entry",
                symbol,
                "buy",
                4,
                "filled",
                None,
                "news_event_entry",
                None,
                1.00,
                "entry",
                "client-entry",
                f"{as_of_date}T15:30:00.000Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO execution_fills (
                paper_order_id, filled_at, filled_price, filled_qty,
                requested_mid_at_submit, slippage_bps, slippage_usd,
                time_to_fill_ms, partial_qty_remaining
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "po-entry",
                f"{as_of_date}T15:30:01.000Z",
                1.00,
                4,
                1.00,
                0.0,
                0.0,
                0,
                0,
            ),
        )

        # Two exits with different events, both attributed to the
        # same parent play_card_id.
        for idx, (event, qty, price) in enumerate(
            (("iv_crush_exit", 2, 1.40), ("stop_loss", 2, 0.80))
        ):
            oid = f"po-exit-{idx}"
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
                    oid,
                    f"{play_card_id}-exit-{idx}",
                    f"alp-{oid}",
                    symbol,
                    "sell",
                    qty,
                    "filled",
                    None,
                    event,
                    play_card_id,
                    price,
                    "exit",
                    f"client-{oid}",
                    f"{as_of_date}T16:0{idx}:00.000Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO execution_fills (
                    paper_order_id, filled_at, filled_price, filled_qty,
                    requested_mid_at_submit, slippage_bps, slippage_usd,
                    time_to_fill_ms, partial_qty_remaining
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    oid,
                    f"{as_of_date}T16:0{idx}:01.000Z",
                    price,
                    qty,
                    price,
                    0.0,
                    0.0,
                    0,
                    0,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return play_card_id


def test_val_m5_048_news_event_entry_parent_with_exits_single_ledger_play(
    tmp_path: Path,
) -> None:
    """One parent + multiple exits → ONE ``performance_ledger`` play."""
    as_of_date = "2030-05-15"
    db_path = tmp_path / "ledger.db"
    _seed_v10_db(db_path)
    play_card_id = _seed_news_entry_with_exits(db_path, as_of_date=as_of_date)

    news = performance_ledger.roll_up_day(
        as_of_date, event_filter="news_event_entry", db_path=db_path
    )
    assert news.play_count == 1
    assert {p.play_id for p in news.plays} == {play_card_id}
    assert {p.event_path for p in news.plays} == {"news_event_entry"}

    # The two exit-event filters carry zero — no leak into other
    # buckets.
    iv_crush = performance_ledger.roll_up_day(
        as_of_date, event_filter="iv_crush_exit", db_path=db_path
    )
    stop_loss = performance_ledger.roll_up_day(
        as_of_date, event_filter="stop_loss", db_path=db_path
    )
    assert iv_crush.play_count == 0
    assert iv_crush.realized_pnl == pytest.approx(0.0, abs=1e-9)
    assert stop_loss.play_count == 0
    assert stop_loss.realized_pnl == pytest.approx(0.0, abs=1e-9)

    # Total realized P&L:
    #   entry: -1.00 * 4 * 100  = -400.00
    #   exit1: +1.40 * 2 * 100  = +280.00
    #   exit2: +0.80 * 2 * 100  = +160.00
    #   total = +40.00
    assert news.realized_pnl == pytest.approx(40.00, abs=1e-6)

    # Union (no filter) equals the news bucket: every fill rolls
    # up to news_event_entry.
    union = performance_ledger.roll_up_day(
        as_of_date, event_filter=None, db_path=db_path
    )
    assert union.play_count == 1
    assert union.realized_pnl == pytest.approx(news.realized_pnl, abs=0.01)


# ===========================================================================
# VAL-M5-049 — --since honours partial-day windows
# ===========================================================================


def test_val_m5_049_since_iso_datetime_partial_day(populated_db: Path) -> None:
    """``--since 2026-04-29T14:00:00Z`` includes only later-emitted rows."""
    cutoff = f"{_TODAY}T14:00:00Z"
    payload = _run_json(populated_db, "--since", cutoff)
    # Of the 4 _TODAY candidates, ABCD x2 emitted at 13:01 / 13:31
    # are excluded; EFGH (14:01) and IJKL (15:01) remain.
    assert payload["candidate_events_today"] == 2
    tickers = {row["ticker"] for row in payload["top_n_tickers_by_emit"]}
    assert tickers == {"EFGH", "IJKL"}
    # Per-source bucket sum still equals the partial total.
    assert sum(payload["per_source_emit_counts"].values()) == 2


def test_val_m5_049_since_duration_partial_day(tmp_path: Path) -> None:
    """``--since 30m`` resolves to a now-anchored lower bound."""
    db_path = tmp_path / "duration.db"
    _seed_v10_db(db_path)
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    stale = (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")

    conn = _db.connect(db_path)
    try:
        n_recent = _insert_news_event(
            conn,
            ticker="RCNT",
            source="universal_news_watcher",
            title="RCNT recent",
            ingested_at=recent,
            url="https://example.com/rcnt/1",
            published_at=recent,
        )
        n_stale = _insert_news_event(
            conn,
            ticker="OLDX",
            source="universal_news_watcher",
            title="OLDX stale",
            ingested_at=stale,
            url="https://example.com/oldx/1",
            published_at=stale,
        )
        _insert_candidate(
            conn,
            ticker="RCNT",
            news_event_id=n_recent,
            matched="readout",
            emitted_at=recent,
            dedup_key="dk-rcnt",
        )
        _insert_candidate(
            conn,
            ticker="OLDX",
            news_event_id=n_stale,
            matched="readout",
            emitted_at=stale,
            dedup_key="dk-oldx",
        )
        conn.commit()
    finally:
        conn.close()

    payload = _run_json(db_path, "--since", "30m")
    # Only the recent (10m old) candidate survives the 30m window.
    assert payload["candidate_events_today"] == 1
    assert payload["top_n_tickers_by_emit"][0]["ticker"] == "RCNT"


def test_val_m5_049_since_invalid_value_rejected(tmp_path: Path) -> None:
    """``--since blarg`` exits non-zero with stderr ``invalid --since``."""
    db_path = tmp_path / "x.db"
    _seed_v10_db(db_path)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "biotech_sniper.reports.reading_b_report",
            "--since",
            "blarg",
            "--db",
            str(db_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0, proc.stdout
    assert "invalid --since" in proc.stderr, proc.stderr


def test_val_m5_049_parse_since_unit_cases() -> None:
    """``parse_since`` covers date / datetime / duration / garbage."""
    # Date-only — back-compat.
    mode, iso = reading_b_report.parse_since("2026-04-29")
    assert mode == "date"
    assert iso == "2026-04-29"

    # Datetime with explicit Z.
    mode, iso = reading_b_report.parse_since("2026-04-29T13:00:00Z")
    assert mode == "datetime"
    assert iso.startswith("2026-04-29T13:00:00")

    # Duration anchored against an explicit ``now`` so the test
    # is deterministic.
    anchor = datetime(2026, 4, 29, 14, 0, 0, tzinfo=timezone.utc)
    mode, iso = reading_b_report.parse_since("2h", now=anchor)
    assert mode == "datetime"
    assert iso == "2026-04-29T12:00:00Z"

    # Garbage rejected.
    for bad in ("blarg", "29-04-2026", "", "2h30m", "1y", "0h"):
        with pytest.raises(ValueError) as ei:
            reading_b_report.parse_since(bad)
        assert "invalid --since" in str(ei.value)


# ===========================================================================
# VAL-M5-050 — --by-source mode + empty-day output well-formed
# ===========================================================================


def test_val_m5_050_empty_day_json_well_formed(tmp_path: Path) -> None:
    """Empty-day JSON parses, contains all required keys, no NaN."""
    db_path = tmp_path / "empty.db"
    _seed_v10_db(db_path)
    payload = _run_json(db_path, "--since", _TODAY)
    # Required keys present.
    for key in (
        "candidate_events_today",
        "gate_pass_rate",
        "stage2_dollars_today",
        "top_n_tickers_by_emit",
        "top10_tickers_by_emit",
        "per_source_emit_counts",
    ):
        assert key in payload, key
    # Empty-day shape.
    assert payload["candidate_events_today"] == 0
    assert payload["stage2_dollars_today"] == 0.0
    assert payload["top_n_tickers_by_emit"] == []
    assert payload["top10_tickers_by_emit"] == []
    assert payload["per_source_emit_counts"] == {}
    # gate_pass_rate may be null OR 0.0 per spec; never NaN.
    rate = payload["gate_pass_rate"]
    assert rate is None or rate == 0.0
    if rate is not None:
        # Reject NaN explicitly.
        assert rate == rate  # NaN != NaN


def test_val_m5_050_by_source_empty_day_text_well_formed(tmp_path: Path) -> None:
    """``--by-source`` on an empty day exits 0 with non-error output."""
    db_path = tmp_path / "empty.db"
    _seed_v10_db(db_path)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "biotech_sniper.reports.reading_b_report",
            "--since",
            _TODAY,
            "--db",
            str(db_path),
            "--by-source",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    # Must mention the date and the per-source label, not raise.
    assert _TODAY in out
    assert "per_source_emit_counts" in out


def test_val_m5_050_missing_db_exits_with_code_2(tmp_path: Path) -> None:
    """Missing DB → exit code 2 (per VAL-M5-050)."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "biotech_sniper.reports.reading_b_report",
            "--since",
            _TODAY,
            "--db",
            str(tmp_path / "does_not_exist.db"),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2, proc.stderr


def test_val_m5_050_by_source_populated_day_well_formed(populated_db: Path) -> None:
    """``--by-source`` on a populated day exits 0 with all sources visible."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "biotech_sniper.reports.reading_b_report",
            "--since",
            _TODAY,
            "--db",
            str(populated_db),
            "--by-source",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    for src in ("universal_news_watcher", "sec_8k_monitor", "ir_events_watcher"):
        assert src in proc.stdout, src
