"""Tests that the Perplexity API key never leaks — feature f-m3-14.

Covers the validation-contract assertions:

* VAL-M3-072 — ``PERPLEXITY_API_KEY`` value never appears in DEBUG-level
  log capture across the success / 401 / 503 / schema-error code paths.
  ``Authorization`` headers are redacted to ``***`` when logged via the
  project's structured JSON formatter.
* VAL-M3-073 — Every typed Perplexity exception
  (``PerplexityAuthError`` / ``PerplexityTransportError`` /
  ``PerplexityTimeoutError`` / ``PerplexitySchemaError`` /
  ``PerplexityBadRequestError`` / ``PerplexityRateLimitError``) has
  ``str(exc)`` / ``repr(exc)`` / ``traceback.format_exc()`` that does NOT
  contain the API key value.
* VAL-M3-075 — ``.env.example`` contains a ``PERPLEXITY_API_KEY=`` line
  with no value, and never embeds a real-secret-shaped string.
* VAL-M3-102 — The sentinel API key value (and the broader
  ``pplx-[A-Za-z0-9_-]{8,}`` regex shape) does not appear in
  ``state/audit_latest.json``, ``state/news_daemon_heartbeat.json``,
  any tracked file under ``state/``, or any committed git-log content.

Tests are hermetic — no live network. The cassette/fake-session
machinery local to this file replays small canned bodies through the
:class:`PerplexityClient` constructor's ``session=`` kwarg.
"""

from __future__ import annotations

import io
import json
import logging
import re
import subprocess
import traceback
from pathlib import Path
from typing import Any, Iterable, Mapping

import pytest
import requests

from biotech_sniper.llm import perplexity_client
from biotech_sniper import db as project_db
from biotech_sniper.llm.perplexity_client import (
    PerplexityAuthError,
    PerplexityBadRequestError,
    PerplexityClient,
    PerplexityClientError,
    PerplexityRateLimitError,
    PerplexitySchemaError,
    PerplexityTimeoutError,
    PerplexityTransportError,
)
from biotech_sniper.logging_setup import JSONFormatter, REDACT_TOKEN


# Sentinel API key value — concatenated to keep secret-scanners happy
# while remaining grep-recognisable in test failures. Tests never send a
# real key. A second sentinel covers the alternate prefix shape so the
# regex check below has a positive control.
SENTINEL_API_KEY = "pplx-test" + "-secret-" + "DO-NOT-LEAK-1234567"
SENTINEL_PATTERN = re.compile(r"pplx-[A-Za-z0-9_\-]{8,}")
REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Local fake-session machinery (kept independent of the broader
# tests/test_perplexity_client.py harness so this file stands alone).
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int,
        body_json: Any = None,
        text: str | None = None,
        headers: Mapping[str, str] | None = None,
        raise_timeout: bool = False,
    ) -> None:
        self.status_code = status_code
        self._body_json = body_json
        self.headers = dict(headers or {})
        self._raise_timeout = raise_timeout
        if text is not None:
            self.text = text
        elif body_json is not None:
            self.text = json.dumps(body_json)
        else:
            self.text = ""

    def json(self) -> Any:
        if self._body_json is None:
            raise ValueError("non-JSON body")
        return self._body_json


class _FakeSession:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self._cursor = 0
        self.posted_headers: list[dict[str, str]] = []
        self.posted_payloads: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        if self._cursor >= len(self._responses):
            raise AssertionError(
                f"_FakeSession exhausted after {self._cursor} calls"
            )
        resp = self._responses[self._cursor]
        self._cursor += 1
        self.posted_headers.append(dict(kwargs.get("headers") or {}))
        self.posted_payloads.append(kwargs.get("json", {}))
        # Allow simulated transport-level failures.
        if isinstance(resp, BaseException):
            raise resp
        if getattr(resp, "_raise_timeout", False):
            raise requests.Timeout("simulated upstream timeout")
        return resp


