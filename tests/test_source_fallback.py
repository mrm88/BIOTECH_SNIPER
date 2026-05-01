"""Reading-B feature ``f-cross-09-source-fallback-determinism`` regression tests.

The orchestrator under test is
:mod:`biotech_sniper.universe.refresher`. It owns the documented,
DETERMINISTIC fallback when one or both upstream sources for the
``russell2k_biotech`` refresh are unavailable.

Documented behaviour (validation contract VAL-CROSS-035 +
VAL-CROSS-036):

* iShares 404 with EDGAR healthy → ``russell2k_biotech`` is
  PRESERVED (last-good fallback). The orchestrator emits a
  structured WARNING ``{"event":"ishares_404_using_last_good", ...}``
  log line and returns exit-code 0.

* Both iShares AND EDGAR 404 → behaviour follows the
  ``UNIVERSE_FALLBACK_MODE`` env var (deterministic across re-runs
  with the same inputs):

  - ``halt`` (DEFAULT): structured ERROR
    ``{"event":"universe_sources_unavailable", "action":"halt"}``;
    exit non-zero (``EXIT_BOTH_SOURCES_DOWN = 6``);
    ``russell2k_biotech`` rows are NOT modified.

  - ``use_stale``: structured WARNING
    ``{"event":"universe_sources_unavailable",
       "action":"use_stale", "stale_seconds":N}``;
    rows preserved; exit 0.

The dual-path-test convention (per ``python-worker`` skill):
:mod:`tests.test_universe_refresh` re-exports the two contract
node-IDs (``test_ishares_404_falls_back_to_last_good`` and
``test_both_sources_404_deterministic``) from this module so both
collection paths pass without duplicating test bodies.
"""

from __future__ import annotations

import datetime
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import requests

from biotech_sniper.universe import iwm_importer, refresher
from biotech_sniper.universe.refresher import (
    EXIT_BOTH_SOURCES_DOWN,
    EXIT_OK,
    UNIVERSE_FALLBACK_HALT,
    UNIVERSE_FALLBACK_USE_STALE,
    BothSourcesUnavailable,
    RefresherFallbackResult,
    refresh_universe,
    resolve_fallback_mode,
)


# ---------------------------------------------------------------------------
# Cassette helpers (mock requests.get for both iShares and SEC EDGAR)
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal ``requests.Response`` stand-in.

    Carries only the surface area the production code reads:
    ``status_code``, ``content``, ``text``, ``headers``.
    """

    def __init__(
        self,
        *,
        status_code: int,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.content = body
        self.text = body.decode("utf-8", errors="replace")
        self.headers = headers or {}

    def raise_for_status(self) -> None:  # pragma: no cover - unused
        if not (200 <= self.status_code < 300):
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _install_dual_source_cassette(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ishares_status: int = 200,
    edgar_status: int = 200,
    ishares_body: bytes = b"",
    edgar_body: bytes = b'{"ok":true}',
    raise_on_ishares: Exception | None = None,
    raise_on_edgar: Exception | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Patch ``requests.get`` (the only HTTP egress used by the
    refresher) to dispatch by hostname.

    iShares URLs route to ``ishares_*`` parameters; SEC EDGAR
    URLs (``data.sec.gov`` or ``www.sec.gov``) route to ``edgar_*``
    parameters.

    Returns a dict carrying captured calls (one list per source)
    so individual tests can assert how many requests went to
    which host.
    """
    captured: dict[str, list[dict[str, Any]]] = {
        "ishares": [],
        "edgar": [],
        "other": [],
    }

    def _classify(url: str) -> str:
        if "ishares.com" in url:
            return "ishares"
        if "sec.gov" in url:
            return "edgar"
        return "other"

    def fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        which = _classify(url)
        captured[which].append({"url": url, "kwargs": kwargs})
        if which == "ishares":
            if raise_on_ishares is not None:
                raise raise_on_ishares
            return _FakeResponse(status_code=ishares_status, body=ishares_body)
        if which == "edgar":
            if raise_on_edgar is not None:
                raise raise_on_edgar
            return _FakeResponse(status_code=edgar_status, body=edgar_body)
        return _FakeResponse(status_code=200, body=b"")

    # Patch both iwm_importer.requests.get and refresher.requests.get
    # so the refresher's own EDGAR probe and the iwm_importer's
    # iShares fetch both flow through this fake.
    monkeypatch.setattr(iwm_importer.requests, "get", fake_get)
    monkeypatch.setattr(refresher.requests, "get", fake_get)
    return captured


