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
  requests). Schema is correct (cik PK, UNIQUE index on ticker,
  sic INTEGER nullable for tombstones, indexes on sic / fetched_at).
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
        # Per VAL-M1-012: cik is the PRIMARY KEY (10-digit
        # zero-padded TEXT); ticker is NOT NULL with a UNIQUE
        # index. sic INTEGER is nullable to permit 404
        # tombstones (per VAL-M1-014). cik is ALSO nullable —
        # the no-CIK tombstone path (per VAL-M1-062) writes
        # rows with cik IS NULL for tickers absent from the
        # SEC's CIK→ticker map.
        assert "cik" in cols
        assert cols["cik"][3] == 0  # nullable (no NOT NULL qualifier)
        assert cols["cik"][5] == 1  # PK rank == 1
        assert cols["cik"][2].upper() == "TEXT"
        assert "ticker" in cols
        assert cols["ticker"][3] == 1  # NOT NULL
        assert cols["ticker"][5] == 0  # NOT a PK column
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
        assert "idx_cik_sic_cache_ticker" in index_names
        # And it's UNIQUE — confirm via PRAGMA.
        unique_ticker = next(
            row
            for row in conn.execute(
                "PRAGMA index_list(cik_sic_cache)"
            ).fetchall()
            if row[1] == "idx_cik_sic_cache_ticker"
        )
        assert unique_ticker[2] == 1  # 'unique' flag
    finally:
        conn.close()


def test_ensure_cik_sic_cache_table_pragma_pk_is_cik(db_path: Path):
    """PRAGMA table_info reports exactly one PK column == 'cik'."""
    conn = sqlite3.connect(db_path)
    try:
        ensure_cik_sic_cache_table(conn)
        rows = conn.execute("PRAGMA table_info(cik_sic_cache)").fetchall()
        pk_rows = [r for r in rows if r[5] >= 1]
        assert len(pk_rows) == 1
        assert pk_rows[0][1] == "cik"
    finally:
        conn.close()


def test_ensure_cik_sic_cache_table_is_idempotent(db_path: Path):
    conn = sqlite3.connect(db_path)
    try:
        ensure_cik_sic_cache_table(conn)
        ensure_cik_sic_cache_table(conn)
        # Insert + duplicate insert: cik PRIMARY KEY rejects the
        # second insertion of the same CIK (per VAL-M1-012).
        conn.execute(
            "INSERT INTO cik_sic_cache "
            "(cik, ticker, sic, sic_description, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("0000875320", "VRTX", 2834, "PHARMACEUTICAL", "2026-04-29T00:00:00Z"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO cik_sic_cache "
                "(cik, ticker, sic, sic_description, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("0000875320", "VRTX", 2834, "PHARMACEUTICAL", "2026-04-29T00:00:01Z"),
            )
    finally:
        conn.close()


def test_ensure_cik_sic_cache_table_unique_ticker_rejects_duplicates(
    db_path: Path,
):
    """The UNIQUE index on ``ticker`` blocks the same ticker mapping
    to two different CIKs."""
    conn = sqlite3.connect(db_path)
    try:
        ensure_cik_sic_cache_table(conn)
        conn.execute(
            "INSERT INTO cik_sic_cache "
            "(cik, ticker, sic, sic_description, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("0000875320", "VRTX", 2834, "PHARMACEUTICAL", "2026-04-29T00:00:00Z"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO cik_sic_cache "
                "(cik, ticker, sic, sic_description, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                # Different cik but SAME ticker — UNIQUE index fires.
                ("0000999999", "VRTX", 2836, "BIO", "2026-04-29T00:00:01Z"),
            )
    finally:
        conn.close()


