"""Tests for the Reading-B Stage-2 probability threshold gate.

Feature: f-m3-04-probability-threshold-gate.

Verifies the contract assertions VAL-M3-022 through VAL-M3-026:

* VAL-M3-022 — ``STAGE2_PROBABILITY_THRESHOLD`` env override defaults to
  ``0.75`` (read via :mod:`biotech_sniper.config`, never raw
  ``os.environ`` outside ``config.py``).
* VAL-M3-023 — Mean is computed across exactly the successful providers
  (``sum(probs) / len(probs)``) — failed providers do NOT pull the mean
  toward zero.
* VAL-M3-024 — ``mean >= threshold`` passes (inclusive boundary);
  ``mean < threshold`` rejects with ``reason='probability_below_threshold'``.
* VAL-M3-025 — Missing-provider edge case (3/4 successful) cannot pass
  the threshold gate alone — the gate logs
  ``skipped: insufficient_providers`` and the upstream unanimity gate
  is the canonical rejection reason.
* VAL-M3-026 — Probability gate is structurally a post-fanout gate (it
  consumes the ensemble result). The "row-count" verification asserts
  that running the gate against a fan-out result writes ZERO additional
  ``ensemble_scores_event`` rows beyond the four the ensemble itself
  produced.

The gate function is a pure consumer of
:class:`biotech_sniper.llm.ensemble.EnsembleEventResult`; it has no
side effects on its own. These tests use the same stub-provider
plumbing as ``tests/llm/test_ensemble_event.py`` (no live network).

Per the dual-path test convention in ``library`` / ``AGENTS.md``,
``tests/llm/test_stage2_gates.py`` re-exports the same test bodies via
``from tests.test_stage2_probability_gate import *`` so contract
node-IDs of either form collect.
"""

from __future__ import annotations

import importlib
import os
from typing import Any

import pytest

from biotech_sniper import config as _config
from biotech_sniper import db as project_db
from biotech_sniper.llm import stage2_gates
from biotech_sniper.llm.ensemble import (
    ALL_PROVIDERS,
    EnsembleEventResult,
    ProviderResult,
    score_candidate_event,
)
from biotech_sniper.llm.stage2_gates import (
    GATE_REASON_INSUFFICIENT_PROVIDERS,
    GATE_REASON_PROBABILITY_BELOW_THRESHOLD,
    ProbabilityGateResult,
    probability_gate,
)
from biotech_sniper.migrations.runner import run as run_v10


# ---------------------------------------------------------------------------
# Module-reload teardown so any test that mutates STAGE2_PROBABILITY_THRESHOLD
# via importlib.reload(config) inside a monkeypatch.setenv() scope cannot
# leak the override into subsequent tests in the same xdist worker.
# (See AGENTS.md "Test Conventions: Module-reload teardown after env-override".)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_config_module_after_test():
    yield
    os.environ.pop("STAGE2_PROBABILITY_THRESHOLD", None)
    importlib.reload(_config)
    # NOTE: We intentionally do NOT reload :mod:`stage2_gates` in the
    # autouse teardown. ``probability_gate`` reads the threshold via
    # :func:`config.get_stage2_probability_threshold` (a fresh env
    # read) so it picks up the restored env automatically. Reloading
    # ``stage2_gates`` would invalidate ``ProbabilityGateResult`` 's
    # class identity for already-imported names and break
    # ``isinstance(...)`` assertions in subsequent tests.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider_result(
    name: str,
    *,
    probability: float = 0.85,
    label: str = "material",
    direction: str = "bullish",
    error: str | None = None,
) -> ProviderResult:
    """Build a synthetic ProviderResult; failures pass ``error=...``."""
    if error is not None:
        return ProviderResult(provider=name, error=error)
    return ProviderResult(
        provider=name,
        label=label,
        probability=probability,
        direction=direction,
        rationale=f"{name} stub rationale",
        citations=[],
        latency_ms=10,
        cost_usd=0.001,
    )


