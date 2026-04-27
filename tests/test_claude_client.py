"""Tests for :mod:`biotech_sniper.llm.claude_client`.

These tests are fully hermetic: the live Anthropic API is never hit.
We replay hand-crafted cassettes from
``tests/fixtures/cassettes/claude/`` through a tiny fake Anthropic
SDK client. Each cassette is a JSON file with an ordered list of
``interactions``; the fake client consumes them in order and returns
SDK-shaped :class:`Message` doubles (or raises typed SDK exceptions).

The cassettes here contain no secret material — the fake client
never inspects request headers, and the model id / request id values
are scrubbed by construction.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import anthropic
import pytest

from biotech_sniper import db
from biotech_sniper.llm import claude_client
from biotech_sniper.llm.claude_client import (
    ClaudeAuthError,
    ClaudeClient,
    ClaudeError,
    ClaudeParseError,
)


CASSETTE_DIR = Path(__file__).parent / "fixtures" / "cassettes" / "claude"


# ---------------------------------------------------------------------------
# Fake Anthropic SDK doubles
# ---------------------------------------------------------------------------


class _FakeUsage:
    """Mirrors the SDK's ``Message.usage`` attribute surface."""

    def __init__(self, input_tokens: int, output_tokens: int):
        self.input_tokens = int(input_tokens)
        self.output_tokens = int(output_tokens)


class _FakeTextBlock:
    """Mirrors a Claude ``TextBlock`` content entry."""

    type = "text"

    def __init__(self, text: str):
        self.text = text


class _FakeMessage:
    """Mirrors :class:`anthropic.types.Message` for cassette replay."""

    def __init__(
        self,
        *,
        id: str,
        model: str,
        text: str,
        usage: _FakeUsage,
    ):
        self.id = id
        self.model = model
        self.content = [_FakeTextBlock(text)]
        self.usage = usage


class _FakeStreamManager:
    """Context manager mirroring ``client.messages.stream(...)``.

    On enter, exposes ``.text_stream`` (iterator of partial text
    deltas) and ``.get_final_message()`` returning the assembled
    :class:`_FakeMessage`.
    """

    def __init__(self, *, chunks: list[str], message: _FakeMessage):
        self._chunks = list(chunks)
        self._message = message

    def __enter__(self) -> "_FakeStreamManager":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None

    @property
    def text_stream(self):
        # Yield one chunk at a time, mirroring real SSE pacing.
        for chunk in self._chunks:
            yield chunk

    def get_final_message(self) -> _FakeMessage:
        return self._message


class _FakeMessages:
    """Mimics ``client.messages.{create,stream}`` surface."""

    def __init__(self, parent: "_FakeAnthropicClient"):
        self._parent = parent

    def create(self, **kwargs: Any) -> _FakeMessage:
        return self._parent._consume_create_or_stream(stream=False, kwargs=kwargs)

    def stream(self, **kwargs: Any) -> _FakeStreamManager:
        return self._parent._consume_create_or_stream(stream=True, kwargs=kwargs)


class _FakeAnthropicClient:
    """Replay-only Anthropic client double driven by a cassette."""

    def __init__(self, cassette: dict[str, Any]):
        self._interactions = list(cassette["interactions"])
        self._cursor = 0
        self.calls: list[dict[str, Any]] = []
        self.messages = _FakeMessages(self)

    def _next(self) -> dict[str, Any]:
        if self._cursor >= len(self._interactions):
            raise AssertionError(
                f"_FakeAnthropicClient exhausted after {self._cursor} calls; "
                "cassette has no more interactions"
            )
        interaction = self._interactions[self._cursor]
        self._cursor += 1
        return interaction

    def _consume_create_or_stream(
        self, *, stream: bool, kwargs: dict[str, Any]
    ) -> Any:
        interaction = self._next()
        self.calls.append({"stream": stream, "kwargs": kwargs})

        kind = interaction.get("type")

        if kind == "error":
            raise _build_sdk_error(interaction["error"])

        if kind == "create":
            if stream:
                raise AssertionError(
                    "cassette interaction was 'create' but the client "
                    "called stream(...) — fix the cassette ordering"
                )
            return _build_message(interaction["result"])

        if kind == "stream":
            if not stream:
                raise AssertionError(
                    "cassette interaction was 'stream' but the client "
                    "called create(...) — fix the cassette ordering"
                )
            result = interaction["result"]
            chunks = list(result.get("content_chunks", []))
            full_text = "".join(chunks)
            usage = _FakeUsage(
                input_tokens=int(result.get("usage", {}).get("input_tokens", 0)),
                output_tokens=int(result.get("usage", {}).get("output_tokens", 0)),
            )
            message = _FakeMessage(
                id=str(result.get("id", "msg_unknown")),
                model=str(result.get("model", "claude-test")),
                text=full_text,
                usage=usage,
            )
            return _FakeStreamManager(chunks=chunks, message=message)

        raise AssertionError(f"unknown cassette interaction type: {kind!r}")


