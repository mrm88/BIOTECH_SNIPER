"""Structured JSON logging — single source of truth for the project.

This module wires the standard :mod:`logging` framework with a JSON
formatter that emits one canonical JSON object per line:

.. code-block:: json

    {"ts": "2026-04-27T13:14:15.123456+00:00",
     "level": "INFO",
     "event": "daily_start",
     "module": "master_unified_run",
     "duration_ms": 4231}

Every line carries the four contract-required keys ``ts``, ``level``,
``event`` and ``module`` (per VAL-M4-024 / VAL-M4-025). Additional
context propagates through the ``extra=`` mapping on the
:class:`logging.Logger` calls and is merged into the JSON payload.

Usage in modules
----------------

.. code-block:: python

    from biotech_sniper import logging_setup

    log = logging_setup.get_logger(__name__)
    log.info(
        "daily_start",
        extra={"event": "daily_start", "date": "2026-04-27"},
    )

Entrypoints (``master_unified_run``, ``intraday_scanner``,
``watchdog``) call :func:`configure` once at process start to bind the
file destination:

.. code-block:: python

    logging_setup.configure(log_name="daily")  # → /var/log/alpha_sniper/daily.log

Path resolution priority (first hit wins):

1. ``log_path`` argument passed to :func:`configure`.
2. ``ALPHA_SNIPER_LOG_PATH`` env var (full path override).
3. ``ALPHA_SNIPER_LOG_DIR`` env var + ``<log_name>.log``.
4. ``/var/log/alpha_sniper/<log_name>.log`` (default VPS layout).
5. Stream-only (no file handler) when nothing is writable.

Secret redaction
----------------

Any ``extra=`` field whose key contains the substring ``key``,
``secret``, ``token``, or ``authorization`` (case-insensitive) is
serialized as the literal string ``"***"`` instead of its real value
(per VAL-M4-026 and the f-m4-02 secret-redaction contract). Redaction
recurses into nested dicts/lists so a payload such as
``{"headers": {"Authorization": "Bearer …"}}`` redacts the inner
field.

Replaces ``logging.basicConfig``
--------------------------------

This module is the project's single source of truth for log
configuration; no module should call :func:`logging.basicConfig`
directly. New modules should obtain a logger via
:func:`get_logger` (or the standard
``logging.getLogger(__name__)`` — both are fine because
:func:`configure` attaches the JSON formatter to the *root* logger so
every child logger inherits it).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from biotech_sniper.paths import DEFAULT_VPS_LOG_DIR

__all__ = [
    "JSONFormatter",
    "configure",
    "get_logger",
    "REDACT_TOKEN",
    "DEFAULT_LOG_DIR",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Token written in place of any sensitive field value.
REDACT_TOKEN: str = "***"

#: Default log directory on the VPS. Overridable via ``ALPHA_SNIPER_LOG_DIR``.
#: Re-exported from :mod:`biotech_sniper.paths` so this module never
#: hard-codes an absolute filesystem location — ``paths.py`` is the
#: single source of truth for path constants across the package.
DEFAULT_LOG_DIR: Path = DEFAULT_VPS_LOG_DIR

# Substrings (lowercased) that mark a field key as sensitive.
_SENSITIVE_SUBSTRINGS: tuple[str, ...] = (
    "key",
    "secret",
    "token",
    "authorization",
)

# Standard :class:`logging.LogRecord` attributes we never copy as extras.
# Anything not in this set that lives on ``record.__dict__`` was passed
# in by the caller via ``extra=`` and should be merged into the payload.
_STD_LOGRECORD_ATTRS: frozenset[str] = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "asctime",
        "taskName",
    }
)


# ---------------------------------------------------------------------------
# Redaction helpers
# ---------------------------------------------------------------------------


def _is_sensitive_key(key: Any) -> bool:
    """Return ``True`` when *key* names a sensitive field."""
    text = str(key).lower()
    return any(needle in text for needle in _SENSITIVE_SUBSTRINGS)


def _redact(value: Any) -> Any:
    """Recursively redact sensitive values nested inside *value*.

    Mappings are walked key-by-key; lists/tuples are walked element-wise.
    Scalars are returned unchanged. Sensitive keys collapse to
    :data:`REDACT_TOKEN` regardless of the underlying value's type.
    """
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if _is_sensitive_key(k):
                out[str(k)] = REDACT_TOKEN
            else:
                out[str(k)] = _redact(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    """Emit one JSON object per :class:`logging.LogRecord`.

    The formatter guarantees the four contract-required fields
    (``ts``, ``level``, ``event``, ``module``) on every line. Any
    caller-supplied ``extra=`` keys are merged into the JSON payload
    after being passed through :func:`_redact`.
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401 - imperative
        # Timestamp: ISO-8601 UTC with explicit ``+00:00`` suffix so
        # the validator's substring/timezone check (VAL-M4-024) passes.
        ts = (
            datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="microseconds")
        )

        # ``record.module`` is the file basename minus ``.py`` set by
        # the logging framework. It is always a non-empty string for
        # records originating from real code, satisfying the
        # ``module`` requirement.
        module = getattr(record, "module", record.name) or record.name

        # Event: prefer caller-supplied ``event`` extra; fall back to
        # the formatted message string. Either way the field is always
        # populated.
        event_value = getattr(record, "event", None)
        if event_value in (None, ""):
            event_value = record.getMessage()

        payload: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "event": str(event_value),
            "module": module,
        }

        # Preserve the message verbatim when it differs from the event
        # so structured callers can keep a human-readable description
        # alongside the snake_case event identifier.
        message = record.getMessage()
        if message and message != payload["event"]:
            payload["message"] = message

        # Merge ``extra=`` fields. ``record.__dict__`` carries them as
        # plain attributes; anything not in the standard set was
        # injected by the caller.
        for key, value in record.__dict__.items():
            if key in _STD_LOGRECORD_ATTRS:
                continue
            if key in payload:
                continue
            if key.startswith("_"):
                continue
            payload[key] = value

        # Exception info (formatted traceback) when present.
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if getattr(record, "stack_info", None):
            payload["stack_info"] = record.stack_info

        # Final pass: redact sensitive fields anywhere in the payload.
        redacted = _redact(payload)

        # ``default=str`` keeps unusual types (e.g. ``Path``,
        # ``datetime``) serializable without hard-failing the call
        # site. ``ensure_ascii=False`` keeps unicode legible.
        return json.dumps(redacted, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


_CONFIGURED: bool = False


def _resolve_log_path(
    log_path: str | os.PathLike[str] | None,
    log_name: str,
) -> Path | None:
    """Resolve the destination log file path.

    Returns ``None`` when no writable path can be determined; in that
    case :func:`configure` falls back to a stream-only handler.
    """
    if log_path is not None:
        return Path(log_path)
    env_path = os.environ.get("ALPHA_SNIPER_LOG_PATH")
    if env_path:
        return Path(env_path)
    env_dir = os.environ.get("ALPHA_SNIPER_LOG_DIR")
    if env_dir:
        return Path(env_dir) / f"{log_name}.log"
    # Default VPS layout. Probe writability without raising — the
    # default directory does not exist on developer laptops.
    return DEFAULT_LOG_DIR / f"{log_name}.log"


def _coerce_level(level: int | str) -> int:
    if isinstance(level, str):
        numeric = logging.getLevelName(level.upper())
        if isinstance(numeric, int):
            return numeric
        # ``getLevelName`` returns a string for unknown names; fall
        # back to INFO so a typo never silences logs entirely.
        return logging.INFO
    return int(level)


def configure(
    level: int | str = logging.INFO,
    log_path: str | os.PathLike[str] | None = None,
    log_name: str = "app",
    *,
    add_stream: bool = True,
    force: bool = False,
) -> logging.Logger:
    """Install the JSON formatter on the root logger.

    Parameters
    ----------
    level:
        Minimum log level; integer or case-insensitive name.
    log_path:
        Optional explicit destination file. Overrides env-var
        resolution.
    log_name:
        Used to form the default destination filename
        (``<log_name>.log``) when neither ``log_path`` nor
        ``ALPHA_SNIPER_LOG_PATH`` is set. Entrypoints typically pass
        ``"daily"``, ``"intraday"`` or ``"watchdog"`` to produce
        ``/var/log/alpha_sniper/daily.log`` etc. on the VPS.
    add_stream:
        When ``True`` (default) attach a :class:`logging.StreamHandler`
        on top of the file handler so journalctl / interactive runs
        still see structured output.
    force:
        Reconfigure even if :func:`configure` has already been called
        once in this process (handy for tests that need a clean slate).

    Returns
    -------
    :class:`logging.Logger`
        The reconfigured root logger.
    """
    global _CONFIGURED

    root = logging.getLogger()
    if _CONFIGURED and not force:
        return root

    # Strip any pre-existing handlers (legacy ``logging.basicConfig``,
    # third-party libs, etc.) so the project really has a single
    # source of truth for log output. This is the contract surface
    # exercised by VAL-M4-027 (no duplicate basicConfig calls).
    for handler in list(root.handlers):
        root.removeHandler(handler)

    root.setLevel(_coerce_level(level))

    formatter = JSONFormatter()

    if add_stream:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    resolved = _resolve_log_path(log_path, log_name=log_name)
    if resolved is not None:
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(str(resolved), encoding="utf-8")
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except (OSError, PermissionError):
            # The default ``/var/log/alpha_sniper`` is not writable
            # for unprivileged users (developer laptops, CI). Keep
            # the stream handler and proceed — production runs as
            # root via systemd and will succeed. We deliberately do
            # not raise so importing modules can call ``configure``
            # defensively.
            pass

    _CONFIGURED = True
    return root


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a project logger, configuring the root on first use.

    Equivalent to :func:`logging.getLogger` but ensures the JSON
    formatter is installed before the first record is emitted.
    """
    if not _CONFIGURED:
        configure()
    return logging.getLogger(name)
