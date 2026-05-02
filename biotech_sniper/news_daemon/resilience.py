"""Long-lived poll loop with resilience semantics.

This module wraps :func:`biotech_sniper.news_daemon.emit.run_one_poll_cycle`
in the production long-lived loop.  Stage-1 daemon resilience contract
(f-m2-09 / VAL-M2-037 .. VAL-M2-052):

* **Single RSS source 500** — one failing source must not halt the
  daemon: errors are caught, ``errors_session`` increments, the loop
  continues to the next source.  The cycle still emits candidates from
  the healthy sources.
* **All RSS sources 500** — every source failing keeps the daemon alive
  (no SystemExit, no traceback): an ERROR is logged once per failed
  cycle, ``errors_session`` climbs but the loop keeps running on its
  cadence.
* **News spike (1000+ headlines)** — :func:`run_one_poll_cycle` already
  uses ``executemany`` inside a single ``with conn:`` BEGIN/COMMIT block
  (per AGENTS.md); this loop preserves that single-transaction
  invariant so the spike commits or rolls back atomically.
* **SIGTERM mid-cycle** — the loop installs a SIGTERM/SIGINT handler
  that sets ``state.shutdown = True``.  The current cycle finishes
  (single-transaction guarantee → no half-committed candidate rows),
  a final heartbeat is flushed, and the loop returns ``0``.
* **Clock skew** — ``dedup_key`` is sha256 over the
  ``(ticker, news_event_id, matched_keywords)`` triple — wall-clock is
  NOT an input.  The loop never derives keys from ``time.time()``.
* **max_workers=1** — this module performs no concurrency; the
  validator greps for ``ThreadPoolExecutor`` and rejects any
  ``max_workers≥2`` inside the package.
* **Heartbeat on every cycle** — :func:`write_heartbeat` is called once
  per cycle (and once on graceful shutdown) so the M4 watchdog never
  sees a stale file under any error condition.

The module is intentionally test-friendly: every external dependency
(time, signal, sleep, heartbeat path, RSS fetchers) is parameterisable
so the resilience tests can drive deterministic state without spawning
subprocesses.
"""

from __future__ import annotations

import datetime
import logging
import signal
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Deque, Optional, Sequence, Union

from biotech_sniper.news_daemon.emit import run_one_poll_cycle
from biotech_sniper.news_daemon.heartbeat import (
    Heartbeat,
    default_heartbeat_path,
    resolve_version_sha,
    write_heartbeat,
)
from biotech_sniper.news_daemon.scope import resolve_polled_tickers


__all__ = [
    "DEFAULT_SHUTDOWN_POLL_SECONDS",
    "RSS_FAILURES_HISTORY_MAXLEN",
    "RssFetcherCallable",
    "ShutdownState",
    "install_signal_handlers",
    "run_main_loop",
]


#: Per-test override of Stage-2 dispatch collaborators (armed_path,
#: providers, submit_fn, market_open_check, chain_quote_fn,
#: paper_executor).  Production code must NEVER set this — it is a
#: monkeypatch seam used by ``tests/integration/
#: test_news_daemon_stage2_dispatch.py`` and
#: ``tests/integration/test_news_daemon_stage2_dispatch_with_submit.py``
#: to drive the connector tests against a tmp_path-rooted ``.armed``
#: file + deterministic provider/submit stubs without polluting
#: ``os.environ`` across xdist workers.
#:
#: When set, the presence of the dict ITSELF means "test mode — do
#: not attempt production submit-collaborator wiring".  Keys (when
#: present) are forwarded directly into
#: :func:`dispatch_after_poll_cycle`; missing keys default to
#: ``None``.  The supported keys are ``armed_path``, ``providers``,
#: ``submit_fn``, ``market_open_check``, ``chain_quote_fn``, and
#: ``paper_executor``.
_STAGE2_DISPATCH_OVERRIDES_FOR_TESTS: Optional[dict] = None


