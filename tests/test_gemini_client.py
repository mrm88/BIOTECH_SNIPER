"""Tests for :mod:`biotech_sniper.llm.gemini_client`.

These tests are fully hermetic: the live Gemini API is never hit.
We replay hand-crafted cassettes from
``tests/fixtures/cassettes/gemini/`` through a tiny fake Google
GenAI SDK client. Each cassette is a JSON file with an ordered list
of ``interactions``; the fake client consumes them in order and
returns SDK-shaped :class:`GenerateContentResponse` doubles (or
raises typed SDK exceptions).

The cassettes here contain no secret material — the fake client
never inspects request headers, and the model id / response id
values are scrubbed by construction.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from google.genai import errors as genai_errors

from biotech_sniper import db
from biotech_sniper.llm import claude_client, gemini_client
from biotech_sniper.llm.gemini_client import (
    GeminiAuthError,
    GeminiClient,
    GeminiError,
    GeminiParseError,
)


CASSETTE_DIR = Path(__file__).parent / "fixtures" / "cassettes" / "gemini"


# ---------------------------------------------------------------------------
# Fake Google GenAI SDK doubles
# ---------------------------------------------------------------------------


class _FakeUsage:
    """Mirrors :class:`GenerateContentResponseUsageMetadata`."""

    def __init__(self, prompt_token_count: int, candidates_token_count: int):
        self.prompt_token_count = int(prompt_token_count)
        self.candidates_token_count = int(candidates_token_count)


class _FakeResponse:
    """Mirrors :class:`google.genai.types.GenerateContentResponse`.

    We expose just the surface ``GeminiClient`` consumes: ``text``
    (string concatenation of all text parts), ``usage_metadata``,
    ``model_version``, and a synthetic ``response_id`` used by the
    cost-ledger row.
    """

    def __init__(
        self,
        *,
        response_id: str,
        model: str,
        text: str,
        usage: _FakeUsage,
    ):
        self.response_id = response_id
        self.model_version = model
        self.text = text
        self.usage_metadata = usage


class _FakeModels:
    """Mimics ``client.models`` surface — only ``generate_content``."""

    def __init__(self, parent: "_FakeGenAIClient"):
        self._parent = parent

    def generate_content(self, **kwargs: Any) -> _FakeResponse:
        return self._parent._consume_generate_content(kwargs)


class _FakeGenAIClient:
    """Replay-only Google GenAI client double driven by a cassette."""

    def __init__(self, cassette: dict[str, Any]):
        self._interactions = list(cassette["interactions"])
        self._cursor = 0
        self.calls: list[dict[str, Any]] = []
        self.models = _FakeModels(self)

    def _next(self) -> dict[str, Any]:
        if self._cursor >= len(self._interactions):
            raise AssertionError(
                f"_FakeGenAIClient exhausted after {self._cursor} calls; "
                "cassette has no more interactions"
            )
        interaction = self._interactions[self._cursor]
        self._cursor += 1
        return interaction

    def _consume_generate_content(self, kwargs: dict[str, Any]) -> _FakeResponse:
        interaction = self._next()
        self.calls.append({"kwargs": kwargs})

        kind = interaction.get("type")

        if kind == "error":
            raise _build_sdk_error(interaction["error"])

        if kind == "create":
            return _build_response(interaction["result"])

        raise AssertionError(f"unknown cassette interaction type: {kind!r}")


def _build_response(result: dict[str, Any]) -> _FakeResponse:
    usage_raw = result.get("usage", {})
    return _FakeResponse(
        response_id=str(result.get("id", "resp_unknown")),
        model=str(result.get("model", "gemini-test")),
        text=str(result.get("content_text", "")),
        usage=_FakeUsage(
            prompt_token_count=int(usage_raw.get("input_tokens", 0)),
            candidates_token_count=int(usage_raw.get("output_tokens", 0)),
        ),
    )


def _build_sdk_error(spec: dict[str, Any]) -> Exception:
    """Build a SDK-style exception named in a cassette ``error`` entry.

    The real :class:`google.genai.errors.APIError` constructor needs
    a :class:`requests.Response`; we sidestep that by subclassing and
    bypassing the base ``__init__``. The wrapper only inspects the
    ``code`` attribute and the exception class, so this minimal
    surface is sufficient.
    """
    cls_name = spec.get("class", "ClientError")
    code = int(spec.get("code", 0))
    message = spec.get("message", "fake error")

    if cls_name == "ClientError":
        class _FakeClientError(genai_errors.ClientError):
            def __init__(self, msg: str, http_code: int):
                Exception.__init__(self, msg)
                self.message = msg
                self.code = http_code
                self.status = "ERROR"
                self.details = {"message": msg}

        return _FakeClientError(message, code)

    if cls_name == "ServerError":
        class _FakeServerError(genai_errors.ServerError):
            def __init__(self, msg: str, http_code: int):
                Exception.__init__(self, msg)
                self.message = msg
                self.code = http_code
                self.status = "ERROR"
                self.details = {"message": msg}

        return _FakeServerError(message, code)

    raise AssertionError(f"unknown cassette error class: {cls_name!r}")


def _load_cassette(name: str) -> dict[str, Any]:
    path = CASSETTE_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Common fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "alpha.db"


@pytest.fixture
def make_client(temp_db_path: Path):
    """Factory returning ``(client, fake_genai_client)``."""

    def _factory(
        cassette_name: str,
        *,
        no_sleep: bool = True,
        **kwargs: Any,
    ):
        cassette = _load_cassette(cassette_name)
        fake = _FakeGenAIClient(cassette)
        client = GeminiClient(
            api_key="gemini-test-fixture-key",
            db_path=temp_db_path,
            client=fake,
            backoff_base=0.0 if no_sleep else 0.5,
            max_retries=3,
            **kwargs,
        )
        return client, fake

    return _factory


@pytest.fixture
def sample_science_profile() -> dict[str, Any]:
    return {
        "ticker": "IDYA",
        "moa_seed": "MAT2A inhibition",
        "indication_seed": "MTAP-del NSCLC, 2L",
        "phase": 3,
    }


@pytest.fixture
def sample_full_context() -> dict[str, Any]:
    return {
        "ct_gov": {
            "nct_id": "NCT05123456",
            "phase": "PHASE3",
            "primary_endpoint": "PFS",
            "comparator": "docetaxel",
            "estimated_enrolment": 420,
        },
        "prior_readouts": [
            {"phase": 2, "orr_pct": 32, "n": 58, "median_dor_mo": 7.2}
        ],
        "recent_filings": [
            {"form": "8-K", "date": "2026-04-15", "summary": "DSMB safety review"}
        ],
        "indication_base_rate_p3": 0.45,
    }


# ---------------------------------------------------------------------------
# Constructor / config sourcing
# ---------------------------------------------------------------------------


def test_constructor_raises_when_api_key_missing(monkeypatch, temp_db_path):
    monkeypatch.setattr(
        "biotech_sniper.llm.gemini_client.config.get_gemini_api_key",
        lambda: None,
    )
    with pytest.raises(GeminiAuthError, match="GEMINI_API_KEY"):
        GeminiClient(db_path=temp_db_path)


def test_constructor_uses_config_when_api_key_arg_omitted(monkeypatch, temp_db_path):
    sentinel = "gemini-from-config-helper"
    monkeypatch.setattr(
        "biotech_sniper.llm.gemini_client.config.get_gemini_api_key",
        lambda: sentinel,
    )
    fake = _FakeGenAIClient({"interactions": []})
    client = GeminiClient(db_path=temp_db_path, client=fake)
    assert client._api_key == sentinel  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Happy path: deep_science_review (VAL-M2-040 / VAL-M2-041)
# ---------------------------------------------------------------------------


def test_deep_science_review_shape(
    make_client, sample_science_profile, sample_full_context
):
    """Returned object must carry the validation-contract keys + types."""
    client, fake = make_client("deep_science_review_success.json")

    result = client.deep_science_review(
        sample_science_profile, sample_full_context
    )

    # All required keys present.
    for key in (
        "science_profile",
        "letter_grade",
        "probability",
        "rationale",
        "citations",
        "model_id",
        "latency_ms",
        "cost_usd",
    ):
        assert key in result, f"missing required key {key!r}"

    # Letter-grade domain.
    assert result["letter_grade"] in gemini_client.LETTER_GRADE_ORDER

    # Probability domain.
    assert isinstance(result["probability"], float)
    assert 0.0 <= result["probability"] <= 1.0

    # Rationale and citations.
    assert isinstance(result["rationale"], str) and result["rationale"].strip()
    assert isinstance(result["citations"], list) and result["citations"]
    for c in result["citations"]:
        assert "source" in c and isinstance(c["source"], str)
        assert "quote_or_url" in c

    # Model id begins with 'gemini-' (VAL-M2-043).
    assert isinstance(result["model_id"], str)
    assert result["model_id"].startswith("gemini-")

    assert isinstance(result["latency_ms"], int) and result["latency_ms"] >= 0
    assert isinstance(result["cost_usd"], float) and result["cost_usd"] >= 0.0

    # Exactly one SDK call on the happy path.
    assert len(fake.calls) == 1


def test_output_shape_matches_claude(
    make_client, sample_science_profile, sample_full_context
):
    """VAL-M2-041: same key set + value-type set as ClaudeClient.

    We exclude ``model_id`` and ``cost_usd`` per the contract — those
    are provider-specific by design.
    """
    client, _ = make_client("deep_science_review_success.json")
    gemini_result = client.deep_science_review(
        sample_science_profile, sample_full_context
    )

    # Reference claude shape: load the claude success cassette through
    # the claude client to construct an equivalent reference dict.
    from tests.test_claude_client import (  # noqa: E402 (import-by-design)
        _FakeAnthropicClient,
        _load_cassette as _load_claude,
    )
    from biotech_sniper.llm.claude_client import ClaudeClient

    claude_fake = _FakeAnthropicClient(
        _load_claude("deep_science_review_success.json")
    )
    claude = ClaudeClient(
        api_key="sk-ant-test",
        client=claude_fake,
        backoff_base=0.0,
    )
    claude_result = claude.deep_science_review(
        sample_science_profile, sample_full_context
    )

    # Same key set.
    assert set(gemini_result.keys()) == set(claude_result.keys())

    # Same value-type set, excluding model_id (string but provider-specific
    # prefix) and cost_usd (provider-specific pricing).
    for key in gemini_result:
        if key in {"model_id", "cost_usd"}:
            continue
        assert type(gemini_result[key]) is type(claude_result[key]), (
            f"type mismatch for {key!r}: "
            f"gemini={type(gemini_result[key]).__name__}, "
            f"claude={type(claude_result[key]).__name__}"
        )


def test_deep_science_review_passes_system_and_user_prompt(
    make_client, sample_science_profile, sample_full_context
):
    """Sanity: system instruction + user prompt are forwarded."""
    client, fake = make_client("deep_science_review_success.json")
    client.deep_science_review(sample_science_profile, sample_full_context)
    call = fake.calls[0]["kwargs"]

    # The user-side prompt is forwarded as ``contents``.
    assert "preliminary_science_profile" in call["contents"]
    assert "NCT05123456" in call["contents"]

    # The system instruction lives in the GenerateContentConfig.
    cfg = call["config"]
    sys_inst = getattr(cfg, "system_instruction", None)
    assert sys_inst == gemini_client.DEEP_SCIENCE_SYSTEM_PROMPT

    # JSON-mode is enabled.
    assert getattr(cfg, "response_mime_type", None) == "application/json"


def test_deep_science_review_uses_provided_model_override(
    make_client, sample_science_profile, sample_full_context
):
    client, fake = make_client("deep_science_review_success.json")
    client.deep_science_review(
        sample_science_profile,
        sample_full_context,
        model="gemini-2.5-pro-preview",
    )
    assert fake.calls[0]["kwargs"]["model"] == "gemini-2.5-pro-preview"


# ---------------------------------------------------------------------------
# JSON-mode robustness
# ---------------------------------------------------------------------------


def test_json_mode_recovers_from_malformed_response(
    make_client, sample_science_profile, sample_full_context
):
    """Truncated JSON triggers a single deterministic re-prompt."""
    client, fake = make_client("malformed_then_valid.json")
    result = client.deep_science_review(
        sample_science_profile, sample_full_context
    )

    # Two SDK calls: malformed → re-prompt → success.
    assert len(fake.calls) == 2
    second_prompt = fake.calls[1]["kwargs"]["contents"]
    assert "STRICT JSON" in second_prompt
    assert result["model_id"] == "gemini-2.5-pro"
    assert result["letter_grade"] == "B+"


def test_persistent_malformed_json_raises_parse_error(
    temp_db_path, sample_science_profile, sample_full_context
):
    """If both attempts return malformed bodies, raise GeminiParseError."""
    cassette = {
        "interactions": [
            {
                "type": "create",
                "result": {
                    "id": "resp_bad_1",
                    "model": "gemini-2.5-pro",
                    "content_text": "not json at all { ?",
                    "usage": {"input_tokens": 50, "output_tokens": 10},
                },
            },
            {
                "type": "create",
                "result": {
                    "id": "resp_bad_2",
                    "model": "gemini-2.5-pro",
                    "content_text": "still not json },",
                    "usage": {"input_tokens": 60, "output_tokens": 10},
                },
            },
        ]
    }
    fake = _FakeGenAIClient(cassette)
    client = GeminiClient(
        api_key="gemini-test",
        db_path=temp_db_path,
        client=fake,
        backoff_base=0.0,
        max_retries=3,
    )
    with pytest.raises(GeminiParseError):
        client.deep_science_review(sample_science_profile, sample_full_context)


def test_strips_markdown_fences():
    """Models occasionally wrap JSON in ```json fences; we tolerate."""
    fenced = (
        "```json\n"
        '{"science_profile": {"moa": "x"}, "letter_grade": "B", '
        '"probability": 0.5, "rationale": "ok citation provided", '
        '"citations": [{"source": "NCT1", "quote_or_url": "https://x"}]}\n'
        "```"
    )
    parsed = GeminiClient._parse_assistant_text(fenced)
    assert parsed["letter_grade"] == "B"


