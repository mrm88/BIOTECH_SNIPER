"""Behavioural tests for :mod:`biotech_sniper.universe.iwm_importer`.

These exercise the happy path, the iShares 4xx/5xx fallback, the
network-timeout (no partial-commit) path, and the user-agent +
table-bootstrap invariants. The schema-drift assertions live in
:mod:`tests.universe.test_iwm_importer_schema` per VAL-M1-055/056.

Cassette policy — byte-faithful CSV fixtures in lieu of vcrpy YAML
-------------------------------------------------------------------
The fixtures under ``tests/fixtures/cassettes/ishares/`` act as
VCR cassettes for the iShares ``www.ishares.com`` CSV endpoint:
every HTTP request is intercepted with ``monkeypatch`` and
answered from a committed CSV file, so the test suite never
touches the network. We deliberately use byte-faithful CSV
fixtures here instead of a vcrpy YAML cassette because:

* The endpoint is a plain-body GET with NO Authorization /
  cookie / API-key header — there is nothing for vcrpy's
  default redaction to protect, and no auth surface to record.
* BOM-byte fidelity matters for the parser (see
  ``iwm_with_bom.csv`` and the BOM-tolerance asserts).
  vcrpy round-trips bodies through YAML which can
  re-encode / re-quote bytes; a raw committed CSV preserves
  the exact 0xEF 0xBB 0xBF prefix and CRLF/LF line endings.
* Replay determinism is identical to vcrpy: same input bytes
  on every run, zero network egress under ``pytest -n 2``.

This is the documented exception in AGENTS.md "Tests" — for
plain-body GET endpoints with no auth headers and where byte
fidelity matters, byte-faithful committed fixtures are a
vcrpy-equivalent cassette. The cassette helper is named
``_install_csv_cassette`` to make the convention explicit.
The vcrpy YAML pattern remains the default for any provider
with auth headers (Perplexity, Alpaca, Claude, Gemini, xAI).
"""

from __future__ import annotations

import io
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
import requests

from biotech_sniper.universe import iwm_importer
from biotech_sniper.universe.iwm_importer import (
    DEFAULT_IWM_HOLDINGS_URL,
    DEFAULT_USER_AGENT,
    EXIT_OK,
    EXIT_SCHEMA_ERROR,
    EXIT_UPSTREAM_UNAVAILABLE,
    ImportResult,
    IWMSchemaError,
    UpstreamUnavailable,
    fetch_csv_bytes,
    import_iwm_holdings,
    main,
    parse_iwm_csv,
)


FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "cassettes" / "ishares"


# ---------------------------------------------------------------------------
# Cassette helpers (mock requests.get / session.get)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, *, status_code: int, body: bytes = b"") -> None:
        self.status_code = status_code
        self.content = body
        self.text = body.decode("utf-8", errors="replace")
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:  # pragma: no cover - unused
        if not (200 <= self.status_code < 300):
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _install_csv_cassette(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fixture: str | None = None,
    body: bytes | None = None,
    status_code: int = 200,
    raise_exc: Exception | None = None,
) -> dict[str, Any]:
    """Patch ``requests.get`` to answer with a CSV cassette fixture or an error.

    Acts as the byte-faithful CSV-cassette equivalent of vcrpy for
    the iShares plain-body GET endpoint (no auth headers, BOM-byte
    fidelity required). See module docstring for the full rationale.

    Returns a dict carrying the captured call args so individual
    tests can assert on User-Agent + URL + timeout values.
    """
    captured: dict[str, Any] = {"calls": []}

    def fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        captured["calls"].append({"url": url, "kwargs": kwargs})
        if raise_exc is not None:
            raise raise_exc
        if body is not None:
            payload = body
        elif fixture is not None:
            payload = (FIXTURE_DIR / fixture).read_bytes()
        else:
            payload = b""
        return _FakeResponse(status_code=status_code, body=payload)

    monkeypatch.setattr(iwm_importer.requests, "get", fake_get)
    return captured


# ---------------------------------------------------------------------------
# parse_iwm_csv — happy path / preamble skip
# ---------------------------------------------------------------------------


