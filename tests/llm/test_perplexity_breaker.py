"""Dual-path shim for the Perplexity breaker tests.

The validation contract evidence cites
``tests/llm/test_perplexity_breaker.py``; the feature's verification
step runs ``tests/test_perplexity_breaker.py``. Per the python-worker
skill's dual-path test convention, the actual test bodies live at the
feature path and this module re-exports them so both pytest node-ID
forms collect and pass.

The autouse fixture from the source module does NOT re-bind through
``import *`` because pytest discovers autouse fixtures by the module
in which the fixture function is defined. Re-declare a local
autouse fixture below so this collection path also resets the
breaker singleton between tests.
"""

import pytest

from tests.test_perplexity_breaker import *  # noqa: F401,F403

from biotech_sniper.exec import breaker as _breaker_module


@pytest.fixture(autouse=True)
def _reset_global_breaker_dual_path():
    """Mirror of the source module's autouse breaker reset."""
    _breaker_module.reset_breaker_for_test()
    yield
    _breaker_module.reset_breaker_for_test()
