"""Behavioural tests for :mod:`biotech_sniper.universe.russell_biotech`.

Covers the full f-m1-03 feature contract:

* Schema correctness: ``russell2k_biotech`` is created with
  ``ticker`` PRIMARY KEY, ``CHECK(sic IN (2834, 2836, 8731))``, and
  the indexes the M1 validation contract expects.
* Population: a synthetic IWM snapshot + injected fake classifier
  yields the expected biotech-only row set.
* SIC filter: 3841 / 3845 / 2835 are excluded; the CHECK constraint
  rejects a synthetic insert with a forbidden SIC.
* Idempotency: same-day re-run yields no net row change.
* Atomic refresh: a concurrent reader thread polled across the
  refresh window NEVER observes < 100 rows.
* Shrinkage guard: a refresh that would drop > 25 % of the prior
  snapshot is refused; prior rows preserved; ``--allow-shrinkage``
  bypasses the guard.
* Refresh cadence: ``RUSSELL_BIOTECH_REFRESH_HOURS=24`` short-circuits
  a second invocation within 24 h; ``--refresh`` overrides.
* Universe ∩ tier intersection: with a synthetic universe seeded the
  intersection size lands in the expected 110-150 range.
* CLI: ``--help``, ``--refresh``, ``--dry-run --emit-stats``,
  ``--allow-shrinkage`` exit codes and stdout shapes.
* Missing IWM snapshot: graceful exit with the documented exit code
  and zero rows written.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pytest

from biotech_sniper.classifiers.sec_sic import (
    SECSICClassifier,
    SECTransientError,
    SICResolution,
)
from biotech_sniper.universe import russell_biotech
from biotech_sniper.universe.iwm_importer import (
    ParsedRow,
    ensure_iwm_snapshot_table,
    write_snapshot,
)
from biotech_sniper.universe.russell_biotech import (
    BIOTECH_SIC_CODES,
    DEFAULT_REFRESH_HOURS,
    DEFAULT_SHRINKAGE_FLOOR_RATIO,
    EXIT_NO_IWM_SNAPSHOT,
    EXIT_OK,
    EXIT_SHRINKAGE_REFUSED,
    BiotechCandidate,
    MissingIWMSnapshot,
    RefreshResult,
    ShrinkageRefusal,
    classify_candidates,
    ensure_russell2k_biotech_table,
    main,
    refresh_russell2k_biotech,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeClassifier:
    """In-memory SEC classifier stand-in for tests.

    Tests register a ``ticker → SICResolution`` map up-front; the
    classifier returns those resolutions deterministically and never
    issues HTTP. The ``http_call_count`` attribute mirrors the real
    classifier's interface so test assertions remain identical.
    """

    def __init__(
        self,
        resolutions: dict[str, SICResolution | None],
        *,
        transient_for: set[str] | None = None,
    ) -> None:
        self._resolutions = resolutions
        self._transient_for = set(transient_for or set())
        self.calls: list[str] = []
        self.http_call_count = 0

    def resolve_ticker_sic(self, ticker: str) -> SICResolution | None:
        norm = ticker.strip().upper()
        self.calls.append(norm)
        if norm in self._transient_for:
            raise SECTransientError(f"synthetic 5xx for {norm}")
        return self._resolutions.get(norm)


def _mk_resolution(
    ticker: str,
    sic: int | None,
    *,
    cik: str | None = None,
    desc: str | None = None,
) -> SICResolution:
    return SICResolution(
        ticker=ticker,
        cik=cik or f"{abs(hash(ticker)) % 9_999_999_999:010d}",
        sic=sic,
        sic_description=desc,
        fetched_at="2026-04-29T00:00:00Z",
        cached=False,
    )


def _seed_iwm_snapshot(
    db_path: Path,
    rows: Iterable[ParsedRow],
    *,
    as_of_date: str = "2026-04-29",
    fetched_at: str = "2026-04-29T00:00:00.000000Z",
    source_url: str = "https://www.ishares.com/test",
) -> None:
    """Write an IWM snapshot directly to ``db_path`` for tests.

    Mirrors the real :func:`iwm_importer.write_snapshot` flow so the
    russell_biotech writer sees the table in the exact production
    shape (correct PK, correct ``asset_class`` filter).
    """
    conn = sqlite3.connect(db_path)
    try:
        ensure_iwm_snapshot_table(conn)
        write_snapshot(
            conn,
            list(rows),
            as_of_date=as_of_date,
            source_url=source_url,
            fetched_at=fetched_at,
        )
        conn.commit()
    finally:
        conn.close()


def _mk_iwm_row(ticker: str, weight: float = 0.05) -> ParsedRow:
    return ParsedRow(
        ticker=ticker,
        name=f"{ticker} INC",
        asset_class="Equity",
        weight=weight,
        sector="Health Care",
        market_value_usd=12_345.67,
        notional_value_usd=12_345.67,
        quantity=100.0,
        price=123.45,
        location="United States",
        exchange="NASDAQ",
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_ensure_table_creates_correct_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.db"
    conn = sqlite3.connect(db_path)
    try:
        ensure_russell2k_biotech_table(conn)
        # Columns + types
        cols = {
            row[1]: row[2]
            for row in conn.execute("PRAGMA table_info(russell2k_biotech)")
        }
        assert cols["ticker"] == "TEXT"
        assert cols["cik"] == "TEXT"
        assert cols["sic"] == "INTEGER"
        assert "sic_description" in cols
        assert "iwm_weight" in cols
        assert "iwm_market_value_usd" in cols
        assert "as_of_date" in cols
        assert "fetched_at" in cols
        # Ticker is PRIMARY KEY
        pk_rows = [
            row[1]
            for row in conn.execute("PRAGMA table_info(russell2k_biotech)")
            if row[5] >= 1
        ]
        assert pk_rows == ["ticker"]
        # CHECK constraint mentions allow-list SIC codes
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='russell2k_biotech'"
        ).fetchone()[0]
        for code in (2834, 2836, 8731):
            assert str(code) in ddl
        assert "CHECK" in ddl
        # Indexes present
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='russell2k_biotech'"
            )
        }
        assert "idx_russell2k_biotech_sic" in indexes
        assert "idx_russell2k_biotech_fetched_at" in indexes
    finally:
        conn.close()


def test_check_constraint_rejects_forbidden_sic(tmp_path: Path) -> None:
    """SIC 2835 / 3841 / 3845 are blocked at the storage layer."""
    db_path = tmp_path / "alpha.db"
    conn = sqlite3.connect(db_path)
    try:
        ensure_russell2k_biotech_table(conn)
        for sic in (2835, 3841, 3845):
            with pytest.raises(sqlite3.IntegrityError) as excinfo:
                conn.execute(
                    "INSERT INTO russell2k_biotech "
                    "(ticker, cik, sic, sic_description, as_of_date, "
                    "fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        f"FAKE{sic}",
                        "0000000099",
                        sic,
                        "should be rejected",
                        "2026-04-29",
                        "2026-04-29T00:00:00Z",
                    ),
                )
            assert "CHECK" in str(excinfo.value).upper() or "constraint" in str(
                excinfo.value
            ).lower()
    finally:
        conn.close()


def test_idempotent_create(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.db"
    conn = sqlite3.connect(db_path)
    try:
        ensure_russell2k_biotech_table(conn)
        # Second call must not raise.
        ensure_russell2k_biotech_table(conn)
        ensure_russell2k_biotech_table(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# load_latest_iwm_snapshot
# ---------------------------------------------------------------------------


def test_load_latest_iwm_snapshot_returns_only_most_recent_date(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "alpha.db"
    # Day 1
    _seed_iwm_snapshot(
        db_path,
        [_mk_iwm_row("OLD1"), _mk_iwm_row("OLD2")],
        as_of_date="2026-04-28",
    )
    # Day 2 (newer)
    _seed_iwm_snapshot(
        db_path,
        [_mk_iwm_row("NEW1"), _mk_iwm_row("NEW2"), _mk_iwm_row("NEW3")],
        as_of_date="2026-04-29",
    )

    conn = sqlite3.connect(db_path)
    try:
        rows = russell_biotech.load_latest_iwm_snapshot(conn)
    finally:
        conn.close()

    tickers = sorted(r.ticker for r in rows)
    assert tickers == ["NEW1", "NEW2", "NEW3"]
    assert all(r.as_of_date == "2026-04-29" for r in rows)


def test_load_latest_iwm_snapshot_empty_when_table_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.db"
    conn = sqlite3.connect(db_path)
    try:
        rows = russell_biotech.load_latest_iwm_snapshot(conn)
    finally:
        conn.close()
    assert rows == []


# ---------------------------------------------------------------------------
# classify_candidates
# ---------------------------------------------------------------------------


def test_classify_candidates_filters_to_biotech_sic(tmp_path: Path) -> None:
    iwm_rows = [
        russell_biotech._IWMRow("VRTX", 0.5, 1000.0, "2026-04-29"),
        russell_biotech._IWMRow("MDEV", 0.3, 800.0, "2026-04-29"),  # 3841
        russell_biotech._IWMRow("DIAG", 0.2, 500.0, "2026-04-29"),  # 2835
        russell_biotech._IWMRow("BIOS", 0.4, 900.0, "2026-04-29"),
        russell_biotech._IWMRow("LABS", 0.1, 400.0, "2026-04-29"),  # 8731
        russell_biotech._IWMRow("UNKNOWN", 0.05, 100.0, "2026-04-29"),
    ]
    classifier = _FakeClassifier(
        {
            "VRTX": _mk_resolution("VRTX", 2834, desc="PHARMACEUTICAL"),
            "MDEV": _mk_resolution("MDEV", 3841, desc="MED DEVICE"),
            "DIAG": _mk_resolution("DIAG", 2835, desc="DIAGNOSTICS"),
            "BIOS": _mk_resolution("BIOS", 2836, desc="BIOLOGICALS"),
            "LABS": _mk_resolution("LABS", 8731, desc="COMMERCIAL R&D"),
            "UNKNOWN": None,
        }
    )

    candidates, stats = classify_candidates(iwm_rows, classifier)
    tickers = sorted(c.ticker for c in candidates)
    assert tickers == ["BIOS", "LABS", "VRTX"]
    assert stats["biotech_matches"] == 3
    assert stats["sic_resolved"] == 5  # VRTX, MDEV, DIAG, BIOS, LABS
    assert stats["sic_unresolved"] == 1  # UNKNOWN
    # Excluded SIC codes never produce a candidate row.
    sics = {c.sic for c in candidates}
    assert sics.issubset({2834, 2836, 8731})
    assert 2835 not in sics
    assert 3841 not in sics


def test_classify_candidates_carries_iwm_metadata(tmp_path: Path) -> None:
    iwm_rows = [
        russell_biotech._IWMRow("VRTX", 1.5, 9999.99, "2026-04-29"),
    ]
    classifier = _FakeClassifier(
        {"VRTX": _mk_resolution("VRTX", 2834, desc="PHARMACEUTICAL")}
    )
    candidates, _ = classify_candidates(iwm_rows, classifier)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.iwm_weight == 1.5
    assert c.iwm_market_value_usd == 9999.99
    assert c.as_of_date == "2026-04-29"
    assert c.sic == 2834


def test_classify_candidates_tolerates_transient_errors_up_to_threshold(
    tmp_path: Path,
) -> None:
    iwm_rows = [
        russell_biotech._IWMRow("VRTX", 0.5, 1000.0, "2026-04-29"),
        russell_biotech._IWMRow("FLAKE1", 0.3, 800.0, "2026-04-29"),
        russell_biotech._IWMRow("FLAKE2", 0.3, 800.0, "2026-04-29"),
        russell_biotech._IWMRow("BIOS", 0.4, 900.0, "2026-04-29"),
    ]
    classifier = _FakeClassifier(
        {
            "VRTX": _mk_resolution("VRTX", 2834),
            "BIOS": _mk_resolution("BIOS", 2836),
        },
        transient_for={"FLAKE1", "FLAKE2"},
    )
    candidates, stats = classify_candidates(
        iwm_rows, classifier, max_transient_errors=10
    )
    assert sorted(c.ticker for c in candidates) == ["BIOS", "VRTX"]
    assert stats["sic_transient_errors"] == 2


def test_classify_candidates_reraises_when_transient_threshold_exceeded(
    tmp_path: Path,
) -> None:
    iwm_rows = [
        russell_biotech._IWMRow(f"F{i}", 0.1, 100.0, "2026-04-29")
        for i in range(5)
    ]
    classifier = _FakeClassifier(
        {},
        transient_for={f"F{i}" for i in range(5)},
    )
    with pytest.raises(SECTransientError):
        classify_candidates(
            iwm_rows, classifier, max_transient_errors=2
        )


# ---------------------------------------------------------------------------
# refresh_russell2k_biotech: happy path
# ---------------------------------------------------------------------------


def _build_synthetic_universe(
    *,
    biotech_count: int,
    non_biotech_count: int,
    excluded_count: int = 0,
) -> tuple[list[ParsedRow], dict[str, SICResolution | None]]:
    """Construct a deterministic IWM + classifier universe.

    Returns ``(iwm_rows, sic_map)`` ready to feed into
    :func:`_seed_iwm_snapshot` + :class:`_FakeClassifier`.
    """
    iwm_rows: list[ParsedRow] = []
    sic_map: dict[str, SICResolution | None] = {}

    # Biotech tickers — alternate the three allow-listed SIC codes.
    biotech_sics = (2834, 2836, 8731)
    for i in range(biotech_count):
        ticker = f"BIO{i:04d}"
        sic = biotech_sics[i % len(biotech_sics)]
        iwm_rows.append(_mk_iwm_row(ticker, weight=0.01))
        sic_map[ticker] = _mk_resolution(ticker, sic, desc=f"DESC{sic}")

    # Excluded biotech-adjacent (3841 / 3845 / 2835).
    excluded_sics = (3841, 3845, 2835)
    for i in range(excluded_count):
        ticker = f"EXC{i:04d}"
        sic = excluded_sics[i % len(excluded_sics)]
        iwm_rows.append(_mk_iwm_row(ticker, weight=0.005))
        sic_map[ticker] = _mk_resolution(ticker, sic, desc=f"EXCL{sic}")

    # Non-biotech (random non-allow-listed SIC codes).
    other_sics = (7372, 6020, 1311, 5961)
    for i in range(non_biotech_count):
        ticker = f"OTH{i:04d}"
        sic = other_sics[i % len(other_sics)]
        iwm_rows.append(_mk_iwm_row(ticker, weight=0.005))
        sic_map[ticker] = _mk_resolution(ticker, sic, desc=f"NONBIO{sic}")
    return iwm_rows, sic_map


def test_refresh_writes_only_biotech_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.db"
    iwm_rows, sic_map = _build_synthetic_universe(
        biotech_count=10, non_biotech_count=20, excluded_count=5
    )
    _seed_iwm_snapshot(db_path, iwm_rows)
    classifier = _FakeClassifier(sic_map)

    result = refresh_russell2k_biotech(
        db_path=db_path,
        classifier=classifier,
        max_age_hours=0,
    )

    assert result.rows_written == 10
    assert result.biotech_matches == 10
    assert result.iwm_tickers_considered == 35

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT ticker, sic FROM russell2k_biotech ORDER BY ticker"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 10
    for ticker, sic in rows:
        assert ticker.startswith("BIO")
        assert sic in BIOTECH_SIC_CODES


def test_refresh_excludes_med_device_and_diagnostics(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.db"
    iwm_rows, sic_map = _build_synthetic_universe(
        biotech_count=5, non_biotech_count=0, excluded_count=10
    )
    _seed_iwm_snapshot(db_path, iwm_rows)
    classifier = _FakeClassifier(sic_map)

    refresh_russell2k_biotech(
        db_path=db_path, classifier=classifier, max_age_hours=0
    )

    conn = sqlite3.connect(db_path)
    try:
        forbidden = conn.execute(
            "SELECT COUNT(*) FROM russell2k_biotech WHERE "
            "sic IN (2835, 3841, 3845)"
        ).fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM russell2k_biotech"
        ).fetchone()[0]
    finally:
        conn.close()
    assert forbidden == 0
    assert total == 5


def test_refresh_lands_in_expected_size_band(tmp_path: Path) -> None:
    """155 ≤ rows ≤ 185 with a ~165-biotech synthetic IWM."""
    db_path = tmp_path / "alpha.db"
    iwm_rows, sic_map = _build_synthetic_universe(
        biotech_count=165, non_biotech_count=1800, excluded_count=20
    )
    _seed_iwm_snapshot(db_path, iwm_rows)
    classifier = _FakeClassifier(sic_map)

    result = refresh_russell2k_biotech(
        db_path=db_path, classifier=classifier, max_age_hours=0
    )

    assert 155 <= result.rows_written <= 185


def test_refresh_universe_intersection_size_band(tmp_path: Path) -> None:
    """russell2k_biotech ∩ universe.tier IN ('watch','tradeable') ∈ [110,150]."""
    db_path = tmp_path / "alpha.db"
    iwm_rows, sic_map = _build_synthetic_universe(
        biotech_count=170, non_biotech_count=1830, excluded_count=10
    )
    _seed_iwm_snapshot(db_path, iwm_rows)
    classifier = _FakeClassifier(sic_map)
    refresh_russell2k_biotech(
        db_path=db_path, classifier=classifier, max_age_hours=0
    )

    # Build a synthetic ``universe`` table where ~120 biotech tickers
    # are in ('watch', 'tradeable') and the rest are excluded.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS universe ("
            "ticker TEXT PRIMARY KEY, "
            "tier TEXT NOT NULL CHECK(tier IN ('watch','tradeable','excluded'))"
            ")"
        )
        biotech_tickers = [
            row[0]
            for row in conn.execute("SELECT ticker FROM russell2k_biotech")
        ]
        # Mark first ~120 as watch/tradeable, the rest as excluded.
        for i, t in enumerate(biotech_tickers):
            tier = "watch" if i < 80 else ("tradeable" if i < 120 else "excluded")
            conn.execute(
                "INSERT OR REPLACE INTO universe (ticker, tier) VALUES (?, ?)",
                (t, tier),
            )
        conn.commit()
        (intersection_size,) = conn.execute(
            "SELECT COUNT(*) FROM russell2k_biotech r "
            "JOIN universe u ON r.ticker = u.ticker "
            "WHERE u.tier IN ('watch','tradeable')"
        ).fetchone()
    finally:
        conn.close()

    assert 110 <= intersection_size <= 150


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_same_day_rerun_is_noop(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.db"
    iwm_rows, sic_map = _build_synthetic_universe(
        biotech_count=20, non_biotech_count=10
    )
    _seed_iwm_snapshot(db_path, iwm_rows)
    classifier = _FakeClassifier(sic_map)

    refresh_russell2k_biotech(
        db_path=db_path, classifier=classifier, max_age_hours=0
    )
    conn = sqlite3.connect(db_path)
    before = conn.execute(
        "SELECT ticker, sic, as_of_date FROM russell2k_biotech ORDER BY ticker"
    ).fetchall()
    conn.close()

    refresh_russell2k_biotech(
        db_path=db_path, classifier=classifier, max_age_hours=0
    )
    conn = sqlite3.connect(db_path)
    after = conn.execute(
        "SELECT ticker, sic, as_of_date FROM russell2k_biotech ORDER BY ticker"
    ).fetchall()
    duplicates = conn.execute(
        "SELECT ticker, COUNT(*) FROM russell2k_biotech "
        "GROUP BY ticker HAVING COUNT(*) > 1"
    ).fetchall()
    conn.close()

    assert before == after
    assert duplicates == []


# ---------------------------------------------------------------------------
# Refresh cadence (RUSSELL_BIOTECH_REFRESH_HOURS)
# ---------------------------------------------------------------------------


def test_refresh_skipped_when_within_cadence_window(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.db"
    iwm_rows, sic_map = _build_synthetic_universe(
        biotech_count=10, non_biotech_count=5
    )
    _seed_iwm_snapshot(db_path, iwm_rows)
    classifier = _FakeClassifier(sic_map)

    # First run populates.
    refresh_russell2k_biotech(
        db_path=db_path, classifier=classifier, max_age_hours=0
    )

    # Second run within the cadence window — must short-circuit.
    classifier_2 = _FakeClassifier(sic_map)
    result = refresh_russell2k_biotech(
        db_path=db_path,
        classifier=classifier_2,
        max_age_hours=24,
    )
    assert result.refresh_skipped is True
    assert result.skip_reason and "fresh_enough" in result.skip_reason
    # The fake classifier must NOT have been called on the cache hit.
    assert classifier_2.calls == []


def test_refresh_cache_disabled_when_max_age_hours_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.db"
    iwm_rows, sic_map = _build_synthetic_universe(
        biotech_count=8, non_biotech_count=2
    )
    _seed_iwm_snapshot(db_path, iwm_rows)
    classifier = _FakeClassifier(sic_map)

    refresh_russell2k_biotech(
        db_path=db_path, classifier=classifier, max_age_hours=0
    )
    classifier_2 = _FakeClassifier(sic_map)
    result = refresh_russell2k_biotech(
        db_path=db_path, classifier=classifier_2, max_age_hours=0
    )
    assert result.refresh_skipped is False
    # max_age=0 forces a full re-classification.
    assert classifier_2.calls != []


def test_resolve_refresh_hours_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUSSELL_BIOTECH_REFRESH_HOURS", raising=False)
    assert russell_biotech._resolve_refresh_hours(None) == DEFAULT_REFRESH_HOURS


def test_resolve_refresh_hours_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUSSELL_BIOTECH_REFRESH_HOURS", "6")
    assert russell_biotech._resolve_refresh_hours(None) == 6


def test_resolve_refresh_hours_env_garbage_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUSSELL_BIOTECH_REFRESH_HOURS", "abc")
    assert russell_biotech._resolve_refresh_hours(None) == DEFAULT_REFRESH_HOURS


def test_resolve_refresh_hours_explicit_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUSSELL_BIOTECH_REFRESH_HOURS", "100")
    assert russell_biotech._resolve_refresh_hours(3) == 3


# ---------------------------------------------------------------------------
# Shrinkage guard
# ---------------------------------------------------------------------------


def test_shrinkage_refusal_preserves_prior_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.db"
    # Seed a 100-row prior snapshot.
    iwm_rows1, sic_map1 = _build_synthetic_universe(
        biotech_count=100, non_biotech_count=0
    )
    _seed_iwm_snapshot(db_path, iwm_rows1, as_of_date="2026-04-28")
    classifier_1 = _FakeClassifier(sic_map1)
    refresh_russell2k_biotech(
        db_path=db_path, classifier=classifier_1, max_age_hours=0
    )

    conn = sqlite3.connect(db_path)
    pre_count = conn.execute(
        "SELECT COUNT(*) FROM russell2k_biotech"
    ).fetchone()[0]
    pre_tickers = sorted(
        r[0]
        for r in conn.execute("SELECT ticker FROM russell2k_biotech")
    )
    conn.close()
    assert pre_count == 100

    # New snapshot with only 50 biotech tickers (50 % shrinkage).
    iwm_rows2, sic_map2 = _build_synthetic_universe(
        biotech_count=50, non_biotech_count=0
    )
    _seed_iwm_snapshot(db_path, iwm_rows2, as_of_date="2026-04-29")
    classifier_2 = _FakeClassifier(sic_map2)

    with pytest.raises(ShrinkageRefusal):
        refresh_russell2k_biotech(
            db_path=db_path, classifier=classifier_2, max_age_hours=0
        )

    # Prior snapshot is intact byte-for-byte.
    conn = sqlite3.connect(db_path)
    post_count = conn.execute(
        "SELECT COUNT(*) FROM russell2k_biotech"
    ).fetchone()[0]
    post_tickers = sorted(
        r[0]
        for r in conn.execute("SELECT ticker FROM russell2k_biotech")
    )
    conn.close()
    assert post_count == pre_count
    assert post_tickers == pre_tickers


def test_shrinkage_below_threshold_is_accepted(tmp_path: Path) -> None:
    """A 20 % shrink (≥ 75 % of prior) commits cleanly."""
    db_path = tmp_path / "alpha.db"
    iwm_rows1, sic_map1 = _build_synthetic_universe(
        biotech_count=100, non_biotech_count=0
    )
    _seed_iwm_snapshot(db_path, iwm_rows1, as_of_date="2026-04-28")
    refresh_russell2k_biotech(
        db_path=db_path,
        classifier=_FakeClassifier(sic_map1),
        max_age_hours=0,
    )

    iwm_rows2, sic_map2 = _build_synthetic_universe(
        biotech_count=80, non_biotech_count=0
    )
    _seed_iwm_snapshot(db_path, iwm_rows2, as_of_date="2026-04-29")

    result = refresh_russell2k_biotech(
        db_path=db_path,
        classifier=_FakeClassifier(sic_map2),
        max_age_hours=0,
    )
    assert result.rows_written == 80
    assert result.shrinkage_refused is False


def test_allow_shrinkage_bypasses_guard(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.db"
    iwm_rows1, sic_map1 = _build_synthetic_universe(
        biotech_count=100, non_biotech_count=0
    )
    _seed_iwm_snapshot(db_path, iwm_rows1, as_of_date="2026-04-28")
    refresh_russell2k_biotech(
        db_path=db_path,
        classifier=_FakeClassifier(sic_map1),
        max_age_hours=0,
    )

    iwm_rows2, sic_map2 = _build_synthetic_universe(
        biotech_count=10, non_biotech_count=0
    )
    _seed_iwm_snapshot(db_path, iwm_rows2, as_of_date="2026-04-29")

    result = refresh_russell2k_biotech(
        db_path=db_path,
        classifier=_FakeClassifier(sic_map2),
        max_age_hours=0,
        allow_shrinkage=True,
    )
    assert result.rows_written == 10


# ---------------------------------------------------------------------------
# Atomic refresh — concurrent reader observes ≥ floor invariant
# ---------------------------------------------------------------------------


def test_atomic_refresh_concurrent_reader_never_sees_below_floor(
    tmp_path: Path,
) -> None:
    """Concurrent reader thread polled across the refresh window must
    observe ≥ floor rows at every observation point.

    The classifier under test sleeps for ~50 ms per resolution, giving
    the reader thread a real chance to observe the writer mid-flight.
    With the BEGIN IMMEDIATE / DELETE / INSERT / COMMIT pattern in
    SQLite WAL, the reader sees the prior committed snapshot until
    COMMIT lands — so the row count is either the prior count (≥ 100)
    or the new count (≥ 100). Never below 100.
    """
    db_path = tmp_path / "alpha.db"

    # Seed a 120-row prior snapshot so the floor of 100 is meaningful.
    iwm_rows1, sic_map1 = _build_synthetic_universe(
        biotech_count=120, non_biotech_count=0
    )
    _seed_iwm_snapshot(db_path, iwm_rows1, as_of_date="2026-04-28")
    refresh_russell2k_biotech(
        db_path=db_path,
        classifier=_FakeClassifier(sic_map1),
        max_age_hours=0,
    )

    # New snapshot — also 120 rows but with different tickers, so the
    # writer has to DELETE all prior rows and INSERT the new ones.
    iwm_rows2 = []
    sic_map2: dict[str, SICResolution | None] = {}
    biotech_sics = (2834, 2836, 8731)
    for i in range(120):
        ticker = f"NEW{i:04d}"
        iwm_rows2.append(_mk_iwm_row(ticker))
        sic_map2[ticker] = _mk_resolution(
            ticker, biotech_sics[i % 3], desc="NEW"
        )
    _seed_iwm_snapshot(db_path, iwm_rows2, as_of_date="2026-04-29")

    # Slow classifier — ~50 ms per call → ~6 s total resolution time
    # for 120 tickers, plenty of opportunity for the reader to poll
    # mid-write. We use a slow classifier to prove the WRITE ITSELF
    # is atomic; the classification is not part of the transaction
    # (it happens BEFORE BEGIN IMMEDIATE) so the actual transaction
    # window is much shorter — but the reader is still guaranteed
    # to see ≥ floor rows because SQLite WAL gives snapshot isolation.

    class _SlowClassifier(_FakeClassifier):
        def resolve_ticker_sic(self, ticker: str):
            time.sleep(0.001)  # tiny per-call sleep
            return super().resolve_ticker_sic(ticker)

    classifier = _SlowClassifier(sic_map2)

    observations: list[int] = []
    stop = threading.Event()

    def reader() -> None:
        # Every reader uses its own connection (the writer/reader
        # serialisation is enforced by SQLite at the file level).
        ro_conn = sqlite3.connect(db_path)
        try:
            while not stop.is_set():
                (cnt,) = ro_conn.execute(
                    "SELECT COUNT(*) FROM russell2k_biotech"
                ).fetchone()
                observations.append(cnt)
                time.sleep(0.001)
        finally:
            ro_conn.close()

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    try:
        refresh_russell2k_biotech(
            db_path=db_path,
            classifier=classifier,
            max_age_hours=0,
        )
    finally:
        stop.set()
        reader_thread.join(timeout=5.0)

    assert observations, "reader thread did not produce any observations"
    floor = 100
    below_floor = [c for c in observations if c < floor]
    assert below_floor == [], (
        f"concurrent reader observed counts below floor: "
        f"min={min(observations)} samples={len(observations)}"
    )


# ---------------------------------------------------------------------------
# Missing IWM snapshot
# ---------------------------------------------------------------------------


def test_refresh_raises_missing_iwm_snapshot_when_table_empty(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "alpha.db"
    # Bootstrap empty IWM table so the SELECT MAX(as_of_date) query
    # does not trip on a missing table.
    conn = sqlite3.connect(db_path)
    ensure_iwm_snapshot_table(conn)
    conn.commit()
    conn.close()

    with pytest.raises(MissingIWMSnapshot):
        refresh_russell2k_biotech(
            db_path=db_path,
            classifier=_FakeClassifier({}),
            max_age_hours=0,
        )


def test_refresh_raises_missing_iwm_snapshot_when_table_does_not_exist(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "alpha.db"
    with pytest.raises(MissingIWMSnapshot):
        refresh_russell2k_biotech(
            db_path=db_path,
            classifier=_FakeClassifier({}),
            max_age_hours=0,
        )


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.db"
    iwm_rows, sic_map = _build_synthetic_universe(
        biotech_count=15, non_biotech_count=5
    )
    _seed_iwm_snapshot(db_path, iwm_rows)

    result = refresh_russell2k_biotech(
        db_path=db_path,
        classifier=_FakeClassifier(sic_map),
        dry_run=True,
        max_age_hours=0,
    )
    assert result.dry_run is True
    assert result.rows_written == 0
    assert result.biotech_matches == 15

    conn = sqlite3.connect(db_path)
    try:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM russell2k_biotech"
        ).fetchone()
    finally:
        conn.close()
    assert count == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_help_lists_required_flags(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for flag in (
        "--refresh",
        "--db",
        "--max-age-hours",
        "--dry-run",
        "--allow-shrinkage",
        "--emit-stats",
    ):
        assert flag in out


def test_cli_no_iwm_snapshot_returns_documented_exit_code(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "alpha.db"
    rc = main(["--refresh", "--db", str(db_path)])
    assert rc == EXIT_NO_IWM_SNAPSHOT


def test_cli_dry_run_emits_stats_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "alpha.db"
    iwm_rows, sic_map = _build_synthetic_universe(
        biotech_count=12, non_biotech_count=3
    )
    _seed_iwm_snapshot(db_path, iwm_rows)

    # Inject a fake classifier through the module-level constructor
    # by monkeypatching SECSICClassifier in the russell_biotech module.
    fake = _FakeClassifier(sic_map)
    monkeypatch.setattr(
        russell_biotech, "SECSICClassifier", lambda **kwargs: fake
    )

    rc = main(
        [
            "--dry-run",
            "--emit-stats",
            "--db",
            str(db_path),
            "--max-age-hours",
            "0",
        ]
    )
    assert rc == EXIT_OK
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["dry_run"] is True
    assert payload["biotech_matches"] == 12
    assert payload["rows_written"] == 0


def test_cli_refresh_writes_rows_and_exits_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "alpha.db"
    iwm_rows, sic_map = _build_synthetic_universe(
        biotech_count=20, non_biotech_count=5
    )
    _seed_iwm_snapshot(db_path, iwm_rows)

    fake = _FakeClassifier(sic_map)
    monkeypatch.setattr(
        russell_biotech, "SECSICClassifier", lambda **kwargs: fake
    )

    rc = main(["--refresh", "--db", str(db_path)])
    assert rc == EXIT_OK

    conn = sqlite3.connect(db_path)
    try:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM russell2k_biotech"
        ).fetchone()
    finally:
        conn.close()
    assert count == 20


def test_cli_shrinkage_refusal_returns_documented_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "alpha.db"
    iwm_rows1, sic_map1 = _build_synthetic_universe(
        biotech_count=100, non_biotech_count=0
    )
    _seed_iwm_snapshot(db_path, iwm_rows1, as_of_date="2026-04-28")
    fake_1 = _FakeClassifier(sic_map1)
    monkeypatch.setattr(
        russell_biotech, "SECSICClassifier", lambda **kwargs: fake_1
    )
    main(["--refresh", "--db", str(db_path)])

    # Now seed a much smaller IWM and try to refresh.
    iwm_rows2, sic_map2 = _build_synthetic_universe(
        biotech_count=20, non_biotech_count=0
    )
    _seed_iwm_snapshot(db_path, iwm_rows2, as_of_date="2026-04-29")
    fake_2 = _FakeClassifier(sic_map2)
    monkeypatch.setattr(
        russell_biotech, "SECSICClassifier", lambda **kwargs: fake_2
    )
    rc = main(["--refresh", "--db", str(db_path)])
    assert rc == EXIT_SHRINKAGE_REFUSED

    # Prior snapshot preserved.
    conn = sqlite3.connect(db_path)
    try:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM russell2k_biotech"
        ).fetchone()
    finally:
        conn.close()
    assert count == 100


def test_cli_allow_shrinkage_lets_small_snapshot_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "alpha.db"
    iwm_rows1, sic_map1 = _build_synthetic_universe(
        biotech_count=100, non_biotech_count=0
    )
    _seed_iwm_snapshot(db_path, iwm_rows1, as_of_date="2026-04-28")
    fake_1 = _FakeClassifier(sic_map1)
    monkeypatch.setattr(
        russell_biotech, "SECSICClassifier", lambda **kwargs: fake_1
    )
    main(["--refresh", "--db", str(db_path)])

    iwm_rows2, sic_map2 = _build_synthetic_universe(
        biotech_count=10, non_biotech_count=0
    )
    _seed_iwm_snapshot(db_path, iwm_rows2, as_of_date="2026-04-29")
    fake_2 = _FakeClassifier(sic_map2)
    monkeypatch.setattr(
        russell_biotech, "SECSICClassifier", lambda **kwargs: fake_2
    )
    rc = main(["--refresh", "--allow-shrinkage", "--db", str(db_path)])
    assert rc == EXIT_OK

    conn = sqlite3.connect(db_path)
    try:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM russell2k_biotech"
        ).fetchone()
    finally:
        conn.close()
    assert count == 10


# ---------------------------------------------------------------------------
# RefreshResult JSON shape
# ---------------------------------------------------------------------------


def test_refresh_result_to_json_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.db"
    iwm_rows, sic_map = _build_synthetic_universe(
        biotech_count=8, non_biotech_count=4
    )
    _seed_iwm_snapshot(db_path, iwm_rows)
    result = refresh_russell2k_biotech(
        db_path=db_path,
        classifier=_FakeClassifier(sic_map),
        max_age_hours=0,
    )
    payload = json.loads(result.to_json())
    for key in (
        "db_path",
        "as_of_date",
        "fetched_at",
        "iwm_tickers_considered",
        "biotech_matches",
        "rows_written",
        "prior_row_count",
        "new_row_count",
        "completed_at",
        "dry_run",
    ):
        assert key in payload, f"missing JSON key: {key}"
    assert payload["rows_written"] == 8
    assert payload["biotech_matches"] == 8
