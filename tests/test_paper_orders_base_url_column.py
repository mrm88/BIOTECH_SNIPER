"""Tests for the v11 → v12 ``paper_orders.base_url`` schema extension.

Feature ``f-misc-10-paper-orders-base-url-column`` (surfaced 2026-05-01
from f-cross-02 discoveredIssue #1) adds a ``base_url`` column to the
``paper_orders`` table, defaulting to
``'https://paper-api.alpaca.markets'``. The column makes audit
queries of the form

    SELECT COUNT(*) FROM paper_orders WHERE base_url NOT LIKE '%paper-api%';

directly runnable without grepping order journals — the paper-only
invariant is still enforced operationally by the ``LIVE_MODE`` two-flag
gate plus ``PAPER_BASE_URL`` validation in :class:`PaperExecutor`.

This file pins:

a) v11 → v12 upgrades a fresh db (``run_migrations_runner(db_path,
   target_version=12)``) adds exactly the ``base_url`` column with
   default ``'https://paper-api.alpaca.markets'``.
b) Re-running the migration on a v12 db is a no-op (idempotent).
c) Existing rows (inserted at v11 before the migration runs) get the
   default value backfilled by SQLite's
   ``ALTER TABLE ... ADD COLUMN ... DEFAULT '<value>'`` behaviour.
d) :class:`PaperExecutor` populates the column from
   ``settings.ALPACA_BASE_URL`` (i.e.
   :func:`biotech_sniper.config.get_alpaca_base_url`) on every
   ``paper_orders`` insert (success and rejection paths).
e) :data:`db.CURRENT_VERSION` is bumped from 11 to 12 so a fresh
   ``db.run_migrations(conn)`` (default target=CURRENT_VERSION)
   auto-bootstraps the column without an explicit
   ``runner.run(db_path, 12)`` call.

The tests use isolated ``tmp_path``-rooted SQLite databases so they
are hermetic and parallel-safe (``pytest -n 2``).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper import db
from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.migrations.runner import (
    load_migration,
    run as run_migrations_runner,
)
from biotech_sniper.paper_executor import (
    OrderRejected,
    PaperExecutor,
    PaperOnlyViolation,
)


_DEFAULT_BASE_URL = "https://paper-api.alpaca.markets"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _migrate_to(db_path: Path, target: int) -> dict:
    return run_migrations_runner(
        db_path, target_version=target, take_backup_first=False
    )


def _column_info(db_path: Path, table: str) -> dict[str, dict]:
    """Return ``{name: {type, notnull, dflt_value, pk}}`` from PRAGMA."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    out: dict[str, dict] = {}
    for cid, name, ctype, notnull, dflt, pk in rows:
        out[name] = {
            "type": ctype,
            "notnull": int(notnull),
            "dflt_value": dflt,
            "pk": int(pk),
        }
    return out


# ---------------------------------------------------------------------------
# 1) Module shape — FROM_VERSION / TO_VERSION / DESCRIPTION / apply
# ---------------------------------------------------------------------------


def test_migration_module_declares_canonical_version_markers() -> None:
    """v12 module must declare FROM_VERSION=11 / TO_VERSION=12 / apply()."""
    module = load_migration(12)
    assert module.FROM_VERSION == 11
    assert module.TO_VERSION == 12
    assert callable(getattr(module, "apply", None))
    description = getattr(module, "DESCRIPTION", "")
    assert isinstance(description, str) and description.strip()


def test_migration_012_file_exists_at_canonical_path() -> None:
    """The runner's load_migration(12) must resolve to a file on disk
    matching the canonical ``012_paper_orders_base_url.py`` name so
    future workers / validators can grep for it directly.
    """
    from biotech_sniper.migrations import runner as runner_mod

    migrations_dir = Path(runner_mod.__file__).resolve().parent
    candidate = migrations_dir / "012_paper_orders_base_url.py"
    assert candidate.is_file(), (
        f"expected {candidate} to exist for the v12 migration"
    )


# ---------------------------------------------------------------------------
# 2) v11 → v12 upgrade adds the base_url column with the right default
# ---------------------------------------------------------------------------


def test_v11_to_v12_upgrade_adds_base_url_column(tmp_path: Path) -> None:
    """A fresh v11 db gains exactly the ``base_url`` column on upgrade."""
    db_path = tmp_path / "alpha.db"
    summary_v11 = _migrate_to(db_path, 11)
    assert 11 in summary_v11["applied"] or summary_v11["from_version"] == 11

    pre_cols = _column_info(db_path, "paper_orders")
    assert "base_url" not in pre_cols, (
        "v11 baseline must NOT contain base_url column; "
        f"found pre-existing column with PRAGMA={pre_cols.get('base_url')!r}"
    )

    summary_v12 = _migrate_to(db_path, 12)
    assert summary_v12["from_version"] == 11
    assert summary_v12["applied"] == [12]

    post_cols = _column_info(db_path, "paper_orders")
    assert "base_url" in post_cols, "missing base_url column post-migration"
    info = post_cols["base_url"]
    assert info["type"].upper() == "TEXT"
    assert info["notnull"] == 0, (
        "base_url must remain nullable so no existing INSERT is broken"
    )
    # SQLite stores the literal default token as recorded in the DDL —
    # for a string default ``'https://paper-api.alpaca.markets'`` it
    # comes back wrapped in single quotes from PRAGMA.
    dflt = info["dflt_value"]
    assert dflt is not None, "base_url must have an explicit DEFAULT"
    assert _DEFAULT_BASE_URL in str(dflt), (
        f"base_url DEFAULT must contain {_DEFAULT_BASE_URL!r}, got {dflt!r}"
    )


