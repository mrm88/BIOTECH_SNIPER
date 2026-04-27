"""Hold-duration policy + exit arbiter (f-m3-09).

This module is the **single arbiter** for whether an exit may fire on
an active paper-trading play. It owns three responsibilities:

1. The closed enum of allowed exit-event tags
   (:data:`ALLOWED_EXIT_EVENTS`).
2. The ``client_order_id`` / ``play_card_id`` namespace generator
   (:func:`make_exit_client_order_id`, :func:`make_exit_play_card_id`)
   that downstream triggers (iv_crush_exit, stop_loss, adverse_news,
   rotation) all share so the broker-level idempotency invariant
   (``ticker-event-date`` is unique within a day) holds.
3. The policy guards (:func:`should_resolve`, :func:`assert_exit_allowed`)
   that ``auto_resolver`` and :class:`PaperExecutor.submit_exit` consult
   before they take action.

Validation contract assertions fulfilled
----------------------------------------

* **VAL-M3-047** — the only sells permitted on a non-catalyst date for
  an active play are those tagged with one of the four allowed exit
  triggers. Any other ``event`` (or no event at all) on a sell raises
  :class:`HoldPolicyViolation` BEFORE any submission.
* **VAL-M3-048** — :func:`should_resolve` returns ``False`` when
  ``today < catalyst_date`` AND no exit order has filled, so
  ``auto_resolver`` cannot pre-resolve a play.
* **VAL-M3-049** — :data:`ALLOWED_EXIT_EVENTS` includes the f-m3-05
  wire value ``'iv_crush_exit'`` (NOT the historical ``'iv_crush'``)
  to match the persisted-row evidence already shipped by
  :class:`biotech_sniper.iv_crush_exit_rules.IVCrushExitRunner`.
* **VAL-M3-052** — :data:`ALLOWED_EXIT_EVENTS` matches the f-m3-09
  ``orders.event`` CHECK constraint (the ``'open'`` entry tag is
  separate; see :data:`ENTRY_EVENT`).

Design notes
------------

* The four allowed events are documented inline so schema +
  ``hold_policy`` cannot drift apart.
* The "catalyst-day open" trigger (the original 50% IV-crush exit) is
  tagged ``'iv_crush_exit'`` rather than ``'iv_crush'`` to preserve
  backward compatibility with rows already persisted by f-m3-05 (per
  VAL-M3-028 evidence). Updating the wire value would require a
  backfill migration that this feature deliberately avoids.
* :func:`assert_exit_allowed` is stateless and side-effect-free so it
  can be called from any layer (the executor, the resolver, the
  individual trigger modules) without reaching for the database.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Final, Mapping, Optional


logger = logging.getLogger(__name__)


__all__ = [
    "ALLOWED_EXIT_EVENTS",
    "ENTRY_EVENT",
    "VALID_EVENT_VALUES",
    "HoldPolicyViolation",
    "is_exit_allowed",
    "make_exit_client_order_id",
    "make_exit_play_card_id",
    "assert_exit_allowed",
    "should_resolve",
    "coerce_date",
]


# ---------------------------------------------------------------------------
# Allowed event enum
# ---------------------------------------------------------------------------

#: The four allowed exit-event tags. Mirrors the f-m3-09 description
#: verbatim. Order is alphabetical for grep stability; the lookup is
#: a frozenset so callers do not depend on iteration order.
ALLOWED_EXIT_EVENTS: Final[frozenset[str]] = frozenset(
    {"adverse_news", "iv_crush_exit", "rotation", "stop_loss"}
)

#: The entry-event tag that paper-executor entries use. Distinct from
#: the four exit triggers. Persisted onto entry rows so the orders
#: table can disambiguate entries from exits via a single column.
ENTRY_EVENT: Final[str] = "open"

#: Closed enum of legal ``orders.event`` values. Matches the CHECK
#: constraint declared in ``schema.sql`` so the Python and SQL layers
#: cannot drift.
VALID_EVENT_VALUES: Final[frozenset[str]] = (
    ALLOWED_EXIT_EVENTS | {ENTRY_EVENT}
)


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class HoldPolicyViolation(Exception):
    """Raised when an attempted action violates the hold/exit policy.

    The exception carries a human-readable message naming the
    offending event and (when supplied) the play and date so the
    operator can identify the call site without reading logs.
    """


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def is_exit_allowed(event: Optional[str]) -> bool:
    """Return ``True`` when ``event`` names a legal exit trigger.

    ``None`` and unknown strings return ``False``; callers MUST treat
    the negative result as a refusal to submit (see
    :func:`assert_exit_allowed` for the raising variant).
    """
    if not isinstance(event, str):
        return False
    return event in ALLOWED_EXIT_EVENTS


def coerce_date(value: Any) -> datetime.date:
    """Normalise ``value`` to a :class:`datetime.date`.

    Accepts a :class:`date`, :class:`datetime` (strips the time
    component), an ISO-8601 ``YYYY-MM-DD`` string, or ``None``
    (defaults to UTC today). Any other type raises ``TypeError``.
    """
    if value is None:
        return datetime.date.today()
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        return datetime.date.fromisoformat(value)
    raise TypeError(
        "coerce_date() accepts None, date, datetime, or ISO string; "
        f"got {type(value).__name__}"
    )


def make_exit_client_order_id(
    ticker: str, event: str, date: Any
) -> str:
    """Return ``f"{ticker}-{event}-{YYYY-MM-DD}"`` (the broker idempotency key).

    ``ticker`` is upper-cased, ``event`` is validated against
    :data:`ALLOWED_EXIT_EVENTS`, and ``date`` is coerced via
    :func:`coerce_date`. The resulting string is the canonical
    ``client_order_id`` shared by every f-m3-09 exit trigger so the
    broker (and our local persistence layer) automatically de-dupe a
    same-day re-submission of the same exit.

    Raises :class:`HoldPolicyViolation` for unknown events so the
    caller cannot smuggle arbitrary strings through this helper.
    """
    if not isinstance(ticker, str) or not ticker.strip():
        raise HoldPolicyViolation(
            "make_exit_client_order_id: ticker must be a non-empty string"
        )
    if not is_exit_allowed(event):
        raise HoldPolicyViolation(
            f"make_exit_client_order_id: event={event!r} is not allowed; "
            f"allowed events are {sorted(ALLOWED_EXIT_EVENTS)}"
        )
    return f"{ticker.strip().upper()}-{event}-{coerce_date(date).isoformat()}"


def make_exit_play_card_id(
    parent_play_card_id: Optional[str],
    ticker: str,
    event: str,
    date: Any,
) -> str:
    """Return a deterministic play-card id for an exit submission.

    Format: ``{parent_play_card_id}-{event}-{YYYY-MM-DD}`` when a
    parent id is known, falling back to
    :func:`make_exit_client_order_id` otherwise. Used by every exit
    trigger so the persisted ``orders`` row's ``play_card_id`` column
    is recoverable from the broker side via a textual prefix match
    against the entry's ``play_card_id``.
    """
    if not is_exit_allowed(event):
        raise HoldPolicyViolation(
            f"make_exit_play_card_id: event={event!r} is not allowed; "
            f"allowed events are {sorted(ALLOWED_EXIT_EVENTS)}"
        )
    date_str = coerce_date(date).isoformat()
    if parent_play_card_id and isinstance(parent_play_card_id, str):
        return f"{parent_play_card_id}-{event}-{date_str}"
    return make_exit_client_order_id(ticker, event, date)


def assert_exit_allowed(
    *,
    event: Optional[str],
    side: str = "sell",
    catalyst_date: Any = None,
    today: Any = None,
    play_id: Optional[str] = None,
) -> None:
    """Raise :class:`HoldPolicyViolation` if the proposed exit is illegal.

    The arbiter exists in one place so every caller (the paper
    executor, the resolver, individual trigger runners) honours the
    same policy:

    * Buys (``side != 'sell'``) bypass the gate — entries are not
      governed by hold rules.
    * Sells on the catalyst date (``today == catalyst_date``) are
      always legal because that is when the IV-crush exit fires.
    * Sells **before** the catalyst date MUST carry an ``event`` in
      :data:`ALLOWED_EXIT_EVENTS`. ``None`` / unknown events raise.
    * Sells **after** the catalyst date are also gated to the four
      allowed events — once a play exits its window, only the
      sanctioned triggers may liquidate it.

    A missing ``catalyst_date`` is treated as "no catalyst-day relief"
    — the strict path applies. This is the conservative default so
    a malformed play card cannot smuggle a free-form sell through.
    """
    if str(side).strip().lower() != "sell":
        return

    catalyst = None
    if catalyst_date is not None:
        try:
            catalyst = coerce_date(catalyst_date)
        except (TypeError, ValueError):
            catalyst = None

    today_d = coerce_date(today) if today is not None else datetime.date.today()

    if catalyst is not None and today_d == catalyst:
        # Catalyst-day open is the natural exit window; the IV-crush
        # 50% sell still flows through a tagged event but a sell
        # without a tag is permitted on this single day per the
        # original mission policy. We still log that an untagged
        # sell happened so it shows up in audits.
        if not is_exit_allowed(event):
            logger.info(
                "hold_policy.catalyst_day_untagged_sell play_id=%s event=%s "
                "catalyst_date=%s today=%s",
                play_id,
                event,
                catalyst,
                today_d,
            )
        return

    if is_exit_allowed(event):
        return

    raise HoldPolicyViolation(
        "hold_policy: refusing to submit a sell for play_id="
        f"{play_id!r} on {today_d.isoformat()} (catalyst_date="
        f"{catalyst.isoformat() if catalyst else 'unknown'}) with "
        f"event={event!r}; allowed events are "
        f"{sorted(ALLOWED_EXIT_EVENTS)}"
    )


def should_resolve(
    play: Mapping[str, Any],
    *,
    today: Any = None,
    has_filled_exit: bool = False,
) -> bool:
    """Return ``True`` when ``auto_resolver`` may mark ``play`` resolved.

    Implements the f-m3-09 rule (and VAL-M3-048):

    * If ``today >= catalyst_date`` → resolve permitted.
    * If an exit order has already filled (``has_filled_exit=True``)
      → resolve permitted (the position is closed).
    * Otherwise (``today < catalyst_date`` AND no exit filled) → the
      caller MUST keep the play active.

    A missing / unparseable ``catalyst_date`` is interpreted as "we
    do not know" — the conservative answer is to allow resolution
    only when an exit has filled. Without a catalyst we cannot know
    whether the no-pre-resolve guard applies, so the resolver should
    fall back to its existing per-play heuristics.
    """
    today_d = coerce_date(today) if today is not None else datetime.date.today()

    catalyst_raw = (
        play.get("catalyst_date")
        or play.get("pdufa_date")
        or play.get("estimated_announcement")
    )
    catalyst: Optional[datetime.date] = None
    if catalyst_raw:
        try:
            # ``estimated_announcement`` is sometimes a YYYY-MM string;
            # coerce_date raises on those — fall back to None.
            catalyst = coerce_date(str(catalyst_raw)[:10])
        except (TypeError, ValueError):
            catalyst = None

    if has_filled_exit:
        return True

    if catalyst is None:
        # Conservative default: refuse to pre-resolve when we cannot
        # verify the catalyst window. The resolver still calls this
        # with explicit signals (filled exit, expired option) so a
        # play with no catalyst date never gets stuck active forever.
        return False

    return today_d >= catalyst
