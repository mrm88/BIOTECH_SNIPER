"""Tests for the Perplexity cost-ledger wiring (f-m3-02).

Covers the validation contract assertions VAL-M3-012, VAL-M3-013,
VAL-M3-014, VAL-M3-082, VAL-M3-099:

* VAL-M3-012 — Each successful ``score_candidate(...)`` writes exactly
  one ``llm_cost_ledger`` row with ``provider='perplexity'``, the
  resolved ``model_id``, non-null ``prompt_tokens``,
  ``completion_tokens``, ``latency_ms``, ``cost_usd``, and the
  ``purpose='stage2_event_scoring'`` default.
* VAL-M3-013 — The ``llm_cost_ledger.provider`` CHECK enum admits
  ``'perplexity'`` (no IntegrityError) and still rejects unknown values.
* VAL-M3-014 — Per-call ``cost_usd`` matches the documented sonar
  pricing: ``prompt_tokens × INPUT_RATE + completion_tokens ×
  OUTPUT_RATE + (search_context=='low' ? SEARCH_RATE : 0)``. Test
  fixture: prompt=1000, completion=500, search=low → 0.0065 USD.
* VAL-M3-082 — When the response omits ``usage.cost.total_cost`` the
  ledger writer falls back to the formula above and tags
  ``cost_estimated=1``. When ``usage.cost.total_cost`` is present it
  is used verbatim and ``cost_estimated=0``.
* VAL-M3-099 — Token usage exceeding the sanity cap (50_000) logs a
  WARNING but the recorded ``cost_usd`` reflects the true bill (no
  clipping).

The ledger row is written BEFORE the verdict is consumed by the
ensemble logic (audit-safe): even when schema validation raises, the
row is already persisted.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Mapping

import pytest
import requests

from biotech_sniper import db
from biotech_sniper import db as project_db
from biotech_sniper.migrations.runner import run as run_migrations_runner
from biotech_sniper.llm import perplexity_client
from biotech_sniper.llm.perplexity_client import (
    DEFAULT_BACKOFF_BASE,
    DEFAULT_MODEL,
    INPUT_USD_PER_TOKEN,
    OUTPUT_USD_PER_TOKEN,
    SEARCH_USD_PER_LOW_REQUEST,
    TOKEN_SANITY_CAP,
    PerplexityClient,
    PerplexitySchemaError,
    compute_cost_usd,
)


SENTINEL_API_KEY = "pplx-test" + "-sentinel-" + "0123456789abcdef"


# ---------------------------------------------------------------------------
# Fake session replay machinery (small, local — independent of the
# tests/test_perplexity_client.py harness so this file can be run
# standalone via the verification step in the feature spec).
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
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
            raise ValueError("No JSON body in fake response")
        return self._body_json


class _FakeSession:
    def __init__(self, response_bodies: list[Any]):
        self._responses = list(response_bodies)
        self._cursor = 0
        self.posted_payloads: list[dict[str, Any]] = []
        self.posted_urls: list[str] = []

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        if self._cursor >= len(self._responses):
            raise AssertionError(
                f"_FakeSession exhausted after {self._cursor} calls"
            )
        body = self._responses[self._cursor]
        self._cursor += 1
        self.posted_urls.append(url)
        self.posted_payloads.append(kwargs.get("json", {}))
        return _FakeResponse(status_code=200, body_json=body)


def _make_response_body(
    *,
    content_obj: dict[str, Any] | None = None,
    prompt_tokens: int = 220,
    completion_tokens: int = 90,
    total_cost: float | None = None,
    model: str = "sonar",
) -> dict[str, Any]:
    """Build a valid Perplexity chat-completions envelope."""
    if content_obj is None:
        content_obj = {
            "probability": 0.84,
            "label": "material",
            "direction": "bullish",
            "rationale": "Phase 3 readout positive; FDA briefing scheduled.",
            "citations": [
                {
                    "url": "https://example.com/idya",
                    "title": "Idaya readout positive",
                }
            ],
        }
    usage: dict[str, Any] = {
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "total_tokens": int(prompt_tokens) + int(completion_tokens),
    }
    if total_cost is not None:
        usage["cost"] = {"total_cost": float(total_cost)}
    return {
        "id": "pplx-resp-ledger-test",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(content_obj),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Fresh SQLite db with the v10 (Reading-B foundations) schema applied.

    The v10 migration extends ``llm_cost_ledger.provider`` CHECK to
    accept ``'perplexity'`` (VAL-M3-013). We use the canonical
    migration runner so tests exercise the same path the VPS deploy
    uses.
    """
    db_path = tmp_path / "ledger.db"
    run_migrations_runner(db_path, target_version=project_db.CURRENT_VERSION, take_backup_first=False)
    return db_path


