"""Re-export shim for the validation-contract node ID.

The contract evidence for VAL-CROSS-034 references
``tests/test_cooldown_tz.py::test_dst_invariance``. The single source
of truth for that test lives in ``tests/test_cooldown_utc.py`` per
the dual-path test convention. This shim re-exports the same test
functions so pytest collects them under both node IDs.

Do NOT add test bodies here — modify ``tests/test_cooldown_utc.py``
instead.
"""

from __future__ import annotations

from tests.test_cooldown_utc import *  # noqa: F401,F403