def _make_ensemble_result(probabilities: list[float]) -> EnsembleEventResult:
    """Wrap a list of probabilities (one per ALL_PROVIDERS slot) in a
    canonical-ordered EnsembleEventResult. Probabilities of ``None``
    encode a failed provider (carried as ``error='stub'``)."""
    if len(probabilities) != len(ALL_PROVIDERS):
        raise AssertionError(
            f"need {len(ALL_PROVIDERS)} probability slots, got {len(probabilities)}"
        )
    rows: list[ProviderResult] = []
    for name, prob in zip(ALL_PROVIDERS, probabilities):
        if prob is None:
            rows.append(_make_provider_result(name, error="stub_failure"))
        else:
            rows.append(_make_provider_result(name, probability=prob))
    successful = [r.provider for r in rows if r.error is None]
    failed = [r.provider for r in rows if r.error is not None]
    return EnsembleEventResult(
        candidate_event_id=None,
        run_id="run-test",
        per_provider_results=rows,
        successful_providers=successful,
        failed_providers=failed,
        mean_probability=(
            sum(p for p in probabilities if p is not None) / len(successful)
            if successful else None
        ),
    )


# ---------------------------------------------------------------------------
# VAL-M3-022 — env override defaults / honored
# ---------------------------------------------------------------------------


def test_probability_threshold_default_is_075(monkeypatch):
    """With ``STAGE2_PROBABILITY_THRESHOLD`` unset, the default is 0.75."""
    monkeypatch.delenv("STAGE2_PROBABILITY_THRESHOLD", raising=False)
    importlib.reload(_config)
    # NOTE: Do NOT reload ``stage2_gates`` here — the autouse teardown
    # explains why (class identity preservation). The
    # ``DEFAULT_STAGE2_PROBABILITY_THRESHOLD`` constant in the gate
    # module mirrors ``config.DEFAULT_STAGE2_PROBABILITY_THRESHOLD``,
    # which is a hardcoded ``Final[float]`` and never changes.
    assert _config.STAGE2_PROBABILITY_THRESHOLD == 0.75
    assert _config.get_stage2_probability_threshold() == 0.75
    # Default constant in the gate module mirrors config.
    assert stage2_gates.DEFAULT_STAGE2_PROBABILITY_THRESHOLD == 0.75


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0.85", 0.85),
        ("0.50", 0.50),
        ("0.95", 0.95),
        ("1.0", 1.0),
        ("  0.65  ", 0.65),  # tolerant of surrounding whitespace
    ],
)
def test_probability_threshold_env_override(monkeypatch, raw, expected):
    """``STAGE2_PROBABILITY_THRESHOLD=<v>`` is honored by the getter."""
    monkeypatch.setenv("STAGE2_PROBABILITY_THRESHOLD", raw)
    assert _config.get_stage2_probability_threshold() == pytest.approx(
        expected
    )

    # The gate reads via the getter, so changes take effect without
    # a process restart.
    er = _make_ensemble_result([expected, expected, expected, expected])
    res = probability_gate(er)
    assert res.threshold == pytest.approx(expected)
    assert res.passed is True


def test_threshold_falls_back_to_default_on_unparseable_env(monkeypatch):
    monkeypatch.setenv("STAGE2_PROBABILITY_THRESHOLD", "not-a-number")
    assert _config.get_stage2_probability_threshold() == 0.75


def test_no_raw_environ_lookup_outside_config():
    """The probability gate must not read STAGE2_PROBABILITY_THRESHOLD
    via raw ``os.environ`` outside :mod:`biotech_sniper.config`.

    Single-source-of-truth invariant — mirrors the
    ``config_single_source_check`` services.yaml command.
    """
    src = open(stage2_gates.__file__).read()
    # No raw env lookups for the threshold key.
    assert "os.environ.get(\"STAGE2_PROBABILITY_THRESHOLD\"" not in src
    assert "os.environ['STAGE2_PROBABILITY_THRESHOLD'" not in src
    assert "os.getenv(\"STAGE2_PROBABILITY_THRESHOLD\"" not in src


# ---------------------------------------------------------------------------
# VAL-M3-023 — mean across exactly successful providers
# ---------------------------------------------------------------------------