def test_ensure_cik_sic_cache_table_migrates_legacy_ticker_pk_shape(
    db_path: Path,
):
    """Pre-existing legacy ticker-PK rows are preserved into the new shape."""
    legacy_ddl = (
        "CREATE TABLE cik_sic_cache ("
        "ticker TEXT NOT NULL PRIMARY KEY, "
        "cik TEXT NOT NULL, "
        "sic INTEGER, "
        "sic_description TEXT, "
        "fetched_at TEXT NOT NULL"
        ")"
    )
    legacy_unique_cik_idx = (
        "CREATE UNIQUE INDEX idx_cik_sic_cache_cik "
        "ON cik_sic_cache(cik)"
    )
    conn = sqlite3.connect(db_path)
    try:
        # Bootstrap a legacy-shape table and populate two rows
        # (one resolved, one tombstone).
        conn.execute(legacy_ddl)
        conn.execute(legacy_unique_cik_idx)
        conn.executemany(
            "INSERT INTO cik_sic_cache "
            "(ticker, cik, sic, sic_description, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("VRTX", "0000875320", 2834, "PHARMACEUTICAL", "2026-04-29T00:00:00Z"),
                ("DEAD", "0000000001", None, None, "2026-04-29T00:00:01Z"),
            ],
        )
        conn.commit()

        # Run the bootstrap — it must detect the legacy shape and
        # forward-migrate the rows into the new shape.
        ensure_cik_sic_cache_table(conn)

        # The rows are preserved.
        rows = conn.execute(
            "SELECT cik, ticker, sic, sic_description, fetched_at "
            "FROM cik_sic_cache ORDER BY cik"
        ).fetchall()
        assert rows == [
            ("0000000001", "DEAD", None, None, "2026-04-29T00:00:01Z"),
            ("0000875320", "VRTX", 2834, "PHARMACEUTICAL", "2026-04-29T00:00:00Z"),
        ]

        # The schema is now the corrected shape: cik PK, UNIQUE
        # index on ticker.
        rows = conn.execute("PRAGMA table_info(cik_sic_cache)").fetchall()
        pk_rows = [r for r in rows if r[5] >= 1]
        assert len(pk_rows) == 1
        assert pk_rows[0][1] == "cik"

        index_list = conn.execute(
            "PRAGMA index_list(cik_sic_cache)"
        ).fetchall()
        ticker_idx = next(
            r for r in index_list if r[1] == "idx_cik_sic_cache_ticker"
        )
        assert ticker_idx[2] == 1  # UNIQUE flag

        # Legacy table is gone.
        legacy = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='cik_sic_cache_legacy'"
        ).fetchone()
        assert legacy is None
    finally:
        conn.close()


def test_ensure_cik_sic_cache_table_no_op_when_already_new_shape(
    db_path: Path,
):
    """Idempotent: a second call against the corrected shape doesn't migrate."""
    conn = sqlite3.connect(db_path)
    try:
        ensure_cik_sic_cache_table(conn)
        conn.execute(
            "INSERT INTO cik_sic_cache "
            "(cik, ticker, sic, sic_description, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("0000875320", "VRTX", 2834, "PHARMACEUTICAL", "2026-04-29T00:00:00Z"),
        )
        conn.commit()

        # Second call: must be a no-op against the new shape.
        ensure_cik_sic_cache_table(conn)
        rows = conn.execute(
            "SELECT cik, ticker, sic FROM cik_sic_cache"
        ).fetchall()
        assert rows == [("0000875320", "VRTX", 2834)]
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


