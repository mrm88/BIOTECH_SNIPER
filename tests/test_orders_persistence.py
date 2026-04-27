"""Tests for the ``orders`` SQLite persistence layer (feature f-m3-06).

Hermetic and standalone: every test stands up an isolated SQLite db
under ``tmp_path`` and exercises :class:`PaperExecutor` against an
injected fake :class:`AlpacaClient` so no network calls fire and no
project-wide state is mutated.

Validation contract assertions exercised
----------------------------------------

* **VAL-M3-031** — the ``orders`` table is present in the project
  schema with the full column set named in the f-m3-06 spec
  (``id, play_card_id, alpaca_order_id, symbol, side, qty, status,
  reason, event, parent_play_card_id, created_at``). Construction
  of :class:`PaperExecutor` against a fresh SQLite file applies the
  schema migration and the resulting ``.schema orders`` is asserted
  here against the spec's exact column list. The ``schema_version``
  row is also bumped forward so an older db with no ``orders``
  table self-heals on the first connect after a deploy.
* **VAL-M3-032** — every successful submission writes exactly one
  row to ``orders`` with the broker-assigned ``alpaca_order_id``,
  the originating ``play_card_id``, and ``status`` reflecting the
  broker's last-known state (``submitted`` / ``accepted`` /
  ``filled`` — all are accepted by the contract per the f-m3-03
  parent feature, this f-m3-06 verification just locks the row
  count at exactly one).
* **VAL-M3-033** — every rejection (broker 4xx, concurrency cap,
  deployed-cap, contract-too-expensive) writes a single row with
  ``status='rejected'`` and ``reason`` populated with the broker /
  policy message, and never leaves a stray ``status='submitted'``
  row behind from the same attempt.
* **VAL-M3-034** — :meth:`PaperExecutor.get_orders_for_play` returns
  every persisted row for a play card in ``created_at ASC`` order
  (chronological lifecycle entry → exits / rejections), enabling the
  M3 rotation engine and the validators to walk the play history
  without client-side sorts.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper import db as db_module
from biotech_sniper.alpaca_client import (
    AlpacaClientError,
    PAPER_BASE_URL,
)
from biotech_sniper.paper_executor import (
    OrderRejected,
    PaperExecutor,
)


CASSETTE_DIR = Path(__file__).parent / "fixtures" / "cassettes" / "alpaca"


# ---------------------------------------------------------------------------
# Cassette + fake client helpers
# ---------------------------------------------------------------------------


def _load_cassette(name: str) -> dict[str, Any]:
    """Read a JSON cassette under ``tests/fixtures/cassettes/alpaca/``."""
    return json.loads((CASSETTE_DIR / name).read_text(encoding="utf-8"))


class _FakeAlpacaClient:
    """Minimal duck-typed substitute for :class:`AlpacaClient`.

    Mirrors the surface :class:`PaperExecutor` actually consumes:
    ``base_url`` (read), ``submit_order``, ``get_order``,
    ``get_positions``. Tests queue results / errors via the
    constructor; the executor never reaches a real network.
    """

    def __init__(
        self,
        *,
        base_url: str = PAPER_BASE_URL,
        submit_order_results: list[Any] | None = None,
        submit_order_error: Exception | None = None,
        get_order_results: list[Any] | None = None,
        positions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = base_url
        self._submit_results = list(submit_order_results or [])
        self._submit_error = submit_order_error
        self._get_order_results = list(get_order_results or [])
        self._positions = list(positions or [])
        self.submit_calls: list[Any] = []

    def get_positions(self) -> list[dict[str, Any]]:
        return list(self._positions)

    def submit_order(self, order_request: Any) -> dict[str, Any]:
        self.submit_calls.append(order_request)
        if self._submit_error is not None:
            raise self._submit_error
        if not self._submit_results:
            raise AssertionError(
                "_FakeAlpacaClient.submit_order called but no result queued"
            )
        return self._submit_results.pop(0)

    def get_order(self, order_id: str) -> dict[str, Any]:
        if not self._get_order_results:
            raise AssertionError(
                "_FakeAlpacaClient.get_order called but no result queued"
            )
        result = self._get_order_results[0]
        if len(self._get_order_results) > 1:
            self._get_order_results.pop(0)
        return result


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Fresh per-test SQLite db. The orders table is created lazily by the
    PaperExecutor's bootstrap migration on construction so the fixture
    itself does no I/O — keeping tests fast and order-independent."""
    return tmp_path / "alpha_sniper.db"


