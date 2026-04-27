"""Stop-loss exit trigger (f-m3-09).

When the live mid of an active position drops by at least
:data:`biotech_sniper.config.STOP_LOSS_PCT` (default ``-0.50`` =
50% below the entry mid), this module submits an exit order tagged
``event='stop_loss'`` that closes 100% of the position.

The trigger funnels every submission through
:meth:`PaperExecutor.submit_exit`, which guarantees:

* ``event='stop_loss'`` is in :data:`hold_policy.ALLOWED_EXIT_EVENTS`
  so the hold-rule guard does not refuse it on a non-catalyst date.
* ``client_order_id = f"{ticker}-stop_loss-{date}"`` (the broker
  idempotency key shared by every f-m3-09 exit trigger).
* Local idempotency: a non-rejected exit row matching the same
  ``(ticker, stop_loss, date)`` triplet short-circuits the second
  call, satisfying VAL-M3-050.

Validation contract assertions exercised
----------------------------------------

* **VAL-M3-049** — :data:`config.STOP_LOSS_PCT` is the trigger
  threshold and equals ``-0.50``.
* **VAL-M3-050** — at ``current_mid / entry_mid - 1 <= -0.50`` the
  trigger fires exactly once per day; subsequent calls are
  idempotent on the ``client_order_id``.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Iterable, Mapping, Optional, Sequence

from biotech_sniper import config as _config
from biotech_sniper import hold_policy as _hold_policy
from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.paper_executor import (
    PaperExecutor,
    PaperOnlyViolation,
)


__all__ = [
    "STOP_LOSS_EVENT",
    "StopLossRunner",
    "compute_drawdown_pct",
    "should_stop_loss",
    "run_stop_loss_check",
]


logger = logging.getLogger(__name__)


#: Event tag persisted on the ``orders`` row + emitted in the
#: structured log line. Matches the f-m3-09 enum verbatim.
STOP_LOSS_EVENT: str = "stop_loss"


# ---------------------------------------------------------------------------
# Pure helpers (testable without an executor)
# ---------------------------------------------------------------------------


def compute_drawdown_pct(
    *, entry_mid: float, current_mid: float
) -> Optional[float]:
    """Return ``current_mid / entry_mid - 1`` or ``None`` for bad inputs.

    Both inputs must be strictly positive floats; non-positive or
    unparseable values yield ``None`` so callers can short-circuit
    cleanly without try/except. The return value is signed —
    negative means a drawdown.
    """
    try:
        em = float(entry_mid)
        cm = float(current_mid)
    except (TypeError, ValueError):
        return None
    if em <= 0 or cm < 0:
        return None
    return cm / em - 1.0


def should_stop_loss(
    *,
    entry_mid: float,
    current_mid: float,
    threshold: Optional[float] = None,
) -> bool:
    """Return ``True`` when the drawdown is at-or-below the stop threshold.

    ``threshold`` defaults to :data:`biotech_sniper.config.STOP_LOSS_PCT`.
    A positive threshold is treated as "stop-loss disabled" — we
    refuse to fire because a positive drawdown threshold is a
    nonsense input that would close every winning play.
    """
    if threshold is None:
        threshold = _config.STOP_LOSS_PCT
    if threshold >= 0:
        # Disabled: a non-negative threshold would never represent a
        # drawdown and treating it as "always fire" would close
        # winners. Refuse.
        return False
    drawdown = compute_drawdown_pct(
        entry_mid=entry_mid, current_mid=current_mid
    )
    if drawdown is None:
        return False
    return drawdown <= threshold


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class StopLossRunner:
    """Iterate active positions and submit ``stop_loss`` exits.

    The runner is the only sanctioned entry point for the f-m3-09
    stop-loss policy. It wraps an existing :class:`PaperExecutor`
    (which already enforces paper-only at construction) and
    additionally re-checks the wrapped client's ``base_url`` on
    every :meth:`run_check` call so a runtime drift to the live URL
    is caught BEFORE any sell is submitted.

    Each position dict is expected to expose:

    * ``ticker`` — equity ticker (string).
    * ``symbol`` — OCC option symbol (legacy ``option_symbol`` is
      also honoured).
    * ``entry_mid`` — the mid price at entry (float). When missing
      the runner falls back to ``entry_fill`` for backward
      compatibility with the legacy ``state/active_plays.json``
      schema.
    * ``current_mid`` — the latest broker mid (float). The runner
      does NOT fetch quotes itself; the caller is responsible for
      attaching the latest mid to each position dict before
      invoking :meth:`run_check`.
    * ``qty`` — current open contract count (legacy ``contracts`` /
      broker ``open_qty`` are also honoured).
    * ``play_card_id`` — string id of the entry play card; used as
      the parent link on the persisted exit row.
    * ``catalyst_date`` (optional) — passed through to the hold
      policy gate so a stop-loss on a non-catalyst date is allowed
      because ``stop_loss`` is in :data:`ALLOWED_EXIT_EVENTS`.
    """

    def __init__(
        self,
        executor: PaperExecutor,
        *,
        threshold: Optional[float] = None,
    ) -> None:
        if executor is None:
            raise TypeError("executor must be a PaperExecutor instance")
        self.executor = executor
        self.threshold = (
            float(threshold)
            if threshold is not None
            else _config.STOP_LOSS_PCT
        )

    def run_check(
        self,
        positions: Iterable[Mapping[str, Any]],
        *,
        today: Optional[Any] = None,
    ) -> list[dict[str, Any]]:
        """Submit a stop-loss exit for any position whose drawdown trips.

        Returns one result dict per inspected position. ``status``
        is one of ``'submitted'``, ``'skipped'``, or ``'error'``;
        ``reason`` carries the skip / error tag for human inspection.
        """
        client = getattr(self.executor, "client", None)
        base_url = getattr(client, "base_url", None)
        if base_url != PAPER_BASE_URL:
            raise PaperOnlyViolation(
                "StopLossRunner refuses to submit exit orders against "
                f"a non-paper Alpaca client (base_url={base_url!r}). "
                f"Expected {PAPER_BASE_URL!r}."
            )

        today_date = _hold_policy.coerce_date(today)
        results: list[dict[str, Any]] = []

        for play in positions:
            if not isinstance(play, Mapping):
                continue
            ticker = play.get("ticker") or play.get("symbol") or "?"
            play_card_id = play.get("play_card_id")

            entry_mid = play.get("entry_mid")
            if entry_mid is None:
                entry_mid = play.get("entry_fill")
            current_mid = play.get("current_mid")

            try:
                em = float(entry_mid) if entry_mid is not None else 0.0
                cm = (
                    float(current_mid) if current_mid is not None else 0.0
                )
            except (TypeError, ValueError):
                em = 0.0
                cm = 0.0

            if em <= 0:
                results.append(
                    {
                        "ticker": ticker,
                        "play_card_id": play_card_id,
                        "status": "skipped",
                        "reason": "no_entry_mid",
                    }
                )
                continue

            if not should_stop_loss(
                entry_mid=em,
                current_mid=cm,
                threshold=self.threshold,
            ):
                drawdown = compute_drawdown_pct(
                    entry_mid=em, current_mid=cm
                )
                results.append(
                    {
                        "ticker": ticker,
                        "play_card_id": play_card_id,
                        "status": "skipped",
                        "reason": "above_threshold",
                        "drawdown_pct": drawdown,
                    }
                )
                continue

            qty = _coerce_qty(play)
            if qty < 1:
                results.append(
                    {
                        "ticker": ticker,
                        "play_card_id": play_card_id,
                        "status": "skipped",
                        "reason": "no_open_qty",
                    }
                )
                continue

            try:
                order_id = self.executor.submit_exit(
                    play,
                    STOP_LOSS_EVENT,
                    today=today_date,
                    sell_qty=qty,
                )
            except PaperOnlyViolation:
                raise
            except Exception as exc:  # noqa: BLE001 — surfaced in result
                logger.warning(
                    "stop_loss.exit_failed ticker=%s play_card_id=%s "
                    "qty=%s reason=%s",
                    ticker,
                    play_card_id,
                    qty,
                    type(exc).__name__,
                )
                results.append(
                    {
                        "ticker": ticker,
                        "play_card_id": play_card_id,
                        "status": "error",
                        "reason": type(exc).__name__,
                        "qty": qty,
                    }
                )
                continue

            logger.info(
                "stop_loss.exit_submitted event=%s order_id=%s qty=%s "
                "ticker=%s parent_play_card_id=%s",
                STOP_LOSS_EVENT,
                order_id,
                qty,
                ticker,
                play_card_id,
            )
            results.append(
                {
                    "ticker": ticker,
                    "play_card_id": play_card_id,
                    "status": "submitted",
                    "event": STOP_LOSS_EVENT,
                    "order_id": order_id,
                    "qty": qty,
                    "parent_play_card_id": play_card_id,
                    "drawdown_pct": compute_drawdown_pct(
                        entry_mid=em, current_mid=cm
                    ),
                }
            )

        return results


def _coerce_qty(play: Mapping[str, Any]) -> int:
    """Return the open contract count for ``play``, defaulting to ``0``."""
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


def run_stop_loss_check(
    executor: PaperExecutor,
    positions: Iterable[Mapping[str, Any]],
    *,
    today: Optional[Any] = None,
    threshold: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Module-level convenience wrapper around :class:`StopLossRunner`."""
    return StopLossRunner(executor, threshold=threshold).run_check(
        positions, today=today
    )
