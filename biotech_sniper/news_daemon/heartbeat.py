"""Atomic heartbeat writer for ``state/news_daemon_heartbeat.json``.

The watchdog (existing ``alpha-sniper-watchdog.timer``) consumes the
heartbeat file every 15 minutes and alarms when:

* the file is missing,
* the JSON fails to parse,
* the file mtime is older than 5 minutes.

Atomic write is mandatory (per ``library/news-daemon.md``):
write a sibling ``.tmp`` then ``os.replace`` to the canonical path.
A direct ``open(...,"w").write(...)`` is forbidden because it
exposes a window where the watchdog can read a half-written file
and false-alarm.

Skeleton (f-m2-01)
-------------------

Real schema + atomic write + version_sha resolution land in f-m2-08
and are asserted by VAL-M2-026..030.  This file documents the
public surface so callers can import the symbol from a stable
location.

Heartbeat schema
----------------

.. code-block:: json

    {
        "last_poll_ts": "2026-04-29T15:00:00Z",
        "candidates_emitted_total": 12345,
        "candidates_emitted_session": 42,
        "errors_session": 0,
        "version_sha": "abcd1234"
    }
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

__all__ = [
    "Heartbeat",
    "default_heartbeat_path",
    "write_heartbeat",
]


@dataclass(frozen=True)
class Heartbeat:
    """Frozen heartbeat payload."""

    last_poll_ts: str
    candidates_emitted_total: int
    candidates_emitted_session: int
    errors_session: int
    version_sha: str


def default_heartbeat_path() -> Path:
    """Return the canonical heartbeat path.

    Resolved via :mod:`biotech_sniper.paths` so the value tracks
    ``BIOTECH_SNIPER_HOME`` overrides used in unit tests.

    The import is performed inside the function (rather than at
    module top-level) so :mod:`biotech_sniper.news_daemon.heartbeat`
    stays cheap to import in the package smoke test.
    """

    from biotech_sniper.paths import STATE_DIR

    return STATE_DIR / "news_daemon_heartbeat.json"


def write_heartbeat(
    heartbeat: Heartbeat,
    path: Optional[Path] = None,
) -> Path:
    """Atomically write ``heartbeat`` to ``path`` (or the default).

    Skeleton stub (f-m2-01): no file is written.  Returns the
    target path so callers can log the resolved location even
    before the real writer lands in f-m2-08.
    """

    target = path if path is not None else default_heartbeat_path()
    return target
