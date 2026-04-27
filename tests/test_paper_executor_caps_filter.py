"""Tests for the f-m3-07b options-only cap filter.

Background
----------

The :class:`PaperExecutor` enforces two caps before submitting a
single-leg long call/put entry:

* concurrency cap — :data:`config.MAX_CONCURRENT_PLAYS` (default 3),
* deployed-capital cap — :data:`config.MAX_DEPLOYED_USD` (default
  $750).

Before f-m3-07b, both caps counted **every** Alpaca position the
broker reported, including shares held outside the M3 strategy. On
a real paper account that already holds 6 unrelated equities (e.g.
AMDL/AMZZ/CONL/LMT/NVDL/TSLL), the executor refused every fresh
options entry — the equity count alone exceeded
``MAX_CONCURRENT_PLAYS`` and the equity ``qty * avg * 100`` arithmetic
inflated the deployed-capital sum into the millions.

f-m3-07b filters the positions list to ``asset_class == 'us_option'``
before either cap arithmetic runs. The behaviours pinned here:

* equities (``asset_class == 'us_equity'``) are skipped from BOTH
  the concurrency count AND the deployed-capital sum,
* the cap still trips when 3+ option positions are open, regardless
  of any equity holdings,
* a position with no ``asset_class`` field at all is treated as
  non-option (skipped) — the conservative default.

Hermetic — no network. Fake :class:`AlpacaClient` doubles supply
``get_positions`` and ``submit_order`` so the cap-arithmetic paths
exercise without touching the broker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.paper_executor import (
    ConcurrencyCapExceeded,
    DeployedCapExceeded,
    PaperExecutor,
    _deployed_capital_usd,
    _options_positions,
)


# ---------------------------------------------------------------------------
# Fake Alpaca client mirroring the helpers used elsewhere in tests/.
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Duck-typed substitute exposing the methods PaperExecutor invokes."""

    def __init__(
        self,
        *,
        base_url: str = PAPER_BASE_URL,
        positions: list[dict[str, Any]] | None = None,
        submit_order_result: dict[str, Any] | None = None,
    ) -> None:
        self.base_url = base_url
        self._positions = list(positions or [])
        self._submit_result = submit_order_result or {
            "id": "order-id-fixture",
            "symbol": "PFE250620C00030000",
            "side": "buy",
            "qty": 2,
            "status": "accepted",
        }
        self.submit_calls: list[Any] = []
        self.get_positions_calls: int = 0

    def get_positions(self) -> list[dict[str, Any]]:
        self.get_positions_calls += 1
        return list(self._positions)

    def submit_order(self, order_request: Any) -> dict[str, Any]:
        self.submit_calls.append(order_request)
        result = dict(self._submit_result)
        result.setdefault("symbol", getattr(order_request, "symbol", None))
        if hasattr(order_request, "qty"):
            result["qty"] = order_request.qty
        return result

    def get_order(self, order_id: str) -> dict[str, Any]:  # pragma: no cover
        return self._submit_result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "alpha_sniper.db"


def _equity(symbol: str, qty: int = 100, avg: float = 50.0) -> dict[str, Any]:
    """Convenience: build a synthetic equity-share position payload."""
    return {
        "symbol": symbol,
        "qty": qty,
        "avg_entry_price": avg,
        "asset_class": "us_equity",
    }


def _option(
    symbol: str = "PFE250620C00030000",
    qty: int = 1,
    avg: float = 1.20,
) -> dict[str, Any]:
    """Convenience: build a synthetic options-contract position payload."""
    return {
        "symbol": symbol,
        "qty": qty,
        "avg_entry_price": avg,
        "asset_class": "us_option",
    }


def _play_card(*, bid: float = 1.00, ask: float = 1.40) -> dict[str, Any]:
    return {
        "play_card_id": "PFE-2026-04-27",
        "ticker": "PFE",
        "option_legs": [
            {
                "symbol": "PFE250620C00030000",
                "side": "buy",
                "bid": bid,
                "ask": ask,
                "limit_price": 1.30,
                "option_type": "call",
            }
        ],
    }


# ---------------------------------------------------------------------------
# _options_positions helper (unit-level)
# ---------------------------------------------------------------------------


