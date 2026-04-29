"""Tests for :mod:`biotech_sniper.alpaca_client`.

These tests are fully hermetic: the live Alpaca paper / live API is
never hit. We replay hand-crafted cassettes from
``tests/fixtures/cassettes/alpaca/`` through tiny fake SDK clients
that emulate :class:`alpaca.trading.client.TradingClient` and
:class:`alpaca.data.historical.option.OptionHistoricalDataClient`.

Why not vcrpy: alpaca-py's pydantic models are constructed from the
parsed HTTP body, so injecting body fixtures works equally well. The
pattern mirrors :mod:`tests.test_claude_client` and
:mod:`tests.test_xai_client` (cassette-driven SDK doubles).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from biotech_sniper import alpaca_client as ac
from biotech_sniper.alpaca_client import (
    AlpacaAuthError,
    AlpacaClient,
    AlpacaClientError,
    AlpacaTransportError,
    LiveTradingBlockedError,
    LIVE_BASE_URL,
    PAPER_BASE_URL,
)


CASSETTE_DIR = Path(__file__).parent / "fixtures" / "cassettes" / "alpaca"


# ---------------------------------------------------------------------------
# Cassette helpers
# ---------------------------------------------------------------------------


def _load_cassette(name: str) -> dict[str, Any]:
    return json.loads((CASSETTE_DIR / name).read_text(encoding="utf-8"))


def _make_fake_api_error(status_code: int, message: str) -> APIError:
    """Build an APIError-shaped exception with a fixed ``status_code``."""

    class _StatusedAPIError(APIError):
        def __init__(self, status: int, msg: str):
            super().__init__(json.dumps({"code": status, "message": msg}))
            self._fixed_status = status

        @property
        def status_code(self) -> int:  # type: ignore[override]
            return self._fixed_status

    return _StatusedAPIError(status_code, message)


# ---------------------------------------------------------------------------
# Fake SDK client doubles
# ---------------------------------------------------------------------------


class _FakeTradingClient:
    """Minimal duck-type substitute for ``alpaca.trading.client.TradingClient``.

    Each method either returns a pre-canned dict or raises a queued
    exception. Tests build the fake by passing keyword overrides.
    """

    def __init__(
        self,
        *,
        account: Any = None,
        positions: Any = None,
        contracts_response: Any = None,
        submit_order_result: Any = None,
        submit_order_results: Any = None,
        get_order_result: Any = None,
        get_order_results: Any = None,
        errors: Any = None,
    ):
        self.account = account
        self.positions = positions or []
        self.contracts_response = contracts_response
        self._submit_results = (
            list(submit_order_results)
            if submit_order_results is not None
            else ([submit_order_result] if submit_order_result is not None else [])
        )
        self._get_order_results = (
            list(get_order_results)
            if get_order_results is not None
            else ([get_order_result] if get_order_result is not None else [])
        )
        self._errors = errors or {}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _maybe_raise(self, method: str) -> None:
        if method in self._errors:
            err = self._errors[method]
            # If the error is a list/queue, pop the next one.
            if isinstance(err, list):
                if err:
                    raise err.pop(0)
            else:
                raise err

    def get_account(self) -> Any:
        self.calls.append(("get_account", (), {}))
        self._maybe_raise("get_account")
        return self.account

    def get_all_positions(self) -> list[Any]:
        self.calls.append(("get_all_positions", (), {}))
        self._maybe_raise("get_all_positions")
        return list(self.positions)

    def submit_order(self, order_request: Any) -> Any:
        self.calls.append(("submit_order", (order_request,), {}))
        self._maybe_raise("submit_order")
        if not self._submit_results:
            raise AssertionError("no submit_order result queued")
        return self._submit_results.pop(0)

    def get_order_by_id(self, order_id: str) -> Any:
        self.calls.append(("get_order_by_id", (order_id,), {}))
        self._maybe_raise("get_order_by_id")
        if not self._get_order_results:
            raise AssertionError("no get_order_by_id result queued")
        return self._get_order_results.pop(0)

    def cancel_order_by_id(self, order_id: str) -> None:
        self.calls.append(("cancel_order_by_id", (order_id,), {}))
        self._maybe_raise("cancel_order_by_id")

    def get_option_contracts(self, request: Any) -> Any:
        self.calls.append(("get_option_contracts", (request,), {}))
        self._maybe_raise("get_option_contracts")
        return self.contracts_response


class _FakeOptionsDataClient:
    """Minimal duck-type substitute for ``OptionHistoricalDataClient``."""

    def __init__(self, *, snapshots: Any = None, errors: Any = None):
        self.snapshots = snapshots if snapshots is not None else {}
        self._errors = errors or {}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def get_option_chain(self, request: Any) -> Any:
        self.calls.append(("get_option_chain", (request,), {}))
        if "get_option_chain" in self._errors:
            raise self._errors["get_option_chain"]
        return self.snapshots


# Wrap dicts in a tiny attribute-accessor so the wrapper helpers see
# both pydantic-like attributes and dict-key access. Cassette JSON
# parses to plain dicts; the wrapper's `_attr` helper already supports
# dict access, so we simply pass dicts through.
class _Contracts:
    """Container mimicking the SDK's option-contracts response object."""

    def __init__(self, contracts: list[Any]):
        self.option_contracts = contracts


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def paper_env(monkeypatch):
    """Default paper-mode env: keys present, base URL = paper, LIVE_MODE off."""
    monkeypatch.setenv("ALPACA_KEY_ID", "AKTEST_KEY_ID_VISIBLE_FOR_LEAK_CHECK")
    monkeypatch.setenv(
        "ALPACA_SECRET_KEY", "SK_TEST_SECRET_VISIBLE_FOR_LEAK_CHECK_XYZ"
    )
    monkeypatch.setenv("ALPACA_BASE_URL", PAPER_BASE_URL)
    monkeypatch.setenv("LIVE_MODE", "0")
    # Force config to re-read the env values rather than its cached
    # module-level LIVE_MODE constant. We expose the bool via a
    # patched attribute since LIVE_MODE is a Final[bool] cached at
    # import time.
    monkeypatch.setattr(ac.config, "LIVE_MODE", False)
    return monkeypatch


