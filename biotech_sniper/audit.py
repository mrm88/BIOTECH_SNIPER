#!/usr/bin/env python3
import logging
import os
import sys, json, datetime, time, traceback, tempfile
from pathlib import Path
from typing import Any, Callable, Optional

# f-m4-02a: ``requests`` is the heaviest top-level dependency in this
# module (~200 ms cold import) and is only needed inside the live
# probe functions and the legacy ``__main__`` script. Importing it
# lazily keeps ``from biotech_sniper.audit import write_audit_latest``
# under the 100 ms hermetic-import budget required by the f-m4-02a
# contract.

# f-m4-02 / f-m4-02a: route module-level audit output through the
# project's structured JSON logger. Module-level ``log =
# logging.getLogger(__name__)`` is hermetic (no handler
# installation on import); ``configure()`` is invoked only inside
# ``if __name__ == '__main__':`` so importing
# ``biotech_sniper.audit`` does NOT install handlers, open files,
# or perform any network calls.
from biotech_sniper import logging_setup  # noqa: F401 — module side-effect free
from biotech_sniper.logging_setup import configure, get_logger  # noqa: F401

log = logging.getLogger(__name__)


def __getattr__(name: str):  # noqa: D401 — module-level lazy attribute loader
    """Lazy-load heavy dependencies on first attribute access.

    f-m4-02a: ``requests`` weighs ~200 ms on cold import. The
    f-m4-02a contract requires ``from biotech_sniper.audit import
    write_audit_latest`` to return in well under 100 ms with zero
    network calls. We satisfy that by deferring ``import requests``
    to the first time anything (probe function, test patch via
    ``biotech_sniper.audit.requests.get``, or the legacy ``__main__``
    body) actually references the name.
    """
    if name == "requests":
        import requests as _requests

        globals()["requests"] = _requests
        return _requests
    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r}"
    )


# ---------------------------------------------------------------------------
# f-m4-03 — M4 audit contract helpers (importable, no network at import time).
# ---------------------------------------------------------------------------
#
# The legacy module-level audit script below performs many ad-hoc network
# probes and writes ``state/audit_latest.json``. The f-m4-03 contract
# augments that file with the following required top-level keys:
#
# * ``generated_at`` (ISO-8601 UTC timestamp)
# * ``sources`` — map keyed by ``ct.gov``, ``sec_edgar``, ``news_rss``,
#   ``alpaca_paper``, ``xai``, ``anthropic``, ``gemini``. Each value
#   carries ``status`` (one of ``ok`` / ``degraded`` / ``error``),
#   ``last_checked`` (ISO-8601 UTC), and ``latency_ms`` (int ≥ 0).
# * ``last_daily_run`` — ISO-8601 UTC of the most recent daily run,
#   read from db state (``scoring_cache.created_at``).
# * ``last_intraday_run`` — ISO-8601 UTC of the most recent intraday
#   run, read from db state (``execution_events.event_at``).
# * ``db_size_bytes`` — size of ``data/alpha_sniper.db`` on disk.
# * ``paper_account_equity`` — current Alpaca paper account equity
#   (float, ``None`` when credentials are missing or unreachable).
#
# All probe helpers are pure functions that capture timing internally
# and never raise to the caller — failure modes surface as
# ``{"status": "error", ...}`` so the audit JSON always lands.
# ---------------------------------------------------------------------------


_M4_SOURCE_KEYS: tuple[str, ...] = (
    "ct.gov",
    "sec_edgar",
    "news_rss",
    "alpaca_paper",
    "xai",
    "anthropic",
    "gemini",
)


# f-m4-12: VAL-M4-035 requires every entry in ``audit_latest.json``'s
# ``sources`` map to expose a ``status`` field whose value is one of
# the canonical enum below. Legacy entries written by the
# module-level audit script use the older ``{'ok': bool, ...}`` shape
# (``clinicaltrials_gov``, ``alpha_sniper_db``, ``scoring_cache``,
# ``llm_cost_ledger``, ``news_events``, etc.) and are folded into
# the merged ``sources`` map by :func:`write_audit_latest`. We
# normalize those entries below so the audit file always satisfies
# the contract.
_M4_STATUS_ENUM: frozenset[str] = frozenset(
    {"ok", "degraded", "error", "unknown", "missing_credential"}
)


def _normalize_legacy_source_entry(entry: Any) -> dict[str, Any]:
    """Return ``entry`` augmented with a contract-conformant ``status`` key.

    Mapping rules (preserves all other keys verbatim):

    * If ``entry`` is not a dict, return ``{'status': 'unknown', 'value': entry}``
      so the merged ``sources`` map stays a dict-of-dicts.
    * If ``entry`` already carries ``status`` with a value in
      :data:`_M4_STATUS_ENUM`, return it untouched.
    * Else if ``entry`` has ``ok == True`` → ``status='ok'``.
    * Else if ``entry`` has ``ok == False`` → ``status='error'``.
    * Otherwise → ``status='unknown'``.
    """
    if not isinstance(entry, dict):
        return {"status": "unknown", "value": entry}

    existing_status = entry.get("status")
    if isinstance(existing_status, str) and existing_status in _M4_STATUS_ENUM:
        return entry

    normalized = dict(entry)
    if "ok" in normalized:
        normalized["status"] = "ok" if normalized.get("ok") else "error"
    else:
        normalized["status"] = "unknown"
    return normalized


