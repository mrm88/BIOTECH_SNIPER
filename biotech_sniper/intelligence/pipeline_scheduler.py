#!/usr/bin/env python3
"""
PIPELINE SCHEDULER
Runs the batch company pipeline on a tiered schedule:

  Tier 1 tickers  — run every 1 day  (highest priority: PDUFA imminent, Phase 3 near-term)
  Tier 2 tickers  — run every 3 days
  Tier 3 tickers  — run every 7 days
  New tickers     — run immediately, batched in groups of 50

Entry point: run_scheduled_pipelines() → {run_count, new_discoveries, errors}

The scheduler reads the pipeline state to determine which tickers are due,
executes the appropriate batch, saves results, and returns a summary.
"""

import json
import datetime
import logging
import time
from typing import Optional

from biotech_sniper.config import MAX_WORKERS
from biotech_sniper.paths import BASE_DIR

# ---------------------------------------------------------------------------
# Paths & logging
# ---------------------------------------------------------------------------
#
# f-m4-02: log configuration is owned exclusively by
# ``biotech_sniper.logging_setup``; no module installs its own
# ``basicConfig``. Importing ``logging_setup`` here is a no-op when an
# entrypoint has already called ``configure()``; otherwise it falls
# back to a stream handler with the project's JSON formatter.
from biotech_sniper import logging_setup  # noqa: F401 — installs handlers on first import

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tier refresh intervals (days)
# ---------------------------------------------------------------------------
TIER_INTERVALS = {
    1: 1,   # Tier 1 — daily
    2: 3,   # Tier 2 — every 3 days
    3: 7,   # Tier 3 — every 7 days
}
NEW_TICKER_BATCH_SIZE = 50  # process new tickers in batches of this size

# ---------------------------------------------------------------------------
# Import pipeline functions (lazy-safe)
# ---------------------------------------------------------------------------
try:
    from biotech_sniper.intelligence.company_pipeline_manager import (
        run_pipeline_batch,
        load_pipeline_state,
        save_pipeline_state,
        get_new_catalyst_discoveries,
    )
except ImportError:
    # Legacy fallback: when the package is not on sys.path (e.g. a
    # script runs ``intelligence/pipeline_scheduler.py`` directly),
    # add the repo root and retry. We deliberately do NOT add the
    # package directory anymore — that pattern made
    # ``Path(__file__).parent``-style writers leak under
    # ``<package>/state/`` (the read-only reference tree on the
    # VPS). See VAL-M2-061.
    import sys
    sys.path.insert(0, str(BASE_DIR))
    from biotech_sniper.intelligence.company_pipeline_manager import (
        run_pipeline_batch,
        load_pipeline_state,
        save_pipeline_state,
        get_new_catalyst_discoveries,
    )


# ---------------------------------------------------------------------------
# Universe loader
# ---------------------------------------------------------------------------

def _load_full_universe() -> dict:
    """
    Load the full biotech universe (ticker → metadata).
    Merges:
      1. state/universe_watchlist.json     (primary, bulk-scanned)
      2. state/company_pipelines.json      (already-tracked tickers)
      3. PDUFA_CALENDAR from bulk_universe_scanner (always include PDUFA tickers)
      4. RESEARCH_TICKERS from bulk_universe_scanner (seed tickers)
    Returns {ticker: metadata_dict}.
    """
    universe: dict = {}

    # 1. universe_watchlist
    uw_file = BASE_DIR / "state/universe_watchlist.json"
    if uw_file.exists():
        try:
            with open(uw_file) as f:
                uw = json.load(f)
            candidates = uw.get("candidates", uw)
            if isinstance(candidates, dict):
                for t, meta in candidates.items():
                    universe[t.upper()] = meta
        except Exception as exc:
            log.warning("Could not load universe_watchlist.json: %s", exc)

    # 2. company_pipelines (already-tracked)
    state = load_pipeline_state()
    for t, meta in state.items():
        if t.upper() not in universe:
            universe[t.upper()] = meta

    # 3. PDUFA + research seed tickers
    try:
        from biotech_sniper.intelligence.bulk_universe_scanner import (
            PDUFA_CALENDAR,
            RESEARCH_TICKERS,
        )
        for t in PDUFA_CALENDAR:
            universe.setdefault(t.upper(), {})
        for t in RESEARCH_TICKERS:
            universe.setdefault(t.upper(), {})
    except ImportError:
        pass

    return universe


# ---------------------------------------------------------------------------
# Determine which tickers are due for refresh
# ---------------------------------------------------------------------------

