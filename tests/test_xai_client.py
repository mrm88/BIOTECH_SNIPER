"""Tests for :mod:`biotech_sniper.llm.xai_client`.

These tests are fully hermetic: the live xAI HTTP API is never hit.
We replay hand-crafted cassettes from
``tests/fixtures/cassettes/xai/`` through a tiny fake
:class:`requests.Session` substitute. Each cassette is a JSON file
with an ordered list of ``interactions`` (the same shape VCR uses,
minus the request-matching machinery — we replay strictly in order).

The cassettes here are scrubbed by construction: the fake session
never inspects the request ``Authorization`` header, and the
authentic-looking model id / request id values do not encode any
real key material.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import requests

from biotech_sniper import db
from biotech_sniper.llm import xai_client
from biotech_sniper.llm.xai_client import (
    XAIAuthError,
    XAIClient,
    XAIError,
    XAIParseError,
)


CASSETTE_DIR = Path(__file__).parent / "fixtures" / "cassettes" / "xai"


# ---------------------------------------------------------------------------
# Fake session replay machinery
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal :class:`requests.Response` substitute for cassette replay."""

    def __init__(self, *, status_code: int, body_json: Any = None, text: str | None = None):
        self.status_code = status_code
        self._body_json = body_json
        if text is not None:
            self.text = text
        elif body_json is not None:
            self.text = json.dumps(body_json)
        else:
            self.text = ""

    def json(self) -> Any:
        if self._body_json is None:
            # Mirror requests.Response.json() raising ValueError for
            # non-JSON bodies.
            raise ValueError("No JSON body in cassette response")
        return self._body_json


class _FakeSession:
    """Replays cassette interactions in order through a single ``post``.

    Tracks call count + URLs so tests can assert on retry behaviour.
    """

    def __init__(self, cassette: dict[str, Any]):
        self._interactions = list(cassette["interactions"])
        self._cursor = 0
        self.posted_urls: list[str] = []
        self.posted_payloads: list[dict[str, Any]] = []

    @property
    def calls(self) -> int:
        return len(self.posted_urls)

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        if self._cursor >= len(self._interactions):
            raise AssertionError(
                f"_FakeSession exhausted after {self._cursor} calls; "
                "cassette has no more interactions"
            )
        interaction = self._interactions[self._cursor]
        self._cursor += 1
        self.posted_urls.append(url)
        self.posted_payloads.append(kwargs.get("json", {}))

        resp_spec = interaction["response"]
        return _FakeResponse(
            status_code=int(resp_spec["status_code"]),
            body_json=resp_spec.get("json"),
            text=resp_spec.get("text"),
        )


def _load_cassette(name: str) -> dict[str, Any]:
    path = CASSETTE_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Common fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """An empty temp SQLite db path for the cost-ledger tests."""
    return tmp_path / "alpha.db"


@pytest.fixture
def make_client(temp_db_path: Path):
    """Factory returning ``(client, fake_session)`` from a cassette name."""

    def _factory(cassette_name: str, *, no_sleep: bool = True, **kwargs: Any):
        cassette = _load_cassette(cassette_name)
        fake = _FakeSession(cassette)
        client = XAIClient(
            api_key="xai-test-fixture-key",
            db_path=temp_db_path,
            session=fake,
            backoff_base=0.0 if no_sleep else 0.5,  # zero out sleeps in tests
            max_retries=3,
            **kwargs,
        )
        return client, fake

    return _factory


@pytest.fixture
def sample_context() -> dict[str, Any]:
    return {
        "catalyst_date": "2026-05-20",
        "catalyst_type": "P3_readout",
        "nct_id": "NCT05123456",
        "phase": 3,
        "indication": "small-cell lung cancer",
        "sponsor": "Idaya Biosciences",
        "recent_news": ["Filed 8-K on 2026-04-15 disclosing DSMB review"],
    }


# ---------------------------------------------------------------------------
# Constructor / config sourcing
# ---------------------------------------------------------------------------


def test_constructor_raises_when_api_key_missing(monkeypatch, temp_db_path):
    """Without a configured key, instantiation must raise XAIAuthError."""
    # Force config.get_xai_api_key() to return empty.
    monkeypatch.setattr(
        "biotech_sniper.llm.xai_client.config.get_xai_api_key",
        lambda: None,
    )
    with pytest.raises(XAIAuthError, match="XAI_API_KEY"):
        XAIClient(db_path=temp_db_path)


def test_constructor_uses_config_when_api_key_arg_omitted(monkeypatch, temp_db_path):
    """When constructor arg is omitted, the key MUST come from config."""
    sentinel = "xai-from-config-helper"
    monkeypatch.setattr(
        "biotech_sniper.llm.xai_client.config.get_xai_api_key",
        lambda: sentinel,
    )
    client = XAIClient(db_path=temp_db_path)
    assert client._api_key == sentinel  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Happy path: score_ticker
