"""Catalyst keyword + trial-calendar matcher.

The matcher consumes a single ``news_events`` row and returns a
:class:`MatchResult` describing the matched keywords (sorted,
deduped) and the trial-calendar lookup outcome.  Multiple matching
keywords on a single row collapse to ONE :class:`MatchResult`
— Stage-1 emits a single ``candidate_events`` row per
``(ticker, news_events.id)`` regardless of vocab fan-out (per
VAL-M2-019).

Vocab reuse contract (VAL-M2-015 / f-m2-05)
-------------------------------------------

The catalyst keyword vocabulary is **imported, never redefined**
from :mod:`biotech_sniper.intelligence.universal_news_watcher`.
This module exposes :data:`CATALYST_KEYWORDS` as a
:class:`frozenset` union of ``TIER_1_SIGNALS ∪ TIER_2_SIGNALS``.

The single-source-of-truth invariant is enforced by a CI grep over
this file: any ``TIER_1_SIGNALS = [`` / ``TIER_2_SIGNALS = [``
literal redefinition fails the validator (VAL-M2-015 evidence).

The TIER lists themselves were extended in f-m2-05 to add:

* Partnership / collaboration / license vocab without any
  $-threshold (Stage-2 unanimity filters small deals): ``partnership``,
  ``collaboration``, ``license agreement``, ``licensing agreement``,
  ``license deal``, ``co-development``, ``option agreement``,
  ``collaboration agreement``.
* M&A rumour vocab: ``acquisition``, ``acquires``, ``to acquire``,
  ``agreed to be acquired``, ``merger``, ``buyout``, ``take-private``,
  ``take private``, ``strategic alternatives``, ``tender offer``.
* Regulatory submission vocab: ``ind filing``,
  ``investigational new drug``, ``nda submission``,
  ``new drug application``, ``bla submission``,
  ``biologics license application``, ``snda``.

Trial-calendar lookup (VAL-M2-020 / VAL-M2-021)
------------------------------------------------

After a positive keyword match, the matcher consults the
``trial_calendar`` table via
:func:`biotech_sniper.calendar.trial_calendar.get_next_catalyst`.

When a row exists for the ticker AND the catalyst_date is within
:data:`CALENDAR_LOOKUP_WINDOW_DAYS` of "now", the
:attr:`MatchResult.calendar_match` field is populated as a JSON
string ``{"source": "trial_calendar", "catalyst_date":
"YYYY-MM-DD", "days_until": int}`` — Stage-2 reads this for
catalyst-type routing.

When the LEFT-JOIN misses (no row, or a row outside the window,
or the ``trial_calendar`` table itself is absent — M1 not yet
run), :attr:`MatchResult.calendar_match` is left as :data:`None`.
This is **not** an error: VAL-M2-021 pins that the candidate is
still emitted with ``calendar_match`` NULL.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Tuple

from biotech_sniper.intelligence.universal_news_watcher import (
    TIER_1_SIGNALS as _TIER_1_LIST,
    TIER_2_SIGNALS as _TIER_2_LIST,
)

__all__ = [
    "CATALYST_KEYWORDS",
    "CALENDAR_LOOKUP_WINDOW_DAYS",
    "MatchResult",
    "match_news_row",
    "match_keywords",
]


#: Stage-1 catalyst vocabulary — the union of TIER-1 and TIER-2
#: signals from
#: :mod:`biotech_sniper.intelligence.universal_news_watcher`.
#:
#: Frozenset to make :data:`CATALYST_KEYWORDS <= (TIER_1_SIGNALS |
#: TIER_2_SIGNALS)` evaluate as a set-subset relation in the f-m2-05
#: verification step (and to make membership lookups O(1) per
#: keyword on the haystack).
#:
#: NEVER redefine TIER_1_SIGNALS / TIER_2_SIGNALS literals here —
#: the single-source-of-truth invariant is asserted by VAL-M2-015.
CATALYST_KEYWORDS: frozenset[str] = (
    frozenset(_TIER_1_LIST) | frozenset(_TIER_2_LIST)
)


#: Window (in days) over which a trial-calendar entry is considered a
#: "match" for the candidate's :attr:`MatchResult.calendar_match`
#: payload.  The Stage-2 ensemble decides whether the catalyst is
#: imminent enough to gate the entry; Stage-1 just records the
#: lookup outcome for downstream consumers.
CALENDAR_LOOKUP_WINDOW_DAYS: int = 90


# Pre-compiled per-keyword word-boundary regexes.  Built once at
# import time so the poll loop's per-row matching is cheap.  Each
# keyword is matched case-insensitively with ``\b`` boundaries on
# both sides for word-shaped keywords; multi-word keywords (e.g.
# ``"new drug application"``) embed a relaxed whitespace
# ``\s+`` separator so a headline that uses a single space, an
# en-dash, or wrapped whitespace still matches.
def _compile_keyword(kw: str) -> re.Pattern[str]:
    """Compile one keyword into a case-insensitive matcher.

    Word boundaries are applied at the outer edges only.  Internal
    whitespace is tolerant (one-or-more whitespace chars), and an
    internal hyphen accepts either ``-`` or `` `` (the headline
    writer's choice).
    """

    pieces = kw.split()
    escaped = [re.escape(piece).replace(r"\-", r"[-\s]") for piece in pieces]
    body = r"\s+".join(escaped)
    # ``\b`` doesn't fire next to non-word characters like ``-`` —
    # use a softer lookaround that accepts string boundary OR a
    # non-letter char on either side.  This lets ``snda`` match in
    # ``"a sNDA was filed."`` but not in ``"asndas"``.
    pattern = (
        r"(?<![A-Za-z0-9])"
        + body
        + r"(?![A-Za-z0-9])"
    )
    return re.compile(pattern, re.IGNORECASE)


_KEYWORD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    sorted(
        ((kw, _compile_keyword(kw)) for kw in CATALYST_KEYWORDS),
        key=lambda pair: pair[0],
    )
)


@dataclass(frozen=True)
class MatchResult:
    """Outcome of matching a single news_events row.

    Attributes
    ----------
    matched_keywords:
        Sorted, deduped tuple of catalyst keywords matched on the
        row.  Empty tuple = no match (caller skips the row).
    calendar_match:
        Optional structured-string lookup result.  When a
        trial_calendar entry exists for the ticker within the
        :data:`CALENDAR_LOOKUP_WINDOW_DAYS` window, this is a JSON
        string ``{"source": ..., "catalyst_date": "YYYY-MM-DD",
        "days_until": int}``.  When the LEFT-JOIN misses, this is
        :data:`None`.  Per VAL-M2-021 a miss is **not** an error —
        the candidate is still emitted.
    """

    matched_keywords: Tuple[str, ...] = field(default_factory=tuple)
    calendar_match: Optional[str] = None

    @property
    def is_match(self) -> bool:
        """``True`` when at least one keyword matched."""

        return bool(self.matched_keywords)

    @property
    def matched_keywords_csv(self) -> str:
        """Sorted comma-joined CSV form for ``candidate_events.matched_keywords``.

        Deterministic across runs (already sorted at construction
        time) so the dedup_key formula is stable.
        """

        return ",".join(self.matched_keywords)


def match_keywords(text: str) -> Tuple[str, ...]:
    """Return the sorted, deduped tuple of catalyst keywords in ``text``.

    Implementation detail of :func:`match_news_row`, exposed for
    unit tests so the keyword pass and the calendar pass can be
    exercised independently.

    Parameters
    ----------
    text:
        The combined headline + body string (case-insensitive).

    Returns
    -------
    tuple[str, ...]
        Sorted ascending tuple of unique keyword literals from
        :data:`CATALYST_KEYWORDS` that appear in ``text``.  Empty
        when no keyword matches.
    """

    if not text:
        return ()
    matches: set[str] = set()
    for keyword, pattern in _KEYWORD_PATTERNS:
        if pattern.search(text):
            matches.add(keyword)
    return tuple(sorted(matches))


def _today_utc() -> datetime.date:
    """Return today's date in UTC.  Wrapped for monkeypatching in tests."""

    return datetime.datetime.now(datetime.timezone.utc).date()


def _lookup_calendar_match(
    ticker: str,
    *,
    db_path: Optional[str],
    window_days: int,
    today: Optional[datetime.date] = None,
) -> Optional[str]:
    """Look up a trial_calendar row for ``ticker`` and shape the payload.

    Returns the JSON payload string on a hit within the window, or
    :data:`None` on any of:

    * empty/blank ticker
    * missing trial_calendar table (M1 not yet run)
    * no row for the ticker
    * row exists but ``catalyst_date`` is outside the window or
      malformed

    The function NEVER raises — every failure mode degrades to
    "no calendar match" so the daemon emits the candidate without
    blocking the Stage-1 path.
    """

    if not ticker or not isinstance(ticker, str):
        return None

    log = logging.getLogger("biotech_sniper.news_daemon.matcher")

    try:
        # Lazy import — keeps the matcher's import side-effect
        # surface tight.  ``trial_calendar`` is part of the M1
        # foundations and pulls in ``biotech_sniper.db`` (which
        # opens a sqlite3 connection on demand only).
        from biotech_sniper.calendar.trial_calendar import get_next_catalyst
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(
            "trial_calendar lookup unavailable: %r", exc,
            extra={
                "event": "news_daemon_calendar_import_error",
                "src_module": "news_daemon.matcher",
                "ticker": ticker,
            },
        )
        return None

    try:
        catalyst_date_iso = get_next_catalyst(ticker, db_path=db_path)
    except Exception as exc:  # pragma: no cover - defensive
        # Calendar lookup must NEVER block emission (VAL-M2-021).
        log.warning(
            "trial_calendar lookup failed: %r", exc,
            extra={
                "event": "news_daemon_calendar_lookup_error",
                "src_module": "news_daemon.matcher",
                "ticker": ticker,
            },
        )
        return None

    if not catalyst_date_iso:
        return None

    try:
        catalyst_date = datetime.date.fromisoformat(catalyst_date_iso)
    except (TypeError, ValueError):
        return None

    today_date = today or _today_utc()
    delta = (catalyst_date - today_date).days

    # A catalyst already past the date is still potentially relevant
    # for retrospective analysis but we limit to the forward window
    # so Stage-2 only sees imminent-or-recent catalysts.
    if delta < -window_days or delta > window_days:
        return None

    payload = {
        "source": "trial_calendar",
        "catalyst_date": catalyst_date_iso,
        "days_until": int(delta),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def match_news_row(
    ticker: str,
    headline: str,
    body: str = "",
    *,
    db_path: Optional[str] = None,
    today: Optional[datetime.date] = None,
    window_days: int = CALENDAR_LOOKUP_WINDOW_DAYS,
) -> MatchResult:
    """Match a single news_events row against the Stage-1 vocab.

    Returns a :class:`MatchResult`.  When at least one catalyst
    keyword fires, the returned ``matched_keywords`` is a sorted,
    deduped tuple AND a trial_calendar lookup is attempted on
    ``ticker``.  When no keyword matches, an empty
    :class:`MatchResult` is returned and no calendar lookup is
    performed (saves a SQLite roundtrip on the cold path).

    Parameters
    ----------
    ticker:
        Upper-case canonical ticker symbol.  Empty / blank ticker
        rows are silently rejected by the scope filter upstream;
        this function tolerates a blank ticker by returning an
        empty result.
    headline:
        News headline (string).  Combined with ``body`` for the
        keyword scan.
    body:
        Optional body text.  May be empty.
    db_path:
        Optional override for the SQLite database path passed to
        :func:`biotech_sniper.calendar.trial_calendar.get_next_catalyst`.
        Tests pass a tmp-path DB; production callers omit and
        accept the canonical path.
    today:
        Optional override for "today" — used by tests to pin the
        calendar window relative to a fixed date.  Defaults to
        :func:`_today_utc`.
    window_days:
        Forward/backward window for the calendar lookup; rows
        outside this window degrade to ``calendar_match=None``.

    Returns
    -------
    MatchResult
        Frozen dataclass.  ``is_match`` is False when
        ``matched_keywords`` is empty.
    """

    if not isinstance(headline, str):
        headline = ""
    if not isinstance(body, str):
        body = ""

    haystack = f"{headline}\n{body}" if body else headline
    matched = match_keywords(haystack)
    if not matched:
        return MatchResult()

    calendar_payload = _lookup_calendar_match(
        ticker,
        db_path=db_path,
        window_days=window_days,
        today=today,
    )

    return MatchResult(
        matched_keywords=matched,
        calendar_match=calendar_payload,
    )


def match_text(
    text: str,
    keywords: Optional[Iterable[str]] = None,
) -> Tuple[str, ...]:
    """Backward-compat alias for :func:`match_keywords`.

    ``keywords`` is accepted but ignored — the canonical vocab is
    sourced from :data:`CATALYST_KEYWORDS`.  Kept for callers that
    want a named entry point distinct from
    :func:`match_news_row`.
    """

    del keywords  # vocab is fixed; argument retained for API compat
    return match_keywords(text)
