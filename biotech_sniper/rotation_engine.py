"""Rotation engine (M3 feature f-m3-10).

This module implements :func:`evaluate_rotation`, the single sanctioned
entry point for swapping a weak active paper-trading play for a higher-
ranked challenger from the day's :data:`scoring_cache`. It runs at the
top of every intraday cycle (wired from
:mod:`biotech_sniper.intraday_scanner`) so a stale incumbent never holds
a slot when a strictly better candidate has scored today.

Algorithm
---------
1. Load the active plays (default: from the SQLite ``orders`` /
   ``plays`` view; tests inject the list directly). When the active
   count is below ``max_concurrent`` we short-circuit — rotation is
   only useful when slots are full.
2. Iterate the day's ``scoring_cache`` candidates (highest
   ``ensemble_score`` first). Skip any candidate whose ticker already
   has an active play.
3. Find the weakest incumbent — the active play with the **lowest
   current** ``ensemble_score`` (the score is re-scored today; the
   caller MUST supply the freshly-scored value on every active play).
4. Compute ``delta = challenger.ensemble_score - weakest.ensemble_score``.
5. Skip with ``rotation_skipped: below_threshold`` when
   ``delta < ROTATION_THRESHOLD`` (default 0.10).
6. Skip with ``rotation_skipped: catalyst_too_close`` when the
   challenger's catalyst date or the incumbent's catalyst date is
   within 24h of today (i.e. ``catalyst_date - today < 1 day``). The
   guard fires for either side independently.
7. Fire :func:`biotech_sniper.llm.llm_debate.run_debate` with
   ``trigger='rotation'`` for both incumbent and challenger so the
   debate transcript pair is persisted in ``llm_debate``. The
   adjudicated ``final_grade`` of each side is compared via
   :data:`LETTER_GRADE_ORDER` (lower index = better grade).
8. Skip with ``rotation_skipped: debate_inverted_preference`` when
   the debate's challenger grade does NOT strictly outrank the
   incumbent's grade (i.e. the LLM debate inverts the static
   ensemble ordering).
9. Otherwise execute the swap in a single run:
   * :meth:`PaperExecutor.submit_exit` with ``event='rotation'``
     (sell-to-close the incumbent's full open quantity).
   * :meth:`PaperExecutor.execute` with ``event='open'`` (buy the
     challenger leg). The challenger's option_legs come from the
     candidate Mapping so the caller controls the play card shape.
10. The post-run active count is ``≤ max_concurrent`` because each
    swap removes one incumbent before adding one challenger.

Audit logging
-------------
Every skip is appended to ``state/audit_latest.json`` under the
``rotation_skipped`` key (an object with ``reason`` plus per-skip
context). Re-running on the same day overwrites only the
``rotation_skipped`` block, preserving every other key in the audit
JSON.

Validation contract assertions fulfilled
----------------------------------------

* **VAL-M3-053** — above-threshold + outside-24h fires exactly one
  ``sell_to_close(event='rotation')`` + one ``buy(event='open')`` per
  rotation; resulting active count ≤ ``max_concurrent``.
* **VAL-M3-054** — below-threshold candidates do NOT rotate (no
  ``event='rotation'`` rows persisted).
* **VAL-M3-055** — within-24h candidates do NOT rotate; the audit
  JSON's ``rotation_skipped.reason`` is ``catalyst_too_close``.
* **VAL-M3-056** — every rotation decision creates one or more
  ``llm_debate`` rows with ``trigger='rotation'`` (one per side).
* **VAL-M3-057** — rotation only fires when the debate's
  ``final_grade(challenger) > final_grade(incumbent)``; an inverted
  preference is logged as
  ``rotation_skipped.reason='debate_inverted_preference'``.
"""

from __future__ import annotations

import datetime
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from biotech_sniper import config as _config
from biotech_sniper import db as _db
from biotech_sniper import hold_policy as _hold_policy
from biotech_sniper.llm.claude_client import LETTER_GRADE_ORDER
from biotech_sniper.paths import BASE_DIR, DATA_DIR


__all__ = [
    "ROTATION_THRESHOLD",
    "ROTATION_EVENT",
    "ENTRY_EVENT",
    "VALID_SKIP_REASONS",
    "evaluate_rotation",
    "challenger_outranks",
    "is_within_24h",
    "load_active_plays_from_db",
    "load_today_candidates_from_db",
    "DebateRunner",
    "_validate_challenger_card_complete",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Minimum ``challenger.ensemble_score - weakest.ensemble_score`` delta
#: required to fire a rotation. Below this threshold the rotation is
#: skipped with ``reason='below_threshold'``. Documented in
#: ``mission.md`` and ``AGENTS.md`` (M3 section).
ROTATION_THRESHOLD: float = 0.10

#: Event tag persisted on the ``orders`` row when an incumbent is
#: closed to free a slot. Mirrors the f-m3-09 enum.
ROTATION_EVENT: str = "rotation"

#: Event tag persisted on the ``orders`` row when a challenger is
#: opened. Mirrors the ``hold_policy.ENTRY_EVENT`` constant verbatim.
ENTRY_EVENT: str = _hold_policy.ENTRY_EVENT  # 'open'

#: Closed enum of allowed ``rotation_skipped`` reasons. Matches the
#: feature spec (catalyst_too_close, below_threshold,
#: debate_inverted_preference) plus the f-m3-21 preflight reason
#: (``incomplete_challenger_card``) which fires before any leg is
#: submitted when the challenger candidate is missing the metadata
#: required to construct a valid PaperExecutor card.
VALID_SKIP_REASONS: frozenset[str] = frozenset(
    {
        "catalyst_too_close",
        "below_threshold",
        "debate_inverted_preference",
        "incomplete_challenger_card",
    }
)


# Signature of an injectable rotation-debate runner. Production
# callers can wrap :func:`biotech_sniper.llm.llm_debate.run_debate`;
# tests pass a hand-rolled callable so the LLM stack is not exercised.
#
# The runner receives the challenger and incumbent dicts (carrying at
# least ``ticker`` plus optionally ``scoring_cache_id`` and
# ``science_grade``) and returns a Mapping with at minimum:
#
# * ``challenger_grade`` — letter grade per :data:`LETTER_GRADE_ORDER`.
# * ``incumbent_grade`` — letter grade per :data:`LETTER_GRADE_ORDER`.
#
# Optional keys (rendered into the decision audit trail when present):
# ``debate_id``, ``rounds``, ``cost_usd``, ``short_circuited``.
DebateRunner = Callable[..., Mapping[str, Any]]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _coerce_date_or_none(value: Any) -> Optional[datetime.date]:
    """Best-effort coerce ``value`` to a date; ``None`` for unparseable inputs.

    Mirrors :func:`hold_policy.coerce_date` but never raises — bad
    inputs collapse to ``None`` so the caller can decide how to
    handle a missing catalyst date (the ``is_within_24h`` guard
    treats ``None`` as "do not block").
    """
    if value is None:
        return None
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value[:10])
        except (TypeError, ValueError):
            return None
    return None


