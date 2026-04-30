"""Tests for :func:`biotech_sniper.llm.ensemble.score_candidate_event`.

Covers the f-m3-03 Reading-B Stage-2 4-provider event-driven fan-out:
parallel ThreadPoolExecutor max_workers=4, per-provider 20 s timeout,
partial-failure tolerance, persistence to ``ensemble_scores_event``,
fail-safe on all-4-fail (`insufficient_providers`), null/empty label
coercion to ``ambiguous``, and idempotent partial recovery.

Tests use stub provider callables (no real LLM HTTP traffic) so the
suite runs hermetically without VCR cassettes for this layer; the
provider clients themselves are covered by their own dedicated test
modules (test_xai_client, test_claude_client, test_gemini_client,
test_perplexity_client).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from biotech_sniper import db as project_db
from biotech_sniper.llm.ensemble import (
    ALL_PROVIDERS,
    EnsembleEventResult,
    ProviderResult,
    score_candidate_event,
)
from biotech_sniper.migrations.runner import run as run_v10


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_v10_db(db_path):
    """Create a fresh test DB at v10 with one candidate_events row seeded."""
    # Build a v9 db, then migrate to v10.
    conn = project_db.connect(db_path)
    try:
        project_db.run_migrations(conn)
    finally:
        conn.close()
    run_v10(db_path, 10, take_backup_first=False)

    conn = project_db.connect(db_path)
    try:
        # Need at least one news_events row for the FK target.
        conn.execute(
            "INSERT INTO news_events ("
            "ticker, source, title, url, published_at, ingested_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                "TESTX",
                "rss",
                "TESTX announces positive Phase III readout",
                "https://example.com/testx-readout",
                "2026-04-30T12:00:00Z",
                "2026-04-30T12:00:01Z",
            ),
        )
        news_id = conn.execute("SELECT MAX(id) FROM news_events").fetchone()[0]
        conn.execute(
            "INSERT INTO candidate_events ("
            "ticker, source_news_event_id, matched_keywords, "
            "calendar_match, emitted_at, dedup_key"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                "TESTX",
                news_id,
                "phase_iii,readout",
                None,
                "2026-04-30T12:00:02Z",
                "dedup-testx-001",
            ),
        )
        cand_id = conn.execute(
            "SELECT MAX(id) FROM candidate_events"
        ).fetchone()[0]
        conn.commit()
        return cand_id
    finally:
        conn.close()


def _make_unanimous_material_provider(
    *,
    direction: str = "bullish",
    probability: float = 0.85,
    sleep: float = 0.0,
):
    """Return a provider callable that returns the canonical material verdict."""
    def _call(candidate, *, name=None, sleep=sleep):  # noqa: ARG001
        if sleep > 0:
            time.sleep(sleep)
        return {
            "label": "material",
            "probability": probability,
            "direction": direction,
            "rationale": "stub rationale",
            "citations": [{"url": "https://example.com", "title": "ex"}],
            "latency_ms": int(sleep * 1000),
            "cost_usd": 0.001,
        }
    return _call


def _make_failing_provider(exc: BaseException):
    def _call(candidate, *, name=None):  # noqa: ARG001
        raise exc
    return _call


def _make_slow_provider(sleep_seconds: float):
    def _call(candidate, *, name=None):  # noqa: ARG001
        time.sleep(sleep_seconds)
        return {
            "label": "material",
            "probability": 0.9,
            "direction": "bullish",
            "rationale": "slow",
            "citations": [],
            "latency_ms": int(sleep_seconds * 1000),
            "cost_usd": 0.001,
        }
    return _call


def _all_unanimous(direction="bullish", probability=0.85):
    return {
        p: _make_unanimous_material_provider(
            direction=direction, probability=probability
        )
        for p in ALL_PROVIDERS
    }


# ---------------------------------------------------------------------------
# VAL-M3-016: shape
# ---------------------------------------------------------------------------


def test_score_candidate_event_shape(tmp_path):
    db_path = tmp_path / "alpha.db"
    cand_id = _build_v10_db(db_path)

    result = score_candidate_event(
        {"id": cand_id, "ticker": "TESTX",
         "matched_keywords": "phase_iii,readout"},
        run_id="run-001",
        db_path=db_path,
        providers=_all_unanimous(),
    )

    assert isinstance(result, EnsembleEventResult)
    assert result.candidate_event_id == cand_id
    assert result.run_id == "run-001"
    assert len(result.per_provider_results) == 4
    providers_seen = {r.provider for r in result.per_provider_results}
    assert providers_seen == set(ALL_PROVIDERS)
    # Canonical order: xai, anthropic, gemini, perplexity.
    assert [r.provider for r in result.per_provider_results] == list(
        ALL_PROVIDERS
    )
    # Every successful provider has the documented fields.
    for r in result.per_provider_results:
        assert r.error is None
        assert r.label == "material"
        assert r.probability == pytest.approx(0.85)
        assert r.direction == "bullish"
        assert isinstance(r.rationale, str)
        assert isinstance(r.citations, list)
        assert isinstance(r.latency_ms, int)
        assert isinstance(r.cost_usd, float)
    # Aggregate fields.
    assert result.label == "material"
    assert result.direction == "bullish"
    assert result.mean_probability == pytest.approx(0.85)
    assert result.successful_providers == list(ALL_PROVIDERS)
    assert result.failed_providers == []
    assert result.gate_failed_reason is None


# ---------------------------------------------------------------------------
# VAL-M3-017: parallel fan-out wall time
# ---------------------------------------------------------------------------


def test_parallel_fanout_wall_time(tmp_path):
    db_path = tmp_path / "alpha.db"
    cand_id = _build_v10_db(db_path)

    providers = {
        p: _make_slow_provider(1.0) for p in ALL_PROVIDERS
    }
    t0 = time.perf_counter()
    result = score_candidate_event(
        {"id": cand_id, "ticker": "TESTX"},
        run_id="run-parallel",
        db_path=db_path,
        providers=providers,
    )
    elapsed = time.perf_counter() - t0
    # Serial would be ~4.0s; parallel must be < 1.5s.
    assert elapsed < 1.5, f"expected parallel fan-out, got {elapsed:.2f}s"
    assert all(r.error is None for r in result.per_provider_results)


# ---------------------------------------------------------------------------
# VAL-M3-018: per-provider 20s timeout
# ---------------------------------------------------------------------------


def test_per_provider_timeout(tmp_path):
    db_path = tmp_path / "alpha.db"
    cand_id = _build_v10_db(db_path)

    # Three fast, one slow beyond timeout.
    providers = {
        "xai": _make_unanimous_material_provider(),
        "anthropic": _make_unanimous_material_provider(),
        "gemini": _make_unanimous_material_provider(),
        "perplexity": _make_slow_provider(5.0),
    }
    t0 = time.perf_counter()
    result = score_candidate_event(
        {"id": cand_id, "ticker": "TESTX"},
        run_id="run-timeout",
        db_path=db_path,
        providers=providers,
        timeout_seconds=0.5,
    )
    elapsed = time.perf_counter() - t0
    assert elapsed < 2.0, f"timeout not enforced: elapsed={elapsed:.2f}s"
    pp = {r.provider: r for r in result.per_provider_results}
    assert pp["perplexity"].error is not None
    assert "timeout" in (pp["perplexity"].error or "").lower()
    # The other three should succeed.
    for name in ("xai", "anthropic", "gemini"):
        assert pp[name].error is None
        assert pp[name].label == "material"


# ---------------------------------------------------------------------------
# VAL-M3-019: partial-failure tolerated; mean over successful only.
# ---------------------------------------------------------------------------


def test_partial_failure_unanimity_rejects(tmp_path):
    db_path = tmp_path / "alpha.db"
    cand_id = _build_v10_db(db_path)

    providers = {
        "xai": _make_unanimous_material_provider(probability=0.80),
        "anthropic": _make_unanimous_material_provider(probability=0.80),
        "gemini": _make_unanimous_material_provider(probability=0.80),
        "perplexity": _make_failing_provider(RuntimeError("transport")),
    }
    result = score_candidate_event(
        {"id": cand_id, "ticker": "TESTX"},
        run_id="run-partial",
        db_path=db_path,
        providers=providers,
    )
    pp = {r.provider: r for r in result.per_provider_results}
    assert pp["perplexity"].error is not None
    assert result.successful_providers == ["xai", "anthropic", "gemini"]
    assert result.failed_providers == ["perplexity"]
    # Unanimity rejects (3 of 4 successful).
    assert result.gate_failed_reason == "unanimity"
    # Mean computed over successful only.
    assert result.mean_probability == pytest.approx(0.80)


# ---------------------------------------------------------------------------
# VAL-M3-020 / VAL-M3-021: persistence to ensemble_scores_event
# ---------------------------------------------------------------------------


def test_persists_to_ensemble_scores_event(tmp_path):
    db_path = tmp_path / "alpha.db"
    cand_id = _build_v10_db(db_path)

    score_candidate_event(
        {"id": cand_id, "ticker": "TESTX"},
        run_id="run-persist",
        db_path=db_path,
        providers=_all_unanimous(),
    )

    conn = project_db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT provider, label, probability, direction, "
            "rationale, citations, latency_ms, cost_usd, called_at "
            "FROM ensemble_scores_event "
            "WHERE candidate_event_id=? ORDER BY provider",
            (cand_id,),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 4
    providers = sorted(r["provider"] for r in rows)
    assert providers == sorted(ALL_PROVIDERS)
    for r in rows:
        assert r["label"] == "material"
        assert r["probability"] == pytest.approx(0.85)
        assert r["direction"] == "bullish"
        assert r["called_at"]


# ---------------------------------------------------------------------------
# VAL-M3-076: all 4 fail → fail-safe `insufficient_providers`, no raise
# ---------------------------------------------------------------------------


def test_all_four_providers_fail_no_order(tmp_path):
    db_path = tmp_path / "alpha.db"
    cand_id = _build_v10_db(db_path)

    providers = {
        p: _make_failing_provider(RuntimeError(f"{p} broken"))
        for p in ALL_PROVIDERS
    }
    # MUST NOT raise.
    result = score_candidate_event(
        {"id": cand_id, "ticker": "TESTX"},
        run_id="run-allfail",
        db_path=db_path,
        providers=providers,
    )
    assert result.gate_failed_reason == "insufficient_providers"
    assert result.successful_providers == []
    assert all(r.error is not None for r in result.per_provider_results)
    # No rows persisted.
    conn = project_db.connect(db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM ensemble_scores_event "
            "WHERE candidate_event_id=?",
            (cand_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 0


# ---------------------------------------------------------------------------
# VAL-M3-077: 2/4 fail behaves identically (unanimity rejects)
# ---------------------------------------------------------------------------


def test_2_of_4_failure_unanimity_rejects(tmp_path):
    db_path = tmp_path / "alpha.db"
    cand_id = _build_v10_db(db_path)

    providers = {
        "xai": _make_unanimous_material_provider(),
        "anthropic": _make_unanimous_material_provider(),
        "gemini": _make_failing_provider(RuntimeError("g down")),
        "perplexity": _make_failing_provider(RuntimeError("p down")),
    }
    result = score_candidate_event(
        {"id": cand_id, "ticker": "TESTX"},
        run_id="run-2of4",
        db_path=db_path,
        providers=providers,
    )
    assert result.gate_failed_reason == "unanimity"
    assert result.successful_providers == ["xai", "anthropic"]
    assert result.failed_providers == ["gemini", "perplexity"]
    conn = project_db.connect(db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM ensemble_scores_event "
            "WHERE candidate_event_id=?",
            (cand_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 2


# ---------------------------------------------------------------------------
# VAL-M3-078: null/empty label coerced to 'ambiguous'
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_label", [None, "", "   "])
def test_null_label_treated_as_ambiguous(tmp_path, bad_label):
    db_path = tmp_path / "alpha.db"
    cand_id = _build_v10_db(db_path)

    def _bad_provider(candidate, *, name=None):  # noqa: ARG001
        return {
            "label": bad_label,
            "probability": 0.9,
            "direction": "bullish",
            "rationale": "hmm",
            "citations": [],
            "latency_ms": 100,
            "cost_usd": 0.001,
        }

    providers = {
        "xai": _make_unanimous_material_provider(),
        "anthropic": _make_unanimous_material_provider(),
        "gemini": _make_unanimous_material_provider(),
        "perplexity": _bad_provider,
    }
    result = score_candidate_event(
        {"id": cand_id, "ticker": "TESTX"},
        run_id="run-nulllabel",
        db_path=db_path,
        providers=providers,
    )
    pp = {r.provider: r for r in result.per_provider_results}
    assert pp["perplexity"].label == "ambiguous"
    # Unanimity blocked.
    assert result.gate_failed_reason == "unanimity"

    conn = project_db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT label FROM ensemble_scores_event "
            "WHERE candidate_event_id=? AND provider='perplexity'",
            (cand_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "ambiguous"


# ---------------------------------------------------------------------------
# VAL-M3-084: idempotent partial recovery — re-call only failed providers
# ---------------------------------------------------------------------------


def test_partial_recovery_recalls_failed_providers_only(tmp_path):
    db_path = tmp_path / "alpha.db"
    cand_id = _build_v10_db(db_path)

    call_counts = {p: 0 for p in ALL_PROVIDERS}

    def _make_counter_provider(name, *, fail_first=False):
        def _call(candidate, *, name=name):  # noqa: ARG001
            call_counts[name] += 1
            if fail_first and call_counts[name] == 1:
                raise RuntimeError(f"{name} first call fails")
            return {
                "label": "material",
                "probability": 0.85,
                "direction": "bullish",
                "rationale": f"{name} ok",
                "citations": [],
                "latency_ms": 50,
                "cost_usd": 0.001,
            }
        return _call

    providers_round_1 = {
        "xai": _make_counter_provider("xai", fail_first=False),
        "anthropic": _make_counter_provider("anthropic", fail_first=False),
        "gemini": _make_counter_provider("gemini", fail_first=True),
        "perplexity": _make_counter_provider("perplexity", fail_first=True),
    }

    score_candidate_event(
        {"id": cand_id, "ticker": "TESTX"},
        run_id="run-recover",
        db_path=db_path,
        providers=providers_round_1,
    )
    # Round 1: xai+anthropic succeed, gemini+perplexity fail.
    assert call_counts == {
        "xai": 1, "anthropic": 1, "gemini": 1, "perplexity": 1,
    }

    # Round 2 — partial recovery: only gemini+perplexity should be re-called.
    result2 = score_candidate_event(
        {"id": cand_id, "ticker": "TESTX"},
        run_id="run-recover",
        db_path=db_path,
        providers=providers_round_1,
    )
    assert call_counts == {
        "xai": 1, "anthropic": 1, "gemini": 2, "perplexity": 2,
    }
    assert result2.gate_failed_reason is None
    assert sorted(result2.successful_providers) == sorted(ALL_PROVIDERS)

    # ensemble_scores_event has exactly 4 rows.
    conn = project_db.connect(db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM ensemble_scores_event "
            "WHERE candidate_event_id=? AND run_id=?",
            (cand_id, "run-recover"),
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 4


# ---------------------------------------------------------------------------
# VAL-M3-088: ensemble_scores_event rejects unknown provider value
# ---------------------------------------------------------------------------


def test_unknown_provider_rejected_at_insert(tmp_path):
    db_path = tmp_path / "alpha.db"
    cand_id = _build_v10_db(db_path)

    conn = project_db.connect(db_path)
    try:
        # Each enumerated value individually accepted.
        for p in ALL_PROVIDERS:
            conn.execute(
                "INSERT INTO ensemble_scores_event ("
                "candidate_event_id, provider, run_id, label, "
                "probability, direction, called_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cand_id, p, f"r-{p}", "material", 0.9, "bullish",
                 "2026-04-30T00:00:00Z"),
            )
        conn.commit()
        # Unknown provider rejected.
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            conn.execute(
                "INSERT INTO ensemble_scores_event ("
                "candidate_event_id, provider, run_id, label, "
                "probability, direction, called_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cand_id, "openai", "r-openai", "material", 0.9, "bullish",
                 "2026-04-30T00:00:00Z"),
            )
        assert "CHECK constraint" in str(exc_info.value)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Idempotent same-run-id: re-call doesn't duplicate rows.
# ---------------------------------------------------------------------------


def test_idempotent_per_run_id(tmp_path):
    db_path = tmp_path / "alpha.db"
    cand_id = _build_v10_db(db_path)

    score_candidate_event(
        {"id": cand_id, "ticker": "TESTX"},
        run_id="run-same",
        db_path=db_path,
        providers=_all_unanimous(),
    )
    score_candidate_event(
        {"id": cand_id, "ticker": "TESTX"},
        run_id="run-same",
        db_path=db_path,
        providers=_all_unanimous(),
    )
    conn = project_db.connect(db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM ensemble_scores_event "
            "WHERE candidate_event_id=? AND run_id=?",
            (cand_id, "run-same"),
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 4

    # Fresh run_id produces another set.
    score_candidate_event(
        {"id": cand_id, "ticker": "TESTX"},
        run_id="run-fresh",
        db_path=db_path,
        providers=_all_unanimous(),
    )
    conn = project_db.connect(db_path)
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM ensemble_scores_event "
            "WHERE candidate_event_id=?",
            (cand_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert total == 8


# ---------------------------------------------------------------------------
# Citations JSON round-trip (VAL-M3-087)
# ---------------------------------------------------------------------------


def test_citations_json_round_trip(tmp_path):
    db_path = tmp_path / "alpha.db"
    cand_id = _build_v10_db(db_path)

    citations_payload = [
        {"url": "https://example.com/a", "title": "A — \"quoted\""},
        {"url": "https://example.com/b", "title": "B \\ slash"},
        {"url": "https://example.com/c", "title": "C unicode 漢字"},
    ]

    def _provider(candidate, *, name=None):  # noqa: ARG001
        return {
            "label": "material",
            "probability": 0.85,
            "direction": "bullish",
            "rationale": "ok",
            "citations": citations_payload,
            "latency_ms": 25,
            "cost_usd": 0.001,
        }

    providers = {p: _provider for p in ALL_PROVIDERS}
    score_candidate_event(
        {"id": cand_id, "ticker": "TESTX"},
        run_id="run-cites",
        db_path=db_path,
        providers=providers,
    )

    import json as _json

    conn = project_db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT citations, json_valid(citations) "
            "FROM ensemble_scores_event WHERE candidate_event_id=?",
            (cand_id,),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 4
    for row in rows:
        assert row[1] == 1  # json_valid
        decoded = _json.loads(row[0])
        assert decoded == citations_payload


# ---------------------------------------------------------------------------
# Concurrency: multiple concurrent invocations don't crash.
# ---------------------------------------------------------------------------


def test_concurrent_invocations_distinct_candidates(tmp_path):
    db_path = tmp_path / "alpha.db"
    cand_id_a = _build_v10_db(db_path)
    # Add a second candidate.
    conn = project_db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO news_events ("
            "ticker, source, title, url, published_at, ingested_at"
            ") VALUES ('TESTY','rss','TESTY headline',"
            "'https://example.com/y','2026-04-30T12:00:00Z',"
            "'2026-04-30T12:00:01Z')"
        )
        nid = conn.execute("SELECT MAX(id) FROM news_events").fetchone()[0]
        conn.execute(
            "INSERT INTO candidate_events ("
            "ticker, source_news_event_id, matched_keywords,"
            " emitted_at, dedup_key) VALUES (?,?,?,?,?)",
            ("TESTY", nid, "phase_iii",
             "2026-04-30T12:00:02Z", "dedup-testy-001"),
        )
        cand_id_b = conn.execute(
            "SELECT MAX(id) FROM candidate_events"
        ).fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    providers = _all_unanimous()
    results = []
    errors: list[BaseException] = []

    def _runner(cand_id, run_id):
        try:
            results.append(
                score_candidate_event(
                    {"id": cand_id, "ticker": "T"},
                    run_id=run_id,
                    db_path=db_path,
                    providers=providers,
                )
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=_runner, args=(cand_id_a, "rA")),
        threading.Thread(target=_runner, args=(cand_id_b, "rB")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    assert not errors, errors
    assert len(results) == 2
