"""Reading-B Stage-2 Perplexity circuit breaker (feature ``f-m3-12``).

In-process circuit breaker shielding the Stage-2 ensemble fan-out
from sustained Perplexity ``5xx`` (or transport-timeout) outages.

Design contract (validation contract VAL-M3-063 .. VAL-M3-067,
VAL-M3-086, VAL-M3-095):

* Rolling 60-second window of Perplexity outcomes (``5xx`` /
  ``timeout`` count as failures; ``2xx`` and HTTP-level ``429``
  count as non-failures).
* When the failure rate over the window strictly exceeds 20% AND
  the window contains at least :data:`MIN_REQUESTS_FOR_TRIP` (= 5)
  events, the breaker enters :class:`BreakerState.OPEN` for
  :data:`OPEN_DURATION_SECONDS` (5 minutes).
* After the OPEN window elapses, the next state read transitions
  to :class:`BreakerState.HALF_OPEN`. The next outcome recorded
  (the "probe") decides:

  - success → :class:`BreakerState.CLOSED` (rolling window cleared)
  - failure → :class:`BreakerState.OPEN` for another 5 minutes
* While ``OPEN``, the ensemble fan-out skips the Perplexity provider
  entirely (3-leg ``score_candidate_event``); successful 3/3 results
  are still persisted to ``ensemble_scores_event`` as score-only
  audit telemetry.
* HTTP ``429`` does NOT count toward the breaker's failure rate
  (treated distinctly from ``5xx``); the rate-limit retry path
  honors the upstream ``Retry-After`` header as a lower bound on
  retry sleep (see :func:`parse_retry_after`).
* Breaker state is held in this module's singleton — there is NO
  ``breaker_state`` SQL table. A daemon restart resets the breaker,
  which is the intended behaviour because the restart already
  disrupted the failure window.

Public surface
--------------
* :class:`BreakerState` — three-state enum (CLOSED, OPEN, HALF_OPEN).
* :class:`PerplexityBreaker` — the breaker class. Tests construct
  one directly with an injected fake clock; production callers use
  :func:`get_breaker` to access the module singleton.
* :func:`get_breaker` — return (and lazily construct) the singleton.
* :func:`reset_breaker_for_test` — clear the singleton between tests.
* :func:`record_success`, :func:`record_5xx_failure`,
  :func:`record_timeout`, :func:`record_429`, :func:`is_open`,
  :func:`allow_request` — module-level helpers that delegate to the
  singleton (used by ``perplexity_client.py`` and
  ``ensemble.score_candidate_event``).
* :func:`parse_retry_after` — parse an HTTP ``Retry-After`` header
  value (seconds-int OR HTTP-date) into a ``float`` seconds value
  (or ``None`` when unparseable).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from enum import Enum
from typing import Any, Callable, Optional

__all__ = [
    "BreakerState",
    "PerplexityBreaker",
    "FAILURE_RATE_THRESHOLD",
    "MIN_REQUESTS_FOR_TRIP",
    "WINDOW_SECONDS",
    "OPEN_DURATION_SECONDS",
    "get_breaker",
    "reset_breaker_for_test",
    "record_success",
    "record_5xx_failure",
    "record_timeout",
    "record_429",
    "is_open",
    "allow_request",
    "parse_retry_after",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — locked by VAL-M3-063 / VAL-M3-065
# ---------------------------------------------------------------------------

#: Rolling-window length in seconds.
WINDOW_SECONDS: float = 60.0

#: How long the breaker stays OPEN before transitioning to HALF_OPEN.
OPEN_DURATION_SECONDS: float = 5 * 60.0

#: Failure rate (failures / total) above which the breaker trips.
#: Strictly greater-than: 20% exactly is NOT enough.
FAILURE_RATE_THRESHOLD: float = 0.20

#: Minimum number of events in the rolling window before the breaker
#: is allowed to trip — guards against false positives on sparse
#: traffic.
MIN_REQUESTS_FOR_TRIP: int = 5


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------


class BreakerState(str, Enum):
    """Three-state breaker FSM."""

    CLOSED = "CLOSED"      # normal operation
    OPEN = "OPEN"          # short-circuit: skip Perplexity calls entirely
    HALF_OPEN = "HALF_OPEN"  # one probe call permitted to test recovery


# ---------------------------------------------------------------------------
# Breaker class
# ---------------------------------------------------------------------------


class PerplexityBreaker:
    """In-process circuit breaker for Perplexity 5xx/timeout outages.

    The implementation is thread-safe via an :class:`RLock`; the
    Stage-2 ensemble fan-out invokes the breaker concurrently from
    up to four worker threads (one per provider).

    Parameters
    ----------
    clock:
        Callable returning a monotonic timestamp in seconds.
        Defaults to :func:`time.monotonic`. Tests inject a fake
        clock so the 5-minute OPEN window can be exercised without
        sleeping.
    window_seconds:
        Rolling window length. Defaults to :data:`WINDOW_SECONDS`.
    open_duration_seconds:
        How long the breaker stays OPEN. Defaults to
        :data:`OPEN_DURATION_SECONDS`.
    failure_rate_threshold:
        Failure rate strictly greater-than which trips the breaker.
        Defaults to :data:`FAILURE_RATE_THRESHOLD` (0.20).
    min_requests_for_trip:
        Minimum events in the window before the threshold is
        evaluated. Defaults to :data:`MIN_REQUESTS_FOR_TRIP` (5).
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        window_seconds: float = WINDOW_SECONDS,
        open_duration_seconds: float = OPEN_DURATION_SECONDS,
        failure_rate_threshold: float = FAILURE_RATE_THRESHOLD,
        min_requests_for_trip: int = MIN_REQUESTS_FOR_TRIP,
    ) -> None:
        self._clock = clock
        self._window_seconds = float(window_seconds)
        self._open_duration_seconds = float(open_duration_seconds)
        self._failure_rate_threshold = float(failure_rate_threshold)
        self._min_requests_for_trip = int(min_requests_for_trip)
        self._lock = threading.RLock()
        # Each entry: (timestamp, is_failure: bool).
        self._events: deque[tuple[float, bool]] = deque()
        self._state: BreakerState = BreakerState.CLOSED
        self._opened_at: Optional[float] = None

    # ------------------------------------------------------------------
    # Internal helpers (must be called under self._lock)
    # ------------------------------------------------------------------

    def _prune(self, now: float) -> None:
        """Drop events older than the rolling-window cutoff."""
        cutoff = now - self._window_seconds
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def _maybe_transition_open_to_half_open(self, now: float) -> None:
        """If OPEN duration has elapsed, transition to HALF_OPEN."""
        if (
            self._state is BreakerState.OPEN
            and self._opened_at is not None
            and (now - self._opened_at) >= self._open_duration_seconds
        ):
            logger.info(
                "perplexity_breaker: state OPEN -> HALF_OPEN after %.1fs",
                now - self._opened_at,
            )
            self._state = BreakerState.HALF_OPEN

    def _maybe_open_from_closed(self, now: float) -> None:
        """If failure-rate threshold tripped, transition CLOSED -> OPEN."""
        if self._state is not BreakerState.CLOSED:
            return
        total = len(self._events)
        if total < self._min_requests_for_trip:
            return
        failures = sum(1 for _, is_fail in self._events if is_fail)
        rate = failures / total
        if rate > self._failure_rate_threshold:
            logger.warning(
                "perplexity_breaker: state CLOSED -> OPEN "
                "(5xx_rate=%.2f failures=%d total=%d)",
                rate,
                failures,
                total,
            )
            self._state = BreakerState.OPEN
            self._opened_at = now
            self._events.clear()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> BreakerState:
        """Return the current breaker state.

        Reads have a side effect: when the OPEN window has elapsed,
        the state transitions to HALF_OPEN on the read. This is
        intentional — it keeps the implementation dead-simple
        (no background timer thread).
        """
        with self._lock:
            now = self._clock()
            self._maybe_transition_open_to_half_open(now)
            return self._state

    def is_open(self) -> bool:
        """Return ``True`` iff the breaker state is currently OPEN."""
        return self.state is BreakerState.OPEN

    def allow_request(self) -> bool:
        """Return ``True`` iff a Perplexity call is permitted now.

        ``CLOSED`` and ``HALF_OPEN`` permit calls (HALF_OPEN sends a
        single probe). ``OPEN`` blocks calls.
        """
        return self.state is not BreakerState.OPEN

    def record_success(self) -> None:
        """Record a successful Perplexity outcome.

        In ``HALF_OPEN`` this closes the breaker. In ``CLOSED`` the
        success contributes to the rolling window. (In ``OPEN`` it
        is a no-op — production code should never reach here while
        the breaker is OPEN.)
        """
        with self._lock:
            now = self._clock()
            self._maybe_transition_open_to_half_open(now)
            if self._state is BreakerState.HALF_OPEN:
                logger.info(
                    "perplexity_breaker: state HALF_OPEN -> CLOSED (probe success)"
                )
                self._state = BreakerState.CLOSED
                self._opened_at = None
                self._events.clear()
                return
            if self._state is BreakerState.OPEN:
                # Defensive: should not happen because the caller is
                # gated on ``allow_request()``. Treat as a no-op.
                return
            self._events.append((now, False))
            self._prune(now)

    def record_5xx_failure(self) -> None:
        """Record an HTTP 5xx failure. Counts toward the breaker."""
        self._record_failure()

    def record_timeout(self) -> None:
        """Record a transport timeout. Counts toward the breaker
        (per VAL-M3-086 — 5xx + timeouts share the breaker counter)."""
        self._record_failure()

    def record_429(self) -> None:
        """Record an HTTP 429 — does NOT count toward the breaker.

        429 has its own retry-with-backoff path inside the client; the
        ``Retry-After`` header (parsed by :func:`parse_retry_after`)
        is honored as a lower bound on inter-attempt sleep.
        """
        # Intentional no-op: 429 is rate-limiting, not an availability
        # signal. Tracking 429s here would mask transient quota issues
        # as upstream outages and unnecessarily degrade the ensemble
        # to 3-leg score-only mode.
        return

    def _record_failure(self) -> None:
        with self._lock:
            now = self._clock()
            self._maybe_transition_open_to_half_open(now)
            if self._state is BreakerState.HALF_OPEN:
                logger.warning(
                    "perplexity_breaker: state HALF_OPEN -> OPEN (probe failed)"
                )
                self._state = BreakerState.OPEN
                self._opened_at = now
                self._events.clear()
                return
            if self._state is BreakerState.OPEN:
                # Defensive no-op (caller should be gated on allow_request).
                return
            self._events.append((now, True))
            self._prune(now)
            self._maybe_open_from_closed(now)

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Return the breaker to its initial CLOSED, empty-window state.

        Used by tests; production code should never need this.
        """
        with self._lock:
            self._state = BreakerState.CLOSED
            self._opened_at = None
            self._events.clear()

    def force_open(self) -> None:
        """Force the breaker into OPEN as of ``clock()`` now.

        Used by tests to exercise the OPEN / HALF_OPEN paths without
        having to inject a synthetic 5xx burst.
        """
        with self._lock:
            self._state = BreakerState.OPEN
            self._opened_at = self._clock()
            self._events.clear()


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------


_BREAKER: Optional[PerplexityBreaker] = None
_BREAKER_LOCK = threading.Lock()


def get_breaker() -> PerplexityBreaker:
    """Return the process-wide :class:`PerplexityBreaker` singleton."""
    global _BREAKER
    if _BREAKER is None:
        with _BREAKER_LOCK:
            if _BREAKER is None:
                _BREAKER = PerplexityBreaker()
    return _BREAKER


def reset_breaker_for_test() -> None:
    """Discard the module singleton so the next :func:`get_breaker`
    call constructs a fresh CLOSED instance.

    This is the ONLY supported way to reset breaker state across
    tests. Production code never calls this.
    """
    global _BREAKER
    with _BREAKER_LOCK:
        _BREAKER = None


# ---------------------------------------------------------------------------
# Module-level convenience wrappers
# ---------------------------------------------------------------------------


def record_success() -> None:
    """Record a successful Perplexity outcome on the singleton."""
    get_breaker().record_success()


def record_5xx_failure() -> None:
    """Record a 5xx failure on the singleton."""
    get_breaker().record_5xx_failure()


def record_timeout() -> None:
    """Record a transport timeout on the singleton."""
    get_breaker().record_timeout()


def record_429() -> None:
    """Record a 429 (no-op, but exposed for symmetry)."""
    get_breaker().record_429()


def is_open() -> bool:
    """Return ``True`` iff the singleton breaker is OPEN."""
    return get_breaker().is_open()


def allow_request() -> bool:
    """Return ``True`` iff a Perplexity call is permitted right now."""
    return get_breaker().allow_request()


# ---------------------------------------------------------------------------
# Retry-After header parsing (VAL-M3-095)
# ---------------------------------------------------------------------------


def parse_retry_after(value: Any) -> Optional[float]:
    """Parse a ``Retry-After`` header value into seconds.

    Supports both forms documented in RFC 7231 §7.1.3:

    * ``Retry-After: 120`` — a non-negative integer count of seconds
      to wait.
    * ``Retry-After: Wed, 21 Oct 2099 07:28:00 GMT`` — an HTTP-date.
      The returned value is ``max(0.0, (date - now).total_seconds())``.

    Returns ``None`` for missing / blank / unparseable inputs so the
    caller can fall back to its geometric backoff curve.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # bool is an int subclass; reject to avoid 'True' → 1.0 surprises.
        return None
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # Numeric seconds form first.
    try:
        return max(0.0, float(s))
    except ValueError:
        pass
    # HTTP-date form.
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        # RFC 7231 §7.1.1.1: HTTP-dates are always in GMT.
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, float(delta))
