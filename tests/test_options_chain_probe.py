"""f-m3-16: AlpacaBackedProbe nearest-expiry-within-60d regression tests.

Pin the contract from the f-m3-16 surgical fix:

* :class:`AlpacaBackedProbe` queries the chain with NO single
  ``expiration_date`` filter (which Alpaca treats as an exact
  match). Instead it fetches all chain rows and filters
  client-side to the inclusive window
  ``[today, today + PROBE_LOOKAHEAD_DAYS]``.
* When multiple expiries land inside the window, the probe selects
  the **NEAREST** one (smallest ``expiry - today`` delta). The
  selected contract is exposed via :meth:`nearest_contract` for
  callers that need it; :meth:`probe` returns ``True`` iff
  :meth:`nearest_contract` returns non-``None``.
* The 60-day cutoff is **inclusive** — a contract exactly on day
  ``today + 60d`` still counts as in-window.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from biotech_sniper.options_chain_probe import (
    AlpacaBackedProbe,
    PROBE_LOOKAHEAD_DAYS,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Records every ``get_options_chain`` call for assertions."""

    def __init__(
        self,
        *,
        chain: list[dict[str, Any]] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._chain = list(chain or [])
        self._raise_exc = raise_exc
        self.calls: list[tuple[str, str | None]] = []

    def get_options_chain(
        self, ticker: str, expiry: str | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((ticker, expiry))
        if self._raise_exc is not None:
            raise self._raise_exc
        return list(self._chain)


def _row(*, expiry: str, strike: float, symbol: str | None = None) -> dict[str, Any]:
    """Build a chain row of the shape ``AlpacaClient.get_options_chain`` emits."""
    return {
        "symbol": symbol or f"AXSM{expiry.replace('-', '')[:6]}C{int(strike * 1000):08d}",
        "strike": float(strike),
        "expiry": expiry,
        "type": "call",
        "bid": 1.0,
        "ask": 1.1,
        "mid": 1.05,
        "iv": 0.30,
        "delta": 0.45,
        "oi": 100,
    }


# ---------------------------------------------------------------------------
# Nearest-expiry-within-60d (f-m3-16 fix #1)
# ---------------------------------------------------------------------------


def test_nearest_contract_selects_30d_over_45d_and_90d():
    """Chain with expiries +30d, +45d, +90d → +30d selected, not +60d nor +90d.

    Pins the f-m3-16 fix: previously the probe sent
    ``target_expiry=today+60d`` as an EXACT filter (no contracts
    exactly on day 60 → empty broker response → False answer for a
    ticker that actually had +30d and +45d contracts available).
    The new probe fetches all rows, filters to the inclusive 60-day
    window, and picks the nearest expiry.
    """
    today = datetime.date.today()
    iso_30 = (today + datetime.timedelta(days=30)).isoformat()
    iso_45 = (today + datetime.timedelta(days=45)).isoformat()
    iso_90 = (today + datetime.timedelta(days=90)).isoformat()

    rows = [
        _row(expiry=iso_45, strike=130.0, symbol="AXSM-45D"),
        _row(expiry=iso_90, strike=140.0, symbol="AXSM-90D"),
        _row(expiry=iso_30, strike=120.0, symbol="AXSM-30D"),
    ]
    fake = _FakeAlpacaClient(chain=rows)
    probe = AlpacaBackedProbe(client=fake)

    selected = probe.nearest_contract("AXSM")
    assert selected is not None
    assert selected["symbol"] == "AXSM-30D"
    assert selected["expiry"] == iso_30

    # And the bool API answers True (because nearest is non-None).
    assert probe.probe("AXSM") is True


def test_probe_does_not_pass_exact_expiry_filter_to_client():
    """The probe must fetch the chain UNFILTERED (expiry=None), not pin to day-60.

    Pins the root-cause of the f-m3-16 fix: the previous
    implementation called
    ``client.get_options_chain(symbol, today+60d.isoformat())``
    which Alpaca's ``OptionChainRequest`` treats as an EXACT
    expiration_date match. The new implementation must pass
    ``expiry=None`` so the broker returns whatever near-term
    contracts it has, and the probe filters client-side to the
    60-day window.
    """
    today = datetime.date.today()
    iso_30 = (today + datetime.timedelta(days=30)).isoformat()
    fake = _FakeAlpacaClient(
        chain=[_row(expiry=iso_30, strike=120.0, symbol="AXSM-30D")]
    )
    probe = AlpacaBackedProbe(client=fake)

    probe.probe("AXSM")

    assert len(fake.calls) == 1
    ticker_arg, expiry_arg = fake.calls[0]
    assert ticker_arg == "AXSM"
    assert expiry_arg is None, (
        f"probe must not pre-filter expiry; got expiry_arg={expiry_arg!r}. "
        "Passing an exact-day filter is the f-m3-16 regression."
    )


def test_60d_cutoff_is_inclusive():
    """A contract exactly on day ``today + 60d`` counts as in-window.

    The probe contract states the 60d cutoff is INCLUSIVE — a
    contract ``today + PROBE_LOOKAHEAD_DAYS`` from now is
    tradeable. This test pins that off-by-one boundary explicitly.
    """
    today = datetime.date.today()
    on_cutoff = (today + datetime.timedelta(days=PROBE_LOOKAHEAD_DAYS)).isoformat()

    rows = [_row(expiry=on_cutoff, strike=120.0, symbol="AXSM-CUTOFF")]
    fake = _FakeAlpacaClient(chain=rows)
    probe = AlpacaBackedProbe(client=fake)

    assert probe.probe("AXSM") is True
    selected = probe.nearest_contract("AXSM")
    assert selected is not None
    assert selected["expiry"] == on_cutoff


def test_returns_false_when_all_expiries_are_past_cutoff():
    """All expiries strictly outside the window → probe returns False."""
    today = datetime.date.today()
    iso_70 = (today + datetime.timedelta(days=PROBE_LOOKAHEAD_DAYS + 10)).isoformat()
    iso_90 = (today + datetime.timedelta(days=PROBE_LOOKAHEAD_DAYS + 30)).isoformat()

    rows = [
        _row(expiry=iso_70, strike=120.0, symbol="AXSM-70D"),
        _row(expiry=iso_90, strike=130.0, symbol="AXSM-90D"),
    ]
    fake = _FakeAlpacaClient(chain=rows)
    probe = AlpacaBackedProbe(client=fake)

    assert probe.probe("AXSM") is False
    assert probe.nearest_contract("AXSM") is None


def test_nearest_contract_returns_none_on_transport_error():
    """Broker exception is swallowed → probe degrades to no-chain (False)."""
    fake = _FakeAlpacaClient(raise_exc=RuntimeError("alpaca 502"))
    probe = AlpacaBackedProbe(client=fake)

    assert probe.nearest_contract("BOOM") is None
    assert probe.probe("BOOM") is False


def test_nearest_contract_returns_none_on_empty_chain():
    """Empty broker payload → no contract → probe returns False."""
    fake = _FakeAlpacaClient(chain=[])
    probe = AlpacaBackedProbe(client=fake)

    assert probe.nearest_contract("EMPTY") is None
    assert probe.probe("EMPTY") is False
