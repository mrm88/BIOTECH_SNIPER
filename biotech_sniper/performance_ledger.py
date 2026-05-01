"""Reading-B performance-ledger roll-up queries.

This module provides the canonical entry point for the daily P&L
roll-up that distinguishes Reading-B (``news_event_entry``) plays
from the existing daily-curated (``open``) path. The rollup
function :func:`roll_up_day` accepts an optional ``event_filter``
parameter so callers can isolate a single path or compute the
union sum.

Validation contract coverage
----------------------------

* **VAL-M5-032** — ``roll_up_day(date, event_filter='news_event_entry')``
  returns Reading-B-only rows; ``event_filter='open'`` returns
  daily-curated-only rows.
* **VAL-M5-033** — Sum of P&L across ``'open'`` and
  ``'news_event_entry'`` filters equals the union sum (no
  double-counting, no missing fills).
* **VAL-M5-034** — Schema is unchanged here; the v10 migration
  (``010_reading_b_foundations``) extends the
  ``paper_orders.event`` CHECK constraint to admit
  ``'news_event_entry'`` while preserving every legacy value.
* **VAL-M5-048** — A ``news_event_entry`` parent + its exits
  ({iv_crush_exit, stop_loss, adverse_news, rotation}) all roll
  up to the SAME ledger play with ``event_path='news_event_entry'``.

P&L attribution model
---------------------

Every fill is attributed to a single ``event_path`` — the entry
event of the play it belongs to:

* For an entry order's fill (``purpose='entry'``) the
  ``event_path`` is the order's own ``event`` column
  (``'open'`` for daily-curated, ``'news_event_entry'`` for
  Reading-B).
* For an exit order's fill (``purpose='exit'``) the
  ``event_path`` is the **parent entry's** ``event``, resolved
  via ``parent.play_card_id = exit.parent_play_card_id`` AND
  ``parent.purpose = 'entry'``. This is what guarantees
  exit-fill cash flows roll up to the same bucket as the entry
  that opened the play (VAL-M5-048).

The ``play_id`` of a fill is similarly the parent entry's
``play_card_id``: for entry fills that's the row's own
``play_card_id``; for exit fills it's the row's
``parent_play_card_id``.

Realized P&L
------------

Per fill: ``side_sign * filled_price * filled_qty * 100`` where
``side_sign = +1`` for ``side='sell'`` and ``-1`` for
``side='buy'``. The 100 multiplier is the standard option
contract multiplier already used elsewhere in the codebase
(``execution_fills.compute_slippage_usd``,
``performance_tracker.take_snapshot``).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Union

__all__ = [
    "PlayContribution",
    "RollUpResult",
    "roll_up_day",
]


PathLike = Union[str, Path]


# Option contract multiplier — every realized-P&L computation in
# the codebase scales option premium by 100 (see
# :func:`biotech_sniper.execution_fills.compute_slippage_usd` and
# :func:`biotech_sniper.performance_tracker.take_snapshot`).
_CONTRACT_MULTIPLIER: int = 100


@dataclass(frozen=True)
class PlayContribution:
    """One play's contribution to the daily roll-up.

    Attributes
    ----------
    play_id:
        The parent entry's ``play_card_id``. Exits belonging to
        this play (linked via ``parent_play_card_id``) collapse
        into this row alongside the entry's own fill cash flow.
    event_path:
        The entry event that opened the play — ``'open'`` for
        daily-curated, ``'news_event_entry'`` for Reading-B.
    realized_pnl:
        Sum of fill cash flows for every order belonging to this
        play (entry buys are negative, exit sells positive),
        scaled by the option contract multiplier.
    fill_count:
        Number of ``execution_fills`` rows that contributed to the
        roll-up for this play.
    """

    play_id: str
    event_path: str
    realized_pnl: float
    fill_count: int


@dataclass(frozen=True)
class RollUpResult:
    """Result of a daily roll-up.

    Attributes
    ----------
    as_of_date:
        The ``YYYY-MM-DD`` date the rollup was computed for.
    event_filter:
        The filter that was applied (``None`` means union/no
        filter).
    plays:
        Per-play contributions (one row per distinct play_id
        whose fills were attributed to ``event_filter``).
    realized_pnl:
        Sum of ``play.realized_pnl`` across :attr:`plays`.
    play_count:
        ``len(plays)`` — the number of distinct plays that
        contributed.
    """

    as_of_date: str
    event_filter: Optional[str]
    plays: list[PlayContribution] = field(default_factory=list)
    realized_pnl: float = 0.0
    play_count: int = 0


# The roll-up SQL. Joins each fill to its order, then to its
# parent entry order (NULL for entry orders themselves) so we
# can compute the canonical ``event_path`` and ``play_id``.
#
# The ``event_path`` column is ``COALESCE(parent.event, po.event)``:
# for an entry-order fill there is no parent, so the entry's own
# event is used; for an exit-order fill the parent's event takes
# precedence.
#
# The ``play_id`` column is
# ``COALESCE(po.parent_play_card_id, po.play_card_id)`` — exit
# orders carry the parent's play_card_id in
# ``parent_play_card_id``, while entry orders identify their play
# via their own ``play_card_id``.
#
# Parent-row disambiguation (f-fix-m5-05-rollup-double-count):
# ``paper_orders.play_card_id`` is NOT unique per entry order —
# multi-strike entries (paper_executor multi-leg dispatch around
# lines 1810-1850) and rotation retries
# (rotation_engine.py:391-405) write multiple rows that share the
# same ``play_card_id`` AND ``purpose='entry'``. A naive
# ``LEFT JOIN paper_orders parent ON parent.play_card_id =
# po.parent_play_card_id AND parent.purpose='entry'`` therefore
# multiplies each exit fill's contribution by the number of entry
# rows sharing the play_card_id, silently double-counting the
# exit pnl. The fix collapses parents to exactly one row per
# (play_card_id, purpose='entry') by selecting MIN(id) — the
# earliest entry row "owns" the event_path label for the play.
# Entry rows still carry ``parent_play_card_id IS NULL`` so the
# LEFT JOIN never fires for them; only exits resolve through the
# subquery. See VAL-M5-033 (no double-counting partition) and the
# regression tests in test_performance_ledger_rollup.py.
#
# The query restricts to fills whose ``filled_at`` falls on the
# requested ``as_of_date`` (DATE-truncated for ISO-8601
# timestamps). The optional ``event_filter`` clause is appended
# after the SELECT so the parser doesn't have to reason about
# trailing ``WHERE`` placement when no filter is supplied.
_ROLLUP_SQL_BASE: str = """
SELECT
    COALESCE(po.parent_play_card_id, po.play_card_id) AS play_id,
    COALESCE(parent.event, po.event)                  AS event_path,
    SUM(
        CASE
            WHEN LOWER(po.side) = 'sell' THEN  ef.filled_price * ef.filled_qty
            WHEN LOWER(po.side) = 'buy'  THEN -ef.filled_price * ef.filled_qty
            ELSE 0
        END
    ) * :multiplier AS realized_pnl,
    COUNT(ef.id) AS fill_count
