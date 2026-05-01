"""End-to-end gate-order reproducibility test
(feature ``f-m5-04-cheap-first-side-effect-invariants``).

Walks VAL-M5-047: ``Same input candidate evaluated 10 times produces
IDENTICAL ordered gate-trace each time. Gate that fires rejection is
always at the same position (no race or non-deterministic dict
iteration).``

For each of FIVE rejection scenarios, the test seeds the failing
condition once, captures a "gate-trace" tuple
``(gate, reason, passed)`` from :func:`run_stage2_chain`, and
asserts that the tuple is byte-identical across 10 sequential runs.
Because the test does not vary any input between runs, any
divergence would surface a non-deterministic dict-iteration order
or a race in the gate-evaluation chain.

The five scenarios — matching the M5.GATE_ORDER section of the
contract — are:

* ``cooldown`` (cheap-first; ``ensemble_result is None``)
* ``armed_missing`` (cheap-first; ``ensemble_result is None``)
* ``cap_hit`` (cheap-first; ``ensemble_result is None``)
* ``unanimity_split`` (post-fanout; ``ensemble_result is not None``)
* ``probability_below_threshold`` (post-fanout;
  ``ensemble_result is not None``)

Hermeticity
-----------

The test never touches the real Alpaca paper API or any LLM
endpoint. All four ensemble providers are stubbed with
deterministic callables; no broker is plumbed (the chain itself
performs no broker I/O on the rejection paths).

The test is decorated with ``@pytest.mark.e2e`` so it is selected
by ``pytest -m e2e``.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path
from typing import Any, Optional

import pytest

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
_SOURCE: str = "test_gate_order_reproducibility"
_PUBLISHED_AT: str = "2026-04-30T14:00:00.000Z"

#: Number of sequential repetitions per scenario. The contract
#: prescribes 10 (VAL-M5-047 / feature brief).
N_REPS: int = 10


# ---------------------------------------------------------------------------
# Stub provider tracker — emits configurable per-provider payloads.
# ---------------------------------------------------------------------------


class _StubProviderTracker:
    """Tracks provider invocations + emits configurable payloads."""

    def __init__(self) -> None:
        self.call_counts: dict[str, int] = {p: 0 for p in ALL_PROVIDERS}

    def make_uniform_providers(
        self,
        *,
        label: str = "material",
        direction: str = "bullish",
        probability: float = 0.85,
    ) -> dict[str, Any]:
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
    """Bring a fresh sqlite db up to schema v10 (Reading-B foundations)."""
    db = tmp_path / "alpha_sniper_e2e.db"
    run_migrations_runner(db, target_version=11, take_backup_first=False)
    return db


@pytest.fixture
def armed_path(tmp_path: Path) -> Path:
    """Create a dummy ``.armed`` marker so the armed gate passes."""
    p = tmp_path / ".armed"
    p.write_text("e2e-gate-order", encoding="utf-8")
    return p


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    """Path used for the ``audit_latest.json`` Reading-B summary write."""
    return tmp_path / "state" / "audit_latest.json"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _seed_synthetic_news_event(db_path: Path) -> int:
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


def _emit_candidate(db_path: Path) -> dict[str, Any]:
    news_event_id = _seed_synthetic_news_event(db_path)
    assert news_event_id > 0
    scanned, inserted = run_one_poll_cycle(
        str(db_path), polled_tickers=[_TICKER], after_id=0,
    )
    assert scanned == 1, "Stage-1 should scan the seed row"
    assert inserted == 1, "Stage-1 should insert one candidate"
    return _candidate_event_row(db_path, news_event_id)


def _seed_active_cooldown(db_path: Path) -> None:
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


def _seed_cap_hit_ledger(db_path: Path) -> None:
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


# ---------------------------------------------------------------------------
# Gate-trace helper
# ---------------------------------------------------------------------------


def _trace(result: Stage2ChainResult) -> tuple:
    """Return a hashable, byte-comparable gate-trace tuple.

    The trace captures the four chain-result fields that the
    contract expects to be deterministic across runs:

    * ``passed`` (bool) — outcome of the chain
    * ``gate`` (str | None) — symbolic gate name that produced the
      rejection (``cooldown`` / ``armed`` / ``cap`` / ``unanimity``
      / ``probability``)
    * ``reason`` (str | None) — canonical short-circuit reason
    * ``ensemble_present`` (bool) — derived flag (``True`` when the
      fan-out ran; ``False`` when a cheap gate short-circuited).
      Captured as a derived bool rather than the full
      :class:`EnsembleEventResult` because the latter carries
      structurally-equal but identity-differing dataclass instances
      across runs and would defeat byte-identity equality.
    """
    return (
        bool(result.passed),
        result.gate,
        result.reason,
        result.ensemble_result is not None,
    )


# ---------------------------------------------------------------------------
# Per-scenario seeding helpers
# ---------------------------------------------------------------------------


def _build_split_per_provider() -> dict[str, dict[str, Any]]:
    """3 material + 1 non_material — unanimity gate fails."""
    base = {
        "label": "material",
        "probability": 0.85,
        "direction": "bullish",
        "rationale": "stub",
        "citations": [],
        "latency_ms": 10,
        "cost_usd": 0.001,
    }
    out = {p: dict(base) for p in ALL_PROVIDERS}
    out["perplexity"]["label"] = "non_material"
    return out


def _build_low_prob_per_provider() -> dict[str, dict[str, Any]]:
    """4/4 material; mean(0.6, 0.65, 0.7, 0.7) = 0.6625 < 0.75."""
    probs = {"xai": 0.6, "anthropic": 0.65, "gemini": 0.7, "perplexity": 0.7}
    return {
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


# ---------------------------------------------------------------------------
# Test 1 — Cooldown rejection: byte-identical gate-trace across 10 runs.
# ---------------------------------------------------------------------------


def test_cooldown_gate_trace_byte_identical_across_10_runs(
    db_path: Path,
    armed_path: Path,
    audit_path: Path,
) -> None:
    """VAL-M5-047 — cooldown scenario."""
    candidate = _emit_candidate(db_path)
    _seed_active_cooldown(db_path)

    tracker = _StubProviderTracker()
    providers = tracker.make_uniform_providers()

    traces: list[tuple] = []
    for _ in range(N_REPS):
        result = run_stage2_chain(
            candidate_event_row=candidate,
            db_path=db_path,
            armed_path=armed_path,
            audit_path=audit_path,
            providers=providers,
        )
        traces.append(_trace(result))

    # All N_REPS traces are identical to the first.
    first = traces[0]
    assert first == (False, "cooldown", GATE_REASON_COOLDOWN_ACTIVE, False), first
    assert all(t == first for t in traces), traces
    # Single distinct value across the full set proves no
    # divergence (a stronger statement than pairwise equality).
    assert len(set(traces)) == 1, set(traces)


# ---------------------------------------------------------------------------
# Test 2 — Armed-missing rejection: byte-identical gate-trace across 10 runs.
# ---------------------------------------------------------------------------


def test_armed_missing_gate_trace_byte_identical_across_10_runs(
    db_path: Path,
    tmp_path: Path,
    audit_path: Path,
) -> None:
    """VAL-M5-047 — armed-missing scenario."""
    candidate = _emit_candidate(db_path)
    missing_armed = tmp_path / "no-armed-here"
    assert not missing_armed.exists()

    tracker = _StubProviderTracker()
    providers = tracker.make_uniform_providers()

    traces: list[tuple] = []
    for _ in range(N_REPS):
        result = run_stage2_chain(
            candidate_event_row=candidate,
            db_path=db_path,
            armed_path=missing_armed,
            audit_path=audit_path,
            providers=providers,
        )
        traces.append(_trace(result))

    first = traces[0]
    assert first == (False, "armed", GATE_REASON_ARMED_FILE_MISSING, False), first
    assert all(t == first for t in traces), traces
    assert len(set(traces)) == 1, set(traces)


# ---------------------------------------------------------------------------
# Test 3 — Cap-hit rejection: byte-identical gate-trace across 10 runs.
# ---------------------------------------------------------------------------


def test_cap_hit_gate_trace_byte_identical_across_10_runs(
    db_path: Path,
    armed_path: Path,
    audit_path: Path,
) -> None:
    """VAL-M5-047 — cap-hit scenario."""
    candidate = _emit_candidate(db_path)
    _seed_cap_hit_ledger(db_path)

    tracker = _StubProviderTracker()
    providers = tracker.make_uniform_providers()

    traces: list[tuple] = []
    for _ in range(N_REPS):
        result = run_stage2_chain(
            candidate_event_row=candidate,
            db_path=db_path,
            armed_path=armed_path,
            audit_path=audit_path,
            providers=providers,
        )
        traces.append(_trace(result))

    first = traces[0]
    assert first == (False, "cap", GATE_REASON_DAILY_CAP_EXCEEDED, False), first
    assert all(t == first for t in traces), traces
    assert len(set(traces)) == 1, set(traces)


# ---------------------------------------------------------------------------
# Test 4 — Unanimity-split rejection: byte-identical gate-trace across 10 runs.
# ---------------------------------------------------------------------------


def test_unanimity_split_gate_trace_byte_identical_across_10_runs(
    db_path: Path,
    armed_path: Path,
    audit_path: Path,
) -> None:
    """VAL-M5-047 — unanimity-split (post-fanout) scenario."""
    candidate = _emit_candidate(db_path)

    tracker = _StubProviderTracker()
    providers = tracker.make_per_provider_providers(_build_split_per_provider())

    traces: list[tuple] = []
    for _ in range(N_REPS):
        result = run_stage2_chain(
            candidate_event_row=candidate,
            db_path=db_path,
            armed_path=armed_path,
            audit_path=audit_path,
            providers=providers,
        )
        traces.append(_trace(result))

    first = traces[0]
    assert first == (False, "unanimity", GATE_REASON_UNANIMITY_FAILED, True), first
    assert all(t == first for t in traces), traces
    assert len(set(traces)) == 1, set(traces)


# ---------------------------------------------------------------------------
# Test 5 — Probability-below-threshold: byte-identical gate-trace across 10 runs.
# ---------------------------------------------------------------------------


def test_probability_below_threshold_gate_trace_byte_identical_across_10_runs(
    db_path: Path,
    armed_path: Path,
    audit_path: Path,
) -> None:
    """VAL-M5-047 — probability-below-threshold (post-fanout) scenario."""
    candidate = _emit_candidate(db_path)

    tracker = _StubProviderTracker()
    providers = tracker.make_per_provider_providers(
        _build_low_prob_per_provider()
    )

    traces: list[tuple] = []
    for _ in range(N_REPS):
        result = run_stage2_chain(
            candidate_event_row=candidate,
            db_path=db_path,
            armed_path=armed_path,
            audit_path=audit_path,
            providers=providers,
        )
        traces.append(_trace(result))

    first = traces[0]
    assert first == (
        False,
        "probability",
        GATE_REASON_PROBABILITY_BELOW_THRESHOLD,
        True,
    ), first
    assert all(t == first for t in traces), traces
    assert len(set(traces)) == 1, set(traces)


# ---------------------------------------------------------------------------
# Test 6 — Combined byte-identity across all five scenarios in one shot.
# ---------------------------------------------------------------------------


def test_all_five_scenarios_each_produce_one_distinct_gate_trace(
    tmp_path: Path,
) -> None:
    """Cross-scenario sanity check.

    Each of the five rejection scenarios MUST produce a distinct
    ``(passed, gate, reason, ensemble_present)`` trace — five
    distinct tuples in total. Within each scenario, the trace is
    byte-identical across ``N_REPS=10`` runs; across scenarios the
    traces are pairwise distinct.

    Each scenario gets a fresh ``tmp_path``-rooted sqlite db so
    cross-scenario state cannot leak.
    """
    expected_traces = {
        "cooldown": (False, "cooldown", GATE_REASON_COOLDOWN_ACTIVE, False),
        "armed_missing": (
            False,
            "armed",
            GATE_REASON_ARMED_FILE_MISSING,
            False,
        ),
        "cap_hit": (False, "cap", GATE_REASON_DAILY_CAP_EXCEEDED, False),
        "unanimity_split": (
            False,
            "unanimity",
            GATE_REASON_UNANIMITY_FAILED,
            True,
        ),
        "probability_below_threshold": (
            False,
            "probability",
            GATE_REASON_PROBABILITY_BELOW_THRESHOLD,
            True,
        ),
    }
    # Five distinct expected traces.
    assert len(set(expected_traces.values())) == 5

    observed_traces: dict[str, set] = {}
    for scenario_id, expected in expected_traces.items():
        scenario_dir = tmp_path / scenario_id
        scenario_dir.mkdir()
        db = scenario_dir / "alpha_sniper.db"
        run_migrations_runner(db, target_version=11, take_backup_first=False)
        armed = scenario_dir / ".armed"
        armed.write_text("e2e-gate-order-combined", encoding="utf-8")
        audit = scenario_dir / "state" / "audit_latest.json"

        # Seed news event + candidate.
        candidate = _emit_candidate(db)

        # Per-scenario seeding + provider configuration.
        armed_for_run: Path
        if scenario_id == "cooldown":
            _seed_active_cooldown(db)
            providers = _StubProviderTracker().make_uniform_providers()
            armed_for_run = armed
        elif scenario_id == "armed_missing":
            armed_for_run = scenario_dir / "no-armed-here"
            assert not armed_for_run.exists()
            providers = _StubProviderTracker().make_uniform_providers()
        elif scenario_id == "cap_hit":
            _seed_cap_hit_ledger(db)
            providers = _StubProviderTracker().make_uniform_providers()
            armed_for_run = armed
        elif scenario_id == "unanimity_split":
            providers = _StubProviderTracker().make_per_provider_providers(
                _build_split_per_provider()
            )
            armed_for_run = armed
        elif scenario_id == "probability_below_threshold":
            providers = _StubProviderTracker().make_per_provider_providers(
                _build_low_prob_per_provider()
            )
            armed_for_run = armed
        else:
            pytest.fail(f"unknown scenario_id={scenario_id}")

        traces: list[tuple] = []
        for _ in range(N_REPS):
            result = run_stage2_chain(
                candidate_event_row=candidate,
                db_path=db,
                armed_path=armed_for_run,
                audit_path=audit,
                providers=providers,
            )
            traces.append(_trace(result))

        # Within-scenario reproducibility.
        assert len(set(traces)) == 1, (scenario_id, set(traces))
        assert traces[0] == expected, (scenario_id, traces[0], expected)
        observed_traces[scenario_id] = set(traces)

    # Cross-scenario distinctness — five distinct traces in total.
    flat = {t for s in observed_traces.values() for t in s}
    assert len(flat) == 5, flat


# ---------------------------------------------------------------------------
# Smoke import — keeps ``pytest --collect-only`` green.
# ---------------------------------------------------------------------------


def test_module_imports_cleanly() -> None:
    """Defensive smoke test: every named import above resolves at module load."""
    import sys
    assert (
        "tests.e2e.test_gate_order_reproducibility" in sys.modules
        or __name__.endswith("test_gate_order_reproducibility")
    )
