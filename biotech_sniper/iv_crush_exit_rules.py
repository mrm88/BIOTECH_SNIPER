#!/usr/bin/env python3
"""
IV CRUSH EXIT RULES
The #1 lesson from TVTX, IDYA: even when direction is RIGHT, IV collapse post-announcement
destroys option value faster than the stock move creates intrinsic value.

THE RULE (hardcoded, mandatory):
  On catalyst announcement day (PDUFA date, topline, etc.):
  - Sell 50% of the position at market OPEN (9:30-9:45 AM ET) regardless of direction
  - This locks in IV premium while it still exists
  - Hold remaining 50% for the directional move to play out

WHY THIS WORKS:
  TVTX example: Stock opened +8% pre-market on approval. IV was 305% at entry.
  If we sold 50% at open, we would have captured remaining time value before IV collapsed.
  Instead holding 100% through 9:45 AM = IV crushed, option dead.

  IDYA example: PFS positive pre-market +12%. IV 188% at entry.
  Selling 50% at open = capture IV premium. Hold 50% for NDA thesis.
  Instead: IV collapsed from 188% to ~40%, option -91%.

IMPLEMENTATION:
  1. Daily cron checks: is today a catalyst day for any active play?
  2. If yes AND IV > 100%: generate sell order alert at 9:30 AM ET open
  3. Alert format: pre-market email with specific exit instructions
  4. Also flags plays approaching expiry (DTE < 10) for roll decisions

INTRADAY RULE:
  If a play gaps >15% pre-market AND IV was >100% at entry:
  - Immediate alert to sell 50% at open
  - Subject: "SNIPER EXIT ALERT — [TICKER] — Sell 50% at open"
"""

from __future__ import annotations

import json
import datetime
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from biotech_sniper import db as _db
from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.paper_executor import (
    PaperExecutor,
    PaperOnlyViolation,
)
from biotech_sniper.paths import BASE_DIR, DATA_DIR

ACTIVE_PLAYS_FILE = BASE_DIR / "state/active_plays.json"
LEDGER_FILE = BASE_DIR / "state/performance_ledger.json"

logger = logging.getLogger(__name__)

# IV threshold above which we apply the 50% exit rule
IV_CRUSH_THRESHOLD = 100  # IV > 100% = rule applies

# Days to expiry below which we send roll warning
DTE_ROLL_WARNING = 10

# Minimum stock gap to trigger immediate exit alert
GAP_TRIGGER_PCT = 15


def load_active_plays() -> dict:
    """Read active plays from the legacy ``state/active_plays.json`` file.

    Kept for backwards compatibility (the legacy email-alert helpers
    in this module — :func:`check_catalyst_days`,
    :func:`check_dte_warnings`, :func:`check_gap_exit_alerts` —
    still consume the JSON layout). Tests inject this loader
    explicitly via the ``active_plays`` argument when they want to
    exercise the JSON code path. Production wiring goes through
    :func:`load_active_plays_from_db` instead — see f-m3-15.
    """
    if ACTIVE_PLAYS_FILE.exists():
        with open(ACTIVE_PLAYS_FILE) as f:
            return json.load(f).get("active", {})
    return {}


