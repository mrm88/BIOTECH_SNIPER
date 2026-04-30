"""Structured JSON logging helpers — ≤ 4 KB per line.

Wraps :mod:`biotech_sniper.logging_setup` (the project-wide JSON
formatter) with a per-line size cap.  Oversize string fields are
truncated with a ``"_truncated":true`` annotation and the
``"original_length"`` of the offending field — no record is dropped,
no record is silently corrupted.

Why a dedicated module?
-----------------------

The news daemon writes high-cardinality payloads (per-headline log
records that can include the headline body, RSS source URL, and
matched-keyword vocab).  A 4 KB per-line cap is a hard contract
from the systemd unit / logrotate config — exceeding it risks
journald rejection and broken JSON parsing in downstream tooling
(``jq``, the CLI report).  Centralising the truncation here keeps
the per-call sites DRY.

Log-rotation contract
---------------------

The daemon writes to a single canonical sink at
``/var/log/alpha_sniper/news.log``.  Rotation is delegated to
``logrotate`` per the M4 contract (``size 50M``, ``rotate 5``,
``compress``, ``copytruncate``); the daemon never rolls its own
files and never opens an alternate path for "rotated" output.

f-m2-08 implementation
----------------------

This module owns the production logger surface used by the
:mod:`biotech_sniper.news_daemon.poll_loop` entrypoint:

* :data:`MAX_LINE_BYTES` — hard cap (4 KB).
* :data:`DEFAULT_LOG_PATH` — the canonical sink path.
* :func:`get_news_logger` — returns the configured logger.
* :func:`truncate_field` — UTF-8-safe truncation of a single value.
* :class:`TruncatingJSONFormatter` — :class:`JSONFormatter` subclass
  that enforces the per-line cap and stamps the ``_truncated`` /
  ``original_length`` markers on the offending payload.
* :func:`configure_news_logging` — installs a file handler at
  :data:`DEFAULT_LOG_PATH` (or the env-overridden destination) with
  the truncating formatter.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from biotech_sniper.logging_setup import JSONFormatter, _redact

__all__ = [
    "DEFAULT_LOG_PATH",
    "MAX_LINE_BYTES",
    "TRUNCATION_MARKER",
    "TRUNCATION_SUFFIX",
    "TruncatingJSONFormatter",
    "configure_news_logging",
    "get_news_logger",
    "truncate_field",
]

#: Hard cap on serialised JSON record length (bytes).  Exceeding
#: this triggers field-level truncation in :func:`truncate_field`.
MAX_LINE_BYTES: int = 4 * 1024

#: Suffix appended to a truncated string to mark it.  The full
#: per-record marker is the boolean ``"_truncated": true`` key on
#: the JSON payload; this string is the visible suffix on the
#: clipped field value itself.
TRUNCATION_SUFFIX: str = "…[truncated]"

#: Backward-compat alias used by the original f-m2-01 skeleton.
TRUNCATION_MARKER: str = TRUNCATION_SUFFIX

#: Canonical news-daemon log sink.  Pinned by VAL-M2-034 and the
#: systemd unit's ``StandardOutput=append:`` / ``StandardError=append:``
#: directives.  Rotation (``size 50M`` × ``rotate 5``) is delegated
#: to ``/etc/logrotate.d/alpha-sniper-news`` — the daemon must NOT
#: roll its own files.
DEFAULT_LOG_PATH: Path = Path("/var/log/alpha_sniper/news.log")

#: Substrings reserved for the marker keys on the JSON payload so
#: oversize-detection can be performed on the serialised string and
#: the marker keys are guaranteed not to clash with caller-supplied
#: extras.
_TRUNCATED_MARKER_KEY: str = "_truncated"
_ORIGINAL_LENGTH_KEY: str = "_original_length"


def get_news_logger(name: str = "biotech_sniper.news_daemon") -> logging.Logger:
    """Return the news-daemon logger.

    Defers to :mod:`biotech_sniper.logging_setup` so the JSON
    formatter and secret-redaction hook are inherited from the
    root logger.  No ``configure()`` call is performed here —
    the entrypoint (``poll_loop.main``) is responsible for
    binding the file destination via
    :func:`configure_news_logging`.
    """

    return logging.getLogger(name)


def truncate_field(
    value: Any,
    max_bytes: int = MAX_LINE_BYTES,
) -> Tuple[Any, bool, Optional[int]]:
    """Truncate ``value`` to ``max_bytes`` UTF-8 bytes (UTF-8-safe).

    The function is a pure helper — the formatter calls it indirectly
    after JSON serialisation, but call sites that already know a
    field is large (e.g. the headline body) can pre-truncate to
    avoid the formatter's full-record retry path.

    Returns a 3-tuple ``(value, truncated, original_length)`` where:

    * ``value`` — the original value when no truncation was needed,
      otherwise the clipped string with :data:`TRUNCATION_SUFFIX`.
    * ``truncated`` — boolean flag matching the
      ``"_truncated": true`` JSON marker.
    * ``original_length`` — UTF-8 byte length of the original input
      when truncation occurred, ``None`` otherwise.
    """

    if not isinstance(value, str):
        return value, False, None

    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False, None

    suffix = TRUNCATION_SUFFIX
    suffix_bytes = suffix.encode("utf-8")
    keep = max_bytes - len(suffix_bytes)
    if keep <= 0:
        # Pathologically small budget — fall back to a bare suffix.
        return suffix, True, len(encoded)

    clipped = encoded[:keep]
    # UTF-8-safe trim: peel back any partial multi-byte tail bytes.
    while clipped and (clipped[-1] & 0xC0) == 0x80:
        clipped = clipped[:-1]

    truncated_value = clipped.decode("utf-8", errors="ignore") + suffix
    return truncated_value, True, len(encoded)


def _serialize(payload: Mapping[str, Any]) -> str:
    """JSON-serialise ``payload`` with the project conventions."""

    return json.dumps(payload, default=str, ensure_ascii=False)


_PROTECTED_KEYS: frozenset[str] = frozenset({"ts", "level", "module", "event"})


def _find_largest_truncatable_field(
    payload: Mapping[str, Any],
    already_truncated: frozenset[str] = frozenset(),
) -> Tuple[Optional[str], int]:
    """Return ``(key, byte_len)`` of the largest truncatable string field.

    Skipped keys: the contract-required ``ts`` / ``level`` / ``module``
    / ``event`` fields, any keys already prefixed with ``_`` (reserved
    for markers), and any keys named in ``already_truncated`` so the
    caller's loop converges deterministically.
    """

    candidate_key: Optional[str] = None
    candidate_len: int = 0
    for key, value in payload.items():
        if key in _PROTECTED_KEYS or key.startswith("_"):
            continue
        if key in already_truncated:
            continue
        if not isinstance(value, str):
            continue
        byte_len = len(value.encode("utf-8"))
        if byte_len > candidate_len:
            candidate_key = key
            candidate_len = byte_len

    return candidate_key, candidate_len


class TruncatingJSONFormatter(JSONFormatter):
    """:class:`JSONFormatter` with a per-line UTF-8 byte cap.

    The formatter delegates the JSON shape to the parent class
    (which guarantees the four contract-required keys ``ts`` /
    ``level`` / ``event`` / ``module`` and applies secret redaction)
    and then enforces the size cap by truncating the longest
    top-level string field — repeating until the encoded line fits
    or no truncatable field remains.
    """

    def __init__(self, max_bytes: int = MAX_LINE_BYTES) -> None:
        super().__init__()
        self.max_bytes = int(max_bytes)

    # NOTE: We re-implement ``format`` rather than calling the parent
    # because we need access to the un-serialised payload dict in
    # order to truncate top-level fields and re-serialise.  The body
    # below mirrors :class:`JSONFormatter.format` so the contract
    # surface stays in lockstep.
    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        from datetime import datetime, timezone

        ts = (
            datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="microseconds")
        )
        module = getattr(record, "module", record.name) or record.name

        event_value = getattr(record, "event", None)
        if event_value in (None, ""):
            event_value = record.getMessage()

        payload: dict = {
            "ts": ts,
            "level": record.levelname,
            "event": str(event_value),
            "module": module,
        }

        message = record.getMessage()
        if message and message != payload["event"]:
            payload["message"] = message

        # Standard LogRecord attributes we never copy as extras.
        std_attrs = {
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated",
            "thread", "threadName", "processName", "process", "message",
            "asctime", "taskName",
        }
        for key, value in record.__dict__.items():
            if key in std_attrs:
                continue
            if key in payload:
                continue
            if key.startswith("_"):
                continue
            payload[key] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if getattr(record, "stack_info", None):
            payload["stack_info"] = record.stack_info

        # Redact sensitive fields anywhere in the payload BEFORE the
        # size check (a redacted secret saves bytes on the wire).
        redacted = _redact(payload)
        # ``_redact`` returns a new dict; coerce back to a plain dict
        # so we can mutate in the truncation loop below.
        payload = dict(redacted)

        encoded = _serialize(payload)
        encoded_bytes = encoded.encode("utf-8")
        if len(encoded_bytes) <= self.max_bytes:
            return encoded

        # Oversize: iteratively truncate the longest string field
        # until the line fits.  Each iteration aims a strictly
        # smaller per-field target so the loop converges in O(N)
        # iterations where N = number of truncatable string fields.
        # If no truncatable field remains the loop exits with the
        # ``_truncated`` marker set so downstream tooling can see
        # *why* the line is still oversize.
        original_lengths: dict[str, int] = {}
        truncated_keys: set[str] = set()

        # Cap the loop to keep behaviour bounded under unexpected
        # inputs (each iteration truncates a *new* field; bound is
        # generous but finite).
        max_iters = 64
        for _ in range(max_iters):
            excess = len(encoded_bytes) - self.max_bytes
            if excess <= 0:
                return encoded

            key, byte_len = _find_largest_truncatable_field(
                payload, frozenset(truncated_keys)
            )
            if key is None:
                # No more strings to clip; emit best-effort with
                # the marker so downstream parsers know.
                payload[_TRUNCATED_MARKER_KEY] = True
                if original_lengths:
                    payload[_ORIGINAL_LENGTH_KEY] = original_lengths
                return _serialize(payload)

            # Aim a per-field byte budget that comfortably absorbs
            # the excess plus a safety buffer for the marker keys
            # we are about to add.  Floor at 64 bytes so the field
            # is still recognisable.
            safety = 256
            target = max(64, byte_len - excess - safety)
            original_value = payload[key]
            truncated_value, was_truncated, original_length = truncate_field(
                original_value, max_bytes=target
            )
            payload[key] = truncated_value
            truncated_keys.add(key)
            if original_length is not None:
                original_lengths[key] = original_length
            else:
                # Field already fit at its new target — record its
                # original byte length so the marker is still useful.
                original_lengths[key] = byte_len
            payload[_TRUNCATED_MARKER_KEY] = True
            payload[_ORIGINAL_LENGTH_KEY] = original_lengths

            encoded = _serialize(payload)
            encoded_bytes = encoded.encode("utf-8")

        # Fallback: the loop hit its bound; return the best-effort
        # serialisation with the marker set.
        payload[_TRUNCATED_MARKER_KEY] = True
        return _serialize(payload)


def _resolve_news_log_path(
    log_path: Optional[Path] = None,
) -> Path:
    """Return the destination path for the news-daemon sink.

    Resolution priority:

    1. Explicit ``log_path`` argument.
    2. :envvar:`ALPHA_SNIPER_NEWS_LOG_PATH` env var (full path).
    3. :envvar:`ALPHA_SNIPER_LOG_DIR` env var + ``/news.log``.
    4. :data:`DEFAULT_LOG_PATH` (``/var/log/alpha_sniper/news.log``).
    """

    if log_path is not None:
        return Path(log_path)
    env_path = os.environ.get("ALPHA_SNIPER_NEWS_LOG_PATH")
    if env_path:
        return Path(env_path)
    env_dir = os.environ.get("ALPHA_SNIPER_LOG_DIR")
    if env_dir:
        return Path(env_dir) / "news.log"
    return DEFAULT_LOG_PATH


def configure_news_logging(
    log_path: Optional[Path] = None,
    level: int = logging.INFO,
    *,
    add_stream: bool = False,
) -> logging.Logger:
    """Install the truncating JSON formatter on the news-daemon logger.

    The function binds a :class:`logging.FileHandler` at the resolved
    path (creating parent directories on demand) and a
    :class:`TruncatingJSONFormatter` on top of it.  The handler is
    attached to the *named* news-daemon logger (NOT the root logger)
    so the daemon's structured records are scoped to the news.log
    sink and do not leak into the daily-curated path's
    ``/var/log/alpha_sniper/daily.log`` destination.

    Parameters
    ----------
    log_path:
        Optional override of the destination path.  When ``None``
        the resolution priority documented above is applied.
    level:
        Minimum log level (default :data:`logging.INFO`).
    add_stream:
        When ``True``, also attach a :class:`logging.StreamHandler`
        for interactive runs.  Production systemd runs leave this
        ``False`` because ``StandardOutput=append:`` already routes
        stdout to the same file.

    Returns
    -------
    logging.Logger
        The configured news-daemon logger.
    """

    resolved = _resolve_news_log_path(log_path)
    logger = get_news_logger()
    logger.setLevel(level)
    # The news-daemon logger MUST NOT propagate to the root logger;
    # the project root logger handles the daily-curated path's
    # journal sink and would otherwise duplicate every news-daemon
    # record into ``/var/log/alpha_sniper/daily.log``.
    logger.propagate = False

    # Detach any handlers a previous ``configure_news_logging`` call
    # (or a stale test fixture) installed so the contract surface
    # stays "single sink".
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    formatter = TruncatingJSONFormatter(max_bytes=MAX_LINE_BYTES)

    # The default sink lives under ``/var/log/alpha_sniper`` which is
    # only writable by root on the VPS.  Failing to bind the file
    # handler is a soft error: tests / dev laptops fall through to
    # the stream-only path so the daemon can still emit records.
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(resolved), encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except (OSError, PermissionError):
        pass

    if add_stream:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    return logger
