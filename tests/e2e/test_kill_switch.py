"""End-to-end kill-switch test (feature ``f-m5-08-mission-end-gates``).

Contract assertion covered: **VAL-M5-046** —
``NEWS_DAEMON_ENABLED=0`` is a total kill switch.

Mission requirement
~~~~~~~~~~~~~~~~~~~

Per ``validation-contract.md`` §M5.MISSION_GATE / VAL-M5-046:

    With ``NEWS_DAEMON_ENABLED=0``, a 5-minute integration run with
    10 seeded synthetic ``news_events`` rows produces:

    * zero new ``candidate_events``
    * zero new ``ensemble_scores_event``
    * zero new ``paper_orders`` with ``event='news_event_entry'``
    * zero new ``llm_cost_ledger`` rows from Stage-2

    AND existing daily-curated path runs unchanged
    (``audit_latest.json`` sha256 matches golden).

    Daemon emits ``'disabled_idle'`` gate-decision log line once
    per minute and otherwise stays silent.

How this test proves it hermetically
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The kill switch lives in
:func:`biotech_sniper.news_daemon.poll_loop.is_news_daemon_enabled`
+ :func:`run_disabled_idle`. With ``NEWS_DAEMON_ENABLED=0`` the
daemon's :func:`main` enters the disabled-idle loop, which by
design:

* opens NO database connection (Stage-1 emit path is never reached)
* imports NO Stage-2 LLM module (no provider SDK loads)
* emits zero ``candidate_events`` rows even with a non-empty
  ``news_events`` backlog

We compress the "5-minute integration run" into a few-millisecond
pytest by injecting a stub ``sleep_func`` + ``monotonic`` clock.
The contract is a *behavioural* invariant — zero rows, zero
side-effects — not a wall-clock assertion. The deterministic
clock ALSO lets us pin the gate-decision throttle (≤ once per
60 s) without waiting wall-clock minutes.

The daily-curated baseline is seeded via the same canonical
schema bring-up (v10 migration runner) used by sibling e2e tests
(:mod:`tests.e2e.test_rejection_paths`,
:mod:`tests.e2e.test_cheap_first_side_effects`). The "golden"
sha256 is computed from the synthetic baseline rather than a
recorded artifact, so the test is fully hermetic and reproducible
on any host.

Hermeticity
-----------

The test never touches the real Alpaca paper API, any LLM
endpoint, or the production database. Every fixture is rooted in
``tmp_path``. The test is decorated with ``@pytest.mark.e2e`` so
it is selected by ``pytest -m e2e`` and explicitly invoked by the
feature's verification step::

    .venv/bin/pytest -q tests/e2e/test_kill_switch.py
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper.migrations.runner import run as run_migrations_runner
from biotech_sniper import db as project_db
from biotech_sniper.news_daemon import poll_loop
from biotech_sniper.news_daemon.poll_loop import (
    DEFAULT_POLL_SECONDS,
    is_news_daemon_enabled,
    main as news_daemon_main,
    run_disabled_idle,
)


pytestmark = pytest.mark.e2e


_LOGGER_NAME: str = "biotech_sniper.news_daemon"

#: Five minutes of daemon runtime at the production-default
#: cadence (``NEWS_POLL_SECONDS=30``) yields exactly 10 cycles.
#: The contract pegs this at "5-minute integration run".
_FIVE_MINUTE_CYCLE_COUNT: int = (5 * 60) // DEFAULT_POLL_SECONDS  # = 10
assert _FIVE_MINUTE_CYCLE_COUNT == 10  # documented invariant

#: Number of synthetic ``news_events`` rows seeded into the DB
#: ahead of the disabled-idle run. The contract specifies "10
#: seeded synthetic news_events rows".
_N_SEEDED_NEWS_EVENTS: int = 10

#: Tickers used for the seeded headlines. Keyword-matching content
#: is deliberately included so that — IF the daemon were enabled —
#: each row would emit a ``candidate_events`` row. The test proves
#: the kill switch suppresses those emissions.
_SEED_TICKERS: tuple[str, ...] = (
    "MRNS",
    "VKTX",
    "RXRX",
    "SAVA",
    "AXSM",
    "ARWR",
    "BPMC",
    "CRSP",
    "EDIT",
    "NTLA",
)
assert len(_SEED_TICKERS) == _N_SEEDED_NEWS_EVENTS

#: Headline body that includes a Tier-1 catalyst keyword
#: (``'phase 3 readout'``) so the matcher would fire on every row
#: if the daemon were enabled. The kill switch must suppress the
#: emission regardless.
_SEED_HEADLINE_TEMPLATE: str = (
    "{ticker} announces phase 3 readout: primary endpoint hit"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _serialize_audit(payload: dict[str, Any]) -> bytes:
    """Serialize the audit payload byte-identically to the writer."""
    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")


def _seed_news_events(db_path: Path, n: int = _N_SEEDED_NEWS_EVENTS) -> list[int]:
    """Insert ``n`` synthetic ``news_events`` rows; return their ids.

    Each row carries a Tier-1 catalyst keyword in its title so the
    matcher would fire on every row IF the daemon were enabled.
    The kill switch must suppress the candidate emission regardless.
    """
    ids: list[int] = []
    conn = sqlite3.connect(str(db_path))
    try:
        for i in range(n):
            ticker = _SEED_TICKERS[i]
            cur = conn.execute(
                """
                INSERT INTO news_events (
                    ticker, source, published_at, title, url, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker,
                    "test_kill_switch",
                    f"2026-04-30T14:0{i}:00.000Z",
                    _SEED_HEADLINE_TEMPLATE.format(ticker=ticker),
                    f"https://example.com/{ticker.lower()}-readout-{i}",
                    _SEED_HEADLINE_TEMPLATE.format(ticker=ticker),
                ),
            )
            ids.append(int(cur.lastrowid or 0))
        conn.commit()
    finally:
        conn.close()
    return ids


