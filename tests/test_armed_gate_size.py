"""Tests for the f-fix-live-02 non-zero-size invariant on the
Reading-B Stage-2 ``.armed`` filesystem gate.

VAL-LIVE-003 requires ``--live`` to refuse a zero-byte ``.armed``
file: a bare ``touch /root/alpha_sniper/.armed`` MUST NOT arm the
system. This test module pins three behavioural points that
distinguish the "exists" check (insufficient) from the "exists with
non-zero size" check (canonical):

* ``armed_file_zero_bytes_rejects`` — zero-byte file rejects with the
  canonical ``armed_file_missing`` reason (the reason string is
  preserved to keep ``news_match_log.reason`` consumers stable —
  see grep evidence in the f-fix-live-02 worker handoff).
* ``armed_file_one_byte_passes`` — a single byte is sufficient.
* ``armed_file_with_armed_at_timestamp_passes`` — the canonical
  real-world content shape produced by f-live-04 (a single
  ``armed_at=YYYY-MM-DDTHH:MM:SSZ`` line).

These tests intentionally live in their own module so the size
invariant can be located by grep without diluting the broader
armed-gate behaviour suite in ``tests/test_armed_gate.py``.
"""

from __future__ import annotations

from pathlib import Path

from biotech_sniper.llm.stage2_gates import (
    GATE_REASON_ARMED_FILE_MISSING,
    armed_gate,
)


def test_armed_file_zero_bytes_rejects(tmp_path: Path) -> None:
    """A zero-byte ``.armed`` file MUST be treated as absent.

    ``touch`` produces a regular readable file with size 0; without
    this size guard, a stray ``touch`` on the production VPS would
    arm Stage-2 (which the f-fix-live-02 contract forbids).
    """
    armed = tmp_path / ".armed"
    armed.write_bytes(b"")
    assert armed.exists() and armed.stat().st_size == 0
    res = armed_gate(armed_path=armed)
    assert res.passed is False
    assert res.reason == GATE_REASON_ARMED_FILE_MISSING


def test_armed_file_one_byte_passes(tmp_path: Path) -> None:
    """A single-byte ``.armed`` file is sufficient to arm Stage-2."""
    armed = tmp_path / ".armed"
    armed.write_bytes(b"1")
    assert armed.stat().st_size == 1
    res = armed_gate(armed_path=armed)
    assert res.passed is True
    assert res.reason is None


def test_armed_file_with_armed_at_timestamp_passes(tmp_path: Path) -> None:
    """Real-world content (``armed_at=YYYY-MM-DDTHH:MM:SSZ``) passes.

    Pins the f-live-04 content convention: the operator-supplied
    ``.armed`` file carries a single ``armed_at=<UTC ISO>`` line so
    audit consumers can correlate the arming moment with subsequent
    ledger writes.
    """
    armed = tmp_path / ".armed"
    armed.write_text("armed_at=2026-05-02T13:14:15Z\n")
    assert armed.stat().st_size > 0
    res = armed_gate(armed_path=armed)
    assert res.passed is True
    assert res.reason is None
