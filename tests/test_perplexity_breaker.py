"""Tests for the Perplexity in-process circuit breaker.

Covers feature ``f-m3-12-circuit-breaker`` and validation contract
assertions VAL-M3-063..067, VAL-M3-086, VAL-M3-095.

The breaker is a module-level singleton in
:mod:`biotech_sniper.exec.breaker`. It opens on > 20% 5xx (or
timeouts) in a 60-second sliding window with a minimum of 5
requests, holds OPEN for 5 minutes, then transitions to HALF_OPEN
where a single probe call decides whether it returns to CLOSED
(probe success) or re-opens for another 5 minutes (probe failure).
HTTP 429 responses are tracked for retry-with-backoff (and the
``Retry-After`` header is honored as a lower bound on retry sleep)
but do NOT count toward the 5xx breaker threshold.

Tests run hermetically against a fake clock so the 5-minute OPEN
window can be exercised without sleeping for 5 minutes; production
code uses :func:`time.monotonic` by default.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

import pytest

from biotech_sniper.exec import breaker as breaker_module
from biotech_sniper.exec.breaker import (
    BreakerState,
    PerplexityBreaker,
    parse_retry_after,
)


# ---------------------------------------------------------------------------
# Fake clock + fixtures
# ---------------------------------------------------------------------------


class _FakeClock:
    """Monotonic-style clock that only advances when the test asks it to."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


@pytest.fixture
def fake_clock():
    return _FakeClock()


@pytest.fixture
def breaker(fake_clock):
    """Return a fresh PerplexityBreaker bound to a fake clock."""
    return PerplexityBreaker(clock=fake_clock)


@pytest.fixture(autouse=True)
def _reset_global_breaker():
    """Reset the module-level singleton before and after each test."""
    breaker_module.reset_breaker_for_test()
    yield
    breaker_module.reset_breaker_for_test()


# ---------------------------------------------------------------------------
# State-machine basics
# ---------------------------------------------------------------------------


def test_breaker_starts_closed(breaker):
    assert breaker.state is BreakerState.CLOSED
    assert breaker.allow_request() is True
    assert breaker.is_open() is False


def test_record_success_keeps_closed(breaker):
    for _ in range(10):
        breaker.record_success()
    assert breaker.state is BreakerState.CLOSED


# ---------------------------------------------------------------------------
# VAL-M3-063: opens on > 20% 5xx in 60s window with >= 5 sample requests
# ---------------------------------------------------------------------------


def test_breaker_opens_on_5xx_rate(breaker):
    """Sequence [success, 503, 503, 503, success, 503] -> 4/6 = 67% > 20%."""
    breaker.record_success()
    breaker.record_5xx_failure()
    breaker.record_5xx_failure()
    breaker.record_5xx_failure()
    breaker.record_success()
    breaker.record_5xx_failure()
    assert breaker.state is BreakerState.OPEN
    assert breaker.is_open() is True
    assert breaker.allow_request() is False


def test_breaker_does_not_open_below_min_requests(breaker):
    """Even all-5xx but only 4 requests stays CLOSED (sample size guard)."""
    for _ in range(4):
        breaker.record_5xx_failure()
    assert breaker.state is BreakerState.CLOSED


def test_breaker_does_not_open_at_or_below_threshold(breaker):
    """20% rate (1/5) is NOT > 20% — must remain CLOSED. 2/5 = 40% trips it."""
    breaker.record_5xx_failure()
    for _ in range(4):
        breaker.record_success()
    assert breaker.state is BreakerState.CLOSED
    # Add one more failure → 2/6 = 33% > 20% → OPEN.
    breaker.record_5xx_failure()
    assert breaker.state is BreakerState.OPEN


def test_breaker_window_prunes_old_events(breaker, fake_clock):
    """5xx events older than 60s drop out of the rolling window."""
    breaker.record_5xx_failure()
    breaker.record_5xx_failure()
    fake_clock.advance(61.0)
    # Now seed 5 successes; the old failures are out of window.
    for _ in range(5):
        breaker.record_success()
    assert breaker.state is BreakerState.CLOSED