def is_within_24h(
    catalyst_date: Any, today: Any = None
) -> bool:
    """Return ``True`` when ``catalyst_date - today < 1 day``.

    Per VAL-M3-055: a catalyst date that is *less than* one day away
    from today (i.e. catalyst is today or in the past) blocks
    rotation. A catalyst on the day after today is NOT within 24h.

    A ``None`` / unparseable catalyst date returns ``False`` so a
    rotation cannot be silently blocked by a missing catalyst date.
    Operators must surface the missing catalyst upstream rather
    than have the rotation engine guess.
    """
    today_d = _hold_policy.coerce_date(today) if today is not None else datetime.date.today()
    cat_d = _coerce_date_or_none(catalyst_date)
    if cat_d is None:
        return False
    return (cat_d - today_d).days < 1


def challenger_outranks(
    challenger_grade: Optional[str], incumbent_grade: Optional[str]
) -> bool:
    """Return ``True`` when ``challenger_grade`` is strictly better.

    Compares both inputs against :data:`LETTER_GRADE_ORDER` (lower
    index = better grade). Unknown grades return ``False`` —
    rotation only fires on a positive, well-defined ranking.
    """
    if challenger_grade not in LETTER_GRADE_ORDER:
        return False
    if incumbent_grade not in LETTER_GRADE_ORDER:
        # An unknown incumbent grade is treated as "we cannot
        # confirm the challenger is better"; refuse to rotate.
        return False
    return (
        LETTER_GRADE_ORDER.index(challenger_grade)
        < LETTER_GRADE_ORDER.index(incumbent_grade)
    )


def _coerce_qty(play: Mapping[str, Any]) -> int:
    """Return the open contract count for ``play`` (defaults to 0)."""
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


def _ticker(play: Mapping[str, Any]) -> str:
    raw = play.get("ticker") or play.get("symbol") or ""
    return str(raw).strip().upper()


def _validate_challenger_card_complete(
    candidate: Mapping[str, Any],
) -> bool:
    """Return ``True`` when ``candidate`` carries a fully-shaped buy card.

    Per f-m3-21, the rotation engine submits the SELL leg before the
    BUY leg, so a partially-populated challenger can leave the
    portfolio one-sided (incumbent sold, challenger never bought) if
    the buy raises mid-flight. This validator runs BEFORE the sell
    leg fires so an incomplete card aborts the rotation cleanly,
    with a structured ``rotation_skipped`` audit entry.

    A challenger card is considered complete when ALL of the
    following are true:

    * ``candidate['catalyst_date']`` is non-None and parseable to a
      :class:`datetime.date` — the 24h guard depends on this.
    * ``candidate['play_card']`` is a Mapping (or the candidate itself
      carries the buy-card fields directly).
    * ``candidate['option_legs']`` is a non-empty Sequence and the
      first leg is a Mapping with a non-empty ``symbol`` string.

    The validator is intentionally conservative: ANY missing piece
    returns ``False`` so the engine never submits half a rotation.
    """
    if not isinstance(candidate, Mapping):
        return False

    # 1) catalyst_date must be present and parseable.
    if _coerce_date_or_none(candidate.get("catalyst_date")) is None:
        return False

    # 2) play_card must exist (either nested or the candidate itself
    #    must be usable as the play card).
    play_card = candidate.get("play_card")
    if not isinstance(play_card, Mapping):
        return False

    # 3) option_legs must be a non-empty Sequence whose first leg
    #    has a non-empty ``symbol`` string. Prefer the candidate's
    #    top-level ``option_legs`` (rotation engine reads from there
    #    in ``_extract_buy_symbol``) but fall back to the play_card's
    #    nested copy.
    legs = candidate.get("option_legs")
    if not isinstance(legs, Sequence) or isinstance(legs, (str, bytes)):
        legs = play_card.get("option_legs")
    if not isinstance(legs, Sequence) or isinstance(legs, (str, bytes)):
        return False
    if len(legs) == 0:
        return False
    first = legs[0]
    if not isinstance(first, Mapping):
        return False
    sym = first.get("symbol")
    if not isinstance(sym, str) or not sym.strip():
        return False

    return True


# ---------------------------------------------------------------------------
# Audit JSON merge
# ---------------------------------------------------------------------------