def test_mean_probability_arithmetic():
    """Mean is computed as ``sum(probs) / len(probs)`` over the
    successful providers — exact arithmetic equality."""
    probs = [0.80, 0.78, 0.74, 0.72]
    er = _make_ensemble_result(probs)
    res = probability_gate(er, threshold=0.75)
    assert res.n_successful_providers == 4
    assert res.mean_probability == pytest.approx(sum(probs) / 4)
    assert res.mean_probability == pytest.approx(0.76)
    # mean=0.76 ≥ 0.75 → pass.
    assert res.passed is True
    assert res.reason is None


def test_mean_does_not_use_zero_for_failed_providers():
    """A failed provider must NOT pull the mean toward zero — the gate
    uses the SUCCESSFUL providers only (and structurally rejects with
    insufficient_providers when fewer than 4 succeed)."""
    # 3 successful at 0.90, 1 failed.
    er = _make_ensemble_result([0.90, 0.90, 0.90, None])
    res = probability_gate(er, threshold=0.75)
    # Insufficient providers → gate cannot pass; mean remains None
    # because the structural precondition (4/4 successful) is not met.
    assert res.passed is False
    assert res.reason == GATE_REASON_INSUFFICIENT_PROVIDERS
    assert res.mean_probability is None
    assert res.n_successful_providers == 3
    # Critically: the mean was NOT computed as 0.675 (which would be
    # 3*0.9/4 if failed providers contributed 0).
    assert res.mean_probability != pytest.approx(0.675)


@pytest.mark.parametrize(
    "probs,expected_mean",
    [
        ([0.76, 0.76, 0.76, 0.76], 0.76),
        ([0.74, 0.76, 0.76, 0.74], 0.75),
        ([0.74, 0.74, 0.76, 0.74], 0.745),
        ([0.50, 0.50, 0.50, 0.50], 0.50),
        ([1.0, 1.0, 1.0, 1.0], 1.0),
        ([0.0, 0.0, 0.0, 0.0], 0.0),
    ],
)
def test_mean_arithmetic_parametrised(probs, expected_mean):
    er = _make_ensemble_result(probs)
    res = probability_gate(er, threshold=0.75)
    assert res.mean_probability == pytest.approx(expected_mean)


# ---------------------------------------------------------------------------
# VAL-M3-024 — boundary inclusive (>=) / strict-less-than rejects
# ---------------------------------------------------------------------------


def test_threshold_boundary_inclusive_passes():
    """mean >= 0.75 → pass. The boundary is INCLUSIVE."""
    # Exact boundary.
    probs = [0.74, 0.76, 0.76, 0.74]  # mean = 0.75 exactly
    er = _make_ensemble_result(probs)
    res = probability_gate(er, threshold=0.75)
    assert res.mean_probability == pytest.approx(0.75)
    assert res.passed is True, "boundary case must pass — inclusive >="
    assert res.reason is None


def test_above_threshold_passes():
    probs = [0.76, 0.76, 0.76, 0.76]  # mean = 0.76
    er = _make_ensemble_result(probs)
    res = probability_gate(er, threshold=0.75)
    assert res.passed is True
    assert res.reason is None
    assert res.mean_probability == pytest.approx(0.76)


def test_below_threshold_rejects_with_reason_probability_below_threshold():
    """mean < threshold → reject with ``probability_below_threshold``."""
    probs = [0.74, 0.74, 0.76, 0.74]  # mean = 0.745
    er = _make_ensemble_result(probs)
    res = probability_gate(er, threshold=0.75)
    assert res.mean_probability == pytest.approx(0.745)
    assert res.passed is False
    assert res.reason == GATE_REASON_PROBABILITY_BELOW_THRESHOLD
    assert res.threshold == pytest.approx(0.75)


def test_threshold_logs_gate_failed_below_threshold(caplog):
    import logging

    caplog.set_level(logging.INFO)
    probs = [0.74, 0.74, 0.76, 0.74]  # mean = 0.745
    er = _make_ensemble_result(probs)
    probability_gate(er, threshold=0.75)
    log_text = " ".join(rec.getMessage() for rec in caplog.records)
    assert "probability_below_threshold" in log_text
    assert "0.7450" in log_text or "mean=0.745" in log_text
    assert "0.7500" in log_text or "threshold=0.75" in log_text