def test_breaker_treats_timeout_as_5xx_for_breaker(breaker):
    """Timeouts also count toward the breaker per VAL-M3-086 wording."""
    breaker.record_success()
    breaker.record_timeout()
    breaker.record_timeout()
    breaker.record_timeout()
    breaker.record_success()
    breaker.record_timeout()
    assert breaker.state is BreakerState.OPEN


# ---------------------------------------------------------------------------
# VAL-M3-086: 429 does NOT count toward the 5xx breaker
# ---------------------------------------------------------------------------


def test_429_does_not_open_breaker(breaker):
    """Ten consecutive 429 responses must NOT open the breaker."""
    for _ in range(10):
        breaker.record_429()
    assert breaker.state is BreakerState.CLOSED
    # Ensure 429s also don't poison the failure counter when mixed in:
    # 5 successes interspersed with 10 429s is still 0 failures / 5 successes.
    for _ in range(5):
        breaker.record_success()
    assert breaker.state is BreakerState.CLOSED


# ---------------------------------------------------------------------------
# VAL-M3-065: auto-close after 5 minutes; probe success → CLOSED;
#             probe failure → OPEN
# ---------------------------------------------------------------------------


def test_auto_close_half_open_probe(breaker, fake_clock):
    """5min+1s after OPEN, state transitions to HALF_OPEN; success → CLOSED."""
    breaker.force_open()
    assert breaker.state is BreakerState.OPEN
    # Advance just under 5 minutes — still OPEN.
    fake_clock.advance(299.0)
    assert breaker.state is BreakerState.OPEN
    # Cross the 5-minute boundary → HALF_OPEN on next state read.
    fake_clock.advance(2.0)  # 301 total
    assert breaker.state is BreakerState.HALF_OPEN
    # A probe call's success closes the breaker.
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED


def test_failed_probe_reopens(breaker, fake_clock):
    """HALF_OPEN + probe failure → OPEN for another 5 minutes."""
    breaker.force_open()
    fake_clock.advance(301.0)
    assert breaker.state is BreakerState.HALF_OPEN
    breaker.record_5xx_failure()
    assert breaker.state is BreakerState.OPEN
    # Still OPEN until ANOTHER 5 minutes elapse from the probe-failure.
    fake_clock.advance(299.0)
    assert breaker.state is BreakerState.OPEN
    fake_clock.advance(2.0)
    assert breaker.state is BreakerState.HALF_OPEN


def test_half_open_allows_request_for_probe(breaker, fake_clock):
    """allow_request() is True in HALF_OPEN so the probe call can fire."""
    breaker.force_open()
    fake_clock.advance(301.0)
    assert breaker.state is BreakerState.HALF_OPEN
    assert breaker.allow_request() is True


# ---------------------------------------------------------------------------
# VAL-M3-067: in-process state — no DB persistence; restart resets
# ---------------------------------------------------------------------------


def test_breaker_state_is_in_process_only(tmp_path):
    """No 'breaker' / 'circuit' table should exist in the project schema."""
    from biotech_sniper import db as project_db
    db_path = tmp_path / "alpha.db"
    conn = project_db.connect(db_path)
    try:
        project_db.run_migrations(conn)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND (name LIKE 'breaker%' OR name LIKE 'circuit%')"
        ).fetchall()
    finally:
        conn.close()
    assert rows == []


def test_breaker_singleton_resets_on_module_reset(fake_clock):
    """A fresh breaker instance always starts CLOSED — restart semantics."""
    b1 = PerplexityBreaker(clock=fake_clock)
    b1.force_open()
    assert b1.state is BreakerState.OPEN
    b2 = PerplexityBreaker(clock=fake_clock)
    assert b2.state is BreakerState.CLOSED


# ---------------------------------------------------------------------------
# VAL-M3-095: 429 with Retry-After header is honored as min sleep
# ---------------------------------------------------------------------------


def test_parse_retry_after_seconds_int():
    assert parse_retry_after("3") == pytest.approx(3.0)
    assert parse_retry_after("0") == pytest.approx(0.0)
    assert parse_retry_after(5) == pytest.approx(5.0)
    assert parse_retry_after("  7  ") == pytest.approx(7.0)