def test_options_filter_returns_only_us_option_entries():
    positions = [
        _equity("AAPL", qty=10, avg=200.0),
        _option("PFE_C", qty=2, avg=1.20),
        _equity("MSFT", qty=5, avg=400.0),
        _option("AXSM_P", qty=1, avg=2.40),
    ]
    filtered = _options_positions(positions)
    assert [p["symbol"] for p in filtered] == ["PFE_C", "AXSM_P"]


def test_options_filter_handles_missing_asset_class():
    """Positions where ``asset_class`` is absent are treated as non-option.

    Mirrors the f-m3-07b spec line: "accept legacy ``asset_class``
    absent → treat as not-options (skip)". The conservative default
    avoids inflating cap counts on a broker payload whose schema
    drifted.
    """
    positions = [
        # No asset_class at all — must be skipped.
        {"symbol": "LEGACY1", "qty": 1, "avg_entry_price": 1.0},
        _option("PFE_C"),
        # Truthy but unrecognised — also skipped.
        {
            "symbol": "WEIRD",
            "qty": 1,
            "avg_entry_price": 1.0,
            "asset_class": "crypto",
        },
        # Asset class on an equity — skipped.
        _equity("LMT", qty=10, avg=460.0),
    ]
    filtered = _options_positions(positions)
    assert [p["symbol"] for p in filtered] == ["PFE_C"]


def test_options_filter_ignores_non_dict_entries():
    """Defensive: garbage list entries are silently skipped."""
    positions = [None, "not-a-dict", _option("PFE_C"), 42]
    filtered = _options_positions(positions)  # type: ignore[arg-type]
    assert [p["symbol"] for p in filtered] == ["PFE_C"]


def test_options_filter_accepts_uppercase_us_option():
    """Case-insensitive match — defends against any future enum casing."""
    positions = [
        {
            "symbol": "PFE_C",
            "qty": 1,
            "avg_entry_price": 1.20,
            "asset_class": "US_OPTION",
        }
    ]
    filtered = _options_positions(positions)
    assert len(filtered) == 1


# ---------------------------------------------------------------------------
# Concurrency-cap behaviour (asset-class filter applied)
# ---------------------------------------------------------------------------


def test_cap_skips_equities_when_counting_concurrency(db_path: Path):
    """3 equities + 0 options → submission proceeds (concurrency 0/3)."""
    fake = _FakeAlpacaClient(
        positions=[
            _equity("AMDL", qty=10, avg=12.0),
            _equity("CONL", qty=5, avg=8.0),
            _equity("LMT", qty=2, avg=460.0),
        ]
    )
    executor = PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]

    order_id = executor.execute(_play_card())

    assert order_id == "order-id-fixture"
    assert len(fake.submit_calls) == 1


def test_cap_blocks_when_3_options_open_alongside_equities(db_path: Path):
    """3 options + 5 equities → ConcurrencyCapExceeded (3 >= 3)."""
    fake = _FakeAlpacaClient(
        positions=[
            _option("OPT_A"),
            _option("OPT_B"),
            _option("OPT_C"),
            _equity("AMDL"),
            _equity("AMZZ"),
            _equity("CONL"),
            _equity("LMT"),
            _equity("NVDL"),
        ]
    )
    executor = PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]

    with pytest.raises(ConcurrencyCapExceeded) as excinfo:
        executor.execute(_play_card())

    # Exception message reports the FILTERED option count, not the
    # raw broker total — confirms the filter is wired into the
    # rejection path.
    assert "3 active option positions" in str(excinfo.value)
    assert fake.submit_calls == []


def test_cap_proceeds_when_2_options_plus_99_equities(db_path: Path):
    """2 options + 99 equities → proceeds (2 < 3 from filtered count)."""
    positions = [
        _option("OPT_A"),
        _option("OPT_B"),
    ] + [_equity(f"EQ{i}", qty=1, avg=10.0) for i in range(99)]

    fake = _FakeAlpacaClient(positions=positions)
    executor = PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]

    order_id = executor.execute(_play_card())
    assert order_id == "order-id-fixture"
    assert len(fake.submit_calls) == 1


# ---------------------------------------------------------------------------
# Deployed-capital cap behaviour (asset-class filter applied)
# ---------------------------------------------------------------------------


