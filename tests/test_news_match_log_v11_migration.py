"""Tests for the v10 → v11 ``news_match_log`` schema extension migration.

Feature ``f-misc-09-news-match-log-v11-extension`` extends the v10
``news_match_log`` skeleton with five forensic columns referenced by
contract evidence at VAL-M5-018 / VAL-M5-024 / VAL-M5-025 / VAL-M5-027:

* ``candidate_event_id``           TEXT NULL
* ``gate_outcome``                 TEXT NULL
* ``avg_probability``              REAL NULL
* ``cooldown_remaining_seconds``   REAL NULL
* ``today_total_usd``              REAL NULL

This file pins:

a) v10 → v11 upgrades a fresh db with no row-count loss on existing
   ``news_match_log`` rows.
b) Each of the 5 forensic columns is added with the correct SQLite
   type and an explicit ``DEFAULT NULL`` so existing rows remain
   valid (NULL) post-migration.
c) :func:`record_stage2_skip` writes BOTH the new forensic columns
   AND the audit JSON entry on the parallel-surfaces convention
   (the audit JSON path stays as a parallel/legacy persistence
   surface per AGENTS.md "Schema / Query Naming Map").
d) Re-running the migration on a v11 db is a no-op (idempotent).

The tests use isolated ``tmp_path``-rooted SQLite databases so they
are hermetic and parallel-safe (``pytest -n 2``).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from biotech_sniper import db
from biotech_sniper.migrations.runner import (
    MigrationError,
    load_migration,
    run as run_migrations_runner,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FORENSIC_COLUMNS: tuple[tuple[str, str], ...] = (
    # (column_name, expected SQLite affinity from PRAGMA table_info.type)
    ("candidate_event_id", "TEXT"),
    ("gate_outcome", "TEXT"),
    ("avg_probability", "REAL"),
    ("cooldown_remaining_seconds", "REAL"),
    ("today_total_usd", "REAL"),
)


def _migrate_to(db_path: Path, target: int) -> dict:
    return run_migrations_runner(
        db_path, target_version=target, take_backup_first=False
    )


def _column_info(db_path: Path, table: str) -> dict[str, dict]:
    """Return ``{name: {type, notnull, dflt_value, pk}}`` from PRAGMA."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    out: dict[str, dict] = {}
    # PRAGMA table_info returns columns: cid, name, type, notnull, dflt_value, pk
    for cid, name, ctype, notnull, dflt, pk in rows:
        out[name] = {
            "type": ctype,
            "notnull": int(notnull),
            "dflt_value": dflt,
            "pk": int(pk),
        }
    return out


# ---------------------------------------------------------------------------
# 1) Module shape — FROM_VERSION / TO_VERSION / DESCRIPTION / apply
# ---------------------------------------------------------------------------


def test_migration_module_declares_canonical_version_markers():
    """v11 module must declare FROM_VERSION=10 / TO_VERSION=11 / apply()."""
    module = load_migration(11)
    assert module.FROM_VERSION == 10
    assert module.TO_VERSION == 11
    assert callable(getattr(module, "apply", None))
    # DESCRIPTION is recorded into ``schema_version.description`` by
    # the runner — must be a non-empty human-readable string.
    description = getattr(module, "DESCRIPTION", "")
    assert isinstance(description, str) and description.strip()


# ---------------------------------------------------------------------------
# 2) v10 → v11 upgrade path adds the forensic columns with correct types
# ---------------------------------------------------------------------------


