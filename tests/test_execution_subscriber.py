"""Regression tests for f-m4-11: execution_subscriber structured logging.

The user-testing-validator-m4-scheduler round 1 caught a production
regression where ``execution_subscriber.poll_complete`` emitted a
bare-text line through the standard library's default
``logging.Formatter`` instead of the project's
:class:`biotech_sniper.logging_setup.JSONFormatter`. The root cause
was that the CLI ``main()`` never called
:func:`logging_setup.configure`, so the file handler / JSON
formatter were never attached.

These tests lock in the fix. They exercise three independent
contracts:

1. ``main()`` calls :func:`logging_setup.configure` with
   ``log_name='intraday'`` BEFORE any log line is emitted, so the
   intraday log destination is wired correctly.
2. The structured ``execution_subscriber_poll_complete`` event
   appears as a single valid JSON line in the intraday log file
   with the contract-required keys
   (``ts``/``level``/``event``/``module``/``db_path``/``date``/``recorded``).
3. The ``execution_subscriber.py`` source contains no bare
   ``logging.info`` / ``print(`` calls outside the
   ``if __name__ == '__main__':`` guard — which would silently
   bypass the JSONFormatter (the original failure mode).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper import db as _db_module
from biotech_sniper import execution_subscriber as _exec_sub
from biotech_sniper import logging_setup
from biotech_sniper.execution_subscriber import (
    ExecutionSubscriber,
    main as execution_subscriber_main,
    record_execution_event,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _migrated_db(tmp_path: Path) -> Path:
    p = tmp_path / "alpha_sniper.db"
    conn = _db_module.connect(p)
    try:
        _db_module.run_migrations(conn)
    finally:
        conn.close()
    return p


def _insert_paper_order(
    db_path: Path,
    *,
    paper_order_id: str,
    alpaca_order_id: str,
    client_order_id: str,
    side: str = "buy",
    qty: int = 2,
    requested_mid_at_submit: float = 1.50,
    created_at: str = "2026-04-28T15:30:00.000Z",
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO paper_orders (
                id, play_card_id, alpaca_order_id, symbol, side, qty,
                status, client_order_id, created_at,
                requested_mid_at_submit
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                paper_order_id,
                "PC-1",
                alpaca_order_id,
                "AXSM250620C00125000",
                side,
                qty,
                "submitted",
                client_order_id,
                created_at,
                requested_mid_at_submit,
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _reset_logging(monkeypatch):
    """Reset ``logging_setup``'s module-level ``_CONFIGURED`` flag and the
    root logger's handlers between tests so each test exercises the
    real :func:`configure` path. Also ensures we don't leak file handles
    pointing at /tmp/m4_test/* across tests.
    """
    import logging

    monkeypatch.setattr(logging_setup, "_CONFIGURED", False, raising=False)
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    # Restore handlers & level so subsequent tests see a clean root logger.
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:  # noqa: BLE001 - best effort
            pass
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)
    monkeypatch.setattr(logging_setup, "_CONFIGURED", False, raising=False)


# ---------------------------------------------------------------------------
# Test 1: main() configures logging_setup with log_name='intraday'.
# ---------------------------------------------------------------------------


def test_main_configures_logging_setup_with_log_name_intraday(
    tmp_path: Path, monkeypatch
) -> None:
    """``main()`` must call :func:`logging_setup.configure` with
    ``log_name='intraday'`` BEFORE emitting any log line.

    Regression for VAL-M4-025: prior to f-m4-11 the CLI never
    wired the JSON formatter, so the intraday log contained a bare
    ``execution_subscriber.poll_complete db_path=…`` line instead
    of a structured JSON object.
    """
    db_path = _migrated_db(tmp_path)
    configure_calls: list[dict[str, Any]] = []

    real_configure = logging_setup.configure

    def spy_configure(*args: Any, **kwargs: Any):
        configure_calls.append({"args": args, "kwargs": kwargs})
        return real_configure(*args, **kwargs)

    monkeypatch.setattr(logging_setup, "configure", spy_configure)
    monkeypatch.setattr(_exec_sub._logging_setup, "configure", spy_configure)

    rc = execution_subscriber_main(
        ["--dry-run", "--date", "2026-04-28"],
        db_path=db_path,
    )
    assert rc == 0
    assert configure_calls, (
        "execution_subscriber.main must call logging_setup.configure "
        "before emitting any log line"
    )
    first = configure_calls[0]
    log_name = first["kwargs"].get("log_name") or (
        first["args"][0] if first["args"] else None
    )
    assert log_name == "intraday", (
        f"expected log_name='intraday', got {log_name!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: structured ``execution_subscriber_poll_complete`` JSON line.
# ---------------------------------------------------------------------------


class _FillStubClient:
    """Minimal Alpaca duck-type for poll_once that returns one ``filled``."""

    def __init__(self, alpaca_order_id: str) -> None:
        self._oid = alpaca_order_id

    def get_order(self, order_id: str) -> dict[str, Any]:
        return {
            "id": self._oid,
            "status": "filled",
            "filled_at": "2026-04-28T15:30:05.000Z",
            "filled_qty": 2,
            "filled_avg_price": 1.51,
        }


def test_main_emits_structured_poll_complete_json(
    tmp_path: Path, monkeypatch
) -> None:
    """After ``main(['--poll-once'])`` the intraday log line for
    ``execution_subscriber_poll_complete`` parses as JSON and
    contains the contract-required keys.

    Validates the user-testing-validator-m4-scheduler fix: the line
    must be a single JSON object with the four base keys
    (``ts``, ``level``, ``event``, ``module``) plus the f-m4-11
    structured extras (``db_path``, ``date``, ``recorded``).
    """
    db_path = _migrated_db(tmp_path)
    log_dir = tmp_path / "log_intraday"
    monkeypatch.setenv("ALPHA_SNIPER_LOG_DIR", str(log_dir))

    # Seed one open paper_orders row so poll_once has something to walk.
    _insert_paper_order(
        db_path,
        paper_order_id="po-1",
        alpaca_order_id="alp-1",
        client_order_id="client-1",
    )
    record_execution_event(
        db_path,
        paper_order_id="po-1",
        event_type="submitted",
        event_at="2026-04-28T15:30:00.500Z",
    )
    record_execution_event(
        db_path,
        paper_order_id="po-1",
        event_type="accepted",
        event_at="2026-04-28T15:30:01.000Z",
    )

    rc = execution_subscriber_main(
        ["--poll-once", "--date", "2026-04-28"],
        client=_FillStubClient("alp-1"),
        db_path=db_path,
    )
    assert rc == 0

    log_file = log_dir / "intraday.log"
    assert log_file.is_file(), (
        f"intraday log file not created at {log_file}"
    )
    lines = [ln for ln in log_file.read_text().splitlines() if ln.strip()]
    assert lines, "intraday log file is empty"

    # Every line must parse as JSON (contract: structured-only).
    parsed = []
    for ln in lines:
        try:
            parsed.append(json.loads(ln))
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"intraday log line is not valid JSON: {ln!r} ({exc})"
            )

    poll_complete = [
        p for p in parsed if p.get("event") == "execution_subscriber_poll_complete"
    ]
    assert poll_complete, (
        "intraday log must contain an "
        "`execution_subscriber_poll_complete` event"
    )
    record = poll_complete[-1]
    for key in ("ts", "level", "event", "module", "db_path", "date", "recorded"):
        assert key in record, (
            f"poll_complete JSON missing required key {key!r}: {record}"
        )
    assert record["level"] == "INFO"
    assert record["module"] == "execution_subscriber"
    assert record["date"] == "2026-04-28"
    assert isinstance(record["recorded"], int)
    assert record["recorded"] >= 1


# ---------------------------------------------------------------------------
# Test 3: source-level grep — no bare logging.info / print outside __main__.
# ---------------------------------------------------------------------------


def test_no_bare_logging_or_print_outside_main_block() -> None:
    """``execution_subscriber.py`` must not contain any bare
    ``logging.info(`` / ``logging.warning(`` / ``logging.error(`` /
    ``logging.exception(`` / ``logging.debug(`` calls, and no
    top-level ``print(`` calls outside the
    ``if __name__ == '__main__':`` guard.

    Locks in the fix surfaced by user-testing-validator-m4-scheduler:
    bare-stdlib-logging calls bypass the JSONFormatter, and bare
    ``print()`` calls dump unstructured text into the journal.
    """
    src_path = Path(_exec_sub.__file__)
    src = src_path.read_text(encoding="utf-8")

    # Bare ``logging.<level>(`` calls (the regression mode).
    bare_logging = re.compile(
        r"^[^#\n]*\blogging\.(?:info|warning|error|exception|debug|basicConfig)\b\(",
        re.MULTILINE,
    )
    bare_logging_hits = bare_logging.findall(src)
    assert not bare_logging_hits, (
        f"execution_subscriber.py still has bare logging.* calls: "
        f"{bare_logging_hits}. Use the module-level `logger` from "
        f"`logging.getLogger(__name__)` (formatter installed by "
        f"`logging_setup.configure`) instead."
    )

    # ``print(...)`` outside the ``if __name__ == "__main__":`` block.
    # Find the position of the main-guard, treat anything BEFORE it as
    # the production region. Production prints would dump unstructured
    # text into the journal.
    main_guard_match = re.search(
        r'^if\s+__name__\s*==\s*[\'\"]__main__[\'\"]\s*:',
        src,
        re.MULTILINE,
    )
    cutoff = main_guard_match.start() if main_guard_match else len(src)
    production_region = src[:cutoff]
    bare_print = re.compile(
        r"^[^#\n]*\bprint\s*\(",
        re.MULTILINE,
    )
    print_hits = bare_print.findall(production_region)
    assert not print_hits, (
        f"execution_subscriber.py still has bare print() calls in the "
        f"production region: {print_hits}. Replace each with a structured "
        f"`logger.info('event_name', extra={{...}})` call so it lands as "
        f"a JSON line in intraday.log."
    )


# ---------------------------------------------------------------------------
# Test 4: importing execution_subscriber does NOT lock log destination.
# ---------------------------------------------------------------------------


def test_module_import_does_not_lock_log_destination(
    tmp_path: Path,
) -> None:
    """Importing ``execution_subscriber`` must not eagerly call
    :func:`logging_setup.configure`. Otherwise the destination would
    be pinned to ``app.log`` (the default ``log_name``) before the
    legitimate ``main()`` / orchestrator entrypoint can install its
    own ``intraday.log`` / ``daily.log`` destination.

    Run in a subprocess so the assertion is hermetic — reloading the
    module in-process would create a NEW
    :class:`IllegalStateTransition` class and break sibling test
    files that imported the original class.
    """
    import subprocess
    import sys as _sys

    log_dir = tmp_path / "log_canary"
    log_dir.mkdir()

    script = (
        "import os, sys\n"
        "from biotech_sniper import execution_subscriber  # noqa: F401\n"
        "from biotech_sniper import logging_setup\n"
        "log_dir = os.environ['ALPHA_SNIPER_LOG_DIR']\n"
        "files = sorted(os.listdir(log_dir))\n"
        "print('CONFIGURED=' + str(logging_setup._CONFIGURED))\n"
        "print('FILES=' + ','.join(files))\n"
    )
    proc = subprocess.run(
        [_sys.executable, "-c", script],
        env={
            "ALPHA_SNIPER_LOG_DIR": str(log_dir),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"subprocess import failed: stdout={proc.stdout!r} "
        f"stderr={proc.stderr!r}"
    )
    out = proc.stdout
    assert "CONFIGURED=False" in out, (
        f"execution_subscriber import must not auto-configure logging; "
        f"saw {out!r}"
    )
    assert "FILES=" in out
    # Strip the prefix and assert no log files were created.
    files_line = next(
        line for line in out.splitlines() if line.startswith("FILES=")
    )
    files_value = files_line[len("FILES="):]
    assert files_value == "", (
        f"importing execution_subscriber must not create any log file in "
        f"ALPHA_SNIPER_LOG_DIR; got {files_value!r}"
    )


# ---------------------------------------------------------------------------
# Test 5–9: f-misc-07 — LEGAL_TRANSITIONS hardening for submitted → terminal
# ---------------------------------------------------------------------------
#
# Background: the cron-driven ``ExecutionSubscriber.poll_once()``
# polls every minute and almost always observes Alpaca paper's
# intermediate ``accepted`` status before any cancel / expiry. But
# the broker can in principle skip ``accepted`` entirely (e.g. a
# very fast manual cancel, broker outage during fill, or a contract
# being rejected after the local ``submitted`` row landed but before
# the broker side accepted it). Prior to f-misc-07 the
# ``LEGAL_TRANSITIONS`` graph mapped ``'submitted' → {'accepted',
# 'rejected'}`` only — so a broker-emitted ``canceled`` / ``expired``
# without a prior ``accepted`` event raised
# :class:`IllegalStateTransition` mid-loop and the terminal row was
# never written. Tests below pin the hardened behaviour: the
# subscriber records the terminal event without raising, while the
# happy-path (submitted → accepted → filled) and the rejection
# (submitted → rejected) paths continue to work unchanged.


class _StaticStatusClient:
    """Minimal Alpaca duck-type returning a fixed status payload."""

    def __init__(
        self,
        alpaca_order_id: str,
        status: str,
        *,
        filled_qty: int | None = None,
        filled_avg_price: float | None = None,
        event_at: str = "2026-04-28T15:30:05.000Z",
    ) -> None:
        self._oid = alpaca_order_id
        self._status = status
        self._filled_qty = filled_qty
        self._filled_avg_price = filled_avg_price
        self._event_at = event_at

    def get_order(self, order_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self._oid,
            "status": self._status,
            "updated_at": self._event_at,
        }
        if self._filled_qty is not None:
            payload["filled_qty"] = self._filled_qty
            payload["filled_at"] = self._event_at
        if self._filled_avg_price is not None:
            payload["filled_avg_price"] = self._filled_avg_price
        return payload


class _SequencedStatusClient:
    """Alpaca duck-type that returns a different status on each call."""

    def __init__(
        self,
        alpaca_order_id: str,
        statuses: list[dict[str, Any]],
    ) -> None:
        self._oid = alpaca_order_id
        self._statuses = statuses
        self._idx = 0

    def get_order(self, order_id: str) -> dict[str, Any]:
        idx = min(self._idx, len(self._statuses) - 1)
        payload = dict(self._statuses[idx])
        payload.setdefault("id", self._oid)
        self._idx += 1
        return payload


def _seed_submitted_order(
    db_path: Path,
    *,
    paper_order_id: str = "po-misc07",
    alpaca_order_id: str = "alp-misc07",
    client_order_id: str = "client-misc07",
    submitted_at: str = "2026-04-28T15:30:00.500Z",
) -> str:
    """Seed a paper_orders row with a single ``submitted`` execution_event."""
    _insert_paper_order(
        db_path,
        paper_order_id=paper_order_id,
        alpaca_order_id=alpaca_order_id,
        client_order_id=client_order_id,
    )
    record_execution_event(
        db_path,
        paper_order_id=paper_order_id,
        event_type="submitted",
        event_at=submitted_at,
    )
    return paper_order_id


def _last_n_event_types(db_path: Path, paper_order_id: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT event_type FROM execution_events "
            "WHERE paper_order_id=? ORDER BY id ASC",
            (paper_order_id,),
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def test_poll_once_submitted_to_canceled_writes_canceled_row(
    tmp_path: Path,
) -> None:
    """f-misc-07: when the broker reports ``canceled`` for an order
    whose only prior execution_event is ``submitted`` (no
    intermediate ``accepted``), :meth:`ExecutionSubscriber.poll_once`
    MUST write exactly one new ``execution_events`` row with
    ``event_type='canceled'`` and MUST NOT raise
    :class:`IllegalStateTransition`.
    """
    db_path = _migrated_db(tmp_path)
    paper_order_id = _seed_submitted_order(db_path)

    subscriber = ExecutionSubscriber(
        client=_StaticStatusClient("alp-misc07", "canceled"),
        db_path=db_path,
    )
    recorded = subscriber.poll_once()

    assert recorded == 1, (
        f"f-misc-07: poll_once must write exactly 1 row; got {recorded}"
    )
    assert _last_n_event_types(db_path, paper_order_id) == [
        "submitted",
        "canceled",
    ]


def test_poll_once_submitted_to_expired_writes_expired_row(
    tmp_path: Path,
) -> None:
    """f-misc-07: same hardening, but for the ``expired`` terminal
    status. Alpaca paper documents ``expired`` as a legal end-state
    from any non-terminal status — the subscriber must record it
    even without a prior ``accepted`` event.
    """
    db_path = _migrated_db(tmp_path)
    paper_order_id = _seed_submitted_order(db_path)

    subscriber = ExecutionSubscriber(
        client=_StaticStatusClient("alp-misc07", "expired"),
        db_path=db_path,
    )
    recorded = subscriber.poll_once()

    assert recorded == 1
    assert _last_n_event_types(db_path, paper_order_id) == [
        "submitted",
        "expired",
    ]


def test_poll_once_submitted_to_accepted_to_filled_happy_path(
    tmp_path: Path,
) -> None:
    """Regression: the canonical happy path
    ``submitted → accepted → filled`` continues to work after the
    f-misc-07 hardening — first poll captures ``accepted``, second
    poll captures ``filled``.
    """
    db_path = _migrated_db(tmp_path)
    paper_order_id = _seed_submitted_order(db_path)

    client = _SequencedStatusClient(
        "alp-misc07",
        [
            {
                "status": "accepted",
                "updated_at": "2026-04-28T15:30:01.000Z",
            },
            {
                "status": "filled",
                "filled_at": "2026-04-28T15:30:05.000Z",
                "updated_at": "2026-04-28T15:30:05.000Z",
                "filled_qty": 2,
                "filled_avg_price": 1.51,
            },
        ],
    )
    subscriber = ExecutionSubscriber(client=client, db_path=db_path)

    first = subscriber.poll_once()
    second = subscriber.poll_once()

    assert first == 1 and second == 1
    assert _last_n_event_types(db_path, paper_order_id) == [
        "submitted",
        "accepted",
        "filled",
    ]


def test_poll_once_submitted_to_accepted_to_canceled_happy_path(
    tmp_path: Path,
) -> None:
    """Regression: the canonical cancel-after-accept lifecycle still
    works. The subscriber walks ``submitted → accepted`` on the
    first poll and ``accepted → canceled`` on the second.
    """
    db_path = _migrated_db(tmp_path)
    paper_order_id = _seed_submitted_order(db_path)

    client = _SequencedStatusClient(
        "alp-misc07",
        [
            {
                "status": "accepted",
                "updated_at": "2026-04-28T15:30:01.000Z",
            },
            {
                "status": "canceled",
                "updated_at": "2026-04-28T15:30:02.000Z",
            },
        ],
    )
    subscriber = ExecutionSubscriber(client=client, db_path=db_path)

    subscriber.poll_once()
    subscriber.poll_once()

    assert _last_n_event_types(db_path, paper_order_id) == [
        "submitted",
        "accepted",
        "canceled",
    ]


def test_poll_once_submitted_to_rejected_happy_path(tmp_path: Path) -> None:
    """Regression: the existing ``submitted → rejected`` short-path
    (broker explicitly rejects the order after the executor's local
    ``submitted`` row landed) is unchanged after f-misc-07.
    """
    db_path = _migrated_db(tmp_path)
    paper_order_id = _seed_submitted_order(db_path)

    subscriber = ExecutionSubscriber(
        client=_StaticStatusClient("alp-misc07", "rejected"),
        db_path=db_path,
    )
    recorded = subscriber.poll_once()

    assert recorded == 1
    assert _last_n_event_types(db_path, paper_order_id) == [
        "submitted",
        "rejected",
    ]


def test_legal_transitions_submitted_includes_canceled_and_expired() -> None:
    """f-misc-07 source-level invariant: ``LEGAL_TRANSITIONS['submitted']``
    explicitly admits ``'canceled'`` and ``'expired'`` so the
    state-machine validator stays honest about the broker's
    permitted transitions.
    """
    from biotech_sniper.execution_subscriber import LEGAL_TRANSITIONS

    allowed = LEGAL_TRANSITIONS["submitted"]
    for terminal in ("accepted", "rejected", "canceled", "expired"):
        assert terminal in allowed, (
            f"LEGAL_TRANSITIONS['submitted'] missing {terminal!r}: "
            f"{sorted(allowed)}"
        )
