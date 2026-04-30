"""Behavioural tests for f-m2-04 — news_daemon.scope filter.

Covers the full f-m2-04 feature contract and pins the assertion IDs
listed in ``features.json::fulfills``:

* VAL-M2-012 — set equality vs the SQL intersection
  ``russell2k_biotech ∩ universe.tier IN ('watch','tradeable')``.
* VAL-M2-013 — tickers outside the intersection are NEVER polled
  (synthetic ticker not in russell2k_biotech yields zero candidates).
* VAL-M2-014 / VAL-M2-042 — empty ``russell2k_biotech`` logs a single
  WARNING per call, returns an empty set, and the daemon idles
  gracefully without crashing.
* VAL-M2-053 — empty / whitespace-only ticker rows are silently
  rejected by :func:`scope.filter_universe`.
* VAL-M2-054 — a ticker present in ``universe`` (e.g. TSLA) but
  absent from ``russell2k_biotech`` produces zero candidate
  emissions.

Test scaffolding
----------------

The tests use an in-process SQLite database (``tmp_path / "alpha.db"``)
seeded with the minimal schema needed for the JOIN — :sql:`russell2k_biotech`
(via the canonical helper from
:mod:`biotech_sniper.universe.russell_biotech`) and :sql:`universe`.
We do NOT run the full migration runner; the scope module is purely
read-only and only needs the two tables present.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Iterable

import pytest

from biotech_sniper.news_daemon import scope
from biotech_sniper.news_daemon.scope import (
    ALLOWED_TIERS,
    EMPTY_RUSSELL_WARNING_MESSAGE,
    filter_universe,
    load_polled_universe,
    resolve_polled_tickers,
)
from biotech_sniper.universe.russell_biotech import (
    ensure_russell2k_biotech_table,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _create_universe_table(conn: sqlite3.Connection) -> None:
    """Create the minimal ``universe`` schema needed for the JOIN.

    Mirrors the ``CHECK(tier IN ('watch','tradeable'))`` constraint
    from :file:`biotech_sniper/db/schema.sql` so a synthetic
    ``tier='excluded'`` row would be rejected at the storage layer
    just as it is in production. (For VAL-M2-013 we test the
    ``absent from russell2k_biotech`` branch instead, which is the
    contract-permitted alternate path.)
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS universe (
            ticker               TEXT    NOT NULL PRIMARY KEY,
            tier                 TEXT    NOT NULL CHECK(tier IN ('watch', 'tradeable')),
            has_options_chain    BOOLEAN NOT NULL DEFAULT 0,
            last_chain_check_at  TEXT,
            source               TEXT,
            created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )


def _seed_russell2k_row(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    sic: int = 2834,
    cik: str = "0000000001",
) -> None:
    conn.execute(
        "INSERT INTO russell2k_biotech "
        "(ticker, cik, sic, sic_description, as_of_date, fetched_at) "
        "VALUES (?, ?, ?, ?, '2026-04-29', '2026-04-29T00:00:00Z')",
        (ticker, cik, sic, "PHARMACEUTICAL PREPARATIONS"),
    )


def _seed_universe_row(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    tier: str = "watch",
) -> None:
    conn.execute(
        "INSERT INTO universe (ticker, tier) VALUES (?, ?)",
        (ticker, tier),
    )


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    """Seed an SQLite db with russell2k_biotech + universe rows.

    Layout:

    * russell2k_biotech: BIOX, MRNA, VRTX, NONE_TIER (last lacks a
      universe row), TSLA_NOT_BIOTECH (no, this one is ONLY in
      universe — see below).
    * universe (tier='watch'): BIOX, MRNA, NONE_TIER_PEER (a fake to
      ensure unmatched universe rows don't leak through).
    * universe (tier='tradeable'): VRTX, TSLA (TSLA is the
      VAL-M2-054 case — present in universe, absent from
      russell2k_biotech).
    """

    db_path = tmp_path / "alpha.db"
    conn = sqlite3.connect(db_path)
    try:
        ensure_russell2k_biotech_table(conn)
        _create_universe_table(conn)

        for ticker in ("BIOX", "MRNA", "VRTX"):
            _seed_russell2k_row(conn, ticker)
        # NONE_TIER is in russell2k but NOT in universe → must be
        # excluded by the JOIN.
        _seed_russell2k_row(conn, "NONE_TIER")

        _seed_universe_row(conn, "BIOX", tier="watch")
        _seed_universe_row(conn, "MRNA", tier="watch")
        _seed_universe_row(conn, "VRTX", tier="tradeable")
        # TSLA is the VAL-M2-054 case: present in universe with a
        # tradeable tier, but absent from russell2k_biotech.
        _seed_universe_row(conn, "TSLA", tier="tradeable")

        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.fixture
