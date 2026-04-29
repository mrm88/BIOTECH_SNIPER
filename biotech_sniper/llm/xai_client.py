"""xAI / Grok-4 client — fast-tier ranking pass over the universe.

This module implements :class:`XAIClient`, a thin wrapper around the
xAI Grok-4 chat-completions endpoint (``POST /v1/chat/completions``,
OpenAI-compatible). It is the **fast tier** of the M2 two-tier LLM
stack: Grok-4 ranks every ticker in the daily universe pass; the
deep tier (Claude + Gemini) then scores the top-N survivors.

Design contract
---------------
* **API key sourcing**: reads ``XAI_API_KEY`` exclusively via
  :func:`biotech_sniper.config.get_xai_api_key`. Direct environment
  lookups are forbidden in this module by mission policy
  (``AGENTS.md`` § "LLM boundaries"). Validators grep this file for
  the standard env-lookup APIs and fail the build if any match is
  found, so this docstring deliberately avoids those literal names.
* **Transport**: pure :mod:`requests` (already pinned). No new SDK
  dependency. The xAI HTTP API is OpenAI-compatible so a small
  hand-rolled wrapper is sufficient and gives full control over the
  retry envelope.
* **Retry policy**: 3 retries with exponential backoff
  (``0.5 → 1.0 → 2.0`` seconds) on HTTP 429 and 5xx, also on
  ``ConnectionError`` / ``Timeout`` from :mod:`requests`. The fourth
  failure raises :class:`XAIError`.
* **Fail-fast on 401**: raises :class:`XAIAuthError` on the *first*
  401. No retry storm — bad keys should be loud, not laggy.
* **JSON-mode parsing**: requests ``response_format={"type":
  "json_object"}`` and parses the assistant payload via
  :func:`json.loads`. Partial / malformed JSON raises
  :class:`XAIParseError` so the caller can route the ticker to the
  deep tier or skip.
* **Cost ledger**: every successful call inserts one row into the
  SQLite ``llm_cost_ledger`` table (``provider='xai'``). The DB path
  defaults to ``DATA_DIR / "alpha_sniper.db"`` but is injectable for
  tests. Ledger failures (e.g. db locked, schema missing) are
  swallowed with a WARNING so a single sqlite hiccup never blocks the
  scoring pipeline.

Public surface
--------------
* :class:`XAIClient` — the wrapper class.
* :func:`score_ticker` — module-level convenience that constructs a
  default client and forwards. Used by smoke imports / quick scripts.
* :class:`XAIError`, :class:`XAIAuthError`, :class:`XAIParseError` —
  typed exceptions.
* :data:`GROK_4_INPUT_USD_PER_1K`, :data:`GROK_4_OUTPUT_USD_PER_1K`
  — published Grok-4 list pricing (per 1k tokens) used to compute
  ``cost_usd`` per call. Override via ``input_price_per_1k`` /
  ``output_price_per_1k`` if pricing changes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping, Optional

import requests

from biotech_sniper import config, db
from biotech_sniper.paths import DATA_DIR

__all__ = [
    "XAIClient",
    "XAIError",
    "XAIAuthError",
    "XAIParseError",
    "score_ticker",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "GROK_4_INPUT_USD_PER_1K",
    "GROK_4_OUTPUT_USD_PER_1K",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: xAI's OpenAI-compatible chat-completions base URL.
DEFAULT_BASE_URL: str = "https://api.x.ai/v1"

#: Default model for fast-tier ranking. Operators may override per call.
DEFAULT_MODEL: str = "grok-4"

#: Grok-4 published pricing (USD per 1k tokens). Sourced from xAI docs as
#: of mission setup; override via ``input_price_per_1k`` /
#: ``output_price_per_1k`` constructor args if list pricing changes.
GROK_4_INPUT_USD_PER_1K: float = 0.005   # $5 / 1M input tokens
GROK_4_OUTPUT_USD_PER_1K: float = 0.015  # $15 / 1M output tokens

#: Maximum number of retries (excluding the first attempt) on 429/5xx.
DEFAULT_MAX_RETRIES: int = 3

#: Base sleep (seconds) for exponential backoff between retries.
#: Effective sleeps: 0.5 → 1.0 → 2.0 seconds for retries 0, 1, 2.
DEFAULT_BACKOFF_BASE: float = 0.5

#: Default per-request HTTP timeout (seconds). Grok-4 latencies have a
#: long tail when the model is busy; 60s leaves headroom without ever
#: looking like a hang.
DEFAULT_TIMEOUT: float = 60.0


# Sentinel for "argument not supplied" — distinguishes the case
# ``response_format=None`` (caller opts out of JSON mode) from "caller
# did not pass a value, use the historical default".
_SENTINEL: Any = object()


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class XAIError(Exception):
    """Base class for all xAI client errors."""


class XAIAuthError(XAIError):
    """Raised on HTTP 401 — fail-fast, no retries."""


class XAIParseError(XAIError):
    """Raised when the assistant payload is not parseable JSON or is
    missing required keys (``probability``, ``rationale``,
    ``confidence``)."""


# ---------------------------------------------------------------------------
# Prompt scaffolding
# ---------------------------------------------------------------------------

#: System prompt for the fast-tier ranking pass. Kept compact so the
#: 600-ticker daily sweep stays cheap. The deep tier does the heavy
#: science reasoning.
FAST_RANK_SYSTEM_PROMPT: str = (
    "You are a quantitative biotech catalyst analyst. "
    "You score the probability that a single biotech ticker will "
    "experience a positive material catalyst (clinical readout, FDA "
    "decision, AdCom outcome, contract award) within the supplied "
    "context window. "
    "Return STRICT JSON only with these keys: "
    "probability (float in [0,1]), "
    "rationale (1-3 sentence string), "
    "confidence (float in [0,1] reflecting how much signal the "
    "context contained). "
    "Do not include markdown, prose outside the JSON, or commentary."
)


def build_fast_rank_prompt(ticker: str, context: Mapping[str, Any]) -> str:
    """Build the user-side prompt for :meth:`XAIClient.score_ticker`.

    ``context`` is the structured per-ticker dict assembled by the
    universe scanner — typically containing ``catalyst_date``,
    ``catalyst_type``, ``nct_id``, ``phase``, ``indication``,
    ``sponsor``, ``recent_news``, etc. We serialise it as JSON so the
    model sees an unambiguous structure rather than free-form text.
    """
    payload = {
        "ticker": ticker,
        "context": dict(context),
    }
    return (
        "Score the catalyst probability for the following ticker.\n\n"
        + json.dumps(payload, sort_keys=True, default=str)
        + "\n\nReturn JSON only."
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class XAIClient:
    """Thin Grok-4 client wrapping xAI's OpenAI-compatible HTTP API.

    Parameters
    ----------
    api_key:
        Optional API key override. When ``None`` (the default) the
        client reads :func:`biotech_sniper.config.get_xai_api_key`.
        Mission policy forbids any other lookup path. A missing key
        raises :class:`XAIAuthError` at construction time so callers
        fail loudly during DI rather than silently mid-pipeline.
    base_url:
        Override for the chat-completions endpoint. Defaults to
        :data:`DEFAULT_BASE_URL`.
    model:
        Default model name. Defaults to :data:`DEFAULT_MODEL`.
    db_path:
        SQLite path for the ``llm_cost_ledger`` row write. Defaults
        to ``DATA_DIR / "alpha_sniper.db"``. Injectable for tests.
    session:
        Optional pre-configured :class:`requests.Session`. Tests
        substitute a fake session that replays cassettes.
    max_retries:
        Number of retries (in addition to the first attempt) on
        429/5xx and transient network errors. Defaults to 3.
    backoff_base:
        Base seconds for the exponential backoff. Default 0.5s
        produces 0.5s / 1.0s / 2.0s sleeps for retries 0, 1, 2.
    timeout:
        Per-request timeout in seconds. Default 60.0.
    input_price_per_1k / output_price_per_1k:
        USD pricing per 1k tokens. Defaults to current Grok-4 list
        prices.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        db_path: Optional[Path] = None,
        session: Optional[requests.Session] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        timeout: float = DEFAULT_TIMEOUT,
        input_price_per_1k: float = GROK_4_INPUT_USD_PER_1K,
        output_price_per_1k: float = GROK_4_OUTPUT_USD_PER_1K,
    ) -> None:
        resolved_key = api_key if api_key is not None else config.get_xai_api_key()
        if not resolved_key:
            raise XAIAuthError(
                "XAI_API_KEY is not configured. Set it in /root/alpha_sniper/.env "
                "(or repo-root .env locally) and ensure config.get_xai_api_key() "
                "returns a non-empty value."
            )
        self._api_key = resolved_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._db_path: Path = (
            Path(db_path) if db_path is not None else DATA_DIR / "alpha_sniper.db"
        )
        self._session = session or requests.Session()
        self._max_retries = int(max_retries)
        self._backoff_base = float(backoff_base)
        self._timeout = float(timeout)
        self._input_price_per_1k = float(input_price_per_1k)
        self._output_price_per_1k = float(output_price_per_1k)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score_ticker(
        self,
        ticker: str,
        context: Mapping[str, Any],
        *,
        purpose: str = "fast_rank",
        model: Optional[str] = None,
    ) -> dict[str, Any]:
        """Score a single ticker for catalyst probability.

        Returns a dict with the keys required by the validation
        contract (VAL-M2-030): ``probability`` (float in [0,1]),
        ``rationale`` (str), ``confidence`` (float in [0,1]),
        ``model_id`` (the actual model id the API echoed back, e.g.
        ``"grok-4-0709"``), ``latency_ms`` (int >= 0), ``cost_usd``
        (float >= 0).

        Side effect: appends one row to ``llm_cost_ledger`` with
        ``provider='xai'`` for **every** HTTP 200 response — including
        malformed-JSON cases (the row is logged with
        ``note='unparseable_response'`` and the actual computed cost
        from the ``usage`` block, then :class:`XAIParseError` is
        raised). Calls that never reached HTTP 200 (auth failure,
        retries exhausted, …) do NOT produce a ledger row because
        they are not billable.
        """
        chosen_model = model or self._model
        prompt = build_fast_rank_prompt(ticker, context)
        messages = [
            {"role": "system", "content": FAST_RANK_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        t_start = time.perf_counter()
        response_json = self._chat_completion(messages=messages, model=chosen_model)
        latency_ms = int((time.perf_counter() - t_start) * 1000)

        # f-m2-13 fix #2: log the cost-ledger row BEFORE parsing the
        # assistant payload. xAI bills for any HTTP 200 response, so a
        # malformed JSON body (or a missing ``probability`` field) must
        # still produce a llm_cost_ledger row. If the usage block is
        # missing we fall back to ``cost_usd=0.0`` and record
        # ``note='unparseable_response'`` so the audit trail is still
        # complete.
        usage = response_json.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        cost_usd = self._compute_cost_usd(prompt_tokens, completion_tokens)
        actual_model_id = str(response_json.get("model") or chosen_model)
        request_id = response_json.get("id")

        try:
            parsed = self._parse_assistant_payload(response_json)
        except XAIParseError:
            # Bill the call before re-raising so the ledger captures
            # the spend even though the parse failed.
            self._log_cost_row(
                model_id=actual_model_id,
                purpose=purpose,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                request_id=request_id,
                note="unparseable_response",
            )
            raise

        result = {
            "probability": parsed["probability"],
            "rationale": parsed["rationale"],
            "confidence": parsed["confidence"],
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
    # HTTP layer
    # ------------------------------------------------------------------

    def _chat_completion(
        self,
        *,
        messages: list[dict[str, str]],
        model: str,
        response_format: Optional[dict[str, Any]] = _SENTINEL,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """POST to ``/chat/completions`` with retry + backoff.

        ``response_format`` defaults to the
        ``{"type": "json_object"}`` shape used by the fast-rank
        scoring path. Pass ``None`` to opt out (for callers that
        explicitly want non-JSON-mode replies) or any other dict to
        override.
        """
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
        }
        # Sentinel preserves the historical default while allowing
        # callers to disable JSON mode by passing ``response_format=None``.
        if response_format is _SENTINEL:
            payload["response_format"] = {"type": "json_object"}
        elif response_format is not None:
            payload["response_format"] = response_format

        # We try the first attempt + ``max_retries`` retries. ``attempt``
        # is the zero-based index of the *retry* (so attempt=0 is the
        # first retry, after the initial call).
        last_error: Optional[Exception] = None
        attempts_so_far = 0
        max_total = self._max_retries + 1  # initial + retries

        while attempts_so_far < max_total:
            try:
                resp = self._session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self._timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = exc
                attempts_so_far += 1
                if attempts_so_far >= max_total:
                    break
                self._sleep_backoff(attempts_so_far - 1)
                continue

            status = getattr(resp, "status_code", None)

            # Fail-fast on 401 — bad keys never come back.
            if status == 401:
                raise XAIAuthError(
                    f"xAI returned HTTP 401 (unauthorized). Body: "
                    f"{_safe_body_snippet(resp)}"
                )

            # Retryable: 429 (rate limit), 5xx (server side).
            if status == 429 or (status is not None and 500 <= status < 600):
                last_error = XAIError(
                    f"Retryable HTTP {status}: {_safe_body_snippet(resp)}"
                )
                attempts_so_far += 1
                if attempts_so_far >= max_total:
                    break
                self._sleep_backoff(attempts_so_far - 1)
                continue

            # Any other non-2xx is non-retryable.
            if status is None or not (200 <= status < 300):
                raise XAIError(
                    f"xAI HTTP {status}: {_safe_body_snippet(resp)}"
                )

            # Success path.
            try:
                return resp.json()
            except ValueError as exc:
                raise XAIError(
                    f"xAI returned non-JSON 200: {_safe_body_snippet(resp)}"
                ) from exc

        # Out of retries.
        msg = (
            f"xAI request failed after {attempts_so_far} attempts "
            f"(max_retries={self._max_retries}): {last_error!r}"
        )
        if isinstance(last_error, XAIError):
            raise last_error
        raise XAIError(msg) from last_error

    def _sleep_backoff(self, retry_index: int) -> None:
        """Sleep ``backoff_base * 2**retry_index`` seconds.

        ``retry_index`` is zero-based (first retry = 0), so for the
        default base 0.5s the sleeps are 0.5, 1.0, 2.0 s.
        """
        delay = self._backoff_base * (2 ** max(0, retry_index))
        time.sleep(delay)

    # ------------------------------------------------------------------
    # JSON-mode parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_assistant_payload(response_json: Mapping[str, Any]) -> dict[str, Any]:
        """Extract + validate the JSON-mode body returned by the model."""
        try:
            choices = response_json["choices"]
            content = choices[0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise XAIParseError(
                f"xAI response missing choices[0].message.content: "
                f"{response_json!r}"
            ) from exc

        if not isinstance(content, str):
            raise XAIParseError(
                f"xAI message.content was not a string: {type(content).__name__}"
            )

        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise XAIParseError(
                f"xAI message.content was not valid JSON: {content[:200]!r}"
            ) from exc

        if not isinstance(data, dict):
            raise XAIParseError(
                f"xAI JSON-mode payload was not an object: {type(data).__name__}"
            )

        try:
            probability = float(data["probability"])
        except (KeyError, TypeError, ValueError) as exc:
            raise XAIParseError(
                f"xAI JSON missing/invalid 'probability': {data!r}"
            ) from exc
        if not 0.0 <= probability <= 1.0:
            raise XAIParseError(
                f"xAI 'probability' out of [0,1] range: {probability}"
            )

        rationale = str(data.get("rationale", ""))
        try:
            confidence = float(data.get("confidence", 0.5))
        except (TypeError, ValueError) as exc:
            raise XAIParseError(
                f"xAI 'confidence' was not a number: {data.get('confidence')!r}"
            ) from exc
        # Clamp confidence rather than rejecting; the model occasionally
        # emits 1.05 or -0.0 due to numeric instability.
        confidence = max(0.0, min(1.0, confidence))

        return {
            "probability": probability,
            "rationale": rationale,
            "confidence": confidence,
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
        # Round to 6 dp — preserves sub-millicent fidelity, which is
        # important when summing thousands of rows for the daily cap.
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
        note: Optional[str] = None,
    ) -> None:
        """Append one row to ``llm_cost_ledger``.

        Parameters
        ----------
        note:
            Optional free-form annotation persisted on the row. Used
            by f-m2-13 fix #2 to record ``note='unparseable_response'``
            when a 200 response could not be parsed but was still
            billed.

        Failures (db missing, schema not yet migrated, db locked) are
        logged at WARNING and swallowed — a cost-ledger hiccup must
        not break the scoring pipeline. The DB is the source of truth
        for cost reconciliation; if the row is missing, downstream
        cap-enforcement simply under-counts (which is conservative,
        not catastrophic).
        """
        try:
            # f-misc-08: ``db.connect`` centralises the
            # ``parent.mkdir(parents=True, exist_ok=True)`` for
            # paths under DATA_DIR, so we no longer need to do it
            # here. ``DATA_DIR`` is not auto-created by ``paths.py``
            # (by design — see paths docstring), so connect() handles
            # the first-after-fresh-checkout case.
            conn = db.connect(self._db_path)
            try:
                db.run_migrations(conn)
                with conn:
                    conn.execute(
                        """
                        INSERT INTO llm_cost_ledger (
                            provider, model_id, purpose,
                            prompt_tokens, completion_tokens,
                            latency_ms, cost_usd, request_id, note
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "xai",
                            model_id,
                            purpose,
                            int(prompt_tokens),
                            int(completion_tokens),
                            int(latency_ms),
                            float(cost_usd),
                            request_id,
                            note,
                        ),
                    )
            finally:
                conn.close()
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            logger.warning(
                "xai_client: failed to log llm_cost_ledger row (%s)",
                exc,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "xai_client: unexpected error writing cost ledger: %r",
                exc,
            )

    # ------------------------------------------------------------------
    # Public debate-friendly chat helper (f-m2-13 fix #6)
    # ------------------------------------------------------------------

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: Optional[str] = None,
        purpose: str = "debate",
        json_mode: bool = True,
    ) -> dict[str, Any]:
        """Generic public chat helper that ALWAYS writes a cost-ledger row.

        Used by :mod:`biotech_sniper.llm.llm_debate` for the
        adjudication round so the public API path is the same one
        that handles cost-ledger persistence. Returns a dict with
        ``text``, ``prompt_tokens``, ``completion_tokens``,
        ``cost_usd``, ``latency_ms``, ``model_id``, and the raw
        ``response_json`` for callers that need provider metadata.

        ``json_mode=True`` (default) requests
        ``response_format={"type": "json_object"}`` so the model
        emits parseable JSON for downstream consumers.
        """
        chosen_model = model or self._model
        t_start = time.perf_counter()
        response_json = self._chat_completion(
            messages=messages,
            model=chosen_model,
            response_format={"type": "json_object"} if json_mode else None,
        )
        latency_ms = int((time.perf_counter() - t_start) * 1000)
        usage = response_json.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        cost_usd = self._compute_cost_usd(prompt_tokens, completion_tokens)
        actual_model_id = str(response_json.get("model") or chosen_model)
        request_id = response_json.get("id")
        try:
            text = str(
                response_json["choices"][0]["message"]["content"] or ""
            )
        except (KeyError, IndexError, TypeError):
            text = ""

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
            "response_json": response_json,
        }


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


def score_ticker(
    ticker: str,
    context: Mapping[str, Any],
    **client_kwargs: Any,
) -> dict[str, Any]:
    """One-shot helper: build a default :class:`XAIClient` and score.

    Useful for ad-hoc scripts and the smoke import in
    :mod:`biotech_sniper.audit`. Production paths should hold a
    long-lived :class:`XAIClient` so the underlying
    :class:`requests.Session` can pool keep-alive connections across
    the 600-ticker sweep.
    """
    client = XAIClient(**client_kwargs)
    return client.score_ticker(ticker, context)


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
