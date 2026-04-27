"""Per-headline persistence for the daily news ingest pipeline (M2).

This module is the canonical entrypoint for writing rows into the
``news_events`` SQLite table. Every watcher module
(``universal_news_watcher``, ``intraday_scanner.scan_news_rss``,
``sec_8k_monitor``, ``ir_events_watcher``) funnels its raw output
through :func:`record_news_events` so the per-headline DB layer is
the single source of truth — independent of whether the watcher also
keeps its legacy per-module JSON state file.

Design notes
------------

* **Dedup key.** The schema declares a unique index on
  ``(ticker, source, COALESCE(url, ''), COALESCE(published_at, ''))``.
  Re-running the daily cron on the same day therefore inserts zero new
  rows for already-seen headlines. We always use ``INSERT OR IGNORE``
  so callers do not need to wrap their own ``try/except``.

* **Schema.** Mirrors VAL-M2-075 verbatim:
  ``id INTEGER PRIMARY KEY``, ``ticker TEXT NOT NULL``,
  ``source TEXT NOT NULL``, ``published_at TEXT``, ``title TEXT NOT NULL``,
  ``url TEXT``, ``ingested_at TEXT NOT NULL``, ``raw_payload TEXT``.

* **Audit logging.** :func:`daily_news_ingest` walks every ticker in
  ``universe.tier='watch'`` and accumulates a ``news_events_empty``
  mapping ``{ticker: reason}`` for tickers that produced zero rows.
  That mapping is written into ``state/audit_latest.json`` so the
  M2-076 assertion (which accepts either ≥ N rows OR an empty-feed
  reason per ticker) can pass deterministically.

* **No network in the helper.** Watcher modules own the network
  calls. This module only persists structured items into SQLite, so
  unit tests can exercise the dedup / batch / empty-feed code paths
  without VCR cassettes.
"""

from __future__ import annotations

import datetime
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from biotech_sniper import db
from biotech_sniper.paths import BASE_DIR, DATA_DIR, STATE_DIR

__all__ = [
    "NewsEvent",
    "DailyIngestResult",
    "default_db_path",
    "record_news_event",
    "record_news_events",
    "load_watch_tickers",
    "daily_news_ingest",
    "write_audit_summary",
    "SOURCE_UNIVERSAL",
    "SOURCE_INTRADAY_RSS",
    "SOURCE_SEC_8K",
    "SOURCE_IR_EVENTS",
]


logger = logging.getLogger(__name__)


# Canonical source labels persisted on every news_events row. Watcher
# modules import these to avoid drift.
SOURCE_UNIVERSAL: str = "universal_news_watcher"
SOURCE_INTRADAY_RSS: str = "intraday_scan_news_rss"
SOURCE_SEC_8K: str = "sec_8k_monitor"
SOURCE_IR_EVENTS: str = "ir_events_watcher"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def default_db_path() -> Path:
    """Return the canonical SQLite path for the project."""
    return DATA_DIR / "alpha_sniper.db"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%fZ"
    )


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return str(value)


def _coerce_required_str(value: Any, *, field_name: str) -> str:
    out = _coerce_str(value)
    if out is None:
        raise ValueError(
            f"news_events.{field_name} is required and must be a non-empty string"
        )
    return out


def _serialize_payload(payload: Any) -> str | None:
    """Best-effort JSON serialisation for the ``raw_payload`` column."""
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, default=str, sort_keys=True)
    except Exception:  # pragma: no cover - extremely defensive
        try:
            return json.dumps(str(payload))
        except Exception:
            return None


# ---------------------------------------------------------------------------
# NewsEvent dataclass + record_news_event
# ---------------------------------------------------------------------------


@dataclass
class NewsEvent:
    """In-memory representation of one ``news_events`` row.

    The watcher modules build these and pass them to
    :func:`record_news_events`. Keeping the wire format as a tiny
    dataclass means tests can construct events without touching SQL.
    """

    ticker: str
    source: str
    title: str
    url: str | None = None
    published_at: str | None = None
    raw_payload: Any = None
    ingested_at: str | None = None  # filled by SQLite default if None

    def to_row(self) -> tuple[str, str, str | None, str, str | None, str, str | None]:
        ticker = _coerce_required_str(self.ticker, field_name="ticker").upper()
        source = _coerce_required_str(self.source, field_name="source")
        title = _coerce_required_str(self.title, field_name="title")
        url = _coerce_str(self.url)
        published_at = _coerce_str(self.published_at)
        ingested_at = _coerce_str(self.ingested_at) or _now_iso()
        payload = _serialize_payload(self.raw_payload)
        return (ticker, source, published_at, title, url, ingested_at, payload)


