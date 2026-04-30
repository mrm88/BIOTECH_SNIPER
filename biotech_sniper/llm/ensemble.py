"""LLM ensemble synthesis layer (M2).

This module combines the fast-tier ranking (xAI / Grok-4) with the
deep-tier biotech-science scores (Claude Opus + Gemini 2.5 Pro) into
a single ``ensemble_score`` per candidate ticker.

Design contract
---------------
* **Documented weighting formula** (single source of truth — repeated
  here in code so the validator at
  ``tests/scoring/test_ensemble.py::test_weighting_formula_documented``
  can grep for the exact arithmetic)::

      ensemble_score = w_grok   * grok_score
                     + w_claude * claude_probability
                     + w_gemini * gemini_probability

  The base weights live in :data:`biotech_sniper.config.ENSEMBLE_WEIGHTS`
  and sum to 1.0. If a provider is disabled via
  :data:`biotech_sniper.config.LLM_PROVIDERS` (or its API key is
  missing), its weight is **redistributed proportionally** across the
  remaining active providers so the active weights still sum to 1.0.
  When the deep tier is disabled entirely (``LLM_PROVIDERS["deep"]``
  is empty), ``ensemble_score == grok_score`` (the effective grok
  weight collapses to 1.0).

* **Divergence detection**: when both Claude and Gemini are active
  AND their letter grades differ by ≥ 2 levels in the canonical
  :data:`biotech_sniper.llm.claude_client.LETTER_GRADE_ORDER`,
  ``divergence_flag`` is set to ``True`` and the ensemble result
  carries the reasoned-rationale notes from each model in the
  ``model_breakdown`` payload so downstream consumers (the M2 LLM
  debate loop, the play-card builder) can surface the disagreement
  to the operator.

* **Persistence**: every successful ``score(...)`` call upserts one
  row into the ``scoring_cache`` table keyed on ``(ticker,
  as_of_date)``. The full ``model_breakdown`` is JSON-serialised into
  the ``payload`` column so the scoring history is reproducible.

* **Feature-flag toggles**: any single provider may be disabled
  without crashing the pipeline. The three documented modes
  exercised by the test suite are:

    1. ``all-on`` — Grok + Claude + Gemini (default).
    2. ``fast-only`` — only Grok; deep tier off.
    3. ``anthropic-only-deep`` — Grok + Claude; Gemini disabled.

  Each of these modes still produces a valid
  :class:`EnsembleResult`; ``divergence_flag`` is ``False`` for any
  single-deep-source mode (it requires both deep models to detect
  disagreement).

Public surface
--------------
* :class:`EnsembleScorer` — the orchestrator class.
* :func:`compute_ensemble` — pure function for the weighting math
  (used directly by the divergence / weighting tests).
* :func:`letter_grade_distance` — canonical helper for grade-gap
  computation.
* :class:`EnsembleError` — typed error for orchestration failures.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from biotech_sniper import config, db
from biotech_sniper.llm.claude_client import LETTER_GRADE_ORDER
from biotech_sniper.paths import DATA_DIR

# Imports kept lazy where possible (XAI / Claude / Gemini clients) so
# that this module remains import-safe even when one of the SDKs is
# not installed in the local environment.

__all__ = [
    "EnsembleScorer",
    "EnsembleError",
    "compute_ensemble",
    "letter_grade_distance",
    "DIVERGENCE_THRESHOLD",
    "ALL_PROVIDERS",
    "EnsembleEventResult",
    "ProviderResult",
    "score_candidate_event",
    "STAGE2_FANOUT_MAX_WORKERS",
    "STAGE2_PROVIDER_TIMEOUT_SECONDS",
]


# ---------------------------------------------------------------------------
# Stage-2 (Reading-B) event-driven 4-provider ensemble — feature f-m3-03
# ---------------------------------------------------------------------------

#: Canonical provider order. Stable across the ensemble result so callers
#: (audit JSON writer, dashboard) can assume a deterministic per-row layout.
ALL_PROVIDERS: tuple[str, ...] = ("xai", "anthropic", "gemini", "perplexity")

#: ThreadPoolExecutor cap for the Stage-2 fan-out. The mission-wide
#: AGENTS.md cap is ``max_workers <= 4`` anywhere in the project; the
#: Stage-2 ensemble is the canonical single point that uses 4 (one
#: per provider).
STAGE2_FANOUT_MAX_WORKERS: int = 4

#: Per-provider timeout. Mirrors the per-call HTTP timeout used by the
#: Perplexity client (``DEFAULT_TIMEOUT=20.0``) so the ensemble never
#: blocks longer than the underlying HTTP transport. A hung provider is
#: surfaced as an ``error='timeout: ...'`` :class:`ProviderResult` and
#: excluded from the unanimity / probability gates.
STAGE2_PROVIDER_TIMEOUT_SECONDS: float = 20.0


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Two-grade gap between the deep-tier letter grades flips the
#: divergence flag (per VAL-M2-045 — Claude=A vs Gemini=B- is the
#: canonical divergence example, with a gap of 4 in the canonical
#: ordering).
DIVERGENCE_THRESHOLD: int = 2


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class EnsembleError(Exception):
    """Raised when the ensemble cannot produce a result.

    Distinct from per-client errors (``XAIError`` / ``ClaudeError`` /
    ``GeminiError``); those propagate out of the underlying clients
    and are caught at the orchestration layer to decide whether to
    degrade gracefully or fail loudly.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def letter_grade_distance(a: str, b: str) -> int:
    """Return ``|index(a) - index(b)|`` in :data:`LETTER_GRADE_ORDER`.

    Both inputs MUST be members of :data:`LETTER_GRADE_ORDER`; an
    unknown grade raises :class:`ValueError` so callers receive a
    deterministic failure rather than a silent miscount.
    """
    try:
        ai = LETTER_GRADE_ORDER.index(a)
        bi = LETTER_GRADE_ORDER.index(b)
    except ValueError as exc:
        raise ValueError(
            f"Unknown letter grade: a={a!r}, b={b!r}; "
            f"expected one of {LETTER_GRADE_ORDER}"
        ) from exc
    return abs(ai - bi)


