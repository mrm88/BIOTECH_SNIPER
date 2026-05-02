"""Integration tests for f-fix-live-01: submit collaborator wiring in run_main_loop.

Covers VAL-LIVE-001 explicitly: "if chain_result.passed=True AND market is
open AND underlying is tradable, invoke submit_news_event_entry". The
existing f-live-01 ``dispatch_after_poll_cycle`` orchestrator already
implements the conditional submit-fn invocation; this fix wires the
production submit collaborator chain (paper_executor +
market_open_check + chain_quote_fn + submit_fn) inside
``biotech_sniper.news_daemon.resilience.run_main_loop`` so the daemon
path can ACTUALLY place a paper_orders row when all gates open.

Cases:

* ``all_gates_open_with_submit_collaborators`` — 1 candidate emitted,
  ``.armed`` present, all three env gates open, mock submit_fn /
  market_open_check / chain_quote_fn / paper_executor wired →
  assert exactly 1 ``paper_orders`` row with
  ``event='news_event_entry'`` AND ``alpaca_order_id`` non-null AND
  >=1 ``stage2_order_submitted`` log entry.

* ``market_closed_skips_submit`` — same wiring as above but
  ``market_open_check`` returns ``False`` → assert 0
  ``paper_orders`` rows AND >=1 ``stage2_chain_completed`` log entry
  (Stage-2 still scored AND ensemble rows persisted) AND >=1
  ``stage2_order_skipped_market_closed`` log entry.

* ``underlying_unavailable_skips_submit`` — ``market_open_check`` =
  ``True`` but ``submit_fn`` raises
  :class:`biotech_sniper.exec.stage2_paper_executor.UnderlyingUnavailable`
  → assert 0 ``paper_orders`` AND >=1 WARNING-level
  ``stage2_underlying_unavailable`` log entry AND daemon exit code
  is 0 (continues to next iteration).

* ``submit_collaborator_missing_logs_warning`` — all gates open but
  the test-seam override leaves ``submit_fn=None`` (the canonical
  "no executor wired" production scenario) → assert 0
  ``paper_orders`` AND >=1 WARNING-level
  ``stage2_submit_collaborator_unavailable`` log entry.

These tests drive the FULL ``run_main_loop`` entry-point so a
connector-level bug between ``dispatch_after_poll_cycle`` and the
collaborator-wiring step would surface (per AGENTS.md
§ "Integration tests for connector code").
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

from biotech_sniper import db as project_db
from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.exec.stage2_paper_executor import (
    UnderlyingUnavailable,
    submit_news_event_entry,
)
from biotech_sniper.migrations.runner import run as run_migrations_runner
from biotech_sniper.paper_executor import PaperExecutor


# ---------------------------------------------------------------------------
# Fake Alpaca client (mirrors tests/test_stage2_paper_executor.py double).
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Duck-typed substitute exposing the surface PaperExecutor uses."""

    def __init__(
        self,
        *,
        base_url: str = PAPER_BASE_URL,
        latest_trade_price: Optional[float] = 100.0,
    ) -> None:
        self.base_url = base_url
        self._latest_trade_price = latest_trade_price
        self.submit_calls: list[Any] = []
        self.get_positions_calls: int = 0

    def get_positions(self) -> list[dict[str, Any]]:
        self.get_positions_calls += 1
        return []

    def submit_order(self, order_request: Any) -> dict[str, Any]:
        self.submit_calls.append(order_request)
        return {
            "id": "fake-order-id-XYZ",
            "symbol": getattr(order_request, "symbol", None),
            "side": "buy",
            "qty": getattr(order_request, "qty", 1),
            "status": "accepted",
        }

    def get_order(self, order_id: str) -> dict[str, Any]:
        return {"id": order_id, "status": "accepted"}

    def get_latest_trade(self, ticker: str) -> Optional[float]:
        return self._latest_trade_price


