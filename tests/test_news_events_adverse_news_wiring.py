"""Tests for the f-m3-17 daily_news_ingest → adverse_news wiring.

f-m3-09 added the :mod:`biotech_sniper.adverse_news` exit hook, but
:func:`biotech_sniper.news_events.daily_news_ingest` did not invoke
:func:`adverse_news.scan_and_trigger` after :func:`record_news_events`
persisted the row. As a result, ingesting an
``enrichment_label='negative_material'`` headline on an active play
did NOT automatically fire the required ``adverse_news`` exit.

f-m3-17 wires the call in. These tests cover the contract described
in the feature spec:

* (a) ingesting a ``negative_material`` headline for an active
  play_cards ticker → exactly one ``submit_exit(event='adverse_news')``
  row is recorded in ``paper_orders``.
* (b) ingesting non-negative headlines → zero ``adverse_news`` exits.
* (c) ``scan_and_trigger`` raising → ``daily_news_ingest`` still
  completes, returns a populated :class:`DailyIngestResult`, and
  audits.

The test injects a fake :class:`PaperExecutor` (via the new
``adverse_news_scan`` hook) so the wiring is exercised end-to-end
without live Alpaca credentials.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Sequence

import pytest

from biotech_sniper import adverse_news as an
from biotech_sniper import db as db_module
from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.news_events import (
    SOURCE_UNIVERSAL,
    DailyIngestResult,
    daily_news_ingest,
)
from biotech_sniper.paper_executor import PaperExecutor


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Minimal AlpacaClient stand-in for the wiring tests.

    Mirrors the surface used by :class:`PaperExecutor.submit_exit`:
    ``submit_order`` returns a queued response and ``get_positions``
    is unused for sell-to-close paths.
    """

    def __init__(self) -> None:
        self.base_url = PAPER_BASE_URL
        self.submit_calls: list[Any] = []
        self._submit_results: list[dict[str, Any]] = []

    def queue(self, response: dict[str, Any]) -> None:
        self._submit_results.append(response)

    def get_positions(self) -> list[dict[str, Any]]:
        return []

    def submit_order(self, order_request: Any) -> dict[str, Any]:
        self.submit_calls.append(order_request)
        if not self._submit_results:
            raise AssertionError("submit_order: no result queued")
        return self._submit_results.pop(0)

    def get_order(self, order_id: str) -> dict[str, Any]:
        return {"id": order_id, "status": "accepted"}


