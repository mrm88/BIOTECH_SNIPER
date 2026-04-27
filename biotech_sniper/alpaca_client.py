"""Alpaca paper-trading client — broker integration for M3 paper executor.

This module implements :class:`AlpacaClient`, a thin wrapper over the
official ``alpaca-py`` SDK. It is the **only** entry point for any
Alpaca interaction in this project: account snapshots, position
listings, options-chain fetches, and order submission / cancellation
all flow through this class. Hand-rolled ``requests.post`` calls to
the Alpaca host are explicitly forbidden by mission policy
(``AGENTS.md`` § "LLM boundaries" and the M3 validation contract
VAL-M3-002 grep).

Design contract
---------------
* **Paper-only by default.** Default construction targets
  :data:`PAPER_BASE_URL` resolved through
  :func:`biotech_sniper.config.get_alpaca_base_url`. Constructing
  against the live URL (`https://api.alpaca.markets`) requires BOTH
  guardrails to be open simultaneously:

    1. ``LIVE_MODE`` env var truthy (loaded by ``config.py``).
    2. A confirmation marker file
       ``i-understand-this-trades-real-money`` present at
       :data:`biotech_sniper.paths.BASE_DIR`.

  Either gate missing → :class:`LiveTradingBlockedError` raised
  before any SDK construction or network call. Production code must
  not write the confirmation file — only the user does, manually.

* **API key sourcing.** Reads ``ALPACA_KEY_ID`` and
  ``ALPACA_SECRET_KEY`` exclusively via the
  :func:`biotech_sniper.config.get_alpaca_key_id` and
  :func:`biotech_sniper.config.get_alpaca_secret_key` helpers.
  Direct environment lookups outside ``config.py`` are forbidden.

* **Transport.** alpaca-py's :class:`alpaca.trading.client.TradingClient`
  for account / positions / orders, and
  :class:`alpaca.data.historical.option.OptionHistoricalDataClient` for
  options chain snapshots. No hand-rolled HTTP. Validators grep the
  source for ``requests\\.(post|get)\\(.+alpaca`` and fail the build
  if any match is found.

* **Typed exceptions.** All HTTP failures surface as a typed
  exception hierarchy rooted at :class:`AlpacaClientError`. Auth
  failures (HTTP 401 / 403) raise :class:`AlpacaAuthError`; transport
  failures (HTTP 5xx, connection errors, timeouts) raise
  :class:`AlpacaTransportError`. Live-URL block raises
  :class:`LiveTradingBlockedError`. No bare ``Exception`` propagates
  out of public methods.

* **Secret hygiene.** API keys are NEVER written to logs. Debug log
  lines redact the key id and secret to ``"***"``. The wrapper does
  not log Authorization headers (the SDK manages headers internally).

Public surface
--------------
* :class:`AlpacaClient` — the wrapper class.
* :class:`AlpacaClientError`, :class:`AlpacaAuthError`,
  :class:`AlpacaTransportError`, :class:`LiveTradingBlockedError` —
  typed exceptions.
* :data:`PAPER_BASE_URL`, :data:`LIVE_BASE_URL`,
  :data:`LIVE_CONFIRMATION_FILE` — public constants used by the
  paper-only enforcement and tests.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from alpaca.common.exceptions import APIError
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    OrderRequest,
)

from biotech_sniper import config
from biotech_sniper.paths import BASE_DIR


__all__ = [
    "AlpacaClient",
    "AlpacaClientError",
    "AlpacaAuthError",
    "AlpacaTransportError",
    "LiveTradingBlockedError",
    "PAPER_BASE_URL",
    "LIVE_BASE_URL",
    "LIVE_CONFIRMATION_FILE",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Alpaca paper-trading base URL. Default for all default-constructed
#: instances of :class:`AlpacaClient`.
PAPER_BASE_URL: str = "https://paper-api.alpaca.markets"

#: Alpaca live-trading base URL. Constructing :class:`AlpacaClient`
#: against this URL is hard-blocked unless both LIVE_MODE gates open.
LIVE_BASE_URL: str = "https://api.alpaca.markets"

#: File-system marker that, together with ``LIVE_MODE=1``, unlocks
#: live-trading construction. Resolved via :data:`paths.BASE_DIR`.
#: Production code never writes this file — only the user, manually.
LIVE_CONFIRMATION_FILE: str = "i-understand-this-trades-real-money"


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class AlpacaClientError(Exception):
    """Base class for all AlpacaClient errors."""


class AlpacaAuthError(AlpacaClientError):
    """Raised on Alpaca authentication failure (HTTP 401 / 403).

    Also raised at construction time when both ``ALPACA_KEY_ID`` and
    ``ALPACA_SECRET_KEY`` are unset and no test-injected SDK clients
    are provided. Fail-fast — no retry storm.
    """


class AlpacaTransportError(AlpacaClientError):
    """Raised on Alpaca server / connection errors.

    Covers HTTP 5xx responses and the requests-level connection /
    timeout exceptions surfaced by alpaca-py.
    """


class LiveTradingBlockedError(AlpacaClientError):
    """Raised when constructing against the live URL with gates closed.

    The error message names BOTH required gates (``LIVE_MODE=1`` AND
    the confirmation file) so operators can immediately see which
    side of the guardrail tripped. Defensive: the check runs BEFORE
    any SDK construction or network call.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_live_url(url: str) -> bool:
    """Return True iff ``url`` matches :data:`LIVE_BASE_URL` (post-strip)."""
    return url.rstrip("/") == LIVE_BASE_URL.rstrip("/")