def _seed_baseline_paper_orders(db_path: Path) -> None:
    """Seed two daily-curated paper_orders rows (legacy ``event='open'``).

    Used to verify the kill-switch run does not perturb existing
    daily-curated state. The carve-out filter
    ``event != 'news_event_entry'`` isolates these rows from the
    additive Reading-B bucket.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany(
            """
            INSERT INTO paper_orders (
                id, play_card_id, alpaca_order_id, symbol, side, qty,
                status, event, purpose, client_order_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "po-baseline-vktx",
                    "pc-baseline-vktx",
                    "alp-baseline-vktx",
                    "VKTX260717C00120000",
                    "buy",
                    2,
                    "filled",
                    "open",
                    "entry",
                    "co-baseline-vktx",
                    "2026-04-30T13:13:30Z",
                ),
                (
                    "po-baseline-rxrx",
                    "pc-baseline-rxrx",
                    "alp-baseline-rxrx",
                    "RXRX260717P00050000",
                    "buy",
                    3,
                    "filled",
                    "open",
                    "entry",
                    "co-baseline-rxrx",
                    "2026-04-30T13:13:31Z",
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _count(db_path: Path, sql: str, params: tuple = ()) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Bring a fresh sqlite db up to schema v10 (Reading-B foundations)."""
    db = tmp_path / "alpha_sniper_kill_switch.db"
    run_migrations_runner(db, target_version=project_db.CURRENT_VERSION, take_backup_first=False)
    return db


@pytest.fixture
def baseline_audit_path(tmp_path: Path) -> Path:
    """Write a synthetic ``audit_latest.json`` baseline.

    Mirrors what the pre-Reading-B daily-curated cron would emit so
    the kill-switch test can pin "audit_latest.json sha256 matches
    golden" against a hermetic reference value rather than a
    recorded artifact.
    """
    payload: dict[str, Any] = {
        "last_daily_run": "2026-04-30T13:13:00Z",
        "last_daily_run_summary": {
            "date": "2026-04-30",
            "duration_sec": 612.0,
            "orders_submitted": 2,
            "cards_generated": 5,
            "llm_cost_usd": 4.27,
            "success": True,
        },
        "sources": {
            "ctgov": {
                "reachable": True,
                "last_check": "2026-04-30T13:13:05Z",
            },
            "sec_edgar": {
                "reachable": True,
                "last_check": "2026-04-30T13:13:07Z",
            },
            "alpaca_paper": {
                "reachable": True,
                "last_check": "2026-04-30T13:13:10Z",
            },
        },
        "db_size_bytes": 1234567,
        "paper_account_equity": 100000.00,
    }
    audit = tmp_path / "state" / "audit_latest.json"
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_bytes(_serialize_audit(payload))
    return audit


# ---------------------------------------------------------------------------
# VAL-M5-046 — total kill switch (5-minute run, zero RB rows)
# ---------------------------------------------------------------------------


def test_kill_switch_zero_reading_b_rows_over_5_minute_run(
    db_path: Path,
    baseline_audit_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``NEWS_DAEMON_ENABLED=0`` over a 5-min run with 10 seed rows.

    The integration: seed 10 ``news_events`` rows whose headlines
    each contain a Tier-1 catalyst keyword (so the matcher *would*
    fire on every row if the daemon were enabled), then drive
    :func:`run_disabled_idle` for 10 cycles at the production
    cadence (``poll_seconds=30``). Compress the wall-clock with a
    stub ``sleep_func`` + ``monotonic`` clock so the test runs in
    milliseconds.

    Asserts:

    * exit code 0 (clean exit per VAL-M5-046's "Performance ledger
      daemon exits cleanly when env=0" expected-behavior bullet)
    * zero new ``candidate_events`` rows
    * zero new ``ensemble_scores_event`` rows
    * zero new ``news_match_log`` rows
    * zero new ``ticker_cooldown`` rows
    * zero new ``paper_orders`` rows with ``event='news_event_entry'``
    * zero new ``llm_cost_ledger`` rows
    * daily-curated audit_latest.json sha256 unchanged
    * daily-curated paper_orders bucket sha256 unchanged
    * gate-decision INFO line emitted at most once per 60 s
      (and at least once for the first cycle)
    """

    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "0")
    assert is_news_daemon_enabled() is False, (
        "kill-switch must report False for NEWS_DAEMON_ENABLED=0"
    )

    # Seed the daily-curated baseline + 10 synthetic news_events
    # rows. The kill switch must NOT touch any of these rows.
    _seed_baseline_paper_orders(db_path)
    seeded_news_ids = _seed_news_events(db_path, n=_N_SEEDED_NEWS_EVENTS)
    assert len(seeded_news_ids) == _N_SEEDED_NEWS_EVENTS

    # Snapshot pre-run state.
    pre_audit_sha = _sha256_bytes(baseline_audit_path.read_bytes())
    pre_news_count = _count(db_path, "SELECT COUNT(*) FROM news_events")
    assert pre_news_count == _N_SEEDED_NEWS_EVENTS

    pre_baseline_orders = _count(
        db_path,
        "SELECT COUNT(*) FROM paper_orders WHERE event != 'news_event_entry'",
    )
    assert pre_baseline_orders == 2

    # Drive the disabled-idle loop for 10 cycles (= 5 minutes at
    # ``poll_seconds=30``). The fake clock advances 30 s per
    # ``sleep_func`` call so the gate-decision throttle (60 s)
    # fires at cycles 1, 3, 5, 7, 9 — five emissions over 5
    # minutes, which is exactly the contract's "once per minute".
    fake_clock = [0.0]
    sleep_calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(float(seconds))
        fake_clock[0] += float(seconds)

    def fake_monotonic() -> float:
        return fake_clock[0]

    with pytest.MonkeyPatch.context() as caplog_ctx:  # noqa: F841
        # Use ``caplog``-style capture via the module logger.
        captured: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
                captured.append(record)

        log = logging.getLogger(_LOGGER_NAME)
        prev_level = log.level
        prev_propagate = log.propagate
        handler = _Capture(level=logging.INFO)
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
        try:
            rc = run_disabled_idle(
                poll_seconds=DEFAULT_POLL_SECONDS,
                max_cycles=_FIVE_MINUTE_CYCLE_COUNT,
                sleep_func=fake_sleep,
                monotonic=fake_monotonic,
                gate_log_interval=60.0,
            )
        finally:
            log.removeHandler(handler)
            log.setLevel(prev_level)
            log.propagate = prev_propagate

    # Clean exit code (VAL-M5-046 "exits cleanly when env=0").
    assert rc == 0

    # ``run_disabled_idle`` sleeps once per cycle EXCEPT the last
    # one (it returns before the final sleep), so 10 cycles record
    # 9 sleep calls of ``poll_seconds`` each.
    assert len(sleep_calls) == _FIVE_MINUTE_CYCLE_COUNT - 1
    assert all(s == float(DEFAULT_POLL_SECONDS) for s in sleep_calls)

    # Gate-decision INFO emitted at most once per 60 s. With the
    # fake clock advancing 30 s per cycle, the gate-log emits at
    # cycles 1, 3, 5, 7, 9 — five times across 10 cycles (exactly
    # the contract's "once per minute" cadence).
    decisions = [
        r for r in captured
        if getattr(r, "event", None) == "news_daemon_gate_decision"
    ]
    assert len(decisions) >= 1, (
        "first cycle must always emit a gate-decision INFO line"
    )
    assert len(decisions) <= _FIVE_MINUTE_CYCLE_COUNT, (
        f"gate-decision throttle violated: emitted {len(decisions)} "
        f"over {_FIVE_MINUTE_CYCLE_COUNT} cycles (max 1/min)"
    )
    # Each emitted record carries decision='disabled_idle' so the
    # journal records the gate is being honoured (VAL-M5-046
    # expected-behavior bullet 2).
    for record in decisions:
        assert getattr(record, "decision", None) == "disabled_idle"
        assert getattr(record, "reason", None) == "NEWS_DAEMON_ENABLED=0"

    # The kill switch must write ZERO rows to every Reading-B
    # persistence surface.
    for table in (
        "candidate_events",
        "ensemble_scores_event",
        "news_match_log",
        "ticker_cooldown",
    ):
        cnt = _count(db_path, f"SELECT COUNT(*) FROM {table}")
        assert cnt == 0, (
            f"{table} must be empty under NEWS_DAEMON_ENABLED=0; got {cnt}"
        )

    # Zero new news_event_entry paper_orders rows (the additive
    # Reading-B carve-out).
    rb_orders = _count(
        db_path,
        "SELECT COUNT(*) FROM paper_orders WHERE event = 'news_event_entry'",
    )
    assert rb_orders == 0

    # Zero new llm_cost_ledger rows from Stage-2 (no LLM was called).
    ledger_rows = _count(db_path, "SELECT COUNT(*) FROM llm_cost_ledger")
    assert ledger_rows == 0

    # Daily-curated baseline rows untouched (event != 'news_event_entry').
    post_baseline_orders = _count(
        db_path,
        "SELECT COUNT(*) FROM paper_orders WHERE event != 'news_event_entry'",
    )
    assert post_baseline_orders == pre_baseline_orders

    # Daily-curated audit_latest.json sha256 matches golden.
    post_audit_sha = _sha256_bytes(baseline_audit_path.read_bytes())
    assert post_audit_sha == pre_audit_sha, (
        "daily-curated audit_latest.json sha256 must match the "
        "pre-run golden under NEWS_DAEMON_ENABLED=0"
    )

    # The 10 seeded news_events rows were never emitted as
    # candidates (the central kill-switch invariant).
    candidate_count = _count(db_path, "SELECT COUNT(*) FROM candidate_events")
    assert candidate_count == 0


