"""Validator-path alias for the news_daemon matcher tests.

The Reading-B validation contract pins assertion evidence under the
canonical path ``tests/news_daemon/test_matcher.py`` (see VAL-M2-015,
VAL-M2-016, VAL-M2-017, VAL-M2-018, VAL-M2-019). The feature-level
verification step uses the flat path ``tests/test_news_daemon_matcher.py``
instead.

To make BOTH locations runnable from the same source-of-truth this
module re-exports the relevant test classes/functions from
``tests.test_news_daemon_matcher``.

Maintenance: NEW test cases land in
``tests/test_news_daemon_matcher.py`` ONLY. This module is a thin
import shim and should not redefine the test logic.
"""

from __future__ import annotations

from tests.test_news_daemon_matcher import (  # noqa: F401
    TestCalendarMatchWithinWindowVAL_M2_020,
    TestCalendarMissStillEmitsVAL_M2_021,
    TestCatalystKeywordsReuseVAL_M2_015,
    TestMatchedKeywordsSortedDedupVAL_M2_019,
    TestMatcherDoesNotRaise,
    TestMatchKeywordsHelper,
    TestMatchResult,
    TestMnAKeywordsVAL_M2_017,
    TestPartnershipNoThresholdVAL_M2_016,
    TestRegulatorySubmissionVAL_M2_018,
    calendar_db,
    empty_calendar_db,
    no_calendar_table_db,
    test_calendar_window_default_is_90_days,
)