@pytest.fixture
def make_client(paper_env):
    """Factory: build an AlpacaClient with injected fake SDK clients."""

    def _factory(
        *,
        trading_client: Any = None,
        option_data_client: Any = None,
        base_url: str = PAPER_BASE_URL,
    ) -> AlpacaClient:
        return AlpacaClient(
            api_key="AKTEST_KEY_ID_VISIBLE_FOR_LEAK_CHECK",
            secret_key="SK_TEST_SECRET_VISIBLE_FOR_LEAK_CHECK_XYZ",
            base_url=base_url,
            trading_client=trading_client or _FakeTradingClient(),
            option_data_client=option_data_client or _FakeOptionsDataClient(),
        )

    return _factory


# ---------------------------------------------------------------------------
# VAL-M3-001 / VAL-M3-002: Default construction targets paper URL via SDK.
# ---------------------------------------------------------------------------


def test_default_construction_targets_paper_url(paper_env):
    """Default-constructed client points at paper URL exactly (VAL-M3-001)."""
    client = AlpacaClient(
        trading_client=_FakeTradingClient(),
        option_data_client=_FakeOptionsDataClient(),
    )
    assert client.base_url == PAPER_BASE_URL


def test_module_imports_alpaca_py_sdk():
    """alpaca-py is the underlying transport (VAL-M3-002).

    Greppable proof: the module imports from alpaca.trading.client and
    alpaca.data.historical.option, and contains no hand-rolled
    ``requests.post(...alpaca``/``requests.get(...alpaca`` call.
    """
    src = Path(ac.__file__).read_text(encoding="utf-8")
    assert "from alpaca.trading.client import TradingClient" in src
    assert (
        "from alpaca.data.historical.option import OptionHistoricalDataClient"
        in src
    )
    # No bare requests.{post,get}(... that targets the alpaca host.
    import re
    assert not re.search(r"requests\.(post|get)\([^)]*alpaca", src)


