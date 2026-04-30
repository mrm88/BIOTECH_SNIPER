"""Tests for the Reading-B Stage-2 ``.armed`` filesystem gate.

Feature: f-m3-06-armed-file-gate.

The Stage-2 dispatcher composes a small set of pure-function gates in
cheap-first order to decide whether a candidate event produces a
``news_event_entry`` paper order. This module verifies the cheapest
filesystem-stat gate — the ``.armed`` marker — implemented in
:func:`biotech_sniper.llm.stage2_gates.armed_gate`.

Behavioural contract (mirroring ``validation-contract.md`` VAL-M3-032
through VAL-M3-036, plus VAL-M3-081):

* Absent file (or unresolvable path) → ``passed=False, reason='armed_file_missing'``.
* Present regular readable file → ``passed=True, reason=None``.
* Symlink to a real readable file → allowed (passes).
* Dangling symlink, directory, or chmod-000 file → treated as **absent**
  (``passed=False, reason='armed_file_missing'``). The gate is forgiving
  in semantics (any non-usable target = absent) but strict in effect (no
  entry submission unless the file is a regular readable file).
* Path resolution goes through :data:`biotech_sniper.paths.READING_B_ARMED_FILE`;
  no hardcoded ``"/root/alpha_sniper/.armed"`` literal exists outside
  ``paths.py`` itself.
* Atomic single-stat: the gate function performs the existence /
  type / readability checks within a single synchronous call with no
  sleep / await / lock-acquisition between them.
* Production-code source-grep verifies no ``Path.touch``,
  ``Path.write_text``, ``Path.write_bytes``, or ``open(..., 'w')`` call
  ever targets the ``.armed`` path inside ``biotech_sniper/``.

Per the dual-path test convention (``library`` / ``AGENTS.md``),
``tests/llm/test_stage2_gates.py`` re-exports these test bodies via
``from tests.test_armed_gate import *`` so contract node IDs of either
form collect.
"""

from __future__ import annotations

import importlib
import os
import re
import stat as stat_mod
import subprocess
import sys
import time
from pathlib import Path

import pytest

