"""Tests for ``biotech_sniper.training.build_execution_dataset``.

Each test seeds a hermetic SQLite DB under ``tmp_path`` (full schema
applied via :func:`biotech_sniper.db.run_migrations`) with a
combination of:

* one or more ``paper_orders`` rows (canonical OCC option symbols),
* matching ``execution_events`` (``submitted`` and ``filled`` /
  ``partial_fill``), and
* one or more ``execution_fills`` rows;
* optionally a matching ``liquidity_probes`` row keyed on
  ``(ticker, expiry, strike, date(submitted_at))``.

The builder is then invoked against the test DB and per-test parquet
output, and the parquet + ``v_execution_stats`` view are inspected.

Validation contract assertions exercised:

* **VAL-M5-037** — parquet has all the spec'd columns with the right
  dtypes.
* **VAL-M5-038** — parquet row count == ``execution_fills`` row count.
* **VAL-M5-039** — ``v_execution_stats`` view exists and returns ≥ 1
  row when ≥ 1 fill exists.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from biotech_sniper import db
from biotech_sniper.training import build_execution_dataset as bed


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _seed_paper_order(
    conn: sqlite3.Connection,
    *,
    paper_order_id: str,
    symbol: str,
    qty: int = 2,
    side: str = "buy",
    requested_mid_at_submit: float = 1.50,
    purpose: str = "entry",
    created_at: str = "2026-04-27T15:30:00.000Z",
    client_order_id: str | None = None,
    play_card_id: str | None = None,
    status: str = "filled",
) -> None:
    conn.execute(
        """
        INSERT INTO paper_orders (
            id, play_card_id, alpaca_order_id, symbol, side, qty,
            status, client_order_id, created_at,
            requested_mid_at_submit, purpose
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            paper_order_id,
            play_card_id or f"PC-{paper_order_id}",
            f"alp-{paper_order_id}",
            symbol,
            side,
            qty,
            status,
            client_order_id or f"client-{paper_order_id}",
            created_at,
            requested_mid_at_submit,
            purpose,
        ),
    )


