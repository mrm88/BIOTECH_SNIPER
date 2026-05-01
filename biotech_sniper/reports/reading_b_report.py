"""Reading-B observability CLI (Reading-B M4 — feature ``f-m4-08``).

This module is the single Reading-B surface that summarises the
news-driven Stage-1/Stage-2 trading path for a given UTC day.

CLI
---
``python -m biotech_sniper.reports.reading_b_report [--since YYYY-MM-DD]
[--db PATH] [--json] [--by-source]``

Modes
~~~~~

* default — human-readable fixed-width report.
* ``--json`` — single-line JSON document, consumed by VPS validators
  and the Reading-B watchdog.
* ``--by-source`` — augment the human report with a per-source
  breakdown table (the JSON mode always carries the per-source
  block in ``per_source_emit_counts``).

Output schema (JSON)
~~~~~~~~~~~~~~~~~~~~

The JSON document always contains the following keys (with the
exact spelling required by the validation contract):

* ``candidate_events_today`` (int) — count of ``candidate_events``
  rows whose ``DATE(emitted_at) = since``.
* ``gate_pass_rate`` (float in ``[0, 1]``) — share of candidates
  that produced a ``paper_orders`` row with
  ``event='news_event_entry'`` AND ``DATE(created_at)=since``. Zero
  candidates → ``0.0`` (no-op pass-through).
* ``stage2_dollars_today`` (float ≥ 0) — ``SUM(cost_usd)`` from
  ``llm_cost_ledger`` where ``provider='perplexity' AND
  DATE(called_at)=since``. Source-of-truth match within $0.01
  (VAL-M4-049).
* ``top_n_tickers_by_emit`` (list of ``{ticker, count}``) — top 10
  tickers by candidate_events emit count for the day. Sorted by
  ``count DESC, ticker ASC``. Length ≤ 10.
* ``top10_tickers_by_emit`` (list) — alias for
  ``top_n_tickers_by_emit`` so the contract evidence form
  (``VAL-M4-048``) and the feature description form
  (``f-m4-08`` description) both pass against the same payload.
* ``per_source_emit_counts`` (dict ``{source: count}``) — emit
  count grouped by ``news_events.source`` (the upstream feed name).
* ``since`` (str) — the ISO date that was queried.
* ``cap_usd`` (float) — the active Stage-2 daily cap from
  :func:`config.get_llm_stage2_daily_usd_cap`. Reported so the
  consumer can verify ``stage2_dollars_today <= cap_usd``.

The schema is intentionally additive — the contract requires the
keys above to exist with the documented types/ranges; downstream
consumers MUST tolerate extra keys. The ``top_n``/``top10`` aliasing
trick is the simplest safe fix for the contract / feature spec
naming drift.

Source-of-truth invariants (VAL-M4-049)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The Stage-2 dollar total reported here is computed by the SAME
SQL the validation contract uses:

    SELECT COALESCE(ROUND(SUM(cost_usd), 4), 0)
      FROM llm_cost_ledger
     WHERE provider='perplexity'
       AND DATE(called_at)=DATE(:since)

so the spend reported by ``--json`` matches the ledger
source-of-truth deterministically (within $0.01 floating-point
tolerance).

The module never writes to the database; it only reads via
:func:`biotech_sniper.db.connect_readonly` (best-effort fallback to
the regular connect helper if the read-only URI mode is rejected).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Iterable, Mapping, Sequence

from biotech_sniper import db as _db
from biotech_sniper.config import get_llm_stage2_daily_usd_cap
from biotech_sniper.paths import DATA_DIR

__all__ = [
    "DEFAULT_TOP_N",
    "REQUIRED_KEYS",
    "build_report",
    "format_text_report",
    "main",
]


#: Default ``top_n`` size for the ``top_n_tickers_by_emit`` block.
#: Locked at ``10`` to match VAL-M4-048's ``length<=10`` constraint
#: AND the ``top10_`` alias key.
DEFAULT_TOP_N: Final[int] = 10


#: The required JSON keys per the feature description / VAL-M4-048.
REQUIRED_KEYS: Final[tuple[str, ...]] = (
    "candidate_events_today",
    "gate_pass_rate",
    "stage2_dollars_today",
    "top_n_tickers_by_emit",
    "per_source_emit_counts",
)


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------


_SQL_CANDIDATE_COUNT: Final[str] = (
    "SELECT COUNT(*) AS n FROM candidate_events "
    "WHERE DATE(emitted_at) = DATE(?)"
)

_SQL_PERPLEXITY_SUM: Final[str] = (
    "SELECT COALESCE(ROUND(SUM(cost_usd), 4), 0) AS total "
    "FROM llm_cost_ledger "
    "WHERE provider='perplexity' AND DATE(called_at) = DATE(?)"
)

_SQL_NEWS_EVENT_ENTRY_COUNT: Final[str] = (
    "SELECT COUNT(*) AS n FROM paper_orders "
    "WHERE event='news_event_entry' AND DATE(created_at) = DATE(?)"
)

_SQL_TOP_TICKERS: Final[str] = (
    "SELECT ticker, COUNT(*) AS cnt "
    "FROM candidate_events "
    "WHERE DATE(emitted_at) = DATE(?) "
    "GROUP BY ticker "
    "ORDER BY cnt DESC, ticker ASC "
    "LIMIT ?"
)

_SQL_PER_SOURCE: Final[str] = (
    "SELECT ne.source AS source, COUNT(*) AS cnt "
    "FROM candidate_events ce "
    "JOIN news_events ne ON ce.source_news_event_id = ne.id "
    "WHERE DATE(ce.emitted_at) = DATE(?) "
    "GROUP BY ne.source "
    "ORDER BY cnt DESC, ne.source ASC"
)


def _connect_readonly_or_fallback(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path`` read-only, falling back to a normal connect."""
    try:
        return _db.connect_readonly(db_path)
    except (sqlite3.Error, OSError, ValueError):
        # ``connect_readonly`` rejects ``:memory:`` and may raise on
        # exotic SQLite URI builds — we only ever read here, so the
        # regular connect helper is a safe fallback.
        return _db.connect(db_path)


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------