# ---------------------------------------------------------------------------
# VAL-M3-003: get_account() returns required fields.
# ---------------------------------------------------------------------------


def test_get_account_returns_required_fields(make_client):
    """get_account() returns dict with equity/buying_power/cash (VAL-M3-003)."""
    cassette = _load_cassette("account_success.json")
    trading = _FakeTradingClient(account=cassette["result"])
    client = make_client(trading_client=trading)

    account = client.get_account()

    for k in ("equity", "buying_power", "cash"):
        assert k in account, f"missing {k!r} in account"
        assert isinstance(account[k], float)
        assert account[k] >= 0
    # Pinned values from the cassette so regressions are caught.
    assert account["equity"] == 100000.0
    assert account["buying_power"] == 200000.0
    assert account["cash"] == 100000.0


def test_get_account_calls_trading_client_once(make_client):
    cassette = _load_cassette("account_success.json")
    trading = _FakeTradingClient(account=cassette["result"])
    client = make_client(trading_client=trading)

    client.get_account()

    assert [call[0] for call in trading.calls] == ["get_account"]


# ---------------------------------------------------------------------------
# VAL-M3-004: get_options_chain returns expected schema.
# ---------------------------------------------------------------------------


def test_options_chain_schema(make_client):
    """get_options_chain rows expose strike/expiry/mid/bid/ask/iv/delta/oi/type."""
    cassette = _load_cassette("options_chain_spy.json")
    trading = _FakeTradingClient(
        contracts_response=_Contracts(cassette["contracts"])
    )
    options = _FakeOptionsDataClient(snapshots=cassette["snapshots"])
    client = make_client(trading_client=trading, option_data_client=options)

    chain = client.get_options_chain("SPY", "2025-06-20")

    assert len(chain) == 4
    for row in chain:
        for key in (
            "strike",
            "expiry",
            "mid",
            "bid",
            "ask",
            "iv",
            "delta",
            "oi",
            "type",
        ):
            assert key in row, f"missing {key!r} in row {row!r}"
        assert row["type"] in {"call", "put"}
        assert row["strike"] > 0
        assert row["bid"] >= 0
        assert row["ask"] >= 0
        # bid <= mid <= ask (VAL-M3-004 explicit value-bound assertion).
        if row["mid"] is not None:
            assert row["bid"] <= row["mid"] <= row["ask"]


def test_options_chain_merges_contract_metadata_and_snapshot(make_client):
    """The merge correctly pairs contract OI with snapshot bid/ask/IV/delta."""
    cassette = _load_cassette("options_chain_spy.json")
    trading = _FakeTradingClient(
        contracts_response=_Contracts(cassette["contracts"])
    )
    options = _FakeOptionsDataClient(snapshots=cassette["snapshots"])
    client = make_client(trading_client=trading, option_data_client=options)

    chain = client.get_options_chain("SPY", "2025-06-20")
    by_symbol = {r["symbol"]: r for r in chain}

    call500 = by_symbol["SPY250620C00500000"]
    assert call500["type"] == "call"
    assert call500["strike"] == 500.0
    assert call500["expiry"] == "2025-06-20"
    assert call500["bid"] == 12.30
    assert call500["ask"] == 12.60
    assert call500["mid"] == pytest.approx((12.30 + 12.60) / 2)
    assert call500["iv"] == 0.182
    assert call500["delta"] == 0.55
    assert call500["oi"] == 1234

    put490 = by_symbol["SPY250620P00490000"]
    assert put490["type"] == "put"
    assert put490["strike"] == 490.0
    assert put490["delta"] == -0.32
    assert put490["oi"] == 1500


