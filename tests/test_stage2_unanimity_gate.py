"""Tests for the Reading-B Stage-2 unanimity (label + direction) gate.

Feature: f-m3-05-unanimity-and-direction.

Verifies the contract assertions VAL-M3-027 through VAL-M3-031:

* VAL-M3-027 — All 4 providers must label ``material``. The gate is
  strict equality, NOT majority vote. Any non-material label rejects
  with ``reason='unanimity_failed'``.
* VAL-M3-028 — ``ambiguous`` is NOT silently coerced to material —
  even one ``ambiguous`` label among otherwise-material providers
  blocks entry.
* VAL-M3-029 — Mixed labels record each provider's actual decision in
  ``ensemble_scores_event``; the gate's :class:`UnanimityGateResult`
  exposes a ``label_histogram`` for the audit log.
* VAL-M3-030 — Even with 4/4 ``material``, direction must also be
  unanimous (all ``bullish`` OR all ``bearish``). Direction divergence
  among material providers rejects with ``reason='direction_split'``.
  An ``ambiguous`` / null direction from any provider (even on a
  material label) counts as a split.
* VAL-M3-031 — When the probability threshold gate AND the unanimity
  gate both fail, the canonical (first) audit reason is
  ``probability_below_threshold`` — ``evaluate_post_fanout_gates``
  must NOT swallow the threshold failure under a unanimity message.

The unanimity gate is a pure consumer of
:class:`biotech_sniper.llm.ensemble.EnsembleEventResult`; it has no
side effects on the persistence layer (no ``ensemble_scores_event``
rows are written by the gate itself — those come from the upstream
fan-out).

Per the dual-path test convention in ``library`` / ``AGENTS.md``,
``tests/llm/test_stage2_gates.py`` re-exports the same test bodies via
``from tests.test_stage2_unanimity_gate import *`` so contract
node-IDs of either form collect.
"""

from __future__ import annotations

import importlib
import os
from typing import Any, Optional

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
    GATE_REASON_DIRECTION_SPLIT,
    GATE_REASON_INSUFFICIENT_PROVIDERS,
    GATE_REASON_PROBABILITY_BELOW_THRESHOLD,
    GATE_REASON_UNANIMITY_FAILED,
    PostFanoutGatesResult,
    UnanimityGateResult,
    evaluate_post_fanout_gates,
    unanimity_gate,
)
from biotech_sniper.migrations.runner import run as run_v10


# ---------------------------------------------------------------------------
# Module-reload teardown — same pattern as test_stage2_probability_gate.py.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_config_module_after_test():
    yield
    os.environ.pop("STAGE2_PROBABILITY_THRESHOLD", None)
    importlib.reload(_config)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider_result(
    name: str,
    *,
    probability: float = 0.85,
    label: str = "material",
    direction: Optional[str] = "bullish",
    error: Optional[str] = None,
) -> ProviderResult:
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


def _make_ensemble_result_full(
    *,
    labels: list[str],
    directions: list[Optional[str]],
    probabilities: Optional[list[float]] = None,
) -> EnsembleEventResult:
    """Build an EnsembleEventResult from explicit per-provider labels /
    directions / probabilities. Lists are aligned with
    :data:`ALL_PROVIDERS` slot order. Use ``label='__error__'`` to
    encode a failed provider in that slot."""
    if probabilities is None:
        probabilities = [0.85] * len(ALL_PROVIDERS)
    if not (len(labels) == len(directions) == len(probabilities) == len(ALL_PROVIDERS)):
        raise AssertionError(
            f"all lists must have length {len(ALL_PROVIDERS)}"
        )
    rows: list[ProviderResult] = []
    for name, label, direction, prob in zip(
        ALL_PROVIDERS, labels, directions, probabilities
    ):
        if label == "__error__":
            rows.append(_make_provider_result(name, error="stub_failure"))
        else:
            rows.append(
                _make_provider_result(
                    name,
                    label=label,
                    direction=direction,
                    probability=prob,
                )
            )
    successful = [r.provider for r in rows if r.error is None]
    failed = [r.provider for r in rows if r.error is not None]
    label_hist: dict[str, int] = {}
    for r in rows:
        if r.error is None:
            key = r.label or "unknown"
            label_hist[key] = label_hist.get(key, 0) + 1
    succ_probs = [r.probability for r in rows if r.error is None and r.probability is not None]
    return EnsembleEventResult(
        candidate_event_id=None,
        run_id="run-test",
        per_provider_results=rows,
        successful_providers=successful,
        failed_providers=failed,
        mean_probability=(sum(succ_probs) / len(succ_probs)) if succ_probs else None,
        label_histogram=label_hist,
    )


