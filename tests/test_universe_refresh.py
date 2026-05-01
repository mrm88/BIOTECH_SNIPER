"""Dual-path collection shim for the source-fallback regression.

The validation contract (VAL-CROSS-035, VAL-CROSS-036) cites the
node IDs ``tests/test_universe_refresh.py::test_ishares_404_falls_back_to_last_good``
and ``tests/test_universe_refresh.py::test_both_sources_404_deterministic``,
but the worker's verification command points at
``tests/test_source_fallback.py``. Per the python-worker skill's
dual-path test convention, the actual test bodies live in the
verification path and this module re-exports them so both
collection node-IDs pass without test-body duplication.
"""

# noqa: F401  — wildcard re-export keeps both pytest collection
# paths in sync without duplicating test source.
from tests.test_source_fallback import *  # noqa: F401,F403