def _seed_event(
    conn: sqlite3.Connection,
    *,
    paper_order_id: str,
    event_type: str,
    event_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO execution_events (
            paper_order_id, event_type, event_at
        ) VALUES (?, ?, ?)
        """,
        (paper_order_id, event_type, event_at),
    )


def _seed_fill(
    conn: sqlite3.Connection,
    *,
    paper_order_id: str,
    filled_at: str,
    filled_price: float,
    filled_qty: int,
    requested_mid_at_submit: float,
    slippage_bps: float,
    slippage_usd: float,
    time_to_fill_ms: int,
    partial_qty_remaining: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO execution_fills (
            paper_order_id, filled_at, filled_price, filled_qty,
            requested_mid_at_submit, slippage_bps, slippage_usd,
            time_to_fill_ms, partial_qty_remaining
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            paper_order_id,
            filled_at,
            filled_price,
            filled_qty,
            requested_mid_at_submit,
            slippage_bps,
            slippage_usd,
            time_to_fill_ms,
            partial_qty_remaining,
        ),
    )


def _seed_probe(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    expiry: str,
    strike: float,
    classification: str,
    submitted_at: str,
    side: str = "buy",
    client_order_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO liquidity_probes (
            ticker, expiry, strike, side, probe_size, submitted_at,
            outcome, classification, client_order_id, cost_usd
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticker,
            expiry,
            strike,
            side,
            1,
            submitted_at,
            "filled",
            classification,
            client_order_id or f"probe-{ticker}-{strike}",
            10.0,
        ),
    )


def _empty_db(tmp_path: Path) -> Path:
    """Initialise a fresh schema-applied DB."""

    db_path = tmp_path / "data" / "alpha_sniper.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
    finally:
        conn.close()
    return db_path


def _seed_minimal_dataset(db_path: Path) -> None:
    """Seed two filled paper_orders + matching events / fills /
    one liquidity probe so a single ticker has 2 fills with one
    matching probe entry.
    """

    conn = sqlite3.connect(db_path)
    try:
        # AXSM 2026-06-20 $125 Call → submitted 2026-04-27, filled
        # within 30s.
        _seed_paper_order(
            conn,
            paper_order_id="po-1",
            symbol="AXSM260620C00125000",
            qty=2,
            requested_mid_at_submit=1.50,
            created_at="2026-04-27T15:30:00.000Z",
            client_order_id="coid-1",
        )
        _seed_event(
            conn,
            paper_order_id="po-1",
            event_type="submitted",
            event_at="2026-04-27T15:30:00.500Z",
        )
        _seed_event(
            conn,
            paper_order_id="po-1",
            event_type="filled",
            event_at="2026-04-27T15:30:30.000Z",
        )
        _seed_fill(
            conn,
            paper_order_id="po-1",
            filled_at="2026-04-27T15:30:30.000Z",
            filled_price=1.55,
            filled_qty=2,
            requested_mid_at_submit=1.50,
            slippage_bps=333.3333,
            slippage_usd=10.0,
            time_to_fill_ms=30_000,
            partial_qty_remaining=0,
        )

        # PFE 2026-05-15 $30 Call → partial fill that takes 75s
        # (above the 60s threshold).
        _seed_paper_order(
            conn,
            paper_order_id="po-2",
            symbol="PFE260515C00030000",
            qty=4,
            requested_mid_at_submit=0.85,
            created_at="2026-04-27T15:31:00.000Z",
            client_order_id="coid-2",
        )
        _seed_event(
            conn,
            paper_order_id="po-2",
            event_type="submitted",
            event_at="2026-04-27T15:31:00.250Z",
        )
        _seed_event(
            conn,
            paper_order_id="po-2",
            event_type="partial_fill",
            event_at="2026-04-27T15:32:15.000Z",
        )
        _seed_fill(
            conn,
            paper_order_id="po-2",
            filled_at="2026-04-27T15:32:15.000Z",
            filled_price=0.90,
            filled_qty=2,
            requested_mid_at_submit=0.85,
            slippage_bps=588.235,
            slippage_usd=10.0,
            time_to_fill_ms=75_000,
            partial_qty_remaining=2,
        )

        # Liquidity probe matching AXSM only (PFE has no probe row).
        _seed_probe(
            conn,
            ticker="AXSM",
            expiry="2026-06-20",
            strike=125.0,
            classification="fillable",
            submitted_at="2026-04-27T15:29:00.000Z",
        )

        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_parquet_has_required_columns_and_dtypes(tmp_path: Path) -> None:
    """VAL-M5-037: parquet schema must include every spec'd column."""

    db_path = _empty_db(tmp_path)
    _seed_minimal_dataset(db_path)
    out_path = tmp_path / "data" / "training" / "execution.parquet"

    result = bed.build(db_path=db_path, out_path=out_path)

    assert result.rows == 2
    assert out_path.exists()

    table = pq.read_table(out_path)
    schema_names = set(table.schema.names)
    missing = set(bed.REQUIRED_COLS) - schema_names
    assert not missing, f"missing required columns: {missing}"

    df = table.to_pandas()
    for col in (
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
    ):
        assert pd.api.types.is_float_dtype(df[col]), (
            f"{col} should be float64, got {df[col].dtype}"
        )
    for col in ("was_filled", "was_filled_within_60s", "partial_fill"):
        assert pd.api.types.is_bool_dtype(df[col]), (
            f"{col} should be bool, got {df[col].dtype}"
        )


def test_parquet_row_count_matches_execution_fills(tmp_path: Path) -> None:
    """VAL-M5-038: parquet row count == execution_fills row count."""

    db_path = _empty_db(tmp_path)
    _seed_minimal_dataset(db_path)
    out_path = tmp_path / "data" / "training" / "execution.parquet"

    result = bed.build(db_path=db_path, out_path=out_path)

    conn = sqlite3.connect(db_path)
    try:
        fill_count = conn.execute(
            "SELECT COUNT(*) FROM execution_fills"
        ).fetchone()[0]
    finally:
        conn.close()

    assert pq.read_table(out_path).num_rows == fill_count
    assert result.rows == fill_count
    assert result.fills_count == fill_count


def test_v_execution_stats_returns_rows_when_fills_exist(tmp_path: Path) -> None:
    """VAL-M5-039: ``v_execution_stats`` view exists and aggregates per
    ticker. Returns ≥ 1 row whenever ≥ 1 fill exists.
    """

    db_path = _empty_db(tmp_path)
    _seed_minimal_dataset(db_path)
    out_path = tmp_path / "data" / "training" / "execution.parquet"

    bed.build(db_path=db_path, out_path=out_path)

    conn = sqlite3.connect(db_path)
    try:
        # View definition exists.
        master_row = conn.execute(
            "SELECT type, sql FROM sqlite_master "
            "WHERE type='view' AND name='v_execution_stats'"
        ).fetchone()
        assert master_row is not None, "v_execution_stats view missing"
        assert "CREATE VIEW" in master_row[1].upper()

        # Required column set.
        cols = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(v_execution_stats)"
            ).fetchall()
        }
        for required in (
            "ticker",
            "mean_slippage_bps",
            "p95_slippage_bps",
            "fill_rate",
            "partial_fill_rate",
            "mean_time_to_fill_ms",
            "n_orders",
        ):
            assert required in cols, f"v_execution_stats missing {required}"

        rows = list(
            conn.execute(
                "SELECT ticker, mean_slippage_bps, fill_rate, "
                "partial_fill_rate, n_orders FROM v_execution_stats "
                "ORDER BY ticker"
            )
        )
    finally:
        conn.close()

    assert len(rows) >= 1, "expected ≥ 1 aggregated row"
    tickers = {r[0] for r in rows}
    assert tickers == {"AXSM", "PFE"}

    by_ticker = {r[0]: r for r in rows}
    # AXSM: one fully filled order → fill_rate == 1.0,
    # partial_fill_rate == 0.0, mean_slippage_bps == ~333.33.
    assert by_ticker["AXSM"][2] == pytest.approx(1.0)
    assert by_ticker["AXSM"][3] == pytest.approx(0.0)
    assert by_ticker["AXSM"][4] == 1
    # PFE: one order with a partial fill remaining → partial_fill_rate
    # == 1.0 (the order had a partial fill), fill_rate == 0.0
    # (no fully-filled fill rows for that order).
    assert by_ticker["PFE"][3] == pytest.approx(1.0)
    assert by_ticker["PFE"][4] == 1


