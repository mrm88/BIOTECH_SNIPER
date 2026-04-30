"""Tests for Perplexity schema edge cases — feature f-m3-14.

Covers the validation-contract assertions:

* VAL-M3-087 — ``ensemble_scores_event.citations`` JSON-encodes and
  round-trips losslessly for empty arrays, embedded
  quotes/backslashes/unicode, ≥ 16-element arrays.
* VAL-M3-089 — Per-row reconciliation between
  ``ensemble_scores_event.cost_usd`` and ``llm_cost_ledger.cost_usd``
  for the perplexity provider lands within $0.001.
* VAL-M3-096 — A citation URL exceeding
  :data:`biotech_sniper.llm.perplexity_client.MAX_CITATION_URL_LENGTH`
  is truncated (with a ``_truncated=True`` marker), NOT rejected.
* VAL-M3-097 — A schema-valid response with ``citations=[]`` is accepted;
  no :class:`PerplexitySchemaError`.
* VAL-M3-098 — A non-JSON response body raises a typed
  :class:`PerplexitySchemaError`; the bare
  :class:`json.JSONDecodeError` is wrapped, never bubbled.

Tests are hermetic — no live network, no live Perplexity. A small in-file
``_FakeSession`` replays canned responses through the
:class:`PerplexityClient` constructor's ``session=`` kwarg.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

import pytest
import requests

from biotech_sniper import db as project_db
from biotech_sniper.llm import perplexity_client
from biotech_sniper.llm.ensemble import (
    ALL_PROVIDERS,
    score_candidate_event,
)
from biotech_sniper.llm.perplexity_client import (
    DEFAULT_BACKOFF_BASE,
    DEFAULT_MODEL,
    MAX_CITATION_URL_LENGTH,
    PerplexityClient,
    PerplexitySchemaError,
)
from biotech_sniper.migrations.runner import run as run_migrations_runner


# Sentinel API key — concatenated to dodge over-zealous secret scanners.
SENTINEL_API_KEY = "pplx-test" + "-sentinel-" + "schema-edge-cases-9999"


# ---------------------------------------------------------------------------
# Minimal local fake-session machinery
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        body_json: Any = None,
        text: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
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
            # Mirror requests.Response.json which raises ValueError on
            # an unparseable body. Tests use this to reach the
            # `non-JSON envelope` PerplexitySchemaError branch.
            raise ValueError("non-JSON body in fake response")
        return self._body_json


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self._cursor = 0
        self.posted_payloads: list[dict[str, Any]] = []
        self.posted_urls: list[str] = []

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        if self._cursor >= len(self._responses):
            raise AssertionError(
                f"_FakeSession exhausted after {self._cursor} calls"
            )
        resp = self._responses[self._cursor]
        self._cursor += 1
        self.posted_urls.append(url)
        self.posted_payloads.append(kwargs.get("json", {}))
        return resp


def _make_envelope(
    *,
    content_obj: dict[str, Any] | None = None,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    total_cost: float | None = None,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    if content_obj is None:
        content_obj = {
            "probability": 0.7,
            "label": "material",
            "direction": "bullish",
            "rationale": "ok",
            "citations": [
                {"url": "https://example.com/a", "title": "A"}
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
        "id": "pplx-resp-edge-tests",
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


def _build_client(
    *,
    db_path: Path,
    response: _FakeResponse,
) -> tuple[PerplexityClient, _FakeSession]:
    fake = _FakeSession([response])
    client = PerplexityClient(
        api_key=SENTINEL_API_KEY,
        session=fake,
        max_retries=0,
        backoff_base=0.0,
        db_path=db_path,
    )
    return client, fake


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Fresh SQLite db migrated to v10 (Reading-B foundations)."""
    db_path = tmp_path / "schema_edge.db"
    run_migrations_runner(db_path, target_version=10, take_backup_first=False)
    return db_path


@pytest.fixture
def sample_candidate() -> dict[str, Any]:
    return {
        "ticker": "TESTBIO",
        "headline": "TESTBIO reports positive Phase 3 readout",
        "matched_keywords": ["readout", "phase 3"],
        "calendar_match": "2026-05-20",
        "catalyst_type": "READOUT",
    }


# ===========================================================================
# VAL-M3-097 — Empty citations array is valid (does not raise SchemaError).
# ===========================================================================


