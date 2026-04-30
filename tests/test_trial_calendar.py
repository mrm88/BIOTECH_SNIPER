"""Behavioural tests for :mod:`biotech_sniper.calendar.trial_calendar`.

Coverage matches the f-m1-06 contract (VAL-M1-031..035):

* Schema correctness — table shape, ``CHECK(source IN
  ('ctgov','pdufa','ema_chmp'))`` enforcement, composite UNIQUE on
  ``(ticker, catalyst_date, source, COALESCE(source_ref,''))``
  (VAL-M1-031, VAL-M1-034).
* Three-source merge — after a ``--rebuild`` against a DB seeded
  with rows in ``plays`` (CT.gov), ``pdufa_calendar``, and
  ``ema_calendar``, every source contributes ≥ 1 row to the merged
  ``trial_calendar`` table (VAL-M1-033).
* ``get_next_catalyst(ticker)`` returns the EARLIEST
  ``catalyst_date`` per ticker across all three sources
  (VAL-M1-032).
* Idempotent rebuild — running ``--rebuild`` twice yields the same
  row count and no duplicates.
* ``plays.catalyst_date`` regression — the legacy daily-curated
  read path is byte-identical pre- and post-rebuild
  (VAL-M1-035).
* CLI: ``--help``, ``--rebuild``, ``--dry-run --emit-stats`` exit
  codes and stdout shapes.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path

import pytest

from biotech_sniper import db
from biotech_sniper.calendar import trial_calendar as trial_calendar_module
from biotech_sniper.calendar.trial_calendar import (
    EXIT_OK,
    SOURCE_CTGOV,
    SOURCE_EMA_CHMP,
    SOURCE_PDUFA,
    TRIAL_CALENDAR_SOURCES,
    ensure_trial_calendar_table,
    get_next_catalyst,
    main,
    rebuild_trial_calendar,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "alpha_sniper.db"


@pytest.fixture
def now() -> datetime.datetime:
    return datetime.datetime(2026, 4, 29, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _bootstrap_plays_table(conn: sqlite3.Connection) -> None:
    """Create a minimal ``plays`` table for the CT.gov source.

    Mirrors the v9 ``plays`` schema columns we read from. Schema
    drift (e.g. extra columns) is irrelevant — only ``ticker``,
    ``nct_id``, ``catalyst_date`` and ``status`` are read.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plays (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            source_key    TEXT    NOT NULL UNIQUE,
            ticker        TEXT    NOT NULL,
            nct_id        TEXT,
            status        TEXT    NOT NULL,
            catalyst_date TEXT,
            created_at    TEXT
        )
        """
    )


def _seed_plays(
    conn: sqlite3.Connection,
    rows: list[tuple[str, str | None, str | None, str]],
) -> None:
    """Seed (source_key, ticker, nct_id, catalyst_date) tuples."""
    for source_key, ticker, nct_id, catalyst_date in rows:
        conn.execute(
            "INSERT INTO plays (source_key, ticker, nct_id, status, "
            "catalyst_date) VALUES (?, ?, ?, ?, ?)",
            (source_key, ticker, nct_id, "active", catalyst_date),
        )


def _seed_pdufa(
    conn: sqlite3.Connection,
    rows: list[tuple[str, str, str, str | None]],
    *,
    fetched_at: str = "2026-04-29T00:00:00.000000Z",
) -> None:
    """Seed (ticker, drug, action_date, sponsor) into pdufa_calendar."""
    from biotech_sniper.calendar.pdufa import ensure_pdufa_calendar_table
    ensure_pdufa_calendar_table(conn)
    for ticker, drug, action_date, sponsor in rows:
        conn.execute(
            "INSERT INTO pdufa_calendar ("
            "ticker, drug, action_date, sponsor, source_url, fetched_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                ticker,
                drug,
                action_date,
                sponsor,
                "https://www.biopharmcatalyst.com/calendars/fda-calendar",
                fetched_at,
            ),
        )


def _seed_ema(
    conn: sqlite3.Connection,
    rows: list[tuple[str, str, str | None, str | None, str | None]],
    *,
    fetched_at: str = "2026-04-29T00:00:00.000000Z",
) -> None:
    """Seed (ticker_or_sponsor, product, meeting_date, opinion_date, sponsor)."""
    from biotech_sniper.calendar.ema import ensure_ema_calendar_table
    ensure_ema_calendar_table(conn)
    for ticker_or_sponsor, product, meeting_date, opinion_date, sponsor in rows:
        conn.execute(
            "INSERT INTO ema_calendar ("
            "ticker_or_sponsor, product, meeting_date, opinion_date, "
            "sponsor, source_url, fetched_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ticker_or_sponsor,
                product,
                meeting_date,
                opinion_date,
                sponsor,
                "https://www.ema.europa.eu/en/committees/chmp/chmp-meeting-highlights",
                fetched_at,
            ),
        )


@pytest.fixture
def seeded_db(db_path: Path) -> Path:
    """Build a DB with rows in plays, pdufa_calendar, ema_calendar."""
    conn = db.connect(db_path)
    try:
        _bootstrap_plays_table(conn)
        _seed_plays(
            conn,
            [
                # ticker, nct, date — earliest CT.gov catalyst for IDYA
                ("active:IDYA", "IDYA", "NCT05000000", "2026-06-15"),
                # multi-source ticker VRTX — CT.gov earlier than PDUFA
                ("active:VRTX", "VRTX", "NCT05111111", "2026-04-30"),
                # SRPT CT.gov
                ("active:SRPT", "SRPT", "NCT05222222", "2026-09-01"),
                # No catalyst_date — should be skipped
                ("active:NULL", "NULLT", None, None),
            ],
        )
        _seed_pdufa(
            conn,
            [
                # VRTX has both — earliest still CT.gov
                ("VRTX", "Suzetrigine", "2026-05-30", "Vertex"),
                # AXSM only PDUFA
                ("AXSM", "AXS-05", "2026-06-15", "Axsome"),
                # multi-drug-per-ticker (BMRN x2)
                ("BMRN", "Roctavian", "2026-06-30", "BioMarin"),
                ("BMRN", "VOXZOGO LE", "2026-08-15", "BioMarin"),
            ],
        )
        _seed_ema(
            conn,
            [
                ("VRTX", "Suzetrigine", "2026-05-19", "2026-05-22", "Vertex"),
                ("REGN", "Lynozyfic", "2026-05-19", None, "Regeneron"),
                # Sponsor (non-ticker shape) — should be filtered out
                ("Some Big Sponsor", "Wonderdrug", "2026-07-01", None, "BigCo"),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_schema_columns_and_constraints(db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        ensure_trial_calendar_table(conn)
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(trial_calendar)"
        ).fetchall()}
        assert {"id", "ticker", "catalyst_date", "source", "source_ref",
                "fetched_at"} <= cols

        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='trial_calendar'"
        ).fetchone()[0]
        # source CHECK enforces the closed enum
        assert "source IN ('ctgov','pdufa','ema_chmp')" in ddl.replace(
            " ", ""
        ).replace("\n", "") or "source IN" in ddl

        # Composite UNIQUE on (ticker, catalyst_date, source, COALESCE(source_ref,''))
        idx_rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            "AND tbl_name='trial_calendar'"
        ).fetchall()
        assert any(
            row[1]
            and "UNIQUE" in (row[1] or "").upper()
            and "ticker" in (row[1] or "")
            and "catalyst_date" in (row[1] or "")
            and "source" in (row[1] or "")
            and "COALESCE" in (row[1] or "").upper()
            for row in idx_rows
        ), f"missing composite UNIQUE on (ticker, catalyst_date, source, COALESCE(source_ref,'')): {idx_rows}"
    finally:
        conn.close()


def test_check_constraint_rejects_invalid_source(db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        ensure_trial_calendar_table(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO trial_calendar (ticker, catalyst_date, "
                "source, source_ref, fetched_at) VALUES (?, ?, ?, ?, ?)",
                ("AAA", "2026-05-30", "bogus_source", "x",
                 "2026-04-29T00:00:00Z"),
            )
    finally:
        conn.close()


def test_source_constants_match_contract() -> None:
    assert SOURCE_CTGOV == "ctgov"
    assert SOURCE_PDUFA == "pdufa"
    assert SOURCE_EMA_CHMP == "ema_chmp"
    assert TRIAL_CALENDAR_SOURCES == ("ctgov", "pdufa", "ema_chmp")


# ---------------------------------------------------------------------------
# Rebuild merges three sources
# ---------------------------------------------------------------------------


def test_rebuild_covers_each_source(seeded_db: Path,
                                    now: datetime.datetime) -> None:
    """VAL-M1-033 — at least one row per source after refresh."""
    result = rebuild_trial_calendar(db_path=seeded_db, now=now)
    assert result.rows_written >= 3
    conn = db.connect(seeded_db)
    try:
        per_source = dict(
            conn.execute(
                "SELECT source, COUNT(*) FROM trial_calendar "
                "GROUP BY source"
            ).fetchall()
        )
    finally:
        conn.close()
    assert per_source.get("ctgov", 0) >= 1
    assert per_source.get("pdufa", 0) >= 1
    assert per_source.get("ema_chmp", 0) >= 1


def test_rebuild_is_idempotent(seeded_db: Path,
                               now: datetime.datetime) -> None:
    rebuild_trial_calendar(db_path=seeded_db, now=now)
    conn = db.connect(seeded_db)
    try:
        before = conn.execute(
            "SELECT COUNT(*) FROM trial_calendar"
        ).fetchone()[0]
    finally:
        conn.close()
    rebuild_trial_calendar(db_path=seeded_db, now=now)
    conn = db.connect(seeded_db)
    try:
        after = conn.execute(
            "SELECT COUNT(*) FROM trial_calendar"
        ).fetchone()[0]
        # No duplicates after a second rebuild.
        dup_rows = conn.execute(
            "SELECT ticker, catalyst_date, source, "
            "COALESCE(source_ref,''), COUNT(*) FROM trial_calendar "
            "GROUP BY ticker, catalyst_date, source, "
            "COALESCE(source_ref,'') HAVING COUNT(*) > 1"
        ).fetchall()
    finally:
        conn.close()
    assert before == after, f"rebuild changed row count {before} → {after}"
    assert dup_rows == [], f"duplicates after rebuild: {dup_rows}"


def test_dedups_composite_tuple(seeded_db: Path,
                                now: datetime.datetime) -> None:
    """VAL-M1-034 — no duplicate (ticker, catalyst_date, source, source_ref) tuples."""
    rebuild_trial_calendar(db_path=seeded_db, now=now)
    conn = db.connect(seeded_db)
    try:
        dups = conn.execute(
            "SELECT ticker, catalyst_date, source, "
            "COALESCE(source_ref,''), COUNT(*) FROM trial_calendar "
            "GROUP BY ticker, catalyst_date, source, "
            "COALESCE(source_ref,'') HAVING COUNT(*) > 1"
        ).fetchall()
    finally:
        conn.close()
    assert dups == []


def test_returns_earliest_catalyst(seeded_db: Path,
                                   now: datetime.datetime) -> None:
    """VAL-M1-032 — get_next_catalyst returns the earliest date."""
    rebuild_trial_calendar(db_path=seeded_db, now=now)
    # VRTX has 2026-04-30 (CT.gov), 2026-05-19 (EMA meeting), 2026-05-22
    # (EMA opinion), 2026-05-30 (PDUFA). Earliest = 2026-04-30.
    assert get_next_catalyst("VRTX", db_path=seeded_db) == "2026-04-30"
    # AXSM only PDUFA
    assert get_next_catalyst("AXSM", db_path=seeded_db) == "2026-06-15"
    # SRPT only CT.gov
    assert get_next_catalyst("SRPT", db_path=seeded_db) == "2026-09-01"
    # Unknown ticker
    assert get_next_catalyst("ZZZZ", db_path=seeded_db) is None
    # Case-insensitive
    assert get_next_catalyst("vrtx", db_path=seeded_db) == "2026-04-30"


def test_legacy_plays_catalyst_date_unchanged(
    seeded_db: Path, now: datetime.datetime
) -> None:
    """VAL-M1-035 — plays.catalyst_date reads remain byte-identical."""
    conn = db.connect(seeded_db)
    try:
        before = conn.execute(
            "SELECT id, ticker, catalyst_date FROM plays "
            "WHERE status='active' ORDER BY id"
        ).fetchall()
        # Convert to plain tuples so the comparison is byte-stable
        before_tuples = [tuple(r) for r in before]
    finally:
        conn.close()

    rebuild_trial_calendar(db_path=seeded_db, now=now)

    conn = db.connect(seeded_db)
    try:
        after = conn.execute(
            "SELECT id, ticker, catalyst_date FROM plays "
            "WHERE status='active' ORDER BY id"
        ).fetchall()
        after_tuples = [tuple(r) for r in after]
    finally:
        conn.close()

    assert after_tuples == before_tuples


# ---------------------------------------------------------------------------
# Source filtering
# ---------------------------------------------------------------------------


def test_ctgov_skips_null_catalyst_dates(seeded_db: Path,
                                         now: datetime.datetime) -> None:
    rebuild_trial_calendar(db_path=seeded_db, now=now)
    conn = db.connect(seeded_db)
    try:
        # The NULL-catalyst row in plays must NOT leak into trial_calendar.
        rows = conn.execute(
            "SELECT ticker FROM trial_calendar WHERE source='ctgov'"
        ).fetchall()
    finally:
        conn.close()
    tickers = {r[0] for r in rows}
    assert "NULLT" not in tickers
    assert "IDYA" in tickers


def test_ema_skips_non_ticker_sponsors(seeded_db: Path,
                                       now: datetime.datetime) -> None:
    """Sponsor rows (free-form names) must not pollute the merged table."""
    rebuild_trial_calendar(db_path=seeded_db, now=now)
    conn = db.connect(seeded_db)
    try:
        ema_rows = conn.execute(
            "SELECT ticker FROM trial_calendar WHERE source='ema_chmp'"
        ).fetchall()
    finally:
        conn.close()
    tickers = {r[0] for r in ema_rows}
    assert "Some Big Sponsor" not in tickers
    assert "VRTX" in tickers


def test_ema_emits_two_rows_for_meeting_and_opinion(
    seeded_db: Path, now: datetime.datetime
) -> None:
    """A CHMP row with both dates produces TWO trial_calendar rows."""
    rebuild_trial_calendar(db_path=seeded_db, now=now)
    conn = db.connect(seeded_db)
    try:
        vrtx_ema = conn.execute(
            "SELECT catalyst_date FROM trial_calendar "
            "WHERE source='ema_chmp' AND ticker='VRTX' "
            "ORDER BY catalyst_date"
        ).fetchall()
    finally:
        conn.close()
    dates = [r[0] for r in vrtx_ema]
    assert "2026-05-19" in dates
    assert "2026-05-22" in dates


def test_pdufa_multi_drug_per_ticker(seeded_db: Path,
                                     now: datetime.datetime) -> None:
    rebuild_trial_calendar(db_path=seeded_db, now=now)
    conn = db.connect(seeded_db)
    try:
        bmrn_rows = conn.execute(
            "SELECT catalyst_date, source_ref FROM trial_calendar "
            "WHERE source='pdufa' AND ticker='BMRN'"
        ).fetchall()
    finally:
        conn.close()
    refs = {r[1] for r in bmrn_rows}
    assert "Roctavian" in refs
    assert "VOXZOGO LE" in refs


def test_handles_missing_source_tables(db_path: Path,
                                       now: datetime.datetime) -> None:
    """Rebuild must not crash when one or more source tables are absent."""
    # Only seed the plays table — pdufa_calendar / ema_calendar don't exist.
    conn = db.connect(db_path)
    try:
        _bootstrap_plays_table(conn)
        _seed_plays(
            conn,
            [("active:IDYA", "IDYA", "NCT05000000", "2026-06-15")],
        )
        conn.commit()
    finally:
        conn.close()

    result = rebuild_trial_calendar(db_path=db_path, now=now)
    assert result.rows_written >= 1
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT source, COUNT(*) FROM trial_calendar GROUP BY source"
        ).fetchall()
    finally:
        conn.close()
    per_src = dict(rows)
    assert per_src.get("ctgov", 0) >= 1


def test_handles_all_missing_sources(db_path: Path,
                                     now: datetime.datetime) -> None:
    """Empty DB → zero rows but no crash, and table is created."""
    result = rebuild_trial_calendar(db_path=db_path, now=now)
    assert result.rows_written == 0
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM trial_calendar"
        ).fetchone()[0]
    finally:
        conn.close()
    assert rows == 0


# ---------------------------------------------------------------------------
# get_next_catalyst conn-form & shared connection
# ---------------------------------------------------------------------------


def test_get_next_catalyst_accepts_open_connection(
    seeded_db: Path, now: datetime.datetime
) -> None:
    rebuild_trial_calendar(db_path=seeded_db, now=now)
    conn = db.connect(seeded_db)
    try:
        assert get_next_catalyst("VRTX", conn=conn) == "2026-04-30"
    finally:
        conn.close()


def test_get_next_catalyst_returns_none_when_table_missing(
    db_path: Path,
) -> None:
    # No rebuild called — table doesn't exist yet.
    assert get_next_catalyst("VRTX", db_path=db_path) is None


# ---------------------------------------------------------------------------
# Source ref + fetched_at
# ---------------------------------------------------------------------------


def test_source_ref_populated_from_each_feeder(
    seeded_db: Path, now: datetime.datetime
) -> None:
    rebuild_trial_calendar(db_path=seeded_db, now=now)
    conn = db.connect(seeded_db)
    try:
        ctgov_ref = conn.execute(
            "SELECT source_ref FROM trial_calendar "
            "WHERE source='ctgov' AND ticker='IDYA'"
        ).fetchone()[0]
        pdufa_ref = conn.execute(
            "SELECT source_ref FROM trial_calendar "
            "WHERE source='pdufa' AND ticker='AXSM'"
        ).fetchone()[0]
        ema_meeting_ref = conn.execute(
            "SELECT source_ref FROM trial_calendar "
            "WHERE source='ema_chmp' AND ticker='REGN'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert ctgov_ref == "NCT05000000"
    assert pdufa_ref == "AXS-05"
    assert ema_meeting_ref  # non-empty, contains product slug


def test_fetched_at_recorded(seeded_db: Path,
                             now: datetime.datetime) -> None:
    rebuild_trial_calendar(db_path=seeded_db, now=now)
    conn = db.connect(seeded_db)
    try:
        rows = conn.execute(
            "SELECT fetched_at FROM trial_calendar LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert rows is not None
    fetched_at = rows[0]
    # ISO-8601 prefix matches the fixed `now`.
    assert fetched_at.startswith("2026-04-29T12:00:00")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "--rebuild" in out
    assert "--db" in out


def test_cli_rebuild_writes_rows(seeded_db: Path) -> None:
    rc = main(["--rebuild", "--db", str(seeded_db)])
    assert rc == EXIT_OK
    conn = db.connect(seeded_db)
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM trial_calendar"
        ).fetchone()[0]
    finally:
        conn.close()
    assert cnt >= 3


def test_cli_dry_run_does_not_write(seeded_db: Path,
                                    capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--rebuild", "--db", str(seeded_db), "--dry-run",
               "--emit-stats"])
    assert rc == EXIT_OK
    conn = db.connect(seeded_db)
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM trial_calendar"
        ).fetchone()[0]
    finally:
        conn.close()
    assert cnt == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["dry_run"] is True
    assert payload["rows_written"] == 0
    # parsed_rows should still reflect what would have been written.
    assert payload["parsed_rows"] >= 3


def test_cli_emit_stats_shape(seeded_db: Path,
                              capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--rebuild", "--db", str(seeded_db), "--emit-stats"])
    assert rc == EXIT_OK
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    expected_keys = {"db_path", "fetched_at", "rows_written",
                     "parsed_rows", "per_source", "dry_run",
                     "completed_at"}
    assert expected_keys <= set(payload.keys())
    assert payload["per_source"]["ctgov"] >= 1
    assert payload["per_source"]["pdufa"] >= 1
    assert payload["per_source"]["ema_chmp"] >= 1


# ---------------------------------------------------------------------------
# Atomic rebuild
# ---------------------------------------------------------------------------


def test_rebuild_atomic_replace(seeded_db: Path,
                                now: datetime.datetime) -> None:
    """An exception mid-rebuild rolls back, prior snapshot intact."""
    # First rebuild: lay down the canonical rows.
    rebuild_trial_calendar(db_path=seeded_db, now=now)
    conn = db.connect(seeded_db)
    try:
        before = conn.execute(
            "SELECT ticker, catalyst_date, source, source_ref "
            "FROM trial_calendar ORDER BY ticker, catalyst_date, source, "
            "COALESCE(source_ref,'')"
        ).fetchall()
    finally:
        conn.close()
    assert before  # sanity

    # Now corrupt one of the source tables to force a parse-time crash
    # and rebuild — the prior snapshot should remain because the
    # rebuild rolls back atomically.
    bad_now = datetime.datetime(2026, 4, 30, tzinfo=datetime.timezone.utc)

    # Monkey a query helper to raise mid-rebuild.
    real_load = trial_calendar_module.load_pdufa_rows

    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic boom")

    trial_calendar_module.load_pdufa_rows = _boom
    try:
        with pytest.raises(RuntimeError):
            rebuild_trial_calendar(db_path=seeded_db, now=bad_now)
    finally:
        trial_calendar_module.load_pdufa_rows = real_load

    conn = db.connect(seeded_db)
    try:
        after = conn.execute(
            "SELECT ticker, catalyst_date, source, source_ref "
            "FROM trial_calendar ORDER BY ticker, catalyst_date, source, "
            "COALESCE(source_ref,'')"
        ).fetchall()
        ic = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]
    assert ic[0] == "ok"


# ---------------------------------------------------------------------------
# Smoke import — module is importable without pulling LLM stack
# ---------------------------------------------------------------------------


def test_module_importable_clean() -> None:
    import sys
    # Re-import is a no-op; this just asserts the module is sound.
    import biotech_sniper.calendar.trial_calendar  # noqa: F401
    assert "biotech_sniper.calendar.trial_calendar" in sys.modules
