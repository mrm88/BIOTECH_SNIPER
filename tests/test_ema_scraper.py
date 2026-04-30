"""Behavioural tests for :mod:`biotech_sniper.calendar.ema`.

Coverage matches the f-m1-05 contract (VAL-M1-027..030) and the
feature description for ema.py.

* Schema correctness — table shape + composite UNIQUE expression
  index over ``(ticker_or_sponsor, product, COALESCE(meeting_date,''),
  COALESCE(opinion_date,''))`` (VAL-M1-027).
* CHECK constraint enforces meeting_date IS NOT NULL OR
  opinion_date IS NOT NULL.
* Live HTML parse: synthetic CHMP-shaped HTML produces ≥ 1 row,
  all dates ISO-8601, multi-product rows for the same ticker
  persist as distinct rows (VAL-M1-028).
* Composite-key idempotency: re-running yields zero net new rows,
  ``fetched_at`` is refreshed (VAL-M1-029).
* 404 / 5xx / timeout from an explicitly-overridden upstream
  triggers the last-good fallback path: scraper exits non-zero,
  table is unchanged from pre-run state; transaction rolls back so
  no partial commit (VAL-M1-030).
* User-Agent: every outbound HTTP request carries the descriptive
  project UA matching ``^BiotechSniper/[0-9].* contact: .*@.*$``.
* Seed fallback: default URL fetch failure silently falls back to
  the bundled seed JSON; ``--no-fallback`` suppresses the fallback.
* CLI: ``--help``, ``--refresh``, ``--use-seed``, ``--dry-run
  --emit-stats`` exit codes and stdout shapes.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import requests

from biotech_sniper.calendar import ema as ema_module
from biotech_sniper.calendar.ema import (
    DEFAULT_EMA_SOURCE_URL,
    DEFAULT_USER_AGENT,
    EXIT_OK,
    EXIT_PARSE_ERROR,
    EXIT_UPSTREAM_UNAVAILABLE,
    EMAParseError,
    ParsedEMA,
    UpstreamUnavailable,
    default_seed_path,
    ensure_ema_calendar_table,
    fetch_html_bytes,
    is_iso_date,
    main,
    normalize_date,
    parse_ema_html,
    parse_seed_json,
    scrape_ema_calendar,
    write_rows,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ema"
SAMPLE_HTML_PATH = FIXTURE_DIR / "sample_meeting.html"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal :class:`requests.Response` stand-in."""

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


class _RecordingSession:
    """Records every ``get`` call so tests can inspect headers / URLs."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._script: list[Any] = []

    def enqueue(self, *items: Any) -> None:
        self._script.extend(items)

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, "kwargs": kwargs})
        if not self._script:
            raise AssertionError(f"session script exhausted; url={url!r}")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(url, **kwargs)
        return item


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "alpha_sniper.db"


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip any host-leaked EMA env overrides before each test."""
    monkeypatch.delenv("EMA_SOURCE_URL", raising=False)


@pytest.fixture
def html_body() -> bytes:
    return SAMPLE_HTML_PATH.read_bytes()


@pytest.fixture
def today() -> datetime.date:
    """Deterministic 'today' so date-band assertions are stable."""
    return datetime.date(2026, 4, 29)


@pytest.fixture
def now(today: datetime.date) -> datetime.datetime:
    return datetime.datetime.combine(
        today, datetime.time(12, 0, 0), tzinfo=datetime.timezone.utc
    )


# ---------------------------------------------------------------------------
# normalize_date
# ---------------------------------------------------------------------------


