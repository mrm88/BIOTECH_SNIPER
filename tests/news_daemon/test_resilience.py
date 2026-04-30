"""Validator-path alias for the news_daemon resilience tests.

The Reading-B validation contract pins assertion evidence under the
canonical path ``tests/news_daemon/test_resilience.py`` (see
VAL-M2-037, VAL-M2-038, VAL-M2-039, VAL-M2-040, VAL-M2-043,
VAL-M2-044, VAL-M2-047, VAL-M2-049, VAL-M2-052).  The feature-level
verification step uses the flat path
``tests/test_news_daemon_resilience.py`` instead.

To make BOTH locations runnable from the same source-of-truth this
module re-exports the relevant test classes/functions from
:mod:`tests.test_news_daemon_resilience`.

Maintenance: NEW test cases land in
``tests/test_news_daemon_resilience.py`` ONLY.  This module is a
thin import shim and should not redefine the test logic.
"""

from __future__ import annotations

from tests.test_news_daemon_resilience import (  # noqa: F401
    TestAllRssSourcesFailure,
    TestClockSkew,
    TestHeartbeatFlushOnDrain,
    TestMaxWorkersInvariant,
    TestNewsSpike,
    TestPollCycleExceptionNonFatal,
    TestResourceCapsInUnit,
    TestRestartAtomicState,
    TestSigtermDrainAndCleanExit,
    TestSingleRssSourceFailure,
    TestSustainedRunMemoryBounded,
    heartbeat_path,
    v10_db,
)


# Top-level node-ID aliases so the validation contract evidence
# commands ``tests/news_daemon/test_resilience.py::test_<name>``
# resolve to the underlying test methods on the imported test
# classes.  pytest collects test functions, not test methods, when
# the function lives at module scope, so each of these is a thin
# wrapper that calls the underlying class' test method on a fresh
# instance with the right fixtures.

def test_one_rss_source_500_others_fine(v10_db, heartbeat_path) -> None:
    TestSingleRssSourceFailure().test_one_rss_source_500_others_fine(
        v10_db, heartbeat_path
    )


def test_all_sources_500_daemon_survives(v10_db, heartbeat_path) -> None:
    TestAllRssSourcesFailure().test_all_sources_500_daemon_survives(
        v10_db, heartbeat_path
    )


def test_news_spike_1000_headlines(v10_db) -> None:
    TestNewsSpike().test_news_spike_1000_headlines_single_transaction(v10_db)


def test_sigterm_mid_poll_atomic(v10_db) -> None:
    TestRestartAtomicState().test_partial_batch_failure_rolls_back_atomically(
        v10_db
    )


def test_clock_skew_no_dedup_corruption() -> None:
    TestClockSkew().test_clock_skew_no_dedup_corruption()


def test_sigterm_drain_and_clean_exit(v10_db, heartbeat_path) -> None:
    TestSigtermDrainAndCleanExit().test_sigterm_drain_and_clean_exit(
        v10_db, heartbeat_path
    )
