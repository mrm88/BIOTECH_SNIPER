"""Tests for ``news_daemon_poll_cycle_complete`` per-cycle INFO event.

f-misc-08-promote-poll-cycle-event-to-info — telemetry refinement.

The Stage-1 news daemon currently logs only ``news_daemon_loop_started``
and ``news_daemon_loop_drained`` at INFO level (session boundaries).
At the production default ``LOG_LEVEL=INFO`` this means a 70-second
runtime produces only 1-2 INFO log lines, making
``VAL-M4-017``-style ``tail -n 200 all-JSON`` assertions painfully
slow to validate. f-misc-08 promotes ONE per-cycle event from DEBUG
to INFO with a tight, structured payload — six fields, no body
bloat, one record per cycle:

* ``cycles_completed`` — running count of cycles run in this session.
* ``candidates_emitted_session`` — running count of candidates emitted
  in this session.
* ``errors_session`` — running count of errors observed in this
  session (RSS + poll-body combined).
* ``duration_ms`` — wall-time of the cycle that just finished.
* ``polled_ticker_count`` — size of the
  ``russell2k_biotech ∩ universe.tier`` set resolved for the cycle.
* ``news_events_scanned`` — count of ``news_events`` rows examined
  by ``run_one_poll_cycle`` during the cycle (includes non-matching
  rows; ``inserted`` is captured separately via the running
  ``candidates_emitted_session`` counter).

The four cases mandated by the feature description:

(a) one INFO ``news_daemon_poll_cycle_complete`` line per cycle in
    a dry-run-style invocation;
(b) all six required fields present and typed correctly on the
    structured payload;
(c) sustained 5-cycle dry-run produces exactly 5 such INFO lines;
(d) at ``LOG_LEVEL=DEBUG`` the line still appears (no regression
    from suppression at the more permissive level).

The tests drive :func:`biotech_sniper.news_daemon.resilience.run_main_loop`
directly with a ``_FakeClock`` so cycles run in milliseconds, and
read the on-disk ``news.log`` produced by
:func:`biotech_sniper.news_daemon.log.configure_news_logging` so the
assertions exercise the same JSON-formatter pipeline production
records flow through.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator, List

import pytest

from biotech_sniper.news_daemon import resilience
from biotech_sniper.news_daemon.log import (
    configure_news_logging,
    get_news_logger,
)
from biotech_sniper.news_daemon.resilience import ShutdownState, run_main_loop


POLL_CYCLE_EVENT: str = "news_daemon_poll_cycle_complete"
REQUIRED_FIELDS: tuple[str, ...] = (
    "cycles_completed",
    "candidates_emitted_session",
    "errors_session",
    "duration_ms",
    "polled_ticker_count",
    "news_events_scanned",
)


# ---------------------------------------------------------------------------
# Fixtures — restore the news-daemon logger across tests so handlers
# installed by configure_news_logging do not leak into the rest of the
# suite (mirrors tests/test_news_daemon_logging_wiring.py).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_news_logger_state() -> Iterator[None]:
    logger = get_news_logger()
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    saved_propagate = logger.propagate
    try:
        yield
    finally:
        for handler in list(logger.handlers):
            if handler not in saved_handlers:
                try:
                    handler.close()
                except Exception:
                    pass
                logger.removeHandler(handler)
        for handler in saved_handlers:
            if handler not in logger.handlers:
                logger.addHandler(handler)
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


class _FakeClock:
    """Deterministic monotonic + sleep — mirrors the resilience suite."""

    def __init__(self) -> None:
        self._now: float = 0.0

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._now += float(seconds)


def _read_poll_cycle_records(log_path: Path) -> List[dict]:
    """Return the JSON-decoded ``news_daemon_poll_cycle_complete`` lines."""

    # Flush all handlers so the file contents land before we read.
    logger = get_news_logger()
    for handler in logger.handlers:
        try:
            handler.flush()
        except Exception:
            pass

    if not log_path.exists():
        return []
    content = log_path.read_text(encoding="utf-8")
    records: List[dict] = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            pytest.fail(f"news.log line is not JSON: {line!r} (err={exc!r})")
        if parsed.get("event") == POLL_CYCLE_EVENT:
            records.append(parsed)
    return records


def _run_loop_with_log(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cycles: int,
    log_level: int,
    db_path: Path | None = None,
) -> tuple[ShutdownState, Path]:
    """Configure news logging to a tmp ``news.log`` and drive ``run_main_loop``.

    Uses a non-existent SQLite path when ``db_path`` is omitted so the
    poll cycle short-circuits via ``resolve_polled_tickers`` raising
    (caught by the resilience loop and counted as an error) — the
    cycle still completes and the per-cycle INFO event is still
    emitted, which is the exact behaviour the assertions verify.
    The ``news_events_scanned`` count is therefore ``0`` on the
    error path and equals the real scan count on the happy path.
    """

    log_path = tmp_path / "news.log"
    monkeypatch.setenv("ALPHA_SNIPER_NEWS_LOG_PATH", str(log_path))
    configure_news_logging(level=log_level)

    state = ShutdownState()
    fake_clock = _FakeClock()
    resolved_db = db_path if db_path is not None else tmp_path / "missing.db"

    def _noop_fetcher() -> None:  # zero-failure RSS source
        return None

    rc = run_main_loop(
        resolved_db,
        poll_seconds=1,
        max_cycles=cycles,
        rss_fetchers=[_noop_fetcher],
        state=state,
        install_handlers=False,
        sleep_func=fake_clock.sleep,
        monotonic=fake_clock.monotonic,
        heartbeat_path=tmp_path / "state" / "heartbeat.json",
        version_sha="c" * 40,
    )
    assert rc == 0
    return state, log_path


# ---------------------------------------------------------------------------
# (a) one INFO news_daemon_poll_cycle_complete line per cycle
# ---------------------------------------------------------------------------


def test_one_info_line_per_cycle_at_info_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single 1-cycle run emits exactly one poll-cycle INFO line."""

    state, log_path = _run_loop_with_log(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        cycles=1,
        log_level=logging.INFO,
    )
    assert state.cycles_completed == 1

    records = _read_poll_cycle_records(log_path)
    assert len(records) == 1, (
        f"Expected exactly 1 {POLL_CYCLE_EVENT} INFO record after "
        f"a 1-cycle run; got {len(records)}: {records!r}"
    )
    assert records[0]["level"] == "INFO", records[0]


