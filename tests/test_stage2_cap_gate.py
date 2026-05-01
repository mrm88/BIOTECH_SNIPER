"""Tests for the Stage-2 daily $ cap gate (f-m3-07).

Feature: f-m3-07-daily-cap-gate.

Verifies the contract assertions VAL-M3-037 through VAL-M3-042:

* VAL-M3-037 — ``LLM_STAGE2_DAILY_USD_CAP`` env override defaults to
  ``20.0``; overrides are honored via :mod:`biotech_sniper.config`
  (no raw ``os.environ`` outside ``config.py``).
* VAL-M3-038 — PRE-spend projection mirrors the ``liquidity_probe``
  pattern: ``today_total = SUM(cost_usd) FROM llm_cost_ledger WHERE
  purpose='stage2_event_scoring' AND DATE(called_at)=today``. When
  ``today_total + projected_cost > cap`` the gate raises
  ``DailyCapExceeded`` and dispatches NO LLM calls.
* VAL-M3-039 — On cap-hit, the gate logs a structured WARNING with
  ``reason='daily_cap_exceeded'`` plus the snapshot fields, writes a
  ``news_match_log`` row with ``reason='daily_cap_exceeded'``, and
  merges a ``stage2_skipped[]`` block into ``state/audit_latest.json``
  with ``reason='daily_cap_exceeded'`` and a non-zero count.
* VAL-M3-040 — Daily reset uses UTC: rows straddling UTC midnight
  count toward different daily totals (``DATE(called_at)`` boundary).
* VAL-M3-041 — Stage-2 cap is SEPARATE from the existing
  ``LLM_DEBATE_DAILY_USD_CAP=10`` debate cap. Both caps coexist;
  neither path's ledger sum bleeds into the other's gate.
* VAL-M3-042 — Total LLM ceiling = $30/day audit invariant
  (Stage-2 $20 + debate $10).

Per the dual-path test convention in ``library`` / ``AGENTS.md``,
``tests/llm/test_stage2_gates.py`` re-exports the same test bodies via
``from tests.test_stage2_cap_gate import *`` so contract node-IDs of
either form collect.
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from biotech_sniper import config as _config
from biotech_sniper import db as project_db
from biotech_sniper.llm import stage2_gates
from biotech_sniper.llm.stage2_gates import (
    DailyCapExceeded,
    DailyCapGateResult,
    GATE_REASON_DAILY_CAP_EXCEEDED,
    STAGE2_CALL_USD_PROJECTION,
    daily_cap_gate,
    record_stage2_skip,
)
from biotech_sniper.migrations.runner import run as run_v10


# ---------------------------------------------------------------------------
# Module-reload teardown — same pattern as test_stage2_probability_gate.py.
# Ensures STAGE2 / cap env vars leaked from one test cannot bleed into
# subsequent xdist-worker tests when the module-level constant is read
# from ``importlib.reload(_config)`` somewhere in the suite.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_config_module_after_test():
    yield
    os.environ.pop("LLM_STAGE2_DAILY_USD_CAP", None)
    importlib.reload(_config)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Fresh SQLite db with the v10 (Reading-B foundations) schema applied."""
    db_path = tmp_path / "stage2_cap.db"
    run_v10(db_path, target_version=project_db.CURRENT_VERSION, take_backup_first=False)
    return db_path