def _seed_russell2k_rows(db_path: Path, *, count: int = 3) -> int:
    """Write ``count`` synthetic ``russell2k_biotech`` rows.

    Returns the row count actually written. Used to simulate a
    pre-existing last-good snapshot that the fallback must
    preserve byte-for-byte.
    """
    from biotech_sniper.universe.russell_biotech import (
        ensure_russell2k_biotech_table,
    )

    conn = sqlite3.connect(db_path)
    try:
        ensure_russell2k_biotech_table(conn)
        payload = [
            (
                f"FAKE{i}",
                f"{i:010d}",
                2834,
                "PHARMACEUTICALS",
                0.05 + i * 0.01,
                10_000.0 + i * 100.0,
                "2026-04-29",
                "2026-04-29T00:00:00.000000Z",
            )
            for i in range(count)
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO russell2k_biotech ("
            "ticker, cik, sic, sic_description, iwm_weight, "
            "iwm_market_value_usd, as_of_date, fetched_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
        conn.commit()
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM russell2k_biotech"
        ).fetchone()
    finally:
        conn.close()
    return n


def _row_count(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        try:
            (n,) = conn.execute(
                "SELECT COUNT(*) FROM russell2k_biotech"
            ).fetchone()
        except sqlite3.OperationalError:
            return 0
    finally:
        conn.close()
    return int(n)


def _row_fingerprint(db_path: Path) -> list[tuple]:
    """Return a stable ordered fingerprint of every row.

    Used to assert byte-exact preservation across a fallback
    refresh — not just the row COUNT but every cell's value.
    """
    conn = sqlite3.connect(db_path)
    try:
        try:
            return conn.execute(
                "SELECT ticker, cik, sic, sic_description, iwm_weight, "
                "iwm_market_value_usd, as_of_date, fetched_at "
                "FROM russell2k_biotech ORDER BY ticker"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    finally:
        conn.close()


def _find_log(records: list[logging.LogRecord], event: str) -> dict | None:
    """Scan ``records`` for a structured log line whose ``event`` field
    matches.

    The refresher emits one JSON object per WARNING / ERROR; the
    helper finds the first matching record and returns the parsed
    dict (or ``None`` if no match).
    """
    for rec in records:
        msg = rec.getMessage()
        try:
            payload = json.loads(msg)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("event") == event:
            return payload
    return None


# ---------------------------------------------------------------------------
# resolve_fallback_mode — env-var parsing
# ---------------------------------------------------------------------------


def test_resolve_fallback_mode_defaults_to_halt(
    monkeypatch: pytest.MonkeyPatch,
):
    """Default policy is HALT — fail-loud is the documented preferred mode."""
    monkeypatch.delenv("UNIVERSE_FALLBACK_MODE", raising=False)
    assert resolve_fallback_mode(None) == UNIVERSE_FALLBACK_HALT


def test_resolve_fallback_mode_respects_env(
    monkeypatch: pytest.MonkeyPatch,
):
    """``UNIVERSE_FALLBACK_MODE`` env var picks the alternative mode."""
    monkeypatch.setenv("UNIVERSE_FALLBACK_MODE", "use_stale")
    assert resolve_fallback_mode(None) == UNIVERSE_FALLBACK_USE_STALE


def test_resolve_fallback_mode_explicit_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
):
    """An explicit kwarg trumps the env var."""
    monkeypatch.setenv("UNIVERSE_FALLBACK_MODE", "use_stale")
    assert (
        resolve_fallback_mode(UNIVERSE_FALLBACK_HALT) == UNIVERSE_FALLBACK_HALT
    )


def test_resolve_fallback_mode_rejects_invalid(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """An unknown value falls back to HALT (deterministic, conservative)."""
    monkeypatch.setenv("UNIVERSE_FALLBACK_MODE", "totally-bogus")
    with caplog.at_level(logging.WARNING, logger="biotech_sniper.universe.refresher"):
        mode = resolve_fallback_mode(None)
    assert mode == UNIVERSE_FALLBACK_HALT
    # The override is logged so operators notice the typo.
    assert any(
        "UNIVERSE_FALLBACK_MODE" in rec.getMessage() for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# VAL-CROSS-035: iShares 404 + EDGAR 200 → preserve last-good, WARNING
# ---------------------------------------------------------------------------


def test_ishares_404_falls_back_to_last_good(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    """VAL-CROSS-035: iShares 404 with EDGAR healthy preserves the
    russell2k_biotech rows verbatim and emits a structured WARNING.
    """
    db_path = tmp_path / "alpha.db"
    pre_count = _seed_russell2k_rows(db_path, count=5)
    pre_fingerprint = _row_fingerprint(db_path)

    _install_dual_source_cassette(
        monkeypatch,
        ishares_status=404,
        edgar_status=200,
    )

    with caplog.at_level(
        logging.WARNING, logger="biotech_sniper.universe.refresher"
    ):
        result = refresh_universe(
            db_path=db_path,
            fallback_mode=UNIVERSE_FALLBACK_HALT,
        )

    # Exit code is 0 — last-good fallback is a soft, recoverable
    # condition that downstream cron must NOT misread as a hard
    # failure.
    assert result.exit_code == EXIT_OK
    assert result.fallback_taken is True
    assert result.action == "ishares_404_using_last_good"

    # russell2k_biotech is byte-exact preserved.
    assert _row_count(db_path) == pre_count
    assert _row_fingerprint(db_path) == pre_fingerprint

    # The refresher emitted exactly one structured WARNING
    # naming the failed source.
    log = _find_log(caplog.records, "ishares_404_using_last_good")
    assert log is not None
    assert log["source"] == "ishares"
    assert "preserved_row_count" in log
    assert log["preserved_row_count"] == pre_count
    # The log captures the actual upstream HTTP status so
    # operators can grep "status: 404" reliably.
    assert log.get("status") == 404


def test_ishares_404_does_not_re_run_classifier(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """When iShares fails, the refresher MUST NOT trigger a full
    SEC SIC re-classification.

    The contract is "preserve russell2k_biotech as-is" — re-running
    SIC classification (a) burns SEC fair-access budget and (b)
    risks producing a slightly different snapshot if the
    cik_sic_cache has shifted between runs. We probe EDGAR for
    health (one request) and STOP.
    """
    db_path = tmp_path / "alpha.db"
    _seed_russell2k_rows(db_path, count=3)

    captured = _install_dual_source_cassette(
        monkeypatch,
        ishares_status=404,
        edgar_status=200,
    )

    refresh_universe(db_path=db_path, fallback_mode=UNIVERSE_FALLBACK_HALT)

    # Exactly one EDGAR probe — no per-ticker submissions look-ups.
    assert len(captured["edgar"]) == 1
    # Exactly one (or zero, if the iwm_importer never fired) iShares
    # request from the importer probe path. Crucially: the refresher
    # MUST NOT issue >1 EDGAR call (which would imply per-ticker
    # SIC fetching against the SEC submissions endpoint).
    assert len(captured["edgar"]) <= 1


# ---------------------------------------------------------------------------
# VAL-CROSS-036: both 404 → deterministic action per UNIVERSE_FALLBACK_MODE
# ---------------------------------------------------------------------------


def test_both_sources_404_deterministic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    """VAL-CROSS-036: with iShares 404 AND EDGAR 404, the refresher's
    action is deterministic across re-runs given the same configured
    mode. The default mode is HALT (preferred, fail-loud).

    Two consecutive runs with the same input produce IDENTICAL
    results: same exit code, same action, same russell2k_biotech
    row count.
    """
    db_path = tmp_path / "alpha.db"
    pre_count = _seed_russell2k_rows(db_path, count=4)
    pre_fingerprint = _row_fingerprint(db_path)

    _install_dual_source_cassette(
        monkeypatch,
        ishares_status=404,
        edgar_status=404,
    )

    # Default mode = HALT.
    monkeypatch.delenv("UNIVERSE_FALLBACK_MODE", raising=False)

    # First run.
    with caplog.at_level(
        logging.ERROR, logger="biotech_sniper.universe.refresher"
    ):
        result1 = refresh_universe(db_path=db_path)
    log1 = _find_log(caplog.records, "universe_sources_unavailable")

    # Second run — same input, same action.
    caplog.clear()
    with caplog.at_level(
        logging.ERROR, logger="biotech_sniper.universe.refresher"
    ):
        result2 = refresh_universe(db_path=db_path)
    log2 = _find_log(caplog.records, "universe_sources_unavailable")

    # Same exit code, same action, same fallback_taken flag.
    assert result1.exit_code == result2.exit_code == EXIT_BOTH_SOURCES_DOWN
    assert result1.action == result2.action == "halt"
    assert result1.fallback_taken == result2.fallback_taken is True

    # russell2k_biotech is byte-exact preserved across BOTH runs.
    assert _row_count(db_path) == pre_count
    assert _row_fingerprint(db_path) == pre_fingerprint

    # Structured ERROR present on every run with action=halt.
    assert log1 is not None and log1["action"] == "halt"
    assert log2 is not None and log2["action"] == "halt"


def test_both_sources_404_use_stale_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    """``UNIVERSE_FALLBACK_MODE=use_stale`` keeps the prior list and
    emits a WARNING with ``stale_seconds``. Exit 0.
    """
    db_path = tmp_path / "alpha.db"
    pre_count = _seed_russell2k_rows(db_path, count=3)

    _install_dual_source_cassette(
        monkeypatch,
        ishares_status=404,
        edgar_status=404,
    )

    with caplog.at_level(
        logging.WARNING, logger="biotech_sniper.universe.refresher"
    ):
        result = refresh_universe(
            db_path=db_path,
            fallback_mode=UNIVERSE_FALLBACK_USE_STALE,
        )

    assert result.exit_code == EXIT_OK
    assert result.fallback_taken is True
    assert result.action == "use_stale"
    assert _row_count(db_path) == pre_count

    log = _find_log(caplog.records, "universe_sources_unavailable")
    assert log is not None
    assert log["action"] == "use_stale"
    # ``stale_seconds`` is a non-negative number (the wall-clock
    # gap since the most recent ``fetched_at``).
    assert isinstance(log.get("stale_seconds"), (int, float))
    assert log["stale_seconds"] >= 0


def test_use_stale_emits_stale_warning_true(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    """VAL-CROSS-036 regression: in ``use_stale`` mode, the WARNING
    payload MUST carry ``stale_warning=true`` alongside
    ``action='use_stale'`` and ``stale_seconds=<float>`` so log
    consumers (watchdog, dashboards, alerting) can filter on the
    ``stale_warning=true`` flag without re-deriving it from
    ``action``. Determinism: two consecutive runs in use_stale mode
    emit byte-identical structured payloads.
    """
    db_path = tmp_path / "alpha.db"
    _seed_russell2k_rows(db_path, count=3)

    _install_dual_source_cassette(
        monkeypatch,
        ishares_status=404,
        edgar_status=404,
    )

    # Pin ``now`` across both runs so ``stale_seconds`` is identical;
    # the determinism contract is "same input → same payload" and the
    # wall-clock between two real runs is the only non-deterministic
    # input that the orchestrator otherwise consumes.
    fixed_now = datetime.datetime(2026, 4, 30, 12, 0, 0, tzinfo=datetime.timezone.utc)

    # Run #1.
    with caplog.at_level(
        logging.WARNING, logger="biotech_sniper.universe.refresher"
    ):
        result1 = refresh_universe(
            db_path=db_path,
            fallback_mode=UNIVERSE_FALLBACK_USE_STALE,
            now=fixed_now,
        )
    log1 = _find_log(caplog.records, "universe_sources_unavailable")
    # Capture the raw JSON message for byte-identity comparison.
    raw1 = next(
        rec.getMessage()
        for rec in caplog.records
        if "universe_sources_unavailable" in rec.getMessage()
    )

    # Run #2 — same DB, same cassette, same pinned ``now``.
    caplog.clear()
    with caplog.at_level(
        logging.WARNING, logger="biotech_sniper.universe.refresher"
    ):
        result2 = refresh_universe(
            db_path=db_path,
            fallback_mode=UNIVERSE_FALLBACK_USE_STALE,
            now=fixed_now,
        )
    log2 = _find_log(caplog.records, "universe_sources_unavailable")
    raw2 = next(
        rec.getMessage()
        for rec in caplog.records
        if "universe_sources_unavailable" in rec.getMessage()
    )

    # The dataclass action is unchanged.
    assert result1.action == result2.action == "use_stale"
    assert result1.exit_code == result2.exit_code == EXIT_OK

    # Both runs must emit a structured WARNING payload that includes
    # stale_warning=true, action=use_stale, and a numeric stale_seconds.
    for log in (log1, log2):
        assert log is not None
        assert log["action"] == "use_stale"
        assert log.get("stale_warning") is True
        assert isinstance(log.get("stale_seconds"), (int, float))
        assert log["stale_seconds"] >= 0

    # Determinism: byte-identical structured payloads across re-runs.
    assert raw1 == raw2


def test_both_sources_404_halt_raises_helpful(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """``raise_on_halt=True`` lifts the halt action to a typed
    exception so the cron entrypoint can ``sys.exit`` cleanly.

    Without the flag the orchestrator returns the result object
    so callers can branch on ``result.exit_code``. With the flag
    it raises :class:`BothSourcesUnavailable`.
    """
    db_path = tmp_path / "alpha.db"
    _seed_russell2k_rows(db_path, count=2)

    _install_dual_source_cassette(
        monkeypatch,
        ishares_status=404,
        edgar_status=404,
    )

    with pytest.raises(BothSourcesUnavailable):
        refresh_universe(
            db_path=db_path,
            fallback_mode=UNIVERSE_FALLBACK_HALT,
            raise_on_halt=True,
        )


# ---------------------------------------------------------------------------
# Determinism across re-runs (separate from contract case above)
# ---------------------------------------------------------------------------


def test_deterministic_across_consecutive_runs_use_stale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """Two consecutive use_stale runs against the same DB / cassette
    yield identical action + row counts (no flapping).
    """
    db_path = tmp_path / "alpha.db"
    _seed_russell2k_rows(db_path, count=4)

    _install_dual_source_cassette(
        monkeypatch,
        ishares_status=404,
        edgar_status=404,
    )

    r1 = refresh_universe(
        db_path=db_path, fallback_mode=UNIVERSE_FALLBACK_USE_STALE
    )
    fp1 = _row_fingerprint(db_path)

    r2 = refresh_universe(
        db_path=db_path, fallback_mode=UNIVERSE_FALLBACK_USE_STALE
    )
    fp2 = _row_fingerprint(db_path)

    assert r1.exit_code == r2.exit_code == EXIT_OK
    assert r1.action == r2.action == "use_stale"
    assert fp1 == fp2


def test_deterministic_ishares_404_edgar_200_across_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """The iShares-404-only path is also deterministic on re-run."""
    db_path = tmp_path / "alpha.db"
    _seed_russell2k_rows(db_path, count=6)

    _install_dual_source_cassette(
        monkeypatch,
        ishares_status=404,
        edgar_status=200,
    )

    r1 = refresh_universe(
        db_path=db_path, fallback_mode=UNIVERSE_FALLBACK_HALT
    )
    fp1 = _row_fingerprint(db_path)
    r2 = refresh_universe(
        db_path=db_path, fallback_mode=UNIVERSE_FALLBACK_HALT
    )
    fp2 = _row_fingerprint(db_path)

    assert r1.action == r2.action == "ishares_404_using_last_good"
    assert r1.exit_code == r2.exit_code == EXIT_OK
    assert fp1 == fp2


# ---------------------------------------------------------------------------
# Edge: iShares 5xx + connection errors flow through the same fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ishares_status", [403, 500, 502, 503, 504])
def test_ishares_5xx_with_edgar_healthy_takes_last_good(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ishares_status: int,
    caplog: pytest.LogCaptureFixture,
):
    """Akamai 403 / iShares 5xx route through the same WARNING +
    last-good fallback as the documented 404 case.
    """
    db_path = tmp_path / "alpha.db"
    _seed_russell2k_rows(db_path, count=3)

    _install_dual_source_cassette(
        monkeypatch,
        ishares_status=ishares_status,
        edgar_status=200,
    )

    with caplog.at_level(
        logging.WARNING, logger="biotech_sniper.universe.refresher"
    ):
        result = refresh_universe(
            db_path=db_path, fallback_mode=UNIVERSE_FALLBACK_HALT
        )

    assert result.exit_code == EXIT_OK
    assert result.fallback_taken is True
    log = _find_log(caplog.records, "ishares_404_using_last_good")
    # The structured event uses the documented ``ishares_404_*``
    # name across all non-2xx statuses; the actual status code is
    # carried in the payload.
    assert log is not None
    assert log["status"] == ishares_status


def test_ishares_connection_error_with_edgar_healthy_takes_last_good(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """A ConnectionError / Timeout on iShares is treated identically
    to an HTTP 4xx/5xx for fallback purposes.
    """
    db_path = tmp_path / "alpha.db"
    _seed_russell2k_rows(db_path, count=2)

    _install_dual_source_cassette(
        monkeypatch,
        ishares_status=200,  # unused — error short-circuits
        edgar_status=200,
        raise_on_ishares=requests.ConnectionError("dns failure"),
    )

    result = refresh_universe(
        db_path=db_path, fallback_mode=UNIVERSE_FALLBACK_HALT
    )
    assert result.exit_code == EXIT_OK
    assert result.action == "ishares_404_using_last_good"


# ---------------------------------------------------------------------------
# RefresherFallbackResult shape
# ---------------------------------------------------------------------------


def test_refresher_fallback_result_is_a_dataclass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The result is a frozen-ish dataclass with stable field names —
    ``exit_code``, ``action``, ``fallback_taken``, ``preserved_row_count``,
    ``stale_seconds``.
    """
    db_path = tmp_path / "alpha.db"
    _seed_russell2k_rows(db_path, count=4)
    _install_dual_source_cassette(
        monkeypatch, ishares_status=404, edgar_status=200
    )
    result = refresh_universe(
        db_path=db_path, fallback_mode=UNIVERSE_FALLBACK_HALT
    )
    assert isinstance(result, RefresherFallbackResult)
    # Stable public field names.
    for attr in (
        "exit_code",
        "action",
        "fallback_taken",
        "preserved_row_count",
    ):
        assert hasattr(result, attr)
    assert result.preserved_row_count >= 0


# ---------------------------------------------------------------------------
# Decision-documented invariant: source code names the chosen action
# ---------------------------------------------------------------------------


def test_decision_documented_in_code():
    """VAL-CROSS-036 requires the both-sources-404 decision to be
    documented IN CODE (so the deterministic action is auditable
    without spelunking the contract).

    The refresher module's docstring MUST mention both
    ``UNIVERSE_FALLBACK_MODE`` legal values (``halt`` and
    ``use_stale``) AND explicitly call out that ``halt`` is the
    DEFAULT.
    """
    doc = (refresher.__doc__ or "").lower()
    assert "halt" in doc
    assert "use_stale" in doc
    assert "default" in doc
    # The mode constants exposed at module level are the canonical
    # spellings.
    assert refresher.UNIVERSE_FALLBACK_HALT == "halt"
    assert refresher.UNIVERSE_FALLBACK_USE_STALE == "use_stale"