# ---------------------------------------------------------------------------
# DB + seed helpers (match tests/integration/test_news_daemon_stage2_dispatch.py).
# ---------------------------------------------------------------------------


def _build_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "alpha.db"
    conn = project_db.connect(db_path)
    try:
        project_db.run_migrations(conn)
    finally:
        conn.close()
    run_migrations_runner(
        db_path,
        target_version=project_db.CURRENT_VERSION,
        take_backup_first=False,
    )
    return db_path


def _seed_news_event(
    db_path: Path,
    *,
    ticker: str,
    title: str,
    url: str,
) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO news_events ("
            "ticker, source, title, url, published_at, ingested_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                ticker,
                "rss",
                title,
                url,
                "2026-04-30T12:00:00Z",
                "2026-04-30T12:00:01Z",
            ),
        )
        nid = int(
            conn.execute("SELECT MAX(id) FROM news_events").fetchone()[0]
        )
        conn.commit()
    finally:
        conn.close()
    return nid


def _seed_candidate(
    db_path: Path,
    *,
    ticker: str,
    matched_keywords: str = "pdufa,approval",
    dedup_seed: str = "stage2-submit-001",
) -> int:
    nid = _seed_news_event(
        db_path,
        ticker=ticker,
        title=f"{ticker} pdufa approval imminent",
        url=f"https://example.com/{ticker.lower()}-{dedup_seed}",
    )
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO candidate_events ("
            "ticker, source_news_event_id, matched_keywords,"
            " emitted_at, dedup_key) VALUES (?, ?, ?, "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?)",
            (
                ticker,
                nid,
                matched_keywords,
                f"dedup-{ticker.lower()}-{dedup_seed}",
            ),
        )
        cid = int(
            conn.execute(
                "SELECT MAX(id) FROM candidate_events"
            ).fetchone()[0]
        )
        conn.commit()
    finally:
        conn.close()
    return cid