def test_probe_left_join_matches_by_ticker_expiry_strike_and_date(
    tmp_path: Path,
) -> None:
    """``probe_classification`` is sourced from ``liquidity_probes`` via
    a LEFT JOIN on ``(ticker, expiry, strike, date(submitted_at))``.
    Rows without a matching probe row come back as NaN.
    """

    db_path = _empty_db(tmp_path)
    _seed_minimal_dataset(db_path)
    out_path = tmp_path / "data" / "training" / "execution.parquet"

    bed.build(db_path=db_path, out_path=out_path)

    df = pq.read_table(out_path).to_pandas()
    df = df.set_index("ticker")

    assert df.loc["AXSM", "probe_classification"] == "fillable"
    # PFE has no probe row at the same (ticker, expiry, strike, date)
    # tuple → expect NaN.
    pfe_value = df.loc["PFE", "probe_classification"]
    assert pfe_value is None or (
        isinstance(pfe_value, float) and math.isnan(pfe_value)
    ) or (pd.isna(pfe_value))


def test_targets_and_derived_metrics_are_consistent(tmp_path: Path) -> None:
    """Spot-check the derived columns:

    * ``was_filled`` is True for every emitted row.
    * ``was_filled_within_60s`` follows the 60_000 ms threshold.
    * ``partial_fill`` follows ``partial_qty_remaining > 0``.
    * ``partial_fill_ratio == filled_qty / order_qty``.
    * ``slippage_bps_actual == slippage_bps``.
    * ``dte`` matches ``(expiry - submitted_date).days``.
    """

    db_path = _empty_db(tmp_path)
    _seed_minimal_dataset(db_path)
    out_path = tmp_path / "data" / "training" / "execution.parquet"

    bed.build(db_path=db_path, out_path=out_path)

    df = pq.read_table(out_path).to_pandas().set_index("ticker")

    # Identical slippage_bps_actual / slippage_bps.
    assert (df["slippage_bps_actual"] == df["slippage_bps"]).all()

    # AXSM filled in 30s → within 60s; PFE in 75s → outside 60s.
    assert bool(df.loc["AXSM", "was_filled_within_60s"]) is True
    assert bool(df.loc["PFE", "was_filled_within_60s"]) is False

    # AXSM was a clean fill (no partial); PFE had partial_qty_remaining=2.
    assert bool(df.loc["AXSM", "partial_fill"]) is False
    assert bool(df.loc["PFE", "partial_fill"]) is True

    # AXSM filled 2/2 → 1.0; PFE filled 2/4 → 0.5.
    assert df.loc["AXSM", "partial_fill_ratio"] == pytest.approx(1.0)
    assert df.loc["PFE", "partial_fill_ratio"] == pytest.approx(0.5)

    # was_filled is always True.
    assert df["was_filled"].all()

    # dte: AXSM 2026-06-20 - 2026-04-27 = 54 days;
    #      PFE  2026-05-15 - 2026-04-27 = 18 days.
    assert df.loc["AXSM", "dte"] == pytest.approx(54.0)
    assert df.loc["PFE", "dte"] == pytest.approx(18.0)


