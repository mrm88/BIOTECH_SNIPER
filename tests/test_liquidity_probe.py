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


# ---------------------------------------------------------------------------
# f-m3-20 — surgical fixes to the liquidity probe + size_entry surface.
# ---------------------------------------------------------------------------


def test_probe_daily_cap_pre_check_blocks_marginal_overshoot(
    db_path: Path,
) -> None:
    """f-m3-20 fix (1): pre-spend cap check uses estimated_cost.

    Pre-seed today_total = $19.00 and submit a probe whose
    ``limit_price`` implies an estimated_cost of $2.00 — the sum
    ($21.00) exceeds the daily cap ($20.00), so the probe MUST
    raise :class:`DailyCapExceeded` BEFORE the broker is called.

    The previous post-spend check (``spent >= cap``) would have
    permitted this probe to fire and pushed the day's total over
    the cap to $21 (the very invariant VAL-M3-066 forbids).
    """
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
                'preseed-19', 19.0
            )
            """,
        )
        conn.commit()
    finally:
        conn.close()

    fake = _FakeAlpacaClient(
        submit_order_results=[
            _broker_response(
                order_id="probe-blocked",
                status="filled",
                filled_qty=1,
                filled_avg_price=0.02,
            )
        ],
    )
    # limit_price=$0.02/share => estimated_cost = 0.02 * 100 = $2.00.
    # 19.0 + 2.0 = 21.0 > 20.0 cap → DailyCapExceeded.
    with pytest.raises(lp.DailyCapExceeded):
        lp.probe_chain(
            "AXSM",
            "2026-06-19",
            125.0,
            "buy",
            alpaca_client=fake,
            db_path=db_path,
            poll_interval_seconds=0.0,
            limit_price=0.02,
        )

    # Broker MUST NOT have been touched (pre-spend check fired
    # before the submit step).
    assert fake.submit_calls == []
    assert fake.cancel_calls == []
    # No fresh paper_orders row was persisted either — the
    # write-then-submit step also lives behind the cap check.
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE purpose = 'liquidity_probe'"
        ).fetchone()
    finally:
        conn.close()
    assert rows[0] == 0


def test_probe_daily_cap_pre_check_allows_within_budget(
    db_path: Path,
) -> None:
    """f-m3-20 fix (1): a probe that fits in the remaining budget runs.

    Today_total = $18.00; estimated_cost @ $0.01 = $1.00; 18+1 = 19
    which is *less than* $20 → probe proceeds as normal.
    """
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
                'preseed-18', 18.0
            )
            """,
        )
        conn.commit()
    finally:
        conn.close()

    fake = _FakeAlpacaClient(
        submit_order_results=[
            _broker_response(
                order_id="probe-allowed",
                status="filled",
                filled_qty=1,
                filled_avg_price=0.01,
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
        limit_price=0.01,
    )
    assert result.outcome == "filled"
    assert result.classification == "fillable"
    assert fake.submit_calls != []


def test_probe_default_effective_limit_is_conservative_per_share(
    db_path: Path,
) -> None:
    """f-m3-20 fix (1): default limit price is conservative ($5/contract).

    When ``limit_price`` is omitted, the probe uses
    :data:`CONSERVATIVE_PROBE_LIMIT_USD_PER_SHARE` (= $0.05) rather
    than the strike (which on a $125 strike would inflate the
    estimated_cost to $12_500 and trip the daily cap on a fresh DB).
    """
    fake = _FakeAlpacaClient(
        submit_order_results=[
            _broker_response(
                order_id="probe-default",
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
        # limit_price intentionally omitted → conservative default.
    )
    assert result.outcome == "filled"
    assert fake.submit_calls != []
    submitted = fake.submit_calls[0]
    # The order request carries the conservative default limit
    # rather than the strike. Compare to the constant directly so
    # this test breaks loudly if the default is later tuned.
    assert float(submitted.limit_price) == pytest.approx(
        lp.CONSERVATIVE_PROBE_LIMIT_USD_PER_SHARE
    )


def test_size_entry_uses_play_card_leg_symbol_for_put(db_path: Path) -> None:
    """f-m3-20 fix (2): probe symbol is sourced from option_legs[0].

    For a put-card candidate, the canonical OCC symbol on
    ``play_card['option_legs'][0]['symbol']`` encodes ``P`` (put).
    The previous code passed only ``candidate['symbol']`` (often
    missing) into :func:`probe_chain`, which then synthesised a
    CALL symbol via :func:`_build_probe_symbol` — wrong for puts.
    The fix forwards the leg symbol verbatim.
    """
    from biotech_sniper.paper_executor import PaperExecutor

    fake = _FakeAlpacaClient()
    executor = PaperExecutor(
        fake,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )

    captured_kwargs: dict[str, Any] = {}

    class _StubProbeModule:
        DailyCapExceeded = lp.DailyCapExceeded

        @staticmethod
        def probe_chain(*args: Any, **kwargs: Any) -> lp.ProbeResult:
            captured_kwargs.update(kwargs)
            return lp.ProbeResult(
                ticker="AAPL",
                expiry="2025-05-16",
                strike=150.0,
                side="buy",
                probe_size=1,
                outcome="filled",
                classification="fillable",
                cost_usd=120.0,
                time_to_fill_ms=500,
                client_order_id="stub",
                paper_order_id="stub",
                alpaca_order_id="ax-put-1",
            )

    put_symbol = "AAPL250516P00150000"
    candidate = {
        # Note: NO top-level ``symbol`` key — exercise the leg
        # fallback path explicitly.
        "ticker": "AAPL",
        "expiry": "2025-05-16",
        "strike": 150.0,
        "side": "buy",
        "play_card": {
            "play_card_id": "AAPL-put",
            "ticker": "AAPL",
            "option_legs": [
                {
                    "symbol": put_symbol,
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
    # The probe was called with the put-card OCC symbol — NOT a
    # synthesised call symbol from :func:`_build_probe_symbol`.
    assert captured_kwargs.get("symbol_override") == put_symbol


def test_size_entry_falls_back_to_candidate_symbol_when_no_legs(
    db_path: Path,
) -> None:
    """f-m3-20 fix (2): fallback to candidate['symbol'] when no legs."""
    from biotech_sniper.paper_executor import PaperExecutor

    fake = _FakeAlpacaClient()
    executor = PaperExecutor(
        fake,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )

    captured_kwargs: dict[str, Any] = {}

    class _StubProbeModule:
        DailyCapExceeded = lp.DailyCapExceeded

        @staticmethod
        def probe_chain(*args: Any, **kwargs: Any) -> lp.ProbeResult:
            captured_kwargs.update(kwargs)
            return lp.ProbeResult(
                ticker="AAPL",
                expiry="2025-05-16",
                strike=150.0,
                side="buy",
                probe_size=1,
                outcome="filled",
                classification="fillable",
                cost_usd=120.0,
                time_to_fill_ms=500,
                client_order_id="stub",
                paper_order_id="stub",
                alpaca_order_id="ax-fb",
            )

    candidate = {
        "ticker": "AAPL",
        "expiry": "2025-05-16",
        "strike": 150.0,
        "side": "buy",
        "symbol": "AAPL250516C00150000",
        # play_card omitted entirely — the fallback path must use
        # candidate['symbol'].
    }
    executor.size_entry(
        candidate, liquidity_probe_module=_StubProbeModule
    )
    assert (
        captured_kwargs.get("symbol_override") == "AAPL250516C00150000"
    )


def test_size_entry_truncates_two_leg_card_when_action_is_single(
    db_path: Path,
) -> None:
    """f-m3-20 fix (3): action='single' truncates a 2-leg card to 1 leg.

    A play card carrying 2 BUY legs but classified as ``fillable``
    by the probe (action=``single``) MUST be truncated to a single
    leg before downstream dispatch — otherwise
    :func:`_is_multi_strike` returns ``True`` and the executor
    fans out the entry across both strikes despite the probe's
    ``fillable`` classification. The cheaper of the two legs is
    kept (mirrors :func:`_maybe_demote_unfillable_to_single_leg`).
    """
    from biotech_sniper.paper_executor import PaperExecutor, _is_multi_strike

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
            "play_card_id": "AXSM-truncate",
            "ticker": "AXSM",
            "option_legs": [
                {
                    "symbol": "AXSM260620C00120000",
                    "side": "buy",
                    "qty": 1,
                    "bid": 1.10,
                    "ask": 1.30,  # mid 1.20 (more expensive)
                },
                {
                    "symbol": "AXSM260620C00130000",
                    "side": "buy",
                    "qty": 1,
                    "bid": 0.85,
                    "ask": 1.05,  # mid 0.95 (cheaper — kept)
                },
            ],
        },
    }
    decision = executor.size_entry(
        candidate, liquidity_probe_module=_StubProbeModule
    )
    assert decision["action"] == "single"
    assert decision["classification"] == "fillable"
    legs_out = decision["play_card"]["option_legs"]
    assert len(legs_out) == 1
    # Cheaper leg wins.
    assert legs_out[0]["symbol"] == "AXSM260620C00130000"
    # Now the play card's effective leg-count agrees with
    # ``_is_multi_strike``: single-leg → False.
    assert _is_multi_strike(decision["play_card"]) is False


def test_size_entry_warns_when_partial_classified_on_single_leg_card(
    db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """f-m3-20 fix (3): action='multi' on a 1-leg card → WARN + stay single.

    A probe that returns ``partial`` (action=``multi``) on a play
    card that only has 1 leg cannot be honoured — there is no
    second leg to split to. We log a WARNING and leave the card
    as a single-leg entry so :func:`_is_multi_strike` returns
    ``False`` and the executor's single-strike path runs.
    """
    from biotech_sniper.paper_executor import PaperExecutor, _is_multi_strike

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
            "play_card_id": "AXSM-partial-single",
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
    with caplog.at_level("WARNING", logger="biotech_sniper.paper_executor"):
        decision = executor.size_entry(
            candidate, liquidity_probe_module=_StubProbeModule
        )
    assert decision["action"] == "multi"
    # Card stays single-leg; downstream multi-strike helper agrees.
    assert len(decision["play_card"]["option_legs"]) == 1
    assert _is_multi_strike(decision["play_card"]) is False
    # WARN message surfaces for the operator.
    assert any(
        "partial_on_single_leg" in rec.message
        for rec in caplog.records
    )
