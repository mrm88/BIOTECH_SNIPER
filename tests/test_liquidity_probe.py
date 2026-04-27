"""Tests for :mod:`biotech_sniper.liquidity_probe` (M3 feature f-m3-12).

Exercises the probe lifecycle end-to-end against a hermetic
``_FakeAlpacaClient`` double — the same pattern used by
``tests/test_paper_executor_multistrike.py``. The double records
``submit_order``, ``get_order`` (poll) and ``cancel_order`` calls so
each test asserts the broker sequence the probe contract requires.

Validation contract assertions exercised here:

* **VAL-M3-060** — ``liquidity_probes`` schema (FK / enum / unique
  index) verified by inserting a row and querying back.
* **VAL-M3-065** — probe size is fixed at 1 contract and non-filled
  outcomes finalise within the 60s + 5s window.
* **VAL-M3-066** — daily ``SUM(cost_usd) <= 20``; submission refused
  when the cap is reached.
* **VAL-M3-067** — classification gates real-entry sizing
  (``unfillable`` → skip, ``partial`` → multi, ``fillable`` →
  single).
* **VAL-M3-068** — every ``paper_orders`` row originating from the
  probe carries ``purpose='liquidity_probe'``; no real-entry rows
  do.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper import config as _config
from biotech_sniper import db as _db_module
from biotech_sniper import liquidity_probe as lp
from biotech_sniper.alpaca_client import (
    AlpacaClientError,
    PAPER_BASE_URL,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Minimal duck-typed substitute for AlpacaClient.

    ``submit_order_results`` is a queue of dicts the fake returns
    from successive :meth:`submit_order` calls. ``submit_order_raises``
    optionally injects an :class:`AlpacaClientError` instead.
    ``get_order_results`` is a queue of dicts the fake returns from
    successive :meth:`get_order` calls — the test can model the
    poll loop's broker-side state machine deterministically.
    """

    def __init__(
        self,
        *,
        base_url: str = PAPER_BASE_URL,
        submit_order_results: list[dict[str, Any]] | None = None,
        submit_order_raises: Exception | None = None,
        get_order_results: list[dict[str, Any]] | None = None,
        cancel_raises: Exception | None = None,
    ) -> None:
        self.base_url = base_url
        self._submit_results = list(submit_order_results or [])
        self._submit_raises = submit_order_raises
        self._get_order_results = list(get_order_results or [])
        self._cancel_raises = cancel_raises
        self.submit_calls: list[Any] = []
        self.get_order_calls: list[str] = []
        self.cancel_calls: list[str] = []

    def submit_order(self, order_request: Any) -> dict[str, Any]:
        self.submit_calls.append(order_request)
        if self._submit_raises is not None:
            raise self._submit_raises
        if not self._submit_results:
            raise AssertionError(
                "_FakeAlpacaClient.submit_order called without queued result"
            )
        return self._submit_results.pop(0)

    def get_order(self, order_id: str) -> dict[str, Any]:
        self.get_order_calls.append(order_id)
        if not self._get_order_results:
            # Loop forever on the last status, simulating a broker
            # that never moves off the prior state.
            return {"id": order_id, "status": "accepted", "filled_qty": 0}
        return self._get_order_results.pop(0)

    def cancel_order(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)
        if self._cancel_raises is not None:
            raise self._cancel_raises


def _broker_response(
    *,
    order_id: str = "probe-1",
    status: str = "accepted",
    filled_qty: float = 0.0,
    filled_avg_price: float = 0.0,
) -> dict[str, Any]:
    return {
        "id": order_id,
        "client_order_id": "probe-coid",
        "symbol": "AXSM260620C00125000",
        "asset_class": "us_option",
        "qty": 1,
        "filled_qty": filled_qty,
        "filled_avg_price": filled_avg_price if filled_avg_price else None,
        "side": "buy",
        "status": status,
        "order_class": "simple",
        "order_type": "limit",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": 1.20,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Tmp SQLite path with the schema migrated up-front."""
    p = tmp_path / "alpha_sniper.db"
    conn = _db_module.connect(p)
    try:
        _db_module.run_migrations(conn)
    finally:
        conn.close()
    return p


# ---------------------------------------------------------------------------
# Schema (VAL-M3-060)
# ---------------------------------------------------------------------------


def test_liquidity_probes_table_schema(db_path: Path) -> None:
    """The migration creates ``liquidity_probes`` with the required cols."""
    conn = sqlite3.connect(db_path)
    try:
        cols = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(liquidity_probes)"
            ).fetchall()
        }
    finally:
        conn.close()
    required = {
        "id",
        "ticker",
        "expiry",
        "strike",
        "side",
        "probe_size",
        "submitted_at",
        "finalized_at",
        "outcome",
        "time_to_fill_ms",
        "classification",
        "client_order_id",
        "cost_usd",
    }
    assert required.issubset(cols), f"missing columns: {required - cols}"


