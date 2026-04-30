"""Universe scope filter — russell2k_biotech ∩ universe.tier ∈ {watch, tradeable}.

This submodule narrows the polled-news cursor down to the addressable
Reading-B universe.  Tickers outside the intersection are NEVER
polled; tickers inside the intersection but with empty/whitespace
names are silently rejected.

Skeleton (f-m2-01)
-------------------

Real SQL + cache + logging lives in f-m2-04 (and is asserted by
VAL-M2-012..014).  This file documents the public surface so
sibling modules can import the symbols at the right place when the
real implementation lands.
"""

from __future__ import annotations

from typing import Iterable, Set

__all__ = [
    "ALLOWED_TIERS",
    "filter_universe",
    "load_polled_universe",
]

#: Universe tiers polled by the daemon.  Other tiers (``ignore``,
#: ``research``) are excluded from Stage-1 scope.
ALLOWED_TIERS: frozenset[str] = frozenset({"watch", "tradeable"})


def load_polled_universe(db_path: str) -> Set[str]:
    """Return the set of tickers in russell2k_biotech ∩ universe(allowed_tiers).

    Skeleton stub (f-m2-01): returns an empty set so callers can
    short-circuit cleanly when the real implementation has not yet
    landed.  f-m2-04 fills this in with a single ``JOIN`` query and
    a session-scoped cache invalidated by the russell2k_biotech
    refresh hook.
    """

    return set()


def filter_universe(
    tickers: Iterable[str],
    polled: Set[str],
) -> Set[str]:
    """Filter ``tickers`` to those present in ``polled``.

    Empty / whitespace-only tickers are silently rejected
    (per VAL-M2-013).  Comparison is case-sensitive; canonical
    upper-case is enforced upstream by the news_events writer.
    """

    out: Set[str] = set()
    for raw in tickers:
        if not raw:
            continue
        ticker = raw.strip()
        if not ticker:
            continue
        if ticker in polled:
            out.add(ticker)
    return out