def _seed_ledger(
    db_path: Path,
    *,
    provider: str = "perplexity",
    model_id: str = "sonar",
    purpose: str = "stage2_event_scoring",
    cost_usd: float,
    called_at: str | None = None,
) -> None:
    """Insert one ``llm_cost_ledger`` row with the supplied attributes."""
    conn = project_db.connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO llm_cost_ledger (
                    provider, model_id, purpose,
                    prompt_tokens, completion_tokens,
                    latency_ms, cost_usd, called_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))
                """,
                (
                    provider,
                    model_id,
                    purpose,
                    100,
                    50,
                    250,
                    float(cost_usd),
                    called_at,
                ),
            )
    finally:
        conn.close()


def _count_ledger(db_path: Path) -> int:
    conn = project_db.connect(db_path)
    try:
        return int(
            conn.execute("SELECT COUNT(*) FROM llm_cost_ledger").fetchone()[0]
        )
    finally:
        conn.close()


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _seed_news_event(db_path: Path, *, news_event_id: int, ticker: str) -> None:
    """Insert a ``news_events`` row so a downstream ``news_match_log``
    insert with ``news_event_id=<id>`` satisfies the FK constraint."""
    conn = project_db.connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO news_events (
                    id, ticker, source, title
                ) VALUES (?, ?, 'unit-test', ?)
                """,
                (news_event_id, ticker, f"seed for {ticker}"),
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# VAL-M3-037 — env override defaults to 20.0 / honored
# ---------------------------------------------------------------------------


def test_cap_default_is_20(monkeypatch):
    """With ``LLM_STAGE2_DAILY_USD_CAP`` unset the resolved cap is 20.0."""
    monkeypatch.delenv("LLM_STAGE2_DAILY_USD_CAP", raising=False)
    importlib.reload(_config)
    assert _config.LLM_STAGE2_DAILY_USD_CAP == 20.0
    assert _config.get_llm_stage2_daily_usd_cap() == 20.0
    # Default constant in the gate module mirrors config.
    assert stage2_gates.DEFAULT_LLM_STAGE2_DAILY_USD_CAP == 20.0


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("5", 5.0),
        ("5.0", 5.0),
        ("100", 100.0),
        ("0.50", 0.50),
        ("  25.0  ", 25.0),  # tolerant of surrounding whitespace
    ],
)
def test_cap_env_override(monkeypatch, raw, expected):
    """``LLM_STAGE2_DAILY_USD_CAP=<v>`` is honored by the getter."""
    monkeypatch.setenv("LLM_STAGE2_DAILY_USD_CAP", raw)
    assert _config.get_llm_stage2_daily_usd_cap() == pytest.approx(expected)


def test_cap_falls_back_to_default_on_unparseable(monkeypatch):
    monkeypatch.setenv("LLM_STAGE2_DAILY_USD_CAP", "not-a-number")
    assert _config.get_llm_stage2_daily_usd_cap() == 20.0


def test_no_raw_environ_outside_config():
    """The cap gate must not read LLM_STAGE2_DAILY_USD_CAP via raw
    ``os.environ`` outside :mod:`biotech_sniper.config`."""
    src = open(stage2_gates.__file__).read()
    assert 'os.environ.get("LLM_STAGE2_DAILY_USD_CAP"' not in src
    assert "os.environ['LLM_STAGE2_DAILY_USD_CAP'" not in src
    assert 'os.getenv("LLM_STAGE2_DAILY_USD_CAP"' not in src


# ---------------------------------------------------------------------------
# VAL-M3-038 — PRE-spend projection blocks at cap
# ---------------------------------------------------------------------------


def test_pre_spend_projection_blocks_at_cap(temp_db: Path):
    """Seed rows summing to $19.50; with cap=$20, projection=$0.60 →
    ``today_total + projection = 20.10 > 20`` → block, raise
    :class:`DailyCapExceeded`, no new ``llm_cost_ledger`` rows."""
    # Seed 3 rows summing to $19.50.
    _seed_ledger(temp_db, cost_usd=10.00)
    _seed_ledger(temp_db, cost_usd=6.50)
    _seed_ledger(temp_db, cost_usd=3.00)
    pre_count = _count_ledger(temp_db)
    assert pre_count == 3

    res = daily_cap_gate(
        db_path=temp_db,
        projected_cost=0.60,
        cap=20.0,
    )
    assert isinstance(res, DailyCapGateResult)
    assert res.passed is False
    assert res.reason == GATE_REASON_DAILY_CAP_EXCEEDED
    assert res.reason == "daily_cap_exceeded"
    assert res.today_total_usd == pytest.approx(19.50)
    assert res.projected_cost == pytest.approx(0.60)
    assert res.cap == pytest.approx(20.0)

    # No new ledger rows on cap-hit.
    assert _count_ledger(temp_db) == pre_count


