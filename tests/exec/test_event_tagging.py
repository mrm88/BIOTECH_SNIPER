"""Dual-path shim for Stage-2 paper_orders.event-tagging tests.

The validation contract Evidence cites node IDs of the form
``tests/exec/test_event_tagging.py::test_*`` (see VAL-M3-058 in
``validation-contract.md``) but the f-m3-10 feature's verification
command points at ``tests/test_stage2_paper_executor.py``. Per the
dual-path test convention, the canonical bodies live ONCE under the
feature path and are re-exported here so both node-ID forms
collect.
"""

from tests.test_stage2_paper_executor import *  # noqa: F401,F403
