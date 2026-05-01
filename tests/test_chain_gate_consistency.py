"""Chain-gate DB-level consistency regression tests (f-m3-24).

VAL-M3-045 mandates the invariant:

    For every ticker scored in ``scoring_cache``, the corresponding
    ``universe`` row has ``has_options_chain=1``.

The user-testing round 1 evidence at
``evidence/m3-alpaca/group-b-036-070/VAL-M3-045.log`` recorded
``5`` violating rows in production — synthetic ``SMOK1..5`` tickers
injected by the f-m2-19 / f-m2-22 smoke runs BEFORE f-m3-08 wired
the chain gate (and BEFORE f-m3-16 closed the empty-lookup bypass).
The chain-gate code paths themselves are correct; the violation is
stale data from a prior smoke run.

f-m3-24 adds
:func:`biotech_sniper.db.cleanup_scoring_cache_chain_gate_violations`
and wires it into :func:`biotech_sniper.db.run_migrations` so the
invariant is self-healing — every ``connect()`` re-asserts the join
returns zero rows. This module locks that contract in.

The tests avoid the real production db by creating an isolated
SQLite db per ``tmp_path`` and exercising the migration flow
end-to-end.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from biotech_sniper import db as _db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_universe(
    conn,
    *,
    tradeable: list[str] | None = None,
    watch_only: list[str] | None = None,
) -> None:
    """Insert ``universe`` rows. ``tradeable`` → has_options_chain=1."""
    for ticker in tradeable or []:
        conn.execute(
            "INSERT INTO universe (ticker, tier, has_options_chain) "
            "VALUES (?, 'tradeable', 1)",
            (ticker,),
        )
    for ticker in watch_only or []:
        conn.execute(
            "INSERT INTO universe (ticker, tier, has_options_chain) "
            "VALUES (?, 'watch', 0)",
            (ticker,),
        )


def _seed_scoring_cache(conn, tickers: list[str], as_of_date: str) -> None:
    """Insert minimal ``scoring_cache`` rows for ``tickers``."""
    for ticker in tickers:
        conn.execute(
            "INSERT INTO scoring_cache (ticker, as_of_date) VALUES (?, ?)",
            (ticker, as_of_date),
        )


def _violations(conn) -> int:
    """Return the count from the canonical VAL-M3-045 query."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
          FROM scoring_cache sc
          LEFT JOIN universe u ON sc.ticker = u.ticker
         WHERE u.has_options_chain IS NULL
            OR u.has_options_chain = 0
        """
    ).fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# Cleanup helper — direct unit tests
# ---------------------------------------------------------------------------


def test_cleanup_deletes_rows_missing_universe(tmp_path: Path) -> None:
    """Tickers absent from ``universe`` MUST be deleted."""
    conn = _db.connect(tmp_path / "alpha.db")
    try:
        _db.run_migrations(conn)
        _seed_universe(conn, tradeable=["AAA"])
        # SMOK1 is a synthetic stale ticker with no universe row.
        _seed_scoring_cache(
            conn, ["AAA", "SMOK1"], as_of_date="2026-04-27"
        )
        assert _violations(conn) == 1

        deleted = _db.cleanup_scoring_cache_chain_gate_violations(conn)
        assert deleted == 1
        assert _violations(conn) == 0
        # The legitimate ticker survives the cleanup.
        kept = conn.execute(
            "SELECT ticker FROM scoring_cache"
        ).fetchall()
        assert [row["ticker"] for row in kept] == ["AAA"]
    finally:
        conn.close()


def test_cleanup_deletes_watch_only_tickers(tmp_path: Path) -> None:
    """``has_options_chain=0`` rows MUST be deleted (watch tier)."""
    conn = _db.connect(tmp_path / "alpha.db")
    try:
        _db.run_migrations(conn)
        _seed_universe(conn, tradeable=["AAA"], watch_only=["WWW"])
        _seed_scoring_cache(
            conn, ["AAA", "WWW"], as_of_date="2026-04-27"
        )
        assert _violations(conn) == 1

        deleted = _db.cleanup_scoring_cache_chain_gate_violations(conn)
        assert deleted == 1
        assert _violations(conn) == 0
    finally:
        conn.close()


def test_cleanup_is_idempotent_on_clean_db(tmp_path: Path) -> None:
    """Re-running the cleanup against a clean db is a no-op."""
    conn = _db.connect(tmp_path / "alpha.db")
    try:
        _db.run_migrations(conn)
        _seed_universe(conn, tradeable=["AAA", "BBB"])
        _seed_scoring_cache(
            conn, ["AAA", "BBB"], as_of_date="2026-04-27"
        )
        # Three back-to-back cleanups should each delete zero rows
        # because every scoring_cache ticker is tradeable.
        assert _db.cleanup_scoring_cache_chain_gate_violations(conn) == 0
        assert _db.cleanup_scoring_cache_chain_gate_violations(conn) == 0
        assert _db.cleanup_scoring_cache_chain_gate_violations(conn) == 0
        assert _violations(conn) == 0
        # Both rows survive every iteration.
        kept = {
            row["ticker"]
            for row in conn.execute(
                "SELECT ticker FROM scoring_cache"
            ).fetchall()
        }
        assert kept == {"AAA", "BBB"}
    finally:
        conn.close()


def test_cleanup_logs_warning_when_deleting(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Operators MUST see a WARNING line when stale rows are deleted."""
    conn = _db.connect(tmp_path / "alpha.db")
    try:
        _db.run_migrations(conn)
        _seed_universe(conn, tradeable=["AAA"])
        _seed_scoring_cache(
            conn, ["SMOK1", "SMOK2", "AAA"], as_of_date="2026-04-27"
        )

        with caplog.at_level(logging.WARNING, logger="biotech_sniper.db"):
            deleted = _db.cleanup_scoring_cache_chain_gate_violations(conn)

        assert deleted == 2
        warnings = [
            rec.getMessage()
            for rec in caplog.records
            if rec.levelno == logging.WARNING
        ]
        assert any(
            "chain_gate_cleanup" in msg and "deleted=2" in msg
            for msg in warnings
        ), f"expected chain_gate_cleanup warning, got: {warnings}"
    finally:
        conn.close()


