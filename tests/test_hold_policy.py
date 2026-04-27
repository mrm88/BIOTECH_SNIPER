"""Unit tests for :mod:`biotech_sniper.hold_policy` (f-m3-09).

The hold-policy module is the single arbiter for whether an exit may
fire on an active paper-trading play. These tests lock its three
responsibilities in place:

* The closed enum of allowed exit-event tags
  (:data:`hold_policy.ALLOWED_EXIT_EVENTS`).
* The deterministic ``client_order_id`` namespace
  (:func:`make_exit_client_order_id`).
* The pre-resolve guard (:func:`should_resolve`) and the
  general-purpose sell gate (:func:`assert_exit_allowed`).

In addition the suite exercises the integration points wired into
:class:`PaperExecutor`:

* :meth:`PaperExecutor.submit_exit` refuses sells with an event
  outside :data:`ALLOWED_EXIT_EVENTS`.
* The ``orders`` table CHECK constraint mirrors the Python enum so
  the SQL layer rejects unknown event values.
* :func:`auto_resolver.hold_policy_blocks_resolution` denies a
  resolution while ``today < catalyst_date`` and no exit has filled.
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper import db as db_module
from biotech_sniper import hold_policy as hp
from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.auto_resolver import hold_policy_blocks_resolution
from biotech_sniper.paper_executor import PaperExecutor


# ---------------------------------------------------------------------------
# Fake Alpaca client (mirrors the pattern in test_paper_executor.py).
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    def __init__(self) -> None:
        self.base_url = PAPER_BASE_URL
        self.submit_calls: list[Any] = []
        self._submit_results: list[dict[str, Any]] = []

    def queue(self, response: dict[str, Any]) -> None:
        self._submit_results.append(response)

    def get_positions(self) -> list[dict[str, Any]]:
        return []

    def submit_order(self, order_request: Any) -> dict[str, Any]:
        self.submit_calls.append(order_request)
        if not self._submit_results:
            raise AssertionError(
                "_FakeAlpacaClient.submit_order: no result queued"
            )
        return self._submit_results.pop(0)

    def get_order(self, order_id: str) -> dict[str, Any]:
        return {"id": order_id, "status": "accepted"}


def _stub_sell_response(
    *, order_id: str = "11111111-1111-1111-1111-111111111111", qty: int = 4
) -> dict[str, Any]:
    return {
        "id": order_id,
        "client_order_id": "stub-cid",
        "symbol": "AXSM250620C00125000",
        "asset_class": "us_option",
        "qty": qty,
        "side": "sell",
        "status": "accepted",
        "order_class": "simple",
        "type": "market",
        "time_in_force": "day",
    }


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "alpha_sniper.db"


@pytest.fixture
def make_executor(db_path: Path):
    def _factory(*, client: _FakeAlpacaClient | None = None):
        fake = client or _FakeAlpacaClient()
        executor = PaperExecutor(
            fake,  # type: ignore[arg-type]
            db_path=db_path,
            poll_interval_seconds=0.0,
        )
        return executor, fake

    return _factory


# ---------------------------------------------------------------------------
# 1) Allowed-event enum.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    ["iv_crush_exit", "stop_loss", "adverse_news", "rotation"],
)
def test_allowed_exit_events_contains_each_trigger(event: str) -> None:
    """All four f-m3-09 exit triggers are members of the allowed set."""
    assert event in hp.ALLOWED_EXIT_EVENTS
    assert hp.is_exit_allowed(event)


def test_unknown_event_is_rejected() -> None:
    """Unknown / misspelled events are rejected by ``is_exit_allowed``."""
    assert not hp.is_exit_allowed("iv_crush")  # the legacy spelling
    assert not hp.is_exit_allowed("foo")
    assert not hp.is_exit_allowed(None)
    assert not hp.is_exit_allowed("")


def test_entry_event_not_an_exit() -> None:
    """``open`` is the entry tag, NOT an exit trigger."""
    assert hp.ENTRY_EVENT == "open"
    assert hp.ENTRY_EVENT not in hp.ALLOWED_EXIT_EVENTS
    assert hp.ENTRY_EVENT in hp.VALID_EVENT_VALUES


# ---------------------------------------------------------------------------
# 2) client_order_id namespace.
# ---------------------------------------------------------------------------


def test_make_exit_client_order_id_format() -> None:
    """Returns ``{ticker}-{event}-{YYYY-MM-DD}`` exactly."""
    cid = hp.make_exit_client_order_id(
        "axsm", "stop_loss", datetime.date(2025, 4, 27)
    )
    assert cid == "AXSM-stop_loss-2025-04-27"


def test_make_exit_client_order_id_rejects_unknown_event() -> None:
    """Unknown events raise :class:`HoldPolicyViolation`."""
    with pytest.raises(hp.HoldPolicyViolation, match="not allowed"):
        hp.make_exit_client_order_id("AXSM", "iv_crush", "2025-04-27")


# ---------------------------------------------------------------------------
# 3) should_resolve guard (VAL-M3-048).
# ---------------------------------------------------------------------------


def test_should_resolve_blocks_pre_catalyst_no_exit() -> None:
    """today < catalyst AND no filled exit → False."""
    play = {"catalyst_date": "2025-04-30"}
    assert (
        hp.should_resolve(play, today="2025-04-27", has_filled_exit=False)
        is False
    )


def test_should_resolve_allows_after_catalyst() -> None:
    """today == catalyst → True."""
    play = {"catalyst_date": "2025-04-27"}
    assert hp.should_resolve(play, today="2025-04-27") is True


def test_should_resolve_allows_when_exit_filled() -> None:
    """has_filled_exit overrides the pre-catalyst guard."""
    play = {"catalyst_date": "2025-04-30"}
    assert (
        hp.should_resolve(play, today="2025-04-27", has_filled_exit=True)
        is True
    )


def test_should_resolve_falls_back_when_catalyst_missing() -> None:
    """No catalyst date AND no filled exit → False (conservative)."""
    play: dict[str, Any] = {}
    assert hp.should_resolve(play, today="2025-04-27") is False


# ---------------------------------------------------------------------------
# 4) PaperExecutor.submit_exit hold-rule integration (VAL-M3-047).
# ---------------------------------------------------------------------------


def test_submit_exit_refuses_unknown_event(make_executor) -> None:
    """submit_exit raises HoldPolicyViolation for events outside the enum."""
    executor, fake = make_executor()
    play = {
        "ticker": "AXSM",
        "symbol": "AXSM250620C00125000",
        "play_card_id": "AXSM-2025-04-27",
        "catalyst_date": "2025-04-30",
        "qty": 4,
    }
    with pytest.raises(hp.HoldPolicyViolation, match="not an allowed"):
        executor.submit_exit(play, "manual_close", today="2025-04-27")
    assert fake.submit_calls == []


def test_submit_exit_blocks_pre_catalyst_date_for_disallowed_event(
    make_executor,
) -> None:
    """Pre-catalyst sells with an unknown event raise even when the
    event is a pure typo (not in the allowed set)."""
    executor, fake = make_executor()
    play = {
        "ticker": "AXSM",
        "symbol": "AXSM250620C00125000",
        "play_card_id": "AXSM-2025-04-27",
        "catalyst_date": "2025-04-30",
        "qty": 4,
    }
    with pytest.raises(hp.HoldPolicyViolation):
        executor.submit_exit(play, "iv_crush", today="2025-04-27")
    assert fake.submit_calls == []


def test_submit_exit_allows_pre_catalyst_for_allowed_event(
    make_executor, db_path: Path
) -> None:
    """A stop_loss exit on a non-catalyst date is permitted."""
    fake = _FakeAlpacaClient()
    fake.queue(_stub_sell_response(qty=4))
    executor, _ = make_executor(client=fake)

    play = {
        "ticker": "AXSM",
        "symbol": "AXSM250620C00125000",
        "play_card_id": "AXSM-2025-04-27",
        "catalyst_date": "2025-04-30",
        "qty": 4,
    }
    order_id = executor.submit_exit(play, "stop_loss", today="2025-04-27")
    assert isinstance(order_id, str) and order_id

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT event, side, qty FROM paper_orders WHERE event = ?",
            ("stop_loss",),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["event"] == "stop_loss"
    assert rows[0]["side"] == "sell"
    assert rows[0]["qty"] == 4


def test_submit_exit_idempotent_on_same_day(
    make_executor, db_path: Path
) -> None:
    """Second submit_exit for same (ticker, event, date) → no duplicate."""
    fake = _FakeAlpacaClient()
    fake.queue(_stub_sell_response(qty=4))
    executor, _ = make_executor(client=fake)

    play = {
        "ticker": "AXSM",
        "symbol": "AXSM250620C00125000",
        "play_card_id": "AXSM-2025-04-27",
        "catalyst_date": "2025-04-30",
        "qty": 4,
    }
    first = executor.submit_exit(play, "stop_loss", today="2025-04-27")
    second = executor.submit_exit(play, "stop_loss", today="2025-04-27")
    # The duplicate call short-circuits and does not hit the broker.
    assert len(fake.submit_calls) == 1
    assert first == second
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE event = ?", ("stop_loss",)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


# ---------------------------------------------------------------------------
# 5) Event-enum CHECK constraint at the SQL layer (VAL-M3-052).
# ---------------------------------------------------------------------------


def test_orders_event_check_accepts_each_allowed_value(db_path: Path) -> None:
    """All five legal event values insert successfully."""
    conn = db_module.connect(db_path)
    db_module.run_migrations(conn)
    for i, event in enumerate(
        ["open", "iv_crush_exit", "stop_loss", "adverse_news", "rotation"]
    ):
        conn.execute(
            "INSERT INTO paper_orders (id, status, event, client_order_id) "
            "VALUES (?, ?, ?, ?)",
            (f"id-{i}", "accepted", event, f"client-{i}"),
        )
    conn.commit()
    rows = conn.execute("SELECT event FROM paper_orders").fetchall()
    conn.close()
    assert {r["event"] for r in rows} == {
        "open",
        "iv_crush_exit",
        "stop_loss",
        "adverse_news",
        "rotation",
    }


def test_orders_event_check_rejects_unknown_value(db_path: Path) -> None:
    """An unknown event value raises ``IntegrityError`` at insert time."""
    conn = db_module.connect(db_path)
    db_module.run_migrations(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO paper_orders (id, status, event, client_order_id) "
            "VALUES (?, ?, ?, ?)",
            ("id-bad", "accepted", "not_a_real_event", "client-bad"),
        )
    conn.close()


def test_orders_event_check_rejects_legacy_iv_crush(db_path: Path) -> None:
    """The historical ``'iv_crush'`` literal is no longer valid."""
    conn = db_module.connect(db_path)
    db_module.run_migrations(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO paper_orders (id, status, event, client_order_id) "
            "VALUES (?, ?, ?, ?)",
            ("id-legacy", "accepted", "iv_crush", "client-legacy"),
        )
    conn.close()


# ---------------------------------------------------------------------------
# 6) auto_resolver guard wires through to should_resolve.
# ---------------------------------------------------------------------------


def test_auto_resolver_guard_blocks_pre_catalyst_no_exit() -> None:
    """``hold_policy_blocks_resolution`` returns True before catalyst with
    no filled exit — the exact gate the auto_resolver consults before
    calling ``classify_resolution``."""
    entry = {"pdufa_date": "2025-04-30"}
    active = {"status": "ACTIVE"}
    assert (
        hold_policy_blocks_resolution(entry, active, today="2025-04-27")
        is True
    )


def test_auto_resolver_guard_releases_when_exit_filled() -> None:
    """A filled exit on the active play releases the guard."""
    entry = {"pdufa_date": "2025-04-30"}
    active = {"status": "ACTIVE", "filled_exit": True}
    assert (
        hold_policy_blocks_resolution(entry, active, today="2025-04-27")
        is False
    )


def test_auto_resolver_guard_releases_after_catalyst() -> None:
    """today >= catalyst → guard releases regardless of exit state."""
    entry = {"pdufa_date": "2025-04-27"}
    active = {"status": "ACTIVE"}
    assert (
        hold_policy_blocks_resolution(entry, active, today="2025-04-27")
        is False
    )
