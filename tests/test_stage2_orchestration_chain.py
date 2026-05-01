"""Orchestration-level tests for the cheap-first chain (f-fix-m3-13).

Feature: ``f-fix-m3-13-cheap-first-orchestration-coverage``.

The existing :mod:`tests.test_cheap_first_invariant` module pins the
cheap-first ordering invariant by exercising each helper directly
(``cooldown_gate`` / ``armed_gate`` / ``daily_cap_gate``). That
coverage is necessary but not sufficient: an orchestration-level
re-ordering bug (e.g. the dispatcher invoking the 4-provider fan-out
BEFORE checking ``armed``) would pass the helper-level tests but
violate the contract VAL-M3-071 requires.

This module drives the production Stage-2 entry helper —
:func:`biotech_sniper.exec.stage2_dispatcher.run_stage2_chain` — end
to end, asserting that EACH cheap-first rejection reason
(``cooldown_active`` / ``armed_file_missing`` /
``daily_cap_exceeded``) short-circuits the chain BEFORE any LLM
provider callable is invoked. The four provider callables are
``Mock``-injected; each test asserts ``assert_not_called`` on every
mock + zero new ``llm_cost_ledger`` rows for the four canonical
providers + zero ``ensemble_scores_event`` rows for the candidate.

Validation contract assertion verified
--------------------------------------

* **VAL-M3-071** — Short-circuit ordering invariant: cheap gates
  fail-fast before LLM fan-out, observed at the orchestration entry
  (not just at each helper independently).
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from biotech_sniper import db as project_db
from biotech_sniper.exec.stage2_dispatcher import run_stage2_chain
from biotech_sniper.llm.stage2_gates import (
    GATE_REASON_ARMED_FILE_MISSING,
    GATE_REASON_COOLDOWN_ACTIVE,
    GATE_REASON_DAILY_CAP_EXCEEDED,
)
from biotech_sniper.migrations.runner import run as run_v10
from tests.test_cheap_first_invariant import (
    _count_ledger,
    _count_ledger_by_providers,
    _iso,
    _seed_cooldown,
    _seed_ledger_row,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Fresh SQLite db with the v10 (Reading-B foundations) schema applied."""

    db_path = tmp_path / "stage2_chain.db"
    run_v10(db_path, target_version=11, take_backup_first=False)
    return db_path


@pytest.fixture
def armed_path_present(tmp_path: Path) -> Path:
    """A regular readable file standing in for the operator-armed marker."""

    p = tmp_path / "armed_present" / ".armed"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")
    return p


@pytest.fixture
def armed_path_missing(tmp_path: Path) -> Path:
    """A path that does NOT exist (the armed gate must reject)."""

    return tmp_path / "armed_missing" / ".armed"


def _seed_candidate_event(
    db_path: Path,
    *,
    ticker: str,
    matched_keywords: str = "pdufa",
) -> int:
    """Insert one ``candidate_events`` row and return its primary key."""

    # First seed a news_events row so the FK resolves cleanly.
    conn = project_db.connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO news_events ("
                "ticker, source, title, url, published_at, ingested_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    ticker,
                    "test_source",
                    "headline",
                    f"https://example.com/{ticker}",
                    "2026-04-30T12:00:00Z",
                    "2026-04-30T12:00:01Z",
                ),
            )
            news_event_id = int(cur.lastrowid or 0)
        with conn:
            cur = conn.execute(
                "INSERT INTO candidate_events ("
                "ticker, source_news_event_id, matched_keywords, "
                "calendar_match, emitted_at, dedup_key"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    ticker,
                    news_event_id,
                    matched_keywords,
                    None,
                    "2026-04-30T12:00:02Z",
                    f"dedup-{ticker}-{news_event_id}",
                ),
            )
            cand_id = int(cur.lastrowid or 0)
    finally:
        conn.close()
    return cand_id


def _count_ensemble_rows(db_path: Path, candidate_event_id: int) -> int:
    """Count rows in ``ensemble_scores_event`` for one candidate event."""

    conn = project_db.connect(db_path)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM ensemble_scores_event "
                "WHERE candidate_event_id = ?",
                (candidate_event_id,),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _make_recording_providers() -> dict[str, MagicMock]:
    """Return ``{provider_name: MagicMock}`` for the four LLM providers.

    Each mock records its invocation count via ``Mock.call_count``;
    the orchestration tests assert ``assert_not_called`` on every
    mock to prove the cheap-first short-circuit invariant.
    """

    return {
        "xai": MagicMock(name="xai_provider"),
        "anthropic": MagicMock(name="anthropic_provider"),
        "gemini": MagicMock(name="gemini_provider"),
        "perplexity": MagicMock(name="perplexity_provider"),
    }