def test_cleanup_silent_when_tables_missing(tmp_path: Path) -> None:
    """Returns 0 without raising when scoring_cache/universe absent."""
    db_path = tmp_path / "alpha.db"
    conn = _db.connect(db_path)
    try:
        # Skip run_migrations — neither table exists yet.
        deleted = _db.cleanup_scoring_cache_chain_gate_violations(conn)
        assert deleted == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Integration: run_migrations re-asserts the invariant on every connect
# ---------------------------------------------------------------------------


def test_run_migrations_purges_pre_v9_chain_gate_violations(
    tmp_path: Path,
) -> None:
    """A db whose schema_version<9 MUST be cleaned by run_migrations.

    Reproduces the production VAL-M3-045 scenario: scoring_cache
    rows for synthetic ``SMOK1..5`` tickers that never got a
    ``universe`` row. The next ``run_migrations`` call (which
    upgrades the schema_version to 9) MUST purge them and leave the
    canonical join at ``COUNT(*)=0``.

    The cleanup is gated on ``pre_migration_version < 9`` so it
    runs exactly once per legacy db (and as a no-op on fresh dbs
    that already have an empty ``scoring_cache``). To simulate a
    pre-f-m3-24 db, we build the schema with run_migrations,
    monkey-patch the ``schema_version`` row back to 8, and inject
    the violations alongside legitimate rows.
    """
    db_path = tmp_path / "alpha.db"
    # First connect: build the schema, then downgrade
    # ``schema_version`` to 8 to simulate a pre-f-m3-24 production
    # db. Inject 5 stale ``SMOK`` rows + 2 legitimate ones.
    conn = _db.connect(db_path)
    try:
        _db.run_migrations(conn)
        _seed_universe(conn, tradeable=["AAA", "BBB"])
        _seed_scoring_cache(
            conn,
            ["SMOK1", "SMOK2", "SMOK3", "SMOK4", "SMOK5", "AAA", "BBB"],
            as_of_date="2026-04-27",
        )
        # Force ``schema_version`` back to 8 — exactly the state of
        # the production VPS db at the moment VAL-M3-045 fired.
        conn.execute("DELETE FROM schema_version")
        conn.execute(
            "INSERT INTO schema_version (version, description) "
            "VALUES (8, 'simulated pre-f-m3-24 db')"
        )
        conn.commit()
    finally:
        conn.close()

    # Second connect: confirm the violations are still there before
    # the cleanup migration fires (we open a raw connection so
    # ``run_migrations`` is not implicitly invoked).
    import sqlite3

    raw = sqlite3.connect(db_path)
    raw.row_factory = sqlite3.Row
    try:
        assert _violations(raw) == 5
        version_row = raw.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()
        assert version_row[0] == 8
    finally:
        raw.close()

    # Third connect: this is the real ``connect() + run_migrations``
    # flow that production uses post-f-m3-24. The cleanup MUST fire
    # because the snapshot version (8) is < 9, and the join MUST be
    # zero afterwards.
    conn = _db.connect(db_path)
    try:
        _db.run_migrations(conn)
        assert _violations(conn) == 0
        # Legitimate rows survive.
        kept = {
            row["ticker"]
            for row in conn.execute(
                "SELECT ticker FROM scoring_cache"
            ).fetchall()
        }
        assert kept == {"AAA", "BBB"}
        # schema_version has been bumped to the current version.
        # f-misc-06: CURRENT_VERSION moved from 9 → 10 so the chained
        # equality below now anchors at the active CURRENT_VERSION
        # (the v10/v11 dispatcher in :func:`db.run_migrations` lifts
        # the simulated v8 db all the way to the Reading-B foundations
        # schema). The chain-gate cleanup still fires because the
        # pre-migration version (8) is < 9, which is the gate the
        # cleanup is keyed on. f-misc-09 bumped CURRENT_VERSION to 11.
        latest = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        assert latest == _db.CURRENT_VERSION
        assert _db.CURRENT_VERSION >= 10
    finally:
        conn.close()