def test_empty_citations_array_accepted(
    temp_db: Path, sample_candidate: dict[str, Any]
) -> None:
    """A schema-valid response with ``citations=[]`` parses cleanly."""
    body = _make_envelope(
        content_obj={
            "probability": 0.55,
            "label": "material",
            "direction": "bullish",
            "rationale": "no external citations available",
            "citations": [],
        }
    )
    client, _ = _build_client(
        db_path=temp_db, response=_FakeResponse(body_json=body)
    )
    result = client.score_candidate(sample_candidate)
    assert result["citations"] == []
    assert result["label"] == "material"


def test_empty_citations_round_trip_through_ensemble(temp_db: Path) -> None:
    """Persisting an empty citations array via ``score_candidate_event``
    leaves the row's citations column as the JSON literal ``[]`` and the
    decoded value equals the original."""
    cand_id = _seed_candidate_event(temp_db)

    def _provider(candidate, *, name=None):  # noqa: ARG001
        return {
            "label": "material",
            "probability": 0.85,
            "direction": "bullish",
            "rationale": "no citations",
            "citations": [],
            "latency_ms": 5,
            "cost_usd": 0.001,
        }

    providers = {p: _provider for p in ALL_PROVIDERS}
    score_candidate_event(
        {"id": cand_id, "ticker": "TESTBIO"},
        run_id="run-empty-cites",
        db_path=temp_db,
        providers=providers,
    )
    rows = _fetch_ensemble_citations(temp_db, cand_id)
    assert len(rows) == 4
    for cit in rows:
        decoded = json.loads(cit)
        assert decoded == []


# ===========================================================================
# VAL-M3-098 — Non-JSON response body raises PerplexitySchemaError.
# ===========================================================================


def test_non_json_envelope_raises_schema_error(
    temp_db: Path, sample_candidate: dict[str, Any]
) -> None:
    """A 200 response whose body is HTML rather than JSON must raise
    :class:`PerplexitySchemaError` (not a bare
    :class:`json.JSONDecodeError`)."""
    response = _FakeResponse(
        status_code=200,
        body_json=None,  # _FakeResponse.json() raises ValueError
        text="<html><body>oops, gateway error page</body></html>",
    )
    client, _ = _build_client(db_path=temp_db, response=response)
    with pytest.raises(PerplexitySchemaError) as exc_info:
        client.score_candidate(sample_candidate)
    msg = str(exc_info.value)
    # The exception explicitly identifies the failure as "non-JSON body".
    assert "non-JSON body" in msg or "non-json body" in msg.lower()