# ---------------------------------------------------------------------------
# Test 1 — cooldown short-circuits the chain at the orchestration entry
# ---------------------------------------------------------------------------


class TestOrchestrationCooldownShortCircuit:
    """Cooldown active for a ticker → orchestration short-circuits.

    The four LLM provider callables MUST NOT be invoked, no
    ``llm_cost_ledger`` row appears for the canonical providers, and
    no ``ensemble_scores_event`` row is written for the candidate.
    """

    def test_cooldown_active_short_circuits_before_llm_fanout(
        self,
        temp_db: Path,
        armed_path_present: Path,
    ) -> None:
        """Active cooldown rejects orchestration before any LLM call.

        Even with armed file present and ledger empty (cap clean),
        the cooldown rejection alone must short-circuit the chain.
        """

        ticker = "NVAX"
        cand_id = _seed_candidate_event(temp_db, ticker=ticker)

        # Seed cooldown active (1h ago against 24h window).
        now = _dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)
        _seed_cooldown(
            temp_db,
            ticker=ticker,
            last_entry_at=_iso(now - _dt.timedelta(hours=1)),
            cooldown_hours=24,
        )
        providers = _make_recording_providers()

        # Pre-state snapshot.
        assert _count_ledger_by_providers(temp_db) == 0
        assert _count_ensemble_rows(temp_db, cand_id) == 0

        result = run_stage2_chain(
            candidate_event_row={
                "id": cand_id,
                "ticker": ticker,
                "matched_keywords": "pdufa",
            },
            db_path=temp_db,
            armed_path=armed_path_present,
            now=now,
            providers=providers,
        )

        # (a) Cooldown rejects — chain short-circuits.
        assert result.passed is False
        assert result.reason == GATE_REASON_COOLDOWN_ACTIVE

        # (b) NONE of the four LLM provider callables were invoked.
        for name, mock in providers.items():
            mock.assert_not_called()
            assert mock.call_count == 0, (
                f"provider {name!r} was invoked despite cooldown rejection"
            )

        # (c) Zero rows in llm_cost_ledger filtered by today AND
        #     provider IN ('xai','anthropic','gemini','perplexity').
        today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
        conn = project_db.connect(temp_db)
        try:
            today_rows = int(
                conn.execute(
                    "SELECT COUNT(*) FROM llm_cost_ledger "
                    "WHERE DATE(called_at) = ? "
                    "AND provider IN ('xai','anthropic','gemini','perplexity')",
                    (today,),
                ).fetchone()[0]
            )
        finally:
            conn.close()
        assert today_rows == 0
        assert _count_ledger_by_providers(temp_db) == 0
        assert _count_ledger(temp_db) == 0

        # (d) Zero rows in ensemble_scores_event for this candidate.
        assert _count_ensemble_rows(temp_db, cand_id) == 0


# ---------------------------------------------------------------------------
# Test 2 — armed-missing short-circuits the chain when cooldown clear
# ---------------------------------------------------------------------------


class TestOrchestrationArmedMissingShortCircuit:
    """Armed file missing → orchestration short-circuits at gate 2.

    The cooldown gate is intentionally clear (no row), so the
    rejection MUST come from the armed gate. Same invariants as the
    cooldown test: zero LLM calls, zero ledger rows, zero ensemble
    rows.
    """

    def test_armed_missing_short_circuits_before_llm_fanout(
        self,
        temp_db: Path,
        armed_path_missing: Path,
    ) -> None:
        """Cooldown clear + armed missing → reject at armed gate."""

        ticker = "FRESH"
        cand_id = _seed_candidate_event(temp_db, ticker=ticker)

        # No cooldown row → cooldown gate passes.
        # The armed_path target does not exist → armed gate rejects.
        assert not armed_path_missing.exists()

        providers = _make_recording_providers()

        # Pre-state snapshot.
        assert _count_ledger_by_providers(temp_db) == 0
        assert _count_ensemble_rows(temp_db, cand_id) == 0

        result = run_stage2_chain(
            candidate_event_row={
                "id": cand_id,
                "ticker": ticker,
                "matched_keywords": "pdufa",
            },
            db_path=temp_db,
            armed_path=armed_path_missing,
            providers=providers,
        )

        # (a) Armed-missing rejects — chain short-circuits.
        assert result.passed is False
        assert result.reason == GATE_REASON_ARMED_FILE_MISSING

        # (b) NONE of the four LLM provider callables were invoked.
        for name, mock in providers.items():
            mock.assert_not_called()
            assert mock.call_count == 0, (
                f"provider {name!r} was invoked despite armed-missing rejection"
            )

        # (c) Zero rows in llm_cost_ledger filtered by today AND
        #     provider IN ('xai','anthropic','gemini','perplexity').
        today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
        conn = project_db.connect(temp_db)
        try:
            today_rows = int(
                conn.execute(
                    "SELECT COUNT(*) FROM llm_cost_ledger "
                    "WHERE DATE(called_at) = ? "
                    "AND provider IN ('xai','anthropic','gemini','perplexity')",
                    (today,),
                ).fetchone()[0]
            )
        finally:
            conn.close()
        assert today_rows == 0
        assert _count_ledger_by_providers(temp_db) == 0
        assert _count_ledger(temp_db) == 0

        # (d) Zero rows in ensemble_scores_event for this candidate.
        assert _count_ensemble_rows(temp_db, cand_id) == 0