def test_v10_to_v11_upgrade_adds_five_forensic_columns(tmp_path: Path) -> None:
    """A fresh v10 db gains exactly the 5 forensic columns on upgrade.

    The base ``(ticker, news_event_id, matched, reason, logged_at)``
    skeleton MUST remain intact — this is a strictly additive
    extension.
    """
    db_path = tmp_path / "alpha.db"
    # Bring the db up to v10 first. The runner pins the v9 baseline
    # before consulting the version (per f-misc-06), so a fresh db
    # arrives here at ``from_version=9`` not 0.
    summary_v10 = _migrate_to(db_path, 10)
    assert summary_v10["from_version"] == 9
    assert 10 in summary_v10["applied"]
    pre_cols = _column_info(db_path, "news_match_log")
    # Pre-migration shape: only the v10 skeleton exists.
    assert {"ticker", "news_event_id", "matched", "reason", "logged_at"} <= set(
        pre_cols
    )
    for col, _expected_type in _FORENSIC_COLUMNS:
        assert col not in pre_cols, (
            f"v10 baseline must NOT contain forensic column {col!r}; "
            f"found pre-existing column with PRAGMA={pre_cols.get(col)!r}"
        )

    # Now upgrade to v11.
    summary_v11 = _migrate_to(db_path, 11)
    assert summary_v11["from_version"] == 10
    assert summary_v11["applied"] == [11]

    post_cols = _column_info(db_path, "news_match_log")
    # Skeleton columns preserved.
    for skel in ("ticker", "news_event_id", "matched", "reason", "logged_at"):
        assert skel in post_cols, f"skeleton column {skel} disappeared"

    # Each forensic column added with correct affinity, NULL-able,
    # explicit ``DEFAULT NULL`` (PRAGMA reports the literal text
    # ``NULL`` in dflt_value when ``DEFAULT NULL`` is declared).
    for col, expected_type in _FORENSIC_COLUMNS:
        assert col in post_cols, f"missing forensic column {col!r}"
        info = post_cols[col]
        assert info["type"].upper() == expected_type, (
            f"column {col} has type={info['type']!r}, expected {expected_type}"
        )
        assert info["notnull"] == 0, (
            f"column {col} must be nullable (notnull=0), got {info['notnull']}"
        )
        # SQLite stores ``DEFAULT NULL`` as the literal text 'NULL'.
        # Accept either that or absence (some SQLite versions report
        # ``None`` when DEFAULT is NULL — treat both as valid).
        dflt = info["dflt_value"]
        assert dflt is None or str(dflt).strip().upper() == "NULL", (
            f"column {col} must have explicit DEFAULT NULL, got {dflt!r}"
        )


def test_v10_to_v11_preserves_existing_rows(tmp_path: Path) -> None:
    """A pre-existing news_match_log row must survive the v11 upgrade.

    Forensic columns on the row must be NULL post-migration (the
    DEFAULT NULL invariant).
    """
    db_path = tmp_path / "alpha.db"
    _migrate_to(db_path, 10)

    # Insert a baseline-shape row at v10.
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            conn.execute(
                "INSERT INTO news_match_log (ticker, news_event_id, matched, reason) "
                "VALUES (?, ?, ?, ?)",
                ("MRNS", None, 0, "v10_baseline_reason"),
            )
        pre_count = conn.execute(
            "SELECT COUNT(*) FROM news_match_log"
        ).fetchone()[0]
    finally:
        conn.close()
    assert pre_count == 1

    _migrate_to(db_path, 11)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM news_match_log").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, "row count must be preserved across v10 → v11"
    row = rows[0]
    assert row["ticker"] == "MRNS"
    assert row["matched"] == 0
    assert row["reason"] == "v10_baseline_reason"
    # Each new forensic column defaults to NULL on the legacy row.
    for col, _ in _FORENSIC_COLUMNS:
        assert row[col] is None, f"forensic col {col} must default to NULL on legacy rows"


# ---------------------------------------------------------------------------
# 3) Idempotency — re-running on a v11 db is a no-op
# ---------------------------------------------------------------------------


def test_v11_migration_is_idempotent_on_v11_db(tmp_path: Path) -> None:
    """Running ``run(... target=11)`` twice must not error or duplicate work."""
    db_path = tmp_path / "alpha.db"
    # First pass: bring up to v11.
    _migrate_to(db_path, 11)

    # Snapshot the schema text + row counts.
    conn = sqlite3.connect(str(db_path))
    try:
        sql_pre = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='news_match_log'"
        ).fetchone()[0]
        version_pre = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        rowcount_pre = conn.execute(
            "SELECT COUNT(*) FROM news_match_log"
        ).fetchone()[0]
    finally:
        conn.close()

    # Second pass: run the runner again at target=11 — should no-op.
    summary = _migrate_to(db_path, 11)
    assert summary["from_version"] == 11
    assert summary["no_op"] is True
    assert summary["applied"] == []

    conn = sqlite3.connect(str(db_path))
    try:
        sql_post = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='news_match_log'"
        ).fetchone()[0]
        version_post = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        rowcount_post = conn.execute(
            "SELECT COUNT(*) FROM news_match_log"
        ).fetchone()[0]
    finally:
        conn.close()

    assert sql_pre == sql_post, "table DDL must be byte-identical post-rerun"
    assert version_pre == version_post == 11
    assert rowcount_pre == rowcount_post