def _all_material_bullish(probability: float = 0.85) -> EnsembleEventResult:
    return _make_ensemble_result_full(
        labels=["material"] * 4,
        directions=["bullish"] * 4,
        probabilities=[probability] * 4,
    )


# ---------------------------------------------------------------------------
# VAL-M3-027 — strict 4/4 'material'
# ---------------------------------------------------------------------------


def test_unanimity_4M_passes():
    """Canonical happy path: 4/4 material + 4/4 bullish → pass."""
    er = _all_material_bullish()
    res = unanimity_gate(er)
    assert isinstance(res, UnanimityGateResult)
    assert res.passed is True
    assert res.reason is None
    assert res.n_material == 4
    assert res.n_successful_providers == 4
    assert res.label_histogram == {"material": 4}
    assert res.direction == "bullish"


def test_unanimity_4M_bearish_passes():
    """4/4 material + 4/4 bearish → pass with consensus direction='bearish'."""
    er = _make_ensemble_result_full(
        labels=["material"] * 4,
        directions=["bearish"] * 4,
    )
    res = unanimity_gate(er)
    assert res.passed is True
    assert res.reason is None
    assert res.direction == "bearish"
    assert res.label_histogram == {"material": 4}


@pytest.mark.parametrize(
    "labels,expected_hist",
    [
        # Exact contract fixture matrix from VAL-M3-027.
        (["material", "material", "material", "ambiguous"],
         {"material": 3, "ambiguous": 1}),
        (["material", "material", "material", "immaterial"],
         {"material": 3, "immaterial": 1}),
        (["material", "material", "ambiguous", "ambiguous"],
         {"material": 2, "ambiguous": 2}),
        (["ambiguous", "ambiguous", "ambiguous", "ambiguous"],
         {"ambiguous": 4}),
        (["immaterial", "immaterial", "immaterial", "immaterial"],
         {"immaterial": 4}),
    ],
)
def test_unanimity_strict_4of4(labels, expected_hist):
    """The gate is strict equality — only 4M passes. Every other
    fixture in the contract matrix rejects with unanimity_failed."""
    er = _make_ensemble_result_full(
        labels=labels,
        directions=["bullish"] * 4,
    )
    res = unanimity_gate(er)
    assert res.passed is False
    assert res.reason == GATE_REASON_UNANIMITY_FAILED
    assert res.label_histogram == expected_hist
    # Consensus direction MUST NOT be reported on a failed gate
    # (avoids downstream code mistakenly routing to bullish CALL).
    assert res.direction is None


def test_unanimity_logs_gate_failed(caplog):
    import logging

    caplog.set_level(logging.INFO)
    er = _make_ensemble_result_full(
        labels=["material", "material", "material", "ambiguous"],
        directions=["bullish"] * 4,
    )
    unanimity_gate(er)
    log_text = " ".join(rec.getMessage() for rec in caplog.records)
    assert "unanimity_failed" in log_text
    # Histogram counts surface in the log so operators can audit.
    assert "material" in log_text
    assert "ambiguous" in log_text


# ---------------------------------------------------------------------------
# VAL-M3-028 — ambiguous blocks entry
# ---------------------------------------------------------------------------