def test_build_is_idempotent(tmp_path: Path) -> None:
    """Re-running the builder produces an identical parquet."""

    db_path = _empty_db(tmp_path)
    _seed_minimal_dataset(db_path)
    out_path = tmp_path / "data" / "training" / "execution.parquet"

    first = bed.build(db_path=db_path, out_path=out_path)
    df_first = pq.read_table(out_path).to_pandas()

    second = bed.build(db_path=db_path, out_path=out_path)
    df_second = pq.read_table(out_path).to_pandas()

    assert first.rows == second.rows
    pd.testing.assert_frame_equal(df_first, df_second)

    # The view is also idempotent — re-running build does not duplicate
    # ``v_execution_stats``.
    conn = sqlite3.connect(db_path)
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='view' AND name='v_execution_stats'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert cnt == 1


def test_empty_db_produces_empty_parquet_with_schema(tmp_path: Path) -> None:
    """When no fills have been recorded the builder still emits a
    parquet with the full column set (no rows).
    """

    db_path = _empty_db(tmp_path)
    out_path = tmp_path / "data" / "training" / "execution.parquet"

    result = bed.build(db_path=db_path, out_path=out_path)

    assert result.rows == 0
    assert result.fills_count == 0

    table = pq.read_table(out_path)
    assert table.num_rows == 0
    schema_names = set(table.schema.names)
    missing = set(bed.REQUIRED_COLS) - schema_names
    assert not missing, f"missing columns in empty parquet: {missing}"


def test_cli_main_runs_and_emits_json(tmp_path: Path, capsys) -> None:
    """The CLI entrypoint exits 0 and prints a JSON summary."""

    db_path = _empty_db(tmp_path)
    _seed_minimal_dataset(db_path)
    out_path = tmp_path / "data" / "training" / "execution.parquet"

    rc = bed.main(["--db", str(db_path), "--out", str(out_path)])
    assert rc == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["rows"] == 2
    assert payload["fills_count"] == 2
    assert payload["out_path"] == str(out_path)
    assert "ticker" in payload["columns"]