# ---------------------------------------------------------------------------
# (b) all six required fields present and typed correctly
# ---------------------------------------------------------------------------


def test_required_fields_present_and_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All six fields from the f-misc-08 contract are present + typed."""

    _, log_path = _run_loop_with_log(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        cycles=1,
        log_level=logging.INFO,
    )
    records = _read_poll_cycle_records(log_path)
    assert len(records) == 1, records
    payload = records[0]

    for field in REQUIRED_FIELDS:
        assert field in payload, (
            f"Missing required field {field!r} on "
            f"{POLL_CYCLE_EVENT}: {payload!r}"
        )
        value = payload[field]
        assert isinstance(value, int) and not isinstance(value, bool), (
            f"Field {field!r} must be int (got {type(value).__name__}: "
            f"{value!r})"
        )
        # All six fields are non-negative running counters / measurements.
        assert value >= 0, (
            f"Field {field!r} must be non-negative; got {value!r}"
        )


# ---------------------------------------------------------------------------
# (c) sustained 5-cycle run emits exactly 5 INFO lines
# ---------------------------------------------------------------------------


def test_five_cycles_emit_exactly_five_info_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 5-cycle run produces exactly 5 ``poll_cycle_complete`` records."""

    state, log_path = _run_loop_with_log(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        cycles=5,
        log_level=logging.INFO,
    )
    assert state.cycles_completed == 5

    records = _read_poll_cycle_records(log_path)
    assert len(records) == 5, (
        f"Expected exactly 5 {POLL_CYCLE_EVENT} INFO records after "
        f"a 5-cycle run; got {len(records)}: cycles_completed values="
        f"{[r.get('cycles_completed') for r in records]!r}"
    )
    # ``cycles_completed`` should be monotonically increasing 1..5.
    assert [r["cycles_completed"] for r in records] == [1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# (d) LOG_LEVEL=DEBUG still surfaces the line (no regression)
# ---------------------------------------------------------------------------


def test_event_visible_at_debug_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At ``LOG_LEVEL=DEBUG`` the same INFO line still appears.

    DEBUG is more permissive than INFO so an INFO record is always
    surfaced.  This test guards against an accidental future change
    that suppresses ``news_daemon_poll_cycle_complete`` only at the
    INFO call-site (e.g., gating it behind an ``if effective_level
    <= INFO`` guard) — the line MUST surface at DEBUG too.
    """

    _, log_path = _run_loop_with_log(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        cycles=2,
        log_level=logging.DEBUG,
    )
    records = _read_poll_cycle_records(log_path)
    assert len(records) == 2, (
        f"Expected 2 {POLL_CYCLE_EVENT} records at DEBUG; "
        f"got {len(records)}: {records!r}"
    )
    for record in records:
        # Even at DEBUG, the level on the record itself must remain
        # INFO — promoting from DEBUG → INFO means the call-site
        # MUST log at INFO (verified by the level field, not just
        # whether the record is present at all).
        assert record["level"] == "INFO", record
        # All required fields still present at DEBUG.
        for field in REQUIRED_FIELDS:
            assert field in record, (field, record)


# ---------------------------------------------------------------------------
# Sanity / wiring tests — these guard against the contract drifting
# unintentionally even if the four explicit cases above continue to pass.
# ---------------------------------------------------------------------------


def test_event_emitted_on_resilience_logger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The event is emitted on the news_daemon.resilience logger.

    The resilience module is the natural owner of cycle-level
    telemetry; this test pins the call-site so future refactors do
    not silently move the emit to a less canonical location.
    """

    _, log_path = _run_loop_with_log(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        cycles=1,
        log_level=logging.INFO,
    )
    records = _read_poll_cycle_records(log_path)
    assert len(records) == 1, records
    payload = records[0]
    # The ``module`` key is the canonical news-daemon module marker.
    assert payload.get("module") in {
        "news_daemon.resilience",
        "biotech_sniper.news_daemon.resilience",
        "resilience",
    }, payload


def test_duration_ms_reflects_cycle_wall_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``duration_ms`` is a non-negative integer (no negative skew)."""

    _, log_path = _run_loop_with_log(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        cycles=3,
        log_level=logging.INFO,
    )
    records = _read_poll_cycle_records(log_path)
    assert len(records) == 3, records
    for record in records:
        duration = record["duration_ms"]
        assert isinstance(duration, int)
        assert duration >= 0, duration