@pytest.fixture
def make_executor(db_path: Path):
    """Factory: return a (PaperExecutor, FakeClient) pair sharing ``db_path``."""

    def _factory(
        *,
        client: _FakeAlpacaClient | None = None,
    ) -> tuple[PaperExecutor, _FakeAlpacaClient]:
        fake = client or _FakeAlpacaClient()
        executor = PaperExecutor(
            fake,  # type: ignore[arg-type]
            db_path=db_path,
            poll_interval_seconds=0.0,
        )
        return executor, fake

    return _factory


def _call_play_card(
    *, play_card_id: str = "AXSM-2025-04-27"
) -> dict[str, Any]:
    """Build a single-leg long-call play card the executor accepts."""
    return {
        "play_card_id": play_card_id,
        "ticker": "AXSM",
        "option_legs": [
            {
                "symbol": "AXSM250620C00125000",
                "side": "buy",
                "qty": 2,
                "limit_price": 1.50,
                "option_type": "call",
                "strike": 125.0,
                "expiry": "2025-06-20",
                "client_order_id": (
                    "biotech-sniper-AXSM-call-2025-04-27"
                ),
            }
        ],
    }


# ---------------------------------------------------------------------------
# VAL-M3-031: orders table schema (full column set + schema_version bump).
# ---------------------------------------------------------------------------


# Exact column set the f-m3-06 description names. Kept module-level so
# additional tests can reference it without re-typing.
_EXPECTED_ORDERS_COLUMNS: set[str] = {
    "id",
    "play_card_id",
    "alpaca_order_id",
    "symbol",
    "side",
    "qty",
    "status",
    "reason",
    "event",
    "parent_play_card_id",
    "created_at",
}


def test_orders_table_present_after_migration(db_path: Path) -> None:
    """A brand-new SQLite db gains the ``orders`` table after one connect.

    Mimics the production deploy path: the ``alpha_sniper.db`` exists
    from M2 (with the older table set) and gets the f-m3-06 ``orders``
    table when any code path opens a connection through
    :func:`biotech_sniper.db.connect` / :func:`run_migrations` (which
    is what :class:`PaperExecutor` does in its constructor).
    """
    fake = _FakeAlpacaClient()
    PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='orders'"
        ).fetchall()
    finally:
        conn.close()
    assert rows, "orders table is missing after PaperExecutor migration"


def test_orders_table_full_column_set(db_path: Path) -> None:
    """The orders table exposes every column f-m3-06 names — no more,
    no less for the documented set (extra implementation columns
    such as auto-bookkeeping are tolerated as long as the spec
    columns are all present).
    """
    fake = _FakeAlpacaClient()
    PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]

    conn = sqlite3.connect(db_path)
    try:
        cols = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(orders)"
            ).fetchall()
        }
    finally:
        conn.close()
    missing = _EXPECTED_ORDERS_COLUMNS - cols
    assert not missing, (
        f"orders table missing columns required by f-m3-06: {sorted(missing)}"
    )


def test_orders_id_is_primary_key_text(db_path: Path) -> None:
    """``id`` is a TEXT primary key (mirrors the executor's UUID4 ids)."""
    fake = _FakeAlpacaClient()
    PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]

    conn = sqlite3.connect(db_path)
    try:
        info = conn.execute("PRAGMA table_info(orders)").fetchall()
    finally:
        conn.close()
    by_name = {row[1]: row for row in info}
    assert by_name["id"][2].upper() == "TEXT", "orders.id type should be TEXT"
    assert by_name["id"][5] == 1, "orders.id should be the primary key"


def test_schema_version_bumped_forward(db_path: Path) -> None:
    """f-m3-06 forwards the schema version past the M2 baseline (>=5)."""
    fake = _FakeAlpacaClient()
    PaperExecutor(fake, db_path=db_path)  # type: ignore[arg-type]

    assert db_module.CURRENT_VERSION >= 5, (
        "f-m3-06 must keep CURRENT_VERSION at >= 5 (the orders table "
        "is part of the M3 schema)"
    )
    conn = sqlite3.connect(db_path)
    try:
        version = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
    finally:
        conn.close()
    assert version == db_module.CURRENT_VERSION


# ---------------------------------------------------------------------------
# VAL-M3-032: Successful execute → exactly one row, submitted/filled status.
# ---------------------------------------------------------------------------


