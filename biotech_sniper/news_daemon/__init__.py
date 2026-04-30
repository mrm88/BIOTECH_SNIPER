"""Stage-1 news-watcher daemon package.

This package implements the long-lived ``alpha-sniper-news.service``
poll loop that consumes :mod:`biotech_sniper.news_events` rows,
intersects with the ``russell2k_biotech`` universe, applies the
catalyst keyword + trial-calendar matcher, and emits ``candidate_events``
rows for the Stage-2 ensemble scorer to score asynchronously.

Design contract (Reading-B M2)
------------------------------

The package is **synchronous-only**: only ``requests`` and
``feedparser`` are permitted transports.  Non-blocking I/O stacks
(see ``library/news-daemon.md`` for the disallowed list) are
forbidden and a CI grep enforces their absence in the source tree.

The package never imports the Stage-2 scoring submodule tree (the
4-provider ensemble lives outside this package) and never makes
outbound HTTP calls to scoring-provider APIs.  Stage-1 is a pure
data emitter; Stage-2 owns scoring and gating.

Per-restart deduplication is sourced from the
``candidate_events.dedup_key`` ``UNIQUE`` constraint via
``INSERT OR IGNORE``.  A module-level in-memory dedup set is
**forbidden**: it would silently degrade dedup across daemon
restarts (SIGKILL, OOM, systemd ``Restart=on-failure``) where
in-memory state is lost but the database persists.

Submodules
----------

* :mod:`biotech_sniper.news_daemon.poll_loop` — main loop + CLI entry.
* :mod:`biotech_sniper.news_daemon.scope` — universe filter
  (russell2k_biotech ∩ universe.tier ∈ {watch, tradeable}).
* :mod:`biotech_sniper.news_daemon.matcher` — TIER-1/TIER-2 catalyst
  keyword + partnership/M&A/IND-NDA-BLA-sNDA matcher with
  trial-calendar lookup.
* :mod:`biotech_sniper.news_daemon.emit` — ``candidate_events`` writer
  with ``INSERT OR IGNORE`` on ``dedup_key``.
* :mod:`biotech_sniper.news_daemon.emitter` — alias re-export of
  :mod:`biotech_sniper.news_daemon.emit` for legacy import paths.
* :mod:`biotech_sniper.news_daemon.heartbeat` — atomic
  ``state/news_daemon_heartbeat.json`` writer (via :func:`os.replace`).
* :mod:`biotech_sniper.news_daemon.log` — JSON logger with
  ≤ 4 KB/line truncation.

The actual logic lives in those submodules; this top-level
``__init__`` intentionally performs **no** transitive imports so
``import biotech_sniper.news_daemon`` is cheap and side-effect-free.
"""

from __future__ import annotations

# Public package version.  Bumped when behaviour changes.
__version__ = "0.1.0"

__all__ = ["__version__"]