@pytest.fixture
def sample_candidate() -> dict[str, Any]:
    return {
        "ticker": "IDYA",
        "headline": "Idaya reports positive Phase 3 readout for IDYA-3001",
        "matched_keywords": ["readout", "phase 3"],
        "calendar_match": "2026-05-20",
        "catalyst_type": "READOUT",
    }


def _build_client(
    *,
    db_path: Path,
    response_bodies: list[Any],
    purpose: str | None = None,
    model: str = DEFAULT_MODEL,
) -> tuple[PerplexityClient, _FakeSession]:
    fake = _FakeSession(response_bodies)
    kwargs: dict[str, Any] = {
        "api_key": SENTINEL_API_KEY,
        "session": fake,
        "max_retries": 0,
        "backoff_base": 0.0,
        "db_path": db_path,
        "model": model,
    }
    if purpose is not None:
        kwargs["default_purpose"] = purpose
    client = PerplexityClient(**kwargs)
    return client, fake


# ---------------------------------------------------------------------------
# VAL-M3-013: CHECK enum accepts perplexity, rejects unknown.
# ---------------------------------------------------------------------------


def test_check_enum_accepts_perplexity(temp_db: Path) -> None:
    conn = db.connect(temp_db)
    try:
        # Directly insert a row with provider='perplexity' — must succeed.
        with conn:
            conn.execute(
                "INSERT INTO llm_cost_ledger ("
                "provider, model_id, purpose, prompt_tokens, "
                "completion_tokens, latency_ms, cost_usd, request_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "perplexity",
                    "sonar",
                    "stage2_event_scoring",
                    100,
                    50,
                    250,
                    0.0065,
                    "pplx-resp-001",
                ),
            )
        row = conn.execute(
            "SELECT provider, model_id FROM llm_cost_ledger "
            "WHERE provider='perplexity'"
        ).fetchone()
        assert row is not None
        assert row["provider"] == "perplexity"
        assert row["model_id"] == "sonar"
    finally:
        conn.close()