def test_classify_outcome_mapping() -> None:
    """The classification map matches the documented spec exactly."""
    assert lp.classify_outcome("filled") == "fillable"
    assert lp.classify_outcome("partial") == "partial"
    assert lp.classify_outcome("unfilled") == "unfillable"
    assert lp.classify_outcome("rejected") == "unfillable"


# ---------------------------------------------------------------------------
# Filled probe → fillable
# ---------------------------------------------------------------------------


def test_probe_filled_returns_fillable(db_path: Path) -> None:
    """Submit returns 'filled' immediately → classification 'fillable'."""
    fake = _FakeAlpacaClient(
        submit_order_results=[
            _broker_response(
                order_id="probe-fill",
                status="filled",
                filled_qty=1,
                filled_avg_price=1.20,
            )
        ],
    )
    result = lp.probe_chain(
        "AXSM",
        "2026-06-19",
        125.0,
        "buy",
        alpaca_client=fake,
        db_path=db_path,
        poll_interval_seconds=0.0,
    )
    assert result.outcome == "filled"
    assert result.classification == "fillable"
    assert result.cost_usd == pytest.approx(1.20 * 100.0)
    # Probe size is always 1 contract per VAL-M3-065.
    assert result.probe_size == 1
    # Already filled at submit → no broker cancel needed.
    assert fake.cancel_calls == []
    assert len(fake.submit_calls) == 1

    # liquidity_probes row persisted with the expected outcome /
    # classification.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM liquidity_probes "
            "WHERE client_order_id = ?",
            (result.client_order_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["outcome"] == "filled"
    assert row["classification"] == "fillable"
    assert row["probe_size"] == 1
    assert row["cost_usd"] == pytest.approx(120.0)


def test_probe_paper_order_row_tagged_purpose_liquidity_probe(
    db_path: Path,
) -> None:
    """VAL-M3-068: the probe's parent paper_orders row carries the flag."""
    fake = _FakeAlpacaClient(
        submit_order_results=[
            _broker_response(
                order_id="probe-purpose",
                status="filled",
                filled_qty=1,
                filled_avg_price=1.00,
            )
        ],
    )
    result = lp.probe_chain(
        "AXSM",
        "2026-06-19",
        125.0,
        "buy",
        alpaca_client=fake,
        db_path=db_path,
        poll_interval_seconds=0.0,
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT purpose, status, side, qty FROM paper_orders "
            "WHERE client_order_id = ?",
            (result.client_order_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["purpose"] == "liquidity_probe"
    # No real-entry / exit rows accidentally inherit the flag — the
    # test db has only the probe row in it.
    conn = sqlite3.connect(db_path)
    try:
        purposes = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT purpose FROM paper_orders"
            ).fetchall()
        }
    finally:
        conn.close()
    assert purposes == {"liquidity_probe"}


# ---------------------------------------------------------------------------
# Unfilled probe (timeout) → unfillable
# ---------------------------------------------------------------------------


def test_probe_unfilled_canceled_within_60s(db_path: Path) -> None:
    """A probe that never fills is canceled within the timeout window."""
    fake = _FakeAlpacaClient(
        submit_order_results=[
            _broker_response(order_id="probe-unfilled", status="accepted")
        ],
        # After cancel, broker reports terminal "canceled" with no fills.
        get_order_results=[
            _broker_response(order_id="probe-unfilled", status="canceled"),
        ],
    )

    # Drive the monotonic clock forward past the deadline so the
    # poll loop hits the cancel branch deterministically.
    times = iter([0.0, 1000.0])

    def fake_monotonic() -> float:
        return next(times)

    result = lp.probe_chain(
        "AXSM",
        "2026-06-19",
        125.0,
        "buy",
        alpaca_client=fake,
        db_path=db_path,
        poll_interval_seconds=0.0,
        timeout_seconds=60.0,
        monotonic=fake_monotonic,
    )
    assert result.outcome == "unfilled"
    assert result.classification == "unfillable"
    assert result.cost_usd == 0.0
    # Cancel was issued exactly once on the broker.
    assert fake.cancel_calls == ["probe-unfilled"]


def test_probe_unfilled_finalizes_within_window(db_path: Path) -> None:
    """VAL-M3-065: finalized_at - submitted_at <= 65s for non-filled."""
    fake = _FakeAlpacaClient(
        submit_order_results=[
            _broker_response(order_id="probe-window", status="accepted")
        ],
        get_order_results=[
            _broker_response(order_id="probe-window", status="canceled"),
        ],
    )

    times = iter([0.0, 100.0])

    def fake_monotonic() -> float:
        return next(times)

    result = lp.probe_chain(
        "AXSM",
        "2026-06-19",
        125.0,
        "buy",
        alpaca_client=fake,
        db_path=db_path,
        poll_interval_seconds=0.0,
        timeout_seconds=60.0,
        monotonic=fake_monotonic,
    )
    assert result.outcome == "unfilled"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT submitted_at, finalized_at, "
            "(JULIANDAY(finalized_at) - JULIANDAY(submitted_at)) * 86400 "
            "AS window_seconds "
            "FROM liquidity_probes WHERE client_order_id = ?",
            (result.client_order_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    # The wall-clock between submit and finalize is the test's own
    # near-instant turnaround — the validator only allows up to 65s.
    assert 0.0 <= row["window_seconds"] <= 65.0


# ---------------------------------------------------------------------------
# Rejected probe → unfillable
# ---------------------------------------------------------------------------


def test_probe_rejected_classifies_unfillable(db_path: Path) -> None:
    """A broker rejection at submit → outcome=rejected → unfillable."""
    fake = _FakeAlpacaClient(
        submit_order_raises=AlpacaClientError("invalid symbol"),
    )
    result = lp.probe_chain(
        "AXSM",
        "2026-06-19",
        125.0,
        "buy",
        alpaca_client=fake,
        db_path=db_path,
        poll_interval_seconds=0.0,
    )
    assert result.outcome == "rejected"
    assert result.classification == "unfillable"
    assert result.cost_usd == 0.0
    assert fake.cancel_calls == []
    # The DB row landed with outcome='rejected'.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT outcome, classification FROM liquidity_probes "
            "WHERE client_order_id = ?",
            (result.client_order_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["outcome"] == "rejected"
    assert row["classification"] == "unfillable"


# ---------------------------------------------------------------------------
# Partial probe → partial classification (split)
# ---------------------------------------------------------------------------


def test_probe_partial_fill_classifies_partial(db_path: Path) -> None:
    """A broker partial fill at the deadline → outcome=partial."""
    fake = _FakeAlpacaClient(
        submit_order_results=[
            _broker_response(order_id="probe-partial", status="accepted")
        ],
        # After timeout-cancel, broker reports a partial fill: 0.5
        # contracts at $1.00. Classification → 'partial'.
        get_order_results=[
            _broker_response(
                order_id="probe-partial",
                status="canceled",
                filled_qty=0.5,
                filled_avg_price=1.00,
            ),
        ],
    )

    times = iter([0.0, 1000.0])

    def fake_monotonic() -> float:
        return next(times)

    result = lp.probe_chain(
        "AXSM",
        "2026-06-19",
        125.0,
        "buy",
        alpaca_client=fake,
        db_path=db_path,
        poll_interval_seconds=0.0,
        timeout_seconds=60.0,
        monotonic=fake_monotonic,
    )
    assert result.outcome == "partial"
    assert result.classification == "partial"
    # cost_usd reflects the 0.5-contract partial fill: 0.5 * 1.00 * 100.
    assert result.cost_usd == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Daily cap (VAL-M3-066)
# ---------------------------------------------------------------------------


def test_probe_daily_cap_refuses_submission(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When today's spend already meets the cap, refuse to submit."""
    # Pre-seed a row at exactly the cap so the next probe is denied.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO liquidity_probes (
                ticker, expiry, strike, side, probe_size,
                submitted_at, finalized_at, outcome,
                time_to_fill_ms, classification,
                client_order_id, cost_usd
            ) VALUES (
                'NVAX','2026-06-19',100.0,'buy',1,
                strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                'filled',1000,'fillable',
                'preseed', ?
            )
            """,
            (float(_config.LIQUIDITY_PROBE_DAILY_USD_CAP),),
        )
        conn.commit()
    finally:
        conn.close()

    fake = _FakeAlpacaClient()
    with pytest.raises(lp.DailyCapExceeded):
        lp.probe_chain(
            "AXSM",
            "2026-06-19",
            125.0,
            "buy",
            alpaca_client=fake,
            db_path=db_path,
            poll_interval_seconds=0.0,
        )
    # No broker call attempted.
    assert fake.submit_calls == []


def test_today_probe_spend_usd_helper(db_path: Path) -> None:
    """The cap helper sums today's cost_usd and returns a float."""
    assert lp.today_probe_spend_usd(db_path) == 0.0
    fake = _FakeAlpacaClient(
        submit_order_results=[
            _broker_response(
                order_id="cap-1",
                status="filled",
                filled_qty=1,
                filled_avg_price=0.05,
            )
        ],
    )
    lp.probe_chain(
        "AXSM",
        "2026-06-19",
        125.0,
        "buy",
        alpaca_client=fake,
        db_path=db_path,
        poll_interval_seconds=0.0,
    )
    spent = lp.today_probe_spend_usd(db_path)
    assert spent == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# size_entry wiring on PaperExecutor (VAL-M3-067)
# ---------------------------------------------------------------------------


def test_size_entry_unfillable_returns_skip(db_path: Path) -> None:
    """unfillable classification → action='skip', play_card=None."""
    from biotech_sniper.paper_executor import PaperExecutor

    fake = _FakeAlpacaClient()
    executor = PaperExecutor(
        fake,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )

    # Stub the probe module so we don't recurse into the real probe.
    class _StubProbeModule:
        DailyCapExceeded = lp.DailyCapExceeded

        @staticmethod
        def probe_chain(*args: Any, **kwargs: Any) -> lp.ProbeResult:
            return lp.ProbeResult(
                ticker="AXSM",
                expiry="2026-06-19",
                strike=125.0,
                side="buy",
                probe_size=1,
                outcome="unfilled",
                classification="unfillable",
                cost_usd=0.0,
                time_to_fill_ms=60000,
                client_order_id="stub",
                paper_order_id="stub",
                alpaca_order_id=None,
            )

    decision = executor.size_entry(
        {
            "ticker": "AXSM",
            "expiry": "2026-06-19",
            "strike": 125.0,
            "side": "buy",
        },
        liquidity_probe_module=_StubProbeModule,
    )
    assert decision["action"] == "skip"
    assert decision["classification"] == "unfillable"
    assert decision["play_card"] is None


def test_size_entry_fillable_returns_single(db_path: Path) -> None:
    """fillable classification → action='single', play_card forwarded."""
    from biotech_sniper.paper_executor import PaperExecutor

    fake = _FakeAlpacaClient()
    executor = PaperExecutor(
        fake,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )

    class _StubProbeModule:
        DailyCapExceeded = lp.DailyCapExceeded

        @staticmethod
        def probe_chain(*args: Any, **kwargs: Any) -> lp.ProbeResult:
            return lp.ProbeResult(
                ticker="AXSM",
                expiry="2026-06-19",
                strike=125.0,
                side="buy",
                probe_size=1,
                outcome="filled",
                classification="fillable",
                cost_usd=120.0,
                time_to_fill_ms=500,
                client_order_id="stub",
                paper_order_id="stub",
                alpaca_order_id="ax-1",
            )

    candidate = {
        "ticker": "AXSM",
        "expiry": "2026-06-19",
        "strike": 125.0,
        "side": "buy",
        "play_card": {
            "play_card_id": "AXSM-fillable",
            "ticker": "AXSM",
            "option_legs": [
                {
                    "symbol": "AXSM260620C00125000",
                    "side": "buy",
                    "qty": 1,
                    "limit_price": 1.20,
                }
            ],
        },
    }
    decision = executor.size_entry(
        candidate, liquidity_probe_module=_StubProbeModule
    )
    assert decision["action"] == "single"
    assert decision["classification"] == "fillable"
    assert decision["play_card"]["liquidity_classification"] == "fillable"
    # The leg list is preserved (single-strike full size).
    assert len(decision["play_card"]["option_legs"]) == 1


def test_size_entry_partial_returns_multi(db_path: Path) -> None:
    """partial classification → action='multi', card stamped 'partial'."""
    from biotech_sniper.paper_executor import PaperExecutor

    fake = _FakeAlpacaClient()
    executor = PaperExecutor(
        fake,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )

    class _StubProbeModule:
        DailyCapExceeded = lp.DailyCapExceeded

        @staticmethod
        def probe_chain(*args: Any, **kwargs: Any) -> lp.ProbeResult:
            return lp.ProbeResult(
                ticker="AXSM",
                expiry="2026-06-19",
                strike=125.0,
                side="buy",
                probe_size=1,
                outcome="partial",
                classification="partial",
                cost_usd=50.0,
                time_to_fill_ms=60000,
                client_order_id="stub",
                paper_order_id="stub",
                alpaca_order_id="ax-1",
            )

    candidate = {
        "ticker": "AXSM",
        "expiry": "2026-06-19",
        "strike": 125.0,
        "side": "buy",
        "play_card": {
            "play_card_id": "AXSM-partial",
            "ticker": "AXSM",
            "option_legs": [
                {
                    "symbol": "AXSM260620C00120000",
                    "side": "buy",
                    "qty": 1,
                    "limit_price": 1.20,
                },
                {
                    "symbol": "AXSM260620C00130000",
                    "side": "buy",
                    "qty": 1,
                    "limit_price": 0.95,
                },
            ],
        },
    }
    decision = executor.size_entry(
        candidate, liquidity_probe_module=_StubProbeModule
    )
    assert decision["action"] == "multi"
    assert decision["classification"] == "partial"
    assert decision["play_card"]["liquidity_classification"] == "partial"
    assert len(decision["play_card"]["option_legs"]) == 2