def test_ambiguous_blocks_entry():
    """Fixture [material, material, material, ambiguous] → reject;
    audit log records unanimity_failed (3 material, 1 ambiguous)."""
    er = _make_ensemble_result_full(
        labels=["material", "material", "material", "ambiguous"],
        directions=["bullish"] * 4,
    )
    res = unanimity_gate(er)
    assert res.passed is False
    assert res.reason == GATE_REASON_UNANIMITY_FAILED
    assert res.label_histogram == {"material": 3, "ambiguous": 1}
    assert res.n_material == 3


def test_ambiguous_label_blocks_even_with_unanimous_direction():
    """Even if all directions agree, a single ambiguous label rejects."""
    er = _make_ensemble_result_full(
        labels=["material", "material", "ambiguous", "material"],
        directions=["bullish"] * 4,
    )
    res = unanimity_gate(er)
    assert res.passed is False
    assert res.reason == GATE_REASON_UNANIMITY_FAILED


# ---------------------------------------------------------------------------
# VAL-M3-029 — mixed labels persist per-provider
# ---------------------------------------------------------------------------


def _make_provider_callable(label: str, direction: str, probability: float):
    """Build a deterministic stub provider callable for fan-out."""
    def _call(candidate, *, name=None):  # noqa: ARG001
        return {
            "label": label,
            "probability": probability,
            "direction": direction,
            "rationale": f"{name} stub rationale",
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
    # Belt-and-suspenders: run the explicit migration runner against
    # the current floor (CURRENT_VERSION). f-misc-09 bumped this from
    # 10 → 11; using ``project_db.CURRENT_VERSION`` keeps the helper
    # forward-compatible across future bumps.
    run_v10(db_path, project_db.CURRENT_VERSION, take_backup_first=False)
    conn = project_db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO news_events ("
            "ticker, source, title, url, published_at, ingested_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                "TESTU", "rss", "TESTU phase iii readout",
                "https://example.com/u",
                "2026-04-30T12:00:00Z", "2026-04-30T12:00:01Z",
            ),
        )
        nid = conn.execute("SELECT MAX(id) FROM news_events").fetchone()[0]
        conn.execute(
            "INSERT INTO candidate_events ("
            "ticker, source_news_event_id, matched_keywords,"
            " emitted_at, dedup_key) VALUES (?,?,?,?,?)",
            ("TESTU", nid, "phase_iii,readout",
             "2026-04-30T12:00:02Z", "dedup-testu-001"),
        )
        cid = conn.execute(
            "SELECT MAX(id) FROM candidate_events"
        ).fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return cid


def test_mixed_labels_persist_per_provider(tmp_path):
    """After a 3M+1A fixture run, ``SELECT provider, label FROM
    ensemble_scores_event WHERE candidate_event_id=?`` returns 4 rows
    with the EXACT original labels (no overwrite to a synthetic
    consensus value)."""
    db_path = tmp_path / "alpha.db"
    cand_id = _build_v10_db_with_candidate(db_path)

    # 3 material providers + 1 ambiguous, all bullish probability 0.9.
    providers = {
        "xai": _make_provider_callable("material", "bullish", 0.90),
        "anthropic": _make_provider_callable("material", "bullish", 0.90),
        "gemini": _make_provider_callable("material", "bullish", 0.90),
        "perplexity": _make_provider_callable("ambiguous", "bullish", 0.90),
    }
    er = score_candidate_event(
        {"id": cand_id, "ticker": "TESTU"},
        run_id="run-mixed-labels",
        db_path=db_path,
        providers=providers,
    )

    # Persisted 4 rows; the original labels are preserved verbatim.
    conn = project_db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT provider, label FROM ensemble_scores_event "
            "WHERE candidate_event_id=? AND run_id=? "
            "ORDER BY provider",
            (cand_id, "run-mixed-labels"),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 4
    label_by_provider = {row[0]: row[1] for row in rows}
    assert label_by_provider["xai"] == "material"
    assert label_by_provider["anthropic"] == "material"
    assert label_by_provider["gemini"] == "material"
    assert label_by_provider["perplexity"] == "ambiguous"

    # Unanimity gate rejects with the canonical histogram.
    res = unanimity_gate(er)
    assert res.passed is False
    assert res.reason == GATE_REASON_UNANIMITY_FAILED
    assert res.label_histogram == {"material": 3, "ambiguous": 1}


