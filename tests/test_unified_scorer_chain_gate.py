"""f-m3-16: empty-lookup-not-bypass regression tests.

Pin the contract from the f-m3-16 surgical fix #2:

* :func:`unified_scorer.filter_chain_gated_tickers` returns an
  EMPTY ``scored`` list and a FULL ``skipped`` list when the
  universe lookup is empty (universe table missing OR no row for
  any of the requested tickers). Previously it bypassed the gate
  and returned every ticker as scored — actively unsafe because
  unverified tickers leaked into ``scoring_cache`` and downstream
  paper executions.
* :func:`unified_scorer.main` integrates that strict-reject
  semantics: a fresh-checkout invocation with three requested
  tickers and an empty universe yields ``tickers_scored=[]`` and
  ``tickers_skipped_no_chain=[<all three>]`` in the JSON summary.
* The bypass is logged at WARNING (not INFO) so operators see the
  rejection.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper import db as _db
from biotech_sniper.sectors import unified_scorer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_empty_db(db_path: Path) -> None:
    """Initialize the DB schema but leave the universe table empty."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _db.connect(db_path)
    try:
        _db.run_migrations(conn)
    finally:
        conn.close()


def _patch_scorer_factory(monkeypatch, db_path: Path) -> None:
    """Stub :func:`_build_scorer` so unified_scorer.main runs without LLMs.

    A faux ensemble that writes a row into ``scoring_cache`` so
    callers can detect leakage if the gate fails open.
    """

    class _StubEnsemble:
        def score(self, payload: dict[str, Any]) -> dict[str, Any]:
            ticker = payload["ticker"]
            conn = _db.connect(db_path)
            try:
                _db.run_migrations(conn)
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO scoring_cache ("
                        "ticker, as_of_date, ensemble_score) "
                        "VALUES (?, ?, ?)",
                        (ticker, payload["as_of_date"], 0.5),
                    )
            finally:
                conn.close()
            return {
                "ticker": ticker,
                "ensemble_score": 0.5,
                "grade": "B",
                "providers_used": ["xai"],
            }

    monkeypatch.setattr(unified_scorer, "_build_scorer", lambda **_: _StubEnsemble())


# ---------------------------------------------------------------------------
# filter_chain_gated_tickers — empty-lookup-not-bypass
# ---------------------------------------------------------------------------


def test_filter_chain_gated_tickers_returns_zero_for_empty_universe(tmp_path: Path):
    """3 tickers requested, none in universe → 0 returned, 3 skipped.

    f-m3-16 fix #2: the previous behaviour returned all 3 as
    "scored" so a fresh checkout could produce play cards without
    any chain validation. The strict semantics now apply: empty
    lookup → reject everything.
    """
    db_path = tmp_path / "alpha_sniper.db"
    _make_empty_db(db_path)

    requested = ["SRPT", "VRTX", "BIIB"]
    scored, skipped = unified_scorer.filter_chain_gated_tickers(
        requested, db_path=db_path
    )
    assert scored == [], (
        f"empty universe lookup must NOT score any ticker; got scored={scored}"
    )
    assert skipped == requested, (
        f"empty universe lookup must skip ALL requested tickers; got skipped={skipped}"
    )


def test_filter_chain_gated_tickers_logs_warning_on_empty_lookup(
    tmp_path: Path, caplog
):
    """An empty universe lookup logs a structured WARNING, not INFO."""
    db_path = tmp_path / "alpha_sniper.db"
    _make_empty_db(db_path)

    with caplog.at_level(
        logging.WARNING, logger="biotech_sniper.sectors.unified_scorer"
    ):
        scored, skipped = unified_scorer.filter_chain_gated_tickers(
            ["SRPT", "VRTX"], db_path=db_path
        )
    assert scored == []
    assert skipped == ["SRPT", "VRTX"]

    warn_records = [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING
        and "empty universe lookup" in rec.getMessage()
    ]
    assert warn_records, [r.getMessage() for r in caplog.records]


def test_filter_chain_gated_tickers_unknown_ticker_with_partial_universe(
    tmp_path: Path,
):
    """Universe has SRPT but not VRTX → SRPT scored, VRTX dropped.

    The non-empty-lookup path is unchanged from f-m3-08: tickers
    absent from a populated universe table are skipped (not
    bypassed). This ensures the f-m3-16 inversion only affects
    the empty-lookup case.
    """
    db_path = tmp_path / "alpha_sniper.db"
    _make_empty_db(db_path)
    conn = _db.connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO universe ("
                "ticker, tier, has_options_chain, source) "
                "VALUES (?, ?, ?, ?)",
                ("SRPT", "tradeable", 1, "test"),
            )
    finally:
        conn.close()

    scored, skipped = unified_scorer.filter_chain_gated_tickers(
        ["SRPT", "VRTX"], db_path=db_path
    )
    assert scored == ["SRPT"]
    assert skipped == ["VRTX"]


# ---------------------------------------------------------------------------
# unified_scorer.main() — integration
# ---------------------------------------------------------------------------


def test_main_drops_all_tickers_when_universe_empty(
    monkeypatch, tmp_path: Path, capsys, caplog
):
    """unified_scorer.main with empty universe → tickers_scored=[].

    Three tickers requested via ``--tickers``; the universe table
    has zero rows → all three end up in
    ``tickers_skipped_no_chain``; nothing is scored;
    ``rows_upserted`` is 0.
    """
    db_path = tmp_path / "data" / "alpha_sniper.db"
    _make_empty_db(db_path)

    from biotech_sniper import paths as _paths

    monkeypatch.setattr(_paths, "DATA_DIR", db_path.parent)
    monkeypatch.setattr(
        "biotech_sniper.play_card_formatter.DEFAULT_PLAY_CARDS_ROOT",
        tmp_path / "play_cards",
        raising=False,
    )
    monkeypatch.setattr(
        _paths, "PLAY_CARDS_DIR", tmp_path / "play_cards", raising=False
    )
    _patch_scorer_factory(monkeypatch, db_path)

    with caplog.at_level(
        logging.WARNING, logger="biotech_sniper.sectors.unified_scorer"
    ):
        rc = unified_scorer.main(
            [
                "--tickers",
                "SRPT,VRTX,BIIB",
                "--date",
                "2026-04-27",
                "--no-emit-play-cards",
            ]
        )
    assert rc == 0

    summary_lines = [
        ln for ln in capsys.readouterr().out.splitlines() if ln.strip()
    ]
    summary = json.loads(summary_lines[-1])

    assert summary["tickers_scored"] == [], (
        f"empty universe must not score any ticker; got {summary['tickers_scored']}"
    )
    assert sorted(summary["tickers_skipped_no_chain"]) == ["BIIB", "SRPT", "VRTX"]
    assert summary["rows_upserted"] == 0

    # And no scoring_cache rows leaked through.
    conn = _db.connect(db_path)
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) AS n FROM scoring_cache "
            "WHERE as_of_date = '2026-04-27'"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert cnt == 0, (
        f"scoring_cache must be empty when chain gate rejects all tickers; got {cnt}"
    )

    # Operator sees a WARNING, not INFO, for the empty-lookup case.
    warn_messages = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING
    ]
    assert any(
        "empty universe lookup" in msg for msg in warn_messages
    ), warn_messages
