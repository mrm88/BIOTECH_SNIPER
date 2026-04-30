"""Stage-1 poll loop CLI + main entry.

This module is the single ``main()`` entry point for the news daemon
package.  It owns argparse, exit-code semantics, and the long-lived
poll loop.

The loop body itself (cursor news_events → scope filter → matcher →
emit candidate_events) is implemented across the sibling submodules
:mod:`scope`, :mod:`matcher`, and :mod:`emit`.  This module wires
them together; subsequent M2 features (f-m2-04 scope filter,
f-m2-05 matcher, f-m2-06 emit) flesh out the loop body.

f-m2-01 (package skeleton) wired up the CLI; f-m2-03 (this feature)
adds env-driven poll-cadence resolution + clamp + WARNING logging,
the ``NEWS_DAEMON_ENABLED=0`` disabled-idle gate, and a real-run
sleep cadence helper that the M4 watchdog can read from the
heartbeat timeline.

Exit codes
----------

* ``0`` — successful run (graceful drain, dry-run completion, or
  ``--once`` with no candidates).
* ``2`` — invalid argument / configuration error (argparse path).

Synchronous-only transports
---------------------------

The poll loop uses ``requests`` + ``feedparser`` synchronously.
Non-blocking I/O stacks are not permitted anywhere in the package;
the validator greps for their absence.

Module-level dedup-cache state is **forbidden** — the dedup primitive
is ``candidate_events.dedup_key UNIQUE`` via ``INSERT OR IGNORE``.  A
local ``last_event_cursor`` for the news_events scan is allowed; it
is rebuilt from the database on every restart by selecting
``MAX(source_news_event_id)`` from ``candidate_events`` so SIGKILL +
restart loses no work.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Callable, Iterable, Optional, Sequence

# Lightweight imports only.  We deliberately avoid pulling in
# biotech_sniper.config / biotech_sniper.db at module-import time so
# that ``import biotech_sniper.news_daemon.poll_loop`` is cheap and
# safe to call from ``--help`` on a host without a configured DB.

__all__ = [
    "build_default_rss_fetchers",
    "build_parser",
    "main",
    "resolve_poll_seconds",
    "is_news_daemon_enabled",
    "run_disabled_idle",
    "DEFAULT_POLL_SECONDS",
    "MIN_POLL_SECONDS",
    "MAX_POLL_SECONDS",
    "GATE_DECISION_LOG_INTERVAL_SECONDS",
]


# Re-exported from :mod:`biotech_sniper.news_daemon.adapters` so the
# verification step
# ``python -c 'from biotech_sniper.news_daemon.poll_loop import
#  build_default_rss_fetchers; fs = build_default_rss_fetchers();
#  assert len(fs) >= 4'``
# can resolve the symbol without importing the adapter module
# directly.  The import is at module top-level (rather than lazily
# inside :func:`main`) because the adapter module itself performs
# only lightweight imports — the heavy ``intelligence`` watcher
# imports are deferred inside each adapter function so the cost
# of building the list is negligible.
from biotech_sniper.news_daemon.adapters import build_default_rss_fetchers

#: Default poll cadence in seconds.  Used when ``NEWS_POLL_SECONDS``
#: is unset, blank, non-integer, or non-positive (the latter two
#: trigger a WARNING log line via :func:`resolve_poll_seconds`).
DEFAULT_POLL_SECONDS: int = 30

#: Floor for ``NEWS_POLL_SECONDS`` (inclusive).  Values below this
#: floor are clamped to this floor by :func:`resolve_poll_seconds`
#: with a WARNING log line so operators see the override happen.
MIN_POLL_SECONDS: int = 15

#: Ceiling for ``NEWS_POLL_SECONDS`` (inclusive).  Values above this
#: ceiling are clamped to this ceiling by :func:`resolve_poll_seconds`
#: with a WARNING log line.  The ceiling exists so a misconfigured
#: env var cannot stall the daemon for hours between polls; the
#: watchdog (existing ``alpha-sniper-watchdog.timer``) alarms when
#: heartbeat mtime exceeds 5 minutes.
MAX_POLL_SECONDS: int = 90

#: How often the disabled-idle path emits its gate-decision log line
#: (in real wall-clock seconds, independent of the poll cadence).
#: The feature description pins this at "once per minute" so the
#: ops dashboard (and the M4 watchdog) can observe the gate is
#: actively being honoured without flooding the journal.
GATE_DECISION_LOG_INTERVAL_SECONDS: float = 60.0

#: Sentinel used by :func:`resolve_poll_seconds` to distinguish
#: "caller wants the env var lookup" from "caller explicitly passed
#: ``None``".  The latter is a no-op (treated as unset) but kept
#: separate so the call site reads cleanly in tests.
_RESOLVE_FROM_ENV = object()


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser.

    Exposed as a top-level helper so unit tests can invoke ``--help``
    without spawning a subprocess and so other entry points can reuse
    the parser shape.

    The parser advertises the following flags (per VAL-M2-002):

    * ``--once`` — run a single poll cycle and exit (useful for tests
      and one-shot debugging).
    * ``--poll-seconds`` — override the ``NEWS_POLL_SECONDS`` env var
      for this run; still subject to the [15, 90] clamp.
    * ``--db`` — override the SQLite db path; defaults to the value
      resolved by :mod:`biotech_sniper.paths`.
    * ``--dry-run`` — perform no DB writes and no network egress;
      exits cleanly after argument parsing.  Used by
      ``services.yaml::news_daemon_dry_run``.
    * ``--max-cycles`` — cap on the number of poll cycles before a
      clean exit.  Defaults to ``0`` (run forever).  Combined with
      ``--dry-run`` this drives the f-m2-09 resilience tests.
    """

    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.news_daemon",
        description=(
            "Stage-1 news-watcher daemon for the Biotech Sniper "
            "Reading-B mission.  Cursors news_events, applies the "
            "russell2k_biotech ∩ universe.tier scope filter and the "
            "TIER-1/TIER-2 catalyst keyword matcher, and emits "
            "candidate_events rows (idempotent on dedup_key) for the "
            "Stage-2 ensemble scorer."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Run a single poll cycle and exit.  Useful for unit tests "
            "and one-shot debugging.  Implies --max-cycles=1."
        ),
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=None,
        metavar="SECONDS",
        help=(
            "Override NEWS_POLL_SECONDS for this run.  Clamped to "
            f"[{MIN_POLL_SECONDS}, {MAX_POLL_SECONDS}]; non-integer "
            f"or non-positive values fall back to the default "
            f"({DEFAULT_POLL_SECONDS}s) with a WARNING log line."
        ),
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Override the SQLite database path.  Defaults to the "
            "value resolved by biotech_sniper.paths "
            "(BASE_DIR/data/alpha_sniper.db)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Skip all DB writes and network egress; exit 0 cleanly "
            "after argument parsing.  Used by the smoke-test harness "
            "to verify the entrypoint resolves correctly."
        ),
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Cap on the number of poll cycles before a clean exit.  "
            "0 = run forever (default).  Combined with --dry-run "
            "this drives the resilience test harness."
        ),
    )
    return parser