def _normalise_weights(
    base_weights: Mapping[str, float],
    active_providers: Sequence[str],
) -> dict[str, float]:
    """Redistribute ``base_weights`` across ``active_providers``.

    The weights of inactive providers are dropped, then the remaining
    weights are scaled so they sum to 1.0. Returns ``{}`` when there
    are no active providers (caller should treat this as a no-op).
    """
    active = [p for p in active_providers if p in base_weights]
    if not active:
        return {}

    total = sum(float(base_weights[p]) for p in active)
    if total <= 0:
        # Degenerate config — fall back to uniform weighting across the
        # active providers so we always produce a deterministic answer
        # rather than a NaN.
        return {p: 1.0 / len(active) for p in active}

    return {p: float(base_weights[p]) / total for p in active}


def compute_ensemble(
    *,
    grok_score: Optional[float],
    claude_grade: Optional[str],
    claude_probability: Optional[float],
    gemini_grade: Optional[str],
    gemini_probability: Optional[float],
    weights: Optional[Mapping[str, float]] = None,
) -> dict[str, Any]:
    """Pure ensemble math — the documented weighting formula.

    Parameters mirror the columns persisted to ``scoring_cache``:
    ``grok_score`` (fast-tier probability in [0,1]), ``claude_grade``
    + ``claude_probability``, ``gemini_grade`` + ``gemini_probability``.
    Any of the three provider blocks may be ``None``; the helper
    skips them and renormalises ``weights`` over the active set.

    Returns a dict with::

        {
          "fast_score":      <float | None>,
          "deep_score":      <float | None>,   # weighted average of
                                               # the deep-tier probs;
                                               # None when no deep
                                               # tier is active
          "ensemble_score":  <float | None>,   # final weighted score
          "divergence_flag": <bool>,
          "active_weights":  {provider: weight, ...},
        }

    The formula is::

        ensemble_score = sum(w[p] * value[p] for p in active_providers)

    where ``value['grok']`` = ``grok_score``, ``value['claude']`` =
    ``claude_probability``, ``value['gemini']`` =
    ``gemini_probability`` and ``w[p]`` is the renormalised
    proportional weight from :data:`biotech_sniper.config.ENSEMBLE_WEIGHTS`.

    ``divergence_flag`` is ``True`` only when both Claude AND Gemini
    are active AND their letter grades differ by
    ``>= DIVERGENCE_THRESHOLD`` levels in
    :data:`LETTER_GRADE_ORDER`. Any single-deep-source configuration
    yields ``divergence_flag=False`` because there is no second
    opinion to disagree with.
    """
    base_weights = dict(weights) if weights is not None else dict(config.ENSEMBLE_WEIGHTS)

    # Determine which providers are "active" (a value was supplied).
    active: list[str] = []
    values: dict[str, float] = {}

    if grok_score is not None:
        active.append("grok")
        values["grok"] = float(grok_score)
    if claude_probability is not None:
        active.append("claude")
        values["claude"] = float(claude_probability)
    if gemini_probability is not None:
        active.append("gemini")
        values["gemini"] = float(gemini_probability)

    active_weights = _normalise_weights(base_weights, active)

    # Compute the ensemble score over the active providers using the
    # renormalised weights. The single canonical formula:
    #
    #     ensemble_score = w_grok*grok_score
    #                    + w_claude*claude_probability
    #                    + w_gemini*gemini_probability
    #
    # ...with weights renormalised to the active subset.
    if not active_weights:
        ensemble_score: Optional[float] = None
    else:
        ensemble_score = sum(active_weights[p] * values[p] for p in active_weights)

    # Deep-only weighted average (informational; surfaced in the
    # ``EnsembleScorer.score`` payload as ``deep_score``).
    deep_active = [p for p in ("claude", "gemini") if p in active_weights]
    if deep_active:
        deep_weights = _normalise_weights(base_weights, deep_active)
        deep_score: Optional[float] = sum(deep_weights[p] * values[p] for p in deep_active)
    else:
        deep_score = None

    fast_score: Optional[float] = values.get("grok")

    # Divergence detection requires both deep providers to be present.
    divergence_flag = False
    if claude_grade is not None and gemini_grade is not None:
        try:
            divergence_flag = (
                letter_grade_distance(claude_grade, gemini_grade)
                >= DIVERGENCE_THRESHOLD
            )
        except ValueError:
            # Unknown grade — be conservative and don't flag
            # divergence; let the upstream parser surface the bad
            # grade through its own validation path.
            divergence_flag = False

    return {
        "fast_score": fast_score,
        "deep_score": deep_score,
        "ensemble_score": ensemble_score,
        "divergence_flag": bool(divergence_flag),
        "active_weights": active_weights,
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class EnsembleScorer:
    """Run the fast and deep tiers, blend them, and persist to SQLite.

    Parameters
    ----------
    xai_client:
        Optional fast-tier client. When ``None``, the fast tier is
        skipped (this is allowed but unusual — the deep tier alone
        produces an ensemble with ``fast_score=None``).
    claude_client:
        Optional Claude deep-tier client. When ``None``, Claude is
        skipped.
    gemini_client:
        Optional Gemini deep-tier client. When ``None``, Gemini is
        skipped.
    weights:
        Override for the base weights. Defaults to
        :data:`biotech_sniper.config.ENSEMBLE_WEIGHTS`.
    db_path:
        SQLite path for the ``scoring_cache`` upsert. Defaults to
        ``DATA_DIR / "alpha_sniper.db"``. Injectable for tests.
    persist:
        When ``False``, ``score(...)`` skips the SQLite upsert. Used
        by ad-hoc smoke runs and tests that exercise the math
        without exercising the persistence layer.

    Example
    -------
    Default construction wires up all three clients per
    :data:`biotech_sniper.config.LLM_PROVIDERS`::

        scorer = EnsembleScorer.from_config()
        result = scorer.score({
            "ticker": "TESTX",
            "as_of_date": "2025-04-26",
            "fast_context": {...},
            "science_profile": {...},
            "full_context": {...},
        })
    """

    def __init__(
        self,
        *,
        xai_client: Optional[Any] = None,
        claude_client: Optional[Any] = None,
        gemini_client: Optional[Any] = None,
        weights: Optional[Mapping[str, float]] = None,
        db_path: Optional[Path] = None,
        persist: bool = True,
    ) -> None:
        self._xai = xai_client
        self._claude = claude_client
        self._gemini = gemini_client
        self._weights: dict[str, float] = (
            dict(weights) if weights is not None else dict(config.ENSEMBLE_WEIGHTS)
        )
        self._db_path: Path = (
            Path(db_path) if db_path is not None else DATA_DIR / "alpha_sniper.db"
        )
        self._persist = bool(persist)

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        *,
        weights: Optional[Mapping[str, float]] = None,
        db_path: Optional[Path] = None,
        persist: bool = True,
    ) -> "EnsembleScorer":
        """Build a scorer with clients enabled per :data:`LLM_PROVIDERS`.

        Each provider is included only when both:

        * the provider name appears in ``LLM_PROVIDERS["fast"]`` /
          ``LLM_PROVIDERS["deep"]`` (i.e. enabled by config), and
        * its API key is present
          (:func:`biotech_sniper.config.provider_enabled` returns
          ``True``).

        A provider that is configured but key-less is silently
        skipped — the pipeline degrades gracefully rather than
        crashing on startup.
        """
        xai = _maybe_build_xai_client() if config.provider_enabled("xai") else None
        claude = (
            _maybe_build_claude_client()
            if config.provider_enabled("anthropic")
            else None
        )
        gemini = (
            _maybe_build_gemini_client()
            if config.provider_enabled("gemini")
            else None
        )
        return cls(
            xai_client=xai,
            claude_client=claude,
            gemini_client=gemini,
            weights=weights,
            db_path=db_path,
            persist=persist,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        """Run the configured tiers and return the blended result.

        ``candidate`` must contain ``ticker`` and ``as_of_date`` and
        SHOULD contain the per-tier context payloads:

        * ``fast_context`` — passed to ``XAIClient.score_ticker``.
          Defaults to ``{}`` when missing.
        * ``science_profile`` and ``full_context`` — passed to
          ``ClaudeClient.deep_science_review`` and
          ``GeminiClient.deep_science_review``. Default to ``{}``.
        * ``grok_rank`` — optional integer rank from the upstream
          universe sweep; persisted as-is to ``scoring_cache``.

        Returns a dict with ``fast_score``, ``deep_score``,
        ``ensemble_score``, ``divergence_flag``, ``model_breakdown``
        plus echoed identifiers (``ticker``, ``as_of_date``).

        Side effect (when ``persist=True``): upserts one row into
        ``scoring_cache`` keyed on ``(ticker, as_of_date)``.
        """
        ticker = str(candidate.get("ticker") or "").strip()
        if not ticker:
            raise EnsembleError(
                "ensemble.score: candidate is missing 'ticker' field"
            )
        as_of_date = str(candidate.get("as_of_date") or "").strip()
        if not as_of_date:
            raise EnsembleError(
                "ensemble.score: candidate is missing 'as_of_date' field"
            )

        fast_context = dict(candidate.get("fast_context") or {})
        science_profile = dict(candidate.get("science_profile") or {})
        full_context = dict(candidate.get("full_context") or {})
        grok_rank = candidate.get("grok_rank")

        breakdown: dict[str, Any] = {"grok": None, "claude": None, "gemini": None}

        # ---- fast tier -----------------------------------------------------
        grok_score: Optional[float] = None
        if self._xai is not None:
            try:
                grok_result = self._xai.score_ticker(ticker, fast_context)
                grok_score = float(grok_result["probability"])
                breakdown["grok"] = {
                    "score": grok_score,
                    "rationale": grok_result.get("rationale", ""),
                    "confidence": grok_result.get("confidence"),
                    "model_id": grok_result.get("model_id"),
                }
            except Exception as exc:  # noqa: BLE001 - graceful degradation
                logger.warning(
                    "ensemble: xAI fast tier failed for %s (%s); "
                    "degrading to deep-only",
                    ticker,
                    exc,
                )

        # ---- deep tier: claude --------------------------------------------
        claude_grade: Optional[str] = None
        claude_probability: Optional[float] = None
        if self._claude is not None:
            try:
                claude_result = self._claude.deep_science_review(
                    science_profile, full_context
                )
                claude_grade = str(claude_result["letter_grade"])
                claude_probability = float(claude_result["probability"])
                breakdown["claude"] = {
                    "letter_grade": claude_grade,
                    "probability": claude_probability,
                    "rationale": claude_result.get("rationale", ""),
                    "citations": claude_result.get("citations", []),
                    "model_id": claude_result.get("model_id"),
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ensemble: Claude deep tier failed for %s (%s); "
                    "skipping",
                    ticker,
                    exc,
                )

        # ---- deep tier: gemini --------------------------------------------
        gemini_grade: Optional[str] = None
        gemini_probability: Optional[float] = None
        if self._gemini is not None:
            try:
                gemini_result = self._gemini.deep_science_review(
                    science_profile, full_context
                )
                gemini_grade = str(gemini_result["letter_grade"])
                gemini_probability = float(gemini_result["probability"])
                breakdown["gemini"] = {
                    "letter_grade": gemini_grade,
                    "probability": gemini_probability,
                    "rationale": gemini_result.get("rationale", ""),
                    "citations": gemini_result.get("citations", []),
                    "model_id": gemini_result.get("model_id"),
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ensemble: Gemini deep tier failed for %s (%s); "
                    "skipping",
                    ticker,
                    exc,
                )

        ensemble = compute_ensemble(
            grok_score=grok_score,
            claude_grade=claude_grade,
            claude_probability=claude_probability,
            gemini_grade=gemini_grade,
            gemini_probability=gemini_probability,
            weights=self._weights,
        )

        # When divergence_flag=True, surface the dueling rationales at
        # the top level so consumers don't have to dig into
        # ``model_breakdown`` to display them. The structure mirrors
        # the M2 ``llm_debate`` table input contract.
        divergence_notes: Optional[dict[str, Any]] = None
        if ensemble["divergence_flag"]:
            divergence_notes = {
                "claude": (
                    {
                        "letter_grade": claude_grade,
                        "rationale": breakdown["claude"]["rationale"]
                        if breakdown["claude"]
                        else "",
                    }
                ),
                "gemini": (
                    {
                        "letter_grade": gemini_grade,
                        "rationale": breakdown["gemini"]["rationale"]
                        if breakdown["gemini"]
                        else "",
                    }
                ),
            }

        # Pick a canonical "science_grade" to persist alongside the
        # numeric score: prefer Claude when present, fallback to
        # Gemini, else None. Downstream consumers use this for their
        # documented threshold filters.
        science_grade: Optional[str] = claude_grade if claude_grade is not None else gemini_grade

        result: dict[str, Any] = {
            "ticker": ticker,
            "as_of_date": as_of_date,
            "fast_score": ensemble["fast_score"],
            "deep_score": ensemble["deep_score"],
            "ensemble_score": ensemble["ensemble_score"],
            "divergence_flag": ensemble["divergence_flag"],
            "active_weights": ensemble["active_weights"],
            "model_breakdown": breakdown,
            "divergence_notes": divergence_notes,
            "grok_score": grok_score,
            "claude_grade": claude_grade,
            "claude_probability": claude_probability,
            "gemini_grade": gemini_grade,
            "gemini_probability": gemini_probability,
            "science_grade": science_grade,
            "grok_rank": grok_rank,
        }

        if self._persist:
            self._persist_to_scoring_cache(result)

        return result

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist_to_scoring_cache(self, result: Mapping[str, Any]) -> None:
        """Upsert one row into ``scoring_cache`` keyed on (ticker, as_of_date).

        Idempotent on re-runs of the same date because of the ``UNIQUE
        (ticker, as_of_date)`` index in the schema. Failures are
        logged at WARNING and re-raised — unlike the cost-ledger row,
        a missing scoring_cache row would silently break the play
        card builder, so we want loud failures here.
        """
        try:
            # f-misc-08: ``db.connect`` centralises the parent mkdir
            # for paths under DATA_DIR; no need to mkdir here.
            conn = db.connect(self._db_path)
            try:
                db.run_migrations(conn)
                payload_json = json.dumps(
                    {
                        "model_breakdown": result.get("model_breakdown"),
                        "active_weights": result.get("active_weights"),
                        "fast_score": result.get("fast_score"),
                        "deep_score": result.get("deep_score"),
                        "divergence_notes": result.get("divergence_notes"),
                    },
                    sort_keys=True,
                    default=str,
                )
                with conn:
                    conn.execute(
                        """
                        INSERT INTO scoring_cache (
                            ticker, as_of_date,
                            grok_rank, grok_score,
                            claude_grade, claude_probability,
                            gemini_grade, gemini_probability,
                            science_grade,
                            ensemble_score, divergence_flag,
                            payload
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(ticker, as_of_date) DO UPDATE SET
                            grok_rank          = excluded.grok_rank,
                            grok_score         = excluded.grok_score,
                            claude_grade       = excluded.claude_grade,
                            claude_probability = excluded.claude_probability,
                            gemini_grade       = excluded.gemini_grade,
                            gemini_probability = excluded.gemini_probability,
                            science_grade      = excluded.science_grade,
                            ensemble_score     = excluded.ensemble_score,
                            divergence_flag    = excluded.divergence_flag,
                            payload            = excluded.payload
                        """,
                        (
                            result["ticker"],
                            result["as_of_date"],
                            _coerce_int_or_none(result.get("grok_rank")),
                            _coerce_float_or_none(result.get("grok_score")),
                            result.get("claude_grade"),
                            _coerce_float_or_none(result.get("claude_probability")),
                            result.get("gemini_grade"),
                            _coerce_float_or_none(result.get("gemini_probability")),
                            result.get("science_grade"),
                            _coerce_float_or_none(result.get("ensemble_score")),
                            int(bool(result.get("divergence_flag"))),
                            payload_json,
                        ),
                    )
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.error(
                "ensemble: failed to upsert scoring_cache row for "
                "(%s, %s): %s",
                result.get("ticker"),
                result.get("as_of_date"),
                exc,
            )
            raise


# ---------------------------------------------------------------------------
# Internal helpers — lazy client construction
# ---------------------------------------------------------------------------


def _maybe_build_xai_client() -> Optional[Any]:
    """Return a default :class:`XAIClient` or ``None`` on failure.

    Imported lazily so this module remains import-safe in environments
    where the xAI client's transport (``requests``) misbehaves.
    """
    try:
        from biotech_sniper.llm.xai_client import XAIClient

        return XAIClient()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensemble: failed to build XAIClient (%s)", exc)
        return None


def _maybe_build_claude_client() -> Optional[Any]:
    """Return a default :class:`ClaudeClient` or ``None`` on failure."""
    try:
        from biotech_sniper.llm.claude_client import ClaudeClient

        return ClaudeClient()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensemble: failed to build ClaudeClient (%s)", exc)
        return None


def _maybe_build_gemini_client() -> Optional[Any]:
    """Return a default :class:`GeminiClient` or ``None`` on failure."""
    try:
        from biotech_sniper.llm.gemini_client import GeminiClient

        return GeminiClient()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensemble: failed to build GeminiClient (%s)", exc)
        return None


def _coerce_int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ===========================================================================
# Stage-2 (Reading-B) — event-driven 4-provider fan-out (feature f-m3-03)
# ===========================================================================
#
# This block implements ``score_candidate_event(candidate_event_row)`` —
# the entry point Stage-2 uses to score a fresh ``candidate_events`` row
# across all four LLM providers in parallel and persist one
# ``ensemble_scores_event`` row per successful provider call.
#
# Design contract (mirrors validation contract VAL-M3-016 .. VAL-M3-021,
# VAL-M3-076 .. VAL-M3-078, VAL-M3-084, VAL-M3-088):
#
# * Fan-out: ThreadPoolExecutor with ``max_workers=4`` (one per
#   provider). Per-provider 20 s timeout enforced via
#   ``Future.result(timeout=...)``. Hung providers are surfaced as
#   ``error='timeout'`` :class:`ProviderResult`s; the executor's
#   ``ThreadPoolExecutor.shutdown(wait=False, cancel_futures=True)``
#   call lets the run return without joining the slow worker.
#
# * Partial failure tolerated: providers that raise / time out / return
#   malformed payloads are recorded as failed; the ensemble computes
#   over the successful subset. The unanimity gate (caller-side, not
#   in this module) is structurally dependent on having all four
#   results — anything < 4 collapses to ``gate_failed_reason='unanimity'``
#   (or ``insufficient_providers`` when zero providers succeeded).
#
# * Persistence: every successful provider call inserts exactly one
#   row into ``ensemble_scores_event`` keyed by
#   ``UNIQUE (candidate_event_id, provider, run_id)``. Failed providers
#   write zero rows. ``INSERT OR IGNORE`` makes re-runs with the same
#   ``run_id`` idempotent at the SQL layer.
#
# * Idempotent partial recovery (VAL-M3-084): when the ensemble is
#   re-invoked with the same ``run_id`` for the same
#   ``candidate_event_id``, providers that already have a persisted
#   row are skipped (their stored result is hydrated into the return
#   value); only previously-failed / never-called providers are
#   re-invoked. This lets operators retry a partially-failed Stage-2
#   evaluation without paying for the providers that already produced
#   a verdict.
#
# * Null/empty label coercion (VAL-M3-078): when a provider returns
#   HTTP 200 but the parsed ``label`` is ``None`` or whitespace-only,
#   the ensemble coerces it to ``"ambiguous"`` BEFORE persistence and
#   BEFORE unanimity. Direction is left as-returned (the unanimity
#   gate handles direction-divergence separately).


# Provider callable signature: takes a candidate dict, returns a dict
# with keys {label, probability, direction, rationale, citations,
# latency_ms, cost_usd}. Failures must raise (the fan-out converts
# exceptions into per-provider error markers).
ProviderCallable = Callable[..., Mapping[str, Any]]


@dataclass
class ProviderResult:
    """One per-provider entry in :class:`EnsembleEventResult`.

    On success, ``error`` is ``None`` and the score fields are
    populated. On failure (exception raised, timeout, or malformed
    payload), ``error`` is a non-empty string describing the failure
    and the score fields are ``None``.
    """

    provider: str
    label: Optional[str] = None
    probability: Optional[float] = None
    direction: Optional[str] = None
    rationale: Optional[str] = None
    citations: list[Any] = field(default_factory=list)
    latency_ms: Optional[int] = None
    cost_usd: Optional[float] = None
    error: Optional[str] = None
    persisted: bool = False  # True when the row was committed to ensemble_scores_event


@dataclass
class EnsembleEventResult:
    """Return type of :func:`score_candidate_event`.

    * ``per_provider_results`` always contains 4 entries in
      :data:`ALL_PROVIDERS` order, regardless of how many succeeded.
    * ``successful_providers`` / ``failed_providers`` are derived
      lists kept in :data:`ALL_PROVIDERS` order for stable audit
      output.
    * ``label`` and ``direction`` are populated only when the
      4-provider fan-out yields a strict consensus (label='material'
      AND unanimous direction). Otherwise they are ``None`` and the
      caller's unanimity gate handles rejection.
    * ``mean_probability`` is computed over successful providers only
      and is informational when unanimity is not satisfied.
    * ``gate_failed_reason`` is set to ``'insufficient_providers'``
      when zero providers succeeded, or ``'unanimity'`` when the
      4/4 unanimity precondition cannot be checked (any failure
      among the four), or ``None`` when all four returned material.
    """

    candidate_event_id: Optional[int]
    run_id: str
    per_provider_results: list[ProviderResult]
    successful_providers: list[str] = field(default_factory=list)
    failed_providers: list[str] = field(default_factory=list)
    label: Optional[str] = None
    direction: Optional[str] = None
    mean_probability: Optional[float] = None
    label_histogram: dict[str, int] = field(default_factory=dict)
    gate_failed_reason: Optional[str] = None


def _coerce_label(raw: Any) -> str:
    """Coerce a raw provider label to one of {material, immaterial,
    ambiguous}. Null / empty / whitespace-only → ``ambiguous``."""
    if raw is None:
        return "ambiguous"
    if not isinstance(raw, str):
        return "ambiguous"
    stripped = raw.strip()
    if not stripped:
        return "ambiguous"
    lower = stripped.lower()
    if lower in ("material", "immaterial", "ambiguous"):
        return lower
    # Unknown label string → ambiguous (defensive; the schema CHECK
    # would otherwise reject the row at INSERT time).
    return "ambiguous"


def _coerce_direction(raw: Any) -> Optional[str]:
    """Coerce a provider direction to ``bullish``/``bearish`` or ``None``."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    if s in ("bullish", "bearish"):
        return s
    return None


def _safe_citations_json(value: Any) -> str:
    """Serialise the citations field to a stable JSON string. Defensive
    — non-list / unserialisable values become ``'[]'``."""
    if value is None:
        return "[]"
    try:
        if isinstance(value, str):
            # If already a JSON string, validate by parse-then-dump.
            parsed = json.loads(value)
            return json.dumps(parsed, ensure_ascii=False)
        return json.dumps(list(value), ensure_ascii=False)
    except (TypeError, ValueError):
        return "[]"


def _normalise_provider_payload(
    provider: str, raw: Mapping[str, Any]
) -> ProviderResult:
    """Convert a provider's raw return dict into a :class:`ProviderResult`."""
    label = _coerce_label(raw.get("label"))
    probability = _coerce_float_or_none(raw.get("probability"))
    if probability is not None:
        # Clamp to [0,1] defensively so the schema CHECK never trips on
        # an off-by-epsilon float.
        if probability < 0.0:
            probability = 0.0
        elif probability > 1.0:
            probability = 1.0
    direction = _coerce_direction(raw.get("direction"))
    rationale = raw.get("rationale")
    if rationale is not None and not isinstance(rationale, str):
        rationale = str(rationale)
    citations = raw.get("citations")
    if not isinstance(citations, list):
        citations = []
    latency_ms = _coerce_int_or_none(raw.get("latency_ms"))
    cost_usd = _coerce_float_or_none(raw.get("cost_usd"))
    return ProviderResult(
        provider=provider,
        label=label,
        probability=probability,
        direction=direction,
        rationale=rationale,
        citations=list(citations),
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        error=None,
    )


def _hydrate_existing_rows(
    conn: sqlite3.Connection,
    candidate_event_id: int,
    run_id: str,
) -> dict[str, ProviderResult]:
    """Return ``{provider: ProviderResult}`` for already-persisted rows.

    Used by the partial-recovery path so a re-call with the same
    ``run_id`` does NOT re-invoke providers that already produced a
    verdict.
    """
    out: dict[str, ProviderResult] = {}
    rows = conn.execute(
        "SELECT provider, label, probability, direction, "
        "rationale, citations, latency_ms, cost_usd "
        "FROM ensemble_scores_event "
        "WHERE candidate_event_id=? AND run_id=?",
        (candidate_event_id, run_id),
    ).fetchall()
    for row in rows:
        prov = row["provider"] if isinstance(row, sqlite3.Row) else row[0]
        if prov not in ALL_PROVIDERS:
            continue
        try:
            citations = json.loads(
                row["citations"] if isinstance(row, sqlite3.Row) else row[5]
            ) if (
                row["citations"] if isinstance(row, sqlite3.Row) else row[5]
            ) else []
        except (TypeError, ValueError):
            citations = []
        out[prov] = ProviderResult(
            provider=prov,
            label=row["label"] if isinstance(row, sqlite3.Row) else row[1],
            probability=_coerce_float_or_none(
                row["probability"] if isinstance(row, sqlite3.Row) else row[2]
            ),
            direction=row["direction"] if isinstance(row, sqlite3.Row) else row[3],
            rationale=row["rationale"] if isinstance(row, sqlite3.Row) else row[4],
            citations=citations if isinstance(citations, list) else [],
            latency_ms=_coerce_int_or_none(
                row["latency_ms"] if isinstance(row, sqlite3.Row) else row[6]
            ),
            cost_usd=_coerce_float_or_none(
                row["cost_usd"] if isinstance(row, sqlite3.Row) else row[7]
            ),
            error=None,
            persisted=True,
        )
    return out


def _persist_provider_row(
    conn: sqlite3.Connection,
    *,
    candidate_event_id: int,
    run_id: str,
    result: ProviderResult,
) -> None:
    """INSERT OR IGNORE one row into ``ensemble_scores_event``.

    The UNIQUE(candidate_event_id, provider, run_id) clause makes the
    write idempotent on a same-run re-invocation. Failures (e.g. FK
    violation because the candidate is gone) are re-raised so the
    caller's transaction rolls back atomically.
    """
    citations_json = _safe_citations_json(result.citations)
    called_at = _now_isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO ensemble_scores_event ("
        "candidate_event_id, provider, run_id, label, probability, "
        "direction, rationale, citations, latency_ms, cost_usd, called_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(candidate_event_id),
            result.provider,
            run_id,
            result.label,
            result.probability,
            result.direction,
            result.rationale,
            citations_json,
            result.latency_ms,
            result.cost_usd,
            called_at,
        ),
    )