def test_parse_retry_after_invalid_returns_none():
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("not-a-number-or-date") is None


def test_parse_retry_after_negative_clamped_to_zero():
    assert parse_retry_after("-1") == pytest.approx(0.0)


def test_parse_retry_after_http_date_parses_to_nonnegative():
    # Future HTTP date → positive seconds; past date → 0.
    assert parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT") > 0
    assert parse_retry_after("Wed, 21 Oct 1990 07:28:00 GMT") == pytest.approx(0.0)


def test_perplexity_client_honors_retry_after_header(monkeypatch):
    """A 429 with Retry-After: 3 makes the client's next sleep >= 3.0s.

    Verified by patching ``time.sleep`` inside the module to capture
    the requested duration and short-circuiting the actual sleep.
    """
    from biotech_sniper.llm import perplexity_client as pc

    sleep_calls: list[float] = []

    def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(float(seconds))

    monkeypatch.setattr(pc.time, "sleep", _fake_sleep)
    # Force deterministic jitter so we can assert tight bounds.
    monkeypatch.setattr(pc.random, "uniform", lambda a, b: 1.0)

    class _Resp:
        def __init__(self, status_code, *, headers=None, body=None):
            self.status_code = status_code
            self.headers = headers or {}
            self._body = body or {}
            self.text = json.dumps(self._body)

        def json(self):
            return self._body

    success_body = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "probability": 0.9,
                            "label": "material",
                            "direction": "bullish",
                            "rationale": "ok",
                            "citations": [],
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    responses = iter(
        [
            _Resp(429, headers={"Retry-After": "3"}),
            _Resp(200, body=success_body),
        ]
    )

    class _Session:
        def post(self, *_a, **_kw):
            return next(responses)

    client = pc.PerplexityClient(
        api_key="pplx-test" + "-sentinel-" + "xyz",
        session=_Session(),
        backoff_base=0.5,
        max_retries=3,
        db_path=None,  # falls back to default but the cost ledger swallows errors
    )
    # Avoid touching the real ledger DB.
    monkeypatch.setattr(
        client, "_record_cost_ledger_row", lambda **_kw: None
    )

    out = client.score_candidate({"ticker": "TESTX"})
    assert out["label"] == "material"
    # First sleep was for the Retry-After: 3 — must be at least 3.0s.
    assert sleep_calls, "expected at least one sleep between 429 and success"
    assert sleep_calls[0] >= 3.0


# ---------------------------------------------------------------------------
# Integration: PerplexityClient feeds outcomes into the breaker
# ---------------------------------------------------------------------------


def _make_perplexity_client(monkeypatch, responses):
    from biotech_sniper.llm import perplexity_client as pc

    monkeypatch.setattr(pc.time, "sleep", lambda *_: None)
    monkeypatch.setattr(pc.random, "uniform", lambda a, b: 1.0)
    iter_resp = iter(responses)

    class _Session:
        def post(self, *_a, **_kw):
            return next(iter_resp)

    client = pc.PerplexityClient(
        api_key="pplx-test" + "-sentinel-" + "abc",
        session=_Session(),
        backoff_base=0.0,
        max_retries=3,
    )
    monkeypatch.setattr(client, "_record_cost_ledger_row", lambda **_kw: None)
    return client


class _Resp:
    def __init__(self, status_code, *, headers=None, body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body) if isinstance(self._body, dict) else str(self._body)

    def json(self):
        return self._body