def empty_russell_db(tmp_path: Path) -> Path:
    """SQLite db with both tables present but russell2k_biotech empty.

    Used for VAL-M2-014 / VAL-M2-042 — empty russell2k_biotech logs
    a WARNING and the daemon idles without crashing.
    """

    db_path = tmp_path / "alpha.db"
    conn = sqlite3.connect(db_path)
    try:
        ensure_russell2k_biotech_table(conn)
        _create_universe_table(conn)
        # Universe has rows; russell2k_biotech is empty.
        _seed_universe_row(conn, "BIOX", tier="watch")
        _seed_universe_row(conn, "MRNA", tier="tradeable")
        conn.commit()
    finally:
        conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Module-level surface (VAL-M2-001 spillover — pin public symbols)
# ---------------------------------------------------------------------------


def test_module_public_surface() -> None:
    """The public ``__all__`` covers the symbols the poll loop imports."""

    expected = {
        "ALLOWED_TIERS",
        "EMPTY_RUSSELL_WARNING_MESSAGE",
        "filter_universe",
        "load_polled_universe",
        "resolve_polled_tickers",
    }
    assert expected <= set(scope.__all__)


def test_allowed_tiers_pinned_to_watch_and_tradeable() -> None:
    """The polled tier set is exactly {watch, tradeable}.

    Pinned as a frozenset so callers can intersect against it
    cheaply without worrying about mutation; the literal pair
    matches the universe.tier CHECK constraint in db/schema.sql.
    """

    assert ALLOWED_TIERS == frozenset({"watch", "tradeable"})


def test_empty_warning_message_pinned() -> None:
    """The canonical WARNING text is pinned for VAL-M2-014 inspection."""

    assert "russell2k_biotech empty" in EMPTY_RUSSELL_WARNING_MESSAGE
    assert "M1" in EMPTY_RUSSELL_WARNING_MESSAGE
    assert "idling" in EMPTY_RUSSELL_WARNING_MESSAGE


# ---------------------------------------------------------------------------
# resolve_polled_tickers — set equality (VAL-M2-012)
# ---------------------------------------------------------------------------


def test_resolve_polled_tickers_set_equality(seeded_db: Path) -> None:
    """VAL-M2-012: result equals SQL ``INTERSECT`` on the same db."""

    actual = resolve_polled_tickers(seeded_db)

    # Reference SQL identical to the validator's evidence command.
    conn = sqlite3.connect(seeded_db)
    try:
        expected = {
            row[0]
            for row in conn.execute(
                "SELECT ticker FROM russell2k_biotech "
                "INTERSECT "
                "SELECT ticker FROM universe "
                "WHERE tier IN ('watch','tradeable')"
            )
        }
    finally:
        conn.close()

    assert actual == expected
    assert actual == {"BIOX", "MRNA", "VRTX"}


def test_load_polled_universe_alias_matches_resolve(seeded_db: Path) -> None:
    """The legacy ``load_polled_universe`` alias mirrors ``resolve_polled_tickers``."""

    assert load_polled_universe(seeded_db) == resolve_polled_tickers(seeded_db)


def test_resolve_accepts_pre_opened_connection(seeded_db: Path) -> None:
    """Test seam: callers may supply a shared sqlite3 connection."""

    conn = sqlite3.connect(seeded_db)
    try:
        actual = resolve_polled_tickers(conn=conn)
        assert actual == {"BIOX", "MRNA", "VRTX"}
        # Caller still owns the connection; should be usable after.
        cur = conn.execute("SELECT COUNT(*) FROM russell2k_biotech")
        assert cur.fetchone()[0] == 4  # BIOX, MRNA, VRTX, NONE_TIER
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Outside-intersection tickers are NEVER polled (VAL-M2-013, VAL-M2-054)
# ---------------------------------------------------------------------------


def test_excluded_ticker_never_polled(seeded_db: Path) -> None:
    """VAL-M2-013: a ticker absent from russell2k_biotech is never returned.

    The seeded fixture inserts ``EXCLU`` into universe(tier='tradeable')
    but NOT into russell2k_biotech. The polled set must NOT contain
    ``EXCLU``; downstream filter_universe must also drop it even when
    a synthetic news_events row mentions it.
    """

    # Seed the additional excluded ticker into universe only.
    conn = sqlite3.connect(seeded_db)
    try:
        _seed_universe_row(conn, "EXCLU", tier="tradeable")
        conn.commit()
    finally:
        conn.close()

    polled = resolve_polled_tickers(seeded_db)
    assert "EXCLU" not in polled

    # filter_universe with a synthetic news_events cursor mentioning
    # EXCLU must produce zero matches.
    news_cursor = ["EXCLU", "EXCLU", "BIOX"]
    matched = filter_universe(news_cursor, polled)
    assert matched == {"BIOX"}
    assert "EXCLU" not in matched


