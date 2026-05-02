"""Operator-triggered Stage-2 fan-out CLI (f-live-02).

This module implements the on-demand Stage-2 scan invoked as::

    python -m biotech_sniper.cli.force_scan
        [--dry-run | --live]
        [--scope=pdufa-soon|all|tickers]
        [--n=N]
        [--tickers=A,B,C]
        [--json]
        [--db=PATH]

Modes are mutually exclusive AND mandatory — operator must opt in to
the LLM-spending ``--live`` path or the side-effect-free ``--dry-run``
preview path. There is intentionally no default: omitting both flags
exits with usage error code 2.

``--dry-run``
    Runs the cheap-first chain with mock provider stubs (or
    operator-supplied mocks) and ``persist=False``, so ZERO rows are
    written to ``llm_cost_ledger`` and ZERO rows to
    ``ensemble_scores_event``. The ``.armed`` file is NOT consulted
    (operator can preview without arming). For each in-scope
    candidate the report includes ``ticker``, ``mean_probability``,
    ``label``, ``gate_failed_reason``, ``would_submit``. Exits 0.

``--live``
    Requires the canonical ``.armed`` filesystem marker to exist
    with non-zero size BEFORE invoking any LLM client. Absent /
    empty ``.armed`` exits non-zero with a stderr message AND
    persists a single ``news_match_log`` row per in-scope candidate
    (``reason='armed_file_missing'``, ``gate_outcome='rejected'``).
    With ``.armed`` present, every in-scope candidate flows through
    :func:`biotech_sniper.exec.stage2_dispatcher.run_stage2_chain`
    which honours the cheap-first gate order (cooldown → armed →
    cap → fan-out → post-fanout). When the daily cap fires
    mid-loop, the CLI exits 0 with a stderr ``daily LLM cap
    exceeded; remaining candidates skipped`` message and reports
    ``cap_fired=True`` in the JSON output.

The scope-resolution helpers are intentionally a thin wrapper around
the f-live-01 :func:`biotech_sniper.exec.stage2_news_dispatch
.query_in_scope_candidates` function so both surfaces (auto-dispatch
inside ``run_main_loop`` AND the operator force_scan) share the same
SQL filter for ``pdufa-soon`` / ``all`` scopes. The ``tickers`` scope
is force_scan-specific and intersects the explicit comma list with
``russell2k_biotech ∩ universe.tier IN ('watch','tradeable')`` so an
operator cannot probe a ticker outside the addressable Reading-B
universe.
"""

from __future__ import annotations

import argparse
import json as _json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Union

from biotech_sniper.exec.stage2_news_dispatch import (
    DEFAULT_LIMIT_PDUFA_SOON,
    query_in_scope_candidates,
)
from biotech_sniper.llm.stage2_gates import (
    armed_gate,
    record_stage2_skip,
)


__all__ = [
    "DEFAULT_SCOPE",
    "VALID_SCOPES",
    "DEFAULT_N",
    "ARMED_MISSING_STDERR",
    "ARMED_EMPTY_STDERR",
    "CAP_EXCEEDED_STDERR",
    "build_parser",
    "resolve_force_scan_candidates",
    "main",
]


logger = logging.getLogger(__name__)


DEFAULT_SCOPE: str = "pdufa-soon"
VALID_SCOPES: tuple[str, ...] = ("pdufa-soon", "all", "tickers")
DEFAULT_N: int = 5