def test_options_chain_skips_symbols_without_matching_contract(make_client):
    """Snapshot entries without a contract counterpart are skipped."""
    cassette = _load_cassette("options_chain_spy.json")
    # Drop one contract so its snapshot becomes orphaned.
    contracts = [
        c
        for c in cassette["contracts"]
        if c["symbol"] != "SPY250620P00490000"
    ]
    trading = _FakeTradingClient(contracts_response=_Contracts(contracts))
    options = _FakeOptionsDataClient(snapshots=cassette["snapshots"])
    client = make_client(trading_client=trading, option_data_client=options)

    chain = client.get_options_chain("SPY", "2025-06-20")
    syms = {r["symbol"] for r in chain}
    assert "SPY250620P00490000" not in syms
    assert len(chain) == 3


# ---------------------------------------------------------------------------
# VAL-M3-005: API keys never logged.
# ---------------------------------------------------------------------------


def test_api_keys_redacted_in_logs(make_client, caplog):
    """At DEBUG level no log line contains the literal key/secret."""
    cassette = _load_cassette("account_success.json")
    trading = _FakeTradingClient(account=cassette["result"])

    caplog.set_level(logging.DEBUG, logger="biotech_sniper.alpaca_client")
    client = make_client(trading_client=trading)
    client.get_account()
    client.get_positions()

    # Concatenate all captured log lines for a single grep.
    full_log = "\n".join(record.getMessage() for record in caplog.records)
    # Plus the formatted log lines (covers %s substitutions).
    full_log += "\n" + "\n".join(record.message for record in caplog.records)

    assert "AKTEST_KEY_ID_VISIBLE_FOR_LEAK_CHECK" not in full_log
    assert "SK_TEST_SECRET_VISIBLE_FOR_LEAK_CHECK_XYZ" not in full_log
    assert "***" in full_log  # redaction marker present


def test_module_source_does_not_log_secret_values():
    """Static check: no f-string in alpaca_client.py logs the raw secret."""
    src = Path(ac.__file__).read_text(encoding="utf-8")
    # Ensure no logger call directly interpolates `_api_key` or
    # `_secret_key` without going through the redactor. (We grep for
    # the most likely leak shapes.)
    forbidden_patterns = [
        "logger.info(self._api_key",
        "logger.debug(self._api_key",
        "logger.warning(self._api_key",
        "logger.info(self._secret_key",
        "logger.debug(self._secret_key",
        "logger.warning(self._secret_key",
    ]
    for pattern in forbidden_patterns:
        assert pattern not in src, f"forbidden leak pattern in source: {pattern}"


# ---------------------------------------------------------------------------
# VAL-M3-006: Network/auth errors surface as typed exceptions.
# ---------------------------------------------------------------------------


def test_401_raises_alpaca_auth_error(make_client):
    """Alpaca 401 → AlpacaAuthError (subclass of AlpacaClientError)."""
    err = _make_fake_api_error(401, "unauthorized")
    trading = _FakeTradingClient(errors={"get_account": err})
    client = make_client(trading_client=trading)

    with pytest.raises(AlpacaAuthError) as excinfo:
        client.get_account()

    assert isinstance(excinfo.value, AlpacaClientError)
    assert "401" in str(excinfo.value)


def test_403_raises_alpaca_auth_error(make_client):
    err = _make_fake_api_error(403, "forbidden")
    trading = _FakeTradingClient(errors={"get_account": err})
    client = make_client(trading_client=trading)

    with pytest.raises(AlpacaAuthError):
        client.get_account()


def test_503_raises_alpaca_transport_error(make_client):
    """Alpaca 5xx → AlpacaTransportError."""
    err = _make_fake_api_error(503, "service unavailable")
    trading = _FakeTradingClient(errors={"get_account": err})
    client = make_client(trading_client=trading)

    with pytest.raises(AlpacaTransportError) as excinfo:
        client.get_account()
    assert isinstance(excinfo.value, AlpacaClientError)
    assert "503" in str(excinfo.value)


def test_connection_error_raises_alpaca_transport_error(make_client):
    """Bare ConnectionError surfaces as AlpacaTransportError."""
    trading = _FakeTradingClient(
        errors={"get_account": ConnectionError("DNS lookup failed")}
    )
    client = make_client(trading_client=trading)

    with pytest.raises(AlpacaTransportError):
        client.get_account()


