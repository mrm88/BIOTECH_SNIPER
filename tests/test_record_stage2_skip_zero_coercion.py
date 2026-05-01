"""Regression tests for ``record_stage2_skip`` zero-value coercion.

Pins the fix for scrutiny-misc-stage2-hygiene blocking finding #2:
``f-misc-09`` extended ``news_match_log`` with five forensic columns,
but the ``INSERT`` in :func:`biotech_sniper.llm.stage2_gates.record_stage2_skip`
coerced numeric kwargs via a TRUTHINESS check
(``float(x) if x else None``). For ``today_total_usd=0.0`` (cap-gate
fires BEFORE any spend today — the first rejection of the day) the
truthiness check evaluates ``0.0`` as falsy and writes ``NULL``,
losing forensic data exactly when it matters most.

This file pins the contract: numeric forensic columns (``today_total_usd``,
``avg_probability``, ``cooldown_remaining_seconds``) MUST persist
literal-zero inputs as ``0.0`` / ``0``, NOT ``NULL``. Non-zero values
must continue to persist correctly. Explicit ``None`` continues to
persist as ``NULL`` (the gate-not-applicable signal).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from biotech_sniper.migrations.runner import run as run_migrations_runner
from biotech_sniper import db as project_db


def _migrate_to_v11(db_path: Path) -> None:
    run_migrations_runner(db_path, target_version=project_db.CURRENT_VERSION, take_backup_first=False)


def _fetch_row(db_path: Path, ticker: str) -> sqlite3.Row:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT today_total_usd, avg_probability, "
            "cooldown_remaining_seconds, candidate_event_id, "
            "gate_outcome, reason "
            "FROM news_match_log WHERE ticker = ?",
            (ticker,),
        ).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1) today_total_usd=0.0 — cap-gate fires-before-any-spend scenario
# ---------------------------------------------------------------------------


def test_today_total_usd_zero_persists_as_zero_not_null(tmp_path: Path) -> None:
    """Cap-gate fires at zero spend → ``today_total_usd`` MUST be 0.0,
    not NULL.

    This is the primary regression: f-misc-09's truthiness coercion
    was ``float(today_total_usd) if today_total_usd else None``,
    which collapses ``0.0`` → ``None``. The fix replaces it with an
    explicit ``is not None`` check so legitimate zero values land in
    the forensic column.
    """
    from biotech_sniper.llm.stage2_gates import (
        GATE_REASON_DAILY_CAP_EXCEEDED,
        record_stage2_skip,
    )

    db_path = tmp_path / "alpha.db"
    _migrate_to_v11(db_path)
    audit_path = tmp_path / "state" / "audit_latest.json"

    record_stage2_skip(
        db_path=db_path,
        audit_path=audit_path,
        ticker="ZERO",
        candidate_event_id=1,
        news_event_id=None,
        today_total_usd=0.0,        # <-- cap-gate fires at zero spend
        projected_cost=20.5,
        cap=20.0,
        reason=GATE_REASON_DAILY_CAP_EXCEEDED,
    )

    row = _fetch_row(db_path, "ZERO")
    assert row is not None
    assert row["reason"] == GATE_REASON_DAILY_CAP_EXCEEDED
    assert row["gate_outcome"] == "rejected"
    # The critical assertion — 0.0 must persist as 0.0, NOT NULL.
    assert row["today_total_usd"] is not None, (
        "today_total_usd=0.0 was coerced to NULL — truthiness regression"
    )
    assert float(row["today_total_usd"]) == pytest.approx(0.0)


def test_today_total_usd_via_last_insert_rowid_returns_zero_not_null(
    tmp_path: Path,
) -> None:
    """SQLite ``last_insert_rowid()`` query reproduces the verification
    step from the feature description verbatim.
    """
    from biotech_sniper.llm.stage2_gates import (
        GATE_REASON_DAILY_CAP_EXCEEDED,
        record_stage2_skip,
    )

    db_path = tmp_path / "alpha.db"
    _migrate_to_v11(db_path)
    audit_path = tmp_path / "state" / "audit_latest.json"

    record_stage2_skip(
        db_path=db_path,
        audit_path=audit_path,
        ticker="LIROW",
        candidate_event_id=2,
        news_event_id=None,
        today_total_usd=0.0,
        projected_cost=21.0,
        cap=20.0,
        reason=GATE_REASON_DAILY_CAP_EXCEEDED,
    )

    conn = sqlite3.connect(str(db_path))
    try:
        # Match the exact verification SELECT in the feature
        # description: SELECT today_total_usd FROM news_match_log
        # WHERE rowid=last_insert_rowid().
        row = conn.execute(
            "SELECT today_total_usd FROM news_match_log "
            "WHERE rowid = (SELECT MAX(rowid) FROM news_match_log)"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] is not None, "today_total_usd must NOT be NULL on zero-cap-gate"
    assert float(row[0]) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 2) avg_probability=0.0 — defense-in-depth pin
# ---------------------------------------------------------------------------


def test_avg_probability_zero_persists_as_zero_not_null(tmp_path: Path) -> None:
    """``avg_probability=0.0`` must persist as 0.0, not NULL.

    The current ``avg_probability`` coercion already uses
    ``is not None`` (correct), but we pin a regression test so any
    future refactor that re-introduces a truthiness check is caught
    immediately.
    """
    from biotech_sniper.llm.stage2_gates import (
        GATE_REASON_PROBABILITY_BELOW_THRESHOLD,
        record_stage2_skip,
    )

    db_path = tmp_path / "alpha.db"
    _migrate_to_v11(db_path)
    audit_path = tmp_path / "state" / "audit_latest.json"

    record_stage2_skip(
        db_path=db_path,
        audit_path=audit_path,
        ticker="PROB0",
        candidate_event_id=3,
        news_event_id=None,
        reason=GATE_REASON_PROBABILITY_BELOW_THRESHOLD,
        avg_probability=0.0,
    )

    row = _fetch_row(db_path, "PROB0")
    assert row is not None
    assert row["avg_probability"] is not None, (
        "avg_probability=0.0 was coerced to NULL — truthiness regression"
    )
    assert float(row["avg_probability"]) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 3) cooldown_remaining_seconds=0 — boundary-zero pin
# ---------------------------------------------------------------------------


def test_cooldown_remaining_seconds_zero_persists_as_zero_not_null(
    tmp_path: Path,
) -> None:
    """``cooldown_remaining_seconds=0`` must persist as 0, not NULL.

    Same defensive pin as ``avg_probability`` — the column is REAL
    in the v11 schema, so we accept ``0`` or ``0.0`` from the
    storage round-trip.
    """
    from biotech_sniper.llm.stage2_gates import (
        GATE_REASON_COOLDOWN_ACTIVE,
        record_stage2_skip,
    )

    db_path = tmp_path / "alpha.db"
    _migrate_to_v11(db_path)
    audit_path = tmp_path / "state" / "audit_latest.json"

    record_stage2_skip(
        db_path=db_path,
        audit_path=audit_path,
        ticker="CD0",
        candidate_event_id=4,
        news_event_id=None,
        reason=GATE_REASON_COOLDOWN_ACTIVE,
        cooldown_remaining_seconds=0,
    )

    row = _fetch_row(db_path, "CD0")
    assert row is not None
    assert row["cooldown_remaining_seconds"] is not None, (
        "cooldown_remaining_seconds=0 was coerced to NULL — "
        "truthiness regression"
    )
    assert float(row["cooldown_remaining_seconds"]) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 4) Non-zero values still persist correctly (no regression on the fix)
# ---------------------------------------------------------------------------


def test_nonzero_today_total_still_persists_correctly(tmp_path: Path) -> None:
    """Sanity: the ``is not None`` fix MUST not break non-zero values."""
    from biotech_sniper.llm.stage2_gates import (
        GATE_REASON_DAILY_CAP_EXCEEDED,
        record_stage2_skip,
    )

    db_path = tmp_path / "alpha.db"
    _migrate_to_v11(db_path)
    audit_path = tmp_path / "state" / "audit_latest.json"

    record_stage2_skip(
        db_path=db_path,
        audit_path=audit_path,
        ticker="NZ",
        candidate_event_id=5,
        news_event_id=None,
        today_total_usd=19.95,
        projected_cost=0.5,
        cap=20.0,
        reason=GATE_REASON_DAILY_CAP_EXCEEDED,
    )

    row = _fetch_row(db_path, "NZ")
    assert row is not None
    assert float(row["today_total_usd"]) == pytest.approx(19.95)


# ---------------------------------------------------------------------------
# 5) Explicit None still persists as NULL (gate-not-applicable signal)
# ---------------------------------------------------------------------------


def test_unset_today_total_persists_as_null_for_non_cap_paths(tmp_path: Path) -> None:
    """When the caller omits ``today_total_usd`` (non-cap-gate paths),
    the SQL row's ``today_total_usd`` column lands NULL.

    The kwarg signature defaults to ``None`` (post-f-fix-misc-09) so
    armed/cooldown/probability/unanimity rejections — which do NOT
    compute a cap-state value — leave the column NULL. Cap-gate
    rejections that DO compute the value pass it explicitly (even
    when it's literal-zero — see
    :func:`test_today_total_usd_zero_persists_as_zero_not_null`).
    """
    from biotech_sniper.llm.stage2_gates import (
        GATE_REASON_ARMED_FILE_MISSING,
        record_stage2_skip,
    )

    db_path = tmp_path / "alpha.db"
    _migrate_to_v11(db_path)

    record_stage2_skip(
        db_path=db_path,
        audit_path=None,
        ticker="ARM",
        candidate_event_id=6,
        news_event_id=None,
        reason=GATE_REASON_ARMED_FILE_MISSING,
        # today_total_usd / avg_probability /
        # cooldown_remaining_seconds all unset — the gate type is
        # armed_file_missing, no numeric forensic data applies.
    )

    row = _fetch_row(db_path, "ARM")
    assert row is not None
    assert row["reason"] == GATE_REASON_ARMED_FILE_MISSING
    # All three numeric forensic columns left NULL for non-cap paths.
    assert row["avg_probability"] is None
    assert row["cooldown_remaining_seconds"] is None
    assert row["today_total_usd"] is None