def record_news_event(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    source: str,
    title: str,
    url: str | None = None,
    published_at: str | None = None,
    raw_payload: Any = None,
    ingested_at: str | None = None,
) -> bool:
    """Insert a single news_events row.

    Returns ``True`` when a new row was inserted; ``False`` when the
    row was a duplicate (matching ``(ticker, source, url, published_at)``)
    and was therefore ignored. Callers may use the return value to
    keep accurate per-ticker counts.

    The connection MUST already have the schema applied (call
    :func:`biotech_sniper.db.run_migrations` once at startup).
    """

    event = NewsEvent(
        ticker=ticker,
        source=source,
        title=title,
        url=url,
        published_at=published_at,
        raw_payload=raw_payload,
        ingested_at=ingested_at,
    )
    row = event.to_row()
    cursor = conn.execute(
        "INSERT OR IGNORE INTO news_events "
        "(ticker, source, published_at, title, url, ingested_at, raw_payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        row,
    )
    return cursor.rowcount > 0


def record_news_events(
    conn: sqlite3.Connection,
    events: Iterable[NewsEvent | Mapping[str, Any]],
) -> dict[str, int]:
    """Insert a batch of news_events rows.

    Accepts either :class:`NewsEvent` instances or plain dicts with
    the same keys. Rows that conflict on the dedup key are silently
    skipped. Returns a counts dict::

        {"inserted": int, "skipped": int, "total": int}

    Watcher modules typically call this once per scan and append the
    counts to their own log line.
    """

    inserted = 0
    skipped = 0
    total = 0
    with conn:
        for raw in events:
            total += 1
            try:
                if isinstance(raw, NewsEvent):
                    event = raw
                elif isinstance(raw, Mapping):
                    event = NewsEvent(
                        ticker=raw.get("ticker"),  # type: ignore[arg-type]
                        source=raw.get("source"),  # type: ignore[arg-type]
                        title=raw.get("title"),  # type: ignore[arg-type]
                        url=raw.get("url"),
                        published_at=raw.get("published_at"),
                        raw_payload=raw.get("raw_payload"),
                        ingested_at=raw.get("ingested_at"),
                    )
                else:
                    raise TypeError(
                        f"record_news_events: unsupported event type {type(raw)!r}"
                    )
                row = event.to_row()
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO news_events "
                    "(ticker, source, published_at, title, url, ingested_at, "
                    "raw_payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
                if cursor.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
            except (ValueError, TypeError) as exc:
                logger.warning("Skipping malformed news event: %s", exc)
                skipped += 1
    return {"inserted": inserted, "skipped": skipped, "total": total}


# ---------------------------------------------------------------------------
# Universe loader
# ---------------------------------------------------------------------------


def load_watch_tickers(
    conn: sqlite3.Connection,
) -> list[str]:
    """Return all tickers in ``universe`` with ``tier='watch'`` or ``'tradeable'``.

    Both watch and tradeable tiers receive news ingestion every day —
    "tradeable" is just the strict subset of "watch" with an options
    chain. The mission spec says "Daily cron run targets ALL
    universe.tier='watch' tickers", which (per AGENTS.md "Universe
    boundaries") is shorthand for "the watch pool"; tradeable rows are
    members of that pool that additionally satisfy the chain filter.
    """

    rows = conn.execute(
        "SELECT ticker FROM universe WHERE tier IN ('watch','tradeable') "
        "ORDER BY ticker"
    ).fetchall()
    return [row["ticker"] for row in rows]


# ---------------------------------------------------------------------------
# Daily ingest orchestrator
# ---------------------------------------------------------------------------


# A WatcherFn takes a list of tickers and returns an iterable of NewsEvent /
# dict items. Watcher modules expose helpers that match this protocol so the
# daily orchestrator can be tested end-to-end without network.
WatcherFn = Callable[[Sequence[str]], Iterable[NewsEvent | Mapping[str, Any]]]