ARMED_MISSING_STDERR: str = (
    "armed file missing: .armed must exist with non-zero size before "
    "--live force_scan"
)
ARMED_EMPTY_STDERR: str = (
    "armed file empty: .armed must exist with NON-ZERO SIZE before "
    "--live force_scan"
)
CAP_EXCEEDED_STDERR: str = (
    "daily LLM cap exceeded; remaining candidates skipped"
)


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.cli.force_scan",
        description=(
            "Operator-triggered Stage-2 fan-out (Reading-B). "
            "Either --dry-run or --live must be supplied; the "
            "operator chooses explicitly to avoid accidental "
            "LLM spend."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=False)
    mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help=(
            "Mock-provider preview; writes no llm_cost_ledger / "
            "ensemble_scores_event rows and does NOT consult .armed."
        ),
    )
    mode.add_argument(
        "--live",
        dest="live",
        action="store_true",
        help=(
            "Real Stage-2 fan-out. Requires .armed to exist with "
            "non-zero size; honors LLM_STAGE2_DAILY_USD_CAP."
        ),
    )
    parser.add_argument(
        "--scope",
        choices=VALID_SCOPES,
        default=DEFAULT_SCOPE,
        help=(
            "Candidate scope (default: pdufa-soon). When --tickers "
            "is supplied, the scope is implicitly 'tickers'."
        ),
    )
    parser.add_argument(
        "--n",
        type=int,
        default=DEFAULT_N,
        help="Cap on the number of candidates processed (default: 5).",
    )
    parser.add_argument(
        "--tickers",
        type=str,
        default=None,
        help=(
            "Comma-separated explicit ticker list. Intersected with "
            "russell2k_biotech ∩ universe.tier IN (watch,tradeable)."
        ),
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Emit a single-line JSON document on stdout.",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Optional SQLite DB path override (default: data/alpha_sniper.db).",
    )
    return parser


# ---------------------------------------------------------------------------
# Scope helpers
# ---------------------------------------------------------------------------