def _get_due_tickers(universe: dict, pipeline_state: dict, today: datetime.date) -> dict:
    """
    Classify each ticker in the universe into one of:
      'new'    — never run (not in pipeline_state)
      'tier_1' — tier 1 and due (age >= 1 day)
      'tier_2' — tier 2 and due (age >= 3 days)
      'tier_3' — tier 3 and due (age >= 7 days)

    Returns {category: [ticker_list]}.
    """
    due = {"new": [], "tier_1": [], "tier_2": [], "tier_3": []}

    for ticker in universe:
        entry = pipeline_state.get(ticker)

        if not entry:
            due["new"].append(ticker)
            continue

        tier         = int(entry.get("tier", 3))
        last_updated = entry.get("last_updated", "")
        interval     = TIER_INTERVALS.get(tier, 7)

        if not last_updated:
            due["new"].append(ticker)
            continue

        try:
            lu_date = datetime.date.fromisoformat(last_updated)
            age     = (today - lu_date).days
        except Exception:
            due["new"].append(ticker)
            continue

        if age >= interval:
            key = f"tier_{tier}"
            due.setdefault(key, []).append(ticker)

    return due


# ---------------------------------------------------------------------------
# Run a batch with error capture
# ---------------------------------------------------------------------------

def _run_batch_safe(
    tickers: list,
    max_workers: int = MAX_WORKERS,
    label: str = "",
) -> tuple:
    """
    Wrapper around run_pipeline_batch that catches top-level exceptions.
    Returns (results_dict, errors_list).
    """
    if not tickers:
        return {}, []

    log.info("Running batch [%s]: %d tickers (workers=%d)", label, len(tickers), max_workers)
    errors = []

    try:
        results = run_pipeline_batch(tickers, max_workers=max_workers)
    except Exception as exc:
        log.error("Batch [%s] failed entirely: %s", label, exc)
        errors = [{"ticker": t, "error": str(exc)} for t in tickers]
        return {}, errors

    # Collect per-ticker errors (None results)
    for t in tickers:
        if t not in results:
            errors.append({"ticker": t, "error": "no result returned"})

    return results, errors


# ---------------------------------------------------------------------------
# Main scheduled run
# ---------------------------------------------------------------------------

def run_scheduled_pipelines(
    max_workers: int = MAX_WORKERS,
    since_date: Optional[str] = None,
) -> dict:
    """
    Execute all due pipeline updates according to tier schedule.

    Steps:
      1. Load universe and pipeline state
      2. Classify tickers by tier/staleness
      3. Run new tickers first (in batches of 50)
      4. Run Tier 1 (highest priority)
      5. Run Tier 2
      6. Run Tier 3
      7. Save merged state
      8. Report new catalyst discoveries

    Args:
      max_workers:  ThreadPoolExecutor workers per batch (capped at
                    :data:`biotech_sniper.config.MAX_WORKERS`)
      since_date:   ISO date string for get_new_catalyst_discoveries (defaults to today)

    Returns dict:
      {
        run_count:        int,     # total tickers processed
        new_discoveries:  list,    # new catalyst events found
        errors:           list,    # per-ticker errors
        tiers_run:        dict,    # {"new": n, "tier_1": n, "tier_2": n, "tier_3": n}
        elapsed_seconds:  float,
      }
    """
    t0    = time.time()
    today = datetime.date.today()

    if since_date is None:
        since_date = today.isoformat()

    log.info("=== Scheduled pipeline run | %s ===", today.isoformat())

    # --- Load state ---
    universe       = _load_full_universe()
    pipeline_state = load_pipeline_state()
    log.info("Universe: %d tickers | State: %d tracked", len(universe), len(pipeline_state))

    # --- Classify due tickers ---
    due         = _get_due_tickers(universe, pipeline_state, today)
    new_tickers = due.get("new", [])
    t1_tickers  = due.get("tier_1", [])
    t2_tickers  = due.get("tier_2", [])
    t3_tickers  = due.get("tier_3", [])

    log.info(
        "Due — new=%d | T1=%d | T2=%d | T3=%d",
        len(new_tickers), len(t1_tickers), len(t2_tickers), len(t3_tickers),
    )

    all_results: dict = {}
    all_errors:  list = []
    tiers_run         = {"new": 0, "tier_1": 0, "tier_2": 0, "tier_3": 0}

    # --- New tickers (batches of 50) ---
    for i in range(0, len(new_tickers), NEW_TICKER_BATCH_SIZE):
        chunk  = new_tickers[i: i + NEW_TICKER_BATCH_SIZE]
        label  = f"new[{i}:{i+len(chunk)}]"
        res, errs = _run_batch_safe(chunk, max_workers=max_workers, label=label)
        all_results.update(res)
        all_errors.extend(errs)
        tiers_run["new"] += len(res)
        # Persist after each new-ticker batch to preserve progress
        if res:
            save_pipeline_state(res)

    # --- Tier 1 ---
    if t1_tickers:
        res, errs = _run_batch_safe(t1_tickers, max_workers=max_workers, label="tier_1")
        all_results.update(res)
        all_errors.extend(errs)
        tiers_run["tier_1"] = len(res)

    # --- Tier 2 ---
    if t2_tickers:
        res, errs = _run_batch_safe(t2_tickers, max_workers=max_workers, label="tier_2")
        all_results.update(res)
        all_errors.extend(errs)
        tiers_run["tier_2"] = len(res)

    # --- Tier 3 ---
    if t3_tickers:
        res, errs = _run_batch_safe(t3_tickers, max_workers=max_workers, label="tier_3")
        all_results.update(res)
        all_errors.extend(errs)
        tiers_run["tier_3"] = len(res)

    # --- Save all results (Tier 1/2/3) ---
    non_new_results = {
        k: v for k, v in all_results.items()
        if k not in [t for t in new_tickers]
    }
    if non_new_results:
        save_pipeline_state(non_new_results)

    # --- Catalyst discoveries ---
    try:
        discoveries = get_new_catalyst_discoveries(since_date)
    except Exception as exc:
        log.error("catalyst discovery failed: %s", exc)
        discoveries = []

    run_count = sum(tiers_run.values())
    elapsed   = round(time.time() - t0, 1)

    log.info(
        "=== Done | run=%d | discoveries=%d | errors=%d | %.1fs ===",
        run_count, len(discoveries), len(all_errors), elapsed,
    )

    return {
        "run_count":       run_count,
        "new_discoveries": discoveries,
        "errors":          all_errors,
        "tiers_run":       tiers_run,
        "elapsed_seconds": elapsed,
    }