def _now_isoformat() -> str:
    """UTC ISO-8601 timestamp matching the rest of the project."""
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .replace(tzinfo=None)
        .isoformat(timespec="milliseconds")
        + "Z"
    )


def _resolve_default_providers() -> dict[str, ProviderCallable]:
    """Construct the default Stage-2 provider adapter map.

    Each adapter is a small wrapper around the existing client that
    converts the per-client return shape into the unified
    ``{label, probability, direction, rationale, citations,
    latency_ms, cost_usd}`` shape consumed by the fan-out.

    The xAI / Claude / Gemini clients do not natively produce a
    ``label`` / ``direction`` field (they were originally built for
    daily-curated ranking). For the Reading-B Stage-2 entry path the
    canonical adapter calls into a forthcoming
    ``score_candidate_event`` method on each client (see f-m3-04 +
    onwards). When such a method is missing on a given client, the
    ensemble falls back to a NotImplementedError-marked stub that
    surfaces as ``error='not_implemented'`` so partial recovery on
    later re-calls picks up only the providers that DO have an
    adapter wired.

    Tests inject explicit stubs via the ``providers=...`` kwarg so
    this default path is only exercised by the production code path
    (which in turn is gated by f-m3-04 / f-m3-05 / f-m3-06 wiring).
    """
    providers: dict[str, ProviderCallable] = {}
    for name in ALL_PROVIDERS:
        providers[name] = _make_default_provider_stub(name)
    return providers