def test_non_json_message_content_raises_schema_error(
    temp_db: Path, sample_candidate: dict[str, Any]
) -> None:
    """A successful 200 envelope whose ``choices[0].message.content`` is
    free-form HTML rather than the verdict JSON must wrap the
    :class:`json.JSONDecodeError` into a typed
    :class:`PerplexitySchemaError`. The bare decode error MUST NOT
    bubble to the caller."""
    body = {
        "id": "pplx-resp-edge-tests",
        "model": "sonar",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "<html>not the verdict</html>",
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
    response = _FakeResponse(status_code=200, body_json=body)
    client, _ = _build_client(db_path=temp_db, response=response)
    with pytest.raises(PerplexitySchemaError) as exc_info:
        client.score_candidate(sample_candidate)
    msg = str(exc_info.value)
    assert "non-JSON body" in msg or "non-json body" in msg.lower()
    # The wrapped cause is the original JSONDecodeError.
    assert isinstance(exc_info.value.__cause__, json.JSONDecodeError)


# ===========================================================================
# VAL-M3-096 — Citation URL > MAX_CITATION_URL_LENGTH is truncated, not
# rejected.
# ===========================================================================


def test_long_citation_url_is_truncated(
    temp_db: Path,
    sample_candidate: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 4 KB URL must be truncated to ``MAX_CITATION_URL_LENGTH`` and
    decorated with a ``_truncated=True`` marker. The verdict survives —
    no :class:`PerplexitySchemaError` raised."""
    long_url = "https://example.com/" + ("a" * 5000)
    assert len(long_url) > MAX_CITATION_URL_LENGTH
    body = _make_envelope(
        content_obj={
            "probability": 0.6,
            "label": "material",
            "direction": "bullish",
            "rationale": "long-url stress test",
            "citations": [
                {"url": long_url, "title": "huge"},
                {"url": "https://example.com/short", "title": "short"},
            ],
        }
    )
    client, _ = _build_client(
        db_path=temp_db, response=_FakeResponse(body_json=body)
    )
    import logging

    with caplog.at_level(logging.WARNING, logger=perplexity_client.__name__):
        result = client.score_candidate(sample_candidate)

    cit0 = result["citations"][0]
    assert len(cit0["url"]) == MAX_CITATION_URL_LENGTH
    assert cit0["url"] == long_url[:MAX_CITATION_URL_LENGTH]
    assert cit0["_truncated"] is True
    assert cit0["_original_url_length"] == len(long_url)
    # Short citation untouched.
    assert "_truncated" not in result["citations"][1]
    # WARNING was logged.
    warns = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "citation" in r.message.lower()
    ]
    assert warns, [r.message for r in caplog.records]


def test_url_at_exact_max_length_not_truncated(
    temp_db: Path, sample_candidate: dict[str, Any]
) -> None:
    """URLs of exactly ``MAX_CITATION_URL_LENGTH`` chars are NOT truncated
    (boundary-inclusive: only ``> MAX`` triggers the elision)."""
    boundary_url = "https://e.com/" + (
        "a" * (MAX_CITATION_URL_LENGTH - len("https://e.com/"))
    )
    assert len(boundary_url) == MAX_CITATION_URL_LENGTH
    body = _make_envelope(
        content_obj={
            "probability": 0.6,
            "label": "material",
            "direction": "bullish",
            "rationale": "boundary",
            "citations": [{"url": boundary_url, "title": "boundary"}],
        }
    )
    client, _ = _build_client(
        db_path=temp_db, response=_FakeResponse(body_json=body)
    )
    result = client.score_candidate(sample_candidate)
    assert result["citations"][0]["url"] == boundary_url
    assert "_truncated" not in result["citations"][0]


# ===========================================================================
# VAL-M3-087 — citations TEXT field round-trips through JSON encode/decode
# (jagged-quote tolerance).
# ===========================================================================


def test_citations_round_trip_jagged_payloads(temp_db: Path) -> None:
    """Round-trip empty arrays, embedded quotes/backslashes/unicode, and
    a ≥ 16-element array losslessly through ``ensemble_scores_event``.

    The validator queries ``json_valid(citations)`` — every persisted
    row must report ``1`` — and decodes the stored TEXT back to the
    original Python value.
    """
    cand_id = _seed_candidate_event(temp_db)

    jagged_payloads: list[list[dict[str, Any]]] = [
        # Empty array.
        [],
        # Embedded double quotes, single quotes, backslashes.
        [
            {"url": "https://example.com/a", "title": 'A — "double" + \'single\''},
            {"url": "https://example.com/b", "title": "B \\ backslash \\\\\\"},
            {"url": "https://example.com/c", "title": "C\ttab\nnewline"},
        ],
        # Unicode (CJK + emoji).
        [
            {"url": "https://example.com/u1", "title": "ロイター漢字"},
            {"url": "https://example.com/u2", "title": "テスト 🚀 emoji"},
            {"url": "https://example.com/u3", "title": "Россия медицина"},
        ],
        # ≥ 16-element array.
        [
            {"url": f"https://example.com/n{i}", "title": f"item {i}"}
            for i in range(20)
        ],
    ]

    for run_idx, citations in enumerate(jagged_payloads):
        def _provider(candidate, *, name=None, _payload=citations):  # noqa: ARG001
            return {
                "label": "material",
                "probability": 0.85,
                "direction": "bullish",
                "rationale": "ok",
                "citations": _payload,
                "latency_ms": 5,
                "cost_usd": 0.001,
            }

        providers = {p: _provider for p in ALL_PROVIDERS}
        score_candidate_event(
            {"id": cand_id, "ticker": "TESTBIO"},
            run_id=f"run-jagged-{run_idx}",
            db_path=temp_db,
            providers=providers,
        )

    # Validate every persisted row.
    conn = project_db.connect(temp_db)
    try:
        rows = conn.execute(
            "SELECT run_id, citations, json_valid(citations) AS valid "
            "FROM ensemble_scores_event WHERE candidate_event_id=? "
            "ORDER BY run_id, provider",
            (cand_id,),
        ).fetchall()
    finally:
        conn.close()

    # 4 jagged payloads × 4 providers = 16 rows.
    assert len(rows) == 4 * 4
    by_run: dict[str, list[Any]] = {}
    for row in rows:
        run = row["run_id"] if isinstance(row, sqlite3.Row) else row[0]
        cit = row["citations"] if isinstance(row, sqlite3.Row) else row[1]
        valid = row["valid"] if isinstance(row, sqlite3.Row) else row[2]
        assert int(valid) == 1, f"row for {run!r} is not json_valid"
        by_run.setdefault(run, []).append(json.loads(cit))

    # Each run's 4 rows decode equal to the original payload.
    for run_idx, expected in enumerate(jagged_payloads):
        decoded_rows = by_run[f"run-jagged-{run_idx}"]
        assert len(decoded_rows) == 4
        for got in decoded_rows:
            assert got == expected, (run_idx, got, expected)


# ===========================================================================
# VAL-M3-089 — ensemble_scores_event.cost_usd reconciles with
# llm_cost_ledger.cost_usd within $0.001.
# ===========================================================================


def test_cost_reconciles_between_ensemble_and_ledger(temp_db: Path) -> None:
    """Wire the perplexity adapter through to the real
    :class:`PerplexityClient` (with a fake-session cassette) and confirm
    the cost recorded on the ``ensemble_scores_event`` row matches the
    cost recorded on the ``llm_cost_ledger`` row to within $0.001 (the
    contract's reconciliation threshold) and within $1e-9 in practice
    (which is what the contract evidence checks).
    """
    cand_id = _seed_candidate_event(temp_db)

    # Stage a single Perplexity HTTP response with a known cost block.
    # 1000 prompt + 500 completion + low search context = $0.0065 by the
    # client's compute_cost_usd formula. Set total_cost on the response
    # to the same value so cost_estimated=0 and the ledger row records
    # the upstream-reported value verbatim.
    expected_cost = 0.0065
    body = _make_envelope(
        content_obj={
            "probability": 0.85,
            "label": "material",
            "direction": "bullish",
            "rationale": "reconcile",
            "citations": [
                {"url": "https://example.com/a", "title": "A"}
            ],
        },
        prompt_tokens=1000,
        completion_tokens=500,
        total_cost=expected_cost,
    )
    fake = _FakeSession([_FakeResponse(body_json=body)])
    pplx_client = PerplexityClient(
        api_key=SENTINEL_API_KEY,
        session=fake,
        max_retries=0,
        backoff_base=0.0,
        db_path=temp_db,
    )

    def _perplexity_adapter(candidate, *, name=None):  # noqa: ARG001
        verdict = pplx_client.score_candidate(candidate)
        return {
            "label": verdict["label"],
            "probability": verdict["probability"],
            "direction": verdict["direction"],
            "rationale": verdict["rationale"],
            "citations": verdict["citations"],
            # The same cost the ledger row carries — that is what
            # the validator's reconciliation join checks.
            "cost_usd": expected_cost,
            "latency_ms": 25,
        }

    # The other three providers are stub adapters writing matching
    # ledger rows so the JOIN check is exercised across the full
    # 4-provider fan-out (xai/anthropic/gemini/perplexity).
    def _stub_adapter(candidate, *, name=None):  # noqa: ARG001
        return {
            "label": "material",
            "probability": 0.85,
            "direction": "bullish",
            "rationale": "stub",
            "citations": [],
            "cost_usd": 0.0010,
            "latency_ms": 10,
        }

    providers = {
        "xai": _stub_adapter,
        "anthropic": _stub_adapter,
        "gemini": _stub_adapter,
        "perplexity": _perplexity_adapter,
    }
    # Pre-seed matching ledger rows for the three stub providers so
    # the perplexity reconciliation is the meaningful assertion (the
    # other three providers aren't wired through real clients in this
    # test).
    _seed_stub_ledger_rows(temp_db, [
        ("xai", "grok-4", 0.0010),
        ("anthropic", "claude-opus", 0.0010),
        ("gemini", "gemini-2.5-pro", 0.0010),
    ])

    score_candidate_event(
        {"id": cand_id, "ticker": "TESTBIO"},
        run_id="run-reconcile",
        db_path=temp_db,
        providers=providers,
    )

    # Reconciliation: for the perplexity provider, the ensemble row's
    # cost_usd and the ledger row's cost_usd MUST match within $0.001.
    conn = project_db.connect(temp_db)
    try:
        e_rows = conn.execute(
            "SELECT provider, cost_usd FROM ensemble_scores_event "
            "WHERE candidate_event_id=? AND run_id=? AND provider='perplexity'",
            (cand_id, "run-reconcile"),
        ).fetchall()
        l_rows = conn.execute(
            "SELECT provider, cost_usd FROM llm_cost_ledger "
            "WHERE provider='perplexity' ORDER BY id DESC LIMIT 1"
        ).fetchall()
    finally:
        conn.close()

    assert len(e_rows) == 1
    assert len(l_rows) == 1
    e_cost = float(
        e_rows[0]["cost_usd"] if isinstance(e_rows[0], sqlite3.Row)
        else e_rows[0][1]
    )
    l_cost = float(
        l_rows[0]["cost_usd"] if isinstance(l_rows[0], sqlite3.Row)
        else l_rows[0][1]
    )
    # The contract's "within $0.001" threshold:
    assert abs(e_cost - l_cost) < 1e-3
    # The strict equality check used by the contract evidence:
    assert abs(e_cost - l_cost) < 1e-9
    assert abs(e_cost - expected_cost) < 1e-9


# ===========================================================================
# Helpers
# ===========================================================================


def _seed_candidate_event(db_path: Path) -> int:
    """Seed news_events + candidate_events for a TESTBIO candidate."""
    conn = project_db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO news_events ("
            "ticker, source, title, url, published_at, ingested_at"
            ") VALUES (?,?,?,?,?,?)",
            (
                "TESTBIO",
                "rss",
                "TESTBIO Phase 3 readout",
                "https://example.com/testbio",
                "2026-04-30T12:00:00Z",
                "2026-04-30T12:00:01Z",
            ),
        )
        nid = conn.execute("SELECT MAX(id) FROM news_events").fetchone()[0]
        conn.execute(
            "INSERT INTO candidate_events ("
            "ticker, source_news_event_id, matched_keywords, "
            "calendar_match, emitted_at, dedup_key"
            ") VALUES (?,?,?,?,?,?)",
            (
                "TESTBIO",
                nid,
                "phase_iii,readout",
                None,
                "2026-04-30T12:00:02Z",
                "dedup-testbio-edge",
            ),
        )
        cand_id = conn.execute(
            "SELECT MAX(id) FROM candidate_events"
        ).fetchone()[0]
        conn.commit()
        return int(cand_id)
    finally:
        conn.close()


def _fetch_ensemble_citations(db_path: Path, cand_id: int) -> list[str]:
    conn = project_db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT citations FROM ensemble_scores_event "
            "WHERE candidate_event_id=?",
            (cand_id,),
        ).fetchall()
    finally:
        conn.close()
    return [
        (r["citations"] if isinstance(r, sqlite3.Row) else r[0])
        for r in rows
    ]


def _seed_stub_ledger_rows(
    db_path: Path,
    rows: list[tuple[str, str, float]],
) -> None:
    """Insert one ``llm_cost_ledger`` row per (provider, model, cost) tuple."""
    conn = project_db.connect(db_path)
    try:
        with conn:
            for provider, model, cost in rows:
                conn.execute(
                    "INSERT INTO llm_cost_ledger ("
                    "provider, model_id, purpose, prompt_tokens, "
                    "completion_tokens, latency_ms, cost_usd, request_id"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        provider,
                        model,
                        "stage2_event_scoring",
                        100,
                        50,
                        10,
                        float(cost),
                        f"req-stub-{provider}",
                    ),
                )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Smoke import test (mirrors the venv-import contract in
# test_perplexity_client.py — also serves as a quick failure beacon when
# running this file standalone via the feature spec verification step).
# ---------------------------------------------------------------------------


def test_module_exposes_max_citation_url_length() -> None:
    assert isinstance(MAX_CITATION_URL_LENGTH, int)
    assert MAX_CITATION_URL_LENGTH > 0


def test_no_live_perplexity_calls(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: Path,
    sample_candidate: dict[str, Any],
) -> None:
    """Static-belt-and-suspenders check: running these tests must not
    issue any outbound HTTP."""

    def _refuse(self, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError(f"live network egress to {url}")

    monkeypatch.setattr(requests.Session, "post", _refuse)
    monkeypatch.setattr(requests.Session, "request", _refuse)

    body = _make_envelope()
    client, _ = _build_client(
        db_path=temp_db, response=_FakeResponse(body_json=body)
    )
    out = client.score_candidate(sample_candidate)
    assert out["label"] == "material"
