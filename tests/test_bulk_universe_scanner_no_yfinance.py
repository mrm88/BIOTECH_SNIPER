"""Regression test for f-m3-13 bulk_universe_scanner yfinance fallout.

After f-m3-02 dropped the yfinance branch from
``biotech_sniper.intelligence.bulk_universe_scanner.filter_and_score``,
the function still referenced three locals (``price``, ``mktcap``,
``opts``) that no longer existed, raising ``NameError`` for every
ticker that survived the chain probe. f-m3-13 removes those keys
from the scored row and uses the Alpaca-backed ``chain`` rows to
derive ``n_expiries`` instead.

This test pins that contract:

* ``filter_and_score`` runs end-to-end on a single AAPL-shape ticker
  without raising ``NameError``.
* The returned row contains the expected non-yfinance keys
  (``pre_score``, ``tier``, ``last_checked``, ``n_expiries``).
* ``price`` and ``market_cap`` are explicitly ``None`` (or absent
  by intent) — the M3 selection layer is the new arbiter for those
  fields, so the bulk scanner must not silently inject stale values.
"""

from __future__ import annotations

import datetime
import importlib

import pytest


@pytest.fixture
def _stub_pull_chain(monkeypatch):
    """Stub ``options_chains.pull_options.pull_chain`` to a known shape.

    The stub returns three rows with two distinct expiries so we can
    assert that ``filter_and_score`` derives ``n_expiries`` from the
    chain (rather than from the long-dead ``opts`` local).
    """
    stub_chain = [
        {
            "symbol": "AAPL250620C00190000",
            "strike": 190.0,
            "expiry": "2025-06-20",
            "mid": 5.0,
            "bid": 4.9,
            "ask": 5.1,
            "iv": 0.3,
            "delta": 0.5,
            "oi": 100,
            "type": "call",
        },
        {
            "symbol": "AAPL250620P00190000",
            "strike": 190.0,
            "expiry": "2025-06-20",
            "mid": 4.8,
            "bid": 4.7,
            "ask": 4.9,
            "iv": 0.3,
            "delta": -0.5,
            "oi": 80,
            "type": "put",
        },
        {
            "symbol": "AAPL250718C00200000",
            "strike": 200.0,
            "expiry": "2025-07-18",
            "mid": 3.5,
            "bid": 3.4,
            "ask": 3.6,
            "iv": 0.32,
            "delta": 0.4,
            "oi": 50,
            "type": "call",
        },
    ]

    # Patch the function the scanner actually imports lazily.
    from biotech_sniper.options_chains import pull_options as pull_options_mod

    monkeypatch.setattr(pull_options_mod, "pull_chain", lambda *_a, **_kw: list(stub_chain))
    return stub_chain


def test_filter_and_score_aapl_shape_no_name_error(_stub_pull_chain, monkeypatch):
    """``filter_and_score`` runs cleanly on a single AAPL-shape ticker.

    Pre-f-m3-13 this assertion failed with ``NameError: name 'price'
    is not defined`` because the scored-row dict referenced three
    locals removed by the f-m3-02 yfinance teardown.
    """
    # Sleep is not needed for the test; patch it out so the suite
    # stays fast even if the scanner iterates over many candidates.
    bulk_universe_scanner = importlib.import_module(
        "biotech_sniper.intelligence.bulk_universe_scanner"
    )
    monkeypatch.setattr(bulk_universe_scanner.time, "sleep", lambda *_: None)

    candidates = {
        "AAPL": {
            "ticker": "AAPL",
            "nct_id": None,
            "sponsor": "Apple Inc.",
            "primary_completion": "2026-06-15",
            "pdufa_date": None,
            "conditions": "test",
            "indication": "test",
            "phase": "PHASE3",
            "enrollment": 100,
            "source": "research",
            "has_pdufa": False,
        },
    }

    today = datetime.date(2026, 4, 27)
    result = bulk_universe_scanner.filter_and_score(candidates, today)

    assert "AAPL" in result, "filter_and_score dropped AAPL row unexpectedly"
    row = result["AAPL"]

    # The fields that survived the f-m3-02 teardown.
    assert "pre_score" in row, "pre_score must be on the scored row"
    assert "tier" in row, "tier must be on the scored row"
    assert row.get("last_checked") == today.isoformat()
    assert row.get("n_expiries") == 2, (
        "n_expiries must be derived from the Alpaca chain rows "
        "(two distinct expiries in the stub)"
    )

    # The yfinance-backed fields must be None — they are the M3
    # selection layer's responsibility now.
    assert row.get("price") is None, (
        "filter_and_score must not fabricate a price after f-m3-02"
    )
    assert row.get("market_cap") is None, (
        "filter_and_score must not fabricate a market_cap after f-m3-02"
    )

    # Sanity: the row carried through the input fields too.
    assert row["ticker"] == "AAPL"
    assert row["phase"] == "PHASE3"


def test_filter_and_score_handles_empty_chain(monkeypatch):
    """Tickers whose chain probe returns empty are dropped, not exploded."""
    from biotech_sniper.options_chains import pull_options as pull_options_mod

    monkeypatch.setattr(pull_options_mod, "pull_chain", lambda *_a, **_kw: [])

    bulk_universe_scanner = importlib.import_module(
        "biotech_sniper.intelligence.bulk_universe_scanner"
    )
    monkeypatch.setattr(bulk_universe_scanner.time, "sleep", lambda *_: None)

    result = bulk_universe_scanner.filter_and_score(
        {"NOPE": {"ticker": "NOPE", "phase": "PHASE2"}},
        datetime.date(2026, 4, 27),
    )
    assert result == {}, "Tickers without chain rows must be dropped"
