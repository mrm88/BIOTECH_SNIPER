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

import logging
import os
import stat as _stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from biotech_sniper import config
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
    type / readability checks are performed within one call with
    NO ``time.sleep``, ``await``, ``asyncio.sleep``, or
    ``Lock.acquire(timeout=...)`` between them. Under the Python GIL
    the sequence ``stat() → S_ISREG → access(R_OK) → return`` cannot
    be interrupted by another thread observing an intermediate
    decision, so a TOCTOU race between the gate and the downstream
    submit is structurally impossible. (VAL-M3-036.)

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

    # Mode 000 (or any mode that strips the read bit for the running
    # uid) — treated as absent per the f-m3-06 contract. ``os.access``
    # honours the effective uid + ACLs, so root bypasses this branch
    # (which is acceptable: on the VPS the daemon runs as root and
    # the operator's intent is "any readable .armed file = armed").
    if not os.access(target_path, os.R_OK):
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
