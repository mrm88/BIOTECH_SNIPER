"""End-to-end CHEAP-first short-circuit invariants
(feature ``f-m5-04-cheap-first-side-effect-invariants``).

Verifies that the documented Reading-B Stage-2 gate ordering — cooldown
→ armed → cap → fan-out → unanimity → probability — produces ZERO
side effects on the rejection paths. Specifically:

* **Cooldown active (VAL-M5-028)** — Pre-seeded ``ticker_cooldown``
  row blocks the candidate at the cheap-first cooldown gate. Across
  **100 sequential trials**, the ``llm_cost_ledger`` count delta is
  exactly ``0`` (cheap-first proven: cooldown precedes any LLM
  fan-out).
* **Armed-file missing (VAL-M5-029)** — ``.armed`` absent → armed gate
  rejects BEFORE LLM fan-out. Across **100 trials**, the
  ``llm_cost_ledger`` count delta is exactly ``0`` (cheap-first
  proven: armed precedes any LLM fan-out).
* **Daily cap hit (VAL-M5-030)** — Pre-seeded ``llm_cost_ledger``
  rows summing ≥ ``LLM_STAGE2_DAILY_USD_CAP`` ($20) trip the cap
  gate. Across **100 trials**, the fake Alpaca client's
  ``submit_order`` / ``get_order`` / ``get_positions`` /
  ``get_latest_trade`` call counts are all exactly ``0`` (cap-then-
  submit proven: cap rejection short-circuits both LLM fan-out AND
  order submission).
* **Threshold / unanimity fail (VAL-M5-031)** — All cheap gates
  pass, fan-out completes, but the post-fanout score gates reject
  (parametrised across 3/4-mixed-labels AND avg-p<0.75 cases).
  Across **100 trials per case**, the fake Alpaca client's call
  counts are all exactly ``0`` (score-then-submit proven).

The 100-trial repetition is the explicit ``expectedBehavior`` from
the feature brief — running each rejection scenario 100 times and
asserting on the post-loop count delta amounts to a non-flake
equivalent of the documented "ZERO" invariant. Single-trial
versions of these assertions live in
``tests/e2e/test_rejection_paths.py``; this file complements those
tests by proving the no-side-effect property holds under repeated
fire.

Hermeticity
-----------

The test never touches the real Alpaca paper API or any LLM
endpoint. All four ensemble providers are stubbed with
deterministic callables that NEVER write to ``llm_cost_ledger``
(stubs return plain dicts; they don't call any provider client).
The broker is a duck-typed :class:`_FakeAlpacaClient` whose
``submit_order`` raises an :class:`AssertionError` on call so any
attempted submission fails LOUDLY in addition to bumping the call
counter.

The test is decorated with ``@pytest.mark.e2e`` so it is selected
by ``pytest -m e2e``.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Optional

import pytest

from biotech_sniper.alpaca_client import PAPER_BASE_URL
from biotech_sniper.exec.stage2_dispatcher import (
    Stage2ChainResult,
    run_stage2_chain,
)
from biotech_sniper.exec.stage2_paper_executor import submit_news_event_entry
from biotech_sniper.llm.ensemble import ALL_PROVIDERS
from biotech_sniper.llm.stage2_gates import (
    GATE_REASON_ARMED_FILE_MISSING,
    GATE_REASON_COOLDOWN_ACTIVE,
    GATE_REASON_DAILY_CAP_EXCEEDED,
    GATE_REASON_PROBABILITY_BELOW_THRESHOLD,
    GATE_REASON_UNANIMITY_FAILED,
    STAGE2_LEDGER_PURPOSE,
)
from biotech_sniper.migrations.runner import run as run_migrations_runner
from biotech_sniper.news_daemon.emit import run_one_poll_cycle
from biotech_sniper.paper_executor import PaperExecutor


pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Synthetic fixture data
# ---------------------------------------------------------------------------


_TICKER: str = "MRNS"
_HEADLINE: str = "MRNS phase 3 readout: primary endpoint announced"
_SOURCE: str = "test_cheap_first_side_effects"
_PUBLISHED_AT: str = "2026-04-30T14:00:00.000Z"

#: Trial count for the per-scenario repetition loop. The feature
#: brief specifies "ZERO ... over 100 trials" — the constant is
#: extracted to a module-level name so both the docstring and the
#: assertion message reference the same value.
N_TRIALS: int = 100


# ---------------------------------------------------------------------------
# Fake Alpaca client double — never expected to be invoked on rejection paths.
# ---------------------------------------------------------------------------


class _FakeAlpacaClient:
    """Duck-typed Alpaca client used by both rejection AND positive paths.

    Every Alpaca-surface method increments a counter so a test can
    assert the post-loop count delta is exactly ``0`` for the
    cap-rejection and threshold/unanimity-rejection scenarios. When
    constructed WITHOUT ``submit_order_result`` / ``get_order_results``
    (the rejection-path default), :meth:`submit_order` and
    :meth:`get_order` raise :class:`AssertionError` so any
    accidental broker contact fails LOUDLY in addition to bumping
    the call counter.

    When constructed WITH ``submit_order_result`` /
    ``get_order_results`` (the positive-path / sibling test), the
    fake walks the broker state-machine deterministically — the
    submission returns the queued payload and ``get_order`` walks
    a left-to-right queue (last entry sticks) mirroring the
    pattern in ``tests/e2e/test_bull_flow.py``. This dual mode lets
    the same helper (``_run_chain_then_submit``) drive both flavors
    of test against an identical fake client.
    """

    def __init__(
        self,
        *,
        base_url: str = PAPER_BASE_URL,
        submit_order_result: Any = None,
        get_order_results: Optional[list[Any]] = None,
        latest_trade_price: float = 25.0,
        positions: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        self.base_url = base_url
        self.submit_calls: list[Any] = []
        self.get_order_calls: list[str] = []
        self.get_positions_calls: int = 0
        self.get_latest_trade_calls: list[str] = []
        self._submit_result = submit_order_result
        self._get_order_results = list(get_order_results or [])
        self._latest_trade_price = latest_trade_price
        self._positions = list(positions or [])

    def get_positions(self) -> list[dict[str, Any]]:
        self.get_positions_calls += 1
        return list(self._positions)

    def submit_order(self, order_request: Any) -> dict[str, Any]:
        # Record FIRST so a debugging post-mortem can read the call.
        self.submit_calls.append(order_request)
        if self._submit_result is None:
            raise AssertionError(
                "_FakeAlpacaClient.submit_order MUST NOT be called on a "
                "rejection-path test (cheap-first invariant)"
            )
        return self._submit_result

    def get_order(self, order_id: str) -> dict[str, Any]:
        self.get_order_calls.append(order_id)
        if not self._get_order_results:
            raise AssertionError(
                "_FakeAlpacaClient.get_order MUST NOT be called on a "
                "rejection-path test (cheap-first invariant)"
            )
        result = self._get_order_results[0]
        if len(self._get_order_results) > 1:
            self._get_order_results.pop(0)
        return result

    def get_latest_trade(self, ticker: str) -> Optional[float]:
        self.get_latest_trade_calls.append(ticker)
        return self._latest_trade_price

    def total_call_count(self) -> int:
        """Sum across all four Alpaca-surface methods."""
        return (
            len(self.submit_calls)
            + len(self.get_order_calls)
            + self.get_positions_calls
            + len(self.get_latest_trade_calls)
        )


# ---------------------------------------------------------------------------
# Stub provider tracker — counts per-provider invocations + emits payloads.
# ---------------------------------------------------------------------------


class _StubProviderTracker:
    """Tracks provider invocations + emits configurable per-provider payloads."""

    def __init__(self) -> None:
        self.call_counts: dict[str, int] = {p: 0 for p in ALL_PROVIDERS}

    def make_uniform_providers(
        self,
        *,
        label: str = "material",
        direction: str = "bullish",
        probability: float = 0.85,
    ) -> dict[str, Any]:
        """Return a ``{provider_name: callable}`` mapping with identical payloads."""

        def _factory(provider_name: str):
            def _stub(_row: Any, *, name: str = provider_name) -> dict[str, Any]:
                self.call_counts[name] += 1
                return {
                    "label": label,
                    "probability": probability,
                    "direction": direction,
                    "rationale": f"{name}-stub",
                    "citations": [],
                    "latency_ms": 10,
                    "cost_usd": 0.001,
                }

            return _stub

        return {p: _factory(p) for p in ALL_PROVIDERS}

    def make_per_provider_providers(
        self,
        per_provider: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Return ``{provider_name: callable}`` returning ``per_provider[name]``."""

        def _factory(provider_name: str):
            payload = dict(per_provider[provider_name])

            def _stub(_row: Any, *, name: str = provider_name) -> dict[str, Any]:
                self.call_counts[name] += 1
                return dict(payload)

            return _stub

        return {p: _factory(p) for p in ALL_PROVIDERS}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Bring a fresh sqlite db up to schema v10 (Reading-B foundations)."""
    db = tmp_path / "alpha_sniper_e2e.db"
    run_migrations_runner(db, target_version=10, take_backup_first=False)
    return db


@pytest.fixture
def armed_path(tmp_path: Path) -> Path:
    """Create a dummy ``.armed`` marker so the armed gate passes."""
    p = tmp_path / ".armed"
    p.write_text("e2e-cheap-first", encoding="utf-8")
    return p


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    """Path used for the ``audit_latest.json`` Reading-B summary write."""
    return tmp_path / "state" / "audit_latest.json"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _seed_synthetic_news_event(db_path: Path) -> int:
    """Insert one synthetic news_events row and return its id."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            """
            INSERT INTO news_events (
                ticker, source, published_at, title, url, raw_payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _TICKER,
                _SOURCE,
                _PUBLISHED_AT,
                _HEADLINE,
                "https://example.com/mrns-readout",
                _HEADLINE,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _candidate_event_row(db_path: Path, news_event_id: int) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT id, ticker, source_news_event_id, matched_keywords,
                   calendar_match, emitted_at, dedup_key
            FROM candidate_events
            WHERE source_news_event_id = ?
            """,
            (news_event_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "candidate_events row missing"
    return dict(row)


def _count(db_path: Path, sql: str, params: tuple = ()) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    finally:
        conn.close()


def _emit_candidate(db_path: Path) -> dict[str, Any]:
    """Seed a news_events row, run Stage-1 once, return the candidate row."""
    news_event_id = _seed_synthetic_news_event(db_path)
    assert news_event_id > 0
    scanned, inserted = run_one_poll_cycle(
        str(db_path), polled_tickers=[_TICKER], after_id=0,
    )
    assert scanned == 1, "Stage-1 should scan the seed row"
    assert inserted == 1, "Stage-1 should insert one candidate"
    return _candidate_event_row(db_path, news_event_id)


def _seed_active_cooldown(db_path: Path) -> None:
    """Insert a fresh ticker_cooldown row 1h ago — well within the 24h window."""
    one_hour_ago = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)
    )
    last_entry_at = (
        one_hour_ago.strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{one_hour_ago.microsecond // 1000:03d}Z"
    )
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO ticker_cooldown
                    (ticker, last_entry_at, last_event_id, cooldown_hours)
                VALUES (?, ?, NULL, 24)
                """,
                (_TICKER, last_entry_at),
            )
    finally:
        conn.close()