# ---------------------------------------------------------------------------
# VAL-M3-030 — direction unanimity within material
# ---------------------------------------------------------------------------


def test_direction_unanimity_within_material():
    """Fixture [4×material, directions=(bull,bull,bear,bull)] is
    rejected with reason ``direction_split`` (no order submitted)."""
    er = _make_ensemble_result_full(
        labels=["material"] * 4,
        directions=["bullish", "bullish", "bearish", "bullish"],
    )
    res = unanimity_gate(er)
    assert res.passed is False
    assert res.reason == GATE_REASON_DIRECTION_SPLIT
    # Direction split → no consensus direction.
    assert res.direction is None
    # Label histogram still records the per-provider truth.
    assert res.label_histogram == {"material": 4}


@pytest.mark.parametrize(
    "directions",
    [
        ["bullish", "bullish", "bullish", "bearish"],
        ["bearish", "bearish", "bearish", "bullish"],
        ["bullish", "bearish", "bullish", "bearish"],
    ],
)
def test_direction_split_rejects(directions):
    er = _make_ensemble_result_full(
        labels=["material"] * 4,
        directions=directions,
    )
    res = unanimity_gate(er)
    assert res.passed is False
    assert res.reason == GATE_REASON_DIRECTION_SPLIT


def test_ambiguous_direction_blocks_even_with_4M_unanimous():
    """A null / ambiguous direction from ANY material provider blocks
    entry — direction must be a clean bull/bear, never None."""
    er = _make_ensemble_result_full(
        labels=["material"] * 4,
        directions=["bullish", "bullish", None, "bullish"],
    )
    res = unanimity_gate(er)
    assert res.passed is False
    assert res.reason == GATE_REASON_DIRECTION_SPLIT


def test_direction_split_logs_gate_failed(caplog):
    import logging

    caplog.set_level(logging.INFO)
    er = _make_ensemble_result_full(
        labels=["material"] * 4,
        directions=["bullish", "bullish", "bearish", "bullish"],
    )
    unanimity_gate(er)
    log_text = " ".join(rec.getMessage() for rec in caplog.records)
    assert "direction_split" in log_text


# ---------------------------------------------------------------------------
# Insufficient-provider edge case (3/4) → insufficient_providers
# ---------------------------------------------------------------------------


def test_insufficient_providers_fails_unanimity_gate():
    """If fewer than 4 providers succeeded, the unanimity gate
    structurally cannot evaluate 4/4 — it returns
    ``insufficient_providers`` rather than ``unanimity_failed``."""
    er = _make_ensemble_result_full(
        labels=["material", "material", "material", "__error__"],
        directions=["bullish"] * 4,
    )
    res = unanimity_gate(er)
    assert res.passed is False
    assert res.reason == GATE_REASON_INSUFFICIENT_PROVIDERS
    assert res.n_successful_providers == 3
    assert res.n_material == 3


# ---------------------------------------------------------------------------
# VAL-M3-031 — probability gate evaluated BEFORE unanimity gate
# ---------------------------------------------------------------------------


def test_threshold_evaluated_before_unanimity():
    """Fixture [3 material at 0.6, 1 immaterial at 0.5] → mean=0.575.

    Both the threshold gate (0.575 < 0.75) AND the unanimity gate
    (3M+1I) fail. The canonical (first) audit reason is
    ``probability_below_threshold`` — the implementation MUST NOT
    swallow the threshold failure under a unanimity message.
    """
    er = _make_ensemble_result_full(
        labels=["material", "material", "material", "immaterial"],
        directions=["bullish"] * 4,
        probabilities=[0.6, 0.6, 0.6, 0.5],
    )
    # Mean = 0.575.
    assert er.mean_probability == pytest.approx(0.575)

    result = evaluate_post_fanout_gates(er, threshold=0.75)
    assert isinstance(result, PostFanoutGatesResult)
    assert result.passed is False
    # First (canonical) failure is the probability gate.
    assert result.reason == GATE_REASON_PROBABILITY_BELOW_THRESHOLD
    # The unanimity gate result is ALSO recorded as secondary detail
    # — the implementation must surface both failures so operators
    # can see the dual-failure case in audit JSON.
    assert result.probability_gate is not None
    assert result.probability_gate.passed is False
    assert result.probability_gate.reason == GATE_REASON_PROBABILITY_BELOW_THRESHOLD
    assert result.unanimity_gate is not None
    assert result.unanimity_gate.passed is False
    assert result.unanimity_gate.reason == GATE_REASON_UNANIMITY_FAILED