from biotech_sniper import paths as _paths
from biotech_sniper.llm import stage2_gates
from biotech_sniper.llm.stage2_gates import (
    GATE_REASON_ARMED_FILE_MISSING,
    ArmedGateResult,
    armed_gate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tmp_armed(tmp_path: Path, *, contents: bytes = b"") -> Path:
    """Create a regular readable ``.armed`` file under ``tmp_path``."""
    armed = tmp_path / ".armed"
    armed.write_bytes(contents)
    # Default mode (0o644) is fine; some shells inherit 0o600, both
    # are readable by the owner.
    return armed


# ---------------------------------------------------------------------------
# VAL-M3-032 — absent file rejects
# ---------------------------------------------------------------------------


def test_armed_absent_blocks(tmp_path):
    """``.armed`` not present → ``passed=False, reason='armed_file_missing'``."""
    missing = tmp_path / ".armed"
    assert not missing.exists()
    res = armed_gate(armed_path=missing)
    assert isinstance(res, ArmedGateResult)
    assert res.passed is False
    assert res.reason == "armed_file_missing"
    assert res.reason == GATE_REASON_ARMED_FILE_MISSING


def test_armed_missing_when_parent_dir_does_not_exist(tmp_path):
    """A path under a non-existent parent dir is treated as absent (no crash)."""
    nested = tmp_path / "no_such_subdir" / ".armed"
    res = armed_gate(armed_path=nested)
    assert res.passed is False
    assert res.reason == "armed_file_missing"


def test_armed_default_path_points_to_paths_module(monkeypatch, tmp_path):
    """When ``armed_path`` kwarg is ``None``, the gate reads the path from
    ``biotech_sniper.paths.READING_B_ARMED_FILE`` at call time — so a
    monkeypatched override takes effect without re-importing the module."""
    target = tmp_path / ".armed"
    monkeypatch.setattr(_paths, "READING_B_ARMED_FILE", target)
    # Absent.
    res = armed_gate()
    assert res.passed is False
    assert res.reason == "armed_file_missing"
    # Now create the file and re-call: should pass.
    target.write_text("")
    res2 = armed_gate()
    assert res2.passed is True
    assert res2.reason is None


# ---------------------------------------------------------------------------
# VAL-M3-033 — present regular readable file allows
# ---------------------------------------------------------------------------


def test_armed_present_allows(tmp_path):
    """Regular readable file → ``passed=True, reason=None``."""
    armed = _make_tmp_armed(tmp_path)
    res = armed_gate(armed_path=armed)
    assert res.passed is True
    assert res.reason is None


def test_armed_present_contents_not_inspected(tmp_path):
    """Contents are irrelevant — mere existence is the signal (per VAL-M3-033)."""
    armed = tmp_path / ".armed"
    # Various contents — all should pass.
    for payload in (b"", b"1", b"yes\n", b"\x00\x01\x02", b"x" * 4096):
        armed.write_bytes(payload)
        res = armed_gate(armed_path=armed)
        assert res.passed is True, f"contents={payload!r}"
        assert res.reason is None


# ---------------------------------------------------------------------------
# Symlinks: real-file target allowed; dangling treated as absent
# ---------------------------------------------------------------------------


def test_armed_symlink_to_real_readable_file_allowed(tmp_path):
    """A symlink whose target is a readable regular file → passes."""
    target = tmp_path / "real_marker"
    target.write_text("")
    link = tmp_path / ".armed"
    link.symlink_to(target)
    # Sanity: link is a symlink to a regular file.
    assert link.is_symlink()
    assert link.resolve().is_file()
    res = armed_gate(armed_path=link)
    assert res.passed is True
    assert res.reason is None


def test_armed_dangling_symlink_treated_as_absent(tmp_path):
    """Symlink whose target does not exist → treated as absent."""
    nonexistent = tmp_path / "no_such_file"
    link = tmp_path / ".armed"
    link.symlink_to(nonexistent)
    assert link.is_symlink()
    assert not link.exists()  # dangling
    res = armed_gate(armed_path=link)
    assert res.passed is False
    assert res.reason == "armed_file_missing"


# ---------------------------------------------------------------------------
# Wrong file types: directory and special-mode files → treated as absent
# ---------------------------------------------------------------------------


def test_armed_directory_treated_as_absent(tmp_path):
    """A directory at the ``.armed`` path → treated as absent."""
    armed = tmp_path / ".armed"
    armed.mkdir()
    assert armed.is_dir()
    res = armed_gate(armed_path=armed)
    assert res.passed is False
    assert res.reason == "armed_file_missing"


def test_armed_chmod_000_treated_as_absent(tmp_path):
    """A regular file with mode 000 (no read bit) → treated as absent.

    Skipped when running as root because root reads anything regardless
    of the mode bits.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses mode bits; cannot validate chmod 000")
    armed = tmp_path / ".armed"
    armed.write_bytes(b"")
    armed.chmod(0o000)
    try:
        res = armed_gate(armed_path=armed)
        assert res.passed is False
        assert res.reason == "armed_file_missing"
    finally:
        # Restore writability so pytest can clean up tmp_path.
        armed.chmod(0o644)


def test_armed_symlink_to_directory_treated_as_absent(tmp_path):
    """Symlink to a directory → not a regular file → treated as absent."""
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    link = tmp_path / ".armed"
    link.symlink_to(real_dir)
    res = armed_gate(armed_path=link)
    assert res.passed is False
    assert res.reason == "armed_file_missing"


# ---------------------------------------------------------------------------
# VAL-M3-034 — path resolved via paths.py, not hardcoded
# ---------------------------------------------------------------------------


def test_no_hardcoded_armed_path_in_production_code():
    """No source file under ``biotech_sniper/`` (other than ``paths.py``)
    contains the literal ``"/root/alpha_sniper/.armed"`` string."""
    repo_root = Path(__file__).resolve().parents[1]
    pkg = repo_root / "biotech_sniper"
    offenders: list[Path] = []
    for path in pkg.rglob("*.py"):
        if path.name == "paths.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "/root/alpha_sniper/.armed" in text:
            offenders.append(path)
    assert offenders == [], (
        "Hardcoded /root/alpha_sniper/.armed literal found in non-paths.py "
        f"production source: {offenders}"
    )


def test_paths_module_exports_reading_b_armed_file():
    """``biotech_sniper.paths`` exports the canonical armed-file path."""
    assert hasattr(_paths, "READING_B_ARMED_FILE")
    assert isinstance(_paths.READING_B_ARMED_FILE, Path)
    # Lives one level above BASE_DIR (so a ``git clean`` on the repo
    # cannot delete it).
    assert _paths.READING_B_ARMED_FILE.parent == _paths.BASE_DIR.parent
    assert _paths.READING_B_ARMED_FILE.name == ".armed"
    # Listed in __all__.
    assert "READING_B_ARMED_FILE" in _paths.__all__


def test_paths_module_armed_path_uses_basedir_parent_invariant(monkeypatch, tmp_path):
    """``BIOTECH_SNIPER_HOME`` rebases ``READING_B_ARMED_FILE`` accordingly.

    The constant is computed at import time so we reload the module
    inside the env-var scope — same pattern as ``test_paths.py``.
    """
    monkeypatch.setenv("BIOTECH_SNIPER_HOME", str(tmp_path))
    if "biotech_sniper.paths" in sys.modules:
        reloaded = importlib.reload(sys.modules["biotech_sniper.paths"])
    else:  # pragma: no cover - the module is always imported by this point
        reloaded = importlib.import_module("biotech_sniper.paths")
    try:
        assert reloaded.READING_B_ARMED_FILE == tmp_path.parent / ".armed"
    finally:
        # Restore canonical module state for subsequent tests.
        monkeypatch.delenv("BIOTECH_SNIPER_HOME", raising=False)
        importlib.reload(sys.modules["biotech_sniper.paths"])


# ---------------------------------------------------------------------------
# VAL-M3-035 — production code MUST NEVER write or touch .armed
# ---------------------------------------------------------------------------


def test_no_production_writes_to_armed_file():
    """Repo-wide grep verifies no production write paths target ``.armed``.

    Looks across ``biotech_sniper/`` for any ``open(..., 'w')``,
    ``.touch``, ``.write_text``, ``.write_bytes``, or
    ``subprocess.run(['touch', ...])`` call whose target is the
    ``.armed`` path or the ``READING_B_ARMED_FILE`` constant. Test
    sources under ``tests/`` are excluded — tests legitimately toggle
    ``.armed`` files under ``tmp_path``.

    This test is the canonical guard for the f-m3-06 invariant:
    "Production code MUST NEVER write or `touch` `.armed`".
    """
    repo_root = Path(__file__).resolve().parents[1]
    pkg = repo_root / "biotech_sniper"
    write_re = re.compile(
        r"\.(touch|write_text|write_bytes)\s*\(",
    )
    open_w_re = re.compile(r"open\s*\([^)]*['\"]w[+b]?['\"]")
    subprocess_touch_re = re.compile(
        r"subprocess[^;\n]*['\"]touch['\"]"
    )
    armed_token_re = re.compile(r"\.armed|READING_B_ARMED_FILE")

    offenders: list[tuple[Path, int, str]] = []
    for path in pkg.rglob("*.py"):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
        ):
            if not armed_token_re.search(line):
                continue
            if (
                write_re.search(line)
                or open_w_re.search(line)
                or subprocess_touch_re.search(line)
            ):
                offenders.append((path, lineno, line.rstrip()))
    assert offenders == [], (
        "Production code MUST NEVER write or touch the .armed file. "
        f"Offending lines under biotech_sniper/: {offenders}"
    )


def test_grep_evidence_command_returns_clean():
    """The exact grep evidence command from the feature manifest must
    produce no matches under ``biotech_sniper/`` — modulo
    legitimate read-side / docstring references which the command
    explicitly filters out via ``grep -E '(touch|open\\(.*w|write_text)'``.

    This test runs the command via ``subprocess`` and asserts the final
    pipeline stdout is empty.
    """
    repo_root = Path(__file__).resolve().parents[1]
    cmd = (
        "grep -RIE '\\.armed' biotech_sniper/ --include='*.py' "
        "| grep -v 'tests/' "
        "| grep -E '(touch|open\\(.*w|write_text)'"
    )
    completed = subprocess.run(
        ["bash", "-c", cmd],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
    )
    # ``grep`` exits 1 when there are no matches in the final pipe stage,
    # which is the SUCCESS condition for this evidence command.
    assert completed.stdout.strip() == "", (
        f"grep evidence command produced non-empty output:\n"
        f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
    )


def test_no_raw_armed_string_writes_in_stage2_gates():
    """``stage2_gates.py`` must not contain any ``open(..., 'w')`` call
    or ``Path.touch`` / ``Path.write_text`` / ``Path.write_bytes`` call
    targeting ``.armed`` — defensive guard against future regressions.
    """
    src = Path(stage2_gates.__file__).read_text(encoding="utf-8")
    # Must reference paths.READING_B_ARMED_FILE (or `.armed`) only on the
    # READ side. We assert directly that no write-pattern substring
    # appears in the same line as the armed token.
    for line in src.splitlines():
        if ".armed" in line or "READING_B_ARMED_FILE" in line:
            for forbidden in (
                ".touch(",
                ".write_text(",
                ".write_bytes(",
                "open(",
                "os.mknod",
            ):
                assert forbidden not in line, (
                    f"stage2_gates.py contains a forbidden write pattern "
                    f"{forbidden!r} on a line referencing .armed: {line!r}"
                )


# ---------------------------------------------------------------------------
# VAL-M3-036 — atomic single-stat (no TOCTOU window)
# ---------------------------------------------------------------------------


def test_armed_gate_no_sleep_or_await_in_source():
    """Source-level guard: the ``armed_gate`` function body contains no
    ``time.sleep``, ``await``, or ``Lock.acquire(timeout=...)`` calls.
    A future refactor that re-introduced any of those would surface a
    TOCTOU window between the existence check and the gate decision.
    """
    src = Path(stage2_gates.__file__).read_text(encoding="utf-8")
    # Locate the start of ``def armed_gate(...):`` and slice from there
    # to the next top-level ``def `` / ``class `` / ``@dataclass`` /
    # ``# ----`` boundary, or end-of-file when the gate is the last
    # definition in the module (the current layout).
    start_match = re.search(r"^def armed_gate\(", src, re.MULTILINE)
    assert start_match is not None, (
        "Could not locate `def armed_gate(` in stage2_gates.py — "
        "test selector may need an update."
    )
    rest = src[start_match.start():]
    # Find the next top-level boundary after the gate definition (the
    # ``def armed_gate`` line itself is at offset 0 of ``rest``).
    boundary = re.search(
        r"\n(def |class |@dataclass\n|# ---)",
        rest[1:],  # skip the ``def armed_gate`` line itself
    )
    body = rest if boundary is None else rest[: 1 + boundary.start()]
    # Strip the function docstring before scanning — we only want to
    # guard the executable code path, not docstring prose that may
    # legitimately reference ``Lock.acquire`` to explain the
    # invariant.
    docstring_match = re.search(
        r'"""(?:.|\n)*?"""',
        body,
    )
    if docstring_match is not None:
        body_code = body[: docstring_match.start()] + body[docstring_match.end():]
    else:
        body_code = body
    for forbidden in (
        "time.sleep(",
        "asyncio.sleep(",
        "Lock.acquire(",
        # Real ``await`` statements in Python source live at the start
        # of a logical line; checking for ``\n    await `` keeps us
        # robust to comments / strings that happen to contain
        # the bare word "await".
        "\n    await ",
    ):
        assert forbidden not in body_code, (
            f"armed_gate body contains forbidden TOCTOU-introducing "
            f"construct: {forbidden!r}"
        )


def test_armed_gate_returns_synchronously_no_await():
    """Behavioural sanity: the gate is a regular synchronous function
    (NOT a coroutine) — calling it returns a result, not an awaitable."""
    armed = Path("/no/such/path/.armed")
    res = armed_gate(armed_path=armed)
    # ``res`` is a dataclass, not a coroutine.
    import inspect

    assert not inspect.iscoroutine(res)
    assert isinstance(res, ArmedGateResult)


def test_armed_gate_runtime_is_fast(tmp_path):
    """Soft latency check: a single armed-gate call completes in < 50ms.

    A regression to a multi-stat / poll-with-sleep implementation would
    blow this threshold immediately. The check is deliberately generous
    (50ms) so it doesn't false-positive on a busy CI host.
    """
    armed = _make_tmp_armed(tmp_path)
    started = time.monotonic()
    for _ in range(50):
        armed_gate(armed_path=armed)
    elapsed = time.monotonic() - started
    assert elapsed < 50 * 0.05, (
        f"50 armed_gate calls took {elapsed:.3f}s — expected < 2.5s; "
        f"a sleep / poll regression is likely."
    )


# ---------------------------------------------------------------------------
# Result dataclass shape
# ---------------------------------------------------------------------------


def test_armed_gate_result_shape_passed(tmp_path):
    armed = _make_tmp_armed(tmp_path)
    res = armed_gate(armed_path=armed)
    assert isinstance(res.passed, bool)
    assert res.reason is None
    assert isinstance(res.armed_path, str)
    # Path string is reported faithfully (so audit logs can
    # reproduce the resolved path).
    assert res.armed_path == str(armed)


def test_armed_gate_result_shape_rejected(tmp_path):
    missing = tmp_path / "no_such_file" / ".armed"
    res = armed_gate(armed_path=missing)
    assert res.passed is False
    assert res.reason == "armed_file_missing"
    assert res.armed_path == str(missing)


def test_armed_gate_result_is_frozen_or_dataclass():
    """``ArmedGateResult`` is a dataclass, mirroring the existing
    Probability / Unanimity gate result types in stage2_gates.py."""
    import dataclasses as _dc

    assert _dc.is_dataclass(ArmedGateResult)


def test_gate_reason_constant_is_canonical():
    """The exported reason constant matches the contract evidence string
    (``armed_file_missing``)."""
    assert GATE_REASON_ARMED_FILE_MISSING == "armed_file_missing"