def _iso_utc_now() -> str:
    """Return the current time as an ISO-8601 UTC string with ``Z`` suffix."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _classify_http_status(status: int) -> str:
    """Map an HTTP status code to the M4 ``ok`` / ``degraded`` / ``error`` enum.

    * 2xx → ``ok``
    * 3xx → ``degraded`` (redirects are reachable but unexpected here)
    * 4xx → ``degraded`` (auth failures count as reachable but
      not-fully-functional; mark as degraded so operators investigate
      without paging on a public-API auth-only endpoint).
    * 5xx → ``error``
    * everything else → ``error``
    """
    if 200 <= status < 300:
        return "ok"
    if 300 <= status < 400:
        return "degraded"
    if 400 <= status < 500:
        return "degraded"
    return "error"


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write ``data`` as JSON to ``path`` atomically.

    Creates a temp file in the same directory, ``json.dump``s the
    payload there, calls ``os.replace`` to swap it into place. On any
    failure the temp file is cleaned up and the destination is
    untouched (so a partially-written audit JSON is never observable
    by readers).

    Parameters
    ----------
    path:
        Destination file path. Parent directory is created if missing.
    data:
        Any JSON-serializable Python object.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp_path), str(path))
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _probe_with_timing(
    fn: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Wrap a probe callable with timing + uniform error handling.

    The probe callable returns a dict carrying at minimum a ``status``
    key (``ok`` / ``degraded`` / ``error``); additional context (HTTP
    status, error string, etc.) is preserved verbatim. ``last_checked``
    and ``latency_ms`` are merged into the returned dict before it is
    handed back. Any exception thrown by the probe is converted to
    ``{"status": "error", "error": repr(exc)}`` so callers never need
    to wrap the call in a try/except themselves.
    """
    started = time.monotonic()
    last_checked = _iso_utc_now()
    try:
        result = fn() or {}
        if not isinstance(result, dict):
            result = {"status": "error", "error": f"non-dict probe result: {result!r}"}
    except Exception as exc:  # noqa: BLE001 — probe must not crash audit
        result = {"status": "error", "error": repr(exc)}
    elapsed_ms = int((time.monotonic() - started) * 1000)
    result.setdefault("status", "error")
    result.setdefault("last_checked", last_checked)
    result.setdefault("latency_ms", elapsed_ms)
    return result


def _probe_ctgov() -> dict[str, Any]:
    """Lightweight reachability probe for ClinicalTrials.gov v2 API."""
    # ``requests`` is supplied by the module-level ``__getattr__``
    # lazy-loader on first access. Tests that patch
    # ``biotech_sniper.audit.requests.get`` work transparently.
    r = requests.get(
        "https://clinicaltrials.gov/api/v2/studies?pageSize=1",
        timeout=10,
    )
    return {
        "status": _classify_http_status(r.status_code),
        "http_status": r.status_code,
    }


def _probe_sec_edgar() -> dict[str, Any]:
    """Lightweight reachability probe for SEC EDGAR (8-K RSS feed)."""
    r = requests.get(
        "https://www.sec.gov/cgi-bin/browse-edgar?"
        "action=getcurrent&type=8-K&dateb=&owner=include&count=1&output=atom",
        headers={"User-Agent": "BioCatalystBot research@mantisvc.com"},
        timeout=10,
    )
    return {
        "status": _classify_http_status(r.status_code),
        "http_status": r.status_code,
    }


def _probe_news_rss() -> dict[str, Any]:
    """Lightweight reachability probe for the canonical FDA press-releases RSS."""
    r = requests.get(
        "https://www.fda.gov/about-fda/contact-fda/stay-informed/"
        "rss-feeds/press-releases/rss.xml",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=10,
    )
    return {
        "status": _classify_http_status(r.status_code),
        "http_status": r.status_code,
    }


