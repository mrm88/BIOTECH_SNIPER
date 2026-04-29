"""Claude Opus deep-tier client — biotech catalyst science reasoning.

This module implements :class:`ClaudeClient`, a thin wrapper around
the official ``anthropic`` Python SDK. It is the **deep tier** of the
M2 two-tier LLM stack: after the Grok-4 fast tier ranks the daily
universe, the top-N survivors are routed through Claude Opus for full
biotech-science reasoning over CT.gov protocol text, prior Phase 1/2
readouts, sponsor 8-Ks and briefing-doc highlights.

Design contract
---------------
* **API key sourcing**: reads ``ANTHROPIC_API_KEY`` exclusively via
  :func:`biotech_sniper.config.get_anthropic_api_key`. Direct
  environment lookups are forbidden in this module by mission policy
  (``AGENTS.md`` § "LLM boundaries"). Validators grep this file for
  the standard env-lookup APIs and fail the build if any match is
  found, so this docstring deliberately avoids those literal names.
* **Transport**: the official ``anthropic`` Python SDK (already
  pinned in ``requirements.txt``). The SDK is preferred over a
  hand-rolled HTTP client because Anthropic streaming and retry
  semantics are non-trivial; the SDK encodes the right defaults and
  tracks API revisions for us.
* **Retry policy**: 3 retries with exponential backoff
  (``0.5 → 1.0 → 2.0`` seconds) on ``RateLimitError`` (429) and
  ``APIStatusError`` (5xx), plus :class:`anthropic.APIConnectionError`
  / :class:`anthropic.APITimeoutError`. The fourth failure raises
  :class:`ClaudeError`. The SDK's own internal retries are disabled
  (``max_retries=0``) so we own the policy end-to-end.
* **Fail-fast on auth errors**: raises :class:`ClaudeAuthError` on
  the *first* :class:`anthropic.AuthenticationError`. No retry storm.
* **JSON-mode parsing**: prompt enforces strict JSON output. If the
  response body is malformed (truncated, prose-wrapped, …) we issue a
  single deterministic re-prompt with stricter instructions before
  raising :class:`ClaudeParseError`.
* **Streaming reassembly**: when ``use_streaming=True`` the SDK
  ``messages.stream()`` API is used and text chunks are reassembled
  into the final payload. Default is non-streaming because biotech
  rationales fit comfortably under ``max_tokens`` and the streaming
  surface is more brittle to test.
* **Cost ledger**: every successful call inserts one row into the
  SQLite ``llm_cost_ledger`` table (``provider='anthropic'``). Ledger
  failures are swallowed at WARNING so a single sqlite hiccup never
  blocks the scoring pipeline.

Public surface
--------------
* :class:`ClaudeClient` — the wrapper class.
* :func:`deep_science_review` — module-level convenience that builds
  a default client and forwards. Used by smoke imports and the
  validation contract (VAL-M2-035).
* :class:`ClaudeError`, :class:`ClaudeAuthError`,
  :class:`ClaudeParseError` — typed exceptions.
* :data:`CLAUDE_INPUT_USD_PER_1K`, :data:`CLAUDE_OUTPUT_USD_PER_1K`
  — published Claude Opus list pricing (per 1k tokens) used to
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

import anthropic

from biotech_sniper import config, db
from biotech_sniper.paths import DATA_DIR

__all__ = [
    "ClaudeClient",
    "ClaudeError",
    "ClaudeAuthError",
    "ClaudeParseError",
    "deep_science_review",
    "build_deep_science_prompt",
    "DEFAULT_MODEL",
    "DEEP_SCIENCE_SYSTEM_PROMPT",
    "CLAUDE_INPUT_USD_PER_1K",
    "CLAUDE_OUTPUT_USD_PER_1K",
    "LETTER_GRADE_ORDER",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default Claude model. Anthropic's flagship deep-reasoning offering
#: as of mission setup. Operators may override per-call via
#: :meth:`ClaudeClient.deep_science_review`.
DEFAULT_MODEL: str = "claude-opus-4-1-20250805"

#: Claude Opus published pricing (USD per 1k tokens) sourced from
#: Anthropic docs at mission setup. Override via
#: ``input_price_per_1k`` / ``output_price_per_1k`` if the list price
#: changes upstream.
CLAUDE_INPUT_USD_PER_1K: float = 0.015   # $15 / 1M input tokens
CLAUDE_OUTPUT_USD_PER_1K: float = 0.075  # $75 / 1M output tokens

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

#: Default ``max_tokens`` for Claude responses. 4096 fits comfortably
#: within Opus output budgets and is more than enough for the
#: structured ScienceProfile JSON object.
DEFAULT_MAX_TOKENS: int = 4096

#: Number of *additional* attempts on malformed JSON output before
#: raising :class:`ClaudeParseError`. One re-prompt is sufficient in
#: practice; further retries are dominated by latency, not signal.
JSON_REPROMPT_RETRIES: int = 1

#: Canonical letter-grade ordering — referenced by
#: :mod:`biotech_sniper.unified_scorer` for divergence detection
#: (VAL-M2-045). Keep this list as the single source of truth across
#: the project.
LETTER_GRADE_ORDER: tuple[str, ...] = (
    "A+", "A", "A-",
    "B+", "B", "B-",
    "C+", "C", "C-",
    "D",
    "F",
)


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class ClaudeError(Exception):
    """Base class for all Claude client errors."""


class ClaudeAuthError(ClaudeError):
    """Raised on Anthropic authentication errors — fail-fast, no retries."""


class ClaudeParseError(ClaudeError):
    """Raised when the model output is not parseable JSON or fails
    structural validation against the deep-science contract (missing
    ``letter_grade``, ``probability`` out of [0,1], ...)."""


# ---------------------------------------------------------------------------
# Prompt scaffolding
# ---------------------------------------------------------------------------

#: System prompt for the deep-science reasoning pass. Encodes the
#: biotech-specific cues that the validation feature explicitly calls
#: out: MoA, comparator, prior phase data, indication base rate,
#: sponsor track record, and trial-design risk surface.
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
    """Build the user-side prompt for :meth:`ClaudeClient.deep_science_review`.

    ``science_profile`` is the partial / preliminary profile assembled
    by :mod:`biotech_sniper.intelligence.trial_science_reader` (MoA
    notes, prior reads, indication, base-rate seed). ``full_context``
    is the larger structured payload — CT.gov protocol excerpts,
    8-Ks, prior NCT readouts, briefing-doc highlights — that Claude
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


