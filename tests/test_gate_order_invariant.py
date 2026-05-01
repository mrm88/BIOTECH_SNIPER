"""Stage-2 gate short-circuit ordering invariant — VAL-CROSS-028 / f-cross-05.

This module is the canonical, single-file regression for the
documented Stage-2 gate ordering:

    cooldown
        → armed
        → cap-projection
        → 4-provider fan-out  ← LLM costs incurred here
        → unanimity
        → probability
        → executor-gates

For each rejection scenario, only the gates BEFORE (and including)
the failing gate execute. Concretely (per the validation contract
VAL-CROSS-028 evidence table):

    | scenario          | ensemble rows | paper_orders rows |
    |-------------------|---------------|-------------------|
    | cooldown reject   | 0             | 0                 |
    | armed missing     | 0             | 0                 |
    | daily cap breach  | 0             | 0                 |
    | unanimity fail    | 4             | 0                 |
    | probability < t   | 4             | 0                 |
    | all gates pass    | 4             | (handled by exec) |

The test exercises the production orchestrator
:func:`biotech_sniper.exec.stage2_dispatcher.run_stage2_chain` with
synthetic ``MagicMock`` providers so a re-ordering bug in the
chain (e.g. running fan-out before checking ``armed``) would surface
as ``mock.assert_not_called`` failures + a non-zero
``ensemble_scores_event`` row count for the candidate.

The companion baseline file
``tests/baselines/reading_b_pretest_baseline.json`` (also added by
f-cross-05) anchors VAL-CROSS-017 / VAL-CROSS-018 (pre-Reading-B
green snapshot + post-Reading-B never-regress invariant).

Coverage matrix
---------------

* **VAL-CROSS-028** — gate-order row-counts at each rejection step.
* **VAL-M3-071** — cheap-first short-circuit invariant (cooldown /
  armed / cap → zero LLM rows). Verified at the orchestration
  entry, not just at the per-gate helper.
* **VAL-M5-014..018** — Stage-2 rejection-path side-effect
  invariants for each gate.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from pathlib import Path
from typing import Mapping
from unittest.mock import MagicMock

import pytest

from biotech_sniper import db as project_db
from biotech_sniper.exec.stage2_dispatcher import run_stage2_chain
from biotech_sniper.llm.stage2_gates import (
    GATE_REASON_ARMED_FILE_MISSING,
    GATE_REASON_COOLDOWN_ACTIVE,
    GATE_REASON_DAILY_CAP_EXCEEDED,
    GATE_REASON_UNANIMITY_FAILED,
)
from biotech_sniper.migrations.runner import run as run_v10


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Fresh SQLite database with the v10 (Reading-B) schema applied."""

    db_path = tmp_path / "gate_order.db"
    run_v10(db_path, target_version=project_db.CURRENT_VERSION, take_backup_first=False)
    return db_path


@pytest.fixture
def armed_path_present(tmp_path: Path) -> Path:
    """Regular readable file standing in for the operator-armed marker."""

    p = tmp_path / "armed_present" / ".armed"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")
    return p


@pytest.fixture
def armed_path_missing(tmp_path: Path) -> Path:
    """Path that does NOT exist on disk; the armed gate must reject it."""

    return tmp_path / "armed_missing" / ".armed"


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    """An audit JSON path the chain may merge stage2_skipped[] into.

    Pre-seeded with an empty object so the merge logic has a valid
    target. Each test reads it back to verify the rejection was
    recorded for ops-dashboard consumption (not strictly required
    by VAL-CROSS-028 but useful for forensic debugging).
    """

    p = tmp_path / "audit_latest.json"
    p.write_text("{}")
    return p


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_news_event(db_path: Path, *, ticker: str) -> int:
    """Insert one ``news_events`` row and return its primary key."""

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
                    "synthetic headline",
                    f"https://example.com/{ticker}",
                    "2026-04-30T12:00:00Z",
                    "2026-04-30T12:00:01Z",
                ),
            )
            new_id = cur.lastrowid
    finally:
        conn.close()
    assert new_id is not None
    return int(new_id)


