"""Alias module for :mod:`biotech_sniper.news_daemon.emit`.

The architecture documents (``library/architecture.md``,
``library/news-daemon.md``, mission ``mission.md``, and the
feature description for f-m2-01) all refer to the writer module as
``emit.py``.  The validation contract (VAL-M2-001 / VAL-M2-003)
references it as ``emitter.py``.

Both names refer to the same submodule.  This file is a thin
re-export so external import paths
(``from biotech_sniper.news_daemon.emitter import …``) work
identically to ``from biotech_sniper.news_daemon.emit import …``.

The canonical implementation lives in :mod:`emit`; new code should
prefer that name.  This alias exists so the validator's
import-smoke test (``import biotech_sniper.news_daemon.emitter``)
passes without forking the implementation.
"""

from __future__ import annotations

# Re-export the full public surface of :mod:`emit`.  Star-import is
# safe here because :mod:`emit` defines an explicit ``__all__``.
from biotech_sniper.news_daemon.emit import *  # noqa: F401,F403
from biotech_sniper.news_daemon.emit import (  # noqa: F401  (explicit re-export for IDEs)
    FIELD_SEPARATOR,
    CandidateEvent,
    compute_dedup_key,
    write_candidate,
    write_candidates,
)

__all__ = [
    "FIELD_SEPARATOR",
    "CandidateEvent",
    "compute_dedup_key",
    "write_candidate",
    "write_candidates",
]
