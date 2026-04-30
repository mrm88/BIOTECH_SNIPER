"""Tests for :mod:`biotech_sniper.llm.perplexity_client` (Reading-B M3).

Hermetic tests: the live Perplexity API is never contacted. We replay
hand-crafted JSON cassettes from
``tests/fixtures/cassettes/perplexity/`` through a tiny fake
:class:`requests.Session` substitute. Each cassette has an ordered
list of ``interactions`` (the same shape vcrpy uses, minus the
request-matching machinery — we replay strictly in order).

Validation contract assertions covered by this module
-----------------------------------------------------
* VAL-M3-001 — module imports cleanly inside the venv
* VAL-M3-002 — default endpoint is ``https://api.perplexity.ai/chat/completions``
* VAL-M3-003 — model defaults to ``sonar`` (NOT ``sonar-pro``)
* VAL-M3-004 — ``Authorization: Bearer <key>`` header present
* VAL-M3-005 — ``response_format = json_schema`` with ``strict=true``
* VAL-M3-006 — schema requires probability/label/direction/rationale/citations
* VAL-M3-007 — ``web_search_options.search_context_size = "low"``
* VAL-M3-008 — HTTP timeout is 20 seconds
* VAL-M3-009 — 3× retry on 429 / 5xx / timeout with exponential backoff + jitter
* VAL-M3-010 — typed exception hierarchy
* VAL-M3-011 — schema-violating responses raise :class:`PerplexitySchemaError`
* VAL-M3-015 — cassettes redact API key sentinels; pytest performs zero live calls
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Mapping

import pytest
import requests

from biotech_sniper.llm import perplexity_client
from biotech_sniper.llm.perplexity_client import (
    BIOTECH_CATALYST_VERDICT_SCHEMA,
    DEFAULT_BACKOFF_BASE,
    DEFAULT_ENDPOINT_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    PerplexityAuthError,
    PerplexityBadRequestError,
    PerplexityClient,
    PerplexityClientError,
    PerplexityRateLimitError,
    PerplexitySchemaError,
    PerplexityTimeoutError,
    PerplexityTransportError,
    score_candidate,
)


# Sentinel API key — concatenated to dodge over-zealous secret scanners
# while keeping the literal recognisable. Tests never send a real key.
SENTINEL_API_KEY = "pplx-test" + "-sentinel-" + "0123456789abcdef"

CASSETTE_DIR = Path(__file__).parent / "fixtures" / "cassettes" / "perplexity"


# ---------------------------------------------------------------------------
# Fake session replay machinery
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal :class:`requests.Response` substitute for cassette replay."""

    def __init__(
        self,
        *,
        status_code: int,
        body_json: Any = None,
        text: str | None = None,
        headers: Mapping[str, str] | None = None,
    ):
        self.status_code = status_code
        self._body_json = body_json
        self.headers = dict(headers or {})
        if text is not None:
            self.text = text
        elif body_json is not None:
            self.text = json.dumps(body_json)
        else:
            self.text = ""

    def json(self) -> Any:
        if self._body_json is None:
            raise ValueError("No JSON body in cassette response")
        return self._body_json


class _FakeSession:
    """Replays cassette interactions in order through a single ``post``.

    Tracks call count + URLs + posted headers so tests can inspect
    request shape and retry behaviour.
    """

    def __init__(self, cassette: dict[str, Any]):
        self._interactions = list(cassette["interactions"])
        self._cursor = 0
        self.posted_urls: list[str] = []
        self.posted_payloads: list[dict[str, Any]] = []
        self.posted_headers: list[dict[str, str]] = []
        self.timeouts: list[Any] = []

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
        self.posted_headers.append(dict(kwargs.get("headers") or {}))
        self.timeouts.append(kwargs.get("timeout"))

        resp_spec = interaction["response"]
        if resp_spec.get("raise_timeout"):
            raise requests.Timeout("simulated upstream timeout")
        if resp_spec.get("raise_connection_error"):
            raise requests.ConnectionError("simulated dropped connection")
        return _FakeResponse(
            status_code=int(resp_spec["status_code"]),
            body_json=resp_spec.get("json"),
            text=resp_spec.get("text"),
            headers=resp_spec.get("headers"),
        )


