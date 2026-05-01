"""Tests for the per-ticker 24h cooldown gate (f-m3-08).

Feature: f-m3-08-cooldown-gate.

Verifies the contract assertions VAL-M3-043, VAL-M3-044, VAL-M3-045,
VAL-M3-046, VAL-M3-047, VAL-M3-070, VAL-M3-083, VAL-M3-100, VAL-M3-101:

* VAL-M3-043 — ``ticker_cooldown`` schema present with ``ticker`` PK,
  ``last_entry_at`` TEXT NOT NULL, ``last_event_id`` INTEGER,
  ``cooldown_hours`` INTEGER NOT NULL DEFAULT 24, FK
  ``last_event_id REFERENCES candidate_events(id)``.
* VAL-M3-044 — ``PER_TICKER_COOLDOWN_HOURS`` env defaults to 24;
  override honored. Read through :mod:`biotech_sniper.config`.
* VAL-M3-045 — Active cooldown blocks entry; LLM calls are NOT
  dispatched (cheap-first short-circuit). Audit reason
  ``cooldown_active`` + ``remaining_seconds``.
* VAL-M3-046 — Expired cooldown allows entry.
* VAL-M3-047 — ``last_entry_at`` is updated ONLY on successful entry
  submission. Failed attempts (probability/unanimity/cap/armed/
  concurrency) do NOT advance the cooldown row.
* VAL-M3-070 — Cooldown UPSERT is idempotent: 10 successive successful
  entries → 1 row, ``last_entry_at`` advances.
* VAL-M3-083 — Per-ticker cooldown is case-insensitive: writes use
  canonical UPPERCASE ticker.
* VAL-M3-100 — Per-row ``cooldown_hours`` override beats env default.
* VAL-M3-101 — ``>=`` boundary semantics: at exactly cooldown_hours
  the gate ALLOWS (inclusive). Single UTC clock per gate evaluation.
"""

from __future__ import annotations

import datetime as _dt
import importlib
import os
import sqlite3
from pathlib import Path

import pytest

from biotech_sniper import config as _config
from biotech_sniper import db as project_db
from biotech_sniper.llm import stage2_gates
from biotech_sniper.llm.stage2_gates import (
    CooldownGateResult,
    GATE_REASON_COOLDOWN_ACTIVE,
    cooldown_gate,
    record_cooldown_on_success,
)
from biotech_sniper.migrations.runner import run as run_v10