def _probe_alpaca_paper() -> dict[str, Any]:
    """Reachability + auth probe for the Alpaca paper trading API.

    Builds an :class:`AlpacaClient` and calls ``get_account``. On
    success the snapshot's ``equity`` is folded into the result so a
    single round-trip serves both the ``alpaca_paper`` source status
    AND the top-level ``paper_account_equity`` field.

    Failure modes:
    * Missing credentials → ``status='degraded'``, ``reason='credentials_missing'``.
    * Auth error (HTTP 401/403) → ``status='degraded'``.
    * Transport error → ``status='error'``.
    """
    try:
        from biotech_sniper.alpaca_client import (
            AlpacaAuthError,
            AlpacaClient,
            AlpacaTransportError,
        )
    except Exception as exc:  # pragma: no cover — alpaca-py should always import
        return {"status": "error", "error": f"import_failed: {exc!r}"}

    try:
        client = AlpacaClient()
    except AlpacaAuthError:
        return {
            "status": "degraded",
            "reason": "credentials_missing",
            "equity": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": repr(exc), "equity": None}

    try:
        account = client.get_account()
    except AlpacaAuthError as exc:
        return {
            "status": "degraded",
            "reason": "auth_failed",
            "error": repr(exc),
            "equity": None,
        }
    except AlpacaTransportError as exc:
        return {"status": "error", "error": repr(exc), "equity": None}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": repr(exc), "equity": None}

    equity = account.get("equity")
    try:
        equity_f = float(equity) if equity is not None else None
    except (TypeError, ValueError):
        equity_f = None
    return {
        "status": "ok",
        "equity": equity_f,
        "currency": account.get("currency", "USD"),
    }


def _probe_xai() -> dict[str, Any]:
    """Lightweight reachability + auth probe for the xAI / Grok API.

    Hits ``GET /v1/api-key`` on ``api.x.ai``. Without a key configured
    we return ``status='degraded'`` (``reason='api_key_missing'``)
    rather than fabricating a request — the validator contract
    requires the field present on every line and degraded reads as
    "endpoint reachable but not exercised for this run".
    """
    from biotech_sniper.config import get_xai_api_key

    api_key = get_xai_api_key()
    if not api_key:
        return {"status": "degraded", "reason": "api_key_missing"}
    r = requests.get(
        "https://api.x.ai/v1/api-key",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )
    return {
        "status": _classify_http_status(r.status_code),
        "http_status": r.status_code,
    }


def _probe_anthropic() -> dict[str, Any]:
    """Lightweight reachability + auth probe for the Anthropic API."""
    from biotech_sniper.config import get_anthropic_api_key

    api_key = get_anthropic_api_key()
    if not api_key:
        return {"status": "degraded", "reason": "api_key_missing"}
    # GET /v1/models lists available models without consuming
    # generation budget; perfect for a reachability ping.
    r = requests.get(
        "https://api.anthropic.com/v1/models",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        timeout=10,
    )
    return {
        "status": _classify_http_status(r.status_code),
        "http_status": r.status_code,
    }


def _probe_gemini() -> dict[str, Any]:
    """Lightweight reachability + auth probe for the Gemini API."""
    from biotech_sniper.config import get_gemini_api_key

    api_key = get_gemini_api_key()
    if not api_key:
        return {"status": "degraded", "reason": "api_key_missing"}
    r = requests.get(
        f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
        timeout=10,
    )
    return {
        "status": _classify_http_status(r.status_code),
        "http_status": r.status_code,
    }


# Probe registry — preserved as a tuple so the order in which sources
# appear in the JSON is deterministic. Tests can override individual
# entries via ``monkeypatch`` on the module-level callables above.
_M4_PROBES: tuple[tuple[str, Callable[[], dict[str, Any]]], ...] = (
    ("ct.gov", _probe_ctgov),
    ("sec_edgar", _probe_sec_edgar),
    ("news_rss", _probe_news_rss),
    ("alpaca_paper", _probe_alpaca_paper),
    ("xai", _probe_xai),
    ("anthropic", _probe_anthropic),
    ("gemini", _probe_gemini),
)


def _get_db_size_bytes(db_path: Path) -> int:
    """Return the on-disk size of the SQLite db (or ``0`` when missing)."""
    try:
        return int(db_path.stat().st_size)
    except (FileNotFoundError, OSError):
        return 0


def _query_max_timestamp(
    db_path: Path, table: str, column: str
) -> Optional[str]:
    """Return ``MAX(<column>)`` from ``<table>`` or ``None`` if unavailable.

    Returns ``None`` for any of:
    * db file does not exist,
    * table does not exist,
    * table is empty,
    * SQLite raises (corrupt db / locked / permission error).
    """
    if not db_path.exists():
        return None
    try:
        import sqlite3 as _sqlite3

        conn = _sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if row is None:
                return None
            row = conn.execute(
                f"SELECT MAX({column}) FROM {table}"  # noqa: S608 — table whitelisted
            ).fetchone()
            if row is None or row[0] is None:
                return None
            return str(row[0])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — never crash the audit on db issues
        return None


def _get_last_daily_run(db_path: Path) -> Optional[str]:
    """Return ISO-8601 UTC for the most recent daily run.

    The daily systemd unit (M4) writes ``scoring_cache`` rows per
    candidate ticker, so ``MAX(scoring_cache.created_at)`` is the
    canonical "last daily run completed" timestamp.
    """
    return _query_max_timestamp(db_path, "scoring_cache", "created_at")


def _get_last_intraday_run(db_path: Path) -> Optional[str]:
    """Return ISO-8601 UTC for the most recent intraday run.

    The intraday systemd unit (M4) writes ``execution_events`` rows
    when polling Alpaca order state, so ``MAX(execution_events.event_at)``
    is the canonical "last intraday tick completed" timestamp.
    """
    return _query_max_timestamp(db_path, "execution_events", "event_at")


def build_m4_audit_payload(
    *, db_path: Optional[Path] = None
) -> dict[str, Any]:
    """Build the f-m4-03 audit JSON payload.

    Runs every probe in :data:`_M4_PROBES` (capturing status / latency
    individually), reads db-state for ``last_daily_run`` /
    ``last_intraday_run`` / ``db_size_bytes``, and extracts
    ``paper_account_equity`` from the ``alpaca_paper`` probe's result.

    The returned dict is the M4 contract surface — callers merge it
    into the existing ``audit_latest.json`` payload before writing.
    """
    from biotech_sniper.paths import DATA_DIR

    if db_path is None:
        db_path = DATA_DIR / "alpha_sniper.db"

    sources_payload: dict[str, dict[str, Any]] = {}
    for name, probe in _M4_PROBES:
        sources_payload[name] = _probe_with_timing(probe)

    paper_equity = sources_payload["alpaca_paper"].get("equity")
    # ``equity`` in degraded probes may be ``None`` so leave it as-is
    # (the contract allows ``None`` when credentials are missing).
    if isinstance(paper_equity, (int, float)):
        paper_equity_value: Any = float(paper_equity)
    else:
        paper_equity_value = None

    return {
        "generated_at": _iso_utc_now(),
        "sources": sources_payload,
        "last_daily_run": _get_last_daily_run(db_path),
        "last_intraday_run": _get_last_intraday_run(db_path),
        "db_size_bytes": _get_db_size_bytes(db_path),
        "paper_account_equity": paper_equity_value,
    }


def write_audit_latest(
    audit_path: Path,
    *,
    extra: Optional[dict[str, Any]] = None,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Build the M4 payload and write it atomically to ``audit_path``.

    Existing keys in any pre-existing ``audit_latest.json`` (e.g. the
    legacy module-level probes' richer source data, or
    ``news_ingestion`` written by the daily news pipeline) are
    preserved unless overwritten by an M4-contract key.

    Parameters
    ----------
    audit_path:
        Destination file (typically ``state/audit_latest.json``).
    extra:
        Optional additional top-level keys to merge into the payload
        BEFORE writing. The legacy script uses this to fold in
        ``as_of_date``, ``failures``, ``warnings`` and the rich
        per-source diagnostics.
    db_path:
        Optional override of the SQLite database location. Defaults
        to ``DATA_DIR/alpha_sniper.db``.

    Returns
    -------
    dict
        The full payload that was written (useful for tests and
        callers that want to log a summary).
    """
    audit_path = Path(audit_path)

    existing: dict[str, Any] = {}
    if audit_path.is_file():
        try:
            existing = json.loads(audit_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}

    payload = dict(existing)
    if extra:
        payload.update(extra)
    m4 = build_m4_audit_payload(db_path=db_path)

    # The M4 contract owns ``sources`` (it must contain the seven
    # canonical keys with the status/last_checked/latency_ms shape).
    # If a legacy ``sources`` map exists in the on-disk file, fold its
    # extra entries in WITHOUT overwriting the M4 ones — that keeps
    # rich per-source diagnostics for operators while still
    # satisfying the contract.
    #
    # f-m4-12: VAL-M4-035 requires every value in ``sources`` to carry a
    # ``status`` key drawn from the canonical enum. Legacy entries use
    # ``{'ok': bool, ...}`` instead, so we normalize them through
    # :func:`_normalize_legacy_source_entry` while preserving all
    # other keys (``ok``, ``error``, ``http_status``, count fields…).
    legacy_sources = payload.get("sources")
    merged_sources = dict(m4["sources"])
    if isinstance(legacy_sources, dict):
        for k, v in legacy_sources.items():
            if k not in merged_sources:
                merged_sources[k] = _normalize_legacy_source_entry(v)
    m4["sources"] = merged_sources

    payload.update(m4)
    _atomic_write_json(audit_path, payload)
    return payload


if __name__ == '__main__':
    # f-m4-02a: configure the structured JSON logger before invoking
    # any of the legacy probes so production runs (``python -m
    # biotech_sniper.audit`` from systemd) emit ts/level/event/module
    # JSON lines into ``/var/log/alpha_sniper/audit.log``.
    configure(log_name='audit')

    # f-m4-02a: the module-level ``import requests`` was deferred
    # (replaced by a ``__getattr__`` lazy loader) to keep
    # ``import biotech_sniper.audit`` under the 100 ms hermetic
    # budget. The legacy probe body below uses ``requests.get`` /
    # ``requests.post`` as bare names; importing here at the top of
    # the ``__main__`` block binds the name into module globals so
    # those references resolve normally during the CLI run.
    import requests  # noqa: F401 — imported for legacy probe body

    # ---------------------------------------------------------------------------
    # Legacy module-level audit script (kept verbatim — runs on
    # ``python -m biotech_sniper.audit``). The block at the bottom now
    # delegates the actual JSON write to :func:`write_audit_latest` so the
    # M4 contract fields land alongside the existing rich source data.
    # ---------------------------------------------------------------------------

    # Load .env before paths.py reads BIOTECH_SNIPER_HOME, so audit.py invoked
    # directly via `python -m biotech_sniper.audit` (without sourcing .env in
    # the shell) writes its JSON to <project>/state/, not <package>/state/.
    try:  # pragma: no cover - best-effort; dotenv is a hard dep but imports must not crash
        from pathlib import Path as _PathForEnv
        from dotenv import load_dotenv as _load_dotenv  # type: ignore[import-not-found]
        _here = _PathForEnv(__file__).resolve().parent
        for _cand in (_here.parent / '.env', _here.parent.parent / '.env'):
            if _cand.is_file():
                _load_dotenv(dotenv_path=str(_cand), override=False)
                break
    except Exception:
        pass

    from biotech_sniper.paths import BASE_DIR, DATA_DIR

    # NOTE: f-m2-07 removed the legacy sandbox-style sys.path mutations
    # that used to live here (they referenced the bare ``intelligence`` and
    # ``sectors`` directories so legacy ``from intelligence.X`` imports
    # would resolve).  The legacy submodules are now imported via their
    # proper ``biotech_sniper.<sub>`` package paths inside try/except
    # blocks below; failed probes are downgraded to warnings so a broken
    # legacy module never crashes the audit run.
    #
    # New audit data sources land in ``data/alpha_sniper.db`` (M2 SQLite
    # tables: ``scoring_cache``, ``llm_cost_ledger``, ``plays``,
    # ``performance_ledger``, ``discovery_state``, and the future
    # ``news_events`` table introduced by M2 universe + news ingest).

    failures = []
    warnings = []
    sources: dict = {}
    today = datetime.date.today().isoformat()
    log.info(f'FULL RUNTIME AUDIT — {today}')
    log.info('='*60)

    # ── 1. ClinicalTrials.gov ───────────────────────────────────
    log.info('\n[1] ClinicalTrials.gov API')
    try:
        r = requests.get(
            'https://clinicaltrials.gov/api/v2/studies?filter.advanced=AREA[Phase]PHASE3+AND+AREA[OverallStatus]ACTIVE_NOT_RECRUITING&pageSize=5&sort=LastUpdatePostDate',
            timeout=10)
        studies = r.json().get('studies', [])
        log.info(f'  Status: {r.status_code} | Studies: {len(studies)}')
        if studies:
            log.info(f'  Sample: {studies[0].get("protocolSection",{}).get("identificationModule",{}).get("briefTitle","")[:60]}')
        log.info('  OK')
        sources['clinicaltrials_gov'] = {'ok': r.status_code == 200, 'status': r.status_code, 'studies': len(studies)}
    except Exception as e:
        failures.append(f'ClinicalTrials: {e}'); log.error(f'  FAIL: {e}')
        sources['clinicaltrials_gov'] = {'ok': False, 'error': str(e)}

    # ── 2. Warpspeed.sh ─────────────────────────────────────────
    log.info('\n[2] Warpspeed.sh')
    try:
        r = requests.get('https://warpspeed.sh/', timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        has_data = any(kw in r.text.lower() for kw in ['experiment', 'ideaya', 'phase', 'trial'])
        log.info(f'  Status: {r.status_code} | Length: {len(r.text):,} | Experiment data: {has_data}')
        if not has_data:
            warnings.append('Warpspeed: no experiment data in raw HTML — JS-rendered, needs browser_task in cron')
            log.warning('  WARNING: JS-rendered, browser_task needed in cron (expected)')
        else:
            log.info('  OK')
    except Exception as e:
        failures.append(f'Warpspeed: {e}'); log.error(f'  FAIL: {e}')

    # ── 3. SEC EDGAR ────────────────────────────────────────────
    log.info('\n[3] SEC EDGAR RSS + CIK file')
    try:
        import feedparser
        r = requests.get(
            'https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=10&search_text=&output=atom',
            headers={'User-Agent': 'BioCatalystBot research@mantisvc.com'}, timeout=12)
        feed = feedparser.parse(r.text)
        log.info(f'  RSS: {r.status_code} | Entries: {len(feed.entries)}')
        r2 = requests.get('https://www.sec.gov/files/company_tickers.json',
                          headers={'User-Agent': 'BioCatalystBot research@mantisvc.com'}, timeout=12)
        tickers_data = r2.json()
        log.info(f'  CIK file: {r2.status_code} | Companies: {len(tickers_data):,}')
        log.info('  OK')
        sources['sec_edgar'] = {'ok': r.status_code == 200 and r2.status_code == 200,
                                'rss_status': r.status_code, 'cik_status': r2.status_code,
                                'rss_entries': len(feed.entries), 'companies': len(tickers_data)}
    except Exception as e:
        failures.append(f'SEC: {e}'); log.error(f'  FAIL: {e}')
        sources['sec_edgar'] = {'ok': False, 'error': str(e)}

    # ── 4. USASpending.gov ──────────────────────────────────────
    log.info('\n[4] USASpending.gov awards')
    try:
        payload = {
            'filters': {
                'time_period': [{'start_date': '2026-03-01', 'end_date': today}],
                'award_type_codes': ['A', 'B', 'C', 'D'],
                'keywords': ['Rocket Lab']
            },
            'fields': ['Award ID', 'Recipient Name', 'Award Amount', 'Awarding Agency', 'Description'],
            'limit': 5, 'page': 1, 'sort': 'Award Amount', 'order': 'desc'
        }
        r = requests.post('https://api.usaspending.gov/api/v2/search/spending_by_award/',
                          json=payload, headers={'Content-Type': 'application/json'}, timeout=20)
        awards = r.json().get('results', [])
        log.info(f'  Status: {r.status_code} | Awards: {len(awards)}')
        for a in awards[:2]:
            log.info(f'    {a.get("Recipient Name","?")} | ${float(a.get("Award Amount",0)):,.0f} | {a.get("Awarding Agency","?")[:40]}')
        log.info('  OK')
    except Exception as e:
        failures.append(f'USASpending: {e}'); log.error(f'  FAIL: {e}')

    # ── 5. Defense.gov RSS ──────────────────────────────────────
    log.info('\n[5] Defense.gov contract RSS')
    try:
        r = requests.get('https://www.defense.gov/News/Contracts/rss/',
                         headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        log.info(f'  Status: {r.status_code} | Length: {len(r.text):,}')
        if r.status_code == 200:
            feed = feedparser.parse(r.text)
            log.info(f'  Entries: {len(feed.entries)}')
            if feed.entries:
                log.info(f'  Sample: {feed.entries[0].get("title","")[:80]}')
                log.info('  OK')
            else:
                warnings.append('Defense.gov: 0 entries parsed from RSS'); log.warning('  WARNING: 0 entries')
        else:
            failures.append(f'Defense.gov: HTTP {r.status_code}'); log.error(f'  FAIL: {r.status_code}')
    except Exception as e:
        failures.append(f'Defense.gov: {e}'); log.error(f'  FAIL: {e}')

    # ── 6. FDA news RSS (press releases) ────────────────────────
    # The legacy advisory-committee-meetings-coming-soon.rss endpoint now 404s;
    # use the FDA press-releases RSS as the canonical "news_rss" health source.
    log.info('\n[6] FDA press-releases RSS')
    try:
        r = requests.get('https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml',
                         headers={'User-Agent': 'Mozilla/5.0'}, timeout=12)
        feed = feedparser.parse(r.text)
        log.info(f'  Status: {r.status_code} | Entries: {len(feed.entries)}')
        if feed.entries:
            log.info(f'  Sample: {feed.entries[0].get("title","")[:80]}')
        log.info('  OK')
        sources['news_rss'] = {'ok': r.status_code == 200, 'status': r.status_code,
                               'entries': len(feed.entries), 'feed': 'fda_press_releases_rss'}
    except Exception as e:
        failures.append(f'FDA RSS: {e}'); log.error(f'  FAIL: {e}')
        sources['news_rss'] = {'ok': False, 'error': str(e)}

    # ── 7. BiopharmCatalyst ─────────────────────────────────────
    log.info('\n[7] BiopharmCatalyst AdCom')
    try:
        r = requests.get('https://www.biopharmcatalyst.com/calendars/adcom-calendar',
                         headers={'User-Agent': 'Mozilla/5.0'}, timeout=12)
        has_adcom = 'advisory' in r.text.lower() or 'adcom' in r.text.lower()
        log.info(f'  Status: {r.status_code} | Length: {len(r.text):,} | AdCom data: {has_adcom}')
        if not has_adcom:
            warnings.append('BiopharmCatalyst: JS-rendered — no table data in raw HTML, browser_task needed')
            log.warning('  WARNING: JS-rendered, browser_task needed in cron (expected)')
        else:
            log.info('  OK')
    except Exception as e:
        failures.append(f'BiopharmCatalyst: {e}'); log.error(f'  FAIL: {e}')

    # ── 8. IDYA IR page live signal check ───────────────────────
    log.info('\n[8] IDEAYA IR live check')
    try:
        r = requests.get('https://ir.ideayabio.com/events',
                         headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        has_reg     = 'register' in r.text.lower()
        has_topline = any(kw in r.text.lower() for kw in ['topline', 'top-line', 'phase 3', 'optimum'])
        log.info(f'  Status: {r.status_code} | Pre-reg: {has_reg} | Topline content: {has_topline}')
        if has_reg and has_topline:
            log.info('  *** SIGNAL: webcast pre-reg detected on IDYA IR page ***')
        log.info('  OK')
    except Exception as e:
        failures.append(f'IDYA IR: {e}'); log.error(f'  FAIL: {e}')

    # ── 9. Twitter via DDG ──────────────────────────────────────
    log.info('\n[9] Twitter via DuckDuckGo')
    try:
        r = requests.post('https://html.duckduckgo.com/html/',
                          data={'q': 'site:twitter.com BioPharmCatalyst PDUFA 2026'},
                          headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        has_results = 'result__a' in r.text and len(r.text) > 3000
        log.info(f'  Status: {r.status_code} | Length: {len(r.text):,} | Results: {has_results}')
        if not has_results:
            warnings.append('Twitter/DDG: unreliable HTML scraping — cron must use search_web tool instead')
            log.warning('  WARNING: DDG not returning results — cron uses search_web tool (expected)')
        else:
            log.info('  OK')
    except Exception as e:
        warnings.append(f'Twitter/DDG: {e}')
        log.warning(f'  WARNING: {e}')

    # ── 10. master_discovery full run (legacy probe) ────────────
    # f-m2-07: failed legacy probes warn rather than ImportError.
    log.info('\n[10] master_discovery.run_discovery() (legacy probe)')
    try:
        from biotech_sniper.intelligence.master_discovery import run_discovery
        result = run_discovery()
        biotech   = len(result.get('new_biotech', []))
        contracts = len(result.get('new_contracts', []))
        adcom     = len(result.get('new_adcom', []))
        defense   = len(result.get('defense_rss', []))
        signals   = len(result.get('signals', []))
        log.info(f'  biotech={biotech} contracts={contracts} adcom={adcom} defense={defense} signals={signals}')
        log.info('  OK')
    except ImportError as e:
        warnings.append(f'master_discovery: legacy module probe skipped ({e})')
        log.warning(f'  WARNING (legacy probe): {e}')
    except Exception as e:
        warnings.append(f'master_discovery: legacy probe raised {type(e).__name__}: {e}')
        log.warning(f'  WARNING (legacy probe): {e}')

    # ── 11. twitter build_queries (legacy probe) ────────────────
    log.info('\n[11] twitter build_twitter_search_queries() (legacy probe)')
    try:
        from biotech_sniper.intelligence.twitter_biotech_monitor import (
            build_twitter_search_queries, parse_twitter_results,
        )
        queries = build_twitter_search_queries()
        log.info(f'  Queries generated: {len(queries)}')
        log.info(f'  Sample: {queries[0] if queries else "NONE"}')
        # parse_twitter_results with dummy data
        dummy = [{'title': 'IDYA topline positive phase 3 optimum', 'snippet': 'IDEAYA hits primary endpoint', 'url': 'https://x.com/test'}]
        sigs = parse_twitter_results(dummy)
        log.info(f'  parse_twitter_results (dummy): {len(sigs)} signals')
        log.info('  OK')
    except ImportError as e:
        warnings.append(f'twitter_monitor: legacy module probe skipped ({e})')
        log.warning(f'  WARNING (legacy probe): {e}')
    except Exception as e:
        warnings.append(f'twitter_monitor: legacy probe raised {type(e).__name__}: {e}')
        log.warning(f'  WARNING (legacy probe): {e}')

    # ── 12. adcom drug_ticker_map (legacy probe) ────────────────
    log.info('\n[12] adcom build_drug_ticker_map() (legacy probe)')
    try:
        from biotech_sniper.sectors.adcom.adcom_scanner import (
            build_drug_ticker_map, run_adcom_scan,
        )
        drug_map = build_drug_ticker_map()
        log.info(f'  Drug entries: {len(drug_map)}')
        # Check a known drug
        axs05 = drug_map.get('AXS-05', drug_map.get('axs-05', {}))
        log.info(f'  AXS-05 lookup: {axs05}')
        log.info('  OK')
    except ImportError as e:
        warnings.append(f'adcom drug_map: legacy module probe skipped ({e})')
        log.warning(f'  WARNING (legacy probe): {e}')
    except Exception as e:
        warnings.append(f'adcom drug_map: legacy probe raised {type(e).__name__}: {e}')
        log.warning(f'  WARNING (legacy probe): {e}')

    # ── 13. contracts defense.gov fetch (legacy probe) ──────────
    log.info('\n[13] sam_sniper.fetch_defense_gov_contracts() (legacy probe)')
    try:
        from biotech_sniper.sectors.contracts.sam_sniper import (
            fetch_defense_gov_contracts, run_contract_scan,
        )
        contracts = fetch_defense_gov_contracts(days_back=7)
        log.info(f'  Contracts: {len(contracts)}')
        if contracts:
            log.info(f'  Sample: {contracts[0].get("title","")[:60]}')
        log.info('  OK')
    except ImportError as e:
        warnings.append(f'defense_contracts: legacy module probe skipped ({e})')
        log.warning(f'  WARNING (legacy probe): {e}')
    except Exception as e:
        warnings.append(f'defense_contracts: legacy probe raised {type(e).__name__}: {e}')
        log.warning(f'  WARNING (legacy probe): {e}')

    # ── 14. company_ticker_map aliases ──────────────────────────
    log.info('\n[14] company_ticker_map aliases check')
    try:
        import json
        # f-m2-07: prefer the package-relative path (``biotech_sniper/sectors/...``);
        # fall back to the bare ``sectors/...`` layout for VPS clones where
        # ``BASE_DIR`` already points at the package root.
        _cmap_candidates = [
            BASE_DIR / 'biotech_sniper/sectors/contracts/company_ticker_map.json',
            BASE_DIR / 'sectors/contracts/company_ticker_map.json',
        ]
        _cmap_path = next((p for p in _cmap_candidates if p.is_file()), _cmap_candidates[0])
        cmap = json.load(open(_cmap_path))
        companies = cmap.get('companies', {})
        has_aliases = sum(1 for v in companies.values() if v.get('aliases'))
        has_options = sum(1 for v in companies.values() if 'options' in v)
        has_cik     = sum(1 for v in companies.values() if v.get('sec_cik'))
        log.info(f'  Companies: {len(companies)} | With aliases: {has_aliases} | With options flag: {has_options} | With CIK: {has_cik}')
        if has_aliases < 10:
            failures.append(f'company_ticker_map: only {has_aliases} companies have aliases (need full expansion)')
        else:
            log.info('  OK')
    except Exception as e:
        failures.append(f'ticker_map: {e}'); log.error(f'  FAIL: {e}')

    # ── 15. active_plays seed check ─────────────────────────────
    # f-m1-03 moved the runtime state JSONs to migrations/seed/. The runtime
    # active_plays SQLite path is introduced in M2; until then, validate the
    # seed JSON shape so M2 has a clean source to backfill from.
    log.info('\n[15] active_plays.json health (seed)')
    try:
        seed_candidates = [
            BASE_DIR / 'migrations/seed/active_plays.json',          # VPS layout (BASE_DIR=repo root)
            BASE_DIR.parent / 'migrations/seed/active_plays.json',    # local layout (BASE_DIR=package dir)
        ]
        seed_path = next((p for p in seed_candidates if p.is_file()), None)
        if seed_path is None:
            # Not a hard failure: the seed file is only needed by the M2 backfill;
            # M1 audit should not flag it as broken.
            warnings.append('active_plays: migrations/seed/active_plays.json not found (expected for fresh checkouts)')
            log.warning('  WARNING: migrations/seed/active_plays.json not found (expected for fresh checkouts)')
        else:
            plays = json.load(open(seed_path))
            active = plays.get('active', {})
            required_fields = ['ticker', 'direction', 'p_success', 'estimated_announcement', 'option_expiry']
            for ticker, play in active.items():
                missing = [f for f in required_fields if not play.get(f) and play.get('direction') != 'EQUITY_ONLY']
                if missing:
                    log.warning(f'  WARNING {ticker}: missing {missing}')
            log.info(f'  Source: {seed_path}')
            log.info(f'  Active: {len(active)} | Monitor: {len(plays.get("monitor",{}))}')
            log.info('  OK')
    except Exception as e:
        failures.append(f'active_plays: {e}'); log.error(f'  FAIL: {e}')

    # ── 16-19. New M2 sources: alpha_sniper.db (SQLite) ─────────
    # These probes are the canonical health source going forward; the
    # legacy JSON state files (sections 14-15 above) are kept for
    # parity with the M1 audit shape but the new M2+ runtime reads
    # from ``data/alpha_sniper.db``.
    db_summary: dict = {}

    def _sqlite_count(conn, table: str):
        try:
            return int(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])
        except Exception:
            return None

    log.info('\n[16] alpha_sniper.db (SQLite) presence + tables')
    try:
        import sqlite3
        db_path = DATA_DIR / 'alpha_sniper.db'
        if not db_path.exists():
            warnings.append(f'alpha_sniper.db not present at {db_path} (expected pre-M2 install)')
            log.warning(f'  WARNING: {db_path} not found')
            db_summary = {'ok': False, 'present': False, 'path': str(db_path)}
        else:
            conn = sqlite3.connect(str(db_path))
            try:
                tables = sorted(
                    r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                )
                log.info(f'  Tables: {tables}')
                db_summary = {
                    'ok': True,
                    'present': True,
                    'path': str(db_path),
                    'tables': tables,
                }
                log.info('  OK')
            finally:
                conn.close()
    except Exception as e:
        failures.append(f'alpha_sniper.db: {e}')
        log.error(f'  FAIL: {e}')
        db_summary = {'ok': False, 'error': str(e)}
    sources['alpha_sniper_db'] = db_summary

    log.info('\n[17] scoring_cache health (SQLite)')
    sc_summary: dict = {}
    try:
        import sqlite3
        db_path = DATA_DIR / 'alpha_sniper.db'
        if not db_path.exists():
            warnings.append('scoring_cache: alpha_sniper.db missing — skipped')
            log.warning('  WARNING: alpha_sniper.db missing — skipped')
            sc_summary = {'ok': False, 'reason': 'db_missing'}
        else:
            conn = sqlite3.connect(str(db_path))
            try:
                total = _sqlite_count(conn, 'scoring_cache')
                today_count = None
                divergent = None
                try:
                    today_count = int(conn.execute(
                        "SELECT COUNT(*) FROM scoring_cache WHERE as_of_date=?",
                        (today,),
                    ).fetchone()[0])
                    divergent = int(conn.execute(
                        "SELECT COUNT(*) FROM scoring_cache WHERE divergence_flag=1"
                    ).fetchone()[0])
                except Exception:
                    pass
                log.info(f'  Rows: {total} | as_of_date={today}: {today_count} | divergent: {divergent}')
                sc_summary = {
                    'ok': True,
                    'rows_total': total,
                    'rows_today': today_count,
                    'rows_divergent': divergent,
                }
                log.info('  OK')
            finally:
                conn.close()
    except Exception as e:
        failures.append(f'scoring_cache: {e}')
        log.error(f'  FAIL: {e}')
        sc_summary = {'ok': False, 'error': str(e)}
    sources['scoring_cache'] = sc_summary

    log.info('\n[18] llm_cost_ledger health (SQLite)')
    ledger_summary: dict = {}
    try:
        import sqlite3
        db_path = DATA_DIR / 'alpha_sniper.db'
        if not db_path.exists():
            warnings.append('llm_cost_ledger: alpha_sniper.db missing — skipped')
            log.warning('  WARNING: alpha_sniper.db missing — skipped')
            ledger_summary = {'ok': False, 'reason': 'db_missing'}
        else:
            conn = sqlite3.connect(str(db_path))
            try:
                total = _sqlite_count(conn, 'llm_cost_ledger')
                spend_by_provider: list = []
                try:
                    rows = conn.execute(
                        "SELECT provider, COUNT(*) AS calls, "
                        "ROUND(COALESCE(SUM(cost_usd),0),4) AS spend "
                        "FROM llm_cost_ledger GROUP BY provider ORDER BY provider"
                    ).fetchall()
                    spend_by_provider = [
                        {'provider': r[0], 'calls': r[1], 'spend_usd': r[2]}
                        for r in rows
                    ]
                except Exception:
                    pass
                log.info(f'  Rows: {total} | by provider: {spend_by_provider}')
                ledger_summary = {
                    'ok': True,
                    'rows_total': total,
                    'spend_by_provider': spend_by_provider,
                }
                log.info('  OK')
            finally:
                conn.close()
    except Exception as e:
        failures.append(f'llm_cost_ledger: {e}')
        log.error(f'  FAIL: {e}')
        ledger_summary = {'ok': False, 'error': str(e)}
    sources['llm_cost_ledger'] = ledger_summary

    log.info('\n[19] news_events health (SQLite, optional)')
    # ``news_events`` is introduced by a later M2 feature; absence is a
    # warning, not a failure, so this audit script can land before that
    # feature ships.
    news_summary: dict = {}
    try:
        import sqlite3
        db_path = DATA_DIR / 'alpha_sniper.db'
        if not db_path.exists():
            warnings.append('news_events: alpha_sniper.db missing — skipped')
            log.warning('  WARNING: alpha_sniper.db missing — skipped')
            news_summary = {'ok': False, 'reason': 'db_missing'}
        else:
            conn = sqlite3.connect(str(db_path))
            try:
                tbl = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='news_events'"
                ).fetchone()
                if tbl is None:
                    warnings.append('news_events: table not yet created (pending M2 universe + news ingest feature)')
                    log.warning('  WARNING: news_events table not yet created')
                    news_summary = {'ok': False, 'present': False}
                else:
                    total = _sqlite_count(conn, 'news_events')
                    today_count = None
                    try:
                        today_count = int(conn.execute(
                            "SELECT COUNT(*) FROM news_events WHERE date(ingested_at)=?",
                            (today,),
                        ).fetchone()[0])
                    except Exception:
                        pass
                    log.info(f'  Rows: {total} | ingested today: {today_count}')
                    news_summary = {
                        'ok': True,
                        'present': True,
                        'rows_total': total,
                        'rows_today': today_count,
                    }
                    log.info('  OK')
            finally:
                conn.close()
    except Exception as e:
        warnings.append(f'news_events: probe raised {type(e).__name__}: {e}')
        log.warning(f'  WARNING: {e}')
        news_summary = {'ok': False, 'error': str(e)}
    sources['news_events'] = news_summary

    log.info('audit_summary_separator')
    log.info('='*60)
    log.info(f'FAILURES ({len(failures)}):')
    for f in failures:
        log.error(f'  FAIL: {f}')
    log.info(f'WARNINGS ({len(warnings)}):')
    for w in warnings:
        log.warning(f'  WARN: {w}')
    if not failures:
        log.info('\nALL CRITICAL TESTS PASS')

    # ── Write structured audit JSON to state/audit_latest.json ──
    #
    # f-m4-03 contract: the file MUST contain ``generated_at``,
    # ``sources`` (with the seven canonical keys ``ct.gov`` / ``sec_edgar``
    # / ``news_rss`` / ``alpaca_paper`` / ``xai`` / ``anthropic`` /
    # ``gemini``), ``last_daily_run``, ``last_intraday_run``,
    # ``db_size_bytes``, ``paper_account_equity``. The write is atomic
    # (temp + rename) so a crashed audit run never leaves a partially
    # written JSON observable to readers.
    #
    # The legacy ``sources`` dict assembled above is preserved by passing
    # it as ``extra`` — :func:`write_audit_latest` folds its richer
    # per-source diagnostics (``rss_entries``, ``companies``, etc.) into
    # the merged ``sources`` map without overwriting the seven
    # contract-required entries.
    try:
        state_dir = BASE_DIR / 'state'
        audit_path = state_dir / 'audit_latest.json'
        payload = write_audit_latest(
            audit_path,
            extra={
                'as_of_date': today,
                'sources': sources,
                'failures': failures,
                'warnings': warnings,
            },
        )
        log.info(f'\nAudit JSON written to {audit_path}')
    except Exception as e:
        log.warning(f'\nWARNING: failed to write audit JSON: {e}')
