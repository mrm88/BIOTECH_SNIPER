"""Stage-2 paper-roundtrip end-to-end test — feature ``f-m3-11-paper-roundtrip-e2e``.

Live-network verification of the full Stage-2 ``news_event_entry``
pipeline against the Alpaca paper sandbox. Covers:

* **VAL-M3-060** — Synthetic candidate → ensemble → gate-pass → order
  accepted by Alpaca paper. The ensemble fan-out uses deterministic
  in-process stub providers (no live LLM endpoints) so the test
  deterministically produces a 4/4 unanimous, mean-probability ≥ 0.80
  verdict — the LLM-stack integration test surface is covered
  separately by VCR-cassette suites under ``tests/llm/``. The
  Alpaca side is the ONLY live-network component.
* **VAL-M3-061** — Telemetry rows written: ``ensemble_scores_event``
  (× 4) + ``paper_orders`` (× 1) + ``execution_events`` (≥ 1) +
  ``ticker_cooldown`` (UPSERT, exactly 1 row).
* **VAL-M3-062** — Roundtrip cancels cleanly. After acceptance the
  test calls :meth:`AlpacaClient.cancel_order(alpaca_order_id)`;
  ``execution_events`` records the cancel; the ``paper_orders.status``
  is updated to ``canceled`` (or remains in ``submitted`` /
  ``accepted`` until the broker emits the cancel — the test polls
  the broker briefly to bridge this gap).

Gating
------

This module is gated behind ``RUN_E2E=1``. Pytest's parallel mode
(``-n 2``) ALSO short-circuits because mission policy forbids live
calls inside ``pytest -n 2`` regardless of ``RUN_E2E`` — the suite
emits a visible skip rather than a silent pass-without-network so the
operator running validators can confirm the gate fired.

Per-test fixture isolates Alpaca account state by:

1. Cancelling every open order before the test runs.
2. Closing every open option position before the test runs.
3. Repeating step 1 + 2 in teardown so a flaky test does not leave
   open orders or stranded positions in the paper account.

Boundaries observed
-------------------

* No call to ``api.alpaca.markets`` (live host) anywhere in the
  module — paper-only via :data:`PAPER_BASE_URL`.
* No write to ``.armed`` or any production filesystem path.
* No write to ``llm_cost_ledger`` (stub providers report
  ``cost_usd=0.001`` which never reaches the cap gate because this
  test bypasses the dispatcher and calls the wired entry helper
  directly with a unanimous ensemble result).

The verification command for the feature is::

    RUN_E2E=1 .venv/bin/pytest -q -m e2e tests/test_stage2_paper_roundtrip.py

When the gate is closed the same command exits 0 with all cases
skipped — no live network call is made.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import pytest

from biotech_sniper import db as project_db
from biotech_sniper.alpaca_client import (
    AlpacaClient,
    AlpacaClientError,
    PAPER_BASE_URL,
)
from biotech_sniper.exec.stage2_dispatcher import EVENT_NEWS_ENTRY, build_play_card
from biotech_sniper.exec.stage2_paper_executor import submit_news_event_entry
from biotech_sniper.llm.ensemble import (
    ALL_PROVIDERS,
    EnsembleEventResult,
    ProviderResult,
    score_candidate_event,
)
from biotech_sniper.migrations.runner import run as run_v10
from biotech_sniper.paper_executor import PaperExecutor


# ---------------------------------------------------------------------------
# Module-level marker (collected node IDs carry @pytest.mark.e2e)
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Hard gate — RUN_E2E + Alpaca paper credentials.
# ---------------------------------------------------------------------------


def _xdist_parallel_active() -> bool:
    """True when pytest-xdist is running with workers (``-n N`` for N>=2).

    Mission policy: ``pytest -n 2`` MUST NEVER make live calls
    regardless of RUN_E2E. We detect xdist by checking the
    ``PYTEST_XDIST_WORKER`` env var that xdist sets on each worker.
    """
    return bool(os.environ.get("PYTEST_XDIST_WORKER"))


def _e2e_gate_open() -> tuple[bool, str]:
    """Return ``(open_p, reason)`` for the e2e gate.

    ``open_p=True`` → the test is allowed to make live network calls.
    ``open_p=False`` → skip with ``reason``.
    """
    if os.environ.get("RUN_E2E", "").strip() not in ("1", "true", "yes"):
        return False, "RUN_E2E not set; skipping live e2e roundtrip"
    if _xdist_parallel_active():
        return False, (
            "pytest-xdist parallel mode forbids live e2e calls "
            "(mission policy AGENTS.md)"
        )
    if not os.environ.get("ALPACA_KEY_ID") or not os.environ.get(
        "ALPACA_SECRET_KEY"
    ):
        return False, "ALPACA_KEY_ID / ALPACA_SECRET_KEY not provisioned"
    return True, ""


_open, _skip_reason = _e2e_gate_open()
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not _open, reason=_skip_reason),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider_callable(
    *,
    label: str = "material",
    direction: str = "bullish",
    probability: float = 0.85,
):
    def _call(candidate, *, name=None):  # noqa: ARG001
        return {
            "label": label,
            "probability": probability,
            "direction": direction,
            "rationale": f"{name} stub e2e rationale",
            "citations": [],
            "latency_ms": 5,
            "cost_usd": 0.001,
        }
    return _call


def _build_v10_db_with_candidate(
    db_path: Path,
    *,
    ticker: str,
    matched_keywords: str = "pdufa",
) -> int:
    conn = project_db.connect(db_path)
    try:
        project_db.run_migrations(conn)
    finally:
        conn.close()
    # f-misc-09: use CURRENT_VERSION so the helper stays
    # forward-compatible across schema bumps.
    run_v10(db_path, project_db.CURRENT_VERSION, take_backup_first=False)

    conn = project_db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO news_events ("
            "ticker, source, title, url, published_at, ingested_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                ticker,
                "rss",
                f"{ticker} pdufa decision granted",
                f"https://example.com/{ticker.lower()}-{uuid.uuid4().hex[:6]}",
                "2026-04-30T12:00:00Z",
                "2026-04-30T12:00:01Z",
            ),
        )
        nid = conn.execute("SELECT MAX(id) FROM news_events").fetchone()[0]
        conn.execute(
            "INSERT INTO candidate_events ("
            "ticker, source_news_event_id, matched_keywords,"
            " emitted_at, dedup_key) VALUES (?,?,?,?,?)",
            (
                ticker,
                nid,
                matched_keywords,
                "2026-04-30T12:00:02Z",
                f"dedup-{ticker.lower()}-{uuid.uuid4().hex[:6]}",
            ),
        )
        cid = conn.execute(
            "SELECT MAX(id) FROM candidate_events"
        ).fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return int(cid)


def _fanout_4_unanimous(
    *,
    db_path: Path,
    candidate_event_id: int,
    ticker: str,
    run_id: str,
) -> EnsembleEventResult:
    """Run the Stage-2 fan-out with 4 deterministic stub providers.

    Persists 4 ``ensemble_scores_event`` rows and returns the
    (unanimous-material, bullish, mean=0.85) verdict.
    """
    providers = {
        name: _make_provider_callable() for name in ALL_PROVIDERS
    }
    er = score_candidate_event(
        {"id": candidate_event_id, "ticker": ticker},
        run_id=run_id,
        db_path=db_path,
        providers=providers,
    )
    assert er.gate_failed_reason is None, (
        f"ensemble gate must pass; got {er.gate_failed_reason}"
    )
    assert er.label == "material"
    assert er.direction == "bullish"
    assert er.successful_providers == list(ALL_PROVIDERS)
    return er


# ---------------------------------------------------------------------------
# Fixture — Alpaca paper account isolation per-test
# ---------------------------------------------------------------------------


def _cancel_all_open_orders(client: AlpacaClient) -> None:
    """Best-effort: cancel every open order in the paper account.

    Tolerates ``AlpacaClientError`` so a partial cancel does not
    abort fixture teardown.
    """
    try:
        # The trading client exposes get_orders via duck-typing; in
        # the absence of a wrapper helper we walk the underlying
        # client. Narrow surface so the fixture works without
        # importing alpaca-py directly.
        trading = client._trading  # noqa: SLF001 — fixture-internal
        get_orders = getattr(trading, "get_orders", None)
        if callable(get_orders):
            try:
                from alpaca.trading.requests import GetOrdersRequest

                req = GetOrdersRequest(status="open")
                orders = get_orders(filter=req) or []
            except Exception:  # pragma: no cover — defensive
                orders = []
            for order in orders:
                oid = getattr(order, "id", None)
                if oid:
                    try:
                        client.cancel_order(str(oid))
                    except AlpacaClientError:
                        pass
    except Exception:  # pragma: no cover — fixture must never raise
        pass


def _close_all_option_positions(client: AlpacaClient) -> None:
    """Best-effort: liquidate every open OPTION position.

    Equity holdings (if any) are deliberately ignored — they belong
    to other paths and the test never opens equity positions.
    """
    try:
        positions = client.get_positions() or []
    except AlpacaClientError:
        return
    trading = getattr(client, "_trading", None)
    close_position = (
        getattr(trading, "close_position", None) if trading else None
    )
    if not callable(close_position):
        return
    for pos in positions:
        if pos.get("asset_class") != "us_option":
            continue
        sym = pos.get("symbol")
        if not sym:
            continue
        try:
            close_position(sym)
        except Exception:  # pragma: no cover — best-effort
            pass


@pytest.fixture
def isolated_alpaca_paper() -> AlpacaClient:
    """Per-test Alpaca paper-client fixture.

    Pre-test: cancels every open order + closes every option
    position so the fan-out / cap-projection arithmetic operates on
    a clean slate.

    Post-test: repeats the cleanup so a flaky case does not leave
    stranded paper positions for the next worker.
    """
    client = AlpacaClient()
    assert client.base_url == PAPER_BASE_URL, (
        "e2e fixture refuses non-paper Alpaca client"
    )
    _cancel_all_open_orders(client)
    _close_all_option_positions(client)
    yield client
    _cancel_all_open_orders(client)
    _close_all_option_positions(client)


# ---------------------------------------------------------------------------
# Test 1 — Full happy-path roundtrip with telemetry assertions
# ---------------------------------------------------------------------------


def test_full_roundtrip_with_telemetry(
    tmp_path: Path,
    isolated_alpaca_paper: AlpacaClient,
):
    """End-to-end roundtrip: synthetic candidate → ensemble → gates pass
    → ``submit_news_event_entry`` accepted by Alpaca paper.

    Telemetry assertions (VAL-M3-061):

    * ``ensemble_scores_event`` count for the candidate = 4.
    * ``paper_orders`` count for ``event='news_event_entry'`` = 1.
    * ``execution_events`` count for the order ≥ 1 (at minimum the
      ``submitted`` row written by the executor's wired emit).
    * ``ticker_cooldown`` count for the ticker = 1.
    """
    # 1) Build a fresh tmp_path SQLite db at v10 with a real
    # candidate_events row (FK target for ticker_cooldown).
    ticker = f"ETEST{uuid.uuid4().hex[:4].upper()}"
    db_path = tmp_path / "alpha.db"
    candidate_event_id = _build_v10_db_with_candidate(
        db_path, ticker=ticker, matched_keywords="pdufa"
    )

    # 2) Run the Stage-2 fan-out with 4 stub providers — persists 4
    # ensemble_scores_event rows.
    run_id = f"e2e-{uuid.uuid4().hex}"
    er = _fanout_4_unanimous(
        db_path=db_path,
        candidate_event_id=candidate_event_id,
        ticker=ticker,
        run_id=run_id,
    )

    # 3) Build the executor against the live paper Alpaca client.
    executor = PaperExecutor(
        isolated_alpaca_paper,
        db_path=db_path,
        poll_interval_seconds=0.0,
    )
    # The constructor brought db to v9; we already advanced to v10.

    # 4) Submit the news_event_entry. The synthetic ticker will not
    # have a real underlying tradable, so we disable the pre-check
    # and rely on the broker's normal rejection / acceptance path
    # for the OCC option symbol the dispatcher constructs. To
    # ensure the broker accepts a real options chain, we use a
    # well-known liquid biotech underlying instead of the synthetic
    # ticker for the actual broker call. Map the test ticker to a
    # real liquid underlying via the candidate event's ticker
    # field — but keep the candidate row's ticker as the synthetic
    # one so the cooldown / paper_orders rows are uniquely tagged
    # for this test.
    #
    # Pragmatic approach: use a real liquid biotech ticker for the
    # broker call. The test asserts on the synthetic-ticker
    # candidate row by candidate_event_id, not by ticker name, so
    # the SQL filters remain accurate.
    real_underlying = os.environ.get("E2E_TEST_TICKER", "MRNA").strip().upper()

    # Re-issue a candidate row whose ticker matches the real
    # underlying so the cooldown UPSERT records the live ticker
    # (the paper_orders.event filter does not depend on ticker, so
    # ensemble + paper_orders + execution_events assertions remain
    # tied to candidate_event_id / alpaca_order_id).
    candidate_event_id_real = _build_v10_db_with_candidate(
        db_path,
        ticker=real_underlying,
        matched_keywords="pdufa",
    )
    er_real = _fanout_4_unanimous(
        db_path=db_path,
        candidate_event_id=candidate_event_id_real,
        ticker=real_underlying,
        run_id=f"{run_id}-real",
    )

    # Resolve a near-the-money chain quote — pick the front-month
    # expiry returned by the broker and grab a single OTM call so
    # the executor can size at $250 cap.
    chain = isolated_alpaca_paper.get_options_chain(real_underlying)
    if not chain:
        pytest.skip(
            f"no options chain returned for {real_underlying}; "
            "broker may have empty chain outside market hours"
        )

    # Find the live underlying spot.
    spot = isolated_alpaca_paper.get_latest_trade(real_underlying)
    if spot is None or spot <= 0:
        pytest.skip(
            f"no live trade for {real_underlying}; market may be closed"
        )

    # Pick a call slightly OTM with a tradable mid <= $2 (so qty>=1
    # at $250 cap).
    candidates = [
        row
        for row in chain
        if row.get("type", "").lower().startswith("c")
        and row.get("strike") is not None
        and float(row["strike"]) > float(spot)
        and row.get("bid") and row.get("ask")
    ]
    candidates.sort(
        key=lambda r: (float(r["strike"]) - float(spot), float(r.get("ask", 99)))
    )
    chosen = next(
        (r for r in candidates if float(r.get("ask", 99)) * 100 <= 250),
        None,
    )
    if chosen is None:
        pytest.skip(
            f"no affordable OTM call for {real_underlying} at $250 cap"
        )

    expiry = chosen.get("expiry")

    # 5) Submit. The wiring records cooldown UPSERT on success.
    candidate_row = {
        "id": candidate_event_id_real,
        "ticker": real_underlying,
        "matched_keywords": "pdufa",
    }
    alpaca_order_id = submit_news_event_entry(
        executor,
        candidate_event=candidate_row,
        ensemble_result=er_real,
        stock_price=float(spot),
        bid=float(chosen.get("bid") or 0.5),
        ask=float(chosen.get("ask") or 0.5),
        expiry=expiry,
        # Live tradability is the broker's job at this point — we
        # already confirmed via get_latest_trade above.
        check_underlying_tradable=False,
    )
    assert isinstance(alpaca_order_id, str) and alpaca_order_id, (
        "broker must return a non-empty order id"
    )

    # 6) Telemetry assertions.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensemble_count = conn.execute(
            "SELECT COUNT(*) FROM ensemble_scores_event "
            "WHERE candidate_event_id=?",
            (candidate_event_id_real,),
        ).fetchone()[0]

        paper_rows = conn.execute(
            "SELECT id, alpaca_order_id, status, event "
            "FROM paper_orders "
            "WHERE alpaca_order_id=?",
            (alpaca_order_id,),
        ).fetchall()

        cooldown_rows = conn.execute(
            "SELECT ticker, last_event_id, last_entry_at "
            "FROM ticker_cooldown WHERE ticker=?",
            (real_underlying,),
        ).fetchall()

        # paper_orders.id is the FK target for execution_events.
        assert len(paper_rows) == 1
        internal_id = paper_rows[0]["id"]
        execution_count = conn.execute(
            "SELECT COUNT(*) FROM execution_events "
            "WHERE paper_order_id=?",
            (internal_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert ensemble_count == 4, (
        f"VAL-M3-061: expected 4 ensemble_scores_event rows; "
        f"got {ensemble_count}"
    )
    assert paper_rows[0]["event"] == EVENT_NEWS_ENTRY
    assert paper_rows[0]["status"] not in ("rejected",)
    assert execution_count >= 1, (
        f"VAL-M3-061: expected ≥1 execution_events row; "
        f"got {execution_count}"
    )
    assert len(cooldown_rows) == 1, (
        f"VAL-M3-061: expected exactly 1 ticker_cooldown row; "
        f"got {len(cooldown_rows)}"
    )
    assert cooldown_rows[0]["last_event_id"] == candidate_event_id_real

    # ----------------------------------------------------------------
    # 7) VAL-M3-062 — clean cancel exercised via the PRODUCTION
    # subscriber path (NOT the manual writer).
    #
    # The cancel-telemetry assertion MUST be driven by
    # :class:`biotech_sniper.execution_subscriber.ExecutionSubscriber`
    # because that is the same entrypoint the production
    # ``alpha-sniper-news`` daemon (and the
    # ``execution_subscriber --poll-once`` cron CLI) invokes against
    # the live broker. Calling :func:`record_execution_event`
    # directly here would bypass the subscriber's broker-status →
    # ``event_type`` mapping AND its open-orders walk, which means a
    # regression in either of those production code paths would
    # leave VAL-M3-062 GREEN despite the subscriber-driven cancel
    # path being broken (the round-1 scrutiny finding that this
    # f-fix-m3-11 feature corrects).
    #
    # Likewise, ``paper_orders.status`` is mirrored via the
    # production helper :meth:`PaperExecutor.wait_for_fill` (which
    # internally calls ``_update_order_status``) — the same helper
    # exercised by ``tests/test_paper_executor.py``. The contract at
    # validation-contract.md line 1169 explicitly requires
    # "paper_orders.status becomes canceled".
    # ----------------------------------------------------------------
    isolated_alpaca_paper.cancel_order(alpaca_order_id)

    # Poll briefly for the broker to reflect the cancel — the
    # paper sandbox usually returns ``canceled`` within a few
    # hundred ms; we poll up to ~3s in 0.25s slices to bridge the
    # gap without flaking on slow networks.
    canceled = False
    for _ in range(12):
        try:
            order_after = isolated_alpaca_paper.get_order(alpaca_order_id)
        except AlpacaClientError:
            order_after = None
        status_after = (
            (order_after or {}).get("status", "").lower()
        )
        if status_after in ("canceled", "cancelled", "expired"):
            canceled = True
            break
        time.sleep(0.25)
    assert canceled, (
        f"VAL-M3-062: order {alpaca_order_id} did not reach canceled "
        f"status after cancel_order"
    )

    # Drive the cancel-telemetry write through the production
    # subscriber. ``poll_once`` walks every non-terminal
    # ``paper_orders`` row, fetches the broker-side state via
    # :meth:`AlpacaClient.get_order`, maps the status through
    # ``_BROKER_STATUS_TO_EVENT_TYPE`` and inserts the
    # corresponding ``execution_events`` row via
    # :func:`record_execution_event` — the SAME entrypoint the
    # daemon calls in production. We invoke it twice: once to
    # capture any intermediate ``accepted`` lifecycle event the
    # broker emitted after submission (so the canonical
    # state-transition graph stays satisfied), and once after the
    # cancel reaches the broker so the final ``canceled`` row
    # lands.
    from biotech_sniper.execution_subscriber import ExecutionSubscriber

    subscriber = ExecutionSubscriber(
        client=isolated_alpaca_paper, db_path=db_path
    )
    # First poll: catches the broker's pre-cancel ``accepted``
    # transition (Alpaca paper typically accepts within ms of
    # submission) so the subsequent ``canceled`` write satisfies
    # the LEGAL_TRANSITIONS graph (``accepted`` → ``canceled``).
    subscriber.poll_once()
    # Second poll: records the broker-confirmed cancel via the
    # production code path.
    subscriber.poll_once()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        last = conn.execute(
            "SELECT event_type FROM execution_events "
            "WHERE paper_order_id=? "
            "ORDER BY id DESC LIMIT 1",
            (internal_id,),
        ).fetchone()
    finally:
        conn.close()
    assert last is not None and last["event_type"] == "canceled", (
        f"VAL-M3-062: subscriber-driven cancel must yield "
        f"execution_events.event_type='canceled'; got "
        f"{dict(last) if last else None}"
    )

    # Mirror ``paper_orders.status`` via the production executor
    # helper. ``wait_for_fill`` calls ``_update_order_status``
    # exactly as production does, ensuring the local row reflects
    # the broker's terminal status. ``timeout_seconds=5`` keeps the
    # poll bounded; ``poll_interval_seconds=0`` makes the loop
    # tight since the broker is already at the terminal
    # ``canceled`` state. The duplicate ``canceled`` event row is
    # de-duped inside ``_record_event_for_status`` (no double
    # write), and the LEGAL_TRANSITIONS validator is satisfied.
    executor.wait_for_fill(
        alpaca_order_id,
        timeout_seconds=5.0,
        poll_interval_seconds=0.0,
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        status_row = conn.execute(
            "SELECT status FROM paper_orders WHERE id=?",
            (internal_id,),
        ).fetchone()
    finally:
        conn.close()
    assert status_row is not None and status_row["status"] == "canceled", (
        f"VAL-M3-062: paper_orders.status must mirror the broker "
        f"cancel; got {dict(status_row) if status_row else None}"
    )


# ---------------------------------------------------------------------------
# Test 2 — Re-running same run_id is ensemble-idempotent (live)
# ---------------------------------------------------------------------------


def test_re_run_same_run_id_is_idempotent(
    tmp_path: Path,
    isolated_alpaca_paper: AlpacaClient,
):
    """Calling :func:`score_candidate_event` twice with the same
    ``run_id`` for the same ``candidate_event_id`` produces 4 rows
    total in ``ensemble_scores_event`` (VAL-M3-068 confirmed in the
    e2e environment).
    """
    ticker = f"ITEST{uuid.uuid4().hex[:4].upper()}"
    db_path = tmp_path / "alpha.db"
    candidate_event_id = _build_v10_db_with_candidate(
        db_path, ticker=ticker
    )
    run_id = f"e2e-idem-{uuid.uuid4().hex}"

    _fanout_4_unanimous(
        db_path=db_path,
        candidate_event_id=candidate_event_id,
        ticker=ticker,
        run_id=run_id,
    )
    _fanout_4_unanimous(
        db_path=db_path,
        candidate_event_id=candidate_event_id,
        ticker=ticker,
        run_id=run_id,
    )

    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM ensemble_scores_event "
            "WHERE candidate_event_id=?",
            (candidate_event_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 4