def test_v11_to_v12_backfills_existing_rows_with_default(
    tmp_path: Path,
) -> None:
    """Pre-existing rows (inserted at v11) must end up with the default
    value in ``base_url`` after the v12 upgrade.

    SQLite's ``ALTER TABLE ... ADD COLUMN <col> <type> DEFAULT <val>``
    populates every existing row with the default value.
    """
    db_path = tmp_path / "alpha.db"
    _migrate_to(db_path, 11)

    # Seed a v11 ``paper_orders`` row using the canonical INSERT shape.
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO paper_orders (
                    id, play_card_id, alpaca_order_id, symbol, side, qty,
                    status, reason, event, parent_play_card_id,
                    requested_mid_at_submit, purpose, client_order_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-row-1",
                    "PCID-LEGACY",
                    None,
                    "MRNS250620C00010000",
                    "buy",
                    1,
                    "accepted",
                    None,
                    "open",
                    None,
                    1.25,
                    "entry",
                    "client-legacy-1",
                ),
            )
    finally:
        conn.close()

    _migrate_to(db_path, 12)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, base_url FROM paper_orders WHERE id = ?",
            ("legacy-row-1",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["base_url"] == _DEFAULT_BASE_URL


# ---------------------------------------------------------------------------
# 3) Idempotency — re-running on a v12 db is a no-op
# ---------------------------------------------------------------------------


def test_v12_migration_is_idempotent_on_v12_db(tmp_path: Path) -> None:
    """Running ``run(... target=12)`` twice must not error or duplicate work."""
    db_path = tmp_path / "alpha.db"
    _migrate_to(db_path, 12)

    conn = sqlite3.connect(str(db_path))
    try:
        sql_pre = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='paper_orders'"
        ).fetchone()[0]
        version_pre = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
    finally:
        conn.close()

    summary = _migrate_to(db_path, 12)
    assert summary["from_version"] == 12
    assert summary["no_op"] is True
    assert summary["applied"] == []

    conn = sqlite3.connect(str(db_path))
    try:
        sql_post = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='paper_orders'"
        ).fetchone()[0]
        version_post = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
    finally:
        conn.close()
    assert sql_pre == sql_post
    assert version_pre == version_post == 12


def test_v12_migration_apply_called_directly_is_idempotent(
    tmp_path: Path,
) -> None:
    """Calling ``module.apply(conn)`` directly on a v12 db must be a no-op.

    Catches any ``ALTER TABLE`` that would re-fail on duplicate column
    names — the canonical idempotency check for an additive
    ALTER-TABLE-ADD-COLUMN migration.
    """
    db_path = tmp_path / "alpha.db"
    _migrate_to(db_path, 12)

    module = load_migration(12)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.isolation_level = None
        conn.execute("BEGIN")
        try:
            module.apply(conn)  # MUST NOT raise
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    cols = _column_info(db_path, "paper_orders")
    assert "base_url" in cols
    assert cols["base_url"]["type"].upper() == "TEXT"


# ---------------------------------------------------------------------------
# 4) db.CURRENT_VERSION is bumped to 12 + auto-bootstrap path
# ---------------------------------------------------------------------------


def test_db_current_version_is_twelve() -> None:
    """f-misc-10: db.CURRENT_VERSION must be bumped from 11 to 12."""
    assert db.CURRENT_VERSION == 12


def test_paper_executor_default_run_migrations_targets_v12(
    tmp_path: Path,
) -> None:
    """A fresh ``db.run_migrations(conn)`` (default target=CURRENT_VERSION)
    auto-bootstraps a fresh db all the way to v12 — preserves the
    f-misc-06 invariant for downstream callers (``PaperExecutor``).
    """
    db_path = tmp_path / "alpha.db"
    conn = db.connect(db_path)
    try:
        applied = db.run_migrations(conn)  # default target=CURRENT_VERSION
    finally:
        conn.close()
    assert applied == 12

    cols = _column_info(db_path, "paper_orders")
    assert "base_url" in cols, "v12 base_url column missing after bootstrap"


