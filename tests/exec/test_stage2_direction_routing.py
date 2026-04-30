"""Dual-path shim for the Stage-2 dispatcher direction-routing tests.

The validation contract Evidence cites node IDs of the form
``tests/exec/test_stage2_direction_routing.py::test_*`` (see
VAL-M3-048 .. VAL-M3-052, VAL-M3-080, VAL-M3-092 in
``validation-contract.md``), but the f-m3-09 feature's verification
command points at ``tests/test_stage2_dispatcher.py``. Per the
dual-path test convention in ``library`` / ``AGENTS.md`` /
``skills/python-worker/SKILL.md``, the canonical test bodies live
ONCE under the feature path and are re-exported here so both
node-ID forms collect.
"""

from tests.test_stage2_dispatcher import *  # noqa: F401,F403

# f-m3-10 adds the halted-underlying assertion VAL-M3-090; the canonical
# body lives in ``tests.test_stage2_paper_executor`` (the wiring layer),
# but the validation contract Evidence pins the node ID under this
# direction-routing path. Re-export the single test the contract names
# so the dual-path collection still works.
from tests.test_stage2_paper_executor import (  # noqa: F401
    test_halted_underlying_rejects_cleanly,
)
