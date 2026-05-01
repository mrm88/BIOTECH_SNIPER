"""Tests for ``biotech_sniper.reports.reading_b_report`` (f-m4-08).

Covers VAL-M4-047, VAL-M4-048, VAL-M4-049 from the Reading-B
validation contract:

* CLI is dispatchable via ``python -m`` and exits 0 on ``--help``.
* JSON mode emits the required Reading-B keys with correct types
  and ranges.
* ``stage2_dollars_today`` never exceeds the daily cap AND matches
  ``llm_cost_ledger`` source-of-truth (within $0.01).

The tests build a fresh SQLite database via the v10 migration
helpers, seed synthetic ``candidate_events`` / ``news_events`` /
``llm_cost_ledger`` / ``paper_orders`` rows, and assert the JSON
schema + numeric invariants.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pytest

from biotech_sniper import db as _db
from biotech_sniper.migrations import runner as _migrations_runner
from biotech_sniper.reports import reading_b_report


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _seed_v10_db(db_path: Path) -> None:
    """Migrate ``db_path`` to schema v10 (Reading-B foundations)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _db.connect(db_path)
    try:
        _db.run_migrations(conn)
    finally:
        conn.close()
    # Apply v10 directly (idempotent — no-op when already at v10).
    rc = _migrations_runner.main(
        [
            "--db",
            str(db_path),
            "--target",
            "10",
            "--no-backup",
        ]
    )
    assert rc == 0, f"v10 migration runner returned {rc}"


def _insert_news_event(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    source: str,
    title: str,
    ingested_at: str,
    url: str | None = None,
    published_at: str | None = None,
) -> int:
    """Insert a synthetic news_events row, return its id.

    Provides ``url`` / ``published_at`` overrides because the
    composite UNIQUE INDEX ``idx_news_events_dedup`` is over
    ``(ticker, source, COALESCE(url,''), COALESCE(published_at,''))``,
    so multiple rows for the same ticker+source must vary at least
    one of those two fields to avoid IntegrityError on insert.
    """
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


@pytest.fixture
def seeded_db(tmp_path: Path):
    """Seed a v10 db with deterministic Reading-B rows for two days.

    Day under test (``since='2026-04-29'``):
      * 4 candidate_events for tickers ABCD, ABCD, EFGH, IJKL
      * 1 paper_orders row (event='news_event_entry') → gate-pass=1/4
      * 2 perplexity ledger rows summing to $5.10 (well under $20 cap)

    A second day (``2026-04-28``) seeds extra rows that MUST NOT
    be counted by ``--since 2026-04-29`` (they verify date-bound
    filtering).
    """
    db_path = tmp_path / "alpha_sniper.db"
    _seed_v10_db(db_path)
    conn = _db.connect(db_path)
    try:
        # ---- Day 1: 2026-04-29 (the day we will report on) ----------
        n1 = _insert_news_event(
            conn,
            ticker="ABCD",
            source="rss_pr",
            title="ABCD reports phase 3 readout",
            ingested_at="2026-04-29T13:00:00.000Z",
            url="https://example.com/abcd/1",
            published_at="2026-04-29T13:00:00.000Z",
        )
        n2 = _insert_news_event(
            conn,
            ticker="ABCD",
            source="rss_pr",
            title="ABCD secondary headline",
            ingested_at="2026-04-29T13:30:00.000Z",
            url="https://example.com/abcd/2",
            published_at="2026-04-29T13:30:00.000Z",
        )
        n3 = _insert_news_event(
            conn,
            ticker="EFGH",
            source="sec_8k",
            title="EFGH 8-K material",
            ingested_at="2026-04-29T14:00:00.000Z",
            url="https://example.com/efgh/1",
            published_at="2026-04-29T14:00:00.000Z",
        )
        n4 = _insert_news_event(
            conn,
            ticker="IJKL",
            source="ir_events",
            title="IJKL partnership",
            ingested_at="2026-04-29T15:00:00.000Z",
            url="https://example.com/ijkl/1",
            published_at="2026-04-29T15:00:00.000Z",
        )

        _insert_candidate(
            conn,
            ticker="ABCD",
            news_event_id=n1,
            matched="readout",
            emitted_at="2026-04-29T13:01:00.000Z",
            dedup_key="dk-abcd-1",
        )
        _insert_candidate(
            conn,
            ticker="ABCD",
            news_event_id=n2,
            matched="readout",
            emitted_at="2026-04-29T13:31:00.000Z",
            dedup_key="dk-abcd-2",
        )
        _insert_candidate(
            conn,
            ticker="EFGH",
            news_event_id=n3,
            matched="material",
            emitted_at="2026-04-29T14:01:00.000Z",
            dedup_key="dk-efgh-1",
        )
        _insert_candidate(
            conn,
            ticker="IJKL",
            news_event_id=n4,
            matched="partnership",
            emitted_at="2026-04-29T15:01:00.000Z",
            dedup_key="dk-ijkl-1",
        )

        _insert_perplexity_cost(
            conn,
            cost_usd=2.1,
            called_at="2026-04-29T13:05:00.000Z",
        )
        _insert_perplexity_cost(
            conn,
            cost_usd=3.0,
            called_at="2026-04-29T14:05:00.000Z",
        )

        _insert_paper_order(
            conn,
            order_id="po-1",
            client_order_id="coid-1",
            event="news_event_entry",
            created_at="2026-04-29T13:10:00.000Z",
        )

        # ---- Day 0: 2026-04-28 (must NOT be counted) ----------------
        n0 = _insert_news_event(
            conn,
            ticker="ZZZZ",
            source="rss_pr",
            title="ZZZZ pre-window",
            ingested_at="2026-04-28T10:00:00.000Z",
            url="https://example.com/zzzz/1",
            published_at="2026-04-28T10:00:00.000Z",
        )
        _insert_candidate(
            conn,
            ticker="ZZZZ",
            news_event_id=n0,
            matched="readout",
            emitted_at="2026-04-28T10:01:00.000Z",
            dedup_key="dk-zzzz-prev",
        )
        _insert_perplexity_cost(
            conn,
            cost_usd=99.0,
            called_at="2026-04-28T10:05:00.000Z",
        )

        conn.commit()
    finally:
        conn.close()
    return db_path


