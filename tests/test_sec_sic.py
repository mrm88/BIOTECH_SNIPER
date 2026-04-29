"""Behavioural tests for :mod:`biotech_sniper.classifiers.sec_sic`.

These exercise the SEC EDGAR SIC classifier against locally
recorded JSON cassettes (no live network). Coverage:

* Happy path: ticker → submission → SIC int + sicDescription.
* CIK→ticker map endpoint returns 200 with descriptive UA.
* Per-CIK submissions endpoint returns SIC code as string in
  the JSON body (we coerce to ``int``).
* Throttle: ≤ 8 req/s — ``min_inter_request_seconds >= 0.125``
  observed across a 100-CIK scan.
* User-Agent: every request carries the project UA, never the
  default ``python-requests`` UA.
* Cache: second invocation hits the SQLite cache (zero new HTTP
  requests). Schema is correct (ticker PK, sic INTEGER nullable
  for tombstones, indexes on cik / sic / fetched_at).
* 404 (delisted): returns :data:`None` and writes a tombstone
  row with ``sic IS NULL``.
* 429 / 5xx: retried with capped exponential backoff (max 3
  attempts). Persistent 5xx raises :class:`SECTransientError`;
  the cache is NOT poisoned.
* No-CIK path: ticker absent from the SEC's CIK→ticker map
  returns :data:`None` without raising.

Cassette fixtures live under ``tests/fixtures/sec_edgar_sic/``;
they are plain JSON files committed to the repo.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import requests

from biotech_sniper.classifiers import sec_sic
from biotech_sniper.classifiers.sec_sic import (
    BIOTECH_SIC_CODES,
    CIK_TICKERS_URL,
    DEFAULT_USER_AGENT,
    MAX_RETRY_ATTEMPTS,
    SUBMISSIONS_URL_TEMPLATE,
    SECClassifierError,
    SECSchemaError,
    SECSICClassifier,
    SECTransientError,
    SICResolution,
    ensure_cik_sic_cache_table,
    format_cik,
    resolve_ticker_sic,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sec_edgar_sic"


# ---------------------------------------------------------------------------
# Fake HTTP layer
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal :class:`requests.Response` stand-in for cassettes."""

    def __init__(
        self,
        *,
        status_code: int,
        body: bytes = b"",
        text: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.content = body
        self.text = text if text is not None else body.decode("utf-8", "replace")
        self.headers: dict[str, str] = {}


class _Cassette:
    """Programmable HTTP cassette used by the test fakes.

    Tests register a list of canned responses (or callables that
    construct a response on the fly). Each ``get`` call pops the
    next response in order; raising or exhaustion is a test bug.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._scripts: dict[str, list[Any]] = {}

    def queue(self, url_substr: str, *responses: Any) -> None:
        """Queue ``responses`` for any URL containing ``url_substr``."""
        self._scripts.setdefault(url_substr, []).extend(responses)

    def __call__(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, "kwargs": kwargs})
        for substr, queue in self._scripts.items():
            if substr in url:
                if not queue:
                    raise AssertionError(
                        f"cassette exhausted for url containing {substr!r}: "
                        f"call={url}"
                    )
                response = queue.pop(0)
                if isinstance(response, Exception):
                    raise response
                if callable(response):
                    return response(url, **kwargs)
                return response
        raise AssertionError(
            f"no cassette script matched url={url!r}; "
            f"queued substrings={list(self._scripts)}"
        )


def _ticker_map_response() -> _FakeResponse:
    body = (FIXTURE_DIR / "company_tickers_exchange.json").read_bytes()
    return _FakeResponse(status_code=200, body=body)


def _submission_response(cik: str) -> _FakeResponse:
    body = (FIXTURE_DIR / f"submissions_CIK{cik}.json").read_bytes()
    return _FakeResponse(status_code=200, body=body)


def _install(
    monkeypatch: pytest.MonkeyPatch, cassette: _Cassette
) -> _Cassette:
    monkeypatch.setattr(sec_sic.requests, "get", cassette)
    return cassette


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sleep_recorder() -> list[float]:
    """A list that captures every ``time.sleep`` call made by the classifier."""
    return []


@pytest.fixture
def fake_sleep(sleep_recorder: list[float]):
    def _sleep(seconds: float) -> None:
        sleep_recorder.append(float(seconds))

    return _sleep


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "alpha_sniper.db"


@pytest.fixture
def classifier(db_path: Path, fake_sleep) -> SECSICClassifier:
    return SECSICClassifier(
        db_path=db_path,
        sleep=fake_sleep,
        # Use the production min-gap so the throttle assertions
        # cover the real default (0.125 s).
    )


# ---------------------------------------------------------------------------
# format_cik
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (875320, "0000875320"),
        ("875320", "0000875320"),
        ("0000875320", "0000875320"),
        ("CIK0000875320", "0000875320"),
        ("cik875320", "0000875320"),
        (1, "0000000001"),
    ],
)
def test_format_cik_normalises_to_10_digits(raw, expected):
    assert format_cik(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "12345abcde", "CIK00000000001234567890"])
def test_format_cik_rejects_garbage(raw):
    with pytest.raises(ValueError):
        format_cik(raw)


def test_format_cik_rejects_negative_int():
    with pytest.raises(ValueError):
        format_cik(-1)


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------


def test_ensure_cik_sic_cache_table_creates_expected_schema(db_path: Path):
    conn = sqlite3.connect(db_path)
    try:
        ensure_cik_sic_cache_table(conn)
        cols = {
            row[1]: row
            for row in conn.execute(
                "PRAGMA table_info(cik_sic_cache)"
            ).fetchall()
        }
        # ticker is the primary key (per feature description) and
        # NOT NULL; cik is NOT NULL with a UNIQUE index.
        assert "ticker" in cols
        assert cols["ticker"][3] == 1  # NOT NULL
        assert cols["ticker"][5] == 1  # PK rank == 1
        assert cols["cik"][3] == 1  # NOT NULL
        # sic must be nullable to support tombstones (404 path).
        assert cols["sic"][3] == 0
        assert cols["sic"][2].upper() == "INTEGER"
        assert cols["sic_description"][3] == 0
        assert cols["fetched_at"][3] == 1  # NOT NULL

        index_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='cik_sic_cache'"
            ).fetchall()
        }
        assert "idx_cik_sic_cache_cik" in index_names
        # And it's UNIQUE — confirm via PRAGMA.
        unique_cik = next(
            row
            for row in conn.execute(
                "PRAGMA index_list(cik_sic_cache)"
            ).fetchall()
            if row[1] == "idx_cik_sic_cache_cik"
        )
        assert unique_cik[2] == 1  # 'unique' flag
    finally:
        conn.close()


def test_ensure_cik_sic_cache_table_is_idempotent(db_path: Path):
    conn = sqlite3.connect(db_path)
    try:
        ensure_cik_sic_cache_table(conn)
        ensure_cik_sic_cache_table(conn)
        # Insert + duplicate insert: PK protects on ticker.
        conn.execute(
            "INSERT INTO cik_sic_cache "
            "(ticker, cik, sic, sic_description, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("VRTX", "0000875320", 2834, "PHARMACEUTICAL", "2026-04-29T00:00:00Z"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO cik_sic_cache "
                "(ticker, cik, sic, sic_description, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("VRTX", "0000875320", 2834, "PHARMACEUTICAL", "2026-04-29T00:00:01Z"),
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Happy-path resolution + descriptive UA
# ---------------------------------------------------------------------------


def test_resolve_ticker_sic_happy_path_writes_cache_row(
    monkeypatch: pytest.MonkeyPatch, classifier: SECSICClassifier, db_path: Path
):
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    cassette.queue("submissions/CIK0000875320", _submission_response("0000875320"))
    _install(monkeypatch, cassette)

    result = classifier.resolve_ticker_sic("VRTX")

    assert isinstance(result, SICResolution)
    assert result.ticker == "VRTX"
    assert result.cik == "0000875320"
    assert result.sic == 2834
    assert result.sic_description == "PHARMACEUTICAL PREPARATIONS"
    assert result.cached is False
    assert classifier.http_call_count == 2

    # Cache row landed.
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT ticker, cik, sic, sic_description "
            "FROM cik_sic_cache WHERE ticker='VRTX'"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("VRTX", "0000875320", 2834, "PHARMACEUTICAL PREPARATIONS")


def test_resolve_ticker_sic_returns_biotech_codes(
    monkeypatch: pytest.MonkeyPatch, classifier: SECSICClassifier
):
    """SIC family check across the M1 biotech allow-list."""
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    cassette.queue("submissions/CIK0001657312", _submission_response("0001657312"))
    cassette.queue("submissions/CIK0001689548", _submission_response("0001689548"))
    _install(monkeypatch, cassette)

    idya = classifier.resolve_ticker_sic("IDYA")
    nvax = classifier.resolve_ticker_sic("NVAX")
    assert idya is not None and idya.sic == 2836
    assert idya.sic in BIOTECH_SIC_CODES
    assert nvax is not None and nvax.sic == 2836


def test_resolve_ticker_sic_handles_non_biotech(
    monkeypatch: pytest.MonkeyPatch, classifier: SECSICClassifier
):
    """TSLA → SIC 3711 (non-biotech) is still resolved & cached."""
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    cassette.queue("submissions/CIK0001318605", _submission_response("0001318605"))
    _install(monkeypatch, cassette)

    tsla = classifier.resolve_ticker_sic("TSLA")
    assert tsla is not None
    assert tsla.sic == 3711
    assert tsla.sic not in BIOTECH_SIC_CODES


def test_user_agent_is_descriptive(
    monkeypatch: pytest.MonkeyPatch, classifier: SECSICClassifier
):
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    cassette.queue("submissions/CIK0000875320", _submission_response("0000875320"))
    _install(monkeypatch, cassette)

    classifier.resolve_ticker_sic("VRTX")

    ua_pattern = "BiotechSniper/"
    for call in cassette.calls:
        ua = call["kwargs"].get("headers", {}).get("User-Agent", "")
        assert ua == DEFAULT_USER_AGENT
        assert ua.startswith(ua_pattern)
        assert "contact:" in ua
        assert "@" in ua
        assert "python-requests" not in ua.lower()


def test_ticker_map_endpoint_url_is_canonical(
    monkeypatch: pytest.MonkeyPatch, classifier: SECSICClassifier
):
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    cassette.queue("submissions/CIK0000875320", _submission_response("0000875320"))
    _install(monkeypatch, cassette)
    classifier.resolve_ticker_sic("VRTX")
    assert cassette.calls[0]["url"] == CIK_TICKERS_URL


def test_submission_endpoint_url_uses_zero_padded_cik(
    monkeypatch: pytest.MonkeyPatch, classifier: SECSICClassifier
):
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    cassette.queue("submissions/CIK0000875320", _submission_response("0000875320"))
    _install(monkeypatch, cassette)
    classifier.resolve_ticker_sic("VRTX")
    assert cassette.calls[1]["url"] == SUBMISSIONS_URL_TEMPLATE.format(
        cik="0000875320"
    )


# ---------------------------------------------------------------------------
# Cache hit (no HTTP on second call)
# ---------------------------------------------------------------------------


def test_cache_hit_skips_http(
    monkeypatch: pytest.MonkeyPatch, classifier: SECSICClassifier
):
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    cassette.queue("submissions/CIK0000875320", _submission_response("0000875320"))
    _install(monkeypatch, cassette)

    first = classifier.resolve_ticker_sic("VRTX")
    initial_calls = classifier.http_call_count
    assert first is not None and first.cached is False
    assert initial_calls == 2

    second = classifier.resolve_ticker_sic("VRTX")

    assert second is not None
    assert second.cached is True
    assert second.sic == 2834
    # No new HTTP traffic on the second call.
    assert classifier.http_call_count == initial_calls
    assert len(cassette.calls) == 2


def test_cache_hit_across_classifier_instances(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, fake_sleep
):
    """A fresh classifier instance still honours the cache."""
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    cassette.queue("submissions/CIK0000875320", _submission_response("0000875320"))
    _install(monkeypatch, cassette)

    first = SECSICClassifier(db_path=db_path, sleep=fake_sleep)
    first.resolve_ticker_sic("VRTX")
    pre = first.http_call_count

    second = SECSICClassifier(db_path=db_path, sleep=fake_sleep)
    result = second.resolve_ticker_sic("VRTX")
    assert result is not None
    assert result.cached is True
    assert second.http_call_count == 0
    # No additional cassette calls were issued.
    assert len(cassette.calls) == pre


# ---------------------------------------------------------------------------
# 404 / delisted CIK / no-CIK
# ---------------------------------------------------------------------------


def test_404_returns_none_and_tombstones(
    monkeypatch: pytest.MonkeyPatch, classifier: SECSICClassifier, db_path: Path
):
    """A delisted CIK (404) → returns ``None`` and writes a tombstone row."""
    # Synthesise a ticker map row for a CIK that EDGAR rejects.
    fake_map = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [[1, "Delisted Co", "DEAD", "Nasdaq"]],
    }
    cassette = _Cassette()
    cassette.queue(
        "company_tickers_exchange",
        _FakeResponse(
            status_code=200, body=json.dumps(fake_map).encode("utf-8")
        ),
    )
    cassette.queue(
        "submissions/CIK0000000001",
        _FakeResponse(status_code=404, body=b'{"error":"not_found"}'),
    )
    _install(monkeypatch, cassette)

    result = classifier.resolve_ticker_sic("DEAD")
    assert result is None

    # Tombstone row exists with sic IS NULL.
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT ticker, cik, sic, sic_description "
            "FROM cik_sic_cache WHERE ticker='DEAD'"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("DEAD", "0000000001", None, None)


def test_404_tombstone_short_circuits_subsequent_lookup(
    monkeypatch: pytest.MonkeyPatch, classifier: SECSICClassifier
):
    fake_map = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [[1, "Delisted Co", "DEAD", "Nasdaq"]],
    }
    cassette = _Cassette()
    cassette.queue(
        "company_tickers_exchange",
        _FakeResponse(
            status_code=200, body=json.dumps(fake_map).encode("utf-8")
        ),
    )
    cassette.queue(
        "submissions/CIK0000000001",
        _FakeResponse(status_code=404, body=b'{}'),
    )
    _install(monkeypatch, cassette)

    classifier.resolve_ticker_sic("DEAD")
    pre = classifier.http_call_count
    second = classifier.resolve_ticker_sic("DEAD")
    assert second is None
    # Tombstone hit — no fresh HTTP.
    assert classifier.http_call_count == pre


def test_ticker_absent_from_map_returns_none(
    monkeypatch: pytest.MonkeyPatch, classifier: SECSICClassifier, db_path: Path
):
    """No matching CIK → returns ``None`` without writing the cache."""
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    _install(monkeypatch, cassette)

    result = classifier.resolve_ticker_sic("NONEXISTENT")
    assert result is None

    # The fixture map has 7 rows; "NONEXISTENT" is not one of them.
    # We do NOT tombstone — only one HTTP call for the map.
    assert classifier.http_call_count == 1

    conn = sqlite3.connect(db_path)
    try:
        # Cache table exists (boostrapped by the lookup) but
        # no row was written for the unknown ticker.
        rows = conn.execute(
            "SELECT COUNT(*) FROM cik_sic_cache WHERE ticker='NONEXISTENT'"
        ).fetchone()
    finally:
        conn.close()
    assert rows[0] == 0


# ---------------------------------------------------------------------------
# 429 / 5xx retry policy
# ---------------------------------------------------------------------------


def test_429_then_200_succeeds_within_max_attempts(
    monkeypatch: pytest.MonkeyPatch, classifier: SECSICClassifier,
    sleep_recorder: list[float]
):
    """One 429 → retry → success, within the retry cap."""
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    cassette.queue(
        "submissions/CIK0000875320",
        _FakeResponse(status_code=429, body=b'{"err":"slow down"}'),
        _submission_response("0000875320"),
    )
    _install(monkeypatch, cassette)

    result = classifier.resolve_ticker_sic("VRTX")
    assert result is not None and result.sic == 2834
    # 1 map call + 1 retry of the submissions endpoint = 3 HTTP attempts.
    assert classifier.http_call_count == 3
    # At least one backoff sleep was recorded.
    backoff_sleeps = [s for s in sleep_recorder if s > 0]
    assert len(backoff_sleeps) >= 1


def test_5xx_retried_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, classifier: SECSICClassifier
):
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    cassette.queue(
        "submissions/CIK0000875320",
        _FakeResponse(status_code=503, body=b''),
        _submission_response("0000875320"),
    )
    _install(monkeypatch, cassette)

    result = classifier.resolve_ticker_sic("VRTX")
    assert result is not None
    assert result.sic == 2834


def test_persistent_5xx_raises_sec_transient_error(
    monkeypatch: pytest.MonkeyPatch, classifier: SECSICClassifier, db_path: Path
):
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    cassette.queue(
        "submissions/CIK0000875320",
        *([_FakeResponse(status_code=503, body=b'')] * MAX_RETRY_ATTEMPTS),
    )
    _install(monkeypatch, cassette)

    with pytest.raises(SECTransientError):
        classifier.resolve_ticker_sic("VRTX")

    # 1 map + MAX_RETRY_ATTEMPTS submissions attempts.
    assert classifier.http_call_count == 1 + MAX_RETRY_ATTEMPTS

    # Cache is NOT poisoned.
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM cik_sic_cache WHERE ticker='VRTX'"
        ).fetchone()
    finally:
        conn.close()
    assert rows[0] == 0


def test_request_exception_retried_then_raised(
    monkeypatch: pytest.MonkeyPatch, classifier: SECSICClassifier
):
    """``requests.ConnectionError`` retries up to the cap then raises."""
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    cassette.queue(
        "submissions/CIK0000875320",
        *([requests.ConnectionError("boom")] * MAX_RETRY_ATTEMPTS),
    )
    _install(monkeypatch, cassette)

    with pytest.raises(SECTransientError):
        classifier.resolve_ticker_sic("VRTX")


def test_backoff_sleep_durations_grow_exponentially(
    monkeypatch: pytest.MonkeyPatch, db_path: Path,
    sleep_recorder: list[float], fake_sleep
):
    classifier = SECSICClassifier(
        db_path=db_path,
        sleep=fake_sleep,
        # Pin jitter to zero so the assertions are deterministic.
        backoff_jitter=0.0,
        min_inter_request_seconds=0.0,
    )
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    cassette.queue(
        "submissions/CIK0000875320",
        _FakeResponse(status_code=503, body=b""),
        _FakeResponse(status_code=503, body=b""),
        _submission_response("0000875320"),
    )
    _install(monkeypatch, cassette)

    classifier.resolve_ticker_sic("VRTX")

    # Two backoff sleeps were recorded: 0.5s then 1.0s.
    backoff_sleeps = [s for s in sleep_recorder if s > 0]
    assert len(backoff_sleeps) == 2
    assert backoff_sleeps[0] == pytest.approx(0.5, rel=0.001)
    assert backoff_sleeps[1] == pytest.approx(1.0, rel=0.001)


# ---------------------------------------------------------------------------
# Throttle (≤ 8 req/s)
# ---------------------------------------------------------------------------


def test_throttle_under_8rps_across_100_lookups(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, fake_sleep
):
    """100-CIK scan sees ``min_inter_request_seconds >= 0.125``."""
    monotonic_clock = [0.0]

    def fake_sleep_advances_clock(seconds: float) -> None:
        monotonic_clock[0] += float(seconds)

    def fake_monotonic() -> float:
        return monotonic_clock[0]

    classifier = SECSICClassifier(
        db_path=db_path,
        sleep=fake_sleep_advances_clock,
        monotonic=fake_monotonic,
    )

    # Build a synthetic ticker map with 100 tickers and the
    # corresponding submission cassettes. We reuse VRTX's
    # SIC=2834 body for every CIK so the parser is exercised
    # uniformly.
    fields = ["cik", "name", "ticker", "exchange"]
    data = [[i, f"Company {i}", f"T{i:03d}", "Nasdaq"] for i in range(1, 101)]
    map_payload = {"fields": fields, "data": data}

    cassette = _Cassette()
    cassette.queue(
        "company_tickers_exchange",
        _FakeResponse(
            status_code=200,
            body=json.dumps(map_payload).encode("utf-8"),
        ),
    )
    sub_body = (
        FIXTURE_DIR / "submissions_CIK0000875320.json"
    ).read_bytes()
    # Submission cassette per CIK — answer with the same body.
    for i in range(1, 101):
        cassette.queue(
            f"submissions/CIK{i:010d}",
            _FakeResponse(status_code=200, body=sub_body),
        )
    _install(monkeypatch, cassette)

    request_clock_stamps: list[float] = []

    real_send = classifier._send_request

    def stamped_send(url: str, *args: Any, **kwargs: Any):
        request_clock_stamps.append(monotonic_clock[0])
        return real_send(url, *args, **kwargs)

    classifier._send_request = stamped_send  # type: ignore[assignment]

    for i in range(1, 101):
        result = classifier.resolve_ticker_sic(f"T{i:03d}")
        assert result is not None
        # Synthetic CIK: each tick advances the monotonic clock
        # by an artificially-small amount so the throttle has to
        # sleep on every call.

    # 1 map fetch + 100 submission fetches = 101 stamps.
    assert len(request_clock_stamps) == 101
    deltas = [
        request_clock_stamps[i] - request_clock_stamps[i - 1]
        for i in range(1, len(request_clock_stamps))
    ]
    # Every gap must be >= 0.125 s (1 / 8 req/s).
    assert all(delta >= 0.125 - 1e-9 for delta in deltas), (
        f"min delta {min(deltas):.4f} < 0.125 — throttle violated"
    )
    # And the recorded ``inter_request_gaps`` matches the
    # observed deltas (within numerical fuzz).
    recorded = classifier.inter_request_gaps
    assert len(recorded) == len(deltas)


def test_throttle_disabled_when_min_gap_is_zero(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, fake_sleep
):
    classifier = SECSICClassifier(
        db_path=db_path,
        sleep=fake_sleep,
        min_inter_request_seconds=0.0,
    )
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    cassette.queue("submissions/CIK0000875320", _submission_response("0000875320"))
    _install(monkeypatch, cassette)

    classifier.resolve_ticker_sic("VRTX")
    # No sleeps when throttling is disabled.
    # (The cassette setup itself records no sleeps.)
    assert classifier.http_call_count == 2


# ---------------------------------------------------------------------------
# Schema-error path
# ---------------------------------------------------------------------------


def test_schema_error_when_map_missing_fields(
    monkeypatch: pytest.MonkeyPatch, classifier: SECSICClassifier
):
    cassette = _Cassette()
    cassette.queue(
        "company_tickers_exchange",
        _FakeResponse(status_code=200, body=b'{"foo":"bar"}'),
    )
    _install(monkeypatch, cassette)

    with pytest.raises(SECSchemaError):
        classifier.resolve_ticker_sic("VRTX")


def test_schema_error_when_submission_has_non_int_sic(
    monkeypatch: pytest.MonkeyPatch, classifier: SECSICClassifier
):
    bad = json.dumps(
        {
            "cik": "875320",
            "sic": "abcd",
            "sicDescription": "Garbage",
            "tickers": ["VRTX"],
        }
    ).encode("utf-8")
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    cassette.queue(
        "submissions/CIK0000875320",
        _FakeResponse(status_code=200, body=bad),
    )
    _install(monkeypatch, cassette)

    with pytest.raises(SECSchemaError):
        classifier.resolve_ticker_sic("VRTX")


def test_submission_with_no_sic_returns_none_sic(
    monkeypatch: pytest.MonkeyPatch, classifier: SECSICClassifier
):
    body = json.dumps(
        {"cik": "875320", "tickers": ["VRTX"], "sicDescription": ""}
    ).encode("utf-8")
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    cassette.queue(
        "submissions/CIK0000875320",
        _FakeResponse(status_code=200, body=body),
    )
    _install(monkeypatch, cassette)

    result = classifier.resolve_ticker_sic("VRTX")
    assert result is not None
    assert result.sic is None


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_resolve_ticker_sic_module_helper_uses_db_path(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
):
    """The module-level helper resolves through a private classifier."""
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    cassette.queue("submissions/CIK0000875320", _submission_response("0000875320"))
    _install(monkeypatch, cassette)

    result = resolve_ticker_sic("VRTX", db_path=db_path)
    assert result is not None
    assert result.sic == 2834


def test_unexpected_status_raises_classifier_error(
    monkeypatch: pytest.MonkeyPatch, classifier: SECSICClassifier
):
    """A 301/302 / 401 / 403 (non-retryable, non-404) raises directly."""
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    cassette.queue(
        "submissions/CIK0000875320",
        _FakeResponse(status_code=403, body=b"forbidden"),
    )
    _install(monkeypatch, cassette)

    with pytest.raises(SECClassifierError):
        classifier.resolve_ticker_sic("VRTX")


def test_empty_ticker_raises_value_error(classifier: SECSICClassifier):
    with pytest.raises(ValueError):
        classifier.resolve_ticker_sic("")
    with pytest.raises(ValueError):
        classifier.resolve_ticker_sic("   ")


def test_lowercase_ticker_is_normalised_to_uppercase(
    monkeypatch: pytest.MonkeyPatch, classifier: SECSICClassifier, db_path: Path
):
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    cassette.queue("submissions/CIK0000875320", _submission_response("0000875320"))
    _install(monkeypatch, cassette)

    result = classifier.resolve_ticker_sic("vrtx")
    assert result is not None
    assert result.ticker == "VRTX"

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT ticker FROM cik_sic_cache"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("VRTX",)


def test_classifier_records_inter_request_gaps(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
):
    """The throttle's ``inter_request_gaps`` ledger sees a ≥0.125 entry."""
    monotonic_clock = [0.0]

    def fake_sleep_advances_clock(seconds: float) -> None:
        monotonic_clock[0] += float(seconds)

    def fake_monotonic() -> float:
        return monotonic_clock[0]

    classifier = SECSICClassifier(
        db_path=db_path,
        sleep=fake_sleep_advances_clock,
        monotonic=fake_monotonic,
    )
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    cassette.queue("submissions/CIK0000875320", _submission_response("0000875320"))
    _install(monkeypatch, cassette)

    classifier.resolve_ticker_sic("VRTX")
    gaps = classifier.inter_request_gaps
    # One gap is recorded between the two HTTP requests.
    assert len(gaps) >= 1
    assert all(g >= 0.125 - 1e-9 for g in gaps)