class TestNormalizeDate:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2026-05-22", "2026-05-22"),
            ("2026-12-31", "2026-12-31"),
        ],
    )
    def test_iso_passthrough(self, raw: str, expected: str) -> None:
        assert normalize_date(raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("19-22 May 2026", "2026-05-22"),  # range → last day
            ("19\u201322 May 2026", "2026-05-22"),  # en-dash range
            ("23-26 June 2026", "2026-06-26"),
            ("21-24 July 2026", "2026-07-24"),
        ],
    )
    def test_date_range_returns_last_day(
        self, raw: str, expected: str
    ) -> None:
        assert normalize_date(raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("22 May 2026", "2026-05-22"),
            ("22 May 26", "2026-05-22"),
            ("1 June 2026", "2026-06-01"),
            ("22 Sep 2026", "2026-09-22"),
        ],
    )
    def test_eu_day_month_year(self, raw: str, expected: str) -> None:
        assert normalize_date(raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("May 22, 2026", "2026-05-22"),
            ("June 26 2026", "2026-06-26"),
        ],
    )
    def test_us_month_day_year(self, raw: str, expected: str) -> None:
        assert normalize_date(raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("22/05/2026", "2026-05-22"),  # EU-style preferred
            ("22-05-2026", "2026-05-22"),
        ],
    )
    def test_numeric_eu(self, raw: str, expected: str) -> None:
        assert normalize_date(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            None,
            "TBD",
            "approval expected",
            "garbage",
            "2026-13-40",
        ],
    )
    def test_unparseable_returns_none(self, raw: str | None) -> None:
        assert normalize_date(raw) is None


class TestIsIsoDate:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2026-05-22", True),
            ("2026-13-01", False),
            ("2026-02-30", False),
            ("2026/05/22", False),
            ("19 May 2026", False),
            ("", False),
            (None, False),
        ],
    )
    def test_strict_validation(self, raw: Any, expected: bool) -> None:
        assert is_iso_date(raw) is expected


# ---------------------------------------------------------------------------
# parse_ema_html
# ---------------------------------------------------------------------------


class TestParseEmaHtml:
    def test_yields_min_one_row_on_sample(self, html_body: bytes) -> None:
        rows = parse_ema_html(html_body)
        assert len(rows) >= 1, rows

    def test_all_dates_iso_when_present(self, html_body: bytes) -> None:
        rows = parse_ema_html(html_body)
        for row in rows:
            if row.meeting_date is not None:
                assert is_iso_date(row.meeting_date), row
            if row.opinion_date is not None:
                assert is_iso_date(row.opinion_date), row
            assert (
                row.meeting_date is not None or row.opinion_date is not None
            ), row

    def test_multi_product_per_ticker(self, html_body: bytes) -> None:
        rows = parse_ema_html(html_body)
        vrtx = [r for r in rows if r.ticker_or_sponsor == "VRTX"]
        assert len(vrtx) >= 2, [r.product for r in vrtx]
        products = {r.product for r in vrtx}
        assert len(products) == len(vrtx), "product names must be unique"

    def test_meeting_only_row(self, html_body: bytes) -> None:
        rows = parse_ema_html(html_body)
        regn = next(
            (r for r in rows if r.ticker_or_sponsor == "REGN"), None
        )
        assert regn is not None
        assert regn.meeting_date == "2026-05-22"
        assert regn.opinion_date is None

    def test_opinion_only_row(self, html_body: bytes) -> None:
        rows = parse_ema_html(html_body)
        mrk = next(
            (r for r in rows if r.ticker_or_sponsor == "MRK"), None
        )
        assert mrk is not None
        assert mrk.opinion_date == "2026-06-26"
        assert mrk.meeting_date is None

    def test_unparseable_date_row_dropped(self, html_body: bytes) -> None:
        rows = parse_ema_html(html_body)
        fakes = [r for r in rows if r.ticker_or_sponsor == "FAKE"]
        assert fakes == [], fakes

    def test_no_table_raises_parse_error(self) -> None:
        body = b"<html><body><p>No tables here.</p></body></html>"
        with pytest.raises(EMAParseError):
            parse_ema_html(body)


# ---------------------------------------------------------------------------
# parse_seed_json
# ---------------------------------------------------------------------------


class TestParseSeedJson:
    def test_bundled_seed_parses_min_one_row(self) -> None:
        body = default_seed_path().read_bytes()
        rows, source_url = parse_seed_json(body)
        assert len(rows) >= 1
        assert source_url.startswith("https://")
        # Per VAL-M1-028, source_url must point at www.ema.europa.eu.
        assert "www.ema.europa.eu" in source_url

    def test_seed_dates_are_iso_when_present(self) -> None:
        body = default_seed_path().read_bytes()
        rows, _ = parse_seed_json(body)
        for row in rows:
            if row.meeting_date is not None:
                assert is_iso_date(row.meeting_date), row
            if row.opinion_date is not None:
                assert is_iso_date(row.opinion_date), row
            assert (
                row.meeting_date is not None or row.opinion_date is not None
            ), row

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(EMAParseError):
            parse_seed_json(b"not-json")

    def test_missing_entries_raises(self) -> None:
        with pytest.raises(EMAParseError):
            parse_seed_json(json.dumps({"foo": "bar"}).encode())


