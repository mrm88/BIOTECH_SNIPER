"""``candidate_events`` writer with ``INSERT OR IGNORE`` dedup.

The emitter is the **only** writer of ``candidate_events`` rows.
Dedup semantics rely entirely on the ``candidate_events.dedup_key``
``UNIQUE`` constraint provisioned by migration v9 → v10
(``010_reading_b_foundations``).  Module-level in-memory dedup
cache state is **forbidden**: that would silently degrade dedup
across daemon restarts (SIGKILL, OOM, systemd ``Restart=on-failure``)
because in-memory state is wiped while the database persists.

f-m2-06 implementation
-----------------------

This module provides the production writer surface used by
:mod:`biotech_sniper.news_daemon.poll_loop`:

* :func:`compute_dedup_key` — pure-Python SHA-256 hash over the
  ``(ticker, news_event_id, matched_keywords)`` triple, with the
  ASCII Unit Separator (``0x1F``) as field delimiter and the
  matched_keywords sequence sorted+deduped before joining.
* :func:`make_candidate` — construct a :class:`CandidateEvent` from
  the matcher's :class:`~biotech_sniper.news_daemon.matcher.MatchResult`
  output (fills ``emitted_at`` and ``dedup_key``).
* :func:`write_candidate` — single-row ``INSERT OR IGNORE``;
  returns ``True`` when a fresh row was committed, ``False`` when
  the dedup_key already existed.
* :func:`write_candidates` — batch ``executemany`` inside a single
  ``BEGIN`` / ``COMMIT`` block (per VAL-M2-024 / AGENTS.md
  "Multi-row inserts in a poll cycle MUST use a single transaction").
* :func:`get_last_emitted_news_event_id` — durable watermark
  recovery from ``MAX(source_news_event_id)`` so a SIGKILL +
  restart resumes without re-emitting any candidate (VAL-M2-027 /
  VAL-M2-028).
* :func:`run_one_poll_cycle` — the loop body wired to scope filter
  → matcher → ``write_candidates``.  Subsequent features
  (f-m2-08 heartbeat, f-m2-09 resilience) wrap this in the systemd
  long-lived loop; this function is the single source of truth for
  what one cycle does, and tests exercise it directly.

dedup_key formula
------------------

::

    dedup_key = sha256(
        f"{ticker}\\x1f{news_event_id}\\x1f{matched_keywords_sorted}"
    ).hexdigest()

The ``\\x1f`` ASCII Unit Separator is the field delimiter — pipes
are NOT injection-safe for an unbounded keyword vocab (a future
keyword containing ``|`` would collide trivially).  The
``matched_keywords`` payload is the comma-joined output of
``",".join(sorted(set(kws)))`` so the dedup_key is invariant under
duplicate / reordered keyword input.

Cross-restart durability
------------------------

The emitter NEVER references a phantom dedup-key column on the
news_events table (no such column exists — :file:`db/schema.sql`
declares the existing composite UNIQUE INDEX
``idx_news_events_dedup`` on
``(ticker, source, COALESCE(url,''), COALESCE(published_at,''))``).
Stage-1 dedup operates entirely on the ``dedup_key`` column of
``candidate_events``; the input-side watermark is recovered from
the database on every process startup via
:func:`get_last_emitted_news_event_id`.  No module-level
"seen-set" exists in this package — see ``__init__.py`` for the
contract — so SIGKILL + systemd restart loses no work.
"""

from __future__ import annotations