# ---------------------------------------------------------------------------
# VAL-M4-047 — module dispatch + --help
# ---------------------------------------------------------------------------


def test_module_help_exits_zero(tmp_path: Path):
    """``python -m biotech_sniper.reports.reading_b_report --help`` exits 0."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "biotech_sniper.reports.reading_b_report",
            "--help",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "BIOTECH_SNIPER_HOME": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr
    # Usage text must mention each documented flag.
    for flag in ("--since", "--json", "--by-source"):
        assert flag in proc.stdout, f"missing flag in --help: {flag}"


def test_main_returns_zero_on_json_mode(seeded_db: Path, capsys):
    """``main([...--json])`` returns exit code 0 and prints valid JSON."""
    rc = reading_b_report.main(
        [
            "--since",
            "2026-04-29",
            "--db",
            str(seeded_db),
            "--json",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr().out.strip().splitlines()
    payload = json.loads(captured[-1])
    # The required keys must be present.
    required = {
        "candidate_events_today",
        "gate_pass_rate",
        "stage2_dollars_today",
        "top_n_tickers_by_emit",
        "per_source_emit_counts",
    }
    assert required <= set(payload.keys())


# ---------------------------------------------------------------------------
# VAL-M4-048 — output schema correctness
# ---------------------------------------------------------------------------


def test_output_schema_types_and_ranges(seeded_db: Path, capsys):
    """Every Reading-B JSON key has the correct type AND legal range."""
    rc = reading_b_report.main(
        [
            "--since",
            "2026-04-29",
            "--db",
            str(seeded_db),
            "--json",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr().out.strip().splitlines()
    payload = json.loads(captured[-1])

    # candidate_events_today: numeric, equals 4 (4 day-1 candidates).
    assert isinstance(payload["candidate_events_today"], int)
    assert payload["candidate_events_today"] == 4

    # gate_pass_rate: float in [0, 1].
    rate = payload["gate_pass_rate"]
    assert isinstance(rate, float)
    assert 0.0 <= rate <= 1.0
    # 1 entry / 4 candidates = 0.25 (within float tolerance).
    assert abs(rate - 0.25) < 1e-9

    # stage2_dollars_today: float ≥ 0.
    spend = payload["stage2_dollars_today"]
    assert isinstance(spend, float)
    assert spend >= 0.0
    # 2.1 + 3.0 = 5.1.
    assert abs(spend - 5.1) < 0.01

    # top_n_tickers_by_emit: list (length ≤ 10) AND alias top10_tickers_by_emit.
    top_n = payload["top_n_tickers_by_emit"]
    assert isinstance(top_n, list)
    assert len(top_n) <= 10
    # ABCD has 2 emits → must be first.
    assert top_n[0]["ticker"] == "ABCD"
    assert top_n[0]["count"] == 2
    # Contract alias (VAL-M4-048 evidence uses ``top10_tickers_by_emit``).
    assert payload["top10_tickers_by_emit"] == top_n

    # per_source_emit_counts: dict {source -> non-negative int}.
    per_src = payload["per_source_emit_counts"]
    assert isinstance(per_src, dict)
    assert per_src["rss_pr"] == 2  # ABCD + ABCD
    assert per_src["sec_8k"] == 1  # EFGH
    assert per_src["ir_events"] == 1  # IJKL
    for k, v in per_src.items():
        assert isinstance(v, int)
        assert v >= 0


def test_only_seeded_day_is_counted(seeded_db: Path, capsys):
    """``--since`` filter excludes rows from the prior day (VAL-M4-048)."""
    rc = reading_b_report.main(
        [
            "--since",
            "2026-04-29",
            "--db",
            str(seeded_db),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    # The prior-day ZZZZ candidate (count=1) and $99.00 cost row must
    # be excluded.
    assert payload["candidate_events_today"] == 4
    assert "ZZZZ" not in {row["ticker"] for row in payload["top_n_tickers_by_emit"]}
    assert payload["stage2_dollars_today"] < 50.0


# ---------------------------------------------------------------------------
# VAL-M4-049 — Stage-2 dollars never exceed cap AND match ledger ±$0.01
# ---------------------------------------------------------------------------


def test_stage2_dollars_match_ledger_within_one_cent(seeded_db: Path, capsys):
    """``stage2_dollars_today`` matches ``SUM(llm_cost_ledger.cost_usd)``."""
    rc = reading_b_report.main(
        [
            "--since",
            "2026-04-29",
            "--db",
            str(seeded_db),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    conn = _db.connect(seeded_db)
    try:
        row = conn.execute(
            "SELECT COALESCE(ROUND(SUM(cost_usd), 4), 0) AS total "
            "FROM llm_cost_ledger "
            "WHERE provider='perplexity' AND DATE(called_at)=DATE(?)",
            ("2026-04-29",),
        ).fetchone()
        ledger_total = float(row["total"])
    finally:
        conn.close()

    assert abs(payload["stage2_dollars_today"] - ledger_total) < 0.01


def test_stage2_dollars_never_exceed_daily_cap(seeded_db: Path, capsys, monkeypatch):
    """JSON spend stays ≤ ``LLM_STAGE2_DAILY_USD_CAP`` (default $20)."""
    monkeypatch.delenv("LLM_STAGE2_DAILY_USD_CAP", raising=False)
    rc = reading_b_report.main(
        [
            "--since",
            "2026-04-29",
            "--db",
            str(seeded_db),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    from biotech_sniper.config import get_llm_stage2_daily_usd_cap

    cap = get_llm_stage2_daily_usd_cap()
    assert payload["stage2_dollars_today"] <= cap
    assert payload["cap_usd"] == cap


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_db_returns_zero_counts(tmp_path: Path, capsys):
    """No candidates / no cost rows → counts are 0 and gate_pass_rate is 0."""
    db_path = tmp_path / "empty.db"
    _seed_v10_db(db_path)
    rc = reading_b_report.main(
        [
            "--since",
            "2026-04-29",
            "--db",
            str(db_path),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["candidate_events_today"] == 0
    assert payload["gate_pass_rate"] == 0.0
    assert payload["stage2_dollars_today"] == 0.0
    assert payload["top_n_tickers_by_emit"] == []
    assert payload["per_source_emit_counts"] == {}


def test_text_mode_default_renders_table(seeded_db: Path, capsys):
    """Without ``--json``, output is a human-readable table."""
    rc = reading_b_report.main(
        [
            "--since",
            "2026-04-29",
            "--db",
            str(seeded_db),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    # Should contain the date and key headers, but not be raw JSON.
    assert "2026-04-29" in out
    assert "candidate_events_today" in out or "candidate events" in out.lower()
    # Top ticker (ABCD) should be visible in the table.
    assert "ABCD" in out


def test_by_source_mode_shows_per_source_breakdown(seeded_db: Path, capsys):
    """``--by-source`` emits a per-source breakdown in the human table."""
    rc = reading_b_report.main(
        [
            "--since",
            "2026-04-29",
            "--db",
            str(seeded_db),
            "--by-source",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    # Each seeded source must be visible.
    for src in ("rss_pr", "sec_8k", "ir_events"):
        assert src in out


def test_invalid_since_format_rejected(tmp_path: Path):
    """``--since`` must be ``YYYY-MM-DD``; bogus input exits non-zero."""
    db_path = tmp_path / "x.db"
    _seed_v10_db(db_path)
    with pytest.raises(SystemExit) as exc:
        reading_b_report.main(
            [
                "--since",
                "29-04-2026",
                "--db",
                str(db_path),
                "--json",
            ]
        )
    assert exc.value.code != 0


def test_missing_db_returns_nonzero(tmp_path: Path):
    """Pointing at a nonexistent db path returns a non-zero exit."""
    rc = reading_b_report.main(
        [
            "--since",
            "2026-04-29",
            "--db",
            str(tmp_path / "does_not_exist.db"),
            "--json",
        ]
    )
    assert rc != 0
