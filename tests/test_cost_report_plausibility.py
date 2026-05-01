"""Tests for the perplexity-aware plausibility branch in
:mod:`biotech_sniper.llm.cost_report` (feature
``f-misc-11-perplexity-plausibility-surcharge``).

Background
----------
Perplexity sonar carries a flat per-call surcharge of ``$5 / 1k``
requests for ``search_context_size='low'`` on top of the per-token
input/output rates. The Stage-2 Reading-B path always passes
``low``, so the per-call cost is

::

    cost = prompt_tokens * INPUT_RATE
         + completion_tokens * OUTPUT_RATE
         + SEARCH_USD_PER_LOW_REQUEST   # ($0.005)

The surcharge is NOT representable in the per-1k-token
:data:`MODEL_PRICING` table that the legacy plausibility recompute
relies on. For a low-token perplexity row, the table-derived
expected can be off by up to ``$0.005`` versus the true ledger
value (the ``$0.005`` surcharge dominates a $0.00002 token bill),
which trips the default 20 % tolerance even when the ledger row is
correct.

The fix: :func:`plausibility_check` gets a ``provider=='perplexity'``
branch that recomputes the expected via
:func:`biotech_sniper.llm.perplexity_client.compute_cost_usd` —
the same formula the perplexity client uses to populate the ledger
when the upstream ``usage.cost.total_cost`` field is absent. With
that branch in place, the ledger value matches the recompute within
``$0.0005`` (a defensive sub-rounding tolerance covering the small
divisor effects from the input/output token rates), and
``--check-plausibility`` no longer false-flags low-token perplexity
rows.

Other providers (xai, anthropic, gemini) MUST retain the existing
per-1k :data:`MODEL_PRICING`-driven recompute. The ``--tolerance``
CLI flag MUST continue to widen / tighten the plausibility window
for every provider.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

import pytest

from biotech_sniper import db
from biotech_sniper.llm import cost_report, perplexity_client
from biotech_sniper.llm.cost_report import (
    DEFAULT_TOLERANCE,
    expected_cost_usd,
    find_implausible_rows,
    plausibility_check,
)
from biotech_sniper.llm.perplexity_client import (
    INPUT_USD_PER_TOKEN,
    OUTPUT_USD_PER_TOKEN,
    SEARCH_USD_PER_LOW_REQUEST,
    compute_cost_usd,
)


# ---------------------------------------------------------------------------
# Helpers (kept minimal — full ledger-row utilities live in test_cost_ledger.py)
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Path:
    """Create a fresh SQLite db with the project schema applied."""

    db_path = tmp_path / "ledger.db"
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
    finally:
        conn.close()
    return db_path


def _insert_rows(db_path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    conn = db.connect(db_path)
    try:
        with conn:
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO llm_cost_ledger (
                        provider, model_id, purpose,
                        prompt_tokens, completion_tokens,
                        latency_ms, cost_usd, request_id, called_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["provider"],
                        row["model_id"],
                        row.get("purpose", "stage2_event_scoring"),
                        row["prompt_tokens"],
                        row["completion_tokens"],
                        row.get("latency_ms", 100),
                        row["cost_usd"],
                        row.get("request_id", "req-test"),
                        row.get("called_at", "2026-04-30T10:00:00.000Z"),
                    ),
                )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# (a) Perplexity rows pass plausibility within $0.0005 of ledger value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt_tokens, completion_tokens",
    [
        (10, 10),       # Tiny — the surcharge dominates ($0.00500002).
        (100, 100),     # Small — surcharge still dominates ($0.0050002).
        (1_000, 500),   # Medium — surcharge still ~3x token cost.
        (10_000, 2_000),  # Larger — surcharge becomes minority.
    ],
)
def test_perplexity_branch_uses_compute_cost_usd_with_search_context(
    prompt_tokens, completion_tokens
):
    """The perplexity branch recomputes the expected via
    :func:`perplexity_client.compute_cost_usd` so the ``$0.005`` flat
    search-context-low surcharge is included.

    The ledger ``cost_usd`` is the value the perplexity client would
    write — ``compute_cost_usd(prompt, completion, 'low')`` — and
    the plausibility recompute MUST match that within ``$0.0005``
    (the per-feature tolerance threshold).
    """

    ledger_cost = compute_cost_usd(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        search_context="low",
    )

    result = plausibility_check(
        provider="perplexity",
        model_id="sonar",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=ledger_cost,
    )

    assert result.ok is True, result
    assert result.reason == "", result
    # The branch must produce an expected that includes the $0.005 surcharge,
    # which means abs(ledger - expected) is at most floating-point noise.
    assert abs(result.cost_usd - result.expected_cost_usd) <= 0.0005, result
    # And the expected MUST include the surcharge — otherwise the deviation
    # would be > $0.0005 for the low-token rows.
    expected_floor = SEARCH_USD_PER_LOW_REQUEST  # $0.005
    if prompt_tokens + completion_tokens <= 1_000:
        assert result.expected_cost_usd >= expected_floor * 0.95, (
            "expected must include the search-context-low surcharge"
        )


def test_perplexity_branch_low_token_row_passes_default_tolerance():
    """The motivating regression case: a ledger row with
    ``prompt_tokens=10, completion_tokens=10, cost_usd=$0.00500002``
    (the canonical sonar low-token bill) MUST pass with the default
    20% tolerance.

    Without the perplexity branch the table-derived expected would
    be ``$0.00002``, the deviation would be ``~25,000%``, and the
    row would be flagged.
    """

    p, c = 10, 10
    ledger_cost = (
        p * INPUT_USD_PER_TOKEN
        + c * OUTPUT_USD_PER_TOKEN
        + SEARCH_USD_PER_LOW_REQUEST
    )
    # Sanity-check the test setup itself: the per-1k table-derived
    # expected is genuinely tiny, which is why the legacy code
    # path false-flagged low-token perplexity rows.
    legacy_expected = (p / 1000.0) * 0.001 + (c / 1000.0) * 0.001
    assert ledger_cost / legacy_expected > 100, (
        "test setup invalid — ledger cost should dwarf legacy expected"
    )

    result = plausibility_check(
        provider="perplexity",
        model_id="sonar",
        prompt_tokens=p,
        completion_tokens=c,
        cost_usd=ledger_cost,
        tolerance=DEFAULT_TOLERANCE,
    )

    assert result.ok is True, result
    assert abs(result.cost_usd - result.expected_cost_usd) <= 0.0005, result


def test_find_implausible_rows_passes_clean_perplexity_rows(tmp_path):
    """End-to-end: a ledger seeded with realistic perplexity rows
    (where ``cost_usd`` matches :func:`compute_cost_usd` exactly)
    produces ZERO implausibility findings.
    """

    db_path = _make_db(tmp_path)

    rows = []
    for prompt_tokens, completion_tokens in [
        (10, 10),
        (100, 50),
        (1_500, 600),
        (5_000, 1_000),
    ]:
        cost = compute_cost_usd(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            search_context="low",
        )
        rows.append(
            {
                "provider": "perplexity",
                "model_id": "sonar",
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": cost,
            }
        )

    _insert_rows(db_path, rows)

    conn = db.connect(db_path)
    try:
        bad = find_implausible_rows(conn, since="2026-04-01")
    finally:
        conn.close()

    assert bad == [], bad


def test_find_implausible_rows_legacy_path_would_have_false_flagged(tmp_path):
    """Regression marker: confirm that without the perplexity branch
    the legacy per-1k recompute WOULD flag low-token perplexity rows.

    This test does NOT call :func:`plausibility_check` — it computes
    the legacy table-derived expected directly via
    :func:`expected_cost_usd` (which is now perplexity-aware too),
    but compares it against a hypothetical pre-fix expected (the
    pure per-1k formula) to lock in the documentary record of the
    surcharge dominance for low-token rows.
    """

    p, c = 10, 10
    ledger_cost = compute_cost_usd(
        prompt_tokens=p, completion_tokens=c, search_context="low"
    )
    # Pure per-1k recompute (the pre-fix formula):
    pre_fix_expected = (p / 1000.0) * 0.001 + (c / 1000.0) * 0.001
    assert pre_fix_expected > 0
    pre_fix_deviation = abs(ledger_cost - pre_fix_expected) / pre_fix_expected
    # Anything > 20 % is the false-flag scenario.
    assert pre_fix_deviation > DEFAULT_TOLERANCE


# ---------------------------------------------------------------------------
# (b) Other providers retain the existing per-1k MODEL_PRICING path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider, model_id, input_rate, output_rate",
    [
        ("xai", "grok-4", 0.005, 0.015),
        ("anthropic", "claude-opus-4-1-20250805", 0.015, 0.075),
        ("gemini", "gemini-2.5-pro", 0.00125, 0.010),
    ],
)
def test_non_perplexity_provider_uses_per_1k_pricing(
    provider, model_id, input_rate, output_rate
):
    """The xai / anthropic / gemini path continues to use
    :data:`MODEL_PRICING` (per-1k rates) for the recompute. The
    perplexity-specific compute_cost_usd helper is NOT invoked.

    Verified by computing the per-1k expected by hand and confirming
    :func:`plausibility_check` reports the same number in
    ``result.expected_cost_usd``, AND that a tightly-matched ledger
    row passes within tolerance.
    """

    prompt_tokens, completion_tokens = 1_000, 200
    expected_per_1k = (
        (prompt_tokens / 1000.0) * input_rate
        + (completion_tokens / 1000.0) * output_rate
    )
    # Sanity-check against the public helper so a future MODEL_PRICING
    # bump is caught here too.
    helper_expected = expected_cost_usd(
        provider, model_id, prompt_tokens, completion_tokens
    )
    assert helper_expected is not None
    assert helper_expected == pytest.approx(expected_per_1k, abs=1e-9)

    result = plausibility_check(
        provider=provider,
        model_id=model_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=expected_per_1k,
    )

    assert result.ok is True
    assert result.expected_cost_usd == pytest.approx(expected_per_1k, abs=1e-9)
    # The perplexity surcharge MUST NOT have been added — that would
    # bump expected by ~$0.005 and break the equality above.
    assert abs(result.expected_cost_usd - expected_per_1k) < 1e-6


def test_non_perplexity_provider_does_not_call_perplexity_client(monkeypatch):
    """Defensive: if a future refactor wires :func:`compute_cost_usd`
    into the generic path by accident, this test catches it.
    """

    sentinel_calls: list[tuple[Any, ...]] = []

    def _spy(*args, **kwargs):
        sentinel_calls.append((args, kwargs))
        # Return a value that would obviously break the assertion if used.
        return 9_999.0

    monkeypatch.setattr(
        "biotech_sniper.llm.cost_report._perplexity_compute_cost_usd",
        _spy,
        raising=False,
    )
    # When the indirection doesn't exist (initial baseline before the
    # implementation lands), the patch is a no-op; the assertion below
    # still verifies the user-visible behavior.

    result = plausibility_check(
        provider="xai",
        model_id="grok-4",
        prompt_tokens=1_000,
        completion_tokens=200,
        cost_usd=expected_cost_usd("xai", "grok-4", 1_000, 200) or 0.0,
    )

    assert result.ok is True
    assert sentinel_calls == [], (
        "non-perplexity rows must NOT route through the perplexity "
        f"compute_cost_usd helper; observed calls: {sentinel_calls!r}"
    )


# ---------------------------------------------------------------------------
# (c) Tolerance flag still works
# ---------------------------------------------------------------------------


def test_tolerance_flag_tightens_perplexity_window():
    """A tolerance of 0% rejects ANY deviation, even on the
    perplexity branch. Confirms ``--tolerance`` still narrows the
    window for perplexity rows (it must not be silently bypassed).
    """

    p, c = 1_000, 200
    expected = compute_cost_usd(
        prompt_tokens=p, completion_tokens=c, search_context="low"
    )

    # 1% above expected: passes default 20% but fails 0% tolerance.
    inflated = expected * 1.01

    lenient = plausibility_check(
        provider="perplexity",
        model_id="sonar",
        prompt_tokens=p,
        completion_tokens=c,
        cost_usd=inflated,
        tolerance=0.20,
    )
    assert lenient.ok is True

    strict = plausibility_check(
        provider="perplexity",
        model_id="sonar",
        prompt_tokens=p,
        completion_tokens=c,
        cost_usd=inflated,
        tolerance=0.0,
    )
    assert strict.ok is False
    assert strict.reason == "outside-tolerance"


def test_tolerance_flag_widens_perplexity_window():
    """A wide tolerance (e.g. 100%) allows a ledger row that
    deviates by 50% to pass. Verifies the tolerance argument is
    still applied to the perplexity branch.
    """

    p, c = 1_000, 200
    expected = compute_cost_usd(
        prompt_tokens=p, completion_tokens=c, search_context="low"
    )

    inflated = expected * 1.5  # +50%

    default = plausibility_check(
        provider="perplexity",
        model_id="sonar",
        prompt_tokens=p,
        completion_tokens=c,
        cost_usd=inflated,
        tolerance=DEFAULT_TOLERANCE,  # 20%
    )
    assert default.ok is False

    wide = plausibility_check(
        provider="perplexity",
        model_id="sonar",
        prompt_tokens=p,
        completion_tokens=c,
        cost_usd=inflated,
        tolerance=1.0,  # 100% — accepts the 50% deviation.
    )
    assert wide.ok is True


def test_tolerance_flag_still_works_for_non_perplexity_providers():
    """Locks in (c) for the non-perplexity path too — a 1%-inflated
    xai row passes default 20% tolerance, fails 0% tolerance.
    """

    p, c = 1_000, 200
    expected = expected_cost_usd("xai", "grok-4", p, c) or 0.0
    inflated = expected * 1.01

    lenient = plausibility_check(
        provider="xai",
        model_id="grok-4",
        prompt_tokens=p,
        completion_tokens=c,
        cost_usd=inflated,
        tolerance=0.20,
    )
    assert lenient.ok is True

    strict = plausibility_check(
        provider="xai",
        model_id="grok-4",
        prompt_tokens=p,
        completion_tokens=c,
        cost_usd=inflated,
        tolerance=0.0,
    )
    assert strict.ok is False


def test_find_implausible_rows_flags_obvious_perplexity_outliers(tmp_path):
    """A perplexity ledger row whose recorded ``cost_usd`` is 10x the
    real bill is still flagged — the new branch must not become a
    blanket pass for the perplexity provider.
    """

    db_path = _make_db(tmp_path)

    p, c = 1_000, 200
    real_cost = compute_cost_usd(
        prompt_tokens=p, completion_tokens=c, search_context="low"
    )
    _insert_rows(
        db_path,
        [
            {
                "provider": "perplexity",
                "model_id": "sonar",
                "prompt_tokens": p,
                "completion_tokens": c,
                "cost_usd": real_cost * 10,  # blatantly wrong.
            },
        ],
    )

    conn = db.connect(db_path)
    try:
        bad = find_implausible_rows(conn, since="2026-04-01")
    finally:
        conn.close()

    assert len(bad) == 1
    assert bad[0].provider == "perplexity"
    assert bad[0].reason == "outside-tolerance"


# ---------------------------------------------------------------------------
# Edge cases on the perplexity branch
# ---------------------------------------------------------------------------


def test_perplexity_zero_tokens_ledger_value_passes():
    """A perplexity row with both token counts at 0 still incurs the
    flat ``$0.005`` surcharge. The branch must compute that and
    accept the row.
    """

    ledger_cost = SEARCH_USD_PER_LOW_REQUEST
    result = plausibility_check(
        provider="perplexity",
        model_id="sonar",
        prompt_tokens=0,
        completion_tokens=0,
        cost_usd=ledger_cost,
    )
    assert result.ok is True


def test_perplexity_negative_cost_always_rejected():
    """Even on the perplexity branch, a negative ``cost_usd`` must
    fail (ledger corruption / sign-flip bug).
    """

    result = plausibility_check(
        provider="perplexity",
        model_id="sonar",
        prompt_tokens=1_000,
        completion_tokens=200,
        cost_usd=-0.001,
    )
    assert result.ok is False
    assert result.reason == "negative-cost"


def test_perplexity_branch_independent_of_model_id_prefix():
    """The perplexity branch keys on ``provider == 'perplexity'``,
    not on a specific ``model_id`` prefix — so an unknown sonar
    variant (e.g. a future ``sonar-medium`` rename) does NOT fall
    through to ``unknown-pricing`` when the cost is sensible.
    """

    p, c = 1_000, 200
    ledger_cost = compute_cost_usd(
        prompt_tokens=p, completion_tokens=c, search_context="low"
    )
    result = plausibility_check(
        provider="perplexity",
        model_id="sonar-future-variant-not-in-table",
        prompt_tokens=p,
        completion_tokens=c,
        cost_usd=ledger_cost,
    )
    assert result.ok is True
    # The result must reflect the perplexity recompute (NOT the
    # ``unknown-pricing`` short-circuit).
    assert result.reason in ("", "outside-tolerance")
    assert abs(result.cost_usd - result.expected_cost_usd) <= 0.0005