#: Hard cap on the per-cycle RSS-failure history retained on
#: :class:`ShutdownState`.  Bounded-memory discipline matters for a
#: long-uptime daemon (f-fix-m2-09 / VAL-M2-039 spirit): an unbounded
#: list would grow by one int per poll cycle (~30 s cadence) for as
#: long as the systemd service stays alive, eventually defeating the
#: ``MemoryMax=200M`` envelope.  1024 cycles ≈ 8.5 hours of diagnostic
#: history at the default 30 s cadence — adequate for forensic
#: incident reconstruction without any unbounded growth.  Older
#: entries fall off the left edge of the deque automatically.
RSS_FAILURES_HISTORY_MAXLEN: int = 1024


#: How frequently the sleep loop wakes up to check
#: :attr:`ShutdownState.shutdown`.  Smaller values give faster
#: SIGTERM response at the cost of more wake-ups; the contract
#: requires "exit within 10 s" so 0.5 s is more than enough headroom.
DEFAULT_SHUTDOWN_POLL_SECONDS: float = 0.5


#: Type alias for an RSS-source fetcher callable.  Each callable owns
#: one source; raising ``Exception`` from it must NEVER halt the loop
#: (the wrapper in :func:`_drive_rss_fetchers` catches every exception
#: and increments ``errors_session``).  A successful return value of
#: ``None`` is fine; the fetcher is responsible for writing into the
#: ``news_events`` table out-of-band (matching the existing
#: ``daily_news_ingest`` contract).
RssFetcherCallable = Callable[[], object]


PathLike = Union[str, Path]


@dataclass
class ShutdownState:
    """Mutable shutdown sentinel shared between the loop and signals.

    The default state is a fresh, never-signalled :class:`ShutdownState`
    that the caller passes to :func:`run_main_loop` and to the signal
    handler installed by :func:`install_signal_handlers`.  Tests can
    flip ``shutdown`` from another thread to simulate a SIGTERM
    without raising real signals (which would only work in the main
    thread).
    """

    shutdown: bool = False
    signal_received: Optional[int] = None
    cycles_completed: int = 0
    errors_session: int = 0
    candidates_emitted_session: int = 0
    last_heartbeat_path: Optional[Path] = None
    #: Per-cycle RSS-failure counts, retained as a bounded deque so a
    #: long-uptime daemon never grows this attribute without limit
    #: (see :data:`RSS_FAILURES_HISTORY_MAXLEN`).  Older entries drop
    #: off the left edge automatically once the cap is reached.  The
    #: per-call observable values (e.g. ``failures`` returned by
    #: :func:`_drive_rss_fetchers`) are unchanged — only the retained
    #: history is bounded.
    rss_failures_per_cycle: Deque[int] = field(
        default_factory=lambda: deque(maxlen=RSS_FAILURES_HISTORY_MAXLEN)
    )


def _now_iso() -> str:
    """ISO-8601 UTC timestamp with ``Z`` suffix (microsecond precision).

    Mirrors :func:`biotech_sniper.news_daemon.emit._now_iso` so the
    heartbeat ``last_poll_ts`` lines up byte-for-byte with the
    ``candidate_events.emitted_at`` values written in the same cycle.
    """

    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def install_signal_handlers(state: ShutdownState) -> bool:
    """Install SIGTERM/SIGINT handlers on the main thread.

    The handler flips ``state.shutdown = True`` and records the signal
    number on ``state.signal_received``.  The next cycle observes the
    flag and falls through to the graceful-drain path.

    Returns ``True`` when the handlers were installed successfully and
    ``False`` when called from a non-main thread (Python's
    :func:`signal.signal` raises :class:`ValueError` in that case).
    Tests that run the loop in a worker thread simply skip the
    install and drive ``state.shutdown`` directly.
    """

    def handler(signum: int, _frame: object) -> None:
        state.shutdown = True
        state.signal_received = int(signum)

    try:
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
        return True
    except (ValueError, OSError):
        # ``signal.signal`` outside the main thread raises ValueError;
        # the OSError branch covers exotic platforms where the call
        # fails for other reasons.  Either way the test path drives
        # state.shutdown manually so this is non-fatal.
        return False