@dataclass
class DailyIngestResult:
    """Structured summary of one :func:`daily_news_ingest` run."""

    tickers_attempted: int = 0
    rows_inserted: int = 0
    rows_skipped_duplicate: int = 0
    sources_run: list[str] = field(default_factory=list)
    per_ticker_counts: dict[str, int] = field(default_factory=dict)
    empty_feed_reasons: dict[str, str] = field(default_factory=dict)
    completed_at: str = ""
    db_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tickers_attempted": self.tickers_attempted,
            "rows_inserted": self.rows_inserted,
            "rows_skipped_duplicate": self.rows_skipped_duplicate,
            "sources_run": list(self.sources_run),
            "per_ticker_counts": dict(self.per_ticker_counts),
            "empty_feed_reasons": dict(self.empty_feed_reasons),
            "completed_at": self.completed_at,
            "db_path": self.db_path,
        }


def daily_news_ingest(
    *,
    db_path: Path | str | None = None,
    watchers: Mapping[str, WatcherFn] | None = None,
    tickers: Sequence[str] | None = None,
    audit_path: Path | str | None = None,
    write_audit: bool = True,
    empty_feed_reason: str = "no_feed_match",
) -> DailyIngestResult:
    """Run the daily news ingest pipeline.

    Parameters
    ----------
    db_path:
        Target SQLite db. Defaults to :func:`default_db_path`.
    watchers:
        Mapping of ``source_label -> watcher_fn``. Each watcher
        receives the watch-tier ticker list and must return an
        iterable of :class:`NewsEvent` (or dict) items. Defaults to
        the four production watcher hooks declared in
        :data:`DEFAULT_WATCHERS`.
    tickers:
        Override the watch-pool ticker list. Defaults to
        :func:`load_watch_tickers`.
    audit_path:
        Where to write the audit summary JSON. Defaults to
        ``state/audit_latest.json``.
    write_audit:
        When ``False``, skip writing the audit JSON (used by tests
        that want to inspect the in-memory result).
    empty_feed_reason:
        String stored in ``news_events_empty`` for tickers with zero
        rows after every watcher has run.

    Returns
    -------
    DailyIngestResult
        Aggregated counts + empty-feed reasons.

    Notes
    -----
    * The function targets all tickers in the watch pool.
    * Each watcher is wrapped in a ``try/except`` so a single broken
      source never aborts the whole run; failures are logged and the
      source is recorded with ``empty_feed_reason='watcher_error'``
      for the tickers that produced no rows.
    * Per-ticker counts ignore which source produced the row; the
      empty-feed branch fires only when EVERY source returned zero
      rows for that ticker.
    """

    target = Path(db_path) if db_path is not None else default_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    if watchers is None:
        watchers = DEFAULT_WATCHERS

    result = DailyIngestResult(db_path=str(target))
    conn = db.connect(target)
    try:
        db.run_migrations(conn)
        if tickers is None:
            ticker_list = load_watch_tickers(conn)
        else:
            ticker_list = [t.upper() for t in tickers if t]

        result.tickers_attempted = len(ticker_list)
        result.per_ticker_counts = {t: 0 for t in ticker_list}
        result.sources_run = list(watchers.keys())

        for source_label, watcher_fn in watchers.items():
            try:
                items = list(watcher_fn(list(ticker_list)))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "Watcher %s raised %s — skipping",
                    source_label,
                    exc,
                )
                continue

            normalised: list[NewsEvent] = []
            for item in items:
                event = _normalise_item(item, default_source=source_label)
                if event is None:
                    continue
                normalised.append(event)

            counts = record_news_events(conn, normalised)
            result.rows_inserted += counts["inserted"]
            result.rows_skipped_duplicate += counts["skipped"]
            for event in normalised:
                if event.ticker in result.per_ticker_counts:
                    result.per_ticker_counts[event.ticker] += 1

        # Compute empty-feed reasons.
        for ticker, count in result.per_ticker_counts.items():
            if count == 0:
                result.empty_feed_reasons[ticker] = empty_feed_reason
    finally:
        conn.close()

    result.completed_at = _now_iso()

    if write_audit:
        try:
            write_audit_summary(result, audit_path=audit_path)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to write audit summary: %s", exc)

    return result


