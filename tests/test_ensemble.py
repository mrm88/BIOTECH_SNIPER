"""Compatibility shim: re-exports tests from ``tests.scoring.test_ensemble``.

The validation contract for M2 places the canonical ensemble tests at
``tests/scoring/test_ensemble.py``; the feature description for
``f-m2-05-ensemble-and-flags`` references ``tests/test_ensemble.py``.

This shim re-exports every test function from the canonical module so
both pytest invocations resolve the same set of tests:

* ``pytest -q tests/test_ensemble.py``
* ``pytest -q tests/scoring/test_ensemble.py``

Pytest collects the imported callables transparently because they are
plain ``test_*`` module-level functions.
"""

from __future__ import annotations

from tests.scoring.test_ensemble import *  # noqa: F401,F403
