"""End-to-end Rejection-paths regression test (feature ``f-m5-03-rejection-paths``).

Walks each Reading-B Stage-2 gate-rejection path on a synthetic
Russell-2000 biotech ticker and verifies the documented invariants:

* **Unanimity rejection (3/4 mixed labels — VAL-M5-014..016)** — the
  ensemble fan-out runs and writes 4 ``ensemble_scores_event`` rows
  for forensic review, but the post-fan-out unanimity gate rejects
  with ``reason='unanimity_failed'``. ZERO ``paper_orders`` rows are
  inserted; the broker is never contacted.
* **Probability rejection (avg < 0.75 — VAL-M5-017..019)** —
  unanimity passes (4/4 ``material``), but the arithmetic mean of
  the four probabilities (0.6, 0.65, 0.7, 0.7 → 0.6625) falls below
  ``STAGE2_PROBABILITY_THRESHOLD=0.75``. The probability gate rejects
  with ``reason='probability_below_threshold'``; ZERO
  ``paper_orders`` rows; ZERO ``ticker_cooldown`` rows for the
  ticker (cooldown only advances on successful entry per
  VAL-M3-047).
* **Armed-file rejection (.armed missing — VAL-M5-020..022)** — the
  cheap-first ``armed`` gate rejects BEFORE the LLM fan-out is
  dispatched, so ZERO ``llm_cost_ledger`` rows are written. ZERO
  ``paper_orders`` rows; the broker is never contacted.
* **Cooldown rejection (active cooldown — VAL-M5-023..025)** — a
  pre-seeded ``ticker_cooldown`` row blocks the candidate at the
  cheap-first cooldown gate. ZERO ``llm_cost_ledger`` rows; ZERO
  ``paper_orders`` rows; the broker is never contacted; the audit
  payload records the remaining-seconds value for ops dashboards.
* **Cap rejection (Stage-2 daily $ cap hit — VAL-M5-026..027)** —
  pre-seeded ``llm_cost_ledger`` rows summing ≥ $20 (with
  ``purpose='stage2_event_scoring'`` and today's UTC date) trip the
  cap gate. ZERO new ``llm_cost_ledger`` rows; ZERO ``paper_orders``
  rows; the broker is never contacted; the audit payload records
  the snapshotted ``today_total_usd``.

Each gate rejection writes a row into ``news_match_log`` with
``matched=0`` and ``reason=<canonical>``. Forensic metadata that
the contract Evidence references via column-form (``avg_probability``,
``cooldown_remaining_seconds``, ``today_total_usd``) is persisted on
``state/audit_latest.json`` per the existing :func:`record_stage2_skip`
audit-merge convention — the news_match_log row + audit JSON
together form the equivalent persistence surface.

Hermeticity
-----------

The test never touches the real Alpaca paper API or any LLM
endpoint. All four ensemble providers are stubbed with
deterministic callables; the broker is a duck-typed
:class:`_FakeAlpacaClient`. Every rejection scenario gets a fresh
``tmp_path``-rooted SQLite db (schema v10) so cross-scenario state
is impossible.

The test is decorated with ``@pytest.mark.e2e`` so it is selected
by ``pytest -m e2e``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

import pytest

from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.exec.stage2_dispatcher import (
    Stage2ChainResult,
    run_stage2_chain,
)
from biotech_sniper.llm.ensemble import ALL_PROVIDERS
from biotech_sniper.llm.stage2_gates import (
    GATE_REASON_ARMED_FILE_MISSING,
    GATE_REASON_COOLDOWN_ACTIVE,
    GATE_REASON_DAILY_CAP_EXCEEDED,
    GATE_REASON_PROBABILITY_BELOW_THRESHOLD,
    GATE_REASON_UNANIMITY_FAILED,
    STAGE2_LEDGER_PURPOSE,
)
from biotech_sniper.migrations.runner import run as run_migrations_runner
from biotech_sniper.news_daemon.emit import run_one_poll_cycle


pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Synthetic fixture data
# ---------------------------------------------------------------------------

_TICKER: str = "MRNS"
_HEADLINE: str = "MRNS phase 3 readout: primary endpoint announced"
_SOURCE: str = "test_rejection_paths"
_PUBLISHED_AT: str = "2026-04-30T14:00:00.000Z"
_STOCK_PRICE: float = 25.0


# ---------------------------------------------------------------------------
# Fake Alpaca client double — never expected to be invoked on rejection paths.
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Duck-typed Alpaca double that records (and rejects) any submit."""

    def __init__(self, *, base_url: str = PAPER_BASE_URL) -> None:
        self.base_url = base_url
        self.submit_calls: list[Any] = []
        self.get_order_calls: list[str] = []
        self.get_positions_calls: int = 0
        self.get_latest_trade_calls: list[str] = []

    def get_positions(self) -> list[dict[str, Any]]:
        self.get_positions_calls += 1
        return []

    def submit_order(self, order_request: Any) -> dict[str, Any]:
        self.submit_calls.append(order_request)
        raise AssertionError(
            "_FakeAlpacaClient.submit_order MUST NOT be called on a "
            "rejection-path test"
        )

    def get_order(self, order_id: str) -> dict[str, Any]:
        self.get_order_calls.append(order_id)
        raise AssertionError(
            "_FakeAlpacaClient.get_order MUST NOT be called on a "
            "rejection-path test"
        )

    def get_latest_trade(self, ticker: str) -> Optional[float]:
        self.get_latest_trade_calls.append(ticker)
        return _STOCK_PRICE