# ---------------------------------------------------------------------------


def test_score_ticker_returns_required_keys(make_client, sample_context):
    """VAL-M2-030: returned dict must carry the contract keys."""
    client, fake = make_client("score_ticker_success.json")

    result = client.score_ticker("IDYA", sample_context)

    # Required keys.
    for key in ("probability", "rationale", "confidence", "model_id", "latency_ms", "cost_usd"):
        assert key in result, f"missing required key {key!r}"

    # Value domains.
    assert isinstance(result["probability"], float)
    assert 0.0 <= result["probability"] <= 1.0
    assert isinstance(result["rationale"], str) and result["rationale"].strip()
    assert isinstance(result["confidence"], float)
    assert 0.0 <= result["confidence"] <= 1.0
    assert isinstance(result["model_id"], str)
    assert result["model_id"].startswith("grok-")
    assert isinstance(result["latency_ms"], int) and result["latency_ms"] >= 0
    assert isinstance(result["cost_usd"], float) and result["cost_usd"] >= 0.0

    assert fake.calls == 1


def test_score_ticker_posts_to_chat_completions_endpoint(make_client, sample_context):
    client, fake = make_client("score_ticker_success.json")
    client.score_ticker("IDYA", sample_context)
    assert fake.posted_urls == ["https://api.x.ai/v1/chat/completions"]


def test_score_ticker_requests_json_response_format(make_client, sample_context):
    """Body must include ``response_format={"type": "json_object"}``."""
    client, fake = make_client("score_ticker_success.json")
    client.score_ticker("IDYA", sample_context)
    payload = fake.posted_payloads[0]
    assert payload["response_format"] == {"type": "json_object"}
    # Sanity: messages include both system and user roles.
    roles = [m["role"] for m in payload["messages"]]
    assert "system" in roles and "user" in roles