def _confirmation_file_path() -> Path:
    """Return the absolute path to the live-trading confirmation marker."""
    return BASE_DIR / LIVE_CONFIRMATION_FILE


def _confirmation_file_present() -> bool:
    """Return True iff the live-trading confirmation marker exists."""
    return _confirmation_file_path().is_file()


def _validate_paper_only(base_url: str) -> None:
    """Reject construction against the live URL unless both gates are open.

    Raises :class:`LiveTradingBlockedError` if ``base_url`` resolves to
    the live host AND either ``LIVE_MODE`` is falsy OR the confirmation
    marker file is missing.
    """
    if not _is_live_url(base_url):
        return

    live_mode = bool(config.LIVE_MODE)
    confirm_present = _confirmation_file_present()
    if live_mode and confirm_present:
        return

    raise LiveTradingBlockedError(
        "Refusing to construct AlpacaClient against the live URL "
        f"{base_url!r}. Both gates required: LIVE_MODE=1 (got "
        f"{int(live_mode)}) AND confirmation file "
        f"{LIVE_CONFIRMATION_FILE!r} at {_confirmation_file_path()} "
        f"(present={confirm_present}). Default to paper trading by "
        "leaving ALPACA_BASE_URL unset or pointing it at "
        f"{PAPER_BASE_URL!r}."
    )


def _redact(value: Optional[str]) -> str:
    """Return ``"***"`` when a secret is set, ``"<unset>"`` otherwise.

    Used in DEBUG log lines so the wrapper is greppable for the
    redaction marker without ever leaking the actual secret string.
    """
    return "***" if value else "<unset>"


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Attribute-or-key accessor that works for pydantic models AND dicts.

    The alpaca-py SDK returns pydantic-model instances; tests inject
    plain dicts via cassette replay. Both surface fields under the
    same name, so a single accessor keeps the conversion helpers
    simple.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _enum_value(value: Any) -> Optional[str]:
    """Return the underlying string value of an enum, or the value itself."""
    if value is None:
        return None
    inner = getattr(value, "value", None)
    if isinstance(inner, str):
        return inner
    if isinstance(value, str):
        return value
    return str(value)


