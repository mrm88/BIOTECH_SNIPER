"""Validator-path alias for the news_daemon emit tests.

The Reading-B validation contract pins assertion evidence under the
canonical path ``tests/news_daemon/test_emit.py`` (see VAL-M2-022,
VAL-M2-023, VAL-M2-024, VAL-M2-025).  The feature-level verification
step uses the flat path ``tests/test_news_daemon_emit.py`` instead.

To make BOTH locations runnable from the same source-of-truth this
module re-exports the relevant test classes/functions from
``tests.test_news_daemon_emit``.

Maintenance: NEW test cases land in ``tests/test_news_daemon_emit.py``
ONLY.  This module is a thin import shim and should not redefine the
test logic.
"""

from __future__ import annotations

from tests.test_news_daemon_emit import (  # noqa: F401
    TestCandidateEventsSchemaVAL_M2_022,
    TestDedupKeyFormulaVAL_M2_023,
    TestIterPendingNewsEvents,
    TestMakeCandidate,
    TestRunOnePollCycleVAL_M2_024,
    TestWatermarkRecovery,
    TestWriteCandidate,
    TestWriteCandidatesBatch,
    test_emit_module_no_phantom_dedup_key_on_news_events,
    test_emit_module_uses_executemany_for_batch,
    test_emit_module_uses_insert_or_ignore,
    v10_db,
)
