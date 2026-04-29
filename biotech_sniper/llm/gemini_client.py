"""Gemini 2.5 Pro deep-tier client — biotech catalyst science reasoning.

This module implements :class:`GeminiClient`, a thin wrapper around
the official ``google-genai`` Python SDK. It is the **second deep
tier** of the M2 two-tier LLM stack (alongside
:mod:`biotech_sniper.llm.claude_client`): after the Grok-4 fast tier
ranks the daily universe, the top-N survivors are routed through
both Claude Opus *and* Gemini 2.5 Pro for full biotech-science
reasoning. The two deep-tier outputs are then ensembled by
:mod:`biotech_sniper.unified_scorer` and a divergence flag is raised
when the two models disagree on letter grade by ≥ 2 levels.

Design contract
---------------
* **API key sourcing**: reads ``GEMINI_API_KEY`` exclusively via
  :func:`biotech_sniper.config.get_gemini_api_key`. Direct environment
  lookups are forbidden in this module by mission policy
  (``AGENTS.md`` § "LLM boundaries"). Validators grep this file for
  the standard env-lookup APIs and fail the build if any match is
  found, so this docstring deliberately avoids those literal names.
* **Transport**: the official ``google-genai`` Python SDK (already
  pinned in ``requirements.txt``). The SDK is preferred over a
  hand-rolled HTTP client because Gemini's safety / streaming /
  schema features are non-trivial; the SDK encodes the right defaults
  and tracks the v1 / v1beta API revisions for us.
* **Retry policy**: 3 retries with exponential backoff
  (``0.5 → 1.0 → 2.0`` seconds) on
  :class:`google.genai.errors.ClientError` with HTTP code 429 and on
  any :class:`google.genai.errors.ServerError` (5xx). The fourth
  failure raises :class:`GeminiError`.
* **Fail-fast on auth errors**: raises :class:`GeminiAuthError` on
  the *first* ``ClientError`` whose ``code`` is 401 or 403. No retry
  storm.
* **JSON-mode parsing**: prompt enforces strict JSON output and the
  SDK call sets ``response_mime_type='application/json'`` so the
  service hard-formats the body. If the response body is malformed
  (truncated, prose-wrapped, …) we issue a single deterministic
  re-prompt with stricter instructions before raising
  :class:`GeminiParseError`.
* **Disable-via-feature-flag**: the GeminiClient itself is a thin
  wrapper around the SDK and never auto-runs. The pipeline-level
  enable/disable lives in :func:`biotech_sniper.config.provider_enabled`
  and :data:`biotech_sniper.config.LLM_PROVIDERS`. When
  ``LLM_PROVIDERS["deep"]`` does not include ``"gemini"`` (or the
  ``GEMINI_API_KEY`` is unset), callers in
  :mod:`biotech_sniper.unified_scorer` skip the GeminiClient entirely
  — the module remains importable and the pipeline does not crash.
* **Cost ledger**: every successful call inserts one row into the
  SQLite ``llm_cost_ledger`` table (``provider='gemini'``). Ledger
  failures are swallowed at WARNING so a single sqlite hiccup never
  blocks the scoring pipeline.

Public surface
--------------
* :class:`GeminiClient` — the wrapper class.
* :func:`deep_science_review` — module-level convenience that builds
  a default client and forwards. Used by smoke imports and the
  validation contract (VAL-M2-040).
* :class:`GeminiError`, :class:`GeminiAuthError`,
  :class:`GeminiParseError` — typed exceptions.
* :data:`GEMINI_INPUT_USD_PER_1K`, :data:`GEMINI_OUTPUT_USD_PER_1K`
  — published Gemini 2.5 Pro list pricing (per 1k tokens) used to
  compute ``cost_usd`` per call. Override via
  ``input_price_per_1k`` / ``output_price_per_1k`` if pricing
  changes.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from biotech_sniper import config, db
from biotech_sniper.llm.claude_client import LETTER_GRADE_ORDER
from biotech_sniper.paths import DATA_DIR

__all__ = [
    "GeminiClient",
    "GeminiError",
    "GeminiAuthError",
    "GeminiParseError",
    "deep_science_review",
    "build_deep_science_prompt",
    "DEFAULT_MODEL",
    "DEEP_SCIENCE_SYSTEM_PROMPT",
    "GEMINI_INPUT_USD_PER_1K",
    "GEMINI_OUTPUT_USD_PER_1K",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default Gemini model. Google's flagship deep-reasoning offering as
#: of mission setup. Operators may override per-call via
#: :meth:`GeminiClient.deep_science_review`.
DEFAULT_MODEL: str = "gemini-2.5-pro"

#: Gemini 2.5 Pro published pricing (USD per 1k tokens) for the
#: standard / <=200k-context tier, sourced from Google docs at
#: mission setup. Override via ``input_price_per_1k`` /
#: ``output_price_per_1k`` if list pricing changes upstream.
GEMINI_INPUT_USD_PER_1K: float = 0.00125  # $1.25 / 1M input tokens
GEMINI_OUTPUT_USD_PER_1K: float = 0.010    # $10 / 1M output tokens

#: Maximum number of retries (excluding the first attempt) on
#: 429/5xx and transient network errors.
DEFAULT_MAX_RETRIES: int = 3

#: Base sleep (seconds) for exponential backoff between retries.
#: Effective sleeps: 0.5 → 1.0 → 2.0 seconds for retries 0, 1, 2.
DEFAULT_BACKOFF_BASE: float = 0.5

#: Default per-request timeout (seconds). Deep-tier reasoning over
#: full CT.gov protocol text can run long; 120s leaves headroom
#: without ever masking a hang.
DEFAULT_TIMEOUT: float = 120.0

#: Default ``max_output_tokens`` for Gemini responses. 4096 fits
#: comfortably within 2.5-Pro output budgets and is more than enough
#: for the structured ScienceProfile JSON object.
DEFAULT_MAX_OUTPUT_TOKENS: int = 4096

#: Number of *additional* attempts on malformed JSON output before
#: raising :class:`GeminiParseError`. One re-prompt is sufficient in
#: practice; further retries are dominated by latency, not signal.
JSON_REPROMPT_RETRIES: int = 1


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class GeminiError(Exception):
    """Base class for all Gemini client errors."""


class GeminiAuthError(GeminiError):
    """Raised on Gemini authentication errors — fail-fast, no retries."""


class GeminiParseError(GeminiError):
    """Raised when the model output is not parseable JSON or fails
    structural validation against the deep-science contract (missing
    ``letter_grade``, ``probability`` out of [0,1], ...)."""


# ---------------------------------------------------------------------------
# Prompt scaffolding
# ---------------------------------------------------------------------------

#: System prompt for the deep-science reasoning pass. Mirrors the
#: Claude system prompt so the two deep-tier providers reason against
#: the same biotech-specific cues — divergence between Claude and
#: Gemini under identical instructions is the signal the ensemble
#: ``divergence_flag`` is meant to capture (VAL-M2-045).
DEEP_SCIENCE_SYSTEM_PROMPT: str = (
    "You are a senior biotech clinical-development analyst evaluating "
    "a single catalyst-driven trade thesis. Reason rigorously about "
    "the underlying science before assigning a grade. Pay particular "
    "attention to:\n"
    "  * Mechanism of action (MoA) plausibility for the indication\n"
    "  * Comparator-arm choice and strength (active vs SoC vs placebo)\n"
    "  * Prior Phase 1/2 data quality: effect sizes, durability, "
    "    safety, prior interim looks, p-values\n"
    "  * Indication-specific base rates for the requested phase outcome "
    "    (e.g. ~50%% for P3 oncology pivotal vs ~25%% for AD)\n"
    "  * Sponsor execution / regulatory track record\n"
    "  * Trial design risks: powering, primary endpoint, PRO vs OS, "
    "    open-label vs blinded, alpha spending\n"
    "\n"
    "Return STRICT JSON only. No markdown fences, no prose outside "
    "the JSON object. The JSON must have exactly these top-level keys: "
    "science_profile (object with moa, comparator, prior_phase_data, "
    "base_rate (float in [0,1]), indication, design_risks), "
    "letter_grade (one of A+, A, A-, B+, B, B-, C+, C, C-, D, F), "
    "probability (float in [0,1]), "
    "rationale (3-6 sentence string), "
    "citations (array of {source: str, quote_or_url: str})."
)


def build_deep_science_prompt(
    science_profile: Mapping[str, Any],
    full_context: Mapping[str, Any],
) -> str:
    """Build the user-side prompt for :meth:`GeminiClient.deep_science_review`.

    ``science_profile`` is the partial / preliminary profile assembled
    by :mod:`biotech_sniper.intelligence.trial_science_reader` (MoA
    notes, prior reads, indication, base-rate seed). ``full_context``
    is the larger structured payload — CT.gov protocol excerpts,
    8-Ks, prior NCT readouts, briefing-doc highlights — that Gemini
    reasons over to refine the grade. Both are serialised as JSON so
    the model sees an unambiguous structure rather than free prose.
    """
    payload = {
        "preliminary_science_profile": dict(science_profile),
        "full_context": dict(full_context),
    }
    return (
        "Evaluate the following biotech catalyst thesis. Refine the "
        "preliminary science profile, assign a letter grade, and "
        "estimate the probability of a positive material catalyst.\n\n"
        + json.dumps(payload, sort_keys=True, default=str, indent=2)
        + "\n\nReturn JSON only."
    )


def _strict_reprompt(prompt: str) -> str:
    """Wrap a prompt with a stronger JSON-only directive."""
    return (
        "Your previous response was not valid JSON or was missing "
        "required keys. Return STRICT JSON only — no markdown fences, "
        "no commentary outside the object — that matches the schema "
        "exactly.\n\n"
        + prompt
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GeminiClient:
    """Google GenAI SDK wrapper for biotech deep-science reasoning.

    Parameters
    ----------
    api_key:
        Optional override. ``None`` (the default) reads
        :func:`biotech_sniper.config.get_gemini_api_key`. Mission
        policy forbids any other lookup path.
    model:
        Default Gemini model id. Defaults to :data:`DEFAULT_MODEL`.
    db_path:
        SQLite path for the ``llm_cost_ledger`` row write. Defaults
        to ``DATA_DIR / "alpha_sniper.db"``. Injectable for tests.
    client:
        Optional pre-built :class:`google.genai.Client` instance (or
        any duck-typed substitute exposing ``.models.generate_content``).
        Tests inject a fake client that replays cassettes; production
        callers leave this ``None`` so the wrapper builds a fresh SDK
        client.
    max_retries:
        Number of retries (in addition to the first attempt) on
        429/5xx and transient network errors. Defaults to 3.
    backoff_base:
        Base seconds for the exponential backoff. Default 0.5s
        produces 0.5s / 1.0s / 2.0s sleeps.
    timeout:
        Per-request timeout in seconds. Default 120.0. Forwarded to
        the SDK via ``http_options``.
    max_output_tokens:
        ``max_output_tokens`` parameter forwarded to the Gemini API.
        Defaults to :data:`DEFAULT_MAX_OUTPUT_TOKENS`.
    input_price_per_1k / output_price_per_1k:
        USD pricing per 1k tokens for cost-ledger calculation.
        Defaults to current Gemini 2.5 Pro list prices (standard
        context tier, ≤200k tokens).
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        db_path: Optional[Path] = None,
        client: Optional[Any] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        timeout: float = DEFAULT_TIMEOUT,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        input_price_per_1k: float = GEMINI_INPUT_USD_PER_1K,
        output_price_per_1k: float = GEMINI_OUTPUT_USD_PER_1K,
    ) -> None:
        resolved_key = (
            api_key if api_key is not None else config.get_gemini_api_key()
        )
        if not resolved_key:
            raise GeminiAuthError(
                "GEMINI_API_KEY is not configured. Set it in "
                "/root/alpha_sniper/.env (or repo-root .env locally) "
                "and ensure config.get_gemini_api_key() returns a "
                "non-empty value."
            )
        self._api_key = resolved_key
        self._model = model
        self._db_path: Path = (
            Path(db_path) if db_path is not None else DATA_DIR / "alpha_sniper.db"
        )
        self._max_retries = int(max_retries)
        self._backoff_base = float(backoff_base)
        self._timeout = float(timeout)
        self._max_output_tokens = int(max_output_tokens)
        self._input_price_per_1k = float(input_price_per_1k)
        self._output_price_per_1k = float(output_price_per_1k)

        if client is not None:
            self._client = client
        else:
            # ``timeout`` (in milliseconds) is forwarded via
            # ``http_options`` so the SDK plumbs it through to the
            # underlying ``requests``-based transport.
            #
            # We pass the ``HttpOptionsDict`` (plain dict) form rather
            # than the ``HttpOptions`` dataclass: the dataclass moved
            # between ``google.genai.types`` and the private
            # ``google.genai._api_client`` module across SDK versions
            # (e.g. 0.4.0 only exposes it under the private path),
            # and importing from a private module would tightly
            # couple us to one SDK release. The dict form is
            # documented and accepted across all currently shipping
            # versions — see ``genai.Client.__init__``'s
            # ``http_options: HttpOptions | HttpOptionsDict | None``
            # signature. Dropping this dict (or reaching for the
            # dataclass) is what triggered the f-misc-05 bug:
            # ``module 'google.genai.types' has no attribute
            # 'HttpOptions'`` on every CLI invocation, silently
            # degrading the ensemble to xai+anthropic only even with
            # ``GEMINI_API_KEY`` set.
            self._client = genai.Client(
                api_key=resolved_key,
                http_options={"timeout": int(self._timeout * 1000)},
            )

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        *,
        model: str = DEFAULT_MODEL,
        db_path: Optional[Path] = None,
        client: Optional[Any] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        timeout: float = DEFAULT_TIMEOUT,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        input_price_per_1k: float = GEMINI_INPUT_USD_PER_1K,
        output_price_per_1k: float = GEMINI_OUTPUT_USD_PER_1K,
    ) -> "GeminiClient":
        """Build a :class:`GeminiClient` using the API key from :mod:`config`.

        Mirrors :meth:`biotech_sniper.llm.ensemble.EnsembleScorer.from_config`
        so callers (and the f-misc-05 canary test
        ``tests/test_gemini_client_construction.py``) have a single
        well-known constructor that does not depend on the caller
        knowing the exact key-resolution path.

        Raises :class:`GeminiAuthError` when ``GEMINI_API_KEY`` is
        not configured. Other AttributeError / ImportError raised by
        SDK drift will propagate — this is intentional, the canary
        test asserts the call does not raise on the installed
        google-genai version (so an SDK upgrade that re-breaks
        construction will fail loudly here instead of silently
        degrading the ensemble at run time, as observed in f-m2-22).
        """
        return cls(
            api_key=None,
            model=model,
            db_path=db_path,
            client=client,
            max_retries=max_retries,
            backoff_base=backoff_base,
            timeout=timeout,
            max_output_tokens=max_output_tokens,
            input_price_per_1k=input_price_per_1k,
            output_price_per_1k=output_price_per_1k,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def deep_science_review(
        self,
        science_profile: Mapping[str, Any],
        full_context: Mapping[str, Any],
        *,
        purpose: str = "deep_science",
        model: Optional[str] = None,
    ) -> dict[str, Any]:
        """Run deep biotech-science reasoning over a single thesis.

        Returns a dict with the keys required by the validation
        contract (VAL-M2-041): ``science_profile`` (refined dict),
        ``letter_grade`` (one of :data:`LETTER_GRADE_ORDER`),
        ``probability`` (float in [0,1]), ``rationale`` (str),
        ``citations`` (list[dict]), ``model_id`` (echoed by the API,
        always starts with ``gemini-``), ``latency_ms`` (int >= 0),
        ``cost_usd`` (float >= 0).

        Side effect: appends one row to ``llm_cost_ledger`` with
        ``provider='gemini'``.
        """
        chosen_model = model or self._model
        prompt = build_deep_science_prompt(science_profile, full_context)

        last_parse_error: Optional[GeminiParseError] = None
        response: Any = None
        latency_ms: int = 0
        parsed: dict[str, Any] = {}

        # JSON-mode parse loop: attempt + JSON_REPROMPT_RETRIES re-prompts
        # on malformed bodies. Network retries (429 / 5xx / transient)
        # are handled inside :meth:`_send`.
        for attempt in range(JSON_REPROMPT_RETRIES + 1):
            t_start = time.perf_counter()
            response = self._send(prompt=prompt, model=chosen_model)
            latency_ms = int((time.perf_counter() - t_start) * 1000)

            try:
                parsed = self._parse_response(response)
                break
            except GeminiParseError as exc:
                last_parse_error = exc
                logger.warning(
                    "gemini_client: malformed JSON on attempt %d (%s); "
                    "retrying with stricter directive",
                    attempt,
                    exc,
                )
                prompt = _strict_reprompt(prompt)
        else:
            # Loop exhausted without ``break``.
            assert last_parse_error is not None
            raise last_parse_error

        prompt_tokens, completion_tokens = _extract_usage(response)
        cost_usd = self._compute_cost_usd(prompt_tokens, completion_tokens)
        actual_model_id = _extract_model_id(response, default=chosen_model)
        request_id = _extract_request_id(response)

        result: dict[str, Any] = {
            "science_profile": parsed["science_profile"],
            "letter_grade": parsed["letter_grade"],
            "probability": parsed["probability"],
            "rationale": parsed["rationale"],
            "citations": parsed["citations"],
            "model_id": actual_model_id,
            "latency_ms": latency_ms,
            "cost_usd": cost_usd,
        }

        self._log_cost_row(
            model_id=actual_model_id,
            purpose=purpose,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            request_id=request_id,
        )
        return result

    # ------------------------------------------------------------------
    # SDK invocation with retry
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Public debate-friendly chat helper (f-m2-13 fix #6)
    # ------------------------------------------------------------------

    def chat(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        model: Optional[str] = None,
        purpose: str = "debate",
        json_mode: bool = True,
    ) -> dict[str, Any]:
        """Generic public chat helper that ALWAYS writes a cost-ledger row.

        Used by :mod:`biotech_sniper.llm.llm_debate` for the
        Gemini-side debate rounds so the public API path is the same
        one that handles cost-ledger persistence. Returns a dict with
        ``text``, ``prompt_tokens``, ``completion_tokens``,
        ``cost_usd``, ``latency_ms``, ``model_id``.
        """
        chosen_model = model or self._model
        chosen_system = system if system is not None else DEEP_SCIENCE_SYSTEM_PROMPT
        request_config = genai_types.GenerateContentConfig(
            system_instruction=chosen_system,
            temperature=0.2,
            response_mime_type="application/json" if json_mode else "text/plain",
            max_output_tokens=self._max_output_tokens,
        )
        t_start = time.perf_counter()
        response = self._client.models.generate_content(
            model=chosen_model,
            contents=prompt,
            config=request_config,
        )
        latency_ms = int((time.perf_counter() - t_start) * 1000)

        text = _extract_response_text(response) or ""
        prompt_tokens, completion_tokens = _extract_usage(response)
        cost_usd = self._compute_cost_usd(prompt_tokens, completion_tokens)
        actual_model_id = _extract_model_id(response, default=chosen_model)
        request_id = _extract_request_id(response)

        self._log_cost_row(
            model_id=actual_model_id,
            purpose=purpose,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            request_id=request_id,
        )
        return {
            "text": text,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usd": cost_usd,
            "latency_ms": latency_ms,
            "model_id": actual_model_id,
            "request_id": request_id,
        }

    def _send(self, *, prompt: str, model: str) -> Any:
        """Issue one ``models.generate_content`` call with retry.

        Returns a SDK :class:`GenerateContentResponse` (or duck-typed
        substitute for tests).
        """
        request_config = genai_types.GenerateContentConfig(
            system_instruction=DEEP_SCIENCE_SYSTEM_PROMPT,
            temperature=0.2,
            response_mime_type="application/json",
            max_output_tokens=self._max_output_tokens,
        )

        last_error: Optional[Exception] = None
        attempts_so_far = 0
        max_total = self._max_retries + 1

        while attempts_so_far < max_total:
            try:
                return self._client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=request_config,
                )
            except genai_errors.ClientError as exc:
                code = _coerce_int(getattr(exc, "code", None))
                # 401 / 403 are auth — fail-fast, never retry.
                if code in (401, 403):
                    raise GeminiAuthError(
                        f"Gemini authentication failed (HTTP {code}): {exc!s}"
                    ) from exc
                # 429 is the only retryable client-side status.
                if code == 429:
                    last_error = exc
                else:
                    raise GeminiError(
                        f"Gemini client error (HTTP {code}): {exc!s}"
                    ) from exc
            except genai_errors.ServerError as exc:
                # All 5xx are transient; retry under our backoff.
                last_error = exc

            attempts_so_far += 1
            if attempts_so_far >= max_total:
                break
            self._sleep_backoff(attempts_so_far - 1)

        msg = (
            f"Gemini request failed after {attempts_so_far} attempts "
            f"(max_retries={self._max_retries}): {last_error!r}"
        )
        if isinstance(last_error, GeminiError):
            raise last_error
        raise GeminiError(msg) from last_error

    def _sleep_backoff(self, retry_index: int) -> None:
        """Sleep ``backoff_base * 2**retry_index`` seconds.

        ``retry_index`` is zero-based (first retry = 0), so for the
        default base 0.5s the sleeps are 0.5, 1.0, 2.0 s.
        """
        delay = self._backoff_base * (2 ** max(0, retry_index))
        if delay > 0:
            time.sleep(delay)

    # ------------------------------------------------------------------
    # JSON-mode parsing
    # ------------------------------------------------------------------

    @classmethod
    def _parse_response(cls, response: Any) -> dict[str, Any]:
        """Validate + extract the JSON payload from a Gemini response."""
        text = _extract_response_text(response)
        if not text or not text.strip():
            raise GeminiParseError(
                "Gemini returned an empty content body"
            )
        return cls._parse_assistant_text(text)

    @staticmethod
    def _parse_assistant_text(text: str) -> dict[str, Any]:
        """Parse + validate a JSON-mode response body."""
        cleaned = _strip_markdown_fences(text)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise GeminiParseError(
                f"Gemini content was not valid JSON: {cleaned[:200]!r}"
            ) from exc

        if not isinstance(data, dict):
            raise GeminiParseError(
                f"Gemini JSON-mode payload was not an object: "
                f"{type(data).__name__}"
            )

        # ---- science_profile ------------------------------------------------
        science_profile = data.get("science_profile")
        if not isinstance(science_profile, dict):
            raise GeminiParseError(
                f"Gemini JSON missing/invalid 'science_profile': "
                f"got {type(science_profile).__name__}"
            )

        # ---- letter_grade ---------------------------------------------------
        letter_grade = data.get("letter_grade")
        if (
            not isinstance(letter_grade, str)
            or letter_grade not in LETTER_GRADE_ORDER
        ):
            raise GeminiParseError(
                f"Gemini JSON missing/invalid 'letter_grade': "
                f"got {letter_grade!r}"
            )

        # ---- probability ----------------------------------------------------
        try:
            probability = float(data["probability"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GeminiParseError(
                f"Gemini JSON missing/invalid 'probability': {data!r}"
            ) from exc
        if not 0.0 <= probability <= 1.0:
            raise GeminiParseError(
                f"Gemini 'probability' out of [0,1] range: {probability}"
            )

        # ---- rationale ------------------------------------------------------
        rationale = data.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise GeminiParseError(
                f"Gemini JSON missing/invalid 'rationale': {rationale!r}"
            )

        # ---- citations ------------------------------------------------------
        citations_raw = data.get("citations")
        if not isinstance(citations_raw, list):
            raise GeminiParseError(
                f"Gemini JSON missing/invalid 'citations' list: "
                f"got {type(citations_raw).__name__}"
            )
        citations: list[dict[str, Any]] = []
        for idx, c in enumerate(citations_raw):
            if not isinstance(c, dict):
                raise GeminiParseError(
                    f"Gemini citations[{idx}] is not an object: "
                    f"got {type(c).__name__}"
                )
            source = c.get("source")
            quote_or_url = c.get("quote_or_url") or c.get("quote") or c.get("url")
            if not isinstance(source, str) or not source.strip():
                raise GeminiParseError(
                    f"Gemini citations[{idx}] missing 'source': {c!r}"
                )
            if not isinstance(quote_or_url, str) or not quote_or_url.strip():
                raise GeminiParseError(
                    f"Gemini citations[{idx}] missing 'quote_or_url' "
                    f"(or 'quote'/'url'): {c!r}"
                )
            citations.append({"source": source, "quote_or_url": quote_or_url})

        return {
            "science_profile": science_profile,
            "letter_grade": letter_grade,
            "probability": probability,
            "rationale": rationale,
            "citations": citations,
        }

    # ------------------------------------------------------------------
    # Cost ledger
    # ------------------------------------------------------------------

    def _compute_cost_usd(
        self,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float:
        cost = (
            (prompt_tokens / 1000.0) * self._input_price_per_1k
            + (completion_tokens / 1000.0) * self._output_price_per_1k
        )
        return round(cost, 6)

    def _log_cost_row(
        self,
        *,
        model_id: str,
        purpose: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int,
        cost_usd: float,
        request_id: Optional[str],
    ) -> None:
        """Append one row to ``llm_cost_ledger``.

        Mirrors :meth:`ClaudeClient._log_cost_row`: failures are
        swallowed at WARNING because a cost-ledger hiccup must not
        break the scoring pipeline. The DB is the source of truth for
        cost reconciliation; if the row is missing, downstream
        cap-enforcement simply under-counts (which is conservative,
        not catastrophic).
        """
        try:
            # f-misc-08: ``db.connect`` centralises the parent mkdir
            # for paths under DATA_DIR; no need to mkdir here.
            conn = db.connect(self._db_path)
            try:
                db.run_migrations(conn)
                with conn:
                    conn.execute(
                        """
                        INSERT INTO llm_cost_ledger (
                            provider, model_id, purpose,
                            prompt_tokens, completion_tokens,
                            latency_ms, cost_usd, request_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "gemini",
                            model_id,
                            purpose,
                            int(prompt_tokens),
                            int(completion_tokens),
                            int(latency_ms),
                            float(cost_usd),
                            request_id,
                        ),
                    )
            finally:
                conn.close()
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            logger.warning(
                "gemini_client: failed to log llm_cost_ledger row (%s)",
                exc,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "gemini_client: unexpected error writing cost ledger: %r",
                exc,
            )


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


def deep_science_review(
    science_profile: Mapping[str, Any],
    full_context: Mapping[str, Any],
    **client_kwargs: Any,
) -> dict[str, Any]:
    """One-shot helper: build a default :class:`GeminiClient` and review.

    Useful for ad-hoc scripts and the smoke import in
    :mod:`biotech_sniper.audit`. Production paths should hold a
    long-lived :class:`GeminiClient` so the underlying
    :class:`google.genai.Client` HTTP client can pool connections
    across the top-N candidates list.
    """
    client = GeminiClient(**client_kwargs)
    return client.deep_science_review(science_profile, full_context)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_MARKDOWN_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*(.*?)\s*```\s*$",
    re.DOTALL | re.IGNORECASE,
)


def _strip_markdown_fences(text: str) -> str:
    """Strip a wrapping ```json ... ``` fence if present.

    Models occasionally ignore "no markdown" instructions and wrap
    JSON in a fenced block. Stripping here is more forgiving than
    rejecting the response outright.
    """
    match = _MARKDOWN_FENCE_RE.match(text)
    if match:
        return match.group(1)
    return text


def _extract_response_text(response: Any) -> str:
    """Extract the assistant text body from a Gemini response.

    Supports both real :class:`GenerateContentResponse` objects (which
    expose a ``text`` property concatenating all text parts) and
    duck-typed substitutes used by tests:

    * objects with a ``text`` attribute returning a string,
    * objects with a ``candidates[0].content.parts`` list of parts
      where each part has a ``text`` attribute,
    * dicts shaped like the Gemini REST response.
    """
    # Real SDK response: ``text`` is a property on
    # GenerateContentResponse that joins all text parts.
    text_attr = getattr(response, "text", None)
    if isinstance(text_attr, str):
        return text_attr

    candidates = getattr(response, "candidates", None)
    if candidates is None and isinstance(response, dict):
        candidates = response.get("candidates")
    if not candidates:
        return ""

    first = candidates[0]
    content = getattr(first, "content", None)
    if content is None and isinstance(first, dict):
        content = first.get("content")
    if content is None:
        return ""

    parts = getattr(content, "parts", None)
    if parts is None and isinstance(content, dict):
        parts = content.get("parts")
    if not parts:
        return ""

    chunks: list[str] = []
    for part in parts:
        text = getattr(part, "text", None)
        if text is None and isinstance(part, dict):
            text = part.get("text")
        if isinstance(text, str):
            chunks.append(text)
    return "".join(chunks)


def _extract_usage(response: Any) -> tuple[int, int]:
    """Pull ``(prompt_tokens, candidates_tokens)`` from a Gemini response.

    Tolerant of dict-shaped test doubles. Missing fields default to
    ``0`` so cost-ledger writes never explode on partial responses
    (the DB stores zeros, and reconciliation under-counts safely).
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage_metadata")
    if usage is None:
        return (0, 0)

    def _get(name: str) -> int:
        val = getattr(usage, name, None)
        if val is None and isinstance(usage, dict):
            val = usage.get(name)
        try:
            return int(val) if val is not None else 0
        except (TypeError, ValueError):
            return 0

    return (_get("prompt_token_count"), _get("candidates_token_count"))


def _extract_model_id(response: Any, *, default: str) -> str:
    """Return the model id echoed back by the API, or ``default``."""
    model_version = getattr(response, "model_version", None)
    if model_version is None and isinstance(response, dict):
        model_version = response.get("model_version")
    if isinstance(model_version, str) and model_version.strip():
        return model_version
    return default


def _extract_request_id(response: Any) -> Optional[str]:
    """Return the request id echoed by the API, or ``None``.

    The google-genai SDK does not currently expose a stable request
    id on :class:`GenerateContentResponse`; tests set a synthetic
    ``response_id`` attribute on the fake response so the cost
    ledger row can be cross-referenced.
    """
    rid = getattr(response, "response_id", None)
    if rid is None and isinstance(response, dict):
        rid = response.get("response_id")
    if isinstance(rid, str) and rid.strip():
        return rid
    return None


def _coerce_int(value: Any) -> Optional[int]:
    """Best-effort int coercion (for HTTP-status ``code`` fields)."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
