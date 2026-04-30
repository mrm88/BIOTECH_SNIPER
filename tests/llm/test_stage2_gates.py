"""Dual-path shim for the Stage-2 gate tests.

The validation contract Evidence cites node IDs of the form
``tests/llm/test_stage2_gates.py::test_*`` (see VAL-M3-022 .. VAL-M3-026
in ``validation-contract.md``), but the f-m3-04 feature's verification
command points at ``tests/test_stage2_probability_gate.py``. Per the
dual-path test convention in ``library`` / ``AGENTS.md``, the canonical
test bodies live ONCE under the feature path and are re-exported here
so both node-ID forms collect.

Subsequent f-m3 features (unanimity, direction, cap-projection) extend
this file by adding their own re-exports — keep one source of truth
per gate.
"""

from tests.test_stage2_probability_gate import *  # noqa: F401,F403
from tests.test_stage2_unanimity_gate import *  # noqa: F401,F403
from tests.test_armed_gate import *  # noqa: F401,F403
