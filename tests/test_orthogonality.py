"""Re-export shim for the validation-contract node IDs.

The contract evidence for VAL-CROSS-031 / VAL-CROSS-032 references
``tests/test_orthogonality.py::test_news_entry_blocked_by_cooldown``
and ``tests/test_orthogonality.py::test_adverse_exit_bypasses_cooldown``.
The single source of truth for those tests lives in
``tests/test_cooldown_utc.py`` per the dual-path test convention
documented in ``library/`` (M2-onwards). This shim re-exports the
same test functions so pytest collects them under both node IDs.

Do NOT add test bodies here — modify ``tests/test_cooldown_utc.py``
instead.
"""

from __future__ import annotations

from tests.test_cooldown_utc import *  # noqa: F401,F403