import datetime
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import (
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

__all__ = [
    "FIELD_SEPARATOR",
    "INSERT_SQL",
    "CandidateEvent",
    "compute_dedup_key",
    "make_candidate",
    "write_candidate",
    "write_candidates",
    "get_last_emitted_news_event_id",
    "iter_pending_news_events",
    "run_one_poll_cycle",
]


#: ASCII Unit Separator (0x1F).  Used to delimit fields in the
#: ``dedup_key`` SHA-256 input.  Chosen because it cannot appear in
#: any reasonable ticker / keyword vocabulary, so an attacker cannot
#: craft a colliding keyword set via field-injection (per VAL-M2-023
#: pipe-resistance contract).
FIELD_SEPARATOR: str = "\x1f"


#: Canonical ``INSERT OR IGNORE`` statement used by both the
#: single-row and batch writers.  Exposed as a module-level constant
#: so tests can diff the SQL text against the schema and so future
#: callers (replay tooling, audit jobs) re-use the exact form.
INSERT_SQL: str = (
    "INSERT OR IGNORE INTO candidate_events "
    "(ticker, source_news_event_id, matched_keywords, calendar_match, "
    "emitted_at, dedup_key) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)


# Path-or-connection alias.  The writer accepts either a ready-to-use
# :class:`sqlite3.Connection` (test fixtures, callers that share a
# transaction) or a filesystem path / string (production).
_ConnOrPath = Union[sqlite3.Connection, str, Path]


@dataclass(frozen=True)
class CandidateEvent:
    """In-memory shape of a ``candidate_events`` row.

    Mirrors the table schema from migration v10:

    * ``id`` is assigned by SQLite ``AUTOINCREMENT``; ``None`` until
      the row is committed.
    * ``ticker`` is upper-case canonical (enforced by news_events
      upstream).
    * ``source_news_event_id`` is the ``news_events.id`` foreign key.
    * ``matched_keywords`` is the sorted, comma-joined string used in
      the ``dedup_key`` hash.
    * ``calendar_match`` is the trial_calendar lookup result, or
      ``None`` when no upcoming catalyst was found (LEFT-JOIN miss
      is NOT an error per VAL-M2-022).
    * ``emitted_at`` is the ISO-8601 UTC timestamp of the write.
    * ``dedup_key`` is the SHA-256 hex digest computed by
      :func:`compute_dedup_key`.
    """

    ticker: str
    source_news_event_id: int
    matched_keywords: str
    emitted_at: str
    dedup_key: str
    calendar_match: Optional[str] = None
    id: Optional[int] = None

    def to_row(
        self,
    ) -> Tuple[str, int, str, Optional[str], str, str]:
        """Return the positional tuple matching :data:`INSERT_SQL`."""

        return (
            self.ticker,
            int(self.source_news_event_id),
            self.matched_keywords,
            self.calendar_match,
            self.emitted_at,
            self.dedup_key,
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """UTC ISO-8601 timestamp, microsecond precision, ``Z`` suffix.

    Matches the format produced by SQLite's
    ``strftime('%Y-%m-%dT%H:%M:%fZ', 'now')`` so emitted_at values
    sort lexicographically next to ``ingested_at`` /
    ``called_at`` columns elsewhere in the schema.
    """

    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _normalise_keywords(matched_keywords: Sequence[str]) -> List[str]:
    """Return the sorted, deduped, blank-stripped keyword list."""

    return sorted({kw for kw in matched_keywords if kw})


def compute_dedup_key(
    ticker: str,
    news_event_id: int,
    matched_keywords: Sequence[str],
) -> str:
    """Compute ``dedup_key`` for a ``(ticker, news_event_id, kw)`` triple.

    The matched-keywords sequence is **sorted, deduped, and
    comma-joined** before hashing so a row that matches the same
    vocab in a different order produces the same key.

    The ASCII Unit Separator (``0x1F``) is the field delimiter — a
    keyword containing ``|`` cannot collide with another legitimate
    keyword set under this scheme (VAL-M2-023 pipe-injection
    resistance).
    """

    import hashlib

    deduped_sorted = ",".join(_normalise_keywords(matched_keywords))
    payload = (
        f"{ticker}{FIELD_SEPARATOR}"
        f"{news_event_id}{FIELD_SEPARATOR}"
        f"{deduped_sorted}"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def make_candidate(
    ticker: str,
    news_event_id: int,
    matched_keywords: Sequence[str],
    *,
    calendar_match: Optional[str] = None,
    emitted_at: Optional[str] = None,
) -> CandidateEvent:
    """Construct a :class:`CandidateEvent` ready for ``write_candidate``.

    The factory normalises ``matched_keywords`` (sorts + dedupes +
    drops blanks) so the persisted ``matched_keywords`` column and
    the ``dedup_key`` hash agree on the same canonical input.

    Parameters
    ----------
    ticker:
        Upper-case canonical ticker symbol.
    news_event_id:
        ``news_events.id`` foreign key.
    matched_keywords:
        Iterable of keyword literals from the matcher.  Order is
        irrelevant — the factory sorts before joining.
    calendar_match:
        Optional structured-string trial-calendar payload (JSON);
        ``None`` is the explicit "no calendar hit" sentinel
        (per VAL-M2-021 a miss is NOT an error).
    emitted_at:
        Optional ISO-8601 UTC timestamp override.  Defaults to
        :func:`_now_iso` (microsecond precision).

    Returns
    -------
    CandidateEvent
        Frozen dataclass with ``matched_keywords`` and ``dedup_key``
        in canonical form.
    """

    deduped_sorted = _normalise_keywords(matched_keywords)
    matched_csv = ",".join(deduped_sorted)
    return CandidateEvent(
        ticker=ticker,
        source_news_event_id=int(news_event_id),
        matched_keywords=matched_csv,
        calendar_match=calendar_match,
        emitted_at=emitted_at or _now_iso(),
        dedup_key=compute_dedup_key(ticker, news_event_id, deduped_sorted),
    )


# ---------------------------------------------------------------------------
# Connection plumbing
# ---------------------------------------------------------------------------


@contextmanager
def _connection(conn_or_path: _ConnOrPath) -> Iterator[sqlite3.Connection]:
    """Yield an opened sqlite3 connection.

    When ``conn_or_path`` is already a :class:`sqlite3.Connection` we
    yield it as-is and do NOT close it on exit (the caller owns the
    lifetime).  When it's a path we open a fresh connection via
    :func:`biotech_sniper.db.connect` so the project-wide PRAGMAs
    (``foreign_keys=ON``, ``journal_mode=WAL``,
    ``synchronous=NORMAL``) are applied; we close it on exit.
    """

    if isinstance(conn_or_path, sqlite3.Connection):
        yield conn_or_path
        return

    # Lazy import — keeps ``import emit`` cheap and avoids a circular
    # dep on :mod:`biotech_sniper.db` at package load time.
    from biotech_sniper import db as _db

    conn = _db.connect(conn_or_path)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def write_candidate(
    conn_or_path: _ConnOrPath,
    candidate: CandidateEvent,
) -> bool:
    """Insert a single ``candidate_events`` row with ``INSERT OR IGNORE``.

    Idempotent on ``dedup_key``: re-emitting the same
    ``(ticker, news_event_id, matched_keywords)`` triple is a no-op
    that returns ``False`` (per VAL-M2-025).  The dedup primitive
    is the ``UNIQUE`` constraint on ``candidate_events.dedup_key``
    provisioned by migration v10 — this writer never tries to
    pre-check membership in Python.

    Parameters
    ----------
    conn_or_path:
        Either an open :class:`sqlite3.Connection` (caller-owned)
        or a filesystem path (we open + close a connection).
    candidate:
        :class:`CandidateEvent` to insert.  Use :func:`make_candidate`
        to build one with the canonical ``matched_keywords`` /
        ``dedup_key`` fields.

    Returns
    -------
    bool
        ``True`` when a fresh row was inserted, ``False`` when the
        dedup_key already existed (silent dedup via
        ``INSERT OR IGNORE``).
    """

    with _connection(conn_or_path) as conn:
        with conn:
            cursor = conn.execute(INSERT_SQL, candidate.to_row())
            inserted = cursor.rowcount > 0
    return inserted


def write_candidates(
    conn_or_path: _ConnOrPath,
    candidates: Iterable[CandidateEvent],
) -> Tuple[int, int]:
    """Batch-insert candidates inside a single transaction.

    Uses ``executemany`` wrapped in a single ``BEGIN`` / ``COMMIT``
    so a poll cycle that produces N candidate rows commits atomically
    — partial failures roll back cleanly and the news_events
    watermark stays consistent (per AGENTS.md "Multi-row inserts in
    a poll cycle MUST use a single transaction").

    Parameters
    ----------
    conn_or_path:
        Connection or path (see :func:`write_candidate`).
    candidates:
        Iterable of :class:`CandidateEvent`.  Empty iterable is a
        no-op that returns ``(0, 0)`` without opening any cursor.

    Returns
    -------
    tuple[int, int]
        ``(attempted, inserted)`` — ``inserted`` excludes the
        ``INSERT OR IGNORE`` dedup misses.  ``attempted`` counts the
        rows the caller submitted, regardless of dedup outcome.
    """

    materialised = [c.to_row() for c in candidates]
    attempted = len(materialised)
    if attempted == 0:
        return (0, 0)

    inserted = 0
    with _connection(conn_or_path) as conn:
        # ``with conn:`` forms a single BEGIN/COMMIT block so the
        # batch is atomic even when ``executemany`` partially
        # short-circuits via ``INSERT OR IGNORE``.
        with conn:
            # ``executemany`` does not surface per-row rowcount on
            # SQLite's default driver, so we count via ``changes()``
            # before/after the call.
            before_total = conn.execute(
                "SELECT total_changes()"
            ).fetchone()[0]
            conn.executemany(INSERT_SQL, materialised)
            after_total = conn.execute(
                "SELECT total_changes()"
            ).fetchone()[0]
            inserted = int(after_total) - int(before_total)
    return (attempted, inserted)


def get_last_emitted_news_event_id(conn_or_path: _ConnOrPath) -> int:
    """Return the largest ``source_news_event_id`` already emitted.

    Used as the cross-restart watermark: on every poll-loop startup
    we resume from this value (``WHERE news_events.id > watermark``)
    so SIGKILL + systemd ``Restart=on-failure`` loses no work and
    cannot double-emit a previously-handled headline (per VAL-M2-027
    / VAL-M2-028).

    Returns ``0`` when ``candidate_events`` is empty so the first
    cycle scans every news_events row.
    """

    with _connection(conn_or_path) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(source_news_event_id), 0) AS watermark "
            "FROM candidate_events"
        ).fetchone()
    if row is None:
        return 0
    # Tolerate both Row and tuple shapes.
    try:
        return int(row[0] or 0)
    except (TypeError, ValueError):
        return 0


def iter_pending_news_events(
    conn: sqlite3.Connection,
    after_id: int = 0,
    polled_tickers: Optional[Iterable[str]] = None,
    *,
    limit: Optional[int] = None,
) -> Iterator[Tuple[int, str, str, Optional[str]]]:
    """Cursor over ``news_events.id > after_id`` rows in ascending order.

    The cursor uses the existing primary key on ``news_events.id`` —
    the daemon does NOT reference any phantom dedup-key column on
    that table (no such column exists; cross-restart dedup is
    enforced on the ``candidate_events`` side via the UNIQUE
    constraint on its own ``dedup_key`` column).

    Parameters
    ----------
    conn:
        An open SQLite connection.  The caller owns its lifetime.
    after_id:
        Watermark.  Pass the result of
        :func:`get_last_emitted_news_event_id` to resume from a
        prior session.  Defaults to ``0`` (scan everything).
    polled_tickers:
        Optional ticker allow-list.  When provided, the cursor
        filters at the SQL layer via ``ticker IN (...)`` so the
        scope filter ride-along is cheap.  When ``None``, every
        row past the watermark is returned.
    limit:
        Optional ``LIMIT`` cap on the result set (for back-pressure).

    Yields
    ------
    tuple
        ``(news_event_id, ticker, title, raw_payload)`` per row.
    """

    if polled_tickers is None:
        sql = (
            "SELECT id, ticker, title, raw_payload FROM news_events "
            "WHERE id > ? ORDER BY id ASC"
        )
        params: tuple = (int(after_id),)
    else:
        # Materialise once — we may iterate twice (count + bind).
        polled = [t for t in polled_tickers if t]
        if not polled:
            return
        placeholders = ",".join(["?"] * len(polled))
        sql = (
            f"SELECT id, ticker, title, raw_payload FROM news_events "
            f"WHERE id > ? AND ticker IN ({placeholders}) "
            f"ORDER BY id ASC"
        )
        params = (int(after_id), *polled)

    if limit is not None and limit > 0:
        sql = sql + f" LIMIT {int(limit)}"

    cursor = conn.execute(sql, params)
    for row in cursor:
        yield (
            int(row[0]),
            str(row[1]) if row[1] is not None else "",
            str(row[2]) if row[2] is not None else "",
            row[3],
        )


# ---------------------------------------------------------------------------
# Loop body
# ---------------------------------------------------------------------------


def run_one_poll_cycle(
    conn_or_path: _ConnOrPath,
    *,
    polled_tickers: Optional[Iterable[str]] = None,
    after_id: Optional[int] = None,
    today: Optional[datetime.date] = None,
    limit: Optional[int] = None,
) -> Tuple[int, int]:
    """Run a single poll cycle: scope → match → emit.

    Cycle body:

    1. Resolve the watermark via
       :func:`get_last_emitted_news_event_id` (unless
       ``after_id`` is passed explicitly — useful for tests).
    2. Cursor ``news_events.id > watermark`` filtered by
       ``polled_tickers`` (when supplied) so out-of-scope rows are
       skipped at the SQL layer.
    3. For each row, run
       :func:`biotech_sniper.news_daemon.matcher.match_news_row` to
       compute the matched-keyword set + the trial-calendar lookup.
    4. Skip rows with no keyword matches.
    5. Build :class:`CandidateEvent` instances and pass the batch
       to :func:`write_candidates` for an ``executemany`` insert
       inside one transaction.

    Multi-keyword headlines collapse to ONE candidate per
    ``(ticker, news_event_id)`` pair (per VAL-M2-019 + the
    f-m2-06 "exactly ONE candidate" contract).

    Parameters
    ----------
    conn_or_path:
        Database handle.  Tests typically pass a tmp-path string;
        production passes the canonical project path.
    polled_tickers:
        Optional ticker allow-list (the scope filter result).  When
        ``None``, every news_events row past the watermark is
        considered (callers like the f-m2-09 wrapper resolve scope
        once per cycle and pass the set in).
    after_id:
        Optional watermark override.  When ``None``, the function
        recovers the watermark from
        :func:`get_last_emitted_news_event_id`.
    today:
        Optional override for the "today" date used by the
        trial_calendar window check (forwarded to the matcher).
    limit:
        Optional cap on the number of news_events rows scanned in
        one cycle.

    Returns
    -------
    tuple[int, int]
        ``(scanned, inserted)`` — scanned counts the news_events
        rows examined regardless of match outcome; inserted counts
        candidate_events rows actually committed (post dedup).
    """

    # Lazy import — avoids a hard circular dep with the matcher
    # subpackage (matcher imports the calendar lazily; we keep the
    # emitter import-clean for the f-m2-01 smoke test).
    from biotech_sniper.news_daemon.matcher import match_news_row

    log = logging.getLogger("biotech_sniper.news_daemon.emit")

    polled_set: Optional[List[str]] = None
    if polled_tickers is not None:
        polled_set = sorted({t for t in polled_tickers if t})
        if not polled_set:
            # Empty allow-list = nothing to do.  We deliberately do
            # NOT recover the watermark here — there's no useful
            # output and tests that pass an empty set expect zero
            # work.
            return (0, 0)

    candidates: List[CandidateEvent] = []
    scanned = 0

    with _connection(conn_or_path) as conn:
        watermark = (
            int(after_id)
            if after_id is not None
            else get_last_emitted_news_event_id(conn)
        )

        for news_event_id, ticker, title, payload in iter_pending_news_events(
            conn,
            after_id=watermark,
            polled_tickers=polled_set,
            limit=limit,
        ):
            scanned += 1
            # body for matcher: prefer the raw payload's body when
            # present, else fall back to the title alone (the
            # matcher tolerates an empty body string).
            body = ""
            if isinstance(payload, str) and payload:
                body = payload

            try:
                result = match_news_row(
                    ticker,
                    title,
                    body,
                    db_path=None
                    if isinstance(conn_or_path, sqlite3.Connection)
                    else str(conn_or_path),
                    today=today,
                )
            except Exception as exc:  # pragma: no cover - defensive
                # Matcher must NEVER crash the poll cycle (the
                # adverse-news exit hook depends on the loop staying
                # alive).  Log + skip the row.
                log.warning(
                    "matcher_row_error: news_event_id=%d ticker=%s err=%r",
                    news_event_id,
                    ticker,
                    exc,
                    extra={
                        "event": "news_daemon_matcher_row_error",
                        "src_module": "news_daemon.emit",
                        "news_event_id": news_event_id,
                        "ticker": ticker,
                    },
                )
                continue

            if not result.is_match:
                continue

            candidates.append(
                make_candidate(
                    ticker=ticker,
                    news_event_id=news_event_id,
                    matched_keywords=result.matched_keywords,
                    calendar_match=result.calendar_match,
                )
            )

        if not candidates:
            return (scanned, 0)

        # Single transaction: ``executemany`` inside ``with conn:``.
        attempted, inserted = write_candidates(conn, candidates)

    log.debug(
        "news_daemon_emit_cycle: scanned=%d attempted=%d inserted=%d",
        scanned,
        attempted,
        inserted,
        extra={
            "event": "news_daemon_emit_cycle",
            "src_module": "news_daemon.emit",
            "scanned": scanned,
            "attempted": attempted,
            "inserted": inserted,
        },
    )
    return (scanned, inserted)


# ---------------------------------------------------------------------------
# Backward-compat helpers (used by emitter.py star re-export).
# ---------------------------------------------------------------------------


def _replace_calendar_match(
    candidate: CandidateEvent,
    calendar_match: Optional[str],
) -> CandidateEvent:
    """Return a new CandidateEvent with ``calendar_match`` replaced.

    Helper used by ad-hoc test code that wants to mutate a frozen
    dataclass without rebuilding it from scratch.  Kept for
    completeness; not part of the public surface advertised in
    :data:`__all__`.
    """

    return replace(candidate, calendar_match=calendar_match)
