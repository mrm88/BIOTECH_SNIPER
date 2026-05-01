"""Schema-and-WAL invariants regression test (f-cross-04).

This module pins the cross-cutting schema-integrity contract:

* **VAL-CROSS-013** — ``schema_version`` is monotonically increasing
  and reaches the mission-max version (``v10`` for Reading-B; ``v11``
  is reserved for a hypothetical M3 expansion that did not land,
  so the assertion is "≥ 10").
* **VAL-CROSS-014** — every fresh connection opened via
  :func:`biotech_sniper.db.connect` has ``PRAGMA journal_mode = wal``
  AND ``PRAGMA foreign_keys = ON``.
* **VAL-CROSS-015 / VAL-M1-061** — the migration runner is
  forward-only: a ``--target`` lower than the current
  ``schema_version`` raises :class:`DowngradeForbidden` and leaves
  the database untouched.
* **VAL-CROSS-016** — the SQLite WAL single-writer invariant holds.
  Two concurrent writers serialise (the second waits for the first
  to commit and then succeeds) and the database is not corrupted.
* **VAL-CROSS-041** — ``schema_version.version`` enforces uniqueness
  (declared as ``INTEGER PRIMARY KEY`` in ``schema.sql``); a
  duplicate-version INSERT raises ``sqlite3.IntegrityError`` with
  ``UNIQUE constraint failed: schema_version.version``.

The fixture builds a tmp-path SQLite db at ``schema_version=10`` so
each test runs against a self-contained, byte-clean database — no
production VPS access is required.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest

from biotech_sniper import db
from biotech_sniper.migrations.runner import (
    DowngradeForbidden,
    run as run_migrations_runner,
)


# Reading-B caps the schema at v10. v11 is reserved for a future
# M3 expansion that did NOT land (the M1 v10 migration provisioned
# every Stage-2 table). The invariant test therefore asserts
# ``MAX(version) >= MISSION_MAX_VERSION`` so a future v11 bump
# does not break the regression.
MISSION_MAX_VERSION: int = 10


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_v10_db(tmp_path: Path) -> Path:
    """Create a tmp-path SQLite db at schema_version=10 (WAL on)."""
    db_path = tmp_path / "alpha.db"
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
    finally:
        conn.close()
    summary = run_migrations_runner(
        db_path, target_version=10, take_backup_first=False
    )
    assert summary["to_version"] == 10, summary
    return db_path


@pytest.fixture
def v10_db(tmp_path: Path) -> Iterator[Path]:
    yield _build_v10_db(tmp_path)


def _max_version(db_path: Path) -> int:
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()
    finally:
        conn.close()
    return int(row["v"])


# ---------------------------------------------------------------------------
# VAL-CROSS-013 — monotonically increasing & reaches mission-max
# ---------------------------------------------------------------------------


def test_schema_version_is_monotonic_and_reaches_mission_max(
    v10_db: Path,
) -> None:
    conn = db.connect(v10_db)
    try:
        rows = conn.execute(
            "SELECT version, applied_at FROM schema_version "
            "ORDER BY version"
        ).fetchall()
    finally:
        conn.close()
    versions = [int(r["version"]) for r in rows]
    timestamps = [r["applied_at"] for r in rows]
    assert versions, "schema_version table is empty"
    # Strictly monotonic (no duplicates, ascending).
    assert versions == sorted(versions), (
        f"versions not monotonically increasing: {versions}"
    )
    assert len(versions) == len(set(versions)), (
        f"duplicate version rows: {versions}"
    )
    assert max(versions) >= MISSION_MAX_VERSION, (
        f"max(schema_version)={max(versions)} < mission max "
        f"{MISSION_MAX_VERSION}"
    )
    assert all(ts is not None for ts in timestamps), (
        f"applied_at NULL in {timestamps}"
    )
    # ISO-8601 UTC timestamps sort lexicographically — a non-decreasing
    # sequence under string sort therefore is a non-decreasing
    # sequence in time.
    assert timestamps == sorted(timestamps), (
        f"applied_at not non-decreasing: {timestamps}"
    )


def test_schema_version_has_no_null_columns(v10_db: Path) -> None:
    conn = db.connect(v10_db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM schema_version "
            "WHERE applied_at IS NULL OR version IS NULL"
        ).fetchone()
    finally:
        conn.close()
    assert int(n["c"]) == 0


# ---------------------------------------------------------------------------
# VAL-CROSS-014 — WAL + foreign_keys=ON on every fresh connection
# ---------------------------------------------------------------------------


def test_fresh_connection_uses_wal_journal_mode(v10_db: Path) -> None:
    """journal_mode is wal for every fresh on-disk connection."""
    for _ in range(3):
        conn = db.connect(v10_db)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        assert str(mode).lower() == "wal", mode


def test_fresh_connection_enables_foreign_keys(v10_db: Path) -> None:
    """foreign_keys is ON (=1) for every fresh connection."""
    for _ in range(3):
        conn = db.connect(v10_db)
        try:
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        finally:
            conn.close()
        assert int(fk) == 1, fk


def test_foreign_key_check_is_clean(v10_db: Path) -> None:
    """No FK violations after migrating to v10."""
    conn = db.connect(v10_db)
    try:
        rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()
    assert rows == [], f"foreign_key_check found violations: {rows}"


# ---------------------------------------------------------------------------
# VAL-CROSS-015 / VAL-M1-061 — forward-only / DowngradeForbidden
# ---------------------------------------------------------------------------


def test_migration_runner_refuses_downgrade(v10_db: Path) -> None:
    """A target < current raises DowngradeForbidden, schema unchanged."""
    before = _max_version(v10_db)
    assert before >= MISSION_MAX_VERSION

    with pytest.raises(DowngradeForbidden):
        run_migrations_runner(
            v10_db, target_version=before - 1, take_backup_first=False
        )

    after = _max_version(v10_db)
    assert after == before


def test_migration_runner_refuses_downgrade_to_zero(v10_db: Path) -> None:
    """Downgrade to v0 is also forbidden — exhaustive lower bound."""
    before = _max_version(v10_db)
    with pytest.raises(DowngradeForbidden):
        run_migrations_runner(
            v10_db, target_version=0, take_backup_first=False
        )
    assert _max_version(v10_db) == before


# ---------------------------------------------------------------------------
# VAL-CROSS-041 — UNIQUE constraint on schema_version.version
# ---------------------------------------------------------------------------


def test_schema_version_table_declares_unique_or_pk_on_version(
    v10_db: Path,
) -> None:
    """``schema.sql`` MUST declare version as PRIMARY KEY (or UNIQUE)."""
    conn = db.connect(v10_db)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='schema_version'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    sql = (row["sql"] or "").lower()
    # INTEGER PRIMARY KEY is the canonical SQLite uniqueness clause;
    # UNIQUE INDEX or UNIQUE column-constraint is also acceptable.
    assert "primary key" in sql or "unique" in sql, sql


def test_double_insert_of_same_version_raises_unique_constraint(
    v10_db: Path,
) -> None:
    """INSERT INTO schema_version VALUES(10) twice raises IntegrityError.

    The fixture already inserted the v10 row via the migration
    runner, so a fresh INSERT of the same version triggers the
    PRIMARY KEY uniqueness check.
    """
    conn = db.connect(v10_db)
    try:
        with pytest.raises(sqlite3.IntegrityError) as excinfo:
            conn.execute(
                "INSERT INTO schema_version (version, description) "
                "VALUES (?, ?)",
                (10, "duplicate-attempt"),
            )
        msg = str(excinfo.value).lower()
        assert "unique" in msg or "primary key" in msg, excinfo.value
        assert "schema_version" in msg, excinfo.value
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# VAL-CROSS-016 — concurrent-writer single-writer invariant
# ---------------------------------------------------------------------------


def test_concurrent_writers_serialise_without_corruption(
    v10_db: Path,
) -> None:
    """SQLite WAL serialises writers; second writer waits then succeeds.

    Two threads each insert a row into ``schema_version``. The first
    holds an explicit ``BEGIN IMMEDIATE`` for ~250 ms while the
    second attempts to write. Python's sqlite3 default
    ``busy_timeout`` (5000 ms) makes the second writer wait for the
    first to commit, after which it proceeds without raising
    ``database is locked``. The final database passes
    ``PRAGMA integrity_check``.
    """
    barrier = threading.Barrier(2)
    errors: list[str] = []

    def writer(idx: int) -> None:
        try:
            conn = db.connect(v10_db)
            try:
                if idx == 0:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "INSERT INTO schema_version "
                        "(version, description) VALUES (?, ?)",
                        (900 + idx, f"writer-{idx}"),
                    )
                    barrier.wait(timeout=5)
                    # Hold the writer-lock briefly so writer-1 has
                    # to wait. 250 ms is well under the 5 s
                    # busy_timeout, so the second writer is
                    # guaranteed to succeed.
                    time.sleep(0.25)
                    conn.execute("COMMIT")
                else:
                    barrier.wait(timeout=5)
                    # Tiny delay so writer-0's BEGIN IMMEDIATE
                    # definitely lands first.
                    time.sleep(0.05)
                    conn.execute(
                        "INSERT INTO schema_version "
                        "(version, description) VALUES (?, ?)",
                        (900 + idx, f"writer-{idx}"),
                    )
                    conn.commit()
            finally:
                conn.close()
        except Exception as exc:  # pragma: no cover - surfaced via assert
            errors.append(f"writer-{idx}: {exc!r}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    for t in threads:
        assert not t.is_alive(), f"writer thread did not finish: {t}"
    assert errors == [], errors

    # Both rows landed and the database is internally consistent.
    conn = db.connect(v10_db)
    try:
        chk = conn.execute("PRAGMA integrity_check").fetchone()[0]
        rows = conn.execute(
            "SELECT version FROM schema_version "
            "WHERE version IN (900, 901) "
            "ORDER BY version"
        ).fetchall()
    finally:
        conn.close()
    assert str(chk).lower() == "ok", chk
    assert [int(r["version"]) for r in rows] == [900, 901]


def test_concurrent_writer_does_not_emit_database_is_locked(
    v10_db: Path,
) -> None:
    """The second writer's INSERT must not surface 'database is locked'.

    Independent regression of the busy-timeout behaviour: with WAL
    + the project's default sqlite3 busy_timeout, a contended writer
    blocks rather than raising ``OperationalError``.
    """
    started = threading.Event()
    release = threading.Event()
    raised: list[str] = []

    def hold_writer_lock() -> None:
        conn = db.connect(v10_db)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO schema_version (version, description) "
                "VALUES (?, ?)",
                (910, "lock-holder"),
            )
            started.set()
            release.wait(timeout=5)
            conn.execute("COMMIT")
        finally:
            conn.close()

    holder = threading.Thread(target=hold_writer_lock)
    holder.start()
    try:
        assert started.wait(timeout=5), "lock holder never started"
        # Now try to write from this thread; the busy_timeout (5s)
        # will let us through once the holder commits.
        contender = db.connect(v10_db)
        try:
            t0 = time.monotonic()
            release.set()  # tell the holder to commit
            try:
                contender.execute(
                    "INSERT INTO schema_version "
                    "(version, description) VALUES (?, ?)",
                    (911, "contender"),
                )
                contender.commit()
            except sqlite3.OperationalError as exc:
                raised.append(repr(exc))
            elapsed = time.monotonic() - t0
        finally:
            contender.close()
    finally:
        holder.join(timeout=5)

    assert not holder.is_alive(), "holder thread did not finish"
    assert raised == [], (
        f"contender saw OperationalError (likely 'database is locked'): "
        f"{raised}"
    )
    # Sanity: the contender did not wait pathologically long.
    assert elapsed < 5.0, f"contender waited too long: {elapsed:.2f}s"
