"""Stage-2 dispatch from the news_daemon poll loop.

Feature: ``f-live-01-stage2-poll-loop-wiring``.

This module wires the Stage-2 cheap-first chain (and optional paper
order submission) into ``biotech_sniper.news_daemon.resilience.run_main_loop``
so that ``candidate_events`` emitted by Stage-1 actually flow into
the 4-LLM ensemble fan-out continuously while the daemon is running
— gated by THREE conjunctive conditions (any one closed ⇒ zero
LLM calls):

1. ``NEWS_DAEMON_ENABLED=='1'`` (existing kill switch).
2. The Reading-B armed marker file (canonical path lives in
   :data:`biotech_sniper.paths.READING_B_ARMED_FILE`) is a regular
   readable file — delegated to
   :func:`biotech_sniper.llm.stage2_gates.armed_gate` so the
   canonical filesystem-stat semantics are preserved.
3. ``STAGE2_AUTO_DISPATCH=='1'`` (NEW env var; default ``'0'`` so
   existing deployments are not auto-armed by upgrade).

When all 3 gates open, the dispatcher queries an in-scope subset of
``candidate_events`` (configurable via ``STAGE2_DISPATCH_SCOPE``),
runs :func:`biotech_sniper.exec.stage2_dispatcher.run_stage2_chain`
for each, and — when a ``submit_fn`` is wired by the caller, the
market is open, and the underlying is tradable — invokes
:func:`biotech_sniper.exec.stage2_paper_executor.submit_news_event_entry`.

The dispatcher emits structured INFO-level log records at each step
(``stage2_dispatch_start`` / ``stage2_chain_completed`` / per-candidate
/ ``stage2_order_submitted`` / ``stage2_dispatch_complete``) so the
production journal is queryable via ``jq``.

Disabled-idle invariance
------------------------

When :func:`biotech_sniper.news_daemon.poll_loop.is_news_daemon_enabled`
returns ``False`` (``NEWS_DAEMON_ENABLED=0``), ``run_main_loop`` is
NEVER reached — the daemon enters
:func:`biotech_sniper.news_daemon.poll_loop.run_disabled_idle` and
this dispatcher cannot be invoked. VAL-M5-040/041/042/043 are
preserved structurally by that out-of-band gate, AND defensively
honoured here by the explicit ``NEWS_DAEMON_ENABLED=='1'`` check
(symmetric with the contract evidence at VAL-LIVE-001).
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Union

__all__ = [
    "NEWS_DAEMON_ENABLED_ENV",
    "STAGE2_AUTO_DISPATCH_ENV",
    "STAGE2_DISPATCH_SCOPE_ENV",
    "DEFAULT_DISPATCH_SCOPE",
    "VALID_SCOPES",
    "DEFAULT_LIMIT_PDUFA_SOON",
    "Stage2DispatchOutcome",
    "is_news_daemon_enabled_strict",
    "is_stage2_auto_dispatch_enabled",
    "resolve_dispatch_scope",
    "query_in_scope_candidates",
    "dispatch_after_poll_cycle",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env-var names + scope vocabulary
# ---------------------------------------------------------------------------


NEWS_DAEMON_ENABLED_ENV: str = "NEWS_DAEMON_ENABLED"
STAGE2_AUTO_DISPATCH_ENV: str = "STAGE2_AUTO_DISPATCH"
STAGE2_DISPATCH_SCOPE_ENV: str = "STAGE2_DISPATCH_SCOPE"

DEFAULT_DISPATCH_SCOPE: str = "pdufa-soon"
VALID_SCOPES: tuple[str, ...] = ("pdufa-soon", "all", "none")
DEFAULT_LIMIT_PDUFA_SOON: int = 5


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Stage2DispatchOutcome:
    """Return value of :func:`dispatch_after_poll_cycle`.

    Attributes
    ----------
    invoked:
        ``True`` when all three gates were open AND the in-scope set
        was non-empty AND :func:`run_stage2_chain` was invoked at
        least once. ``False`` whenever any gate short-circuited.
    gate_failed:
        Symbolic name of the gate that closed first
        (``'news_daemon_disabled'`` / ``'auto_dispatch_off'`` /
        ``'scope_none'`` / ``'armed_missing'``). ``None`` on
        successful invocation.
    in_scope_count:
        Number of ``candidate_events`` rows the scope filter
        selected for this cycle.
    processed:
        Number of candidates whose chain ran to completion (no
        infrastructure exception).
    passed:
        Number of candidates whose ``run_stage2_chain`` returned
        ``passed=True``.
    orders_submitted:
        Number of ``submit_news_event_entry`` calls that returned a
        non-empty ``alpaca_order_id``.
    rejections:
        Number of candidates that produced a ``news_match_log``
        rejection row inside the dispatcher (currently used for
        the ``armed_missing`` short-circuit, where one row is
        written per in-scope candidate).
    cycle_id:
        UUID-style string identifying this dispatch cycle in logs.
    """

    invoked: bool
    gate_failed: Optional[str]
    in_scope_count: int
    processed: int
    passed: int
    orders_submitted: int
    rejections: int
    cycle_id: str


# ---------------------------------------------------------------------------
# Env-var helpers
# ---------------------------------------------------------------------------


def is_news_daemon_enabled_strict(env_value: Optional[str]) -> bool:
    """Return ``True`` iff ``env_value`` is exactly the literal ``'1'``.

    Distinct from :func:`biotech_sniper.news_daemon.poll_loop.is_news_daemon_enabled`,
    which is fail-OPEN (any value except ``'0'`` is treated as
    enabled). The Stage-2 dispatch gate is fail-CLOSED: if the
    operator wants Stage-2 auto-dispatch on, ``NEWS_DAEMON_ENABLED``
    must be set to the canonical ``'1'``. Anything else (unset,
    blank, ``'true'``, ``'on'``, …) keeps the gate closed.
    """
    if env_value is None:
        return False
    return str(env_value).strip() == "1"


def is_stage2_auto_dispatch_enabled(env_value: Optional[str]) -> bool:
    """Return ``True`` iff ``STAGE2_AUTO_DISPATCH`` is exactly ``'1'``.

    Default (unset / blank) is ``False`` — existing deployments are
    NOT auto-armed by upgrade per the f-live-01 description.
    """
    if env_value is None:
        return False
    return str(env_value).strip() == "1"


def resolve_dispatch_scope(env_value: Optional[str]) -> str:
    """Resolve ``STAGE2_DISPATCH_SCOPE`` into one of :data:`VALID_SCOPES`.

    Unset / blank / unrecognised → :data:`DEFAULT_DISPATCH_SCOPE`
    (``'pdufa-soon'``). Case-insensitive on the env input.
    """
    if env_value is None:
        return DEFAULT_DISPATCH_SCOPE
    value = str(env_value).strip().lower()
    if not value:
        return DEFAULT_DISPATCH_SCOPE
    if value not in VALID_SCOPES:
        logger.warning(
            "stage2_dispatch_scope_unrecognised: value=%r; "
            "falling back to default=%r",
            value,
            DEFAULT_DISPATCH_SCOPE,
        )
        return DEFAULT_DISPATCH_SCOPE
    return value


# ---------------------------------------------------------------------------
# Candidate query
# ---------------------------------------------------------------------------


def query_in_scope_candidates(
    db_path: Union[str, Path],
    scope: str,
    *,
    limit: int = DEFAULT_LIMIT_PDUFA_SOON,
) -> list[dict[str, Any]]:
    """Return the in-scope ``candidate_events`` subset for this cycle.

    Parameters
    ----------
    db_path:
        Path to the SQLite db.
    scope:
        One of :data:`VALID_SCOPES`. ``'none'`` returns ``[]``.
    limit:
        Cap on the number of rows returned for the ``'pdufa-soon'``
        scope (the ``'all'`` scope returns every emitted candidate
        in the last 24h without an explicit cap — the inner
        ``daily_cap_gate`` provides the spend ceiling).

    Returns
    -------
    list[dict[str, Any]]
        Each dict has the canonical ``candidate_events`` column
        names: ``id``, ``ticker``, ``source_news_event_id``,
        ``matched_keywords``, ``calendar_match``, ``emitted_at``,
        ``dedup_key``.
    """
    if scope == "none":
        return []

    columns = (
        "id",
        "ticker",
        "source_news_event_id",
        "matched_keywords",
        "calendar_match",
        "emitted_at",
        "dedup_key",
    )

    conn = sqlite3.connect(str(db_path))
    try:
        if scope == "pdufa-soon":
            rows = conn.execute(
                """
                SELECT ce.id, ce.ticker, ce.source_news_event_id,
                       ce.matched_keywords, ce.calendar_match,
                       ce.emitted_at, ce.dedup_key
                FROM candidate_events ce
                JOIN pdufa_calendar pc ON pc.ticker = ce.ticker
                WHERE pc.action_date BETWEEN DATE('now')
                                         AND DATE('now', '+7 days')
                  AND ce.emitted_at >= datetime('now', '-24 hours')
                ORDER BY pc.action_date ASC, ce.id ASC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        else:  # 'all'
            rows = conn.execute(
                """
                SELECT id, ticker, source_news_event_id,
                       matched_keywords, calendar_match,
                       emitted_at, dedup_key
                FROM candidate_events
                WHERE emitted_at >= datetime('now', '-24 hours')
                ORDER BY emitted_at DESC, id DESC
                """
            ).fetchall()
    except sqlite3.OperationalError as exc:
        logger.warning(
            "stage2_dispatch_query_failed: scope=%s err=%r",
            scope,
            exc,
        )
        return []
    finally:
        conn.close()

    return [dict(zip(columns, row)) for row in rows]


