"""Regression tests for ``biotech_sniper.bulk_universe_scanner._now_iso``.

f-m4-10a-fix-now-iso-strftime: the original implementation used the
malformed strftime format ``"%Y-%m-%dT%H:%M:%fZ"`` (missing ``%S.``
between ``%M:`` and ``%f``). That produced strings like
``'2026-04-28T01:21:894161Z'`` (minute and microsecond colliding with
no seconds field), which SQLite's ``date()`` function cannot parse —
it silently returns ``NULL``. ``build_universe`` UPSERTs every row
with ``last_chain_check_at = _now_iso()``, so the daily ratio query
``date(last_chain_check_at) = date('now')`` returned 0 rows even
when every tradeable row had been freshly stamped. That broke
VAL-M4-067 and any other assertion comparing universe timestamps via
SQLite's date() function.

These tests pin the corrected ISO 8601 contract:

* ``_now_iso()`` is parseable by ``datetime.fromisoformat`` after the
  ``Z`` suffix is replaced with ``+00:00``.
* SQLite's ``date(_now_iso())`` returns today's UTC date (a
  10-character ``YYYY-MM-DD`` string, not ``NULL``).
"""

from __future__ import annotations

import datetime
import re
import sqlite3

from biotech_sniper.bulk_universe_scanner import _now_iso


def test_now_iso_parseable_by_fromisoformat() -> None:
    """``_now_iso()`` must round-trip through ``datetime.fromisoformat``.

    The trailing ``Z`` is the UTC designator; Python's
    ``fromisoformat`` only accepts ``+HH:MM`` offsets, so we
    substitute before parsing. The parsed datetime must be
    timezone-aware and within a few seconds of ``datetime.now(UTC)``.
    """
    raw = _now_iso()
    iso = re.sub(r"Z$", "+00:00", raw)

    parsed = datetime.datetime.fromisoformat(iso)

    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == datetime.timedelta(0)

    now = datetime.datetime.now(datetime.timezone.utc)
    assert abs((now - parsed).total_seconds()) < 5


def test_now_iso_sqlite_date_round_trips() -> None:
    """SQLite ``date(_now_iso())`` must equal today's UTC date.

    This is the exact pattern used by the universe-refresh ratio
    query (``date(last_chain_check_at) = date('now')``). Before the
    f-m4-10a fix, ``date()`` returned ``None`` because the strftime
    format produced a malformed string.
    """
    raw = _now_iso()
    today_utc = datetime.datetime.now(datetime.timezone.utc).date().isoformat()

    with sqlite3.connect(":memory:") as conn:
        result = conn.execute("SELECT date(?)", (raw,)).fetchone()[0]

    assert result is not None, f"sqlite3 date() returned NULL for {raw!r}"
    assert len(result) == 10, f"expected YYYY-MM-DD, got {result!r}"
    assert result == today_utc


def test_now_iso_format_includes_seconds_and_microseconds() -> None:
    """Format must be ``YYYY-MM-DDTHH:MM:SS.ffffffZ``.

    Guards against accidental regression to the malformed
    ``%H:%M:%fZ`` template (which omitted the seconds field).
    """
    raw = _now_iso()

    assert raw.endswith("Z")
    # Strict shape: 4-2-2 T 2:2:2.6 Z
    pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
    assert re.match(pattern, raw), f"malformed timestamp: {raw!r}"