def _build_message(result: dict[str, Any]) -> _FakeMessage:
    usage_raw = result.get("usage", {})
    return _FakeMessage(
        id=str(result.get("id", "msg_unknown")),
        model=str(result.get("model", "claude-test")),
        text=str(result.get("content_text", "")),
        usage=_FakeUsage(
            input_tokens=int(usage_raw.get("input_tokens", 0)),
            output_tokens=int(usage_raw.get("output_tokens", 0)),
        ),
    )


def _build_sdk_error(spec: dict[str, Any]) -> Exception:
    """Build the SDK-style exception named in a cassette ``error`` entry.

    We construct a bare-bones instance compatible with the SDK
    exception hierarchy. The wrapper code only inspects ``status_code``
    and the exception type, so we don't need to populate the full
    HTTP-response surface.
    """
    cls_name = spec.get("class", "APIError")
    message = spec.get("message", "fake error")

    if cls_name == "AuthenticationError":
        # Subclass to allow construction without a real Response.
        class _FakeAuthError(anthropic.AuthenticationError):
            def __init__(self, msg: str):
                Exception.__init__(self, msg)
                self.message = msg
                self.status_code = 401

        return _FakeAuthError(message)

    if cls_name == "RateLimitError":
        class _FakeRateLimit(anthropic.RateLimitError):
            def __init__(self, msg: str):
                Exception.__init__(self, msg)
                self.message = msg
                self.status_code = 429

        return _FakeRateLimit(message)

    if cls_name == "APIConnectionError":
        class _FakeConn(anthropic.APIConnectionError):
            def __init__(self, msg: str):
                Exception.__init__(self, msg)
                self.message = msg

        return _FakeConn(message)

    if cls_name == "APITimeoutError":
        class _FakeTimeout(anthropic.APITimeoutError):
            def __init__(self, msg: str):
                Exception.__init__(self, msg)
                self.message = msg

        return _FakeTimeout(message)

    if cls_name == "APIStatusError":
        class _FakeStatus(anthropic.APIStatusError):
            def __init__(self, msg: str, status_code: int):
                Exception.__init__(self, msg)
                self.message = msg
                self.status_code = status_code

        return _FakeStatus(message, int(spec.get("status_code", 500)))

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
    """Factory returning ``(client, fake_anthropic_client)``."""

    def _factory(
        cassette_name: str,
        *,
        no_sleep: bool = True,
        use_streaming: bool = False,
        **kwargs: Any,
    ):
        cassette = _load_cassette(cassette_name)
        fake = _FakeAnthropicClient(cassette)
        client = ClaudeClient(
            api_key="sk-ant-test-fixture-key",
            db_path=temp_db_path,
            client=fake,
            backoff_base=0.0 if no_sleep else 0.5,
            max_retries=3,
            use_streaming=use_streaming,
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
        "biotech_sniper.llm.claude_client.config.get_anthropic_api_key",
        lambda: None,
    )
    with pytest.raises(ClaudeAuthError, match="ANTHROPIC_API_KEY"):
        ClaudeClient(db_path=temp_db_path)


def test_constructor_uses_config_when_api_key_arg_omitted(monkeypatch, temp_db_path):
    sentinel = "sk-ant-from-config-helper"
    monkeypatch.setattr(
        "biotech_sniper.llm.claude_client.config.get_anthropic_api_key",
        lambda: sentinel,
    )
    # Pass a fake client to avoid building a real anthropic.Anthropic
    # (which would otherwise validate the key shape on construction).
    fake = _FakeAnthropicClient({"interactions": []})
    client = ClaudeClient(db_path=temp_db_path, client=fake)
    assert client._api_key == sentinel  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Happy path: deep_science_review (VAL-M2-036)
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
    assert result["letter_grade"] in claude_client.LETTER_GRADE_ORDER

    # Probability domain.
    assert isinstance(result["probability"], float)
    assert 0.0 <= result["probability"] <= 1.0

    # Rationale and citations.
    assert isinstance(result["rationale"], str) and result["rationale"].strip()
    assert isinstance(result["citations"], list) and result["citations"]
    for c in result["citations"]:
        assert "source" in c and isinstance(c["source"], str)
        # Per validator: each citation has 'source' and 'quote' or 'url'.
        # Our normaliser emits 'quote_or_url' which satisfies "or".
        assert "quote_or_url" in c

    # Model id begins with 'claude-' (VAL-M2-036).
    assert isinstance(result["model_id"], str)
    assert result["model_id"].startswith("claude-")

    assert isinstance(result["latency_ms"], int) and result["latency_ms"] >= 0
    assert isinstance(result["cost_usd"], float) and result["cost_usd"] >= 0.0

    # Exactly one SDK call on the happy path.
    assert len(fake.calls) == 1
    assert fake.calls[0]["stream"] is False


