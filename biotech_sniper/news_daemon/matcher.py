"""Catalyst keyword + trial-calendar matcher.

The matcher consumes a single ``news_events`` row and returns a
``MatchResult`` describing the matched keywords (sorted, comma-joined,
deduped) and the trial-calendar lookup outcome.  Multiple matching
keywords on a single row collapse to ONE :class:`MatchResult`
(per VAL-M2-005) — Stage-1 emits a single ``candidate_events`` row
per ``(ticker, news_events.id)`` regardless of vocab fan-out.

Skeleton (f-m2-01)
-------------------

Real keyword vocab REUSE from
:mod:`biotech_sniper.intelligence.universal_news_watcher`
(``TIER_1_SIGNALS`` ∪ ``TIER_2_SIGNALS``) plus the partnership /
M&A / IND-NDA-BLA-sNDA additions and the trial_calendar lookup land
in f-m2-05 and are asserted by VAL-M2-015..023.  This file documents
the public surface; the body is a structured no-op so the package
imports cleanly without pulling in the universal_news_watcher
heavy-weight import chain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

__all__ = [
    "MatchResult",
    "match_news_row",
]


@dataclass(frozen=True)
class MatchResult:
    """Outcome of matching a single news_events row.

    Attributes
    ----------
    matched_keywords:
        Sorted, deduped tuple of catalyst keywords matched on the
        row.  Empty tuple = no match (caller skips the row).
    calendar_match:
        Optional ``(source, catalyst_date_iso)`` pair from the
        trial_calendar lookup; ``None`` when the ticker has no
        upcoming catalyst within the lookup window.  A LEFT-JOIN
        miss is NOT an error — the candidate is still emitted.
    """

    matched_keywords: Tuple[str, ...] = field(default_factory=tuple)
    calendar_match: Optional[Tuple[str, str]] = None

    @property
    def is_match(self) -> bool:
        """``True`` when at least one keyword matched."""

        return bool(self.matched_keywords)


def match_news_row(
    ticker: str,
    headline: str,
    body: str = "",
    *,
    db_path: Optional[str] = None,
) -> MatchResult:
    """Match a single news_events row against the Stage-1 vocab.

    Skeleton stub (f-m2-01): always returns an empty
    :class:`MatchResult` (``is_match=False``) so the package import
    smoke-test passes without pulling in heavy keyword dependencies.
    f-m2-05 fills this in with the real TIER-1/TIER-2 vocab REUSE
    and trial_calendar lookup.
    """

    return MatchResult()
