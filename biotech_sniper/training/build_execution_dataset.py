"""Assemble the execution best-practices feature store at
``data/training/execution.parquet``.

The builder joins four tables — :data:`paper_orders`,
:data:`execution_events`, :data:`execution_fills` and (LEFT JOIN)
:data:`liquidity_probes` — into a single parquet with one row per
fill event recorded in :data:`execution_fills` (i.e. one row per
executed leg). The schema covers every column listed in the M5
contract VAL-M5-037:

* Identity: ``ticker``
* Submit-time chain features: ``dte``, ``strike_offset_pct``,
  ``spread_width_bps``, ``oi_at_submit``, ``volume_at_submit``,
  ``vix``, ``iv_at_submit``, ``probe_classification``
* Submit-time order features: ``requested_mid``
* Fill outcomes: ``filled_price``, ``slippage_bps``,
  ``time_to_fill_ms``, ``partial_fill_ratio``
* Targets: ``was_filled``, ``was_filled_within_60s``,
  ``partial_fill``, ``slippage_bps_actual``

Several submit-time features (``oi_at_submit``, ``volume_at_submit``,
``vix``, ``iv_at_submit``, ``spread_width_bps``,
``strike_offset_pct``) are not yet captured in the M3 telemetry
tables. The columns are emitted as ``float64`` with ``NaN`` so the
parquet schema satisfies the contract today and downstream consumers
have a stable home for those values once they are wired up. The
target columns and the columns sourced directly from the
``execution_fills`` row are always populated.

Liquidity-probe LEFT JOIN
-------------------------
The probe match key is ``(ticker, expiry, strike, date(submitted_at))``:

* ``ticker`` is parsed out of the OCC option symbol on
  :data:`paper_orders.symbol` (the trailing 15 chars are
  ``YYMMDD<C/P>STRIKE*1000`` so ``ticker = symbol[:-15]``);
* ``expiry`` and ``strike`` come from the same OCC parse;
* ``date`` is the date portion of the ``submitted`` execution-event
  timestamp (or :data:`paper_orders.created_at` as a fallback).

When no probe row matches the tuple, ``probe_classification`` is
``None`` (NaN in the parquet). When multiple probes share the same
key the most recent one wins.

Per-ticker rollup view
----------------------
The builder also creates the SQLite view :data:`v_execution_stats`
(idempotent ``CREATE VIEW IF NOT EXISTS``) so the validation
contract VAL-M5-039 finds it. The view aggregates per-ticker:

* ``mean_slippage_bps``: ``AVG(execution_fills.slippage_bps)``
* ``p95_slippage_bps``: conservative ``MAX(slippage_bps)``
  approximation while fill volume is small.
* ``fill_rate``: distinct ``paper_orders`` with ≥1 fill / total
  ``paper_orders`` per ticker.
* ``partial_fill_rate``: distinct ``paper_orders`` with ≥1 partial
  fill / total ``paper_orders`` per ticker.
* ``mean_time_to_fill_ms``: ``AVG(time_to_fill_ms)`` over fills.
* ``n_orders``: distinct ``paper_orders`` count.

The view is filtered on ``LENGTH(symbol) > 15`` so legacy rows that
predate the OCC-symbol invariant don't pollute the rollup.

CLI
---

::

    python -m biotech_sniper.training.build_execution_dataset
        [--db DATA_DIR/alpha_sniper.db]
        [--out DATA_DIR/training/execution.parquet]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, Optional

import pandas as pd

from biotech_sniper import db
from biotech_sniper.paths import DATA_DIR


__all__ = [
    "REQUIRED_COLS",
    "BuildResult",
    "V_EXECUTION_STATS_DDL",
    "build",
    "main",
]


_log = logging.getLogger(__name__)


# Required columns per VAL-M5-037 (order matches the contract).
REQUIRED_COLS: Final[tuple[str, ...]] = (
    "ticker",
    "dte",
    "strike_offset_pct",
    "spread_width_bps",
    "oi_at_submit",
    "volume_at_submit",
    "vix",
    "iv_at_submit",
    "probe_classification",
    "requested_mid",
    "filled_price",
    "slippage_bps",
    "time_to_fill_ms",
    "partial_fill_ratio",
    "was_filled",
    "was_filled_within_60s",
    "partial_fill",
    "slippage_bps_actual",
)


# OCC option symbol format: ``<ticker><YYMMDD><C|P><STRIKE*1000:08>``.
# Trailing 15 chars are the date+type+strike, so ``ticker = symbol[:-15]``.
_OCC_SUFFIX_LEN: Final[int] = 15

# Strict OCC matcher used when we want the parsed expiry / strike too.
# Tickers are 1–6 chars of [A-Z0-9.] starting with a letter.
_OCC_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<ticker>[A-Z][A-Z0-9.]{0,5})"
    r"(?P<yymmdd>\d{6})"
    r"(?P<cp>[CP])"
    r"(?P<strike>\d{8})$"
)


# Default output path used by the CLI and library callers.
DEFAULT_OUT: Final[Path] = DATA_DIR / "training" / "execution.parquet"


# Per-ticker rollup view (VAL-M5-039). Created during ``build`` so
# the schema lives next to the writer that depends on it. Uses
# ``CREATE VIEW IF NOT EXISTS`` so re-running is a no-op.
V_EXECUTION_STATS_DDL: Final[str] = """
CREATE VIEW IF NOT EXISTS v_execution_stats AS
SELECT
    SUBSTR(po.symbol, 1, LENGTH(po.symbol) - 15)              AS ticker,
    AVG(ef.slippage_bps)                                      AS mean_slippage_bps,
    -- p95 approximated as MAX while fill volume is small. Refine
    -- with a window-function percentile once n_orders >> 20.
    MAX(ef.slippage_bps)                                      AS p95_slippage_bps,
    CAST(COUNT(DISTINCT CASE WHEN ef.id IS NOT NULL THEN po.id END) AS REAL)
        / NULLIF(COUNT(DISTINCT po.id), 0)                    AS fill_rate,
    CAST(COUNT(DISTINCT CASE WHEN ef.partial_qty_remaining > 0
                              THEN po.id END) AS REAL)
        / NULLIF(COUNT(DISTINCT po.id), 0)                    AS partial_fill_rate,
    AVG(ef.time_to_fill_ms)                                   AS mean_time_to_fill_ms,
    COUNT(DISTINCT po.id)                                     AS n_orders