def test_no_silent_none_on_error(make_client):
    """Errors must not be swallowed into a None return."""
    err = _make_fake_api_error(500, "boom")
    trading = _FakeTradingClient(errors={"get_account": err})
    client = make_client(trading_client=trading)

    with pytest.raises(AlpacaTransportError):
        client.get_account()


# ---------------------------------------------------------------------------
# VAL-M3-007 / VAL-M3-035: Live URL gated on BOTH LIVE_MODE and confirm file.
# ---------------------------------------------------------------------------


def test_live_url_blocked_when_live_mode_off_and_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(ac.config, "LIVE_MODE", False)
    monkeypatch.setattr(ac, "_confirmation_file_present", lambda: False)
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    with pytest.raises(LiveTradingBlockedError, match="LIVE_MODE"):
        AlpacaClient(
            api_key="k",
            secret_key="s",
            base_url=LIVE_BASE_URL,
            trading_client=_FakeTradingClient(),
            option_data_client=_FakeOptionsDataClient(),
        )


def test_live_url_blocked_when_only_live_mode_on(monkeypatch):
    """LIVE_MODE=1 alone is insufficient — confirmation file must also exist."""
    monkeypatch.setattr(ac.config, "LIVE_MODE", True)
    monkeypatch.setattr(ac, "_confirmation_file_present", lambda: False)
    with pytest.raises(LiveTradingBlockedError, match="LIVE_MODE"):
        AlpacaClient(
            api_key="k",
            secret_key="s",
            base_url=LIVE_BASE_URL,
            trading_client=_FakeTradingClient(),
            option_data_client=_FakeOptionsDataClient(),
        )


def test_live_url_blocked_when_only_file_present(monkeypatch):
    """File alone is insufficient — env var must also be set."""
    monkeypatch.setattr(ac.config, "LIVE_MODE", False)
    monkeypatch.setattr(ac, "_confirmation_file_present", lambda: True)
    with pytest.raises(LiveTradingBlockedError, match="LIVE_MODE"):
        AlpacaClient(
            api_key="k",
            secret_key="s",
            base_url=LIVE_BASE_URL,
            trading_client=_FakeTradingClient(),
            option_data_client=_FakeOptionsDataClient(),
        )


def test_live_url_constructs_when_both_gates_open(monkeypatch, caplog):
    """Both gates open → live client constructs and emits WARNING (VAL-M3-036)."""
    monkeypatch.setattr(ac.config, "LIVE_MODE", True)
    monkeypatch.setattr(ac, "_confirmation_file_present", lambda: True)

    caplog.set_level(logging.WARNING, logger="biotech_sniper.alpaca_client")
    client = AlpacaClient(
        api_key="k",
        secret_key="s",
        base_url=LIVE_BASE_URL,
        trading_client=_FakeTradingClient(),
        option_data_client=_FakeOptionsDataClient(),
    )
    assert client.base_url == LIVE_BASE_URL
    warns = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("LIVE TRADING ENABLED" in m for m in warns)


def test_live_url_block_message_names_both_gates(monkeypatch):
    """The LiveTradingBlockedError message names both required gates."""
    monkeypatch.setattr(ac.config, "LIVE_MODE", False)
    monkeypatch.setattr(ac, "_confirmation_file_present", lambda: False)
    with pytest.raises(LiveTradingBlockedError) as excinfo:
        AlpacaClient(
            api_key="k",
            secret_key="s",
            base_url=LIVE_BASE_URL,
            trading_client=_FakeTradingClient(),
            option_data_client=_FakeOptionsDataClient(),
        )
    msg = str(excinfo.value)
    assert "LIVE_MODE" in msg
    assert "i-understand-this-trades-real-money" in msg


# ---------------------------------------------------------------------------
# Constructor: missing keys without injected fakes raise auth error.
# ---------------------------------------------------------------------------