def _maybe_float(value: Any) -> Optional[float]:
    """Coerce numeric / numeric-string to float, return None on missing."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _maybe_int(value: Any) -> Optional[int]:
    """Coerce int-ish to int, return None on missing or unparseable input."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _maybe_iso(value: Any) -> Optional[str]:
    """Return ISO-8601 string for ``datetime``/``date`` values, else str(value)."""
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return iso()
    return str(value)


def _classify_api_error(exc: Exception) -> AlpacaClientError:
    """Map an SDK / requests error to our typed exception hierarchy.

    * HTTP 401 / 403 → :class:`AlpacaAuthError`
    * HTTP 5xx → :class:`AlpacaTransportError`
    * Connection / timeout → :class:`AlpacaTransportError`
    * Anything else → :class:`AlpacaClientError`
    """
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return AlpacaAuthError(
            f"Alpaca authentication failed (HTTP {status}): {exc!s}"
        )
    if isinstance(status, int) and 500 <= status < 600:
        return AlpacaTransportError(
            f"Alpaca server error (HTTP {status}): {exc!s}"
        )
    name = type(exc).__name__
    if name in {
        "ConnectionError",
        "Timeout",
        "ReadTimeout",
        "ConnectTimeout",
        "RetryException",
    }:
        return AlpacaTransportError(f"Alpaca transport error: {exc!r}")
    return AlpacaClientError(f"Alpaca API error: {exc!s}")


# ---------------------------------------------------------------------------
# Response → dict converters
# ---------------------------------------------------------------------------


def _account_to_dict(account: Any) -> dict[str, Any]:
    """Coerce a :class:`TradeAccount` (or dict) into the validation dict.

    The contract requires ``equity``, ``buying_power``, ``cash`` as
    numeric values. We retain a small set of additional fields that
    downstream sizing logic finds useful (portfolio_value, status,
    account_number) without leaking the entire raw payload.
    """
    return {
        "id": _attr(account, "id"),
        "account_number": _attr(account, "account_number"),
        "status": _enum_value(_attr(account, "status")),
        "currency": _attr(account, "currency", "USD"),
        "equity": _maybe_float(_attr(account, "equity")) or 0.0,
        "buying_power": _maybe_float(_attr(account, "buying_power")) or 0.0,
        "cash": _maybe_float(_attr(account, "cash")) or 0.0,
        "portfolio_value": _maybe_float(_attr(account, "portfolio_value")) or 0.0,
        "options_buying_power": _maybe_float(
            _attr(account, "options_buying_power")
        ),
    }


def _position_to_dict(position: Any) -> dict[str, Any]:
    return {
        "asset_id": _attr(position, "asset_id"),
        "symbol": _attr(position, "symbol"),
        "qty": _maybe_float(_attr(position, "qty")),
        "qty_available": _maybe_float(_attr(position, "qty_available")),
        "side": _enum_value(_attr(position, "side")),
        "avg_entry_price": _maybe_float(_attr(position, "avg_entry_price")),
        "market_value": _maybe_float(_attr(position, "market_value")),
        "cost_basis": _maybe_float(_attr(position, "cost_basis")),
        "current_price": _maybe_float(_attr(position, "current_price")),
        "unrealized_pl": _maybe_float(_attr(position, "unrealized_pl")),
        "unrealized_plpc": _maybe_float(_attr(position, "unrealized_plpc")),
    }


def _order_to_dict(order: Any) -> dict[str, Any]:
    return {
        "id": str(_attr(order, "id") or ""),
        "client_order_id": _attr(order, "client_order_id"),
        "symbol": _attr(order, "symbol"),
        "asset_class": _enum_value(_attr(order, "asset_class")),
        "qty": _maybe_float(_attr(order, "qty")),
        "filled_qty": _maybe_float(_attr(order, "filled_qty")),
        "filled_avg_price": _maybe_float(_attr(order, "filled_avg_price")),
        "side": _enum_value(_attr(order, "side")),
        "status": _enum_value(_attr(order, "status")),
        "order_class": _enum_value(_attr(order, "order_class")),
        "order_type": _enum_value(
            _attr(order, "order_type") or _attr(order, "type")
        ),
        "time_in_force": _enum_value(_attr(order, "time_in_force")),
        "limit_price": _maybe_float(_attr(order, "limit_price")),
        "stop_price": _maybe_float(_attr(order, "stop_price")),
        "created_at": _maybe_iso(_attr(order, "created_at")),
        "updated_at": _maybe_iso(_attr(order, "updated_at")),
        "submitted_at": _maybe_iso(_attr(order, "submitted_at")),
        "filled_at": _maybe_iso(_attr(order, "filled_at")),
        "canceled_at": _maybe_iso(_attr(order, "canceled_at")),
    }