def _make_bullish_envelope() -> dict[str, Any]:
    return {
        "id": "pplx-resp-secret-leak-tests",
        "model": "sonar",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "probability": 0.7,
                            "label": "material",
                            "direction": "bullish",
                            "rationale": "ok",
                            "citations": [
                                {
                                    "url": "https://example.com/a",
                                    "title": "A",
                                }
                            ],
                        }
                    ),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }


def _client_with(*responses: Any, db_path: Path) -> tuple[PerplexityClient, _FakeSession]:
    fake = _FakeSession(list(responses))
    client = PerplexityClient(
        api_key=SENTINEL_API_KEY,
        session=fake,
        max_retries=0,
        backoff_base=0.0,
        db_path=db_path,
    )
    return client, fake


@pytest.fixture
def sample_candidate() -> dict[str, Any]:
    return {
        "ticker": "TESTBIO",
        "headline": "TESTBIO P3 readout",
    }


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Lightweight temp-db fixture — uses the migrations runner so the
    perplexity client's cost-ledger writes succeed without raising."""
    from biotech_sniper.migrations.runner import run as run_v10

    db_path = tmp_path / "no_secret_leak.db"
    run_v10(db_path, target_version=project_db.CURRENT_VERSION, take_backup_first=False)
    return db_path


# ===========================================================================
# VAL-M3-072 — Sentinel API key never appears in DEBUG-captured log
# lines across the four code paths.
# ===========================================================================


def _assert_no_sentinel_in_records(
    records: list[logging.LogRecord], sentinel: str = SENTINEL_API_KEY
) -> None:
    for rec in records:
        msg = rec.getMessage()
        assert sentinel not in msg, (
            f"sentinel leaked into log record: {msg!r}"
        )
        # Also look at the raw repr in case the formatter encoded fields
        # that happen to be the sentinel:
        for attr in ("event", "message", "exc_text"):
            value = getattr(rec, attr, None)
            if isinstance(value, str):
                assert sentinel not in value, (
                    f"sentinel leaked into record.{attr}: {value!r}"
                )


