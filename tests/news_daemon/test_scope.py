"""Validator-path alias for the news_daemon scope tests.

The Reading-B validation contract pins assertion evidence under the
canonical path ``tests/news_daemon/test_scope.py`` (see VAL-M2-013,
VAL-M2-014, VAL-M2-053, VAL-M2-054). The feature-level verification
step uses the flat path ``tests/test_news_daemon_scope.py`` instead.

To make BOTH locations runnable from the same source-of-truth this
module re-exports the relevant test functions from
``tests.test_news_daemon_scope``. Pytest collects the imported
functions at the new module path so node IDs like
``tests/news_daemon/test_scope.py::test_excluded_ticker_never_polled``
resolve and pass.

Maintenance: NEW test cases land in
``tests/test_news_daemon_scope.py`` ONLY. This module is a thin
import shim and should not redefine the test logic.
"""

from __future__ import annotations

from tests.test_news_daemon_scope import (  # noqa: F401
    seeded_db,
    empty_russell_db,
    test_allowed_tiers_pinned_to_watch_and_tradeable,
    test_empty_russell2k_idle,
    test_empty_russell_returns_empty_set_when_db_missing,
    test_empty_russell_returns_empty_set_when_table_missing,
    test_empty_ticker_silently_rejected,
    test_empty_warning_message_pinned,
    test_excluded_ticker_never_polled,
    test_filter_universe_case_sensitive,
    test_filter_universe_dedups_repeated_tickers,
    test_filter_universe_drops_none_and_non_string,
    test_filter_universe_empty_polled_returns_empty,
    test_filter_universe_handles_empty_iterable,
    test_filter_universe_strips_surrounding_whitespace,
    test_full_synthetic_filter_only_polls_intersection,
    test_load_polled_universe_alias_matches_resolve,
    test_module_public_surface,
    test_non_biotech_in_universe_skipped,
    test_only_watch_and_tradeable_tiers_polled,
    test_resolve_accepts_pre_opened_connection,
    test_resolve_polled_tickers_set_equality,
    test_universe_only_ticker_with_unrelated_tier_skipped,
    test_universe_table_missing_logs_warning,
)