def test_deep_science_review_passes_system_and_user_messages(
    make_client, sample_science_profile, sample_full_context
):
    """Sanity: system prompt + user prompt are both forwarded to Claude."""
    client, fake = make_client("deep_science_review_success.json")
    client.deep_science_review(sample_science_profile, sample_full_context)
    call = fake.calls[0]["kwargs"]
    assert call.get("system") == claude_client.DEEP_SCIENCE_SYSTEM_PROMPT
    messages = call.get("messages")
    assert isinstance(messages, list) and len(messages) == 1
    assert messages[0]["role"] == "user"
    # The user prompt should embed the structured science_profile JSON.
    assert "preliminary_science_profile" in messages[0]["content"]
    assert "NCT05123456" in messages[0]["content"]


def test_deep_science_review_uses_provided_model_override(
    make_client, sample_science_profile, sample_full_context
):
    client, fake = make_client("deep_science_review_success.json")
    client.deep_science_review(
        sample_science_profile,
        sample_full_context,
        model="claude-3-opus-20240229",
    )
    assert fake.calls[0]["kwargs"]["model"] == "claude-3-opus-20240229"


# ---------------------------------------------------------------------------
# JSON-mode robustness (VAL-M2-037)
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
    # Re-prompt prepends the strict-JSON directive.
    second_prompt = fake.calls[1]["kwargs"]["messages"][0]["content"]
    assert "STRICT JSON" in second_prompt
    # And the recovered result is the second cassette entry.
    assert result["model_id"] == "claude-opus-4-1-20250805"
    assert result["letter_grade"] == "B+"


def test_persistent_malformed_json_raises_parse_error(temp_db_path, sample_science_profile, sample_full_context):
    """If both attempts return malformed bodies, raise ClaudeParseError."""
    cassette = {
        "interactions": [
            {
                "type": "create",
                "result": {
                    "id": "msg_bad_1",
                    "model": "claude-opus-4-1-20250805",
                    "content_text": "not json at all { ?",
                    "usage": {"input_tokens": 50, "output_tokens": 10},
                },
            },
            {
                "type": "create",
                "result": {
                    "id": "msg_bad_2",
                    "model": "claude-opus-4-1-20250805",
                    "content_text": "still not json },",
                    "usage": {"input_tokens": 60, "output_tokens": 10},
                },
            },
        ]
    }
    fake = _FakeAnthropicClient(cassette)
    client = ClaudeClient(
        api_key="sk-ant-test",
        db_path=temp_db_path,
        client=fake,
        backoff_base=0.0,
        max_retries=3,
    )
    with pytest.raises(ClaudeParseError):
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
    parsed = ClaudeClient._parse_assistant_text(fenced)
    assert parsed["letter_grade"] == "B"


def test_parse_rejects_invalid_letter_grade():
    bad = json.dumps(
        {
            "science_profile": {"moa": "x"},
            "letter_grade": "Z+",  # not in LETTER_GRADE_ORDER
            "probability": 0.5,
            "rationale": "ok",
            "citations": [{"source": "NCT1", "quote_or_url": "u"}],
        }
    )
    with pytest.raises(ClaudeParseError, match="letter_grade"):
        ClaudeClient._parse_assistant_text(bad)


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
    with pytest.raises(ClaudeParseError, match="probability"):
        ClaudeClient._parse_assistant_text(bad)


def test_parse_rejects_missing_citations_field():
    bad = json.dumps(
        {
            "science_profile": {"moa": "x"},
            "letter_grade": "B",
            "probability": 0.5,
            "rationale": "ok",
            # citations missing
        }
    )
    with pytest.raises(ClaudeParseError, match="citations"):
        ClaudeClient._parse_assistant_text(bad)


# ---------------------------------------------------------------------------
# Streaming reassembly (VAL-M2-038)
# ---------------------------------------------------------------------------


def test_streaming_reassembly(
    make_client, sample_science_profile, sample_full_context
):
    """Streaming chunks must reassemble into the same structured payload."""
    client, fake = make_client(
        "streaming_assembly.json", use_streaming=True
    )

    result = client.deep_science_review(
        sample_science_profile, sample_full_context
    )

    # Cassette declares 10 chunks; assembled payload must parse identically.
    assert fake.calls[0]["stream"] is True
    assert result["letter_grade"] == "B+"
    assert result["probability"] == pytest.approx(0.58)
    assert "Reassembled from streamed deltas" in result["rationale"]
    # Cost still computed from final usage (input_tokens=412, output=240).
    expected_cost = round((412 * 0.015 + 240 * 0.075) / 1000, 6)
    assert result["cost_usd"] == pytest.approx(expected_cost, abs=1e-9)