def _drive_rss_fetchers(
    fetchers: Sequence[RssFetcherCallable],
    state: ShutdownState,
    log: logging.Logger,
) -> int:
    """Drive each RSS fetcher in turn; return the count of failures.

    Each fetcher executes inside its own try/except so a single source
    raising HTTP 500 (or any other exception) cannot halt the cycle.
    Failures bump ``state.errors_session`` and emit a WARNING with
    ``event=news_daemon_rss_source_error``.  When **every** fetcher in
    the cycle fails, the caller emits an additional ERROR-level
    aggregate log line.

    Returns the number of failed fetchers in this cycle so the caller
    can detect the "all sources failed" condition.
    """

    failures = 0
    for index, fetcher in enumerate(fetchers):
        try:
            fetcher()
        except Exception as exc:  # noqa: BLE001 - resilience hook
            failures += 1
            state.errors_session += 1
            log.warning(
                "news_daemon_rss_source_error: idx=%d err=%r",
                index,
                exc,
                extra={
                    "event": "news_daemon_rss_source_error",
                    "src_module": "news_daemon.resilience",
                    "rss_source_index": index,
                    "error_repr": repr(exc),
                },
            )
    state.rss_failures_per_cycle.append(failures)
    return failures


def _candidate_events_total(db_path: PathLike) -> int:
    """Return the running total of ``candidate_events`` rows.

    Used to populate :attr:`Heartbeat.candidates_emitted_total`.  Any
    sqlite error degrades to ``0`` rather than blocking the heartbeat
    write — operators must never lose visibility of the daemon's
    aliveness because of a transient query failure.
    """

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM candidate_events"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return 0
    if row is None:
        return 0
    try:
        return int(row[0] or 0)
    except (TypeError, ValueError):
        return 0


def _flush_heartbeat(
    state: ShutdownState,
    db_path: PathLike,
    heartbeat_path: Optional[Path],
    version_sha: str,
    log: logging.Logger,
) -> None:
    """Write a heartbeat snapshot.  Best-effort — never raises.

    The watchdog (existing ``alpha-sniper-watchdog.timer``) consumes
    the heartbeat file every 15 minutes.  A failed write here is a
    degraded-mode signal — we log a WARNING but keep the loop alive
    so subsequent cycles get another chance to flush.
    """

    try:
        hb = Heartbeat(
            last_poll_ts=_now_iso(),
            candidates_emitted_total=_candidate_events_total(db_path),
            candidates_emitted_session=int(state.candidates_emitted_session),
            errors_session=int(state.errors_session),
            version_sha=version_sha,
        )
        path = write_heartbeat(hb, path=heartbeat_path)
        state.last_heartbeat_path = path
    except Exception as exc:  # noqa: BLE001 - non-fatal
        log.warning(
            "news_daemon_heartbeat_write_failed: err=%r",
            exc,
            extra={
                "event": "news_daemon_heartbeat_write_failed",
                "src_module": "news_daemon.resilience",
                "error_repr": repr(exc),
            },
        )