def test_pre_spend_projection_allows_under_cap(temp_db: Path):
    """When ``today_total + projection <= cap`` the gate passes."""
    _seed_ledger(temp_db, cost_usd=10.00)
    _seed_ledger(temp_db, cost_usd=5.00)

    res = daily_cap_gate(
        db_path=temp_db,
        projected_cost=0.50,
        cap=20.0,
    )
    assert res.passed is True
    assert res.reason is None
    assert res.today_total_usd == pytest.approx(15.0)
    assert res.projected_cost == pytest.approx(0.50)


def test_cap_boundary_strict_gt(temp_db: Path):
    """Boundary: ``total + projection == cap`` is allowed (strict ``>``).

    ``today_total + projection > cap`` blocks; ``==`` passes. Mirrors
    the contract description ("If today_total + projected_cost > cap,
    raises DailyCapExceeded").
    """
    _seed_ledger(temp_db, cost_usd=19.40)
    res = daily_cap_gate(
        db_path=temp_db,
        projected_cost=0.60,
        cap=20.0,
    )
    # 19.40 + 0.60 = 20.00 == cap → allowed (not > cap).
    assert res.passed is True
    assert res.today_total_usd == pytest.approx(19.40)


def test_pre_spend_uses_purpose_filter(temp_db: Path):
    """Only ``purpose='stage2_event_scoring'`` rows count toward the cap.

    Other purposes (e.g. ``deep_science``, ``debate``, ``daily_curated``)
    are excluded so the Stage-2 cap is independent from other LLM
    spend categories.
    """
    # Stage-2 spend at $10.
    _seed_ledger(
        temp_db,
        purpose="stage2_event_scoring",
        cost_usd=10.0,
    )
    # Other-purpose spend at $5 — must NOT count toward stage2 cap.
    _seed_ledger(
        temp_db,
        provider="anthropic",
        purpose="deep_science",
        cost_usd=5.0,
    )
    _seed_ledger(
        temp_db,
        provider="xai",
        purpose="debate",
        cost_usd=2.0,
    )

    res = daily_cap_gate(
        db_path=temp_db,
        projected_cost=0.50,
        cap=20.0,
    )
    # today_total reflects only stage2_event_scoring rows ($10), not $17.
    assert res.today_total_usd == pytest.approx(10.0)
    assert res.passed is True


def test_cap_gate_dispatches_no_llm_calls_on_block(temp_db: Path):
    """Cap-hit path inserts ZERO new ``llm_cost_ledger`` rows.

    The gate is purely a SELECT — no inserts. Verified by counting
    rows before/after.
    """
    _seed_ledger(temp_db, cost_usd=20.5)
    pre_count = _count_ledger(temp_db)

    with pytest.raises(DailyCapExceeded):
        daily_cap_gate(
            db_path=temp_db,
            projected_cost=0.60,
            cap=20.0,
            raise_on_block=True,
        )
    assert _count_ledger(temp_db) == pre_count


# ---------------------------------------------------------------------------
# VAL-M3-039 — cap-hit logs reason + writes news_match_log + audit
# ---------------------------------------------------------------------------


