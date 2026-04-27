"""Unit tests for the M4 thread-pool workload cap.

Feature ``f-m4-05-thread-pool-cap`` enforces that no
:class:`concurrent.futures.ThreadPoolExecutor` in the project requests
``max_workers > 4``. The mission-policy contract has three pieces:

1. :data:`biotech_sniper.config.MAX_WORKERS` is defined and ≤ 4.
2. :func:`biotech_sniper.thread_pool.bounded_thread_pool` clamps the
   request to that cap and raises
   :class:`biotech_sniper.thread_pool.ThreadPoolCapExceeded` for any
   request above the cap.
3. A repository-wide grep for ``ThreadPoolExecutor`` finds no
   instantiation with a hardcoded ``max_workers > 4`` (the runtime
   cap is the single source of truth and direct instantiation is
   discouraged in favour of ``bounded_thread_pool``).

These tests cover the happy path (≤ cap → real executor returned),
the error path (> cap → ``ThreadPoolCapExceeded``), and the codebase
audit (grep returns no offenders).
"""

from __future__ import annotations

import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from biotech_sniper import config, thread_pool
from biotech_sniper.thread_pool import (
    MAX_WORKERS,
    ThreadPoolCapExceeded,
    bounded_thread_pool,
)


# ---------------------------------------------------------------------------
# Constant
# ---------------------------------------------------------------------------


def test_config_max_workers_is_defined_and_capped() -> None:
    """`config.MAX_WORKERS` must exist, be an int, and be ≤ 4."""
    assert hasattr(config, "MAX_WORKERS"), "config.MAX_WORKERS not defined"
    assert isinstance(config.MAX_WORKERS, int)
    assert not isinstance(config.MAX_WORKERS, bool)
    assert config.MAX_WORKERS >= 1
    assert config.MAX_WORKERS <= 4, (
        f"config.MAX_WORKERS={config.MAX_WORKERS} violates the M4 workload cap "
        "(see AGENTS.md § 'Workload caps on VPS')."
    )


def test_thread_pool_module_re_exports_constant() -> None:
    """The thread_pool helper must re-export the same cap value."""
    assert thread_pool.MAX_WORKERS == config.MAX_WORKERS


def test_max_workers_is_in_config_all() -> None:
    """`MAX_WORKERS` must be in config.__all__ for explicit re-export."""
    assert "MAX_WORKERS" in config.__all__


# ---------------------------------------------------------------------------
# Runtime guard happy path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("workers", [1, 2, 3, 4])
def test_bounded_thread_pool_accepts_values_at_or_below_cap(workers: int) -> None:
    """Values 1..MAX_WORKERS yield a real ThreadPoolExecutor."""
    if workers > MAX_WORKERS:
        pytest.skip(f"parameterized workers={workers} above MAX_WORKERS={MAX_WORKERS}")
    pool = bounded_thread_pool(max_workers=workers)
    try:
        assert isinstance(pool, ThreadPoolExecutor)
        # The pool must actually work — submit a trivial task and read it.
        fut = pool.submit(lambda: 42)
        assert fut.result(timeout=5) == 42
    finally:
        pool.shutdown(wait=True)


def test_bounded_thread_pool_supports_context_manager() -> None:
    """The returned executor MUST be usable as a context manager."""
    with bounded_thread_pool(max_workers=2, thread_name_prefix="test-tp") as pool:
        results = list(pool.map(lambda x: x * 2, [1, 2, 3]))
    assert results == [2, 4, 6]


def test_bounded_thread_pool_passes_thread_name_prefix() -> None:
    """`thread_name_prefix` must reach the underlying executor."""
    with bounded_thread_pool(max_workers=1, thread_name_prefix="m4-cap-prefix") as pool:
        # ThreadPoolExecutor exposes the prefix on the private attribute
        # `_thread_name_prefix`. We use it because there's no public getter.
        assert pool._thread_name_prefix == "m4-cap-prefix"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Runtime guard error path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("workers", [5, 6, 8, 10, 16, 32, 100])
def test_bounded_thread_pool_rejects_values_above_cap(workers: int) -> None:
    """Any request above the cap must raise ThreadPoolCapExceeded."""
    with pytest.raises(ThreadPoolCapExceeded) as excinfo:
        bounded_thread_pool(max_workers=workers)
    msg = str(excinfo.value)
    assert str(workers) in msg
    assert str(MAX_WORKERS) in msg