def _seed_cap_hit_ledger(db_path: Path) -> None:
    """Insert a $20.00 stage2 ledger row dated today so cap projection trips."""
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO llm_cost_ledger (
                    provider, model_id, purpose, cost_usd, called_at
                ) VALUES (
                    'perplexity', 'sonar', ?, 20.00,
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                )
                """,
                (STAGE2_LEDGER_PURPOSE,),
            )
    finally:
        conn.close()


def _run_chain_then_submit(
    chain_result: Stage2ChainResult,
    executor_with_fake_client: PaperExecutor,
    *,
    candidate_event: Mapping[str, Any],
    stock_price: float = 25.0,
    bid: float = 1.10,
    ask: float = 1.30,
    expiry: str = "2026-07-17",
) -> Optional[str]:
    """Production-wiring shim: invoke ``submit_news_event_entry`` iff chain passed.

    The Stage-2 cheap-first contract is a TWO-stage handshake:

    1. :func:`run_stage2_chain` decides pass/fail PURELY on cheap
       gates and the LLM ensemble — it performs **zero broker
       I/O** by design.
    2. The caller, on ``passed=True``, then invokes
       :func:`submit_news_event_entry` against the
       :class:`PaperExecutor` to actually contact the broker.

    The earlier ``_maybe_submit`` stand-in re-checked that
    ``run_stage2_chain`` itself did not touch the fake client and
    that the chain rejected — but the
    :class:`_FakeAlpacaClient` was NEVER injected into the
    production submit path on either branch, which made the
    "zero broker I/O" assertion trivially true regardless of
    contract correctness. That gap was surfaced by f-fix-m5-e2e-rigor
    against VAL-M5-030 / VAL-M5-031 on 2026-05-01.

    This helper closes the loop: it mirrors production wiring (``if
    chain_result.passed: submit_news_event_entry(executor, ...)``),
    so the rejection-path loops genuinely exercise the same
    code path the daemon would run with the same fake client. If
    the chain rejected, the helper short-circuits and returns
    ``None`` — preserving the cheap-first invariant that the
    broker is never contacted on a rejection.

    A sibling positive-path test ``test_passing_chain_reaches_alpaca``
    drives this helper with a chain configured to pass and asserts
    ``fake_client.total_call_count() > 0`` — proving the wiring is
    real (not dead code), so the rejection-path zero-call
    assertions carry meaning.
    """
    if not chain_result.passed:
        # Production: the caller skips the executor on a rejection.
        return None

    ensemble_result = chain_result.ensemble_result
    assert ensemble_result is not None, (
        "passed chain must carry an ensemble_result"
    )
    return submit_news_event_entry(
        executor_with_fake_client,
        candidate_event=candidate_event,
        ensemble_result=ensemble_result,
        stock_price=stock_price,
        bid=bid,
        ask=ask,
        expiry=expiry,
    )


# ---------------------------------------------------------------------------
# Test 1 — Cooldown active → ZERO llm_cost_ledger rows over 100 trials.
# ---------------------------------------------------------------------------


def test_cooldown_active_zero_llm_cost_ledger_over_100_trials(
    db_path: Path,
    armed_path: Path,
    audit_path: Path,
) -> None:
    """Cheap-first cooldown gate proven: zero ledger writes over 100 trials.

    Walks VAL-M5-028: ``count delta of llm_cost_ledger over the
    cooldown-rejected test run is 0``. This file's variant takes
    the assertion further by repeating the rejection ``N_TRIALS=100``
    times and asserting on the *cumulative* count delta — proving
    the cheap-first invariant is non-flake under repeated fire.
    """
    candidate = _emit_candidate(db_path)
    _seed_active_cooldown(db_path)

    tracker = _StubProviderTracker()
    providers = tracker.make_uniform_providers()
    fake_client = _FakeAlpacaClient()
    executor = PaperExecutor(
        fake_client,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )

    cost_before = _count(db_path, "SELECT COUNT(*) FROM llm_cost_ledger")

    rejections = 0
    for _ in range(N_TRIALS):
        chain_result = run_stage2_chain(
            candidate_event_row=candidate,
            db_path=db_path,
            armed_path=armed_path,
            audit_path=audit_path,
            providers=providers,
        )
        assert chain_result.passed is False
        assert chain_result.reason == GATE_REASON_COOLDOWN_ACTIVE
        assert chain_result.gate == "cooldown"
        assert chain_result.ensemble_result is None
        # Production wiring: invoke submit_news_event_entry only on
        # passed=True. Helper short-circuits on rejection here.
        submit_result = _run_chain_then_submit(
            chain_result, executor, candidate_event=candidate,
        )
        assert submit_result is None
        rejections += 1
    assert rejections == N_TRIALS

    cost_after = _count(db_path, "SELECT COUNT(*) FROM llm_cost_ledger")
    assert cost_after - cost_before == 0, (
        f"VAL-M5-028: llm_cost_ledger row count must be unchanged "
        f"across {N_TRIALS} cooldown-rejected trials; "
        f"before={cost_before} after={cost_after}"
    )

    # ZERO providers ever invoked across all 100 trials.
    assert all(tracker.call_counts[p] == 0 for p in ALL_PROVIDERS), (
        tracker.call_counts
    )

    # VAL-M5-028 cheap-first cooldown — zero Alpaca call attempts
    # via the production wiring helper.
    assert fake_client.total_call_count() == 0, (
        f"cooldown-rejection path must NOT touch the fake Alpaca "
        f"client; total_call_count={fake_client.total_call_count()}"
    )


# ---------------------------------------------------------------------------
# Test 2 — .armed missing → ZERO llm_cost_ledger rows over 100 trials.
# ---------------------------------------------------------------------------


def test_armed_missing_zero_llm_cost_ledger_over_100_trials(
    db_path: Path,
    tmp_path: Path,
    audit_path: Path,
) -> None:
    """Cheap-first armed gate proven: zero ledger writes over 100 trials.

    Walks VAL-M5-029: ``count delta of llm_cost_ledger over the
    armed-rejected test run is 0``. This file's variant repeats the
    rejection ``N_TRIALS=100`` times.
    """
    candidate = _emit_candidate(db_path)
    missing_armed = tmp_path / "no-armed-here"
    assert not missing_armed.exists()

    tracker = _StubProviderTracker()
    providers = tracker.make_uniform_providers()
    fake_client = _FakeAlpacaClient()
    executor = PaperExecutor(
        fake_client,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )

    cost_before = _count(db_path, "SELECT COUNT(*) FROM llm_cost_ledger")

    for _ in range(N_TRIALS):
        chain_result = run_stage2_chain(
            candidate_event_row=candidate,
            db_path=db_path,
            armed_path=missing_armed,
            audit_path=audit_path,
            providers=providers,
        )
        assert chain_result.passed is False
        assert chain_result.reason == GATE_REASON_ARMED_FILE_MISSING
        assert chain_result.gate == "armed"
        assert chain_result.ensemble_result is None
        # Production wiring: rejection short-circuits BEFORE submit.
        submit_result = _run_chain_then_submit(
            chain_result, executor, candidate_event=candidate,
        )
        assert submit_result is None

    cost_after = _count(db_path, "SELECT COUNT(*) FROM llm_cost_ledger")
    assert cost_after - cost_before == 0, (
        f"VAL-M5-029: llm_cost_ledger row count must be unchanged "
        f"across {N_TRIALS} armed-rejected trials; "
        f"before={cost_before} after={cost_after}"
    )

    # ZERO providers ever invoked across all 100 trials.
    assert all(tracker.call_counts[p] == 0 for p in ALL_PROVIDERS), (
        tracker.call_counts
    )

    # VAL-M5-029 cheap-first armed — zero Alpaca call attempts via
    # the production wiring helper.
    assert fake_client.total_call_count() == 0, (
        f"armed-rejection path must NOT touch the fake Alpaca "
        f"client; total_call_count={fake_client.total_call_count()}"
    )


# ---------------------------------------------------------------------------
# Test 3 — Daily cap hit → ZERO Alpaca calls over 100 trials.
# ---------------------------------------------------------------------------


def test_cap_hit_zero_alpaca_calls_over_100_trials(
    db_path: Path,
    armed_path: Path,
    audit_path: Path,
) -> None:
    """Cap-then-submit proven: zero broker calls over 100 trials.

    Walks VAL-M5-030: ``alpaca_cassette.play_count == 0``. This
    file's variant repeats the rejection ``N_TRIALS=100`` times and
    asserts the fake Alpaca client was never contacted at any
    point. The cap gate fires BEFORE LLM fan-out, so this also
    implies zero new ``llm_cost_ledger`` rows beyond the seed row.
    """
    candidate = _emit_candidate(db_path)
    _seed_cap_hit_ledger(db_path)

    tracker = _StubProviderTracker()
    providers = tracker.make_uniform_providers()
    fake_client = _FakeAlpacaClient()
    executor = PaperExecutor(
        fake_client,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )

    cost_before = _count(db_path, "SELECT COUNT(*) FROM llm_cost_ledger")
    assert cost_before == 1, "test seed should leave exactly one ledger row"

    for _ in range(N_TRIALS):
        chain_result = run_stage2_chain(
            candidate_event_row=candidate,
            db_path=db_path,
            armed_path=armed_path,
            audit_path=audit_path,
            providers=providers,
        )
        assert chain_result.passed is False
        assert chain_result.reason == GATE_REASON_DAILY_CAP_EXCEEDED
        assert chain_result.gate == "cap"
        assert chain_result.ensemble_result is None
        # Production wiring: invoke submit_news_event_entry only on
        # passed=True. Helper short-circuits on rejection here so
        # the fake client genuinely sees zero broker I/O on the
        # cap-rejection path.
        submit_result = _run_chain_then_submit(
            chain_result, executor, candidate_event=candidate,
        )
        assert submit_result is None

    cost_after = _count(db_path, "SELECT COUNT(*) FROM llm_cost_ledger")
    assert cost_after - cost_before == 0, (
        f"VAL-M5-030: llm_cost_ledger row count must be unchanged "
        f"across {N_TRIALS} cap-rejected trials; "
        f"before={cost_before} after={cost_after}"
    )

    # VAL-M5-030 — zero Alpaca call attempts.
    assert fake_client.submit_calls == []
    assert fake_client.get_order_calls == []
    assert fake_client.get_positions_calls == 0
    assert fake_client.get_latest_trade_calls == []
    assert fake_client.total_call_count() == 0, (
        f"VAL-M5-030: fake Alpaca client must NOT be contacted on "
        f"cap-rejection path; total_call_count={fake_client.total_call_count()}"
    )

    # ZERO providers ever invoked across all 100 trials.
    assert all(tracker.call_counts[p] == 0 for p in ALL_PROVIDERS), (
        tracker.call_counts
    )


# ---------------------------------------------------------------------------
# Test 4 — Threshold/unanimity rejections → ZERO Alpaca calls over 100 trials.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario_id", "expected_reason"),
    [
        ("unanimity_split", GATE_REASON_UNANIMITY_FAILED),
        ("probability_below_threshold", GATE_REASON_PROBABILITY_BELOW_THRESHOLD),
    ],
)
def test_score_fail_zero_alpaca_calls_over_100_trials(
    db_path: Path,
    armed_path: Path,
    audit_path: Path,
    scenario_id: str,
    expected_reason: str,
) -> None:
    """Score-then-submit proven: zero broker calls over 100 trials.

    Walks VAL-M5-031: parametrised across
    ``scenario_id={unanimity_split, probability_below_threshold}``;
    each case asserts ``alpaca_cassette.play_count == 0``. This
    file's variant repeats the rejection ``N_TRIALS=100`` times.

    Note that the post-fanout gates run AFTER the LLM fan-out, so
    ``ensemble_scores_event`` rows DO get persisted on each trial.
    The invariant we assert here is the BROKER invariant (zero
    Alpaca submission attempts), not the LLM invariant — those are
    covered by Tests 1 and 2 above.
    """
    candidate = _emit_candidate(db_path)

    tracker = _StubProviderTracker()
    if scenario_id == "unanimity_split":
        # 3 material + 1 non_material → unanimity fails.
        per_provider = {
            "xai": {
                "label": "material",
                "probability": 0.85,
                "direction": "bullish",
                "rationale": "xai-stub",
                "citations": [],
                "latency_ms": 10,
                "cost_usd": 0.001,
            },
            "anthropic": {
                "label": "material",
                "probability": 0.85,
                "direction": "bullish",
                "rationale": "anthropic-stub",
                "citations": [],
                "latency_ms": 10,
                "cost_usd": 0.001,
            },
            "gemini": {
                "label": "material",
                "probability": 0.85,
                "direction": "bullish",
                "rationale": "gemini-stub",
                "citations": [],
                "latency_ms": 10,
                "cost_usd": 0.001,
            },
            "perplexity": {
                "label": "non_material",
                "probability": 0.85,
                "direction": "bullish",
                "rationale": "perplexity-stub",
                "citations": [],
                "latency_ms": 10,
                "cost_usd": 0.001,
            },
        }
    else:
        # 4/4 material; mean(0.6, 0.65, 0.7, 0.7) = 0.6625 < 0.75.
        probs = {"xai": 0.6, "anthropic": 0.65, "gemini": 0.7, "perplexity": 0.7}
        per_provider = {
            name: {
                "label": "material",
                "probability": probs[name],
                "direction": "bullish",
                "rationale": f"{name}-stub",
                "citations": [],
                "latency_ms": 10,
                "cost_usd": 0.001,
            }
            for name in ALL_PROVIDERS
        }

    providers = tracker.make_per_provider_providers(per_provider)
    fake_client = _FakeAlpacaClient()
    executor = PaperExecutor(
        fake_client,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )

    for _ in range(N_TRIALS):
        chain_result = run_stage2_chain(
            candidate_event_row=candidate,
            db_path=db_path,
            armed_path=armed_path,
            audit_path=audit_path,
            providers=providers,
        )
        assert chain_result.passed is False
        assert chain_result.reason == expected_reason
        # Both the unanimity_split AND probability cases land at a
        # post-fanout gate; the dispatcher tags them as
        # ``gate='unanimity'`` / ``gate='probability'`` respectively.
        if expected_reason == GATE_REASON_UNANIMITY_FAILED:
            assert chain_result.gate == "unanimity"
        else:
            assert chain_result.gate == "probability"
        # Fan-out completed (this is a post-fanout rejection), so
        # ``ensemble_result`` is present even though ``passed=False``.
        assert chain_result.ensemble_result is not None
        # Production wiring: submit only on passed=True. The helper
        # short-circuits on rejection so the fake client genuinely
        # sees zero broker I/O on the score-rejection path.
        submit_result = _run_chain_then_submit(
            chain_result, executor, candidate_event=candidate,
        )
        assert submit_result is None

    # VAL-M5-031 — zero Alpaca call attempts.
    assert fake_client.submit_calls == []
    assert fake_client.get_order_calls == []
    assert fake_client.get_positions_calls == 0
    assert fake_client.get_latest_trade_calls == []
    assert fake_client.total_call_count() == 0, (
        f"VAL-M5-031 ({scenario_id}): fake Alpaca client must NOT "
        f"be contacted on score-rejection path; "
        f"total_call_count={fake_client.total_call_count()}"
    )

    # Each provider was called exactly N_TRIALS times (the fan-out
    # ran on every trial because cheap gates pass).
    for p in ALL_PROVIDERS:
        assert tracker.call_counts[p] == N_TRIALS, (
            f"provider {p} call_count={tracker.call_counts[p]} != {N_TRIALS}"
        )


# ---------------------------------------------------------------------------
# Test 5 — Positive path: passing chain DOES reach the fake Alpaca client.
# ---------------------------------------------------------------------------


_CASSETTE_DIR = (
    Path(__file__).resolve().parent.parent / "fixtures" / "cassettes" / "alpaca"
)


def _load_cassette(name: str) -> dict[str, Any]:
    return json.loads((_CASSETTE_DIR / name).read_text(encoding="utf-8"))


def test_passing_chain_reaches_alpaca(
    db_path: Path,
    armed_path: Path,
    audit_path: Path,
) -> None:
    """Sibling of the rejection-path tests proving the wiring is real.

    The four rejection-path tests above all assert
    ``fake_client.total_call_count() == 0`` after invoking
    :func:`_run_chain_then_submit`. That assertion has meaning only
    if the same helper, given a CHAIN THAT PASSES, would actually
    contact the fake client. This test pins exactly that property:

    * Same :class:`_FakeAlpacaClient` and :class:`PaperExecutor`
      construction the rejection tests use.
    * Same :func:`_run_chain_then_submit` helper invocation.
    * No cooldown row, ``.armed`` present, no daily-cap-hit ledger
      seed, four unanimous-bullish providers at p=0.85 — every
      cheap gate AND the post-fanout score gates pass.
    * Cassette-backed broker payloads loaded via
      ``order_call_roundtrip.json`` so the fake client returns a
      well-formed Alpaca submit response.
    * Asserts ``fake_client.total_call_count() > 0`` post-submit
      AND ``len(fake_client.submit_calls) >= 1`` to disambiguate a
      real broker contact from incidental ``get_positions`` /
      ``get_latest_trade`` polling.

    Without this control, the rejection-path zero-call assertions
    could be vacuously satisfied by dead wiring.
    """
    candidate = _emit_candidate(db_path)
    # No cooldown seed, no cap-hit ledger seed; armed_path fixture
    # already created the marker — every gate should pass.

    tracker = _StubProviderTracker()
    providers = tracker.make_uniform_providers(
        label="material",
        direction="bullish",
        probability=0.85,
    )

    cassette = _load_cassette("order_call_roundtrip.json")

    # Re-stamp cassette timestamps with strictly-increasing
    # wall-clock instants so the execution_subscriber state-machine
    # validator (which orders execution_events by event_at DESC)
    # doesn't reject the cassette-vintage 2025 timestamps as STALE.
    # This mirrors the pattern in tests/e2e/test_bull_flow.py::_iso.
    now = _dt.datetime.now(_dt.timezone.utc)

    def _iso(offset_seconds: int) -> str:
        ts = now + _dt.timedelta(seconds=offset_seconds)
        return (
            ts.strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{ts.microsecond // 1000:03d}Z"
        )

    submit_payload = dict(cassette["submit"])
    submit_payload["created_at"] = _iso(0)
    submit_payload["updated_at"] = _iso(0)
    submit_payload["submitted_at"] = _iso(0)

    poll_intermediate_payload = dict(cassette["poll_intermediate"])
    poll_intermediate_payload["updated_at"] = _iso(60)
    poll_intermediate_payload["submitted_at"] = _iso(0)

    poll_filled_payload = dict(cassette["poll_filled"])
    poll_filled_payload["updated_at"] = _iso(120)
    poll_filled_payload["filled_at"] = _iso(120)
    poll_filled_payload["submitted_at"] = _iso(0)

    fake_client = _FakeAlpacaClient(
        submit_order_result=submit_payload,
        get_order_results=[
            poll_intermediate_payload,
            poll_filled_payload,
        ],
        latest_trade_price=25.0,
    )
    executor = PaperExecutor(
        fake_client,  # type: ignore[arg-type]
        db_path=db_path,
        poll_interval_seconds=0.0,
    )

    pre_total = fake_client.total_call_count()

    chain_result = run_stage2_chain(
        candidate_event_row=candidate,
        db_path=db_path,
        armed_path=armed_path,
        audit_path=audit_path,
        providers=providers,
    )
    assert chain_result.passed is True, (
        f"positive-path chain must pass; gate={chain_result.gate} "
        f"reason={chain_result.reason}"
    )
    assert chain_result.ensemble_result is not None

    alpaca_order_id = _run_chain_then_submit(
        chain_result, executor, candidate_event=candidate,
    )

    # The helper actually contacted the broker, so an Alpaca order
    # id flows back AND the fake client recorded ≥1 submit call.
    assert isinstance(alpaca_order_id, str) and alpaca_order_id, (
        "passing chain must yield a broker-assigned alpaca_order_id"
    )
    assert fake_client.total_call_count() > pre_total, (
        f"_run_chain_then_submit must contact the fake Alpaca "
        f"client on a passing chain; "
        f"total_call_count={fake_client.total_call_count()} "
        f"pre_total={pre_total}"
    )
    assert len(fake_client.submit_calls) >= 1, (
        "passing chain must result in at least one submit_order call "
        "(disambiguates real broker contact from incidental polling)"
    )


# ---------------------------------------------------------------------------
# Smoke import — keeps ``pytest --collect-only`` green.
# ---------------------------------------------------------------------------


def test_module_imports_cleanly() -> None:
    """Defensive smoke test: every named import above resolves at module load."""
    import sys
    assert (
        "tests.e2e.test_cheap_first_side_effects" in sys.modules
        or __name__.endswith("test_cheap_first_side_effects")
    )