FROM paper_orders po
LEFT JOIN execution_fills ef ON ef.paper_order_id = po.id
WHERE po.symbol IS NOT NULL
  AND LENGTH(po.symbol) > 15
GROUP BY SUBSTR(po.symbol, 1, LENGTH(po.symbol) - 15)
HAVING COUNT(DISTINCT CASE WHEN ef.id IS NOT NULL THEN po.id END) >= 1
"""


@dataclass
class BuildResult:
    """Summary returned by :func:`build` and printed on stdout."""

    out_path: Path
    rows: int
    fills_count: int
    columns: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_occ_symbol(symbol: Any) -> Optional[dict[str, Any]]:
    """Return ``{ticker, expiry, strike}`` from an OCC option symbol.

    Falls back to a suffix-strip ticker (no expiry / strike) when the
    strict regex doesn't match — keeps the ``ticker`` column populated
    even for non-standard symbols (legacy rows).
    """

    if not isinstance(symbol, str):
        return None
    sym = symbol.strip()
    if not sym:
        return None
    m = _OCC_RE.match(sym)
    if not m:
        if len(sym) > _OCC_SUFFIX_LEN:
            return {
                "ticker": sym[:-_OCC_SUFFIX_LEN],
                "expiry": None,
                "strike": None,
            }
        return None
    yymmdd = m.group("yymmdd")
    try:
        year = 2000 + int(yymmdd[0:2])
        month = int(yymmdd[2:4])
        day = int(yymmdd[4:6])
        expiry = date(year, month, day).isoformat()
    except ValueError:
        expiry = None
    try:
        strike = int(m.group("strike")) / 1000.0
    except ValueError:
        strike = None
    return {"ticker": m.group("ticker"), "expiry": expiry, "strike": strike}


def _date_only(iso_ts: Any) -> Optional[str]:
    """Return the ``YYYY-MM-DD`` slice from an ISO-8601 timestamp."""

    if iso_ts is None:
        return None
    s = str(iso_ts).strip()
    if len(s) < 10:
        return None
    return s[:10]


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build(
    *,
    db_path: Path | None = None,
    out_path: Path | None = None,
) -> BuildResult:
    """Read the four tables and write the execution parquet.

    Returns a :class:`BuildResult` with the on-disk path, row count
    and the columns written.
    """

    db_path = (db_path or DATA_DIR / "alpha_sniper.db").expanduser()
    out_path = (out_path or DEFAULT_OUT).expanduser()

    fill_rows: list[dict[str, Any]] = []
    probe_index: dict[tuple[str, str, float, str], str] = {}

    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)

        # Idempotent view creation. CREATE VIEW IF NOT EXISTS is a
        # no-op once the view is present in the db.
        conn.execute(V_EXECUTION_STATS_DDL)
        conn.commit()

        cur = conn.execute(
            """
            SELECT
                ef.id              AS fill_id,
                ef.paper_order_id  AS paper_order_id,
                ef.filled_at       AS filled_at,
                ef.filled_price    AS filled_price,
                ef.filled_qty      AS filled_qty,
                ef.requested_mid_at_submit AS requested_mid,
                ef.slippage_bps    AS slippage_bps,
                ef.time_to_fill_ms AS time_to_fill_ms,
                ef.partial_qty_remaining AS partial_qty_remaining,
                po.symbol          AS symbol,
                po.qty             AS order_qty,
                po.created_at      AS order_created_at,
                po.purpose         AS purpose,
                (
                    SELECT MIN(ee.event_at)
                    FROM execution_events ee
                    WHERE ee.paper_order_id = po.id
                      AND ee.event_type = 'submitted'
                ) AS submitted_at_event,
                (
                    SELECT MIN(ee.event_at)
                    FROM execution_events ee
                    WHERE ee.paper_order_id = po.id
                ) AS first_event_at
            FROM execution_fills ef
            JOIN paper_orders po ON po.id = ef.paper_order_id
            ORDER BY ef.id
            """
        )
        fill_rows = [dict(r) for r in cur.fetchall()]

        probe_cur = conn.execute(
            """
            SELECT ticker, expiry, strike, classification, submitted_at
            FROM liquidity_probes
            ORDER BY id
            """
        )
        for r in probe_cur.fetchall():
            row = dict(r) if isinstance(r, sqlite3.Row) else {
                "ticker": r[0], "expiry": r[1], "strike": r[2],
                "classification": r[3], "submitted_at": r[4],
            }
            ticker = row.get("ticker")
            expiry = row.get("expiry")
            strike = row.get("strike")
            classification = row.get("classification")
            submitted_at = row.get("submitted_at")
            d = _date_only(submitted_at)
            if not (
                isinstance(ticker, str)
                and ticker
                and isinstance(expiry, str)
                and expiry
                and strike is not None
                and d
            ):
                continue
            try:
                key = (ticker, expiry, float(strike), d)
            except (TypeError, ValueError):
                continue
            # Most recent probe row (insertion order) wins on duplicates.
            probe_index[key] = classification
    finally:
        conn.close()

    rows = [_row_for_fill(fr, probe_index) for fr in fill_rows]
    df = _coerce_dataframe(rows)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, engine="pyarrow", index=False)

    _log.info(
        "build_execution_dataset wrote %d rows to %s (fills=%d, probes=%d)",
        len(df),
        out_path,
        len(fill_rows),
        len(probe_index),
    )

    return BuildResult(
        out_path=out_path,
        rows=int(len(df)),
        fills_count=len(fill_rows),
        columns=list(df.columns),
    )


def _row_for_fill(
    fr: dict[str, Any],
    probe_index: dict[tuple[str, str, float, str], str],
) -> dict[str, Any]:
    """Convert one ``execution_fills`` row into a parquet record."""

    symbol = fr.get("symbol")
    parsed = _parse_occ_symbol(symbol) if symbol else None
    ticker = parsed["ticker"] if parsed else None
    expiry = parsed["expiry"] if parsed else None
    strike = parsed["strike"] if parsed else None

    submitted_at = (
        fr.get("submitted_at_event")
        or fr.get("first_event_at")
        or fr.get("order_created_at")
    )
    submitted_date = _date_only(submitted_at)

    dte: Optional[float] = None
    if expiry and submitted_date:
        try:
            exp_d = datetime.fromisoformat(expiry).date()
            sub_d = datetime.fromisoformat(submitted_date).date()
            dte = float((exp_d - sub_d).days)
        except ValueError:
            dte = None

    probe_classification: Optional[str] = None
    if ticker and expiry and strike is not None and submitted_date:
        try:
            key = (ticker, expiry, float(strike), submitted_date)
        except (TypeError, ValueError):
            key = None
        if key is not None:
            probe_classification = probe_index.get(key)

    filled_qty_raw = fr.get("filled_qty")
    order_qty_raw = fr.get("order_qty")
    try:
        filled_qty = int(filled_qty_raw) if filled_qty_raw is not None else 0
    except (TypeError, ValueError):
        filled_qty = 0
    try:
        order_qty = int(order_qty_raw) if order_qty_raw is not None else 0
    except (TypeError, ValueError):
        order_qty = 0
    partial_fill_ratio: Optional[float] = (
        float(filled_qty) / float(order_qty) if order_qty > 0 else None
    )

    partial_qty_remaining_raw = fr.get("partial_qty_remaining")
    try:
        partial_qty_remaining = (
            int(partial_qty_remaining_raw)
            if partial_qty_remaining_raw is not None
            else 0
        )
    except (TypeError, ValueError):
        partial_qty_remaining = 0

    time_to_fill_ms_raw = fr.get("time_to_fill_ms")
    if isinstance(time_to_fill_ms_raw, (int, float)):
        time_to_fill_ms: Optional[float] = float(time_to_fill_ms_raw)
    else:
        time_to_fill_ms = None

    was_filled = True  # an execution_fills row by definition = a fill
    was_filled_within_60s = bool(
        time_to_fill_ms is not None and time_to_fill_ms <= 60_000.0
    )
    partial_fill = partial_qty_remaining > 0

    slippage_bps_raw = fr.get("slippage_bps")
    try:
        slippage_bps = (
            float(slippage_bps_raw) if slippage_bps_raw is not None else None
        )
    except (TypeError, ValueError):
        slippage_bps = None

    requested_mid_raw = fr.get("requested_mid")
    try:
        requested_mid = (
            float(requested_mid_raw)
            if requested_mid_raw is not None
            else None
        )
    except (TypeError, ValueError):
        requested_mid = None

    filled_price_raw = fr.get("filled_price")
    try:
        filled_price = (
            float(filled_price_raw) if filled_price_raw is not None else None
        )
    except (TypeError, ValueError):
        filled_price = None

    return {
        "fill_id": fr.get("fill_id"),
        "paper_order_id": fr.get("paper_order_id"),
        "ticker": ticker,
        "dte": dte,
        # The columns below are forward-compatible placeholders; the
        # M3 telemetry tables don't yet capture them. Emitted as
        # float64 NaN so the parquet schema is stable.
        "strike_offset_pct": None,
        "spread_width_bps": None,
        "oi_at_submit": None,
        "volume_at_submit": None,
        "vix": None,
        "iv_at_submit": None,
        # Liquidity-probe LEFT JOIN result.
        "probe_classification": probe_classification,
        # Submit-time + fill-outcome features.
        "requested_mid": requested_mid,
        "filled_price": filled_price,
        "slippage_bps": slippage_bps,
        "time_to_fill_ms": time_to_fill_ms,
        "partial_fill_ratio": partial_fill_ratio,
        # Targets.
        "was_filled": was_filled,
        "was_filled_within_60s": was_filled_within_60s,
        "partial_fill": partial_fill,
        "slippage_bps_actual": slippage_bps,
    }


# ---------------------------------------------------------------------------
# Frame coercion
# ---------------------------------------------------------------------------


def _full_column_order() -> list[str]:
    return ["fill_id", "paper_order_id", *REQUIRED_COLS]


_FLOAT_COLS: Final[tuple[str, ...]] = (
    "dte",
    "strike_offset_pct",
    "spread_width_bps",
    "oi_at_submit",
    "volume_at_submit",
    "vix",
    "iv_at_submit",
    "requested_mid",
    "filled_price",
    "slippage_bps",
    "time_to_fill_ms",
    "partial_fill_ratio",
    "slippage_bps_actual",
)

_BOOL_COLS: Final[tuple[str, ...]] = (
    "was_filled",
    "was_filled_within_60s",
    "partial_fill",
)

_STRING_COLS: Final[tuple[str, ...]] = (
    "fill_id",
    "paper_order_id",
    "ticker",
    "probe_classification",
)


def _coerce_dataframe(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Build a typed dataframe with stable dtypes for parquet.

    Empty-input still produces a frame with the full column order so
    downstream consumers can iterate without special-casing.
    """

    df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=_full_column_order()
    )
    df = df.reindex(columns=_full_column_order())

    for col in _FLOAT_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")

    for col in _BOOL_COLS:
        if col in df.columns:
            df[col] = df[col].fillna(False).astype(bool)

    for col in _STRING_COLS:
        if col in df.columns:
            df[col] = df[col].astype("string")

    return df


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.training.build_execution_dataset",
        description=(
            "Assemble data/training/execution.parquet from execution_fills "
            "joined with paper_orders + execution_events + liquidity_probes. "
            "Idempotent — safe to re-run."
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DATA_DIR / "alpha_sniper.db",
        help="Path to the SQLite db (default: DATA_DIR/alpha_sniper.db).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=(
            "Path to the output parquet "
            "(default: DATA_DIR/training/execution.parquet)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = build(db_path=args.db, out_path=args.out)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "out_path": str(result.out_path),
                "rows": result.rows,
                "fills_count": result.fills_count,
                "columns": result.columns,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