def _stub_sell_response(
    *, order_id: str = "44444444-4444-4444-4444-444444444444", qty: int = 5
) -> dict[str, Any]:
    return {
        "id": order_id,
        "client_order_id": "AXSM-adverse_news-2025-04-27",
        "symbol": "AXSM250620C00125000",
        "asset_class": "us_option",
        "qty": qty,
        "side": "sell",
        "status": "accepted",
        "order_class": "simple",
        "type": "market",
        "time_in_force": "day",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_universe(db_path: Path, tickers: list[str]) -> None:
    """Insert minimal ``universe`` rows so the watcher loop has tickers."""
    conn = db_module.connect(db_path)
    try:
        db_module.run_migrations(conn)
        for t in tickers:
            conn.execute(
                "INSERT INTO universe (ticker, tier, has_options_chain) "
                "VALUES (?, 'watch', 0)",
                (t,),
            )
        conn.commit()
    finally:
        conn.close()


def _watcher_returning(events: list[dict[str, Any]]):
    """Return a watcher fn that always yields ``events``."""

    def _fn(_tickers: Sequence[str]) -> list[dict[str, Any]]:
        return [dict(e) for e in events]

    return _fn


def _make_active_play(
    *,
    ticker: str = "AXSM",
    play_card_id: str = "AXSM-2025-04-27",
    symbol: str = "AXSM250620C00125000",
    catalyst_date: str = "2025-04-30",
    qty: int = 5,
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "play_card_id": play_card_id,
        "symbol": symbol,
        "catalyst_date": catalyst_date,
        "qty": qty,
    }


def _build_real_scan(
    *,
    db_path: Path,
    fake_client: _FakeAlpacaClient,
    active_plays: list[dict[str, Any]],
):
    """Build an ``adverse_news_scan`` callable using a real PaperExecutor.

    The callable matches the production injection contract: it
    receives the resolved DB path and dispatches
    :func:`adverse_news.scan_and_trigger` against a freshly-built
    :class:`PaperExecutor` wired to ``fake_client``. This exercises
    the same submit_exit code path the real cron uses, just with
    an in-process fake on the broker side.
    """

    def _scan(target_path: Path) -> list[dict[str, Any]]:
        executor = PaperExecutor(
            fake_client,  # type: ignore[arg-type]
            db_path=target_path,
            poll_interval_seconds=0.0,
        )
        return list(
            an.scan_and_trigger(
                executor,
                active_plays,
                db_path=target_path,
            )
            or []
        )

    return _scan


# ---------------------------------------------------------------------------
# (a) negative_material headline → exactly one adverse_news exit row
# ---------------------------------------------------------------------------


def test_daily_news_ingest_fires_adverse_news_exit_on_negative_material(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A ``negative_material`` headline on an active play triggers one exit."""
    db_path = tmp_path / "alpha.db"
    _seed_universe(db_path, ["AXSM"])

    fake_client = _FakeAlpacaClient()
    fake_client.queue(_stub_sell_response(qty=5))

    watchers = {
        SOURCE_UNIVERSAL: _watcher_returning(
            [
                {
                    "ticker": "AXSM",
                    "title": "FDA issues CRL for AXSM lead asset",
                    "url": "https://example.com/axsm-crl",
                    "published_at": "2025-04-27",
                    "enrichment_label": "negative_material",
                }
            ]
        )
    }

    scan_fn = _build_real_scan(
        db_path=db_path,
        fake_client=fake_client,
        active_plays=[_make_active_play()],
    )

    with caplog.at_level(logging.INFO, logger="biotech_sniper.news_events"):
        result = daily_news_ingest(
            db_path=db_path,
            watchers=watchers,
            audit_path=tmp_path / "audit.json",
            adverse_news_scan=scan_fn,
        )

    # daily_news_ingest reports a clean run.
    assert isinstance(result, DailyIngestResult)
    assert result.rows_inserted == 1
    assert result.completed_at  # populated → run reached the end

    # Exactly one submit_exit(event='adverse_news') row exists.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT event, side, qty, parent_play_card_id "
            "FROM paper_orders WHERE event = ?",
            ("adverse_news",),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, f"expected 1 adverse_news row, got {len(rows)}"
    assert rows[0]["side"] == "sell"
    assert rows[0]["qty"] == 5
    assert rows[0]["parent_play_card_id"] == "AXSM-2025-04-27"

    # Exactly one submit_order call landed on the fake client.
    assert len(fake_client.submit_calls) == 1

    # The INFO log fires when N>0 with the contractual format.
    matching = [
        r
        for r in caplog.records
        if "adverse_news.scan_and_trigger: triggered=" in r.getMessage()
    ]
    assert matching, (
        "expected 'adverse_news.scan_and_trigger: triggered=N exits' INFO log; "
        f"got {[r.getMessage() for r in caplog.records]}"
    )
    assert "triggered=1 exits" in matching[0].getMessage()
    assert matching[0].levelno == logging.INFO


# ---------------------------------------------------------------------------
# (b) non-negative headlines → zero adverse_news exits
# ---------------------------------------------------------------------------


def test_daily_news_ingest_does_not_fire_for_non_negative_headlines(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Non-``negative_material`` headlines must not produce any exit row."""
    db_path = tmp_path / "alpha.db"
    _seed_universe(db_path, ["AXSM"])

    fake_client = _FakeAlpacaClient()
    # No queued responses — submit_order would raise if called.

    watchers = {
        SOURCE_UNIVERSAL: _watcher_returning(
            [
                {
                    "ticker": "AXSM",
                    "title": "AXSM positive readout, beats expectations",
                    "url": "https://example.com/axsm-positive",
                    "published_at": "2025-04-27",
                    "enrichment_label": "positive_material",
                },
                {
                    "ticker": "AXSM",
                    "title": "Routine 10-Q filing",
                    "url": "https://example.com/axsm-10q",
                    "published_at": "2025-04-27",
                    # no enrichment_label at all → must be ignored too
                },
            ]
        )
    }

    scan_fn = _build_real_scan(
        db_path=db_path,
        fake_client=fake_client,
        active_plays=[_make_active_play()],
    )

    with caplog.at_level(logging.INFO, logger="biotech_sniper.news_events"):
        result = daily_news_ingest(
            db_path=db_path,
            watchers=watchers,
            audit_path=tmp_path / "audit.json",
            adverse_news_scan=scan_fn,
        )

    assert result.rows_inserted == 2
    assert result.completed_at

    # Zero adverse_news rows → zero submit_order calls.
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE event = ?",
            ("adverse_news",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0
    assert fake_client.submit_calls == []

    # The "triggered=N exits" INFO log only fires when N>0; with zero
    # exits it must NOT appear.
    triggered_logs = [
        r
        for r in caplog.records
        if "adverse_news.scan_and_trigger: triggered=" in r.getMessage()
    ]
    assert triggered_logs == []


# ---------------------------------------------------------------------------
# (c) scan failure → daily_news_ingest still completes successfully
# ---------------------------------------------------------------------------


def test_daily_news_ingest_swallows_scan_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A raise inside ``adverse_news_scan`` must NOT abort the ingest."""
    db_path = tmp_path / "alpha.db"
    _seed_universe(db_path, ["AXSM"])

    watchers = {
        SOURCE_UNIVERSAL: _watcher_returning(
            [
                {
                    "ticker": "AXSM",
                    "title": "AXSM CRL",
                    "url": "https://example.com/axsm-crl",
                    "published_at": "2025-04-27",
                    "enrichment_label": "negative_material",
                }
            ]
        )
    }

    def _exploding_scan(_target: Path) -> list[dict[str, Any]]:
        raise RuntimeError("simulated alpaca outage")

    audit_path = tmp_path / "audit.json"
    with caplog.at_level(logging.WARNING, logger="biotech_sniper.news_events"):
        result = daily_news_ingest(
            db_path=db_path,
            watchers=watchers,
            audit_path=audit_path,
            adverse_news_scan=_exploding_scan,
        )

    # The ingest still reports success — populated DailyIngestResult,
    # rows persisted, audit summary written.
    assert isinstance(result, DailyIngestResult)
    assert result.rows_inserted == 1
    assert result.completed_at  # populated → finally branch ran
    assert audit_path.is_file(), "audit summary must be written even on scan failure"

    # The negative_material row landed in news_events.
    conn = sqlite3.connect(db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM news_events "
            "WHERE enrichment_label = 'negative_material'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1

    # Zero paper_orders rows because the scan blew up before submit_exit.
    conn = sqlite3.connect(db_path)
    try:
        adverse_rows = conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE event = ?",
            ("adverse_news",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert adverse_rows == 0

    # A WARNING line names the failure so operators can debug it.
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "adverse_news.scan_and_trigger raised" in r.getMessage()
    ]
    assert warnings, (
        "expected a WARNING log naming the scan failure; got "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    assert "RuntimeError" in warnings[0].getMessage()
    assert "simulated alpaca outage" in warnings[0].getMessage()
