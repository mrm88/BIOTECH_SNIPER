"""Atomic heartbeat writer for ``state/news_daemon_heartbeat.json``.

The watchdog (existing ``alpha-sniper-watchdog.timer``) consumes the
heartbeat file every 15 minutes and alarms when:

* the file is missing,
* the JSON fails to parse,
* the file mtime — or, more precisely, the recorded
  :attr:`Heartbeat.last_poll_ts` — is older than 5 minutes.

Atomic write is mandatory (per ``library/news-daemon.md``):
write a sibling ``.tmp`` then ``os.replace`` to the canonical path.
A direct ``open(...,"w").write(...)`` is forbidden because it
exposes a window where the watchdog can read a half-written file
and false-alarm.

Heartbeat schema
----------------

.. code-block:: json

    {
        "last_poll_ts": "2026-04-29T15:00:00Z",
        "candidates_emitted_total": 12345,
        "candidates_emitted_session": 42,
        "errors_session": 0,
        "version_sha": "abcd1234abcd1234abcd1234abcd1234abcd1234"
    }

* ``last_poll_ts`` — ISO-8601 UTC timestamp of the most-recent poll
  cycle completion (the cadence anchor consumed by the M4 watchdog).
* ``candidates_emitted_total`` — running count over all recorded
  ``candidate_events`` rows in the database (stable across restarts).
* ``candidates_emitted_session`` — count emitted since the current
  process started (resets on restart).
* ``errors_session`` — count of caught-and-logged exceptions in the
  current process.
* ``version_sha`` — 40-char git SHA of the running checkout
  (``git rev-parse HEAD``); allows the watchdog and audit trail to
  pin behaviour to a specific commit.

f-m2-08 implementation
----------------------

This module owns the production heartbeat surface:

* :func:`write_heartbeat` — atomic ``os.replace`` writer (fulfils
  VAL-M2-031 / VAL-M2-032).
* :func:`read_heartbeat` — JSON loader returning a :class:`Heartbeat`.
* :func:`is_news_daemon_stale` — M4 watchdog readiness predicate;
  returns ``True`` when ``now - last_poll_ts`` exceeds the configured
  threshold (default 300 s) or the file is missing / malformed.
* :func:`resolve_version_sha` — 40-char git SHA resolver with a
  test-friendly :envvar:`BIOTECH_SNIPER_VERSION_SHA` override and a
  deterministic 40-char placeholder when ``git`` is unavailable.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

__all__ = [
    "DEFAULT_STALE_THRESHOLD_SECONDS",
    "PLACEHOLDER_VERSION_SHA",
    "Heartbeat",
    "default_heartbeat_path",
    "is_news_daemon_stale",
    "read_heartbeat",
    "resolve_version_sha",
    "write_heartbeat",
]

#: Stale-detection threshold consumed by the M4 watchdog.  The value
#: is pinned by VAL-M2-032: when ``now - last_poll_ts`` strictly
#: exceeds 300 seconds the watchdog must alarm.
DEFAULT_STALE_THRESHOLD_SECONDS: int = 300

#: 40-character placeholder used when no git checkout is available
#: (e.g. inside a sandbox container without ``.git``).  Production
#: deploys always resolve to the real SHA via ``git rev-parse HEAD``.
PLACEHOLDER_VERSION_SHA: str = "0" * 40

#: Type alias for the various path inputs accepted by :func:`write_heartbeat`.
PathLike = Union[str, os.PathLike[str], Path]


@dataclass(frozen=True)
class Heartbeat:
    """Frozen heartbeat payload.

    Field order mirrors the JSON serialisation; the JSON writer relies
    on :func:`dataclasses.asdict` to project the dataclass into a
    deterministic dict shape so the on-disk file is reproducible
    byte-for-byte across runs with identical inputs.
    """

    last_poll_ts: str
    candidates_emitted_total: int
    candidates_emitted_session: int
    errors_session: int
    version_sha: str

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-serialisable mapping."""

        return asdict(self)


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


