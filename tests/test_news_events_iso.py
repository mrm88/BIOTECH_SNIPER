"""Regression tests for ``news_events._now_iso``.

The previous format string ``"%Y-%m-%dT%H:%M:%fZ"`` produced strings
like ``2026-04-27T07:59:687290Z`` (no seconds component, microseconds
spliced after minutes), which SQLite's ``date()`` and ``datetime()``
parsers treat as an empty string. That broke the VAL-M2-076 SQL query
``SELECT date(ingested_at) FROM news_events`` (returned 0 rows).

The fixed format string ``"%Y-%m-%dT%H:%M:%S.%fZ"`` yields
``YYYY-MM-DDTHH:MM:SS.ffffffZ`` which SQLite parses correctly.

These tests pin that contract:

* ``test_now_iso_is_sqlite_parseable`` — SQLite's ``date(?)`` returns
  today's ISO date when given a ``_now_iso()`` value.
* ``test_now_iso_format_has_seconds_and_microseconds`` — the regex
  ``^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{6}Z$`` matches.
* ``test_record_news_event_ingested_at_is_today`` — a real row
  inserted by :func:`record_news_event` has an ``ingested_at`` whose
  ``date()`` cast equals today's date.
"""

from __future__ import annotations

import datetime
import re
import sqlite3

from biotech_sniper import db
from biotech_sniper.news_events import (
    SOURCE_UNIVERSAL,
    _now_iso,
    record_news_event,
)


_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


def test_now_iso_is_sqlite_parseable():
    """SQLite's date() of _now_iso() must equal today's ISO date."""
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    with sqlite3.connect(":memory:") as conn:
        parsed = conn.execute("SELECT date(?)", (_now_iso(),)).fetchone()[0]
    assert parsed == today, (
        f"SQLite could not parse _now_iso() output; got {parsed!r}, "
        f"expected today's date {today!r}"
    )


def test_now_iso_format_has_seconds_and_microseconds():
    """Regex pin for ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` shape."""
    value = _now_iso()
    assert _ISO_RE.match(value), (
        f"_now_iso() returned malformed ISO timestamp: {value!r}"
    )


def test_record_news_event_ingested_at_is_today(tmp_path):
    """Round-trip a real row: SQLite date(ingested_at) must equal today."""
    db_path = tmp_path / "iso.db"
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
        inserted = record_news_event(
            conn,
            ticker="ZZZ",
            source=SOURCE_UNIVERSAL,
            title="iso-fix regression",
            url="https://example.com/iso-fix",
            published_at="2026-04-27",
        )
        conn.commit()
        assert inserted is True

        row = conn.execute(
            "SELECT id, date(ingested_at) AS d FROM news_events "
            "WHERE ticker = ? ORDER BY id DESC LIMIT 1",
            ("ZZZ",),
        ).fetchone()
        assert row is not None
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        assert row["d"] == today, (
            f"date(ingested_at) was {row['d']!r}, expected today {today!r}"
        )
    finally:
        conn.close()