def test_thread_pool_cap_exceeded_subclasses_value_error() -> None:
    """Catching ValueError must observe the breach for callers without
    direct access to the project-specific exception class."""
    assert issubclass(ThreadPoolCapExceeded, ValueError)


@pytest.mark.parametrize("workers", [0, -1, -100])
def test_bounded_thread_pool_rejects_non_positive_values(workers: int) -> None:
    """Zero or negative worker counts must raise plain ValueError."""
    with pytest.raises(ValueError) as excinfo:
        bounded_thread_pool(max_workers=workers)
    # Specifically NOT the cap-exceeded subclass — this is the "must be > 0"
    # guard, distinct from the cap guard.
    assert not isinstance(excinfo.value, ThreadPoolCapExceeded)


@pytest.mark.parametrize("workers", ["4", 4.0, None, True, False])
def test_bounded_thread_pool_rejects_non_int_types(workers: object) -> None:
    """Non-int (incl. bool) inputs must raise ValueError, not silently coerce."""
    with pytest.raises(ValueError):
        bounded_thread_pool(max_workers=workers)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Repo-wide audit: no hardcoded max_workers > 4 anywhere in biotech_sniper/
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_ROOT = REPO_ROOT / "biotech_sniper"


def test_no_hardcoded_thread_pool_above_cap_in_package() -> None:
    """No `ThreadPoolExecutor(...max_workers=N)` literal where N > 4.

    Walks every .py file under biotech_sniper/ and matches the pattern
    `ThreadPoolExecutor(...max_workers=<digits>)`. We only flag literal
    integers above 4; references to `MAX_WORKERS`, function defaults
    that read `config.MAX_WORKERS`, or `bounded_thread_pool(...)` calls
    are intentionally not matched.
    """
    pattern = re.compile(
        r"ThreadPoolExecutor\([^)]*max_workers\s*=\s*(\d+)",
        re.MULTILINE,
    )
    offenders: list[tuple[Path, int, int]] = []
    for py in PKG_ROOT.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for m in pattern.finditer(text):
            n = int(m.group(1))
            if n > MAX_WORKERS:
                # Locate line number for the diagnostic
                line_no = text[: m.start()].count("\n") + 1
                offenders.append((py.relative_to(REPO_ROOT), line_no, n))
    assert offenders == [], (
        f"Hardcoded ThreadPoolExecutor(max_workers > {MAX_WORKERS}) found: {offenders}"
    )


def test_no_bare_thread_pool_executor_in_package() -> None:
    """Mirrors VAL-M4-045: no `ThreadPoolExecutor()` (without args) in the package."""
    pattern = re.compile(r"ThreadPoolExecutor\(\s*\)")
    offenders: list[tuple[Path, int]] = []
    for py in PKG_ROOT.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for m in pattern.finditer(text):
            line_no = text[: m.start()].count("\n") + 1
            offenders.append((py.relative_to(REPO_ROOT), line_no))
    assert offenders == [], f"Bare ThreadPoolExecutor() found: {offenders}"


# ---------------------------------------------------------------------------
# VAL-M4-044 grep contract — enforce the exact source-level cap value.
# ---------------------------------------------------------------------------


def test_val_m4_044_source_level_cap_grep() -> None:
    """Reproduce the VAL-M4-044 awk pipeline as a self-test.

    `grep -RnE 'ThreadPoolExecutor\\([^)]*max_workers' biotech_sniper/ |
     awk -F'max_workers' '{print $2}' | grep -oE '[0-9]+' | sort -nu | tail -1`
    must yield ≤ MAX_WORKERS (or be empty when no instantiations remain).
    """
    proc = subprocess.run(
        [
            "grep",
            "-RnE",
            r"ThreadPoolExecutor\([^)]*max_workers",
            str(PKG_ROOT),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # grep exit code 1 → no matches; that's fine and equivalent to "≤ cap".
    if proc.returncode not in (0, 1):
        pytest.fail(f"grep failed unexpectedly: {proc.stderr}")
    digits = re.findall(r"max_workers\s*=\s*(\d+)", proc.stdout)
    if not digits:
        return  # No literal instantiations remain — passes by default.
    max_seen = max(int(d) for d in digits)
    assert max_seen <= MAX_WORKERS, (
        f"VAL-M4-044 violation: ThreadPoolExecutor(max_workers={max_seen}) "
        f"exceeds MAX_WORKERS={MAX_WORKERS} in biotech_sniper/."
    )