# ---------------------------------------------------------------------------
# Utility: force-refresh a specific ticker list (bypass tier schedule)
# ---------------------------------------------------------------------------

def refresh_tickers(tickers: list, max_workers: int = MAX_WORKERS) -> dict:
    """
    Force-refresh specific tickers regardless of age.
    Saves results and returns {ticker: result}.
    Useful for ad-hoc updates and tests.
    """
    log.info("Force-refresh: %d tickers", len(tickers))
    results, errors = _run_batch_safe(tickers, max_workers=max_workers, label="force")
    if results:
        save_pipeline_state(results)
        log.info("Saved %d force-refreshed tickers", len(results))
    if errors:
        log.warning("Force-refresh errors: %d", len(errors))
    return results


# ---------------------------------------------------------------------------
# Utility: print schedule summary (dry-run)
# ---------------------------------------------------------------------------

def print_schedule_summary():
    """Emit a structured summary of what would run without actually running it.

    f-m4-02a: emits via the project's structured JSON logger rather
    than ``print``; the legacy function name is preserved so callers
    don't break.
    """
    today          = datetime.date.today()
    universe       = _load_full_universe()
    pipeline_state = load_pipeline_state()
    due            = _get_due_tickers(universe, pipeline_state, today)

    total = sum(len(v) for v in due.values())
    log.info(
        "pipeline_schedule_summary",
        extra={
            "event": "pipeline_schedule_summary",
            "as_of_date": today.isoformat(),
            "universe_total": len(universe),
            "state_tracked": len(pipeline_state),
            "due_total": total,
            "due_new": len(due.get("new", [])),
            "due_tier_1": len(due.get("tier_1", [])),
            "due_tier_2": len(due.get("tier_2", [])),
            "due_tier_3": len(due.get("tier_3", [])),
            "tier_1_sample": sorted(due.get("tier_1", []))[:20],
            "new_sample": sorted(due.get("new", []))[:20],
        },
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    # f-m4-02a: configure the structured JSON logger before any code
    # path emits output so the CLI run produces ts/level/event/module
    # JSON lines (and respects the secret-redaction contract).
    from biotech_sniper.logging_setup import configure

    configure(log_name="pipeline_scheduler")

    parser = argparse.ArgumentParser(description="Biotech Pipeline Scheduler")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show schedule summary without running pipelines",
    )
    parser.add_argument(
        "--workers", type=int, default=MAX_WORKERS,
        help=f"ThreadPoolExecutor max_workers (default: {MAX_WORKERS}, hard cap: {MAX_WORKERS})",
    )
    parser.add_argument(
        "--since", type=str, default=None,
        help="ISO date for new_discoveries cutoff (default: today)",
    )
    parser.add_argument(
        "--refresh", nargs="+", metavar="TICKER",
        help="Force-refresh specific tickers",
    )
    args = parser.parse_args()

    if args.dry_run:
        print_schedule_summary()
    elif args.refresh:
        results = refresh_tickers(args.refresh, max_workers=args.workers)
        log.info(
            "pipeline_force_refresh_results",
            extra={
                "event": "pipeline_force_refresh_results",
                "results": {
                    t: {k: v for k, v in r.items() if k != "active_trials"}
                    for t, r in results.items()
                },
            },
        )
    else:
        summary = run_scheduled_pipelines(
            max_workers=args.workers,
            since_date=args.since,
        )
        log.info(
            "pipeline_scheduled_run_summary",
            extra={
                "event": "pipeline_scheduled_run_summary",
                "summary": summary,
            },
        )