def test_cap_hit_writes_news_match_log_row(tmp_path, temp_db):
    """``record_stage2_skip`` writes a ``news_match_log`` row with
    ``reason='daily_cap_exceeded'`` and ``matched=0``."""
    audit_path = tmp_path / "audit_latest.json"
    record_stage2_skip(
        db_path=temp_db,
        audit_path=audit_path,
        ticker="ABCX",
        candidate_event_id=None,
        news_event_id=None,
        today_total_usd=20.10,
        projected_cost=0.60,
        cap=20.0,
    )

    conn = project_db.connect(temp_db)
    try:
        rows = conn.execute(
            "SELECT ticker, matched, reason FROM news_match_log "
            "WHERE reason='daily_cap_exceeded'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "ABCX"
    assert int(rows[0]["matched"]) == 0
    assert rows[0]["reason"] == "daily_cap_exceeded"


def test_cap_hit_audit_log(tmp_path, temp_db):
    """``record_stage2_skip`` merges a ``stage2_skipped`` block in
    ``audit_latest.json`` with ``reason='daily_cap_exceeded'`` and
    a non-zero count."""
    audit_path = tmp_path / "audit_latest.json"
    record_stage2_skip(
        db_path=temp_db,
        audit_path=audit_path,
        ticker="ABCX",
        candidate_event_id=None,
        news_event_id=None,
        today_total_usd=20.10,
        projected_cost=0.60,
        cap=20.0,
    )
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    skipped_block = payload.get("stage2_skipped")
    assert isinstance(skipped_block, list) and skipped_block, (
        "stage2_skipped[] must be present and non-empty"
    )
    reasons = [s.get("reason") for s in skipped_block]
    assert "daily_cap_exceeded" in reasons
    cap_entry = next(s for s in skipped_block if s.get("reason") == "daily_cap_exceeded")
    assert int(cap_entry.get("count") or 0) >= 1


def test_cap_hit_audit_increments_count(tmp_path, temp_db):
    """A second cap-hit on the same UTC day increments the count."""
    audit_path = tmp_path / "audit_latest.json"
    for ticker in ("ABCX", "DEFY", "GHIZ"):
        record_stage2_skip(
            db_path=temp_db,
            audit_path=audit_path,
            ticker=ticker,
            candidate_event_id=None,
            news_event_id=None,
            today_total_usd=20.10,
            projected_cost=0.60,
            cap=20.0,
        )
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    cap_entry = next(
        s for s in payload["stage2_skipped"] if s.get("reason") == "daily_cap_exceeded"
    )
    assert int(cap_entry["count"]) == 3


def test_cap_hit_logs_warning(temp_db, caplog):
    """Cap-hit emits a WARNING-level structured log line carrying
    ``event="stage2_skipped"``, ``reason="daily_cap_exceeded"`` and the
    snapshot fields."""
    import logging

    caplog.set_level(logging.WARNING, logger=stage2_gates.__name__)
    _seed_ledger(temp_db, cost_usd=19.80)
    daily_cap_gate(
        db_path=temp_db,
        projected_cost=0.60,
        cap=20.0,
    )
    text = " ".join(rec.getMessage() for rec in caplog.records)
    assert "stage2_skipped" in text or "daily_cap_exceeded" in text
    assert "today_total" in text or "19.80" in text


# ---------------------------------------------------------------------------
# VAL-M3-040 — Daily reset uses UTC
# ---------------------------------------------------------------------------


def test_daily_reset_utc_boundary(temp_db: Path):
    """Rows straddling UTC midnight count toward different daily totals.

    Seed one row at ``2025-04-29T23:59:00Z`` and another at
    ``2025-04-30T00:01:00Z``; for ``today=2025-04-29`` the cap query
    returns only the first row's cost.
    """
    _seed_ledger(
        temp_db,
        cost_usd=10.0,
        called_at="2025-04-29T23:59:00Z",
    )
    _seed_ledger(
        temp_db,
        cost_usd=15.0,
        called_at="2025-04-30T00:01:00Z",
    )

    res_yesterday = daily_cap_gate(
        db_path=temp_db,
        projected_cost=0.0,
        cap=100.0,
        today=date(2025, 4, 29),
    )
    res_today = daily_cap_gate(
        db_path=temp_db,
        projected_cost=0.0,
        cap=100.0,
        today=date(2025, 4, 30),
    )

    assert res_yesterday.today_total_usd == pytest.approx(10.0)
    assert res_today.today_total_usd == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# VAL-M3-041 — Stage-2 cap is independent of debate cap
# ---------------------------------------------------------------------------


def test_stage2_cap_independent_of_debate_cap(temp_db: Path):
    """Stage-2 PRE-spend filter (``purpose='stage2_event_scoring'``) does
    NOT count debate rows; the existing $10 debate cap is enforced
    against the ``llm_debate`` table only.
    """
    # Stage-2 ledger spend at $19.00.
    _seed_ledger(
        temp_db,
        purpose="stage2_event_scoring",
        cost_usd=19.0,
    )
    # Debate-purpose ledger spend at $9.50 — must NOT count toward
    # stage2 cap.
    _seed_ledger(
        temp_db,
        provider="anthropic",
        purpose="debate",
        cost_usd=9.50,
    )

    res = daily_cap_gate(
        db_path=temp_db,
        projected_cost=0.50,
        cap=20.0,
    )
    # 19 + 0.50 = 19.50 ≤ 20 → allowed; debate $9.50 does NOT count.
    assert res.today_total_usd == pytest.approx(19.0)
    assert res.passed is True


def test_total_llm_ceiling_30_invariant(temp_db: Path):
    """Audit invariant: stage-2 ($20) + debate ($10) ≤ $30/day total.

    Seed today at the boundary ($19 + $0.50 stage-2 + $9 debate) and
    confirm:

    * Stage-2 cap allows the next call (19 + 0.50 ≤ 20).
    * Sum across all llm_cost_ledger rows ≤ $30 (the audit
      invariant).
    """
    _seed_ledger(temp_db, purpose="stage2_event_scoring", cost_usd=19.0)
    _seed_ledger(temp_db, provider="anthropic", purpose="debate", cost_usd=9.0)
    res = daily_cap_gate(db_path=temp_db, projected_cost=0.50, cap=20.0)
    assert res.passed is True

    conn = project_db.connect(temp_db)
    try:
        total = float(
            conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM llm_cost_ledger"
            ).fetchone()[0]
            or 0.0
        )
    finally:
        conn.close()
    assert total <= 30.0