def _safe_query(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...],
    *,
    default: Any,
) -> Any:
    """Execute ``sql`` and return the result, swallowing missing-table errors.

    The Reading-B v10 schema may not yet be applied on every database
    (e.g. a fresh laptop install still at v9). In that case the
    Stage-1 (``candidate_events``, ``news_match_log``) and Stage-2
    (``ensemble_scores_event``) tables are absent and SQLite raises
    ``OperationalError: no such table: ...``. Callers want this CLI
    to remain useful — surfacing zero-counts is the right answer
    rather than crashing — so we trap that single error class and
    return ``default``. Every other ``OperationalError`` (e.g.
    syntax errors, locked db, corrupt journal) bubbles up so
    operators see a loud failure.
    """
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return default
        raise


def build_report(
    db_path: Path,
    since: str,
    *,
    top_n: int = DEFAULT_TOP_N,
    cap_usd: float | None = None,
) -> dict[str, Any]:
    """Compute the Reading-B JSON payload for ``since`` (UTC date).

    Parameters
    ----------
    db_path:
        Path to the SQLite db (e.g. ``data/alpha_sniper.db``).
    since:
        ISO date ``YYYY-MM-DD`` whose UTC day window we report on.
    top_n:
        Cap for the ``top_n_tickers_by_emit`` block. Defaults to 10
        so the contract alias ``top10_tickers_by_emit`` carries the
        same payload.
    cap_usd:
        Override the Stage-2 daily cap. ``None`` (default) reads
        :func:`config.get_llm_stage2_daily_usd_cap` at call time so
        operators can ``LLM_STAGE2_DAILY_USD_CAP=...`` on the shell.

    Returns
    -------
    dict
        JSON-serialisable payload. ``top_n_tickers_by_emit`` and
        ``top10_tickers_by_emit`` carry the same list reference so
        downstream consumers can use either key.
    """

    if cap_usd is None:
        cap_usd = float(get_llm_stage2_daily_usd_cap())

    conn = _connect_readonly_or_fallback(db_path)
    try:
        cand_rows = _safe_query(conn, _SQL_CANDIDATE_COUNT, (since,), default=[])
        candidate_count = int(cand_rows[0]["n"]) if cand_rows else 0

        ledger_rows = _safe_query(
            conn, _SQL_PERPLEXITY_SUM, (since,), default=[]
        )
        stage2_dollars = float(ledger_rows[0]["total"]) if ledger_rows else 0.0

        entry_rows = _safe_query(
            conn, _SQL_NEWS_EVENT_ENTRY_COUNT, (since,), default=[]
        )
        entry_count = int(entry_rows[0]["n"]) if entry_rows else 0

        top_rows = _safe_query(
            conn, _SQL_TOP_TICKERS, (since, int(top_n)), default=[]
        )
        source_rows = _safe_query(conn, _SQL_PER_SOURCE, (since,), default=[])
    finally:
        conn.close()

    if candidate_count > 0:
        gate_pass_rate = float(entry_count) / float(candidate_count)
    else:
        gate_pass_rate = 0.0
    # Clamp into [0, 1] defensively — over-counting in ``paper_orders``
    # (e.g. multi-row exits filed under the same client_order_id) must
    # never push the rate above 1.
    if gate_pass_rate < 0.0:
        gate_pass_rate = 0.0
    if gate_pass_rate > 1.0:
        gate_pass_rate = 1.0

    top_tickers: list[dict[str, Any]] = [
        {"ticker": row["ticker"], "count": int(row["cnt"])}
        for row in top_rows
    ]

    per_source: dict[str, int] = {
        row["source"]: int(row["cnt"]) for row in source_rows
    }

    payload: dict[str, Any] = {
        "since": since,
        "candidate_events_today": candidate_count,
        "gate_pass_rate": float(gate_pass_rate),
        "stage2_dollars_today": float(stage2_dollars),
        "top_n_tickers_by_emit": top_tickers,
        # Alias (VAL-M4-048 evidence form). Same list reference so
        # the two keys never drift out of sync within a single
        # report.
        "top10_tickers_by_emit": top_tickers,
        "per_source_emit_counts": per_source,
        "cap_usd": float(cap_usd),
        "news_event_entries_today": entry_count,
    }
    return payload


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------


