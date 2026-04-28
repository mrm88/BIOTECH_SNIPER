"""Regression tests for f-cross-06 — paper_orders event-check whitespace fix.

The production VPS database carries the ``paper_orders.event`` CHECK
fragment as a multiline snippet (because an older revision of
:func:`biotech_sniper.db._recreate_paper_orders_with_full_constraints`
wrapped the IN-list across two lines)::

    event IN ('open','iv_crush_exit','stop_loss',
              'adverse_news','rotation')

Pre-fix, :func:`biotech_sniper.db._paper_orders_table_has_event_check`
ran a naïve substring match against the single-line canonical
fragment, missed the multiline form, returned ``False`` on every
connect, and forced
:func:`biotech_sniper.db._recreate_paper_orders_with_full_constraints`
to fire on every migration.  The recreate dropped ``paper_orders``
without first dropping the dependent ``v_execution_stats`` view,
leaving the view dangling and the subsequent ``ALTER TABLE
paper_orders__new RENAME TO paper_orders`` failing with
``sqlite3.OperationalError: no such table: main.paper_orders``.

These tests pin both halves of the fix:

* **(a)** ``test_run_migrations_idempotent_on_canonical_schema`` —
  running :func:`biotech_sniper.db.run_migrations` twice on a
  greenfield (canonical) database leaves the
  ``sqlite_master`` ``CREATE TABLE paper_orders`` text byte-identical
  pre and post the second invocation. Catches a regression that
  re-introduces the unnecessary recreate on every connect.

* **(b)** ``test_recreate_preserves_v_execution_stats_view`` —
  manually creating ``v_execution_stats`` and forcing the recreate
  helper to run no longer leaves a dangling view; the view exists
  post-recreate with the same DDL it had pre-recreate.

* **(c)** ``test_event_check_helper_handles_multiline_fragment`` —
  passing a multiline ``CREATE TABLE`` snapshot to
  :func:`biotech_sniper.db._paper_orders_table_has_event_check`
  returns ``True`` after the whitespace + comma normalisation fix.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from biotech_sniper import db
from biotech_sniper.db import (
    _paper_orders_table_has_event_check,
    _recreate_paper_orders_with_full_constraints,
)


# Mirrors the v_execution_stats DDL emitted by
# ``biotech_sniper.training.build_execution_dataset`` so tests do not
# have to import that heavyweight module just to exercise view
# capture/restore.
_V_EXECUTION_STATS_DDL = """
CREATE VIEW v_execution_stats AS
SELECT
    SUBSTR(po.symbol, 1, LENGTH(po.symbol) - 15)              AS ticker,
    AVG(ef.slippage_bps)                                      AS mean_slippage_bps,
    MAX(ef.slippage_bps)                                      AS p95_slippage_bps,
    CAST(COUNT(DISTINCT CASE WHEN ef.id IS NOT NULL THEN po.id END) AS REAL)
        / NULLIF(COUNT(DISTINCT po.id), 0)                    AS fill_rate,
    CAST(COUNT(DISTINCT CASE WHEN ef.partial_qty_remaining > 0
                              THEN po.id END) AS REAL)
        / NULLIF(COUNT(DISTINCT po.id), 0)                    AS partial_fill_rate,
    AVG(ef.time_to_fill_ms)                                   AS mean_time_to_fill_ms,
    COUNT(DISTINCT po.id)                                     AS n_orders
FROM paper_orders po
LEFT JOIN execution_fills ef ON ef.paper_order_id = po.id
WHERE po.symbol IS NOT NULL
  AND LENGTH(po.symbol) > 15