# ---------------------------------------------------------------------------
# Test 3 — cap short-circuits the chain when cooldown + armed pass
# ---------------------------------------------------------------------------


class TestOrchestrationCapHitShortCircuit:
    """Daily Stage-2 $ cap exceeded → orchestration short-circuits at gate 3.

    Cooldown is clear, armed file is present, ledger is seeded over
    the cap. The rejection MUST come from the cap gate. Beyond the
    seeded cap-overflow row, NO new ledger rows appear and NO
    ensemble rows are written for the candidate.
    """

    def test_cap_hit_short_circuits_before_llm_fanout(
        self,
        temp_db: Path,
        armed_path_present: Path,
    ) -> None:
        """Cap-hit short-circuits orchestration; only the seeded
        ledger row exists."""

        ticker = "ABCD"
        cand_id = _seed_candidate_event(temp_db, ticker=ticker)

        # Seed today's stage2 ledger past the $20 cap.
        today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
        seeded_called_at = f"{today}T11:00:00.000Z"
        _seed_ledger_row(
            temp_db,
            cost_usd=20.50,
            called_at=seeded_called_at,
        )
        # Snapshot the seeded provider-row count so the post-rejection
        # delta cleanly excludes the seed.
        pre_ledger_total = _count_ledger(temp_db)
        pre_provider_total = _count_ledger_by_providers(temp_db)
        assert pre_ledger_total == 1
        assert pre_provider_total == 1  # seeded perplexity row

        providers = _make_recording_providers()

        # Pre-state snapshot for the candidate.
        assert _count_ensemble_rows(temp_db, cand_id) == 0

        result = run_stage2_chain(
            candidate_event_row={
                "id": cand_id,
                "ticker": ticker,
                "matched_keywords": "pdufa",
            },
            db_path=temp_db,
            armed_path=armed_path_present,
            providers=providers,
        )

        # (a) Cap rejects — chain short-circuits.
        assert result.passed is False
        assert result.reason == GATE_REASON_DAILY_CAP_EXCEEDED

        # (b) NONE of the four LLM provider callables were invoked.
        for name, mock in providers.items():
            mock.assert_not_called()
            assert mock.call_count == 0, (
                f"provider {name!r} was invoked despite cap-hit rejection"
            )

        # (c) Zero NEW rows in llm_cost_ledger (the seeded row stays
        #     but the gate adds nothing). Filtered by today AND
        #     provider IN ('xai','anthropic','gemini','perplexity').
        conn = project_db.connect(temp_db)
        try:
            today_rows = int(
                conn.execute(
                    "SELECT COUNT(*) FROM llm_cost_ledger "
                    "WHERE DATE(called_at) = ? "
                    "AND provider IN ('xai','anthropic','gemini','perplexity')",
                    (today,),
                ).fetchone()[0]
            )
        finally:
            conn.close()
        # The seeded cap-overflow row IS counted by the query above;
        # the orchestration test asserts ZERO ROWS BEYOND the seed.
        assert today_rows - pre_provider_total == 0
        assert _count_ledger_by_providers(temp_db) == pre_provider_total
        assert _count_ledger(temp_db) == pre_ledger_total

        # (d) Zero rows in ensemble_scores_event for this candidate.
        assert _count_ensemble_rows(temp_db, cand_id) == 0
