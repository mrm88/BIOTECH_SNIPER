"""Intraday hourly entrypoint (f-m4-09).

Runs the intraday trio in fixed order — each step emits its own
structured JSON log line carrying an ``event`` tag so journalctl
JSON tail can verify each one ran:

1. **adverse_news_check** — scans new ``news_events`` rows for
   ``enrichment_label='negative_material'`` and triggers
   ``submit_exit(event='adverse_news')`` for any matching active
   play. Wraps :func:`biotech_sniper.adverse_news.scan_and_trigger`.

2. **stop_loss_tick** — re-prices every active play via the broker
   and fires ``submit_exit(event='stop_loss')`` when the drawdown
   crosses ``config.STOP_LOSS_PCT`` (-50%). Wraps
   :func:`biotech_sniper.stop_loss.run_stop_loss_check`.

3. **rotation_evaluate** — calls
   :func:`biotech_sniper.rotation_engine.evaluate_rotation` to swap
   a weak active play for a higher-ranked challenger from today's
   ``scoring_cache``.

The systemd unit ``alpha-sniper-intraday.service`` invokes this
module via ``python -m biotech_sniper.intraday_run``. Each step
emits ``event=<step_name>`` log lines with ``status`` /
``submitted`` / ``skipped`` / ``errors`` counts so the milestone
validators can grep journalctl JSON for each tag.

Failures inside any step are caught locally and logged with
``status='error'`` so a single broken step does NOT abort the
remaining steps in the trio. The runner is intentionally
defensive: missing Alpaca credentials, an unreachable broker, or
a stale SQLite path each yields a populated summary rather than
an unhandled exception.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import sys
import time
from typing import Any, Mapping, Optional, Sequence


from biotech_sniper import logging_setup


__all__ = [
    "EVENT_INTRADAY_START",
    "EVENT_INTRADAY_DONE",
    "EVENT_ADVERSE_NEWS",
    "EVENT_STOP_LOSS",
    "EVENT_ROTATION",
    "run_adverse_news_check",
    "run_stop_loss_tick",
    "run_rotation_evaluate",
    "run_intraday",
    "main",
]


# Structured ``event`` tags emitted on every log line. Stable strings
# so ``journalctl -u alpha-sniper-intraday.service -o json | jq
# 'select(.event=="adverse_news_check")'`` lights up reliably.
EVENT_INTRADAY_START: str = "intraday_run_start"
EVENT_INTRADAY_DONE: str = "intraday_run_done"
EVENT_ADVERSE_NEWS: str = "adverse_news_check"
EVENT_STOP_LOSS: str = "stop_loss_tick"
EVENT_ROTATION: str = "rotation_evaluate"


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_paper_executor() -> Optional[Any]:
    """Return a :class:`PaperExecutor` for the trio, or ``None`` to dry-run.

    Construction is best-effort: missing Alpaca credentials, a
    misconfigured ``ALPACA_BASE_URL``, or any constructor failure
    yields ``None`` so the trio steps still record audit decisions
    instead of crashing the intraday cycle.
    """
    try:
        from biotech_sniper.alpaca_client import AlpacaClient
        from biotech_sniper.paper_executor import PaperExecutor

        return PaperExecutor(AlpacaClient())
    except Exception as exc:  # noqa: BLE001 - graceful degradation
        log.warning(
            "executor_init_failed",
            extra={
                "event": "executor_init_failed",
                "error": type(exc).__name__,
                "detail": str(exc)[:200],
            },
        )
        return None


def _load_active_plays() -> list[dict[str, Any]]:
    """Return the active option plays from the SQLite db (or empty list)."""
    try:
        from biotech_sniper.iv_crush_exit_rules import (
            load_active_plays_from_db,
        )

        return list(load_active_plays_from_db() or [])
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning(
            "active_plays_load_failed",
            extra={
                "event": "active_plays_load_failed",
                "error": type(exc).__name__,
                "detail": str(exc)[:200],
            },
        )
        return []


def _attach_current_mids(
    plays: Sequence[Mapping[str, Any]],
    *,
    executor: Optional[Any],
) -> list[dict[str, Any]]:
    """Best-effort attach of the latest broker mid to each play.

    Calls :meth:`AlpacaClient.get_positions` once and matches each
    active play's ``symbol`` to the corresponding broker position
    by OCC option symbol. Plays whose symbol is not present in the
    broker positions list keep no ``current_mid`` (and the
    stop-loss runner will skip them as ``no_entry_mid`` when the
    fallback is also absent).

    Failures here are logged but never raise so the stop-loss step
    can still run with the play-card-level ``current_mid`` (if any).
    """
    out = [dict(p) for p in plays]
    if not out or executor is None:
        return out
    client = getattr(executor, "client", None)
    if client is None:
        return out

    broker_mids: dict[str, float] = {}
    try:
        positions = client.get_positions() or []
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning(
            "get_positions_failed",
            extra={
                "event": "get_positions_failed",
                "error": type(exc).__name__,
                "detail": str(exc)[:200],
            },
        )
        return out

    for pos in positions:
        sym = None
        cp = None
        if isinstance(pos, Mapping):
            sym = pos.get("symbol")
            cp = pos.get("current_price")
        else:
            sym = getattr(pos, "symbol", None)
            cp = getattr(pos, "current_price", None)
        if not sym or cp is None:
            continue
        try:
            broker_mids[str(sym)] = float(cp)
        except (TypeError, ValueError):
            continue

    for play in out:
        sym = play.get("symbol") or play.get("option_symbol")
        if sym and sym in broker_mids:
            play["current_mid"] = broker_mids[sym]
    return out


def _summarise_results(
    results: Sequence[Mapping[str, Any]] | None,
) -> dict[str, int]:
    """Aggregate ``status`` field counts from a result list."""
    summary = {"considered": 0, "submitted": 0, "skipped": 0, "errors": 0}
    if not results:
        return summary
    for entry in results:
        if not isinstance(entry, Mapping):
            continue
        summary["considered"] += 1
        status = entry.get("status")
        if status == "submitted":
            summary["submitted"] += 1
        elif status == "error":
            summary["errors"] += 1
        else:
            summary["skipped"] += 1
    return summary


def _coerce_today(today: Any) -> datetime.date:
    if today is None:
        return datetime.date.today()
    if isinstance(today, datetime.date):
        return today
    if isinstance(today, str):
        return datetime.date.fromisoformat(today)
    raise TypeError(f"unsupported today value: {today!r}")


# ---------------------------------------------------------------------------
# Step 1: adverse_news_check
# ---------------------------------------------------------------------------


def run_adverse_news_check(
    *,
    executor: Optional[Any] = None,
    today: Optional[Any] = None,
) -> dict[str, Any]:
    """Scan ``news_events`` for ``negative_material`` rows and exit hits.

    Emits an INFO log line tagged ``event='adverse_news_check'`` with
    the per-call counts. When ``executor`` is ``None`` the step
    short-circuits to ``status='dry_run'`` (no broker interaction)
    so unit tests + dev runs stay hermetic.
    """
    started = time.monotonic()
    today_date = _coerce_today(today)

    if executor is None:
        payload = {
            "considered": 0,
            "submitted": 0,
            "skipped": 0,
            "errors": 0,
            "status": "dry_run",
        }
        log.info(
            EVENT_ADVERSE_NEWS,
            extra={
                "event": EVENT_ADVERSE_NEWS,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "today": today_date.isoformat(),
                **payload,
            },
        )
        return payload

    try:
        from biotech_sniper.adverse_news import scan_and_trigger

        active = _load_active_plays()
        results = list(
            scan_and_trigger(executor, active, today=today_date) or []
        )
    except Exception as exc:  # noqa: BLE001 - log + return
        log.warning(
            EVENT_ADVERSE_NEWS,
            extra={
                "event": EVENT_ADVERSE_NEWS,
                "status": "error",
                "error": type(exc).__name__,
                "detail": str(exc)[:200],
                "today": today_date.isoformat(),
            },
        )
        return {
            "considered": 0,
            "submitted": 0,
            "skipped": 0,
            "errors": 1,
            "status": "error",
        }

    summary = _summarise_results(results)
    summary["status"] = "ok"
    log.info(
        EVENT_ADVERSE_NEWS,
        extra={
            "event": EVENT_ADVERSE_NEWS,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "today": today_date.isoformat(),
            **summary,
        },
    )
    return summary


# ---------------------------------------------------------------------------
# Step 2: stop_loss_tick
# ---------------------------------------------------------------------------


def run_stop_loss_tick(
    *,
    executor: Optional[Any] = None,
    today: Optional[Any] = None,
) -> dict[str, Any]:
    """Re-price active plays and fire stop_loss exits when -50% breached.

    Emits an INFO log line tagged ``event='stop_loss_tick'``. As with
    :func:`run_adverse_news_check`, ``executor=None`` produces
    ``status='dry_run'`` so the step is safe to call without
    credentials.
    """
    started = time.monotonic()
    today_date = _coerce_today(today)

    if executor is None:
        payload = {
            "considered": 0,
            "submitted": 0,
            "skipped": 0,
            "errors": 0,
            "status": "dry_run",
        }
        log.info(
            EVENT_STOP_LOSS,
            extra={
                "event": EVENT_STOP_LOSS,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "today": today_date.isoformat(),
                **payload,
            },
        )
        return payload

    try:
        from biotech_sniper.stop_loss import run_stop_loss_check

        active = _load_active_plays()
        active = _attach_current_mids(active, executor=executor)
        results = list(
            run_stop_loss_check(executor, active, today=today_date) or []
        )
    except Exception as exc:  # noqa: BLE001 - log + return
        log.warning(
            EVENT_STOP_LOSS,
            extra={
                "event": EVENT_STOP_LOSS,
                "status": "error",
                "error": type(exc).__name__,
                "detail": str(exc)[:200],
                "today": today_date.isoformat(),
            },
        )
        return {
            "considered": 0,
            "submitted": 0,
            "skipped": 0,
            "errors": 1,
            "status": "error",
        }

    summary = _summarise_results(results)
    summary["status"] = "ok"
    log.info(
        EVENT_STOP_LOSS,
        extra={
            "event": EVENT_STOP_LOSS,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "today": today_date.isoformat(),
            **summary,
        },
    )
    return summary


# ---------------------------------------------------------------------------
# Step 3: rotation_evaluate
# ---------------------------------------------------------------------------


def run_rotation_evaluate(
    *,
    executor: Optional[Any] = None,
    today: Optional[Any] = None,
) -> dict[str, Any]:
    """Run the rotation engine and emit a structured summary log line.

    Emits ``event='rotation_evaluate'``. ``executor=None`` propagates
    through to :func:`evaluate_rotation` which then runs in dry-run
    mode (no orders submitted; audit-only).
    """
    started = time.monotonic()
    today_date = _coerce_today(today)

    try:
        from biotech_sniper.rotation_engine import evaluate_rotation

        result = evaluate_rotation(executor=executor, today=today_date)
    except Exception as exc:  # noqa: BLE001 - log + return
        log.warning(
            EVENT_ROTATION,
            extra={
                "event": EVENT_ROTATION,
                "status": "error",
                "error": type(exc).__name__,
                "detail": str(exc)[:200],
                "today": today_date.isoformat(),
            },
        )
        return {
            "decisions": 0,
            "skips": 0,
            "active_count": 0,
            "capacity": 0,
            "status": "error",
        }

    decisions = len(result.get("decisions", []) or [])
    skips = len(result.get("skips", []) or [])
    active_count = int(result.get("active_count", 0) or 0)
    capacity = int(result.get("capacity", 0) or 0)
    status = "dry_run" if executor is None else "ok"

    log.info(
        EVENT_ROTATION,
        extra={
            "event": EVENT_ROTATION,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "today": today_date.isoformat(),
            "status": status,
            "decisions": decisions,
            "skips": skips,
            "active_count": active_count,
            "capacity": capacity,
        },
    )
    return {
        "decisions": decisions,
        "skips": skips,
        "active_count": active_count,
        "capacity": capacity,
        "status": status,
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_intraday(
    *,
    executor: Optional[Any] = None,
    today: Optional[Any] = None,
    build_executor: bool = True,
) -> dict[str, Any]:
    """Run the intraday trio in order. Returns aggregated summary dict.

    Order is fixed: ``adverse_news_check`` → ``stop_loss_tick`` →
    ``rotation_evaluate``. Each step is independent — a failure in
    one logs ``status='error'`` and returns its zero summary, but
    the remaining steps still run.

    Parameters
    ----------
    executor:
        Pre-built :class:`PaperExecutor` (tests inject a fake). When
        ``None`` and ``build_executor=True`` (the default for
        production cron) the runner attempts to build one via
        :func:`_build_paper_executor`. When still ``None`` after
        that, every step short-circuits to ``status='dry_run'``.
    today:
        Override the trading date (ISO string or :class:`datetime.date`).
        Defaults to ``datetime.date.today()``.
    build_executor:
        Hermetic switch for unit tests — when ``False`` and
        ``executor`` is ``None`` the runner does NOT attempt to
        construct an :class:`AlpacaClient` (which would otherwise
        try to read credentials from ``config.py``).
    """
    logging_setup.configure(log_name="intraday")
    started = time.monotonic()
    today_date = _coerce_today(today)

    log.info(
        EVENT_INTRADAY_START,
        extra={
            "event": EVENT_INTRADAY_START,
            "today": today_date.isoformat(),
        },
    )

    if executor is None and build_executor:
        executor = _build_paper_executor()

    adverse = run_adverse_news_check(executor=executor, today=today_date)
    stop = run_stop_loss_tick(executor=executor, today=today_date)
    rotation = run_rotation_evaluate(executor=executor, today=today_date)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    summary: dict[str, Any] = {
        "today": today_date.isoformat(),
        "duration_ms": elapsed_ms,
        "executor_present": executor is not None,
        "adverse_news_check": adverse,
        "stop_loss_tick": stop,
        "rotation_evaluate": rotation,
    }
    log.info(
        EVENT_INTRADAY_DONE,
        extra={
            "event": EVENT_INTRADAY_DONE,
            "today": today_date.isoformat(),
            "duration_ms": elapsed_ms,
            "executor_present": executor is not None,
            "adverse_submitted": adverse.get("submitted", 0),
            "stop_loss_submitted": stop.get("submitted", 0),
            "rotation_decisions": rotation.get("decisions", 0),
        },
    )
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.intraday_run",
        description=(
            "Run the intraday trio: adverse_news_check, "
            "stop_loss_tick, rotation_evaluate. Each step emits a "
            "structured JSON log line with an `event` tag for "
            "journalctl observation."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Do not contact Alpaca; every step short-circuits to "
            "status='dry_run'."
        ),
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Override today (ISO YYYY-MM-DD).",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    today = args.date if args.date else None

    if args.dry_run:
        run_intraday(executor=None, today=today, build_executor=False)
    else:
        run_intraday(executor=None, today=today, build_executor=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
