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
from dataclasses import dataclass
from typing import Optional

from biotech_sniper import config
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