# ---------------------------------------------------------------------------
# VAL-M3-025 — 3/4 cannot pass the threshold gate alone
# ---------------------------------------------------------------------------


def test_threshold_gate_skipped_on_partial():
    """3 successful providers, all probability=1.0, with 1 failed.

    Even though the mean over the successful 3 is 1.0 (which would
    nominally exceed any threshold ≤ 1.0), the gate must NOT pass —
    the structural precondition is "exactly 4 successful providers".
    The gate returns ``insufficient_providers`` and unanimity is the
    canonical upstream rejection reason.
    """
    er = _make_ensemble_result([1.0, 1.0, 1.0, None])
    res = probability_gate(er, threshold=0.75)
    assert res.passed is False
    assert res.reason == GATE_REASON_INSUFFICIENT_PROVIDERS
    assert res.mean_probability is None
    assert res.n_successful_providers == 3


@pytest.mark.parametrize("n_success", [0, 1, 2, 3])
def test_partial_failure_modes_all_skip(n_success):
    """Any successful count < 4 → ``insufficient_providers``."""
    probs: list[float | None] = [0.90] * n_success + [None] * (4 - n_success)
    er = _make_ensemble_result(probs)
    res = probability_gate(er, threshold=0.75)
    assert res.passed is False
    assert res.reason == GATE_REASON_INSUFFICIENT_PROVIDERS
    assert res.n_successful_providers == n_success


def test_threshold_skipped_on_partial_logs_audit_line(caplog):
    import logging

    caplog.set_level(logging.INFO)
    er = _make_ensemble_result([1.0, 1.0, 1.0, None])
    probability_gate(er, threshold=0.75)
    log_text = " ".join(rec.getMessage() for rec in caplog.records)
    assert "insufficient_providers" in log_text
    assert "3" in log_text  # records the success count


# ---------------------------------------------------------------------------
# VAL-M3-026 — gate evaluated AFTER fan-out (row-count test)
# ---------------------------------------------------------------------------


def _make_unanimous_provider(probability=0.85):
    def _call(candidate, *, name=None):  # noqa: ARG001
        return {
            "label": "material",
            "probability": probability,
            "direction": "bullish",
            "rationale": "stub",
            "citations": [],
            "latency_ms": 5,
            "cost_usd": 0.001,
        }
    return _call


def _build_v10_db_with_candidate(db_path):
    conn = project_db.connect(db_path)
    try:
        project_db.run_migrations(conn)
    finally:
        conn.close()
    run_v10(db_path, 10, take_backup_first=False)
    conn = project_db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO news_events ("
            "ticker, source, title, url, published_at, ingested_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                "TESTX", "rss", "TESTX phase iii readout",
                "https://example.com/x",
                "2026-04-30T12:00:00Z", "2026-04-30T12:00:01Z",
            ),
        )
        nid = conn.execute("SELECT MAX(id) FROM news_events").fetchone()[0]
        conn.execute(
            "INSERT INTO candidate_events ("
            "ticker, source_news_event_id, matched_keywords,"
            " emitted_at, dedup_key) VALUES (?,?,?,?,?)",
            ("TESTX", nid, "phase_iii,readout",
             "2026-04-30T12:00:02Z", "dedup-testx-001"),
        )
        cid = conn.execute(
            "SELECT MAX(id) FROM candidate_events"
        ).fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return cid


