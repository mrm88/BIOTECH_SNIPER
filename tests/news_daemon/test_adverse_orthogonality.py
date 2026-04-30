"""Validator-path alias for the adverse-news orthogonality tests.

The Reading-B validation contract pins assertion evidence under the
canonical path ``tests/news_daemon/test_adverse_orthogonality.py``
(see VAL-M2-029, VAL-M2-030).  The feature-level verification step
uses the flat path ``tests/test_adverse_news_orthogonality.py``
instead.

To make BOTH locations runnable from the same source-of-truth this
module re-exports the relevant test classes from
``tests.test_adverse_news_orthogonality``.

Maintenance: NEW test cases land in
``tests/test_adverse_news_orthogonality.py`` ONLY.  This module is
a thin import shim and should not redefine the test logic.
"""

from __future__ import annotations

from tests.test_adverse_news_orthogonality import (  # noqa: F401
    TestAdverseNewsModuleUnchanged,
    TestConcurrentEmitAndExit,
    TestStage1AndAdverseNewsOrthogonal,
    _wal_busy_timeout,
    make_runner,
    v10_db,
)
