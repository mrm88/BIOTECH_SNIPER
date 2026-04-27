"""Options-chain fetcher (M3 Alpaca-backed implementation).

This module replaces the legacy chain-pull script that lived under
``scripts/legacy/``. It exposes a single public entry point
:func:`pull_chain` that returns a list of dicts conforming to the
schema declared in the M3 validation contract (VAL-M3-013):

    ``strike, expiry, mid, bid, ask, iv, delta, oi``

(plus a few additional fields like ``symbol`` and ``type`` that
downstream consumers —
:func:`biotech_sniper.sectors.unified_scorer.score_options`, the M3
paper executor, the M3 liquidity probe — also depend on).

Implementation
--------------
The chain is sourced from
:meth:`biotech_sniper.alpaca_client.AlpacaClient.get_options_chain`
which internally merges ``TradingClient.get_option_contracts`` (strike
/ expiry / type / OI) with
``OptionHistoricalDataClient.get_option_chain`` (bid / ask / IV /
delta). Callers may inject a pre-built :class:`AlpacaClient` (used by
tests) or let :func:`pull_chain` lazily construct the default
paper-mode client.

Greppability
------------
This module imports zero legacy-data-vendor symbols. The whole point
of the M3 milestone is to deprecate that dependency project-wide; the
M1 ``requirements.txt`` no longer pins the legacy vendor once this
feature lands. See VAL-M3-010 for the contract and
``tests/test_pull_options.py`` for the regression gate.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from biotech_sniper.alpaca_client import AlpacaClient


__all__ = ["pull_chain", "REQUIRED_CHAIN_KEYS"]


logger = logging.getLogger(__name__)


# The keys every row in the returned chain MUST expose. The M3 paper
# executor and ``unified_scorer.score_options`` consumers index into
# the chain by these names; missing any one would surface as a KeyError
# upstream which the validation contract (VAL-M3-013) explicitly
# forbids.
REQUIRED_CHAIN_KEYS: tuple[str, ...] = (
    "strike",
    "expiry",
    "mid",
    "bid",
    "ask",
    "iv",
    "delta",
    "oi",
)


def pull_chain(
    ticker: str,
    expiry: Optional[str] = None,
    *,
    client: Optional[AlpacaClient] = None,
) -> list[dict[str, Any]]:
    """Return the options chain for ``ticker`` and a target ``expiry``.

    Parameters
    ----------
    ticker:
        Underlying equity ticker (e.g. ``"AXSM"``). Case is preserved.
    expiry:
        Optional ISO-8601 date string (``YYYY-MM-DD``) selecting a
        single expiration. ``None`` lets Alpaca return the default
        chain (typically the nearest-term tradeable expirations).
    client:
        Optional pre-built :class:`AlpacaClient` for dependency
        injection. Tests pass a fake client; production callers leave
        this ``None`` and a default paper-mode client is constructed.

    Returns
    -------
    list[dict]
        One dict per chain row. Each row exposes at minimum the keys
        in :data:`REQUIRED_CHAIN_KEYS`. The caller can pass these rows
        unchanged to
        :func:`biotech_sniper.sectors.unified_scorer.score_options`
        without translation.
    """
    if not ticker or not isinstance(ticker, str):
        raise ValueError(
            f"pull_chain: ticker must be a non-empty string, got {ticker!r}"
        )

    chain_client = client if client is not None else AlpacaClient()
    rows = chain_client.get_options_chain(ticker, expiry)

    logger.debug(
        "pull_chain ticker=%s expiry=%s rows=%d",
        ticker,
        expiry,
        len(rows),
    )
    return rows
