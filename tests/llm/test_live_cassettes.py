"""Live cassette replay + schema-parity tests.

These tests are gated behind the ``RUN_LIVE_CASSETTES=1`` environment
variable. The default ``pytest`` run SKIPS them with the reason
``"live cassette test (set RUN_LIVE_CASSETTES=1)"`` so the suite stays
100% offline and hermetic.

When ``RUN_LIVE_CASSETTES=1`` is set, each test loads the
corresponding live cassette under
``tests/fixtures/cassettes/<provider>/live_*_2026-04-27.json`` and
asserts schema parity with the hand-crafted hermetic cassette
already committed alongside it. The live cassettes were recorded
once on the VPS via ``scripts/record_live_cassettes.py`` (see
feature ``f-misc-02-record-live-llm-cassettes``).

NOTE: these tests do NOT make new live network calls. They replay
the previously-recorded cassettes — which is what "RUN_LIVE_CASSETTES"
toggles: whether the live-recorded fixtures participate in the
suite. Live recording itself is a one-shot manual operation, not a
test.

Scrubbing contract (verified in ``test_live_cassettes_have_no_secrets``):
* No string matching ``xai-[A-Za-z0-9_-]{10,}``,
  ``sk-ant-[A-Za-z0-9_-]{10,}``, or ``AIza[A-Za-z0-9_-]{10,}``
  appears anywhere under ``tests/fixtures/cassettes/``.
* All Authorization headers in xai cassettes are literally
  ``"Bearer REDACTED"``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest


CASSETTE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "cassettes"

RUN_LIVE = os.environ.get("RUN_LIVE_CASSETTES") == "1"
SKIP_REASON = "live cassette test (set RUN_LIVE_CASSETTES=1)"

LIVE_DATE = "2026-04-27"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


_MARKDOWN_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*(.*?)\s*```\s*$",
    re.DOTALL | re.IGNORECASE,
)


def _strip_fences(text: str) -> str:
    """Strip a wrapping ```json ... ``` fence if present.

    Live recordings occasionally come back fence-wrapped despite the
    JSON-mode directive; the production parsers (``ClaudeClient``,
    ``GeminiClient``) strip these via the same regex, so the live
    cassettes are equally replayable through the real code paths
    once we apply this lightweight unwrap.
    """
    match = _MARKDOWN_FENCE_RE.match(text)
    if match:
        return match.group(1)
    return text


def _require_live_cassette(path: Path) -> None:
    """Skip cleanly when a live cassette is absent.

    A missing live cassette typically means the recording could not
    be made at recording time (e.g., depleted prepayment credits on
    one of the providers). The default test run is unaffected; under
    RUN_LIVE_CASSETTES=1 we surface the absence as a skip so the
    other providers' cassettes still validate.
    """
    if not path.is_file():
        pytest.skip(f"live cassette not present yet: {path.name}")


# ---------------------------------------------------------------------------
# Always-on safety check: scrubbing invariant.
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"xai-[A-Za-z0-9_-]{10,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"AIza[A-Za-z0-9_-]{10,}"),
]


def test_live_cassettes_have_no_secrets() -> None:
    """No secret-shaped string may exist under tests/fixtures/cassettes/.

    Runs unconditionally (not gated by RUN_LIVE_CASSETTES) so a
    rogue cassette can never sneak into the repo undetected.
    """
    offenders: list[tuple[Path, str]] = []
    for path in CASSETTE_DIR.rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        for pat in _SECRET_PATTERNS:
            m = pat.search(text)
            if m:
                offenders.append((path, m.group(0)[:8] + "***"))
                break
    assert not offenders, f"secret-shaped strings found in cassettes: {offenders!r}"


# ---------------------------------------------------------------------------
# xAI / Grok-4 — schema parity
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not RUN_LIVE, reason=SKIP_REASON)
def test_xai_live_cassette_schema_parity() -> None:
    live_path = CASSETTE_DIR / "xai" / f"live_score_ticker_{LIVE_DATE}.json"
    ref_path = CASSETTE_DIR / "xai" / "score_ticker_success.json"
    _require_live_cassette(live_path)
    live = _load(live_path)
    ref = _load(ref_path)

    # Outer shape
    assert "interactions" in live and "interactions" in ref
    assert isinstance(live["interactions"], list) and live["interactions"]

    live_interaction = live["interactions"][0]
    ref_interaction = ref["interactions"][0]

    # Authorization header must be redacted in the live cassette.
    auth = (
        live_interaction.get("request", {}).get("headers", {}).get("Authorization")
    )
    assert auth == "Bearer REDACTED", f"xai Authorization not redacted: {auth!r}"

    # Response shape parity.
    live_resp = live_interaction["response"]
    ref_resp = ref_interaction["response"]
    assert live_resp["status_code"] == 200 == ref_resp["status_code"]

    live_body = live_resp["json"]
    ref_body = ref_resp["json"]
    for key in ("id", "model", "choices", "usage"):
        assert key in live_body, f"xai live body missing {key!r}"
        assert key in ref_body, f"xai ref body missing {key!r}"

    assert isinstance(live_body["model"], str) and live_body["model"].startswith("grok-")

    # choices[0].message.content is a JSON-mode string with the
    # contract keys: probability/rationale/confidence.
    content = live_body["choices"][0]["message"]["content"]
    assert isinstance(content, str) and content.strip()
    parsed = json.loads(_strip_fences(content))
    assert isinstance(parsed, dict)
    for key in ("probability", "rationale", "confidence"):
        assert key in parsed, f"xai assistant JSON missing {key!r}"
    prob = float(parsed["probability"])
    assert 0.0 <= prob <= 1.0

    # Usage parity.
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        assert key in live_body["usage"], f"xai live usage missing {key!r}"


# ---------------------------------------------------------------------------
# Anthropic / Claude Opus — schema parity
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not RUN_LIVE, reason=SKIP_REASON)
def test_claude_live_cassette_schema_parity() -> None:
    live_path = CASSETTE_DIR / "claude" / f"live_deep_science_review_{LIVE_DATE}.json"
    ref_path = CASSETTE_DIR / "claude" / "deep_science_review_success.json"
    _require_live_cassette(live_path)
    live = _load(live_path)
    ref = _load(ref_path)

    assert "interactions" in live and "interactions" in ref
    live_result = live["interactions"][0]["result"]
    ref_result = ref["interactions"][0]["result"]

    for key in ("id", "model", "content_text", "usage"):
        assert key in live_result, f"claude live result missing {key!r}"
        assert key in ref_result, f"claude ref result missing {key!r}"

    assert isinstance(live_result["model"], str)
    assert live_result["model"].startswith("claude-")

    # Content text is a JSON-mode body with the deep-science contract.
    parsed = json.loads(_strip_fences(live_result["content_text"]))
    assert isinstance(parsed, dict)
    for key in (
        "science_profile",
        "letter_grade",
        "probability",
        "rationale",
        "citations",
    ):
        assert key in parsed, f"claude assistant JSON missing {key!r}"
    assert isinstance(parsed["science_profile"], dict)
    prob = float(parsed["probability"])
    assert 0.0 <= prob <= 1.0
    assert isinstance(parsed["citations"], list)

    # Usage parity uses Anthropic-style names.
    for key in ("input_tokens", "output_tokens"):
        assert key in live_result["usage"], f"claude usage missing {key!r}"


# ---------------------------------------------------------------------------
# Google Gemini 2.5 Pro — schema parity
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not RUN_LIVE, reason=SKIP_REASON)
def test_gemini_live_cassette_schema_parity() -> None:
    live_path = CASSETTE_DIR / "gemini" / f"live_deep_science_review_{LIVE_DATE}.json"
    ref_path = CASSETTE_DIR / "gemini" / "deep_science_review_success.json"
    _require_live_cassette(live_path)
    live = _load(live_path)
    ref = _load(ref_path)

    assert "interactions" in live and "interactions" in ref
    live_result = live["interactions"][0]["result"]
    ref_result = ref["interactions"][0]["result"]

    for key in ("id", "model", "content_text", "usage"):
        assert key in live_result, f"gemini live result missing {key!r}"
        assert key in ref_result, f"gemini ref result missing {key!r}"

    assert isinstance(live_result["model"], str)
    assert live_result["model"].startswith("gemini-")

    parsed = json.loads(_strip_fences(live_result["content_text"]))
    assert isinstance(parsed, dict)
    for key in (
        "science_profile",
        "letter_grade",
        "probability",
        "rationale",
        "citations",
    ):
        assert key in parsed, f"gemini assistant JSON missing {key!r}"
    prob = float(parsed["probability"])
    assert 0.0 <= prob <= 1.0
    assert isinstance(parsed["citations"], list)

    # Usage parity (recorder normalises Gemini's
    # ``prompt_token_count`` / ``candidates_token_count`` into the
    # provider-agnostic ``input_tokens`` / ``output_tokens`` shape so
    # the live cassette matches the hand-crafted reference exactly).
    for key in ("input_tokens", "output_tokens"):
        assert key in live_result["usage"], f"gemini usage missing {key!r}"


# ---------------------------------------------------------------------------
# Alpaca paper sandbox — schema parity for account / chain / order
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not RUN_LIVE, reason=SKIP_REASON)
def test_alpaca_live_cassette_schema_parity() -> None:
    live_path = (
        CASSETTE_DIR / "alpaca" / f"live_account_chain_order_{LIVE_DATE}.json"
    )
    _require_live_cassette(live_path)
    live = _load(live_path)

    interactions = live.get("interactions") or []
    types = [i.get("type") for i in interactions]
    for required in ("get_account", "get_options_chain", "submit_order", "cancel_order"):
        assert required in types, f"alpaca live cassette missing {required!r} step"

    # account
    account = next(i["result"] for i in interactions if i["type"] == "get_account")
    for key in (
        "status",
        "currency",
        "buying_power",
        "cash",
        "equity",
        "portfolio_value",
    ):
        assert key in account, f"alpaca account result missing {key!r}"
    # account id / account_number must be redacted (no real identifiers committed).
    assert account.get("id") in (None, "REDACTED")
    assert account.get("account_number") in (None, "REDACTED")

    # options chain — at least one row, with the VAL-M3-004 keys.
    chain_interaction = next(
        i for i in interactions if i["type"] == "get_options_chain"
    )
    chain = chain_interaction["result"]
    assert isinstance(chain, list) and chain, "alpaca options chain is empty"
    row = chain[0]
    for key in ("symbol", "strike", "expiry", "mid", "bid", "ask", "type"):
        assert key in row, f"alpaca chain row missing {key!r}"

    # submit_order
    order = next(i["result"] for i in interactions if i["type"] == "submit_order")
    for key in (
        "id",
        "symbol",
        "qty",
        "side",
        "status",
        "client_order_id",
        "order_type",
        "time_in_force",
    ):
        assert key in order, f"alpaca order missing {key!r}"
    assert order["symbol"] == "SPY"
    assert int(float(order["qty"])) == 1
    assert (order.get("side") or "").lower() == "buy"

    # cancel_order — recorded result is None (cancellation acknowledged).
    cancel = next(i for i in interactions if i["type"] == "cancel_order")
    assert "request" in cancel and "order_id" in cancel["request"]
    assert cancel["request"]["order_id"] == order["id"]