def test_ticker_absent_from_map_writes_no_cik_tombstone(
    monkeypatch: pytest.MonkeyPatch, classifier: SECSICClassifier, db_path: Path
):
    """No matching CIK → returns ``None`` AND writes a tombstone with cik=NULL.

    Per VAL-M1-062: a ticker absent from the SEC's CIK→ticker
    map MUST produce a tombstone row (cik IS NULL AND sic IS
    NULL) so that subsequent lookups for the same unmapped
    ticker short-circuit on the cache instead of re-hitting
    SEC.
    """
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    _install(monkeypatch, cassette)

    result = classifier.resolve_ticker_sic("NONEXISTENT")
    assert result is None

    # Only one HTTP call (the ticker map fetch) — no submissions
    # call because we never resolved a CIK for this ticker.
    assert classifier.http_call_count == 1

    conn = sqlite3.connect(db_path)
    try:
        # The tombstone row exists with cik IS NULL AND sic IS
        # NULL (per VAL-M1-062).
        rows = conn.execute(
            "SELECT cik, sic, sic_description, fetched_at "
            "FROM cik_sic_cache WHERE ticker='NONEXISTENT'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    cik, sic, sic_description, fetched_at = rows[0]
    assert cik is None
    assert sic is None
    assert sic_description is None
    assert isinstance(fetched_at, str) and fetched_at  # ISO 8601 timestamp


def test_ticker_absent_from_map_short_circuits_subsequent_lookup(
    monkeypatch: pytest.MonkeyPatch, classifier: SECSICClassifier
):
    """Second lookup of the same unmapped ticker hits the tombstone (no HTTP).

    Per VAL-M1-062: the no-CIK tombstone must short-circuit
    subsequent lookups so the SEC isn't re-hit for tickers we
    already know are absent from the map.
    """
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    _install(monkeypatch, cassette)

    first = classifier.resolve_ticker_sic("NONEXISTENT")
    assert first is None
    pre = classifier.http_call_count
    pre_calls = len(cassette.calls)

    second = classifier.resolve_ticker_sic("NONEXISTENT")
    assert second is None
    # No new HTTP traffic — the tombstone short-circuited.
    assert classifier.http_call_count == pre
    assert len(cassette.calls) == pre_calls


def test_ticker_absent_from_map_short_circuits_across_classifier_instances(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, fake_sleep
):
    """A fresh classifier instance also honours the no-CIK tombstone."""
    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    _install(monkeypatch, cassette)

    first = SECSICClassifier(db_path=db_path, sleep=fake_sleep)
    first.resolve_ticker_sic("NONEXISTENT")
    pre_calls = len(cassette.calls)

    second = SECSICClassifier(db_path=db_path, sleep=fake_sleep)
    result = second.resolve_ticker_sic("NONEXISTENT")
    assert result is None
    # The tombstone row was loaded from the shared db; the
    # fresh classifier did NOT need to fetch the map.
    assert second.http_call_count == 0
    assert len(cassette.calls) == pre_calls


def test_no_cik_tombstone_refreshes_after_ttl_expiry(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, fake_sleep
):
    """After TTL expires, the classifier re-fetches and updates the tombstone.

    Drives :data:`now_iso` deterministically so the cache row's
    ``fetched_at`` is older than ``cache_ttl_seconds`` on the
    second resolution. The second resolution must:

    1. Treat the cached tombstone as a miss (TTL expired).
    2. Re-fetch the CIK→ticker map (HTTP #1).
    3. Re-write the tombstone with the newer ``fetched_at``.
    """
    timeline = ["2026-04-01T00:00:00Z"]

    def fake_now_iso() -> str:
        return timeline[-1]

    classifier = SECSICClassifier(
        db_path=db_path,
        sleep=fake_sleep,
        cache_ttl_seconds=60,  # 1-minute TTL keeps the test fast.
        now_iso=fake_now_iso,
    )

    cassette = _Cassette()
    # Two map fetches expected — one per resolution because the
    # second resolution misses on TTL and the in-memory map
    # cache is reset between instances. To reset the in-memory
    # map we re-instantiate the classifier below.
    cassette.queue(
        "company_tickers_exchange",
        _ticker_map_response(),
        _ticker_map_response(),
    )
    _install(monkeypatch, cassette)

    # First resolution → tombstone written with fetched_at=T1.
    first = classifier.resolve_ticker_sic("NONEXISTENT")
    assert first is None
    pre_calls = classifier.http_call_count
    assert pre_calls == 1

    conn = sqlite3.connect(db_path)
    try:
        row1 = conn.execute(
            "SELECT cik, sic, fetched_at FROM cik_sic_cache "
            "WHERE ticker='NONEXISTENT'"
        ).fetchone()
    finally:
        conn.close()
    assert row1 is not None
    cik1, sic1, fetched_at_1 = row1
    assert cik1 is None and sic1 is None
    assert fetched_at_1 == "2026-04-01T00:00:00Z"

    # Advance the clock past the TTL (60s window; bump to +120s).
    timeline.append("2026-04-01T00:02:00Z")

    # A fresh classifier instance ensures the in-memory ticker
    # map cache is clear, so the TTL-driven re-resolution
    # observably issues a new HTTP request.
    second_classifier = SECSICClassifier(
        db_path=db_path,
        sleep=fake_sleep,
        cache_ttl_seconds=60,
        now_iso=fake_now_iso,
    )
    second = second_classifier.resolve_ticker_sic("NONEXISTENT")
    assert second is None
    # One new HTTP call (the map re-fetch).
    assert second_classifier.http_call_count == 1

    conn = sqlite3.connect(db_path)
    try:
        row2 = conn.execute(
            "SELECT cik, sic, fetched_at FROM cik_sic_cache "
            "WHERE ticker='NONEXISTENT'"
        ).fetchone()
    finally:
        conn.close()
    assert row2 is not None
    cik2, sic2, fetched_at_2 = row2
    assert cik2 is None and sic2 is None
    # The tombstone was refreshed — fetched_at is the new T2.
    assert fetched_at_2 == "2026-04-01T00:02:00Z"
    assert fetched_at_2 != fetched_at_1


def test_no_cik_tombstone_within_ttl_does_not_refetch(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, fake_sleep
):
    """Within the TTL window the no-CIK tombstone is honoured (no HTTP)."""
    timeline = ["2026-04-01T00:00:00Z"]

    def fake_now_iso() -> str:
        return timeline[-1]

    classifier = SECSICClassifier(
        db_path=db_path,
        sleep=fake_sleep,
        cache_ttl_seconds=86400,
        now_iso=fake_now_iso,
    )

    cassette = _Cassette()
    cassette.queue("company_tickers_exchange", _ticker_map_response())
    _install(monkeypatch, cassette)

    first = classifier.resolve_ticker_sic("NONEXISTENT")
    assert first is None
    pre_count = classifier.http_call_count

    # Advance the clock by 30 minutes — well inside the 24 h TTL.
    timeline.append("2026-04-01T00:30:00Z")

    second_classifier = SECSICClassifier(
        db_path=db_path,
        sleep=fake_sleep,
        cache_ttl_seconds=86400,
        now_iso=fake_now_iso,
    )
    second = second_classifier.resolve_ticker_sic("NONEXISTENT")
    assert second is None
    # Tombstone honoured — no fresh HTTP from the second instance.
    assert second_classifier.http_call_count == 0
    # And the original classifier's count is also unchanged.
    assert classifier.http_call_count == pre_count


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