FROM execution_fills ef
JOIN paper_orders po
    ON po.id = ef.paper_order_id
LEFT JOIN (
    SELECT
        play_card_id,
        MIN(id) AS parent_id,
        event
    FROM paper_orders
    WHERE purpose = 'entry'
    GROUP BY play_card_id
) parent
    ON parent.play_card_id = po.parent_play_card_id
WHERE substr(ef.filled_at, 1, 10) = :as_of_date
"""

_ROLLUP_SQL_GROUP: str = """
GROUP BY play_id, event_path
HAVING play_id IS NOT NULL AND event_path IS NOT NULL
ORDER BY play_id
"""


def _execute_rollup(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    event_filter: Optional[str],
) -> list[PlayContribution]:
    params: dict[str, object] = {
        "as_of_date": as_of_date,
        "multiplier": _CONTRACT_MULTIPLIER,
    }
    sql = _ROLLUP_SQL_BASE
    if event_filter is not None:
        sql += "\n  AND COALESCE(parent.event, po.event) = :event_filter"
        params["event_filter"] = event_filter
    sql += _ROLLUP_SQL_GROUP

    rows = conn.execute(sql, params).fetchall()
    plays: list[PlayContribution] = []
    for row in rows:
        # ``conn.row_factory`` may or may not be ``sqlite3.Row``;
        # support positional access as a fallback.
        if isinstance(row, sqlite3.Row):
            play_id = row["play_id"]
            event_path = row["event_path"]
            realized = row["realized_pnl"]
            fill_count = row["fill_count"]
        else:
            play_id, event_path, realized, fill_count = row
        plays.append(
            PlayContribution(
                play_id=str(play_id),
                event_path=str(event_path),
                realized_pnl=float(realized or 0.0),
                fill_count=int(fill_count or 0),
            )
        )
    return plays


def _validate_event_filter(event_filter: Optional[str]) -> None:
    if event_filter is None:
        return
    if not isinstance(event_filter, str) or not event_filter.strip():
        raise ValueError(
            f"event_filter must be a non-empty string or None; "
            f"got {event_filter!r}"
        )


def _validate_as_of_date(as_of_date: str) -> None:
    if not isinstance(as_of_date, str) or len(as_of_date) < 10:
        raise ValueError(
            f"as_of_date must be a YYYY-MM-DD string; got {as_of_date!r}"
        )
    # Cheap shape check — full ISO parsing isn't needed for a
    # SQL filter, but rejecting obviously-malformed input here
    # surfaces test seeding bugs early.
    if as_of_date[4] != "-" or as_of_date[7] != "-":
        raise ValueError(
            f"as_of_date must follow YYYY-MM-DD; got {as_of_date!r}"
        )


def roll_up_day(
    as_of_date: str,
    event_filter: Optional[str] = None,
    *,
    db_path: Optional[PathLike] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> RollUpResult:
    """Roll up a single day's fill cash flows by ``event_path``.

    Parameters
    ----------
    as_of_date:
        ``YYYY-MM-DD`` — the date whose ``execution_fills`` rows
        should be aggregated. Matched on the leading 10 chars of
        ``filled_at`` (project convention: ISO-8601 with optional
        millisecond suffix).
    event_filter:
        ``None`` to compute the union sum across every event
        path; or one of the project's ``paper_orders.event``
        tokens (``'open'``, ``'news_event_entry'``, ...) to
        restrict the rollup to plays opened by that event.
    db_path:
        Path to the SQLite database. Required when ``conn`` is
        not provided.
    conn:
        Pre-opened ``sqlite3.Connection``. When supplied, the
        connection is used directly and NOT closed on return —
        the caller owns its lifetime. Useful when composing
        rollups inside a larger transaction.

    Returns
    -------
    RollUpResult
        Aggregate result; an empty day returns a result with
        ``plays=[]``, ``realized_pnl=0.0``, ``play_count=0``.

    Raises
    ------
    ValueError
        If neither ``db_path`` nor ``conn`` is supplied, or if
        ``as_of_date`` is not a YYYY-MM-DD string, or if
        ``event_filter`` is an empty / non-string value.
    """
    _validate_as_of_date(as_of_date)
    _validate_event_filter(event_filter)

    if conn is None and db_path is None:
        raise ValueError("roll_up_day requires either db_path or conn")

    own_conn = False
    if conn is None:
        # ``db_path`` is guaranteed non-None by the check above.
        assert db_path is not None
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        own_conn = True

    try:
        plays = _execute_rollup(
            conn,
            as_of_date=as_of_date,
            event_filter=event_filter,
        )
    finally:
        if own_conn:
            conn.close()

    realized_pnl = sum(p.realized_pnl for p in plays)
    return RollUpResult(
        as_of_date=as_of_date,
        event_filter=event_filter,
        plays=list(plays),
        realized_pnl=float(realized_pnl),
        play_count=len(plays),
    )


def supported_event_paths() -> Iterable[str]:
    """Return the canonical ``event_path`` token set.

    Useful for callers that want to enumerate per-event-path
    sub-totals (e.g. the Reading-B CLI report) without
    hard-coding the token list.

    The order matches the v10 migration's CHECK enum: every
    legacy entry-or-exit value first, then the new
    ``'news_event_entry'``.
    """
    return (
        "open",
        "iv_crush_exit",
        "stop_loss",
        "adverse_news",
        "rotation",
        "news_event_entry",
    )
