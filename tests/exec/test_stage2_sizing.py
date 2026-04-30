"""Dual-path shim for Stage-2 paper-executor sizing / cap tests.

The validation contract Evidence cites node IDs of the form
``tests/exec/test_stage2_sizing.py::test_*`` (see VAL-M3-053 ..
VAL-M3-056, VAL-M3-091, VAL-M3-092 in ``validation-contract.md``)
but the f-m3-10 feature's verification command points at
``tests/test_stage2_paper_executor.py``. Per the dual-path test
convention in ``library`` / ``AGENTS.md`` /
``skills/python-worker/SKILL.md``, the canonical bodies live ONCE
under the feature path and are re-exported here so both node-ID
forms collect.
"""

from tests.test_stage2_paper_executor import *  # noqa: F401,F403