# ---------------------------------------------------------------------------
# Stub provider tracker — counts per-provider invocations so we can prove
# the cheap-first gates short-circuit before the LLM fan-out fires.
# ---------------------------------------------------------------------------


class _StubProviderTracker:
    """Tracks provider invocations + emits configurable per-provider payloads."""

    def __init__(self) -> None:
        self.call_counts: dict[str, int] = {p: 0 for p in ALL_PROVIDERS}

    def make_uniform_providers(
        self,
        *,
        label: str = "material",
        direction: str = "bullish",
        probability: float = 0.85,
    ) -> dict[str, Any]:
        """Return a `{provider_name: callable}` mapping with identical payloads."""

        def _factory(provider_name: str):
            def _stub(_row: Any, *, name: str = provider_name) -> dict[str, Any]:
                self.call_counts[name] += 1
                return {
                    "label": label,
                    "probability": probability,
                    "direction": direction,
                    "rationale": f"{name}-stub",
                    "citations": [],
                    "latency_ms": 10,
                    "cost_usd": 0.001,
                }

            return _stub

        return {p: _factory(p) for p in ALL_PROVIDERS}

    def make_per_provider_providers(
        self,
        per_provider: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Return `{provider_name: callable}` where each provider returns
        the dict in ``per_provider[name]``.
        """

        def _factory(provider_name: str):
            payload = dict(per_provider[provider_name])

            def _stub(_row: Any, *, name: str = provider_name) -> dict[str, Any]:
                self.call_counts[name] += 1
                return dict(payload)

            return _stub

        return {p: _factory(p) for p in ALL_PROVIDERS}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Bring a fresh sqlite db up to schema v11 (Reading-B foundations
    + f-misc-09 ``news_match_log`` forensic-column extension).
    """
    db = tmp_path / "alpha_sniper_e2e.db"
    run_migrations_runner(db, target_version=11, take_backup_first=False)
    return db


@pytest.fixture
def armed_path(tmp_path: Path) -> Path:
    """Create a dummy ``.armed`` marker so the armed gate passes."""
    p = tmp_path / ".armed"
    p.write_text("e2e-rejection", encoding="utf-8")
    return p


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    """Path used for the ``audit_latest.json`` Reading-B summary write."""
    return tmp_path / "state" / "audit_latest.json"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _seed_synthetic_news_event(db_path: Path) -> int:
    """Insert one synthetic news_events row and return its id."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            """
            INSERT INTO news_events (
                ticker, source, published_at, title, url, raw_payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _TICKER,
                _SOURCE,
                _PUBLISHED_AT,
                _HEADLINE,
                "https://example.com/mrns-readout",
                _HEADLINE,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _candidate_event_row(db_path: Path, news_event_id: int) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT id, ticker, source_news_event_id, matched_keywords,
                   calendar_match, emitted_at, dedup_key
            FROM candidate_events
            WHERE source_news_event_id = ?
            """,
            (news_event_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "candidate_events row missing"
    return dict(row)


def _count(db_path: Path, sql: str, params: tuple = ()) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    finally:
        conn.close()


def _emit_candidate(db_path: Path) -> dict[str, Any]:
    """Seed a news_events row, run Stage-1 once, return the candidate row."""
    news_event_id = _seed_synthetic_news_event(db_path)
    assert news_event_id > 0
    scanned, inserted = run_one_poll_cycle(
        str(db_path), polled_tickers=[_TICKER], after_id=0,
    )
    assert scanned == 1, "Stage-1 should scan the seed row"
    assert inserted == 1, "Stage-1 should insert one candidate"
    return _candidate_event_row(db_path, news_event_id)


def _news_match_log_rows(
    db_path: Path, ticker: str = _TICKER
) -> list[dict[str, Any]]:
    """Return ``news_match_log`` rows for ``ticker``, including v11 forensics.

    f-misc-09 extended the v10 skeleton with five forensic columns
    (``candidate_event_id``, ``gate_outcome``, ``avg_probability``,
    ``cooldown_remaining_seconds``, ``today_total_usd``). The
    rejection-path tests below assert each gate populates the
    column relevant to its decision.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT ticker, news_event_id, matched, reason, logged_at, "
            "candidate_event_id, gate_outcome, avg_probability, "
            "cooldown_remaining_seconds, today_total_usd "
            "FROM news_match_log WHERE ticker = ? ORDER BY id ASC",
            (ticker,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _audit_payload(audit_path: Path) -> dict[str, Any]:
    if not audit_path.is_file():
        return {}
    return json.loads(audit_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Test 1 — Unanimity rejection (3/4 mixed labels)
# ---------------------------------------------------------------------------


def test_unanimity_rejection_writes_audit_no_paper_orders(
    db_path: Path,
    armed_path: Path,
    audit_path: Path,
) -> None:
    """3/4 ``material`` + 1 ``non_material`` → unanimity gate rejects.

    Walks VAL-M5-014 / VAL-M5-015 / VAL-M5-016:

    * ZERO new ``paper_orders`` rows; broker never contacted.
    * All 4 ``ensemble_scores_event`` rows persisted (audit, even on
      rejection — supports retrospective debugging).
    * ``news_match_log`` row exists with ``reason='unanimity_failed'``.
    """
    candidate = _emit_candidate(db_path)

    tracker = _StubProviderTracker()
    # 3 material + 1 non_material — unanimity gate fails AT n_material<4.
    per_provider = {
        "xai": {
            "label": "material",
            "probability": 0.85,
            "direction": "bullish",
            "rationale": "xai-stub",
            "citations": [],
            "latency_ms": 10,
            "cost_usd": 0.001,
        },
        "anthropic": {
            "label": "material",
            "probability": 0.85,
            "direction": "bullish",
            "rationale": "anthropic-stub",
            "citations": [],
            "latency_ms": 10,
            "cost_usd": 0.001,
        },
        "gemini": {
            "label": "material",
            "probability": 0.85,
            "direction": "bullish",
            "rationale": "gemini-stub",
            "citations": [],
            "latency_ms": 10,
            "cost_usd": 0.001,
        },
        "perplexity": {
            "label": "non_material",
            "probability": 0.85,
            "direction": "bullish",
            "rationale": "perplexity-stub",
            "citations": [],
            "latency_ms": 10,
            "cost_usd": 0.001,
        },
    }
    providers = tracker.make_per_provider_providers(per_provider)

    chain_result: Stage2ChainResult = run_stage2_chain(
        candidate_event_row=candidate,
        db_path=db_path,
        armed_path=armed_path,
        audit_path=audit_path,
        providers=providers,
    )
    assert chain_result.passed is False
    assert chain_result.reason == GATE_REASON_UNANIMITY_FAILED
    assert chain_result.gate == "unanimity"
    # Ensemble result is still present so the test can audit the
    # rejected ensemble post-hoc.
    assert chain_result.ensemble_result is not None

    # All 4 ensemble_scores_event rows persisted (audit even on rejection).
    rows = _count(
        db_path,
        "SELECT COUNT(*) FROM ensemble_scores_event WHERE candidate_event_id = ?",
        (candidate["id"],),
    )
    assert rows == 4, "all 4 ensemble_scores_event rows must persist"

    # ZERO paper_orders rows for the candidate.
    assert (
        _count(
            db_path,
            "SELECT COUNT(*) FROM paper_orders WHERE event = 'news_event_entry'",
        )
        == 0
    )

    # ZERO ticker_cooldown rows for the candidate's ticker.
    assert (
        _count(
            db_path,
            "SELECT COUNT(*) FROM ticker_cooldown WHERE ticker = ?",
            (_TICKER,),
        )
        == 0
    )

    # news_match_log row exists with reason='unanimity_failed'.
    nml_rows = _news_match_log_rows(db_path)
    assert len(nml_rows) == 1, nml_rows
    nml = nml_rows[0]
    assert nml["ticker"] == _TICKER
    assert nml["matched"] == 0
    assert nml["reason"] == GATE_REASON_UNANIMITY_FAILED
    # f-misc-09 v11 forensic-columns invariant: every rejection row
    # carries ``gate_outcome='rejected'`` and the ``candidate_event_id``
    # that triggered the gate. Unanimity rejection still computes
    # the post-fanout mean probability (0.85 in this fixture — all
    # four providers returned probability=0.85 even though one
    # disagreed on label), so ``avg_probability`` is recorded for
    # forensic review. ``cooldown_remaining_seconds`` /
    # ``today_total_usd`` remain NULL because those values aren't
    # produced by the unanimity gate.
    assert nml["gate_outcome"] == "rejected"
    assert str(nml["candidate_event_id"]) == str(candidate["id"])
    assert nml["avg_probability"] is not None
    assert abs(float(nml["avg_probability"]) - 0.85) < 1e-6
    assert nml["cooldown_remaining_seconds"] is None
    assert nml["today_total_usd"] is None


# ---------------------------------------------------------------------------
# Test 2 — Probability rejection (avg < 0.75)
# ---------------------------------------------------------------------------


def test_probability_rejection_writes_audit_no_paper_orders(
    db_path: Path,
    armed_path: Path,
    audit_path: Path,
) -> None:
    """4/4 ``material`` + avg(0.6, 0.65, 0.7, 0.7) = 0.6625 < 0.75.

    Walks VAL-M5-017 / VAL-M5-018 / VAL-M5-019:

    * ZERO new ``paper_orders`` rows; broker never contacted.
    * ``news_match_log`` row exists with
      ``reason='probability_below_threshold'``.
    * ZERO ``ticker_cooldown`` rows for the ticker (cooldown only
      advances on successful entry per VAL-M3-047).
    * audit_latest.json payload records the rejected ``avg_probability``
      so ops dashboards can surface the close-to-pass score.
    """
    candidate = _emit_candidate(db_path)

    tracker = _StubProviderTracker()
    probs = {
        "xai": 0.6,
        "anthropic": 0.65,
        "gemini": 0.7,
        "perplexity": 0.7,
    }
    per_provider = {
        name: {
            "label": "material",
            "probability": probs[name],
            "direction": "bullish",
            "rationale": f"{name}-stub",
            "citations": [],
            "latency_ms": 10,
            "cost_usd": 0.001,
        }
        for name in ALL_PROVIDERS
    }
    providers = tracker.make_per_provider_providers(per_provider)

    chain_result: Stage2ChainResult = run_stage2_chain(
        candidate_event_row=candidate,
        db_path=db_path,
        armed_path=armed_path,
        audit_path=audit_path,
        providers=providers,
    )
    assert chain_result.passed is False
    assert chain_result.reason == GATE_REASON_PROBABILITY_BELOW_THRESHOLD
    assert chain_result.gate == "probability"
    assert chain_result.ensemble_result is not None

    # All 4 ensemble_scores_event rows persisted (audit).
    assert (
        _count(
            db_path,
            "SELECT COUNT(*) FROM ensemble_scores_event "
            "WHERE candidate_event_id = ?",
            (candidate["id"],),
        )
        == 4
    )

    # ZERO paper_orders rows.
    assert (
        _count(
            db_path,
            "SELECT COUNT(*) FROM paper_orders WHERE event = 'news_event_entry'",
        )
        == 0
    )

    # VAL-M5-019 — ZERO ticker_cooldown rows for the ticker.
    assert (
        _count(
            db_path,
            "SELECT COUNT(*) FROM ticker_cooldown WHERE ticker = ?",
            (_TICKER,),
        )
        == 0
    )

    # news_match_log row exists with reason='probability_below_threshold'.
    nml_rows = _news_match_log_rows(db_path)
    assert len(nml_rows) == 1, nml_rows
    nml = nml_rows[0]
    assert nml["ticker"] == _TICKER
    assert nml["matched"] == 0
    assert nml["reason"] == GATE_REASON_PROBABILITY_BELOW_THRESHOLD
    # f-misc-09 v11 forensic-columns invariant: probability rejection
    # populates ``avg_probability`` on the SQL row alongside the
    # parallel-surface audit JSON entry. avg(0.6, 0.65, 0.7, 0.7) =
    # 0.6625; allow ±1e-6 for float repr drift.
    assert nml["gate_outcome"] == "rejected"
    assert str(nml["candidate_event_id"]) == str(candidate["id"])
    assert nml["avg_probability"] is not None
    assert abs(float(nml["avg_probability"]) - 0.6625) < 1e-6
    assert nml["cooldown_remaining_seconds"] is None
    assert nml["today_total_usd"] is None

    # audit_latest.json records the avg_probability.
    payload = _audit_payload(audit_path)
    skipped = payload.get("stage2_skipped") or []
    matching = [
        s for s in skipped
        if isinstance(s, dict)
        and s.get("reason") == GATE_REASON_PROBABILITY_BELOW_THRESHOLD
    ]
    assert matching, payload
    avg_p = matching[0].get("last_avg_probability")
    assert avg_p is not None
    assert abs(float(avg_p) - 0.6625) < 1e-6, avg_p


# ---------------------------------------------------------------------------
# Test 3 — Armed-file rejection (.armed missing → cheap-first short-circuit)
# ---------------------------------------------------------------------------


def test_armed_rejection_zero_llm_cost_zero_paper_orders(
    db_path: Path,
    tmp_path: Path,
    audit_path: Path,
) -> None:
    """``.armed`` absent → armed gate rejects BEFORE any LLM dispatch.

    Walks VAL-M5-020 / VAL-M5-021 / VAL-M5-022:

    * ZERO new ``llm_cost_ledger`` rows (cheap-first ordering).
    * ZERO ``paper_orders`` rows; broker never contacted.
    * ``news_match_log`` row exists with ``reason='armed_file_missing'``.
    """
    candidate = _emit_candidate(db_path)

    # Use a non-existent path for `.armed`.
    missing_armed = tmp_path / "no-armed-here"
    assert not missing_armed.exists()

    tracker = _StubProviderTracker()
    providers = tracker.make_uniform_providers()

    cost_before = _count(db_path, "SELECT COUNT(*) FROM llm_cost_ledger")

    chain_result: Stage2ChainResult = run_stage2_chain(
        candidate_event_row=candidate,
        db_path=db_path,
        armed_path=missing_armed,
        audit_path=audit_path,
        providers=providers,
    )
    assert chain_result.passed is False
    assert chain_result.reason == GATE_REASON_ARMED_FILE_MISSING
    assert chain_result.gate == "armed"
    assert chain_result.ensemble_result is None

    # ZERO new llm_cost_ledger rows (cheap-first).
    cost_after = _count(db_path, "SELECT COUNT(*) FROM llm_cost_ledger")
    assert cost_after == cost_before == 0

    # ZERO providers invoked.
    assert all(tracker.call_counts[p] == 0 for p in ALL_PROVIDERS)

    # ZERO ensemble_scores_event rows.
    assert (
        _count(db_path, "SELECT COUNT(*) FROM ensemble_scores_event")
        == 0
    )

    # ZERO paper_orders rows.
    assert (
        _count(
            db_path,
            "SELECT COUNT(*) FROM paper_orders WHERE event = 'news_event_entry'",
        )
        == 0
    )

    # news_match_log row exists with reason='armed_file_missing'.
    nml_rows = _news_match_log_rows(db_path)
    assert len(nml_rows) == 1
    nml = nml_rows[0]
    assert nml["reason"] == GATE_REASON_ARMED_FILE_MISSING
    # f-misc-09 v11 forensic-columns invariant: armed-file rejection
    # is cheap-first (pre-fanout) — no avg_probability, no cooldown
    # value, no cap value to record. Only ``gate_outcome`` and
    # ``candidate_event_id`` are populated.
    assert nml["gate_outcome"] == "rejected"
    assert str(nml["candidate_event_id"]) == str(candidate["id"])
    assert nml["avg_probability"] is None
    assert nml["cooldown_remaining_seconds"] is None
    assert nml["today_total_usd"] is None


# ---------------------------------------------------------------------------
# Test 4 — Cooldown rejection (active cooldown → cheap-first short-circuit)
# ---------------------------------------------------------------------------


def test_cooldown_rejection_zero_llm_cost_records_remaining_seconds(
    db_path: Path,
    armed_path: Path,
    audit_path: Path,
) -> None:
    """Pre-seeded ``ticker_cooldown`` row → cooldown gate rejects.

    Walks VAL-M5-023 / VAL-M5-024 / VAL-M5-025:

    * ZERO new ``llm_cost_ledger`` rows (cheap-first ordering).
    * ZERO ``paper_orders`` rows; broker never contacted.
    * ``news_match_log`` row with ``reason='cooldown_active'``.
    * audit_latest.json payload records ``remaining_seconds`` ≥ 0.
    """
    candidate = _emit_candidate(db_path)

    # Pre-seed a fresh cooldown row 1 hour ago — well within the 24h window.
    import datetime as _dt

    one_hour_ago = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)
    )
    last_entry_at = (
        one_hour_ago.strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{one_hour_ago.microsecond // 1000:03d}Z"
    )
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO ticker_cooldown
                    (ticker, last_entry_at, last_event_id, cooldown_hours)
                VALUES (?, ?, NULL, 24)
                """,
                (_TICKER, last_entry_at),
            )
    finally:
        conn.close()

    tracker = _StubProviderTracker()
    providers = tracker.make_uniform_providers()

    cost_before = _count(db_path, "SELECT COUNT(*) FROM llm_cost_ledger")

    chain_result: Stage2ChainResult = run_stage2_chain(
        candidate_event_row=candidate,
        db_path=db_path,
        armed_path=armed_path,
        audit_path=audit_path,
        providers=providers,
    )
    assert chain_result.passed is False
    assert chain_result.reason == GATE_REASON_COOLDOWN_ACTIVE
    assert chain_result.gate == "cooldown"
    assert chain_result.ensemble_result is None

    # ZERO new llm_cost_ledger rows.
    cost_after = _count(db_path, "SELECT COUNT(*) FROM llm_cost_ledger")
    assert cost_after == cost_before == 0

    # ZERO providers invoked.
    assert all(tracker.call_counts[p] == 0 for p in ALL_PROVIDERS)

    # ZERO ensemble_scores_event rows.
    assert (
        _count(db_path, "SELECT COUNT(*) FROM ensemble_scores_event")
        == 0
    )

    # ZERO paper_orders rows.
    assert (
        _count(
            db_path,
            "SELECT COUNT(*) FROM paper_orders WHERE event = 'news_event_entry'",
        )
        == 0
    )

    # news_match_log row exists with reason='cooldown_active'.
    nml_rows = _news_match_log_rows(db_path)
    assert len(nml_rows) == 1
    nml = nml_rows[0]
    assert nml["reason"] == GATE_REASON_COOLDOWN_ACTIVE
    # f-misc-09 v11 forensic-columns invariant: cooldown rejection
    # populates ``cooldown_remaining_seconds`` on the SQL row
    # alongside the parallel audit JSON entry. ~23h remaining of the
    # 24h window after the seeded 1h cooldown — allow ±1 minute slop.
    assert nml["gate_outcome"] == "rejected"
    assert str(nml["candidate_event_id"]) == str(candidate["id"])
    assert nml["avg_probability"] is None
    assert nml["cooldown_remaining_seconds"] is not None
    sql_remaining = float(nml["cooldown_remaining_seconds"])
    assert sql_remaining >= 23 * 3600 - 60
    assert nml["today_total_usd"] is None

    # audit_latest.json records remaining_seconds.
    payload = _audit_payload(audit_path)
    skipped = payload.get("stage2_skipped") or []
    matching = [
        s for s in skipped
        if isinstance(s, dict) and s.get("reason") == GATE_REASON_COOLDOWN_ACTIVE
    ]
    assert matching, payload
    remaining = matching[0].get("last_cooldown_remaining_seconds")
    assert remaining is not None
    assert int(remaining) >= 0
    # 1 hour elapsed of 24h window → ~23h remaining; allow ±1 minute slop.
    assert int(remaining) >= 23 * 3600 - 60


# ---------------------------------------------------------------------------
# Test 5 — Cap rejection (Stage-2 daily $ cap hit → cheap-first short-circuit)
# ---------------------------------------------------------------------------


def test_cap_rejection_zero_llm_cost_records_today_total(
    db_path: Path,
    armed_path: Path,
    audit_path: Path,
) -> None:
    """Pre-seeded ledger sum ≥ $20 → cap gate rejects.

    Walks VAL-M5-026 / VAL-M5-027:

    * ZERO new ``llm_cost_ledger`` rows (cheap-first ordering).
    * ZERO ``paper_orders`` rows; broker never contacted.
    * ``news_match_log`` row with ``reason='daily_cap_exceeded'``.
    * audit_latest.json payload records ``today_total_usd`` ≥ 20.0.
    """
    candidate = _emit_candidate(db_path)

    # Pre-seed today's Stage-2 ledger to $20 exactly so projection
    # ($20 + $0.50 = $20.50 > $20) trips the cap.
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO llm_cost_ledger (
                    provider, model_id, purpose, cost_usd, called_at
                ) VALUES (
                    'perplexity', 'sonar', ?, 20.00,
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                )
                """,
                (STAGE2_LEDGER_PURPOSE,),
            )
    finally:
        conn.close()

    tracker = _StubProviderTracker()
    providers = tracker.make_uniform_providers()

    cost_before = _count(db_path, "SELECT COUNT(*) FROM llm_cost_ledger")
    assert cost_before == 1, "test seed should leave exactly one ledger row"

    chain_result: Stage2ChainResult = run_stage2_chain(
        candidate_event_row=candidate,
        db_path=db_path,
        armed_path=armed_path,
        audit_path=audit_path,
        providers=providers,
    )
    assert chain_result.passed is False
    assert chain_result.reason == GATE_REASON_DAILY_CAP_EXCEEDED
    assert chain_result.gate == "cap"
    assert chain_result.ensemble_result is None

    # ZERO new llm_cost_ledger rows.
    cost_after = _count(db_path, "SELECT COUNT(*) FROM llm_cost_ledger")
    assert cost_after == cost_before == 1

    # ZERO providers invoked.
    assert all(tracker.call_counts[p] == 0 for p in ALL_PROVIDERS)

    # ZERO paper_orders rows.
    assert (
        _count(
            db_path,
            "SELECT COUNT(*) FROM paper_orders WHERE event = 'news_event_entry'",
        )
        == 0
    )

    # news_match_log row exists with reason='daily_cap_exceeded'.
    nml_rows = _news_match_log_rows(db_path)
    assert len(nml_rows) == 1
    nml = nml_rows[0]
    assert nml["reason"] == GATE_REASON_DAILY_CAP_EXCEEDED
    # f-misc-09 v11 forensic-columns invariant: cap rejection
    # populates ``today_total_usd`` on the SQL row alongside the
    # parallel audit JSON entry. Pre-seeded ledger sum is $20.00.
    assert nml["gate_outcome"] == "rejected"
    assert str(nml["candidate_event_id"]) == str(candidate["id"])
    assert nml["avg_probability"] is None
    assert nml["cooldown_remaining_seconds"] is None
    assert nml["today_total_usd"] is not None
    assert float(nml["today_total_usd"]) >= 20.0

    # audit_latest.json records today_total_usd.
    payload = _audit_payload(audit_path)
    skipped = payload.get("stage2_skipped") or []
    matching = [
        s for s in skipped
        if isinstance(s, dict) and s.get("reason") == GATE_REASON_DAILY_CAP_EXCEEDED
    ]
    assert matching, payload
    today_total = matching[0].get("last_total_usd")
    assert today_total is not None
    assert float(today_total) >= 20.0


# ---------------------------------------------------------------------------
# Smoke import — keeps `pytest --collect-only` green.
# ---------------------------------------------------------------------------


def test_module_imports_cleanly() -> None:
    """Defensive smoke test: every named import above resolves at module load."""
    import sys
    assert "tests.e2e.test_rejection_paths" in sys.modules or __name__.endswith(
        "test_rejection_paths"
    )