def test_parse_iwm_csv_happy_path_skips_preamble_and_keeps_equities():
    body = (FIXTURE_DIR / "iwm_happy.csv").read_bytes()
    rows, stats = parse_iwm_csv(body)

    tickers = [r.ticker for r in rows]
    # 5 equity rows in fixture; cash + derivative excluded.
    assert tickers == ["VRTX", "IDYA", "AAPL", "NVAX", "AXSM"]
    assert stats["equity_rows"] == 5
    assert stats["other_rows"] == 2  # USD cash + SWAP derivative
    assert stats["null_ticker_count"] == 0
    assert stats["null_weight_count"] == 0
    # Preamble rows skipped (8 metadata lines + 1 blank).
    assert stats["preamble_skipped"] >= 8
    # Sample row carries the parsed numeric weight.
    vrtx = rows[0]
    assert vrtx.weight == pytest.approx(1.50)
    assert vrtx.asset_class == "Equity"
    assert vrtx.name == "VERTEX PHARMACEUTICALS INC"
    assert vrtx.sector == "Health Care"


def test_parse_iwm_csv_preamble_strings_never_appear_as_tickers():
    body = (FIXTURE_DIR / "iwm_happy.csv").read_bytes()
    rows, _ = parse_iwm_csv(body)
    tickers = {r.ticker for r in rows}
    forbidden = {
        "Fund Holdings as of",
        "Inception Date",
        "Shares Outstanding",
        "Stock",
        "Bond",
        "Cash",
        "Other",
    }
    assert tickers.isdisjoint(forbidden)


# ---------------------------------------------------------------------------
# UTF-8 BOM tolerance
# ---------------------------------------------------------------------------


def test_parse_iwm_csv_tolerates_utf8_bom():
    body = (FIXTURE_DIR / "iwm_with_bom.csv").read_bytes()
    # Sanity-check: the fixture really starts with the BOM.
    assert body[:3] == b"\xef\xbb\xbf"
    rows, stats = parse_iwm_csv(body)
    assert [r.ticker for r in rows] == ["VRTX", "IDYA", "AAPL", "NVAX", "AXSM"]
    assert stats["equity_rows"] == 5


# ---------------------------------------------------------------------------
# fetch_csv_bytes — UA + last-good fallback on 4xx/5xx
# ---------------------------------------------------------------------------


def test_fetch_csv_bytes_sends_descriptive_user_agent(
    monkeypatch: pytest.MonkeyPatch,
):
    captured = _install_csv_cassette(
        monkeypatch,
        fixture="iwm_happy.csv",
        status_code=200,
    )
    out = fetch_csv_bytes(DEFAULT_IWM_HOLDINGS_URL, timeout=5.0)
    assert out.startswith(b'"iShares Russell 2000 ETF"') or len(out) > 0
    assert len(captured["calls"]) == 1
    headers = captured["calls"][0]["kwargs"].get("headers") or {}
    assert headers.get("User-Agent") == DEFAULT_USER_AGENT
    assert "BiotechSniper" in headers["User-Agent"]
    assert "contact:" in headers["User-Agent"]


@pytest.mark.parametrize("status_code", [403, 404, 500, 502, 503, 504])
def test_fetch_csv_bytes_raises_upstream_unavailable_on_non_2xx(
    monkeypatch: pytest.MonkeyPatch, status_code: int
):
    """Akamai 403, iShares 404, and 5xx all map to UpstreamUnavailable."""
    _install_csv_cassette(monkeypatch, status_code=status_code, body=b"oops")
    with pytest.raises(UpstreamUnavailable) as excinfo:
        fetch_csv_bytes(DEFAULT_IWM_HOLDINGS_URL, timeout=5.0)
    assert str(status_code) in str(excinfo.value)


def test_fetch_csv_bytes_wraps_request_exceptions_as_upstream_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    """ConnectionError / Timeout / SSLError are all transient upstream errors."""
    _install_csv_cassette(monkeypatch, raise_exc=requests.ConnectionError("boom"))
    with pytest.raises(UpstreamUnavailable):
        fetch_csv_bytes(DEFAULT_IWM_HOLDINGS_URL, timeout=5.0)


def test_fetch_csv_bytes_wraps_timeout_as_upstream_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_csv_cassette(
        monkeypatch, raise_exc=requests.Timeout("read timed out")
    )
    with pytest.raises(UpstreamUnavailable):
        fetch_csv_bytes(DEFAULT_IWM_HOLDINGS_URL, timeout=0.001)


# ---------------------------------------------------------------------------
# import_iwm_holdings — happy path persistence
# ---------------------------------------------------------------------------


def test_import_iwm_holdings_writes_snapshot_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _install_csv_cassette(monkeypatch, fixture="iwm_happy.csv")
    db_path = tmp_path / "alpha_sniper.db"

    result = import_iwm_holdings(
        db_path=db_path,
        max_age_hours=0,  # disable cache short-circuit
    )
    assert result.rows_written == 5
    assert result.equity_rows == 5
    assert "ishares.com" in result.source_url

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT ticker, weight, source_url, asset_class FROM "
            "iwm_holdings_snapshot ORDER BY ticker"
        ).fetchall()
    finally:
        conn.close()
    tickers = [r[0] for r in rows]
    assert tickers == ["AAPL", "AXSM", "IDYA", "NVAX", "VRTX"]
    for ticker, weight, source_url, asset_class in rows:
        assert weight is not None
        assert "ishares.com" in source_url
        assert asset_class == "Equity"


