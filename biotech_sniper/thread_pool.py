"""Bounded :class:`ThreadPoolExecutor` factory.

This module is the project's single sanctioned entry point for spawning
:class:`concurrent.futures.ThreadPoolExecutor` instances. It enforces the
workload cap documented in ``AGENTS.md`` § "Workload caps on VPS" — namely
that ``max_workers`` must not exceed :data:`biotech_sniper.config.MAX_WORKERS`
(currently ``4``). Direct instantiation of ``ThreadPoolExecutor(max_workers=N)``
with ``N > MAX_WORKERS`` anywhere under ``biotech_sniper/`` is a mission-policy
violation; the validation contract (VAL-M4-044) greps for it.

Two failure modes are surfaced to callers:

* ``max_workers <= 0`` raises :class:`ValueError` (matches CPython's own
  policy and prevents silent no-ops).
* ``max_workers > MAX_WORKERS`` raises :class:`ThreadPoolCapExceeded`. This
  is the runtime guard required by feature ``f-m4-05-thread-pool-cap``: a
  caller passing too many workers gets a loud, immediate failure rather
  than a quietly clamped pool that would mask configuration drift.

Use this helper as a context manager just like the stdlib executor::

    from biotech_sniper.thread_pool import bounded_thread_pool

    with bounded_thread_pool(max_workers=4, thread_name_prefix="pipeline") as pool:
        futures = [pool.submit(work, item) for item in items]

The returned object is a real :class:`ThreadPoolExecutor`; we do not wrap
it so the existing ``submit`` / ``map`` / ``shutdown`` semantics are
preserved.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from biotech_sniper.config import MAX_WORKERS

__all__ = [
    "MAX_WORKERS",
    "ThreadPoolCapExceeded",
    "bounded_thread_pool",
]


class ThreadPoolCapExceeded(ValueError):
    """Raised when a caller requests more workers than :data:`MAX_WORKERS`.

    Subclassing :class:`ValueError` keeps the exception family
    consistent with stdlib parameter-validation errors (and lets
    catch-all ``except ValueError`` blocks observe the breach) while
    still being narrowly catchable for tests and audit code that want
    to assert the guard fired.
    """


def bounded_thread_pool(
    max_workers: int,
    thread_name_prefix: str = "",
    initializer: Optional[object] = None,
    initargs: tuple = (),
) -> ThreadPoolExecutor:
    """Return a :class:`ThreadPoolExecutor` capped at :data:`MAX_WORKERS`.

    Parameters
    ----------
    max_workers:
        Number of worker threads requested by the caller. MUST be a
        positive integer ≤ :data:`MAX_WORKERS`. Values ``≤ 0`` raise
        :class:`ValueError`; values ``> MAX_WORKERS`` raise
        :class:`ThreadPoolCapExceeded` (i.e. the runtime guard).
    thread_name_prefix:
        Forwarded to :class:`ThreadPoolExecutor`.
    initializer, initargs:
        Forwarded to :class:`ThreadPoolExecutor`.

    Returns
    -------
    concurrent.futures.ThreadPoolExecutor
        A real executor (not a wrapper), so ``with`` blocks, ``submit``,
        ``map``, and ``shutdown`` behave exactly as the stdlib version.

    Raises
    ------
    ValueError
        When ``max_workers`` is not a positive integer.
    ThreadPoolCapExceeded
        When ``max_workers`` exceeds :data:`MAX_WORKERS`. The message
        includes both the requested value and the cap so operators
        can quickly find the offending call site.
    """
    if not isinstance(max_workers, int) or isinstance(max_workers, bool):
        raise ValueError(
            f"max_workers must be an int, got {type(max_workers).__name__}"
        )
    if max_workers <= 0:
        raise ValueError(f"max_workers must be > 0, got {max_workers}")
    if max_workers > MAX_WORKERS:
        raise ThreadPoolCapExceeded(
            f"max_workers={max_workers} exceeds MAX_WORKERS cap "
            f"({MAX_WORKERS}); see biotech_sniper.config.MAX_WORKERS "
            "and AGENTS.md § 'Workload caps on VPS'."
        )

    return ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix=thread_name_prefix,
        initializer=initializer,
        initargs=initargs,
    )