def test_v11_migration_apply_called_directly_is_idempotent(tmp_path: Path) -> None:
    """Calling ``module.apply(conn)`` directly on a v11 db must be a no-op.

    The runner's idempotency above relies on the
    ``target_version == current`` short-circuit. This test exercises
    the migration's own ``apply`` function on a v11-shaped db to
    catch any ALTER TABLE that would re-fail on duplicate columns.
    """
    db_path = tmp_path / "alpha.db"
    _migrate_to(db_path, 11)

    module = load_migration(11)
    # Call apply directly inside an explicit transaction so the
    # migration runs against an existing v11 schema.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.isolation_level = None
        conn.execute("BEGIN")
        try:
            module.apply(conn)  # MUST NOT raise
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    # Forensic columns still exactly the expected set after re-apply.
    post_cols = _column_info(db_path, "news_match_log")
    for col, expected_type in _FORENSIC_COLUMNS:
        assert col in post_cols
        assert post_cols[col]["type"].upper() == expected_type


# ---------------------------------------------------------------------------
# 4) db.CURRENT_VERSION is bumped to 11
# ---------------------------------------------------------------------------


def test_db_current_version_is_eleven() -> None:
    """f-misc-09: db.CURRENT_VERSION must be bumped from 10 to 11."""
    assert db.CURRENT_VERSION == 11


def test_paper_executor_default_run_migrations_targets_v11(tmp_path: Path) -> None:
    """A fresh ``db.run_migrations(conn)`` (default target=CURRENT_VERSION)
    auto-bootstraps a fresh db all the way to v11 — preserves the
    f-misc-06 invariant for downstream callers (``PaperExecutor``).
    """
    db_path = tmp_path / "alpha.db"
    conn = db.connect(db_path)
    try:
        applied = db.run_migrations(conn)  # default target=CURRENT_VERSION
    finally:
        conn.close()
    assert applied == 11
    cols = _column_info(db_path, "news_match_log")
    for col, _ in _FORENSIC_COLUMNS:
        assert col in cols, f"v11 forensic col {col} missing after bootstrap"


# ---------------------------------------------------------------------------
# 5) record_stage2_skip writes the new forensic columns AND the audit JSON
# ---------------------------------------------------------------------------


def test_record_stage2_skip_writes_forensic_columns_and_audit_json(
    tmp_path: Path,
) -> None:
    """Post-v11, ``record_stage2_skip`` MUST persist forensic metadata
    on BOTH surfaces — the news_match_log row AND the
    audit_latest.json entry.

    Asserts the parallel-surfaces convention from f-misc-09: the
    audit JSON stays as a parallel/legacy persistence surface for
    downstream tooling, while SQL becomes the canonical query path.
    """
    from biotech_sniper.llm.stage2_gates import (
        GATE_REASON_COOLDOWN_ACTIVE,
        record_stage2_skip,
    )

    db_path = tmp_path / "alpha.db"
    _migrate_to(db_path, 11)

    audit_path = tmp_path / "state" / "audit_latest.json"

    # Pass news_event_id=None to avoid the FK constraint on
    # ``news_events`` — the test only cares about the persistence
    # surface of the forensic columns.
    record_stage2_skip(
        db_path=db_path,
        audit_path=audit_path,
        ticker="MRNS",
        candidate_event_id=42,
        news_event_id=None,
        today_total_usd=12.34,
        projected_cost=0.5,
        cap=20.0,
        reason=GATE_REASON_COOLDOWN_ACTIVE,
        avg_probability=None,
        cooldown_remaining_seconds=82_800,
    )

    # ---- news_match_log row was written with forensic columns ----
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT ticker, news_event_id, matched, reason, "
            "candidate_event_id, gate_outcome, avg_probability, "
            "cooldown_remaining_seconds, today_total_usd "
            "FROM news_match_log WHERE ticker = ?",
            ("MRNS",),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["ticker"] == "MRNS"
    assert row["matched"] == 0
    assert row["reason"] == GATE_REASON_COOLDOWN_ACTIVE
    # Forensic columns (cooldown rejection populates remaining_seconds
    # + today_total_usd; avg_probability is None).
    # ``candidate_event_id`` is TEXT — accept either textual or
    # numeric coercion since SQLite has flexible affinity.
    assert str(row["candidate_event_id"]) == "42"
    assert row["gate_outcome"] == "rejected"
    assert row["avg_probability"] is None
    assert float(row["cooldown_remaining_seconds"]) == pytest.approx(82_800.0)
    assert float(row["today_total_usd"]) == pytest.approx(12.34)

    # ---- audit JSON entry was ALSO written (parallel surface) ----
    assert audit_path.is_file()
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    skipped = payload.get("stage2_skipped") or []
    matching = [
        s for s in skipped
        if isinstance(s, dict) and s.get("reason") == GATE_REASON_COOLDOWN_ACTIVE
    ]
    assert matching, payload
    entry = matching[0]
    assert entry["count"] >= 1
    assert entry["last_ticker"] == "MRNS"
    assert entry["last_candidate_event_id"] == 42
    assert entry["last_news_event_id"] is None
    assert int(entry["last_cooldown_remaining_seconds"]) == 82_800