def test_import_iwm_holdings_2k_rows_meets_validation_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """≥1900 equity rows after a refresh, per VAL-M1-003 / feature spec."""
    _install_csv_cassette(monkeypatch, fixture="iwm_2k_rows.csv")
    db_path = tmp_path / "alpha_sniper.db"

    result = import_iwm_holdings(db_path=db_path, max_age_hours=0)

    assert result.rows_written >= 1900
    assert result.equity_rows >= 1900
    assert result.null_ticker_count == 0
    assert result.null_weight_count == 0

    conn = sqlite3.connect(db_path)
    try:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM iwm_holdings_snapshot"
        ).fetchone()
    finally:
        conn.close()
    assert count >= 1900


def test_import_iwm_holdings_idempotent_on_same_day(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Running twice on the same day upserts; row count stays constant."""
    _install_csv_cassette(monkeypatch, fixture="iwm_happy.csv")
    db_path = tmp_path / "alpha_sniper.db"

    import_iwm_holdings(db_path=db_path, max_age_hours=0)
    conn = sqlite3.connect(db_path)
    (before,) = conn.execute(
        "SELECT COUNT(*) FROM iwm_holdings_snapshot"
    ).fetchone()
    conn.close()

    import_iwm_holdings(db_path=db_path, max_age_hours=0)
    conn = sqlite3.connect(db_path)
    (after,) = conn.execute(
        "SELECT COUNT(*) FROM iwm_holdings_snapshot"
    ).fetchone()
    conn.close()

    assert before == after == 5


# ---------------------------------------------------------------------------
# Last-good fallback: 403 / 404 / 5xx leave snapshot rows intact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [403, 404, 500, 502, 503, 504])
def test_import_iwm_holdings_last_good_fallback_on_non_2xx(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status_code: int
):
    """Pre-existing snapshot rows are preserved when iShares fails."""
    db_path = tmp_path / "alpha_sniper.db"

    # First, seed a healthy snapshot from the cassette.
    _install_csv_cassette(monkeypatch, fixture="iwm_happy.csv")
    import_iwm_holdings(db_path=db_path, max_age_hours=0)
    conn = sqlite3.connect(db_path)
    pre_count = conn.execute(
        "SELECT COUNT(*) FROM iwm_holdings_snapshot"
    ).fetchone()[0]
    pre_max_fetched = conn.execute(
        "SELECT MAX(fetched_at) FROM iwm_holdings_snapshot"
    ).fetchone()[0]
    conn.close()
    assert pre_count == 5

    # Now simulate iShares returning the failure status code.
    _install_csv_cassette(monkeypatch, status_code=status_code, body=b"failure")
    with pytest.raises(UpstreamUnavailable):
        import_iwm_holdings(db_path=db_path, max_age_hours=0)

    conn = sqlite3.connect(db_path)
    post_count = conn.execute(
        "SELECT COUNT(*) FROM iwm_holdings_snapshot"
    ).fetchone()[0]
    post_max_fetched = conn.execute(
        "SELECT MAX(fetched_at) FROM iwm_holdings_snapshot"
    ).fetchone()[0]
    conn.close()
    assert post_count == pre_count
    assert post_max_fetched == pre_max_fetched


def test_import_iwm_holdings_no_partial_commit_on_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A network timeout writes ZERO rows to a fresh DB."""
    db_path = tmp_path / "alpha_sniper.db"
    _install_csv_cassette(
        monkeypatch, raise_exc=requests.Timeout("read timed out")
    )
    with pytest.raises(UpstreamUnavailable):
        import_iwm_holdings(
            db_path=db_path,
            max_age_hours=0,
            http_timeout=0.001,
        )
    # The fresh db will have the table (created by the cache
    # short-circuit lookup) but zero data rows.
    conn = sqlite3.connect(db_path)
    try:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM iwm_holdings_snapshot"
        ).fetchone()
        assert count == 0
        # Integrity is intact post-rollback.
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert ok == "ok"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI — main()
# ---------------------------------------------------------------------------


def test_main_help_advertises_required_flags(capsys: pytest.CaptureFixture):
    """--help mentions every documented flag (VAL-M1-002)."""
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--db", "--source", "--csv-path", "--dry-run", "--max-age-hours"):
        assert flag in out