# ---------------------------------------------------------------------------
# Module-reload teardown — same pattern as test_stage2_cap_gate.py.
# Ensures PER_TICKER_COOLDOWN_HOURS env vars leaked from one test cannot
# bleed into subsequent xdist-worker tests when the module-level constant
# is read from importlib.reload(_config).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_config_module_after_test():
    yield
    os.environ.pop("PER_TICKER_COOLDOWN_HOURS", None)
    importlib.reload(_config)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Fresh SQLite db with the v10 (Reading-B foundations) schema applied."""
    db_path = tmp_path / "cooldown.db"
    run_v10(db_path, target_version=11, take_backup_first=False)
    return db_path


def _count_ledger(db_path: Path) -> int:
    conn = project_db.connect(db_path)
    try:
        return int(
            conn.execute("SELECT COUNT(*) FROM llm_cost_ledger").fetchone()[0]
        )
    finally:
        conn.close()


def _count_cooldown(db_path: Path, ticker: str | None = None) -> int:
    conn = project_db.connect(db_path)
    try:
        if ticker is None:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM ticker_cooldown"
                ).fetchone()[0]
            )
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM ticker_cooldown WHERE UPPER(ticker)=?",
                (ticker.upper(),),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _read_row(db_path: Path, ticker: str) -> sqlite3.Row | None:
    conn = project_db.connect(db_path)
    try:
        return conn.execute(
            "SELECT ticker, last_entry_at, last_event_id, cooldown_hours "
            "FROM ticker_cooldown WHERE ticker=?",
            (ticker.upper(),),
        ).fetchone()
    finally:
        conn.close()


def _iso(dt: _dt.datetime) -> str:
    """Return UTC ISO-8601 string with millisecond precision (matches DB)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def _seed_candidate_events(
    db_path: Path,
    *,
    n: int,
    ticker: str = "VKTX",
) -> list[int]:
    """Insert ``n`` candidate_events rows + their ``news_events`` parents.

    Returns the list of ``candidate_events.id`` values in insert order
    so callers can satisfy the ``ticker_cooldown.last_event_id`` FK.
    """
    conn = project_db.connect(db_path)
    cand_ids: list[int] = []
    try:
        with conn:
            for i in range(n):
                conn.execute(
                    "INSERT INTO news_events ("
                    "ticker, source, title, url, published_at, ingested_at"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        ticker,
                        "rss",
                        f"{ticker} headline {i}",
                        f"https://example.com/{i}",
                        f"2026-04-30T{12 + i // 60:02d}:{i % 60:02d}:00Z",
                        f"2026-04-30T{12 + i // 60:02d}:{i % 60:02d}:01Z",
                    ),
                )
                nid = conn.execute(
                    "SELECT MAX(id) FROM news_events"
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO candidate_events ("
                    "ticker, source_news_event_id, matched_keywords,"
                    " emitted_at, dedup_key) VALUES (?, ?, ?, ?, ?)",
                    (
                        ticker,
                        nid,
                        "phase_iii,readout",
                        f"2026-04-30T{12 + i // 60:02d}:{i % 60:02d}:02Z",
                        f"dedup-{ticker}-{i:03d}",
                    ),
                )
                cid = conn.execute(
                    "SELECT MAX(id) FROM candidate_events"
                ).fetchone()[0]
                cand_ids.append(int(cid))
    finally:
        conn.close()
    return cand_ids


def _seed_cooldown(
    db_path: Path,
    *,
    ticker: str,
    last_entry_at: str,
    cooldown_hours: int = 24,
    last_event_id: int | None = None,
) -> None:
    """Insert (raw, no canonicalisation) a ticker_cooldown row directly."""
    conn = project_db.connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO ticker_cooldown
                    (ticker, last_entry_at, last_event_id, cooldown_hours)
                VALUES (?, ?, ?, ?)
                """,
                (ticker, last_entry_at, last_event_id, cooldown_hours),
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# VAL-M3-043 — ticker_cooldown schema present
# ---------------------------------------------------------------------------


def test_schema_columns_present(temp_db: Path):
    """``ticker_cooldown`` has the contract-mandated columns and PK."""
    conn = project_db.connect(temp_db)
    try:
        rows = conn.execute(
            "PRAGMA table_info(ticker_cooldown)"
        ).fetchall()
    finally:
        conn.close()
    cols_by_name = {r["name"]: r for r in rows}

    # Contract columns.
    assert "ticker" in cols_by_name
    assert "last_entry_at" in cols_by_name
    assert "last_event_id" in cols_by_name
    assert "cooldown_hours" in cols_by_name

    # ticker is PRIMARY KEY (column-level pk flag in PRAGMA table_info)
    assert int(cols_by_name["ticker"]["pk"]) == 1
    assert cols_by_name["ticker"]["type"].upper() == "TEXT"
    assert int(cols_by_name["ticker"]["notnull"]) == 1

    # last_entry_at NOT NULL TEXT
    assert cols_by_name["last_entry_at"]["type"].upper() == "TEXT"
    assert int(cols_by_name["last_entry_at"]["notnull"]) == 1

    # last_event_id INTEGER, nullable
    assert cols_by_name["last_event_id"]["type"].upper() == "INTEGER"
    assert int(cols_by_name["last_event_id"]["notnull"]) == 0

    # cooldown_hours INTEGER NOT NULL DEFAULT 24
    assert cols_by_name["cooldown_hours"]["type"].upper() == "INTEGER"
    assert int(cols_by_name["cooldown_hours"]["notnull"]) == 1
    assert int(cols_by_name["cooldown_hours"]["dflt_value"]) == 24


def test_schema_fk_to_candidate_events(temp_db: Path):
    """``last_event_id`` is a foreign key to ``candidate_events.id``."""
    conn = project_db.connect(temp_db)
    try:
        fk_rows = conn.execute(
            "PRAGMA foreign_key_list(ticker_cooldown)"
        ).fetchall()
    finally:
        conn.close()
    fks = [(r["table"], r["from"], r["to"]) for r in fk_rows]
    assert ("candidate_events", "last_event_id", "id") in fks


# ---------------------------------------------------------------------------
# VAL-M3-044 — env override defaults to 24 / honored
# ---------------------------------------------------------------------------


def test_cooldown_default_is_24(monkeypatch):
    """With ``PER_TICKER_COOLDOWN_HOURS`` unset the resolved value is 24."""
    monkeypatch.delenv("PER_TICKER_COOLDOWN_HOURS", raising=False)
    importlib.reload(_config)
    assert _config.PER_TICKER_COOLDOWN_HOURS == 24
    assert _config.get_per_ticker_cooldown_hours() == 24


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("12", 12),
        ("48", 48),
        ("72", 72),
        ("  6  ", 6),  # tolerant of surrounding whitespace
    ],
)
def test_cooldown_env_override(monkeypatch, raw, expected):
    """``PER_TICKER_COOLDOWN_HOURS=<v>`` is honored by the getter."""
    monkeypatch.setenv("PER_TICKER_COOLDOWN_HOURS", raw)
    assert _config.get_per_ticker_cooldown_hours() == expected


def test_cooldown_falls_back_to_default_on_unparseable(monkeypatch):
    monkeypatch.setenv("PER_TICKER_COOLDOWN_HOURS", "not-an-int")
    assert _config.get_per_ticker_cooldown_hours() == 24


def test_no_raw_environ_outside_config():
    """The cooldown gate must not read PER_TICKER_COOLDOWN_HOURS via raw
    ``os.environ`` outside :mod:`biotech_sniper.config`."""
    src = Path(stage2_gates.__file__).read_text(encoding="utf-8")
    assert 'os.environ.get("PER_TICKER_COOLDOWN_HOURS"' not in src
    assert "os.environ['PER_TICKER_COOLDOWN_HOURS'" not in src
    assert 'os.getenv("PER_TICKER_COOLDOWN_HOURS"' not in src


# ---------------------------------------------------------------------------
# VAL-M3-045 — Active cooldown blocks entry; ZERO LLM calls dispatched
# ---------------------------------------------------------------------------


def test_active_cooldown_blocks_entry(temp_db: Path):
    """Last entry 12h ago + 24h cooldown → reject with reason
    'cooldown_active' and ``remaining_seconds > 0``."""
    now = _dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)
    twelve_hours_ago = now - _dt.timedelta(hours=12)
    _seed_cooldown(
        temp_db,
        ticker="NVAX",
        last_entry_at=_iso(twelve_hours_ago),
        cooldown_hours=24,
    )

    res = cooldown_gate(
        ticker="NVAX",
        db_path=temp_db,
        now=now,
    )
    assert isinstance(res, CooldownGateResult)
    assert res.passed is False
    assert res.reason == GATE_REASON_COOLDOWN_ACTIVE
    assert res.reason == "cooldown_active"
    assert res.ticker == "NVAX"
    # 12h remaining (24h cooldown - 12h elapsed = 12h = 43200s).
    assert res.remaining_seconds == pytest.approx(43200, abs=2)
    assert res.cooldown_hours == 24


def test_active_cooldown_dispatches_zero_llm_calls(temp_db: Path):
    """Cheap-first short-circuit: cap-gate path inserts ZERO new
    ``llm_cost_ledger`` rows when cooldown blocks. Mirrors VAL-M3-045.
    """
    now = _dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)
    one_hour_ago = now - _dt.timedelta(hours=1)
    _seed_cooldown(
        temp_db,
        ticker="ABCD",
        last_entry_at=_iso(one_hour_ago),
        cooldown_hours=24,
    )
    pre_count = _count_ledger(temp_db)

    res = cooldown_gate(
        ticker="ABCD",
        db_path=temp_db,
        now=now,
    )
    assert res.passed is False
    # No ledger rows added — gate never dispatches LLM.
    assert _count_ledger(temp_db) == pre_count


def test_no_row_for_ticker_allows_entry(temp_db: Path):
    """When no ``ticker_cooldown`` row exists for the ticker, the gate
    passes (first-time entry has no cooldown to honor)."""
    now = _dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)
    res = cooldown_gate(
        ticker="FRESH",
        db_path=temp_db,
        now=now,
    )
    assert res.passed is True
    assert res.reason is None
    assert res.ticker == "FRESH"
    assert res.last_entry_at is None
    assert res.remaining_seconds == 0


# ---------------------------------------------------------------------------
# VAL-M3-046 — Expired cooldown allows entry
# ---------------------------------------------------------------------------


def test_expired_cooldown_allows(temp_db: Path):
    """Last entry 25h ago + 24h cooldown → gate passes."""
    now = _dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)
    twentyfive_hours_ago = now - _dt.timedelta(hours=25)
    _seed_cooldown(
        temp_db,
        ticker="GILD",
        last_entry_at=_iso(twentyfive_hours_ago),
        cooldown_hours=24,
    )

    res = cooldown_gate(
        ticker="GILD",
        db_path=temp_db,
        now=now,
    )
    assert res.passed is True
    assert res.reason is None
    assert res.cooldown_hours == 24
    assert res.remaining_seconds == 0


# ---------------------------------------------------------------------------
# VAL-M3-047 — last_entry_at updated ONLY on successful entry submission
# ---------------------------------------------------------------------------


def test_failed_entries_do_not_update_cooldown(temp_db: Path):
    """The gate is read-only — calling it never writes/upserts the row.

    Probability/unanimity/cap/armed/concurrency rejections happen
    elsewhere; the cooldown row is updated only via
    :func:`record_cooldown_on_success`.
    """
    now = _dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)

    # No row pre-existing; multiple gate evaluations must NEVER write a row.
    for _ in range(5):
        res = cooldown_gate(ticker="NEWX", db_path=temp_db, now=now)
        assert res.passed is True
    assert _count_cooldown(temp_db, "NEWX") == 0

    # Even when a row exists and the gate blocks, no UPDATE happens.
    one_hour_ago = now - _dt.timedelta(hours=1)
    _seed_cooldown(
        temp_db,
        ticker="HOLDX",
        last_entry_at=_iso(one_hour_ago),
        cooldown_hours=24,
    )
    pre_row = _read_row(temp_db, "HOLDX")
    assert pre_row is not None
    pre_last_entry = pre_row["last_entry_at"]

    for _ in range(3):
        res = cooldown_gate(ticker="HOLDX", db_path=temp_db, now=now)
        assert res.passed is False

    post_row = _read_row(temp_db, "HOLDX")
    assert post_row is not None
    assert post_row["last_entry_at"] == pre_last_entry


def test_record_cooldown_on_success_advances_last_entry_at(temp_db: Path):
    """Calling :func:`record_cooldown_on_success` is what advances
    ``last_entry_at``. After the call the row reflects the new
    timestamp."""
    t0 = _dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)
    record_cooldown_on_success(
        ticker="NVAX",
        db_path=temp_db,
        now=t0,
    )
    row = _read_row(temp_db, "NVAX")
    assert row is not None
    assert row["ticker"] == "NVAX"
    # last_entry_at stored as ISO with the gate's timestamp.
    assert "2026-04-30" in row["last_entry_at"]
    # Default cooldown_hours when not specified = 24.
    assert int(row["cooldown_hours"]) == 24


# ---------------------------------------------------------------------------
# VAL-M3-070 — UPSERT idempotent: 10 successive entries → 1 row
# ---------------------------------------------------------------------------


def test_upsert_idempotent_ten_entries(temp_db: Path):
    """Ten successive successful entries on the same ticker resolve to
    exactly one row whose ``last_entry_at`` reflects the LAST call.

    Mirrors VAL-M3-070.
    """
    # Seed 10 candidate_events rows so the FK on last_event_id is satisfied.
    cand_ids = _seed_candidate_events(temp_db, n=10, ticker="VKTX")

    base = _dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)
    for i in range(10):
        record_cooldown_on_success(
            ticker="VKTX",
            db_path=temp_db,
            now=base + _dt.timedelta(seconds=i),
            last_event_id=cand_ids[i],
        )

    assert _count_cooldown(temp_db, "VKTX") == 1
    row = _read_row(temp_db, "VKTX")
    assert row is not None
    # Last call was base + 9s; that should be the persisted timestamp.
    assert "2026-04-30" in row["last_entry_at"]
    # last_event_id reflects the most recent call.
    assert int(row["last_event_id"]) == cand_ids[-1]


def test_upsert_advances_last_entry_at(temp_db: Path):
    """After two successful entries, ``last_entry_at`` reflects the SECOND
    entry (PRIMARY KEY upsert, not insert-or-ignore)."""
    t0 = _dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)
    t1 = t0 + _dt.timedelta(hours=25)

    record_cooldown_on_success(ticker="MRNA", db_path=temp_db, now=t0)
    row0 = _read_row(temp_db, "MRNA")
    assert row0 is not None
    first_ts = row0["last_entry_at"]

    record_cooldown_on_success(ticker="MRNA", db_path=temp_db, now=t1)
    row1 = _read_row(temp_db, "MRNA")
    assert row1 is not None
    second_ts = row1["last_entry_at"]

    # Same ticker → still one row.
    assert _count_cooldown(temp_db, "MRNA") == 1
    # Timestamp advanced.
    assert second_ts > first_ts


# ---------------------------------------------------------------------------
# VAL-M3-083 — Mixed-case ticker → canonical UPPERCASE on write
# ---------------------------------------------------------------------------


def test_mixed_case_ticker_canonical_uppercase(temp_db: Path):
    """A cooldown set on lowercase ``nvax`` and a candidate with
    UPPERCASE ``NVAX`` resolve to the SAME row."""
    t0 = _dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)

    # Write with lowercase — canonicaliser must UPPERCASE it.
    record_cooldown_on_success(ticker="nvax", db_path=temp_db, now=t0)

    # Read with mixed case — must hit the same row.
    res_upper = cooldown_gate(ticker="NVAX", db_path=temp_db, now=t0)
    res_mixed = cooldown_gate(ticker="NvAx", db_path=temp_db, now=t0)
    res_lower = cooldown_gate(ticker="nvax", db_path=temp_db, now=t0)

    # All three see the same active cooldown.
    assert res_upper.passed is False
    assert res_mixed.passed is False
    assert res_lower.passed is False
    assert res_upper.ticker == "NVAX"
    assert res_mixed.ticker == "NVAX"
    assert res_lower.ticker == "NVAX"

    # Exactly one row exists, keyed on UPPERCASE ticker.
    assert _count_cooldown(temp_db, "NVAX") == 1
    conn = project_db.connect(temp_db)
    try:
        row = conn.execute(
            "SELECT ticker FROM ticker_cooldown WHERE ticker='NVAX'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["ticker"] == "NVAX"


def test_mixed_case_upsert_does_not_duplicate(temp_db: Path):
    """Writes with ``nvax`` and ``NVAX`` UPSERT to the same row."""
    t0 = _dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)
    t1 = t0 + _dt.timedelta(seconds=10)

    record_cooldown_on_success(ticker="nvax", db_path=temp_db, now=t0)
    record_cooldown_on_success(ticker="NVAX", db_path=temp_db, now=t1)
    record_cooldown_on_success(ticker="NvAx", db_path=temp_db, now=t1 + _dt.timedelta(seconds=5))

    # Exactly one row.
    assert _count_cooldown(temp_db) == 1


# ---------------------------------------------------------------------------
# VAL-M3-101 — `>=` boundary semantics (inclusive)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "offset_seconds,expected_passed",
    [
        (-2, False),   # 24h - 2s elapsed → BLOCK
        (-1, False),   # 24h - 1s elapsed → BLOCK
        (0, True),     # exactly 24h elapsed → ALLOW (inclusive boundary)
        (+1, True),    # 24h + 1s elapsed → ALLOW
        (+2, True),    # 24h + 2s elapsed → ALLOW
    ],
)
def test_cooldown_boundary_inclusive(
    temp_db: Path, offset_seconds: int, expected_passed: bool
):
    """``elapsed >= cooldown_hours_seconds`` → allow (inclusive).

    Parametrises across the boundary offsets [-2s, -1s, 0s, +1s, +2s].
    """
    now = _dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)
    cooldown_seconds = 24 * 3600
    elapsed = cooldown_seconds + offset_seconds
    last_entry_at = now - _dt.timedelta(seconds=elapsed)
    _seed_cooldown(
        temp_db,
        ticker="BNDY",
        last_entry_at=_iso(last_entry_at),
        cooldown_hours=24,
    )

    res = cooldown_gate(
        ticker="BNDY",
        db_path=temp_db,
        now=now,
    )
    assert res.passed is expected_passed, (
        f"offset={offset_seconds}s expected passed={expected_passed} "
        f"but got passed={res.passed} reason={res.reason}"
    )


def test_cooldown_uses_single_clock_per_evaluation(temp_db: Path, monkeypatch):
    """The gate captures ``datetime.now(...)`` AT MOST ONCE per call.

    VAL-M3-101 invariant: "Single UTC clock per gate evaluation
    (no two ``datetime.now()`` calls)". A read-then-recheck pattern
    could fail at exactly the boundary on a slow VPS.
    """
    call_count = {"n": 0}
    real_datetime = _dt.datetime

    class _CountingDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            call_count["n"] += 1
            return real_datetime(2026, 4, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)

    monkeypatch.setattr(stage2_gates._dt, "datetime", _CountingDateTime)

    # Active cooldown — gate must still produce one ``datetime.now``
    # call max regardless of branch.
    one_hour_ago = "2026-04-30T11:00:00.000Z"
    _seed_cooldown(
        temp_db,
        ticker="CLOCK",
        last_entry_at=one_hour_ago,
        cooldown_hours=24,
    )
    cooldown_gate(ticker="CLOCK", db_path=temp_db)
    # At most one ``datetime.now()`` call inside the gate.
    assert call_count["n"] <= 1


# ---------------------------------------------------------------------------
# VAL-M3-100 — Per-row cooldown_hours override beats env default
# ---------------------------------------------------------------------------


def test_per_row_cooldown_hours_override_beats_env_default(
    temp_db: Path, monkeypatch
):
    """A row with ``cooldown_hours=72`` overrides env default of 24."""
    monkeypatch.delenv("PER_TICKER_COOLDOWN_HOURS", raising=False)
    importlib.reload(_config)

    now = _dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)
    # 25h ago — under env default (24h) cooldown would expire,
    # but the per-row 72h override keeps it active.
    twentyfive_hours_ago = now - _dt.timedelta(hours=25)
    _seed_cooldown(
        temp_db,
        ticker="LONGB",
        last_entry_at=_iso(twentyfive_hours_ago),
        cooldown_hours=72,
    )

    res = cooldown_gate(
        ticker="LONGB",
        db_path=temp_db,
        now=now,
    )
    assert res.passed is False
    assert res.reason == GATE_REASON_COOLDOWN_ACTIVE
    assert res.cooldown_hours == 72  # row override, not env default
    # Remaining = 72h - 25h = 47h = 169200s.
    assert res.remaining_seconds == pytest.approx(47 * 3600, abs=2)


def test_per_row_shorter_override_allows_under_default(
    temp_db: Path, monkeypatch
):
    """Per-row ``cooldown_hours=6`` (shorter than env default 24) lets
    a 7h-elapsed entry through. Confirms per-row beats env (both ways)."""
    monkeypatch.delenv("PER_TICKER_COOLDOWN_HOURS", raising=False)
    importlib.reload(_config)

    now = _dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)
    seven_hours_ago = now - _dt.timedelta(hours=7)
    _seed_cooldown(
        temp_db,
        ticker="SHORT",
        last_entry_at=_iso(seven_hours_ago),
        cooldown_hours=6,
    )

    res = cooldown_gate(
        ticker="SHORT",
        db_path=temp_db,
        now=now,
    )
    assert res.passed is True
    assert res.cooldown_hours == 6


def test_no_row_uses_env_default_for_no_block(
    temp_db: Path, monkeypatch
):
    """No row → env default determines that the gate passes (since no
    last_entry_at exists, nothing can be active)."""
    monkeypatch.setenv("PER_TICKER_COOLDOWN_HOURS", "48")
    importlib.reload(_config)
    now = _dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)

    res = cooldown_gate(
        ticker="UNUSEDX",
        db_path=temp_db,
        now=now,
    )
    assert res.passed is True
    # When there's no row, cooldown_hours reports the env default.
    assert res.cooldown_hours == 48


# ---------------------------------------------------------------------------
# Sanity: gate result type + reason export
# ---------------------------------------------------------------------------


def test_canonical_reason_export():
    """``GATE_REASON_COOLDOWN_ACTIVE`` is the literal string ``cooldown_active``."""
    assert GATE_REASON_COOLDOWN_ACTIVE == "cooldown_active"


def test_cooldown_result_has_required_fields():
    """``CooldownGateResult`` dataclass surface."""
    fields = {
        "passed",
        "reason",
        "ticker",
        "last_entry_at",
        "cooldown_hours",
        "remaining_seconds",
    }
    res = CooldownGateResult(
        passed=True,
        reason=None,
        ticker="X",
        last_entry_at=None,
        cooldown_hours=24,
        remaining_seconds=0,
    )
    for f in fields:
        assert hasattr(res, f), f"missing field: {f}"