# ---------------------------------------------------------------------------
# Main entry — dispatch_after_poll_cycle
# ---------------------------------------------------------------------------


def _resolve_log(log: Optional[logging.Logger]) -> logging.Logger:
    return log if log is not None else logger


def dispatch_after_poll_cycle(
    db_path: Union[str, Path],
    *,
    cycle_id: Optional[str] = None,
    log: Optional[logging.Logger] = None,
    armed_path: Optional[Union[str, Path]] = None,
    audit_path: Optional[Union[str, Path]] = None,
    now: Optional[_dt.datetime] = None,
    providers: Optional[Mapping[str, Any]] = None,
    paper_executor: Optional[Any] = None,
    market_open_check: Optional[Callable[[], bool]] = None,
    submit_fn: Optional[Callable[..., Any]] = None,
    chain_quote_fn: Optional[Callable[[Mapping[str, Any]], Any]] = None,
    enabled_value: Optional[str] = None,
    auto_dispatch_value: Optional[str] = None,
    scope_value: Optional[str] = None,
) -> Stage2DispatchOutcome:
    """Run Stage-2 dispatch for the in-scope candidates this cycle.

    The function is the single entry point invoked by
    :func:`biotech_sniper.news_daemon.resilience.run_main_loop` after
    each :func:`run_one_poll_cycle` call. It is deliberately
    parameterised so tests can drive every gate combination without
    monkeypatching ``os.environ`` or the ``.armed`` filesystem.

    Parameters
    ----------
    db_path:
        Path to the SQLite db. The dispatcher reads
        ``candidate_events`` / ``pdufa_calendar`` and writes (via
        :func:`run_stage2_chain`) ``ensemble_scores_event`` /
        ``llm_cost_ledger`` / ``news_match_log``.
    cycle_id:
        Optional caller-supplied identifier for this cycle's logs.
        When ``None`` a UUID4 prefix is generated.
    log:
        Optional logger override. Production callers leave this at
        the default (the module-level logger).
    armed_path:
        Optional explicit ``.armed`` path forwarded to
        :func:`biotech_sniper.llm.stage2_gates.armed_gate`. Tests
        pass a tmp_path-rooted location.
    audit_path:
        Optional ``state/audit_latest.json`` path forwarded to
        :func:`record_stage2_skip` for armed-rejection rows.
    now:
        Optional injectable wall-clock used by
        :func:`run_stage2_chain` (cooldown-gate test seam).
    providers:
        Optional mapping ``{provider_name: callable}`` overriding
        the default ensemble adapters. Tests inject deterministic
        stubs.
    paper_executor, submit_fn, chain_quote_fn, market_open_check:
        Optional collaborators wiring the post-pass ``submit`` step.
        When ANY of them is ``None`` the dispatcher logs the chain
        outcome but does NOT submit a paper order. Production
        callers wire all four.
    enabled_value, auto_dispatch_value, scope_value:
        Optional explicit env-var values. ``None`` means "consult
        :func:`os.environ`". Tests typically pass these explicitly
        to avoid env pollution across xdist workers.
    """
    log_ = _resolve_log(log)
    cid = cycle_id or f"cycle-{uuid.uuid4().hex[:12]}"

    enabled_raw = (
        enabled_value
        if enabled_value is not None
        else os.environ.get(NEWS_DAEMON_ENABLED_ENV)
    )
    auto_dispatch_raw = (
        auto_dispatch_value
        if auto_dispatch_value is not None
        else os.environ.get(STAGE2_AUTO_DISPATCH_ENV)
    )
    scope_raw = (
        scope_value
        if scope_value is not None
        else os.environ.get(STAGE2_DISPATCH_SCOPE_ENV)
    )

    # ---- Gate 1: NEWS_DAEMON_ENABLED == '1' ------------------------
    # Defensive symmetric check with poll_loop.is_news_daemon_enabled.
    # In production this gate is also enforced out-of-band by the
    # disabled-idle path (run_main_loop is never invoked when
    # NEWS_DAEMON_ENABLED=0). Re-checking here keeps the dispatch
    # invariant honest if a caller (e.g. force_scan CLI) were to
    # invoke this function outside of run_main_loop.
    if not is_news_daemon_enabled_strict(enabled_raw):
        log_.info(
            "stage2_dispatch_gate_closed: gate=news_daemon_disabled "
            "cycle_id=%s",
            cid,
            extra={
                "event": "stage2_dispatch_gate_closed",
                "cycle_id": cid,
                "gate": "news_daemon_disabled",
            },
        )
        return Stage2DispatchOutcome(
            invoked=False,
            gate_failed="news_daemon_disabled",
            in_scope_count=0,
            processed=0,
            passed=0,
            orders_submitted=0,
            rejections=0,
            cycle_id=cid,
        )

    # ---- Gate 3 (cheapest, no FS access): STAGE2_AUTO_DISPATCH -----
    # Checked BEFORE the armed gate so that ``STAGE2_AUTO_DISPATCH=0``
    # is a true pre-chain short-circuit with NO news_match_log
    # writes — distinct from ``armed_missing`` which DOES write
    # rejection rows.
    if not is_stage2_auto_dispatch_enabled(auto_dispatch_raw):
        log_.info(
            "stage2_dispatch_gate_closed: gate=auto_dispatch_off "
            "cycle_id=%s",
            cid,
            extra={
                "event": "stage2_dispatch_gate_closed",
                "cycle_id": cid,
                "gate": "auto_dispatch_off",
            },
        )
        return Stage2DispatchOutcome(
            invoked=False,
            gate_failed="auto_dispatch_off",
            in_scope_count=0,
            processed=0,
            passed=0,
            orders_submitted=0,
            rejections=0,
            cycle_id=cid,
        )

    # ---- Resolve scope (cheap, no DB hit yet) ----------------------
    scope = resolve_dispatch_scope(scope_raw)
    if scope == "none":
        log_.info(
            "stage2_dispatch_gate_closed: gate=scope_none cycle_id=%s",
            cid,
            extra={
                "event": "stage2_dispatch_gate_closed",
                "cycle_id": cid,
                "gate": "scope_none",
            },
        )
        return Stage2DispatchOutcome(
            invoked=False,
            gate_failed="scope_none",
            in_scope_count=0,
            processed=0,
            passed=0,
            orders_submitted=0,
            rejections=0,
            cycle_id=cid,
        )

    # ---- Gate 2: armed file (delegates to canonical armed_gate) ----
    # Imported lazily so importing this module does not pull in the
    # llm subpackage (preserves the news_daemon → llm import-cleanliness
    # invariant pinned by VAL-M2-003 + test_no_forbidden_substrings_in_source).
    from biotech_sniper.llm.stage2_gates import armed_gate
    from biotech_sniper.llm.stage2_gates import record_stage2_skip

    arm = armed_gate(armed_path=armed_path)
    if not arm.passed:
        # Per requirements: when armed is missing, we still query the
        # in-scope candidate set and write ONE news_match_log row per
        # candidate (reason='armed_file_missing', gate_outcome='rejected').
        # This makes the gate failure forensically reproducible.
        in_scope = query_in_scope_candidates(db_path, scope=scope)
        for cand in in_scope:
            ticker = str(cand.get("ticker", "")).strip().upper()
            cei_raw = cand.get("id")
            try:
                cei: Optional[int] = (
                    int(cei_raw) if cei_raw is not None else None
                )
            except (TypeError, ValueError):
                cei = None
            nei_raw = cand.get("source_news_event_id")
            try:
                nei: Optional[int] = (
                    int(nei_raw) if nei_raw is not None else None
                )
            except (TypeError, ValueError):
                nei = None
            record_stage2_skip(
                db_path=db_path,
                audit_path=audit_path,
                ticker=ticker,
                candidate_event_id=cei,
                news_event_id=nei,
                reason="armed_file_missing",
            )
        log_.info(
            "stage2_dispatch_gate_closed: gate=armed_missing "
            "cycle_id=%s in_scope_count=%d",
            cid,
            len(in_scope),
            extra={
                "event": "stage2_dispatch_gate_closed",
                "cycle_id": cid,
                "gate": "armed_missing",
                "in_scope_count": int(len(in_scope)),
            },
        )
        return Stage2DispatchOutcome(
            invoked=False,
            gate_failed="armed_missing",
            in_scope_count=len(in_scope),
            processed=0,
            passed=0,
            orders_submitted=0,
            rejections=len(in_scope),
            cycle_id=cid,
        )

    # ---- All gates open: query candidates ---------------------------
    candidates = query_in_scope_candidates(db_path, scope=scope)
    log_.info(
        "stage2_dispatch_start: cycle_id=%s in_scope_count=%d scope=%s",
        cid,
        len(candidates),
        scope,
        extra={
            "event": "stage2_dispatch_start",
            "cycle_id": cid,
            "in_scope_count": int(len(candidates)),
            "scope": scope,
        },
    )

    # Empty in-scope set: no chains to run, but the gates were open
    # so we still emit the matched ``stage2_dispatch_complete`` log.
    if not candidates:
        log_.info(
            "stage2_dispatch_complete: cycle_id=%s processed=0 "
            "passed=0 orders_submitted=0",
            cid,
            extra={
                "event": "stage2_dispatch_complete",
                "cycle_id": cid,
                "processed": 0,
                "passed": 0,
                "orders_submitted": 0,
            },
        )
        return Stage2DispatchOutcome(
            invoked=True,
            gate_failed=None,
            in_scope_count=0,
            processed=0,
            passed=0,
            orders_submitted=0,
            rejections=0,
            cycle_id=cid,
        )

    # Lazy import — same import-cleanliness rationale as armed_gate above.
    from biotech_sniper.exec.stage2_dispatcher import run_stage2_chain

    processed = 0
    passed_count = 0
    orders_submitted = 0

    # Underlying-unavailable typed exception is imported lazily so a
    # caller wiring a ``submit_fn`` that does not depend on
    # stage2_paper_executor never pays the import cost.
    try:
        from biotech_sniper.exec.stage2_paper_executor import (
            UnderlyingUnavailable,
        )
    except Exception:  # pragma: no cover - defensive
        UnderlyingUnavailable = None  # type: ignore[assignment]

    for cand in candidates:
        ticker = str(cand.get("ticker", "")).strip().upper()
        cei = cand.get("id")
        try:
            chain_result = run_stage2_chain(
                candidate_event_row=cand,
                db_path=db_path,
                armed_path=armed_path,
                audit_path=audit_path,
                now=now,
                providers=providers,
            )
        except Exception as exc:  # noqa: BLE001 - resilience hook
            log_.exception(
                "stage2_chain_error: cycle_id=%s candidate_event_id=%s "
                "ticker=%s err=%r",
                cid,
                cei,
                ticker,
                exc,
                extra={
                    "event": "stage2_chain_error",
                    "cycle_id": cid,
                    "candidate_event_id": cei,
                    "ticker": ticker,
                    "error_repr": repr(exc),
                },
            )
            continue

        processed += 1
        mean_p: Optional[float] = None
        if chain_result.ensemble_result is not None:
            mean_p = chain_result.ensemble_result.mean_probability

        log_.info(
            "stage2_chain_completed: cycle_id=%s candidate_event_id=%s "
            "ticker=%s passed=%s gate_failed_reason=%s mean_probability=%s",
            cid,
            cei,
            ticker,
            chain_result.passed,
            chain_result.reason,
            mean_p,
            extra={
                "event": "stage2_chain_completed",
                "cycle_id": cid,
                "candidate_event_id": cei,
                "ticker": ticker,
                "passed": bool(chain_result.passed),
                "gate_failed_reason": chain_result.reason,
                "mean_probability": (
                    float(mean_p) if mean_p is not None else None
                ),
            },
        )
        if not chain_result.passed:
            continue
        passed_count += 1

        # ---- Optional submission step --------------------------------
        # Submission is wired by the production caller (a future
        # f-live-04 worker plus operator-defined chain_quote_fn).
        # Whenever any collaborator is missing we log the chain
        # outcome but do NOT attempt to place an order.
        if (
            paper_executor is None
            or submit_fn is None
            or market_open_check is None
            or chain_quote_fn is None
        ):
            continue

        try:
            if not market_open_check():
                log_.info(
                    "stage2_dispatch_market_closed: cycle_id=%s "
                    "ticker=%s",
                    cid,
                    ticker,
                    extra={
                        "event": "stage2_dispatch_market_closed",
                        "cycle_id": cid,
                        "ticker": ticker,
                    },
                )
                continue
        except Exception as exc:  # noqa: BLE001 - resilience hook
            log_.warning(
                "stage2_market_check_error: cycle_id=%s ticker=%s err=%r",
                cid,
                ticker,
                exc,
            )
            continue

        try:
            quote = chain_quote_fn(cand)
        except Exception as exc:  # noqa: BLE001 - resilience hook
            log_.warning(
                "stage2_chain_quote_error: cycle_id=%s ticker=%s err=%r",
                cid,
                ticker,
                exc,
            )
            continue
        if not quote:
            continue

        # quote is expected as (bid, ask, expiry, stock_price).
        try:
            bid, ask, expiry, stock_price = quote
        except (TypeError, ValueError) as exc:
            log_.warning(
                "stage2_chain_quote_shape_invalid: cycle_id=%s "
                "ticker=%s quote=%r err=%r",
                cid,
                ticker,
                quote,
                exc,
            )
            continue

        try:
            order_id = submit_fn(
                executor=paper_executor,
                candidate_event=cand,
                ensemble_result=chain_result.ensemble_result,
                stock_price=float(stock_price),
                bid=bid,
                ask=ask,
                expiry=expiry,
            )
        except Exception as exc:  # noqa: BLE001 - resilience hook
            # UnderlyingUnavailable is the canonical "skip without
            # aborting the whole loop iteration" signal per the
            # f-live-01 requirements; it surfaces at WARNING.
            if (
                UnderlyingUnavailable is not None
                and isinstance(exc, UnderlyingUnavailable)
            ):
                log_.warning(
                    "stage2_underlying_unavailable: cycle_id=%s "
                    "ticker=%s err=%r",
                    cid,
                    ticker,
                    exc,
                    extra={
                        "event": "stage2_underlying_unavailable",
                        "cycle_id": cid,
                        "ticker": ticker,
                    },
                )
            else:
                log_.warning(
                    "stage2_order_submit_error: cycle_id=%s "
                    "ticker=%s err=%r",
                    cid,
                    ticker,
                    exc,
                    extra={
                        "event": "stage2_order_submit_error",
                        "cycle_id": cid,
                        "ticker": ticker,
                        "error_repr": repr(exc),
                    },
                )
            continue

        if order_id:
            orders_submitted += 1
            log_.info(
                "stage2_order_submitted: cycle_id=%s "
                "alpaca_order_id=%s ticker=%s",
                cid,
                order_id,
                ticker,
                extra={
                    "event": "stage2_order_submitted",
                    "cycle_id": cid,
                    "alpaca_order_id": str(order_id),
                    "ticker": ticker,
                },
            )

    log_.info(
        "stage2_dispatch_complete: cycle_id=%s processed=%d "
        "passed=%d orders_submitted=%d",
        cid,
        processed,
        passed_count,
        orders_submitted,
        extra={
            "event": "stage2_dispatch_complete",
            "cycle_id": cid,
            "processed": int(processed),
            "passed": int(passed_count),
            "orders_submitted": int(orders_submitted),
        },
    )

    return Stage2DispatchOutcome(
        invoked=True,
        gate_failed=None,
        in_scope_count=len(candidates),
        processed=processed,
        passed=passed_count,
        orders_submitted=orders_submitted,
        rejections=0,
        cycle_id=cid,
    )
