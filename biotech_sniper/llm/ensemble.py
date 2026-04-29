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
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

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
]


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
