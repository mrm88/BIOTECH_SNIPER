"""Universe refresher with deterministic source-fallback (Reading-B).

This module is the orchestration layer that sits on top of
:mod:`biotech_sniper.universe.iwm_importer` and the SEC EDGAR SIC
classifier. Feature ``f-cross-09-source-fallback-determinism``
(VAL-CROSS-035, VAL-CROSS-036) requires the universe refresh to
take a DETERMINISTIC, DOCUMENTED action when one or both upstream
sources are unavailable, so cron operators see the same behaviour
every run for the same inputs.

Documented decision (also encoded in code below)
------------------------------------------------

* **iShares 404 with EDGAR healthy** → ``russell2k_biotech`` rows
  are PRESERVED verbatim (last-good fallback). The refresher
  emits a structured WARNING JSON line
  ``{"event":"ishares_404_using_last_good", ...}`` naming the
  failed source and returns exit-code 0. No re-classification is
  attempted — re-running SIC classification would burn SEC
  fair-access budget and might produce a different snapshot if
  the cik_sic_cache has shifted, which is at odds with the
  "last-good" contract.

* **Both iShares AND EDGAR 404** → behaviour is governed by the
  ``UNIVERSE_FALLBACK_MODE`` env var (or an explicit
  ``fallback_mode`` argument). Two legal values, both
  deterministic across re-runs:

  - ``halt`` (DEFAULT — preferred per the contract): emits a
    structured ERROR ``{"event":"universe_sources_unavailable",
    "action":"halt"}`` and returns exit-code
    :data:`EXIT_BOTH_SOURCES_DOWN` = ``6``. ``russell2k_biotech``
    is NOT modified. Operators must manually intervene; no
    silent degradation.

  - ``use_stale``: emits a structured WARNING
    ``{"event":"universe_sources_unavailable",
    "action":"use_stale", "stale_seconds":N}`` (with the
    wall-clock gap to the most recent ``fetched_at``), preserves
    the prior list, and returns exit-code 0. Use when uptime
    matters more than freshness.

Why halt is the default
-----------------------

* Fail-loud at the source-of-truth boundary forces the operator
  to investigate WHY both upstreams are down (network outage,
  whitelist regression, expired SSL on iShares). A silent
  ``use_stale`` would mask infrastructure breakage.
* The Stage-1 news daemon's scope (``russell2k_biotech ∩
  universe.tier``) degrades gracefully against a stale list (no
  new candidate emissions for newly-listed biotech tickers, but
  no false positives either), so a HALT is operationally safe.
* The default matches the contract's "preferred" specification.

Public API
----------

* :data:`UNIVERSE_FALLBACK_HALT`, :data:`UNIVERSE_FALLBACK_USE_STALE`
  — the canonical mode strings (also the env-var values).
* :data:`EXIT_OK`, :data:`EXIT_BOTH_SOURCES_DOWN` — stable exit
  codes for cron consumers.
* :func:`resolve_fallback_mode` — env-var + explicit-arg parser
  with deterministic fallback to ``halt`` on invalid input.
* :func:`probe_edgar_health` — single-request EDGAR health probe
  used by :func:`refresh_universe`.
* :func:`refresh_universe` — orchestrator entrypoint.
* :class:`RefresherFallbackResult` — structured result dataclass.
* :class:`BothSourcesUnavailable` — typed exception lifted from
  the halt path when ``raise_on_halt=True``.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import requests

from biotech_sniper import db
from biotech_sniper.universe import iwm_importer
from biotech_sniper.universe.iwm_importer import (
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    DEFAULT_USER_AGENT,
    UpstreamUnavailable,
    default_db_path,
)
from biotech_sniper.universe.russell_biotech import (
    ensure_russell2k_biotech_table,
)


__all__ = [
    "UNIVERSE_FALLBACK_HALT",
    "UNIVERSE_FALLBACK_USE_STALE",
    "EXIT_OK",
    "EXIT_BOTH_SOURCES_DOWN",
    "BothSourcesUnavailable",
    "RefresherFallbackResult",
    "resolve_fallback_mode",
    "probe_edgar_health",
    "refresh_universe",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Canonical fallback-mode strings. Mirrored verbatim by the
#: ``UNIVERSE_FALLBACK_MODE`` env var (i.e. the env value is one
#: of these two literals).
UNIVERSE_FALLBACK_HALT: Final[str] = "halt"
UNIVERSE_FALLBACK_USE_STALE: Final[str] = "use_stale"

_LEGAL_FALLBACK_MODES: Final[frozenset[str]] = frozenset(
    {UNIVERSE_FALLBACK_HALT, UNIVERSE_FALLBACK_USE_STALE}
)

#: Documented exit codes for cron / validators.
EXIT_OK: Final[int] = 0
EXIT_BOTH_SOURCES_DOWN: Final[int] = 6

#: SEC EDGAR endpoint used for the health probe. We intentionally
#: pick the smallest published JSON document on ``www.sec.gov``
#: (the CIK→ticker map is large — we only ever ask for the HTTP
#: status, not the body, by short-circuiting on the response
#: status_code). The host is already on the whitelist.
EDGAR_HEALTH_PROBE_URL: Final[str] = (
    "https://www.sec.gov/files/company_tickers_exchange.json"
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BothSourcesUnavailable(RuntimeError):
    """Lifted by :func:`refresh_universe` when both upstream sources
    are unavailable AND the orchestrator's fallback mode is
    ``halt`` AND the caller passed ``raise_on_halt=True``.

    The message names both failures so cron logs can grep the
    cause without parsing JSON. Carries the structured payload
    the orchestrator already logged so callers do not have to
    re-derive it.
    """

    def __init__(self, message: str, *, payload: dict | None = None):
        super().__init__(message)
        self.payload = payload or {}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class RefresherFallbackResult:
    """Structured summary of a refresh-with-fallback cycle.

    The orchestrator returns this object on EVERY run (happy path
    and fallback paths both). Fields are stable to keep the cron
    consumer contract simple — see ``deploy/`` runbooks.

    Field meanings
    --------------
    exit_code:
        ``EXIT_OK`` (0) on happy path, last-good-fallback, or
        ``use_stale``. ``EXIT_BOTH_SOURCES_DOWN`` (6) on the
        ``halt`` path.
    action:
        Stable token describing what the refresher did:

        * ``"refresh_ok"`` — upstream healthy, normal refresh
          completed.
        * ``"ishares_404_using_last_good"`` — iShares 4xx/5xx,
          EDGAR healthy, prior rows preserved.
        * ``"halt"`` — both sources down, ``halt`` mode active.
        * ``"use_stale"`` — both sources down, ``use_stale`` mode
          active.
    fallback_taken:
        ``True`` when any of the three fallback paths fired.
    preserved_row_count:
        Row count of ``russell2k_biotech`` AT EXIT. The
        invariant for both fallback paths is: pre-call count
        equals post-call count (rows preserved).
    stale_seconds:
        Wall-clock gap (seconds) between the most recent
        ``russell2k_biotech.fetched_at`` and now. ``None`` when
        the table is empty. Always populated on the
        ``use_stale`` action.
    """

    exit_code: int
    action: str
    fallback_taken: bool
    preserved_row_count: int = 0
    stale_seconds: float | None = None


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------


def resolve_fallback_mode(explicit: str | None) -> str:
    """Return the fallback mode for this run.

    Precedence:
      1. ``explicit`` argument (when not ``None``).
      2. ``UNIVERSE_FALLBACK_MODE`` env var.
      3. :data:`UNIVERSE_FALLBACK_HALT` (default).

    Unknown / mistyped values fall back to ``halt`` with a
    WARNING log so the operator notices the typo without the
    refresh silently flipping into ``use_stale`` mode.
    """
    if explicit is not None:
        candidate = explicit.strip()
        if candidate not in _LEGAL_FALLBACK_MODES:
            logger.warning(
                json.dumps(
                    {
                        "event": "universe_fallback_mode_invalid",
                        "source": "explicit_kwarg",
                        "value": candidate,
                        "fell_back_to": UNIVERSE_FALLBACK_HALT,
                    }
                )
            )
            return UNIVERSE_FALLBACK_HALT
        return candidate
    raw = os.environ.get("UNIVERSE_FALLBACK_MODE", "").strip()
    if not raw:
        return UNIVERSE_FALLBACK_HALT
    if raw not in _LEGAL_FALLBACK_MODES:
        logger.warning(
            json.dumps(
                {
                    "event": "universe_fallback_mode_invalid",
                    "source": "UNIVERSE_FALLBACK_MODE",
                    "value": raw,
                    "fell_back_to": UNIVERSE_FALLBACK_HALT,
                }
            )
        )
        return UNIVERSE_FALLBACK_HALT
    return raw


# ---------------------------------------------------------------------------
# EDGAR health probe
# ---------------------------------------------------------------------------


def probe_edgar_health(
    *,
    timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    session: requests.Session | None = None,
) -> tuple[bool, int | None]:
    """Single-request EDGAR liveness probe.

    Returns ``(healthy, status_code)``.

    * ``healthy=True`` iff the response status is 2xx.
    * ``healthy=False`` on 4xx, 5xx, or any
      :class:`requests.RequestException` (timeout / DNS / TLS).
    * ``status_code`` is the integer HTTP status when one was
      received; ``None`` when the request raised before any
      response was returned (e.g. ConnectionError).

    The probe is intentionally a single GET with no retry — a
    healthy EDGAR responds in <500 ms; if the FIRST call fails
    we treat the source as unavailable and let the determination
    propagate to the fallback path.
    """
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json,*/*;q=0.5",
    }
    getter = session.get if session is not None else requests.get
    try:
        response = getter(
            EDGAR_HEALTH_PROBE_URL, headers=headers, timeout=timeout
        )
    except requests.RequestException as exc:
        logger.info(
            json.dumps(
                {
                    "event": "edgar_probe_request_exception",
                    "exc_class": exc.__class__.__name__,
                }
            )
        )
        return False, None
    status = getattr(response, "status_code", None)
    try:
        status_int = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_int = None
    healthy = status_int is not None and 200 <= status_int < 300
    return healthy, status_int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _russell2k_row_count(conn: sqlite3.Connection) -> int:
    try:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM russell2k_biotech"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(n)


def _russell2k_stale_seconds(
    conn: sqlite3.Connection, *, now: datetime.datetime
) -> float | None:
    """Return seconds between MAX(fetched_at) and ``now``.

    Returns ``None`` when the table is empty (no prior snapshot)
    so the caller can decide what to log.
    """
    try:
        row = conn.execute(
            "SELECT MAX(fetched_at) FROM russell2k_biotech"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    raw = row[0]
    if not raw:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(
            str(raw).replace("Z", "+00:00")
        )
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    delta = now - parsed
    return max(0.0, delta.total_seconds())


def _try_ishares(
    *,
    db_path: Path,
    http_timeout: float,
    user_agent: str,
    session: requests.Session | None,
    now: datetime.datetime,
) -> tuple[bool, int | None, str | None]:
    """Attempt the iShares fetch via the iwm_importer module.

    Returns ``(ok, status_or_none, exc_class_name_or_none)``.

    * ``ok=True`` when the importer completes without raising
      :class:`UpstreamUnavailable`.
    * ``ok=False`` on any ``UpstreamUnavailable``. We do NOT swallow
      :class:`iwm_importer.IWMSchemaError` — schema drift is a
      contract break, not a transient outage, and should bubble up
      so cron alarms.
    """
    try:
        iwm_importer.import_iwm_holdings(
            db_path=db_path,
            max_age_hours=0,
            http_timeout=http_timeout,
            user_agent=user_agent,
            session=session,
            now=now,
        )
    except UpstreamUnavailable as exc:
        # Best-effort extract the HTTP status code embedded in the
        # exception message. The iwm_importer wraps Akamai 403s,
        # iShares 404s, and 5xx as ``UpstreamUnavailable("iShares
        # returned HTTP 404 for ...")``. We pattern-match the digits
        # for the structured log; missing → ``None`` (still logged).
        status_int: int | None = None
        msg = str(exc)
        for code in (403, 404, 500, 502, 503, 504):
            if f"HTTP {code}" in msg:
                status_int = code
                break
        return False, status_int, exc.__class__.__name__
    return True, None, None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def refresh_universe(
    *,
    db_path: Path | str | None = None,
    fallback_mode: str | None = None,
    http_timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    session: requests.Session | None = None,
    raise_on_halt: bool = False,
    now: datetime.datetime | None = None,
) -> RefresherFallbackResult:
    """Refresh the universe with deterministic source-fallback.

    Parameters
    ----------
    db_path:
        Override the target SQLite path. ``None`` → project
        default.
    fallback_mode:
        Override the ``UNIVERSE_FALLBACK_MODE`` env var. ``None``
        → :func:`resolve_fallback_mode` reads the env (default
        ``halt``).
    http_timeout:
        Per-request HTTP timeout for both the iShares fetch and
        the EDGAR probe.
    user_agent:
        ``User-Agent`` header for outbound requests.
    session:
        Optional :class:`requests.Session` (used by tests).
    raise_on_halt:
        When ``True`` AND the orchestrator's documented action
        is ``halt``, raise :class:`BothSourcesUnavailable` AFTER
        emitting the structured ERROR log. The cron entrypoint
        sets this so a non-zero exit code propagates without the
        caller having to inspect the result.
    now:
        Override the current UTC time (tests).

    Returns
    -------
    RefresherFallbackResult
        Structured run summary.
    """
    target_db = (
        Path(db_path) if db_path is not None else default_db_path()
    )
    mode = resolve_fallback_mode(fallback_mode)
    now = now or _now_utc()

    # Probe iShares first. The iwm_importer carries its own
    # last-good-fallback semantics for the IWM snapshot table —
    # leaving prior IWM rows in place — but the russell2k_biotech
    # table is independent: we have to handle preservation here.
    ishares_ok, ishares_status, ishares_exc = _try_ishares(
        db_path=target_db,
        http_timeout=http_timeout,
        user_agent=user_agent,
        session=session,
        now=now,
    )

    if ishares_ok:
        # Happy path: iShares fetched cleanly. We DO NOT call the
        # russell2k_biotech writer from this orchestrator — that
        # is the cron entrypoint's job (and is exercised by
        # f-m1-03). The orchestrator's contract is purely the
        # SOURCE-FALLBACK decision; the happy path is a no-op
        # passthrough.
        conn = db.connect(target_db)
        try:
            ensure_russell2k_biotech_table(conn)
            count = _russell2k_row_count(conn)
        finally:
            conn.close()
        return RefresherFallbackResult(
            exit_code=EXIT_OK,
            action="refresh_ok",
            fallback_taken=False,
            preserved_row_count=count,
        )

    # iShares failed. Probe EDGAR.
    edgar_healthy, edgar_status = probe_edgar_health(
        timeout=http_timeout,
        user_agent=user_agent,
        session=session,
    )

    conn = db.connect(target_db)
    try:
        ensure_russell2k_biotech_table(conn)
        preserved_count = _russell2k_row_count(conn)
        stale_seconds = _russell2k_stale_seconds(conn, now=now)
    finally:
        conn.close()

    if edgar_healthy:
        # iShares 404 + EDGAR 200: VAL-CROSS-035 — preserve
        # russell2k_biotech, log WARNING, exit 0. We do NOT call
        # the russell_biotech writer here (it would re-classify
        # against EDGAR and overwrite the snapshot, which is at
        # odds with the "last-good preserved" contract).
        log_payload = {
            "event": "ishares_404_using_last_good",
            "source": "ishares",
            "status": ishares_status,
            "ishares_exc_class": ishares_exc,
            "edgar_healthy": True,
            "edgar_status": edgar_status,
            "preserved_row_count": preserved_count,
        }
        logger.warning(json.dumps(log_payload))
        return RefresherFallbackResult(
            exit_code=EXIT_OK,
            action="ishares_404_using_last_good",
            fallback_taken=True,
            preserved_row_count=preserved_count,
            stale_seconds=stale_seconds,
        )

    # Both sources down. Apply the documented fallback mode.
    base_payload: dict = {
        "event": "universe_sources_unavailable",
        "ishares_status": ishares_status,
        "ishares_exc_class": ishares_exc,
        "edgar_status": edgar_status,
        "preserved_row_count": preserved_count,
    }

    if mode == UNIVERSE_FALLBACK_USE_STALE:
        payload = {
            **base_payload,
            "action": "use_stale",
            "stale_seconds": (
                float(stale_seconds) if stale_seconds is not None else 0.0
            ),
            # VAL-CROSS-036: surface ``stale_warning=true`` so log
            # consumers (watchdog, dashboards, alerting) can filter
            # WARNING events on the flag directly without re-deriving
            # it from ``action``.
            "stale_warning": True,
        }
        logger.warning(json.dumps(payload))
        return RefresherFallbackResult(
            exit_code=EXIT_OK,
            action="use_stale",
            fallback_taken=True,
            preserved_row_count=preserved_count,
            stale_seconds=stale_seconds,
        )

    # Default: halt.
    payload = {**base_payload, "action": "halt"}
    logger.error(json.dumps(payload))
    if raise_on_halt:
        raise BothSourcesUnavailable(
            "iShares + EDGAR both unavailable; halting per "
            f"UNIVERSE_FALLBACK_MODE={UNIVERSE_FALLBACK_HALT!r}",
            payload=payload,
        )
    return RefresherFallbackResult(
        exit_code=EXIT_BOTH_SOURCES_DOWN,
        action="halt",
        fallback_taken=True,
        preserved_row_count=preserved_count,
        stale_seconds=stale_seconds,
    )