def _format_top_table(rows: Sequence[Mapping[str, Any]]) -> str:
    """Return a fixed-width 2-column table of ticker -> count."""
    if not rows:
        return "(no candidate_events)"
    header = ("ticker", "count")
    lines = [list(header)] + [
        [str(r["ticker"]), str(int(r["count"]))] for r in rows
    ]
    widths = [max(len(r[i]) for r in lines) for i in range(len(header))]
    sep = "  ".join("-" * w for w in widths)
    rendered = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r)) for r in lines]
    return "\n".join([rendered[0], sep] + rendered[1:])


def _format_per_source_table(per_source: Mapping[str, int]) -> str:
    """Return a fixed-width 2-column table of source -> count."""
    if not per_source:
        return "(no per-source rows)"
    header = ("source", "count")
    rows = sorted(per_source.items(), key=lambda kv: (-kv[1], kv[0]))
    lines = [list(header)] + [[k, str(int(v))] for k, v in rows]
    widths = [max(len(r[i]) for r in lines) for i in range(len(header))]
    sep = "  ".join("-" * w for w in widths)
    rendered = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r)) for r in lines]
    return "\n".join([rendered[0], sep] + rendered[1:])


def format_text_report(payload: Mapping[str, Any], *, by_source: bool) -> str:
    """Render the JSON payload as a human-readable, fixed-width report."""

    cap = float(payload.get("cap_usd", get_llm_stage2_daily_usd_cap()))
    spend = float(payload["stage2_dollars_today"])
    rate = float(payload["gate_pass_rate"])

    out: list[str] = []
    out.append(f"# Reading-B report — {payload['since']}")
    out.append(f"db                       = {payload.get('db_path', '')}")
    out.append(f"candidate_events_today   = {int(payload['candidate_events_today'])}")
    out.append(
        f"news_event_entries_today = {int(payload.get('news_event_entries_today', 0))}"
    )
    out.append(f"gate_pass_rate           = {rate:.4f}")
    out.append(
        f"stage2_dollars_today     = {spend:.4f}  "
        f"(cap=${cap:.2f}, headroom=${max(cap - spend, 0.0):.4f})"
    )
    out.append("")
    out.append("# top_n_tickers_by_emit")
    out.append(_format_top_table(payload["top_n_tickers_by_emit"]))
    out.append("")
    if by_source:
        out.append("# per_source_emit_counts")
        out.append(_format_per_source_table(payload["per_source_emit_counts"]))
    else:
        per_src = payload["per_source_emit_counts"] or {}
        n_sources = len(per_src)
        out.append(f"per_source_emit_counts: {n_sources} source(s) — pass --by-source for breakdown")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _validate_iso_date(value: str) -> str:
    """Accept ``YYYY-MM-DD``; raise :class:`argparse.ArgumentTypeError` else."""
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--since must be ISO format YYYY-MM-DD; got {value!r}"
        ) from exc
    return value