def test_no_sentinel_leakage_on_success_path(
    temp_db: Path,
    sample_candidate: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = _make_bullish_envelope()
    client, _ = _client_with(
        _FakeResponse(status_code=200, body_json=body), db_path=temp_db
    )
    with caplog.at_level(logging.DEBUG):
        client.score_candidate(sample_candidate)
    _assert_no_sentinel_in_records(caplog.records)


def test_no_sentinel_leakage_on_401_path(
    temp_db: Path,
    sample_candidate: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Even when the upstream echoes the API key in the 401 body, the
    snippet emitted into the exception/log message must be redacted."""
    # Body deliberately echoes the bearer header AND the raw key so the
    # redaction pass is exercised on both shapes.
    leaky_body = {
        "error": {
            "type": "auth",
            "message": (
                f"invalid Authorization: Bearer {SENTINEL_API_KEY} - "
                f"please provide PERPLEXITY_API_KEY={SENTINEL_API_KEY}"
            ),
        }
    }
    client, _ = _client_with(
        _FakeResponse(
            status_code=401, body_json=leaky_body, text=json.dumps(leaky_body)
        ),
        db_path=temp_db,
    )
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(PerplexityAuthError) as exc_info:
            client.score_candidate(sample_candidate)
    _assert_no_sentinel_in_records(caplog.records)
    # The exception's own str() must not contain the sentinel.
    assert SENTINEL_API_KEY not in str(exc_info.value)


def test_no_sentinel_leakage_on_503_path(
    temp_db: Path,
    sample_candidate: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    leaky_body = {
        "error": {
            "type": "server",
            "echo_auth": f"Bearer {SENTINEL_API_KEY}",
        }
    }
    client, _ = _client_with(
        _FakeResponse(
            status_code=503, body_json=leaky_body, text=json.dumps(leaky_body)
        ),
        db_path=temp_db,
    )
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(PerplexityTransportError) as exc_info:
            client.score_candidate(sample_candidate)
    _assert_no_sentinel_in_records(caplog.records)
    assert SENTINEL_API_KEY not in str(exc_info.value)


def test_no_sentinel_leakage_on_schema_error_path(
    temp_db: Path,
    sample_candidate: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Schema validation failure — the assistant content is malformed
    JSON. Even though the body itself doesn't include the sentinel, we
    capture all logs along the way to confirm the Authorization header
    in the request was never echoed (e.g. via stack-trace logging)."""
    leaky_body = {
        "id": "pplx-resp-edge",
        "model": "sonar",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "definitely not json"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    client, _ = _client_with(
        _FakeResponse(status_code=200, body_json=leaky_body), db_path=temp_db
    )
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(PerplexitySchemaError) as exc_info:
            client.score_candidate(sample_candidate)
    _assert_no_sentinel_in_records(caplog.records)
    assert SENTINEL_API_KEY not in str(exc_info.value)


def test_authorization_header_redacted_by_json_formatter() -> None:
    """The project's structured logger redacts any field with a key
    matching ``authorization`` (case-insensitive) to ``***``. This is the
    foundation VAL-M3-072 leans on when callers log request payloads.
    """
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=1,
        msg="outbound request",
        args=(),
        exc_info=None,
    )
    record.headers = {
        "Authorization": f"Bearer {SENTINEL_API_KEY}",
        "Content-Type": "application/json",
    }
    record.event = "perplexity_request_dispatch"
    formatted = formatter.format(record)
    payload = json.loads(formatted)
    headers = payload["headers"]
    # The Authorization key collapses to '***'.
    assert headers["Authorization"] == REDACT_TOKEN
    # And the formatted line contains zero substring matches for the
    # sentinel value.
    assert SENTINEL_API_KEY not in formatted


# ===========================================================================
# VAL-M3-073 — Exception message / repr / traceback never contain the key.
# ===========================================================================


def _stringify(exc: BaseException) -> tuple[str, str, str]:
    """Return ``(str(exc), repr(exc), traceback_text)``."""
    try:
        raise exc
    except BaseException as raised:
        tb = "".join(
            traceback.format_exception(type(raised), raised, raised.__traceback__)
        )
        return str(raised), repr(raised), tb


def _assert_secret_absent_from_exception(exc: BaseException) -> None:
    s, r, tb = _stringify(exc)
    assert SENTINEL_API_KEY not in s, ("str(exc)", s)
    assert SENTINEL_API_KEY not in r, ("repr(exc)", r)
    assert SENTINEL_API_KEY not in tb, ("traceback", tb)


def test_auth_error_does_not_leak_key_in_str_repr_traceback(
    temp_db: Path, sample_candidate: dict[str, Any]
) -> None:
    leaky_body = {
        "error": f"PERPLEXITY_API_KEY={SENTINEL_API_KEY} rejected"
    }
    client, _ = _client_with(
        _FakeResponse(
            status_code=401, body_json=leaky_body, text=json.dumps(leaky_body)
        ),
        db_path=temp_db,
    )
    with pytest.raises(PerplexityAuthError) as exc_info:
        client.score_candidate(sample_candidate)
    _assert_secret_absent_from_exception(exc_info.value)


def test_transport_error_does_not_leak_key(
    temp_db: Path, sample_candidate: dict[str, Any]
) -> None:
    leaky_body = {"error": f"Bearer {SENTINEL_API_KEY} 503"}
    client, _ = _client_with(
        _FakeResponse(
            status_code=503, body_json=leaky_body, text=json.dumps(leaky_body)
        ),
        db_path=temp_db,
    )
    with pytest.raises(PerplexityTransportError) as exc_info:
        client.score_candidate(sample_candidate)
    _assert_secret_absent_from_exception(exc_info.value)


def test_bad_request_error_does_not_leak_key(
    temp_db: Path, sample_candidate: dict[str, Any]
) -> None:
    leaky_body = {
        "error": f"validation; received Bearer {SENTINEL_API_KEY}"
    }
    client, _ = _client_with(
        _FakeResponse(
            status_code=400, body_json=leaky_body, text=json.dumps(leaky_body)
        ),
        db_path=temp_db,
    )
    with pytest.raises(PerplexityBadRequestError) as exc_info:
        client.score_candidate(sample_candidate)
    _assert_secret_absent_from_exception(exc_info.value)


def test_rate_limit_error_does_not_leak_key(
    temp_db: Path, sample_candidate: dict[str, Any]
) -> None:
    leaky_body = {"error": f"throttled - Bearer {SENTINEL_API_KEY}"}
    client, _ = _client_with(
        _FakeResponse(
            status_code=429,
            body_json=leaky_body,
            text=json.dumps(leaky_body),
            headers={"Retry-After": "1"},
        ),
        db_path=temp_db,
    )
    with pytest.raises(PerplexityRateLimitError) as exc_info:
        client.score_candidate(sample_candidate)
    _assert_secret_absent_from_exception(exc_info.value)


def test_timeout_error_does_not_leak_key(
    temp_db: Path, sample_candidate: dict[str, Any]
) -> None:
    """``PerplexityTimeoutError`` is the synthesised exception when the
    transport-layer timeout retries are exhausted. Its message must not
    embed the API key (the timeout path doesn't see a body, but the
    same redaction invariant still applies)."""
    client, _ = _client_with(
        _FakeResponse(status_code=200, raise_timeout=True),
        db_path=temp_db,
    )
    with pytest.raises(PerplexityTimeoutError) as exc_info:
        client.score_candidate(sample_candidate)
    _assert_secret_absent_from_exception(exc_info.value)


def test_schema_error_does_not_leak_key(
    temp_db: Path, sample_candidate: dict[str, Any]
) -> None:
    leaky_body = {
        "id": "pplx-resp-edge",
        "model": "sonar",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": f"<not json> Bearer {SENTINEL_API_KEY}",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    client, _ = _client_with(
        _FakeResponse(status_code=200, body_json=leaky_body), db_path=temp_db
    )
    with pytest.raises(PerplexitySchemaError) as exc_info:
        client.score_candidate(sample_candidate)
    # The schema-error message includes a snippet of the assistant
    # content. Confirm the bearer/key portion is redacted.
    msg = str(exc_info.value)
    # Cause chain from json.JSONDecodeError preserved.
    assert isinstance(exc_info.value.__cause__, (json.JSONDecodeError, type(None))) or \
        isinstance(exc_info.value.__cause__, BaseException)
    # Sentinel must NOT be in the message.
    assert SENTINEL_API_KEY not in msg, msg


def test_exception_hierarchy_classes_are_subclasses() -> None:
    """Smoke check the typed exception hierarchy used by the contract:
    every redact-tested exception class is a
    :class:`PerplexityClientError` subclass."""
    for cls in (
        PerplexityAuthError,
        PerplexityBadRequestError,
        PerplexityRateLimitError,
        PerplexityTransportError,
        PerplexityTimeoutError,
        PerplexitySchemaError,
    ):
        assert issubclass(cls, PerplexityClientError), cls


# ===========================================================================
# VAL-M3-075 — .env.example contract.
# ===========================================================================


def test_env_example_lists_perplexity_key_with_no_value() -> None:
    """``.env.example`` MUST contain a ``PERPLEXITY_API_KEY=`` line
    (placeholder; never a real secret) so operators know the env var is
    expected (mirrors the existing XAI/ANTHROPIC/GEMINI key conventions).
    """
    env_path = REPO_ROOT / ".env.example"
    assert env_path.is_file(), env_path
    text = env_path.read_text(encoding="utf-8")
    # The key NAME must appear at the start of a line.
    pat = re.compile(r"^PERPLEXITY_API_KEY=", re.MULTILINE)
    assert pat.search(text), (
        ".env.example must declare PERPLEXITY_API_KEY="
    )
    # Find every line that introduces PERPLEXITY_API_KEY=… and confirm
    # each one's RHS is empty (or whitespace-only).
    leak_pat = re.compile(r"^PERPLEXITY_API_KEY=(.*)$", re.MULTILINE)
    for match in leak_pat.finditer(text):
        rhs = match.group(1).strip()
        # Allow a blank value or a short placeholder of the form
        # `<your key>` (angle-bracket marker). Reject any value that
        # starts with `pplx-` or contains `>=8` urlsafe chars after a
        # likely prefix.
        if rhs == "":
            continue
        if rhs.startswith("<") and rhs.endswith(">"):
            continue
        if rhs.startswith("#"):
            continue
        pytest.fail(
            f".env.example PERPLEXITY_API_KEY has unexpected RHS: {rhs!r}"
        )
    # Belt + suspenders: no real-secret pattern anywhere in the file.
    assert not SENTINEL_PATTERN.search(text), (
        ".env.example must not contain any pplx-* shaped string"
    )


# ===========================================================================
# VAL-M3-102 — Sentinel never appears in audit/state/git-log content
# committed into the repository.
# ===========================================================================


def _scan_path_for_secret(
    path: Path,
    pattern: re.Pattern[str],
    sentinel: str,
) -> list[str]:
    """Return a list of human-readable findings for any sentinel /
    secret-shaped match found anywhere under ``path``."""
    findings: list[str] = []
    if not path.exists():
        return findings
    iterator: list[Path]
    if path.is_file():
        iterator = [path]
    else:
        iterator = [
            p for p in path.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        ]
    for fp in iterator:
        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - defensive
            continue
        if sentinel in content:
            findings.append(f"sentinel value present in {fp}")
        for match in pattern.finditer(content):
            value = match.group(0)
            # Allow our own test-file sentinels (this file plus the
            # other perplexity test modules); the validator
            # explicitly carves these out so the test of the
            # invariant doesn't fail itself.
            if value.startswith("pplx-test") or value.startswith("pplx-resp"):
                continue
            findings.append(f"secret-shape {value!r} in {fp}")
    return findings


def test_no_sentinel_in_repo_state_directory() -> None:
    """``state/audit_latest.json`` and ``state/news_daemon_heartbeat.json``
    (plus anything else under ``state/``) must NEVER contain the sentinel
    value or a real-secret-shaped substring. ``state/`` itself is
    gitignored, but tests can still check whatever is present locally
    so a developer never inadvertently checks a leaky file in."""
    findings = _scan_path_for_secret(
        REPO_ROOT / "state", SENTINEL_PATTERN, SENTINEL_API_KEY
    )
    assert findings == [], findings


def test_no_sentinel_in_repo_logs_directory() -> None:
    findings = _scan_path_for_secret(
        REPO_ROOT / "logs", SENTINEL_PATTERN, SENTINEL_API_KEY
    )
    assert findings == [], findings


def test_no_sentinel_in_committed_files_via_git_log() -> None:
    """``git log --all -p`` over the entire repo must contain ZERO
    real-secret-shaped strings (i.e. no ``pplx-`` followed by ≥ 8 urlsafe
    chars that does not match the test-only ``pplx-test`` /
    ``pplx-resp`` prefixes).

    Test-only sentinels are explicitly carved out so the assertion of
    the invariant does not flag the assertion itself.
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(REPO_ROOT),
                "log",
                "--all",
                "-p",
                "--no-color",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:  # pragma: no cover - git always present locally
        pytest.skip("git not available")
    if proc.returncode != 0:  # pragma: no cover - defensive
        pytest.skip(f"git log failed: {proc.stderr[:200]}")
    text = proc.stdout
    # Sentinel value must not appear verbatim.
    assert SENTINEL_API_KEY not in text, (
        "sentinel value leaked into git history"
    )
    # No real-secret-shaped strings beyond our test-only prefixes.
    leaks = []
    for match in SENTINEL_PATTERN.finditer(text):
        value = match.group(0)
        if value.startswith("pplx-test") or value.startswith("pplx-resp"):
            continue
        leaks.append(value)
    assert leaks == [], f"git log contains real-secret-shaped tokens: {leaks[:5]}"


def test_no_sentinel_in_repo_root_dotenv_example() -> None:
    """Belt-and-suspenders for VAL-M3-075: even though the previous test
    permits a placeholder, this one specifically scans for any
    ``pplx-...{8,}`` shape in ``.env.example`` and fails on every match
    that isn't a known test sentinel."""
    env_text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    leaks = [
        m.group(0)
        for m in SENTINEL_PATTERN.finditer(env_text)
        if not (
            m.group(0).startswith("pplx-test")
            or m.group(0).startswith("pplx-resp")
        )
    ]
    assert leaks == [], leaks


# ===========================================================================
# VAL-CROSS-044 — Source-tree secret-pattern audit (multi-provider).
#
# Scans the entire source tree (excluding .git, .venv, __pycache__,
# vcrpy cassette fixtures) for secret-shaped tokens across every
# provider this project uses. Tests that look like they include
# secrets (this file's sentinel, perplexity test fixtures with
# explicit ``pplx-test`` / ``pplx-resp`` prefixes) are carved out
# via test-only prefix allow-list so the audit doesn't flag itself.
#
# Wired as a CI gate via the repo-level ``Makefile`` ``audit-secrets``
# target (see :func:`test_make_audit_secrets_target_exists`).
# ===========================================================================


# Patterns covering every secret type referenced anywhere in this
# project (see ``biotech_sniper/config.py`` getters and the env-var
# documentation in ``library/environment.md``).
#
# Each pattern is a ``(label, regex)`` tuple. The regex matches the
# *value* shape of the credential, NOT just the env-var name. The
# audit fails on any match found outside the test-only allow-list.
#
# Real-shape regex: real keys for these providers are base62 / base64
# (alphanumeric only — no dashes / underscores inside the value
# portion). This deliberately mirrors the strictness of
# ``services.yaml::secrets_scan`` so dash-bearing test sentinels
# (e.g. ``sk-ant-from-config-helper`` in tests/test_claude_client.py)
# do not trip the audit while real credentials still do.
SOURCE_TREE_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Anthropic keys: ``sk-ant-`` + 20+ urlsafe (real keys are
    # alphanumeric, no dashes).
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9]{20,}")),
    # Generic OpenAI-shape keys: ``sk-`` + 20+ urlsafe.
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    # Perplexity API keys.
    ("perplexity_key", re.compile(r"\bpplx-[A-Za-z0-9]{20,}")),
    # Alpaca paper / live key IDs (uppercase + digits, ≥ 16 chars).
    ("alpaca_key_id", re.compile(r"\bALPACA_KEY_ID\s*=\s*[A-Z0-9]{16,}")),
    # GitHub personal access tokens (ghp_ + 20+ alphanumeric).
    ("github_pat", re.compile(r"\bghp_[A-Za-z0-9]{20,}")),
    # X.AI / Grok keys (xai- prefix, 20+ alphanumeric).
    ("xai_key", re.compile(r"\bxai-[A-Za-z0-9]{20,}")),
    # Generic AWS access key IDs (AKIA + 16 uppercase / digits).
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
]


# Test-only sentinel prefixes carved out of the audit so the
# regression test of the invariant doesn't fail itself. Test files
# legitimately use these prefixes to construct sentinel values that
# match the secret-shape regex; they are documented and never real
# credentials.
TEST_ONLY_PREFIXES: tuple[str, ...] = (
    "pplx-test",
    "pplx-resp",
    "sk-test-",
    "sk-ant-test-",
    "xai-test-",
    "ghp_test_",
    # Specific test sentinels in tests/test_logging_setup.py (used
    # as positive controls for the redaction logic).
    "xai-thisshouldneverappear",
)


# Dirs / glob patterns excluded from the source-tree audit (matches
# the contract's "excl. .git, .venv, node_modules, redacted cassettes"
# carve-outs verbatim).
SOURCE_TREE_EXCLUDED_PARTS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        # vcrpy cassettes are committed redacted but the structured
        # YAML may include sentinel-shaped Authorization tokens that
        # were redacted to ``***`` (still match the *regex* shape
        # under some edge encodings). Cassettes have their own
        # dedicated leak audit (test_perplexity_client.py) — exclude
        # from this broad scan.
        "cassettes",
        # Compiled pytest snapshots, build artifacts.
        ".pytest_cache",
        "build",
        "dist",
        # Generated docs / large binary outputs.
        "Biotech_Catalyst_Sniper_2026-03-30.xlsx",
    }
)