def _load_cassette(name: str) -> dict[str, Any]:
    return json.loads((CASSETTE_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Common fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def make_client():
    """Factory returning ``(client, fake_session)`` from a cassette name."""

    def _factory(
        cassette_name: str,
        *,
        no_sleep: bool = True,
        max_retries: int = DEFAULT_MAX_RETRIES,
        **kwargs: Any,
    ):
        cassette = _load_cassette(cassette_name)
        fake = _FakeSession(cassette)
        client = PerplexityClient(
            api_key=SENTINEL_API_KEY,
            session=fake,
            backoff_base=0.0 if no_sleep else DEFAULT_BACKOFF_BASE,
            max_retries=max_retries,
            **kwargs,
        )
        return client, fake

    return _factory


@pytest.fixture
def sample_candidate() -> dict[str, Any]:
    return {
        "ticker": "IDYA",
        "headline": "Idaya reports positive Phase 3 readout for IDYA-3001",
        "matched_keywords": ["readout", "phase 3"],
        "calendar_match": "2026-05-20",
        "catalyst_type": "READOUT",
    }


# ---------------------------------------------------------------------------
# VAL-M3-001 — Module imports cleanly inside the venv
# ---------------------------------------------------------------------------


def test_module_exposes_perplexity_client_and_score_candidate():
    """Validation contract evidence: ``from ... import PerplexityClient,
    score_candidate`` exits 0."""
    assert callable(PerplexityClient)
    assert callable(score_candidate)


def test_module_has_no_module_level_network_call(monkeypatch, tmp_path):
    """Re-importing must not hit the network (PYTEST_DISABLE_NETWORK=1).

    The test imports ``perplexity_client`` in a fresh subinterpreter-like
    environment by spawning a subprocess. Using ``importlib.reload`` in
    the same process would cause the typed exception classes to be
    re-defined, breaking ``isinstance``/``pytest.raises`` semantics for
    every subsequent test in the session.
    """
    import subprocess
    import sys

    script = tmp_path / "smoke_import.py"
    script.write_text(
        "import requests\n"
        "def _refuse(*a, **kw):\n"
        "    raise AssertionError('module-level network call')\n"
        "for verb in ('get','post','head','put','delete','request'):\n"
        "    setattr(requests, verb, _refuse)\n"
        "import biotech_sniper.llm.perplexity_client as p\n"
        "assert hasattr(p, 'PerplexityClient')\n"
        "assert hasattr(p, 'score_candidate')\n"
        "print('ok')\n",
        encoding="utf-8",
    )
    import os as _os

    repo_root = Path(__file__).resolve().parent.parent
    env = dict(_os.environ)
    env["PYTHONPATH"] = str(repo_root) + _os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(repo_root),
        env=env,
    )
    assert out.returncode == 0, (out.stdout, out.stderr)
    assert out.stdout.strip().endswith("ok"), out.stdout


# ---------------------------------------------------------------------------
# VAL-M3-002 — Default endpoint
# ---------------------------------------------------------------------------


def test_default_endpoint_url_is_perplexity():
    """``PerplexityClient().endpoint_url`` equals the OpenAI-compat URL."""
    client = PerplexityClient(api_key=SENTINEL_API_KEY)
    assert client.endpoint_url == "https://api.perplexity.ai/chat/completions"
    assert DEFAULT_ENDPOINT_URL == "https://api.perplexity.ai/chat/completions"


def test_endpoint_url_does_not_contain_other_provider_hosts():
    client = PerplexityClient(api_key=SENTINEL_API_KEY)
    forbidden = (
        "api.openai.com",
        "api.anthropic.com",
        "api.x.ai",
        "generativelanguage.googleapis.com",
        "localhost",
        "127.0.0.1",
    )
    for host in forbidden:
        assert host not in client.endpoint_url, host


# ---------------------------------------------------------------------------
# VAL-M3-003 — Model defaults to "sonar"
# ---------------------------------------------------------------------------


def test_default_model_is_sonar(make_client, sample_candidate):
    """JSON body sent to Perplexity carries ``model='sonar'``."""
    client, fake = make_client("score_candidate_bullish.json")
    client.score_candidate(sample_candidate)
    payload = fake.posted_payloads[0]
    assert payload["model"] == "sonar"
    assert payload["model"] != "sonar-pro"
    assert payload["model"] != "sonar-deep-research"
    assert DEFAULT_MODEL == "sonar"


def test_module_source_does_not_reference_sonar_pro_outside_comments():
    """Static check: no live ``sonar-pro`` literal anywhere in the module
    source (allowed in docstrings/comments only — flagged here so reviewers
    can confirm the prohibition is intentional)."""
    src = Path(perplexity_client.__file__).read_text(encoding="utf-8")
    # Strip comments/docstrings before grepping.
    code_lines = []
    in_doc = False
    for line in src.splitlines():
        s = line.strip()
        if s.startswith('"""') or s.endswith('"""'):
            in_doc = not in_doc if s.count('"""') % 2 == 1 else in_doc
            continue
        if in_doc:
            continue
        if s.startswith("#"):
            continue
        code_lines.append(line)
    code = "\n".join(code_lines)
    # The string ``sonar-pro`` must not appear in active code.
    assert "sonar-pro" not in code
    assert "sonar-deep-research" not in code


# ---------------------------------------------------------------------------
# VAL-M3-004 — Authorization Bearer header
# ---------------------------------------------------------------------------


def test_auth_header_carries_bearer_key(make_client, sample_candidate):
    client, fake = make_client("score_candidate_bullish.json")
    client.score_candidate(sample_candidate)
    headers = fake.posted_headers[0]
    auth = headers.get("Authorization", "")
    assert auth.startswith("Bearer "), auth
    assert auth == f"Bearer {SENTINEL_API_KEY}"


def test_module_does_not_read_os_environ_for_secret():
    """Mission policy: secrets MUST come through config.py only."""
    src = Path(perplexity_client.__file__).read_text(encoding="utf-8")
    assert "os.environ" not in src, (
        "perplexity_client.py must not call os.environ directly; "
        "use biotech_sniper.config.get_perplexity_api_key()"
    )
    assert "os.getenv" not in src, (
        "perplexity_client.py must not call os.getenv directly"
    )
    # And the canonical config getter must be referenced.
    assert "get_perplexity_api_key" in src


def test_constructor_raises_when_api_key_missing(monkeypatch):
    monkeypatch.setattr(
        "biotech_sniper.llm.perplexity_client.config.get_perplexity_api_key",
        lambda: None,
    )
    with pytest.raises(PerplexityAuthError, match="PERPLEXITY_API_KEY"):
        PerplexityClient()


def test_constructor_uses_config_when_arg_omitted(monkeypatch):
    sentinel = "pplx-from" + "-config-helper"
    monkeypatch.setattr(
        "biotech_sniper.llm.perplexity_client.config.get_perplexity_api_key",
        lambda: sentinel,
    )
    client = PerplexityClient()
    # Authorization header constructed from the resolved key.
    assert client._api_key == sentinel  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# VAL-M3-005 — response_format = json_schema with strict=true
# ---------------------------------------------------------------------------


def test_request_uses_strict_json_schema(make_client, sample_candidate):
    client, fake = make_client("score_candidate_bullish.json")
    client.score_candidate(sample_candidate)
    body = fake.posted_payloads[0]
    rf = body["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"] == "biotech_catalyst_verdict"
    # The schema is the same canonical constant the module exports.
    assert rf["json_schema"]["schema"] == BIOTECH_CATALYST_VERDICT_SCHEMA


# ---------------------------------------------------------------------------
# VAL-M3-006 — Schema enforces required fields and value domains
# ---------------------------------------------------------------------------


def test_schema_required_fields_and_enums():
    schema = BIOTECH_CATALYST_VERDICT_SCHEMA
    assert set(schema["required"]) >= {
        "probability",
        "label",
        "direction",
        "rationale",
        "citations",
    }
    props = schema["properties"]
    # probability — number in [0, 1].
    assert props["probability"]["type"] == "number"
    assert props["probability"]["minimum"] == 0
    assert props["probability"]["maximum"] == 1
    # label enum.
    assert set(props["label"]["enum"]) == {"material", "immaterial", "ambiguous"}
    # direction enum.
    assert set(props["direction"]["enum"]) == {"bullish", "bearish"}
    # rationale is a string.
    assert props["rationale"]["type"] == "string"
    # citations: array of {url, title, snippet?} with required url+title.
    citations = props["citations"]
    assert citations["type"] == "array"
    cit_item = citations["items"]
    assert cit_item["type"] == "object"
    cit_props = cit_item["properties"]
    assert cit_props["url"]["type"] == "string"
    assert cit_props["url"]["format"] == "uri"
    assert cit_props["title"]["type"] == "string"
    assert "snippet" in cit_props
    assert set(cit_item["required"]) >= {"url", "title"}


# ---------------------------------------------------------------------------
# VAL-M3-007 — web_search_options.search_context_size = "low"
# ---------------------------------------------------------------------------


def test_search_context_size_low(make_client, sample_candidate):
    client, fake = make_client("score_candidate_bullish.json")
    client.score_candidate(sample_candidate)
    body = fake.posted_payloads[0]
    assert body["web_search_options"]["search_context_size"] == "low"


# ---------------------------------------------------------------------------
# VAL-M3-008 — HTTP timeout is 20 seconds
# ---------------------------------------------------------------------------


def test_default_timeout_is_20_seconds():
    client = PerplexityClient(api_key=SENTINEL_API_KEY)
    assert client._timeout == 20.0  # type: ignore[attr-defined]
    assert DEFAULT_TIMEOUT == 20.0


def test_request_passes_timeout_kwarg_to_session(make_client, sample_candidate):
    client, fake = make_client("score_candidate_bullish.json")
    client.score_candidate(sample_candidate)
    assert fake.timeouts == [20.0]


def test_request_timeout_20s_raises_perplexity_timeout_error():
    """A controllably-slow transport blocking past the 20s budget raises
    :class:`PerplexityTimeoutError`. The wall time is bounded because the
    fake post simulates a Timeout eagerly (no actual 25s sleep)."""

    class _SlowSession:
        def post(self, url: str, **kwargs: Any) -> _FakeResponse:
            # Real transport would block 25s and have requests raise
            # Timeout once the 20s budget elapsed; we simulate by
            # raising eagerly so test wall time stays bounded.
            raise requests.Timeout("simulated 25s upstream block")

    client = PerplexityClient(
        api_key=SENTINEL_API_KEY,
        session=_SlowSession(),
        max_retries=0,  # no retries — single attempt that times out
        backoff_base=0.0,
    )
    t0 = time.perf_counter()
    with pytest.raises(PerplexityTimeoutError):
        client.score_candidate({"ticker": "IDYA", "headline": "test"})
    wall = time.perf_counter() - t0
    assert wall < 22.0  # contract: < 22s


# ---------------------------------------------------------------------------
# VAL-M3-009 — Retries 3x on 429 / 5xx / timeout with backoff + jitter
# ---------------------------------------------------------------------------


def test_retries_on_429_5xx_timeout_only(make_client, sample_candidate):
    """Cassette [429 → 503 → timeout → 200] → success after retries.

    With max_retries=3, the client makes 4 attempts total: initial +
    3 retries. The cassette has exactly 4 interactions; the final 200
    delivers the bearish payload.
    """
    client, fake = make_client(
        "retry_429_503_timeout_then_200.json", max_retries=3
    )
    result = client.score_candidate(sample_candidate)
    assert fake.calls == 4
    assert result["label"] == "material"
    assert result["direction"] == "bearish"
    assert result["probability"] == pytest.approx(0.71)


def test_retries_exhausted_429_raises_rate_limit_error(make_client, sample_candidate):
    client, fake = make_client("retries_exhausted_429.json", max_retries=3)
    with pytest.raises(PerplexityRateLimitError):
        client.score_candidate(sample_candidate)
    assert fake.calls == 4  # initial + 3 retries


def test_retries_exhausted_5xx_raises_transport_error(make_client, sample_candidate):
    client, fake = make_client("retries_exhausted_503.json", max_retries=3)
    with pytest.raises(PerplexityTransportError):
        client.score_candidate(sample_candidate)
    assert fake.calls == 4


def test_no_retry_on_400_or_401(make_client, sample_candidate):
    """401 → :class:`PerplexityAuthError` after 1 request; 400 →
    :class:`PerplexityBadRequestError` after 1 request. No retries."""
    client_a, fake_a = make_client("auth_401.json")
    with pytest.raises(PerplexityAuthError):
        client_a.score_candidate(sample_candidate)
    assert fake_a.calls == 1

    client_b, fake_b = make_client("bad_request_400.json")
    with pytest.raises(PerplexityBadRequestError):
        client_b.score_candidate(sample_candidate)
    assert fake_b.calls == 1


def test_backoff_jitter_bounds(monkeypatch):
    """Each inter-attempt sleep falls within ±20% of the geometric
    backoff curve ``base * 2**attempt``.

    For base = 0.5 (DEFAULT_BACKOFF_BASE) the per-retry bounds are:

    * attempt-0 ∈ [0.4, 0.6]
    * attempt-1 ∈ [0.8, 1.2]
    * attempt-2 ∈ [1.6, 2.4]
    """
    base = DEFAULT_BACKOFF_BASE
    assert base == 0.5

    sleeps: list[float] = []
    monkeypatch.setattr(
        "biotech_sniper.llm.perplexity_client.time.sleep",
        lambda s: sleeps.append(s),
    )

    cassette = _load_cassette("retries_exhausted_429.json")
    fake = _FakeSession(cassette)
    client = PerplexityClient(
        api_key=SENTINEL_API_KEY,
        session=fake,
        max_retries=3,
        backoff_base=base,
    )
    with pytest.raises(PerplexityRateLimitError):
        client.score_candidate({"ticker": "IDYA", "headline": "x"})

    assert len(sleeps) == 3
    nominal = [base * (2 ** k) for k in range(3)]
    for sleep, nom in zip(sleeps, nominal):
        lo = 0.8 * nom
        hi = 1.2 * nom
        assert lo <= sleep <= hi, (sleep, lo, hi)


def test_backoff_jitter_varies_between_runs(monkeypatch):
    """Two independent runs should produce different jittered sleeps —
    deterministic equality across runs would mean the jitter helper
    isn't actually random."""
    base = 0.5
    runs: list[list[float]] = []
    for _ in range(2):
        sleeps: list[float] = []
        monkeypatch.setattr(
            "biotech_sniper.llm.perplexity_client.time.sleep",
            lambda s, _bucket=sleeps: _bucket.append(s),
        )
        fake = _FakeSession(_load_cassette("retries_exhausted_429.json"))
        client = PerplexityClient(
            api_key=SENTINEL_API_KEY,
            session=fake,
            max_retries=3,
            backoff_base=base,
        )
        with pytest.raises(PerplexityRateLimitError):
            client.score_candidate({"ticker": "X", "headline": "y"})
        runs.append(sleeps)
    assert runs[0] != runs[1]


# ---------------------------------------------------------------------------
# VAL-M3-010 — Typed exception hierarchy
# ---------------------------------------------------------------------------


def test_exception_hierarchy():
    for cls in (
        PerplexityAuthError,
        PerplexityBadRequestError,
        PerplexityRateLimitError,
        PerplexityTransportError,
        PerplexityTimeoutError,
        PerplexitySchemaError,
    ):
        assert issubclass(cls, PerplexityClientError), cls


# ---------------------------------------------------------------------------
# VAL-M3-011 — Schema validation rejects malformed responses
# ---------------------------------------------------------------------------


def _bullish_payload(**overrides: Any) -> str:
    base = {
        "probability": 0.8,
        "label": "material",
        "direction": "bullish",
        "rationale": "fine",
        "citations": [{"url": "https://example.com", "title": "x"}],
    }
    base.update(overrides)
    return json.dumps(base)


def _make_client_with_content(content: Any):
    """Build a client whose single replayed response is the given content."""

    body = {
        "id": "pplx-resp-test",
        "model": "sonar",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content if isinstance(content, str) else json.dumps(content),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    fake = _FakeSession(
        {
            "interactions": [
                {
                    "request": {
                        "method": "POST",
                        "url": "https://api.perplexity.ai/chat/completions",
                    },
                    "response": {"status_code": 200, "json": body},
                }
            ]
        }
    )
    client = PerplexityClient(
        api_key=SENTINEL_API_KEY,
        session=fake,
        max_retries=0,
        backoff_base=0.0,
    )
    return client


@pytest.mark.parametrize(
    "content,expected_field",
    [
        (_bullish_payload(probability=1.5), "probability"),
        (_bullish_payload(probability=-0.1), "probability"),
        (_bullish_payload(label="strong"), "label"),
        (_bullish_payload(direction="sideways"), "direction"),
    ],
)
def test_schema_validation_rejects_malformed_value_domains(content, expected_field):
    client = _make_client_with_content(content)
    with pytest.raises(PerplexitySchemaError) as exc:
        client.score_candidate({"ticker": "X", "headline": "y"})
    assert expected_field in str(exc.value)


def test_schema_validation_rejects_missing_citations():
    raw = json.dumps(
        {
            "probability": 0.8,
            "label": "material",
            "direction": "bullish",
            "rationale": "fine",
            # citations missing
        }
    )
    client = _make_client_with_content(raw)
    with pytest.raises(PerplexitySchemaError) as exc:
        client.score_candidate({"ticker": "X", "headline": "y"})
    assert "citations" in str(exc.value)


def test_schema_validation_rejects_null_rationale():
    raw = json.dumps(
        {
            "probability": 0.8,
            "label": "material",
            "direction": "bullish",
            "rationale": None,
            "citations": [{"url": "https://example.com", "title": "x"}],
        }
    )
    client = _make_client_with_content(raw)
    with pytest.raises(PerplexitySchemaError) as exc:
        client.score_candidate({"ticker": "X", "headline": "y"})
    assert "rationale" in str(exc.value)


def test_schema_validation_rejects_citation_without_url():
    raw = json.dumps(
        {
            "probability": 0.8,
            "label": "material",
            "direction": "bullish",
            "rationale": "fine",
            "citations": [{"title": "missing url"}],
        }
    )
    client = _make_client_with_content(raw)
    with pytest.raises(PerplexitySchemaError) as exc:
        client.score_candidate({"ticker": "X", "headline": "y"})
    assert "url" in str(exc.value).lower()


def test_non_json_body_raises_schema_error_not_json_decode_error():
    """Bare ``json.JSONDecodeError`` from a malformed assistant payload
    must be wrapped into :class:`PerplexitySchemaError`."""
    client, _ = (
        PerplexityClient(
            api_key=SENTINEL_API_KEY,
            session=_FakeSession(_load_cassette("non_json_body.json")),
            max_retries=0,
            backoff_base=0.0,
        ),
        None,
    )
    with pytest.raises(PerplexitySchemaError):
        client.score_candidate({"ticker": "X", "headline": "y"})


# ---------------------------------------------------------------------------
# VAL-M3-015 — VCR cassettes redact secrets; pytest does NOT call live
# Perplexity.
# ---------------------------------------------------------------------------


def test_cassettes_redact_perplexity_keys():
    """No cassette body may contain a real-looking Perplexity API key.

    The sentinel substring used by the fixture is concatenated at
    runtime, so it does not appear as a literal in any committed
    cassette — only the redacted ``***`` marker (or ``Bearer test-key``)
    is allowed in cassette files.
    """
    pat = re.compile(r"pplx-[A-Za-z0-9_-]{8,}")
    for path in CASSETTE_DIR.glob("*.json"):
        body = path.read_text(encoding="utf-8")
        # The ``pplx-resp-*`` and ``pplx-test-sentinel-*`` prefixes are
        # ID labels, not API keys. Allow them; reject anything else.
        for match in pat.finditer(body):
            value = match.group(0)
            assert (
                value.startswith("pplx-resp-")
                or value.startswith("pplx-test-sentinel-")
            ), (
                f"cassette {path.name} contains suspicious token "
                f"{value!r} that resembles a real Perplexity API key"
            )


def test_pytest_does_not_call_live_perplexity(monkeypatch, sample_candidate):
    """When the global ``requests.Session.post`` is monkeypatched to
    refuse, the client still works against the in-process fake session
    (i.e. tests do not depend on outbound egress)."""

    def _refuse(self, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError(f"live network egress to {url}")

    monkeypatch.setattr(requests.Session, "post", _refuse)
    monkeypatch.setattr(requests.Session, "request", _refuse)
    fake = _FakeSession(_load_cassette("score_candidate_bullish.json"))
    client = PerplexityClient(
        api_key=SENTINEL_API_KEY,
        session=fake,
        max_retries=0,
        backoff_base=0.0,
    )
    result = client.score_candidate(sample_candidate)
    assert result["label"] == "material"


# ---------------------------------------------------------------------------
# Additional happy-path / shape sanity
# ---------------------------------------------------------------------------


def test_score_candidate_returns_validated_dict(make_client, sample_candidate):
    client, _ = make_client("score_candidate_bullish.json")
    result = client.score_candidate(sample_candidate)
    for key in ("probability", "label", "direction", "rationale", "citations"):
        assert key in result
    assert result["label"] in {"material", "immaterial", "ambiguous"}
    assert result["direction"] in {"bullish", "bearish"}
    assert 0.0 <= float(result["probability"]) <= 1.0
    assert isinstance(result["citations"], list)


def test_module_level_score_candidate_uses_default_client(monkeypatch, sample_candidate):
    """The convenience function ``score_candidate(...)`` builds a client
    via DI knobs and forwards to :meth:`PerplexityClient.score_candidate`."""
    fake = _FakeSession(_load_cassette("score_candidate_bullish.json"))
    monkeypatch.setattr(
        "biotech_sniper.llm.perplexity_client.config.get_perplexity_api_key",
        lambda: SENTINEL_API_KEY,
    )
    out = score_candidate(sample_candidate, session=fake, backoff_base=0.0)
    assert out["label"] == "material"


def test_request_includes_messages_with_system_and_user_roles(
    make_client, sample_candidate
):
    client, fake = make_client("score_candidate_bullish.json")
    client.score_candidate(sample_candidate)
    payload = fake.posted_payloads[0]
    roles = [m["role"] for m in payload["messages"]]
    assert "system" in roles
    assert "user" in roles