# ---------------------------------------------------------------------------
# Cost ledger (VAL-M2-039)
# ---------------------------------------------------------------------------


def test_cost_ledger_row_anthropic(
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
            "FROM llm_cost_ledger WHERE provider = 'anthropic'"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "anthropic"
    assert row["model_id"].startswith("claude-")
    assert row["purpose"] == "deep_science"
    assert row["prompt_tokens"] == 412
    assert row["completion_tokens"] == 386
    assert row["latency_ms"] is not None and row["latency_ms"] >= 0
    assert row["cost_usd"] is not None and row["cost_usd"] >= 0
    assert row["request_id"] == "msg_deep_review_001"


def test_cost_uses_published_pricing(
    make_client, sample_science_profile, sample_full_context
):
    client, _ = make_client("deep_science_review_success.json")
    result = client.deep_science_review(
        sample_science_profile, sample_full_context
    )
    expected = round((412 * 0.015 + 386 * 0.075) / 1000, 6)
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
    assert result["probability"] == pytest.approx(0.41)
    assert result["letter_grade"] == "B"


def test_fails_fast_on_auth_error(
    make_client, sample_science_profile, sample_full_context
):
    client, fake = make_client("auth_401.json")
    with pytest.raises(ClaudeAuthError):
        client.deep_science_review(sample_science_profile, sample_full_context)
    # Exactly one SDK call — no retry storm.
    assert len(fake.calls) == 1


def test_failed_auth_does_not_write_cost_ledger(
    make_client, sample_science_profile, sample_full_context, temp_db_path
):
    client, _ = make_client("auth_401.json")
    with pytest.raises(ClaudeAuthError):
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
# Module hygiene: no raw os.environ for secrets (mirrors xai_client check)
# ---------------------------------------------------------------------------


def test_claude_client_module_has_no_raw_os_environ_lookup():
    """Mission policy: secrets MUST come through config.py only.

    A static check ensures no future edit accidentally introduces
    ``os.environ.get`` / ``os.getenv`` for the Anthropic key.
    """
    src = Path(claude_client.__file__).read_text(encoding="utf-8")
    assert "os.environ" not in src, (
        "claude_client.py must not read os.environ directly"
    )
    assert "os.getenv" not in src, (
        "claude_client.py must not call os.getenv directly"
    )


# ---------------------------------------------------------------------------
# Prompt builder — biotech-specific science cues
# ---------------------------------------------------------------------------


def test_system_prompt_contains_biotech_science_cues():
    """The deep-science system prompt must mention each cue called out
    in the feature description: MoA, comparator, prior phase data, and
    indication base rate.
    """
    prompt = claude_client.DEEP_SCIENCE_SYSTEM_PROMPT
    # Case-insensitive checks because the prompt uses sentence case.
    lower = prompt.lower()
    assert "mechanism of action" in lower or "moa" in lower
    assert "comparator" in lower
    assert "prior phase" in lower or "phase 1/2" in lower
    assert "base rate" in lower
    # Bonus: trial-design risks should be present.
    assert "design risk" in lower or "trial design" in lower


def test_build_deep_science_prompt_serialises_inputs(
    sample_science_profile, sample_full_context
):
    prompt = claude_client.build_deep_science_prompt(
        sample_science_profile, sample_full_context
    )
    assert "preliminary_science_profile" in prompt
    assert "full_context" in prompt
    assert "NCT05123456" in prompt
    # Ensure JSON-serialised, not str(dict).
    assert '"primary_endpoint": "PFS"' in prompt


# ---------------------------------------------------------------------------
# Module-level convenience wrapper
# ---------------------------------------------------------------------------


def test_module_level_deep_science_review_uses_config_key(
    monkeypatch, sample_science_profile, sample_full_context, temp_db_path
):
    """The module-level helper must build a default ClaudeClient.

    We patch the config helper to a sentinel and inject a fake SDK
    client via ``client=`` kwarg passed through to the helper.
    """
    monkeypatch.setattr(
        "biotech_sniper.llm.claude_client.config.get_anthropic_api_key",
        lambda: "sk-ant-helper",
    )
    fake = _FakeAnthropicClient(_load_cassette("deep_science_review_success.json"))
    result = claude_client.deep_science_review(
        sample_science_profile,
        sample_full_context,
        client=fake,
        db_path=temp_db_path,
        backoff_base=0.0,
    )
    assert result["letter_grade"] == "B+"
    assert result["model_id"].startswith("claude-")