def test_parse_rejects_invalid_letter_grade():
    bad = json.dumps(
        {
            "science_profile": {"moa": "x"},
            "letter_grade": "Z+",
            "probability": 0.5,
            "rationale": "ok",
            "citations": [{"source": "NCT1", "quote_or_url": "u"}],
        }
    )
    with pytest.raises(GeminiParseError, match="letter_grade"):
        GeminiClient._parse_assistant_text(bad)


def test_parse_rejects_probability_out_of_range():
    bad = json.dumps(
        {
            "science_profile": {"moa": "x"},
            "letter_grade": "B",
            "probability": 1.5,
            "rationale": "ok",
            "citations": [{"source": "NCT1", "quote_or_url": "u"}],
        }
    )
    with pytest.raises(GeminiParseError, match="probability"):
        GeminiClient._parse_assistant_text(bad)


def test_parse_rejects_missing_citations_field():
    bad = json.dumps(
        {
            "science_profile": {"moa": "x"},
            "letter_grade": "B",
            "probability": 0.5,
            "rationale": "ok",
        }
    )
    with pytest.raises(GeminiParseError, match="citations"):
        GeminiClient._parse_assistant_text(bad)


# ---------------------------------------------------------------------------
# Cost ledger (VAL-M2-043)
# ---------------------------------------------------------------------------