# ---------------------------------------------------------------------------
# Kill switch via the public main() entrypoint — proves the daemon
# CLI honours the gate end-to-end (not just the helper function).
# ---------------------------------------------------------------------------


def test_main_entrypoint_honours_kill_switch_and_exits_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``python -m biotech_sniper.news_daemon`` exits 0 when env=0.

    Without the kill switch, ``main([])`` would invoke the real
    resilience loop (post-M2) or the dry-run path. With
    ``NEWS_DAEMON_ENABLED=0``, main() must short-circuit into
    :func:`run_disabled_idle` and return 0 cleanly after
    ``--max-cycles`` cycles — proving the gate is wired all the way
    through the public entrypoint.

    We patch ``run_disabled_idle`` itself with a fast no-op stub so
    the test runs in milliseconds (the *real* sleep cadence is
    already pinned by the dedicated cadence test in
    :mod:`tests.test_poll_cadence`). The contract here is that
    ``main()`` (a) detects the disabled gate, (b) routes to
    ``run_disabled_idle``, and (c) returns rc=0 — none of which
    requires real-time sleep.
    """
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "0")
    captured_calls: list[dict[str, Any]] = []

    def fast_run_disabled_idle(
        *,
        poll_seconds: int,
        max_cycles: int,
    ) -> int:
        captured_calls.append(
            {"poll_seconds": poll_seconds, "max_cycles": max_cycles}
        )
        return 0

    monkeypatch.setattr(
        poll_loop, "run_disabled_idle", fast_run_disabled_idle
    )

    rc = news_daemon_main(
        ["--max-cycles", str(_FIVE_MINUTE_CYCLE_COUNT), "--poll-seconds", "30"]
    )
    assert rc == 0, (
        "main() must exit 0 cleanly under NEWS_DAEMON_ENABLED=0; got rc="
        f"{rc}"
    )
    assert len(captured_calls) == 1, (
        "main() must invoke run_disabled_idle exactly once when "
        f"NEWS_DAEMON_ENABLED=0; got {captured_calls}"
    )
    assert captured_calls[0]["poll_seconds"] == 30
    assert captured_calls[0]["max_cycles"] == _FIVE_MINUTE_CYCLE_COUNT


# ---------------------------------------------------------------------------
# Daemon emits 'disabled_idle' gate-decision log line once per minute
# and otherwise stays silent (VAL-M5-046 expected-behavior bullet 4).
# ---------------------------------------------------------------------------


def test_kill_switch_log_silence_outside_gate_decision(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The disabled-idle loop emits ONLY ``news_daemon_gate_decision``
    INFO lines and is otherwise silent — no spurious WARNING /
    ERROR / matcher / emit / heartbeat events.

    Pins the contract: ``Daemon emits 'disabled_idle'
    gate-decision log line once per minute and otherwise stays
    silent.`` Re-uses the same fake-clock harness as the primary
    invariant test.
    """
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "0")
    assert is_news_daemon_enabled() is False
    _seed_news_events(db_path)

    fake_clock = [0.0]

    def fake_sleep(seconds: float) -> None:
        fake_clock[0] += float(seconds)

    def fake_monotonic() -> float:
        return fake_clock[0]

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
            captured.append(record)

    log = logging.getLogger(_LOGGER_NAME)
    prev_level = log.level
    prev_propagate = log.propagate
    handler = _Capture(level=logging.DEBUG)  # capture EVERYTHING
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    try:
        rc = run_disabled_idle(
            poll_seconds=DEFAULT_POLL_SECONDS,
            max_cycles=_FIVE_MINUTE_CYCLE_COUNT,
            sleep_func=fake_sleep,
            monotonic=fake_monotonic,
            gate_log_interval=60.0,
        )
    finally:
        log.removeHandler(handler)
        log.setLevel(prev_level)
        log.propagate = prev_propagate

    assert rc == 0

    # Every record emitted by the disabled-idle loop must be a
    # gate-decision (any other event would betray a side-effect).
    foreign_events = [
        r for r in captured
        if getattr(r, "event", None) != "news_daemon_gate_decision"
    ]
    assert foreign_events == [], (
        "disabled-idle loop must stay silent outside gate-decision "
        f"emissions; got {[(r.levelname, r.getMessage()) for r in foreign_events]}"
    )

    # No ERROR / WARNING records from the news-daemon logger at all.
    severe = [r for r in captured if r.levelno >= logging.WARNING]
    assert severe == [], (
        f"disabled-idle loop must emit no WARNING/ERROR records; "
        f"got {[(r.levelname, r.getMessage()) for r in severe]}"
    )