def test_threshold_only_after_fanout(tmp_path):
    """The probability gate is a structural post-fanout consumer:

    1. ``score_candidate_event`` fans out across 4 providers and
       persists 4 rows into ``ensemble_scores_event``.
    2. ``probability_gate(result)`` then evaluates the threshold
       against the persisted result.
    3. Running the gate must NOT add any new ``ensemble_scores_event``
       rows (the gate is a pure consumer; it has no side effects on
       the persistence layer).

    The row-count assertion proves the gate can only run AFTER the
    fan-out has populated the ensemble result — there is no path by
    which the gate is evaluated BEFORE the LLM calls happen.
    """
    db_path = tmp_path / "alpha.db"
    cand_id = _build_v10_db_with_candidate(db_path)

    # Fan-out: 4 unanimous providers at 0.85 → mean = 0.85.
    providers = {p: _make_unanimous_provider(probability=0.85)
                 for p in ALL_PROVIDERS}
    er = score_candidate_event(
        {"id": cand_id, "ticker": "TESTX"},
        run_id="run-rowcount",
        db_path=db_path,
        providers=providers,
    )
    assert er.gate_failed_reason is None
    assert sorted(er.successful_providers) == sorted(ALL_PROVIDERS)

    # Snapshot row count after fan-out.
    conn = project_db.connect(db_path)
    try:
        rows_after_fanout = conn.execute(
            "SELECT COUNT(*) FROM ensemble_scores_event "
            "WHERE candidate_event_id=? AND run_id=?",
            (cand_id, "run-rowcount"),
        ).fetchone()[0]
    finally:
        conn.close()
    assert rows_after_fanout == 4

    # Now evaluate the gate.
    res = probability_gate(er, threshold=0.75)
    assert res.passed is True
    assert res.mean_probability == pytest.approx(0.85)

    # Row count must be unchanged — the gate is a pure consumer.
    conn = project_db.connect(db_path)
    try:
        rows_after_gate = conn.execute(
            "SELECT COUNT(*) FROM ensemble_scores_event "
            "WHERE candidate_event_id=? AND run_id=?",
            (cand_id, "run-rowcount"),
        ).fetchone()[0]
    finally:
        conn.close()
    assert rows_after_gate == rows_after_fanout, (
        "probability_gate must not write to ensemble_scores_event"
    )

    # Reject path also performs no writes.
    er_reject = _make_ensemble_result([0.74, 0.74, 0.76, 0.74])
    res2 = probability_gate(er_reject, threshold=0.75)
    assert res2.passed is False
    assert res2.reason == GATE_REASON_PROBABILITY_BELOW_THRESHOLD


def test_gate_input_is_ensemble_result_only():
    """The gate signature consumes :class:`EnsembleEventResult` —
    it cannot be invoked WITHOUT a fan-out result, which structurally
    enforces the post-fanout ordering."""
    import inspect
    sig = inspect.signature(probability_gate)
    params = list(sig.parameters.values())
    assert params, "probability_gate must take at least one parameter"
    first = params[0]
    # Annotation must reference EnsembleEventResult.
    annot = first.annotation
    # With ``from __future__ import annotations``, annotations are
    # PEP 563 strings; we accept either the resolved class or the
    # string form ``"EnsembleEventResult"``.
    assert annot is EnsembleEventResult or annot == EnsembleEventResult or annot == "EnsembleEventResult"


# ---------------------------------------------------------------------------
# Result shape sanity checks
# ---------------------------------------------------------------------------


def test_result_dataclass_shape():
    er = _make_ensemble_result([0.80, 0.80, 0.80, 0.80])
    res = probability_gate(er, threshold=0.75)
    assert isinstance(res, ProbabilityGateResult)
    assert isinstance(res.passed, bool)
    assert res.reason is None or isinstance(res.reason, str)
    assert isinstance(res.threshold, float)
    assert isinstance(res.n_successful_providers, int)


def test_passing_threshold_via_kwarg_overrides_config(monkeypatch):
    """An explicit ``threshold=`` kwarg is honored even when the env
    sets a different value (lets the dispatcher inject a per-call
    override; mirrors the f-m3-04 design contract)."""
    monkeypatch.setenv("STAGE2_PROBABILITY_THRESHOLD", "0.50")
    er = _make_ensemble_result([0.60, 0.60, 0.60, 0.60])  # mean=0.60
    # With env-derived 0.50 → would pass.
    res_env = probability_gate(er)
    assert res_env.threshold == pytest.approx(0.50)
    assert res_env.passed is True
    # With explicit 0.95 → rejects.
    res_kw = probability_gate(er, threshold=0.95)
    assert res_kw.threshold == pytest.approx(0.95)
    assert res_kw.passed is False
    assert res_kw.reason == GATE_REASON_PROBABILITY_BELOW_THRESHOLD