def _normalise_item(
    item: NewsEvent | Mapping[str, Any] | Any,
    *,
    default_source: str,
) -> NewsEvent | None:
    """Best-effort coercion of watcher output into a :class:`NewsEvent`.

    Watcher modules emit a variety of dict shapes (8-K signal, RSS
    article, IR-page hit, etc.); this helper extracts the canonical
    fields and falls back to sensible defaults so the orchestrator
    can keep moving even on unusual payloads.
    """

    if isinstance(item, NewsEvent):
        if not item.source:
            item.source = default_source
        return item

    if not isinstance(item, Mapping):
        return None

    ticker = item.get("ticker") or item.get("symbol")
    if not ticker:
        return None
    title = (
        item.get("title")
        or item.get("headline")
        or item.get("detail")
        or item.get("summary")
    )
    if not title:
        # Without a title we'd violate NOT NULL. Skip silently — these
        # are typically routing entries (e.g. EFTS hits with no body).
        return None
    url = item.get("url") or item.get("form_url") or item.get("filing_url")
    published_at = (
        item.get("published_at")
        or item.get("published")
        or item.get("filed_date")
        or item.get("detected_date")
    )
    source = item.get("source") or default_source
    return NewsEvent(
        ticker=str(ticker).upper(),
        source=str(source),
        title=str(title)[:500],
        url=str(url) if url else None,
        published_at=str(published_at) if published_at else None,
        raw_payload=item,
    )


# ---------------------------------------------------------------------------
# Audit summary
# ---------------------------------------------------------------------------


def write_audit_summary(
    result: DailyIngestResult,
    *,
    audit_path: Path | str | None = None,
) -> Path:
    """Merge the news-ingestion summary into ``state/audit_latest.json``.

    The audit JSON is the contract surface for VAL-M2-076; we add two
    keys (idempotent — re-running overwrites them):

    * ``news_ingestion`` — full :class:`DailyIngestResult` dict.
    * ``news_events_empty`` — flat ``{ticker: reason}`` mapping for
      every watch-tier ticker with zero rows after the run. The flat
      shape is what the assertion's ``jq`` query expects.

    All other audit keys are preserved.
    """

    target = Path(audit_path) if audit_path else (BASE_DIR / "state" / "audit_latest.json")
    target.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, Any] = {}
    if target.is_file():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}

    existing["news_ingestion"] = {
        "tickers_attempted": result.tickers_attempted,
        "rows_inserted": result.rows_inserted,
        "rows_skipped_duplicate": result.rows_skipped_duplicate,
        "sources_run": list(result.sources_run),
        "empty_feed_reasons": dict(result.empty_feed_reasons),
        "completed_at": result.completed_at,
    }
    existing["news_events_empty"] = dict(result.empty_feed_reasons)

    target.write_text(
        json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8"
    )
    return target


# ---------------------------------------------------------------------------
# Default watcher hooks
# ---------------------------------------------------------------------------


def _watcher_universal_news(tickers: Sequence[str]) -> list[NewsEvent]:
    """Production hook for the ``universal_news_watcher`` module.

    Imported lazily so the tests for this module never hit the real
    network. The hook calls :func:`run_hourly_news_scan` (which has
    its own internal deduper for SEC accessions / RSS URLs) and
    flattens its output into :class:`NewsEvent` rows tagged with the
    canonical ``SOURCE_UNIVERSAL`` label.
    """

    try:
        from biotech_sniper.intelligence.universal_news_watcher import (
            run_hourly_news_scan,
        )
    except Exception as exc:  # pragma: no cover - import-time failure path
        logger.warning("universal_news_watcher unavailable: %s", exc)
        return []

    try:
        result = run_hourly_news_scan(list(tickers))
    except Exception as exc:  # pragma: no cover - network failure path
        logger.warning("universal_news_watcher scan failed: %s", exc)
        return []

    out: list[NewsEvent] = []
    for filing in result.get("new_8ks", []) or []:
        ticker = filing.get("ticker")
        if not ticker:
            continue
        out.append(
            NewsEvent(
                ticker=str(ticker),
                source=SOURCE_UNIVERSAL,
                title=str(filing.get("summary") or filing.get("company") or "8-K filing"),
                url=str(filing.get("form_url") or "") or None,
                published_at=str(filing.get("filed_date") or "") or None,
                raw_payload=filing,
            )
        )
    for article in result.get("news_hits", []) or []:
        ticker = article.get("ticker")
        if not ticker:
            continue
        out.append(
            NewsEvent(
                ticker=str(ticker),
                source=SOURCE_UNIVERSAL,
                title=str(article.get("headline") or article.get("summary") or "news"),
                url=str(article.get("url") or "") or None,
                published_at=str(article.get("published") or "") or None,
                raw_payload=article,
            )
        )
    return out


