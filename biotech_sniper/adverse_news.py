"""Adverse-news exit trigger (f-m3-09).

When the news pipeline ingests a row tagged ``enrichment_label =
'negative_material'`` for a ticker that has an active play, this
module submits an exit order tagged ``event='adverse_news'`` that
closes 100% of the position.

Hooking shape
-------------

The trigger is a thin function that callers invoke after each news
ingest:

* :func:`scan_and_trigger` walks the ``news_events`` table for any
  ``enrichment_label='negative_material'`` row created since
  ``since_at`` (defaults to "today UTC midnight") and matches
  affected tickers against the supplied ``active_plays`` mapping.
* :func:`record_negative_news_and_exit` is a one-shot helper that
  inserts a single news event AND fires the exit if the ticker has
  an active play. Convenient for unit tests and for the watcher
  modules that already produce a single :class:`NewsEvent` at a
  time.

Both helpers funnel the submission through
:meth:`PaperExecutor.submit_exit` so the hold-policy guard,
``client_order_id`` namespace, and local idempotency check are all
applied uniformly.

Validation contract assertions exercised
----------------------------------------

* **VAL-M3-051** — a single ``negative_material`` row triggers
  exactly one ``adverse_news`` exit per ``(ticker, date)`` pair;
  re-running on the same day for the same ticker is idempotent.
"""

from __future__ import annotations

import datetime
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from biotech_sniper import db as _db
from biotech_sniper import hold_policy as _hold_policy
from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.news_events import (
    NewsEvent,
    default_db_path,
    record_news_event,
)
from biotech_sniper.paper_executor import (
    PaperExecutor,
    PaperOnlyViolation,
)


__all__ = [
    "ADVERSE_NEWS_EVENT",
    "NEGATIVE_MATERIAL_LABEL",
    "AdverseNewsExitRunner",
    "scan_and_trigger",
    "record_negative_news_and_exit",
]


logger = logging.getLogger(__name__)


#: Event tag persisted on the ``orders`` row for adverse-news exits.
ADVERSE_NEWS_EVENT: str = "adverse_news"

#: Canonical ``enrichment_label`` value the LLM enrichment pass writes
#: when it classifies a headline as materially negative for the
#: ticker. Matches the f-m3-09 feature description verbatim.
NEGATIVE_MATERIAL_LABEL: str = "negative_material"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_active_plays(
    active_plays: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Mapping[str, Any]]:
    """Return ``{TICKER: play_dict}`` from a mapping or iterable.

    Tickers are upper-cased so the lookup matches the upper-cased
    ticker stored on every ``news_events`` row.
    """
    out: dict[str, Mapping[str, Any]] = {}
    if active_plays is None:
        return out
    if isinstance(active_plays, Mapping):
        for ticker, play in active_plays.items():
            if not isinstance(play, Mapping):
                continue
            out[str(ticker).upper()] = play
        return out
    for play in active_plays:
        if not isinstance(play, Mapping):
            continue
        ticker = play.get("ticker") or play.get("symbol")
        if not ticker:
            continue
        out[str(ticker).upper()] = play
    return out


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class AdverseNewsExitRunner:
    """Submit ``adverse_news`` exits for negatively-tagged headlines.

    Wraps an existing :class:`PaperExecutor` and re-checks the
    paper-only guardrail on every invocation as defence-in-depth.
    """

    def __init__(self, executor: PaperExecutor) -> None:
        if executor is None:
            raise TypeError("executor must be a PaperExecutor instance")
        self.executor = executor

    def trigger_for_events(
        self,
        events: Iterable[Mapping[str, Any]],
        active_plays: Iterable[Mapping[str, Any]]
        | Mapping[str, Mapping[str, Any]],
        *,
        today: Optional[Any] = None,
    ) -> list[dict[str, Any]]:
        """Submit exits for any ``negative_material`` event with an active play.

        Each entry in ``events`` is expected to expose at minimum a
        ``ticker`` and an ``enrichment_label``. Rows whose label is
        not :data:`NEGATIVE_MATERIAL_LABEL` are silently ignored.
        Tickers without an active play are recorded as
        ``status='skipped'`` with ``reason='no_active_play'`` so the
        caller can audit the gap.
        """
        client = getattr(self.executor, "client", None)
        base_url = getattr(client, "base_url", None)
        if base_url != PAPER_BASE_URL:
            raise PaperOnlyViolation(
                "AdverseNewsExitRunner refuses to submit exit orders "
                f"against a non-paper Alpaca client (base_url={base_url!r})."
            )

        today_date = _hold_policy.coerce_date(today)
        plays_by_ticker = _coerce_active_plays(active_plays)
        results: list[dict[str, Any]] = []

        for event in events:
            if not isinstance(event, Mapping):
                continue
            label = event.get("enrichment_label")
            if label != NEGATIVE_MATERIAL_LABEL:
                continue
            ticker_raw = event.get("ticker")
            if not isinstance(ticker_raw, str) or not ticker_raw.strip():
                continue
            ticker = ticker_raw.strip().upper()

            play = plays_by_ticker.get(ticker)
            if play is None:
                results.append(
                    {
                        "ticker": ticker,
                        "status": "skipped",
                        "reason": "no_active_play",
                    }
                )
                continue

            qty = _coerce_qty(play)
            if qty < 1:
                results.append(
                    {
                        "ticker": ticker,
                        "play_card_id": play.get("play_card_id"),
                        "status": "skipped",
                        "reason": "no_open_qty",
                    }
                )
                continue

            try:
                order_id = self.executor.submit_exit(
                    play,
                    ADVERSE_NEWS_EVENT,
                    today=today_date,
                    sell_qty=qty,
                )
            except PaperOnlyViolation:
                raise
            except Exception as exc:  # noqa: BLE001 — surfaced in result
                logger.warning(
                    "adverse_news.exit_failed ticker=%s play_card_id=%s "
                    "qty=%s reason=%s",
                    ticker,
                    play.get("play_card_id"),
                    qty,
                    type(exc).__name__,
                )
                results.append(
                    {
                        "ticker": ticker,
                        "play_card_id": play.get("play_card_id"),
                        "status": "error",
                        "reason": type(exc).__name__,
                        "qty": qty,
                    }
                )
                continue

            logger.info(
                "adverse_news.exit_submitted event=%s order_id=%s "
                "qty=%s ticker=%s parent_play_card_id=%s",
                ADVERSE_NEWS_EVENT,
                order_id,
                qty,
                ticker,
                play.get("play_card_id"),
            )
            results.append(
                {
                    "ticker": ticker,
                    "play_card_id": play.get("play_card_id"),
                    "status": "submitted",
                    "event": ADVERSE_NEWS_EVENT,
                    "order_id": order_id,
                    "qty": qty,
                    "parent_play_card_id": play.get("play_card_id"),
                }
            )

        return results

    def scan_and_trigger(
        self,
        active_plays: Iterable[Mapping[str, Any]]
        | Mapping[str, Mapping[str, Any]],
        *,
        db_path: Optional[Path] = None,
        since_at: Optional[str] = None,
        today: Optional[Any] = None,
    ) -> list[dict[str, Any]]:
        """Query ``news_events`` for negative_material rows and trigger exits.

        Reads ``news_events`` rows where ``enrichment_label =
        'negative_material'`` and ``ingested_at >= since_at`` (or
        the start of the day in UTC if ``since_at`` is ``None``),
        then dispatches each matching ticker through
        :meth:`trigger_for_events`.
        """
        target = Path(db_path) if db_path is not None else default_db_path()
        if since_at is None:
            since_at = _start_of_today_utc_iso()

        conn = _db.connect(target)
        try:
            _db.run_migrations(conn)
            rows = conn.execute(
                """
                SELECT ticker, source, title, url, published_at,
                       enrichment_label, ingested_at
                FROM news_events
                WHERE enrichment_label = ?
                  AND ingested_at >= ?
                ORDER BY ingested_at ASC, id ASC
                """,
                (NEGATIVE_MATERIAL_LABEL, since_at),
            ).fetchall()
        finally:
            conn.close()

        events = [dict(row) for row in rows]
        return self.trigger_for_events(
            events, active_plays, today=today
        )


