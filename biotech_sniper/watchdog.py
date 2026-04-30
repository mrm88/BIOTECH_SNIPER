"""Oneshot health check for the alpha-sniper systemd watchdog timer.

The watchdog inspects ``state/audit_latest.json`` and detects three
classes of pipeline staleness, each producing a structured JSON log
line at WARNING (or ERROR for missing / unreadable audit) level on
``/var/log/alpha_sniper/watchdog.log``:

* ``daily_run_stale``  - ``last_daily_run`` from the audit payload is
  older than 26 hours (or missing entirely).
* ``intraday_run_stale`` - ``last_intraday_run`` is older than 70
  minutes during US equities core market hours
  (Mon-Fri, 13:30-21:00 UTC). Outside that band a stale or missing
  intraday timestamp is healthy by definition.
* ``audit_stale``      - the ``audit_latest.json`` file mtime itself
  is older than 26 hours (the audit cron has stopped writing).
* ``news_service_inactive`` - Reading-B M4 augmentation: the
  long-lived ``alpha-sniper-news.service`` unit is not ``active``
  per ``systemctl is-active``.
* ``news_daemon_heartbeat_stale`` - Reading-B M4 augmentation: the
  ``state/news_daemon_heartbeat.json`` file mtime is older than
  :data:`NEWS_HEARTBEAT_STALE_AFTER` (5 min), indicating the daemon
  loop is wedged even if systemd reports the unit ``active``.

Exit code is ``0`` when every check is healthy and ``1`` when ANY
check is degraded — the latter triggers ``OnFailure=`` /
``Restart=on-failure`` plumbing and surfaces in
``systemctl status alpha-sniper-watchdog.service``.

The watchdog is intentionally lightweight: it never imports the heavy
discovery / scoring / Alpaca pipeline modules. The only subprocess
spawned is a single short-lived ``systemctl is-active``
invocation for the news-service health check (Reading-B M4).

Usage
-----

CLI:

.. code-block:: bash

    python -m biotech_sniper.watchdog                     # default state path
    python -m biotech_sniper.watchdog --audit-path PATH   # explicit override

Library:

.. code-block:: python

    from biotech_sniper.watchdog import check_health, main
    findings = check_health(Path("state/audit_latest.json"))
    rc = main(audit_path=tmp_path / "audit_latest.json")
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from biotech_sniper import logging_setup

__all__ = [
    "DAILY_STALE_AFTER",
    "INTRADAY_STALE_AFTER",
    "AUDIT_STALE_AFTER",
    "NEWS_HEARTBEAT_STALE_AFTER",
    "check_health",
    "check_news_daemon_health",
    "is_market_hours_utc",
    "main",
]

# ---------------------------------------------------------------------------
# Thresholds (tunable in one place — the CLI does not currently accept
# overrides; tests import these constants directly).
# ---------------------------------------------------------------------------

#: Daily systemd unit fires once per day at 06:00 PT. We allow up to
#: 26 hours between runs so a single missed-by-one-hour run does not
#: page; anything older than 26 hours signals the daily timer stopped
#: firing entirely.
DAILY_STALE_AFTER: timedelta = timedelta(hours=26)

#: Intraday unit fires hourly during market hours. Anything older than
#: 70 minutes during market hours signals the intraday timer skipped
#: at least one slot.
INTRADAY_STALE_AFTER: timedelta = timedelta(minutes=70)

#: ``audit_latest.json`` is rewritten on every daily AND intraday run
#: (audit.py is the single state writer). 26h covers the gap between
#: the most recent daily run and the next-day fire window.
AUDIT_STALE_AFTER: timedelta = timedelta(hours=26)

#: Reading-B M4: the ``alpha-sniper-news.service`` daemon writes a
#: heartbeat to ``state/news_daemon_heartbeat.json`` every poll cycle
#: (cadence ≈ 30 s, env-clamped 15-90 s). The watchdog treats a
#: heartbeat older than 5 minutes as evidence the daemon is wedged
#: even when systemd reports the unit ``active`` (e.g. main loop
#: blocked on a network call). Threshold mirrors VAL-M4-021.
NEWS_HEARTBEAT_STALE_AFTER: timedelta = timedelta(minutes=5)

#: Reading-B M4: canonical systemd unit name for the long-lived
#: news-watcher daemon. Centralised so the watchdog augmentation
#: references it from a single place (matched verbatim by VAL-M4-023).
NEWS_SERVICE_UNIT: str = "alpha-sniper-news.service"

# US equities core market hours expressed in UTC. NYSE trades 09:30 -
# 16:00 America/New_York which is 13:30-20:00 UTC during EDT and
# 14:30-21:00 UTC during EST. The validation contract uses the union
# 13:30-21:00 UTC (VAL-M4-040 docstring) so a single comparison covers
# both DST regimes without needing the ``zoneinfo`` runtime.
_MARKET_OPEN_UTC: tuple[int, int] = (13, 30)
_MARKET_CLOSE_UTC: tuple[int, int] = (21, 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso_utc(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp from the audit payload.

    Accepts trailing ``Z`` (legacy ``isoformat()`` output) by mapping
    it to ``+00:00`` before delegating to
    :meth:`datetime.fromisoformat`. Returns ``None`` for missing /
    empty / unparseable values rather than raising — the watchdog
    treats unparseable timestamps as ``stale`` so misconfiguration
    surfaces as a finding rather than an exception.
    """
    if not isinstance(value, str) or not value:
        return None
    candidate = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Naive timestamps are interpreted as UTC; the audit writer
        # always tags ``+00:00`` so this branch is defensive only.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_market_hours_utc(now: datetime) -> bool:
    """Return ``True`` when *now* (UTC) sits inside the contract band.

    The band is Mon-Fri 13:30-21:00 UTC inclusive — chosen to cover
    NYSE 09:30-16:00 ET regardless of DST. Saturday/Sunday always
    return ``False``.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    if now.weekday() >= 5:  # 5 = Sat, 6 = Sun
        return False
    open_dt = now.replace(
        hour=_MARKET_OPEN_UTC[0],
        minute=_MARKET_OPEN_UTC[1],
        second=0,
        microsecond=0,
    )
    close_dt = now.replace(
        hour=_MARKET_CLOSE_UTC[0],
        minute=_MARKET_CLOSE_UTC[1],
        second=0,
        microsecond=0,
    )
    return open_dt <= now <= close_dt


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def check_health(
    audit_path: Path,
    *,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Inspect *audit_path* and return a list of degraded findings.

    Each finding is a JSON-serialisable ``dict`` with at minimum:

    * ``event`` - one of ``daily_run_stale`` / ``intraday_run_stale``
      / ``audit_stale``.
    * ``level`` - log level name (``WARNING`` or ``ERROR``).

    Additional keys (``age_seconds``, ``threshold_seconds``,
    timestamps, ``reason``) are included for operator triage.

    A healthy state returns ``[]``.

    Parameters
    ----------
    audit_path:
        Path to ``audit_latest.json`` (typically
        ``state/audit_latest.json``).
    now:
        Override the wall-clock used for staleness math; defaults to
        ``datetime.now(timezone.utc)``. Tests pass an explicit value
        to exercise market-hours and threshold edge cases.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)

    findings: list[dict[str, Any]] = []

    if not audit_path.is_file():
        findings.append(
            {
                "event": "audit_stale",
                "level": "ERROR",
                "reason": "audit_missing",
                "path": str(audit_path),
            }
        )
        return findings

    try:
        mtime = datetime.fromtimestamp(
            audit_path.stat().st_mtime, tz=timezone.utc
        )
    except OSError as exc:
        findings.append(
            {
                "event": "audit_stale",
                "level": "ERROR",
                "reason": f"stat_failed: {exc!r}",
                "path": str(audit_path),
            }
        )
        return findings

    audit_age = now - mtime
    if audit_age > AUDIT_STALE_AFTER:
        findings.append(
            {
                "event": "audit_stale",
                "level": "WARNING",
                "audit_mtime": mtime.isoformat(),
                "age_seconds": round(audit_age.total_seconds(), 3),
                "threshold_seconds": int(
                    AUDIT_STALE_AFTER.total_seconds()
                ),
                "path": str(audit_path),
            }
        )

    try:
        payload = json.loads(audit_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        findings.append(
            {
                "event": "audit_stale",
                "level": "ERROR",
                "reason": f"unreadable: {exc!r}",
                "path": str(audit_path),
            }
        )
        return findings

    if not isinstance(payload, dict):
        findings.append(
            {
                "event": "audit_stale",
                "level": "ERROR",
                "reason": "payload_not_object",
                "path": str(audit_path),
            }
        )
        return findings

    # ── daily_run_stale ────────────────────────────────────────────
    last_daily_raw = payload.get("last_daily_run")
    last_daily = _parse_iso_utc(last_daily_raw)
    if last_daily is None:
        findings.append(
            {
                "event": "daily_run_stale",
                "level": "WARNING",
                "reason": "missing_or_unparseable",
                "value": last_daily_raw,
                "threshold_seconds": int(
                    DAILY_STALE_AFTER.total_seconds()
                ),
            }
        )
    else:
        daily_age = now - last_daily
        if daily_age > DAILY_STALE_AFTER:
            findings.append(
                {
                    "event": "daily_run_stale",
                    "level": "WARNING",
                    "last_daily_run": last_daily.isoformat(),
                    "age_seconds": round(daily_age.total_seconds(), 3),
                    "threshold_seconds": int(
                        DAILY_STALE_AFTER.total_seconds()
                    ),
                }
            )

    # ── intraday_run_stale (only during market hours) ──────────────
    if is_market_hours_utc(now):
        last_intraday_raw = payload.get("last_intraday_run")
        last_intraday = _parse_iso_utc(last_intraday_raw)
        if last_intraday is None:
            findings.append(
                {
                    "event": "intraday_run_stale",
                    "level": "WARNING",
                    "reason": "missing_or_unparseable",
                    "value": last_intraday_raw,
                    "threshold_seconds": int(
                        INTRADAY_STALE_AFTER.total_seconds()
                    ),
                }
            )
        else:
            intraday_age = now - last_intraday
            if intraday_age > INTRADAY_STALE_AFTER:
                findings.append(
                    {
                        "event": "intraday_run_stale",
                        "level": "WARNING",
                        "last_intraday_run": last_intraday.isoformat(),
                        "age_seconds": round(
                            intraday_age.total_seconds(), 3
                        ),
                        "threshold_seconds": int(
                            INTRADAY_STALE_AFTER.total_seconds()
                        ),
                    }
                )

    return findings


# ---------------------------------------------------------------------------
# Reading-B M4 augmentation: news daemon health (VAL-M4-021..024)
# ---------------------------------------------------------------------------


def _systemctl_is_active(unit: str = NEWS_SERVICE_UNIT) -> bool:
    """Return True iff ``systemctl is-active <unit>`` reports ``active``."""
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "active"


def check_news_daemon_health(
    heartbeat_path: Path,
    *,
    now: Optional[datetime] = None,
    is_active_fn: Optional[Callable[[], bool]] = None,
) -> list[dict[str, Any]]:
    """Inspect alpha-sniper-news.service + heartbeat. Healthy returns []."""
    if now is None:
        now = datetime.now(tz=timezone.utc)
    if is_active_fn is None:
        is_active_fn = _systemctl_is_active
    threshold = int(NEWS_HEARTBEAT_STALE_AFTER.total_seconds())
    findings: list[dict[str, Any]] = []
    if not is_active_fn():
        findings.append({
            "event": "news_service_inactive",
            "legacy_event": "news_daemon_inactive",
            "level": "ERROR", "service": NEWS_SERVICE_UNIT,
        })
    if not heartbeat_path.is_file():
        findings.append({
            "event": "news_daemon_heartbeat_stale", "level": "WARNING",
            "reason": "heartbeat_missing", "path": str(heartbeat_path),
            "threshold_seconds": threshold,
        })
        return findings
    try:
        mtime = datetime.fromtimestamp(
            heartbeat_path.stat().st_mtime, tz=timezone.utc
        )
    except OSError as exc:
        findings.append({
            "event": "news_daemon_heartbeat_stale", "level": "ERROR",
            "reason": f"stat_failed: {exc!r}", "path": str(heartbeat_path),
        })
        return findings
    age = now - mtime
    if age > NEWS_HEARTBEAT_STALE_AFTER:
        findings.append({
            "event": "news_daemon_heartbeat_stale", "level": "WARNING",
            "heartbeat_mtime": mtime.isoformat(),
            "age_seconds": round(age.total_seconds(), 3),
            "threshold_seconds": threshold, "path": str(heartbeat_path),
        })
    return findings


def _default_heartbeat_path() -> Path:
    from biotech_sniper.paths import STATE_DIR
    return STATE_DIR / "news_daemon_heartbeat.json"


# ---------------------------------------------------------------------------
# CLI / main
# ---------------------------------------------------------------------------


def _emit(log, findings: Iterable[dict[str, Any]]) -> None:
    """Emit each finding as one structured JSON log line."""
    for finding in findings:
        level_name = str(finding.get("level", "WARNING")).upper()
        # The ``level`` key is purely a routing hint; we drop it from
        # the extras so the JSONFormatter does not duplicate it
        # alongside the LogRecord-derived ``level`` field.
        extras = {k: v for k, v in finding.items() if k != "level"}
        log_method = {
            "DEBUG": log.debug,
            "INFO": log.info,
            "WARN": log.warning,
            "WARNING": log.warning,
            "ERROR": log.error,
            "CRITICAL": log.critical,
        }.get(level_name, log.warning)
        log_method(extras["event"], extra=extras)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.watchdog",
        description=(
            "Oneshot health check for the alpha-sniper pipeline. "
            "Detects stale daily/intraday runs and stale audit JSON. "
            "Exits non-zero when any check is degraded."
        ),
    )
    parser.add_argument(
        "--audit-path",
        type=Path,
        default=None,
        help=(
            "Override the path to audit_latest.json. Defaults to "
            "<BIOTECH_SNIPER_HOME>/state/audit_latest.json."
        ),
    )
    return parser


def _default_audit_path() -> Path:
    """Return the canonical audit_latest.json location.

    Resolved at call time so test patches of
    ``biotech_sniper.paths.STATE_DIR`` (or the ``BIOTECH_SNIPER_HOME``
    env var, when paths is reloaded) take effect.
    """
    from biotech_sniper.paths import STATE_DIR

    return STATE_DIR / "audit_latest.json"


def main(
    argv: Optional[list[str]] = None,
    *,
    audit_path: Optional[Path] = None,
    heartbeat_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    is_active_fn: Optional[Callable[[], bool]] = None,
) -> int:
    """Run the watchdog once and return the process exit code.

    Parameters
    ----------
    argv:
        Optional pre-parsed CLI arguments (defaults to ``sys.argv[1:]``
        when invoked as ``python -m biotech_sniper.watchdog``).
    audit_path:
        Test hook — overrides the resolved default path entirely.
    now:
        Test hook — pin the wall-clock used for staleness math.

    Returns
    -------
    int
        ``0`` on healthy state, ``1`` when any check produced a
        finding.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if audit_path is None:
        audit_path = args.audit_path or _default_audit_path()
    if heartbeat_path is None:
        heartbeat_path = _default_heartbeat_path()

    logging_setup.configure(log_name="watchdog")
    log = logging_setup.get_logger(__name__)

    findings = check_health(audit_path, now=now)
    findings.extend(
        check_news_daemon_health(
            heartbeat_path, now=now, is_active_fn=is_active_fn
        )
    )

    if not findings:
        log.info(
            "watchdog_ok",
            extra={
                "event": "watchdog_ok",
                "audit_path": str(audit_path),
                "heartbeat_path": str(heartbeat_path),
            },
        )
        return 0

    _emit(log, findings)
    log.info(
        "watchdog_degraded",
        extra={
            "event": "watchdog_degraded",
            "audit_path": str(audit_path),
            "heartbeat_path": str(heartbeat_path),
            "finding_count": len(findings),
            "findings": [f["event"] for f in findings],
        },
    )
    return 1


if __name__ == "__main__":  # pragma: no cover - thin runtime entrypoint
    raise SystemExit(main())