def test_run_migrations_skips_cleanup_when_already_v9(
    tmp_path: Path,
) -> None:
    """A db at schema_version>=9 MUST NOT re-run the cleanup.

    Re-running the cleanup on a v9 db would silently delete any
    legitimately-injected ``scoring_cache`` row that doesn't
    happen to live in ``universe`` (every test fixture in the
    suite that pre-dates f-m3-24 follows this pattern). The
    one-shot gate prevents that regression.
    """
    db_path = tmp_path / "alpha.db"
    # First connect builds the schema and bumps to v9.
    conn = _db.connect(db_path)
    try:
        _db.run_migrations(conn)
        # Inject a row with NO universe entry — this would be
        # purged by the cleanup if it fired.
        _seed_scoring_cache(conn, ["LEGACY"], as_of_date="2026-04-27")
        conn.commit()
        assert _violations(conn) == 1
    finally:
        conn.close()

    # Re-open and re-run migrations. Since pre_migration_version
    # is now 9, the cleanup MUST be skipped and the LEGACY row
    # survives.
    conn = _db.connect(db_path)
    try:
        _db.run_migrations(conn)
        # Row should still be there.
        rows = conn.execute(
            "SELECT ticker FROM scoring_cache"
        ).fetchall()
        assert [row["ticker"] for row in rows] == ["LEGACY"]
    finally:
        conn.close()


def test_invariant_holds_after_run_migrations_on_fresh_db(
    tmp_path: Path,
) -> None:
    """A fresh db MUST satisfy the invariant trivially (zero rows)."""
    conn = _db.connect(tmp_path / "alpha.db")
    try:
        _db.run_migrations(conn)
        # Empty scoring_cache + empty universe → join is empty → 0.
        assert _violations(conn) == 0
    finally:
        conn.close()


def test_invariant_holds_when_only_legitimate_rows_present(
    tmp_path: Path,
) -> None:
    """All-tradeable ``scoring_cache`` rows MUST survive migration."""
    conn = _db.connect(tmp_path / "alpha.db")
    try:
        _db.run_migrations(conn)
        _seed_universe(conn, tradeable=["AAA", "BBB", "CCC"])
        _seed_scoring_cache(
            conn, ["AAA", "BBB", "CCC"], as_of_date="2026-04-27"
        )
        conn.commit()
    finally:
        conn.close()

    # Reconnect — even though run_migrations is gated on v<9 (so
    # the cleanup itself won't fire on this v9 db), the join MUST
    # already be zero because every scoring_cache ticker is
    # tradeable in the universe table.
    conn = _db.connect(tmp_path / "alpha.db")
    try:
        _db.run_migrations(conn)
        assert _violations(conn) == 0
        kept = {
            row["ticker"]
            for row in conn.execute(
                "SELECT ticker FROM scoring_cache"
            ).fetchall()
        }
        assert kept == {"AAA", "BBB", "CCC"}
    finally:
        conn.close()