def test_non_biotech_in_universe_skipped(seeded_db: Path) -> None:
    """VAL-M2-054: TSLA (universe-only, not in russell2k_biotech) is dropped."""

    polled = resolve_polled_tickers(seeded_db)

    # TSLA is seeded into universe(tier='tradeable') by the fixture
    # but NOT into russell2k_biotech.
    assert "TSLA" not in polled

    # And a synthetic news_events cursor mentioning TSLA passes through
    # filter_universe with zero rows.
    matched = filter_universe(["TSLA"], polled)
    assert matched == set()


def test_universe_only_ticker_with_unrelated_tier_skipped(seeded_db: Path) -> None:
    """A russell2k ticker without any universe row is dropped (NONE_TIER)."""

    polled = resolve_polled_tickers(seeded_db)
    assert "NONE_TIER" not in polled


# ---------------------------------------------------------------------------
# Empty russell2k_biotech (VAL-M2-014, VAL-M2-042)
# ---------------------------------------------------------------------------


def test_empty_russell2k_idle(
    empty_russell_db: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """VAL-M2-014: 3 calls → 3 WARNING lines, zero candidates, no crash."""

    caplog.set_level(logging.WARNING, logger="biotech_sniper.news_daemon.scope")

    results = [resolve_polled_tickers(empty_russell_db) for _ in range(3)]

    # Three calls, three empty results — daemon idles without crash.
    assert results == [set(), set(), set()]

    # Exactly three WARNING records, each containing the canonical
    # empty-russell text. Per-cycle (one per call) — matches
    # VAL-M2-014's "exactly 3 WARNING lines" evidence.
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and EMPTY_RUSSELL_WARNING_MESSAGE in r.getMessage()
    ]
    assert len(warnings) == 3


def test_empty_russell_returns_empty_set_when_db_missing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing db file is treated as empty russell — daemon idles."""

    caplog.set_level(logging.WARNING, logger="biotech_sniper.news_daemon.scope")

    missing = tmp_path / "does_not_exist.db"
    assert not missing.exists()

    result = resolve_polled_tickers(missing)
    assert result == set()
    # Single WARNING per call.
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING


def test_empty_russell_returns_empty_set_when_table_missing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A db file lacking russell2k_biotech is treated as empty."""

    caplog.set_level(logging.WARNING, logger="biotech_sniper.news_daemon.scope")

    db_path = tmp_path / "no_russell.db"
    conn = sqlite3.connect(db_path)
    try:
        _create_universe_table(conn)
        _seed_universe_row(conn, "BIOX", tier="watch")
        conn.commit()
    finally:
        conn.close()

    result = resolve_polled_tickers(db_path)
    assert result == set()
    assert len(caplog.records) == 1
    assert "russell2k_biotech empty" in caplog.records[0].getMessage()