def load_active_plays_from_db(
    *, db_path: Optional[Path] = None
) -> list[dict[str, Any]]:
    """Return active option plays from the SQLite ``plays`` table.

    Implements VAL-M3-026's "DB is the source of truth" requirement:
    the IV-crush autotrigger reads from
    ``data/alpha_sniper.db`` rather than the legacy
    ``state/active_plays.json``. The query is::

        SELECT * FROM plays
         WHERE status='active' AND catalyst_date IS NOT NULL

    Each returned dict merges the row's columns with any fields
    embedded in the original JSON ``payload`` so legacy keys
    (``option_symbol``, ``contracts``, ``play_card_id``) flow
    through unchanged for the ``IVCrushExitRunner`` to consume.
    Missing ``play_card_id`` falls back to the row's
    ``source_key`` (e.g. ``"active:IDYA"``) so the idempotency
    short-circuit (VAL-M3-029) has a stable parent link.

    Returns an empty list when the database file does not exist or
    the ``plays`` table has not been migrated yet so the autotrigger
    is a no-op on greenfield environments — matching the JSON-loader
    behaviour of returning ``{}`` when ``state/active_plays.json``
    is absent.

    Parameters
    ----------
    db_path:
        Override path for the SQLite db. Defaults to
        ``DATA_DIR / 'alpha_sniper.db'``. Tests inject a
        ``tmp_path`` here.
    """
    target = (
        Path(db_path)
        if db_path is not None
        else DATA_DIR / "alpha_sniper.db"
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
                       direction, catalyst_type, option_type,
                       option_strike, option_expiry, payload
                  FROM plays
                 WHERE status = 'active'
                   AND catalyst_date IS NOT NULL
                """
            ).fetchall()
        except sqlite3.OperationalError:
            # Schema not migrated yet (no ``plays`` table) — degrade
            # to a no-op so a fresh-clone smoke run does not crash.
            return []
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for row in rows:
        record: dict[str, Any] = dict(row)
        payload_raw = record.pop("payload", None)
        merged: dict[str, Any] = {}
        if isinstance(payload_raw, str) and payload_raw.strip():
            try:
                payload_dict = json.loads(payload_raw)
            except (TypeError, ValueError):
                payload_dict = None
            if isinstance(payload_dict, dict):
                merged.update(payload_dict)
        # Row columns override payload values for the canonical fields.
        for key, value in record.items():
            if value is not None:
                merged[key] = value
        if not merged.get("play_card_id"):
            merged["play_card_id"] = (
                record.get("source_key") or merged.get("ticker")
            )
        out.append(merged)
    return out


def load_ledger() -> dict:
    if LEDGER_FILE.exists():
        with open(LEDGER_FILE) as f:
            return json.load(f)
    return {"plays": {}}


def check_catalyst_days() -> list:
    """
    Returns list of plays where today is (or is within 1 day of) the PDUFA date.
    These need pre-market exit alerts.
    """
    today = datetime.date.today()
    active = load_active_plays()
    alerts = []

    for ticker, play in active.items():
        pdufa = play.get("pdufa_date")
        if not pdufa:
            continue
        try:
            pdufa_date = datetime.date.fromisoformat(pdufa)
            days_to_pdufa = (pdufa_date - today).days
            if 0 <= days_to_pdufa <= 1:
                iv = play.get("iv_pct")
                alerts.append({
                    "ticker": ticker,
                    "pdufa_date": pdufa,
                    "days_to_pdufa": days_to_pdufa,
                    "iv_pct": iv,
                    "apply_exit_rule": iv and iv > IV_CRUSH_THRESHOLD,
                    "direction": play.get("direction"),
                    "strike": play.get("option_strike"),
                    "expiry": play.get("option_expiry"),
                    "reason": "PDUFA today" if days_to_pdufa == 0 else "PDUFA tomorrow",
                })
        except:
            pass

    return alerts


def check_dte_warnings() -> list:
    """Returns plays with DTE < DTE_ROLL_WARNING that need roll decisions."""
    today = datetime.date.today()
    active = load_active_plays()
    warnings = []

    for ticker, play in active.items():
        expiry = play.get("option_expiry")
        if not expiry:
            continue
        try:
            exp_date = datetime.date.fromisoformat(expiry)
            dte = (exp_date - today).days
            if 0 < dte < DTE_ROLL_WARNING:
                pdufa = play.get("pdufa_date")
                catalyst_date = pdufa or play.get("estimated_announcement", "")
                warnings.append({
                    "ticker": ticker,
                    "dte": dte,
                    "expiry": expiry,
                    "catalyst": catalyst_date,
                    "direction": play.get("direction"),
                    "strike": play.get("option_strike"),
                    "lottery_tier": play.get("lottery_tier", False),
                })
        except:
            pass

    return warnings


def check_gap_exit_alerts(current_prices: dict) -> list:
    """
    Given a dict of {ticker: current_price}, check for pre-market gaps
    that should trigger immediate 50% exit alerts.
    current_prices: fetched from the broker pre-market snapshot
    """
    ledger = load_ledger()
    active = load_active_plays()
    alerts = []

    for ticker, current_price in current_prices.items():
        if ticker not in active:
            continue
        play = active[ticker]
        ledger_entry = ledger["plays"].get(ticker, {})
        entry_stock = ledger_entry.get("entry_stock", 0) or 0
        iv_at_entry = ledger_entry.get("entry_iv_pct", 0) or 0
        direction = play.get("direction", "")

        if not entry_stock or not current_price:
            continue

        gap_pct = (current_price - entry_stock) / entry_stock * 100

        # Large gap in the right direction + high IV at entry
        if abs(gap_pct) >= GAP_TRIGGER_PCT and iv_at_entry > IV_CRUSH_THRESHOLD:
            correct_direction = (
                (direction == "LONG_CALLS" and gap_pct > 0) or
                (direction == "LONG_PUTS" and gap_pct < 0)
            )
            alerts.append({
                "ticker": ticker,
                "gap_pct": gap_pct,
                "current_price": current_price,
                "entry_stock": entry_stock,
                "iv_at_entry": iv_at_entry,
                "direction": direction,
                "correct_direction": correct_direction,
                "action": "SELL 50% AT OPEN (9:30-9:45 AM ET)",
                "reason": f"Gap {'matches' if correct_direction else 'WRONG direction but'} + IV={iv_at_entry:.0f}% at entry = IV crush risk",
            })

    return alerts


def format_exit_alert_email(catalyst_alerts: list, dte_warnings: list,
                              gap_alerts: list = None) -> str:
    """
    Format the pre-market exit alert email body.
    Sent when PDUFA is today/tomorrow OR large pre-market gap detected.
    """
    lines = []
    today = datetime.date.today().strftime("%A, %B %d, %Y")

    lines.append(f"ALPHA SNIPER — EXIT ALERT | {today}")
    lines.append("=" * 60)
    lines.append("")

    # Gap alerts (highest urgency)
    if gap_alerts:
        lines.append("*** IMMEDIATE ACTION REQUIRED — PRE-MARKET GAP DETECTED ***")
        lines.append("")
        for a in gap_alerts:
            gap_str = f"+{a['gap_pct']:.1f}%" if a['gap_pct'] > 0 else f"{a['gap_pct']:.1f}%"
            dir_tag = "CORRECT DIRECTION" if a["correct_direction"] else "WRONG DIRECTION"
            lines.append(f"  {a['ticker']}: Stock {gap_str} pre-market | {dir_tag}")
            lines.append(f"  IV at entry: {a['iv_at_entry']:.0f}% — HIGH CRUSH RISK")
            lines.append(f"  ACTION: {a['action']}")
            lines.append(f"  Why: {a['reason']}")
            lines.append("")

    # PDUFA day alerts
    if catalyst_alerts:
        lines.append("PDUFA / CATALYST DAY — IV CRUSH EXIT PROTOCOL:")
        lines.append("")
        for a in catalyst_alerts:
            timing = "TODAY" if a["days_to_pdufa"] == 0 else "TOMORROW"
            iv_str = f"{a['iv_pct']:.0f}%" if a["iv_pct"] else "unknown"
            rule_str = " -- IV CRUSH RULE APPLIES" if a["apply_exit_rule"] else ""
            lines.append(f"  {a['ticker']}: PDUFA {timing} ({a['pdufa_date']}){rule_str}")
            lines.append(f"  Strike: ${a['strike']} | Expiry: {a['expiry']} | IV: {iv_str}")
            if a["apply_exit_rule"]:
                lines.append(f"  ACTION: Sell 50% at OPEN (9:30-9:45 AM ET) to lock in IV premium")
                lines.append(f"  Hold 50% for the directional move to play out")
                lines.append(f"  Why: At IV>{IV_CRUSH_THRESHOLD}%, IV collapse post-announcement often exceeds stock move gain")
            lines.append("")

    # DTE warnings
    if dte_warnings:
        lines.append("DTE WARNING — OPTIONS EXPIRING SOON:")
        lines.append("")
        for w in dte_warnings:
            lottery_tag = " [LOTTERY — let expire or sell now]" if w["lottery_tier"] else ""
            lines.append(f"  {w['ticker']}: {w['dte']} days to expiry ({w['expiry']}){lottery_tag}")
            lines.append(f"  Catalyst: {str(w['catalyst'])[:40]} | Strike: ${w['strike']}")
            if not w["lottery_tier"]:
                lines.append(f"  DECISION: Roll to next expiry, take profit, or let expire?")
            lines.append("")

    lines.append("─" * 60)
    lines.append("These alerts are generated automatically from the IV monitoring system.")
    lines.append("Always execute exits before 9:45 AM ET to capture remaining time value.")

    return "\n".join(lines)


def run_daily_exit_check() -> dict:
    """
    Run all exit checks. Returns results for inclusion in daily email.
    Called as part of the daily cron pipeline.
    """
    catalyst_alerts = check_catalyst_days()
    dte_warnings = check_dte_warnings()

    result = {
        "catalyst_alerts": catalyst_alerts,
        "dte_warnings": dte_warnings,
        "needs_alert_email": bool(catalyst_alerts or dte_warnings),
        "alert_email_body": None,
    }

    if catalyst_alerts or dte_warnings:
        result["alert_email_body"] = format_exit_alert_email(
            catalyst_alerts, dte_warnings
        )

    if catalyst_alerts:
        print(f"  [iv_exit] PDUFA day alerts: {[a['ticker'] for a in catalyst_alerts]}")
    if dte_warnings:
        print(f"  [iv_exit] DTE warnings: {[w['ticker'] for w in dte_warnings]}")

    return result


def format_exit_section_for_email(exit_result: dict) -> str:
    """Compact section for the main daily email."""
    alerts = exit_result.get("catalyst_alerts", [])
    warnings = exit_result.get("dte_warnings", [])

    if not alerts and not warnings:
        return ""

    lines = ["", "─" * 65, "IV CRUSH EXIT RULES", "─" * 65]

    for a in alerts:
        timing = "TODAY" if a["days_to_pdufa"] == 0 else "tomorrow"
        rule = " -- SELL 50% AT OPEN" if a["apply_exit_rule"] else ""
        lines.append(f"  {a['ticker']}: PDUFA {timing}{rule}")

    for w in warnings:
        lottery = " [lottery]" if w["lottery_tier"] else ""
        lines.append(f"  {w['ticker']}: {w['dte']}d to expiry — roll or exit?{lottery}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# f-m3-05: IV-crush exit autotrigger.
#
# The legacy email-alert helpers above (``run_daily_exit_check``,
# ``check_catalyst_days``) describe the IV-crush exit policy; the
# autotrigger below WIRES that policy directly into the M3 paper
# executor so the 50% sell on a position's catalyst date no longer
# requires a human in the loop.
#
# Validation contract assertions fulfilled
# ----------------------------------------
# * **VAL-M3-026** — :meth:`IVCrushExitRunner.run_on_open` selects only
#   positions whose ``catalyst_date`` equals today's date; non-catalyst
#   dates iterate to zero exit submissions.
# * **VAL-M3-027** — sells ``floor(N/2)`` contracts. ``N=1`` skips with
#   structured log line ``iv_crush_exit.skipped reason=minimum_size``.
# * **VAL-M3-028** — sell order id logged at INFO with the structured
#   fields ``{event, order_id, qty, parent_play_card_id}`` and the row
#   is persisted to the ``orders`` table with ``event='iv_crush_exit'``
#   and ``parent_play_card_id`` linking back to the entry play card.
# * **VAL-M3-029** — re-invocation on the same trading day is
#   idempotent: the second call observes the persisted exit row and
#   logs ``iv_crush_exit.skipped reason=already_iv_crush_exited``,
#   submitting no order.
# * **VAL-M3-030** — ``IVCrushExitRunner.run_on_open`` re-checks the
#   wrapped executor's ``client.base_url`` against the paper sandbox
#   and raises :class:`PaperOnlyViolation` BEFORE any submission when
#   it drifts to the live URL — defence-in-depth around the
#   ``PaperExecutor`` paper-only guardrail.
# ---------------------------------------------------------------------------


#: Event tag persisted on the ``orders`` row + emitted in the structured
#: log line. The feature description spells the event ``iv_crush_exit``
#: (distinct from the f-m3-09 ``event`` enum value ``iv_crush`` which
#: lands in a later schema migration); this autotrigger uses the
#: feature-spec spelling so the persisted row matches VAL-M3-028's
#: ``SELECT id FROM orders WHERE event='iv_crush_exit'`` query.
IV_CRUSH_EXIT_EVENT: str = "iv_crush_exit"


def _coerce_today(today: Any) -> datetime.date:
    """Normalise ``today`` to a :class:`datetime.date`.

    Accepts ``None`` (defaults to UTC today), a :class:`datetime.date`,
    a :class:`datetime.datetime` (strips the time component), or an
    ISO-8601 ``YYYY-MM-DD`` string. Anything else raises
    :class:`ValueError` so callers cannot accidentally smuggle a
    timezone-naive datetime through and silently mismatch the
    catalyst-date comparison.
    """
    if today is None:
        return datetime.date.today()
    if isinstance(today, datetime.datetime):
        return today.date()
    if isinstance(today, datetime.date):
        return today
    if isinstance(today, str):
        return datetime.date.fromisoformat(today)
    raise ValueError(
        f"`today` must be None, date, datetime, or ISO-8601 string; "
        f"got {type(today).__name__}"
    )


def _coerce_active_plays(
    active_plays: Optional[Any],
) -> list[Mapping[str, Any]]:
    """Normalise the ``active_plays`` argument to a list of position dicts.

    Accepts:
    * ``None`` — falls back to :func:`load_active_plays_from_db`
      (queries the SQLite ``plays`` table — VAL-M3-026's "DB is
      the source of truth"). The legacy JSON loader
      :func:`load_active_plays` is no longer the default; tests
      that want the JSON code path must inject it explicitly via
      this argument.
    * a mapping ``{ticker: play_dict}`` — values become the position
      list; the ticker key is injected into the dict if not already
      present (the legacy active_plays.json layout).
    * a list/iterable of position dicts — passed through verbatim.
    """
    if active_plays is None:
        active_plays = load_active_plays_from_db()
    if isinstance(active_plays, Mapping):
        normalised: list[Mapping[str, Any]] = []
        for ticker, play in active_plays.items():
            if not isinstance(play, Mapping):
                continue
            if "ticker" not in play:
                play = {**play, "ticker": ticker}
            normalised.append(play)
        return normalised
    if isinstance(active_plays, Sequence) and not isinstance(
        active_plays, (str, bytes)
    ):
        return [p for p in active_plays if isinstance(p, Mapping)]
    if isinstance(active_plays, Iterable):
        return [p for p in active_plays if isinstance(p, Mapping)]
    raise TypeError(
        "active_plays must be a Mapping, an iterable of dicts, or None; "
        f"got {type(active_plays).__name__}"
    )


def _position_catalyst_date(play: Mapping[str, Any]) -> Optional[datetime.date]:
    """Return the catalyst date for ``play`` or ``None`` if unparseable.

    Honours both the new ``catalyst_date`` key (M3 schema) and the
    legacy ``pdufa_date`` key (used in the existing
    ``state/active_plays.json``) so the runner works against both
    data sources. Bad / missing values return ``None`` and the
    caller skips the play silently.
    """
    raw = play.get("catalyst_date") or play.get("pdufa_date")
    if not raw:
        return None
    if isinstance(raw, datetime.date) and not isinstance(raw, datetime.datetime):
        return raw
    if isinstance(raw, datetime.datetime):
        return raw.date()
    try:
        return datetime.date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def _position_qty(play: Mapping[str, Any]) -> int:
    """Return the open contract count for ``play``.

    Honours ``qty`` (the M3 schema), ``contracts`` (legacy active
    plays JSON), or ``open_qty`` (broker positions). Non-integer or
    negative values fall back to ``0``.
    """
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


def _position_symbol(play: Mapping[str, Any]) -> Optional[str]:
    """Return the OCC option symbol for ``play``.

    Tries ``symbol`` (M3) and ``option_symbol`` (legacy) so both
    schemas resolve. Empty / non-string values return ``None``;
    the caller skips the play with a structured log line because
    a sell without an OCC symbol cannot route through Alpaca.
    """
    for key in ("symbol", "option_symbol"):
        value = play.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _already_iv_crush_exited(
    executor: PaperExecutor,
    parent_play_card_id: str,
) -> bool:
    """Return ``True`` when an IV-crush exit row exists for the parent play.

    The idempotency check queries the ``orders`` table for any row
    with ``event='iv_crush_exit'`` whose ``parent_play_card_id``
    matches the entry play card. Since the IV-crush exit policy
    fires exactly once per play (on the play's single catalyst
    date), a single hit short-circuits the runner — the second
    call on the same trading day, AND any future call on a later
    day for the same play card, persists no duplicate sell.

    The check is intentionally conservative — only ``status``
    values that succeeded at submission count toward the
    short-circuit. Rows with ``status='rejected'`` (e.g. an earlier
    transient broker rejection) are NOT considered exits and the
    runner retries on the next invocation.
    """
    if not parent_play_card_id:
        return False
    conn = executor._connect()  # noqa: SLF001 — internal access by design
    try:
        cursor = conn.execute(
            """
            SELECT id
            FROM paper_orders
            WHERE event = ?
              AND parent_play_card_id = ?
              AND status != 'rejected'
            LIMIT 1
            """,
            (IV_CRUSH_EXIT_EVENT, parent_play_card_id),
        )
        return cursor.fetchone() is not None
    finally:
        conn.close()


class IVCrushExitRunner:
    """Submit floor(N/2) sells on catalyst-day open via :class:`PaperExecutor`.

    The runner is the only sanctioned entry point for the
    catalyst-day IV-crush exit policy. It wraps an existing
    :class:`PaperExecutor` (which already enforces paper-only at
    construction) and additionally re-checks the wrapped client's
    ``base_url`` on every :meth:`run_on_open` call so a runtime
    drift to the live URL — e.g. a test that mutates ``base_url``
    after construction — is caught BEFORE any sell is submitted
    (VAL-M3-030).

    Position data sources
    ---------------------
    The default :meth:`run_on_open` queries the SQLite ``plays``
    table via :func:`load_active_plays_from_db` (VAL-M3-026: DB is
    the single source of truth for active plays). The legacy
    JSON-backed :func:`load_active_plays` (reading
    ``state/active_plays.json``) is preserved on the module for
    callers that still need the JSON shape (the email-alert
    helpers); tests that want the JSON code path inject it
    explicitly. Production schedulers (intraday cron, M4 systemd
    timer) read from SQLite by default.

    Each position dict is expected to expose:

    * ``play_card_id`` — string id of the entry play card. Used as
      the parent link on the persisted exit row and as the
      idempotency key.
    * ``ticker`` — equity ticker (string).
    * ``symbol`` — OCC option symbol of the open position
      (legacy ``option_symbol`` is also honoured).
    * ``catalyst_date`` — ISO ``YYYY-MM-DD`` (legacy ``pdufa_date``
      is also honoured).
    * ``qty`` — current open contract count (legacy ``contracts``
      / broker ``open_qty`` are also honoured).
    """

    def __init__(self, executor: PaperExecutor) -> None:
        if executor is None:
            raise TypeError("executor must be a PaperExecutor instance")
        self.executor = executor

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_on_open(
        self,
        active_plays: Optional[Any] = None,
        *,
        today: Optional[Any] = None,
    ) -> list[dict[str, Any]]:
        """Submit ``floor(N/2)`` sells for plays whose catalyst is today.

        Parameters
        ----------
        active_plays:
            ``None`` (default) loads positions from the SQLite
            ``plays`` table via :func:`load_active_plays_from_db`
            (VAL-M3-026). A mapping (``{ticker: play_dict}``) or a
            list of position dicts is also accepted for tests /
            scheduler injection — typically used by tests that want
            to exercise the legacy JSON layout via
            :func:`load_active_plays` without going through SQLite.
        today:
            Override for the catalyst-date comparison. Defaults to
            :meth:`datetime.date.today`. Accepts a :class:`date`,
            :class:`datetime`, or an ISO-8601 string.

        Returns
        -------
        list[dict]
            One result dict per position considered. Each entry
            carries ``ticker``, ``play_card_id``, ``status`` (one of
            ``'submitted' | 'skipped' | 'error'``), and a ``reason``
            tag for skips. Submitted entries also carry
            ``order_id`` and ``qty``. Callers (cron schedulers,
            tests) can inspect this list to confirm the run's
            outcome without parsing logs.

        Raises
        ------
        PaperOnlyViolation
            If the wrapped executor's ``client.base_url`` is not the
            paper sandbox at invocation time. No sell is submitted
            (VAL-M3-030).
        """
        client = getattr(self.executor, "client", None)
        base_url = getattr(client, "base_url", None)
        if base_url != PAPER_BASE_URL:
            raise PaperOnlyViolation(
                "IVCrushExitRunner refuses to submit exit orders against "
                f"a non-paper Alpaca client (base_url={base_url!r}). "
                f"Expected {PAPER_BASE_URL!r}. The paper-only guardrail "
                "is re-checked on every run_on_open() call regardless "
                "of LIVE_MODE."
            )

        today_date = _coerce_today(today)
        plays = _coerce_active_plays(active_plays)

        results: list[dict[str, Any]] = []

        for play in plays:
            ticker = play.get("ticker") or play.get("symbol") or "?"
            play_card_id = play.get("play_card_id")
            catalyst_date = _position_catalyst_date(play)

            if catalyst_date is None:
                # Missing / unparseable catalyst date — silently skip;
                # the runner has no opinion on these positions.
                results.append(
                    {
                        "ticker": ticker,
                        "play_card_id": play_card_id,
                        "status": "skipped",
                        "reason": "no_catalyst_date",
                    }
                )
                continue

            if catalyst_date != today_date:
                # Off-catalyst dates iterate to zero exit submissions
                # per VAL-M3-026.
                results.append(
                    {
                        "ticker": ticker,
                        "play_card_id": play_card_id,
                        "status": "skipped",
                        "reason": "not_catalyst_date",
                    }
                )
                continue

            qty = _position_qty(play)
            sell_qty = qty // 2

            if sell_qty < 1:
                # VAL-M3-027: N=1 → 0 contracts, no order submitted,
                # structured log line ``skipped: minimum_size``.
                logger.info(
                    "iv_crush_exit.skipped reason=minimum_size ticker=%s "
                    "play_card_id=%s qty=%s",
                    ticker,
                    play_card_id,
                    qty,
                )
                results.append(
                    {
                        "ticker": ticker,
                        "play_card_id": play_card_id,
                        "status": "skipped",
                        "reason": "minimum_size",
                        "qty": qty,
                    }
                )
                continue

            if play_card_id and _already_iv_crush_exited(
                self.executor, str(play_card_id)
            ):
                # VAL-M3-029: idempotent on re-invocation within the
                # same trading day.
                logger.info(
                    "iv_crush_exit.skipped reason=already_iv_crush_exited "
                    "ticker=%s play_card_id=%s",
                    ticker,
                    play_card_id,
                )
                results.append(
                    {
                        "ticker": ticker,
                        "play_card_id": play_card_id,
                        "status": "skipped",
                        "reason": "already_iv_crush_exited",
                    }
                )
                continue

            symbol = _position_symbol(play)
            if symbol is None:
                logger.warning(
                    "iv_crush_exit.skipped reason=missing_symbol "
                    "ticker=%s play_card_id=%s",
                    ticker,
                    play_card_id,
                )
                results.append(
                    {
                        "ticker": ticker,
                        "play_card_id": play_card_id,
                        "status": "skipped",
                        "reason": "missing_symbol",
                    }
                )
                continue

            sell_card = self._build_sell_card(
                ticker=str(ticker),
                play_card_id=str(play_card_id) if play_card_id else None,
                symbol=symbol,
                sell_qty=sell_qty,
                today=today_date,
            )

            try:
                order_id = self.executor.execute(sell_card)
            except PaperOnlyViolation:
                # The PaperExecutor's runtime guardrail tripped — surface
                # without persisting a duplicate row (PaperExecutor
                # already raises before persistence on this branch).
                raise
            except Exception as exc:  # noqa: BLE001 — re-raised below
                logger.warning(
                    "iv_crush_exit.exit_failed ticker=%s play_card_id=%s "
                    "qty=%s reason=%s",
                    ticker,
                    play_card_id,
                    sell_qty,
                    type(exc).__name__,
                )
                results.append(
                    {
                        "ticker": ticker,
                        "play_card_id": play_card_id,
                        "status": "error",
                        "reason": type(exc).__name__,
                        "qty": sell_qty,
                    }
                )
                continue

            # VAL-M3-028: structured INFO log carrying event, order_id,
            # qty, and parent_play_card_id.
            logger.info(
                "iv_crush_exit.exit_submitted event=%s order_id=%s "
                "qty=%s parent_play_card_id=%s ticker=%s",
                IV_CRUSH_EXIT_EVENT,
                order_id,
                sell_qty,
                play_card_id,
                ticker,
            )
            results.append(
                {
                    "ticker": ticker,
                    "play_card_id": play_card_id,
                    "status": "submitted",
                    "event": IV_CRUSH_EXIT_EVENT,
                    "order_id": order_id,
                    "qty": sell_qty,
                    "parent_play_card_id": play_card_id,
                }
            )

        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_sell_card(
        *,
        ticker: str,
        play_card_id: Optional[str],
        symbol: str,
        sell_qty: int,
        today: datetime.date,
    ) -> dict[str, Any]:
        """Construct the sell ``play_card`` consumed by ``executor.execute``.

        The exit card derives a deterministic ``play_card_id`` and
        ``client_order_id`` (``f"{ticker}-iv_crush_exit-{date}"``)
        so duplicate submissions are de-duped at the broker even
        if the local idempotency check ever short-circuits to false
        on a clock skew.
        """
        date_str = today.isoformat()
        client_order_id = f"{ticker}-{IV_CRUSH_EXIT_EVENT}-{date_str}"
        exit_card_id = (
            f"{play_card_id}-{IV_CRUSH_EXIT_EVENT}-{date_str}"
            if play_card_id
            else f"{ticker}-{IV_CRUSH_EXIT_EVENT}-{date_str}"
        )
        return {
            "play_card_id": exit_card_id,
            "parent_play_card_id": play_card_id,
            "ticker": ticker,
            "event": IV_CRUSH_EXIT_EVENT,
            "client_order_id": client_order_id,
            "option_legs": [
                {
                    "symbol": symbol,
                    "side": "sell",
                    "qty": int(sell_qty),
                    "client_order_id": client_order_id,
                }
            ],
        }


def run_on_open(
    executor: PaperExecutor,
    active_plays: Optional[Any] = None,
    *,
    today: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """Module-level convenience wrapper around :class:`IVCrushExitRunner`.

    Equivalent to ``IVCrushExitRunner(executor).run_on_open(...)``.
    Provided so cron / scheduler entrypoints can fire the autotrigger
    without instantiating the class manually:

    .. code-block:: python

        from biotech_sniper.iv_crush_exit_rules import run_on_open
        from biotech_sniper.paper_executor import PaperExecutor
        from biotech_sniper.alpaca_client import AlpacaClient

        run_on_open(PaperExecutor(AlpacaClient()))
    """
    return IVCrushExitRunner(executor).run_on_open(
        active_plays, today=today
    )


# ---------------------------------------------------------------------------
# f-m3-15: production wiring.
#
# Exposes a top-level CLI so cron / systemd timers (M4) can invoke the
# IV-crush exit autotrigger without writing custom Python wrappers, and
# a fail-soft helper used by ``intraday_scanner.run_intraday_scan`` for
# JOB 5 (IV CRUSH EXIT).
#
# Validation contract assertions fulfilled
# ----------------------------------------
# * **VAL-M3-026** — ``run_on_open()`` defaults to the SQLite ``plays``
#   table loader (see :func:`load_active_plays_from_db`); the legacy
#   JSON loader :func:`load_active_plays` is preserved as an explicit
#   injection for tests / email-alert helpers.
# ---------------------------------------------------------------------------


_CLI_SUMMARY_KEYS: tuple[str, ...] = ("date", "considered", "exited", "errors")


def _cli_summarise_results(
    *,
    date_iso: str,
    considered: int,
    results: Sequence[Mapping[str, Any]] | None,
    dry_run: bool,
    fatal_error: Optional[str] = None,
) -> dict[str, Any]:
    """Build the CLI's stable JSON summary.

    The summary keys are pinned by VAL-M3-026 / f-m3-15 tests:
    ``date`` (YYYY-MM-DD), ``considered`` (positions inspected for
    today), ``exited`` (sells actually submitted), and ``errors``
    (count of per-position failures). ``dry_run`` is added when the
    CLI was invoked with ``--dry-run`` so operators can tell whether
    the run was destructive. ``error`` carries a one-line message
    when a fatal startup error (e.g. missing Alpaca creds) prevented
    the runner from executing.
    """
    summary: dict[str, Any] = {
        "date": date_iso,
        "considered": int(considered),
        "exited": 0,
        "errors": 0,
    }
    if results:
        for r in results:
            status = r.get("status")
            if status == "submitted":
                summary["exited"] += 1
            elif status == "error":
                summary["errors"] += 1
    if dry_run:
        summary["dry_run"] = True
    if fatal_error is not None:
        summary["errors"] = max(summary["errors"], 1)
        summary["error"] = fatal_error
    return summary


def _cli_count_dry_run_exits(
    candidates: Iterable[Mapping[str, Any]],
) -> int:
    """Approximate ``exited`` count for a dry-run.

    A dry-run never actually submits orders so ``exited`` is the
    count of candidates that WOULD pass the floor(N/2) >= 1 size
    gate (VAL-M3-027). Positions with N < 2 are dropped from the
    count so a dry-run summary matches the real run for a
    well-formed plays table.
    """
    count = 0
    for play in candidates:
        if _position_qty(play) // 2 >= 1:
            count += 1
    return count


def _cli_main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for ``python -m biotech_sniper.iv_crush_exit_rules``.

    Behaviour
    ---------
    * ``--run-on-open`` is the only supported mode (room left for
      future ``--check-dte`` / ``--gap-alerts`` modes).
    * ``--date YYYY-MM-DD`` (REQUIRED) — the trading day whose
      catalyst-date plays are eligible for the 50% sell.
    * ``--dry-run`` — query the SQLite ``plays`` table, count what
      WOULD be exited, but never construct an Alpaca client or
      submit an order. The summary's ``exited`` field is populated
      from a synthetic ``floor(N/2) >= 1`` check; ``errors`` stays
      at 0 because no broker call is made.
    * ``--db-path`` — override path to the SQLite db (escape hatch
      for tests). Defaults to ``DATA_DIR / 'alpha_sniper.db'``.

    Output
    ------
    A single line of JSON to stdout matching the stable schema
    documented in :func:`_cli_summarise_results`. Exit code 0 on
    success (including "no active plays" → empty summary). Exit 1
    on a fatal startup error such as a missing Alpaca credential
    when there is real work to do; the JSON ``error`` key carries
    the message so cron operators can grep logs.

    The CLI never raises: every exception is caught, formatted into
    the summary, and surfaced through the exit code.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.iv_crush_exit_rules",
        description=(
            "Run the IV-crush exit autotrigger against the SQLite "
            "plays table and emit a JSON summary."
        ),
    )
    parser.add_argument(
        "--run-on-open",
        action="store_true",
        help=(
            "Execute the catalyst-day open exit policy "
            "(IVCrushExitRunner.run_on_open)."
        ),
    )
    parser.add_argument(
        "--date",
        required=True,
        help="Trading day in ISO format (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Inspect candidates but do not construct an Alpaca "
            "client or submit any sell orders."
        ),
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help=(
            "Override SQLite database path. Defaults to "
            "DATA_DIR/alpha_sniper.db."
        ),
    )
    args = parser.parse_args(argv)

    if not args.run_on_open:
        # Reserve room for future modes; --run-on-open is currently
        # mandatory because that's the only sanctioned operation.
        parser.error("--run-on-open is required")

    try:
        today = datetime.date.fromisoformat(args.date)
    except ValueError:
        summary = _cli_summarise_results(
            date_iso=str(args.date),
            considered=0,
            results=None,
            dry_run=bool(args.dry_run),
            fatal_error=f"invalid --date {args.date!r}; expected YYYY-MM-DD",
        )
        print(json.dumps(summary, sort_keys=True))
        return 1

    db_path = Path(args.db_path) if args.db_path else None
    try:
        all_plays = load_active_plays_from_db(db_path=db_path)
    except Exception as exc:  # pragma: no cover - defensive
        summary = _cli_summarise_results(
            date_iso=today.isoformat(),
            considered=0,
            results=None,
            dry_run=bool(args.dry_run),
            fatal_error=f"db_load_failed: {type(exc).__name__}: {exc}",
        )
        print(json.dumps(summary, sort_keys=True))
        return 1

    candidates = [
        p for p in all_plays if _position_catalyst_date(p) == today
    ]
    considered = len(candidates)

    # No catalyst-day plays ⇒ no broker work; exit 0 immediately and
    # do NOT instantiate an Alpaca client (which would raise
    # AlpacaAuthError when keys are missing — see f-m3-15 test (a)).
    if considered == 0:
        summary = _cli_summarise_results(
            date_iso=today.isoformat(),
            considered=0,
            results=None,
            dry_run=bool(args.dry_run),
        )
        print(json.dumps(summary, sort_keys=True))
        return 0

    if args.dry_run:
        # Dry-run: never construct an executor; estimate ``exited``
        # from a synthetic floor(N/2) >= 1 check so the summary
        # reflects what a live run would have done.
        synthetic_exits = _cli_count_dry_run_exits(candidates)
        summary = _cli_summarise_results(
            date_iso=today.isoformat(),
            considered=considered,
            results=None,
            dry_run=True,
        )
        summary["exited"] = synthetic_exits
        print(json.dumps(summary, sort_keys=True))
        return 0

    # Live mode: construct the paper executor and run.
    try:
        from biotech_sniper.alpaca_client import AlpacaClient

        client = AlpacaClient()
        executor = PaperExecutor(client)
        runner = IVCrushExitRunner(executor)
        results = runner.run_on_open(
            active_plays=candidates, today=today
        )
    except Exception as exc:
        summary = _cli_summarise_results(
            date_iso=today.isoformat(),
            considered=considered,
            results=None,
            dry_run=False,
            fatal_error=f"{type(exc).__name__}: {exc}",
        )
        print(json.dumps(summary, sort_keys=True))
        return 1

    summary = _cli_summarise_results(
        date_iso=today.isoformat(),
        considered=considered,
        results=results,
        dry_run=False,
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


def run_intraday_iv_crush_exit_job(
    *,
    today: Optional[Any] = None,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Best-effort IV-crush exit job invoked by ``intraday_scanner``.

    Wraps :class:`IVCrushExitRunner` with the same JSON summary
    shape as the CLI so the intraday cron + CLI report identically.
    Every failure path returns a populated ``errors`` field rather
    than raising, so JOB 5 in :func:`intraday_scanner.run_intraday_scan`
    never breaks the rest of the cycle.

    Returns
    -------
    dict
        ``{"date", "considered", "exited", "errors"}``. Includes an
        ``error`` key with a one-line message on the fatal-error
        path (missing Alpaca creds, db unreadable, etc.).
    """
    today_date = _coerce_today(today)
    try:
        all_plays = load_active_plays_from_db(db_path=db_path)
    except Exception as exc:
        return _cli_summarise_results(
            date_iso=today_date.isoformat(),
            considered=0,
            results=None,
            dry_run=False,
            fatal_error=f"db_load_failed: {type(exc).__name__}: {exc}",
        )

    candidates = [
        p for p in all_plays if _position_catalyst_date(p) == today_date
    ]
    considered = len(candidates)
    if considered == 0:
        return _cli_summarise_results(
            date_iso=today_date.isoformat(),
            considered=0,
            results=None,
            dry_run=False,
        )

    try:
        from biotech_sniper.alpaca_client import AlpacaClient

        client = AlpacaClient()
        executor = PaperExecutor(client)
        runner = IVCrushExitRunner(executor)
        results = runner.run_on_open(
            active_plays=candidates, today=today_date
        )
    except Exception as exc:
        return _cli_summarise_results(
            date_iso=today_date.isoformat(),
            considered=considered,
            results=None,
            dry_run=False,
            fatal_error=f"{type(exc).__name__}: {exc}",
        )

    return _cli_summarise_results(
        date_iso=today_date.isoformat(),
        considered=considered,
        results=results,
        dry_run=False,
    )


if __name__ == "__main__":
    import sys

    _CLI_SENTINELS = {"--run-on-open", "--date", "--dry-run", "--db-path"}
    if len(sys.argv) > 1 and any(
        arg in _CLI_SENTINELS
        or arg.startswith("--date=")
        or arg.startswith("--db-path=")
        for arg in sys.argv[1:]
    ):
        # New f-m3-15 CLI: emit JSON summary, exit non-zero on fatal error.
        sys.exit(_cli_main(sys.argv[1:]))

    # Legacy default: print the daily exit-alert email body so the
    # original ``python -m biotech_sniper.iv_crush_exit_rules`` (no
    # args) keeps working for operators who relied on it.
    legacy_result = run_daily_exit_check()
    if legacy_result["alert_email_body"]:
        print(legacy_result["alert_email_body"])
    else:
        print("No exit alerts today.")