def test_check_enum_rejects_unknown_provider(temp_db: Path) -> None:
    conn = db.connect(temp_db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO llm_cost_ledger (provider, model_id) "
                "VALUES (?, ?)",
                ("openai", "gpt-4o"),
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# VAL-M3-014: cost formula matches sonar pricing.
# ---------------------------------------------------------------------------


def test_cost_formula_matches_sonar_pricing() -> None:
    """prompt=1000 + completion=500 + search=low → 0.0065 USD."""
    cost = compute_cost_usd(
        prompt_tokens=1000,
        completion_tokens=500,
        search_context="low",
    )
    expected = (
        1000 * INPUT_USD_PER_TOKEN
        + 500 * OUTPUT_USD_PER_TOKEN
        + SEARCH_USD_PER_LOW_REQUEST
    )
    assert abs(cost - expected) < 1e-9
    assert abs(cost - 0.0065) < 1e-9


def test_cost_formula_search_other_contexts_excludes_search_rate() -> None:
    """Only search_context='low' triggers the SEARCH_RATE add."""
    base = (
        1000 * INPUT_USD_PER_TOKEN + 500 * OUTPUT_USD_PER_TOKEN
    )
    for ctx in ("medium", "high", None, ""):
        c = compute_cost_usd(
            prompt_tokens=1000, completion_tokens=500, search_context=ctx
        )
        assert abs(c - base) < 1e-9


def test_cost_formula_zero_tokens_zero_search_returns_zero() -> None:
    assert (
        compute_cost_usd(
            prompt_tokens=0, completion_tokens=0, search_context=None
        )
        == 0.0
    )


# ---------------------------------------------------------------------------
# VAL-M3-012: writes one cost-ledger row with all required fields.
# ---------------------------------------------------------------------------


def test_score_candidate_writes_one_ledger_row(
    temp_db: Path, sample_candidate: dict[str, Any]
) -> None:
    body = _make_response_body(prompt_tokens=220, completion_tokens=90)
    client, fake = _build_client(db_path=temp_db, response_bodies=[body])

    pre_count = _count_perplexity_rows(temp_db)
    client.score_candidate(sample_candidate)
    post_count = _count_perplexity_rows(temp_db)

    assert post_count - pre_count == 1
    assert fake.posted_urls == [client.endpoint_url]

    row = _fetch_latest_perplexity_row(temp_db)
    assert row["provider"] == "perplexity"
    assert row["model_id"] == "sonar"
    assert row["purpose"] == "stage2_event_scoring"
    assert row["prompt_tokens"] == 220
    assert row["completion_tokens"] == 90
    assert row["latency_ms"] is not None
    assert int(row["latency_ms"]) >= 0
    assert row["cost_usd"] is not None
    assert float(row["cost_usd"]) >= 0.0
    assert row["called_at"] is not None


def test_score_candidate_writes_one_row_per_call(
    temp_db: Path, sample_candidate: dict[str, Any]
) -> None:
    body1 = _make_response_body(prompt_tokens=10, completion_tokens=5)
    body2 = _make_response_body(prompt_tokens=20, completion_tokens=15)
    client, _ = _build_client(
        db_path=temp_db, response_bodies=[body1, body2]
    )
    client.score_candidate(sample_candidate)
    client.score_candidate(sample_candidate)
    assert _count_perplexity_rows(temp_db) == 2


def test_score_candidate_uses_explicit_model_in_ledger(
    temp_db: Path, sample_candidate: dict[str, Any]
) -> None:
    body = _make_response_body(model="sonar")
    client, _ = _build_client(
        db_path=temp_db, response_bodies=[body], model="sonar"
    )
    client.score_candidate(sample_candidate)
    row = _fetch_latest_perplexity_row(temp_db)
    assert row["model_id"] == "sonar"


# ---------------------------------------------------------------------------
# VAL-M3-082: cost_estimated tagging.
# ---------------------------------------------------------------------------


def test_cost_uses_total_cost_when_present(
    temp_db: Path, sample_candidate: dict[str, Any]
) -> None:
    body = _make_response_body(
        prompt_tokens=1000, completion_tokens=500, total_cost=0.0123
    )
    client, _ = _build_client(db_path=temp_db, response_bodies=[body])
    client.score_candidate(sample_candidate)
    row = _fetch_latest_perplexity_row(temp_db)
    assert abs(float(row["cost_usd"]) - 0.0123) < 1e-9
    assert int(row["cost_estimated"]) == 0


def test_cost_fallback_when_usage_cost_total_missing(
    temp_db: Path, sample_candidate: dict[str, Any]
) -> None:
    body = _make_response_body(
        prompt_tokens=1000, completion_tokens=500, total_cost=None
    )
    client, _ = _build_client(db_path=temp_db, response_bodies=[body])
    client.score_candidate(sample_candidate)
    row = _fetch_latest_perplexity_row(temp_db)
    expected = (
        1000 * INPUT_USD_PER_TOKEN
        + 500 * OUTPUT_USD_PER_TOKEN
        + SEARCH_USD_PER_LOW_REQUEST
    )
    assert abs(float(row["cost_usd"]) - expected) < 1e-9
    assert float(row["cost_usd"]) > 0.0
    assert int(row["cost_estimated"]) == 1


# ---------------------------------------------------------------------------
# VAL-M3-099: token usage > sanity cap logs WARNING, bills accurately.
# ---------------------------------------------------------------------------


def test_excessive_token_usage_warns_but_bills_accurately(
    temp_db: Path,
    sample_candidate: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 100k tokens — well above the 50k sanity cap.
    body = _make_response_body(
        prompt_tokens=60000, completion_tokens=40000, total_cost=None
    )
    client, _ = _build_client(db_path=temp_db, response_bodies=[body])
    with caplog.at_level(logging.WARNING, logger=perplexity_client.__name__):
        client.score_candidate(sample_candidate)

    # Warning logged with the sanity-cap value.
    sanity_messages = [
        rec.message
        for rec in caplog.records
        if rec.levelno >= logging.WARNING
        and "token" in rec.message.lower()
        and (
            str(TOKEN_SANITY_CAP) in rec.message
            or "sanity" in rec.message.lower()
        )
    ]
    assert sanity_messages, (
        f"expected WARNING about token sanity cap, got: "
        f"{[r.message for r in caplog.records]}"
    )

    # Cost reflects the TRUE bill (fallback formula on full token counts).
    row = _fetch_latest_perplexity_row(temp_db)
    expected = (
        60000 * INPUT_USD_PER_TOKEN
        + 40000 * OUTPUT_USD_PER_TOKEN
        + SEARCH_USD_PER_LOW_REQUEST
    )
    assert abs(float(row["cost_usd"]) - expected) < 1e-9
    assert int(row["prompt_tokens"]) == 60000
    assert int(row["completion_tokens"]) == 40000


def test_token_usage_below_cap_does_not_warn(
    temp_db: Path,
    sample_candidate: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = _make_response_body(prompt_tokens=200, completion_tokens=80)
    client, _ = _build_client(db_path=temp_db, response_bodies=[body])
    with caplog.at_level(logging.WARNING, logger=perplexity_client.__name__):
        client.score_candidate(sample_candidate)
    sanity_warnings = [
        rec
        for rec in caplog.records
        if rec.levelno >= logging.WARNING and "sanity" in rec.message.lower()
    ]
    assert sanity_warnings == []


# ---------------------------------------------------------------------------
# Audit-safe ordering: ledger row is written BEFORE schema validation
# raises (so ensemble logic / cost-cap projections see the bill even on
# malformed responses).
# ---------------------------------------------------------------------------


def test_ledger_written_before_validation_failure(
    temp_db: Path, sample_candidate: dict[str, Any]
) -> None:
    bad_content = {
        # probability out of range — schema validator will raise.
        "probability": 1.5,
        "label": "material",
        "direction": "bullish",
        "rationale": "x",
        "citations": [{"url": "https://example.com", "title": "x"}],
    }
    body = _make_response_body(
        content_obj=bad_content,
        prompt_tokens=120,
        completion_tokens=40,
        total_cost=0.001,
    )
    client, _ = _build_client(db_path=temp_db, response_bodies=[body])

    with pytest.raises(PerplexitySchemaError):
        client.score_candidate(sample_candidate)

    # Even though the call surface raised, the ledger row IS persisted.
    row = _fetch_latest_perplexity_row(temp_db)
    assert row is not None
    assert row["provider"] == "perplexity"
    assert int(row["prompt_tokens"]) == 120
    assert int(row["completion_tokens"]) == 40
    assert abs(float(row["cost_usd"]) - 0.001) < 1e-9


# ---------------------------------------------------------------------------
# Network egress / single-source-of-truth invariants.
# ---------------------------------------------------------------------------


def test_no_live_perplexity_calls(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: Path,
    sample_candidate: dict[str, Any],
) -> None:
    def _refuse(self, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError(f"live network egress to {url}")

    monkeypatch.setattr(requests.Session, "post", _refuse)
    monkeypatch.setattr(requests.Session, "request", _refuse)

    body = _make_response_body()
    client, _ = _build_client(db_path=temp_db, response_bodies=[body])
    client.score_candidate(sample_candidate)
    assert _count_perplexity_rows(temp_db) == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_perplexity_rows(db_path: Path) -> int:
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM llm_cost_ledger "
            "WHERE provider = 'perplexity'"
        ).fetchone()
    finally:
        conn.close()
    return int(row["n"])


def _fetch_latest_perplexity_row(db_path: Path) -> sqlite3.Row:
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM llm_cost_ledger "
            "WHERE provider = 'perplexity' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "no llm_cost_ledger row with provider='perplexity'"
    return row
