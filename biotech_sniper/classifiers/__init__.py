"""Reading-B classifier modules.

This sub-package owns the classifiers used to label tickers from
authoritative public sources. The first member is
:mod:`biotech_sniper.classifiers.sec_sic` which resolves a ticker
through SEC EDGAR's public ``data.sec.gov`` API into its 4-digit
Standard Industrial Classification (SIC) code, persisted in the
``cik_sic_cache`` SQLite table.

The downstream consumer is
:mod:`biotech_sniper.universe.russell_biotech` (separate feature),
which intersects the IWM holdings snapshot with the SIC-based
biotech subset (``2834``, ``2836``, ``8731``) to materialise the
``russell2k_biotech`` universe table.
"""

from __future__ import annotations

from biotech_sniper.classifiers.sec_sic import (
    BIOTECH_SIC_CODES,
    DEFAULT_USER_AGENT,
    SECClassifierError,
    SECSchemaError,
    SECSICClassifier,
    SECTransientError,
    SICResolution,
    ensure_cik_sic_cache_table,
    resolve_ticker_sic,
)

__all__: list[str] = [
    "BIOTECH_SIC_CODES",
    "DEFAULT_USER_AGENT",
    "SECClassifierError",
    "SECSchemaError",
    "SECSICClassifier",
    "SECTransientError",
    "SICResolution",
    "ensure_cik_sic_cache_table",
    "resolve_ticker_sic",
]