def test_constructor_raises_when_keys_missing_and_no_fakes(monkeypatch):
    monkeypatch.delenv("ALPACA_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.setattr(
        ac.config, "get_alpaca_key_id", lambda: None
    )
    monkeypatch.setattr(
        ac.config, "get_alpaca_secret_key", lambda: None
    )
    monkeypatch.setattr(
        ac.config, "get_alpaca_base_url", lambda: PAPER_BASE_URL
    )
    with pytest.raises(AlpacaAuthError, match="ALPACA_KEY_ID"):
        AlpacaClient()


# ---------------------------------------------------------------------------
# Order roundtrip — submit + get + cancel.
# ---------------------------------------------------------------------------


def test_submit_order_returns_order_dict(make_client):
    cassette = _load_cassette("order_call_filled.json")
    trading = _FakeTradingClient(
        submit_order_results=[cassette["submit"]]
    )
    client = make_client(trading_client=trading)

    req = LimitOrderRequest(
        symbol="IDYA250620C00012500",
        qty=2,
        side=OrderSide.BUY,
        type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        limit_price=1.20,
    )
    order = client.submit_order(req)
    assert order["id"] == "11111111-1111-1111-1111-111111111111"
    assert order["status"] == "accepted"
    assert order["side"] == "buy"
    assert order["qty"] == 2.0
    assert order["symbol"] == "IDYA250620C00012500"


def test_get_order_returns_filled_status(make_client):
    cassette = _load_cassette("order_call_filled.json")
    trading = _FakeTradingClient(
        get_order_results=[cassette["filled"]]
    )
    client = make_client(trading_client=trading)

    order = client.get_order("11111111-1111-1111-1111-111111111111")
    assert order["status"] == "filled"
    assert order["filled_qty"] == 2.0
    assert order["filled_avg_price"] == 1.22


def test_cancel_order_calls_trading_client(make_client):
    trading = _FakeTradingClient()
    client = make_client(trading_client=trading)

    client.cancel_order("11111111-1111-1111-1111-111111111111")

    methods = [c[0] for c in trading.calls]
    assert "cancel_order_by_id" in methods


def test_submit_order_translates_401_to_auth_error(make_client):
    err = _make_fake_api_error(401, "unauthorized")
    trading = _FakeTradingClient(errors={"submit_order": err})
    client = make_client(trading_client=trading)

    req = MarketOrderRequest(
        symbol="SPY250620C00500000",
        qty=1,
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
    )
    with pytest.raises(AlpacaAuthError):
        client.submit_order(req)


def test_get_order_translates_500_to_transport_error(make_client):
    err = _make_fake_api_error(500, "internal")
    trading = _FakeTradingClient(errors={"get_order_by_id": err})
    client = make_client(trading_client=trading)

    with pytest.raises(AlpacaTransportError):
        client.get_order("11111111-1111-1111-1111-111111111111")


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


def test_get_positions_returns_list_of_dicts(make_client):
    pos = {
        "asset_id": "00000000-0000-0000-0000-0000000000aa",
        "symbol": "SPY",
        "qty": "10",
        "qty_available": "10",
        "side": "long",
        "avg_entry_price": "500.50",
        "market_value": "5050.00",
        "cost_basis": "5005.00",
        "current_price": "505.00",
        "unrealized_pl": "45.00",
        "unrealized_plpc": "0.009",
    }
    trading = _FakeTradingClient(positions=[pos])
    client = make_client(trading_client=trading)

    positions = client.get_positions()
    assert len(positions) == 1
    p = positions[0]
    assert p["symbol"] == "SPY"
    assert p["qty"] == 10.0
    assert p["avg_entry_price"] == 500.50


def test_get_positions_empty_list_default(make_client):
    trading = _FakeTradingClient(positions=[])
    client = make_client(trading_client=trading)
    assert client.get_positions() == []


# ---------------------------------------------------------------------------
# f-misc-04 (5): AlpacaClient.get_latest_trade — centralized
# stock latest-trade access so callers (e.g. new_opportunity_sniper)
# never import alpaca-py directly. Cassette-backed, mirrors the
# patterns above (account/options-chain/orders): hand-crafted JSON
# fixture replayed through a duck-typed fake stock-data client.
# ---------------------------------------------------------------------------