def _merge_audit_block(
    audit_path: Path, key: str, block: Mapping[str, Any]
) -> None:
    """Merge ``block`` into ``audit_latest.json`` under ``key``.

    Idempotent: existing keys other than ``key`` are preserved
    verbatim, and ``key`` is overwritten with ``block``. Atomic write
    via a sibling ``.tmp`` file so a crash mid-write cannot leave a
    truncated JSON behind.
    """
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if audit_path.is_file():
        try:
            with audit_path.open("r", encoding="utf-8") as fp:
                existing = json.load(fp)
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}
    existing[key] = dict(block)
    tmp = audit_path.with_suffix(audit_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(existing, fp, indent=2, sort_keys=True)
    tmp.replace(audit_path)


# ---------------------------------------------------------------------------
# DB-backed loaders (production wiring)
# ---------------------------------------------------------------------------


def _build_occ_symbol(
    ticker: str,
    expiry: Any,
    option_type: Any,
    strike: Any,
) -> Optional[str]:
    """Construct a standard OCC option symbol from its components.

    Format: ``TICKER + YYMMDD + (C|P) + STRIKE * 1000 zero-padded to 8``.
    Returns ``None`` when any component cannot be parsed cleanly so
    callers can fall back to the legacy ``option_symbol`` payload key
    or skip emitting a symbol entirely.

    Example: ``AXSM`` + ``2025-06-20`` + ``call`` + ``125.0`` →
    ``AXSM250620C00125000``.
    """
    if not isinstance(ticker, str) or not ticker.strip():
        return None
    expiry_str = str(expiry or "").strip()
    if len(expiry_str) < 10:
        return None
    try:
        # Accept both ``2025-06-20`` and ``20250620`` styles.
        if "-" in expiry_str:
            datetime.date.fromisoformat(expiry_str[:10])
            yymmdd = expiry_str.replace("-", "")[2:8]
        else:
            yymmdd = expiry_str[2:8]
            datetime.datetime.strptime(yymmdd, "%y%m%d")
    except (TypeError, ValueError):
        return None
    opt_raw = str(option_type or "").strip().lower()
    if opt_raw.startswith("c"):
        cp = "C"
    elif opt_raw.startswith("p"):
        cp = "P"
    else:
        return None
    try:
        strike_thousandths = int(round(float(strike) * 1000.0))
    except (TypeError, ValueError):
        return None
    if strike_thousandths < 0:
        return None
    return f"{ticker.strip().upper()}{yymmdd}{cp}{strike_thousandths:08d}"


def _latest_filled_buy_qty(
    conn: sqlite3.Connection, play_card_id: Optional[str]
) -> Optional[int]:
    """Return ``qty`` from the most-recent filled buy for ``play_card_id``.

    Looks up ``paper_orders`` for ``side='buy'`` AND ``event='open'``
    AND ``status='filled'``, ordered by ``created_at DESC`` so the
    most recent fill wins (handles the multi-strike case where two
    buys share the same play_card_id by returning the most recent).

    Returns ``None`` when no row matches so the caller can fall back
    to the play card's stored ``qty`` / ``contracts`` payload key
    rather than emitting a synthetic zero (which would later trip
    :func:`_submit_rotation_sell`'s ``qty < 1`` guard).
    """
    if not play_card_id:
        return None
    try:
        row = conn.execute(
            """
            SELECT qty FROM paper_orders
             WHERE play_card_id = ?
               AND side = 'buy'
               AND event = 'open'
               AND status = 'filled'
             ORDER BY created_at DESC, id DESC
             LIMIT 1
            """,
            (play_card_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        # Schema not migrated yet — degrade to None so the caller
        # falls back to the payload qty.
        return None
    if row is None:
        return None
    qty = row["qty"] if isinstance(row, sqlite3.Row) else row[0]
    try:
        return int(qty) if qty is not None else None
    except (TypeError, ValueError):
        return None


def _latest_scoring_cache_for_ticker(
    conn: sqlite3.Connection, ticker: str
) -> Optional[dict[str, Any]]:
    """Return the latest ``scoring_cache`` row for ``ticker`` or ``None``.

    "Latest" is ordered by ``as_of_date DESC, id DESC`` so a fresh
    intraday re-score wins over an older one. Returns the full row
    so callers can read both ``id`` (for the debate runner's
    ``scoring_cache_id``) and ``ensemble_score`` (for the rotation
    weakest-pick).
    """
    if not ticker:
        return None
    try:
        row = conn.execute(
            """
            SELECT id, ticker, as_of_date, ensemble_score,
                   science_grade, claude_grade, gemini_grade,
                   grok_score, payload
              FROM scoring_cache
             WHERE ticker = ?
             ORDER BY as_of_date DESC, id DESC
             LIMIT 1
            """,
            (ticker,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row is not None else None


def _latest_play_for_ticker(
    conn: sqlite3.Connection, ticker: str
) -> Optional[dict[str, Any]]:
    """Return the most-recent ``plays`` row for ``ticker`` (any status).

    Used by :func:`load_today_candidates_from_db` to source option
    leg metadata (strike, expiry, type) for a challenger that has
    no on-disk play card yet. Falls back across statuses so a
    recently-resolved play still surfaces a usable OCC symbol when
    the same ticker is back in the scoring_cache today.
    """
    if not ticker:
        return None
    try:
        row = conn.execute(
            """
            SELECT id, source_key, ticker, status, catalyst_date,
                   option_type, option_strike, option_expiry, payload
              FROM plays
             WHERE ticker = ?
             ORDER BY created_at DESC, id DESC
             LIMIT 1
            """,
            (ticker,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row is not None else None


def _merge_payload(record: dict[str, Any]) -> dict[str, Any]:
    """Return ``record`` with its JSON ``payload`` merged in.

    Mirrors :func:`iv_crush_exit_rules.load_active_plays_from_db`:
    the SQL columns override JSON payload values so a row's
    canonical ``catalyst_date`` (from the column) wins over a stale
    payload-embedded copy.
    """
    payload_raw = record.pop("payload", None)
    merged: dict[str, Any] = {}
    if isinstance(payload_raw, str) and payload_raw.strip():
        try:
            payload_dict = json.loads(payload_raw)
        except (TypeError, ValueError):
            payload_dict = None
        if isinstance(payload_dict, dict):
            merged.update(payload_dict)
    for key, value in record.items():
        if value is not None:
            merged[key] = value
    return merged


def load_active_plays_from_db(
    *, db_path: Optional[Path] = None
) -> list[dict[str, Any]]:
    """Return active option plays from the SQLite ``plays`` table.

    Each returned dict carries the five fields the rotation engine
    requires for production wiring:

    * ``scoring_cache_id`` — latest ``scoring_cache.id`` for the
      ticker (joined on ticker, ordered by ``as_of_date DESC``).
      Drives the default debate runner's ``run_debate(...)`` call.
    * ``symbol`` — the OCC option symbol. Sourced (in priority
      order) from the payload's legacy ``option_symbol`` /
      ``symbol`` keys, the first leg of any embedded
      ``option_legs``, or constructed from
      ``ticker`` + ``option_expiry`` + ``option_type`` +
      ``option_strike``.
    * ``qty`` — current open contract count. Sourced from the most
      recent ``paper_orders`` row with ``side='buy' AND
      event='open' AND status='filled' AND play_card_id=?``
      (per the f-m3-18 spec). Falls back to the payload's
      ``qty`` / ``contracts`` keys when no broker fill is yet
      persisted (greenfield environments + early-cycle wiring).
    * ``ensemble_score`` — latest ``scoring_cache.ensemble_score``
      for the ticker.
    * ``catalyst_date`` — ``plays.catalyst_date`` (column).

    Returns an empty list when the database file does not exist or
    the ``plays`` table has not been migrated yet so callers can
    treat "no DB" and "no active plays" identically.
    """
    target = (
        Path(db_path) if db_path is not None else DATA_DIR / "alpha_sniper.db"
    )
    if not target.is_file():
        return []
    try:
        conn = _db.connect(target)
    except sqlite3.Error:
        return []
    try:
        try:
            rows = conn.execute(
                """
                SELECT id, source_key, ticker, status, catalyst_date,
                       option_type, option_strike, option_expiry,
                       payload
                  FROM plays
                 WHERE status = 'active'
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            record = _merge_payload(dict(row))
            ticker = str(record.get("ticker") or "").strip().upper()
            if not ticker:
                continue

            # ``play_card_id`` from payload — fall back to source_key
            # so a play that pre-dates the play_card_formatter still
            # has a stable identifier to key paper_orders by.
            play_card_id = (
                record.get("play_card_id") or record.get("source_key")
            )
            record["play_card_id"] = play_card_id

            # Symbol: payload override → first leg → constructed.
            symbol = record.get("option_symbol") or record.get("symbol")
            if not symbol:
                legs = record.get("option_legs")
                if isinstance(legs, Sequence) and legs and isinstance(
                    legs[0], Mapping
                ):
                    symbol = legs[0].get("symbol")
            if not symbol:
                symbol = _build_occ_symbol(
                    ticker,
                    record.get("option_expiry"),
                    record.get("option_type"),
                    record.get("option_strike"),
                )
            if symbol:
                record["symbol"] = symbol

            # qty: paper_orders filled buy → payload qty/contracts.
            qty = _latest_filled_buy_qty(conn, play_card_id)
            if qty is None:
                for key in ("qty", "contracts", "open_qty"):
                    raw = record.get(key)
                    if raw is None:
                        continue
                    try:
                        qty = int(raw)
                    except (TypeError, ValueError):
                        continue
                    break
            if qty is not None:
                record["qty"] = int(qty)

            # ensemble_score + scoring_cache_id from latest
            # scoring_cache row for this ticker.
            score_row = _latest_scoring_cache_for_ticker(conn, ticker)
            if score_row is not None:
                record["scoring_cache_id"] = score_row.get("id")
                if score_row.get("ensemble_score") is not None:
                    record["ensemble_score"] = score_row.get(
                        "ensemble_score"
                    )

            record["ticker"] = ticker
            out.append(record)
        return out
    finally:
        conn.close()


def _scoring_cache_symbol_from_payload(
    payload_raw: Any,
) -> Optional[str]:
    """Return the OCC symbol embedded in the scoring_cache payload, if any."""
    if not isinstance(payload_raw, str) or not payload_raw.strip():
        return None
    try:
        data = json.loads(payload_raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    legs = data.get("option_legs")
    if isinstance(legs, Sequence) and legs and isinstance(legs[0], Mapping):
        sym = legs[0].get("symbol")
        if isinstance(sym, str) and sym.strip():
            return sym.strip()
    sym = data.get("option_symbol") or data.get("symbol")
    if isinstance(sym, str) and sym.strip():
        return sym.strip()
    return None


def _decode_payload_dict(payload_raw: Any) -> Optional[dict[str, Any]]:
    """Best-effort decode a JSON ``payload`` blob into a dict; ``None`` on failure."""
    if isinstance(payload_raw, dict):
        return payload_raw
    if not isinstance(payload_raw, str) or not payload_raw.strip():
        return None
    try:
        data = json.loads(payload_raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _extract_option_metadata(
    payload_dict: Optional[Mapping[str, Any]],
) -> tuple[Optional[str], Optional[str], Optional[float], Optional[str]]:
    """Pull ``(symbol, option_type, strike, expiry)`` from a payload dict.

    Reads from both the legs-style shape (``option_legs[0]``) and the
    flat shape (``option_symbol`` / ``option_type`` / ``option_strike``
    / ``option_expiry``). Returns ``None`` for any field that is
    absent or unparseable.
    """
    if not isinstance(payload_dict, Mapping):
        return None, None, None, None

    symbol: Optional[str] = None
    opt_type: Optional[str] = None
    strike: Optional[float] = None
    expiry: Optional[str] = None

    legs = payload_dict.get("option_legs")
    if (
        isinstance(legs, Sequence)
        and not isinstance(legs, (str, bytes))
        and len(legs) > 0
        and isinstance(legs[0], Mapping)
    ):
        leg = legs[0]
        leg_sym = leg.get("symbol")
        if isinstance(leg_sym, str) and leg_sym.strip():
            symbol = leg_sym.strip()
        leg_type = leg.get("option_type") or leg.get("type")
        if isinstance(leg_type, str) and leg_type.strip():
            opt_type = leg_type.strip()
        leg_strike = leg.get("strike") or leg.get("option_strike")
        try:
            if leg_strike is not None:
                strike = float(leg_strike)
        except (TypeError, ValueError):
            strike = None
        leg_expiry = leg.get("expiry") or leg.get("option_expiry")
        if isinstance(leg_expiry, str) and leg_expiry.strip():
            expiry = leg_expiry.strip()

    if not symbol:
        flat_sym = payload_dict.get("option_symbol") or payload_dict.get(
            "symbol"
        )
        if isinstance(flat_sym, str) and flat_sym.strip():
            symbol = flat_sym.strip()
    if not opt_type:
        flat_type = payload_dict.get("option_type")
        if isinstance(flat_type, str) and flat_type.strip():
            opt_type = flat_type.strip()
    if strike is None:
        flat_strike = payload_dict.get("option_strike")
        try:
            if flat_strike is not None:
                strike = float(flat_strike)
        except (TypeError, ValueError):
            strike = None
    if not expiry:
        flat_expiry = payload_dict.get("option_expiry")
        if isinstance(flat_expiry, str) and flat_expiry.strip():
            expiry = flat_expiry.strip()

    return symbol, opt_type, strike, expiry


def load_today_candidates_from_db(
    *, today: Any = None, db_path: Optional[Path] = None
) -> list[dict[str, Any]]:
    """Return today's :data:`scoring_cache` candidates ordered by score.

    Each candidate dict carries:

    * ``id`` / ``scoring_cache_id`` — the ``scoring_cache.id`` so
      the default debate runner can call
      :func:`run_debate(scoring_cache_id, ...)` without a second
      lookup.
    * ``ticker``, ``ensemble_score``, ``science_grade``,
      ``claude_grade``, ``gemini_grade``, ``grok_score`` — the
      static-ensemble breakdown.
    * ``catalyst_date`` — sourced from the most-recent matching
      ``plays`` row for the ticker (the canonical catalyst date
      lives there because there is no separate
      ``catalyst_calendar`` table in the M2 schema). Falls back to
      ``payload.catalyst_date`` from the scoring_cache row when no
      ``plays`` row exists for the ticker yet.
    * ``option_legs`` + ``play_card`` — a buy-card shape
      consumable by :meth:`PaperExecutor.execute`. Each leg's
      ``symbol`` is constructed from the most-recent ``plays``
      row's option metadata (strike, expiry, type), with sane
      defaults so an empty challenger card still emits a single
      valid leg.

    The query selects every ``scoring_cache`` row with
    ``as_of_date == today`` ordered by ``ensemble_score DESC``.
    """
    today_d = (
        _hold_policy.coerce_date(today)
        if today is not None
        else datetime.date.today()
    )
    target = (
        Path(db_path) if db_path is not None else DATA_DIR / "alpha_sniper.db"
    )
    if not target.is_file():
        return []
    try:
        conn = _db.connect(target)
    except sqlite3.Error:
        return []
    try:
        try:
            rows = conn.execute(
                """
                SELECT id, ticker, as_of_date, ensemble_score,
                       science_grade, claude_grade, gemini_grade,
                       grok_score, payload
                  FROM scoring_cache
                 WHERE as_of_date = ?
                 ORDER BY ensemble_score DESC, id ASC
                """,
                (today_d.isoformat(),),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            ticker = str(record.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            payload_raw = record.get("payload")
            record["scoring_cache_id"] = record.get("id")

            play_row = _latest_play_for_ticker(conn, ticker)
            sc_payload = _decode_payload_dict(payload_raw)
            play_payload = (
                _decode_payload_dict(play_row.get("payload"))
                if play_row
                else None
            )

            # catalyst_date: best-effort from any available source so
            # the f-m3-21 preflight (and the 24h guard) is rarely
            # tripped. Priority order:
            #   1. plays.catalyst_date column (canonical)
            #   2. scoring_cache.payload.catalyst_date
            #   3. plays.payload.catalyst_date
            cat: Any = play_row.get("catalyst_date") if play_row else None
            if not cat and isinstance(sc_payload, Mapping):
                cat = sc_payload.get("catalyst_date")
            if not cat and isinstance(play_payload, Mapping):
                cat = play_payload.get("catalyst_date")
            if cat:
                record["catalyst_date"] = cat

            # Build option_legs: synthesize an OCC symbol from any
            # source so the preflight is rarely hit. Priority order
            # for each component (symbol, type, strike, expiry):
            #   1. scoring_cache.payload (legs-style or flat keys)
            #   2. plays row's columns (option_type/option_strike/
            #      option_expiry — canonical)
            #   3. plays.payload (legs-style or flat keys)
            sc_sym, sc_type, sc_strike, sc_expiry = _extract_option_metadata(
                sc_payload
            )
            play_payload_sym, play_payload_type, play_payload_strike, \
                play_payload_expiry = _extract_option_metadata(play_payload)

            # Coerce plays row's column-level strike to float for
            # consistent OCC construction (the column is REAL but
            # SQLite may return Decimal/None).
            row_strike: Optional[float]
            try:
                raw_row_strike = (
                    play_row.get("option_strike") if play_row else None
                )
                row_strike = (
                    float(raw_row_strike)
                    if raw_row_strike is not None
                    else None
                )
            except (TypeError, ValueError):
                row_strike = None

            row_type = play_row.get("option_type") if play_row else None
            row_expiry = play_row.get("option_expiry") if play_row else None

            symbol = sc_sym or play_payload_sym
            opt_type = (
                sc_type
                or row_type
                or play_payload_type
                or "call"
            )
            strike = (
                sc_strike
                if sc_strike is not None
                else (row_strike if row_strike is not None else play_payload_strike)
            )
            expiry = sc_expiry or row_expiry or play_payload_expiry

            if not symbol:
                symbol = _build_occ_symbol(
                    ticker, expiry, opt_type, strike
                )

            leg: dict[str, Any] = {
                "ticker": ticker,
                "side": "buy",
                "qty": 1,
            }
            if symbol:
                leg["symbol"] = symbol
            if opt_type:
                leg["option_type"] = opt_type
            if strike is not None:
                leg["strike"] = strike
            if expiry:
                leg["expiry"] = expiry

            play_card = {
                "play_card_id": (
                    f"{ticker}-rotation-{today_d.isoformat()}"
                ),
                "ticker": ticker,
                "option_legs": [leg],
            }
            record["option_legs"] = [leg]
            record["play_card"] = play_card
            record["ticker"] = ticker
            out.append(record)
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def evaluate_rotation(
    *,
    today: Any = None,
    active_plays: Optional[Iterable[Mapping[str, Any]]] = None,
    candidates: Optional[Iterable[Mapping[str, Any]]] = None,
    executor: Any = None,
    debate_runner: Optional[DebateRunner] = None,
    max_concurrent: Optional[int] = None,
    rotation_threshold: Optional[float] = None,
    db_path: Optional[Path] = None,
    audit_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Evaluate and (optionally) execute rotations for today's run.

    Parameters
    ----------
    today:
        Override for "today". Coerced via :func:`hold_policy.coerce_date`.
        Defaults to UTC today.
    active_plays:
        Iterable of mappings describing the current active option
        plays. Each MUST carry ``ticker`` and ``ensemble_score`` (the
        score re-computed today). Optional but recommended:
        ``play_card_id``, ``symbol`` (OCC option symbol),
        ``catalyst_date``, ``qty`` (open contracts),
        ``scoring_cache_id``. When ``None`` the function loads the
        list from :func:`load_active_plays_from_db`.
    candidates:
        Iterable of mappings describing today's ``scoring_cache``
        rows ordered by ``ensemble_score`` descending. Each MUST
        carry ``ticker`` and ``ensemble_score``. Recommended:
        ``catalyst_date``, ``play_card`` (a dict consumable by
        :meth:`PaperExecutor.execute` to open the buy leg),
        ``scoring_cache_id``. When ``None`` the function loads the
        list from :func:`load_today_candidates_from_db`.
    executor:
        :class:`PaperExecutor` instance used to submit the
        sell-to-close + buy pair. When ``None`` the engine runs in
        DRY-RUN mode: decisions are computed and persisted to the
        audit JSON but no orders are submitted (useful for the
        intraday scanner's first wiring before paper credentials are
        provisioned).
    debate_runner:
        Callable that fires the rotation debate and returns the
        challenger / incumbent grades. When ``None`` a default that
        wraps :func:`biotech_sniper.llm.llm_debate.run_debate` is
        constructed lazily — but the default raises if either
        scoring_cache_id is missing from its inputs, so production
        callers should always supply their own runner that can fall
        back to the static science grade.
    max_concurrent:
        Override for the concurrency cap. Defaults to
        :data:`biotech_sniper.config.MAX_CONCURRENT_PLAYS`.
    rotation_threshold:
        Override for :data:`ROTATION_THRESHOLD`. Tests use this to
        exercise the boundary; production never overrides.
    db_path:
        Optional override for the SQLite db; only consulted when
        ``active_plays`` / ``candidates`` are ``None``.
    audit_path:
        Optional override for ``state/audit_latest.json``.

    Returns
    -------
    dict
        Summary dict with keys:

        * ``today`` — ISO date string.
        * ``decisions`` — list of dicts, one per executed rotation
          (challenger, incumbent, sell_order_id, buy_order_id, debate).
        * ``skips`` — list of dicts, one per non-fired evaluation
          (reason ∈ :data:`VALID_SKIP_REASONS`, plus context).
        * ``active_count`` — final active-play count after the run.
        * ``capacity`` — the ``max_concurrent`` cap that was used.
    """
    today_d = _hold_policy.coerce_date(today) if today is not None else datetime.date.today()
    cap = (
        int(max_concurrent)
        if max_concurrent is not None
        else int(_config.MAX_CONCURRENT_PLAYS)
    )
    threshold = (
        float(rotation_threshold)
        if rotation_threshold is not None
        else ROTATION_THRESHOLD
    )

    audit = (
        Path(audit_path)
        if audit_path is not None
        else BASE_DIR / "state" / "audit_latest.json"
    )

    if active_plays is None:
        active_list: list[dict[str, Any]] = load_active_plays_from_db(
            db_path=db_path
        )
    else:
        active_list = [dict(p) for p in active_plays]

    if candidates is None:
        candidates_list: list[dict[str, Any]] = load_today_candidates_from_db(
            today=today_d, db_path=db_path
        )
    else:
        candidates_list = [dict(c) for c in candidates]

    decisions: list[dict[str, Any]] = []
    skips: list[dict[str, Any]] = []

    # Rotation only kicks in when slots are full. Below-capacity
    # active sets simply admit the next selected candidate via the
    # daily play-card path; the engine has nothing to swap.
    if len(active_list) < cap:
        logger.info(
            "rotation_engine.below_capacity active=%d cap=%d",
            len(active_list),
            cap,
        )
        return {
            "today": today_d.isoformat(),
            "decisions": [],
            "skips": [],
            "active_count": len(active_list),
            "capacity": cap,
        }

    active_tickers = {_ticker(p) for p in active_list}

    runner = debate_runner

    for cand in candidates_list:
        ticker = _ticker(cand)
        if not ticker or ticker in active_tickers:
            continue

        # Re-pick the weakest each iteration — earlier rotations may
        # have updated the active set in-place (we removed the old
        # incumbent and appended the new entry).
        weakest = min(
            active_list,
            key=lambda p: float(p.get("ensemble_score") or 0.0),
        )
        challenger_score = float(cand.get("ensemble_score") or 0.0)
        weakest_score = float(weakest.get("ensemble_score") or 0.0)
        delta = challenger_score - weakest_score

        skip_context = {
            "challenger": ticker,
            "incumbent": _ticker(weakest) or weakest.get("ticker"),
            "challenger_score": challenger_score,
            "incumbent_score": weakest_score,
            "delta": delta,
            "today": today_d.isoformat(),
        }

        if delta < threshold:
            skip = _record_skip(
                audit,
                reason="below_threshold",
                context=skip_context,
            )
            skips.append(skip)
            continue

        if is_within_24h(
            cand.get("catalyst_date"), today_d
        ) or is_within_24h(weakest.get("catalyst_date"), today_d):
            skip = _record_skip(
                audit,
                reason="catalyst_too_close",
                context={
                    **skip_context,
                    "challenger_catalyst_date": _iso_or_none(
                        cand.get("catalyst_date")
                    ),
                    "incumbent_catalyst_date": _iso_or_none(
                        weakest.get("catalyst_date")
                    ),
                },
            )
            skips.append(skip)
            continue

        # Preflight: refuse to submit the SELL leg unless the
        # challenger carries a fully-shaped buy card (catalyst_date,
        # play_card, option_legs[0].symbol). Without this gate a
        # missing-symbol challenger could result in the incumbent
        # being closed AND the buy raising — leaving a one-sided
        # rotation. See f-m3-21.
        if not _validate_challenger_card_complete(cand):
            play_card = cand.get("play_card")
            legs = cand.get("option_legs")
            if not isinstance(legs, Sequence) or isinstance(
                legs, (str, bytes)
            ):
                if isinstance(play_card, Mapping):
                    legs = play_card.get("option_legs")
            first_symbol = None
            if (
                isinstance(legs, Sequence)
                and not isinstance(legs, (str, bytes))
                and len(legs) > 0
                and isinstance(legs[0], Mapping)
            ):
                first_symbol = legs[0].get("symbol")
            skip = _record_skip(
                audit,
                reason="incomplete_challenger_card",
                context={
                    **skip_context,
                    "challenger_catalyst_date": _iso_or_none(
                        cand.get("catalyst_date")
                    ),
                    "has_play_card": isinstance(play_card, Mapping),
                    "option_legs_count": (
                        len(legs)
                        if isinstance(legs, Sequence)
                        and not isinstance(legs, (str, bytes))
                        else 0
                    ),
                    "first_leg_symbol": first_symbol,
                },
            )
            skips.append(skip)
            continue

        # Fire the debate.
        if runner is None:
            runner = _build_default_debate_runner(audit_path=audit)
        try:
            debate_result = runner(
                challenger=cand, incumbent=weakest, today=today_d
            )
        except Exception as exc:  # noqa: BLE001 — graceful degradation
            logger.warning(
                "rotation_engine.debate_failed challenger=%s incumbent=%s "
                "reason=%r",
                ticker,
                _ticker(weakest),
                exc,
            )
            skip = _record_skip(
                audit,
                reason="debate_inverted_preference",
                context={
                    **skip_context,
                    "debate_error": type(exc).__name__,
                },
            )
            skips.append(skip)
            continue

        debate_payload = dict(debate_result) if debate_result else {}
        chal_grade = debate_payload.get("challenger_grade")
        inc_grade = debate_payload.get("incumbent_grade")

        if not challenger_outranks(chal_grade, inc_grade):
            skip = _record_skip(
                audit,
                reason="debate_inverted_preference",
                context={
                    **skip_context,
                    "challenger_grade": chal_grade,
                    "incumbent_grade": inc_grade,
                    "debate": debate_payload,
                },
            )
            skips.append(skip)
            continue

        # Execute the swap. ``submit_exit`` honours the hold-policy
        # gate (event='rotation' is in ALLOWED_EXIT_EVENTS) and the
        # broker dedupe namespace; the buy reuses the standard
        # :meth:`PaperExecutor.execute` entry path so concurrency +
        # deployed-capital caps still apply.
        if executor is None:
            logger.info(
                "rotation_engine.dry_run challenger=%s incumbent=%s "
                "delta=%.4f",
                ticker,
                _ticker(weakest),
                delta,
            )
            sell_order_id: Optional[str] = None
            buy_order_id: Optional[str] = None
        else:
            sell_order_id = _submit_rotation_sell(
                executor, weakest, today_d
            )
            buy_order_id = _submit_rotation_buy(
                executor, cand, today_d, ticker
            )

        decision = {
            "challenger": ticker,
            "incumbent": _ticker(weakest),
            "challenger_score": challenger_score,
            "incumbent_score": weakest_score,
            "delta": delta,
            "challenger_grade": chal_grade,
            "incumbent_grade": inc_grade,
            "sell_order_id": sell_order_id,
            "buy_order_id": buy_order_id,
            "debate": debate_payload,
            "today": today_d.isoformat(),
        }
        decisions.append(decision)

        logger.info(
            "rotation_engine.rotation_executed challenger=%s incumbent=%s "
            "delta=%.4f sell=%s buy=%s",
            ticker,
            _ticker(weakest),
            delta,
            sell_order_id,
            buy_order_id,
        )

        # Update active set: drop the incumbent, add the challenger
        # so the next iteration's "weakest" picks correctly account
        # for the rotation that just fired.
        active_list = [p for p in active_list if p is not weakest]
        new_play = {
            "ticker": ticker,
            "ensemble_score": challenger_score,
            "play_card_id": cand.get("play_card_id"),
            "symbol": _extract_buy_symbol(cand),
            "qty": _extract_buy_qty(cand),
            "catalyst_date": cand.get("catalyst_date"),
        }
        active_list.append(new_play)
        active_tickers = {_ticker(p) for p in active_list}

    return {
        "today": today_d.isoformat(),
        "decisions": decisions,
        "skips": skips,
        "active_count": len(active_list),
        "capacity": cap,
    }


# ---------------------------------------------------------------------------
# Internal: skip recording + executor dispatch
# ---------------------------------------------------------------------------


def _record_skip(
    audit_path: Path,
    *,
    reason: str,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist a ``rotation_skipped`` entry into ``audit_latest.json``.

    The audit JSON's ``rotation_skipped`` key always carries the
    LATEST skip from the run (older skips are still surfaced via the
    return value of :func:`evaluate_rotation` and via the structured
    log line emitted here). Validators query the JSON for the
    ``reason`` field, which is mandatory in every block.
    """
    if reason not in VALID_SKIP_REASONS:
        raise ValueError(
            f"rotation_engine: unknown skip reason {reason!r}; "
            f"allowed = {sorted(VALID_SKIP_REASONS)}"
        )
    block = {
        "reason": reason,
        "recorded_at": (
            datetime.datetime.now(datetime.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        **dict(context),
    }
    try:
        _merge_audit_block(audit_path, "rotation_skipped", block)
    except Exception as exc:  # pragma: no cover - audit failure is best-effort
        logger.warning(
            "rotation_engine.audit_write_failed reason=%s err=%r",
            reason,
            exc,
        )
    logger.info(
        "rotation_engine.rotation_skipped reason=%s challenger=%s "
        "incumbent=%s",
        reason,
        context.get("challenger"),
        context.get("incumbent"),
    )
    return block


def _submit_rotation_sell(
    executor: Any,
    incumbent: Mapping[str, Any],
    today: datetime.date,
) -> Optional[str]:
    """Submit ``submit_exit(event='rotation')`` for the incumbent."""
    qty = _coerce_qty(incumbent)
    if qty < 1:
        logger.warning(
            "rotation_engine.skip_sell ticker=%s reason=no_open_qty",
            _ticker(incumbent),
        )
        return None
    return executor.submit_exit(
        incumbent,
        ROTATION_EVENT,
        today=today,
        sell_qty=qty,
    )


def _submit_rotation_buy(
    executor: Any,
    challenger: Mapping[str, Any],
    today: datetime.date,
    ticker: str,
) -> Optional[str]:
    """Submit a buy entry tagged ``event='open'`` for the challenger.

    The challenger Mapping MUST expose either:

    * ``play_card`` — a dict consumable by
      :meth:`PaperExecutor.execute` (with ``option_legs`` etc.), OR
    * the play-card fields directly on the candidate
      (``option_legs`` / ``play_card_id``).

    ``event`` is forced to :data:`ENTRY_EVENT` so the persisted
    ``orders`` row is correctly tagged as a rotation-driven entry.
    """
    play_card = challenger.get("play_card")
    if not isinstance(play_card, Mapping):
        play_card = challenger
    card = dict(play_card)
    card["event"] = ENTRY_EVENT
    if "ticker" not in card:
        card["ticker"] = ticker
    result = executor.execute(card)
    if isinstance(result, list):
        return result[0] if result else None
    return result


def _extract_buy_symbol(challenger: Mapping[str, Any]) -> Optional[str]:
    """Return the OCC option symbol from the challenger's play card."""
    play_card = challenger.get("play_card") or challenger
    legs = play_card.get("option_legs") if isinstance(play_card, Mapping) else None
    if isinstance(legs, Sequence) and legs and isinstance(legs[0], Mapping):
        sym = legs[0].get("symbol")
        if isinstance(sym, str) and sym.strip():
            return sym.strip()
    return None


def _extract_buy_qty(challenger: Mapping[str, Any]) -> int:
    """Best-effort extract the open contract count from the challenger."""
    play_card = challenger.get("play_card") or challenger
    legs = play_card.get("option_legs") if isinstance(play_card, Mapping) else None
    if isinstance(legs, Sequence) and legs and isinstance(legs[0], Mapping):
        qty_raw = legs[0].get("qty")
        try:
            return int(qty_raw) if qty_raw is not None else 0
        except (TypeError, ValueError):
            return 0
    return 0


def _iso_or_none(value: Any) -> Optional[str]:
    d = _coerce_date_or_none(value)
    return d.isoformat() if d is not None else None


# ---------------------------------------------------------------------------
# Default debate runner — wraps :func:`llm_debate.run_debate`
# ---------------------------------------------------------------------------


def _build_default_debate_runner(
    *, audit_path: Optional[Path] = None
) -> DebateRunner:
    """Return a debate runner that wraps :func:`llm_debate.run_debate`.

    The runner fires :func:`run_debate` once per side (challenger
    and incumbent) so each scoring_cache row has its own debate row
    with ``trigger='rotation'``. Production callers that want a
    cheaper alternative can inject a custom runner instead.

    Both ``challenger`` and ``incumbent`` MUST carry a
    ``scoring_cache_id`` for the wrapped :func:`run_debate` to find
    the cache row. The runner raises :class:`RuntimeError` when an
    id is missing so the caller's ``try/except`` in
    :func:`evaluate_rotation` records a skip rather than silently
    rotating without a debate.
    """

    def _runner(
        *,
        challenger: Mapping[str, Any],
        incumbent: Mapping[str, Any],
        today: Optional[datetime.date] = None,
    ) -> Mapping[str, Any]:
        from biotech_sniper.llm.llm_debate import run_debate

        chal_id = challenger.get("scoring_cache_id") or challenger.get("id")
        inc_id = incumbent.get("scoring_cache_id") or incumbent.get("id")
        if chal_id is None or inc_id is None:
            raise RuntimeError(
                "rotation_engine: default debate runner requires "
                "scoring_cache_id on both challenger and incumbent; "
                f"got challenger.id={chal_id!r} incumbent.id={inc_id!r}"
            )

        chal_debate = run_debate(
            int(chal_id),
            "rotation",
            audit_path=audit_path,
            today=today,
        )
        inc_debate = run_debate(
            int(inc_id),
            "rotation",
            audit_path=audit_path,
            today=today,
        )
        return {
            "challenger_grade": chal_debate.get("final_grade"),
            "incumbent_grade": inc_debate.get("final_grade"),
            "challenger_debate": chal_debate,
            "incumbent_debate": inc_debate,
            "short_circuited": bool(chal_debate.get("short_circuited"))
            or bool(inc_debate.get("short_circuited")),
        }

    return _runner