def test_successful_submission_writes_exactly_one_row(
    make_executor, db_path: Path
) -> None:
    """A successful ``execute()`` writes exactly one row to ``orders``.

    The cassette returns ``status='accepted'`` (a paper-sandbox
    submitted/accepted state). The persisted row must mirror that
    state, carry the broker order id, and the ``play_card_id`` from
    the input.
    """
    cassette = _load_cassette("order_call_roundtrip.json")
    fake = _FakeAlpacaClient(submit_order_results=[cassette["submit"]])
    executor, _ = make_executor(client=fake)

    play_card = _call_play_card()
    order_id = executor.execute(play_card)
    assert isinstance(order_id, str) and order_id

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM orders WHERE play_card_id = ?",
            (play_card["play_card_id"],),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, (
        "expected exactly one orders row per successful submission, "
        f"got {len(rows)}"
    )
    row = rows[0]
    assert row["alpaca_order_id"] == order_id
    assert row["status"] in {"submitted", "accepted", "filled"}
    assert row["reason"] is None, (
        "reason must be NULL on the success path (no broker error)"
    )
    assert row["symbol"] == "AXSM250620C00125000"
    assert row["side"] == "buy"
    assert row["qty"] == 2


def test_successful_submission_persists_filled_status_after_poll(
    make_executor, db_path: Path
) -> None:
    """After ``wait_for_fill`` walks accepted → filled, the persisted
    row reflects the terminal ``status='filled'`` state."""
    cassette = _load_cassette("order_call_roundtrip.json")
    fake = _FakeAlpacaClient(
        submit_order_results=[cassette["submit"]],
        get_order_results=[
            cassette["poll_intermediate"],
            cassette["poll_filled"],
        ],
    )
    executor, _ = make_executor(client=fake)

    order_id = executor.execute(_call_play_card())
    final = executor.wait_for_fill(
        order_id, timeout_seconds=5.0, poll_interval_seconds=0.0
    )
    assert final["status"] == "filled"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status FROM orders WHERE alpaca_order_id = ?",
            (order_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["status"] == "filled"


# ---------------------------------------------------------------------------
# VAL-M3-033: Rejection → exactly one row, status='rejected', reason set.
# ---------------------------------------------------------------------------


def test_broker_rejection_writes_rejected_row_with_reason(
    make_executor, db_path: Path
) -> None:
    """Broker 4xx (mocked here as :class:`AlpacaClientError`) yields
    a single ``status='rejected'`` row whose ``reason`` carries the
    broker message."""
    fake = _FakeAlpacaClient(
        submit_order_error=AlpacaClientError(
            "Alpaca API error: invalid symbol AXSM250620C00125000"
        )
    )
    executor, _ = make_executor(client=fake)

    with pytest.raises(OrderRejected, match="invalid symbol"):
        executor.execute(_call_play_card())

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT status, reason, alpaca_order_id "
            "FROM orders WHERE play_card_id = ?",
            ("AXSM-2025-04-27",),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, (
        "expected exactly one rejected row per failed submission, "
        f"got {len(rows)}"
    )
    row = rows[0]
    assert row["status"] == "rejected"
    assert row["reason"] is not None
    assert "invalid symbol" in row["reason"]
    # Broker never returned an id on rejection, so the column is null.
    assert row["alpaca_order_id"] is None


def test_rejection_does_not_leave_submitted_row_behind(
    make_executor, db_path: Path
) -> None:
    """Crucial VAL-M3-018 / VAL-M3-033 invariant: the rejection path
    must NEVER persist a ``status='submitted'`` (or ``'accepted'``)
    row from the same attempt — the orders table reflects the
    actual outcome, not an aspirational intent."""
    fake = _FakeAlpacaClient(
        submit_order_error=AlpacaClientError(
            "Alpaca API error: 422 Unprocessable Entity"
        )
    )
    executor, _ = make_executor(client=fake)

    with pytest.raises(OrderRejected):
        executor.execute(_call_play_card())

    conn = sqlite3.connect(db_path)
    try:
        leftover = conn.execute(
            "SELECT COUNT(*) FROM orders "
            "WHERE play_card_id = ? AND status IN ('submitted','accepted')",
            ("AXSM-2025-04-27",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert leftover == 0


def test_concurrency_cap_writes_rejected_row(
    make_executor, db_path: Path
) -> None:
    """Hitting :data:`MAX_CONCURRENT_PLAYS` writes a rejected row with
    a recognisable cap-exceeded reason; no broker call is made."""
    from biotech_sniper import config as cfg
    from biotech_sniper.paper_executor import ConcurrencyCapExceeded

    # f-m3-07b: caps count OPTION positions only — tag the synthetic
    # filler positions as ``us_option`` so the executor's filter
    # recognises them when applying the concurrency cap.
    positions = [
        {
            "symbol": f"FILLER{i}",
            "qty": 1,
            "avg_entry_price": 1.0,
            "asset_class": "us_option",
        }
        for i in range(cfg.MAX_CONCURRENT_PLAYS)
    ]
    fake = _FakeAlpacaClient(positions=positions)
    executor, _ = make_executor(client=fake)

    with pytest.raises(ConcurrencyCapExceeded):
        executor.execute(_call_play_card())

    # Broker never reached.
    assert fake.submit_calls == []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT status, reason FROM orders WHERE play_card_id = ?",
            ("AXSM-2025-04-27",),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["status"] == "rejected"
    assert "ConcurrencyCapExceeded" in rows[0]["reason"]


# ---------------------------------------------------------------------------
# VAL-M3-034: get_orders_for_play returns chronological rows.
# ---------------------------------------------------------------------------


def test_get_orders_for_play_returns_rows_chronologically(
    make_executor, db_path: Path
) -> None:
    """Multiple lifecycle events on the same play card surface in
    ``created_at ASC`` order — the helper sorts so callers do not.

    Sequence: entry submission → adverse-news exit attempt that
    rejects (concurrency cap-style scenario simulated via a broker
    error). Both rows belong to the same play card and the helper
    returns them in chronological order.
    """
    cassette = _load_cassette("order_call_roundtrip.json")
    fake = _FakeAlpacaClient(
        submit_order_results=[
            cassette["submit"],
        ],
    )
    executor, _ = make_executor(client=fake)

    play = _call_play_card()
    executor.execute(play)

    # Second attempt rejects; we swap the queued error in place
    # (``_FakeAlpacaClient`` exposes the field directly for tests).
    fake._submit_error = AlpacaClientError("Alpaca API error: 503")
    with pytest.raises(OrderRejected):
        executor.execute(play)
    fake._submit_error = None  # reset for hygiene

    rows = executor.get_orders_for_play(play["play_card_id"])
    assert len(rows) == 2
    statuses = [row["status"] for row in rows]
    assert statuses[0] in {"submitted", "accepted", "filled"}
    assert statuses[-1] == "rejected"
    # created_at is the chronological key — entries are non-decreasing.
    assert rows[0]["created_at"] <= rows[1]["created_at"]


def test_get_orders_for_play_chronology_with_event_tagged_exit(
    make_executor, db_path: Path
) -> None:
    """When an entry + an exit submission both succeed under the same
    play card, the helper interleaves them chronologically and the
    ``event`` enum on the exit row distinguishes it from the entry."""
    cassette = _load_cassette("order_call_roundtrip.json")
    fake = _FakeAlpacaClient(
        submit_order_results=[cassette["submit"], cassette["submit"]]
    )
    executor, _ = make_executor(client=fake)

    play = _call_play_card()
    executor.execute(play)

    exit_card = {
        "play_card_id": play["play_card_id"],
        "parent_play_card_id": play["play_card_id"],
        "event": "iv_crush",
        "option_legs": [
            {
                "symbol": play["option_legs"][0]["symbol"],
                "side": "sell",
                "qty": 1,
                "option_type": "call",
            }
        ],
    }
    executor.execute(exit_card)

    rows = executor.get_orders_for_play(play["play_card_id"])
    assert len(rows) == 2
    # Order is chronological; the entry is first, the exit second.
    assert rows[0]["created_at"] <= rows[1]["created_at"]
    assert rows[0]["event"] is None
    assert rows[1]["event"] == "iv_crush"
    assert rows[1]["parent_play_card_id"] == play["play_card_id"]


def test_get_orders_for_play_unknown_id_returns_empty_list(
    make_executor,
) -> None:
    """Unknown play_card_id is a benign empty result, not an error —
    callers can iterate the helper unconditionally without try/except.
    """
    executor, _ = make_executor()
    assert executor.get_orders_for_play("NONEXISTENT") == []


def test_get_orders_for_play_empty_string_returns_empty_list(
    make_executor,
) -> None:
    """Empty string play_card_id short-circuits to ``[]`` (no SQL run)."""
    executor, _ = make_executor()
    assert executor.get_orders_for_play("") == []