_SUCCESS_BODY = {
    "choices": [
        {
            "message": {
                "content": json.dumps(
                    {
                        "probability": 0.9,
                        "label": "material",
                        "direction": "bullish",
                        "rationale": "ok",
                        "citations": [],
                    }
                )
            }
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


def test_perplexity_client_records_success_to_breaker(monkeypatch):
    client = _make_perplexity_client(monkeypatch, [_Resp(200, body=_SUCCESS_BODY)])
    b = breaker_module.get_breaker()
    assert b.state is BreakerState.CLOSED
    client.score_candidate({"ticker": "TESTX"})
    # A success keeps state CLOSED but contributes to the rolling window.
    assert b.state is BreakerState.CLOSED


def test_perplexity_client_records_5xx_to_breaker(monkeypatch):
    """Six 5xx responses (4 retries × 2 calls) trip the breaker."""
    # Each call retries 4 times on 5xx (initial + 3); 2 calls = 8 events.
    responses = [_Resp(503) for _ in range(8)]
    client = _make_perplexity_client(monkeypatch, responses)
    b = breaker_module.get_breaker()

    # First call raises after exhausting retries; the breaker records 4 5xx.
    from biotech_sniper.llm.perplexity_client import PerplexityTransportError
    with pytest.raises(PerplexityTransportError):
        client.score_candidate({"ticker": "TESTX"})
    # Second call adds 4 more 5xx — total 8/8 = 100% > 20%.
    with pytest.raises(PerplexityTransportError):
        client.score_candidate({"ticker": "TESTX"})
    assert b.state is BreakerState.OPEN


def test_perplexity_client_429_does_not_count_toward_breaker(monkeypatch):
    """10 retries-worth of 429 must not open the breaker."""
    # 3 calls × 4 attempts each = 12 429s; mix in a final 200 to terminate.
    responses = []
    for _ in range(3):
        for _ in range(4):  # initial + 3 retries
            responses.append(_Resp(429, headers={"Retry-After": "0"}))
    client = _make_perplexity_client(monkeypatch, responses)
    b = breaker_module.get_breaker()

    from biotech_sniper.llm.perplexity_client import PerplexityRateLimitError
    for _ in range(3):
        with pytest.raises(PerplexityRateLimitError):
            client.score_candidate({"ticker": "TESTX"})
    assert b.state is BreakerState.CLOSED


# ---------------------------------------------------------------------------
# VAL-M3-064 + VAL-M3-066: ensemble degrades to 3-leg when breaker OPEN
# ---------------------------------------------------------------------------


def _build_v10_db(db_path):
    from biotech_sniper import db as project_db
    from biotech_sniper.migrations.runner import run as run_v10

    conn = project_db.connect(db_path)
    try:
        project_db.run_migrations(conn)
    finally:
        conn.close()
    # f-misc-09: track the active CURRENT_VERSION so the helper
    # stays forward-compatible with future schema bumps.
    run_v10(db_path, project_db.CURRENT_VERSION, take_backup_first=False)

    conn = project_db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO news_events ("
            "ticker, source, title, url, published_at, ingested_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                "TESTX",
                "rss",
                "TESTX positive readout",
                "https://example.com/x",
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
                "dedup-breaker-001",
            ),
        )
        cand_id = conn.execute("SELECT MAX(id) FROM candidate_events").fetchone()[0]
        conn.commit()
        return cand_id
    finally:
        conn.close()


def _make_unanimous_provider(direction: str = "bullish", probability: float = 0.85):
    def _call(candidate, *, name=None):  # noqa: ARG001
        return {
            "label": "material",
            "probability": probability,
            "direction": direction,
            "rationale": "stub",
            "citations": [{"url": "https://example.com", "title": "ex"}],
            "latency_ms": 5,
            "cost_usd": 0.001,
        }
    return _call


def test_open_breaker_degrades_to_3_leg_no_perplexity(tmp_path, caplog):
    """With breaker OPEN, score_candidate_event runs only 3 providers
    (xai/anthropic/gemini). Perplexity is NOT in per_provider_results,
    NOT in ensemble_scores_event, and NO paper_orders rows result.
    """
    from biotech_sniper.llm.ensemble import score_candidate_event

    db_path = tmp_path / "alpha.db"
    cand_id = _build_v10_db(db_path)

    breaker_module.get_breaker().force_open()

    providers = {
        "xai": _make_unanimous_provider(),
        "anthropic": _make_unanimous_provider(),
        "gemini": _make_unanimous_provider(),
        "perplexity": _make_unanimous_provider(),  # should NOT be invoked
    }

    perplexity_called = {"n": 0}
    original_perp = providers["perplexity"]

    def _tracking_perp(candidate, *, name=None):
        perplexity_called["n"] += 1
        return original_perp(candidate, name=name)

    providers["perplexity"] = _tracking_perp

    with caplog.at_level("INFO"):
        result = score_candidate_event(
            {"id": cand_id, "ticker": "TESTX", "matched_keywords": "phase_iii"},
            run_id="run-breaker-1",
            db_path=db_path,
            providers=providers,
        )

    assert perplexity_called["n"] == 0, "perplexity must not be invoked when breaker is OPEN"
    assert len(result.per_provider_results) == 3
    assert {r.provider for r in result.per_provider_results} == {"xai", "anthropic", "gemini"}

    # Persisted scores: 3 rows, no perplexity.
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT provider FROM ensemble_scores_event WHERE candidate_event_id=?",
            (cand_id,),
        ).fetchall()
        providers_seen = sorted(r[0] for r in rows)
        assert providers_seen == ["anthropic", "gemini", "xai"]
        # No paper_orders rows — score-only mode.
        po_count = conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE event='news_event_entry'"
        ).fetchone()[0]
        assert po_count == 0
    finally:
        conn.close()

    # Audit / log line surfaces breaker_open mode.
    log_text = " ".join(rec.getMessage() for rec in caplog.records)
    assert "breaker_open" in log_text or "breaker open" in log_text.lower()


