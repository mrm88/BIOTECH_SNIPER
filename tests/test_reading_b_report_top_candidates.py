"""Reading-B CLI report — top-candidates mode coverage (f-live-03).

Validation contract coverage
----------------------------

VAL-LIVE-004 — ``reading_b_report --mode=top-candidates --since YYYY-MM-DD
--n=10 [--json]`` returns up to N rows ordered by ensemble mean
probability DESC. Each row contains AT MINIMUM: ``ticker``,
``candidate_event_id``, ``mean_probability``, ``label``, ``direction``,
per-provider scores (xai/anthropic/gemini/perplexity probabilities),
``catalyst_type``, source news headline, ``gate_outcome``,
``emitted_at``. Filters: 4 providers present, unanimous labels,
label=material, mean probability >= STAGE2_PROBABILITY_THRESHOLD.

All tests are hermetic: they construct a fresh sqlite db at
``tmp_path``, apply the v10 (or current) migration, and seed rows.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from biotech_sniper import db as _db
from biotech_sniper.migrations import runner as _migrations_runner
from biotech_sniper.reports import reading_b_report


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _db.connect(db_path)
    try:
        _db.run_migrations(conn)
    finally:
        conn.close()
    rc = _migrations_runner.main(
        [
            "--db",
            str(db_path),
            "--target",
            str(_db.CURRENT_VERSION),
            "--no-backup",
        ]
    )
    assert rc == 0, f"migration runner returned {rc}"


def _insert_news_event(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    title: str,
    ingested_at: str,
    url: str,
    source: str = "universal_news_watcher",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO news_events (ticker, source, title, ingested_at, url, published_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (ticker, source, title, ingested_at, url, ingested_at),
    )
    return int(cur.lastrowid)


def _insert_candidate(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    news_event_id: int,
    emitted_at: str,
    dedup_key: str,
    matched: str = "readout",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO candidate_events (
            ticker, source_news_event_id, matched_keywords,
            calendar_match, emitted_at, dedup_key
        ) VALUES (?, ?, ?, NULL, ?, ?)
        """,
        (ticker, news_event_id, matched, emitted_at, dedup_key),
    )
    return int(cur.lastrowid)


def _insert_ensemble(
    conn: sqlite3.Connection,
    *,
    candidate_event_id: int,
    provider: str,
    label: str,
    probability: float,
    direction: str | None,
    run_id: str = "run-1",
    called_at: str = "2026-04-29T13:00:00.000Z",
) -> None:
    conn.execute(
        """
        INSERT INTO ensemble_scores_event (
            candidate_event_id, provider, run_id,
            label, probability, direction,
            rationale, citations, latency_ms, cost_usd, called_at
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 100, 0.01, ?)
        """,
        (
            candidate_event_id,
            provider,
            run_id,
            label,
            probability,
            direction,
            called_at,
        ),
    )


_PROVIDERS: tuple[str, ...] = ("xai", "anthropic", "gemini", "perplexity")
_TODAY = "2026-04-29"