def scan_and_trigger(
    executor: PaperExecutor,
    active_plays: Iterable[Mapping[str, Any]]
    | Mapping[str, Mapping[str, Any]],
    *,
    db_path: Optional[Path] = None,
    since_at: Optional[str] = None,
    today: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """Module-level convenience wrapper around :class:`AdverseNewsExitRunner`."""
    return AdverseNewsExitRunner(executor).scan_and_trigger(
        active_plays,
        db_path=db_path,
        since_at=since_at,
        today=today,
    )


def record_negative_news_and_exit(
    *,
    executor: PaperExecutor,
    ticker: str,
    title: str,
    active_plays: Iterable[Mapping[str, Any]]
    | Mapping[str, Mapping[str, Any]],
    source: str = "test_fixture",
    url: Optional[str] = None,
    published_at: Optional[str] = None,
    db_path: Optional[Path] = None,
    today: Optional[Any] = None,
    enrichment_label: str = NEGATIVE_MATERIAL_LABEL,
) -> dict[str, Any]:
    """Record one news_events row + trigger the adverse-news exit hook.

    Convenience helper for unit tests and watcher modules that want
    to hand a single tagged headline to the trigger without staging
    rows manually. Returns a single dict (the first result from the
    trigger), or an empty dict when the row was not negative_material
    (so the helper degrades gracefully if the caller passes a
    non-adverse label).
    """
    target = Path(db_path) if db_path is not None else default_db_path()
    conn = _db.connect(target)
    try:
        _db.run_migrations(conn)
        record_news_event(
            conn,
            ticker=ticker,
            source=source,
            title=title,
            url=url,
            published_at=published_at,
            enrichment_label=enrichment_label,
        )
        conn.commit()
    finally:
        conn.close()

    if enrichment_label != NEGATIVE_MATERIAL_LABEL:
        return {}

    results = scan_and_trigger(
        executor,
        active_plays,
        db_path=target,
        today=today,
    )
    # Return the first result for the requested ticker so callers do
    # not have to filter the list themselves.
    needle = ticker.strip().upper()
    for entry in results:
        if entry.get("ticker") == needle:
            return entry
    return {}


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _coerce_qty(play: Mapping[str, Any]) -> int:
    for key in ("qty", "contracts", "open_qty"):
        value = play.get(key)
        if value is None:
            continue
        try:
            qty = int(value)
        except (TypeError, ValueError):
            continue
        return qty if qty >= 0 else 0
    return 0


def _start_of_today_utc_iso() -> str:
    """Return ``YYYY-MM-DDT00:00:00.000000Z`` for the current UTC day."""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    return f"{today.isoformat()}T00:00:00.000000Z"