# ---------------------------------------------------------------------------
# fetch_html_bytes / User-Agent
# ---------------------------------------------------------------------------


class TestFetchHtmlBytes:
    def test_user_agent_is_descriptive(self) -> None:
        """Every outbound request carries the project descriptive UA."""
        import re

        session = _RecordingSession()
        session.enqueue(_FakeResponse(status_code=200, body=b"<html></html>"))
        fetch_html_bytes(
            DEFAULT_EMA_SOURCE_URL,
            session=session,
        )
        assert session.calls, "expected one outbound request"
        ua = session.calls[0]["kwargs"]["headers"]["User-Agent"]
        assert ua == DEFAULT_USER_AGENT, ua
        assert re.match(
            r"^BiotechSniper/[0-9][^@]*contact:[^@]*@.+$", ua
        ), ua
        assert "python-requests" not in ua.lower()

    def test_404_raises_upstream_unavailable(self) -> None:
        session = _RecordingSession()
        session.enqueue(_FakeResponse(status_code=404, body=b""))
        with pytest.raises(UpstreamUnavailable):
            fetch_html_bytes("https://example.com/x", session=session)

    def test_5xx_raises_upstream_unavailable(self) -> None:
        session = _RecordingSession()
        session.enqueue(_FakeResponse(status_code=503, body=b""))
        with pytest.raises(UpstreamUnavailable):
            fetch_html_bytes("https://example.com/x", session=session)

    def test_request_exception_wraps_to_upstream_unavailable(self) -> None:
        session = _RecordingSession()
        session.enqueue(requests.ConnectionError("timeout"))
        with pytest.raises(UpstreamUnavailable):
            fetch_html_bytes("https://example.com/x", session=session)

    def test_empty_body_raises(self) -> None:
        session = _RecordingSession()
        session.enqueue(_FakeResponse(status_code=200, body=b""))
        with pytest.raises(UpstreamUnavailable):
            fetch_html_bytes("https://example.com/x", session=session)


# ---------------------------------------------------------------------------
# Schema + write_rows
# ---------------------------------------------------------------------------


class TestSchema:
    def test_table_schema_has_required_columns(self, db_path: Path) -> None:
        conn = sqlite3.connect(db_path)
        try:
            ensure_ema_calendar_table(conn)
            conn.commit()
            row = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='ema_calendar'"
            ).fetchone()
            assert row is not None
            sql = row[0]
            assert "ticker_or_sponsor" in sql
            assert "product" in sql
            assert "meeting_date" in sql
            assert "opinion_date" in sql
            assert "source_url" in sql
            assert "fetched_at" in sql
            # CHECK clause must enforce that at least one date is non-null.
            assert "CHECK" in sql.upper()
            assert "meeting_date IS NOT NULL OR opinion_date IS NOT NULL" in sql
        finally:
            conn.close()

    def test_unique_index_on_composite_key(self, db_path: Path) -> None:
        conn = sqlite3.connect(db_path)
        try:
            ensure_ema_calendar_table(conn)
            conn.commit()
            indexes = {
                r[0]: r[1]
                for r in conn.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='ema_calendar'"
                ).fetchall()
            }
            assert "idx_ema_calendar_unique" in indexes
            sql = (indexes["idx_ema_calendar_unique"] or "").upper()
            assert "UNIQUE" in sql
            assert "TICKER_OR_SPONSOR" in sql
            assert "PRODUCT" in sql
            assert "COALESCE(MEETING_DATE" in sql
            assert "COALESCE(OPINION_DATE" in sql
        finally:
            conn.close()

    def test_check_constraint_rejects_both_null_dates(
        self, db_path: Path
    ) -> None:
        conn = sqlite3.connect(db_path)
        try:
            ensure_ema_calendar_table(conn)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO ema_calendar "
                    "(ticker_or_sponsor, product, meeting_date, "
                    "opinion_date, source_url, fetched_at) "
                    "VALUES ('VRTX', 'X', NULL, NULL, "
                    "'https://www.ema.europa.eu/x', "
                    "'2026-04-29T00:00:00Z')"
                )
        finally:
            conn.close()

    def test_composite_unique_rejects_duplicate(
        self, db_path: Path
    ) -> None:
        conn = sqlite3.connect(db_path)
        try:
            ensure_ema_calendar_table(conn)
            conn.execute(
                "INSERT INTO ema_calendar "
                "(ticker_or_sponsor, product, meeting_date, "
                "opinion_date, source_url, fetched_at) "
                "VALUES ('VRTX', 'Suzetrigine', '2026-05-22', "
                "'2026-05-22', 'https://x', "
                "'2026-04-29T00:00:00Z')"
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO ema_calendar "
                    "(ticker_or_sponsor, product, meeting_date, "
                    "opinion_date, source_url, fetched_at) "
                    "VALUES ('VRTX', 'Suzetrigine', '2026-05-22', "
                    "'2026-05-22', 'https://y', "
                    "'2026-04-30T00:00:00Z')"
                )
        finally:
            conn.close()

    def test_unique_index_treats_null_dates_as_empty(
        self, db_path: Path
    ) -> None:
        """COALESCE(...,'') means NULL dates are stable in unique index."""
        conn = sqlite3.connect(db_path)
        try:
            ensure_ema_calendar_table(conn)
            conn.execute(
                "INSERT INTO ema_calendar "
                "(ticker_or_sponsor, product, meeting_date, "
                "opinion_date, source_url, fetched_at) "
                "VALUES ('REGN', 'Lynozyfic', '2026-05-22', NULL, "
                "'https://x', '2026-04-29T00:00:00Z')"
            )
            with pytest.raises(sqlite3.IntegrityError):
                # Same ticker_or_sponsor, product, meeting_date, NULL
                # opinion_date — should still collide via COALESCE.
                conn.execute(
                    "INSERT INTO ema_calendar "
                    "(ticker_or_sponsor, product, meeting_date, "
                    "opinion_date, source_url, fetched_at) "
                    "VALUES ('REGN', 'Lynozyfic', '2026-05-22', NULL, "
                    "'https://y', '2026-04-30T00:00:00Z')"
                )
        finally:
            conn.close()


