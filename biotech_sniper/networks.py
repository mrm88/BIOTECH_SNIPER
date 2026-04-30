"""Network egress allow-list for the Biotech Sniper project.

This module is the single source of truth for every public hostname this
project is permitted to contact over the network. Validators and tests
import :data:`ALLOWED_NETWORK_HOSTS` to enforce two invariants:

1. Every URL constructed inside ``biotech_sniper/`` resolves to a
   hostname listed here (subset semantics).
2. No off-limits other-project domain (``hyperfund``, ``hl-grok``,
   ``hl-edge``, ``geo-shock``, ``ml_analysis``, ``palmpilot``, etc.)
   ever appears in this list.

The contract behind these invariants lives in:

* ``library/environment.md`` — narrative description of the egress
  policy and per-host purpose.
* ``validation-contract.md`` — VAL-M1-049 (whitelist completeness),
  VAL-M1-050 (VPS reachability sweep), VAL-M1-051 (no off-limits
  collisions), VAL-M1-063 (M1 ingestion subset), VAL-CROSS-043
  (codebase-wide subset audit).
* ``AGENTS.md`` — Mission Boundaries (HARD: paper-only, no live
  Alpaca, off-limits other-project domains).

Reading-B (this mission) introduces six new hosts spread across
M1 (universe + calendar foundations) and M3 (Stage-2 Perplexity
ensemble). M1 hosts are added in :data:`M1_NEW_HOSTS`. M3 hosts
are reserved in :data:`M3_RESERVED_HOSTS` and remain empty until
the f-m3-XX features wire them in (api.perplexity.ai and
status.perplexity.ai).

The list is a :class:`frozenset` (immutable) so consumers cannot
accidentally mutate it at run time.
"""

from __future__ import annotations

from typing import Final


# ---------------------------------------------------------------------------
# M1 — Reading-B Universe & Calendar Foundations
# ---------------------------------------------------------------------------
#
# Hosts contacted by:
#
# * ``biotech_sniper/universe/iwm_importer.py``
#   — ``https://www.ishares.com/us/products/239710/...`` (IWM CSV).
# * ``biotech_sniper/classifiers/sec_sic.py``
#   — ``https://www.sec.gov/files/company_tickers_exchange.json``
#     (already on the prior-mission allow-list — SEC ticker map).
#   — ``https://data.sec.gov/submissions/CIK<10-digit>.json``
#     (NEW — per-CIK SIC submissions endpoint).
# * ``biotech_sniper/calendar/pdufa.py``
#   — Default upstream: ``https://www.biopharmcatalyst.com/...``
#     (already on the prior-mission allow-list).
#   — Override: ``https://www.fda.gov/...`` (operator override and
#     paper-tail / FDA approvals page).
# * ``biotech_sniper/calendar/ema.py``
#   — ``https://www.ema.europa.eu/en/committees/chmp/...`` (EMA
#     CHMP meeting highlights).
#
# api.perplexity.ai / status.perplexity.ai are M3 only — see
# :data:`M3_RESERVED_HOSTS`.
M1_NEW_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "www.ishares.com",
        "data.sec.gov",
        "www.fda.gov",
        "www.ema.europa.eu",
    }
)


# ---------------------------------------------------------------------------
# M3 — Reading-B Stage-2 LLM ensemble (RESERVED, empty until f-m3-XX wires)
# ---------------------------------------------------------------------------
#
# Reserved host slots for M3:
#   * ``api.perplexity.ai``      — Stage-2 LLM provider.
#   * ``status.perplexity.ai``   — health monitoring / breaker poll.
#
# Activated by f-m3-01-perplexity-client (the Perplexity HTTP client
# wrapper). Tests for the client run hermetically against committed
# cassettes under ``tests/fixtures/cassettes/perplexity/`` (no live
# egress); the host is reserved here so the production code path
# (``biotech_sniper.llm.perplexity_client``) — which dials
# ``https://api.perplexity.ai/chat/completions`` — is not flagged as
# off-list by the network whitelist regression tests.
M3_RESERVED_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "api.perplexity.ai",
        "status.perplexity.ai",
    }
)