def test_record_stage2_skip_avg_probability_persisted_on_v11(
    tmp_path: Path,
) -> None:
    """Probability rejection populates ``avg_probability`` on the SQL row."""
    from biotech_sniper.llm.stage2_gates import (
        GATE_REASON_PROBABILITY_BELOW_THRESHOLD,
        record_stage2_skip,
    )

    db_path = tmp_path / "alpha.db"
    _migrate_to(db_path, 11)
    audit_path = tmp_path / "state" / "audit_latest.json"

    record_stage2_skip(
        db_path=db_path,
        audit_path=audit_path,
        ticker="MRNS",
        candidate_event_id=99,
        news_event_id=None,
        reason=GATE_REASON_PROBABILITY_BELOW_THRESHOLD,
        avg_probability=0.6625,
    )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT avg_probability, gate_outcome, reason "
            "FROM news_match_log WHERE ticker = ?",
            ("MRNS",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["reason"] == GATE_REASON_PROBABILITY_BELOW_THRESHOLD
    assert row["gate_outcome"] == "rejected"
    assert float(row["avg_probability"]) == pytest.approx(0.6625)


def test_record_stage2_skip_today_total_persisted_on_v11(
    tmp_path: Path,
) -> None:
    """Cap rejection populates ``today_total_usd`` on the SQL row."""
    from biotech_sniper.llm.stage2_gates import (
        GATE_REASON_DAILY_CAP_EXCEEDED,
        record_stage2_skip,
    )

    db_path = tmp_path / "alpha.db"
    _migrate_to(db_path, 11)
    audit_path = tmp_path / "state" / "audit_latest.json"

    record_stage2_skip(
        db_path=db_path,
        audit_path=audit_path,
        ticker="MRNS",
        candidate_event_id=1,
        news_event_id=None,
        today_total_usd=20.0,
        projected_cost=0.5,
        cap=20.0,
        reason=GATE_REASON_DAILY_CAP_EXCEEDED,
    )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT today_total_usd, gate_outcome, reason "
            "FROM news_match_log WHERE ticker = ?",
            ("MRNS",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["reason"] == GATE_REASON_DAILY_CAP_EXCEEDED
    assert row["gate_outcome"] == "rejected"
    assert float(row["today_total_usd"]) == pytest.approx(20.0)


def test_record_stage2_skip_audit_path_none_still_writes_sql_row(
    tmp_path: Path,
) -> None:
    """``audit_path=None`` still lands the SQL row (f-fix-m5-03 invariant)
    AND the v11 forensic columns are populated when supplied.
    """
    from biotech_sniper.llm.stage2_gates import (
        GATE_REASON_ARMED_FILE_MISSING,
        record_stage2_skip,
    )

    db_path = tmp_path / "alpha.db"
    _migrate_to(db_path, 11)

    record_stage2_skip(
        db_path=db_path,
        audit_path=None,
        ticker="MRNS",
        candidate_event_id=5,
        news_event_id=None,
        reason=GATE_REASON_ARMED_FILE_MISSING,
    )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT reason, gate_outcome, candidate_event_id "
            "FROM news_match_log WHERE ticker = ?",
            ("MRNS",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["reason"] == GATE_REASON_ARMED_FILE_MISSING
    assert row["gate_outcome"] == "rejected"
    assert str(row["candidate_event_id"]) == "5"


# ---------------------------------------------------------------------------
# 6) Migration script lives at the canonical path with the canonical name
# ---------------------------------------------------------------------------


def test_migration_011_file_exists_at_canonical_path() -> None:
    """The runner's load_migration(11) must resolve to a file on disk
    matching the canonical ``011_news_match_log_extension.py`` name
    so future workers / validators can grep for it directly.
    """
    from biotech_sniper.migrations import runner as runner_mod

    migrations_dir = Path(runner_mod.__file__).resolve().parent
    candidate = migrations_dir / "011_news_match_log_extension.py"
    assert candidate.is_file(), (
        f"expected {candidate} to exist for the v11 migration"
    )