def resolve_version_sha(repo_dir: Optional[Path] = None) -> str:
    """Return the 40-char git SHA of the running checkout.

    Resolution priority:

    1. The :envvar:`BIOTECH_SNIPER_VERSION_SHA` env var when set to
       a 40-character hexadecimal string.  This is the deterministic
       hook tests use to pin a known SHA without invoking ``git``.
    2. ``git rev-parse HEAD`` executed inside ``repo_dir`` (or the
       directory containing :mod:`biotech_sniper`).  Failures fall
       through to the placeholder.
    3. :data:`PLACEHOLDER_VERSION_SHA` as a non-fatal sentinel.

    The returned string is ALWAYS exactly 40 characters of lower-case
    hex so downstream consumers (watchdog, audit log) can validate
    the field shape with a simple length check.
    """

    env_value = os.environ.get("BIOTECH_SNIPER_VERSION_SHA", "").strip().lower()
    if len(env_value) == 40 and all(c in "0123456789abcdef" for c in env_value):
        return env_value

    if shutil.which("git") is None:
        return PLACEHOLDER_VERSION_SHA

    if repo_dir is None:
        # The git checkout root is :data:`paths.BASE_DIR` — keeping
        # the resolution centralised in ``paths.py`` avoids leaking
        # ``Path(__file__)`` constructions into the package surface
        # (forbidden by ``tests/test_no_project_writes.py``).
        from biotech_sniper.paths import BASE_DIR

        repo_dir = BASE_DIR

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return PLACEHOLDER_VERSION_SHA

    if completed.returncode != 0:
        return PLACEHOLDER_VERSION_SHA

    sha = completed.stdout.strip().lower()
    if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
        return sha
    return PLACEHOLDER_VERSION_SHA


_REQUIRED_KEYS = (
    "last_poll_ts",
    "candidates_emitted_total",
    "candidates_emitted_session",
    "errors_session",
    "version_sha",
)


def _coerce_heartbeat(payload: Union["Heartbeat", Mapping[str, Any]]) -> Heartbeat:
    """Return a :class:`Heartbeat` from a mapping or :class:`Heartbeat`-like.

    Uses duck-typing rather than ``isinstance`` so callers that
    reload :mod:`biotech_sniper.news_daemon.heartbeat` mid-process
    (test fixtures that monkey-patch :data:`paths.STATE_DIR`) get
    the same coercion behaviour for instances of the *previous*
    class generation.
    """

    if isinstance(payload, Heartbeat) or all(
        hasattr(payload, attr) for attr in _REQUIRED_KEYS
    ) and not isinstance(payload, Mapping):
        return Heartbeat(
            last_poll_ts=str(getattr(payload, "last_poll_ts")),
            candidates_emitted_total=int(getattr(payload, "candidates_emitted_total")),
            candidates_emitted_session=int(
                getattr(payload, "candidates_emitted_session")
            ),
            errors_session=int(getattr(payload, "errors_session")),
            version_sha=str(getattr(payload, "version_sha")),
        )
    # JSON-valid-but-wrong-shape payloads (top-level list, int, str,
    # None) parse cleanly via ``json.loads`` but lack both the
    # dataclass attributes AND the ``Mapping.keys()`` surface. An
    # unguarded ``payload.keys()`` call below would raise
    # ``AttributeError`` — which ``is_news_daemon_stale`` does NOT
    # catch (its narrow ``OSError``/``ValueError`` filter is the
    # contract). Convert the shape error into ``ValueError`` so the
    # existing catch propagates correctly into ``stale=True``.
    if not isinstance(payload, Mapping):
        raise ValueError(
            "heartbeat payload must be a JSON object, got "
            f"{type(payload).__name__}"
        )
    missing = set(_REQUIRED_KEYS) - set(payload.keys())
    if missing:
        raise ValueError(
            f"heartbeat payload missing required keys: {sorted(missing)}"
        )
    return Heartbeat(
        last_poll_ts=str(payload["last_poll_ts"]),
        candidates_emitted_total=int(payload["candidates_emitted_total"]),
        candidates_emitted_session=int(payload["candidates_emitted_session"]),
        errors_session=int(payload["errors_session"]),
        version_sha=str(payload["version_sha"]),
    )