# ---------------------------------------------------------------------------
# Prior-mission allow-list (existing trading + LLM + research APIs and
# RSS feed hosts, unchanged by Reading-B).
# ---------------------------------------------------------------------------
#
# These hosts pre-date Reading-B; they are referenced from existing
# modules and ``audit.py`` probes. They remain in scope post-M1.
PRIOR_MISSION_HOSTS: Final[frozenset[str]] = frozenset(
    {
        # Trading APIs — paper-only. ``api.alpaca.markets`` is
        # deliberately omitted: the LIVE_MODE two-flag gate plus
        # this whitelist together prevent any accidental real-money
        # order submission.
        "paper-api.alpaca.markets",
        "data.alpaca.markets",
        # LLM providers — Stage-1 daily ranker + 3-leg debate ensemble.
        "api.x.ai",
        "api.anthropic.com",
        "generativelanguage.googleapis.com",
        # Government / regulatory data sources.
        "clinicaltrials.gov",
        "www.sec.gov",
        "efts.sec.gov",
        "www.federalregister.gov",
        "api.usaspending.gov",
        "www.defense.gov",
        "sam.gov",
        "eutils.ncbi.nlm.nih.gov",
        # Catalyst calendar HTML — used by master_discovery,
        # universal_news_watcher, AND the Reading-B PDUFA scraper
        # as the default upstream.
        "www.biopharmcatalyst.com",
        "www.fdatracker.com",
        # News RSS feed hosts — daily news ingest + intraday scanner.
        "feeds.feedburner.com",
        "endpts.com",
        "www.fiercebiotech.com",
        "www.biopharmadive.com",
        "www.medpagetoday.com",
        "www.statnews.com",
        "www.globenewswire.com",
        "www.prnewswire.com",
        # Research tooling.
        "warpspeed.sh",
        "html.duckduckgo.com",
    }
)


# ---------------------------------------------------------------------------
# Canonical allow-list (the union of every above category).
# ---------------------------------------------------------------------------
#: The single canonical allow-list of public hostnames this project is
#: permitted to contact. Frozen so consumers cannot mutate it at run time.
ALLOWED_NETWORK_HOSTS: Final[frozenset[str]] = (
    M1_NEW_HOSTS | PRIOR_MISSION_HOSTS | M3_RESERVED_HOSTS
)


# ---------------------------------------------------------------------------
# Off-limits domain tokens (NEVER appear in :data:`ALLOWED_NETWORK_HOSTS`).
# ---------------------------------------------------------------------------
#
# The VPS at root@199.247.25.111 is multi-tenant. Several other
# projects share the same root filesystem; their internal hosts
# (``hl-grok``, ``hl-edge``, ``hyperfund``, ``geo-shock``,
# ``ml_analysis``, ``palmpilot``, ``tkl-signal-engine``,
# ``shadow_scorer``, ``beam-content-watcher``, ``parallel_shadow``)
# MUST NOT be contacted by Biotech Sniper code.
#
# VAL-M1-051 grep'd these exact tokens against the whitelist and
# the policy file; the regression test in
# ``tests/test_network_whitelist.py`` enforces the same invariant.
OFF_LIMITS_DOMAIN_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "hyperfund",
        "hl-grok",
        "hl-edge",
        "geo-shock",
        "ml_analysis",
        "palmpilot",
        "tkl-signal-engine",
        "shadow_scorer",
        "beam-content-watcher",
        "parallel_shadow",
    }
)


__all__ = [
    "ALLOWED_NETWORK_HOSTS",
    "M1_NEW_HOSTS",
    "M3_RESERVED_HOSTS",
    "OFF_LIMITS_DOMAIN_TOKENS",
    "PRIOR_MISSION_HOSTS",
]
