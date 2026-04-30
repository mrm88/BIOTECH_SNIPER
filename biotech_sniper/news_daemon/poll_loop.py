"""Stage-1 poll loop CLI + main entry.

This module is the single ``main()`` entry point for the news daemon
package.  It owns argparse, exit-code semantics, and the long-lived
poll loop.

The loop body itself (cursor news_events → scope filter → matcher →
emit candidate_events) is implemented across the sibling submodules
:mod:`scope`, :mod:`matcher`, and :mod:`emit`.  This module wires
them together; subsequent M2 features (f-m2-03 cadence clamp,
f-m2-04 scope filter, f-m2-05 matcher, f-m2-06 emit) flesh out the
real logic.

For f-m2-01 (package skeleton) the body is intentionally a no-op
under ``--dry-run`` so the CLI verification step
(``python -m biotech_sniper.news_daemon --dry-run``) exits cleanly
without database access or network egress.

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
from typing import Iterable, Optional, Sequence

# Lightweight imports only.  We deliberately avoid pulling in
# biotech_sniper.config / biotech_sniper.db at module-import time so
# that ``import biotech_sniper.news_daemon.poll_loop`` is cheap and
# safe to call from ``--help`` on a host without a configured DB.

__all__ = ["build_parser", "main"]

#: Default poll cadence in seconds.  Real clamp / parse logic lives
#: in f-m2-03 (``resolve_poll_seconds``); this constant is the
#: fallback when ``NEWS_POLL_SECONDS`` is unset.
DEFAULT_POLL_SECONDS: int = 30

#: Floor and ceiling for ``NEWS_POLL_SECONDS`` (inclusive). Values
#: outside this range are clamped (with a WARNING log line) by
#: ``resolve_poll_seconds`` in f-m2-03.
MIN_POLL_SECONDS: int = 15
MAX_POLL_SECONDS: int = 90


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
            f"or negative values fall back to the default "
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point invoked by ``__main__`` and by tests.

    Returns
    -------
    int
        Process exit code.  ``0`` on success, ``2`` on argparse
        failure (handled by argparse itself via ``SystemExit``).
    """

    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    log = logging.getLogger("biotech_sniper.news_daemon")

    if args.dry_run:
        # f-m2-01 skeleton: --dry-run exits cleanly without
        # touching the DB or the network.  Real loop wiring lands
        # in f-m2-03 (cadence) / f-m2-06 (emit).
        log.info(
            "news_daemon_dry_run_exit",
            extra={
                "event": "news_daemon_dry_run_exit",
                "module": "news_daemon.poll_loop",
                "max_cycles": args.max_cycles,
                "once": args.once,
            },
        )
        return 0

    # Non-dry-run path: subsequent M2 features wire in
    #   resolve_poll_seconds()  (f-m2-03)
    #   load_polled_universe()  (f-m2-04 — uses scope.filter_universe)
    #   match_news_row()        (f-m2-05 — uses matcher.match_keywords)
    #   emit_candidate()        (f-m2-06 — uses emit.write_candidate)
    #   write_heartbeat()       (f-m2-08 — uses heartbeat.write)
    #
    # For f-m2-01 the skeleton refuses to run a real loop and asks
    # the operator to use --dry-run so the package is exercised in
    # CI without depending on M2-03..M2-09 deliverables.
    raise NotImplementedError(
        "news_daemon poll loop wiring lands in f-m2-03..f-m2-09. "
        "Use --dry-run to verify the package skeleton resolves."
    )


# ---------------------------------------------------------------------------
# Forward-declared helper signatures (filled in by later f-m2-* features).
# Keeping the signatures here documents the loop shape for reviewers and
# lets the sibling submodules import from a stable location.
# ---------------------------------------------------------------------------


def resolve_poll_seconds(env_value: Optional[str] = None) -> int:
    """Resolve the effective poll cadence in seconds.

    Skeleton implementation (f-m2-01): always returns
    :data:`DEFAULT_POLL_SECONDS`.  The real env-parse + clamp +
    WARNING-log behaviour lands in f-m2-03 and is asserted by
    VAL-M2-008..011.
    """

    return DEFAULT_POLL_SECONDS


def iter_pending_news_event_ids(
    db_path: str,
    after_id: int = 0,
) -> Iterable[int]:
    """Cursor over ``news_events.id`` rows past ``after_id``.

    Skeleton stub (f-m2-01): returns an empty iterator.  Real
    implementation lands in f-m2-04..f-m2-06.
    """

    return iter(())
