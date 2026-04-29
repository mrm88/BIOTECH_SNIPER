"""Schema-drift assertions for the IWM importer (VAL-M1-055 / VAL-M1-056).

Two contract assertions land here:

* VAL-M1-055 — feeding a CSV whose header lacks a required column
  (``Ticker``, ``Asset Class``, or ``Weight (%)``) makes the importer
  raise :class:`IWMSchemaError`. The error message MUST name the
  missing column literally so operators can diff it against the
  upstream header. NO partial snapshot row is written.
* VAL-M1-056 — given a CSV that opens with the iShares preamble,
  carries a UTF-8 BOM, and places the canonical ``Ticker,Name,...``
  header on row ~10, the importer correctly identifies the header,
  parses ≥ 1 equity row, and never includes any preamble string
  (``Fund Holdings as of``, ``Inception Date``, etc.) as a ticker.

Fixtures live under ``tests/fixtures/iwm/``:

* ``iwm_missing_asset_class.csv`` — header with ``Asset Class`` removed.
* ``iwm_renamed_ticker.csv`` — header with ``Ticker`` renamed to ``Symbol``.
* ``iwm_with_bom.csv`` — happy-path fixture prefixed with the UTF-8 BOM.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from biotech_sniper.universe import iwm_importer
from biotech_sniper.universe.iwm_importer import (
    IWMSchemaError,
    import_iwm_holdings,
    parse_iwm_csv,
)


FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "iwm"


# ---------------------------------------------------------------------------
# VAL-M1-055: missing column → IWMSchemaError, no partial commit
# ---------------------------------------------------------------------------


def test_missing_column_fails_loud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """``Asset Class`` removed → IWMSchemaError mentions the column."""
    body = (FIXTURE_DIR / "iwm_missing_asset_class.csv").read_bytes()
    db_path = tmp_path / "alpha_sniper.db"

    # Pre-condition: snapshot table is empty.
    conn = sqlite3.connect(db_path)
    conn.execute(iwm_importer._IWM_SNAPSHOT_DDL)
    conn.commit()
    pre_count = conn.execute(
        "SELECT COUNT(*) FROM iwm_holdings_snapshot"
    ).fetchone()[0]
    conn.close()

    with pytest.raises(IWMSchemaError) as excinfo:
        parse_iwm_csv(body)
    assert "Asset Class" in str(excinfo.value)

    # End-to-end: no partial commit even when the importer wraps parse.
    fixture_path = FIXTURE_DIR / "iwm_missing_asset_class.csv"
    with pytest.raises(IWMSchemaError):
        import_iwm_holdings(
            db_path=db_path,
            csv_path=fixture_path,
            max_age_hours=0,
        )

    conn = sqlite3.connect(db_path)
    try:
        post_count = conn.execute(
            "SELECT COUNT(*) FROM iwm_holdings_snapshot"
        ).fetchone()[0]
    finally:
        conn.close()
    assert post_count == pre_count == 0


def test_renamed_ticker_column_fails_loud():
    """``Ticker`` renamed to ``Symbol`` → IWMSchemaError names ``Ticker``."""
    body = (FIXTURE_DIR / "iwm_renamed_ticker.csv").read_bytes()
    with pytest.raises(IWMSchemaError) as excinfo:
        parse_iwm_csv(body)
    msg = str(excinfo.value)
    assert "Ticker" in msg


# ---------------------------------------------------------------------------
# VAL-M1-056: preamble + UTF-8 BOM tolerated
# ---------------------------------------------------------------------------


def test_preamble_and_bom_handled():
    """Preamble rows skipped + UTF-8 BOM stripped + header located."""
    body = (FIXTURE_DIR / "iwm_with_bom.csv").read_bytes()
    # Sanity: fixture really has the BOM.
    assert body[:3] == b"\xef\xbb\xbf"

    rows, stats = parse_iwm_csv(body)
    tickers = [r.ticker for r in rows]

    # Parsed equity rows match the happy-path fixture.
    assert tickers == ["VRTX", "IDYA", "AAPL", "NVAX", "AXSM"]
    assert stats["equity_rows"] == 5

    # No preamble string snuck in as a ticker.
    forbidden = {
        "Fund Holdings as of",
        "Inception Date",
        "Shares Outstanding",
        "Stock",
        "Bond",
        "Cash",
        "Other",
        "iShares Russell 2000 ETF",
    }
    assert set(tickers).isdisjoint(forbidden)