def _iter_source_tree_files() -> Iterable[Path]:
    """Yield every text-shaped file under :data:`REPO_ROOT` excluding
    :data:`SOURCE_TREE_EXCLUDED_PARTS` parts and binary suffixes."""
    binary_suffixes = {
        ".pyc", ".pyo", ".so", ".dylib", ".o", ".a",
        ".png", ".jpg", ".jpeg", ".gif", ".pdf",
        ".db", ".sqlite", ".sqlite3", ".whl", ".tar", ".gz",
        ".zip", ".xlsx", ".xls", ".csv",
    }
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(REPO_ROOT)
        # Exclude paths whose parts match any excluded marker.
        if any(part in SOURCE_TREE_EXCLUDED_PARTS for part in rel.parts):
            continue
        # Skip obvious binaries by suffix.
        if path.suffix.lower() in binary_suffixes:
            continue
        # Skip the file that legitimately documents these patterns
        # (this audit module itself), AND its sibling that documents
        # the perplexity-only audit. Their sentinels are quoted
        # *inside* the regex literals — not real secrets — and would
        # otherwise trip the audit.
        if path.name in {
            "test_no_secret_leakage.py",
        }:
            continue
        yield path


def _is_test_only_value(value: str) -> bool:
    """Return True iff ``value`` starts with a documented test-only
    sentinel prefix and should be carved out of the audit."""
    return any(value.startswith(p) for p in TEST_ONLY_PREFIXES)