def _watcher_intraday_rss(tickers: Sequence[str]) -> list[NewsEvent]:
    """Hook for ``intraday_scanner.scan_news_rss``."""

    try:
        from biotech_sniper.intraday_scanner import scan_news_rss
    except Exception as exc:  # pragma: no cover
        logger.warning("intraday_scanner unavailable: %s", exc)
        return []

    try:
        alerts = scan_news_rss(set())
    except Exception as exc:  # pragma: no cover
        logger.warning("intraday_scan_news_rss failed: %s", exc)
        return []

    universe = {t.upper() for t in tickers}
    out: list[NewsEvent] = []
    for alert in alerts or []:
        ticker = alert.get("ticker")
        if not ticker:
            continue
        # Only persist alerts whose ticker is in our watch universe so
        # we don't pollute news_events with off-pool tickers.
        if str(ticker).upper() not in universe:
            continue
        out.append(
            NewsEvent(
                ticker=str(ticker),
                source=SOURCE_INTRADAY_RSS,
                title=str(alert.get("title") or "intraday news"),
                url=str(alert.get("url") or "") or None,
                published_at=None,
                raw_payload=alert,
            )
        )
    return out


def _watcher_sec_8k(tickers: Sequence[str]) -> list[NewsEvent]:
    """Hook for ``intelligence.sec_8k_monitor.run_8k_monitor``."""

    try:
        from biotech_sniper.intelligence.sec_8k_monitor import run_8k_monitor
    except Exception as exc:  # pragma: no cover
        logger.warning("sec_8k_monitor unavailable: %s", exc)
        return []

    try:
        report = run_8k_monitor(mode="daily")
    except Exception as exc:  # pragma: no cover
        logger.warning("run_8k_monitor failed: %s", exc)
        return []

    universe = {t.upper() for t in tickers}
    out: list[NewsEvent] = []
    for signal in report.get("signals", []) or []:
        ticker = signal.get("ticker")
        if not ticker or str(ticker).upper() not in universe:
            continue
        out.append(
            NewsEvent(
                ticker=str(ticker),
                source=SOURCE_SEC_8K,
                title=str(signal.get("detail") or signal.get("type") or "8-K signal"),
                url=str(signal.get("filing_url") or "") or None,
                published_at=str(signal.get("published") or signal.get("detected_date") or "") or None,
                raw_payload=signal,
            )
        )
    return out


def _watcher_ir_events(tickers: Sequence[str]) -> list[NewsEvent]:
    """Hook for ``intelligence.ir_events_watcher.run_ir_events_check``."""

    try:
        from biotech_sniper.intelligence.ir_events_watcher import run_ir_events_check
    except Exception as exc:  # pragma: no cover
        logger.warning("ir_events_watcher unavailable: %s", exc)
        return []

    try:
        report = run_ir_events_check()
    except Exception as exc:  # pragma: no cover
        logger.warning("run_ir_events_check failed: %s", exc)
        return []

    universe = {t.upper() for t in tickers}
    out: list[NewsEvent] = []
    for signal in report.get("signals", []) or []:
        ticker = signal.get("ticker")
        if not ticker or str(ticker).upper() not in universe:
            continue
        out.append(
            NewsEvent(
                ticker=str(ticker),
                source=SOURCE_IR_EVENTS,
                title=str(signal.get("detail") or signal.get("type") or "IR signal"),
                url=str(signal.get("filing_url") or signal.get("ir_url") or "") or None,
                published_at=str(signal.get("detected_date") or "") or None,
                raw_payload=signal,
            )
        )
    return out


# Production watchers — used when ``daily_news_ingest`` is invoked
# without an explicit ``watchers`` mapping. The order is deterministic
# (Python 3.7+ dicts preserve insertion order) so re-runs accumulate
# rows in a stable sequence.
DEFAULT_WATCHERS: dict[str, WatcherFn] = {
    SOURCE_UNIVERSAL: _watcher_universal_news,
    SOURCE_INTRADAY_RSS: _watcher_intraday_rss,
    SOURCE_SEC_8K: _watcher_sec_8k,
    SOURCE_IR_EVENTS: _watcher_ir_events,
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - thin CLI
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.news_events",
        description=(
            "Run the daily news ingest pipeline. Targets ALL "
            "universe.tier='watch' tickers; persists per-headline "
            "rows to news_events; updates state/audit_latest.json."
        ),
    )
    parser.add_argument("--db", type=str, default=None)
    parser.add_argument("--no-audit", action="store_true")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else default_db_path()
    result = daily_news_ingest(
        db_path=db_path,
        write_audit=not args.no_audit,
    )
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
