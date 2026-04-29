"""Record live cassettes for xai, claude, gemini, and Alpaca paper sandbox.

This is a one-shot recording script run on the VPS (where the real
API keys live in ``/root/alpha_sniper/.env``). It makes ONE call per
provider and writes a scrubbed cassette under
``tests/fixtures/cassettes/<provider>/live_<purpose>_<DATE>.json``.

Usage::

    cd /root/alpha_sniper/repo
    .venv/bin/python -m scripts.record_live_cassettes               # all
    .venv/bin/python -m scripts.record_live_cassettes xai claude    # subset

Scrubbing contract (per feature spec):
* Authorization headers replaced with ``Bearer REDACTED``.
* No raw API keys (xai-*, sk-ant-*, AIza*) appear anywhere in any
  cassette field.
* Alpaca account id / account_number replaced with ``REDACTED`` so
  paper-account identifiers are not committed.

The recorded cassettes are CONTENT-only; the live request bodies and
response bodies are committed for replay/schema-parity tests gated
behind ``RUN_LIVE_CASSETTES=1`` (see
``tests/llm/test_live_cassettes.py``). The default test run does NOT
exercise these cassettes.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Path bootstrap so this script is runnable as a module or as a file.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from biotech_sniper import config  # noqa: E402  (after sys.path bootstrap)
from biotech_sniper.paths import BASE_DIR  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CASSETTE_DATE = "2026-04-27"
CASSETTE_DIR = BASE_DIR / "tests" / "fixtures" / "cassettes"

# Patterns that must NEVER appear in any cassette (mission policy).
SECRET_PATTERNS = [
    re.compile(r"xai-[A-Za-z0-9_-]{10,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"AIza[A-Za-z0-9_-]{10,}"),
]


def _assert_scrubbed(blob: str, label: str) -> None:
    """Defensive grep — refuse to write a cassette that still has secrets."""
    for pat in SECRET_PATTERNS:
        m = pat.search(blob)
        if m:
            raise SystemExit(
                f"REFUSING to write {label}: secret-shaped string detected: "
                f"{m.group(0)[:8]}***"
            )


def _write_cassette(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True, default=str)
    _assert_scrubbed(text, str(path))
    path.write_text(text + "\n", encoding="utf-8")
    print(f"wrote {path}")


# ---------------------------------------------------------------------------
# xAI / Grok-4
# ---------------------------------------------------------------------------


def record_xai() -> None:
    import requests

    api_key = config.get_xai_api_key()
    if not api_key:
        raise SystemExit("XAI_API_KEY is not configured")

    url = "https://api.x.ai/v1/chat/completions"
    request_payload = {
        "model": "grok-4",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a quantitative biotech catalyst analyst. "
                    "Return STRICT JSON only with these keys: probability "
                    "(float in [0,1]), rationale (1-3 sentence string), "
                    "confidence (float in [0,1]). No markdown, no prose."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Score the catalyst probability for ticker IDYA. "
                    "Phase 3 readout in ~21 days for IDYA-3001 in "
                    "small-cell lung cancer. Sponsor recently filed an "
                    "8-K disclosing a routine DSMB review. Return JSON "
                    "only with keys probability, rationale, confidence."
                ),
            },
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    resp = requests.post(url, headers=headers, json=request_payload, timeout=120)
    resp.raise_for_status()
    body = resp.json()

    cassette = {
        "interactions": [
            {
                "request": {
                    "method": "POST",
                    "url": url,
                    "headers": {
                        "Authorization": "Bearer REDACTED",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    "json": request_payload,
                },
                "response": {
                    "status_code": resp.status_code,
                    "json": body,
                },
            }
        ]
    }
    out = CASSETTE_DIR / "xai" / f"live_score_ticker_{CASSETTE_DATE}.json"
    _write_cassette(out, cassette)


# ---------------------------------------------------------------------------
# Anthropic / Claude Opus
# ---------------------------------------------------------------------------


def record_claude() -> None:
    import anthropic

    api_key = config.get_anthropic_api_key()
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY is not configured")

    client = anthropic.Anthropic(api_key=api_key, timeout=180.0, max_retries=0)

    system_prompt = (
        "You are a senior biotech clinical-development analyst evaluating a "
        "single catalyst-driven trade thesis. Return STRICT JSON only — no "
        "markdown fences, no commentary outside the object. JSON keys: "
        "science_profile (object with moa, comparator, prior_phase_data, "
        "base_rate (float in [0,1]), indication, design_risks), "
        "letter_grade (one of A+, A, A-, B+, B, B-, C+, C, C-, D, F), "
        "probability (float in [0,1]), rationale (3-6 sentence string), "
        "citations (array of {source: str, quote_or_url: str})."
    )
    user_prompt = (
        "Evaluate the following biotech catalyst thesis and return JSON only:\n\n"
        + json.dumps(
            {
                "preliminary_science_profile": {
                    "moa": "Selective MAT2A inhibition exploits MTAP-deletion synthetic lethality",
                    "comparator": "Investigator's choice chemo (docetaxel)",
                    "prior_phase_data": "P2 ORR 32% (n=58), median DoR 7.2mo; PD biomarker confirmed in 84%",
                    "base_rate": 0.45,
                    "indication": "MTAP-deleted NSCLC, 2L+",
                    "design_risks": "Open-label P3 vs SoC; PFS primary; HR 0.65 powering",
                },
                "full_context": {
                    "ticker": "IDYA",
                    "nct_id": "NCT05123456",
                    "catalyst_date": "2026-05-20",
                    "phase": 3,
                },
            },
            sort_keys=True,
            indent=2,
        )
    )

    msg = client.messages.create(
        model="claude-opus-4-1-20250805",
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    content_text = "".join(
        getattr(b, "text", "")
        for b in (msg.content or [])
        if getattr(b, "type", "") == "text"
    )

    cassette = {
        "interactions": [
            {
                "type": "create",
                "request": {
                    "model": "claude-opus-4-1-20250805",
                    "max_tokens": 4096,
                    "system_preview": system_prompt[:200],
                    "user_preview": user_prompt[:200],
                },
                "result": {
                    "id": msg.id,
                    "model": msg.model,
                    "role": getattr(msg, "role", "assistant"),
                    "stop_reason": getattr(msg, "stop_reason", None),
                    "content_text": content_text,
                    "usage": {
                        "input_tokens": int(msg.usage.input_tokens),
                        "output_tokens": int(msg.usage.output_tokens),
                    },
                },
            }
        ]
    }
    out = CASSETTE_DIR / "claude" / f"live_deep_science_review_{CASSETTE_DATE}.json"
    _write_cassette(out, cassette)


# ---------------------------------------------------------------------------
# Google Gemini 2.5 Pro
# ---------------------------------------------------------------------------


def record_gemini() -> None:
    from google import genai
    from google.genai import types as genai_types

    api_key = config.get_gemini_api_key()
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is not configured")

    client = genai.Client(
        api_key=api_key,
        http_options={"timeout": 180000},
    )

    system_prompt = (
        "You are a senior biotech clinical-development analyst. Return "
        "STRICT JSON only. JSON keys: science_profile (object with moa, "
        "comparator, prior_phase_data, base_rate (float in [0,1]), "
        "indication, design_risks), letter_grade (one of A+, A, A-, B+, "
        "B, B-, C+, C, C-, D, F), probability (float in [0,1]), "
        "rationale (3-6 sentence string), citations (array of "
        "{source: str, quote_or_url: str})."
    )
    user_prompt = (
        "Evaluate the following biotech catalyst thesis and return JSON only:\n\n"
        + json.dumps(
            {
                "preliminary_science_profile": {
                    "moa": "Selective MAT2A inhibition exploits MTAP-deletion synthetic lethality",
                    "comparator": "Investigator's choice chemo (docetaxel)",
                    "prior_phase_data": "P2 ORR 32% (n=58), median DoR 7.2mo",
                    "base_rate": 0.45,
                    "indication": "MTAP-deleted NSCLC, 2L+",
                    "design_risks": "Open-label P3 vs SoC; PFS primary",
                },
                "full_context": {
                    "ticker": "IDYA",
                    "nct_id": "NCT05123456",
                    "catalyst_date": "2026-05-20",
                    "phase": 3,
                },
            },
            sort_keys=True,
            indent=2,
        )
    )

    request_config = genai_types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=0.2,
        response_mime_type="application/json",
        max_output_tokens=4096,
    )
    resp = client.models.generate_content(
        model="gemini-2.5-pro",
        contents=user_prompt,
        config=request_config,
    )

    content_text = resp.text or ""
    usage = resp.usage_metadata
    model_id = getattr(resp, "model_version", None) or "gemini-2.5-pro"
    response_id = getattr(resp, "response_id", None) or f"live-gemini-{uuid.uuid4().hex[:12]}"

    cassette = {
        "interactions": [
            {
                "type": "create",
                "request": {
                    "model": "gemini-2.5-pro",
                    "system_preview": system_prompt[:200],
                    "user_preview": user_prompt[:200],
                },
                "result": {
                    "id": response_id,
                    "model": model_id,
                    "content_text": content_text,
                    "usage": {
                        "input_tokens": int(getattr(usage, "prompt_token_count", 0) or 0),
                        "output_tokens": int(getattr(usage, "candidates_token_count", 0) or 0),
                    },
                },
            }
        ]
    }
    out = CASSETTE_DIR / "gemini" / f"live_deep_science_review_{CASSETTE_DATE}.json"
    _write_cassette(out, cassette)


# ---------------------------------------------------------------------------
# Alpaca paper sandbox
# ---------------------------------------------------------------------------


def record_alpaca() -> None:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest

    from biotech_sniper.alpaca_client import AlpacaClient

    client = AlpacaClient()  # paper-only by default

    interactions: list[dict[str, Any]] = []

    # 1) get_account
    account = client.get_account()
    # Scrub paper-account identifiers — keep numeric / status fields.
    scrubbed_account = dict(account)
    if scrubbed_account.get("id") is not None:
        scrubbed_account["id"] = "REDACTED"
    if scrubbed_account.get("account_number") is not None:
        scrubbed_account["account_number"] = "REDACTED"
    interactions.append({"type": "get_account", "result": scrubbed_account})

    # 2) get_options_chain (use SPY for liquidity; cap rows for cassette size).
    chain = client.get_options_chain("SPY")
    interactions.append(
        {
            "type": "get_options_chain",
            "ticker": "SPY",
            "row_count_total": len(chain),
            "result": chain[:5],
        }
    )

    # 3) submit_order — 1-share SPY equity at a far-from-market limit so it
    # stays open long enough to cancel without filling.
    coid = f"live-cassette-{uuid.uuid4().hex[:12]}"
    order_req = LimitOrderRequest(
        symbol="SPY",
        qty=1,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=1.00,  # far below SPY market price; will not fill
        client_order_id=coid,
    )
    submit_result = client.submit_order(order_req)
    interactions.append({"type": "submit_order", "result": submit_result})

    # 4) cancel_order
    order_id = submit_result.get("id")
    if not order_id:
        raise SystemExit("submit_order returned no id; cannot cancel")
    client.cancel_order(order_id)
    interactions.append(
        {
            "type": "cancel_order",
            "request": {"order_id": order_id},
            "result": None,
        }
    )

    cassette = {
        "comment": (
            "Live recording from Alpaca paper sandbox 2026-04-27. Account "
            "id and account_number redacted. SPY equity order @ $1 limit "
            "submitted then cancelled (never filled)."
        ),
        "interactions": interactions,
    }
    out = CASSETTE_DIR / "alpaca" / f"live_account_chain_order_{CASSETTE_DATE}.json"
    _write_cassette(out, cassette)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


RECORDERS = {
    "xai": record_xai,
    "claude": record_claude,
    "gemini": record_gemini,
    "alpaca": record_alpaca,
}


def main(argv: list[str]) -> int:
    targets = argv or list(RECORDERS.keys())
    unknown = [t for t in targets if t not in RECORDERS]
    if unknown:
        raise SystemExit(f"Unknown recorder target(s): {unknown}")
    for name in targets:
        print(f"--- recording {name} ---")
        RECORDERS[name]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