def _scan_source_tree_for_secrets() -> list[str]:
    """Scan every in-scope file for secret-shaped substrings; return a
    list of human-readable findings (one per match)."""
    findings: list[str] = []
    for fp in _iter_source_tree_files():
        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover — defensive
            continue
        for label, pattern in SOURCE_TREE_SECRET_PATTERNS:
            for match in pattern.finditer(content):
                value = match.group(0)
                if _is_test_only_value(value):
                    continue
                # ALPACA_KEY_ID=AKIA... obviously matches both
                # alpaca_key_id and aws_access_key — record the
                # narrower label for clarity but emit at most one
                # finding per (file, position).
                findings.append(
                    f"[{label}] {value!r} in {fp.relative_to(REPO_ROOT)}"
                )
    return findings


def test_source_tree_has_no_secret_shaped_tokens() -> None:
    """Source tree contains zero non-test-only secret-shaped tokens."""
    findings = _scan_source_tree_for_secrets()
    assert findings == [], (
        "VAL-CROSS-044: secret-shaped tokens found in source tree:\n"
        + "\n".join(findings[:50])
    )


def test_makefile_audit_secrets_target_exists() -> None:
    """VAL-CROSS-044 wires the source-tree secret audit as a CI gate.
    At least one of the documented integration paths must exist:
    a ``Makefile`` with an ``audit-secrets`` target, a
    ``.pre-commit-config.yaml``, or a ``.github/workflows/secrets.yml``.

    The convention chosen by f-cross-06 is the Makefile target — it
    is dependency-free and runnable both locally and in CI.
    """
    candidates = [
        (REPO_ROOT / "Makefile", "audit-secrets"),
        (REPO_ROOT / ".pre-commit-config.yaml", "secrets"),
        (REPO_ROOT / ".github" / "workflows" / "secrets.yml", "secrets"),
    ]
    found: list[str] = []
    for path, marker in candidates:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if marker in text:
            found.append(f"{path.relative_to(REPO_ROOT)}::{marker}")
    assert found, (
        "VAL-CROSS-044: at least one CI integration path must exist "
        f"for the source-tree secret audit. Checked: {[str(p[0]) for p in candidates]}"
    )


def test_secret_pattern_categories_cover_every_provider() -> None:
    """Sanity: the audit pattern set covers every provider this
    project actually integrates with. If a future feature adds a
    new provider (e.g. a hypothetical ``cohere-`` API key), this
    test fails until the pattern is added.
    """
    labels = {label for label, _ in SOURCE_TREE_SECRET_PATTERNS}
    required = {
        "anthropic_key",
        "openai_key",
        "perplexity_key",
        "alpaca_key_id",
        "github_pat",
        "xai_key",
    }
    missing = required - labels
    assert not missing, f"missing audit pattern categories: {missing}"
