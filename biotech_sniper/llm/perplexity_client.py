"""Perplexity Sonar client — Reading-B Stage-2 ensemble fourth provider.

This module implements :class:`PerplexityClient`, a thin wrapper around
Perplexity's OpenAI-compatible chat-completions endpoint
(``POST https://api.perplexity.ai/chat/completions``). It is the
fourth leg of the Reading-B Stage-2 LLM ensemble (the existing three
are Grok, Claude, Gemini); the ensemble fan-out lives in
:mod:`biotech_sniper.llm.ensemble` (feature ``f-m3-03``). This module
deliberately scopes itself to the HTTP transport + schema-validated
parsing path; cost-ledger persistence is bolted on in feature
``f-m3-02-perplexity-cost-ledger`` so the two surfaces can ship and
be reviewed independently.

Design contract
---------------
* **Endpoint** — ``https://api.perplexity.ai/chat/completions``
  (see :data:`DEFAULT_ENDPOINT_URL`). Configurable via constructor
  kwarg for cassette-driven tests; production callers MUST use the
  default.
* **Model** — defaults to ``sonar``. ``sonar-pro`` and
  ``sonar-deep-research`` are the higher-cost tiers and are explicitly
  off the table for Reading-B (Stage-2 daily $20 cap).
* **API key sourcing** — reads ``PERPLEXITY_API_KEY`` exclusively via
  :func:`biotech_sniper.config.get_perplexity_api_key`. Direct env
  lookups are forbidden in this module by mission policy
  (``AGENTS.md`` § "Coding Conventions / Secrets"). A static check
  in ``tests/test_perplexity_client.py`` greps this module for the
  standard env-lookup APIs and fails the build if any match is
  found, so the docstring deliberately avoids those literal names.
* **Authorization header** — ``Authorization: Bearer <key>``.
* **Response format** — ``response_format = {"type":"json_schema",
  "json_schema":{"name":"biotech_catalyst_verdict","strict":true,
  "schema":{...}}}``. The schema is the locked
  :data:`BIOTECH_CATALYST_VERDICT_SCHEMA` constant exported below.
* **Web-search options** — ``web_search_options = {"search_context_size":
  "low"}`` to keep per-call cost bounded; ``medium``/``high`` would
  blow the Stage-2 daily $ cap.
* **Timeout** — 20 seconds (per :data:`DEFAULT_TIMEOUT`).
* **Retry policy** — 3 retries (initial + 3) on HTTP 429, HTTP 5xx,
  and request timeout / connection error. Exponential backoff
  ``base * 2**attempt`` seconds with ±20% multiplicative jitter
  around the geometric curve. Non-recoverable errors (HTTP 400, 401,
  422, schema/JSON parse failure) fail fast with no retries.
* **Typed exception hierarchy** — :class:`PerplexityClientError` is
  the base class; :class:`PerplexityAuthError`,
  :class:`PerplexityBadRequestError`, :class:`PerplexityRateLimitError`,
  :class:`PerplexityTransportError`, :class:`PerplexityTimeoutError`,
  and :class:`PerplexitySchemaError` all subclass it.
* **Schema validation** — every successful 200 response body has its
  ``choices[0].message.content`` parsed as JSON and matched against
  :data:`BIOTECH_CATALYST_VERDICT_SCHEMA`. Schema failure raises
  :class:`PerplexitySchemaError` (NOT bare ``json.JSONDecodeError`` /
  ``KeyError`` / ``ValueError``).

Public surface
--------------
* :class:`PerplexityClient` — the wrapper class.
* :func:`score_candidate` — module-level convenience for ad-hoc /
  smoke-import callers.
* :data:`BIOTECH_CATALYST_VERDICT_SCHEMA` — the locked JSON schema.
* :data:`DEFAULT_ENDPOINT_URL`, :data:`DEFAULT_MODEL`,
  :data:`DEFAULT_TIMEOUT`, :data:`DEFAULT_MAX_RETRIES`,
  :data:`DEFAULT_BACKOFF_BASE`.
* The exception hierarchy classes listed above.

Tests live in ``tests/test_perplexity_client.py``; cassettes are
committed under ``tests/fixtures/cassettes/perplexity/``. The live
Perplexity API is never contacted from ``pytest`` — every test runs
hermetically against committed cassettes via a fake session
substitute.
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Mapping, Optional

import requests

from biotech_sniper import config

__all__ = [
    "PerplexityClient",
    "PerplexityClientError",
    "PerplexityAuthError",
    "PerplexityBadRequestError",
    "PerplexityRateLimitError",
    "PerplexityTransportError",
    "PerplexityTimeoutError",
    "PerplexitySchemaError",
    "BIOTECH_CATALYST_VERDICT_SCHEMA",
    "DEFAULT_ENDPOINT_URL",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_BACKOFF_BASE",
    "score_candidate",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Perplexity OpenAI-compatible chat-completions URL.
DEFAULT_ENDPOINT_URL: str = "https://api.perplexity.ai/chat/completions"

#: Default model. Reading-B Stage-2 budget pins us to the cheap tier;
#: the higher-cost ``sonar-pro`` and ``sonar-deep-research`` tiers are
#: explicitly off the table for cost control. Production callers MUST
#: NOT override unless they also raise the daily $ cap accordingly.
DEFAULT_MODEL: str = "sonar"

#: Per-request HTTP timeout (seconds). Per validation contract
#: VAL-M3-008 the budget is exactly 20s — Perplexity sonar latencies
#: are well under that even with `search_context_size="low"`.
DEFAULT_TIMEOUT: float = 20.0

#: Number of retries (in addition to the first attempt) on retryable
#: failures (HTTP 429 / 5xx / timeout / connection error). Per
#: validation contract VAL-M3-009 the implementation constant is 3:
#: initial attempt + 3 retries = 4 attempts total against the upstream.
DEFAULT_MAX_RETRIES: int = 3

#: Base sleep (seconds) for the geometric backoff curve. Effective
#: sleeps follow ``base * 2**attempt`` with ±20% multiplicative jitter,
#: i.e. retry-0 ∈ [0.4, 0.6], retry-1 ∈ [0.8, 1.2], retry-2 ∈ [1.6, 2.4]
#: for the contract base of 0.5s.
DEFAULT_BACKOFF_BASE: float = 0.5


# ---------------------------------------------------------------------------
# Locked JSON schema — biotech_catalyst_verdict
# ---------------------------------------------------------------------------

#: The Perplexity ``response_format.json_schema`` payload sent on every
#: request. Five required top-level fields:
#:
#: * ``probability`` — float in [0, 1].
#: * ``label`` — enum {material, immaterial, ambiguous}.
#: * ``direction`` — enum {bullish, bearish}.
#: * ``rationale`` — free-form string.
#: * ``citations`` — array of objects, each with required ``url`` (URI)
#:   and ``title``, plus optional ``snippet``.
#:
#: This constant is the single source of truth for the schema; the
#: matching validator in :func:`_validate_against_schema` mirrors the
#: same shape so we do not depend on an external ``jsonschema`` library
#: (which is not pinned in :file:`requirements.txt`).
BIOTECH_CATALYST_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "probability",
        "label",
        "direction",
        "rationale",
        "citations",
    ],
    "properties": {
        "probability": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "label": {
            "type": "string",
            "enum": ["material", "immaterial", "ambiguous"],
        },
        "direction": {
            "type": "string",
            "enum": ["bullish", "bearish"],
        },
        "rationale": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["url", "title"],
                "properties": {
                    "url": {"type": "string", "format": "uri"},
                    "title": {"type": "string"},
                    "snippet": {"type": "string"},
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Typed exception hierarchy (VAL-M3-010)
# ---------------------------------------------------------------------------


class PerplexityClientError(Exception):
    """Base class for every Perplexity-client error.

    Catching this base class is sufficient to handle every error
    surface raised by :class:`PerplexityClient`.
    """


class PerplexityAuthError(PerplexityClientError):
    """Raised on HTTP 401 — bad / missing API key. No retries."""


class PerplexityBadRequestError(PerplexityClientError):
    """Raised on HTTP 400 / 422 — malformed request. No retries.

    Indicates a bug in the client's payload construction (or a Perplexity
    schema breaking change). Should never recover by retrying.
    """


class PerplexityRateLimitError(PerplexityClientError):
    """Raised when HTTP 429 retries are exhausted."""


class PerplexityTransportError(PerplexityClientError):
    """Raised when HTTP 5xx retries are exhausted, or a non-retryable
    transport-layer error occurs."""


class PerplexityTimeoutError(PerplexityClientError):
    """Raised when the request times out and retries are exhausted (or
    when the caller configured ``max_retries=0``)."""


class PerplexitySchemaError(PerplexityClientError):
    """Raised when the assistant ``message.content`` is not parseable
    JSON, or parses but violates :data:`BIOTECH_CATALYST_VERDICT_SCHEMA`.

    The exception message names the offending field so callers can log
    it for debugging — the raw payload itself is NOT included in the
    message because it can be large and may contain user-visible
    rationale text.
    """


# ---------------------------------------------------------------------------
# Schema validation helpers (manual; no external jsonschema dependency)
# ---------------------------------------------------------------------------


def _validate_against_schema(payload: Any) -> dict[str, Any]:
    """Validate ``payload`` against :data:`BIOTECH_CATALYST_VERDICT_SCHEMA`.

    Raises :class:`PerplexitySchemaError` on any mismatch; returns the
    payload (typed as ``dict``) on success. The validator is hand-rolled
    so we do not depend on ``jsonschema`` (not pinned in the project's
    ``requirements.txt``).
    """
    if not isinstance(payload, dict):
        raise PerplexitySchemaError(
            f"verdict payload was not an object (got {type(payload).__name__})"
        )
    required = {"probability", "label", "direction", "rationale", "citations"}
    missing = required - set(payload)
    if missing:
        # Sort for deterministic error messages.
        raise PerplexitySchemaError(
            f"verdict missing required field(s): {sorted(missing)}"
        )

    # probability — number in [0, 1].
    prob = payload["probability"]
    if isinstance(prob, bool) or not isinstance(prob, (int, float)):
        raise PerplexitySchemaError(
            f"verdict 'probability' must be a number (got {type(prob).__name__})"
        )
    prob_f = float(prob)
    if not 0.0 <= prob_f <= 1.0:
        raise PerplexitySchemaError(
            f"verdict 'probability' out of [0,1] range: {prob_f}"
        )

    # label — enum.
    label = payload["label"]
    if label not in ("material", "immaterial", "ambiguous"):
        raise PerplexitySchemaError(
            f"verdict 'label' not in enum {{material, immaterial, ambiguous}}: {label!r}"
        )

    # direction — enum.
    direction = payload["direction"]
    if direction not in ("bullish", "bearish"):
        raise PerplexitySchemaError(
            f"verdict 'direction' not in enum {{bullish, bearish}}: {direction!r}"
        )

    # rationale — string (NOT None, NOT coerced).
    rationale = payload["rationale"]
    if not isinstance(rationale, str):
        raise PerplexitySchemaError(
            f"verdict 'rationale' must be a string "
            f"(got {type(rationale).__name__})"
        )

    # citations — array of {url, title, snippet?}.
    citations = payload["citations"]
    if not isinstance(citations, list):
        raise PerplexitySchemaError(
            f"verdict 'citations' must be an array "
            f"(got {type(citations).__name__})"
        )
    for idx, item in enumerate(citations):
        if not isinstance(item, dict):
            raise PerplexitySchemaError(
                f"verdict 'citations[{idx}]' must be an object "
                f"(got {type(item).__name__})"
            )
        for cit_required in ("url", "title"):
            if cit_required not in item:
                raise PerplexitySchemaError(
                    f"verdict 'citations[{idx}]' missing required '{cit_required}'"
                )
        url = item["url"]
        if not isinstance(url, str) or not url:
            raise PerplexitySchemaError(
                f"verdict 'citations[{idx}].url' must be a non-empty string"
            )
        title = item["title"]
        if not isinstance(title, str):
            raise PerplexitySchemaError(
                f"verdict 'citations[{idx}].title' must be a string"
            )
        if "snippet" in item and not isinstance(item["snippet"], str):
            raise PerplexitySchemaError(
                f"verdict 'citations[{idx}].snippet' must be a string when present"
            )
    return payload


# ---------------------------------------------------------------------------
# Prompt scaffolding
# ---------------------------------------------------------------------------

#: System prompt for Stage-2 candidate scoring. Compact and
#: deterministic — the model is asked to return ONLY the structured
#: verdict object (the response_format=json_schema enforces this on
#: Perplexity's side; the system prompt sets caller expectations).
STAGE2_SYSTEM_PROMPT: str = (
    "You are a quantitative biotech catalyst analyst. "
    "Given a candidate news event for a US biotech ticker, decide "
    "whether the event is MATERIAL (likely to move the stock by ≥ 5% "
    "in either direction within 5 trading days), IMMATERIAL, or "
    "AMBIGUOUS. Respond ONLY with the structured biotech_catalyst_verdict "
    "JSON object. Use 'bullish' for positive material catalysts (e.g. "
    "successful readouts, FDA approvals, partnership wins) and "
    "'bearish' for negative material catalysts (e.g. failed readouts, "
    "FDA CRLs, label restrictions). Cite at least one URL whenever "
    "the search context contained relevant evidence."
)


def _build_candidate_user_prompt(candidate: Mapping[str, Any]) -> str:
    """Render a structured candidate dict into a Perplexity user message.

    The candidate is whatever Stage-2 hands us — typically a
    ``candidate_events`` row plus headline metadata. We serialise as
    JSON so the model sees an unambiguous structure rather than
    free-form prose.
    """
    payload = {"candidate_event": dict(candidate)}
    return (
        "Score the following biotech news candidate. Decide MATERIAL / "
        "IMMATERIAL / AMBIGUOUS, choose bullish or bearish for material "
        "events, and provide ≥ 1 citation when search evidence is used.\n\n"
        + json.dumps(payload, sort_keys=True, default=str)
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class PerplexityClient:
    """Thin Perplexity Sonar client wrapping the OpenAI-compatible HTTP
    chat-completions endpoint.

    Parameters
    ----------
    api_key:
        Optional API key override. When ``None`` (the default) the
        client reads :func:`biotech_sniper.config.get_perplexity_api_key`.
        Mission policy forbids any other lookup path. A missing key
        raises :class:`PerplexityAuthError` at construction time.
    endpoint_url:
        Override for the chat-completions URL. Defaults to
        :data:`DEFAULT_ENDPOINT_URL`. Tests use this to point at a
        local mock; production callers MUST use the default.
    model:
        Default model name. Defaults to :data:`DEFAULT_MODEL`
        (``"sonar"``). ``sonar-pro`` and ``sonar-deep-research`` are
        forbidden by the Reading-B daily-cap budget — overriding to
        either tier is a budget violation.
    session:
        Optional pre-configured :class:`requests.Session`. Tests
        substitute a fake session that replays cassettes.
    max_retries:
        Number of retries (in addition to the first attempt) on
        retryable failures. Defaults to :data:`DEFAULT_MAX_RETRIES`
        (3). Tests sometimes set this to ``0`` to assert
        single-attempt behaviour.
    backoff_base:
        Base seconds for the geometric backoff. Default
        :data:`DEFAULT_BACKOFF_BASE` (0.5s) produces sleeps in
        [0.4, 0.6] / [0.8, 1.2] / [1.6, 2.4] for retries 0/1/2.
        Tests pass ``0.0`` to zero out sleeps.
    timeout:
        Per-request timeout (seconds). Default :data:`DEFAULT_TIMEOUT`
        (20.0) per VAL-M3-008. Tests sometimes pass a smaller value
        (e.g. 0.1s) to exercise the timeout path quickly.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        endpoint_url: str = DEFAULT_ENDPOINT_URL,
        model: str = DEFAULT_MODEL,
        session: Optional[Any] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        resolved_key = (
            api_key if api_key is not None else config.get_perplexity_api_key()
        )
        if not resolved_key:
            raise PerplexityAuthError(
                "PERPLEXITY_API_KEY is not configured. Set it in "
                "/root/alpha_sniper/.env (or repo-root .env locally) and "
                "ensure config.get_perplexity_api_key() returns a "
                "non-empty value."
            )
        self._api_key: str = resolved_key
        self.endpoint_url: str = endpoint_url
        self._model: str = model
        self._session = session if session is not None else requests.Session()
        self._max_retries: int = int(max_retries)
        self._backoff_base: float = float(backoff_base)
        self._timeout: float = float(timeout)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score_candidate(
        self,
        candidate: Mapping[str, Any],
        *,
        model: Optional[str] = None,
    ) -> dict[str, Any]:
        """Score a single Stage-2 candidate event.

        Returns the schema-validated verdict dict with required keys
        ``probability`` (float in [0,1]), ``label``
        (``material``/``immaterial``/``ambiguous``), ``direction``
        (``bullish``/``bearish``), ``rationale`` (string), and
        ``citations`` (list of {url, title, snippet?}).

        Raises a typed subclass of :class:`PerplexityClientError` on
        failure — see the module docstring for the full hierarchy.
        """
        chosen_model = model or self._model
        messages = [
            {"role": "system", "content": STAGE2_SYSTEM_PROMPT},
            {"role": "user", "content": _build_candidate_user_prompt(candidate)},
        ]
        response_json = self._chat_completion(
            messages=messages, model=chosen_model
        )
        content = self._extract_message_content(response_json)
        return self._parse_and_validate_verdict(content)

    # ------------------------------------------------------------------
    # HTTP layer
    # ------------------------------------------------------------------

    def _build_request_payload(
        self,
        *,
        messages: list[dict[str, str]],
        model: str,
    ) -> dict[str, Any]:
        """Construct the JSON body sent to Perplexity.

        Centralised so both the production path and tests can inspect
        the exact shape (response_format / web_search_options /
        model / messages) the upstream sees on the wire.
        """
        return {
            "model": model,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "biotech_catalyst_verdict",
                    "strict": True,
                    "schema": BIOTECH_CATALYST_VERDICT_SCHEMA,
                },
            },
            "web_search_options": {"search_context_size": "low"},
        }

    def _chat_completion(
        self,
        *,
        messages: list[dict[str, str]],
        model: str,
    ) -> dict[str, Any]:
        """POST to ``endpoint_url`` with retry + backoff.

        Handles the error-classification fan-out:

        * 401 → :class:`PerplexityAuthError` (no retry, fail fast).
        * 400 / 422 → :class:`PerplexityBadRequestError` (no retry).
        * 429 → retry; on exhaustion → :class:`PerplexityRateLimitError`.
        * 5xx → retry; on exhaustion → :class:`PerplexityTransportError`.
        * Timeout / ConnectionError → retry; on exhaustion →
          :class:`PerplexityTimeoutError` (timeout) or
          :class:`PerplexityTransportError` (connection error).
        """
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = self._build_request_payload(messages=messages, model=model)

        max_total = self._max_retries + 1  # initial + retries
        attempts_so_far = 0
        last_exc: Optional[BaseException] = None
        last_kind: str = "transport"  # 'transport' | 'timeout' | 'rate_limit'

        while attempts_so_far < max_total:
            try:
                resp = self._session.post(
                    self.endpoint_url,
                    headers=headers,
                    json=payload,
                    timeout=self._timeout,
                )
            except requests.Timeout as exc:
                last_exc = exc
                last_kind = "timeout"
                attempts_so_far += 1
                if attempts_so_far >= max_total:
                    break
                self._sleep_backoff(attempts_so_far - 1)
                continue
            except requests.ConnectionError as exc:
                last_exc = exc
                last_kind = "transport"
                attempts_so_far += 1
                if attempts_so_far >= max_total:
                    break
                self._sleep_backoff(attempts_so_far - 1)
                continue

            status = getattr(resp, "status_code", None)

            # Fail-fast classifications.
            if status == 401:
                raise PerplexityAuthError(
                    f"Perplexity returned HTTP 401 (unauthorized). "
                    f"Body: {_safe_body_snippet(resp)}"
                )
            if status in (400, 422):
                raise PerplexityBadRequestError(
                    f"Perplexity returned HTTP {status}. "
                    f"Body: {_safe_body_snippet(resp)}"
                )

            # Retryable classifications.
            if status == 429:
                last_exc = PerplexityRateLimitError(
                    f"Perplexity HTTP 429: {_safe_body_snippet(resp)}"
                )
                last_kind = "rate_limit"
                attempts_so_far += 1
                if attempts_so_far >= max_total:
                    break
                self._sleep_backoff(attempts_so_far - 1)
                continue
            if status is not None and 500 <= status < 600:
                last_exc = PerplexityTransportError(
                    f"Perplexity HTTP {status}: {_safe_body_snippet(resp)}"
                )
                last_kind = "transport"
                attempts_so_far += 1
                if attempts_so_far >= max_total:
                    break
                self._sleep_backoff(attempts_so_far - 1)
                continue

            # Any other non-2xx is non-retryable transport.
            if status is None or not (200 <= status < 300):
                raise PerplexityTransportError(
                    f"Perplexity HTTP {status}: {_safe_body_snippet(resp)}"
                )

            # Success path — parse JSON envelope.
            try:
                return resp.json()
            except ValueError as exc:
                # 200 with a body that is not valid JSON at the
                # envelope level is treated as a schema error so
                # callers can route the candidate to the next provider.
                raise PerplexitySchemaError(
                    f"Perplexity returned non-JSON 200: "
                    f"{_safe_body_snippet(resp)}"
                ) from exc

        # Out of retries.
        if last_kind == "timeout":
            raise PerplexityTimeoutError(
                f"Perplexity timed out after {attempts_so_far} attempt(s) "
                f"(timeout={self._timeout}s, max_retries={self._max_retries})"
            ) from last_exc
        if last_kind == "rate_limit":
            # last_exc is already a typed PerplexityRateLimitError —
            # re-raise it so the caller sees the original detail.
            assert isinstance(last_exc, PerplexityRateLimitError)
            raise last_exc
        # Default: transport.
        if isinstance(last_exc, PerplexityClientError):
            raise last_exc
        raise PerplexityTransportError(
            f"Perplexity request failed after {attempts_so_far} attempts: "
            f"{last_exc!r}"
        ) from last_exc

    def _sleep_backoff(self, retry_index: int) -> None:
        """Sleep for ``backoff_base * 2**retry_index`` seconds with
        ±20% multiplicative jitter.

        ``retry_index`` is zero-based: the first retry uses index 0
        (geometric multiplier 1×), the second uses index 1 (2×), etc.
        For the contract default ``backoff_base=0.5`` this yields
        per-retry sleeps in [0.4, 0.6] / [0.8, 1.2] / [1.6, 2.4]
        seconds.
        """
        if self._backoff_base <= 0:
            # Short-circuit so tests can pass ``backoff_base=0.0`` to
            # eliminate sleep without falling through to ``time.sleep(0)``
            # (which still yields a context switch on some OSes).
            return
        base = self._backoff_base * (2 ** max(0, retry_index))
        jitter = random.uniform(0.8, 1.2)  # ±20% multiplicative
        time.sleep(base * jitter)

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_message_content(response_json: Mapping[str, Any]) -> str:
        """Pull ``choices[0].message.content`` out of a chat-completion
        response, raising :class:`PerplexitySchemaError` on any shape
        mismatch.
        """
        try:
            choices = response_json["choices"]
            content = choices[0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise PerplexitySchemaError(
                "Perplexity response missing choices[0].message.content"
            ) from exc
        if not isinstance(content, str):
            raise PerplexitySchemaError(
                "Perplexity message.content was not a string "
                f"(got {type(content).__name__})"
            )
        return content

    @staticmethod
    def _parse_and_validate_verdict(content: str) -> dict[str, Any]:
        """Parse ``content`` as JSON and validate against
        :data:`BIOTECH_CATALYST_VERDICT_SCHEMA`.

        Wraps ``json.JSONDecodeError`` into :class:`PerplexitySchemaError`
        so callers see a single typed surface for "malformed body"
        failures (per VAL-M3-011).
        """
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise PerplexitySchemaError(
                f"Perplexity message.content was not valid JSON: "
                f"{content[:120]!r}"
            ) from exc
        return _validate_against_schema(data)


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


def score_candidate(
    candidate: Mapping[str, Any],
    **client_kwargs: Any,
) -> dict[str, Any]:
    """One-shot helper: build a default :class:`PerplexityClient` and
    score ``candidate``.

    Useful for ad-hoc scripts and the smoke import in ``audit.py``.
    Production paths should hold a long-lived client so the
    :class:`requests.Session` can pool keep-alive connections.
    """
    client = PerplexityClient(**client_kwargs)
    return client.score_candidate(candidate)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _safe_body_snippet(resp: Any, limit: int = 200) -> str:
    """Return a truncated, non-secret-leaking snippet of a response body.

    Some test doubles set ``resp.text`` to a string; the live
    :class:`requests.Response` exposes the same attr. We tolerate
    objects without ``text`` so a partial mock doesn't blow up.
    """
    text = getattr(resp, "text", None)
    if text is None:
        return "<no body>"
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"