# ---------------------------------------------------------------------------
# 5) PaperExecutor populates base_url from settings.ALPACA_BASE_URL
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Minimal duck-typed substitute for :class:`AlpacaClient`."""

    def __init__(
        self,
        *,
        base_url: str = PAPER_BASE_URL,
        submit_order_results: list[Any] | None = None,
        submit_order_error: Exception | None = None,
        positions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = base_url
        self._submit_results = list(submit_order_results or [])
        self._submit_error = submit_order_error
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
        # Smoke return — the tests below don't poll for fills.
        return {"id": order_id, "status": "accepted"}


def _accepted_submit_payload() -> dict[str, Any]:
    return {
        "id": "abc-12345-base-url-test",
        "client_order_id": "biotech-sniper-MRNS-call-base-url",
        "status": "accepted",
        "symbol": "MRNS250620C00010000",
        "side": "buy",
        "qty": 1,
        "filled_qty": 0,
        "filled_avg_price": None,
        "submitted_at": "2026-05-01T15:00:00Z",
        "order_class": "simple",
    }


def _entry_play_card() -> dict[str, Any]:
    return {
        "play_card_id": "MRNS-2026-05-01",
        "ticker": "MRNS",
        "option_legs": [
            {
                "symbol": "MRNS250620C00010000",
                "side": "buy",
                "qty": 1,
                "limit_price": 1.10,
                "option_type": "call",
                "strike": 10.0,
                "expiry": "2026-06-20",
                "client_order_id": "biotech-sniper-MRNS-call-base-url",
                "bid": 1.05,
                "ask": 1.15,
            }
        ],
    }


def test_executor_persists_default_base_url_on_successful_insert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful ``execute()`` writes the default paper base_url
    when ``ALPACA_BASE_URL`` is unset (config default).
    """
    monkeypatch.delenv("ALPACA_BASE_URL", raising=False)
    db_path = tmp_path / "alpha.db"

    fake = _FakeAlpacaClient(submit_order_results=[_accepted_submit_payload()])
    executor = PaperExecutor(
        fake,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )
    executor.execute(_entry_play_card())

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT base_url FROM paper_orders WHERE play_card_id = ?",
            ("MRNS-2026-05-01",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["base_url"] == _DEFAULT_BASE_URL


def test_executor_persists_base_url_from_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``settings.ALPACA_BASE_URL`` (env var) flows into ``paper_orders.base_url``.

    The test sets ``ALPACA_BASE_URL`` to a paper-host alias (still a
    paper endpoint — the constructor validates the client's
    ``base_url`` is exactly :data:`PAPER_BASE_URL` regardless of env
    config). Even though the alias differs from ``PAPER_BASE_URL``,
    :func:`config.get_alpaca_base_url` returns whatever the env says,
    and that value is what the executor records.
    """
    custom_url = "https://paper-api.alpaca.markets/v2/custom-suffix"
    monkeypatch.setenv("ALPACA_BASE_URL", custom_url)
    db_path = tmp_path / "alpha.db"

    fake = _FakeAlpacaClient(submit_order_results=[_accepted_submit_payload()])
    executor = PaperExecutor(
        fake,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )
    executor.execute(_entry_play_card())

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT base_url FROM paper_orders WHERE play_card_id = ?",
            ("MRNS-2026-05-01",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["base_url"] == custom_url


def test_executor_persists_base_url_on_rejection_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejection-path inserts also stamp the ``base_url`` column.

    The executor's audit-trail invariant holds equally on the
    rejection (``status='rejected'``) row — every ``paper_orders``
    INSERT includes ``base_url``.
    """
    from biotech_sniper.alpaca_client import AlpacaClientError

    monkeypatch.delenv("ALPACA_BASE_URL", raising=False)
    db_path = tmp_path / "alpha.db"

    fake = _FakeAlpacaClient(
        submit_order_error=AlpacaClientError("simulated_broker_rejection")
    )
    executor = PaperExecutor(
        fake,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )
    with pytest.raises(OrderRejected):
        executor.execute(_entry_play_card())

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status, base_url FROM paper_orders "
            "WHERE play_card_id = ?",
            ("MRNS-2026-05-01",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["status"] == "rejected"
    assert row["base_url"] == _DEFAULT_BASE_URL


# ---------------------------------------------------------------------------
# 6) Audit query — base_url filter is directly runnable post-migration
# ---------------------------------------------------------------------------


def test_paper_only_audit_query_returns_zero_post_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contract evidence form ``WHERE base_url NOT LIKE '%paper-api%'``
    is now directly runnable — and returns ``0`` for any DB whose
    inserts originated through :class:`PaperExecutor`.
    """
    monkeypatch.delenv("ALPACA_BASE_URL", raising=False)
    db_path = tmp_path / "alpha.db"

    fake = _FakeAlpacaClient(submit_order_results=[_accepted_submit_payload()])
    executor = PaperExecutor(
        fake,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )
    executor.execute(_entry_play_card())

    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM paper_orders "
            "WHERE base_url NOT LIKE '%paper-api%'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0