# ---------------------------------------------------------------------------
# Default-projection constant
# ---------------------------------------------------------------------------


def test_module_exposes_call_projection_constant():
    """The module exposes a documented ``STAGE2_CALL_USD_PROJECTION``
    constant (per VAL-M3-038 description) used as the default
    projection when ``projected_cost`` is omitted."""
    assert isinstance(STAGE2_CALL_USD_PROJECTION, float)
    assert STAGE2_CALL_USD_PROJECTION > 0.0
    # Must leave room for at least 20 calls under the $20 cap
    # (i.e. <= $1 per call worst-case projection).
    assert STAGE2_CALL_USD_PROJECTION <= 1.0


def test_default_projection_used_when_kwarg_omitted(temp_db: Path):
    """``projected_cost`` defaults to ``STAGE2_CALL_USD_PROJECTION``
    when omitted; the gate result reflects the resolved value."""
    _seed_ledger(temp_db, cost_usd=1.0)
    res = daily_cap_gate(db_path=temp_db, cap=20.0)
    assert res.projected_cost == pytest.approx(STAGE2_CALL_USD_PROJECTION)


def test_default_cap_resolved_from_config(temp_db: Path, monkeypatch):
    """When ``cap`` kwarg is omitted, the gate resolves it from
    :func:`config.get_llm_stage2_daily_usd_cap` at call time so an
    in-process env override takes effect immediately."""
    monkeypatch.setenv("LLM_STAGE2_DAILY_USD_CAP", "5.0")
    _seed_ledger(temp_db, cost_usd=4.50)
    res = daily_cap_gate(db_path=temp_db, projected_cost=0.60)
    assert res.cap == pytest.approx(5.0)
    # 4.50 + 0.60 > 5.0 → blocks.
    assert res.passed is False
    assert res.reason == GATE_REASON_DAILY_CAP_EXCEEDED