def write_heartbeat(
    heartbeat: Union[Heartbeat, Mapping[str, Any]],
    path: Optional[PathLike] = None,
) -> Path:
    """Atomically write ``heartbeat`` to ``path`` (or the default).

    The write is performed in two steps to satisfy the atomicity
    contract pinned by ``library/news-daemon.md`` and VAL-M2-031:

    1. Serialise the JSON payload to a sibling ``.tmp`` file
       (``<target>.tmp``) opened in binary mode and ``fsync``'d.
    2. ``os.replace`` the temp file over the canonical target.  On
       POSIX this is a single rename syscall, so concurrent readers
       (the watchdog) NEVER observe a partial / half-written file.

    Parameters
    ----------
    heartbeat:
        Either a :class:`Heartbeat` dataclass or a mapping with the
        five required keys.
    path:
        Optional override of the destination path.  Defaults to
        :func:`default_heartbeat_path` (resolved via
        :mod:`biotech_sniper.paths`).

    Returns
    -------
    pathlib.Path
        The fully-qualified destination path.
    """

    target = Path(path) if path is not None else default_heartbeat_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = _coerce_heartbeat(heartbeat)
    body = json.dumps(payload.to_dict(), ensure_ascii=False, sort_keys=False)

    tmp = target.with_name(target.name + ".tmp")
    # Open binary so the on-disk file is a single canonical UTF-8
    # encoding regardless of locale; fsync guarantees the bytes hit
    # the disk before the rename.
    with open(tmp, "wb") as handle:
        handle.write(body.encode("utf-8"))
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            # Some test filesystems (e.g. tmpfs in CI) reject fsync;
            # the os.replace below still gives us atomicity within a
            # single device.
            pass

    os.replace(tmp, target)
    return target


def _parse_iso8601(value: str) -> Optional[datetime]:
    """Best-effort ISO-8601 parser tolerating both ``Z`` and ``+00:00``."""

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def read_heartbeat(path: Optional[PathLike] = None) -> Heartbeat:
    """Load a :class:`Heartbeat` from ``path`` (or the default).

    Raises
    ------
    FileNotFoundError
        When the heartbeat file does not exist.
    ValueError
        When the JSON payload is malformed or missing required keys.
    """

    target = Path(path) if path is not None else default_heartbeat_path()
    raw = target.read_text(encoding="utf-8")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"heartbeat JSON malformed: {exc}") from exc
    return _coerce_heartbeat(decoded)


def is_news_daemon_stale(
    path: Optional[PathLike] = None,
    threshold_seconds: int = DEFAULT_STALE_THRESHOLD_SECONDS,
    now: Optional[float] = None,
) -> bool:
    """Return ``True`` when the heartbeat is stale.

    The predicate is the canonical M4-watchdog readiness check:

    * Missing / unreadable file → stale.
    * Malformed JSON / missing keys → stale.
    * Unparseable ``last_poll_ts`` → stale.
    * ``now - last_poll_ts > threshold_seconds`` → stale.

    The boundary is **strict greater-than** so an exactly-300-second-old
    heartbeat is NOT yet stale; any subsequent tick is.

    Parameters
    ----------
    path:
        Optional heartbeat file path; defaults to
        :func:`default_heartbeat_path`.
    threshold_seconds:
        Stale-after threshold; defaults to
        :data:`DEFAULT_STALE_THRESHOLD_SECONDS` (300 s = 5 min).
    now:
        Optional override of the current epoch seconds; tests inject
        a deterministic value to pin the boundary check.

    Returns
    -------
    bool
        ``True`` when the watchdog should alarm.
    """

    target = Path(path) if path is not None else default_heartbeat_path()
    if not target.exists():
        return True

    try:
        heartbeat = read_heartbeat(target)
    except (OSError, ValueError):
        return True

    parsed = _parse_iso8601(heartbeat.last_poll_ts)
    if parsed is None:
        return True

    current = float(now) if now is not None else time.time()
    age_seconds = current - parsed.timestamp()
    return age_seconds > float(threshold_seconds)