def _build_submit_collaborators(
    log: logging.Logger,
) -> tuple[
    Optional[Callable[..., object]],
    Optional[Callable[[], bool]],
    Optional[Callable[[object], object]],
    Optional[object],
]:
    """Lazy-resolve the production Stage-2 submit collaborator chain.

    Returns ``(submit_fn, market_open_check, chain_quote_fn,
    paper_executor)``.  When the underlying ``AlpacaClient`` cannot
    be constructed (missing credentials, transport error at probe
    time), the helper returns all four as ``None`` — caller-side
    logic emits the canonical ``stage2_submit_collaborator_unavailable``
    WARNING and the dispatcher silently skips submission for that
    cycle.

    All imports are intentionally local so a module-level
    ``import biotech_sniper.news_daemon.resilience`` does NOT pull
    in the (heavy) Alpaca SDK or any Stage-2 scoring module —
    preserving the news_daemon → Stage-2 import-cleanliness
    regression pinned by ``test_no_forbidden_substrings_in_source``
    and ``test_import_pulls_no_llm_modules``.

    The ``chain_quote_fn`` slot is intentionally returned as ``None``
    in this fix: a follow-up feature (f-live-04) will wire the live
    options-chain quoter that resolves the OTM strike + bid/ask for
    each candidate.  Until then the dispatcher's ``if any
    collaborator None: skip submission`` short-circuit keeps the
    daemon idempotent — a passing chain still persists ensemble
    rows (forensic), but no broker call is made.
    """
    try:
        from biotech_sniper.alpaca_client import AlpacaClient
        from biotech_sniper.exec.stage2_paper_executor import (
            submit_news_event_entry,
        )
        from biotech_sniper.paper_executor import PaperExecutor
    except Exception as exc:  # noqa: BLE001 - resilience hook
        log.warning(
            "stage2_submit_collaborator_unavailable: reason=%r "
            "stage=imports",
            exc,
            extra={
                "event": "stage2_submit_collaborator_unavailable",
                "src_module": "news_daemon.resilience",
                "stage": "imports",
                "reason": repr(exc),
            },
        )
        return (None, None, None, None)

    try:
        client = AlpacaClient()
        executor = PaperExecutor(client)
    except Exception as exc:  # noqa: BLE001 - resilience hook
        log.warning(
            "stage2_submit_collaborator_unavailable: reason=%r "
            "stage=client_construction",
            exc,
            extra={
                "event": "stage2_submit_collaborator_unavailable",
                "src_module": "news_daemon.resilience",
                "stage": "client_construction",
                "reason": repr(exc),
            },
        )
        return (None, None, None, None)

    def market_open_check() -> bool:
        trading = getattr(client, "_trading", None)
        get_clock = getattr(trading, "get_clock", None)
        if not callable(get_clock):
            return True
        try:
            clock = get_clock()
        except Exception:  # noqa: BLE001 - permissive on probe failure
            return True
        return bool(getattr(clock, "is_open", True))

    chain_quote_fn = None
    return (submit_news_event_entry, market_open_check, chain_quote_fn, executor)


def _interruptible_sleep(
    duration: float,
    state: ShutdownState,
    *,
    sleep_func: Callable[[float], None],
    monotonic: Callable[[], float],
    poll_interval: float,
) -> None:
    """Sleep for up to ``duration`` seconds, returning early on shutdown.

    Production callers pass ``time.sleep`` and ``time.monotonic``; tests
    inject substitutes so the cadence-tolerance assertions can run in
    milliseconds.  ``poll_interval`` controls how often the wake-up
    check fires; the default 0.5 s gives <= 1.0 s shutdown latency
    on top of any in-flight cycle work.
    """

    if duration <= 0:
        return
    deadline = monotonic() + float(duration)
    while not state.shutdown:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return
        sleep_func(min(poll_interval, max(remaining, 0.001)))