# ---------------------------------------------------------------------------
# Cap-gate is a pure SELECT — no side effects on success
# ---------------------------------------------------------------------------


def test_gate_pass_no_side_effects(temp_db: Path, tmp_path: Path):
    """A passing call writes nothing to ``news_match_log`` or to
    ``audit_latest.json`` — the gate is a pure SELECT."""
    audit_path = tmp_path / "audit_latest.json"
    _seed_ledger(temp_db, cost_usd=1.0)
    res = daily_cap_gate(db_path=temp_db, projected_cost=0.50, cap=20.0)
    assert res.passed is True
    conn = project_db.connect(temp_db)
    try:
        n_rows = int(
            conn.execute("SELECT COUNT(*) FROM news_match_log").fetchone()[0]
        )
    finally:
        conn.close()
    assert n_rows == 0
    assert not audit_path.exists()


# ---------------------------------------------------------------------------
# raise_on_block contract
# ---------------------------------------------------------------------------


def test_raise_on_block_true_raises_daily_cap_exceeded(temp_db: Path):
    _seed_ledger(temp_db, cost_usd=20.5)
    with pytest.raises(DailyCapExceeded):
        daily_cap_gate(
            db_path=temp_db,
            projected_cost=0.60,
            cap=20.0,
            raise_on_block=True,
        )


def test_raise_on_block_false_returns_result_object(temp_db: Path):
    _seed_ledger(temp_db, cost_usd=20.5)
    res = daily_cap_gate(
        db_path=temp_db,
        projected_cost=0.60,
        cap=20.0,
        raise_on_block=False,
    )
    assert isinstance(res, DailyCapGateResult)
    assert res.passed is False


# ---------------------------------------------------------------------------
# f-fix-m3-07 — Cap-hit persistence wired into daily_cap_gate
#
# Scrutiny round 1 found that ``daily_cap_gate`` only WARNED on
# cap-hit and never invoked ``record_stage2_skip`` in production —
# the contract clause attached to VAL-M3-039 / VAL-M3-040 (cap-hit
# audit) requires the gate path itself to persist a
# ``news_match_log`` row AND merge the
# ``stage2_skipped[]`` block in ``audit_latest.json``.
#
# These tests verify the surgical fix: extend the gate's signature
# with optional ``ticker``, ``candidate_event_id``, ``news_event_id``,
# and ``audit_path`` kwargs and, on cap-hit, invoke
# ``record_stage2_skip`` BEFORE the raise/return-result fork (so the
# persistence path runs even when ``raise_on_block=True``). When the
# caller did not supply ``ticker``/``audit_path``, the gate must log
# a single WARNING explaining the persistence skip and still return
# the canonical decision (``passed=False``).
# ---------------------------------------------------------------------------


