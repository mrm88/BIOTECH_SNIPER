"""Unit tests for :mod:`biotech_sniper.adverse_news` (f-m3-09).

The adverse-news exit hook closes 100% of an active play when the
news pipeline ingests a row tagged
``enrichment_label='negative_material'`` for that ticker. These
tests exercise:

* Direct event dispatch (``trigger_for_events``) — fires when the
  label matches and the ticker has an active play, skips otherwise.
* The DB-driven scan (``scan_and_trigger``) — picks up
  negative_material rows from ``news_events`` and submits exits.
* Idempotency: re-running the trigger same day produces no
  duplicate.
* The combined helper :func:`record_negative_news_and_exit` for
  test convenience.

Validation contract assertions exercised
----------------------------------------
* **VAL-M3-051** — a ``negative_material`` row triggers exactly one
  ``adverse_news`` exit per ``(ticker, date)``; idempotent.
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper import adverse_news as an
from biotech_sniper import db as db_module
from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.paper_executor import PaperExecutor


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
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
    *, order_id: str = "33333333-3333-3333-3333-333333333333", qty: int = 5
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


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "alpha_sniper.db"


@pytest.fixture
def make_runner(db_path: Path):
    def _factory(
        *, client: _FakeAlpacaClient | None = None
    ) -> tuple[an.AdverseNewsExitRunner, PaperExecutor, _FakeAlpacaClient]:
        fake = client or _FakeAlpacaClient()
        executor = PaperExecutor(
            fake,  # type: ignore[arg-type]
            db_path=db_path,
            poll_interval_seconds=0.0,
        )
        runner = an.AdverseNewsExitRunner(executor)
        return runner, executor, fake

    return _factory


def _active_play(
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


# ---------------------------------------------------------------------------
# 1) Trigger fires on negative_material with an active play.
# ---------------------------------------------------------------------------


def test_adverse_news_fires_on_negative_material(
    make_runner, db_path: Path
) -> None:
    """A ``negative_material`` event fires exactly one adverse_news exit."""
    fake = _FakeAlpacaClient()
    fake.queue(_stub_sell_response(qty=5))
    runner, _, _ = make_runner(client=fake)

    today = datetime.date(2025, 4, 27)
    events = [
        {
            "ticker": "AXSM",
            "title": "FDA issues CRL on AXSM lead asset",
            "enrichment_label": "negative_material",
        }
    ]
    results = runner.trigger_for_events(
        events, [_active_play()], today=today
    )

    assert len(results) == 1
    assert results[0]["status"] == "submitted"
    assert results[0]["event"] == "adverse_news"
    assert results[0]["qty"] == 5
    assert len(fake.submit_calls) == 1

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
    assert len(rows) == 1
    assert rows[0]["side"] == "sell"
    assert rows[0]["qty"] == 5
    assert rows[0]["parent_play_card_id"] == "AXSM-2025-04-27"


# ---------------------------------------------------------------------------
# 2) Trigger ignores other / missing labels.
# ---------------------------------------------------------------------------


def test_adverse_news_does_not_fire_for_other_label(
    make_runner, db_path: Path
) -> None:
    """A ``positive_material`` (or unset) label does not fire any exit."""
    fake = _FakeAlpacaClient()
    runner, _, _ = make_runner(client=fake)

    today = datetime.date(2025, 4, 27)
    events = [
        {
            "ticker": "AXSM",
            "title": "AXSM positive readout",
            "enrichment_label": "positive_material",
        },
        {"ticker": "AXSM", "title": "neutral headline"},
    ]
    results = runner.trigger_for_events(
        events, [_active_play()], today=today
    )

    assert results == []  # no negative_material → nothing returned
    assert fake.submit_calls == []


# ---------------------------------------------------------------------------
# 3) Skipped when no active play exists.
# ---------------------------------------------------------------------------


def test_adverse_news_skipped_when_no_active_play(
    make_runner,
) -> None:
    """A negative_material headline for an off-book ticker is recorded
    as ``skipped`` with reason ``no_active_play``."""
    fake = _FakeAlpacaClient()
    runner, _, _ = make_runner(client=fake)

    today = datetime.date(2025, 4, 27)
    events = [
        {
            "ticker": "ZZZZ",
            "title": "ZZZZ pivotal trial fails",
            "enrichment_label": "negative_material",
        }
    ]
    results = runner.trigger_for_events(events, [], today=today)
    assert len(results) == 1
    assert results[0]["status"] == "skipped"
    assert results[0]["reason"] == "no_active_play"
    assert fake.submit_calls == []


# ---------------------------------------------------------------------------
# 4) Idempotent on the same day.
# ---------------------------------------------------------------------------


def test_adverse_news_idempotent_on_same_day(
    make_runner, db_path: Path
) -> None:
    """Second invocation same day → no duplicate exit row."""
    fake = _FakeAlpacaClient()
    fake.queue(_stub_sell_response(qty=5))
    runner, _, _ = make_runner(client=fake)

    today = datetime.date(2025, 4, 27)
    events = [
        {
            "ticker": "AXSM",
            "title": "FDA CRL",
            "enrichment_label": "negative_material",
        }
    ]
    plays = [_active_play()]

    first = runner.trigger_for_events(events, plays, today=today)
    second = runner.trigger_for_events(events, plays, today=today)

    assert len(fake.submit_calls) == 1
    assert first[0].get("order_id") == second[0].get("order_id")

    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE event = ?",
            ("adverse_news",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


# ---------------------------------------------------------------------------
# 5) DB scan: ``scan_and_trigger`` picks up negative_material rows.
# ---------------------------------------------------------------------------


def test_scan_and_trigger_dispatches_from_db(
    make_runner, db_path: Path
) -> None:
    """Insert a row tagged ``negative_material`` directly into the DB and
    confirm the scan helper dispatches the exit."""
    fake = _FakeAlpacaClient()
    fake.queue(_stub_sell_response(qty=5))
    runner, _, _ = make_runner(client=fake)

    conn = db_module.connect(db_path)
    db_module.run_migrations(conn)
    conn.execute(
        "INSERT INTO news_events "
        "(ticker, source, title, ingested_at, enrichment_label) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "AXSM",
            "test_fixture",
            "AXSM CRL",
            "2025-04-27T08:00:00.000000Z",
            "negative_material",
        ),
    )
    conn.commit()
    conn.close()

    today = datetime.date(2025, 4, 27)
    results = runner.scan_and_trigger(
        [_active_play()],
        db_path=db_path,
        since_at="2025-04-27T00:00:00.000000Z",
        today=today,
    )
    assert len(results) == 1
    assert results[0]["status"] == "submitted"


# ---------------------------------------------------------------------------
# 6) Combined helper round-trip.
# ---------------------------------------------------------------------------


def test_record_negative_news_and_exit_round_trip(
    make_runner, db_path: Path
) -> None:
    """The convenience helper inserts a row + triggers the exit."""
    fake = _FakeAlpacaClient()
    fake.queue(_stub_sell_response(qty=5))
    _, executor, _ = make_runner(client=fake)

    today = datetime.date(2025, 4, 27)
    result = an.record_negative_news_and_exit(
        executor=executor,
        ticker="AXSM",
        title="AXSM bears CRL",
        active_plays=[_active_play()],
        db_path=db_path,
        today=today,
    )

    assert result.get("status") == "submitted"
    assert result.get("event") == "adverse_news"

    # The row landed in news_events.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        news_rows = conn.execute(
            "SELECT enrichment_label FROM news_events WHERE ticker = ?",
            ("AXSM",),
        ).fetchall()
    finally:
        conn.close()
    assert len(news_rows) == 1
    assert news_rows[0]["enrichment_label"] == "negative_material"
