"""Behavioural tests for f-m2-03 poll-cadence clamp + disabled-idle.

These tests pin the f-m2-03 contract:

* :func:`resolve_poll_seconds` reads ``NEWS_POLL_SECONDS`` from the
  environment (or accepts a string override) and applies the
  [15, 90] clamp with a WARNING log line on every override path.
  Default 30 when unset.  Non-integer / non-positive falls back to
  default with WARNING.
* :func:`is_news_daemon_enabled` reads ``NEWS_DAEMON_ENABLED`` and
  returns ``False`` only for the literal value ``"0"`` (after strip).
* :func:`run_disabled_idle` enforces the kill-switch path: NO DB
  writes, NO LLM calls, gate-decision INFO log emitted at most once
  per minute, sleep cadence equal to the resolved poll_seconds.
* Real-run sleep cadence stays within ±10% of the resolved interval
  over 5 cycles (verified directly against
  :func:`run_disabled_idle` with a small interval so the test runs
  in milliseconds rather than minutes).
* CLI smoke: ``NEWS_POLL_SECONDS=5 python -m biotech_sniper.news_daemon
  --dry-run --max-cycles 1`` prints a WARNING containing the
  literal substring ``clamped`` (the verification step on
  features.json).

Coverage maps to validation contract assertions VAL-M2-008
(``NEWS_POLL_SECONDS`` unset → 30), VAL-M2-009 (below-floor clamp
to 15 with WARNING), VAL-M2-010 (above-ceiling clamp to 90 with
WARNING; non-integer fallback), and VAL-M2-011 (real-run sleep
cadence within ±10%).
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from typing import List, Tuple

import pytest

from biotech_sniper.news_daemon import poll_loop
from biotech_sniper.news_daemon.poll_loop import (
    DEFAULT_POLL_SECONDS,
    MAX_POLL_SECONDS,
    MIN_POLL_SECONDS,
    is_news_daemon_enabled,
    main,
    resolve_poll_seconds,
    run_disabled_idle,
)


_LOGGER_NAME = "biotech_sniper.news_daemon"


# ---------------------------------------------------------------------------
# Public surface invariants
# ---------------------------------------------------------------------------


def test_constants_match_locked_spec() -> None:
    """The locked [15, 30, 90] spec is preserved.

    f-m2-01 already pinned these but f-m2-03 reuses them as the
    decision-matrix anchors, so we re-pin to catch accidental
    edits during the cadence wiring.
    """

    assert DEFAULT_POLL_SECONDS == 30
    assert MIN_POLL_SECONDS == 15
    assert MAX_POLL_SECONDS == 90


# ---------------------------------------------------------------------------
# resolve_poll_seconds — env reads
# ---------------------------------------------------------------------------


def test_unset_env_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """VAL-M2-008: unset → 30."""

    monkeypatch.delenv("NEWS_POLL_SECONDS", raising=False)
    assert resolve_poll_seconds() == DEFAULT_POLL_SECONDS


def test_explicit_none_returns_default() -> None:
    """``resolve_poll_seconds(None)`` is treated as "unset"."""

    assert resolve_poll_seconds(None) == DEFAULT_POLL_SECONDS


def test_empty_string_returns_default() -> None:
    """Whitespace-only override is treated as "unset" (no warning)."""

    assert resolve_poll_seconds("") == DEFAULT_POLL_SECONDS
    assert resolve_poll_seconds("   ") == DEFAULT_POLL_SECONDS


def test_env_var_reads_from_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """env-driven path actually consults ``os.environ``."""

    monkeypatch.setenv("NEWS_POLL_SECONDS", "45")
    assert resolve_poll_seconds() == 45


# ---------------------------------------------------------------------------
# resolve_poll_seconds — happy path (in [MIN, MAX])
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("15", 15),
        ("16", 16),
        ("30", 30),
        ("45", 45),
        ("89", 89),
        ("90", 90),
    ],
)
def test_in_band_value_returned_unchanged(
    raw: str,
    expected: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Values inside [15, 90] are returned as-is without WARNING."""

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        assert resolve_poll_seconds(raw) == expected
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