def test_post_fanout_gates_audit_log_lists_threshold_first(caplog):
    """The audit log MUST emit ``probability_below_threshold`` as the
    canonical (first) failure reason, with ``unanimity_failed``
    recorded only as secondary/informational detail."""
    import logging

    caplog.set_level(logging.INFO)
    er = _make_ensemble_result_full(
        labels=["material", "material", "material", "immaterial"],
        directions=["bullish"] * 4,
        probabilities=[0.6, 0.6, 0.6, 0.5],
    )
    evaluate_post_fanout_gates(er, threshold=0.75)
    # Find the canonical audit line emitted by the helper.
    lines = [rec.getMessage() for rec in caplog.records
             if "post_fanout_gates" in rec.getMessage()]
    assert lines, "expected at least one post_fanout_gates audit log line"
    # The canonical line names the threshold failure first.
    canonical = lines[0]
    pos_thr = canonical.find(GATE_REASON_PROBABILITY_BELOW_THRESHOLD)
    pos_uni = canonical.find(GATE_REASON_UNANIMITY_FAILED)
    assert pos_thr != -1, "canonical reason must be probability_below_threshold"
    # If unanimity is also recorded, it appears AFTER the threshold reason.
    if pos_uni != -1:
        assert pos_thr < pos_uni, (
            "threshold failure must be recorded BEFORE the unanimity failure "
            "in the audit line"
        )


def test_post_fanout_gates_unanimity_only_failure():
    """When threshold passes but unanimity fails, the canonical
    reason is ``unanimity_failed``."""
    er = _make_ensemble_result_full(
        labels=["material", "material", "material", "ambiguous"],
        directions=["bullish"] * 4,
        probabilities=[0.85, 0.85, 0.85, 0.85],
    )
    # Mean = 0.85 > 0.75 → threshold passes.
    result = evaluate_post_fanout_gates(er, threshold=0.75)
    assert result.passed is False
    assert result.reason == GATE_REASON_UNANIMITY_FAILED
    assert result.probability_gate is not None
    assert result.probability_gate.passed is True
    assert result.unanimity_gate is not None
    assert result.unanimity_gate.passed is False


def test_post_fanout_gates_direction_split_failure():
    """Direction-split rejection surfaces with reason ``direction_split``."""
    er = _make_ensemble_result_full(
        labels=["material"] * 4,
        directions=["bullish", "bullish", "bearish", "bullish"],
        probabilities=[0.85] * 4,
    )
    result = evaluate_post_fanout_gates(er, threshold=0.75)
    assert result.passed is False
    assert result.reason == GATE_REASON_DIRECTION_SPLIT
    assert result.unanimity_gate is not None
    assert result.unanimity_gate.passed is False
    assert result.unanimity_gate.reason == GATE_REASON_DIRECTION_SPLIT


def test_post_fanout_gates_pass_path():
    """Happy path: threshold passes AND unanimity passes."""
    er = _all_material_bullish(probability=0.85)
    result = evaluate_post_fanout_gates(er, threshold=0.75)
    assert result.passed is True
    assert result.reason is None
    assert result.probability_gate is not None
    assert result.probability_gate.passed is True
    assert result.unanimity_gate is not None
    assert result.unanimity_gate.passed is True
    assert result.unanimity_gate.direction == "bullish"