def test_score_ticker_writes_cost_ledger_row(make_client, sample_context, temp_db_path):
    """VAL-M2-034: every successful call appends one llm_cost_ledger row."""
    client, _ = make_client("score_ticker_success.json")

    client.score_ticker("IDYA", sample_context, purpose="fast_rank")

    conn = db.connect(temp_db_path)
    try:
        rows = conn.execute(
            "SELECT provider, model_id, purpose, prompt_tokens, "
            "completion_tokens, latency_ms, cost_usd, request_id "
            "FROM llm_cost_ledger WHERE provider = 'xai'"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1, f"expected 1 xai cost row, got {len(rows)}"
    row = rows[0]
    assert row["provider"] == "xai"
    assert row["model_id"].startswith("grok-")
    assert row["purpose"] == "fast_rank"
    assert row["prompt_tokens"] == 184
    assert row["completion_tokens"] == 52
    assert row["latency_ms"] is not None and row["latency_ms"] >= 0
    assert row["cost_usd"] is not None and row["cost_usd"] >= 0
    assert row["request_id"] == "resp-fast-rank-001"


def test_score_ticker_cost_usd_uses_published_pricing(make_client, sample_context):
    """Cost is (prompt/1k * input_price) + (completion/1k * output_price)."""
    client, _ = make_client("score_ticker_success.json")
    result = client.score_ticker("IDYA", sample_context)
    # 184 prompt tokens * $0.005/1k + 52 completion tokens * $0.015/1k
    expected = round((184 * 0.005 + 52 * 0.015) / 1000, 6)
    assert result["cost_usd"] == pytest.approx(expected, abs=1e-9)


def test_two_calls_append_two_cost_rows(make_client, sample_context, temp_db_path):
    """Re-using the client across calls keeps appending rows."""
    client, _ = make_client("score_ticker_success.json")
    # Replace the fake with a fresh cassette duplicated 2× since we
    # only have one interaction in the success cassette.
    cassette = _load_cassette("score_ticker_success.json")
    cassette["interactions"] = cassette["interactions"] * 2
    client._session = _FakeSession(cassette)  # type: ignore[attr-defined]

    client.score_ticker("IDYA", sample_context)
    client.score_ticker("RVMD", sample_context)

    conn = db.connect(temp_db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM llm_cost_ledger WHERE provider='xai'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 2


# ---------------------------------------------------------------------------
# Retry / backoff behaviour
# ---------------------------------------------------------------------------


def test_retries_on_429_then_5xx_then_200(make_client, sample_context):
    """VAL-M2-032: 429 + 5xx are retried with backoff; final 200 wins."""
    client, fake = make_client("retry_429_then_200.json")

    result = client.score_ticker("IDYA", sample_context)

    # Three HTTP attempts in total: 429 → 503 → 200.
    assert fake.calls == 3
    assert result["probability"] == pytest.approx(0.41)
    assert result["model_id"] == "grok-4-0709"


def test_retries_exhausted_raises_xai_error(make_client, sample_context):
    """After 1 + max_retries attempts on retryable status, raise XAIError."""
    client, fake = make_client("retries_exhausted_429.json")

    with pytest.raises(XAIError):
        client.score_ticker("IDYA", sample_context)

    # Initial attempt + 3 retries = 4 HTTP calls total.
    assert fake.calls == 4


def test_retries_on_connection_error(monkeypatch, temp_db_path, sample_context):
    """Transient ConnectionError on the first attempt is retried."""
    cassette = _load_cassette("score_ticker_success.json")
    fake = _FakeSession(cassette)

    # Patch fake.post to fail once then defer to the cassette.
    real_post = fake.post
    state = {"failed": False}

    def flaky_post(url: str, **kwargs: Any) -> _FakeResponse:
        if not state["failed"]:
            state["failed"] = True
            raise requests.ConnectionError("simulated dropped connection")
        return real_post(url, **kwargs)

    fake.post = flaky_post  # type: ignore[assignment]

    client = XAIClient(
        api_key="xai-test",
        db_path=temp_db_path,
        session=fake,
        backoff_base=0.0,
        max_retries=3,
    )
    result = client.score_ticker("IDYA", sample_context)
    assert result["probability"] == pytest.approx(0.62)
    assert state["failed"] is True  # the simulated failure happened


# ---------------------------------------------------------------------------
# Auth: fail-fast on 401
# ---------------------------------------------------------------------------


def test_fails_fast_on_401(make_client, sample_context):
    """VAL-M2-033: 401 raises XAIAuthError immediately, no retry storm."""
    client, fake = make_client("auth_401.json")

    with pytest.raises(XAIAuthError):
        client.score_ticker("IDYA", sample_context)

    # Exactly one HTTP attempt — no retry storm.
    assert fake.calls == 1


def test_failed_call_does_not_write_cost_ledger(make_client, sample_context, temp_db_path):
    """A 401 must not insert a cost row; failures aren't billable."""
    client, _ = make_client("auth_401.json")
    with pytest.raises(XAIAuthError):
        client.score_ticker("IDYA", sample_context)

    # The db file may or may not exist (parent mkdir is in the cost
    # writer, which we never reach). If the file exists, the table is
    # empty.
    if not temp_db_path.exists():
        return
    conn = sqlite3.connect(temp_db_path)
    try:
        # Schema may not exist if the writer never ran.
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
# JSON-mode parsing edge cases
# ---------------------------------------------------------------------------


def test_malformed_json_body_raises_xai_parse_error(make_client, sample_context):
    """Non-JSON ``message.content`` raises XAIParseError, not XAIError."""
    client, _ = make_client("malformed_json_body.json")
    with pytest.raises(XAIParseError):
        client.score_ticker("IDYA", sample_context)


def test_parse_assistant_payload_validates_probability_range():
    """Probability outside [0,1] is rejected up front."""
    bad = {
        "choices": [{"message": {"content": json.dumps({"probability": 1.5, "rationale": "x"})}}]
    }
    with pytest.raises(XAIParseError):
        XAIClient._parse_assistant_payload(bad)


def test_parse_assistant_payload_clamps_confidence():
    """Confidence is clamped to [0,1] (numeric instability tolerance)."""
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {"probability": 0.5, "rationale": "ok", "confidence": 1.05}
                    )
                }
            }
        ]
    }
    parsed = XAIClient._parse_assistant_payload(payload)
    assert parsed["confidence"] == 1.0


# ---------------------------------------------------------------------------
# Module hygiene: no raw os.environ for secrets
# ---------------------------------------------------------------------------


def test_xai_client_module_has_no_raw_os_environ_lookup():
    """Mission policy: secrets MUST come through config.py only.

    A static check of the module source ensures no future edit
    accidentally introduces ``os.environ.get`` / ``os.getenv`` for the
    XAI key. (We allow the imports of stdlib ``os`` for path work, but
    reject the secret-reading APIs.)
    """
    src = Path(xai_client.__file__).read_text(encoding="utf-8")
    # Tolerant grep: the module shouldn't reference these symbols at
    # all. If a future edit needs them for a non-secret use, add a
    # pragma exception and update this test to permit a single, scoped
    # match.
    assert "os.environ" not in src, "xai_client.py must not read os.environ directly"
    assert "os.getenv" not in src, "xai_client.py must not call os.getenv directly"


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def test_build_fast_rank_prompt_serialises_context(sample_context):
    prompt = xai_client.build_fast_rank_prompt("IDYA", sample_context)
    assert "IDYA" in prompt
    # The context payload should be embedded as JSON, not stringified
    # Python dict.
    assert '"catalyst_type": "P3_readout"' in prompt
    assert '"nct_id": "NCT05123456"' in prompt
