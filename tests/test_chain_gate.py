"""f-m3-08: Chain-gate + AlpacaBackedProbe tests.

Validates the contract from the f-m3-08 feature description and
VAL-M3-045 / VAL-M3-046:

* :class:`AlpacaBackedProbe` consults
  :meth:`AlpacaClient.get_options_chain` and returns ``True`` only
  when at least one chain row carries an ``expiry`` within the
  60-day lookahead window. Transport / auth errors degrade to
  ``False`` (not a hard raise).
* :func:`refresh_universe_chains` re-probes every existing universe
  row and updates ``has_options_chain`` + ``last_chain_check_at``.
  Tier flips (watch ↔ tradeable) follow the probe answer.
* :func:`filter_chain_gated_tickers` (and the
  ``unified_scorer`` ``main()`` integration) drops tickers whose
  ``universe.has_options_chain != 1``. Each skipped ticker gets a
  ``WARNING`` log line.
* :func:`score_universe` is the wrapper that wires the gate into a
  scoring loop; it returns the ``(scored, skipped)`` summary needed
  by the daily-run orchestrator.

Hermetic — no network calls, no real Alpaca credentials. Uses
fakes / cassette payloads from
``tests/fixtures/cassettes/alpaca/options_chain_spy.json``.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper import db as _db
from biotech_sniper.bulk_universe_scanner import (
    refresh_universe_chains,
)
from biotech_sniper.options_chain_probe import (
    AlpacaBackedProbe,
    OptionsChainProbe,
    PROBE_LOOKAHEAD_DAYS,
)
from biotech_sniper.sectors import unified_scorer


CASSETTE_DIR = Path(__file__).parent / "fixtures" / "cassettes" / "alpaca"


# ---------------------------------------------------------------------------
# Probe doubles
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Minimal substitute exposing only ``get_options_chain``."""

    def __init__(
        self,
        chain_by_ticker: dict[str, list[dict[str, Any]]] | None = None,
        *,
        raise_for: dict[str, Exception] | None = None,
    ) -> None:
        self._chain = dict(chain_by_ticker or {})
        self._raise_for = dict(raise_for or {})
        self.calls: list[tuple[str, str | None]] = []

    def get_options_chain(
        self, ticker: str, expiry: str | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((ticker, expiry))
        if ticker in self._raise_for:
            raise self._raise_for[ticker]
        return list(self._chain.get(ticker, []))


def _spy_chain_rows(
    expiry: str | None = None,
) -> list[dict[str, Any]]:
    """Return chain rows shaped like AlpacaClient.get_options_chain.

    Drives the probe with the same ``expiry`` field shape the real
    client emits — ``YYYY-MM-DD`` strings.
    """
    iso = expiry or (
        datetime.date.today() + datetime.timedelta(days=30)
    ).isoformat()
    payload = json.loads(
        (CASSETTE_DIR / "options_chain_spy.json").read_text(encoding="utf-8")
    )
    rows: list[dict[str, Any]] = []
    for contract in payload["contracts"]:
        rows.append(
            {
                "symbol": contract["symbol"],
                "strike": float(contract["strike_price"]),
                "expiry": iso,
                "type": contract["type"],
                "bid": 1.0,
                "ask": 1.1,
                "mid": 1.05,
                "iv": 0.20,
                "delta": 0.45,
                "oi": int(contract["open_interest"]),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# AlpacaBackedProbe
# ---------------------------------------------------------------------------


def test_alpaca_backed_probe_returns_true_for_chain_within_lookahead():
    """Chain row with expiry inside the 60-day window → True."""
    fake = _FakeAlpacaClient(
        chain_by_ticker={"SPY": _spy_chain_rows()}
    )
    probe = AlpacaBackedProbe(client=fake)

    assert probe.probe("SPY") is True
    assert fake.calls and fake.calls[0][0] == "SPY"


def test_alpaca_backed_probe_returns_false_when_chain_empty():
    """Broker returns no rows → False (ticker has no listed options)."""
    fake = _FakeAlpacaClient(chain_by_ticker={"NONE": []})
    probe = AlpacaBackedProbe(client=fake)

    assert probe.probe("NONE") is False


def test_alpaca_backed_probe_filters_rows_outside_lookahead():
    """A row with expiry >60 days out is rejected; no in-window row → False."""
    far_iso = (
        datetime.date.today()
        + datetime.timedelta(days=PROBE_LOOKAHEAD_DAYS + 30)
    ).isoformat()
    rows = _spy_chain_rows(expiry=far_iso)
    fake = _FakeAlpacaClient(chain_by_ticker={"FAR": rows})
    probe = AlpacaBackedProbe(client=fake)

    # Note: the probe currently degrades to "len > 0" when no row
    # has a parseable expiry inside the window; the rows here DO
    # have parseable expiries, just outside the window. So the
    # answer must be False per the strict 60-day lookahead.
    assert probe.probe("FAR") is False


def test_alpaca_backed_probe_returns_false_on_transport_error():
    """A broker exception is swallowed and the probe returns False."""
    fake = _FakeAlpacaClient(
        raise_for={"BOOM": RuntimeError("alpaca 502")}
    )
    probe = AlpacaBackedProbe(client=fake)

    assert probe.probe("BOOM") is False


# ---------------------------------------------------------------------------
# refresh_universe_chains — daily/intraday refresh path.
# ---------------------------------------------------------------------------


class _StaticProbe(OptionsChainProbe):
    """Probe whose answer is keyed off a fixed ticker → bool map."""

    def __init__(self, answers: dict[str, bool]) -> None:
        self._answers = dict(answers)
        self.calls: list[str] = []

    def probe(self, ticker: str) -> bool:
        self.calls.append(ticker)
        return self._answers.get(ticker.upper(), False)


def _seed_universe(db_path: Path, rows: list[dict[str, Any]]) -> None:
    conn = _db.connect(db_path)
    try:
        _db.run_migrations(conn)
        with conn:
            for row in rows:
                conn.execute(
                    "INSERT OR REPLACE INTO universe ("
                    "ticker, tier, has_options_chain, last_chain_check_at, "
                    "source) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        row["ticker"],
                        row.get("tier", "watch"),
                        1 if row.get("has_options_chain") else 0,
                        row.get("last_chain_check_at"),
                        row.get("source", "test"),
                    ),
                )
    finally:
        conn.close()


def test_refresh_universe_chains_updates_last_chain_check_at(tmp_path: Path):
    """refresh re-probes every row and stamps last_chain_check_at + tier."""
    db_path = tmp_path / "alpha.db"
    _seed_universe(
        db_path,
        [
            {"ticker": "SRPT", "tier": "watch", "has_options_chain": False},
            {"ticker": "VRTX", "tier": "tradeable", "has_options_chain": True},
        ],
    )
    probe = _StaticProbe({"SRPT": True, "VRTX": False})

    result = refresh_universe_chains(db_path=db_path, probe=probe)

    assert sorted(probe.calls) == ["SRPT", "VRTX"]
    assert result.rows_checked == 2
    assert result.flipped_to_tradeable == 1  # SRPT: False → True
    assert result.flipped_to_watch == 1  # VRTX: True → False
    assert result.has_options_chain_count == 1  # only SRPT is now tradeable

    # Verify the persisted row reflects the flip.
    conn = _db.connect(db_path)
    try:
        rows = {
            r["ticker"]: r
            for r in conn.execute(
                "SELECT ticker, tier, has_options_chain, last_chain_check_at "
                "FROM universe"
            ).fetchall()
        }
    finally:
        conn.close()
    assert rows["SRPT"]["tier"] == "tradeable"
    assert rows["SRPT"]["has_options_chain"] == 1
    assert rows["SRPT"]["last_chain_check_at"] is not None
    assert rows["VRTX"]["tier"] == "watch"
    assert rows["VRTX"]["has_options_chain"] == 0
    assert rows["VRTX"]["last_chain_check_at"] is not None


def test_refresh_universe_chains_idempotent(tmp_path: Path):
    """Re-running with the same probe leaves row counts unchanged."""
    db_path = tmp_path / "alpha.db"
    _seed_universe(
        db_path,
        [
            {"ticker": "SRPT", "tier": "tradeable", "has_options_chain": True},
        ],
    )
    probe = _StaticProbe({"SRPT": True})

    first = refresh_universe_chains(db_path=db_path, probe=probe)
    second = refresh_universe_chains(db_path=db_path, probe=probe)

    assert first.flipped_to_tradeable == 0
    assert first.flipped_to_watch == 0
    assert second.flipped_to_tradeable == 0
    assert second.flipped_to_watch == 0
    assert second.has_options_chain_count == 1


# ---------------------------------------------------------------------------
# Chain-gate filter
# ---------------------------------------------------------------------------


def test_filter_chain_gated_tickers_drops_no_chain(tmp_path: Path):
    """Watch-only tickers (has_options_chain=0) land in the skipped list."""
    db_path = tmp_path / "alpha.db"
    _seed_universe(
        db_path,
        [
            {"ticker": "SRPT", "tier": "tradeable", "has_options_chain": True},
            {"ticker": "WATCH1", "tier": "watch", "has_options_chain": False},
        ],
    )

    scored, skipped = unified_scorer.filter_chain_gated_tickers(
        ["SRPT", "WATCH1"], db_path=db_path
    )
    assert scored == ["SRPT"]
    assert skipped == ["WATCH1"]


def test_filter_chain_gated_tickers_handles_unknown_ticker(tmp_path: Path):
    """A ticker absent from universe is treated as no-chain (skipped)."""
    db_path = tmp_path / "alpha.db"
    _seed_universe(
        db_path,
        [
            {"ticker": "SRPT", "tier": "tradeable", "has_options_chain": True},
        ],
    )

    scored, skipped = unified_scorer.filter_chain_gated_tickers(
        ["SRPT", "NEW"], db_path=db_path
    )
    assert scored == ["SRPT"]
    assert skipped == ["NEW"]


def test_filter_chain_gated_tickers_bypasses_when_universe_missing(
    tmp_path: Path,
):
    """When universe table is empty, all tickers are scored (fresh checkout)."""
    db_path = tmp_path / "alpha.db"
    # No rows seeded → query returns empty → bypass.
    _db.run_migrations(_db.connect(db_path))

    scored, skipped = unified_scorer.filter_chain_gated_tickers(
        ["SRPT", "VRTX"], db_path=db_path
    )
    assert scored == ["SRPT", "VRTX"]
    assert skipped == []


# ---------------------------------------------------------------------------
# unified_scorer.main() integration
# ---------------------------------------------------------------------------


def _patch_scorer_factory(monkeypatch, db_path: Path) -> None:
    """Stub :func:`_build_scorer` so unified_scorer.main runs without LLMs.

    The real scorer persists into ``scoring_cache``; the stub mimics
    that minimal subset so :func:`unified_scorer.main` reports the
    ticker in ``tickers_scored`` (the rows_upserted increment is
    driven by the scorer's persistence side effect).
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
                        (ticker, payload["as_of_date"], 0.7),
                    )
            finally:
                conn.close()
            return {
                "ticker": ticker,
                "ensemble_score": 0.7,
                "grade": "A",
                "providers_used": ["xai"],
            }

    monkeypatch.setattr(unified_scorer, "_build_scorer", lambda **_: _StubEnsemble())


def test_unified_scorer_main_skips_no_chain_tickers(
    monkeypatch, tmp_path: Path, capsys, caplog
):
    """unified_scorer.main drops watch-only tickers from scored set + summary."""
    db_path = tmp_path / "data" / "alpha_sniper.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    _seed_universe(
        db_path,
        [
            {"ticker": "TRADE1", "tier": "tradeable", "has_options_chain": True},
            {"ticker": "WATCH1", "tier": "watch", "has_options_chain": False},
        ],
    )

    # Redirect DATA_DIR so unified_scorer's helpers find this db.
    from biotech_sniper import paths as _paths

    monkeypatch.setattr(_paths, "DATA_DIR", db_path.parent)
    # The unified_scorer module imports DATA_DIR lazily inside its
    # helpers, so the monkeypatch above is enough.

    # Redirect play-cards root so it doesn't write into the repo.
    monkeypatch.setattr(
        "biotech_sniper.play_card_formatter.DEFAULT_PLAY_CARDS_ROOT",
        tmp_path / "play_cards",
        raising=False,
    )
    # Most callers also read PLAY_CARDS_ROOT off DATA_DIR via paths.
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
                "TRADE1,WATCH1",
                "--date",
                "2026-04-27",
                "--no-emit-play-cards",
            ]
        )
    assert rc == 0

    summary = json.loads(
        [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()][-1]
    )
    assert summary["tickers_scored"] == ["TRADE1"]
    assert summary["tickers_skipped_no_chain"] == ["WATCH1"]

    # WARNING log names the skipped ticker + reason.
    skip_msgs = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING
        and "WATCH1" in rec.getMessage()
        and "no_options_chain" in rec.getMessage()
    ]
    assert skip_msgs, [rec.getMessage() for rec in caplog.records]


def test_unified_scorer_main_no_chain_gate_flag_disables_gate(
    monkeypatch, tmp_path: Path, capsys
):
    """``--no-chain-gate`` lets watch-only tickers through to scoring."""
    db_path = tmp_path / "data" / "alpha_sniper.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    _seed_universe(
        db_path,
        [
            {"ticker": "TRADE1", "tier": "tradeable", "has_options_chain": True},
            {"ticker": "WATCH1", "tier": "watch", "has_options_chain": False},
        ],
    )

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

    rc = unified_scorer.main(
        [
            "--tickers",
            "TRADE1,WATCH1",
            "--date",
            "2026-04-27",
            "--no-emit-play-cards",
            "--no-chain-gate",
        ]
    )
    assert rc == 0

    summary = json.loads(
        [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()][-1]
    )
    assert sorted(summary["tickers_scored"]) == ["TRADE1", "WATCH1"]
    assert summary["tickers_skipped_no_chain"] == []
