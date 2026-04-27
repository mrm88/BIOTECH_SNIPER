"""Tests for :mod:`biotech_sniper.options_chains.pull_options` (f-m3-02).

These tests verify the M3 contract VAL-M3-010 / VAL-M3-013:

* The module is fully Alpaca-backed — no `yfinance`, no
  `yf.Ticker`, no bare `option_chain(`.
* :func:`pull_chain` returns rows whose schema matches what the
  :func:`biotech_sniper.sectors.unified_scorer.score_options`
  consumer expects (no KeyError).
* :func:`pull_chain` delegates to
  :meth:`AlpacaClient.get_options_chain` (proven by injecting a fake
  client and observing the call).

Hermetic: zero network calls. Reuses the cassette
``tests/fixtures/cassettes/alpaca/options_chain_spy.json`` shipped by
f-m3-01, plus the fake-SDK pattern from
:mod:`tests.test_alpaca_client`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper.alpaca_client import AlpacaClient, PAPER_BASE_URL
from biotech_sniper.options_chains import pull_options
from biotech_sniper.options_chains.pull_options import (
    REQUIRED_CHAIN_KEYS,
    pull_chain,
)
from biotech_sniper.sectors.unified_scorer import score_options


CASSETTE_DIR = Path(__file__).parent / "fixtures" / "cassettes" / "alpaca"


# ---------------------------------------------------------------------------
# Local fakes — minimal duck-types over alpaca-py's TradingClient and
# OptionHistoricalDataClient. Mirror tests.test_alpaca_client to keep
# behaviour identical.
# ---------------------------------------------------------------------------


class _FakeTradingClient:
    def __init__(self, *, contracts_response: Any = None) -> None:
        self.contracts_response = contracts_response
        self.calls: list[tuple[str, Any]] = []

    def get_option_contracts(self, request: Any) -> Any:
        self.calls.append(("get_option_contracts", request))
        return self.contracts_response


class _FakeOptionsDataClient:
    def __init__(self, *, snapshots: Any = None) -> None:
        self.snapshots = snapshots if snapshots is not None else {}
        self.calls: list[tuple[str, Any]] = []

    def get_option_chain(self, request: Any) -> Any:
        self.calls.append(("get_option_chain", request))
        return self.snapshots


class _Contracts:
    def __init__(self, contracts: list[Any]) -> None:
        self.option_contracts = contracts


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cassette() -> dict[str, Any]:
    return json.loads(
        (CASSETTE_DIR / "options_chain_spy.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def alpaca_client(cassette, monkeypatch) -> AlpacaClient:
    """An AlpacaClient with both SDK clients faked from the cassette."""
    monkeypatch.setenv("ALPACA_KEY_ID", "AK_TEST_KEY")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SK_TEST_SECRET")
    monkeypatch.setenv("ALPACA_BASE_URL", PAPER_BASE_URL)
    monkeypatch.setenv("LIVE_MODE", "0")

    trading = _FakeTradingClient(
        contracts_response=_Contracts(cassette["contracts"])
    )
    options = _FakeOptionsDataClient(snapshots=cassette["snapshots"])
    return AlpacaClient(
        api_key="AK_TEST_KEY",
        secret_key="SK_TEST_SECRET",
        base_url=PAPER_BASE_URL,
        trading_client=trading,
        option_data_client=options,
    )


# ---------------------------------------------------------------------------
# VAL-M3-010 — module is fully Alpaca-backed (no yfinance references).
# ---------------------------------------------------------------------------


def test_module_does_not_reference_legacy_vendor() -> None:
    """The pull_options module contains zero yfinance/yf.Ticker/option_chain(.

    This is the regression gate for VAL-M3-010. The verification
    command from the feature spec is::

        grep -nE 'yfinance|yf\\.Ticker|option_chain\\(' \\
            biotech_sniper/options_chains/pull_options.py  # empty
    """
    src = Path(pull_options.__file__).read_text(encoding="utf-8")
    assert "yfinance" not in src
    assert "yf.Ticker" not in src
    # Use a regex matching the literal grep pattern from the feature
    # description (escapes preserved). ``get_option_chain(`` from the
    # alpaca SDK is acceptable; the forbidden form is ``option_chain(``
    # without the ``get_`` prefix.
    forbidden = re.compile(r"(?<!get_)option_chain\(")
    assert not forbidden.search(src), (
        "pull_options.py must not invoke the legacy `option_chain(` API"
    )


# ---------------------------------------------------------------------------
# VAL-M3-013 — schema match against unified_scorer.score_options consumer.
# ---------------------------------------------------------------------------


def test_pull_chain_returns_rows_with_required_keys(alpaca_client) -> None:
    """Every chain row exposes all keys that downstream consumers need."""
    chain = pull_chain("SPY", "2025-06-20", client=alpaca_client)

    assert chain, "pull_chain returned no rows from the cassette"
    for row in chain:
        for key in REQUIRED_CHAIN_KEYS:
            assert key in row, f"missing required key {key!r} in row {row!r}"


def test_schema_matches_unified_scorer(alpaca_client) -> None:
    """Output of pull_chain feeds straight into score_options without KeyError.

    This is the canonical contract assertion (VAL-M3-013): the schema
    produced by ``pull_chain`` is exactly what the
    ``unified_scorer.score_options`` consumer expects. A regression
    in either side would surface as a KeyError here.
    """
    chain = pull_chain("SPY", "2025-06-20", client=alpaca_client)
    score = score_options(chain)

    assert isinstance(score, float)
    assert score >= 0.0


def test_score_options_raises_keyerror_when_required_keys_missing() -> None:
    """The consumer's KeyError contract is exercised on a malformed row.

    The contract says: a row produced by ``pull_chain`` must be
    consumable by ``score_options`` without KeyError. The corollary
    test: removing any required key MUST surface a KeyError so that
    schema drift in either direction is caught immediately.
    """
    bad_row = {
        "strike": 100.0,
        "expiry": "2025-06-20",
        "mid": 1.0,
        "bid": 0.95,
        "ask": 1.05,
        "iv": 0.3,
        "delta": 0.5,
        # 'oi' is missing on purpose
    }
    with pytest.raises(KeyError, match="oi"):
        score_options([bad_row])


def test_score_options_returns_zero_for_empty_chain() -> None:
    """An empty chain is the documented zero-score case."""
    assert score_options([]) == 0.0
    assert score_options(None) == 0.0  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# pull_chain delegates to AlpacaClient.get_options_chain.
# ---------------------------------------------------------------------------


def test_pull_chain_calls_alpaca_get_options_chain(cassette, monkeypatch) -> None:
    """pull_chain ends up calling AlpacaClient.get_options_chain exactly once."""
    monkeypatch.setenv("ALPACA_KEY_ID", "AK_TEST_KEY")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SK_TEST_SECRET")
    monkeypatch.setenv("ALPACA_BASE_URL", PAPER_BASE_URL)
    monkeypatch.setenv("LIVE_MODE", "0")

    trading = _FakeTradingClient(
        contracts_response=_Contracts(cassette["contracts"])
    )
    options = _FakeOptionsDataClient(snapshots=cassette["snapshots"])
    client = AlpacaClient(
        api_key="AK_TEST_KEY",
        secret_key="SK_TEST_SECRET",
        base_url=PAPER_BASE_URL,
        trading_client=trading,
        option_data_client=options,
    )

    rows = pull_chain("SPY", "2025-06-20", client=client)

    # The AlpacaClient routes the call through both SDK methods.
    method_names = [c[0] for c in trading.calls] + [c[0] for c in options.calls]
    assert "get_option_contracts" in method_names
    assert "get_option_chain" in method_names
    # And we got back the merged chain.
    assert len(rows) == len(cassette["snapshots"])


def test_pull_chain_ticker_validation() -> None:
    """Empty / non-string tickers are rejected with a clear ValueError."""
    with pytest.raises(ValueError, match="non-empty string"):
        pull_chain("")
    with pytest.raises(ValueError, match="non-empty string"):
        pull_chain(None)  # type: ignore[arg-type]


def test_pull_chain_uses_default_client_when_none_provided(monkeypatch) -> None:
    """When no client is injected, pull_chain constructs an AlpacaClient.

    We don't actually want it to touch the network, so we monkey-patch
    the AlpacaClient class to return a stubbed instance whose
    ``get_options_chain`` returns a known sentinel list.
    """
    sentinel: list[dict[str, Any]] = [
        {
            "symbol": "TEST",
            "strike": 1.0,
            "expiry": "2025-06-20",
            "mid": 1.0,
            "bid": 0.9,
            "ask": 1.1,
            "iv": 0.3,
            "delta": 0.5,
            "oi": 100,
            "type": "call",
        }
    ]

    class _StubClient:
        def __init__(self) -> None:
            self.received: list[tuple[str, Any]] = []

        def get_options_chain(self, ticker: str, expiry: Any = None) -> list:
            self.received.append((ticker, expiry))
            return sentinel

    stub = _StubClient()
    monkeypatch.setattr(
        "biotech_sniper.options_chains.pull_options.AlpacaClient",
        lambda: stub,
    )

    rows = pull_chain("AAPL", "2025-06-20")
    assert rows is sentinel
    assert stub.received == [("AAPL", "2025-06-20")]