class _FakeStockDataClient:
    """Minimal duck-type substitute for ``StockHistoricalDataClient``.

    Backs :meth:`AlpacaClient.get_latest_trade`. The single method
    returns a pre-canned ``{ticker: Trade}`` dict (or raises a queued
    exception) so tests can pin both happy-path and error-path
    behaviour without a network call.
    """

    def __init__(
        self,
        *,
        latest_trade_response: Any = None,
        errors: Any = None,
    ):
        self.latest_trade_response = latest_trade_response
        self._errors = errors or {}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def get_stock_latest_trade(self, request: Any) -> Any:
        self.calls.append(("get_stock_latest_trade", (request,), {}))
        if "get_stock_latest_trade" in self._errors:
            raise self._errors["get_stock_latest_trade"]
        return self.latest_trade_response


def _make_client_with_stocks(
    *,
    stock_data_client: Any,
    trading_client: Any = None,
    option_data_client: Any = None,
) -> AlpacaClient:
    """Build an AlpacaClient with all three SDK doubles wired.

    Mirrors the ``make_client`` fixture but forwards the stock client
    explicitly so :meth:`get_latest_trade` can be exercised hermetic.
    """
    return AlpacaClient(
        api_key="AKTEST_KEY_ID_VISIBLE_FOR_LEAK_CHECK",
        secret_key="SK_TEST_SECRET_VISIBLE_FOR_LEAK_CHECK_XYZ",
        base_url=PAPER_BASE_URL,
        trading_client=trading_client or _FakeTradingClient(),
        option_data_client=option_data_client or _FakeOptionsDataClient(),
        stock_data_client=stock_data_client,
    )


def test_get_latest_trade_returns_price_from_cassette(paper_env):
    """Happy path: cassette → trade dict → positive float price."""
    cassette = _load_cassette("stock_latest_trade_success.json")
    stocks = _FakeStockDataClient(latest_trade_response=cassette["result"])
    client = _make_client_with_stocks(stock_data_client=stocks)

    price = client.get_latest_trade("AAPL")

    assert isinstance(price, float)
    assert price == 192.50
    # Exactly one SDK call was made.
    assert [call[0] for call in stocks.calls] == ["get_stock_latest_trade"]


def test_get_latest_trade_returns_none_for_empty_ticker(paper_env):
    """``get_latest_trade('')`` short-circuits to ``None`` without an API call."""
    stocks = _FakeStockDataClient(latest_trade_response={})
    client = _make_client_with_stocks(stock_data_client=stocks)

    assert client.get_latest_trade("") is None
    # No SDK call issued for the empty ticker.
    assert stocks.calls == []


def test_get_latest_trade_returns_none_when_response_missing_ticker(paper_env):
    """Response with no entry for the ticker must yield ``None``, not raise."""
    stocks = _FakeStockDataClient(latest_trade_response={"OTHER": {"price": 9.99}})
    client = _make_client_with_stocks(stock_data_client=stocks)

    assert client.get_latest_trade("AAPL") is None


def test_get_latest_trade_returns_none_for_zero_or_negative_price(paper_env):
    """Defensive: zero or negative quotes must be treated as missing."""
    stocks = _FakeStockDataClient(
        latest_trade_response={"AAPL": {"symbol": "AAPL", "price": 0}}
    )
    client = _make_client_with_stocks(stock_data_client=stocks)
    assert client.get_latest_trade("AAPL") is None

    stocks_neg = _FakeStockDataClient(
        latest_trade_response={"AAPL": {"symbol": "AAPL", "price": -1.5}}
    )
    client_neg = _make_client_with_stocks(stock_data_client=stocks_neg)
    assert client_neg.get_latest_trade("AAPL") is None