def test_cap_skips_equities_in_deployed_sum(db_path: Path):
    """1 equity (qty=1000, avg=$200) + 1 option (qty=2, avg=$1.20).

    Without the f-m3-07b filter, the deployed sum would be
    ``1000 * 200 * 100 + 2 * 1.20 * 100 = $20_000_240``, tripping
    DeployedCapExceeded on every entry. With the filter, only the
    option position counts → ``2 * 1.20 * 100 = $240`` deployed,
    which leaves room under the $750 cap and the entry proceeds.
    """
    fake = _FakeAlpacaClient(
        positions=[
            _equity("LMT", qty=1000, avg=200.0),
            _option("PFE_C", qty=2, avg=1.20),
        ]
    )
    executor = PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]

    # Sanity: pure helper sum on the FILTERED list is $240, not $20M+.
    filtered = _options_positions(fake._positions)
    assert _deployed_capital_usd(filtered) == pytest.approx(240.0)

    order_id = executor.execute(_play_card())
    assert order_id == "order-id-fixture"
    assert len(fake.submit_calls) == 1


def test_deployed_cap_still_trips_on_options_only_excess(db_path: Path):
    """Options-only positions still trip the deployed cap correctly.

    3 option contracts at $2.40 avg = $720 deployed. A new entry
    with mid $1.00 (qty=2 → $200 planned) pushes the sum to $920 >
    $750 → DeployedCapExceeded. Confirms the filter does not
    accidentally short-circuit the cap when option positions alone
    breach it.
    """
    fake = _FakeAlpacaClient(
        positions=[
            _option("OPT_A", qty=1, avg=2.40),
            _option("OPT_B", qty=1, avg=2.40),
            # Add an unrelated equity to confirm it's still ignored
            # by the deployed-cap arithmetic even on the rejection
            # path. Without the filter this position would dominate
            # the sum.
            _equity("LMT", qty=1000, avg=460.0),
        ]
    )
    executor = PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]

    # Concurrency: 2 options open → still under the 3-option cap.
    # Deployed: option-only sum = 1*240 + 1*240 = $480.
    # Planned cost for mid=$1.00 qty=2 = $200 → total $680 ≤ $750.
    order_id = executor.execute(_play_card(bid=0.80, ask=1.20))
    assert order_id == "order-id-fixture"
    assert fake.submit_calls  # broker reached on this happy path

    # Now push the option-only deployed sum above the cap.
    fake_blocking = _FakeAlpacaClient(
        positions=[
            _option("OPT_A", qty=2, avg=3.00),  # 2*300 = $600
            _option("OPT_B", qty=1, avg=1.50),  # 1*150 = $150
            _equity("LMT", qty=1000, avg=460.0),
        ]
    )
    executor2 = PaperExecutor(
        fake_blocking, db_path=db_path
    )  # type: ignore[arg-type]
    # Filtered deployed = $600 + $150 = $750. Planned $200 → $950 > $750.
    with pytest.raises(DeployedCapExceeded):
        executor2.execute(_play_card(bid=0.80, ask=1.20))
    assert fake_blocking.submit_calls == []


# ---------------------------------------------------------------------------
# Real-world VPS scenario reproduced (6 pre-existing equities, 0 options).
# ---------------------------------------------------------------------------


def test_six_pre_existing_equities_do_not_block_options_entry(db_path: Path):
    """Exact VPS reproducer: 6 unrelated equities, 0 option positions.

    Mirrors the user's paper account state (AMDL / AMZZ / CONL /
    LMT / NVDL / TSLL) that triggered the original
    ConcurrencyCapExceeded + DeployedCapExceeded false positives
    discovered during f-m3-07's VPS roundtrip. After f-m3-07b's
    filter, this exact broker payload no longer rejects fresh
    options entries.
    """
    fake = _FakeAlpacaClient(
        positions=[
            _equity("AMDL", qty=10, avg=12.0),
            _equity("AMZZ", qty=10, avg=15.0),
            _equity("CONL", qty=5, avg=8.0),
            _equity("LMT", qty=2, avg=460.0),
            _equity("NVDL", qty=20, avg=85.0),
            _equity("TSLL", qty=50, avg=11.0),
        ]
    )
    executor = PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]

    order_id = executor.execute(_play_card())
    assert order_id == "order-id-fixture"
    assert len(fake.submit_calls) == 1
