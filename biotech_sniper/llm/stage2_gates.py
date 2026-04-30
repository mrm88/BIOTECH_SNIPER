"""Stage-2 (Reading-B) entry-gate functions for the 4-provider ensemble.

This module hosts the small, pure-function gates that the Stage-2
dispatcher (``stage2_dispatcher`` — wired by subsequent f-m3 features)
composes in cheap-first order to decide whether a Stage-2
``candidate_events`` row produces a ``news_event_entry`` paper order.

The mission-canonical gate ordering (AGENTS.md, ``mission.md``) is::

    cooldown
        → armed
        → cap-projection
        → 4-provider fan-out  ← LLM costs incurred here
        → unanimity
        → probability         ← THIS MODULE (post-fanout)
        → direction
        → executor-gates

This file currently implements ONE gate — the probability threshold
gate (feature f-m3-04). Subsequent f-m3 features extend the module
with the remaining gates (cooldown, armed, cap-projection, unanimity,
direction). Each gate is implemented as a small pure function that
returns a typed result (``passed`` / ``reason`` / observed values)
the dispatcher can consume to decide whether to short-circuit.

Contract assertions verified by ``tests/test_stage2_probability_gate.py``:

* VAL-M3-022 — ``STAGE2_PROBABILITY_THRESHOLD`` env override defaults
  to ``0.75``; the gate reads the threshold via
  :func:`biotech_sniper.config.get_stage2_probability_threshold` only
  (no raw ``os.environ`` lookup outside ``config.py``).
* VAL-M3-023 — Mean is computed as ``sum(probs) / len(probs)`` over
  the SUCCESSFUL providers only; failed providers do NOT pull the
  mean toward zero.
* VAL-M3-024 — ``mean >= threshold`` passes (the boundary is
  INCLUSIVE); ``mean < threshold`` rejects with
  ``reason='probability_below_threshold'``.
* VAL-M3-025 — When fewer than four providers succeeded, the gate
  returns ``passed=False, reason='insufficient_providers'`` and emits
  an audit log line. Even a 3/4 perfect-score subset cannot pass via
  the threshold alone — the upstream unanimity gate (a separate
  feature) is the canonical rejection reason for this case.
* VAL-M3-026 — The gate is a pure consumer of
  :class:`biotech_sniper.llm.ensemble.EnsembleEventResult`; running
  the gate adds zero rows to ``ensemble_scores_event``. This
  structurally enforces post-fanout ordering: the gate has no input
  before the fan-out has populated the ensemble result.

Public surface
--------------

* :class:`ProbabilityGateResult` — dataclass returned by the gate.
* :func:`probability_gate` — the gate function itself.
* :data:`DEFAULT_STAGE2_PROBABILITY_THRESHOLD` — re-export of the
  config-module default for callers that want the raw constant.
* :data:`GATE_REASON_PROBABILITY_BELOW_THRESHOLD`,
  :data:`GATE_REASON_INSUFFICIENT_PROVIDERS` — canonical reason
  strings used in audit / dispatcher logs.
"""

from __future__ import annotations

import datetime as _dt
import json as _json
import logging
import os
import sqlite3 as _sqlite3
import stat as _stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from biotech_sniper import config
from biotech_sniper import db as _db
from biotech_sniper import paths as _paths
from biotech_sniper.llm.ensemble import (
    ALL_PROVIDERS,
    EnsembleEventResult,
)