def test_get_latest_trade_raises_auth_when_no_stock_client(monkeypatch):
    """Without a configured stock-data client, the wrapper raises AuthError.

    Reproduces the production guarantee that calling
    :meth:`get_latest_trade` on a wrapper built with only trading +
    options doubles surfaces a typed :class:`AlpacaAuthError` instead
    of an attribute-error / silent ``None``.
    """
    # paper_env-equivalent setup but explicitly drop creds so the
    # constructor's missing-creds branch picks the ``_stocks=None``
    # path (injected_doubles tolerates the missing creds because
    # both trading + options doubles are present).
    monkeypatch.setenv("ALPACA_BASE_URL", PAPER_BASE_URL)
    monkeypatch.setenv("LIVE_MODE", "0")
    monkeypatch.setattr(ac.config, "LIVE_MODE", False)
    monkeypatch.setattr(ac.config, "get_alpaca_key_id", lambda: None)
    monkeypatch.setattr(ac.config, "get_alpaca_secret_key", lambda: None)
    monkeypatch.setattr(ac.config, "get_alpaca_base_url", lambda: PAPER_BASE_URL)

    client = AlpacaClient(
        api_key=None,
        secret_key=None,
        base_url=PAPER_BASE_URL,
        trading_client=_FakeTradingClient(),
        option_data_client=_FakeOptionsDataClient(),
    )

    with pytest.raises(AlpacaAuthError, match="get_latest_trade"):
        client.get_latest_trade("AAPL")


def test_get_latest_trade_classifies_401_as_auth_error(paper_env):
    """API 401 from the stock data plane → :class:`AlpacaAuthError`."""
    err = _make_fake_api_error(401, "unauthorized")
    stocks = _FakeStockDataClient(errors={"get_stock_latest_trade": err})
    client = _make_client_with_stocks(stock_data_client=stocks)

    with pytest.raises(AlpacaAuthError):
        client.get_latest_trade("AAPL")


def test_get_latest_trade_classifies_503_as_transport_error(paper_env):
    """API 5xx from the stock data plane → :class:`AlpacaTransportError`."""
    err = _make_fake_api_error(503, "service unavailable")
    stocks = _FakeStockDataClient(errors={"get_stock_latest_trade": err})
    client = _make_client_with_stocks(stock_data_client=stocks)

    with pytest.raises(AlpacaTransportError):
        client.get_latest_trade("AAPL")


def test_get_latest_trade_handles_pydantic_like_attribute_access(paper_env):
    """Accept attribute-style trade objects (real SDK shape), not just dicts."""

    class _PydanticishTrade:
        symbol = "AAPL"
        price = 199.99

    class _PydanticishResponse:
        AAPL = _PydanticishTrade()

    stocks = _FakeStockDataClient(latest_trade_response=_PydanticishResponse())
    client = _make_client_with_stocks(stock_data_client=stocks)

    price = client.get_latest_trade("AAPL")
    assert price == 199.99


def test_module_does_not_import_stock_client_outside_alpaca_client():
    """Boundary check: only ``alpaca_client.py`` imports the alpaca-py SDK.

    The module docstring states that ``alpaca-py is only imported in
    alpaca_client.py``. f-misc-04 (5) re-asserts that boundary by
    moving the stock-latest-trade lookup off ``new_opportunity_sniper``
    and into :meth:`AlpacaClient.get_latest_trade`. This regression
    test fails if any future patch re-introduces a direct
    ``alpaca.data.historical.stock`` / ``alpaca.data.requests`` import
    in ``new_opportunity_sniper.py``.
    """
    sniper_path = (
        Path(ac.__file__).parent / "new_opportunity_sniper.py"
    )
    src = sniper_path.read_text(encoding="utf-8")
    assert "from alpaca.data.historical.stock" not in src, (
        "new_opportunity_sniper.py must not import alpaca-py directly; "
        "use AlpacaClient.get_latest_trade instead."
    )
    assert "StockLatestTradeRequest" not in src, (
        "new_opportunity_sniper.py must not reference alpaca-py request "
        "types directly; route latest-trade calls through "
        "AlpacaClient.get_latest_trade."
    )
    assert "AlpacaClient" in src and "get_latest_trade" in src, (
        "new_opportunity_sniper.py must delegate to "
        "AlpacaClient.get_latest_trade for underlying-price lookups."
    )
