"""Top-level shim re-exporting the universal news watcher signal vocab.

The Reading-B M2 ``news_daemon.matcher`` (and its f-m2-05 verification
step) import :data:`TIER_1_SIGNALS` and :data:`TIER_2_SIGNALS` from
``biotech_sniper.universal_news_watcher`` — a top-level path that
predates the prior mission's namespace cleanup that moved the
canonical implementation to
:mod:`biotech_sniper.intelligence.universal_news_watcher`.

This module is a **thin shim** that:

1. Re-exports the canonical lists as :class:`frozenset` objects so
   ``TIER_1_SIGNALS | TIER_2_SIGNALS`` works as set-union (the
   verification command uses the ``|`` operator). The underlying
   :mod:`biotech_sniper.intelligence.universal_news_watcher` module
   keeps the lists as ``list`` for backward compatibility with the
   ordering-sensitive ``_score_text`` function.
2. Re-exports :data:`CATALYST_KEYWORDS` (the union) so external
   callers have a single named symbol for the full Stage-1 vocab.

Reusing the canonical lists (NOT redefining them) is the
contract-pinned invariant from VAL-M2-015 — a CI grep ensures no
literal ``TIER_1_SIGNALS = [...]`` redefinition exists outside the
canonical module.

This module performs **no** heavy imports — the canonical
universal_news_watcher module's import side effects (RSS feed
constants, SEC headers, etc.) are still executed because we
import the constants from it, but no logging is configured here.
"""

from __future__ import annotations

from biotech_sniper.intelligence.universal_news_watcher import (
    TIER_1_SIGNALS as _TIER_1_LIST,
    TIER_2_SIGNALS as _TIER_2_LIST,
)

#: TIER-1 catalyst signal keywords.  Re-exported from
#: :mod:`biotech_sniper.intelligence.universal_news_watcher` as a
#: :class:`frozenset` so set algebra (``|``, ``<=``) is available
#: at the call site.
TIER_1_SIGNALS: frozenset[str] = frozenset(_TIER_1_LIST)

#: TIER-2 catalyst signal keywords.  Re-exported from
#: :mod:`biotech_sniper.intelligence.universal_news_watcher` as a
#: :class:`frozenset`.
TIER_2_SIGNALS: frozenset[str] = frozenset(_TIER_2_LIST)

#: Union of TIER-1 and TIER-2 signal keywords.  Used by the Stage-1
#: news daemon matcher (:mod:`biotech_sniper.news_daemon.matcher`).
CATALYST_KEYWORDS: frozenset[str] = TIER_1_SIGNALS | TIER_2_SIGNALS

__all__ = ["TIER_1_SIGNALS", "TIER_2_SIGNALS", "CATALYST_KEYWORDS"]