def test_cap_hit_with_ticker_persists_news_match_log_and_audit(
    temp_db: Path, tmp_path: Path
):
    """On cap-hit with ``ticker`` + ``audit_path`` kwargs supplied, the
    gate writes exactly one ``news_match_log`` row with
    ``reason='daily_cap_exceeded'`` and increments the
    ``stage2_skipped[]`` counter in ``audit_latest.json``."""
    audit_path = tmp_path / "audit_latest.json"
    _seed_ledger(temp_db, cost_usd=19.80)
    _seed_news_event(temp_db, news_event_id=7, ticker="ABCX")

    # Pre-conditions — no audit JSON, no news_match_log rows.
    assert not audit_path.exists()
    conn = project_db.connect(temp_db)
    try:
        pre_rows = int(
            conn.execute(
                "SELECT COUNT(*) FROM news_match_log "
                "WHERE reason='daily_cap_exceeded'"
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert pre_rows == 0

    res = daily_cap_gate(
        db_path=temp_db,
        projected_cost=0.60,
        cap=20.0,
        ticker="ABCX",
        candidate_event_id=42,
        news_event_id=7,
        audit_path=audit_path,
    )

    assert res.passed is False
    assert res.reason == GATE_REASON_DAILY_CAP_EXCEEDED

    # Exactly one news_match_log row with reason='daily_cap_exceeded'.
    conn = project_db.connect(temp_db)
    try:
        rows = conn.execute(
            "SELECT ticker, matched, reason FROM news_match_log "
            "WHERE reason='daily_cap_exceeded'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "ABCX"
    assert int(rows[0]["matched"]) == 0
    assert rows[0]["reason"] == "daily_cap_exceeded"

    # Audit JSON has stage2_skipped[reason=daily_cap_exceeded].count >= 1.
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    skipped = payload.get("stage2_skipped")
    assert isinstance(skipped, list) and skipped
    cap_entry = next(
        s for s in skipped if s.get("reason") == "daily_cap_exceeded"
    )
    assert int(cap_entry["count"]) == 1
    assert cap_entry.get("last_ticker") == "ABCX"
    assert int(cap_entry.get("last_candidate_event_id") or 0) == 42
    assert int(cap_entry.get("last_news_event_id") or 0) == 7


def test_cap_hit_without_ticker_skips_persistence_and_warns(
    temp_db: Path, caplog
):
    """On cap-hit when caller did NOT supply ``ticker``, the gate logs
    a single WARNING about skipped persistence, writes ZERO
    ``news_match_log`` rows, and still returns the
    ``passed=False`` decision."""
    import logging

    caplog.set_level(logging.WARNING, logger=stage2_gates.__name__)
    _seed_ledger(temp_db, cost_usd=19.80)

    # Pre-condition.
    conn = project_db.connect(temp_db)
    try:
        pre_rows = int(
            conn.execute("SELECT COUNT(*) FROM news_match_log").fetchone()[0]
        )
    finally:
        conn.close()

    res = daily_cap_gate(
        db_path=temp_db,
        projected_cost=0.60,
        cap=20.0,
    )

    # Decision-only path is preserved.
    assert isinstance(res, DailyCapGateResult)
    assert res.passed is False
    assert res.reason == GATE_REASON_DAILY_CAP_EXCEEDED

    # Zero news_match_log rows added.
    conn = project_db.connect(temp_db)
    try:
        post_rows = int(
            conn.execute("SELECT COUNT(*) FROM news_match_log").fetchone()[0]
        )
    finally:
        conn.close()
    assert post_rows == pre_rows

    # WARNING log line about skipped persistence.
    persistence_warnings = [
        rec
        for rec in caplog.records
        if rec.levelno >= logging.WARNING
        and "persistence skipped" in rec.getMessage()
    ]
    assert len(persistence_warnings) == 1, (
        f"expected exactly one 'persistence skipped' WARNING, got "
        f"{[r.getMessage() for r in caplog.records]}"
    )


def test_cap_hit_without_audit_path_skips_persistence_and_warns(
    temp_db: Path, caplog
):
    """``ticker`` supplied but ``audit_path`` omitted is also a
    persistence-skipped condition (both kwargs are required for the
    persistence path)."""
    import logging

    caplog.set_level(logging.WARNING, logger=stage2_gates.__name__)
    _seed_ledger(temp_db, cost_usd=19.80)

    res = daily_cap_gate(
        db_path=temp_db,
        projected_cost=0.60,
        cap=20.0,
        ticker="ABCX",
    )

    assert res.passed is False
    assert res.reason == GATE_REASON_DAILY_CAP_EXCEEDED

    # Zero news_match_log rows.
    conn = project_db.connect(temp_db)
    try:
        n_rows = int(
            conn.execute(
                "SELECT COUNT(*) FROM news_match_log "
                "WHERE reason='daily_cap_exceeded'"
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert n_rows == 0

    persistence_warnings = [
        rec
        for rec in caplog.records
        if rec.levelno >= logging.WARNING
        and "persistence skipped" in rec.getMessage()
    ]
    assert len(persistence_warnings) == 1


def test_cap_not_hit_does_not_invoke_recorder(
    temp_db: Path, tmp_path: Path
):
    """Regression: when the cap is NOT hit (passed=True), the gate
    must NOT touch ``news_match_log`` or ``audit_latest.json`` even
    when ``ticker`` + ``audit_path`` kwargs are supplied. The recorder
    is invoked ONLY on cap-hit."""
    audit_path = tmp_path / "audit_latest.json"
    _seed_ledger(temp_db, cost_usd=10.0)

    res = daily_cap_gate(
        db_path=temp_db,
        projected_cost=0.50,
        cap=20.0,
        ticker="ABCX",
        candidate_event_id=42,
        news_event_id=7,
        audit_path=audit_path,
    )
    assert res.passed is True
    assert res.reason is None

    conn = project_db.connect(temp_db)
    try:
        n_rows = int(
            conn.execute("SELECT COUNT(*) FROM news_match_log").fetchone()[0]
        )
    finally:
        conn.close()
    assert n_rows == 0
    assert not audit_path.exists()


def test_raise_on_block_persists_then_raises(
    temp_db: Path, tmp_path: Path
):
    """``raise_on_block=True`` on cap-hit attempts the persistence
    write FIRST and THEN raises ``DailyCapExceeded`` — the
    ``news_match_log`` row + audit JSON merge must land BEFORE the
    exception propagates so the audit trail is preserved even on
    exception-driven control-flow paths."""
    audit_path = tmp_path / "audit_latest.json"
    _seed_ledger(temp_db, cost_usd=20.5)
    _seed_news_event(temp_db, news_event_id=11, ticker="ABCX")

    with pytest.raises(DailyCapExceeded):
        daily_cap_gate(
            db_path=temp_db,
            projected_cost=0.60,
            cap=20.0,
            ticker="ABCX",
            candidate_event_id=99,
            news_event_id=11,
            audit_path=audit_path,
            raise_on_block=True,
        )

    # news_match_log row was written before the exception.
    conn = project_db.connect(temp_db)
    try:
        rows = conn.execute(
            "SELECT ticker, matched, reason FROM news_match_log "
            "WHERE reason='daily_cap_exceeded'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "ABCX"

    # audit_latest.json was merged before the exception.
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    skipped = payload["stage2_skipped"]
    cap_entry = next(
        s for s in skipped if s.get("reason") == "daily_cap_exceeded"
    )
    assert int(cap_entry["count"]) == 1


def test_raise_on_block_without_ticker_does_not_persist(
    temp_db: Path, caplog
):
    """``raise_on_block=True`` with ticker omitted: persistence is
    skipped (WARNING logged), then the gate raises
    ``DailyCapExceeded`` — no ``news_match_log`` rows are written."""
    import logging

    caplog.set_level(logging.WARNING, logger=stage2_gates.__name__)
    _seed_ledger(temp_db, cost_usd=20.5)

    with pytest.raises(DailyCapExceeded):
        daily_cap_gate(
            db_path=temp_db,
            projected_cost=0.60,
            cap=20.0,
            raise_on_block=True,
        )

    conn = project_db.connect(temp_db)
    try:
        n_rows = int(
            conn.execute("SELECT COUNT(*) FROM news_match_log").fetchone()[0]
        )
    finally:
        conn.close()
    assert n_rows == 0

    persistence_warnings = [
        rec
        for rec in caplog.records
        if rec.levelno >= logging.WARNING
        and "persistence skipped" in rec.getMessage()
    ]
    assert len(persistence_warnings) == 1