def resolve_poll_seconds(env_value: object = _RESOLVE_FROM_ENV) -> int:
    """Resolve the effective poll cadence in seconds.

    Reads :envvar:`NEWS_POLL_SECONDS` from the process environment
    (or accepts a caller-supplied override) and applies the
    [MIN_POLL_SECONDS, MAX_POLL_SECONDS] clamp with a WARNING log
    line on every override path so operators can see the resolved
    cadence in the journal.

    Decision matrix
    ---------------

    +-------------------------+----------------------------------+
    | Input                   | Result                           |
    +=========================+==================================+
    | unset / ``None``        | :data:`DEFAULT_POLL_SECONDS`     |
    |                         | (silent — VAL-M2-008)            |
    +-------------------------+----------------------------------+
    | ``""`` / ``"   "``      | :data:`DEFAULT_POLL_SECONDS`     |
    | (empty / whitespace)    | + WARNING (VAL-M2-010 ``"" → 30``)|
    +-------------------------+----------------------------------+
    | non-integer (e.g. "abc")| :data:`DEFAULT_POLL_SECONDS`     |
    |                         | + WARNING                        |
    +-------------------------+----------------------------------+
    | <= 0  (e.g. "-1", "0")  | :data:`DEFAULT_POLL_SECONDS`     |
    |                         | + WARNING                        |
    +-------------------------+----------------------------------+
    | < MIN_POLL_SECONDS      | :data:`MIN_POLL_SECONDS`         |
    |                         | + WARNING ("clamped")            |
    +-------------------------+----------------------------------+
    | > MAX_POLL_SECONDS      | :data:`MAX_POLL_SECONDS`         |
    |                         | + WARNING ("clamped")            |
    +-------------------------+----------------------------------+
    | in [MIN, MAX]           | the parsed integer (no log)      |
    +-------------------------+----------------------------------+

    Parameters
    ----------
    env_value:
        Optional override.  When the sentinel default is used (the
        common case), the environment is consulted.  When ``None``
        is passed explicitly, the function behaves exactly as if
        the env var were unset.  When a string is passed, it is
        used as-is in place of the env value.

    Returns
    -------
    int
        The resolved poll cadence in whole seconds, guaranteed to
        be in [:data:`MIN_POLL_SECONDS`, :data:`MAX_POLL_SECONDS`].
    """

    log = logging.getLogger("biotech_sniper.news_daemon")

    if env_value is _RESOLVE_FROM_ENV:
        raw = os.environ.get("NEWS_POLL_SECONDS")
    else:
        raw = env_value  # type: ignore[assignment]

    # ``None`` (explicit unset / env var missing) → silent default.
    # VAL-M2-008 pins this branch as silent — operators rely on the
    # absence of a journal line to confirm no override is in play.
    if raw is None:
        return DEFAULT_POLL_SECONDS

    # An empty or whitespace-only string is an *operator-supplied*
    # value (the env var is set but blank, or the CLI flag was
    # passed an empty argument).  VAL-M2-010's evidence row pins
    # ``"" → 30`` to "fall back to the 30 s default and emit a
    # WARNING with the offending value", so this path MUST log
    # before falling through to the default.  The distinction
    # vs the ``None`` branch above is deliberate: explicit-unset
    # is silent (VAL-M2-008), explicit-empty-string is loud
    # (VAL-M2-010).
    raw_str_unstripped = str(raw)
    raw_str = raw_str_unstripped.strip()
    if raw_str == "":
        # Distinguish truly empty ("") from whitespace-only ("   ")
        # so the journal line preserves the actual operator input.
        reason = (
            "empty_string"
            if raw_str_unstripped == ""
            else "whitespace_only"
        )
        log.warning(
            "news_poll_seconds_fallback: NEWS_POLL_SECONDS=%r is "
            "empty/whitespace; falling back to default %ds",
            raw_str_unstripped,
            DEFAULT_POLL_SECONDS,
            extra={
                "event": "news_poll_seconds_fallback",
                "src_module": "news_daemon.poll_loop",
                "raw_value": raw_str_unstripped,
                "fallback_to": DEFAULT_POLL_SECONDS,
                "reason": reason,
            },
        )
        return DEFAULT_POLL_SECONDS

    try:
        parsed = int(raw_str)
    except (TypeError, ValueError):
        log.warning(
            "news_poll_seconds_fallback: NEWS_POLL_SECONDS=%r is not an "
            "integer; falling back to default %ds",
            raw_str,
            DEFAULT_POLL_SECONDS,
            extra={
                "event": "news_poll_seconds_fallback",
                "src_module": "news_daemon.poll_loop",
                "raw_value": raw_str,
                "fallback_to": DEFAULT_POLL_SECONDS,
                "reason": "not_an_integer",
            },
        )
        return DEFAULT_POLL_SECONDS

    if parsed <= 0:
        log.warning(
            "news_poll_seconds_fallback: NEWS_POLL_SECONDS=%d is not "
            "positive; falling back to default %ds",
            parsed,
            DEFAULT_POLL_SECONDS,
            extra={
                "event": "news_poll_seconds_fallback",
                "src_module": "news_daemon.poll_loop",
                "raw_value": raw_str,
                "parsed": parsed,
                "fallback_to": DEFAULT_POLL_SECONDS,
                "reason": "non_positive",
            },
        )
        return DEFAULT_POLL_SECONDS

    if parsed < MIN_POLL_SECONDS:
        log.warning(
            "news_poll_seconds_clamped: NEWS_POLL_SECONDS=%d below floor "
            "%d; clamped to %d",
            parsed,
            MIN_POLL_SECONDS,
            MIN_POLL_SECONDS,
            extra={
                "event": "news_poll_seconds_clamped",
                "src_module": "news_daemon.poll_loop",
                "raw_value": raw_str,
                "parsed": parsed,
                "clamped_to": MIN_POLL_SECONDS,
                "reason": "below_floor",
            },
        )
        return MIN_POLL_SECONDS

    if parsed > MAX_POLL_SECONDS:
        log.warning(
            "news_poll_seconds_clamped: NEWS_POLL_SECONDS=%d above "
            "ceiling %d; clamped to %d",
            parsed,
            MAX_POLL_SECONDS,
            MAX_POLL_SECONDS,
            extra={
                "event": "news_poll_seconds_clamped",
                "src_module": "news_daemon.poll_loop",
                "raw_value": raw_str,
                "parsed": parsed,
                "clamped_to": MAX_POLL_SECONDS,
                "reason": "above_ceiling",
            },
        )
        return MAX_POLL_SECONDS

    return parsed