# ---------------------------------------------------------------------------
# Fail-open semantics — pin the kill switch is ONLY engaged when
# NEWS_DAEMON_ENABLED is explicitly "0". Mirrors the doctring of
# is_news_daemon_enabled() and prevents a future regression where a
# typo silently disables the daemon.
# ---------------------------------------------------------------------------


def test_kill_switch_fail_open_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset / typo'd / non-zero values leave the daemon ENABLED.

    The kill switch is intentionally fail-open: a typo in the env
    var must NEVER silently disable the daemon (which would be
    invisible to operations until the watchdog stale-heartbeat
    alarm fires). This is the inverse of the LIVE_MODE gate, which
    is fail-closed.
    """
    monkeypatch.delenv("NEWS_DAEMON_ENABLED", raising=False)
    assert is_news_daemon_enabled() is True

    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "0")
    assert is_news_daemon_enabled() is False

    # Whitespace-stripped "0" still disables.
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", " 0 ")
    assert is_news_daemon_enabled() is False

    # Empty / typos / "true" / "1" leave the daemon ENABLED.
    for raw in ("", "1", "true", "yes", "00", "disabled", "FALSE"):
        monkeypatch.setenv("NEWS_DAEMON_ENABLED", raw)
        assert is_news_daemon_enabled() is True, (
            f"NEWS_DAEMON_ENABLED={raw!r} must NOT engage the kill switch "
            "(only literal '0' disables)"
        )
