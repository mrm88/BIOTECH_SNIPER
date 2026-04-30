"""Validator-path alias for the news_daemon dedup/restart tests.

The Reading-B validation contract pins assertion evidence under the
canonical path ``tests/news_daemon/test_dedup_restart.py`` (see
VAL-M2-026, VAL-M2-027, VAL-M2-028).  The feature-level verification
step uses the flat path ``tests/test_news_daemon_dedup.py`` instead.

To make BOTH locations runnable from the same source-of-truth this
module re-exports the relevant test classes/functions from
``tests.test_news_daemon_dedup``.

Maintenance: NEW test cases land in ``tests/test_news_daemon_dedup.py``
ONLY.  This module is a thin import shim and should not redefine the
test logic.
"""

from __future__ import annotations

from tests.test_news_daemon_dedup import (  # noqa: F401
    TestNoDoubleEmitAfterRestartVAL_M2_027,
    TestNoPhantomDedupKeyVAL_M2_026,
    TestPipeInjectionResistanceUnderEmit,
    TestStartupReadsWatermarkFromDbVAL_M2_028,
    v10_db,
)