def test_post_fanout_gates_insufficient_providers_short_circuits():
    """If fewer than 4 providers succeeded, both gates short-circuit
    on ``insufficient_providers`` — the helper surfaces the gate's
    canonical reason WITHOUT inventing a synthetic combined reason."""
    er = _make_ensemble_result_full(
        labels=["material", "material", "material", "__error__"],
        directions=["bullish"] * 4,
    )
    result = evaluate_post_fanout_gates(er, threshold=0.75)
    assert result.passed is False
    assert result.reason == GATE_REASON_INSUFFICIENT_PROVIDERS


# ---------------------------------------------------------------------------
# Result-shape sanity checks
# ---------------------------------------------------------------------------


def test_unanimity_gate_returns_dataclass():
    er = _all_material_bullish()
    res = unanimity_gate(er)
    assert isinstance(res, UnanimityGateResult)
    assert isinstance(res.passed, bool)
    assert res.reason is None or isinstance(res.reason, str)
    assert isinstance(res.label_histogram, dict)
    assert isinstance(res.n_material, int)
    assert isinstance(res.n_successful_providers, int)


def test_unanimity_gate_no_side_effects_on_input():
    """The gate does not mutate the input EnsembleEventResult."""
    er = _make_ensemble_result_full(
        labels=["material"] * 4,
        directions=["bullish"] * 4,
    )
    snap = list(er.per_provider_results)
    unanimity_gate(er)
    assert er.per_provider_results == snap


def test_unanimity_gate_input_is_ensemble_result_only():
    """The gate signature consumes :class:`EnsembleEventResult`."""
    import inspect

    sig = inspect.signature(unanimity_gate)
    params = list(sig.parameters.values())
    assert params, "unanimity_gate must take at least one parameter"
    annot = params[0].annotation
    assert annot is EnsembleEventResult or annot == EnsembleEventResult or \
        annot == "EnsembleEventResult"


def test_unanimity_gate_makes_no_db_writes(tmp_path):
    """Running the gate against a fan-out result writes ZERO additional
    ``ensemble_scores_event`` rows."""
    db_path = tmp_path / "alpha.db"
    cand_id = _build_v10_db_with_candidate(db_path)
    providers = {
        p: _make_provider_callable("material", "bullish", 0.85)
        for p in ALL_PROVIDERS
    }
    er = score_candidate_event(
        {"id": cand_id, "ticker": "TESTU"},
        run_id="run-no-side-effects",
        db_path=db_path,
        providers=providers,
    )
    conn = project_db.connect(db_path)
    try:
        before = conn.execute(
            "SELECT COUNT(*) FROM ensemble_scores_event "
            "WHERE candidate_event_id=? AND run_id=?",
            (cand_id, "run-no-side-effects"),
        ).fetchone()[0]
    finally:
        conn.close()
    assert before == 4
    # Run gate.
    unanimity_gate(er)
    # Run helper too.
    evaluate_post_fanout_gates(er, threshold=0.75)
    conn = project_db.connect(db_path)
    try:
        after = conn.execute(
            "SELECT COUNT(*) FROM ensemble_scores_event "
            "WHERE candidate_event_id=? AND run_id=?",
            (cand_id, "run-no-side-effects"),
        ).fetchone()[0]
    finally:
        conn.close()
    assert after == before, (
        "unanimity_gate / evaluate_post_fanout_gates must not write to "
        "ensemble_scores_event"
    )


# ---------------------------------------------------------------------------
# Module-source single-source-of-truth sanity checks (mirror probability gate)
# ---------------------------------------------------------------------------


def test_unanimity_gate_no_raw_environ_lookup():
    """The unanimity gate must not read STAGE2_PROBABILITY_THRESHOLD
    via raw ``os.environ``; the threshold is the probability gate's
    concern, not unanimity's.
    """
    src = open(stage2_gates.__file__).read()
    # The unanimity gate must not depend on a raw env lookup.
    forbidden = (
        "os.environ.get(\"STAGE2_PROBABILITY_THRESHOLD\"",
        "os.environ['STAGE2_PROBABILITY_THRESHOLD'",
        "os.getenv(\"STAGE2_PROBABILITY_THRESHOLD\"",
    )
    for token in forbidden:
        assert token not in src, f"forbidden raw env lookup: {token}"
