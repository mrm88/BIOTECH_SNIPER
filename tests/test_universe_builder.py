"""Tests for ``biotech_sniper.bulk_universe_scanner.build_universe``.

These tests cover the f-m2-09 universe-expansion contract:

* SECTORS-seed merge — every ticker in
  ``biotech_sniper.state.full_biotech_universe_raw.SECTORS`` is
  inserted with ``source='sectors_seed'`` and ``tier='watch'``.
* CT.gov / SECTORS dedup — tickers present in both source sets do
  NOT get a duplicate row.
* Seed-backed probe TRUE branch — a ticker known to the seed JSON
  ends up with ``tier='tradeable'`` and ``has_options_chain=1``.
* Seed-backed probe FALSE branch — a ticker NOT in the seed JSON
  stays at ``tier='watch'`` with ``has_options_chain=0``.
* Tradeable subset rule (VAL-M2-074): every ``tier='tradeable'`` row
  has ``has_options_chain=1`` and vice versa.
* Idempotent re-run — calling ``build_universe`` twice produces
  identical row counts.
* Watch-tier size (VAL-M2-072): with the real seeds, the build
  yields ≥ 550 ``tier='watch'`` rows.
* Universe table schema (VAL-M2-071): the ``CHECK`` constraint
  rejects bogus tier values; columns match the documented set.

The tests use small in-memory probes and per-test SQLite db files
under ``tmp_path`` so the suite stays hermetic and parallel-safe.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from biotech_sniper import db
from biotech_sniper.bulk_universe_scanner import (
    WATCH_SOURCE_CT_GOV,
    WATCH_SOURCE_SECTORS,
    build_universe,
    load_ct_gov_discovery_tickers,
    load_sectors_seed_tickers,
)
from biotech_sniper.options_chain_probe import (
    OptionsChainProbe,
    SeedBackedProbe,
    load_seed_options_tickers,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StaticProbe(OptionsChainProbe):
    """In-memory probe that knows a fixed set of tickers."""

    def __init__(self, known: set[str]) -> None:
        self._known = {t.upper() for t in known}

    def probe(self, ticker: str) -> bool:
        return ticker.upper() in self._known


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


# ---------------------------------------------------------------------------
# Schema (VAL-M2-071)
# ---------------------------------------------------------------------------


def test_universe_schema_columns_and_check_constraint(tmp_path):
    db_path = tmp_path / "u.db"
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
        cols = _column_names(conn, "universe")
        for required in (
            "ticker",
            "tier",
            "has_options_chain",
            "last_chain_check_at",
            "source",
        ):
            assert required in cols, f"universe.{required} missing"

        # Allowed tiers accepted.
        for tier in ("watch", "tradeable"):
            conn.execute(
                "INSERT INTO universe (ticker, tier, has_options_chain) "
                "VALUES (?, ?, ?)",
                (f"X{tier[:3].upper()}", tier, 0),
            )

        # Unknown tier rejected by CHECK constraint.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO universe (ticker, tier, has_options_chain) "
                "VALUES (?, ?, ?)",
                ("BAD", "bogus", 0),
            )

        # ticker is the primary key — duplicate rejected.
        conn.execute(
            "INSERT INTO universe (ticker, tier, has_options_chain) "
            "VALUES (?, ?, ?)",
            ("DUP", "watch", 0),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO universe (ticker, tier, has_options_chain) "
                "VALUES (?, ?, ?)",
                ("DUP", "watch", 0),
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SECTORS merge + dedup
# ---------------------------------------------------------------------------


def test_build_universe_merges_sectors_seed(tmp_path):
    db_path = tmp_path / "u.db"
    sectors = {"AAA", "BBB", "CCC"}
    ct_gov: set[str] = set()
    probe = _StaticProbe(known=set())  # nothing tradeable

    result = build_universe(
        db_path=db_path,
        sectors_tickers=sectors,
        ct_gov_tickers=ct_gov,
        probe=probe,
    )

    assert result.sectors_seed_added == 3
    assert result.ct_gov_added == 0
    assert result.watch_count == 3
    assert result.tradeable_count == 0

    conn = db.connect(db_path)
    try:
        rows = {
            row["ticker"]: row["source"]
            for row in conn.execute("SELECT ticker, source FROM universe")
        }
    finally:
        conn.close()
    assert rows == {
        "AAA": WATCH_SOURCE_SECTORS,
        "BBB": WATCH_SOURCE_SECTORS,
        "CCC": WATCH_SOURCE_SECTORS,
    }


def test_build_universe_dedup_between_sources(tmp_path):
    """Ticker present in both SECTORS and CT.gov must not duplicate."""
    db_path = tmp_path / "u.db"
    sectors = {"AAA", "BBB"}
    ct_gov = {"BBB", "CCC", "DDD"}  # BBB collides with SECTORS
    probe = _StaticProbe(known=set())

    result = build_universe(
        db_path=db_path,
        sectors_tickers=sectors,
        ct_gov_tickers=ct_gov,
        probe=probe,
    )

    assert result.sectors_seed_added == 2
    # BBB excluded from CT.gov second pass; only CCC + DDD survive.
    assert result.ct_gov_added == 2

    conn = db.connect(db_path)
    try:
        sources = {
            row["ticker"]: row["source"]
            for row in conn.execute("SELECT ticker, source FROM universe")
        }
    finally:
        conn.close()
    # SECTORS wins for BBB on first insert.
    assert sources == {
        "AAA": WATCH_SOURCE_SECTORS,
        "BBB": WATCH_SOURCE_SECTORS,
        "CCC": WATCH_SOURCE_CT_GOV,
        "DDD": WATCH_SOURCE_CT_GOV,
    }


# ---------------------------------------------------------------------------
# Probe TRUE / FALSE branches
# ---------------------------------------------------------------------------


def test_build_universe_promotes_probe_true_to_tradeable(tmp_path):
    db_path = tmp_path / "u.db"
    sectors = {"AAA", "BBB", "CCC"}
    ct_gov = {"DDD"}
    probe = _StaticProbe(known={"BBB", "DDD"})

    result = build_universe(
        db_path=db_path,
        sectors_tickers=sectors,
        ct_gov_tickers=ct_gov,
        probe=probe,
    )

    assert result.tradeable_count == 2
    assert result.watch_count == 2

    conn = db.connect(db_path)
    try:
        tier_map = {
            row["ticker"]: (row["tier"], row["has_options_chain"])
            for row in conn.execute(
                "SELECT ticker, tier, has_options_chain FROM universe"
            )
        }
    finally:
        conn.close()
    assert tier_map["AAA"] == ("watch", 0)
    assert tier_map["BBB"] == ("tradeable", 1)
    assert tier_map["CCC"] == ("watch", 0)
    assert tier_map["DDD"] == ("tradeable", 1)


def test_build_universe_probe_false_keeps_watch(tmp_path):
    db_path = tmp_path / "u.db"
    sectors = {"AAA", "BBB"}
    ct_gov: set[str] = set()
    probe = _StaticProbe(known=set())

    build_universe(
        db_path=db_path,
        sectors_tickers=sectors,
        ct_gov_tickers=ct_gov,
        probe=probe,
    )

    conn = db.connect(db_path)
    try:
        rows = list(
            conn.execute(
                "SELECT ticker, tier, has_options_chain FROM universe"
            )
        )
    finally:
        conn.close()

    assert len(rows) == 2
    for row in rows:
        assert row["tier"] == "watch"
        assert row["has_options_chain"] == 0


# ---------------------------------------------------------------------------
# Tradeable subset rule (VAL-M2-074)
# ---------------------------------------------------------------------------


def test_tradeable_subset_invariants(tmp_path):
    db_path = tmp_path / "u.db"
    sectors = {"AAA", "BBB", "CCC", "DDD"}
    ct_gov: set[str] = set()
    probe = _StaticProbe(known={"AAA", "DDD"})

    build_universe(
        db_path=db_path,
        sectors_tickers=sectors,
        ct_gov_tickers=ct_gov,
        probe=probe,
    )

    conn = db.connect(db_path)
    try:
        # tier='tradeable' AND has_options_chain=0 → must be 0
        bad_tradeable = conn.execute(
            "SELECT COUNT(*) FROM universe WHERE tier='tradeable' "
            "AND has_options_chain=0"
        ).fetchone()[0]
        assert bad_tradeable == 0

        # tier='watch' AND has_options_chain=1 → must be 0
        bad_watch = conn.execute(
            "SELECT COUNT(*) FROM universe WHERE tier='watch' "
            "AND has_options_chain=1"
        ).fetchone()[0]
        assert bad_watch == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_build_universe_idempotent_re_run(tmp_path):
    db_path = tmp_path / "u.db"
    sectors = {"AAA", "BBB", "CCC"}
    ct_gov = {"CCC", "DDD"}
    probe = _StaticProbe(known={"BBB", "DDD"})

    first = build_universe(
        db_path=db_path,
        sectors_tickers=sectors,
        ct_gov_tickers=ct_gov,
        probe=probe,
    )
    second = build_universe(
        db_path=db_path,
        sectors_tickers=sectors,
        ct_gov_tickers=ct_gov,
        probe=probe,
    )

    # Counts are stable.
    assert first.watch_count == second.watch_count
    assert first.tradeable_count == second.tradeable_count

    # No new rows added on second run.
    assert second.sectors_seed_added == 0
    assert second.ct_gov_added == 0
    assert second.duplicates_skipped == first.sectors_seed_added + first.ct_gov_added

    # Total unique tickers equals the union of the two inputs.
    conn = db.connect(db_path)
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM universe"
        ).fetchone()[0]
    finally:
        conn.close()
    assert total == len(sectors | ct_gov)


# ---------------------------------------------------------------------------
# Probe / seed JSON
# ---------------------------------------------------------------------------


def test_seed_backed_probe_loads_tickers_from_json(tmp_path):
    seed_path = tmp_path / "universe_stats.json"
    seed_path.write_text(
        json.dumps({"tickers_with_options": ["AAA", "bbb", " CCC "]}),
        encoding="utf-8",
    )

    probe = SeedBackedProbe(seed_path=seed_path)
    assert probe.probe("AAA") is True
    assert probe.probe("aaa") is True  # case-insensitive
    assert probe.probe("BBB") is True
    assert probe.probe("CCC") is True
    assert probe.probe("ZZZ") is False
    # Empty / non-string inputs are False, not raising.
    assert probe.probe("") is False
    assert probe.probe(None) is False  # type: ignore[arg-type]


def test_seed_backed_probe_missing_file_returns_false(tmp_path):
    probe = SeedBackedProbe(seed_path=tmp_path / "does_not_exist.json")
    assert probe.probe("AAPL") is False
    assert probe.known_tickers == frozenset()


def test_load_seed_options_tickers_handles_missing_field(tmp_path):
    """A seed JSON without ``tickers_with_options`` returns empty set."""
    seed = tmp_path / "stats.json"
    seed.write_text(json.dumps({"date": "2026-01-01"}), encoding="utf-8")
    assert load_seed_options_tickers(seed) == set()


def test_real_seed_options_tickers_count_is_147():
    """The shipped seed JSON lists exactly 147 options-validated tickers."""
    tickers = load_seed_options_tickers()
    assert len(tickers) == 147


# ---------------------------------------------------------------------------
# Real-seed end-to-end (VAL-M2-072 + VAL-M2-073)
# ---------------------------------------------------------------------------


def test_real_seed_build_yields_550_plus_watch_tier(tmp_path):
    """VAL-M2-072: ``tier='watch'`` count must be ≥ 550."""
    db_path = tmp_path / "u.db"
    result = build_universe(db_path=db_path)

    assert result.watch_count >= 550, (
        f"watch tier must be >= 550, got {result.watch_count}"
    )
    # Tradeable subset is the seed's 147.
    assert result.tradeable_count == 147

    conn = db.connect(db_path)
    try:
        # Tradeable subset rule.
        bad_t = conn.execute(
            "SELECT COUNT(*) FROM universe "
            "WHERE tier='tradeable' AND has_options_chain=0"
        ).fetchone()[0]
        bad_w = conn.execute(
            "SELECT COUNT(*) FROM universe "
            "WHERE tier='watch' AND has_options_chain=1"
        ).fetchone()[0]
        assert bad_t == 0
        assert bad_w == 0
    finally:
        conn.close()


def test_real_sectors_seed_present_in_universe(tmp_path):
    """VAL-M2-073: every SECTORS ticker is in the universe table."""
    db_path = tmp_path / "u.db"
    build_universe(db_path=db_path)

    seeds = load_sectors_seed_tickers()
    conn = db.connect(db_path)
    try:
        db_tickers = {
            row[0]
            for row in conn.execute("SELECT ticker FROM universe")
        }
    finally:
        conn.close()
    missing = seeds - db_tickers
    assert not missing, f"SECTORS tickers missing from universe: {sorted(missing)[:20]}"


def test_real_ct_gov_seed_loaded_when_file_present():
    """The CT.gov seed loader returns the 657-ticker list from the JSON."""
    tickers = load_ct_gov_discovery_tickers()
    assert len(tickers) >= 600
    assert "AAPL" not in tickers  # sanity: ticker list is biotech-shaped