class ClaudeClient:
    """Anthropic SDK wrapper for biotech deep-science reasoning.

    Parameters
    ----------
    api_key:
        Optional override. ``None`` (the default) reads
        :func:`biotech_sniper.config.get_anthropic_api_key`. Mission
        policy forbids any other lookup path.
    model:
        Default Anthropic model id. Defaults to :data:`DEFAULT_MODEL`.
    db_path:
        SQLite path for the ``llm_cost_ledger`` row write. Defaults
        to ``DATA_DIR / "alpha_sniper.db"``. Injectable for tests.
    client:
        Optional pre-built :class:`anthropic.Anthropic` instance (or
        any duck-typed substitute exposing ``.messages.create`` /
        ``.messages.stream``). Tests inject a fake client that
        replays cassettes; production callers leave this ``None`` so
        the wrapper builds a fresh SDK client with timeout +
        ``max_retries=0``.
    max_retries:
        Number of retries (in addition to the first attempt) on
        429/5xx and transient network errors. Defaults to 3.
    backoff_base:
        Base seconds for the exponential backoff. Default 0.5s
        produces 0.5s / 1.0s / 2.0s sleeps.
    timeout:
        Per-request timeout in seconds. Default 120.0.
    max_tokens:
        ``max_tokens`` parameter forwarded to the Anthropic API.
        Defaults to :data:`DEFAULT_MAX_TOKENS`.
    use_streaming:
        Opt-in flag for the SDK's ``messages.stream`` path. Default
        ``False`` (use ``messages.create``). Streaming reassembly is
        exercised by :func:`tests.test_claude_client.test_streaming_reassembly`.
    input_price_per_1k / output_price_per_1k:
        USD pricing per 1k tokens for cost-ledger calculation.
        Defaults to current Claude Opus list prices.
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
        max_tokens: int = DEFAULT_MAX_TOKENS,
        use_streaming: bool = False,
        input_price_per_1k: float = CLAUDE_INPUT_USD_PER_1K,
        output_price_per_1k: float = CLAUDE_OUTPUT_USD_PER_1K,
    ) -> None:
        resolved_key = (
            api_key if api_key is not None else config.get_anthropic_api_key()
        )
        if not resolved_key:
            raise ClaudeAuthError(
                "ANTHROPIC_API_KEY is not configured. Set it in "
                "/root/alpha_sniper/.env (or repo-root .env locally) "
                "and ensure config.get_anthropic_api_key() returns a "
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
        self._max_tokens = int(max_tokens)
        self._use_streaming = bool(use_streaming)
        self._input_price_per_1k = float(input_price_per_1k)
        self._output_price_per_1k = float(output_price_per_1k)

        if client is not None:
            self._client = client
        else:
            # ``max_retries=0`` disables the SDK's internal retry loop
            # so our explicit retry policy is the only one in play.
            self._client = anthropic.Anthropic(
                api_key=resolved_key,
                timeout=timeout,
                max_retries=0,
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
        contract (VAL-M2-036): ``science_profile`` (refined dict),
        ``letter_grade`` (one of :data:`LETTER_GRADE_ORDER`),
        ``probability`` (float in [0,1]), ``rationale`` (str),
        ``citations`` (list[dict]), ``model_id`` (echoed by the API,
        always starts with ``claude-``), ``latency_ms`` (int >= 0),
        ``cost_usd`` (float >= 0).

        Side effect: appends one row to ``llm_cost_ledger`` with
        ``provider='anthropic'``.
        """
        chosen_model = model or self._model
        prompt = build_deep_science_prompt(science_profile, full_context)

        last_parse_error: Optional[ClaudeParseError] = None
        message: Any = None
        latency_ms: int = 0
        parsed: dict[str, Any] = {}

        # JSON-mode parse loop: attempt + JSON_REPROMPT_RETRIES re-prompts
        # on malformed bodies. Network retries (429 / 5xx / transient)
        # are handled inside :meth:`_send`.
        for attempt in range(JSON_REPROMPT_RETRIES + 1):
            t_start = time.perf_counter()
            message = self._send(prompt=prompt, model=chosen_model)
            latency_ms = int((time.perf_counter() - t_start) * 1000)

            try:
                parsed = self._parse_message(message)
                break
            except ClaudeParseError as exc:
                last_parse_error = exc
                logger.warning(
                    "claude_client: malformed JSON on attempt %d (%s); "
                    "retrying with stricter directive",
                    attempt,
                    exc,
                )
                prompt = _strict_reprompt(prompt)
        else:
            # Loop exhausted without ``break``.
            assert last_parse_error is not None
            raise last_parse_error

        prompt_tokens, completion_tokens = _extract_usage(message)
        cost_usd = self._compute_cost_usd(prompt_tokens, completion_tokens)
        actual_model_id = str(getattr(message, "model", chosen_model) or chosen_model)
        request_id = getattr(message, "id", None)

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
    ) -> dict[str, Any]:
        """Generic public chat helper that ALWAYS writes a cost-ledger row.

        Used by :mod:`biotech_sniper.llm.llm_debate` for the
        Claude-side debate rounds so the public API path is the same
        one that handles cost-ledger persistence. Returns a dict with
        ``text``, ``prompt_tokens``, ``completion_tokens``,
        ``cost_usd``, ``latency_ms``, ``model_id``.
        """
        chosen_model = model or self._model
        chosen_system = system if system is not None else DEEP_SCIENCE_SYSTEM_PROMPT

        messages = [{"role": "user", "content": prompt}]
        t_start = time.perf_counter()
        msg = self._client.messages.create(
            model=chosen_model,
            max_tokens=self._max_tokens,
            system=chosen_system,
            messages=messages,
        )
        latency_ms = int((time.perf_counter() - t_start) * 1000)

        text = _extract_message_text(msg)
        prompt_tokens, completion_tokens = _extract_usage(msg)
        cost_usd = self._compute_cost_usd(prompt_tokens, completion_tokens)
        actual_model_id = str(getattr(msg, "model", chosen_model) or chosen_model)
        request_id = getattr(msg, "id", None)

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
        """Issue one ``messages.create`` (or streaming) call with retry.

        Returns an object with ``content``, ``usage``, ``id``, and
        ``model`` attributes — i.e. the SDK's :class:`Message`.
        """
        messages = [{"role": "user", "content": prompt}]
        last_error: Optional[Exception] = None
        attempts_so_far = 0
        max_total = self._max_retries + 1

        while attempts_so_far < max_total:
            try:
                if self._use_streaming:
                    msg = self._stream_once(
                        system=DEEP_SCIENCE_SYSTEM_PROMPT,
                        messages=messages,
                        model=model,
                    )
                else:
                    msg = self._client.messages.create(
                        model=model,
                        max_tokens=self._max_tokens,
                        system=DEEP_SCIENCE_SYSTEM_PROMPT,
                        messages=messages,
                    )
                return msg
            except anthropic.AuthenticationError as exc:
                # Fail-fast on auth — bad keys never come back.
                raise ClaudeAuthError(
                    f"Anthropic authentication failed: {exc!s}"
                ) from exc
            except (
                anthropic.RateLimitError,
                anthropic.APIConnectionError,
                anthropic.APITimeoutError,
            ) as exc:
                last_error = exc
            except anthropic.APIStatusError as exc:
                status = getattr(exc, "status_code", None)
                if status is not None and 500 <= status < 600:
                    last_error = exc
                else:
                    raise ClaudeError(
                        f"Anthropic API error {status}: {exc!s}"
                    ) from exc

            attempts_so_far += 1
            if attempts_so_far >= max_total:
                break
            self._sleep_backoff(attempts_so_far - 1)

        msg = (
            f"Anthropic request failed after {attempts_so_far} attempts "
            f"(max_retries={self._max_retries}): {last_error!r}"
        )
        if isinstance(last_error, ClaudeError):
            raise last_error
        raise ClaudeError(msg) from last_error

    def _stream_once(
        self,
        *,
        system: str,
        messages: list[dict[str, str]],
        model: str,
    ) -> Any:
        """Run a single streaming call and return the assembled message.

        We open ``client.messages.stream(...)`` as a context manager,
        consume the SSE chunks (so the SDK reassembles them), then
        return the final :class:`Message` via ``get_final_message``.

        Tests inject a fake client whose ``messages.stream`` returns
        a context manager that yields a stream object exposing the
        same surface (``text_stream`` iterator + ``get_final_message``).
        """
        with self._client.messages.stream(
            model=model,
            max_tokens=self._max_tokens,
            system=system,
            messages=messages,
        ) as stream:
            # Drain the text stream so the SDK accumulates content
            # blocks deterministically. We don't surface the partial
            # tokens upward — the caller wants the assembled JSON.
            for _ in stream.text_stream:
                pass
            return stream.get_final_message()

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
    def _parse_message(cls, message: Any) -> dict[str, Any]:
        """Validate + extract the JSON payload from a Claude :class:`Message`."""
        text = _extract_message_text(message)
        if not text.strip():
            raise ClaudeParseError(
                "Claude returned an empty content body"
            )
        return cls._parse_assistant_text(text)

    @staticmethod
    def _parse_assistant_text(text: str) -> dict[str, Any]:
        """Parse + validate a JSON-mode assistant string body."""
        cleaned = _strip_markdown_fences(text)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ClaudeParseError(
                f"Claude content was not valid JSON: {cleaned[:200]!r}"
            ) from exc

        if not isinstance(data, dict):
            raise ClaudeParseError(
                f"Claude JSON-mode payload was not an object: "
                f"{type(data).__name__}"
            )

        # ---- science_profile ------------------------------------------------
        science_profile = data.get("science_profile")
        if not isinstance(science_profile, dict):
            raise ClaudeParseError(
                f"Claude JSON missing/invalid 'science_profile': "
                f"got {type(science_profile).__name__}"
            )

        # ---- letter_grade ---------------------------------------------------
        letter_grade = data.get("letter_grade")
        if (
            not isinstance(letter_grade, str)
            or letter_grade not in LETTER_GRADE_ORDER
        ):
            raise ClaudeParseError(
                f"Claude JSON missing/invalid 'letter_grade': "
                f"got {letter_grade!r}"
            )

        # ---- probability ----------------------------------------------------
        try:
            probability = float(data["probability"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ClaudeParseError(
                f"Claude JSON missing/invalid 'probability': {data!r}"
            ) from exc
        if not 0.0 <= probability <= 1.0:
            raise ClaudeParseError(
                f"Claude 'probability' out of [0,1] range: {probability}"
            )

        # ---- rationale ------------------------------------------------------
        rationale = data.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ClaudeParseError(
                f"Claude JSON missing/invalid 'rationale': {rationale!r}"
            )

        # ---- citations ------------------------------------------------------
        citations_raw = data.get("citations")
        if not isinstance(citations_raw, list):
            raise ClaudeParseError(
                f"Claude JSON missing/invalid 'citations' list: "
                f"got {type(citations_raw).__name__}"
            )
        citations: list[dict[str, Any]] = []
        for idx, c in enumerate(citations_raw):
            if not isinstance(c, dict):
                raise ClaudeParseError(
                    f"Claude citations[{idx}] is not an object: "
                    f"got {type(c).__name__}"
                )
            source = c.get("source")
            quote_or_url = c.get("quote_or_url") or c.get("quote") or c.get("url")
            if not isinstance(source, str) or not source.strip():
                raise ClaudeParseError(
                    f"Claude citations[{idx}] missing 'source': {c!r}"
                )
            if not isinstance(quote_or_url, str) or not quote_or_url.strip():
                raise ClaudeParseError(
                    f"Claude citations[{idx}] missing 'quote_or_url' "
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

        Mirrors :meth:`XAIClient._log_cost_row`: failures are
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
                            "anthropic",
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
                "claude_client: failed to log llm_cost_ledger row (%s)",
                exc,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "claude_client: unexpected error writing cost ledger: %r",
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
    """One-shot helper: build a default :class:`ClaudeClient` and review.

    Useful for ad-hoc scripts and the smoke import in
    :mod:`biotech_sniper.audit`. Production paths should hold a
    long-lived :class:`ClaudeClient` so the underlying
    :class:`anthropic.Anthropic` HTTP client can pool connections
    across the top-N candidates list.
    """
    client = ClaudeClient(**client_kwargs)
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


def _extract_message_text(message: Any) -> str:
    """Extract the text body from an Anthropic :class:`Message` object.

    The SDK returns ``message.content`` as a list of content blocks
    where each block has a ``type`` and (for ``type=='text'``) a
    ``text`` attribute. We concatenate text blocks in order so
    streaming reassembly produces a single string body identical to
    the non-streaming path.
    """
    content = getattr(message, "content", None)
    if content is None:
        # Some test doubles return a plain dict.
        if isinstance(message, dict):
            content = message.get("content")
    if content is None:
        return ""

    parts: list[str] = []
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type is None and isinstance(block, dict):
            block_type = block.get("type")
        if block_type and block_type != "text":
            continue
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _extract_usage(message: Any) -> tuple[int, int]:
    """Pull ``(input_tokens, output_tokens)`` from a Claude message.

    Tolerant of dict-shaped test doubles. Missing fields default to
    ``0`` so cost-ledger writes never explode on partial responses
    (the DB stores zeros, and reconciliation under-counts safely).
    """
    usage = getattr(message, "usage", None)
    if usage is None and isinstance(message, dict):
        usage = message.get("usage")
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

    return (_get("input_tokens"), _get("output_tokens"))