def test_open_breaker_persists_3_leg_scores_score_only(tmp_path):
    """During OPEN, ensemble_scores_event has exactly 3 rows per candidate."""
    from biotech_sniper.llm.ensemble import score_candidate_event

    db_path = tmp_path / "alpha.db"
    cand_id = _build_v10_db(db_path)

    breaker_module.get_breaker().force_open()

    providers = {
        "xai": _make_unanimous_provider(),
        "anthropic": _make_unanimous_provider(),
        "gemini": _make_unanimous_provider(),
    }
    score_candidate_event(
        {"id": cand_id, "ticker": "TESTX"},
        run_id="run-score-only-1",
        db_path=db_path,
        providers=providers,
    )

    conn = sqlite3.connect(str(db_path))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM ensemble_scores_event WHERE candidate_event_id=?",
            (cand_id,),
        ).fetchone()[0]
        assert n == 3
        n_perp = conn.execute(
            "SELECT COUNT(*) FROM ensemble_scores_event "
            "WHERE candidate_event_id=? AND provider='perplexity'",
            (cand_id,),
        ).fetchone()[0]
        assert n_perp == 0
    finally:
        conn.close()


def test_closed_breaker_runs_full_4_leg(tmp_path):
    """Sanity: breaker CLOSED → 4 providers fire as before."""
    from biotech_sniper.llm.ensemble import score_candidate_event

    db_path = tmp_path / "alpha.db"
    cand_id = _build_v10_db(db_path)

    # Singleton already CLOSED via autouse fixture.
    providers = {
        "xai": _make_unanimous_provider(),
        "anthropic": _make_unanimous_provider(),
        "gemini": _make_unanimous_provider(),
        "perplexity": _make_unanimous_provider(),
    }
    result = score_candidate_event(
        {"id": cand_id, "ticker": "TESTX"},
        run_id="run-closed-1",
        db_path=db_path,
        providers=providers,
    )
    assert len(result.per_provider_results) == 4
    assert {r.provider for r in result.per_provider_results} == {
        "xai", "anthropic", "gemini", "perplexity",
    }


def test_module_singleton_is_used_by_default():
    """get_breaker() returns the same instance across calls."""
    b1 = breaker_module.get_breaker()
    b2 = breaker_module.get_breaker()
    assert b1 is b2


def test_record_5xx_failure_module_helper(monkeypatch):
    """Module-level helpers route to the singleton."""
    breaker_module.record_5xx_failure()
    breaker_module.record_5xx_failure()
    breaker_module.record_5xx_failure()
    breaker_module.record_5xx_failure()
    breaker_module.record_5xx_failure()
    breaker_module.record_5xx_failure()
    # 6/6 = 100% > 20% with min_requests=5 met.
    assert breaker_module.get_breaker().state is BreakerState.OPEN