def _query_tickers_scope(
    db_path: Path,
    explicit_tickers: Sequence[str],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Resolve the ``tickers`` scope.

    The resolver intersects the explicit comma list with the
    addressable Reading-B universe (``russell2k_biotech ∩
    universe.tier IN ('watch','tradeable')``) and returns the
    subset of ``candidate_events`` rows whose ``ticker`` is in the
    intersection AND whose ``emitted_at`` is within the last 24h
    (matching the f-live-01 dispatch window).
    """
    canonical: list[str] = []
    seen: set[str] = set()
    for raw in explicit_tickers:
        if raw is None:
            continue
        token = str(raw).strip().upper()
        if not token or token in seen:
            continue
        seen.add(token)
        canonical.append(token)
    if not canonical:
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
    placeholders = ",".join("?" * len(canonical))
    sql = f"""
        SELECT ce.id, ce.ticker, ce.source_news_event_id,
               ce.matched_keywords, ce.calendar_match,
               ce.emitted_at, ce.dedup_key
        FROM candidate_events ce
        WHERE ce.ticker IN ({placeholders})
          AND ce.ticker IN (
              SELECT ticker FROM russell2k_biotech
              INTERSECT
              SELECT ticker FROM universe
              WHERE tier IN ('watch', 'tradeable')
          )
          AND ce.emitted_at >= datetime('now', '-24 hours')
        ORDER BY ce.emitted_at DESC, ce.id DESC
        LIMIT ?
    """
    conn = sqlite3.connect(str(db_path))
    try:
        try:
            rows = conn.execute(sql, (*canonical, int(limit))).fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning(
                "force_scan_tickers_query_failed: %r", exc,
            )
            return []
    finally:
        conn.close()
    return [dict(zip(columns, row)) for row in rows]


def resolve_force_scan_candidates(
    *,
    db_path: Union[str, Path],
    scope: str,
    tickers: Optional[Sequence[str]] = None,
    n: int = DEFAULT_N,
) -> list[dict[str, Any]]:
    """Resolve the in-scope candidate set for a force_scan invocation.

    Wraps :func:`query_in_scope_candidates` for the ``pdufa-soon`` /
    ``all`` scopes and adds the force_scan-specific ``tickers``
    scope (universe intersection + last-24h window).
    """
    db = Path(db_path)
    if scope == "tickers":
        if not tickers:
            return []
        return _query_tickers_scope(db, tickers, limit=n)
    if scope == "pdufa-soon":
        return query_in_scope_candidates(db, "pdufa-soon", limit=n)
    if scope == "all":
        return query_in_scope_candidates(db, "all")[: max(0, int(n))]
    return []


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------


def _today_total_stage2_usd(db_path: Path) -> float:
    """Return today's accumulated Stage-2 LLM spend.

    Pure SELECT on ``llm_cost_ledger``; returns 0.0 on any
    OperationalError (the table may be absent in degraded test
    fixtures).
    """
    conn = sqlite3.connect(str(db_path))
    try:
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_cost_ledger "
                "WHERE purpose = ? AND DATE(called_at) = DATE('now')",
                ("stage2_event_scoring",),
            ).fetchone()
            return float(row[0] or 0.0)
        except sqlite3.OperationalError:
            return 0.0
    finally:
        conn.close()


def _format_text_summary(payload: Mapping[str, Any]) -> str:
    """Render a human-readable summary table for the text output mode."""
    summary = payload.get("summary", {})
    candidates = payload.get("candidates", [])
    lines: list[str] = []
    lines.append(f"force_scan mode={payload.get('mode')}")
    lines.append(f"scope={payload.get('scope')} n={payload.get('n')}")
    if payload.get("cap_fired"):
        lines.append("cap_fired=True")
    lines.append("-- summary --")
    for key in (
        "in_scope",
        "processed",
        "passed_gate",
        "would_submit",
        "submitted_orders",
        "total_cost_usd_today",
    ):
        if key in summary:
            lines.append(f"{key}={summary[key]}")
    if candidates:
        lines.append("-- candidates --")
        for c in candidates:
            ticker = c.get("ticker", "")
            mean_p = c.get("mean_probability")
            label = c.get("label")
            reason = c.get("gate_failed_reason")
            ws = c.get("would_submit")
            lines.append(
                f"{ticker} mean_probability={mean_p} label={label} "
                f"gate_failed_reason={reason} would_submit={ws}"
            )
    return "\n".join(lines) + "\n"


def _emit_payload(payload: Mapping[str, Any], *, json_output: bool) -> None:
    if json_output:
        sys.stdout.write(_json.dumps(payload) + "\n")
    else:
        sys.stdout.write(_format_text_summary(payload))
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Dry-run path
# ---------------------------------------------------------------------------


def _run_dry_run(
    *,
    candidates: Sequence[Mapping[str, Any]],
    provider_overrides: Optional[Mapping[str, Callable[..., Any]]],
    scope: str,
    n: int,
    json_output: bool,
    db_path: Path,
) -> int:
    """Execute the dry-run preview path.

    The dry-run path injects mock providers and calls
    :func:`score_candidate_event` with ``persist=False`` so the
    function reads no rows from ``ensemble_scores_event``, writes no
    rows to ``ensemble_scores_event``, and the underlying provider
    callables (mocked) write no rows to ``llm_cost_ledger``. The
    ``.armed`` filesystem marker is NOT consulted.
    """
    from biotech_sniper.llm.ensemble import (
        ALL_PROVIDERS,
        score_candidate_event,
    )

    providers: dict[str, Callable[..., Any]]
    if provider_overrides is None:
        providers = {name: _default_dry_run_provider() for name in ALL_PROVIDERS}
    else:
        providers = dict(provider_overrides)

    candidate_reports: list[dict[str, Any]] = []
    processed = 0
    passed = 0
    would_submit = 0
    for cand in candidates:
        ensemble = score_candidate_event(
            cand, providers=providers, persist=False,
        )
        processed += 1
        gate_failed_reason = ensemble.gate_failed_reason
        will_submit = (
            gate_failed_reason is None
            and ensemble.label == "material"
            and ensemble.direction in ("bullish", "bearish")
        )
        if gate_failed_reason is None:
            passed += 1
        if will_submit:
            would_submit += 1
        candidate_reports.append(
            {
                "ticker": str(cand.get("ticker", "")).upper(),
                "candidate_event_id": cand.get("id"),
                "mean_probability": ensemble.mean_probability,
                "label": ensemble.label,
                "direction": ensemble.direction,
                "gate_failed_reason": gate_failed_reason,
                "would_submit": bool(will_submit),
            }
        )

    payload = {
        "mode": "dry-run",
        "scope": scope,
        "n": int(n),
        "cap_fired": False,
        "candidates": candidate_reports,
        "summary": {
            "in_scope": len(candidates),
            "processed": processed,
            "passed_gate": passed,
            "would_submit": would_submit,
            "total_cost_usd_today": _today_total_stage2_usd(db_path),
        },
    }
    _emit_payload(payload, json_output=json_output)
    return 0


def _default_dry_run_provider() -> Callable[..., Any]:
    """Return a stub provider yielding a unanimous-bullish-material verdict.

    Used by ``--dry-run`` invocations that omit the
    ``provider_overrides`` argument (e.g. operator-driven CLI runs).
    The stub does NOT contact the real provider HTTP endpoints and
    therefore writes ZERO rows to ``llm_cost_ledger``. It returns
    deterministic placeholder values so the simulated report is
    legible without forensic surprises.
    """

    def _call(_candidate: Mapping[str, Any], *, name: str = "stub") -> dict[str, Any]:
        return {
            "label": "material",
            "probability": 0.85,
            "direction": "bullish",
            "rationale": f"{name} dry-run stub",
            "citations": [],
            "latency_ms": 1,
            "cost_usd": 0.0,
        }

    return _call


# ---------------------------------------------------------------------------
# Live path
# ---------------------------------------------------------------------------


def _run_live(
    *,
    candidates: Sequence[Mapping[str, Any]],
    provider_overrides: Optional[Mapping[str, Callable[..., Any]]],
    armed_path: Optional[Union[str, Path]],
    scope: str,
    n: int,
    json_output: bool,
    db_path: Path,
) -> int:
    """Execute the live path.

    Pre-flight: armed_gate. If absent → write
    ``news_match_log`` rejection rows for every in-scope candidate
    AND emit the canonical stderr message AND exit non-zero. If
    present → run :func:`run_stage2_chain` per candidate; cap-mid-
    loop short-circuit exits 0 with the cap-exceeded marker.
    """
    arm = armed_gate(armed_path=armed_path)
    if not arm.passed:
        # f-fix-live-02: distinguish "exists but empty" from
        # "missing/unreadable" so the operator stderr signal is
        # specific. The persistence path is identical (one
        # ``news_match_log`` row per in-scope candidate, reason
        # ``armed_file_missing`` for ``news_match_log.reason``
        # consumer stability — see grep evidence in the f-fix-live-02
        # handoff).
        #
        # f-fix-live-06: resolve the effective path the SAME way
        # :func:`armed_gate` does — when the caller omitted
        # ``armed_path`` (the typical operator invocation from the
        # VPS without ``--armed-path``), fall back to
        # :data:`biotech_sniper.paths.READING_B_ARMED_FILE` so a
        # zero-byte canonical file is correctly classified as
        # ``ARMED_EMPTY_STDERR`` instead of being silently
        # misreported as missing.
        from biotech_sniper import paths as _paths

        if armed_path is None:
            effective_armed_path: Optional[Path] = Path(
                _paths.READING_B_ARMED_FILE
            )
        else:
            effective_armed_path = Path(armed_path)
        try:
            arm_exists_empty = (
                effective_armed_path.is_file()
                and effective_armed_path.stat().st_size == 0
            )
        except OSError:
            arm_exists_empty = False
        for cand in candidates:
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
                audit_path=None,
                ticker=ticker,
                candidate_event_id=cei,
                news_event_id=nei,
                reason="armed_file_missing",
            )
        if arm_exists_empty:
            sys.stderr.write(ARMED_EMPTY_STDERR + "\n")
        else:
            sys.stderr.write(ARMED_MISSING_STDERR + "\n")
        sys.stderr.flush()
        payload = {
            "mode": "live",
            "scope": scope,
            "n": int(n),
            "cap_fired": False,
            "armed_missing": True,
            "candidates": [],
            "summary": {
                "in_scope": len(candidates),
                "processed": 0,
                "passed_gate": 0,
                "submitted_orders": 0,
                "total_cost_usd_today": _today_total_stage2_usd(db_path),
            },
        }
        _emit_payload(payload, json_output=json_output)
        return 2

    from biotech_sniper.exec.stage2_dispatcher import run_stage2_chain

    candidate_reports: list[dict[str, Any]] = []
    processed = 0
    passed = 0
    submitted_orders = 0
    cap_fired = False
    for cand in candidates:
        try:
            chain_result = run_stage2_chain(
                candidate_event_row=cand,
                db_path=db_path,
                armed_path=armed_path,
                providers=provider_overrides,
            )
        except Exception as exc:  # noqa: BLE001 - resilience
            logger.exception(
                "force_scan_chain_error: ticker=%s err=%r",
                cand.get("ticker"),
                exc,
            )
            candidate_reports.append(
                {
                    "ticker": str(cand.get("ticker", "")).upper(),
                    "candidate_event_id": cand.get("id"),
                    "mean_probability": None,
                    "label": None,
                    "direction": None,
                    "gate_failed_reason": "chain_error",
                    "would_submit": False,
                }
            )
            continue

        processed += 1
        if chain_result.gate == "cap":
            cap_fired = True
            candidate_reports.append(
                {
                    "ticker": str(cand.get("ticker", "")).upper(),
                    "candidate_event_id": cand.get("id"),
                    "mean_probability": None,
                    "label": None,
                    "direction": None,
                    "gate_failed_reason": chain_result.reason,
                    "would_submit": False,
                }
            )
            break

        ensemble = chain_result.ensemble_result
        mean_p = ensemble.mean_probability if ensemble is not None else None
        label = ensemble.label if ensemble is not None else None
        direction = ensemble.direction if ensemble is not None else None
        if chain_result.passed:
            passed += 1
        candidate_reports.append(
            {
                "ticker": str(cand.get("ticker", "")).upper(),
                "candidate_event_id": cand.get("id"),
                "mean_probability": mean_p,
                "label": label,
                "direction": direction,
                "gate_failed_reason": chain_result.reason,
                "would_submit": bool(chain_result.passed),
            }
        )

    if cap_fired:
        sys.stderr.write(CAP_EXCEEDED_STDERR + "\n")
        sys.stderr.flush()

    payload = {
        "mode": "live",
        "scope": scope,
        "n": int(n),
        "cap_fired": cap_fired,
        "armed_missing": False,
        "candidates": candidate_reports,
        "summary": {
            "in_scope": len(candidates),
            "processed": processed,
            "passed_gate": passed,
            "submitted_orders": submitted_orders,
            "total_cost_usd_today": _today_total_stage2_usd(db_path),
        },
    }
    _emit_payload(payload, json_output=json_output)
    return 0


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def _resolve_db_path(args: argparse.Namespace) -> Path:
    if args.db:
        return Path(args.db)
    from biotech_sniper.paths import DATA_DIR

    return DATA_DIR / "alpha_sniper.db"


def _split_tickers(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [t.strip().upper() for t in raw.split(",") if t.strip()]


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    provider_overrides: Optional[Mapping[str, Callable[..., Any]]] = None,
    armed_path: Optional[Union[str, Path]] = None,
) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 2

    if not args.dry_run and not args.live:
        sys.stderr.write(
            "error: exactly one of --dry-run / --live is required\n"
        )
        return 2

    explicit_tickers = _split_tickers(args.tickers)
    scope = args.scope
    if explicit_tickers:
        scope = "tickers"
    elif scope == "tickers":
        sys.stderr.write(
            "error: --scope=tickers requires --tickers=A,B,C\n"
        )
        return 2

    db_path = _resolve_db_path(args)
    candidates = resolve_force_scan_candidates(
        db_path=db_path,
        scope=scope,
        tickers=explicit_tickers,
        n=args.n,
    )

    if args.dry_run:
        return _run_dry_run(
            candidates=candidates,
            provider_overrides=provider_overrides,
            scope=scope,
            n=args.n,
            json_output=args.json_output,
            db_path=db_path,
        )
    return _run_live(
        candidates=candidates,
        provider_overrides=provider_overrides,
        armed_path=armed_path,
        scope=scope,
        n=args.n,
        json_output=args.json_output,
        db_path=db_path,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