GROUP BY SUBSTR(po.symbol, 1, LENGTH(po.symbol) - 15)
HAVING COUNT(DISTINCT CASE WHEN ef.id IS NOT NULL THEN po.id END) >= 1
""".strip()


def test_run_migrations_idempotent_on_canonical_schema(tmp_path: Path) -> None:
    """Running :func:`run_migrations` twice on a canonical greenfield db is a no-op.

    Pre-fix this assertion failed because the event-check helper
    returned False on every connect (whitespace mismatch), forcing
    the recreate dance to fire and rewrite the table on every call.
    The post-fix invariant: the ``sqlite_master`` snapshot of
    ``CREATE TABLE paper_orders`` is byte-identical across the two
    invocations.
    """
    db_path = tmp_path / "canonical.db"
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
        pre_row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='paper_orders'"
        ).fetchone()
        assert pre_row is not None
        pre_sql = pre_row["sql"]
    finally:
        conn.close()

    # Re-open and migrate again — the helper should detect the
    # event/purpose/client_order_id fragments and skip the recreate.
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
        post_row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='paper_orders'"
        ).fetchone()
        assert post_row is not None
        post_sql = post_row["sql"]
    finally:
        conn.close()

    assert pre_sql == post_sql, (
        "paper_orders CREATE TABLE text changed across consecutive "
        f"migrations — recreate fired unnecessarily.\n"
        f"pre[:200]={pre_sql[:200]!r}\n"
        f"post[:200]={post_sql[:200]!r}"
    )


def test_recreate_preserves_v_execution_stats_view(tmp_path: Path) -> None:
    """Force :func:`_recreate_paper_orders_with_full_constraints` to run with a
    dependent ``v_execution_stats`` view present and assert the view survives.

    Pre-fix, ``DROP TABLE paper_orders`` either failed or left the
    view dangling (depending on SQLite version), and the subsequent
    ``ALTER TABLE paper_orders__new RENAME TO paper_orders`` raised
    ``no such table: main.paper_orders``. Post-fix the recreate
    captures the view DDL beforehand, drops the view, runs the
    table swap, and restores the view atomically.
    """
    db_path = tmp_path / "view_safe.db"
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)

        # Manually create v_execution_stats with the canonical DDL.
        conn.execute(_V_EXECUTION_STATS_DDL)
        conn.commit()
        view_pre = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='view' AND name='v_execution_stats'"
        ).fetchone()
        assert view_pre is not None
        pre_view_sql = view_pre["sql"]

        # Force the recreate dance manually (the dependency tree is
        # exactly what the production VPS-deploy hits when the
        # event-check helper returns False on a multiline fragment).
        conn.isolation_level = None
        conn.execute("BEGIN")
        try:
            _recreate_paper_orders_with_full_constraints(conn)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        # The view must still exist with byte-identical DDL.
        view_post = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='view' AND name='v_execution_stats'"
        ).fetchone()
        assert view_post is not None, (
            "v_execution_stats view was dropped without being restored — "
            "the recreate is no longer view-dependency-safe"
        )
        assert view_post["sql"] == pre_view_sql

        # And the view must still be queryable (i.e. not dangling).
        rows = conn.execute(
            "SELECT * FROM v_execution_stats"
        ).fetchall()
        assert isinstance(rows, list)
    finally:
        conn.close()


def test_event_check_helper_handles_multiline_fragment(tmp_path: Path) -> None:
    """Pass a production-style multiline ``CREATE TABLE`` snapshot to
    :func:`_paper_orders_table_has_event_check` and assert it returns ``True``.

    This is the direct unit-level reproduction of the f-cross-06 bug:
    pre-fix the substring match missed the multiline IN-list and
    returned False, triggering the buggy recreate on every connect.
    """
    db_path = tmp_path / "legacy_multiline.db"
    raw = sqlite3.connect(str(db_path))
    raw.row_factory = sqlite3.Row
    try:
        # Mirrors the exact form observed on the production VPS db
        # that f-cross-06 reproduces: the IN-list wraps across two
        # lines because an older revision of the recreate helper
        # emitted it that way.
        raw.execute(
            """
            CREATE TABLE paper_orders (
                id                          TEXT    NOT NULL PRIMARY KEY,
                play_card_id                TEXT,
                alpaca_order_id             TEXT,
                symbol                      TEXT,
                side                        TEXT,
                qty                         INTEGER,
                status                      TEXT    NOT NULL,
                reason                      TEXT,
                event                       TEXT    CHECK(
                    event IS NULL OR
                    event IN ('open','iv_crush_exit','stop_loss',
                              'adverse_news','rotation')
                ),
                parent_play_card_id         TEXT,
                requested_mid_at_submit     REAL,
                purpose                     TEXT    CHECK(
                    purpose IS NULL OR
                    purpose IN ('entry','exit','liquidity_probe')
                ),
                client_order_id             TEXT    NOT NULL UNIQUE,
                created_at                  TEXT    NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        raw.commit()

        assert _paper_orders_table_has_event_check(raw) is True, (
            "_paper_orders_table_has_event_check should normalise "
            "whitespace + comma-spacing and detect the multiline "
            "production-style event CHECK fragment as present."
        )
    finally:
        raw.close()


def test_run_migrations_self_heals_legacy_multiline_event_check(
    tmp_path: Path,
) -> None:
    """End-to-end: a legacy db whose ``paper_orders`` carries the
    multiline event CHECK is migrated successfully (recreate is
    permitted to fire because the helper returns True post-fix on
    the resulting canonical schema, but if it does fire, the view
    capture/restore keeps it correct).

    Concretely: seed a legacy db with the multiline event check AND
    a ``v_execution_stats`` view, run :func:`run_migrations`, and
    assert (a) the run succeeds, (b) ``paper_orders`` still exists
    after, (c) ``v_execution_stats`` still exists after, (d) the
    final schema is the canonical single-line form so a SECOND
    migration is a no-op.
    """
    db_path = tmp_path / "legacy_view.db"
    raw = sqlite3.connect(str(db_path))
    try:
        # Minimal legacy seed: multiline paper_orders + view.
        raw.execute(
            """
            CREATE TABLE schema_version (
                version       INTEGER PRIMARY KEY,
                applied_at    TEXT    NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                description   TEXT
            )
            """
        )
        raw.execute(
            "INSERT INTO schema_version (version, description) "
            "VALUES (8, 'synthetic legacy multiline event check')"
        )
        raw.execute(
            """
            CREATE TABLE paper_orders (
                id                          TEXT    NOT NULL PRIMARY KEY,
                play_card_id                TEXT,
                alpaca_order_id             TEXT,
                symbol                      TEXT,
                side                        TEXT,
                qty                         INTEGER,
                status                      TEXT    NOT NULL,
                reason                      TEXT,
                event                       TEXT    CHECK(
                    event IS NULL OR
                    event IN ('open','iv_crush_exit','stop_loss',
                              'adverse_news','rotation')
                ),
                parent_play_card_id         TEXT,
                requested_mid_at_submit     REAL,
                purpose                     TEXT    CHECK(
                    purpose IS NULL OR
                    purpose IN ('entry','exit','liquidity_probe')
                ),
                client_order_id             TEXT    NOT NULL UNIQUE,
                created_at                  TEXT    NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        # execution_fills is referenced by the view; create the
        # minimal stub so the view can be defined.
        raw.execute(
            """
            CREATE TABLE execution_fills (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_order_id              TEXT    NOT NULL,
                filled_at                   TEXT    NOT NULL,
                filled_price                REAL    NOT NULL,
                filled_qty                  INTEGER NOT NULL,
                requested_mid_at_submit     REAL    NOT NULL,
                slippage_bps                REAL    NOT NULL,
                slippage_usd                REAL    NOT NULL,
                time_to_fill_ms             INTEGER NOT NULL,
                partial_qty_remaining       INTEGER NOT NULL,
                FOREIGN KEY (paper_order_id) REFERENCES paper_orders(id)
            )
            """
        )
        raw.execute(_V_EXECUTION_STATS_DDL)
        raw.commit()
    finally:
        raw.close()

    # Run migrations through the public entry point — must not raise.
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)

        # paper_orders survives.
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='paper_orders'"
            ).fetchone()
            is not None
        )
        # v_execution_stats survives the migration regardless of
        # whether the recreate dance fired (the post-fix behaviour
        # is "did NOT fire because the helper detected the multiline
        # form as canonical", but the view-safe path is also
        # validated separately by
        # ``test_recreate_preserves_v_execution_stats_view``).
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='view' AND name='v_execution_stats'"
            ).fetchone()
            is not None
        )

        # First-pass canonicalised paper_orders snapshot.
        first_sql = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='paper_orders'"
        ).fetchone()["sql"]

        # Re-running should be a no-op.
        db.run_migrations(conn)
        second_sql = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='paper_orders'"
        ).fetchone()["sql"]
        assert first_sql == second_sql
    finally:
        conn.close()