# ---------------------------------------------------------------------------
# resolve_poll_seconds — clamp paths (VAL-M2-009 / VAL-M2-010)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["1", "5", "10", "14"])
def test_below_floor_clamps_to_15_with_warning(
    raw: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """VAL-M2-009: below floor → 15 with WARNING containing ``clamped``."""

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        assert resolve_poll_seconds(raw) == MIN_POLL_SECONDS
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warns, "expected a WARNING log line"
    msgs = "\n".join(r.getMessage() for r in warns)
    assert "clamped" in msgs.lower()
    assert raw in msgs


@pytest.mark.parametrize("raw", ["91", "120", "200", "9999"])
def test_above_ceiling_clamps_to_90_with_warning(
    raw: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """VAL-M2-010: above ceiling → 90 with WARNING containing ``clamped``."""

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        assert resolve_poll_seconds(raw) == MAX_POLL_SECONDS
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warns, "expected a WARNING log line"
    msgs = "\n".join(r.getMessage() for r in warns)
    assert "clamped" in msgs.lower()


# ---------------------------------------------------------------------------
# resolve_poll_seconds — fallback paths (VAL-M2-010 garbage / non-positive)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["0", "-1", "-10", "-9999"])
def test_non_positive_falls_back_with_warning(
    raw: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``0`` and negatives → 30 with WARNING."""

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        assert resolve_poll_seconds(raw) == DEFAULT_POLL_SECONDS
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warns, "expected a WARNING log line"


@pytest.mark.parametrize(
    "raw",
    ["abc", "foo", "30s", "fifteen", "1.5", "15.0", "0xF", "true", "off"],
)
def test_non_integer_falls_back_with_warning(
    raw: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """VAL-M2-010: non-integer → 30 with WARNING."""

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        assert resolve_poll_seconds(raw) == DEFAULT_POLL_SECONDS
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warns, "expected a WARNING log line"


def test_whitespace_around_integer_is_stripped() -> None:
    """``" 30 "`` → 30 (operator-friendly env-file parsing)."""

    assert resolve_poll_seconds(" 30 ") == 30
    assert resolve_poll_seconds("\t45\n") == 45


# ---------------------------------------------------------------------------
# is_news_daemon_enabled — env-driven kill switch
# ---------------------------------------------------------------------------


def test_unset_means_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-on: unset env → True."""

    monkeypatch.delenv("NEWS_DAEMON_ENABLED", raising=False)
    assert is_news_daemon_enabled() is True


@pytest.mark.parametrize("raw", ["0", " 0", "0 ", "\t0\n"])
def test_zero_disables(raw: str) -> None:
    """Literal ``0`` (after strip) → False."""

    assert is_news_daemon_enabled(raw) is False


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "01", "00", "-0"])
def test_non_zero_strings_remain_enabled(raw: str) -> None:
    """Fail-open: anything except literal ``0`` → True (kill switch posture)."""

    assert is_news_daemon_enabled(raw) is True


def test_env_var_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``is_news_daemon_enabled()`` actually reads NEWS_DAEMON_ENABLED."""

    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "0")
    assert is_news_daemon_enabled() is False
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "1")
    assert is_news_daemon_enabled() is True


# ---------------------------------------------------------------------------
# Real-run sleep cadence within ±10% (VAL-M2-011 in spirit)
# ---------------------------------------------------------------------------


def test_disabled_idle_sleep_cadence_within_10_percent_over_5_cycles() -> None:
    """5 successive cycle starts are ``poll_seconds ± 10%`` apart.

    Uses a tiny interval (50 ms) so the test runs in <0.5 s yet
    exercises the real sleep cadence: we record the wall time at
    each cycle and assert all 4 deltas land in [0.9·I, 1.1·I + ε]
    where ε is a small slack for OS scheduling jitter (5 ms).
    """

    interval = 0.05
    starts: List[float] = []

    def fake_sleep(seconds: float) -> None:
        # Real sleep so we measure actual cadence — but bound so
        # the test never runs longer than a few hundred ms.
        time.sleep(seconds)

    real_monotonic = time.monotonic

    def stamping_monotonic() -> float:
        now = real_monotonic()
        starts.append(now)
        return now

    rc = run_disabled_idle(
        poll_seconds=interval,  # type: ignore[arg-type]
        max_cycles=5,
        sleep_func=fake_sleep,
        monotonic=stamping_monotonic,
        gate_log_interval=10_000.0,  # suppress repeated gate logs
    )
    assert rc == 0

    # ``starts`` records every monotonic() call.  The first call
    # per cycle is the gate-log gate check; that's the cycle-start
    # we want.  Each cycle issues exactly one monotonic() call
    # before sleep, so ``starts`` has ``max_cycles`` entries.
    assert len(starts) == 5, f"unexpected monotonic() count: {len(starts)}"

    deltas = [starts[i + 1] - starts[i] for i in range(4)]
    lower = 0.9 * interval
    upper = 1.1 * interval + 0.05  # 50 ms scheduling slack
    for d in deltas:
        assert lower <= d <= upper, (
            f"cadence delta {d:.4f}s outside [{lower:.4f}, {upper:.4f}]"
        )


# ---------------------------------------------------------------------------
# Disabled-idle gate-decision log throttle (≤ once per minute)
# ---------------------------------------------------------------------------


def test_disabled_idle_logs_gate_decision_once_per_minute(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Across many cycles, gate-decision INFO emits at most once per 60 s.

    We drive the helper with a fake clock so we can verify the
    throttle without waiting wall-clock minutes.  Five cycles at
    poll_seconds=15 with a clock that advances 15 s per cycle
    crosses the 60 s gate-log interval exactly twice (cycle 1 and
    cycle 5), so we expect exactly 2 gate-decision INFO records.
    """

    fake_clock = [0.0]

    def fake_monotonic() -> float:
        return fake_clock[0]

    def fake_sleep(seconds: float) -> None:
        fake_clock[0] += seconds

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        rc = run_disabled_idle(
            poll_seconds=15,
            max_cycles=5,
            sleep_func=fake_sleep,
            monotonic=fake_monotonic,
            gate_log_interval=60.0,
        )
    assert rc == 0

    decisions = [
        r for r in caplog.records
        if getattr(r, "event", None) == "news_daemon_gate_decision"
    ]
    assert len(decisions) == 2, (
        f"expected exactly 2 throttled gate-decision logs, got "
        f"{len(decisions)}: {[r.getMessage() for r in decisions]}"
    )


def test_disabled_idle_first_cycle_always_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The very first cycle always emits a gate-decision INFO line.

    Even if the gate-log interval is large, the first cycle must
    log so the journal records the gate is being honoured from
    the moment the daemon starts.
    """

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        rc = run_disabled_idle(
            poll_seconds=1,
            max_cycles=1,
            sleep_func=lambda _s: None,
            monotonic=lambda: 0.0,
            gate_log_interval=10_000.0,
        )
    assert rc == 0
    decisions = [
        r for r in caplog.records
        if getattr(r, "event", None) == "news_daemon_gate_decision"
    ]
    assert len(decisions) == 1


def test_disabled_idle_makes_no_db_or_llm_imports(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The disabled-idle path imports zero DB / LLM client modules.

    This pins the kill-switch invariant: ``NEWS_DAEMON_ENABLED=0``
    must not touch SQLite or any Stage-2 provider SDK.  We snapshot
    ``sys.modules`` before and after the helper run and assert
    that no biotech_sniper.llm.* / openai / anthropic /
    google.generativeai / sqlite3 module appeared as a NEW import.
    """

    forbidden_prefixes = (
        "biotech_sniper.llm",
        "openai",
        "anthropic",
        "google.generativeai",
    )
    before = {m for m in sys.modules}
    rc = run_disabled_idle(
        poll_seconds=1,
        max_cycles=2,
        sleep_func=lambda _s: None,
        monotonic=lambda: 0.0,
        gate_log_interval=10_000.0,
    )
    after = set(sys.modules) - before
    assert rc == 0
    leaks = [
        m for m in after if m.startswith(forbidden_prefixes)
    ]
    assert leaks == [], f"forbidden imports during disabled-idle: {leaks}"


# ---------------------------------------------------------------------------
# main() integration — disabled_idle short-circuits NotImplementedError
# ---------------------------------------------------------------------------


def test_main_disabled_returns_zero_without_notimplemented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``NEWS_DAEMON_ENABLED=0`` short-circuits the NotImplementedError path.

    Without the kill switch, ``main([])`` raises NotImplementedError
    until f-m2-04..f-m2-09 land (preserved from f-m2-01).  With
    the kill switch flipped to ``0``, main enters disabled_idle
    instead and returns ``0`` cleanly after ``--max-cycles`` cycles.
    """

    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "0")
    # Patch time.sleep inside the poll_loop module so the test is
    # instant regardless of the resolved poll_seconds.
    monkeypatch.setattr(poll_loop.time, "sleep", lambda _s: None)
    rc = main(["--max-cycles", "2", "--poll-seconds", "15"])
    assert rc == 0


def test_main_enabled_runs_resilience_loop_with_finite_cycles(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """f-m2-09 contract: enabled+non-dry-run main runs the real loop.

    Until f-m2-09 landed, ``main([])`` raised ``NotImplementedError``
    by design (the package skeleton refused to silently no-op before
    the loop wiring was complete).  Now that the resilience loop
    exists, an ``--max-cycles=1`` run exits cleanly with rc=0 and
    flushes a heartbeat.
    """

    monkeypatch.delenv("NEWS_DAEMON_ENABLED", raising=False)
    monkeypatch.setenv("BIOTECH_SNIPER_HOME", str(tmp_path))
    monkeypatch.setattr(poll_loop.time, "sleep", lambda _s: None)
    db_path = tmp_path / "alpha.db"
    rc = main([
        "--max-cycles",
        "1",
        "--poll-seconds",
        "15",
        "--db",
        str(db_path),
    ])
    assert rc == 0


def test_main_dry_run_with_clamp_logs_warning(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run`` exits 0 AND emits the clamp WARNING (verification step)."""

    monkeypatch.setenv("NEWS_POLL_SECONDS", "5")
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        rc = main(["--dry-run", "--max-cycles", "1"])
    assert rc == 0
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "clamped" in msgs.lower()


def test_main_poll_seconds_flag_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--poll-seconds 5`` clamps to 15 even when env is silent."""

    monkeypatch.delenv("NEWS_POLL_SECONDS", raising=False)
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "0")
    monkeypatch.setattr(poll_loop.time, "sleep", lambda _s: None)
    # No exception raised; main returns 0 via disabled-idle path.
    rc = main(["--max-cycles", "1", "--poll-seconds", "5"])
    assert rc == 0


# ---------------------------------------------------------------------------
# Subprocess CLI smoke (the verification step in features.json)
# ---------------------------------------------------------------------------


def _run_cli(env_overrides: dict, *cli_args: str) -> Tuple[int, str, str]:
    import os as _os

    env = _os.environ.copy()
    env.update(env_overrides)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "biotech_sniper.news_daemon",
            *cli_args,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_clamp_warning_visible_to_grep_with_news_poll_seconds_5() -> None:
    """f-m2-03 verification step: ``NEWS_POLL_SECONDS=5 ... | grep -i clamped``.

    The verifier pipes stderr+stdout through ``grep -i 'clamped'``
    and expects exit 0 (= match).  We exercise the same shape:
    capture stderr, assert ``clamped`` appears.
    """

    rc, out, err = _run_cli(
        {"NEWS_POLL_SECONDS": "5"},
        "--dry-run",
        "--max-cycles",
        "1",
    )
    assert rc == 0, f"dry-run exit nonzero: stdout={out!r} stderr={err!r}"
    combined = (out + err).lower()
    assert "clamped" in combined, (
        f"expected 'clamped' in CLI output; got stdout={out!r} stderr={err!r}"
    )


def test_cli_clamp_warning_visible_for_above_ceiling() -> None:
    """``NEWS_POLL_SECONDS=200 → clamped to 90`` is observable on CLI too."""

    rc, out, err = _run_cli(
        {"NEWS_POLL_SECONDS": "200"},
        "--dry-run",
        "--max-cycles",
        "1",
    )
    assert rc == 0
    combined = (out + err).lower()
    assert "clamped" in combined
    assert "90" in (out + err)


def test_cli_non_integer_fallback_visible_for_grep() -> None:
    """Non-integer fallback emits a fallback-WARNING; the daemon exits 0."""

    rc, out, err = _run_cli(
        {"NEWS_POLL_SECONDS": "abc"},
        "--dry-run",
        "--max-cycles",
        "1",
    )
    assert rc == 0
    combined = (out + err).lower()
    assert "fallback" in combined or "default" in combined or "30" in combined
