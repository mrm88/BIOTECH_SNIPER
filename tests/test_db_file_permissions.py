"""Regression tests for VAL-M2-001: SQLite DB file mode is 0o640.

The on-disk SQLite database (``data/alpha_sniper.db``) contains play
data, LLM-debate transcripts, audit JSON, and configuration. The
mission contract requires the file mode to be at most ``0o640`` —
readable by the owner and the owner's group only, never world-readable.

These tests verify that :func:`biotech_sniper.db.connect` enforces
the desired mode idempotently:

* The first connect (which creates the file with the process umask
  default of 0o644) tightens the mode to 0o640.
* A subsequent connect on a file that is already 0o640 does NOT
  re-issue ``os.chmod`` (avoids spurious filesystem writes).
* If a third party loosens the mode (e.g. someone runs ``chmod 644``
  manually), the next connect restores 0o640.
* Tightening to 0o600 is allowed but the next connect bumps it back
  to 0o640 — the contract is "exactly 0o640" so future systemd group
  access does not silently fail.
* In-memory connections (``:memory:``) do not crash and do not chmod
  any unrelated file.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest import mock

import pytest

from biotech_sniper import db


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode) & 0o777


def test_first_connect_tightens_mode_to_0o640(tmp_path):
    """Creating the db via :func:`connect` leaves the file at 0o640."""
    db_path = tmp_path / "alpha_sniper.db"
    assert not db_path.exists()

    conn = db.connect(db_path)
    try:
        assert db_path.exists()
        assert _mode(db_path) == 0o640
    finally:
        conn.close()


def test_second_connect_is_idempotent_no_chmod_call(tmp_path):
    """Re-connecting to a file already at 0o640 must NOT call os.chmod."""
    db_path = tmp_path / "alpha_sniper.db"
    conn = db.connect(db_path)
    conn.close()
    assert _mode(db_path) == 0o640

    with mock.patch("biotech_sniper.db.os.chmod") as chmod_mock:
        conn2 = db.connect(db_path)
        conn2.close()
        assert chmod_mock.call_count == 0
    assert _mode(db_path) == 0o640


def test_loose_mode_is_tightened_on_next_connect(tmp_path):
    """If the file mode drifts wider than 0o640, the next connect repairs it."""
    db_path = tmp_path / "alpha_sniper.db"
    conn = db.connect(db_path)
    conn.close()
    # Simulate a manual ``chmod 0644`` (the umask default).
    os.chmod(db_path, 0o644)
    assert _mode(db_path) == 0o644

    conn2 = db.connect(db_path)
    conn2.close()
    assert _mode(db_path) == 0o640


def test_tight_mode_is_relaxed_to_0o640_on_next_connect(tmp_path):
    """A 0o600 mode must be reset to 0o640 (group read is required for systemd)."""
    db_path = tmp_path / "alpha_sniper.db"
    conn = db.connect(db_path)
    conn.close()
    os.chmod(db_path, 0o600)
    assert _mode(db_path) == 0o600

    conn2 = db.connect(db_path)
    conn2.close()
    assert _mode(db_path) == 0o640


def test_memory_connection_does_not_crash(tmp_path, monkeypatch):
    """``:memory:`` connections must not attempt to chmod anything."""
    # Spy on os.chmod to make sure no spurious chmod happens for the
    # in-memory database path.
    chmod_calls: list = []
    real_chmod = os.chmod

    def _spy_chmod(path, mode):  # type: ignore[no-untyped-def]
        chmod_calls.append((str(path), mode))
        return real_chmod(path, mode)

    monkeypatch.setattr(db.os, "chmod", _spy_chmod)
    conn = db.connect(":memory:")
    try:
        # Smoke-check the connection works.
        conn.execute("CREATE TABLE t(a INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        assert conn.execute("SELECT a FROM t").fetchone()[0] == 1
    finally:
        conn.close()
    # No chmod call should have been issued for the in-memory DB.
    assert chmod_calls == []


def test_db_file_mode_constant_is_0o640():
    """The exported constant matches the contract expectation."""
    assert db.DB_FILE_MODE == 0o640