class TestWriteRows:
    def test_writes_rows_and_skips_invalid_dates(
        self, db_path: Path, today: datetime.date
    ) -> None:
        conn = sqlite3.connect(db_path)
        try:
            ensure_ema_calendar_table(conn)
            rows = [
                ParsedEMA(
                    ticker_or_sponsor="VRTX",
                    product="Suzetrigine",
                    meeting_date="2026-05-22",
                    opinion_date="2026-05-22",
                ),
                # invalid meeting date dropped to NULL but opinion
                # remains so the row still persists.
                ParsedEMA(
                    ticker_or_sponsor="BMRN",
                    product="Roctavian",
                    meeting_date="not-a-date",
                    opinion_date="2026-06-26",
                ),
                # both dates out-of-band — skipped under no_dates.
                ParsedEMA(
                    ticker_or_sponsor="OLD",
                    product="Ancient",
                    meeting_date="2000-01-01",
                    opinion_date=None,
                ),
                ParsedEMA(
                    ticker_or_sponsor="FAR",
                    product="Distant",
                    meeting_date=None,
                    opinion_date="2099-12-31",
                ),
            ]
            written, invalid, band, no_dates = write_rows(
                conn,
                rows,
                source_url="https://www.ema.europa.eu/x",
                fetched_at="2026-04-29T00:00:00Z",
                today=today,
            )
            conn.commit()
            assert written == 2
            assert invalid == 1
            assert band == 2
            assert no_dates == 2
            count = conn.execute(
                "SELECT COUNT(*) FROM ema_calendar"
            ).fetchone()[0]
            assert count == 2
        finally:
            conn.close()

    def test_upsert_on_conflict_refreshes_fetched_at(
        self, db_path: Path, today: datetime.date
    ) -> None:
        conn = sqlite3.connect(db_path)
        try:
            ensure_ema_calendar_table(conn)
            row = ParsedEMA(
                ticker_or_sponsor="VRTX",
                product="Suzetrigine",
                meeting_date="2026-05-22",
                opinion_date="2026-05-22",
                sponsor="Vertex Pharmaceuticals",
            )
            write_rows(
                conn,
                [row],
                source_url="https://first.example/",
                fetched_at="2026-04-29T00:00:00Z",
                today=today,
            )
            conn.commit()
            write_rows(
                conn,
                [row],
                source_url="https://second.example/",
                fetched_at="2026-04-30T00:00:00Z",
                today=today,
            )
            conn.commit()
            count = conn.execute(
                "SELECT COUNT(*) FROM ema_calendar"
            ).fetchone()[0]
            assert count == 1
            persisted = conn.execute(
                "SELECT source_url, fetched_at FROM ema_calendar"
            ).fetchone()
            assert persisted[0] == "https://second.example/"
            assert persisted[1] == "2026-04-30T00:00:00Z"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# scrape_ema_calendar — orchestration