def _seed_unanimous_material_candidate(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    probabilities: tuple[float, float, float, float],
    direction: str = "bullish",
    label: str = "material",
    emitted_at: str | None = None,
    headline: str = "phase 3 readout topline data",
    dedup: str | None = None,
) -> int:
    """Seed a candidate_event with all 4 providers scoring `label`."""
    emitted_at = emitted_at or f"{_TODAY}T13:00:00.000Z"
    dedup = dedup or f"dk-{ticker}-{emitted_at}"
    n_id = _insert_news_event(
        conn,
        ticker=ticker,
        title=headline,
        ingested_at=emitted_at,
        url=f"https://example.com/{ticker}/{dedup}",
    )
    c_id = _insert_candidate(
        conn,
        ticker=ticker,
        news_event_id=n_id,
        emitted_at=emitted_at,
        dedup_key=dedup,
    )
    for prov, prob in zip(_PROVIDERS, probabilities):
        _insert_ensemble(
            conn,
            candidate_event_id=c_id,
            provider=prov,
            label=label,
            probability=prob,
            direction=direction,
        )
    return c_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_returns_rows_ordered_by_mean_probability_desc(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha_sniper.db"
    _seed_db(db_path)
    conn = _db.connect(db_path)
    try:
        _seed_unanimous_material_candidate(
            conn, ticker="AAAA", probabilities=(0.70, 0.72, 0.74, 0.76),
            dedup="dk-aaaa",
        )
        _seed_unanimous_material_candidate(
            conn, ticker="BBBB", probabilities=(0.90, 0.92, 0.94, 0.96),
            dedup="dk-bbbb",
        )
        _seed_unanimous_material_candidate(
            conn, ticker="CCCC", probabilities=(0.80, 0.82, 0.84, 0.86),
            dedup="dk-cccc",
        )
        conn.commit()
    finally:
        conn.close()

    payload = reading_b_report.build_top_candidates_report(
        db_path, since=_TODAY, n=10, threshold=0.65
    )
    tickers = [row["ticker"] for row in payload["top_candidates"]]
    probs = [row["mean_probability"] for row in payload["top_candidates"]]
    assert tickers == ["BBBB", "CCCC", "AAAA"]
    assert probs == sorted(probs, reverse=True)
    assert payload["n_returned"] == 3
    assert payload["n_requested"] == 10


def test_filters_out_non_unanimous(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha_sniper.db"
    _seed_db(db_path)
    conn = _db.connect(db_path)
    try:
        # Unanimous, should appear.
        _seed_unanimous_material_candidate(
            conn, ticker="GOOD", probabilities=(0.80, 0.82, 0.84, 0.86),
            dedup="dk-good",
        )
        # Non-unanimous: 3 providers say material, 1 says immaterial.
        n_id = _insert_news_event(
            conn,
            ticker="MIXD",
            title="phase 3 readout",
            ingested_at=f"{_TODAY}T14:00:00.000Z",
            url="https://example.com/mixd",
        )
        c_id = _insert_candidate(
            conn,
            ticker="MIXD",
            news_event_id=n_id,
            emitted_at=f"{_TODAY}T14:00:00.000Z",
            dedup_key="dk-mixd",
        )
        labels = ("material", "material", "material", "immaterial")
        for prov, lbl in zip(_PROVIDERS, labels):
            _insert_ensemble(
                conn,
                candidate_event_id=c_id,
                provider=prov,
                label=lbl,
                probability=0.90,
                direction="bullish",
            )
        conn.commit()
    finally:
        conn.close()

    payload = reading_b_report.build_top_candidates_report(
        db_path, since=_TODAY, n=10, threshold=0.65
    )
    tickers = [row["ticker"] for row in payload["top_candidates"]]
    assert tickers == ["GOOD"]


def test_filters_out_non_material(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha_sniper.db"
    _seed_db(db_path)
    conn = _db.connect(db_path)
    try:
        _seed_unanimous_material_candidate(
            conn, ticker="GOOD", probabilities=(0.80, 0.82, 0.84, 0.86),
            dedup="dk-good",
        )
        # Unanimous immaterial — must not appear.
        _seed_unanimous_material_candidate(
            conn, ticker="IMMA",
            probabilities=(0.90, 0.92, 0.94, 0.96),
            label="immaterial",
            dedup="dk-imma",
        )
        # Unanimous ambiguous — must not appear.
        _seed_unanimous_material_candidate(
            conn, ticker="AMBI",
            probabilities=(0.95, 0.95, 0.95, 0.95),
            label="ambiguous",
            dedup="dk-ambi",
        )
        conn.commit()
    finally:
        conn.close()

    payload = reading_b_report.build_top_candidates_report(
        db_path, since=_TODAY, n=10, threshold=0.65
    )
    tickers = [row["ticker"] for row in payload["top_candidates"]]
    assert tickers == ["GOOD"]


def test_filters_out_below_threshold(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha_sniper.db"
    _seed_db(db_path)
    conn = _db.connect(db_path)
    try:
        _seed_unanimous_material_candidate(
            conn, ticker="HIGH", probabilities=(0.80, 0.82, 0.84, 0.86),
            dedup="dk-high",
        )
        # Mean = 0.50, below 0.65.
        _seed_unanimous_material_candidate(
            conn, ticker="LOWP", probabilities=(0.50, 0.50, 0.50, 0.50),
            dedup="dk-lowp",
        )
        conn.commit()
    finally:
        conn.close()

    payload = reading_b_report.build_top_candidates_report(
        db_path, since=_TODAY, n=10, threshold=0.65
    )
    tickers = [row["ticker"] for row in payload["top_candidates"]]
    assert tickers == ["HIGH"]


def test_returns_at_most_n_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha_sniper.db"
    _seed_db(db_path)
    conn = _db.connect(db_path)
    try:
        for i in range(7):
            _seed_unanimous_material_candidate(
                conn,
                ticker=f"T{i:03d}",
                probabilities=(0.70 + i / 100, 0.71 + i / 100,
                               0.72 + i / 100, 0.73 + i / 100),
                dedup=f"dk-t{i:03d}",
            )
        conn.commit()
    finally:
        conn.close()

    payload = reading_b_report.build_top_candidates_report(
        db_path, since=_TODAY, n=3, threshold=0.65
    )
    assert payload["n_returned"] == 3
    assert payload["n_requested"] == 3
    assert len(payload["top_candidates"]) == 3


def test_json_schema_stable_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha_sniper.db"
    _seed_db(db_path)
    conn = _db.connect(db_path)
    try:
        _seed_unanimous_material_candidate(
            conn, ticker="ZZZZ",
            probabilities=(0.80, 0.82, 0.84, 0.86),
            direction="bullish",
            dedup="dk-zzzz",
            headline="ZZZZ phase 3 pivotal readout topline",
        )
        conn.commit()
    finally:
        conn.close()

    payload = reading_b_report.build_top_candidates_report(
        db_path, since=_TODAY, n=10, threshold=0.65
    )
    assert set(payload.keys()) == {
        "top_candidates", "n_returned", "n_requested", "as_of",
    }
    assert payload["n_returned"] == 1
    row = payload["top_candidates"][0]
    expected_keys = {
        "ticker",
        "candidate_event_id",
        "mean_probability",
        "label",
        "direction",
        "per_provider",
        "catalyst_type",
        "news_headline",
        "emitted_at",
        "gate_outcome",
    }
    assert set(row.keys()) == expected_keys
    assert row["ticker"] == "ZZZZ"
    assert row["label"] == "material"
    assert row["direction"] == "bullish"
    assert row["gate_outcome"] == "passed"
    assert row["catalyst_type"] == "READOUT"
    assert row["news_headline"] == "ZZZZ phase 3 pivotal readout topline"
    assert set(row["per_provider"].keys()) == {
        "xai", "anthropic", "gemini", "perplexity",
    }
    assert row["per_provider"]["xai"] == pytest.approx(0.80)
    assert row["per_provider"]["perplexity"] == pytest.approx(0.86)
    assert row["mean_probability"] == pytest.approx(0.83)

    # Deterministic ordering on re-run (the as_of timestamp is the
    # only field expected to change call-to-call).
    payload2 = reading_b_report.build_top_candidates_report(
        db_path, since=_TODAY, n=10, threshold=0.65
    )
    payload_no_as_of = {k: v for k, v in payload.items() if k != "as_of"}
    payload2_no_as_of = {k: v for k, v in payload2.items() if k != "as_of"}
    assert json.dumps(payload_no_as_of, sort_keys=True) == json.dumps(
        payload2_no_as_of, sort_keys=True
    )


def test_text_format_renders_table(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha_sniper.db"
    _seed_db(db_path)
    conn = _db.connect(db_path)
    try:
        _seed_unanimous_material_candidate(
            conn, ticker="AAAA", probabilities=(0.80, 0.82, 0.84, 0.86),
            dedup="dk-aaaa",
            headline=(
                "AAAA reports positive phase 3 topline data "
                "exceeding all primary endpoints with strong safety "
                "profile through 52 weeks of treatment"
            ),
        )
        conn.commit()
    finally:
        conn.close()

    payload = reading_b_report.build_top_candidates_report(
        db_path, since=_TODAY, n=10, threshold=0.65
    )
    text = reading_b_report.format_top_candidates_text(payload)
    # Required column headers in the rendered table.
    for header in (
        "rank", "ticker", "p_win", "label", "direction",
        "catalyst", "headline", "emitted_at",
    ):
        assert header in text
    # Headline truncated to 60 chars in the rendered cell.
    long_headline = payload["top_candidates"][0]["news_headline"]
    assert len(long_headline) > 60
    assert long_headline[:60] in text
    # Rank starts at 1.
    assert "1" in text
    assert "AAAA" in text


def test_since_flag_filters_by_date(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha_sniper.db"
    _seed_db(db_path)
    conn = _db.connect(db_path)
    try:
        _seed_unanimous_material_candidate(
            conn, ticker="TODY", probabilities=(0.80, 0.82, 0.84, 0.86),
            emitted_at=f"{_TODAY}T13:00:00.000Z",
            dedup="dk-tody",
        )
        prior = "2026-04-28"
        _seed_unanimous_material_candidate(
            conn, ticker="PRIO", probabilities=(0.95, 0.95, 0.95, 0.95),
            emitted_at=f"{prior}T13:00:00.000Z",
            dedup="dk-prio",
        )
        conn.commit()
    finally:
        conn.close()

    payload = reading_b_report.build_top_candidates_report(
        db_path, since=_TODAY, n=10, threshold=0.65
    )
    tickers = [row["ticker"] for row in payload["top_candidates"]]
    assert tickers == ["TODY"]
    assert payload["as_of"].endswith("Z")


def test_empty_db_returns_empty_list_no_error(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha_sniper.db"
    _seed_db(db_path)

    payload = reading_b_report.build_top_candidates_report(
        db_path, since=_TODAY, n=10, threshold=0.65
    )
    assert payload["top_candidates"] == []
    assert payload["n_returned"] == 0
    assert payload["n_requested"] == 10
    # CLI invocation also exits cleanly on empty DB.
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "biotech_sniper.reports.reading_b_report",
            "--db", str(db_path),
            "--mode", "top-candidates",
            "--since", _TODAY,
            "--n", "10",
            "--json",
        ],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout.strip().splitlines()[-1])
    assert parsed["top_candidates"] == []
    assert parsed["n_returned"] == 0
    assert parsed["n_requested"] == 10
