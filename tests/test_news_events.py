"""Tests for the news_events SQLite layer (M2 daily news persistence).

These exercise:

* VAL-M2-075 schema (columns, NOT NULL, PRIMARY KEY id, dedup index).
* Dedup behaviour on (ticker, source, url, published_at) — both
  populated and NULL-valued tuples.
* The ``daily_news_ingest`` orchestrator, including the empty-feed
  branch that writes ``audit_latest.json:news_events_empty`` so the
  M2-076 assertion can pass.
* Multi-source merge: rows from different watchers do NOT collapse on
  identical (ticker, url) tuples because ``source`` is part of the key.
* The audit-JSON write path preserves other consumers' keys.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Sequence

import pytest

from biotech_sniper import db
from biotech_sniper.news_events import (
    DailyIngestResult,
    NewsEvent,
    SOURCE_INTRADAY_RSS,
    SOURCE_IR_EVENTS,
    SOURCE_SEC_8K,
    SOURCE_UNIVERSAL,
    daily_news_ingest,
    load_watch_tickers,
    record_news_event,
    record_news_events,
    write_audit_summary,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _column_names(conn: sqlite3.Connection, table: str) -> dict[str, dict]:
    return {
        row[1]: {"type": row[2], "notnull": row[3], "pk": row[5]}
        for row in conn.execute(f"PRAGMA table_info({table})")
    }


def _seed_universe(conn: sqlite3.Connection, watch: list[str], tradeable: list[str] | None = None) -> None:
    """Insert minimal ``universe`` rows for tests that exercise loaders."""
    tradeable = tradeable or []
    for t in watch:
        conn.execute(
            "INSERT INTO universe (ticker, tier, has_options_chain) "
            "VALUES (?, 'watch', 0)",
            (t,),
        )
    for t in tradeable:
        conn.execute(
            "INSERT INTO universe (ticker, tier, has_options_chain) "
            "VALUES (?, 'tradeable', 1)",
            (t,),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Schema (VAL-M2-075)
# ---------------------------------------------------------------------------


def test_news_events_schema_columns_and_dedup_index(tmp_path):
    """Every column required by VAL-M2-075 is present with the right NOT NULL flags."""
    conn = db.connect(tmp_path / "schema.db")
    try:
        db.run_migrations(conn)

        cols = _column_names(conn, "news_events")
        # Required column set.
        for required in (
            "id",
            "ticker",
            "source",
            "published_at",
            "title",
            "url",
            "ingested_at",
            "raw_payload",
        ):
            assert required in cols, f"news_events.{required} missing"

        # NOT NULL contract for the columns named in VAL-M2-075.
        assert cols["ticker"]["notnull"] == 1
        assert cols["source"]["notnull"] == 1
        assert cols["title"]["notnull"] == 1
        assert cols["ingested_at"]["notnull"] == 1
        # PRIMARY KEY id
        assert cols["id"]["pk"] == 1

        # Dedup unique index exists.
        idx_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_news_events_dedup'"
        ).fetchall()
        assert len(idx_rows) == 1, "dedup unique index missing"

        # NOT NULL on title is enforced.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO news_events (ticker, source, title) "
                "VALUES (?, ?, NULL)",
                ("AAA", SOURCE_UNIVERSAL),
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# record_news_event(s) / dedup
# ---------------------------------------------------------------------------


def test_record_news_event_inserts_row_with_defaults(tmp_path):
    conn = db.connect(tmp_path / "ins.db")
    try:
        db.run_migrations(conn)

        inserted = record_news_event(
            conn,
            ticker="aaa",  # lowercase — should be normalised to upper
            source=SOURCE_UNIVERSAL,
            title="Topline data drops",
            url="https://example.com/aaa-topline",
            published_at="2026-04-26",
            raw_payload={"alpha": 1, "beta": [1, 2, 3]},
        )
        conn.commit()
        assert inserted is True

        row = conn.execute(
            "SELECT ticker, source, title, url, published_at, ingested_at, raw_payload "
            "FROM news_events"
        ).fetchone()
        assert row["ticker"] == "AAA"
        assert row["source"] == SOURCE_UNIVERSAL
        assert row["title"] == "Topline data drops"
        assert row["url"] == "https://example.com/aaa-topline"
        assert row["published_at"] == "2026-04-26"
        assert row["ingested_at"]  # default-supplied timestamp
        # raw_payload round-trips as JSON
        assert json.loads(row["raw_payload"]) == {"alpha": 1, "beta": [1, 2, 3]}
    finally:
        conn.close()


def test_record_news_event_dedups_on_identical_key(tmp_path):
    """Two inserts with the same (ticker, source, url, published_at) → 1 row."""
    conn = db.connect(tmp_path / "dedup.db")
    try:
        db.run_migrations(conn)

        first = record_news_event(
            conn,
            ticker="BBB",
            source=SOURCE_SEC_8K,
            title="8-K filed: topline",
            url="https://www.sec.gov/aaa.htm",
            published_at="2026-04-26",
        )
        second = record_news_event(
            conn,
            ticker="BBB",
            source=SOURCE_SEC_8K,
            title="8-K filed: topline (rephrased)",
            url="https://www.sec.gov/aaa.htm",
            published_at="2026-04-26",
        )
        conn.commit()
        assert first is True
        assert second is False
        count = conn.execute("SELECT COUNT(*) FROM news_events").fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_record_news_event_dedups_when_url_and_published_at_are_null(tmp_path):
    """The dedup index uses COALESCE so NULL-valued tuples still merge."""
    conn = db.connect(tmp_path / "dedup_null.db")
    try:
        db.run_migrations(conn)

        first = record_news_event(
            conn,
            ticker="CCC",
            source=SOURCE_IR_EVENTS,
            title="IR page mentions phase 3",
            url=None,
            published_at=None,
        )
        second = record_news_event(
            conn,
            ticker="CCC",
            source=SOURCE_IR_EVENTS,
            title="IR page mentions phase 3 (again)",
            url=None,
            published_at=None,
        )
        conn.commit()
        assert first is True
        assert second is False
        assert conn.execute("SELECT COUNT(*) FROM news_events").fetchone()[0] == 1
    finally:
        conn.close()


def test_record_news_events_batch_counts(tmp_path):
    conn = db.connect(tmp_path / "batch.db")
    try:
        db.run_migrations(conn)

        events = [
            NewsEvent(
                ticker="DDD",
                source=SOURCE_UNIVERSAL,
                title="hit 1",
                url="https://e.com/1",
            ),
            NewsEvent(
                ticker="DDD",
                source=SOURCE_UNIVERSAL,
                title="hit 1 dup",
                url="https://e.com/1",
            ),
            {
                "ticker": "EEE",
                "source": SOURCE_UNIVERSAL,
                "title": "hit 2",
                "url": "https://e.com/2",
                "published_at": "2026-04-26",
            },
            # Malformed: missing title — should be skipped silently.
            {"ticker": "FFF", "source": SOURCE_UNIVERSAL, "title": ""},
        ]
        counts = record_news_events(conn, events)
        assert counts["total"] == 4
        assert counts["inserted"] == 2  # 2 unique
        assert counts["skipped"] == 2   # 1 dup + 1 malformed

        rows = conn.execute(
            "SELECT ticker FROM news_events ORDER BY ticker"
        ).fetchall()
        assert [r["ticker"] for r in rows] == ["DDD", "EEE"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Multi-source merge
# ---------------------------------------------------------------------------


def test_multi_source_merge_does_not_collapse_on_url(tmp_path):
    """The same (ticker, url, published_at) from two SOURCES → 2 rows.

    Source is part of the dedup key, so the SEC monitor and the
    intraday RSS scanner can both record an article that points to
    the same SEC link without losing one of them.
    """
    conn = db.connect(tmp_path / "multi.db")
    try:
        db.run_migrations(conn)

        record_news_event(
            conn,
            ticker="GGG",
            source=SOURCE_SEC_8K,
            title="8-K filed",
            url="https://www.sec.gov/abc.htm",
            published_at="2026-04-26",
        )
        record_news_event(
            conn,
            ticker="GGG",
            source=SOURCE_INTRADAY_RSS,
            title="Endpoints: GGG 8-K mention",
            url="https://www.sec.gov/abc.htm",
            published_at="2026-04-26",
        )
        record_news_event(
            conn,
            ticker="GGG",
            source=SOURCE_UNIVERSAL,
            title="Universal: same 8-K caught hourly",
            url="https://www.sec.gov/abc.htm",
            published_at="2026-04-26",
        )
        conn.commit()

        rows = conn.execute(
            "SELECT source FROM news_events ORDER BY source"
        ).fetchall()
        sources = sorted(r["source"] for r in rows)
        assert sources == sorted([SOURCE_INTRADAY_RSS, SOURCE_SEC_8K, SOURCE_UNIVERSAL])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# load_watch_tickers
# ---------------------------------------------------------------------------


def test_load_watch_tickers_returns_watch_and_tradeable(tmp_path):
    conn = db.connect(tmp_path / "uni.db")
    try:
        db.run_migrations(conn)
        _seed_universe(conn, watch=["AAA", "BBB"], tradeable=["CCC"])
        out = load_watch_tickers(conn)
        assert out == ["AAA", "BBB", "CCC"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# daily_news_ingest orchestrator
# ---------------------------------------------------------------------------


def _watcher_static(rows: list[dict]):
    """Build a watcher fn that always returns the given rows."""

    def _fn(_tickers: Sequence[str]) -> list[dict]:
        return list(rows)

    return _fn


def test_daily_news_ingest_inserts_rows_for_each_ticker(tmp_path):
    db_path = tmp_path / "daily.db"
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
        _seed_universe(conn, watch=["AAA", "BBB", "CCC"])
    finally:
        conn.close()

    watchers = {
        SOURCE_UNIVERSAL: _watcher_static([
            {
                "ticker": "AAA",
                "title": "AAA topline",
                "url": "https://e.com/aaa",
                "published_at": "2026-04-26",
            },
            {
                "ticker": "BBB",
                "title": "BBB readout",
                "url": "https://e.com/bbb",
                "published_at": "2026-04-26",
            },
        ]),
        SOURCE_SEC_8K: _watcher_static([
            {
                "ticker": "CCC",
                "title": "CCC 8-K filed",
                "url": "https://e.com/ccc-8k",
                "published_at": "2026-04-26",
            },
        ]),
    }

    result = daily_news_ingest(
        db_path=db_path,
        watchers=watchers,
        audit_path=tmp_path / "audit.json",
    )

    assert result.tickers_attempted == 3
    assert result.rows_inserted == 3
    assert result.rows_skipped_duplicate == 0
    assert result.empty_feed_reasons == {}
    assert result.per_ticker_counts == {"AAA": 1, "BBB": 1, "CCC": 1}
    assert sorted(result.sources_run) == sorted([SOURCE_UNIVERSAL, SOURCE_SEC_8K])

    # Verify rows actually landed.
    conn = db.connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM news_events").fetchone()[0]
        assert n == 3
    finally:
        conn.close()


def test_daily_news_ingest_records_empty_feed_reason(tmp_path):
    """Tickers with zero feed rows must surface in news_events_empty."""
    db_path = tmp_path / "empty.db"
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
        _seed_universe(conn, watch=["AAA", "BBB"])
    finally:
        conn.close()

    # Watcher only returns AAA → BBB triggers the empty-feed branch.
    watchers = {
        SOURCE_UNIVERSAL: _watcher_static([
            {
                "ticker": "AAA",
                "title": "AAA news",
                "url": "https://e.com/aaa",
            },
        ]),
    }

    audit_path = tmp_path / "audit.json"
    result = daily_news_ingest(
        db_path=db_path,
        watchers=watchers,
        audit_path=audit_path,
    )

    assert result.tickers_attempted == 2
    assert result.rows_inserted == 1
    assert result.empty_feed_reasons == {"BBB": "no_feed_match"}

    audit_blob = json.loads(audit_path.read_text())
    assert audit_blob["news_events_empty"] == {"BBB": "no_feed_match"}
    assert audit_blob["news_ingestion"]["tickers_attempted"] == 2
    assert audit_blob["news_ingestion"]["empty_feed_reasons"] == {"BBB": "no_feed_match"}


def test_daily_news_ingest_is_idempotent_on_rerun(tmp_path):
    """Re-running on the same day must not insert duplicate rows."""
    db_path = tmp_path / "rerun.db"
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
        _seed_universe(conn, watch=["AAA"])
    finally:
        conn.close()

    payload = [
        {
            "ticker": "AAA",
            "title": "AAA topline",
            "url": "https://e.com/aaa",
            "published_at": "2026-04-26",
        },
    ]
    watchers = {SOURCE_UNIVERSAL: _watcher_static(payload)}

    first = daily_news_ingest(
        db_path=db_path,
        watchers=watchers,
        audit_path=tmp_path / "a.json",
    )
    second = daily_news_ingest(
        db_path=db_path,
        watchers=watchers,
        audit_path=tmp_path / "a.json",
    )
    assert first.rows_inserted == 1
    assert second.rows_inserted == 0
    assert second.rows_skipped_duplicate >= 1

    conn = db.connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM news_events").fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_daily_news_ingest_multi_source_merge(tmp_path):
    """A ticker hit by two sources lands two news_events rows (one per source)."""
    db_path = tmp_path / "multi_daily.db"
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
        _seed_universe(conn, watch=["AAA"])
    finally:
        conn.close()

    watchers = {
        SOURCE_UNIVERSAL: _watcher_static([
            {
                "ticker": "AAA",
                "title": "Universal hit",
                "url": "https://e.com/aaa",
                "published_at": "2026-04-26",
            },
        ]),
        SOURCE_SEC_8K: _watcher_static([
            {
                "ticker": "AAA",
                "title": "8-K filed",
                "url": "https://www.sec.gov/aaa.htm",
                "published_at": "2026-04-26",
            },
        ]),
    }
    result = daily_news_ingest(
        db_path=db_path,
        watchers=watchers,
        audit_path=tmp_path / "a.json",
    )
    assert result.rows_inserted == 2
    assert result.empty_feed_reasons == {}

    conn = db.connect(db_path)
    try:
        sources = sorted(
            r["source"] for r in conn.execute(
                "SELECT source FROM news_events WHERE ticker='AAA'"
            )
        )
        assert sources == [SOURCE_SEC_8K, SOURCE_UNIVERSAL]
    finally:
        conn.close()


def test_write_audit_summary_preserves_other_keys(tmp_path):
    """Running the daily ingest must not clobber existing audit JSON keys."""
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "as_of_date": "2026-04-26",
                "sources": {"clinicaltrials_gov": {"ok": True}},
                "failures": [],
            }
        )
    )

    result = DailyIngestResult(
        tickers_attempted=2,
        rows_inserted=1,
        empty_feed_reasons={"BBB": "no_feed_match"},
    )
    write_audit_summary(result, audit_path=audit_path)

    blob = json.loads(audit_path.read_text())
    # Pre-existing audit keys preserved.
    assert blob["as_of_date"] == "2026-04-26"
    assert blob["sources"] == {"clinicaltrials_gov": {"ok": True}}
    # New keys added.
    assert blob["news_events_empty"] == {"BBB": "no_feed_match"}
    assert blob["news_ingestion"]["tickers_attempted"] == 2
