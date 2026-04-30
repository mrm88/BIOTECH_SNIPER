"""Validator-path alias for the f-m2-08 heartbeat tests.

The Reading-B validation contract pins assertion evidence under the
canonical path ``tests/news_daemon/test_heartbeat.py`` (see
VAL-M2-031 and VAL-M2-032). The feature-level verification step uses
the flat path ``tests/test_heartbeat.py`` instead.

To make BOTH locations runnable from the same source-of-truth this
module re-exports the relevant test functions from
:mod:`tests.test_heartbeat`. Pytest collects the imported functions
at the new module path so node IDs like
``tests/news_daemon/test_heartbeat.py::test_stale_predicate_5min_threshold``
resolve and pass.

Maintenance: NEW test cases land in ``tests/test_heartbeat.py``
ONLY. This module is a thin import shim and should not redefine the
test logic.
"""

from __future__ import annotations

from tests.test_heartbeat import (  # noqa: F401
    heartbeat_path,
    test_default_heartbeat_path_uses_state_dir,
    test_default_stale_threshold_is_300_seconds,
    test_heartbeat_field_types_match_spec,
    test_heartbeat_schema_keys_match_spec,
    test_last_poll_ts_advances_on_each_write,
    test_module_exports_required_symbols,
    test_mtime_advances_on_each_write,
    test_resolve_version_sha_fallback_to_placeholder,
    test_resolve_version_sha_honours_env_override,
    test_resolve_version_sha_rejects_short_override,
    test_resolve_version_sha_returns_40_char_lowercase_hex,
    test_round_trip_preserves_payload,
    test_stale_predicate_5min_threshold,
    test_stale_predicate_at_boundary_exact_300s_is_fresh,
    test_stale_predicate_fresh_returns_false,
    test_stale_predicate_handles_z_suffix,
    test_stale_predicate_malformed_json_is_stale,
    test_stale_predicate_missing_file_is_stale,
    test_stale_predicate_unparseable_timestamp_is_stale,
    test_write_accepts_mapping,
    test_write_atomic_no_partial_file_visible,
    test_write_creates_parent_directory,
    test_write_rejects_payload_missing_required_keys,
    test_write_under_concurrent_reader,
    test_write_uses_os_replace_via_tmp_sibling,
)