def run_main_loop(
    db_path: PathLike,
    *,
    poll_seconds: int,
    max_cycles: int = 0,
    rss_fetchers: Sequence[RssFetcherCallable] = (),
    state: Optional[ShutdownState] = None,
    install_handlers: bool = True,
    sleep_func: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    shutdown_poll_seconds: float = DEFAULT_SHUTDOWN_POLL_SECONDS,
    heartbeat_path: Optional[Path] = None,
    version_sha: Optional[str] = None,
) -> int:
    """Run the long-lived Stage-1 poll loop with resilience semantics.

    The loop body for each cycle is::

        for fetcher in rss_fetchers:
            try: fetcher()
            except: errors_session += 1   # log WARNING, continue
        try:
            polled = resolve_polled_tickers(db_path)
            scanned, inserted = run_one_poll_cycle(db_path, polled)
        except: errors_session += 1       # log EXCEPTION, continue
        write_heartbeat(...)
        sleep poll_seconds (interruptible by state.shutdown)

    Cycle errors NEVER raise out of this function — the daemon stays
    alive across:

    * single RSS source failures (per-source try/except)
    * all RSS source failures (aggregate ERROR + continue)
    * ``run_one_poll_cycle`` exceptions (e.g. transient
      ``OperationalError``) — caught, logged, ``errors_session += 1``,
      next cycle proceeds normally.

    On shutdown (SIGTERM/SIGINT or test-driven ``state.shutdown =
    True``), the function flushes a final heartbeat and returns ``0``.

    Parameters
    ----------
    db_path:
        Filesystem path to the SQLite database.  The function never
        writes to the DB directly; it forwards the path to
        :func:`run_one_poll_cycle` and reads ``COUNT(*)`` from
        :sql:`candidate_events` for the heartbeat aggregate.
    poll_seconds:
        Cadence (already clamped — pass the output of
        :func:`biotech_sniper.news_daemon.poll_loop.resolve_poll_seconds`).
    max_cycles:
        ``0`` = run forever (the production case under systemd).
        Tests pass a small finite value.
    rss_fetchers:
        Optional sequence of zero-argument callables.  Each callable
        owns one RSS source; the loop calls them in order at the top
        of every cycle inside individual try/except blocks.  An empty
        sequence is fine — production wires the existing
        ``universal_news_watcher`` / ``intraday_scanner`` fetchers in
        a follow-up feature; for now the daemon's primary input is
        the ``news_events`` table populated by ``daily_news_ingest``
        out-of-band.
    state:
        Optional shared :class:`ShutdownState`.  When ``None`` a fresh
        instance is created.  Tests pre-create one so they can flip
        ``state.shutdown`` from another thread.
    install_handlers:
        When ``True`` (default), install SIGTERM/SIGINT handlers on
        the main thread.  Tests typically pass ``False`` and drive
        ``state.shutdown`` directly because :func:`signal.signal`
        raises ``ValueError`` outside the main thread.
    sleep_func, monotonic, shutdown_poll_seconds:
        Test seams for the inter-cycle sleep.  Production callers
        leave these at the defaults.
    heartbeat_path:
        Optional override of the heartbeat file destination.  Defaults
        to :func:`default_heartbeat_path` (resolved via
        :mod:`biotech_sniper.paths`).
    version_sha:
        Optional pre-resolved 40-char git SHA.  Defaults to
        :func:`resolve_version_sha` (cached for the loop's lifetime).

    Returns
    -------
    int
        Process exit code; always ``0`` on graceful drain.
    """

    log = logging.getLogger("biotech_sniper.news_daemon.resilience")
    state = state if state is not None else ShutdownState()

    if install_handlers:
        install_signal_handlers(state)

    resolved_version = (
        version_sha if version_sha is not None else resolve_version_sha()
    )
    resolved_heartbeat = (
        Path(heartbeat_path) if heartbeat_path is not None else None
    )

    log.info(
        "news_daemon_loop_started: poll_seconds=%d max_cycles=%d "
        "rss_fetchers=%d",
        poll_seconds,
        max_cycles,
        len(rss_fetchers),
        extra={
            "event": "news_daemon_loop_started",
            "src_module": "news_daemon.resilience",
            "poll_seconds": int(poll_seconds),
            "max_cycles": int(max_cycles),
            "rss_fetcher_count": len(rss_fetchers),
        },
    )

    while not state.shutdown:
        cycle_started_at = monotonic()

        rss_failures = _drive_rss_fetchers(rss_fetchers, state, log)
        if rss_fetchers and rss_failures == len(rss_fetchers):
            # All sources failed — aggregate ERROR + continue.  The
            # daemon stays alive (per VAL-M2-038); the next cycle
            # retries on the configured cadence.
            log.error(
                "news_daemon_all_rss_sources_failed: failed=%d/%d",
                rss_failures,
                len(rss_fetchers),
                extra={
                    "event": "news_daemon_all_rss_sources_failed",
                    "src_module": "news_daemon.resilience",
                    "rss_source_failures": rss_failures,
                    "rss_source_total": len(rss_fetchers),
                },
            )

        # Per-cycle telemetry counters reset at the top of every
        # cycle so the f-misc-08 ``news_daemon_poll_cycle_complete``
        # INFO event reports the *current cycle's* polled-ticker set
        # size and news_events scan count (as opposed to the running
        # session counters that climb monotonically).
        cycle_polled_ticker_count = 0
        cycle_news_events_scanned = 0

        # Run the poll body inside a try/except so a transient
        # database/exception in a downstream module cannot halt the
        # daemon (per VAL-M2-037 / VAL-M2-038).
        try:
            polled = resolve_polled_tickers(db_path)
            cycle_polled_ticker_count = (
                len(polled) if polled is not None else 0
            )
            # f-fix-m2-09: pass the polled set THROUGH UNCHANGED.  An
            # earlier ``polled-or-None`` ternary collapsed an empty
            # set to ``None``, which :func:`run_one_poll_cycle` and
            # :func:`iter_pending_news_events` interpret as "no scope
            # filter, scan all news_events past the watermark".  On
            # 2026-04-30 the VPS emitted 7 stray candidate_events rows
            # during the ~6.5 minute window between the v10 migration
            # restart (18:41:30Z) and universe seeding (18:48:00Z)
            # because of this bug, violating VAL-M2-014 ("zero
            # candidate_events" under empty russell) and VAL-M2-054
            # ("Ticker present in universe but absent from
            # russell2k_biotech yields zero candidates").  Forwarding
            # ``polled`` unchanged means the ``if not polled_set:
            # return (0, 0)`` short-circuit at the top of
            # :func:`run_one_poll_cycle` actually fires and the scope
            # filter is honored.  :func:`resolve_polled_tickers` already
            # logs the canonical empty-russell WARNING + heartbeat is
            # still flushed below, so the M4 watchdog mtime stays
            # fresh.
            scanned, inserted = run_one_poll_cycle(
                db_path,
                polled_tickers=polled,
            )
            cycle_news_events_scanned = int(scanned or 0)
            state.candidates_emitted_session += int(inserted or 0)
        except Exception as exc:  # noqa: BLE001 - resilience hook
            state.errors_session += 1
            log.exception(
                "news_daemon_poll_cycle_error: err=%r",
                exc,
                extra={
                    "event": "news_daemon_poll_cycle_error",
                    "src_module": "news_daemon.resilience",
                    "error_repr": repr(exc),
                },
            )

        _flush_heartbeat(
            state,
            db_path,
            resolved_heartbeat,
            resolved_version,
            log,
        )

        # f-live-01: run Stage-2 dispatch on the in-scope subset of
        # candidate_events emitted this cycle.  All gates (kill switch,
        # ``.armed`` file, ``STAGE2_AUTO_DISPATCH``) are checked inside
        # :func:`dispatch_after_poll_cycle` so this site stays a
        # thin connector — a closed gate short-circuits with zero
        # ``llm_cost_ledger`` writes / zero ``ensemble_scores_event``
        # rows.  Lazy import keeps news_daemon import-clean: the
        # module name ``stage2_news_dispatch`` does not contain the
        # ``llm`` substring banned by VAL-M2-003 +
        # ``test_no_forbidden_substrings_in_source``, but importing
        # the llm subpackage at module load WOULD trip that test
        # because the importer recurses transitively.  Hence we
        # defer until the loop body actually fires.
        try:
            from biotech_sniper.exec.stage2_news_dispatch import (
                dispatch_after_poll_cycle,
            )

            test_overrides = _STAGE2_DISPATCH_OVERRIDES_FOR_TESTS
            if test_overrides is not None:
                overrides = test_overrides
                submit_fn = overrides.get("submit_fn")
                market_open_check = overrides.get("market_open_check")
                chain_quote_fn = overrides.get("chain_quote_fn")
                paper_executor_instance = overrides.get("paper_executor")
            else:
                overrides = {}
                (
                    submit_fn,
                    market_open_check,
                    chain_quote_fn,
                    paper_executor_instance,
                ) = _build_submit_collaborators(log)

            missing = [
                name
                for name, value in (
                    ("submit_fn", submit_fn),
                    ("market_open_check", market_open_check),
                    ("chain_quote_fn", chain_quote_fn),
                    ("paper_executor", paper_executor_instance),
                )
                if value is None
            ]
            if missing:
                log.warning(
                    "stage2_submit_collaborator_unavailable: missing=%s",
                    missing,
                    extra={
                        "event": "stage2_submit_collaborator_unavailable",
                        "src_module": "news_daemon.resilience",
                        "missing": list(missing),
                        "reason": "collaborator_none",
                    },
                )

            dispatch_after_poll_cycle(
                db_path,
                log=log,
                armed_path=overrides.get("armed_path"),
                providers=overrides.get("providers"),
                submit_fn=submit_fn,
                market_open_check=market_open_check,
                chain_quote_fn=chain_quote_fn,
                paper_executor=paper_executor_instance,
            )
        except Exception as exc:  # noqa: BLE001 - resilience hook
            # A defect in the dispatcher MUST NOT halt the daemon;
            # the next cycle gets another chance.  ``errors_session``
            # increments so the watchdog signal mirrors any other
            # cycle-level failure.
            state.errors_session += 1
            log.exception(
                "news_daemon_stage2_dispatch_error: err=%r",
                exc,
                extra={
                    "event": "news_daemon_stage2_dispatch_error",
                    "src_module": "news_daemon.resilience",
                    "error_repr": repr(exc),
                },
            )

        state.cycles_completed += 1

        # f-misc-08: per-cycle INFO telemetry — promoted from the
        # DEBUG-level ``news_daemon_emit_cycle`` so production
        # ``LOG_LEVEL=INFO`` runs surface one structured progress
        # record per poll cycle (was previously suppressed, leaving
        # only the once-per-session ``news_daemon_loop_started`` /
        # ``news_daemon_loop_drained`` INFO lines).  Field count
        # kept tight (six fields, all int) to avoid payload bloat
        # under the 4 KB per-line cap and the systemd
        # ``MemoryMax=200M`` envelope.
        cycle_duration_ms = int(
            max(0.0, monotonic() - cycle_started_at) * 1000
        )
        log.info(
            "news_daemon_poll_cycle_complete: cycles=%d candidates=%d "
            "errors=%d duration_ms=%d polled=%d scanned=%d",
            state.cycles_completed,
            state.candidates_emitted_session,
            state.errors_session,
            cycle_duration_ms,
            cycle_polled_ticker_count,
            cycle_news_events_scanned,
            extra={
                "event": "news_daemon_poll_cycle_complete",
                "src_module": "news_daemon.resilience",
                "cycles_completed": int(state.cycles_completed),
                "candidates_emitted_session": int(
                    state.candidates_emitted_session
                ),
                "errors_session": int(state.errors_session),
                "duration_ms": int(cycle_duration_ms),
                "polled_ticker_count": int(cycle_polled_ticker_count),
                "news_events_scanned": int(cycle_news_events_scanned),
            },
        )

        if max_cycles and state.cycles_completed >= max_cycles:
            break
        if state.shutdown:
            break

        # Account for the cycle's wall-time so cadence stays close to
        # the configured poll_seconds.  Negative remaining means the
        # cycle ran longer than the cadence — proceed immediately.
        elapsed = monotonic() - cycle_started_at
        sleep_for = max(0.0, float(poll_seconds) - float(elapsed))
        _interruptible_sleep(
            sleep_for,
            state,
            sleep_func=sleep_func,
            monotonic=monotonic,
            poll_interval=shutdown_poll_seconds,
        )

    # Graceful drain — final heartbeat (so the watchdog never sees a
    # stale file after a clean SIGTERM exit per VAL-M2-052).
    _flush_heartbeat(
        state,
        db_path,
        resolved_heartbeat,
        resolved_version,
        log,
    )

    log.info(
        "news_daemon_loop_drained: cycles=%d errors=%d candidates=%d "
        "signal=%s",
        state.cycles_completed,
        state.errors_session,
        state.candidates_emitted_session,
        state.signal_received,
        extra={
            "event": "news_daemon_loop_drained",
            "src_module": "news_daemon.resilience",
            "cycles_completed": int(state.cycles_completed),
            "errors_session": int(state.errors_session),
            "candidates_emitted_session": int(state.candidates_emitted_session),
            "signal_received": state.signal_received,
        },
    )
    return 0