def is_news_daemon_enabled(env_value: object = _RESOLVE_FROM_ENV) -> bool:
    """Return True when the daemon is enabled, False when disabled.

    Reads :envvar:`NEWS_DAEMON_ENABLED` from the environment.  The
    semantics mirror the systemd ``EnvironmentFile`` convention:

    * unset / missing → ``True`` (default-on; the production unit
      sets this explicitly to ``1`` for clarity).
    * literal ``"0"`` (after stripping whitespace) → ``False``
      (operator-disabled; daemon enters the disabled-idle loop).
    * any other value → ``True`` (default-on / fail-open).

    The fail-open semantics mirror the existing ``LIVE_MODE`` gate
    where a typo in the env var must NEVER silently disable a
    production safety check.  Here disable is a kill-switch, not a
    safety check, so fail-open is the correct posture.
    """

    if env_value is _RESOLVE_FROM_ENV:
        raw = os.environ.get("NEWS_DAEMON_ENABLED")
    else:
        raw = env_value  # type: ignore[assignment]

    if raw is None:
        return True
    return str(raw).strip() != "0"


def run_disabled_idle(
    poll_seconds: int,
    max_cycles: int = 0,
    *,
    sleep_func: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    gate_log_interval: float = GATE_DECISION_LOG_INTERVAL_SECONDS,
) -> int:
    """Idle the daemon when ``NEWS_DAEMON_ENABLED=0``.

    The disabled-idle path is the kill-switch enforcement:

    * NO database writes (the caller does NOT pass a DB handle in).
    * NO LLM calls (this module does not import the Stage-2 scorer).
    * NO network egress (no RSS fetch, no SEC EDGAR lookup).
    * Gate-decision INFO log emitted at most once per
      ``gate_log_interval`` real seconds so the journal records the
      gate is actively being honoured without flooding.
    * Sleep cadence of ``poll_seconds`` so the watchdog heartbeat
      check (M4) sees a healthy mtime; in M4 a heartbeat write
      will be added here too.

    Parameters
    ----------
    poll_seconds:
        Resolved cadence (already clamped; pass the output of
        :func:`resolve_poll_seconds`).
    max_cycles:
        Number of idle cycles before returning.  ``0`` = run
        forever (the production case under systemd).  Tests and the
        ``--max-cycles`` CLI flag use a finite value.
    sleep_func, monotonic, gate_log_interval:
        Test seams.  Production callers should leave these at the
        defaults; tests inject monkeypatched substitutes so a
        cadence assertion can run in milliseconds rather than
        minutes.

    Returns
    -------
    int
        Process exit code (always ``0``; an interrupted run drops
        out via SIGTERM/SIGKILL handling at the systemd layer).
    """

    log = logging.getLogger("biotech_sniper.news_daemon")

    cycles_run = 0
    last_gate_log: Optional[float] = None
    while True:
        now = monotonic()
        if last_gate_log is None or (now - last_gate_log) >= gate_log_interval:
            log.info(
                "news_daemon_gate_decision: disabled (NEWS_DAEMON_ENABLED=0); "
                "no DB writes, no LLM calls",
                extra={
                    "event": "news_daemon_gate_decision",
                    "src_module": "news_daemon.poll_loop",
                    "decision": "disabled_idle",
                    "reason": "NEWS_DAEMON_ENABLED=0",
                    "poll_seconds": poll_seconds,
                    "cycle": cycles_run,
                },
            )
            last_gate_log = now

        cycles_run += 1
        if max_cycles > 0 and cycles_run >= max_cycles:
            return 0
        sleep_func(float(poll_seconds))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point invoked by ``__main__`` and by tests.

    Decision flow
    -------------

    1. Parse args (argparse exits ``2`` on failure).
    2. Resolve :data:`poll_seconds` (env or ``--poll-seconds`` flag),
       which emits clamp/fallback WARNING lines as a side effect.
    3. ``--dry-run``: log the resolved cadence and exit 0 without
       any DB or network access.
    4. ``NEWS_DAEMON_ENABLED=0``: enter :func:`run_disabled_idle`.
    5. Otherwise: raise ``NotImplementedError`` until f-m2-04..f-m2-09
       wire in the real loop body (scope filter → matcher → emit).

    Returns
    -------
    int
        Process exit code.  ``0`` on success, ``2`` on argparse
        failure (handled by argparse itself via ``SystemExit``).
    """

    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    log = logging.getLogger("biotech_sniper.news_daemon")

    # ``--poll-seconds`` overrides ``NEWS_POLL_SECONDS`` for this run.
    # We pass the value as a string so :func:`resolve_poll_seconds`
    # can apply the same clamp/log path as the env var case.
    if args.poll_seconds is not None:
        poll_seconds = resolve_poll_seconds(str(args.poll_seconds))
    else:
        poll_seconds = resolve_poll_seconds()

    # Resolve the kill-switch up-front so the dry-run path also
    # surfaces the disabled state for ops audits.
    enabled = is_news_daemon_enabled()
    # ``--once`` short-circuits to a single cycle.
    if args.once and args.max_cycles == 0:
        max_cycles = 1
    else:
        max_cycles = args.max_cycles

    if args.dry_run:
        # f-m2-01 contract: --dry-run exits cleanly without touching
        # the DB or the network.  f-m2-03 keeps that behaviour but
        # makes sure the resolved-cadence WARNING (if any) has
        # already been emitted by resolve_poll_seconds() above so
        # the verification step ``NEWS_POLL_SECONDS=5 ... --dry-run
        # | grep -i clamped`` sees the log line.
        log.info(
            "news_daemon_dry_run_exit",
            extra={
                "event": "news_daemon_dry_run_exit",
                "src_module": "news_daemon.poll_loop",
                "max_cycles": max_cycles,
                "once": args.once,
                "poll_seconds": poll_seconds,
                "enabled": enabled,
            },
        )
        return 0

    if not enabled:
        # Disabled-idle kill-switch: log gate-decision once per
        # minute, sleep the resolved cadence, NO DB writes, NO LLM
        # calls.  Honoured for the lifetime of the process so the
        # operator can flip the env var back to ``1`` and
        # ``systemctl restart`` to re-enable the loop.
        return run_disabled_idle(
            poll_seconds=poll_seconds,
            max_cycles=max_cycles,
        )

    # Non-dry-run, enabled path: hand off to the f-m2-09 resilience
    # loop.  The loop wires:
    #   resolve_polled_tickers()  (f-m2-04 scope filter)
    #   run_one_poll_cycle()      (f-m2-05 matcher + f-m2-06 emit)
    #   write_heartbeat()         (f-m2-08 heartbeat) every cycle
    # and survives every failure mode pinned by VAL-M2-037..VAL-M2-052
    # (single-source 500, all-sources 500, news spike, SIGTERM,
    # clock skew).
    from biotech_sniper.news_daemon.resilience import run_main_loop

    if args.db is not None:
        db_path = args.db
    else:
        from biotech_sniper.paths import DATA_DIR

        db_path = str(DATA_DIR / "alpha_sniper.db")

    # f-m2-10 production wiring: the four canonical RSS sources
    # (universal_news_watcher / sec_8k_monitor / ir_events_watcher /
    # intraday_scanner.scan_news_rss) are passed in as the default
    # fetcher list.  Each adapter is sync-only (no non-blocking I/O
    # stacks anywhere in the package) and persists into
    # ``news_events`` via the composite UNIQUE index
    # ``idx_news_events_dedup`` so re-runs are idempotent.  Errors
    # raised by an individual adapter are caught by
    # :func:`run_main_loop._drive_rss_fetchers`, which increments
    # :attr:`errors_session` and continues to the next source — a
    # single failing feed never halts the daemon.
    return run_main_loop(
        db_path,
        poll_seconds=poll_seconds,
        max_cycles=max_cycles,
        rss_fetchers=build_default_rss_fetchers(db_path=db_path),
        install_handlers=True,
    )


# ---------------------------------------------------------------------------
# Forward-declared helper signatures (filled in by later f-m2-* features).
# Keeping the signatures here documents the loop shape for reviewers and
# lets the sibling submodules import from a stable location.
# ---------------------------------------------------------------------------


def iter_pending_news_event_ids(
    db_path: str,
    after_id: int = 0,
) -> Iterable[int]:
    """Cursor over ``news_events.id`` rows past ``after_id``.

    Skeleton stub (f-m2-01): returns an empty iterator.  Real
    implementation lands in f-m2-04..f-m2-06.
    """

    return iter(())