def _make_default_provider_stub(name: str) -> ProviderCallable:
    """Return a placeholder adapter that raises a typed
    ``NotImplementedError`` with a descriptive message.

    The Stage-2 wiring in subsequent f-m3 features replaces these
    with concrete adapters that call into the existing
    ``XAIClient.score_candidate_event`` / ``ClaudeClient.score_candidate_event``
    / ``GeminiClient.score_candidate_event`` /
    ``PerplexityClient.score_candidate`` methods.
    """

    def _stub(candidate, *, name=name):  # noqa: ARG001
        raise NotImplementedError(
            f"Stage-2 adapter for provider {name!r} is not wired in this "
            "release. Pass a concrete provider callable via the "
            "`providers=` kwarg to `score_candidate_event(...)` or supply "
            "the adapter via the f-m3-04 wiring."
        )

    return _stub


def score_candidate_event(
    candidate_event_row: Mapping[str, Any],
    *,
    run_id: Optional[str] = None,
    db_path: Optional[Path] = None,
    providers: Optional[Mapping[str, ProviderCallable]] = None,
    timeout_seconds: float = STAGE2_PROVIDER_TIMEOUT_SECONDS,
    persist: bool = True,
) -> EnsembleEventResult:
    """Score a Stage-2 ``candidate_events`` row against the 4-provider ensemble.

    Parameters
    ----------
    candidate_event_row:
        Mapping containing at least ``id`` (the ``candidate_events``
        primary key). May also contain ``ticker`` and any other
        contextual fields the providers want to inspect; the ensemble
        forwards the entire mapping into each provider callable.
    run_id:
        Optional caller-supplied run identifier. When ``None`` a
        UUID4 is generated. Keep this stable across retries to enable
        the partial-recovery semantics described in VAL-M3-084.
    db_path:
        SQLite path for ``ensemble_scores_event`` writes. Defaults to
        ``DATA_DIR / "alpha_sniper.db"``. Tests typically inject a
        ``tmp_path`` db.
    providers:
        Mapping ``{provider_name: callable}`` overriding the default
        production adapters. Tests inject deterministic stubs here.
        Missing keys fall back to the default adapter (which raises
        ``NotImplementedError`` until f-m3-04 wires real adapters).
    timeout_seconds:
        Per-provider timeout. Defaults to 20 s
        (:data:`STAGE2_PROVIDER_TIMEOUT_SECONDS`). Tests pass a
        smaller value to exercise the timeout path quickly.
    persist:
        When ``False`` the function still computes the ensemble but
        does NOT write to ``ensemble_scores_event``. Used by smoke
        runs and tests that want to verify the math layer without
        the persistence layer.

    Returns
    -------
    EnsembleEventResult
        Always non-None. ``MUST NOT raise`` when all providers fail —
        the all-fail path returns with
        ``gate_failed_reason='insufficient_providers'`` so the
        caller can short-circuit downstream gates without a
        try/except.
    """
    candidate_event_id = candidate_event_row.get("id")
    if isinstance(candidate_event_id, str):
        try:
            candidate_event_id = int(candidate_event_id)
        except (TypeError, ValueError):
            candidate_event_id = None
    resolved_run_id = run_id if run_id else f"run-{uuid.uuid4().hex}"
    resolved_db_path: Path = (
        Path(db_path) if db_path is not None else DATA_DIR / "alpha_sniper.db"
    )
    provider_map: dict[str, ProviderCallable] = {
        **_resolve_default_providers(),
        **(dict(providers) if providers else {}),
    }

    # Hydrate any already-persisted rows for this (candidate, run_id).
    existing: dict[str, ProviderResult] = {}
    if persist and candidate_event_id is not None:
        try:
            conn = db.connect(resolved_db_path)
            try:
                existing = _hydrate_existing_rows(
                    conn, int(candidate_event_id), resolved_run_id
                )
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.warning(
                "score_candidate_event: failed to hydrate existing rows "
                "for candidate_event_id=%s run_id=%s: %s",
                candidate_event_id,
                resolved_run_id,
                exc,
            )
            existing = {}

    # Determine which providers still need to be invoked.
    providers_to_call = [p for p in ALL_PROVIDERS if p not in existing]

    # Fan-out to remaining providers.
    new_results: dict[str, ProviderResult] = {}
    if providers_to_call:
        # Cap workers at min(len(providers_to_call), STAGE2_FANOUT_MAX_WORKERS)
        # so a partial-recovery run with one missing provider doesn't
        # spawn 4 idle workers.
        workers = min(len(providers_to_call), STAGE2_FANOUT_MAX_WORKERS)
        executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="stage2-fanout",
        )
        futures: dict[str, Future[Any]] = {}
        try:
            for name in providers_to_call:
                callable_ = provider_map.get(name)
                if callable_ is None:
                    callable_ = _make_default_provider_stub(name)
                t_start = time.perf_counter()
                fut = executor.submit(
                    _run_one_provider,
                    name=name,
                    callable_=callable_,
                    candidate=dict(candidate_event_row),
                    t_start=t_start,
                )
                futures[name] = fut

            # Wait with per-provider timeout. We use a single overall
            # ``wait`` budget equal to ``timeout_seconds + small slack``
            # because ``Future.result(timeout=...)`` raises
            # ``concurrent.futures.TimeoutError`` per-future and
            # leaves the underlying worker running. Since the worker
            # itself imposes a 20 s HTTP timeout in production, this
            # is fine; for tests the worker is a stub that may sleep
            # arbitrarily, so we explicitly use ``cancel_futures=True``
            # in the shutdown call below.
            for name, fut in futures.items():
                try:
                    pr = fut.result(timeout=timeout_seconds)
                except (TimeoutError, FuturesTimeoutError) as exc:  # type: ignore[misc]
                    pr = ProviderResult(
                        provider=name,
                        error=f"timeout: {exc!r}",
                    )
                except Exception as exc:  # noqa: BLE001 - all failures captured
                    pr = ProviderResult(
                        provider=name,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                new_results[name] = pr
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    # Merge existing + new results into the canonical-order list.
    per_provider_results: list[ProviderResult] = []
    for name in ALL_PROVIDERS:
        if name in existing:
            per_provider_results.append(existing[name])
        elif name in new_results:
            per_provider_results.append(new_results[name])
        else:
            per_provider_results.append(
                ProviderResult(
                    provider=name,
                    error="no_callable_supplied",
                )
            )

    # Persist rows for newly-successful providers.
    if persist and candidate_event_id is not None:
        successful_new = [
            pr for name, pr in new_results.items() if pr.error is None
        ]
        if successful_new:
            try:
                conn = db.connect(resolved_db_path)
                try:
                    with conn:
                        for pr in successful_new:
                            _persist_provider_row(
                                conn,
                                candidate_event_id=int(candidate_event_id),
                                run_id=resolved_run_id,
                                result=pr,
                            )
                            pr.persisted = True
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                logger.error(
                    "score_candidate_event: persistence failed for "
                    "candidate_event_id=%s run_id=%s: %s",
                    candidate_event_id,
                    resolved_run_id,
                    exc,
                )
                # Surface the failure on the affected providers so the
                # caller can see the rows were not written.
                for pr in successful_new:
                    if not pr.persisted:
                        pr.error = pr.error or f"persistence_failed: {exc!r}"

    # Aggregate.
    successful = [r for r in per_provider_results if r.error is None]
    failed = [r for r in per_provider_results if r.error is not None]
    successful_providers = [r.provider for r in successful]
    failed_providers = [r.provider for r in failed]

    label_histogram: dict[str, int] = {}
    for r in successful:
        label_histogram[r.label or "unknown"] = (
            label_histogram.get(r.label or "unknown", 0) + 1
        )

    mean_probability: Optional[float]
    if successful:
        probs = [r.probability for r in successful if r.probability is not None]
        mean_probability = (sum(probs) / len(probs)) if probs else None
    else:
        mean_probability = None

    # Gate evaluation — this module only resolves the structural
    # fan-out gates (sufficient_providers + unanimity skeleton). The
    # caller's M3.GATE.PROB / M3.GATE.UNANIMITY layer makes the final
    # entry decision and routes the audit log.
    gate_failed_reason: Optional[str] = None
    consensus_label: Optional[str] = None
    consensus_direction: Optional[str] = None

    if not successful:
        gate_failed_reason = "insufficient_providers"
    elif len(successful) < len(ALL_PROVIDERS):
        # Any partial failure trips the unanimity precondition (4/4
        # required). The probability gate is structurally dependent
        # on having all four results — VAL-M3-025.
        gate_failed_reason = "unanimity"
    else:
        # All four returned. Check label / direction unanimity.
        labels = {r.label for r in successful}
        if labels == {"material"}:
            directions = {r.direction for r in successful}
            if directions == {"bullish"}:
                consensus_label = "material"
                consensus_direction = "bullish"
            elif directions == {"bearish"}:
                consensus_label = "material"
                consensus_direction = "bearish"
            else:
                gate_failed_reason = "direction_split"
                consensus_label = "material"
        else:
            gate_failed_reason = "unanimity"

    return EnsembleEventResult(
        candidate_event_id=int(candidate_event_id) if candidate_event_id else None,
        run_id=resolved_run_id,
        per_provider_results=per_provider_results,
        successful_providers=successful_providers,
        failed_providers=failed_providers,
        label=consensus_label,
        direction=consensus_direction,
        mean_probability=mean_probability,
        label_histogram=label_histogram,
        gate_failed_reason=gate_failed_reason,
    )


def _run_one_provider(
    *,
    name: str,
    callable_: ProviderCallable,
    candidate: Mapping[str, Any],
    t_start: float,
) -> ProviderResult:
    """Worker body executed inside the ThreadPoolExecutor.

    Calls ``callable_(candidate, name=name)`` and normalises the
    return payload into a :class:`ProviderResult`. Any exception
    propagates so the caller's ``Future.result()`` handler can
    convert it to ``error=...``.

    Latency is measured here when the callable does not provide a
    ``latency_ms`` field of its own (the production adapters DO
    include a precise per-HTTP latency; tests typically don't).
    """
    raw = callable_(candidate, name=name)  # type: ignore[call-arg]
    if not isinstance(raw, Mapping):
        raise TypeError(
            f"Stage-2 provider {name!r} returned non-mapping payload: "
            f"{type(raw).__name__}"
        )
    pr = _normalise_provider_payload(name, raw)
    if pr.latency_ms is None:
        pr.latency_ms = max(0, int((time.perf_counter() - t_start) * 1000))
    return pr
