"""Structured JSON logging helpers — ≤ 4 KB per line.

Wraps :mod:`biotech_sniper.logging_setup` (the project-wide JSON
formatter) with a per-line size cap.  Oversize string fields are
truncated with a ``"_truncated":true`` annotation and the
``"original_length"`` of the offending field — no record is dropped,
no record is silently corrupted.

Skeleton (f-m2-01)
-------------------

Real truncation logic lands in f-m2-08 and is asserted by
VAL-M2-031..033.  This file documents the public surface.

Why a dedicated module?
-----------------------

The news daemon writes high-cardinality payloads (per-headline log
records that can include the headline body, RSS source URL, and
matched-keyword vocab).  A 4 KB per-line cap is a hard contract
from the systemd unit / logrotate config — exceeding it risks
journald rejection and broken JSON parsing in downstream tooling
(``jq``, the CLI report).  Centralising the truncation here keeps
the per-call sites DRY.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

__all__ = [
    "MAX_LINE_BYTES",
    "TRUNCATION_MARKER",
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
TRUNCATION_MARKER: str = "…[truncated]"


def get_news_logger(name: str = "biotech_sniper.news_daemon") -> logging.Logger:
    """Return the news-daemon logger.

    Defers to :mod:`biotech_sniper.logging_setup` so the JSON
    formatter and secret-redaction hook are inherited from the
    root logger.  No ``configure()`` call is performed here —
    the entrypoint (``poll_loop.main``) is responsible for
    binding the file destination via
    :func:`biotech_sniper.logging_setup.configure`.
    """

    return logging.getLogger(name)


def truncate_field(
    value: Any,
    max_bytes: int = MAX_LINE_BYTES,
) -> tuple[Any, bool, Optional[int]]:
    """Truncate ``value`` to ``max_bytes`` UTF-8 bytes.

    Skeleton implementation (f-m2-01): returns ``value`` unchanged
    with ``truncated=False`` and ``original_length=None``.  Real
    UTF-8-safe truncation lands in f-m2-08.  The signature is
    locked here so callers in sibling modules can import it now.
    """

    return value, False, None