def _build_chain_row(contract: Any, snapshot: Any) -> dict[str, Any]:
    """Merge an OptionContract + OptionsSnapshot into the chain-row dict.

    The validation contract (VAL-M3-004) requires keys
    ``strike, expiry, mid, bid, ask, iv, delta, oi, type``. The contract
    side carries ``strike, expiry, type, oi``; the snapshot side
    carries ``bid/ask`` (via ``latest_quote``), ``iv``, and ``delta``
    (via ``greeks``). The two are merged here so callers see one row
    per (ticker, expiry, strike, type).
    """
    quote = _attr(snapshot, "latest_quote")
    bid = _maybe_float(_attr(quote, "bid_price"))
    ask = _maybe_float(_attr(quote, "ask_price"))
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        mid: Optional[float] = (bid + ask) / 2.0
    else:
        mid = None

    iv = _maybe_float(_attr(snapshot, "implied_volatility"))
    greeks = _attr(snapshot, "greeks")
    delta = _maybe_float(_attr(greeks, "delta"))

    contract_type = _attr(contract, "type")
    type_str = _enum_value(contract_type)
    if type_str:
        type_str = type_str.lower()

    expiry_str = _maybe_iso(_attr(contract, "expiration_date"))
    strike = _maybe_float(_attr(contract, "strike_price"))
    oi = _maybe_int(_attr(contract, "open_interest"))

    return {
        "symbol": _attr(contract, "symbol"),
        "strike": strike,
        "expiry": expiry_str,
        "mid": mid,
        "bid": bid if bid is not None else 0.0,
        "ask": ask if ask is not None else 0.0,
        "iv": iv,
        "delta": delta,
        "oi": oi,
        "type": type_str or "call",
    }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AlpacaClient:
    """alpaca-py SDK wrapper for paper trading + options chain data.

    Parameters
    ----------
    api_key:
        Optional override. ``None`` (the default) reads
        :func:`biotech_sniper.config.get_alpaca_key_id`. Mission policy
        forbids any other lookup path.
    secret_key:
        Optional override. ``None`` reads
        :func:`biotech_sniper.config.get_alpaca_secret_key`.
    base_url:
        Optional override. ``None`` reads
        :func:`biotech_sniper.config.get_alpaca_base_url`, which
        defaults to :data:`PAPER_BASE_URL`. Constructing against
        :data:`LIVE_BASE_URL` requires both LIVE_MODE gates open.
    trading_client:
        Optional pre-built :class:`alpaca.trading.client.TradingClient`
        (or any duck-typed substitute exposing ``get_account``,
        ``get_all_positions``, ``submit_order``, ``get_order_by_id``,
        ``cancel_order_by_id``, ``get_option_contracts``). Tests
        inject a fake; production callers leave this ``None``.
    option_data_client:
        Optional pre-built
        :class:`alpaca.data.historical.option.OptionHistoricalDataClient`
        (or duck-typed substitute exposing ``get_option_chain``).
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        base_url: Optional[str] = None,
        trading_client: Optional[Any] = None,
        option_data_client: Optional[Any] = None,
    ) -> None:
        resolved_key = (
            api_key if api_key is not None else config.get_alpaca_key_id()
        )
        resolved_secret = (
            secret_key
            if secret_key is not None
            else config.get_alpaca_secret_key()
        )
        resolved_url = (
            base_url if base_url is not None else config.get_alpaca_base_url()
        )

        # Paper-only guardrail FIRST. We refuse to build any SDK
        # client (and therefore never make a network call) when the
        # live URL is requested without both gates open. This must
        # run before key validation so a missing key never masks the
        # live-block message in the error trace.
        _validate_paper_only(resolved_url)

        self.base_url: str = resolved_url
        self._api_key = resolved_key
        self._secret_key = resolved_secret

        # When test harnesses inject both fake clients we deliberately
        # tolerate missing creds — the smoke-import path on hosts
        # without paper keys (CI, local dev pre-M3-precondition) must
        # still succeed for downstream modules to import. Real network
        # calls without keys will surface AlpacaAuthError on their own.
        injected_doubles = (
            trading_client is not None and option_data_client is not None
        )
        if not (resolved_key and resolved_secret) and not injected_doubles:
            raise AlpacaAuthError(
                "ALPACA_KEY_ID and ALPACA_SECRET_KEY must both be "
                "configured. Set them in /root/alpha_sniper/.env on "
                "the VPS (or repo-root .env locally) and ensure "
                "config.get_alpaca_key_id / get_alpaca_secret_key "
                "return non-empty values."
            )

        # If we're constructing against the live URL with both gates
        # open, emit a loud WARNING. Validators (VAL-M3-036) grep
        # caplog for the literal phrase "LIVE TRADING ENABLED".
        if _is_live_url(resolved_url):
            logger.warning(
                "LIVE TRADING ENABLED — orders will trade real money. "
                "Base URL: %s",
                resolved_url,
            )

        if trading_client is not None:
            self._trading = trading_client
        else:
            self._trading = TradingClient(
                api_key=resolved_key,
                secret_key=resolved_secret,
                paper=not _is_live_url(resolved_url),
                url_override=resolved_url,
            )

        if option_data_client is not None:
            self._options = option_data_client
        else:
            self._options = OptionHistoricalDataClient(
                api_key=resolved_key,
                secret_key=resolved_secret,
            )

        # DEBUG-level redacted construction trace. Keys are redacted
        # to the literal "***" so the file is greppable for redaction
        # without ever leaking the real values.
        logger.debug(
            "AlpacaClient constructed base_url=%s key_id=%s secret_key=%s",
            self.base_url,
            _redact(resolved_key),
            _redact(resolved_secret),
        )

    # ------------------------------------------------------------------
    # Account / positions
    # ------------------------------------------------------------------

    def get_account(self) -> dict[str, Any]:
        """Return the current Alpaca account snapshot.

        Always returns a dict with at minimum ``equity``,
        ``buying_power``, ``cash`` (numeric, ≥ 0 in a paper sandbox).
        Auth failures raise :class:`AlpacaAuthError`; transport
        failures raise :class:`AlpacaTransportError`.
        """
        try:
            account = self._trading.get_account()
        except APIError as exc:
            raise _classify_api_error(exc) from exc
        except Exception as exc:
            raise _classify_api_error(exc) from exc

        result = _account_to_dict(account)
        logger.debug(
            "alpaca.get_account ok equity=%s buying_power=%s cash=%s",
            result.get("equity"),
            result.get("buying_power"),
            result.get("cash"),
        )
        return result

    def get_positions(self) -> list[dict[str, Any]]:
        """Return all open positions on the Alpaca account."""
        try:
            positions = self._trading.get_all_positions()
        except APIError as exc:
            raise _classify_api_error(exc) from exc
        except Exception as exc:
            raise _classify_api_error(exc) from exc

        return [_position_to_dict(p) for p in (positions or [])]

    # ------------------------------------------------------------------
    # Options chain
    # ------------------------------------------------------------------

    def get_options_chain(
        self,
        ticker: str,
        expiry: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Return the options chain for ``ticker`` and a target ``expiry``.

        Each row contains the keys required by VAL-M3-004:
        ``strike, expiry, mid, bid, ask, iv, delta, oi, type``.

        ``expiry`` may be ``None`` (return whatever the broker considers
        the "default" / nearest-term chain) or an ISO-formatted date
        string (``YYYY-MM-DD``). The merge below joins
        :meth:`TradingClient.get_option_contracts` (strike, expiry,
        type, OI metadata) with
        :meth:`OptionHistoricalDataClient.get_option_chain` (live
        bid/ask/IV/delta) keyed by OCC option symbol.
        """
        # 1) Pull contract metadata: strike, expiry, type, open_interest.
        try:
            contracts_resp = self._trading.get_option_contracts(
                GetOptionContractsRequest(
                    underlying_symbols=[ticker],
                    expiration_date=expiry,
                )
            )
        except APIError as exc:
            raise _classify_api_error(exc) from exc
        except Exception as exc:
            raise _classify_api_error(exc) from exc

        contracts_iter = (
            _attr(contracts_resp, "option_contracts")
            if not isinstance(contracts_resp, list)
            else contracts_resp
        )
        contracts = list(contracts_iter or [])
        contract_by_symbol = {
            _attr(c, "symbol"): c for c in contracts if _attr(c, "symbol")
        }

        # 2) Pull live snapshots: bid/ask/IV/delta keyed by OCC symbol.
        try:
            snapshots = self._options.get_option_chain(
                OptionChainRequest(
                    underlying_symbol=ticker,
                    expiration_date=expiry,
                )
            )
        except APIError as exc:
            raise _classify_api_error(exc) from exc
        except Exception as exc:
            raise _classify_api_error(exc) from exc

        snapshots = snapshots or {}

        # 3) Merge — only emit a row when both sides agree on the symbol
        # so the chain never has half-populated entries leaking into
        # the scoring layer.
        chain: list[dict[str, Any]] = []
        for symbol, snap in snapshots.items():
            contract = contract_by_symbol.get(symbol)
            if contract is None:
                continue
            chain.append(_build_chain_row(contract, snap))

        logger.debug(
            "alpaca.get_options_chain ticker=%s expiry=%s rows=%d",
            ticker,
            expiry,
            len(chain),
        )
        return chain

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def submit_order(self, order_request: OrderRequest) -> dict[str, Any]:
        """Submit a single order to Alpaca.

        ``order_request`` MUST be one of the alpaca-py
        :class:`OrderRequest` subclasses (e.g.
        :class:`MarketOrderRequest`, :class:`LimitOrderRequest`). The
        wrapper does not coerce dicts into request models — callers
        in the paper executor build the typed request explicitly so
        validators can reason about order shape statically.
        """
        try:
            order = self._trading.submit_order(order_request)
        except APIError as exc:
            raise _classify_api_error(exc) from exc
        except Exception as exc:
            raise _classify_api_error(exc) from exc

        result = _order_to_dict(order)
        logger.debug(
            "alpaca.submit_order ok id=%s symbol=%s side=%s qty=%s status=%s",
            result.get("id"),
            result.get("symbol"),
            result.get("side"),
            result.get("qty"),
            result.get("status"),
        )
        return result

    def get_order(self, order_id: str) -> dict[str, Any]:
        """Fetch a single order by id."""
        try:
            order = self._trading.get_order_by_id(order_id)
        except APIError as exc:
            raise _classify_api_error(exc) from exc
        except Exception as exc:
            raise _classify_api_error(exc) from exc

        return _order_to_dict(order)

    def cancel_order(self, order_id: str) -> None:
        """Cancel an open order by id. Returns nothing on success."""
        try:
            self._trading.cancel_order_by_id(order_id)
        except APIError as exc:
            raise _classify_api_error(exc) from exc
        except Exception as exc:
            raise _classify_api_error(exc) from exc

        logger.debug("alpaca.cancel_order ok id=%s", order_id)