def _seed_pdufa(
    db_path: Path,
    *,
    ticker: str,
    drug: str = "drugX",
    days_offset: int = 3,
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT OR IGNORE INTO pdufa_calendar ("
            "ticker, drug, action_date, sponsor, source_url, fetched_at"
            ") VALUES (?, ?, DATE('now','+' || ? || ' days'), ?, ?, ?)",
            (
                ticker,
                drug,
                int(days_offset),
                "TestSponsor",
                "https://www.fda.gov/test",
                "2026-04-30T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _make_armed_file(tmp_path: Path) -> Path:
    armed = tmp_path / ".armed"
    armed.write_text("ok")
    return armed


def _provider_callable(
    label: str = "material",
    direction: str = "bullish",
    probability: float = 0.92,
):
    def _call(_candidate, *, name: str = "stub") -> dict[str, Any]:
        return {
            "label": label,
            "probability": probability,
            "direction": direction,
            "rationale": f"{name} stub rationale",
            "citations": [],
            "latency_ms": 5,
            "cost_usd": 0.001,
        }

    return _call


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    yield _build_db(tmp_path)


@pytest.fixture
def armed_path(tmp_path: Path) -> Path:
    return _make_armed_file(tmp_path)


@pytest.fixture
def all_providers() -> dict[str, Any]:
    from biotech_sniper.llm.ensemble import ALL_PROVIDERS

    return {name: _provider_callable() for name in ALL_PROVIDERS}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        self.t += 0.001
        return self.t

    def sleep(self, _s: float) -> None:
        return


def _count(
    db_path: Path,
    table: str,
    where: str = "1=1",
    params: tuple = (),
) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        try:
            return int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {where}", params
                ).fetchone()[0]
            )
        except sqlite3.OperationalError:
            return 0
    finally:
        conn.close()


def _drive_one_cycle(
    db_path: Path,
    *,
    overrides: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    """Run a single ``run_main_loop`` cycle with the given test-seam overrides."""
    from biotech_sniper.news_daemon.resilience import (
        ShutdownState,
        run_main_loop,
    )
    import biotech_sniper.news_daemon.resilience as resilience_module

    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "1")
    monkeypatch.setenv("STAGE2_AUTO_DISPATCH", "1")
    monkeypatch.setenv("STAGE2_DISPATCH_SCOPE", "pdufa-soon")

    monkeypatch.setattr(
        resilience_module,
        "_STAGE2_DISPATCH_OVERRIDES_FOR_TESTS",
        overrides,
        raising=False,
    )

    state = ShutdownState()
    fc = _FakeClock()
    rc = run_main_loop(
        str(db_path),
        poll_seconds=1,
        max_cycles=1,
        rss_fetchers=(),
        state=state,
        install_handlers=False,
        sleep_func=fc.sleep,
        monotonic=fc.monotonic,
        heartbeat_path=Path(db_path).parent / "hb.json",
        version_sha="d" * 40,
    )
    assert state.cycles_completed == 1
    return rc


# ---------------------------------------------------------------------------
# Test 1 — all gates open + collaborators wired → 1 paper_orders row
# ---------------------------------------------------------------------------


def test_all_gates_open_with_submit_collaborators_writes_paper_order(
    db_path: Path,
    armed_path: Path,
    all_providers: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog,
) -> None:
    """Wired collaborators → 1 paper_orders row + stage2_order_submitted log."""
    _seed_pdufa(db_path, ticker="WIRE", days_offset=2)
    _seed_candidate(db_path, ticker="WIRE", dedup_seed="wired-1")

    fake_client = _FakeAlpacaClient()
    executor = PaperExecutor(
        fake_client,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )

    def _market_open() -> bool:
        return True

    def _chain_quote(_candidate: Any) -> tuple:
        return (1.10, 1.30, "2026-07-17", 100.0)

    caplog.set_level("INFO")
    rc = _drive_one_cycle(
        db_path,
        overrides={
            "armed_path": armed_path,
            "providers": all_providers,
            "submit_fn": submit_news_event_entry,
            "market_open_check": _market_open,
            "chain_quote_fn": _chain_quote,
            "paper_executor": executor,
        },
        monkeypatch=monkeypatch,
    )
    assert rc == 0

    rows = sqlite3.connect(str(db_path)).execute(
        "SELECT event, alpaca_order_id FROM paper_orders"
    ).fetchall()
    assert len(rows) == 1, f"expected exactly 1 paper_orders row, got {rows}"
    event, alpaca_order_id = rows[0]
    assert event == "news_event_entry"
    assert alpaca_order_id, (
        f"alpaca_order_id must be non-null, got {alpaca_order_id!r}"
    )

    submitted = [
        r for r in caplog.records
        if getattr(r, "event", None) == "stage2_order_submitted"
    ]
    assert len(submitted) >= 1, (
        f"expected >=1 stage2_order_submitted log entry, got "
        f"{[r.getMessage() for r in submitted]}"
    )


# ---------------------------------------------------------------------------
# Test 2 — market closed → no submit, ensemble rows still persisted
# ---------------------------------------------------------------------------


def test_market_closed_skips_submit_but_persists_ensemble(
    db_path: Path,
    armed_path: Path,
    all_providers: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog,
) -> None:
    """Market closed → 0 paper_orders, ensemble rows persist, skip log emitted."""
    _seed_pdufa(db_path, ticker="MCLOSE", days_offset=2)
    _seed_candidate(db_path, ticker="MCLOSE", dedup_seed="mclose-1")

    fake_client = _FakeAlpacaClient()
    executor = PaperExecutor(
        fake_client,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )

    def _market_closed() -> bool:
        return False

    def _chain_quote(_candidate: Any) -> tuple:
        return (1.10, 1.30, "2026-07-17", 100.0)

    caplog.set_level("INFO")
    rc = _drive_one_cycle(
        db_path,
        overrides={
            "armed_path": armed_path,
            "providers": all_providers,
            "submit_fn": submit_news_event_entry,
            "market_open_check": _market_closed,
            "chain_quote_fn": _chain_quote,
            "paper_executor": executor,
        },
        monkeypatch=monkeypatch,
    )
    assert rc == 0

    assert _count(db_path, "paper_orders") == 0
    assert _count(db_path, "ensemble_scores_event") == 4

    chain_completed = [
        r for r in caplog.records
        if getattr(r, "event", None) == "stage2_chain_completed"
    ]
    assert len(chain_completed) >= 1

    market_skipped = [
        r for r in caplog.records
        if getattr(r, "event", None) == "stage2_order_skipped_market_closed"
    ]
    assert len(market_skipped) >= 1, (
        f"expected >=1 stage2_order_skipped_market_closed log entry, got "
        f"{[r.getMessage() for r in market_skipped]}"
    )


# ---------------------------------------------------------------------------
# Test 3 — UnderlyingUnavailable → daemon continues
# ---------------------------------------------------------------------------


def test_underlying_unavailable_skips_submit_and_continues(
    db_path: Path,
    armed_path: Path,
    all_providers: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog,
) -> None:
    """submit_fn raises UnderlyingUnavailable → 0 paper_orders, WARNING logged."""
    _seed_pdufa(db_path, ticker="HALT", days_offset=2)
    _seed_candidate(db_path, ticker="HALT", dedup_seed="halt-1")

    fake_client = _FakeAlpacaClient()
    executor = PaperExecutor(
        fake_client,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )

    def _market_open() -> bool:
        return True

    def _chain_quote(_candidate: Any) -> tuple:
        return (1.10, 1.30, "2026-07-17", 100.0)

    def _raising_submit(**_kwargs: Any) -> str:
        raise UnderlyingUnavailable(
            "underlying 'HALT' unavailable: simulated halt"
        )

    caplog.set_level("INFO")
    rc = _drive_one_cycle(
        db_path,
        overrides={
            "armed_path": armed_path,
            "providers": all_providers,
            "submit_fn": _raising_submit,
            "market_open_check": _market_open,
            "chain_quote_fn": _chain_quote,
            "paper_executor": executor,
        },
        monkeypatch=monkeypatch,
    )
    assert rc == 0
    assert _count(db_path, "paper_orders") == 0

    underlying_logs = [
        r for r in caplog.records
        if getattr(r, "event", None) == "stage2_underlying_unavailable"
    ]
    assert len(underlying_logs) >= 1, (
        f"expected >=1 stage2_underlying_unavailable WARNING entry, got "
        f"{[r.getMessage() for r in underlying_logs]}"
    )
    assert any(r.levelname == "WARNING" for r in underlying_logs)


# ---------------------------------------------------------------------------
# Test 4 — collaborator missing → WARNING logged, no submit
# ---------------------------------------------------------------------------


def test_submit_collaborator_missing_logs_warning(
    db_path: Path,
    armed_path: Path,
    all_providers: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog,
) -> None:
    """submit_fn=None (no executor wired) → 0 paper_orders, WARNING logged."""
    _seed_pdufa(db_path, ticker="NOEXEC", days_offset=2)
    _seed_candidate(db_path, ticker="NOEXEC", dedup_seed="noexec-1")

    caplog.set_level("INFO")
    rc = _drive_one_cycle(
        db_path,
        overrides={
            "armed_path": armed_path,
            "providers": all_providers,
            "submit_fn": None,
            "market_open_check": None,
            "chain_quote_fn": None,
            "paper_executor": None,
        },
        monkeypatch=monkeypatch,
    )
    assert rc == 0
    assert _count(db_path, "paper_orders") == 0

    missing_logs = [
        r for r in caplog.records
        if getattr(r, "event", None) == "stage2_submit_collaborator_unavailable"
    ]
    assert len(missing_logs) >= 1, (
        f"expected >=1 stage2_submit_collaborator_unavailable WARNING, got "
        f"{[r.getMessage() for r in missing_logs]}"
    )
    assert any(r.levelname == "WARNING" for r in missing_logs)
