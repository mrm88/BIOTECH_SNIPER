"""``candidate_events`` writer with ``INSERT OR IGNORE`` dedup.

The emitter is the **only** writer of ``candidate_events`` rows.
Dedup semantics rely entirely on the ``candidate_events.dedup_key``
``UNIQUE`` constraint provisioned by migration v9 → v10
(``010_reading_b_foundations``).  Module-level in-memory dedup
cache state is **forbidden**: that would silently degrade dedup
across daemon restarts (SIGKILL, OOM, systemd ``Restart=on-failure``)
because in-memory state is wiped while the database persists.

Skeleton (f-m2-01)
-------------------

Real ``INSERT OR IGNORE`` + transactional batching + cross-restart
recovery land in f-m2-06 and are asserted by VAL-M2-016..025.  This
file documents the public surface.

dedup_key formula
------------------

::

    dedup_key = sha256(
        f"{ticker}\\x1f{news_event_id}\\x1f{matched_keywords_sorted}"
    ).hexdigest()

The ``\\x1f`` ASCII Unit Separator is the field delimiter — pipes
are NOT injection-safe for an unbounded keyword vocab (a future
keyword containing ``|`` would collide trivially).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple

__all__ = [
    "FIELD_SEPARATOR",
    "CandidateEvent",
    "compute_dedup_key",
    "write_candidate",
]

#: ASCII Unit Separator (0x1F).  Used to delimit fields in the
#: ``dedup_key`` SHA-256 input.  Chosen because it cannot appear in
#: any reasonable ticker / keyword vocabulary, so an attacker cannot
#: craft a colliding keyword set via field-injection.
FIELD_SEPARATOR: str = "\x1f"


@dataclass(frozen=True)
class CandidateEvent:
    """In-memory shape of a ``candidate_events`` row.

    Mirrors the table schema from migration v10:

    * ``id`` is assigned by SQLite ``AUTOINCREMENT``; ``None`` until
      the row is committed.
    * ``ticker`` is upper-case canonical (enforced by news_events
      upstream).
    * ``source_news_event_id`` is the ``news_events.id`` foreign key.
    * ``matched_keywords`` is the sorted, comma-joined string used in
      the ``dedup_key`` hash.
    * ``calendar_match`` is the trial_calendar lookup result, or
      ``None`` when no upcoming catalyst was found (LEFT-JOIN miss
      is NOT an error per VAL-M2-022).
    * ``emitted_at`` is the ISO-8601 UTC timestamp of the write.
    * ``dedup_key`` is the SHA-256 hex digest computed by
      :func:`compute_dedup_key`.
    """

    ticker: str
    source_news_event_id: int
    matched_keywords: str
    emitted_at: str
    dedup_key: str
    calendar_match: Optional[str] = None
    id: Optional[int] = None


def compute_dedup_key(
    ticker: str,
    news_event_id: int,
    matched_keywords: Sequence[str],
) -> str:
    """Compute ``dedup_key`` for a ``(ticker, news_event_id, kw)`` triple.

    The matched-keywords sequence is **sorted, deduped, and
    comma-joined** before hashing so a row that matches the same
    vocab in a different order produces the same key.
    """

    deduped_sorted = ",".join(sorted({kw for kw in matched_keywords if kw}))
    payload = (
        f"{ticker}{FIELD_SEPARATOR}"
        f"{news_event_id}{FIELD_SEPARATOR}"
        f"{deduped_sorted}"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_candidate(
    db_path: str,
    candidate: CandidateEvent,
) -> bool:
    """Insert a single ``candidate_events`` row with dedup.

    Skeleton stub (f-m2-01): no DB write is performed.  Returns
    ``False`` to indicate "no row inserted".  Real implementation
    lands in f-m2-06 with batched ``executemany`` inside a single
    ``BEGIN`` / ``COMMIT`` block (per VAL-M2-024).
    """

    return False


def write_candidates(
    db_path: str,
    candidates: Iterable[CandidateEvent],
) -> Tuple[int, int]:
    """Batch-insert candidates inside one transaction.

    Returns
    -------
    tuple[int, int]
        ``(attempted, inserted)`` — ``inserted`` excludes
        ``INSERT OR IGNORE`` swallowed dedups.  Skeleton stub returns
        ``(0, 0)``.
    """

    return (0, 0)