def test_universe_table_missing_logs_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A db lacking the ``universe`` table also returns empty + WARNING."""

    caplog.set_level(logging.WARNING, logger="biotech_sniper.news_daemon.scope")

    db_path = tmp_path / "no_universe.db"
    conn = sqlite3.connect(db_path)
    try:
        ensure_russell2k_biotech_table(conn)
        _seed_russell2k_row(conn, "BIOX")
        conn.commit()
    finally:
        conn.close()

    result = resolve_polled_tickers(db_path)
    assert result == set()
    # The universe-missing branch logs under a different event but
    # at WARNING level — daemon idles either way.
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING


# ---------------------------------------------------------------------------
# filter_universe — empty / whitespace ticker reject (VAL-M2-053)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_ticker",
    ["", "   ", "\t", "\n", "  \n\t  "],
)
def test_empty_ticker_silently_rejected(raw_ticker: str) -> None:
    """VAL-M2-053: empty / whitespace tickers are silently rejected."""

    polled = {"BIOX", "MRNA"}
    matched = filter_universe([raw_ticker, "BIOX"], polled)
    # Only BIOX survives; the empty/whitespace token is dropped.
    assert matched == {"BIOX"}


def test_filter_universe_drops_none_and_non_string() -> None:
    """Non-string sentinel values (None, ints) are silently dropped."""

    polled = {"BIOX"}
    # ``None`` and an int are blank by ``_is_blank``; ``"BIOX"`` survives.
    matched = filter_universe([None, 0, "BIOX"], polled)  # type: ignore[list-item]
    assert matched == {"BIOX"}


def test_filter_universe_strips_surrounding_whitespace() -> None:
    """Tickers with leading/trailing whitespace are stripped before lookup."""

    polled = {"BIOX"}
    matched = filter_universe(["  BIOX  ", "\tBIOX\n"], polled)
    assert matched == {"BIOX"}


def test_filter_universe_case_sensitive() -> None:
    """Comparison is case-sensitive — 'biox' is NOT 'BIOX'."""

    polled = {"BIOX"}
    matched = filter_universe(["biox", "Biox", "BIOX"], polled)
    assert matched == {"BIOX"}


def test_filter_universe_empty_polled_returns_empty() -> None:
    """When the polled set is empty (M1 not run), nothing matches."""

    matched = filter_universe(["BIOX", "MRNA"], set())
    assert matched == set()


def test_filter_universe_handles_empty_iterable() -> None:
    """Empty news_events cursor returns empty set."""

    matched = filter_universe([], {"BIOX"})
    assert matched == set()


def test_filter_universe_dedups_repeated_tickers() -> None:
    """A repeated matching ticker collapses to a single set entry."""

    matched = filter_universe(["BIOX", "BIOX", "BIOX"], {"BIOX"})
    assert matched == {"BIOX"}


# ---------------------------------------------------------------------------
# Universe-tier exclusivity: only watch/tradeable rows count
# ---------------------------------------------------------------------------


def test_only_watch_and_tradeable_tiers_polled(tmp_path: Path) -> None:
    """Universe rows with tier outside {watch, tradeable} are excluded.

    The universe.tier CHECK constraint already restricts inserts to
    watch/tradeable, but if a future schema bump permits more tiers
    the scope filter must continue to exclude them. Here we drop
    the CHECK and seed an ``excluded`` row to verify.
    """

    db_path = tmp_path / "alpha.db"
    conn = sqlite3.connect(db_path)
    try:
        ensure_russell2k_biotech_table(conn)
        # Drop the CHECK constraint so we can seed an "excluded" tier.
        conn.execute(
            """
            CREATE TABLE universe (
                ticker  TEXT    NOT NULL PRIMARY KEY,
                tier    TEXT    NOT NULL,
                has_options_chain BOOLEAN NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        _seed_russell2k_row(conn, "BIOX")
        _seed_russell2k_row(conn, "EXCLU")
        _seed_universe_row(conn, "BIOX", tier="watch")
        # tier='excluded' is permitted by this test-only schema —
        # the scope filter should still drop it.
        conn.execute(
            "INSERT INTO universe (ticker, tier) VALUES (?, ?)",
            ("EXCLU", "excluded"),
        )
        conn.commit()
    finally:
        conn.close()

    polled = resolve_polled_tickers(db_path)
    assert polled == {"BIOX"}
    assert "EXCLU" not in polled


# ---------------------------------------------------------------------------
# End-to-end synthetic flow — VAL-M2-013 + VAL-M2-053 + VAL-M2-054
# ---------------------------------------------------------------------------


def _synthetic_news_cursor() -> Iterable[str]:
    """Mimic a news_events cursor with a mix of allowed / disallowed tickers."""

    return iter(
        [
            "BIOX",          # in russell2k ∩ universe.watch  → keep
            "MRNA",          # in russell2k ∩ universe.watch  → keep
            "VRTX",          # in russell2k ∩ universe.tradeable → keep
            "TSLA",          # universe.tradeable only — drop (VAL-M2-054)
            "EXCLU",         # neither table — drop (VAL-M2-013)
            "",              # empty — drop (VAL-M2-053)
            "   ",           # whitespace — drop (VAL-M2-053)
            "NONE_TIER",     # russell2k only, no universe row — drop
        ]
    )


def test_full_synthetic_filter_only_polls_intersection(seeded_db: Path) -> None:
    """End-to-end: only the russell2k ∩ universe(allowed_tier) tickers survive."""

    polled = resolve_polled_tickers(seeded_db)
    matched = filter_universe(_synthetic_news_cursor(), polled)
    assert matched == {"BIOX", "MRNA", "VRTX"}
    # And every excluded token is genuinely absent — no false positives.
    for excluded in ("TSLA", "EXCLU", "", "   ", "NONE_TIER"):
        assert excluded not in matched