__all__ = [
    "ProbabilityGateResult",
    "probability_gate",
    "DEFAULT_STAGE2_PROBABILITY_THRESHOLD",
    "GATE_REASON_PROBABILITY_BELOW_THRESHOLD",
    "GATE_REASON_INSUFFICIENT_PROVIDERS",
    # f-m3-05 — unanimity (label + direction) gate.
    "UnanimityGateResult",
    "unanimity_gate",
    "GATE_REASON_UNANIMITY_FAILED",
    "GATE_REASON_DIRECTION_SPLIT",
    # f-m3-05 — combined cheap-first post-fanout gate evaluation.
    "PostFanoutGatesResult",
    "evaluate_post_fanout_gates",
    # f-m3-06 — .armed filesystem gate (cheap-first, pre-fanout).
    "ArmedGateResult",
    "armed_gate",
    "GATE_REASON_ARMED_FILE_MISSING",
    # f-m3-07 — daily $ cap gate (cheap-first, pre-fanout).
    "DailyCapGateResult",
    "DailyCapExceeded",
    "daily_cap_gate",
    "record_stage2_skip",
    "GATE_REASON_DAILY_CAP_EXCEEDED",
    "STAGE2_CALL_USD_PROJECTION",
    "STAGE2_LEDGER_PURPOSE",
    "DEFAULT_LLM_STAGE2_DAILY_USD_CAP",
    # f-m3-08 — per-ticker 24h cooldown gate (cheap-first, pre-fanout).
    "CooldownGateResult",
    "cooldown_gate",
    "record_cooldown_on_success",
    "GATE_REASON_COOLDOWN_ACTIVE",
    "DEFAULT_PER_TICKER_COOLDOWN_HOURS",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Re-export of the canonical default from :mod:`biotech_sniper.config`
#: so callers that already import the gate module can get the default
#: without a second import of ``config``.
DEFAULT_STAGE2_PROBABILITY_THRESHOLD: float = (
    config.DEFAULT_STAGE2_PROBABILITY_THRESHOLD
)

#: Canonical reason string emitted when ``mean < threshold``. Stable
#: across audit / dispatcher logs; consumed by VAL-M3-024.
GATE_REASON_PROBABILITY_BELOW_THRESHOLD: str = "probability_below_threshold"

#: Canonical reason string emitted when fewer than four providers
#: succeeded. Stable across audit / dispatcher logs; consumed by
#: VAL-M3-025. The dispatcher's upstream unanimity gate produces the
#: actual ``unanimity_failed`` rejection reason — this constant marks
#: the threshold gate's own short-circuit when the structural
#: precondition is unmet.
GATE_REASON_INSUFFICIENT_PROVIDERS: str = "insufficient_providers"

#: Canonical reason string emitted by :func:`unanimity_gate` when the
#: 4/4 ``label='material'`` precondition is not met. Consumed by
#: VAL-M3-027 / VAL-M3-028 / VAL-M3-029. Any non-material label among
#: the four successful providers triggers this reason — the gate is
#: strict equality, NOT majority vote.
GATE_REASON_UNANIMITY_FAILED: str = "unanimity_failed"

#: Canonical reason string emitted by :func:`unanimity_gate` when all
#: four providers labelled ``material`` BUT the per-provider
#: ``direction`` field is not unanimously ``bullish`` / ``bearish``.
#: Consumed by VAL-M3-030. An ambiguous / null / mixed direction
#: among material providers blocks entry — this prevents the system
#: from buying a call when 2 providers said bearish.
GATE_REASON_DIRECTION_SPLIT: str = "direction_split"

#: Canonical reason string emitted by :func:`armed_gate` when the
#: ``.armed`` filesystem marker is absent (or unreadable / wrong file
#: type — those are treated as "absent" per the f-m3-06 contract).
#: Consumed by VAL-M3-032 and VAL-M5-022. The Stage-2 dispatcher
#: short-circuits with this reason BEFORE dispatching any LLM call —
#: a rejected armed-file gate produces ZERO ``llm_cost_ledger`` rows.
GATE_REASON_ARMED_FILE_MISSING: str = "armed_file_missing"

#: Canonical reason string emitted by :func:`daily_cap_gate` when
#: ``today_total + projected_cost > cap``. Consumed by VAL-M3-038 /
#: VAL-M3-039 / VAL-M3-040 / VAL-M3-041 / VAL-M5-027. The Stage-2
#: dispatcher short-circuits with this reason BEFORE dispatching any
#: LLM call — a rejected cap gate produces ZERO ``llm_cost_ledger``
#: rows. The audit trail records the cap-hit via
#: :func:`record_stage2_skip`, which writes a ``news_match_log`` row
#: AND merges a ``stage2_skipped[]`` block into ``audit_latest.json``.
GATE_REASON_DAILY_CAP_EXCEEDED: str = "daily_cap_exceeded"

#: Canonical ``llm_cost_ledger.purpose`` value the Stage-2 cap gate
#: filters on. Mirrors ``perplexity_client.DEFAULT_PURPOSE`` so a
#: change to one is caught by the test suite for both. The cap query
#: is::
#:
#:     SELECT SUM(cost_usd) FROM llm_cost_ledger
#:      WHERE purpose = 'stage2_event_scoring'
#:        AND DATE(called_at) = today_utc
#:
#: This filter intentionally EXCLUDES debate rows
#: (``purpose='debate'``), curated-daily-run rows
#: (``purpose='deep_science'`` / ``purpose='daily_curated'``), and
#: any other LLM spend categories so the Stage-2 cap is independent
#: from those paths (VAL-M3-041).
STAGE2_LEDGER_PURPOSE: str = "stage2_event_scoring"

#: Worst-case projected per-call cost (USD) for a 4-provider Stage-2
#: fan-out. The dispatcher uses this constant as the default
#: ``projected_cost`` when the caller omits the explicit kwarg, so
#: ``daily_cap_gate(db_path=...)`` is sufficient at the cheap-first
#: short-circuit point. Tuned conservatively above the typical
#: 4-provider call sum (Grok + Claude + Gemini + Perplexity at
#: small-prompt / low-search-context settings) so projection-driven
#: false-blocks are rare; tightened tracking happens via the
#: post-fan-out actual ``cost_usd`` writes that reset the daily
#: total on the next call.
#:
#: Re-export of :data:`biotech_sniper.config.DEFAULT_LLM_STAGE2_DAILY_USD_CAP`
#: divided by a per-day call-budget heuristic — keeping the projection
#: at $0.50 implies ≥ 40 fan-out calls fit within the $20 cap.
STAGE2_CALL_USD_PROJECTION: float = 0.50

#: Re-export of the canonical default from :mod:`biotech_sniper.config`
#: so callers that already import the gate module can get the default
#: without a second import of ``config``.
DEFAULT_LLM_STAGE2_DAILY_USD_CAP: float = (
    config.DEFAULT_LLM_STAGE2_DAILY_USD_CAP
)

#: Canonical reason string emitted by :func:`cooldown_gate` when the
#: per-ticker cooldown window is still active for the candidate's
#: ticker. Consumed by VAL-M3-045 / VAL-M5-025 / VAL-CROSS-031. The
#: Stage-2 dispatcher short-circuits with this reason BEFORE
#: dispatching any LLM call — a rejected cooldown gate produces ZERO
#: ``llm_cost_ledger`` rows.
GATE_REASON_COOLDOWN_ACTIVE: str = "cooldown_active"

#: Re-export of the canonical default from :mod:`biotech_sniper.config`
#: so callers that already import the gate module can get the default
#: without a second import of ``config``.
DEFAULT_PER_TICKER_COOLDOWN_HOURS: int = (
    config.DEFAULT_PER_TICKER_COOLDOWN_HOURS
)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ProbabilityGateResult:
    """Outcome of :func:`probability_gate`.

    Attributes
    ----------
    passed:
        ``True`` only when exactly four providers succeeded AND the
        arithmetic mean of their ``probability`` fields is
        ``>= threshold``. ``False`` in every other case (including
        the structural ``insufficient_providers`` short-circuit).
    reason:
        Canonical short-circuit reason. ``None`` when ``passed=True``.
        One of :data:`GATE_REASON_PROBABILITY_BELOW_THRESHOLD` or
        :data:`GATE_REASON_INSUFFICIENT_PROVIDERS` when ``passed=False``.
    mean_probability:
        The arithmetic mean of the successful providers'
        ``probability`` fields. ``None`` when fewer than four
        providers succeeded (the gate cannot evaluate the threshold
        without all four).
    threshold:
        The threshold that was applied (resolved from the
        ``threshold=`` kwarg, then from
        :func:`config.get_stage2_probability_threshold`).
    n_successful_providers:
        The count of successful providers in the ensemble result.
        Always in ``[0, 4]`` when ``ALL_PROVIDERS`` is the canonical
        4-tuple.
    """

    passed: bool
    reason: Optional[str]
    mean_probability: Optional[float]
    threshold: float
    n_successful_providers: int


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def probability_gate(
    ensemble_result: EnsembleEventResult,
    *,
    threshold: Optional[float] = None,
) -> ProbabilityGateResult:
    """Evaluate the Stage-2 mean-probability threshold gate.

    Parameters
    ----------
    ensemble_result:
        The :class:`EnsembleEventResult` returned by
        :func:`biotech_sniper.llm.ensemble.score_candidate_event`.
        The gate consumes the per-provider results to compute the
        mean over the successful subset; it does NOT make any LLM
        calls itself or write to the database. By design, the gate
        cannot run before the fan-out has produced a result — this
        structurally enforces VAL-M3-026 (post-fanout ordering).
    threshold:
        Optional explicit threshold (``[0.0, 1.0]``). When ``None``,
        the gate reads
        :func:`biotech_sniper.config.get_stage2_probability_threshold`
        at call time so an in-process env override (e.g.
        ``STAGE2_PROBABILITY_THRESHOLD=0.85``) takes effect without
        a process restart.

    Returns
    -------
    ProbabilityGateResult
        Always non-None. Never raises — the gate is a pure consumer.

    Behaviour matrix
    ----------------
    +--------------------+--------+-------+----------------------------+
    | n_successful       | mean   | pass? | reason                     |
    +====================+========+=======+============================+
    | < 4                | n/a    | False | insufficient_providers     |
    +--------------------+--------+-------+----------------------------+
    | 4, mean >= thr     | float  | True  | None                       |
    +--------------------+--------+-------+----------------------------+
    | 4, mean <  thr     | float  | False | probability_below_threshold|
    +--------------------+--------+-------+----------------------------+

    The boundary is INCLUSIVE — ``mean == threshold`` passes (VAL-M3-024).

    Side effects
    ------------
    None. The gate writes nothing to SQLite, makes no network calls,
    and emits only structured INFO log lines (``stage2_probability_gate:
    passed | rejected | skipped ...``) so operators can audit the
    decision path post-hoc.
    """
    resolved_threshold: float
    if threshold is None:
        resolved_threshold = config.get_stage2_probability_threshold()
    else:
        resolved_threshold = float(threshold)

    # Filter to the providers that produced a usable probability.
    # ``probability is None`` is treated as a failure (the ensemble
    # layer surfaces those as ``error=...`` rows and they would be
    # excluded by the ``error is None`` filter, but defending against
    # both makes the gate robust to provider adapters that return
    # ``None`` for probability without raising).
    successful = [
        r for r in ensemble_result.per_provider_results
        if r.error is None and r.probability is not None
    ]
    n_success = len(successful)
    expected = len(ALL_PROVIDERS)

    if n_success < expected:
        logger.info(
            "stage2_probability_gate: skipped %s "
            "(%d of %d providers successful, threshold=%.4f)",
            GATE_REASON_INSUFFICIENT_PROVIDERS,
            n_success,
            expected,
            resolved_threshold,
        )
        return ProbabilityGateResult(
            passed=False,
            reason=GATE_REASON_INSUFFICIENT_PROVIDERS,
            mean_probability=None,
            threshold=resolved_threshold,
            n_successful_providers=n_success,
        )

    # All four providers succeeded — compute the mean. Cast each
    # probability to float defensively in case a provider adapter
    # returned a ``Decimal`` / ``numpy.float64`` payload.
    probs = [float(r.probability) for r in successful]  # type: ignore[arg-type]
    mean_probability = sum(probs) / len(probs)

    if mean_probability >= resolved_threshold:
        logger.info(
            "stage2_probability_gate: passed "
            "mean=%.4f threshold=%.4f n=%d",
            mean_probability,
            resolved_threshold,
            n_success,
        )
        return ProbabilityGateResult(
            passed=True,
            reason=None,
            mean_probability=mean_probability,
            threshold=resolved_threshold,
            n_successful_providers=n_success,
        )

    logger.info(
        "stage2_probability_gate: gate_failed %s "
        "mean=%.4f threshold=%.4f n=%d",
        GATE_REASON_PROBABILITY_BELOW_THRESHOLD,
        mean_probability,
        resolved_threshold,
        n_success,
    )
    return ProbabilityGateResult(
        passed=False,
        reason=GATE_REASON_PROBABILITY_BELOW_THRESHOLD,
        mean_probability=mean_probability,
        threshold=resolved_threshold,
        n_successful_providers=n_success,
    )


# ---------------------------------------------------------------------------
# f-m3-05 — Unanimity gate (label + direction)
# ---------------------------------------------------------------------------


@dataclass
class UnanimityGateResult:
    """Outcome of :func:`unanimity_gate`.

    Attributes
    ----------
    passed:
        ``True`` only when exactly four providers succeeded AND every
        successful provider labelled ``material`` AND every
        successful provider's ``direction`` agrees on a single value
        (all ``bullish`` or all ``bearish``). ``False`` in every other
        case.
    reason:
        Canonical short-circuit reason. ``None`` when ``passed=True``.
        One of:

        * :data:`GATE_REASON_INSUFFICIENT_PROVIDERS` — fewer than
          four providers succeeded; the 4/4 gate cannot be evaluated.
        * :data:`GATE_REASON_UNANIMITY_FAILED` — at least one
          successful provider labelled non-``material``.
        * :data:`GATE_REASON_DIRECTION_SPLIT` — every successful
          provider labelled ``material`` BUT the directions disagree
          (or any direction is missing / ambiguous).
    n_material:
        Count of successful providers whose ``label`` is exactly
        ``"material"``. Always ``<= n_successful_providers``.
    n_successful_providers:
        Count of successful providers in the ensemble result
        (``error is None``). Always ``<= len(ALL_PROVIDERS)``.
    label_histogram:
        ``{label: count}`` mapping over the SUCCESSFUL providers
        only. Faithful per-provider record of the rejected ensemble
        for the audit-log payload (VAL-M3-029). Failed providers are
        not represented here — those surface as
        ``failed_providers`` in :class:`EnsembleEventResult`.
    direction_histogram:
        ``{direction: count}`` mapping over the SUCCESSFUL providers
        only. ``None`` directions surface under the ``"unknown"``
        bucket so audit consumers can see the ambiguity.
    direction:
        Consensus direction (``"bullish"`` / ``"bearish"``) when the
        gate passed. ``None`` whenever the gate did NOT pass — never
        set on a rejected gate (prevents downstream code from
        mistakenly routing on a partial direction).
    """

    passed: bool
    reason: Optional[str]
    n_material: int
    n_successful_providers: int
    label_histogram: dict[str, int]
    direction_histogram: dict[str, int]
    direction: Optional[str]


def unanimity_gate(
    ensemble_result: EnsembleEventResult,
) -> UnanimityGateResult:
    """Evaluate the Stage-2 unanimity (label + direction) gate.

    Parameters
    ----------
    ensemble_result:
        The :class:`EnsembleEventResult` returned by
        :func:`biotech_sniper.llm.ensemble.score_candidate_event`.

    Returns
    -------
    UnanimityGateResult
        Always non-None. Never raises — the gate is a pure consumer.

    Behaviour matrix
    ----------------
    +----------------------+----------------------+-------+----------------------+
    | n_successful         | labels / directions  | pass? | reason               |
    +======================+======================+=======+======================+
    | < 4                  | n/a                  | False | insufficient_providers |
    +----------------------+----------------------+-------+----------------------+
    | 4, any non-material  | n/a                  | False | unanimity_failed     |
    +----------------------+----------------------+-------+----------------------+
    | 4 material, mixed    | bull/bear split or   | False | direction_split      |
    | directions           | any None / ambiguous |       |                      |
    +----------------------+----------------------+-------+----------------------+
    | 4 material, all bull | bullish ×4           | True  | None                 |
    +----------------------+----------------------+-------+----------------------+
    | 4 material, all bear | bearish ×4           | True  | None                 |
    +----------------------+----------------------+-------+----------------------+

    Side effects
    ------------
    None. The gate writes nothing to SQLite, makes no network calls,
    and emits only structured INFO log lines so operators can audit
    the decision path post-hoc.
    """
    successful = [
        r for r in ensemble_result.per_provider_results if r.error is None
    ]
    n_success = len(successful)
    expected = len(ALL_PROVIDERS)

    # Per-provider label / direction histograms — faithful to each
    # provider's actual decision (VAL-M3-029). Computed up-front so
    # every return path can attach them to the audit log.
    label_histogram: dict[str, int] = {}
    direction_histogram: dict[str, int] = {}
    for r in successful:
        lkey = r.label or "unknown"
        label_histogram[lkey] = label_histogram.get(lkey, 0) + 1
        dkey = r.direction or "unknown"
        direction_histogram[dkey] = direction_histogram.get(dkey, 0) + 1

    n_material = label_histogram.get("material", 0)

    if n_success < expected:
        logger.info(
            "stage2_unanimity_gate: skipped %s "
            "(%d of %d providers successful, label_histogram=%r)",
            GATE_REASON_INSUFFICIENT_PROVIDERS,
            n_success,
            expected,
            label_histogram,
        )
        return UnanimityGateResult(
            passed=False,
            reason=GATE_REASON_INSUFFICIENT_PROVIDERS,
            n_material=n_material,
            n_successful_providers=n_success,
            label_histogram=label_histogram,
            direction_histogram=direction_histogram,
            direction=None,
        )

    # All four providers succeeded — check label unanimity first.
    # Strict 4/4 'material'; any other label rejects.
    if n_material != expected:
        logger.info(
            "stage2_unanimity_gate: gate_failed %s "
            "label_histogram=%r (n_material=%d of %d)",
            GATE_REASON_UNANIMITY_FAILED,
            label_histogram,
            n_material,
            expected,
        )
        return UnanimityGateResult(
            passed=False,
            reason=GATE_REASON_UNANIMITY_FAILED,
            n_material=n_material,
            n_successful_providers=n_success,
            label_histogram=label_histogram,
            direction_histogram=direction_histogram,
            direction=None,
        )

    # 4/4 material — now check direction unanimity. A null /
    # ambiguous direction from any material provider counts as a
    # split (VAL-M3-030).
    directions = [r.direction for r in successful]
    if any(d is None for d in directions):
        logger.info(
            "stage2_unanimity_gate: gate_failed %s "
            "direction_histogram=%r (one or more directions missing)",
            GATE_REASON_DIRECTION_SPLIT,
            direction_histogram,
        )
        return UnanimityGateResult(
            passed=False,
            reason=GATE_REASON_DIRECTION_SPLIT,
            n_material=n_material,
            n_successful_providers=n_success,
            label_histogram=label_histogram,
            direction_histogram=direction_histogram,
            direction=None,
        )

    direction_set = set(directions)
    if direction_set == {"bullish"}:
        consensus = "bullish"
    elif direction_set == {"bearish"}:
        consensus = "bearish"
    else:
        logger.info(
            "stage2_unanimity_gate: gate_failed %s "
            "direction_histogram=%r",
            GATE_REASON_DIRECTION_SPLIT,
            direction_histogram,
        )
        return UnanimityGateResult(
            passed=False,
            reason=GATE_REASON_DIRECTION_SPLIT,
            n_material=n_material,
            n_successful_providers=n_success,
            label_histogram=label_histogram,
            direction_histogram=direction_histogram,
            direction=None,
        )

    logger.info(
        "stage2_unanimity_gate: passed "
        "direction=%s n_material=%d label_histogram=%r",
        consensus,
        n_material,
        label_histogram,
    )
    return UnanimityGateResult(
        passed=True,
        reason=None,
        n_material=n_material,
        n_successful_providers=n_success,
        label_histogram=label_histogram,
        direction_histogram=direction_histogram,
        direction=consensus,
    )


# ---------------------------------------------------------------------------
# f-m3-05 — Combined post-fanout gate evaluation (probability → unanimity)
# ---------------------------------------------------------------------------


@dataclass
class PostFanoutGatesResult:
    """Outcome of :func:`evaluate_post_fanout_gates`.

    Attributes
    ----------
    passed:
        ``True`` iff BOTH the probability gate AND the unanimity gate
        passed. ``False`` whenever either gate rejected.
    reason:
        Canonical (first) short-circuit reason in cheap-first order:
        probability_threshold → unanimity. When BOTH gates fail
        simultaneously (e.g. fixture [3M+1I, mean<threshold]) the
        canonical reason is the THRESHOLD failure — VAL-M3-031.
        ``None`` when ``passed=True``. ``insufficient_providers``
        short-circuits before either named gate can be evaluated.
    probability_gate:
        The :class:`ProbabilityGateResult` produced by the
        probability gate. ``None`` only when the structural
        precondition (4 successful providers) was not met.
    unanimity_gate:
        The :class:`UnanimityGateResult` produced by the unanimity
        gate. ``None`` only when the structural precondition (4
        successful providers) was not met. Always populated when
        4 providers succeeded — even when the threshold gate
        already rejected — so the audit log can record both
        failures together.
    """

    passed: bool
    reason: Optional[str]
    probability_gate: Optional[ProbabilityGateResult]
    unanimity_gate: Optional[UnanimityGateResult]


def evaluate_post_fanout_gates(
    ensemble_result: EnsembleEventResult,
    *,
    threshold: Optional[float] = None,
) -> PostFanoutGatesResult:
    """Evaluate the post-fan-out gate sequence in the canonical
    cheap-first order: ``probability_threshold → unanimity``.

    The canonical (first) failure reason is
    :data:`GATE_REASON_PROBABILITY_BELOW_THRESHOLD` when the
    threshold gate fails — even if the unanimity gate ALSO would
    have failed. This implements VAL-M3-031: "the implementation
    MUST NOT swallow the threshold failure under a unanimity
    message".

    Both gate results are returned in :class:`PostFanoutGatesResult`
    so the dispatcher can write a complete audit-log payload (the
    threshold reason as primary, the unanimity reason as secondary
    detail when applicable).

    Parameters
    ----------
    ensemble_result:
        The :class:`EnsembleEventResult` returned by
        :func:`biotech_sniper.llm.ensemble.score_candidate_event`.
    threshold:
        Optional explicit probability threshold (forwarded to
        :func:`probability_gate`). When ``None`` the gate reads
        :func:`config.get_stage2_probability_threshold`.

    Returns
    -------
    PostFanoutGatesResult
        Never raises. Pure consumer of the ensemble result; no DB
        writes, no network calls.
    """
    # Structural short-circuit: the unanimity gate's
    # ``insufficient_providers`` reason is canonical when fewer than
    # four providers succeeded. The threshold gate would also report
    # the same reason, so we surface it once and return.
    successful = [
        r for r in ensemble_result.per_provider_results if r.error is None
    ]
    if len(successful) < len(ALL_PROVIDERS):
        prob_res = probability_gate(ensemble_result, threshold=threshold)
        uni_res = unanimity_gate(ensemble_result)
        logger.info(
            "stage2_post_fanout_gates: short_circuit %s "
            "(n_successful=%d of %d)",
            GATE_REASON_INSUFFICIENT_PROVIDERS,
            len(successful),
            len(ALL_PROVIDERS),
        )
        return PostFanoutGatesResult(
            passed=False,
            reason=GATE_REASON_INSUFFICIENT_PROVIDERS,
            probability_gate=prob_res,
            unanimity_gate=uni_res,
        )

    # Run probability first (canonical cheap-first order, VAL-M3-031).
    prob_res = probability_gate(ensemble_result, threshold=threshold)
    uni_res = unanimity_gate(ensemble_result)

    # Both passed → entry allowed.
    if prob_res.passed and uni_res.passed:
        logger.info(
            "stage2_post_fanout_gates: passed "
            "probability_gate=passed unanimity_gate=passed "
            "mean=%.4f direction=%s",
            prob_res.mean_probability if prob_res.mean_probability is not None else -1.0,
            uni_res.direction,
        )
        return PostFanoutGatesResult(
            passed=True,
            reason=None,
            probability_gate=prob_res,
            unanimity_gate=uni_res,
        )

    # If the threshold gate failed, that is the canonical reason —
    # even if unanimity ALSO failed (VAL-M3-031). The audit log MUST
    # record probability_below_threshold first so operators see the
    # earliest failure in the cheap-first chain.
    if not prob_res.passed:
        # Compose a single audit line that names the canonical
        # (threshold) reason FIRST and the secondary (unanimity)
        # reason AFTER, so a downstream grep on substring ordering
        # confirms the gate sequence.
        secondary_marker = ""
        if not uni_res.passed and uni_res.reason:
            secondary_marker = (
                f" secondary={uni_res.reason} "
                f"label_histogram={uni_res.label_histogram!r}"
            )
        logger.info(
            "stage2_post_fanout_gates: gate_failed %s "
            "mean=%.4f threshold=%.4f%s",
            prob_res.reason,
            prob_res.mean_probability if prob_res.mean_probability is not None else -1.0,
            prob_res.threshold,
            secondary_marker,
        )
        return PostFanoutGatesResult(
            passed=False,
            reason=prob_res.reason,
            probability_gate=prob_res,
            unanimity_gate=uni_res,
        )

    # Threshold passed but unanimity failed.
    logger.info(
        "stage2_post_fanout_gates: gate_failed %s "
        "label_histogram=%r direction_histogram=%r",
        uni_res.reason,
        uni_res.label_histogram,
        uni_res.direction_histogram,
    )
    return PostFanoutGatesResult(
        passed=False,
        reason=uni_res.reason,
        probability_gate=prob_res,
        unanimity_gate=uni_res,
    )


# ---------------------------------------------------------------------------
# f-m3-06 — .armed filesystem gate (cheap-first, pre-fanout)
# ---------------------------------------------------------------------------


@dataclass
class ArmedGateResult:
    """Outcome of :func:`armed_gate`.

    Attributes
    ----------
    passed:
        ``True`` only when the resolved ``.armed`` path is a regular
        readable file (or a symlink to one). ``False`` in every other
        case — including the catch-all "absent" semantics applied to
        dangling symlinks, directories, and mode-000 files.
    reason:
        Canonical short-circuit reason. ``None`` when ``passed=True``;
        :data:`GATE_REASON_ARMED_FILE_MISSING` (``"armed_file_missing"``)
        when ``passed=False``.
    armed_path:
        The resolved ``.armed`` path as a string. Faithfully reports
        whichever path was checked (caller-supplied or, when the kwarg
        was omitted, :data:`biotech_sniper.paths.READING_B_ARMED_FILE`).
        Used by audit-log payloads so operators can reproduce the
        path the gate evaluated.
    """

    passed: bool
    reason: Optional[str]
    armed_path: str


def armed_gate(
    *,
    armed_path: Optional[Union[str, Path]] = None,
) -> ArmedGateResult:
    """Evaluate the Stage-2 ``.armed`` filesystem-marker gate.

    The Stage-2 entry pipeline arms (or disarms) Stage-2 paper-order
    submission via the presence (or absence) of a single filesystem
    marker file at :data:`biotech_sniper.paths.READING_B_ARMED_FILE`.
    The file is created and removed by the operator manually — the
    production code path MUST NEVER write or ``touch`` it (verified
    by ``tests/test_armed_gate.py::test_no_production_writes_to_armed_file``
    and the repo-wide grep evidence of VAL-M3-035).

    Parameters
    ----------
    armed_path:
        Optional explicit path to use INSTEAD of
        :data:`biotech_sniper.paths.READING_B_ARMED_FILE`. Tests pass
        a tmp_path-rooted location to toggle ``.armed`` existence
        without polluting the canonical filesystem location.
        Production callers should omit this kwarg so the canonical
        path is used.

    Returns
    -------
    ArmedGateResult
        Always non-None. Never raises — the gate is a pure consumer
        of the filesystem state. ``OSError`` / ``FileNotFoundError``
        / ``PermissionError`` from the underlying ``stat()`` call are
        all caught and translated to the canonical
        ``armed_file_missing`` rejection.

    Behaviour matrix
    ----------------
    +---------------------------------+--------+----------------------+
    | Resolved target                 | passed | reason               |
    +=================================+========+======================+
    | Regular readable file           | True   | None                 |
    +---------------------------------+--------+----------------------+
    | Symlink → regular readable file | True   | None                 |
    +---------------------------------+--------+----------------------+
    | Path does not exist             | False  | armed_file_missing   |
    +---------------------------------+--------+----------------------+
    | Dangling symlink                | False  | armed_file_missing   |
    +---------------------------------+--------+----------------------+
    | Directory (or symlink → dir)    | False  | armed_file_missing   |
    +---------------------------------+--------+----------------------+
    | Regular file with mode 000      | False  | armed_file_missing   |
    +---------------------------------+--------+----------------------+

    Atomicity
    ---------
    The gate is a single synchronous function. The existence /
    type / readability checks are performed via a SINGLE
    ``os.stat()`` syscall — readability is decided from the
    ``st_mode`` bits returned by that one call, NOT a second
    filesystem probe. There is NO ``os.access`` / ``Path.is_file`` /
    ``Path.exists`` second-probe and NO ``time.sleep``,
    ``await``, ``asyncio.sleep``, or ``Lock.acquire(timeout=...)``
    between any operations. Under the Python GIL the sequence
    ``stat() → S_ISREG → mode-bit check → return`` cannot be
    interrupted by another thread observing an intermediate
    decision, so a TOCTOU race between the gate and the downstream
    submit is structurally impossible. (VAL-M3-036, f-fix-m3-06.)

    Side effects
    ------------
    None. The gate writes nothing to SQLite, makes no network calls,
    and writes nothing to the filesystem. It emits a single
    structured INFO log line so operators can audit the decision.
    """
    # Resolve the path at call time (NOT at module import) so a test
    # that monkeypatches ``biotech_sniper.paths.READING_B_ARMED_FILE``
    # is honoured without re-importing the gate module.
    if armed_path is None:
        target = _paths.READING_B_ARMED_FILE
    else:
        target = armed_path
    target_path = Path(target)

    # Single ``os.stat()`` call (follows symlinks; raises on dangling /
    # missing). All error modes collapse to the canonical
    # ``armed_file_missing`` rejection — see the behaviour matrix in
    # the docstring.
    try:
        st = os.stat(target_path)
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        logger.info(
            "stage2_armed_gate: gate_failed %s "
            "armed_path=%s reason=stat_error",
            GATE_REASON_ARMED_FILE_MISSING,
            target_path,
        )
        return ArmedGateResult(
            passed=False,
            reason=GATE_REASON_ARMED_FILE_MISSING,
            armed_path=str(target_path),
        )

    # Wrong file type — directory, fifo, socket, char/block device.
    # Symlinks were dereferenced by ``os.stat`` already.
    if not _stat.S_ISREG(st.st_mode):
        logger.info(
            "stage2_armed_gate: gate_failed %s "
            "armed_path=%s reason=not_regular_file mode=%o",
            GATE_REASON_ARMED_FILE_MISSING,
            target_path,
            st.st_mode,
        )
        return ArmedGateResult(
            passed=False,
            reason=GATE_REASON_ARMED_FILE_MISSING,
            armed_path=str(target_path),
        )

    # Mode-bit check derived from the SAME ``st`` already in scope —
    # any read bit (owner / group / other) satisfies "readable".
    # We deliberately do NOT call ``os.access(R_OK)`` here:
    #   1. ``os.access`` is a SECOND filesystem syscall, breaking the
    #      single-stat invariant and opening a TOCTOU window between
    #      the initial ``stat`` above and the access probe (the
    #      target path could be replaced in between).
    #   2. ``os.access`` honours the effective uid + ACLs, so when
    #      the daemon runs as root (production VPS condition per
    #      AGENTS.md) a chmod-000 ``.armed`` file would be ACCEPTED
    #      — but the contract behaviour matrix above and VAL-M3-031
    #      explicitly require chmod-000 to be REJECTED regardless of
    #      who runs the daemon. Reading the mode bits from the
    #      already-in-scope ``st`` enforces that invariant uniformly.
    read_bit_set = bool(
        st.st_mode & (_stat.S_IRUSR | _stat.S_IRGRP | _stat.S_IROTH)
    )
    if not read_bit_set:
        logger.info(
            "stage2_armed_gate: gate_failed %s "
            "armed_path=%s reason=not_readable mode=%o",
            GATE_REASON_ARMED_FILE_MISSING,
            target_path,
            st.st_mode,
        )
        return ArmedGateResult(
            passed=False,
            reason=GATE_REASON_ARMED_FILE_MISSING,
            armed_path=str(target_path),
        )

    logger.info(
        "stage2_armed_gate: passed armed_path=%s",
        target_path,
    )
    return ArmedGateResult(
        passed=True,
        reason=None,
        armed_path=str(target_path),
    )


# ---------------------------------------------------------------------------
# f-m3-07 — Daily $ cap gate (cheap-first, pre-fanout)
# ---------------------------------------------------------------------------


class DailyCapExceeded(Exception):
    """Raised by :func:`daily_cap_gate` when ``raise_on_block=True`` AND
    ``today_total + projected_cost > cap``.

    Mirrors :class:`biotech_sniper.liquidity_probe.DailyCapExceeded` —
    callers may opt in to exception-driven control flow when the gate
    is the first short-circuit step in a pipeline that expects an
    exception on cap-hit. The default :func:`daily_cap_gate` mode
    returns a :class:`DailyCapGateResult` (no raise) so the
    Stage-2 dispatcher can compose the cap gate alongside other
    gate dataclasses without a try/except.
    """


@dataclass
class DailyCapGateResult:
    """Outcome of :func:`daily_cap_gate`.

    Attributes
    ----------
    passed:
        ``True`` only when ``today_total_usd + projected_cost <= cap``.
        ``False`` otherwise (with ``reason=GATE_REASON_DAILY_CAP_EXCEEDED``).
    reason:
        Canonical short-circuit reason. ``None`` when ``passed=True``;
        :data:`GATE_REASON_DAILY_CAP_EXCEEDED` (``"daily_cap_exceeded"``)
        when ``passed=False``.
    today_total_usd:
        Snapshotted ``SUM(cost_usd)`` from ``llm_cost_ledger`` rows
        with ``purpose='stage2_event_scoring'`` AND
        ``DATE(called_at)=today``. ``0.0`` when no rows match.
    projected_cost:
        Resolved per-call projected cost used in the boundary check.
        Either the caller-supplied ``projected_cost`` kwarg or, when
        omitted, :data:`STAGE2_CALL_USD_PROJECTION`.
    cap:
        The cap that was applied. Either the caller-supplied ``cap``
        kwarg or, when omitted, :func:`config.get_llm_stage2_daily_usd_cap`.
    db_path:
        The ``db_path`` the cap query ran against. Faithfully reports
        whichever path was used so audit-log payloads can reproduce
        the query.
    """

    passed: bool
    reason: Optional[str]
    today_total_usd: float
    projected_cost: float
    cap: float
    db_path: str = ""


def _today_iso(today: Optional[_dt.date]) -> str:
    """Return the UTC ISO date string for the cap query.

    The cap query keys on ``DATE(called_at)`` in SQLite, which
    interprets the stored string as UTC ISO-8601 per the project
    convention (every cost-ledger writer in
    :mod:`biotech_sniper.llm.*_client` stamps ``called_at`` via
    ``strftime('%Y-%m-%dT%H:%M:%fZ', 'now')`` — ``now`` in SQLite is
    always UTC). The optional ``today`` override lets tests pin a
    specific UTC date without freezing the system clock.
    """
    if today is None:
        return _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    return today.isoformat()


def _query_today_stage2_total(
    db_path: Path,
    today_iso: str,
) -> float:
    """Return today's running Stage-2 ``cost_usd`` from ``llm_cost_ledger``.

    Filters on ``purpose='stage2_event_scoring'`` AND
    ``DATE(called_at)=today`` so debate rows / curated-daily-run rows
    do NOT bleed into the Stage-2 cap (VAL-M3-041).
    """
    conn = _db.connect(db_path)
    try:
        _db.run_migrations(conn)
        row = conn.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0.0) AS total
            FROM llm_cost_ledger
            WHERE purpose = ?
              AND DATE(called_at) = ?
            """,
            (STAGE2_LEDGER_PURPOSE, today_iso),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return 0.0
    try:
        total = row["total"] if isinstance(row, _sqlite3.Row) else row[0]
    except (KeyError, IndexError):
        total = 0.0
    try:
        return float(total or 0.0)
    except (TypeError, ValueError):
        return 0.0


def daily_cap_gate(
    *,
    db_path: Optional[Union[str, Path]] = None,
    projected_cost: Optional[float] = None,
    cap: Optional[float] = None,
    today: Optional[_dt.date] = None,
    raise_on_block: bool = False,
    ticker: Optional[str] = None,
    candidate_event_id: Optional[int] = None,
    news_event_id: Optional[int] = None,
    audit_path: Optional[Union[str, Path]] = None,
) -> DailyCapGateResult:
    """Evaluate the Stage-2 daily $ cap gate (PRE-spend projection).

    Mirrors :func:`biotech_sniper.liquidity_probe.today_probe_spend_usd`
    + cap-pre-check pattern: before any LLM fan-out, sum today's
    Stage-2 ``cost_usd`` rows from ``llm_cost_ledger`` (filtered by
    ``purpose='stage2_event_scoring'`` AND UTC date), add the
    worst-case per-call projection, and refuse to proceed when the
    sum would exceed the cap.

    Parameters
    ----------
    db_path:
        Optional override for the SQLite path. Defaults to
        ``DATA_DIR / "alpha_sniper.db"``.
    projected_cost:
        Optional explicit per-call projection (USD). When ``None`` the
        gate uses :data:`STAGE2_CALL_USD_PROJECTION`.
    cap:
        Optional explicit cap (USD). When ``None`` the gate reads
        :func:`config.get_llm_stage2_daily_usd_cap` at call time so
        an in-process env override (e.g.
        ``LLM_STAGE2_DAILY_USD_CAP=5.0``) takes effect without a
        process restart.
    today:
        Optional override for "today" used in the cap query. Tests
        pass a fixed UTC ``date`` so the boundary test is
        deterministic; production leaves it ``None``.
    raise_on_block:
        When ``True``, the gate raises :class:`DailyCapExceeded` on
        cap-hit instead of returning a :class:`DailyCapGateResult`.
        Provided for callers that prefer exception-driven control
        flow (mirrors :mod:`liquidity_probe`'s pattern). The default
        ``False`` returns a result object so the Stage-2 dispatcher
        can compose gates uniformly.
    ticker, candidate_event_id, news_event_id, audit_path:
        Optional kwargs that, when ``ticker`` AND ``audit_path`` are
        BOTH supplied on a cap-hit, drive the audit-trail
        persistence side effect via :func:`record_stage2_skip` —
        ONE row in ``news_match_log`` (``matched=0``,
        ``reason='daily_cap_exceeded'``) AND a ``stage2_skipped[]``
        merge into ``audit_latest.json``. When either is missing the
        gate emits a single WARNING (``daily_cap_gate: persistence
        skipped — caller did not supply ticker/audit_path``) and
        returns the canonical decision unchanged. The persistence
        write is attempted BEFORE the ``raise_on_block=True`` raise
        path so the audit trail lands even when the caller opts in
        to exception-driven flow control. Recorder errors
        (``sqlite3.Error`` / ``OSError``) are swallowed at WARNING
        level by :func:`record_stage2_skip`, preserving the gate's
        contract-clean return shape on persistence failure.

    Returns
    -------
    DailyCapGateResult
        Always non-None when ``raise_on_block=False``. Pure SELECT —
        no rows are written to ``llm_cost_ledger`` or any other
        table. The audit-trail writes (``news_match_log`` row +
        ``audit_latest.json`` merge) happen separately via
        :func:`record_stage2_skip`.

    Raises
    ------
    DailyCapExceeded
        Only when ``raise_on_block=True`` AND the gate determined
        ``today_total + projected_cost > cap``.

    Side effects
    ------------
    None on the gate itself. The function opens a SQLite connection,
    runs the SUM query, and closes the connection. No INSERTs, no
    UPDATEs, no filesystem writes, no network calls.
    """
    if db_path is None:
        from biotech_sniper.paths import DATA_DIR  # late import per project conv.

        resolved_db = DATA_DIR / "alpha_sniper.db"
    else:
        resolved_db = Path(db_path)
    resolved_projection = (
        float(projected_cost)
        if projected_cost is not None
        else STAGE2_CALL_USD_PROJECTION
    )
    resolved_cap = (
        float(cap)
        if cap is not None
        else config.get_llm_stage2_daily_usd_cap()
    )
    today_iso = _today_iso(today)

    today_total = _query_today_stage2_total(resolved_db, today_iso)

    # Strict ``>`` boundary — equality passes (i.e. spending exactly
    # to the cap is allowed; the next call's projection would block).
    over_cap = (today_total + resolved_projection) > resolved_cap

    if not over_cap:
        logger.info(
            "stage2_daily_cap_gate: passed "
            "today_total=%.4f projected_cost=%.4f cap=%.4f today=%s",
            today_total,
            resolved_projection,
            resolved_cap,
            today_iso,
        )
        return DailyCapGateResult(
            passed=True,
            reason=None,
            today_total_usd=today_total,
            projected_cost=resolved_projection,
            cap=resolved_cap,
            db_path=str(resolved_db),
        )

    logger.warning(
        "stage2_skipped: %s "
        "today_total=%.4f projected_cost=%.4f cap=%.4f today=%s",
        GATE_REASON_DAILY_CAP_EXCEEDED,
        today_total,
        resolved_projection,
        resolved_cap,
        today_iso,
    )

    # ---- Audit-trail persistence (f-fix-m3-07) -------------------
    # Run BEFORE the raise/return-result fork so the audit trail
    # lands even when the caller opts in to ``raise_on_block=True``.
    # Both ``ticker`` AND ``audit_path`` are required for the
    # persistence path; either missing logs a single WARNING and
    # skips ONLY the persistence — the gate decision is preserved.
    if ticker is not None and audit_path is not None:
        record_stage2_skip(
            db_path=resolved_db,
            audit_path=audit_path,
            ticker=ticker,
            candidate_event_id=candidate_event_id,
            news_event_id=news_event_id,
            today_total_usd=today_total,
            projected_cost=resolved_projection,
            cap=resolved_cap,
            reason=GATE_REASON_DAILY_CAP_EXCEEDED,
        )
    else:
        logger.warning(
            "daily_cap_gate: persistence skipped — caller did not "
            "supply ticker/audit_path "
            "(ticker=%r audit_path=%r)",
            ticker,
            audit_path,
        )

    if raise_on_block:
        raise DailyCapExceeded(
            f"stage2_daily_cap_gate: today_total={today_total:.4f} + "
            f"projected_cost={resolved_projection:.4f} > "
            f"cap={resolved_cap:.4f}"
        )

    return DailyCapGateResult(
        passed=False,
        reason=GATE_REASON_DAILY_CAP_EXCEEDED,
        today_total_usd=today_total,
        projected_cost=resolved_projection,
        cap=resolved_cap,
        db_path=str(resolved_db),
    )


def record_stage2_skip(
    *,
    db_path: Union[str, Path],
    audit_path: Union[str, Path],
    ticker: str,
    candidate_event_id: Optional[int] = None,
    news_event_id: Optional[int] = None,
    today_total_usd: float,
    projected_cost: float,
    cap: float,
    reason: str = GATE_REASON_DAILY_CAP_EXCEEDED,
) -> None:
    """Record a Stage-2 cap-hit (or other gate-driven skip) in the
    audit trail.

    Two writes:

    1. **``news_match_log`` row.** Persists a ``matched=0`` row with
       the supplied ``ticker``, optional ``news_event_id`` (so the
       row links back to the source headline when known), and
       ``reason``. The row stamps ``logged_at`` via the table's
       ``DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))`` so audit
       consumers see UTC ISO-8601 timestamps consistent with the
       ledger's ``called_at``.
    2. **``audit_latest.json`` merge.** Reads any existing JSON,
       updates the ``stage2_skipped[]`` block (a list of
       ``{reason, count, ...}`` dicts — one per distinct ``reason``),
       increments the ``count`` for this reason, and writes back
       atomically (``tmp.replace``). Mirrors the
       ``llm_debate.run_debate`` audit-merge convention.

    Parameters
    ----------
    db_path:
        Path to the SQLite db (``alpha_sniper.db``).
    audit_path:
        Path to ``state/audit_latest.json``.
    ticker:
        Ticker symbol of the rejected candidate.
    candidate_event_id:
        Optional FK to ``candidate_events.id``. The ``news_match_log``
        schema does NOT have a ``candidate_event_id`` column today,
        so this kwarg is recorded only in the audit JSON payload.
    news_event_id:
        Optional FK to ``news_events.id`` (the source headline).
        Persisted on the ``news_match_log`` row.
    today_total_usd, projected_cost, cap:
        Snapshot of the cap-gate decision values; persisted in the
        audit JSON payload so operators can reproduce the gate state
        post-hoc.
    reason:
        Skip reason. Defaults to
        :data:`GATE_REASON_DAILY_CAP_EXCEEDED`. Other values
        (``armed_file_missing``, ``ticker_in_cooldown``, ...) may be
        passed by other gates that share this audit hook.

    Side effects
    ------------
    * Inserts one row into ``news_match_log``.
    * Reads + writes ``audit_path`` atomically (creates parent dir
      if missing).

    Errors are caught and logged at WARNING level — the recorder
    must not raise into the dispatcher path because the dispatcher
    has already short-circuited on the gate decision.
    """
    db_target = Path(db_path)
    audit_target = Path(audit_path)
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat() + "Z"

    # ---- news_match_log row -----------------------------------------
    try:
        conn = _db.connect(db_target)
        try:
            _db.run_migrations(conn)
            with conn:
                conn.execute(
                    """
                    INSERT INTO news_match_log (
                        ticker, news_event_id, matched, reason
                    ) VALUES (?, ?, 0, ?)
                    """,
                    (ticker, news_event_id, reason),
                )
        finally:
            conn.close()
    except _sqlite3.Error as exc:
        logger.warning(
            "record_stage2_skip: failed to write news_match_log row "
            "ticker=%s reason=%s: %s",
            ticker,
            reason,
            exc,
        )

    # ---- audit_latest.json merge ------------------------------------
    try:
        audit_target.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if audit_target.is_file():
            try:
                with audit_target.open("r", encoding="utf-8") as fp:
                    loaded = _json.load(fp)
                if isinstance(loaded, dict):
                    existing = loaded
            except (OSError, ValueError):
                existing = {}

        skipped = existing.get("stage2_skipped")
        if not isinstance(skipped, list):
            skipped = []

        # Update or append the entry for this reason.
        entry: Optional[dict] = None
        for s in skipped:
            if isinstance(s, dict) and s.get("reason") == reason:
                entry = s
                break
        if entry is None:
            entry = {"reason": reason, "count": 0}
            skipped.append(entry)
        try:
            entry["count"] = int(entry.get("count") or 0) + 1
        except (TypeError, ValueError):
            entry["count"] = 1
        entry["last_ticker"] = ticker
        entry["last_candidate_event_id"] = (
            int(candidate_event_id) if candidate_event_id is not None else None
        )
        entry["last_news_event_id"] = (
            int(news_event_id) if news_event_id is not None else None
        )
        entry["last_total_usd"] = float(today_total_usd)
        entry["last_projected_cost"] = float(projected_cost)
        entry["last_cap"] = float(cap)
        entry["last_logged_at"] = ts

        existing["stage2_skipped"] = skipped

        tmp = audit_target.with_suffix(audit_target.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fp:
            _json.dump(existing, fp, indent=2, sort_keys=True)
        tmp.replace(audit_target)
    except OSError as exc:
        logger.warning(
            "record_stage2_skip: failed to merge audit_latest.json "
            "ticker=%s reason=%s: %s",
            ticker,
            reason,
            exc,
        )


# ---------------------------------------------------------------------------
# f-m3-08 — Per-ticker 24h cooldown gate (cheap-first, pre-fanout)
# ---------------------------------------------------------------------------


def _canonical_ticker(ticker: str) -> str:
    """Return the canonical UPPERCASE form of ``ticker``.

    The cooldown gate uses ``ticker`` as the PRIMARY KEY of
    ``ticker_cooldown``. Per VAL-M3-083, all writes are
    UPPERCASE-canonicalised so a cooldown set on ``nvax`` and a
    candidate with ``NVAX`` resolve to the same row. Surrounding
    whitespace is stripped defensively.
    """
    return (ticker or "").strip().upper()


def _parse_iso_utc(text: str) -> Optional[_dt.datetime]:
    """Parse an ISO-8601 UTC timestamp from ``ticker_cooldown.last_entry_at``.

    The Reading-B writers stamp timestamps via
    ``strftime('%Y-%m-%dT%H:%M:%fZ', 'now')`` (SQLite's UTC) producing
    e.g. ``2026-04-30T12:00:00.000Z``. We accept that exact shape AND
    Python's ``datetime.isoformat()`` shape (with ``+00:00`` or ``Z``)
    so test fixtures that bypass the SQLite default still work.

    Returns ``None`` if the string cannot be parsed — the gate then
    falls through to "no row" semantics (allow), so a corrupted row
    never wedges Stage-2 entries.
    """
    if not text:
        return None
    s = text.strip()
    # Accept trailing 'Z' as UTC.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


@dataclass
class CooldownGateResult:
    """Outcome of :func:`cooldown_gate`.

    Attributes
    ----------
    passed:
        ``True`` only when no active cooldown blocks entry. ``False``
        when a ``ticker_cooldown`` row exists for the (canonicalised)
        ticker AND ``elapsed_seconds < cooldown_seconds``.
    reason:
        Canonical short-circuit reason. ``None`` when ``passed=True``;
        :data:`GATE_REASON_COOLDOWN_ACTIVE` (``"cooldown_active"``)
        when ``passed=False``.
    ticker:
        Canonical UPPERCASE ticker the gate evaluated against.
    last_entry_at:
        The ``last_entry_at`` value from the row (verbatim DB string),
        or ``None`` when no row exists for this ticker.
    cooldown_hours:
        The cooldown window applied (per-row override when present,
        otherwise the env-default
        :func:`config.get_per_ticker_cooldown_hours`).
    remaining_seconds:
        Number of seconds remaining until the cooldown lifts. ``0``
        when the gate passed; positive when blocked. Computed as
        ``max(0, cooldown_seconds - elapsed_seconds)`` against a
        single ``datetime.now(UTC)`` snapshot.
    """

    passed: bool
    reason: Optional[str]
    ticker: str
    last_entry_at: Optional[str]
    cooldown_hours: int
    remaining_seconds: int


def cooldown_gate(
    *,
    ticker: str,
    db_path: Union[str, Path],
    now: Optional[_dt.datetime] = None,
) -> CooldownGateResult:
    """Evaluate the per-ticker 24h cooldown gate (cheap-first, pre-fanout).

    The Stage-2 dispatcher invokes this gate FIRST in the cheap-first
    chain (cooldown → armed → cap-projection → fan-out → unanimity →
    probability → executor-gates). When a ``ticker_cooldown`` row
    exists for the (canonicalised UPPERCASE) ticker AND
    ``elapsed < cooldown_hours``, the gate rejects with
    ``reason='cooldown_active'`` and the dispatcher short-circuits —
    NO LLM call is dispatched, so ZERO new ``llm_cost_ledger`` rows
    appear.

    Parameters
    ----------
    ticker:
        Candidate ticker (case-insensitive). The gate canonicalises
        to UPPERCASE before any DB lookup so a row written with
        ``nvax`` and a query with ``NVAX`` resolve to the same row.
    db_path:
        Path to the SQLite db (``alpha_sniper.db``). Required because
        the gate is keyed off persistent state — there is no
        in-memory fallback.
    now:
        Optional UTC ``datetime`` override. Tests pin a specific
        clock so the boundary parametrisation is deterministic.
        Production callers omit this kwarg; the gate captures
        ``datetime.now(UTC)`` ONCE per call (VAL-M3-101 invariant:
        single clock per evaluation, no two ``datetime.now()``
        calls).

    Returns
    -------
    CooldownGateResult
        Always non-None. Never raises — DB errors short-circuit to
        "allow" (a corrupted row never wedges Stage-2 entries).

    Behaviour matrix
    ----------------
    +-------------------------------------+--------+----------------------+
    | State                               | passed | reason               |
    +=====================================+========+======================+
    | No row for ticker                   | True   | None                 |
    +-------------------------------------+--------+----------------------+
    | Row, elapsed >= cooldown_seconds    | True   | None                 |
    +-------------------------------------+--------+----------------------+
    | Row, elapsed < cooldown_seconds     | False  | cooldown_active      |
    +-------------------------------------+--------+----------------------+

    The boundary is INCLUSIVE — ``elapsed == cooldown_seconds``
    passes (VAL-M3-101: "allows at exactly cooldown_hours").
    Per-row ``cooldown_hours`` beats the env default (VAL-M3-100).

    Side effects
    ------------
    None. The gate is a pure SELECT — it never INSERTs or UPDATEs
    ``ticker_cooldown``. The row is advanced ONLY via
    :func:`record_cooldown_on_success` (called from the executor on
    successful entry submission). Failed entries (probability /
    unanimity / cap / armed / concurrency rejections) DO NOT advance
    the row (VAL-M3-047).
    """
    canonical = _canonical_ticker(ticker)

    # Single UTC clock snapshot per evaluation (VAL-M3-101).
    if now is None:
        now_utc = _dt.datetime.now(_dt.timezone.utc)
    else:
        now_utc = now if now.tzinfo is not None else now.replace(
            tzinfo=_dt.timezone.utc
        )

    # Resolve the env default once. Per-row cooldown_hours (when
    # present in the queried row) beats this; we only fall back when
    # no row exists.
    env_default_hours = config.get_per_ticker_cooldown_hours()

    # --- DB lookup --------------------------------------------------
    row: Optional[_sqlite3.Row] = None
    try:
        conn = _db.connect(Path(db_path))
        try:
            row = conn.execute(
                """
                SELECT ticker, last_entry_at, last_event_id, cooldown_hours
                FROM ticker_cooldown
                WHERE ticker = ?
                """,
                (canonical,),
            ).fetchone()
        finally:
            conn.close()
    except _sqlite3.Error as exc:
        # Defensive: a missing/corrupt DB short-circuits to "allow"
        # so Stage-2 entries are never wedged by an infrastructure
        # error. The dispatcher's downstream gates will still
        # short-circuit on legitimate failures.
        logger.warning(
            "stage2_cooldown_gate: db_error allowing entry "
            "ticker=%s: %s",
            canonical,
            exc,
        )
        return CooldownGateResult(
            passed=True,
            reason=None,
            ticker=canonical,
            last_entry_at=None,
            cooldown_hours=env_default_hours,
            remaining_seconds=0,
        )

    if row is None:
        logger.info(
            "stage2_cooldown_gate: passed (no row) ticker=%s "
            "cooldown_hours=%d",
            canonical,
            env_default_hours,
        )
        return CooldownGateResult(
            passed=True,
            reason=None,
            ticker=canonical,
            last_entry_at=None,
            cooldown_hours=env_default_hours,
            remaining_seconds=0,
        )

    last_entry_at_str = row["last_entry_at"]
    # Per-row override beats env default (VAL-M3-100).
    try:
        cooldown_hours = int(row["cooldown_hours"])
    except (TypeError, ValueError, IndexError, KeyError):
        cooldown_hours = env_default_hours

    last_entry_at = _parse_iso_utc(last_entry_at_str or "")
    if last_entry_at is None:
        # Unparseable timestamp — defensive allow (matches "no row"
        # semantics rather than wedging the dispatcher).
        logger.warning(
            "stage2_cooldown_gate: unparseable last_entry_at=%r "
            "ticker=%s — allowing entry",
            last_entry_at_str,
            canonical,
        )
        return CooldownGateResult(
            passed=True,
            reason=None,
            ticker=canonical,
            last_entry_at=last_entry_at_str,
            cooldown_hours=cooldown_hours,
            remaining_seconds=0,
        )

    elapsed_seconds = (now_utc - last_entry_at).total_seconds()
    cooldown_seconds = cooldown_hours * 3600

    # >= boundary semantics: elapsed >= cooldown → ALLOW (inclusive).
    if elapsed_seconds >= cooldown_seconds:
        logger.info(
            "stage2_cooldown_gate: passed (expired) ticker=%s "
            "elapsed=%.1fs cooldown=%dh",
            canonical,
            elapsed_seconds,
            cooldown_hours,
        )
        return CooldownGateResult(
            passed=True,
            reason=None,
            ticker=canonical,
            last_entry_at=last_entry_at_str,
            cooldown_hours=cooldown_hours,
            remaining_seconds=0,
        )

    remaining = int(cooldown_seconds - elapsed_seconds)
    if remaining < 0:
        remaining = 0
    logger.info(
        "stage2_cooldown_gate: gate_failed %s ticker=%s "
        "elapsed=%.1fs cooldown=%dh remaining_seconds=%d",
        GATE_REASON_COOLDOWN_ACTIVE,
        canonical,
        elapsed_seconds,
        cooldown_hours,
        remaining,
    )
    return CooldownGateResult(
        passed=False,
        reason=GATE_REASON_COOLDOWN_ACTIVE,
        ticker=canonical,
        last_entry_at=last_entry_at_str,
        cooldown_hours=cooldown_hours,
        remaining_seconds=remaining,
    )


def record_cooldown_on_success(
    *,
    ticker: str,
    db_path: Union[str, Path],
    last_event_id: Optional[int] = None,
    cooldown_hours: Optional[int] = None,
    now: Optional[_dt.datetime] = None,
) -> None:
    """UPSERT a ``ticker_cooldown`` row on successful entry submission.

    The cooldown row is advanced ONLY when a ``news_event_entry``
    paper order is successfully accepted by the executor (VAL-M3-047).
    Failed gate evaluations (probability, unanimity, cap, armed,
    concurrency, executor) MUST NOT call this function — the gate
    record advances the cooldown clock and would block the next
    legitimate retry.

    The function uses ``ON CONFLICT(ticker) DO UPDATE`` so 10
    successive calls on the same ticker resolve to exactly ONE row
    (VAL-M3-070). The PRIMARY KEY is the canonical UPPERCASE ticker,
    so a write with ``nvax`` and a write with ``NVAX`` UPSERT to the
    same row.

    Parameters
    ----------
    ticker:
        Ticker symbol (case-insensitive). Canonicalised to UPPERCASE
        on write (VAL-M3-083).
    db_path:
        Path to the SQLite db.
    last_event_id:
        Optional FK to ``candidate_events.id`` of the candidate that
        produced the entry. Persisted alongside ``last_entry_at`` so
        operators can trace cooldown state back to the originating
        candidate.
    cooldown_hours:
        Optional per-row override. When provided, the row's
        ``cooldown_hours`` column is set to this value (per-row beats
        env default, VAL-M3-100). When omitted, the row inherits the
        column DEFAULT (24) on first insert, and on UPSERT preserves
        whatever value already exists (no clobbering of a prior
        operator-set per-row override).
    now:
        Optional UTC ``datetime`` override. Tests pin a specific
        clock for deterministic timestamps. Production callers omit
        this kwarg; the function captures ``datetime.now(UTC)`` ONCE.

    Side effects
    ------------
    Single UPSERT into ``ticker_cooldown``. No reads, no other
    writes. The function does NOT raise on common DB errors —
    failures are logged at WARNING and swallowed so the executor's
    happy path is not derailed by a cooldown bookkeeping miss
    (the dispatcher's next-call cooldown gate still allows entry,
    which is the safe-default behaviour).
    """
    canonical = _canonical_ticker(ticker)

    if now is None:
        now_utc = _dt.datetime.now(_dt.timezone.utc)
    else:
        now_utc = now if now.tzinfo is not None else now.replace(
            tzinfo=_dt.timezone.utc
        )
    last_entry_at_iso = (
        now_utc.strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{now_utc.microsecond // 1000:03d}Z"
    )

    try:
        conn = _db.connect(Path(db_path))
        try:
            with conn:
                if cooldown_hours is None:
                    # Insert with default hours (24); on conflict, only
                    # bump last_entry_at + last_event_id (preserves any
                    # prior operator-set per-row cooldown_hours override).
                    conn.execute(
                        """
                        INSERT INTO ticker_cooldown
                            (ticker, last_entry_at, last_event_id)
                        VALUES (?, ?, ?)
                        ON CONFLICT(ticker) DO UPDATE SET
                            last_entry_at = excluded.last_entry_at,
                            last_event_id = excluded.last_event_id
                        """,
                        (canonical, last_entry_at_iso, last_event_id),
                    )
                else:
                    # Caller-supplied per-row override — write it on both
                    # insert and update paths.
                    conn.execute(
                        """
                        INSERT INTO ticker_cooldown
                            (ticker, last_entry_at, last_event_id, cooldown_hours)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(ticker) DO UPDATE SET
                            last_entry_at = excluded.last_entry_at,
                            last_event_id = excluded.last_event_id,
                            cooldown_hours = excluded.cooldown_hours
                        """,
                        (
                            canonical,
                            last_entry_at_iso,
                            last_event_id,
                            int(cooldown_hours),
                        ),
                    )
        finally:
            conn.close()
    except _sqlite3.Error as exc:
        logger.warning(
            "record_cooldown_on_success: failed to upsert "
            "ticker=%s last_entry_at=%s: %s",
            canonical,
            last_entry_at_iso,
            exc,
        )
