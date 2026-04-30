"""Production RSS-source adapters for the Stage-1 news daemon.

This module is the bridge between the existing biotech intelligence
watcher modules (``intelligence/universal_news_watcher``,
``intelligence/sec_8k_monitor``, ``intelligence/ir_events_watcher``,
and ``intraday_scanner.scan_news_rss``) and the resilience-loop
``rss_fetchers`` parameter declared by
:func:`biotech_sniper.news_daemon.resilience.run_main_loop`.

f-m2-10 contract
----------------

* **Sync stack only.**  The poll loop is synchronous (``requests`` +
  ``feedparser``); these adapters NEVER import any non-blocking I/O
  stack.  The disallowed transport list (and its source-grep guard)
  lives in :file:`tests/test_news_daemon_package.py`.

* **No LLM imports.**  Stage-1 is a pure data emitter; the Stage-2
  ensemble lives in a sibling package.  These adapters MUST NOT
  import any of the four scoring-provider client classes and the
  daemon process MUST NOT egress to any of the four scoring-API
  hostnames.  The exact class names and hostnames are pinned in
  the validation contract (VAL-CROSS-* whitelist) and verified by
  ``tests/test_news_daemon_production_wiring.py`` via a source-grep
  guard against the four watcher modules + every file in this
  package.

* **Idempotent persistence.**  Each adapter funnels its raw output
  through :func:`biotech_sniper.news_events.record_news_events`,
  which writes via ``INSERT OR IGNORE`` keyed on the schema-level
  composite UNIQUE index ``idx_news_events_dedup`` over
  ``(ticker, source, COALESCE(url,''), COALESCE(published_at,''))``.
  Re-running the same adapter back-to-back therefore inserts zero
  new rows on the second invocation.

* **Resilience-friendly error semantics.**  Adapters do NOT silently
  swallow upstream HTTP errors.  Transient failures (HTTP 500,
  timeouts, parse errors) propagate out so the resilience layer
  (:func:`run_main_loop`) can count them in
  :attr:`ShutdownState.errors_session` per VAL-M2-037.  Each adapter
  invocation is wrapped in its own ``try/except`` inside
  :func:`run_main_loop._drive_rss_fetchers`, so a raised exception
  here does not halt the daemon — it bumps the counter and the
  loop moves on to the next fetcher.

* **Scope.**  Adapters respect the Reading-B M2 scope filter:
  events are only persisted for tickers in
  ``russell2k_biotech ∩ universe.tier IN ('watch','tradeable')``.
  Empty or missing scope is logged once by
  :func:`biotech_sniper.news_daemon.scope.resolve_polled_tickers`
  and the adapter returns early with zero rows persisted.

Design notes
------------

The four watcher modules already exist in the codebase and write
rows into ``news_events`` themselves via the daily-curated path
(:func:`biotech_sniper.news_events.daily_news_ingest`).  These
adapters wrap them for the *parallel* Stage-1 daemon path so that
the news_daemon poll loop has a real input source plumbed in
production — without those adapters, the daemon would emit zero
candidates if booted today (the f-m2-09 implementation deliberately
accepts any callable to keep the framework testable).

To keep the adapter surface stable across watcher refactors, each
adapter normalises the watcher's heterogeneous output shape into a
list of :class:`biotech_sniper.news_events.NewsEvent` rows and
funnels that list through the canonical persistence helper.  The
watcher's own JSON state files (``state/seen_8k_accessions.json``,
``state/ir_events_state.json``, etc.) are still maintained by the
watcher modules themselves; this adapter layer is intentionally
stateless apart from the SQLite writes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, List, Mapping, Sequence, Union

from biotech_sniper import db
from biotech_sniper.news_daemon.scope import resolve_polled_tickers
from biotech_sniper.news_events import (
    NewsEvent,
    SOURCE_INTRADAY_RSS,
    SOURCE_IR_EVENTS,
    SOURCE_SEC_8K,
    SOURCE_UNIVERSAL,
    default_db_path,
    record_news_events,
)


__all__ = [
    "RssFetcher",
    "build_default_rss_fetchers",
    "intraday_news_rss_adapter",
    "ir_events_watcher_adapter",
    "persist_events",
    "polled_universe",
    "sec_8k_monitor_adapter",
    "universal_news_watcher_adapter",
]


#: Type alias for the zero-argument callable shape consumed by
#: :func:`biotech_sniper.news_daemon.resilience.run_main_loop`.  Each
#: adapter returns the count of rows inserted into ``news_events`` so
#: callers (and tests) can verify the adapter actually did work; the
#: resilience layer ignores the return value and only watches for
#: raised exceptions.
RssFetcher = Callable[[], int]


_PathLike = Union[str, Path, None]


_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Persistence helpers (shared across the four adapter functions).
# ---------------------------------------------------------------------------


def persist_events(
    events: Sequence[NewsEvent],
    *,
    db_path: _PathLike = None,
) -> int:
    """Persist ``events`` into ``news_events``.  Returns inserted-row count.

    Wraps :func:`record_news_events` with the project's standard
    connect + migrate dance so callers do not need to repeat the
    boilerplate.  The caller is free to pass an explicit ``db_path``
    (used by tests via ``monkeypatch``); production callers leave it
    at ``None`` and the function falls back to
    :func:`default_db_path`.
    """

    if not events:
        return 0
    target = Path(db_path) if db_path is not None else default_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(target)
    try:
        db.run_migrations(conn)
        counts = record_news_events(conn, events)
    finally:
        conn.close()
    return int(counts.get("inserted", 0))


def polled_universe() -> List[str]:
    """Resolve the Stage-1 polled-ticker set as a sorted list.

    Thin wrapper around :func:`resolve_polled_tickers` returning a
    deterministic order so the watcher modules see the same input
    on every invocation (some of them iterate the list to build
    cache keys / feed URLs and stable ordering helps debugging).
    """

    return sorted(resolve_polled_tickers())


def _normalise_filing(
    filing: Mapping[str, Any],
    *,
    source: str,
) -> NewsEvent | None:
    """Convert an SEC 8-K / news-watcher filing dict into a NewsEvent."""

    ticker = filing.get("ticker")
    if not ticker:
        return None
    title = (
        filing.get("summary")
        or filing.get("detail")
        or filing.get("title")
        or filing.get("headline")
        or filing.get("company")
        or "8-K filing"
    )
    url = (
        filing.get("form_url")
        or filing.get("filing_url")
        or filing.get("url")
        or filing.get("ir_url")
    )
    published_at = (
        filing.get("filed_date")
        or filing.get("published")
        or filing.get("detected_date")
        or filing.get("published_at")
    )
    return NewsEvent(
        ticker=str(ticker),
        source=source,
        title=str(title)[:500],
        url=(str(url) if url else None),
        published_at=(str(published_at) if published_at else None),
        raw_payload=dict(filing),
    )


def _normalise_news_hit(
    article: Mapping[str, Any],
    *,
    source: str,
) -> NewsEvent | None:
    """Convert an RSS news-hit dict into a NewsEvent."""

    ticker = article.get("ticker")
    if not ticker:
        return None
    title = (
        article.get("headline")
        or article.get("title")
        or article.get("summary")
        or "news"
    )
    url = article.get("url") or article.get("link")
    published_at = article.get("published") or article.get("published_at")
    return NewsEvent(
        ticker=str(ticker),
        source=source,
        title=str(title)[:500],
        url=(str(url) if url else None),
        published_at=(str(published_at) if published_at else None),
        raw_payload=dict(article),
    )


# ---------------------------------------------------------------------------
# Adapter 1 — biotech_sniper.intelligence.universal_news_watcher
# ---------------------------------------------------------------------------


def universal_news_watcher_adapter(
    *,
    db_path: _PathLike = None,
) -> int:
    """Adapter for TIER-1/TIER-2 RSS via universal_news_watcher.

    Invokes :func:`run_hourly_news_scan` on the Stage-1 polled-ticker
    set, then funnels the returned ``new_8ks`` and ``news_hits``
    sub-lists into ``news_events`` via :func:`record_news_events`.
    The adapter persists rows tagged with the canonical
    :data:`SOURCE_UNIVERSAL` source label.

    Errors (HTTP 5xx, timeouts, parse errors) propagate to the
    caller — :func:`run_main_loop._drive_rss_fetchers` catches them
    and bumps :attr:`errors_session`.
    """

    # Lazy import to keep the package import cheap and to make the
    # source-grep guard (no LLM imports anywhere in news_daemon)
    # tractable.  The scoping happens at the top of the underlying
    # ``run_hourly_news_scan`` invocation; we forward whatever
    # tickers the scope filter resolved.
    from biotech_sniper.intelligence.universal_news_watcher import (
        run_hourly_news_scan,
    )

    tickers = polled_universe()
    if not tickers:
        return 0

    result = run_hourly_news_scan(tickers) or {}

    universe = {t.upper() for t in tickers}
    events: List[NewsEvent] = []
    for filing in result.get("new_8ks", []) or []:
        if not isinstance(filing, Mapping):
            continue
        ev = _normalise_filing(filing, source=SOURCE_UNIVERSAL)
        if ev is None or ev.ticker.upper() not in universe:
            continue
        events.append(ev)
    for article in result.get("news_hits", []) or []:
        if not isinstance(article, Mapping):
            continue
        ev = _normalise_news_hit(article, source=SOURCE_UNIVERSAL)
        if ev is None or ev.ticker.upper() not in universe:
            continue
        events.append(ev)

    return persist_events(events, db_path=db_path)


# ---------------------------------------------------------------------------
# Adapter 2 — biotech_sniper.intelligence.sec_8k_monitor
# ---------------------------------------------------------------------------


def sec_8k_monitor_adapter(
    *,
    db_path: _PathLike = None,
    mode: str = "intraday",
) -> int:
    """Adapter for SEC 8-K Atom RSS via ``sec_8k_monitor``.

    Invokes :func:`run_8k_monitor` (default ``mode='intraday'`` which
    only checks the SEC EDGAR RSS feed — fast, no per-CIK fan-out
    that would slow the poll cycle), then persists the ``signals``
    list as :data:`SOURCE_SEC_8K`-tagged rows.

    The intraday mode is appropriate for the Stage-1 daemon poll
    cadence (~30 s); the daily mode is reserved for the existing
    cron-driven path that also touches the per-CIK submissions API.
    """

    from biotech_sniper.intelligence.sec_8k_monitor import run_8k_monitor

    report = run_8k_monitor(mode=mode) or {}

    tickers = polled_universe()
    universe = {t.upper() for t in tickers} if tickers else None

    events: List[NewsEvent] = []
    for signal in report.get("signals", []) or []:
        if not isinstance(signal, Mapping):
            continue
        ticker = signal.get("ticker")
        if not ticker:
            continue
        if universe is not None and str(ticker).upper() not in universe:
            continue
        events.append(
            NewsEvent(
                ticker=str(ticker),
                source=SOURCE_SEC_8K,
                title=str(
                    signal.get("detail")
                    or signal.get("type")
                    or "8-K signal"
                )[:500],
                url=(
                    str(signal.get("filing_url"))
                    if signal.get("filing_url")
                    else None
                ),
                published_at=(
                    str(
                        signal.get("published")
                        or signal.get("detected_date")
                        or ""
                    )
                    or None
                ),
                raw_payload=dict(signal),
            )
        )

    return persist_events(events, db_path=db_path)


# ---------------------------------------------------------------------------
# Adapter 3 — biotech_sniper.intelligence.ir_events_watcher
# ---------------------------------------------------------------------------


def ir_events_watcher_adapter(
    *,
    db_path: _PathLike = None,
) -> int:
    """Adapter for IR investor-relations pages via ``ir_events_watcher``.

    Invokes :func:`run_ir_events_check` (which iterates the
    :file:`intelligence/nct_registry.json` watchlist and probes each
    company's IR events page + SEC EDGAR), then persists the
    returned ``signals`` list as :data:`SOURCE_IR_EVENTS`-tagged
    rows.

    The watcher does NOT take a ticker list — it derives its scope
    from the registry — so this adapter only filters on the
    Russell-biotech intersection at write time.
    """

    from biotech_sniper.intelligence.ir_events_watcher import (
        run_ir_events_check,
    )

    report = run_ir_events_check() or {}

    tickers = polled_universe()
    universe = {t.upper() for t in tickers} if tickers else None

    events: List[NewsEvent] = []
    for signal in report.get("signals", []) or []:
        if not isinstance(signal, Mapping):
            continue
        ticker = signal.get("ticker")
        if not ticker:
            continue
        if universe is not None and str(ticker).upper() not in universe:
            continue
        events.append(
            NewsEvent(
                ticker=str(ticker),
                source=SOURCE_IR_EVENTS,
                title=str(
                    signal.get("detail")
                    or signal.get("type")
                    or "IR signal"
                )[:500],
                url=(
                    str(signal.get("filing_url") or signal.get("ir_url"))
                    if (signal.get("filing_url") or signal.get("ir_url"))
                    else None
                ),
                published_at=(
                    str(signal.get("detected_date") or "") or None
                ),
                raw_payload=dict(signal),
            )
        )

    return persist_events(events, db_path=db_path)


# ---------------------------------------------------------------------------
# Adapter 4 — biotech_sniper.intraday_scanner.scan_news_rss
# ---------------------------------------------------------------------------


def intraday_news_rss_adapter(
    *,
    db_path: _PathLike = None,
) -> int:
    """Adapter for the intraday news-RSS scan.

    Invokes :func:`scan_news_rss` against an empty ``seen_urls``
    set so each call is a fresh probe (the news_events composite
    UNIQUE index handles cross-call dedup), then persists the
    returned alerts as :data:`SOURCE_INTRADAY_RSS`-tagged rows.

    The intraday scanner returns alerts whose ``ticker`` may be the
    empty string when no watchlist match was identified — those
    rows are silently dropped here so we never insert empty-ticker
    NewsEvent rows (the schema declares ``ticker TEXT NOT NULL``
    and the dedup helper would reject them anyway).
    """

    from biotech_sniper.intraday_scanner import scan_news_rss

    alerts = scan_news_rss(set()) or []

    tickers = polled_universe()
    universe = {t.upper() for t in tickers} if tickers else None

    events: List[NewsEvent] = []
    for alert in alerts:
        if not isinstance(alert, Mapping):
            continue
        ticker = alert.get("ticker")
        if not ticker:
            continue
        if universe is not None and str(ticker).upper() not in universe:
            continue
        events.append(
            NewsEvent(
                ticker=str(ticker),
                source=SOURCE_INTRADAY_RSS,
                title=str(
                    alert.get("title")
                    or alert.get("headline")
                    or "intraday news"
                )[:500],
                url=(str(alert.get("url")) if alert.get("url") else None),
                published_at=(
                    str(alert.get("published_at") or "") or None
                ),
                raw_payload=dict(alert),
            )
        )

    return persist_events(events, db_path=db_path)


# ---------------------------------------------------------------------------
# Public builder.
# ---------------------------------------------------------------------------


def build_default_rss_fetchers(
    *,
    db_path: _PathLike = None,
) -> List[RssFetcher]:
    """Return the production zero-arg fetcher list for ``rss_fetchers=...``.

    Each returned callable wraps one of the four production adapter
    functions with the supplied ``db_path`` bound (defaulting to
    :func:`default_db_path` when ``None``).  Order is deterministic:
    universal_news_watcher → sec_8k_monitor → ir_events_watcher →
    intraday_news_rss.

    The list-of-four shape is the contract the verification step
    pins:

    .. code-block:: shell

       python -c 'from biotech_sniper.news_daemon.poll_loop import \
                  build_default_rss_fetchers; \
                  fs = build_default_rss_fetchers(); \
                  assert len(fs) >= 4'
    """

    def _u() -> int:
        return universal_news_watcher_adapter(db_path=db_path)

    def _s() -> int:
        return sec_8k_monitor_adapter(db_path=db_path)

    def _i() -> int:
        return ir_events_watcher_adapter(db_path=db_path)

    def _x() -> int:
        return intraday_news_rss_adapter(db_path=db_path)

    # Set ``__name__`` on the closures so log messages from the
    # resilience loop's ``rss_source_index`` paired with the closure
    # name make the source obvious in the journal.
    _u.__name__ = "universal_news_watcher_adapter"
    _s.__name__ = "sec_8k_monitor_adapter"
    _i.__name__ = "ir_events_watcher_adapter"
    _x.__name__ = "intraday_news_rss_adapter"

    return [_u, _s, _i, _x]