def _today_utc_iso() -> str:
    """Return today's UTC date as ``YYYY-MM-DD``.

    Uses timezone-aware ``datetime.now(timezone.utc)`` per the
    project's no-deprecated-naive-now lint in
    ``tests/test_no_utcnow.py``.
    """
    from datetime import timezone

    return datetime.now(timezone.utc).date().isoformat()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.reports.reading_b_report",
        description=(
            "Reading-B observability CLI: candidate-events/day, "
            "gate-pass rate, Stage-2 daily $ spend, top-N tickers "
            "by emit, per-source emit counts."
        ),
    )
    parser.add_argument(
        "--since",
        type=_validate_iso_date,
        default=_today_utc_iso(),
        help=(
            "UTC day to report on (YYYY-MM-DD). Defaults to today UTC."
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DATA_DIR / "alpha_sniper.db",
        help=(
            "SQLite database path. Defaults to "
            "$BIOTECH_SNIPER_HOME/data/alpha_sniper.db."
        ),
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=DEFAULT_TOP_N,
        help=f"Top-N ticker cap (default {DEFAULT_TOP_N}, max 10).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit a single-line JSON payload (machine-readable). The "
            "payload always contains the Reading-B keys "
            "candidate_events_today, gate_pass_rate, "
            "stage2_dollars_today, top_n_tickers_by_emit "
            "(alias top10_tickers_by_emit), per_source_emit_counts."
        ),
    )
    parser.add_argument(
        "--by-source",
        action="store_true",
        help=(
            "In text mode, render a per-source breakdown table "
            "instead of the one-line summary. JSON mode always "
            "includes per_source_emit_counts."
        ),
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    """CLI entrypoint. Returns the process exit code."""
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    db_path: Path = args.db.expanduser()
    if not db_path.exists():
        sys.stderr.write(
            f"ERROR: SQLite db not found at {db_path}. Run the v10 "
            f"migration first.\n"
        )
        return 2

    # Clamp ``top_n`` at 10 so ``top10_tickers_by_emit`` (the
    # contract alias) never exceeds its declared length cap.
    top_n = max(1, min(int(args.top_n), DEFAULT_TOP_N))

    try:
        payload = build_report(db_path, args.since, top_n=top_n)
    except sqlite3.OperationalError as exc:
        sys.stderr.write(f"ERROR: SQLite read failed: {exc}\n")
        return 3

    payload["db_path"] = str(db_path)

    if args.json:
        # Single-line JSON document (final stdout line is the JSON
        # payload — VAL-M4-047/048 ``tail -1 | jq`` evidence).
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    else:
        sys.stdout.write(
            format_text_report(payload, by_source=bool(args.by_source)) + "\n"
        )

    # Spend never exceeds the cap by construction (the ledger SUM
    # is not throttled here; the cap is enforced upstream by the
    # Stage-2 cap-projection gate). Surface a non-zero exit only
    # when the spend exceeds the cap so operators get a loud signal
    # in CI.
    if payload["stage2_dollars_today"] > payload["cap_usd"] + 0.005:
        sys.stderr.write(
            f"WARN: stage2_dollars_today={payload['stage2_dollars_today']:.4f} "
            f"exceeds cap_usd={payload['cap_usd']:.4f}\n"
        )
        return 4

    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