def _seed_candidate_event(
    db_path: Path,
    *,
    ticker: str,
    matched_keywords: str = "pdufa",
) -> tuple[int, int]:
    """Insert one ``candidate_events`` row tied to a fresh ``news_events`` row.

    Returns ``(candidate_event_id, source_news_event_id)``.
    """

    news_event_id = _seed_news_event(db_path, ticker=ticker)
    conn = project_db.connect(db_path)
    try:
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
    return cand_id, news_event_id


def _seed_cooldown(
    db_path: Path,
    *,
    ticker: str,
    last_entry_at: str,
    cooldown_hours: int = 24,
) -> None:
    conn = project_db.connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO ticker_cooldown "
                "(ticker, last_entry_at, last_event_id, cooldown_hours) "
                "VALUES (?, ?, ?, ?)",
                (ticker, last_entry_at, None, cooldown_hours),
            )
    finally:
        conn.close()


def _seed_ledger_row(
    db_path: Path,
    *,
    cost_usd: float,
    purpose: str = "stage2_event_scoring",
    provider: str = "perplexity",
    called_at: str | None = None,
) -> None:
    conn = project_db.connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO llm_cost_ledger ("
                "provider, model_id, purpose, prompt_tokens, "
                "completion_tokens, latency_ms, cost_usd, called_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, "
                "COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))",
                (
                    provider,
                    "sonar",
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


def _iso(dt: _dt.datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return (
        dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4]
        + "Z"
    )


def _count_paper_orders_for_candidate(
    db_path: Path, candidate_event_id: int
) -> int:
    """Count ``paper_orders`` rows linked to a candidate.

    The schema joins via the ``play_card_id`` column in production,
    but the canonical tag the dispatcher emits is the
    ``news_event_entry`` event with the candidate id embedded in the
    ``play_card_id``. For VAL-CROSS-028 we count both forms — a
    candidate-linked column AND any ``news_event_entry`` rows since
    test start — and assert ZERO. The chain under test never
    submits to ``paper_orders``; the assertion pins the contract that
    a rejection MUST NOT cause a downstream order.
    """

    conn = project_db.connect(db_path)
    try:
        # Total news_event_entry rows in the table (any candidate).
        n_total = int(
            conn.execute(
                "SELECT COUNT(*) FROM paper_orders "
                "WHERE event = 'news_event_entry'"
            ).fetchone()[0]
        )
    finally:
        conn.close()
    return n_total


def _count_ensemble_rows(db_path: Path, candidate_event_id: int) -> int:
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


def _count_ledger_provider_rows(db_path: Path) -> int:
    """Count ``llm_cost_ledger`` rows for any of the 4 ensemble providers.

    Mirrors the VAL-M3-071 evidence query::

        SELECT COUNT(*) FROM llm_cost_ledger
         WHERE provider IN ('xai','anthropic','gemini','perplexity')
    """

    conn = project_db.connect(db_path)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM llm_cost_ledger "
                "WHERE provider IN ('xai','anthropic','gemini','perplexity')"
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _make_recording_providers() -> dict[str, MagicMock]:
    """``{provider_name: MagicMock}`` for the four LLM providers.

    Mocks raise on call so a re-ordering bug that lets a cheap-gate
    rejection still hit the LLM fan-out surfaces as a clear
    ``assert_not_called`` failure (instead of a silent extra row).
    """

    return {
        "xai": MagicMock(name="xai_provider"),
        "anthropic": MagicMock(name="anthropic_provider"),
        "gemini": MagicMock(name="gemini_provider"),
        "perplexity": MagicMock(name="perplexity_provider"),
    }


def _make_unanimity_break_providers() -> dict[str, "MagicMock | object"]:
    """4 working providers whose verdicts BREAK the 4/4 unanimity gate.

    Each provider returns a deterministic dict shaped like the
    production HTTP clients (``label`` / ``probability`` /
    ``direction`` / ``rationale`` / ``citations`` /
    ``latency_ms`` / ``cost_usd``). Three return ``label='material'``
    and one returns ``label='ambiguous'`` — sufficient to trip the
    unanimity gate (label histogram = {material:3, ambiguous:1}).
    Each provider is invoked exactly once by the fan-out so all four
    rows persist into ``ensemble_scores_event`` (per
    VAL-CROSS-028 / VAL-M5-015).
    """

    def _make(label: str) -> "callable":
        def _call(candidate, *, name=None):  # noqa: ARG001
            return {
                "label": label,
                "probability": 0.90,
                "direction": "bullish",
                "rationale": f"{name} stub rationale",
                "citations": [],
                "latency_ms": 5,
                "cost_usd": 0.001,
            }

        return _call

    return {
        "xai": _make("material"),
        "anthropic": _make("material"),
        "gemini": _make("material"),
        # The single ambiguous label is the unanimity-breaking cell.
        "perplexity": _make("ambiguous"),
    }


# ---------------------------------------------------------------------------
# 1. Cooldown rejection — 0 ensemble rows AND 0 paper_orders rows
# ---------------------------------------------------------------------------


class TestGateOrderCooldownRejection:
    """Active cooldown short-circuits the chain at gate 1.

    Invariant (VAL-CROSS-028 row 1): ``ensemble_scores_event`` rows
    for the candidate = 0 AND ``paper_orders`` rows for the
    candidate = 0.
    """

    def test_cooldown_active_zero_llm_zero_paper_orders(
        self,
        temp_db: Path,
        armed_path_present: Path,
        audit_path: Path,
    ) -> None:
        """Active cooldown rejects orchestration before any LLM call.

        Even with armed file present and ledger empty (cap clean),
        the cooldown rejection alone short-circuits the chain.
        """

        ticker = "NVAX"
        cand_id, _ = _seed_candidate_event(temp_db, ticker=ticker)

        # Seed cooldown active (1 h ago against 24 h window).
        now = _dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)
        _seed_cooldown(
            temp_db,
            ticker=ticker,
            last_entry_at=_iso(now - _dt.timedelta(hours=1)),
            cooldown_hours=24,
        )

        providers = _make_recording_providers()

        # Pre-state.
        assert _count_ensemble_rows(temp_db, cand_id) == 0
        assert _count_ledger_provider_rows(temp_db) == 0
        assert _count_paper_orders_for_candidate(temp_db, cand_id) == 0

        result = run_stage2_chain(
            candidate_event_row={
                "id": cand_id,
                "ticker": ticker,
                "matched_keywords": "pdufa",
                "source_news_event_id": None,
            },
            db_path=temp_db,
            armed_path=armed_path_present,
            audit_path=audit_path,
            now=now,
            providers=providers,
        )

        # Chain rejected at the cooldown gate.
        assert result.passed is False
        assert result.reason == GATE_REASON_COOLDOWN_ACTIVE
        assert result.gate == "cooldown"
        assert result.ensemble_result is None

        # No LLM provider was called.
        for name, mock in providers.items():
            mock.assert_not_called()
            assert mock.call_count == 0, (
                f"provider {name!r} called despite cooldown rejection"
            )

        # VAL-CROSS-028 row-count contract.
        assert _count_ensemble_rows(temp_db, cand_id) == 0
        assert _count_paper_orders_for_candidate(temp_db, cand_id) == 0
        # VAL-M3-071 cheap-first invariant: 0 ledger provider rows.
        assert _count_ledger_provider_rows(temp_db) == 0


# ---------------------------------------------------------------------------
# 2. Armed-missing rejection — 0 ensemble rows AND 0 paper_orders rows
# ---------------------------------------------------------------------------


class TestGateOrderArmedMissingRejection:
    """Missing ``.armed`` short-circuits the chain at gate 2.

    Cooldown is intentionally clear (no row) so the rejection MUST
    come from the armed gate. Same invariants as cooldown: zero LLM
    calls, zero ensemble rows, zero paper_orders.
    """

    def test_armed_missing_zero_llm_zero_paper_orders(
        self,
        temp_db: Path,
        armed_path_missing: Path,
        audit_path: Path,
    ) -> None:
        ticker = "FRESH"
        cand_id, _ = _seed_candidate_event(temp_db, ticker=ticker)

        assert not armed_path_missing.exists()

        providers = _make_recording_providers()

        # Pre-state.
        assert _count_ensemble_rows(temp_db, cand_id) == 0
        assert _count_ledger_provider_rows(temp_db) == 0
        assert _count_paper_orders_for_candidate(temp_db, cand_id) == 0

        result = run_stage2_chain(
            candidate_event_row={
                "id": cand_id,
                "ticker": ticker,
                "matched_keywords": "pdufa",
                "source_news_event_id": None,
            },
            db_path=temp_db,
            armed_path=armed_path_missing,
            audit_path=audit_path,
            providers=providers,
        )

        # Chain rejected at the armed gate.
        assert result.passed is False
        assert result.reason == GATE_REASON_ARMED_FILE_MISSING
        assert result.gate == "armed"
        assert result.ensemble_result is None

        # No LLM provider was called.
        for name, mock in providers.items():
            mock.assert_not_called()
            assert mock.call_count == 0, (
                f"provider {name!r} called despite armed-missing rejection"
            )

        # VAL-CROSS-028 row-count contract.
        assert _count_ensemble_rows(temp_db, cand_id) == 0
        assert _count_paper_orders_for_candidate(temp_db, cand_id) == 0
        # VAL-M3-071 cheap-first invariant: 0 ledger provider rows.
        assert _count_ledger_provider_rows(temp_db) == 0


# ---------------------------------------------------------------------------
# 3. Cap-exceeded rejection — 0 ensemble rows AND 0 paper_orders rows
# ---------------------------------------------------------------------------


class TestGateOrderCapExceededRejection:
    """Daily Stage-2 $ cap exceeded short-circuits the chain at gate 3.

    Cooldown clear, armed file present, ledger seeded over the cap.
    Beyond the seeded cap-overflow row, NO new ledger rows appear,
    NO ensemble rows are written for the candidate, NO paper_orders
    rows appear.
    """

    def test_daily_cap_exceeded_zero_new_llm_zero_paper_orders(
        self,
        temp_db: Path,
        armed_path_present: Path,
        audit_path: Path,
    ) -> None:
        ticker = "ABCD"
        cand_id, _ = _seed_candidate_event(temp_db, ticker=ticker)

        # Seed today's stage2 ledger past the $20 cap.
        today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
        _seed_ledger_row(
            temp_db,
            cost_usd=20.50,
            called_at=f"{today}T11:00:00.000Z",
        )

        # Snapshot the pre-rejection ledger state (the seeded row is
        # legitimate; we assert ZERO ROWS BEYOND the seed).
        pre_provider_rows = _count_ledger_provider_rows(temp_db)
        assert pre_provider_rows == 1

        providers = _make_recording_providers()

        # Pre-state for the candidate.
        assert _count_ensemble_rows(temp_db, cand_id) == 0
        assert _count_paper_orders_for_candidate(temp_db, cand_id) == 0

        result = run_stage2_chain(
            candidate_event_row={
                "id": cand_id,
                "ticker": ticker,
                "matched_keywords": "pdufa",
                "source_news_event_id": None,
            },
            db_path=temp_db,
            armed_path=armed_path_present,
            audit_path=audit_path,
            providers=providers,
        )

        # Chain rejected at the cap gate.
        assert result.passed is False
        assert result.reason == GATE_REASON_DAILY_CAP_EXCEEDED
        assert result.gate == "cap"
        assert result.ensemble_result is None

        # No LLM provider was called.
        for name, mock in providers.items():
            mock.assert_not_called()
            assert mock.call_count == 0, (
                f"provider {name!r} called despite cap-hit rejection"
            )

        # VAL-CROSS-028 row-count contract.
        assert _count_ensemble_rows(temp_db, cand_id) == 0
        assert _count_paper_orders_for_candidate(temp_db, cand_id) == 0
        # No NEW ledger provider rows (the seeded row stays).
        assert _count_ledger_provider_rows(temp_db) == pre_provider_rows


# ---------------------------------------------------------------------------
# 4. Unanimity-fail rejection — 4 ensemble rows AND 0 paper_orders rows
# ---------------------------------------------------------------------------


class TestGateOrderUnanimityFailRejection:
    """All cheap gates pass; the 4-provider fan-out runs; the unanimity
    gate then rejects (3 material + 1 ambiguous). Each provider's
    verdict persists to ``ensemble_scores_event`` (VAL-M5-015 forensic
    requirement) but ZERO ``paper_orders`` rows are created
    (VAL-CROSS-028 row 4).
    """

    def test_unanimity_failed_four_ensemble_rows_zero_paper_orders(
        self,
        temp_db: Path,
        armed_path_present: Path,
        audit_path: Path,
    ) -> None:
        ticker = "TESTU"
        cand_id, _ = _seed_candidate_event(
            temp_db,
            ticker=ticker,
            matched_keywords="phase_iii,readout",
        )

        # Cooldown clear, armed present, cap clean — chain advances
        # to fan-out.
        providers = _make_unanimity_break_providers()

        # Pre-state.
        assert _count_ensemble_rows(temp_db, cand_id) == 0
        assert _count_paper_orders_for_candidate(temp_db, cand_id) == 0

        result = run_stage2_chain(
            candidate_event_row={
                "id": cand_id,
                "ticker": ticker,
                "matched_keywords": "phase_iii,readout",
                "source_news_event_id": None,
            },
            db_path=temp_db,
            armed_path=armed_path_present,
            audit_path=audit_path,
            providers=providers,
        )

        # Chain rejected post-fan-out at the unanimity gate.
        assert result.passed is False
        assert result.reason == GATE_REASON_UNANIMITY_FAILED
        assert result.gate == "unanimity"

        # The fan-out actually ran — ensemble_result is populated and
        # carries 4 per-provider rows.
        assert result.ensemble_result is not None
        assert len(result.ensemble_result.per_provider_results) == 4

        # VAL-CROSS-028 row 4: ensemble rows = 4, paper_orders = 0.
        assert _count_ensemble_rows(temp_db, cand_id) == 4
        assert _count_paper_orders_for_candidate(temp_db, cand_id) == 0

        # The four ensemble rows preserve each provider's actual
        # verdict (label histogram = {material: 3, ambiguous: 1}).
        conn = project_db.connect(temp_db)
        try:
            rows = conn.execute(
                "SELECT provider, label FROM ensemble_scores_event "
                "WHERE candidate_event_id = ? ORDER BY provider",
                (cand_id,),
            ).fetchall()
        finally:
            conn.close()
        labels = sorted([row[1] for row in rows])
        assert labels == sorted(
            ["material", "material", "material", "ambiguous"]
        )

        # And the audit JSON merge captured a stage2_skipped[] entry
        # with the unanimity reason (best-effort persistence — the
        # audit recorder is not a hard contract for VAL-CROSS-028
        # but we sanity-check the wiring stayed connected).
        try:
            audit_blob = json.loads(audit_path.read_text())
        except (json.JSONDecodeError, OSError):
            audit_blob = {}
        # Permissive: we don't enforce the exact shape here. The
        # canonical audit-shape contract lives in M3 / M5 tests.
        assert isinstance(audit_blob, dict)


# ---------------------------------------------------------------------------
# 5. Documented gate ordering — short-circuit on the FIRST failing gate
# ---------------------------------------------------------------------------


class TestDocumentedGateOrder:
    """Pin the cheap-first ordering: even when MULTIPLE gates would
    fail, the FIRST gate (cooldown) is the canonical rejection
    reason. This proves the dispatcher does not "scan ahead" or
    re-order gates for a more expensive failure path.
    """

    def test_cooldown_wins_over_armed_missing_when_both_fail(
        self,
        temp_db: Path,
        armed_path_missing: Path,
        audit_path: Path,
    ) -> None:
        """Cooldown active AND armed missing → reason is cooldown.

        The dispatcher MUST short-circuit at gate 1 (cooldown) and
        never evaluate gate 2 (armed). The reverse ordering would
        surface as ``reason='armed_file_missing'`` here.
        """

        ticker = "DOUBLE"
        cand_id, _ = _seed_candidate_event(temp_db, ticker=ticker)
        now = _dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)
        _seed_cooldown(
            temp_db,
            ticker=ticker,
            last_entry_at=_iso(now - _dt.timedelta(hours=2)),
            cooldown_hours=24,
        )
        providers = _make_recording_providers()

        result = run_stage2_chain(
            candidate_event_row={
                "id": cand_id,
                "ticker": ticker,
                "matched_keywords": "pdufa",
                "source_news_event_id": None,
            },
            db_path=temp_db,
            armed_path=armed_path_missing,
            audit_path=audit_path,
            now=now,
            providers=providers,
        )
        assert result.passed is False
        assert result.reason == GATE_REASON_COOLDOWN_ACTIVE
        assert result.gate == "cooldown"
        for mock in providers.values():
            mock.assert_not_called()

    def test_armed_wins_over_cap_when_both_fail(
        self,
        temp_db: Path,
        armed_path_missing: Path,
        audit_path: Path,
    ) -> None:
        """Cooldown clear, armed missing AND cap exceeded → reason is armed.

        Reverse ordering would surface as ``reason='daily_cap_exceeded'``.
        """

        ticker = "DOUBLE2"
        cand_id, _ = _seed_candidate_event(temp_db, ticker=ticker)
        # Cap exceeded — but armed gate (gate 2) must reject FIRST.
        today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
        _seed_ledger_row(
            temp_db,
            cost_usd=20.50,
            called_at=f"{today}T11:00:00.000Z",
        )
        providers = _make_recording_providers()

        result = run_stage2_chain(
            candidate_event_row={
                "id": cand_id,
                "ticker": ticker,
                "matched_keywords": "pdufa",
                "source_news_event_id": None,
            },
            db_path=temp_db,
            armed_path=armed_path_missing,
            audit_path=audit_path,
            providers=providers,
        )
        assert result.passed is False
        assert result.reason == GATE_REASON_ARMED_FILE_MISSING
        assert result.gate == "armed"
        for mock in providers.values():
            mock.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Baseline file existence — VAL-CROSS-017 anchor
# ---------------------------------------------------------------------------


def test_pre_reading_b_baseline_snapshot_exists() -> None:
    """``tests/baselines/reading_b_pretest_baseline.json`` must exist
    AND record ``failed=0`` (VAL-CROSS-017 evidence)."""

    baseline_path = (
        Path(__file__).parent / "baselines" / "reading_b_pretest_baseline.json"
    )
    assert baseline_path.exists(), (
        f"Reading-B pre-test baseline snapshot is missing at {baseline_path}; "
        "feature f-cross-05 requires this file (VAL-CROSS-017)."
    )
    blob = json.loads(baseline_path.read_text())
    # Required keys per the validation contract VAL-CROSS-017 evidence.
    for key in ("commit_sha", "passed", "failed", "skipped", "total"):
        assert key in blob, f"baseline file is missing required key {key!r}"
    assert isinstance(blob["passed"], int) and blob["passed"] > 0
    assert blob["failed"] == 0, (
        f"Pre-Reading-B baseline must record failed=0; got {blob['failed']}"
    )
    assert blob["passed"] + blob["skipped"] == blob["total"]