def test_main_refresh_writes_rows_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """`python -m biotech_sniper.universe.iwm_importer --refresh` exit 0."""
    _install_csv_cassette(monkeypatch, fixture="iwm_happy.csv")
    db_path = tmp_path / "alpha_sniper.db"

    rc = main(["--refresh", "--db", str(db_path)])
    assert rc == EXIT_OK

    conn = sqlite3.connect(db_path)
    try:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM iwm_holdings_snapshot"
        ).fetchone()
    finally:
        conn.close()
    assert count == 5


def test_main_dry_run_with_emit_stats_emits_well_formed_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
):
    """`--dry-run --emit-stats` prints JSON with equity_rows + null counters."""
    _install_csv_cassette(monkeypatch, fixture="iwm_happy.csv")
    db_path = tmp_path / "alpha_sniper.db"

    rc = main(
        [
            "--dry-run",
            "--emit-stats",
            "--db",
            str(db_path),
            "--max-age-hours",
            "0",
        ]
    )
    assert rc == EXIT_OK
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["equity_rows"] == 5
    assert payload["null_ticker_count"] == 0
    assert payload["null_weight_count"] == 0
    assert payload["dry_run"] is True
    assert payload["rows_written"] == 0  # dry-run never persists.

    conn = sqlite3.connect(db_path)
    try:
        # Dry-run still bootstraps the table (so the cache lookup
        # works), but writes zero rows.
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM iwm_holdings_snapshot"
        ).fetchone()
        assert count == 0
    finally:
        conn.close()


def test_main_returns_upstream_unavailable_exit_code_on_403(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Akamai 403 maps to documented exit code 2."""
    _install_csv_cassette(monkeypatch, status_code=403, body=b"forbidden")
    db_path = tmp_path / "alpha_sniper.db"
    rc = main(["--refresh", "--db", str(db_path)])
    assert rc == EXIT_UPSTREAM_UNAVAILABLE


def test_main_returns_upstream_unavailable_exit_code_on_404(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _install_csv_cassette(monkeypatch, status_code=404, body=b"not found")
    db_path = tmp_path / "alpha_sniper.db"
    rc = main(["--refresh", "--db", str(db_path)])
    assert rc == EXIT_UPSTREAM_UNAVAILABLE


def test_main_returns_upstream_unavailable_exit_code_on_5xx(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _install_csv_cassette(monkeypatch, status_code=503, body=b"unavailable")
    db_path = tmp_path / "alpha_sniper.db"
    rc = main(["--refresh", "--db", str(db_path)])
    assert rc == EXIT_UPSTREAM_UNAVAILABLE


def test_main_csv_path_argument_reads_local_file(
    tmp_path: Path,
):
    """--csv-path reads from disk and bypasses the network."""
    db_path = tmp_path / "alpha_sniper.db"
    fixture = FIXTURE_DIR / "iwm_happy.csv"
    rc = main(
        [
            "--csv-path",
            str(fixture),
            "--db",
            str(db_path),
            "--max-age-hours",
            "0",
        ]
    )
    assert rc == EXIT_OK
    conn = sqlite3.connect(db_path)
    try:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM iwm_holdings_snapshot"
        ).fetchone()
    finally:
        conn.close()
    assert count == 5


# ---------------------------------------------------------------------------
# Cache short-circuit (VAL-M1-005)
# ---------------------------------------------------------------------------


def test_cache_short_circuits_within_refresh_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A second invocation within RUSSELL_BIOTECH_REFRESH_HOURS skips fetch."""
    db_path = tmp_path / "alpha_sniper.db"
    captured = _install_csv_cassette(monkeypatch, fixture="iwm_happy.csv")

    # First run — fetches and persists.
    import_iwm_holdings(db_path=db_path, max_age_hours=0)
    assert len(captured["calls"]) == 1

    # Second run within 24h — must short-circuit, no extra fetch.
    result = import_iwm_holdings(db_path=db_path, max_age_hours=24)
    assert result.refresh_skipped is True
    assert result.skip_reason and "fresh_enough" in result.skip_reason
    assert len(captured["calls"]) == 1  # still 1


def test_cache_disabled_when_max_age_hours_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """RUSSELL_BIOTECH_REFRESH_HOURS=0 forces re-fetch every run."""
    db_path = tmp_path / "alpha_sniper.db"
    captured = _install_csv_cassette(monkeypatch, fixture="iwm_happy.csv")

    import_iwm_holdings(db_path=db_path, max_age_hours=0)
    import_iwm_holdings(db_path=db_path, max_age_hours=0)
    assert len(captured["calls"]) == 2