# ---------------------------------------------------------------------------


class TestScrapeEmaCalendar:
    def test_html_path_writes_rows(
        self, db_path: Path, isolated_env, now: datetime.datetime
    ) -> None:
        result = scrape_ema_calendar(
            db_path=db_path,
            html_path=SAMPLE_HTML_PATH,
            now=now,
        )
        assert result.rows_written >= 1
        assert not result.used_seed_fallback

        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM ema_calendar"
            ).fetchone()[0]
            assert count == result.rows_written
            # All persisted dates ISO-8601.
            bad_meeting = conn.execute(
                "SELECT COUNT(*) FROM ema_calendar "
                "WHERE meeting_date IS NOT NULL AND meeting_date "
                "NOT GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'"
            ).fetchone()[0]
            assert bad_meeting == 0
            bad_opinion = conn.execute(
                "SELECT COUNT(*) FROM ema_calendar "
                "WHERE opinion_date IS NOT NULL AND opinion_date "
                "NOT GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'"
            ).fetchone()[0]
            assert bad_opinion == 0
        finally:
            conn.close()

    def test_back_to_back_yields_zero_net_new_rows(
        self, db_path: Path, isolated_env, now: datetime.datetime
    ) -> None:
        scrape_ema_calendar(
            db_path=db_path, html_path=SAMPLE_HTML_PATH, now=now
        )
        conn = sqlite3.connect(db_path)
        before = conn.execute(
            "SELECT COUNT(*) FROM ema_calendar"
        ).fetchone()[0]
        conn.close()
        scrape_ema_calendar(
            db_path=db_path, html_path=SAMPLE_HTML_PATH, now=now
        )
        conn = sqlite3.connect(db_path)
        try:
            after = conn.execute(
                "SELECT COUNT(*) FROM ema_calendar"
            ).fetchone()[0]
            assert before == after
            dups = conn.execute(
                "SELECT ticker_or_sponsor, product, "
                "COALESCE(meeting_date,''), "
                "COALESCE(opinion_date,''), COUNT(*) "
                "FROM ema_calendar "
                "GROUP BY ticker_or_sponsor, product, "
                "COALESCE(meeting_date,''), COALESCE(opinion_date,'') "
                "HAVING COUNT(*)>1"
            ).fetchall()
            assert dups == []
        finally:
            conn.close()

    def test_multi_product_per_ticker_distinct_rows(
        self, db_path: Path, isolated_env, now: datetime.datetime
    ) -> None:
        scrape_ema_calendar(
            db_path=db_path, html_path=SAMPLE_HTML_PATH, now=now
        )
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT product, meeting_date FROM ema_calendar "
                "WHERE ticker_or_sponsor='VRTX' ORDER BY meeting_date"
            ).fetchall()
            assert len(rows) >= 2
            keys = {(r[0], r[1]) for r in rows}
            assert len(keys) == len(rows)
        finally:
            conn.close()

    def test_use_seed_writes_min_one_row(
        self, db_path: Path, isolated_env, now: datetime.datetime
    ) -> None:
        result = scrape_ema_calendar(
            db_path=db_path, use_seed=True, now=now
        )
        assert result.rows_written >= 1
        assert result.used_seed_fallback
        # Per VAL-M1-028, source_url must reference www.ema.europa.eu.
        conn = sqlite3.connect(db_path)
        try:
            bad = conn.execute(
                "SELECT COUNT(*) FROM ema_calendar "
                "WHERE source_url NOT LIKE 'https://www.ema.europa.eu/%'"
            ).fetchone()[0]
            assert bad == 0
        finally:
            conn.close()

    def test_overridden_404_propagates_and_preserves_rows(
        self,
        db_path: Path,
        isolated_env,
        now: datetime.datetime,
    ) -> None:
        # Seed an existing row so we can confirm last-good fallback.
        scrape_ema_calendar(
            db_path=db_path, use_seed=True, now=now
        )
        conn = sqlite3.connect(db_path)
        before = conn.execute(
            "SELECT COUNT(*) FROM ema_calendar"
        ).fetchone()[0]
        conn.close()
        assert before >= 1

        # Inject 404 on the operator-overridden URL.
        session = _RecordingSession()
        session.enqueue(_FakeResponse(status_code=404, body=b""))
        with pytest.raises(UpstreamUnavailable):
            scrape_ema_calendar(
                db_path=db_path,
                source_url="https://www.ema.europa.eu/__no_such_path__",
                session=session,
                now=now,
            )

        conn = sqlite3.connect(db_path)
        try:
            after = conn.execute(
                "SELECT COUNT(*) FROM ema_calendar"
            ).fetchone()[0]
            assert before == after, "last-good fallback failed"
        finally:
            conn.close()

    def test_overridden_via_env_var_propagates(
        self,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        now: datetime.datetime,
    ) -> None:
        monkeypatch.setenv(
            "EMA_SOURCE_URL", "https://www.ema.europa.eu/__oops__"
        )
        session = _RecordingSession()
        session.enqueue(_FakeResponse(status_code=503, body=b""))
        with pytest.raises(UpstreamUnavailable):
            scrape_ema_calendar(
                db_path=db_path, session=session, now=now
            )

    def test_default_url_failure_falls_back_to_seed(
        self,
        db_path: Path,
        isolated_env,
        now: datetime.datetime,
    ) -> None:
        session = _RecordingSession()
        session.enqueue(_FakeResponse(status_code=503, body=b""))
        result = scrape_ema_calendar(
            db_path=db_path,
            session=session,
            now=now,
        )
        assert result.used_seed_fallback
        assert result.rows_written >= 1

    def test_no_fallback_flag_makes_default_failure_fatal(
        self,
        db_path: Path,
        isolated_env,
        now: datetime.datetime,
    ) -> None:
        session = _RecordingSession()
        session.enqueue(_FakeResponse(status_code=503, body=b""))
        with pytest.raises(UpstreamUnavailable):
            scrape_ema_calendar(
                db_path=db_path,
                session=session,
                no_fallback=True,
                now=now,
            )

    def test_dry_run_does_not_write(
        self, db_path: Path, isolated_env, now: datetime.datetime
    ) -> None:
        result = scrape_ema_calendar(
            db_path=db_path,
            html_path=SAMPLE_HTML_PATH,
            dry_run=True,
            now=now,
        )
        assert result.parsed_rows >= 1
        assert result.rows_written == 0
        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM ema_calendar"
            ).fetchone()[0]
            assert count == 0
        finally:
            conn.close()

    def test_no_partial_commit_on_timeout(
        self,
        db_path: Path,
        isolated_env,
        now: datetime.datetime,
    ) -> None:
        """A timeout from operator-overridden URL leaves the DB unchanged.

        Mirrors VAL-M1-030: 404/timeout aborts without partial commit;
        any prior rows remain byte-identical post-failure.
        """
        # Seed prior rows.
        scrape_ema_calendar(
            db_path=db_path, use_seed=True, now=now
        )
        conn = sqlite3.connect(db_path)
        before_count = conn.execute(
            "SELECT COUNT(*) FROM ema_calendar"
        ).fetchone()[0]
        before_max_fetched = conn.execute(
            "SELECT MAX(fetched_at) FROM ema_calendar"
        ).fetchone()[0]
        conn.close()
        assert before_count >= 1

        session = _RecordingSession()
        session.enqueue(requests.Timeout("synthetic timeout"))
        with pytest.raises(UpstreamUnavailable):
            scrape_ema_calendar(
                db_path=db_path,
                source_url="https://www.ema.europa.eu/__no_such__",
                session=session,
                now=now,
            )

        # PRAGMA integrity + row-count + fetched_at must match.
        conn = sqlite3.connect(db_path)
        try:
            after_count = conn.execute(
                "SELECT COUNT(*) FROM ema_calendar"
            ).fetchone()[0]
            after_max_fetched = conn.execute(
                "SELECT MAX(fetched_at) FROM ema_calendar"
            ).fetchone()[0]
            integrity = conn.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
            assert after_count == before_count
            assert after_max_fetched == before_max_fetched
            assert integrity == "ok"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