def test_cleanup_cascades_to_llm_debate_rows(tmp_path: Path) -> None:
    """Dependent llm_debate rows MUST be deleted before scoring_cache rows.

    Reproduces the production VPS error encountered while applying
    the f-m3-24 cleanup: SMOK1's scoring_cache row has 3 dependent
    llm_debate rounds (claude→gemini→grok) and SMOK2 has a synthetic
    over-cap debate row. With FOREIGN KEYS ON (the project default
    via ``connect()``), deleting the parent without first removing
    the dependents raises ``sqlite3.IntegrityError``. The cleanup
    cascades through llm_debate so the parent DELETE succeeds.
    """
    conn = _db.connect(tmp_path / "alpha.db")
    try:
        _db.run_migrations(conn)
        _seed_universe(conn, tradeable=["AAA"])
        _seed_scoring_cache(
            conn, ["AAA", "SMOK1", "SMOK2"], as_of_date="2026-04-27"
        )
        # Get the scoring_cache.id for the violators so we can
        # attach llm_debate rows.
        smok_ids = {
            row["ticker"]: row["id"]
            for row in conn.execute(
                "SELECT id, ticker FROM scoring_cache "
                "WHERE ticker IN ('SMOK1', 'SMOK2')"
            ).fetchall()
        }
        # Three rounds for SMOK1 (mirrors the f-m2-19 cassette
        # debate transcript), one synthetic over-cap row for SMOK2.
        for idx, model in enumerate(
            ("claude-opus-4-1", "gemini-2.5-pro", "grok-4"), start=1
        ):
            conn.execute(
                "INSERT INTO llm_debate "
                "(scoring_cache_id, trigger, round_index, model) "
                "VALUES (?, 'divergence', ?, ?)",
                (smok_ids["SMOK1"], idx, model),
            )
        conn.execute(
            "INSERT INTO llm_debate "
            "(scoring_cache_id, trigger, round_index, model, cost_usd) "
            "VALUES (?, 'rotation', 1, 'claude-overcap-test', 10.5)",
            (smok_ids["SMOK2"],),
        )
        conn.commit()

        deleted = _db.cleanup_scoring_cache_chain_gate_violations(conn)
        assert deleted == 2

        # Cascade swept all 4 debate rows referencing the violators.
        debate_count = conn.execute(
            "SELECT COUNT(*) FROM llm_debate"
        ).fetchone()[0]
        assert debate_count == 0
        assert _violations(conn) == 0
        # The legitimate ticker survives.
        kept = [
            row["ticker"]
            for row in conn.execute(
                "SELECT ticker FROM scoring_cache"
            ).fetchall()
        ]
        assert kept == ["AAA"]
    finally:
        conn.close()


def test_cleanup_preserves_unrelated_llm_debate_rows(
    tmp_path: Path,
) -> None:
    """Debate rows attached to legitimate scoring_cache rows MUST survive."""
    conn = _db.connect(tmp_path / "alpha.db")
    try:
        _db.run_migrations(conn)
        _seed_universe(conn, tradeable=["AAA"])
        _seed_scoring_cache(
            conn, ["AAA", "SMOK1"], as_of_date="2026-04-27"
        )
        rows = {
            row["ticker"]: row["id"]
            for row in conn.execute(
                "SELECT id, ticker FROM scoring_cache"
            ).fetchall()
        }
        conn.execute(
            "INSERT INTO llm_debate "
            "(scoring_cache_id, trigger, round_index, model) "
            "VALUES (?, 'divergence', 1, 'claude')",
            (rows["AAA"],),
        )
        conn.execute(
            "INSERT INTO llm_debate "
            "(scoring_cache_id, trigger, round_index, model) "
            "VALUES (?, 'divergence', 1, 'claude')",
            (rows["SMOK1"],),
        )
        conn.commit()

        deleted = _db.cleanup_scoring_cache_chain_gate_violations(conn)
        assert deleted == 1

        # Only the legitimate (AAA) debate row remains.
        debate_rows = conn.execute(
            "SELECT scoring_cache_id FROM llm_debate"
        ).fetchall()
        assert len(debate_rows) == 1
        assert debate_rows[0]["scoring_cache_id"] == rows["AAA"]
    finally:
        conn.close()


def test_invariant_holds_across_multiple_dates(tmp_path: Path) -> None:
    """The cleanup considers ALL ``as_of_date`` partitions, not just today."""
    conn = _db.connect(tmp_path / "alpha.db")
    try:
        _db.run_migrations(conn)
        _seed_universe(conn, tradeable=["AAA"])
        # Same stale ticker across two dates — both rows must be
        # purged because a single violator on any date breaks the
        # invariant.
        _seed_scoring_cache(conn, ["SMOK1"], as_of_date="2026-04-26")
        _seed_scoring_cache(conn, ["SMOK1"], as_of_date="2026-04-27")
        _seed_scoring_cache(conn, ["AAA"], as_of_date="2026-04-27")

        deleted = _db.cleanup_scoring_cache_chain_gate_violations(conn)
        assert deleted == 2
        assert _violations(conn) == 0
    finally:
        conn.close()