def test_cost_ledger_row_gemini(
    make_client, sample_science_profile, sample_full_context, temp_db_path
):
    client, _ = make_client("deep_science_review_success.json")

    client.deep_science_review(
        sample_science_profile, sample_full_context, purpose="deep_science"
    )

    conn = db.connect(temp_db_path)
    try:
        rows = conn.execute(
            "SELECT provider, model_id, purpose, prompt_tokens, "
            "completion_tokens, latency_ms, cost_usd, request_id "
            "FROM llm_cost_ledger WHERE provider = 'gemini'"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "gemini"
    assert row["model_id"].startswith("gemini-")
    assert row["purpose"] == "deep_science"
    assert row["prompt_tokens"] == 412
    assert row["completion_tokens"] == 386
    assert row["latency_ms"] is not None and row["latency_ms"] >= 0
    assert row["cost_usd"] is not None and row["cost_usd"] >= 0
    assert row["request_id"] == "resp_deep_review_001"


def test_cost_uses_published_pricing(
    make_client, sample_science_profile, sample_full_context
):
    client, _ = make_client("deep_science_review_success.json")
    result = client.deep_science_review(
        sample_science_profile, sample_full_context
    )
    expected = round(
        (412 * gemini_client.GEMINI_INPUT_USD_PER_1K
         + 386 * gemini_client.GEMINI_OUTPUT_USD_PER_1K)
        / 1000,
        6,
    )
    assert result["cost_usd"] == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# Retry / auth handling
# ---------------------------------------------------------------------------


def test_retries_on_429_then_5xx_then_200(
    make_client, sample_science_profile, sample_full_context
):
    client, fake = make_client("retry_429_then_200.json")
    result = client.deep_science_review(
        sample_science_profile, sample_full_context
    )
    assert len(fake.calls) == 3
    assert result["probability"] == pytest.approx(0.38)
    assert result["letter_grade"] == "B-"


def test_fails_fast_on_auth_error(
    make_client, sample_science_profile, sample_full_context
):
    client, fake = make_client("auth_401.json")
    with pytest.raises(GeminiAuthError):
        client.deep_science_review(sample_science_profile, sample_full_context)
    # Exactly one SDK call — no retry storm.
    assert len(fake.calls) == 1


def test_failed_auth_does_not_write_cost_ledger(
    make_client, sample_science_profile, sample_full_context, temp_db_path
):
    client, _ = make_client("auth_401.json")
    with pytest.raises(GeminiAuthError):
        client.deep_science_review(sample_science_profile, sample_full_context)
    if not temp_db_path.exists():
        return
    conn = sqlite3.connect(temp_db_path)
    try:
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM llm_cost_ledger"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            n = 0
    finally:
        conn.close()
    assert n == 0


# ---------------------------------------------------------------------------
# Disable-via-feature-flag (VAL-M2-042 spirit)
# ---------------------------------------------------------------------------


def test_disabled_via_flag(monkeypatch):
    """When ``LLM_PROVIDERS_DEEP`` does not include ``gemini``, the
    config helper :func:`provider_enabled` returns False and the
    pipeline path that gates on it skips Gemini calls entirely.

    The module remains importable in this state — disabling Gemini
    must NEVER crash the pipeline.
    """
    # Ensure no API key is present so provider_enabled('gemini')
    # would be False even if the flag were set.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # Restrict deep tier to anthropic only.
    monkeypatch.setenv("LLM_PROVIDERS_DEEP", "anthropic")

    # Reload config so the env override is picked up.
    from biotech_sniper import config as _config
    importlib.reload(_config)
    try:
        assert _config.LLM_PROVIDERS["deep"] == ["anthropic"]
        assert _config.provider_enabled("gemini") is False
        assert _config.provider_enabled("anthropic") is False  # no key set

        # Module-level smoke import still works (no import-time crash).
        importlib.reload(gemini_client)
        assert gemini_client.GeminiClient is not None
        assert gemini_client.deep_science_review is not None
    finally:
        # Restore the unmodified config so subsequent tests see defaults.
        monkeypatch.delenv("LLM_PROVIDERS_DEEP", raising=False)
        importlib.reload(_config)
        importlib.reload(gemini_client)


def test_disabled_via_flag_pipeline_does_not_crash_when_key_missing(monkeypatch):
    """Even when GEMINI_API_KEY is missing, importing the module and
    consulting the feature flag must not raise.

    This is the pipeline-level invariant the validation contract calls
    out (VAL-M2-042 / feature expectedBehavior #3): "Disabling Gemini
    via config.LLM_PROVIDERS.deep without 'gemini' does not crash the
    pipeline".
    """
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDERS_DEEP", "anthropic")

    from biotech_sniper import config as _config
    importlib.reload(_config)
    try:
        # Pipeline-style guard: if the provider is disabled, never
        # construct the client at all. This must not raise.
        if _config.provider_enabled("gemini"):
            pytest.fail("provider_enabled returned True with no key + flag off")
    finally:
        monkeypatch.delenv("LLM_PROVIDERS_DEEP", raising=False)
        importlib.reload(_config)


# ---------------------------------------------------------------------------
# Module hygiene: no raw os.environ for secrets
# ---------------------------------------------------------------------------


def test_gemini_client_module_has_no_raw_os_environ_lookup():
    """Mission policy: secrets MUST come through config.py only.

    A static check ensures no future edit accidentally introduces
    ``os.environ.get`` / ``os.getenv`` for the Gemini key.
    """
    src = Path(gemini_client.__file__).read_text(encoding="utf-8")
    assert "os.environ" not in src, (
        "gemini_client.py must not read os.environ directly"
    )
    assert "os.getenv" not in src, (
        "gemini_client.py must not call os.getenv directly"
    )


# ---------------------------------------------------------------------------
# Prompt builder — biotech-specific science cues
# ---------------------------------------------------------------------------


def test_system_prompt_contains_biotech_science_cues():
    """The deep-science system prompt must mention each cue called out
    in the feature description: MoA, comparator, prior phase data, and
    indication base rate.
    """
    prompt = gemini_client.DEEP_SCIENCE_SYSTEM_PROMPT
    lower = prompt.lower()
    assert "mechanism of action" in lower or "moa" in lower
    assert "comparator" in lower
    assert "prior phase" in lower or "phase 1/2" in lower
    assert "base rate" in lower
    assert "design risk" in lower or "trial design" in lower


def test_build_deep_science_prompt_serialises_inputs(
    sample_science_profile, sample_full_context
):
    prompt = gemini_client.build_deep_science_prompt(
        sample_science_profile, sample_full_context
    )
    assert "preliminary_science_profile" in prompt
    assert "full_context" in prompt
    assert "NCT05123456" in prompt
    assert '"primary_endpoint": "PFS"' in prompt


# ---------------------------------------------------------------------------
# Letter-grade ordering shared with Claude
# ---------------------------------------------------------------------------


def test_letter_grade_order_shared_with_claude():
    """Divergence detection (VAL-M2-045) requires both deep clients to
    use the SAME letter-grade ordering. This test pins that invariant.
    """
    assert gemini_client.LETTER_GRADE_ORDER == claude_client.LETTER_GRADE_ORDER


# ---------------------------------------------------------------------------
# Module-level convenience wrapper
# ---------------------------------------------------------------------------


def test_module_level_deep_science_review_uses_config_key(
    monkeypatch, sample_science_profile, sample_full_context, temp_db_path
):
    """The module-level helper must build a default GeminiClient.

    We patch the config helper to a sentinel and inject a fake SDK
    client via ``client=`` kwarg passed through to the helper.
    """
    monkeypatch.setattr(
        "biotech_sniper.llm.gemini_client.config.get_gemini_api_key",
        lambda: "gemini-helper",
    )
    fake = _FakeGenAIClient(_load_cassette("deep_science_review_success.json"))
    result = gemini_client.deep_science_review(
        sample_science_profile,
        sample_full_context,
        client=fake,
        db_path=temp_db_path,
        backoff_base=0.0,
    )
    assert result["letter_grade"] == "A-"
    assert result["model_id"].startswith("gemini-")