class TestCli:
    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "--refresh" in out
        assert "--db" in out
        assert "--source" in out
        assert "--use-seed" in out
        assert "--dry-run" in out

    def test_use_seed_writes_min_one_row(
        self, tmp_path: Path, isolated_env
    ) -> None:
        db = tmp_path / "alpha_sniper.db"
        rc = main(["--refresh", "--use-seed", "--db", str(db)])
        assert rc == EXIT_OK
        conn = sqlite3.connect(db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM ema_calendar"
            ).fetchone()[0]
            assert count >= 1
        finally:
            conn.close()

    def test_html_path_refresh(
        self, tmp_path: Path, isolated_env
    ) -> None:
        db = tmp_path / "alpha_sniper.db"
        rc = main(
            [
                "--refresh",
                "--html-path",
                str(SAMPLE_HTML_PATH),
                "--db",
                str(db),
            ]
        )
        assert rc == EXIT_OK
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                "SELECT ticker_or_sponsor, product, meeting_date, "
                "opinion_date FROM ema_calendar "
                "ORDER BY ticker_or_sponsor, product"
            ).fetchall()
            assert len(rows) >= 1
        finally:
            conn.close()

    def test_emit_stats_prints_json(
        self,
        tmp_path: Path,
        isolated_env,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        db = tmp_path / "alpha_sniper.db"
        rc = main(
            [
                "--refresh",
                "--use-seed",
                "--emit-stats",
                "--db",
                str(db),
            ]
        )
        assert rc == EXIT_OK
        out = capsys.readouterr().out.strip()
        payload = json.loads(out)
        assert payload["rows_written"] >= 1
        assert payload["used_seed_fallback"] is True

    def test_overridden_source_404_returns_exit_2(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = tmp_path / "alpha_sniper.db"

        def _fail_get(url: str, **kwargs: Any) -> Any:
            raise requests.ConnectionError("synthetic")

        monkeypatch.setattr(
            ema_module.requests, "get", _fail_get, raising=True
        )
        rc = main(
            [
                "--refresh",
                "--source",
                "https://www.ema.europa.eu/__no_such__",
                "--db",
                str(db),
            ]
        )
        assert rc == EXIT_UPSTREAM_UNAVAILABLE

    def test_default_failure_with_no_fallback_returns_exit_2(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        isolated_env,
    ) -> None:
        db = tmp_path / "alpha_sniper.db"

        def _fail_get(url: str, **kwargs: Any) -> Any:
            raise requests.ConnectionError("synthetic")

        monkeypatch.setattr(
            ema_module.requests, "get", _fail_get, raising=True
        )
        rc = main(
            ["--refresh", "--no-fallback", "--db", str(db)]
        )
        assert rc == EXIT_UPSTREAM_UNAVAILABLE

    def test_default_failure_seed_fallback_returns_exit_0(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        isolated_env,
    ) -> None:
        db = tmp_path / "alpha_sniper.db"

        def _fail_get(url: str, **kwargs: Any) -> Any:
            raise requests.ConnectionError("synthetic")

        monkeypatch.setattr(
            ema_module.requests, "get", _fail_get, raising=True
        )
        rc = main(["--refresh", "--db", str(db)])
        assert rc == EXIT_OK
        conn = sqlite3.connect(db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM ema_calendar"
            ).fetchone()[0]
            assert count >= 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Module-level smoke tests
# ---------------------------------------------------------------------------


def test_smoke_module_import() -> None:
    """Importing the module does not perform IO or raise."""
    import importlib

    importlib.reload(ema_module)


def test_default_user_agent_matches_validator_regex() -> None:
    """UA matches the cassette-inspection intent."""
    import re

    assert re.match(
        r"^BiotechSniper/[0-9][^@]*contact:[^@]*@.+$", DEFAULT_USER_AGENT
    ), DEFAULT_USER_AGENT
    assert "python-requests" not in DEFAULT_USER_AGENT.lower()


def test_default_url_targets_ema_europa_eu_host() -> None:
    """VAL-M1-028 requires source_url host www.ema.europa.eu."""
    assert DEFAULT_EMA_SOURCE_URL.startswith(
        "https://www.ema.europa.eu/"
    ), DEFAULT_EMA_SOURCE_URL
