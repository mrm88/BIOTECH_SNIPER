"""Universe scope filter — russell2k_biotech ∩ universe.tier ∈ {watch, tradeable}.

This submodule narrows the polled-news cursor down to the addressable
Reading-B universe. Tickers outside the intersection are NEVER
polled; tickers inside the intersection but with empty/whitespace
names are silently rejected.

f-m2-04 implementation
----------------------

The polled-ticker set is the SQL intersection of two tables:

* :sql:`russell2k_biotech` — the Russell-2000 ∩ biotech-SIC subset
  (populated by the M1 refresh pipeline).
* :sql:`universe` rows where :sql:`tier IN ('watch', 'tradeable')`
  (the existing daily-curated universe table).

The intersection is computed once per poll cycle by
:func:`resolve_polled_tickers` (alias :func:`load_polled_universe`).
Empty russell2k_biotech (or a missing table — M1 not yet run) logs a
single WARNING per call ("russell2k_biotech empty; M1 may not be
seeded; idling") and returns an empty set so the daemon idles
gracefully instead of crashing.

:func:`filter_universe` is a pure-Python helper that intersects an
arbitrary iterable of tickers (e.g. the news_events cursor) against
the resolved polled set. Empty / whitespace-only tickers are
silently rejected.

Fulfills the assertions:

* VAL-M2-012 — set equality against the SQL intersection.
* VAL-M2-013 — tickers outside the intersection are never polled.
* VAL-M2-014 / VAL-M2-042 — empty russell2k_biotech idles + WARNING.
* VAL-M2-053 — empty / whitespace ticker silently rejected.
* VAL-M2-054 — universe-only (non-biotech) ticker emits zero candidates.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Iterable, Optional, Set, Union

__all__ = [
    "ALLOWED_TIERS",
    "EMPTY_RUSSELL_WARNING_MESSAGE",
    "filter_universe",
    "load_polled_universe",
    "resolve_polled_tickers",
]


#: Universe tiers polled by the daemon. Other tiers (anything outside
#: this set) are excluded from Stage-1 scope. The literal pair
#: ``{watch, tradeable}`` mirrors the existing :sql:`universe.tier`
#: CHECK constraint in :file:`db/schema.sql`.
ALLOWED_TIERS: frozenset[str] = frozenset({"watch", "tradeable"})


#: Canonical WARNING message emitted when ``russell2k_biotech`` is
#: empty (M1 not yet seeded) or the table is missing entirely.
#: Pinned as a module-level constant so tests can assert on the exact
#: text without coupling to log formatting (VAL-M2-014).
EMPTY_RUSSELL_WARNING_MESSAGE: str = (
    "russell2k_biotech empty; M1 may not be seeded; idling"
)


#: SQL that drives :func:`resolve_polled_tickers`. Encoded as a
#: module-level constant so the validator's
#: ``SELECT ... INTERSECT SELECT ...`` reference query can be diffed
#: against the implementation textually.
_INTERSECTION_SQL: str = (
    "SELECT ticker FROM russell2k_biotech "
    "INTERSECT "
    "SELECT ticker FROM universe WHERE tier IN ('watch', 'tradeable')"
)


def _is_blank(ticker: object) -> bool:
    """Return True when ``ticker`` is None / empty / whitespace-only."""

    if ticker is None:
        return True
    if not isinstance(ticker, str):
        return True
    return ticker.strip() == ""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """Return True when SQLite reports ``name`` as a table or view."""

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') "
        "AND name = ? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def resolve_polled_tickers(
    db_path: Optional[Union[str, Path]] = None,
    *,
    conn: Optional[sqlite3.Connection] = None,
    logger: Optional[logging.Logger] = None,
) -> Set[str]:
    """Resolve the Stage-1 polled-ticker set from the database.

    Parameters
    ----------
    db_path:
        Optional filesystem path to the SQLite database. When omitted,
        the function falls back to
        :data:`biotech_sniper.paths.DATA_DIR` /
        :file:`alpha_sniper.db` (the production location). Pass an
        explicit path in tests.
    conn:
        Optional pre-opened sqlite3 connection. Mutually exclusive
        with ``db_path``; when supplied, the caller owns the
        connection's lifetime (this function will NOT close it).
        Useful for transactional callers that want the read to share
        the same connection as a subsequent write.
    logger:
        Optional logger override. Defaults to the package logger
        ``biotech_sniper.news_daemon.scope`` so tests can intercept
        WARNING records via ``caplog`` without monkeypatching.

    Returns
    -------
    set[str]
        The set of tickers in
        ``russell2k_biotech ∩ universe.tier IN ('watch','tradeable')``.
        Empty / whitespace tickers are silently rejected. When the
        ``russell2k_biotech`` table is empty (or missing — M1 not
        run) the function logs a single WARNING and returns the
        empty set so the daemon can idle gracefully without
        crashing.
    """

    log = logger or logging.getLogger("biotech_sniper.news_daemon.scope")

    owns_conn = False
    if conn is None:
        if db_path is None:
            # Lazy import — keep ``import scope`` cheap and free of
            # filesystem side effects; ``paths`` only resolves the
            # repo root, never creates directories.
            from biotech_sniper.paths import DATA_DIR

            db_path = DATA_DIR / "alpha_sniper.db"
        # Use the URI mode=ro form when the file exists so the
        # daemon never accidentally writes through this read path.
        target = str(db_path)
        try:
            if target == ":memory:" or not Path(target).exists():
                # File missing — fall back to the empty-table code
                # path: log once + return empty set. The daemon
                # idles instead of crashing.
                log.warning(
                    EMPTY_RUSSELL_WARNING_MESSAGE,
                    extra={
                        "event": "news_daemon_scope_empty_russell",
                        "src_module": "news_daemon.scope",
                        "reason": "db_missing",
                        "db_path": target,
                    },
                )
                return set()
            conn = sqlite3.connect(
                f"file:{target}?mode=ro",
                uri=True,
            )
            owns_conn = True
        except sqlite3.OperationalError as exc:
            log.warning(
                EMPTY_RUSSELL_WARNING_MESSAGE,
                extra={
                    "event": "news_daemon_scope_empty_russell",
                    "src_module": "news_daemon.scope",
                    "reason": "db_open_failed",
                    "db_path": target,
                    "error": repr(exc),
                },
            )
            return set()

    try:
        # Missing tables (M1 not run) → treat as "empty russell"
        # rather than raising. The daemon must idle, not crash.
        if not _table_exists(conn, "russell2k_biotech"):
            log.warning(
                EMPTY_RUSSELL_WARNING_MESSAGE,
                extra={
                    "event": "news_daemon_scope_empty_russell",
                    "src_module": "news_daemon.scope",
                    "reason": "table_missing",
                },
            )
            return set()
        if not _table_exists(conn, "universe"):
            # Universe missing is a separate failure mode — log
            # under a distinct event but still return empty so the
            # daemon idles.
            log.warning(
                "universe table missing; daemon idling",
                extra={
                    "event": "news_daemon_scope_universe_missing",
                    "src_module": "news_daemon.scope",
                    "reason": "table_missing",
                },
            )
            return set()

        # Cheap empty-russell check first — emits the canonical
        # WARNING required by VAL-M2-014 / VAL-M2-042 even when the
        # universe table is fully populated.
        empty_row = conn.execute(
            "SELECT 1 FROM russell2k_biotech LIMIT 1"
        ).fetchone()
        if empty_row is None:
            log.warning(
                EMPTY_RUSSELL_WARNING_MESSAGE,
                extra={
                    "event": "news_daemon_scope_empty_russell",
                    "src_module": "news_daemon.scope",
                    "reason": "table_empty",
                },
            )
            return set()

        rows = conn.execute(_INTERSECTION_SQL).fetchall()
    finally:
        if owns_conn and conn is not None:
            conn.close()

    out: Set[str] = set()
    for row in rows:
        # Tolerate both Row and tuple shapes — connect() sets
        # row_factory=Row but a bare sqlite3.connect() does not.
        ticker = row[0]
        if _is_blank(ticker):
            continue
        out.add(ticker)
    return out


def load_polled_universe(
    db_path: Union[str, Path],
    *,
    logger: Optional[logging.Logger] = None,
) -> Set[str]:
    """Alias for :func:`resolve_polled_tickers` taking a positional db path.

    This shape matches the f-m2-01 skeleton signature so existing
    callers compile unchanged.
    """

    return resolve_polled_tickers(db_path, logger=logger)


def filter_universe(
    tickers: Iterable[object],
    polled: Set[str],
) -> Set[str]:
    """Filter ``tickers`` to those present in ``polled``.

    Empty / whitespace-only / non-string tickers are silently
    rejected (per VAL-M2-053). Comparison is case-sensitive;
    canonical upper-case is enforced upstream by the news_events
    writer.

    Parameters
    ----------
    tickers:
        Iterable of raw ticker values (typically read from
        ``news_events.ticker``).
    polled:
        Pre-resolved polled-ticker set from
        :func:`resolve_polled_tickers`. Passing an empty set
        results in an empty return (the daemon idles).

    Returns
    -------
    set[str]
        The subset of ``tickers`` that survive both the
        empty/whitespace reject and the polled-set membership
        check.
    """

    out: Set[str] = set()
    for raw in tickers:
        if _is_blank(raw):
            continue
        # Cast is safe: ``_is_blank`` rejects non-strings.
        ticker = raw.strip()  # type: ignore[union-attr]
        if ticker in polled:
            out.add(ticker)
    return out
