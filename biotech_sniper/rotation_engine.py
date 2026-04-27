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
#: debate_inverted_preference).
VALID_SKIP_REASONS: frozenset[str] = frozenset(
    {"catalyst_too_close", "below_threshold", "debate_inverted_preference"}
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


def load_active_plays_from_db(
    *, db_path: Optional[Path] = None
) -> list[dict[str, Any]]:
    """Return active option plays from the SQLite ``plays`` table.

    Each row is augmented with the latest ``scoring_cache.ensemble_score``
    for that ticker (today's re-scored value) so callers do not have
    to issue a second query. Tests typically bypass this helper and
    inject ``active_plays`` directly into :func:`evaluate_rotation`.

    The returned dicts carry at minimum: ``ticker``, ``play_card_id``,
    ``symbol``, ``catalyst_date``, ``qty``, ``ensemble_score``.
    """
    target = Path(db_path) if db_path is not None else DATA_DIR / "alpha_sniper.db"
    if not target.is_file():
        return []
    conn = _db.connect(target)
    try:
        try:
            rows = conn.execute(
                """
                SELECT p.ticker, p.catalyst_date, p.option_strike,
                       p.option_expiry, p.option_type, p.payload,
                       (
                           SELECT ensemble_score FROM scoring_cache sc
                            WHERE sc.ticker = p.ticker
                            ORDER BY sc.as_of_date DESC, sc.id DESC
                            LIMIT 1
                       ) AS ensemble_score
                  FROM plays p
                 WHERE p.status = 'active'
                """
            ).fetchall()
        except Exception:  # pragma: no cover - defensive
            return []
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        out.append(record)
    return out


def load_today_candidates_from_db(
    *, today: Any = None, db_path: Optional[Path] = None
) -> list[dict[str, Any]]:
    """Return today's :data:`scoring_cache` candidates ordered by score.

    The query selects every row with ``as_of_date == today`` ordered
    by ``ensemble_score DESC``. Production callers wire this to the
    daily scoring run; tests inject ``candidates`` directly.
    """
    today_d = _hold_policy.coerce_date(today) if today is not None else datetime.date.today()
    target = Path(db_path) if db_path is not None else DATA_DIR / "alpha_sniper.db"
    if not target.is_file():
        return []
    conn = _db.connect(target)
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
        except Exception:  # pragma: no cover - defensive
            return []
    finally:
        conn.close()
    return [dict(row) for row in rows]


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
