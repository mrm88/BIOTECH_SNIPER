"""Unit tests for :mod:`biotech_sniper.rotation_engine` (f-m3-10).

The rotation engine swaps a weak active paper-trading play for a
higher-ranked challenger from today's :data:`scoring_cache` when:

* the active count is already at ``MAX_CONCURRENT`` (capacity full),
* the challenger's ``ensemble_score`` exceeds the weakest incumbent's
  current score by ≥ :data:`ROTATION_THRESHOLD` (0.10),
* neither side's catalyst date is within 24h of today,
* the LLM rotation debate (``trigger='rotation'``) ranks the
  challenger strictly higher than the incumbent.

These tests lock the contract for each branch and the post-run
active-count cap. The LLM debate runner and the
:class:`PaperExecutor` are injected as fakes so the tests run
without network or broker fixtures.

Validation contract assertions exercised
----------------------------------------
* **VAL-M3-053** — above-threshold + outside-24h fires exactly one
  ``submit_exit(event='rotation')`` + one ``execute(event='open')``
  per rotation; resulting active count ≤ ``max_concurrent``.
* **VAL-M3-054** — below-threshold candidates do NOT rotate.
* **VAL-M3-055** — within-24h candidates do NOT rotate; audit JSON's
  ``rotation_skipped.reason`` is ``catalyst_too_close``.
* **VAL-M3-056** — every rotation decision triggers the LLM debate
  (the test counts debate invocations).
* **VAL-M3-057** — rotation only fires when the debate's
  ``challenger_grade`` strictly outranks the incumbent's; an inverted
  preference is logged with ``reason='debate_inverted_preference'``.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper import rotation_engine as re_mod
from biotech_sniper.rotation_engine import (
    ENTRY_EVENT,
    ROTATION_EVENT,
    ROTATION_THRESHOLD,
    challenger_outranks,
    evaluate_rotation,
    is_within_24h,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


TODAY = datetime.date(2026, 4, 27)


class _FakeExecutor:
    """Capture ``submit_exit`` / ``execute`` calls without broker IO."""

    def __init__(self) -> None:
        self.exits: list[dict[str, Any]] = []
        self.entries: list[dict[str, Any]] = []
        self._exit_counter = 0
        self._entry_counter = 0

    def submit_exit(
        self, play, event, *, today=None, sell_qty=None
    ) -> str:
        self._exit_counter += 1
        order_id = f"sell-{self._exit_counter:04d}"
        self.exits.append(
            {
                "play": dict(play),
                "event": event,
                "today": today,
                "sell_qty": sell_qty,
                "order_id": order_id,
            }
        )
        return order_id

    def execute(self, card) -> str:
        self._entry_counter += 1
        order_id = f"buy-{self._entry_counter:04d}"
        self.entries.append({"card": dict(card), "order_id": order_id})
        return order_id


def _runner_factory(*, challenger_grade: str, incumbent_grade: str):
    """Return a debate runner that yields the supplied grades.

    The runner records every call so tests can assert that a debate
    fired (VAL-M3-056) AND inspect the inputs the engine passed in.
    """

    calls: list[dict[str, Any]] = []

    def _runner(*, challenger, incumbent, today=None):
        calls.append(
            {
                "challenger": dict(challenger),
                "incumbent": dict(incumbent),
                "today": today,
            }
        )
        return {
            "challenger_grade": challenger_grade,
            "incumbent_grade": incumbent_grade,
            "rounds": 3,
            "trigger": "rotation",
        }

    _runner.calls = calls  # type: ignore[attr-defined]
    return _runner


def _active_play(
    ticker: str,
    score: float,
    *,
    catalyst_date: str | datetime.date | None = "2026-05-15",
    qty: int = 4,
    play_card_id: str | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "ensemble_score": score,
        "catalyst_date": catalyst_date,
        "qty": qty,
        "play_card_id": play_card_id or f"{ticker}-pc-001",
        "symbol": symbol or f"{ticker}260620C00050000",
        "scoring_cache_id": hash(ticker) % 100000,
    }


def _candidate(
    ticker: str,
    score: float,
    *,
    catalyst_date: str | datetime.date | None = "2026-06-10",
    play_card: dict | None = None,
) -> dict[str, Any]:
    pc = play_card or {
        "play_card_id": f"{ticker}-pc-002",
        "ticker": ticker,
        "option_legs": [
            {
                "symbol": f"{ticker}260620C00075000",
                "side": "buy",
                "qty": 1,
            }
        ],
    }
    return {
        "ticker": ticker,
        "ensemble_score": score,
        "catalyst_date": catalyst_date,
        "play_card": pc,
        "scoring_cache_id": (hash(ticker) + 1) % 100000,
    }


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit_latest.json"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_threshold_constant_matches_spec():
    """ROTATION_THRESHOLD must equal the documented 0.10 spec value."""
    assert ROTATION_THRESHOLD == 0.10


def test_event_tags_match_hold_policy_enum():
    """The engine's event tags must mirror the f-m3-09 enum exactly."""
    from biotech_sniper.hold_policy import (
        ALLOWED_EXIT_EVENTS,
        ENTRY_EVENT as POLICY_ENTRY_EVENT,
    )

    assert ROTATION_EVENT == "rotation"
    assert ROTATION_EVENT in ALLOWED_EXIT_EVENTS
    assert ENTRY_EVENT == POLICY_ENTRY_EVENT
    assert ENTRY_EVENT == "open"


def test_is_within_24h_block_when_catalyst_today():
    """A catalyst dated today is within 24h (delta=0 < 1 day)."""
    assert is_within_24h(TODAY, today=TODAY) is True


def test_is_within_24h_block_when_catalyst_tomorrow_is_false():
    """A catalyst exactly 1 day away is NOT within 24h (delta=1, not < 1)."""
    tomorrow = TODAY + datetime.timedelta(days=1)
    assert is_within_24h(tomorrow, today=TODAY) is False


def test_is_within_24h_returns_false_for_missing_date():
    """A missing catalyst date does not block (engine surfaces upstream)."""
    assert is_within_24h(None, today=TODAY) is False
    assert is_within_24h("not-a-date", today=TODAY) is False


def test_challenger_outranks_strict_ordering():
    """``challenger_outranks`` is strictly ordered via LETTER_GRADE_ORDER."""
    assert challenger_outranks("A", "B") is True
    assert challenger_outranks("A+", "A") is True
    assert challenger_outranks("B", "A") is False
    assert challenger_outranks("B", "B") is False  # equal does not outrank
    assert challenger_outranks("Z", "A") is False  # unknown grade
    assert challenger_outranks("A", None) is False  # unknown incumbent


# ---------------------------------------------------------------------------
# Below-capacity short-circuit
# ---------------------------------------------------------------------------


def test_below_capacity_short_circuits_without_iterating(audit_path: Path):
    """When active < cap, the engine MUST short-circuit — no rotations, no skips."""
    runner = _runner_factory(challenger_grade="A", incumbent_grade="C")
    executor = _FakeExecutor()

    active = [_active_play("AAA", 0.50)]
    candidates = [_candidate("BBB", 0.99)]  # would normally trigger

    result = evaluate_rotation(
        today=TODAY,
        active_plays=active,
        candidates=candidates,
        executor=executor,
        debate_runner=runner,
        max_concurrent=4,
        audit_path=audit_path,
    )

    assert result["decisions"] == []
    assert result["skips"] == []
    assert result["active_count"] == 1
    # No debate fired since we never iterated candidates.
    assert runner.calls == []  # type: ignore[attr-defined]
    assert executor.exits == []
    assert executor.entries == []


# ---------------------------------------------------------------------------
# Above-threshold path
# ---------------------------------------------------------------------------


def test_above_threshold_outside_24h_fires_full_swap(audit_path: Path):
    """Above-threshold + outside-24h: one sell + one buy + cap honoured (VAL-M3-053)."""
    runner = _runner_factory(challenger_grade="A", incumbent_grade="C")
    executor = _FakeExecutor()

    active = [
        _active_play("AAA", 0.55, play_card_id="AAA-1"),
        _active_play("BBB", 0.60, play_card_id="BBB-1"),
        _active_play("CCC", 0.70, play_card_id="CCC-1"),
        # Weakest:
        _active_play("DDD", 0.50, play_card_id="DDD-1"),
    ]
    candidates = [
        _candidate("EEE", 0.85),  # delta 0.35 vs DDD (0.50) — fires
    ]

    result = evaluate_rotation(
        today=TODAY,
        active_plays=active,
        candidates=candidates,
        executor=executor,
        debate_runner=runner,
        max_concurrent=4,
        audit_path=audit_path,
    )

    # Exactly ONE rotation fired.
    assert len(result["decisions"]) == 1
    decision = result["decisions"][0]
    assert decision["challenger"] == "EEE"
    assert decision["incumbent"] == "DDD"
    assert decision["sell_order_id"] == "sell-0001"
    assert decision["buy_order_id"] == "buy-0001"

    # Exactly one sell + one buy submitted.
    assert len(executor.exits) == 1
    assert executor.exits[0]["event"] == ROTATION_EVENT
    assert executor.exits[0]["play"]["ticker"] == "DDD"
    assert executor.exits[0]["sell_qty"] == 4

    assert len(executor.entries) == 1
    assert executor.entries[0]["card"]["event"] == ENTRY_EVENT
    assert executor.entries[0]["card"]["ticker"] == "EEE"

    # Post-run active count == cap (one removed, one added).
    assert result["active_count"] == 4
    assert result["capacity"] == 4

    # Debate fired exactly once for the rotation.
    assert len(runner.calls) == 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Below-threshold path
# ---------------------------------------------------------------------------


def test_below_threshold_does_not_rotate(audit_path: Path):
    """Delta < 0.10 → no rotation, no debate, audit reason='below_threshold' (VAL-M3-054)."""
    runner = _runner_factory(challenger_grade="A", incumbent_grade="C")
    executor = _FakeExecutor()

    active = [
        _active_play("AAA", 0.65),
        _active_play("BBB", 0.60),
        _active_play("CCC", 0.55),
        _active_play("DDD", 0.50),
    ]
    candidates = [
        _candidate("EEE", 0.55),  # delta 0.05 < 0.10
    ]

    result = evaluate_rotation(
        today=TODAY,
        active_plays=active,
        candidates=candidates,
        executor=executor,
        debate_runner=runner,
        max_concurrent=4,
        audit_path=audit_path,
    )

    assert result["decisions"] == []
    assert len(result["skips"]) == 1
    assert result["skips"][0]["reason"] == "below_threshold"
    # No orders submitted, no debate fired.
    assert executor.exits == []
    assert executor.entries == []
    assert runner.calls == []  # type: ignore[attr-defined]
    # Audit JSON has the skip block.
    audit = json.loads(audit_path.read_text())
    assert audit["rotation_skipped"]["reason"] == "below_threshold"


def test_threshold_boundary_just_above_fires(audit_path: Path):
    """Delta just above ROTATION_THRESHOLD fires; just below skips.

    Floating-point exactly-at-threshold testing is unreliable
    (0.5 + 0.1 = 0.6 but (0.5 + 0.1) - 0.5 = 0.09999...). We bound
    the contract by checking just-above and just-below the
    threshold instead — the strict ``< threshold`` predicate is
    fully covered by the two cases.
    """
    runner = _runner_factory(challenger_grade="A", incumbent_grade="C")

    base_active = [
        _active_play("AAA", 0.65),
        _active_play("BBB", 0.60),
        _active_play("CCC", 0.55),
        _active_play("DDD", 0.50),
    ]

    # Just above threshold (delta ≈ 0.101) → MUST rotate.
    above_executor = _FakeExecutor()
    above_result = evaluate_rotation(
        today=TODAY,
        active_plays=base_active,
        candidates=[_candidate("EEE", 0.601)],
        executor=above_executor,
        debate_runner=runner,
        max_concurrent=4,
        audit_path=audit_path,
    )
    assert len(above_result["decisions"]) == 1, above_result

    # Just below threshold (delta ≈ 0.099) → MUST skip.
    below_runner = _runner_factory(challenger_grade="A", incumbent_grade="C")
    below_executor = _FakeExecutor()
    below_result = evaluate_rotation(
        today=TODAY,
        active_plays=base_active,
        candidates=[_candidate("EEE", 0.599)],
        executor=below_executor,
        debate_runner=below_runner,
        max_concurrent=4,
        audit_path=audit_path,
    )
    assert below_result["decisions"] == []
    assert below_result["skips"][0]["reason"] == "below_threshold"


# ---------------------------------------------------------------------------
# 24h-before-catalyst guard
# ---------------------------------------------------------------------------


def test_within_24h_challenger_blocks_rotation(audit_path: Path):
    """Challenger catalyst within 24h blocks the rotation (VAL-M3-055)."""
    runner = _runner_factory(challenger_grade="A", incumbent_grade="C")
    executor = _FakeExecutor()

    active = [
        _active_play("AAA", 0.55, catalyst_date="2026-06-15"),
        _active_play("BBB", 0.60, catalyst_date="2026-06-15"),
        _active_play("CCC", 0.70, catalyst_date="2026-06-15"),
        _active_play("DDD", 0.50, catalyst_date="2026-06-15"),
    ]
    candidates = [
        _candidate("EEE", 0.85, catalyst_date=TODAY.isoformat()),  # today
    ]

    result = evaluate_rotation(
        today=TODAY,
        active_plays=active,
        candidates=candidates,
        executor=executor,
        debate_runner=runner,
        max_concurrent=4,
        audit_path=audit_path,
    )

    assert result["decisions"] == []
    assert len(result["skips"]) == 1
    assert result["skips"][0]["reason"] == "catalyst_too_close"
    # No orders, no debate.
    assert executor.exits == []
    assert executor.entries == []
    assert runner.calls == []  # type: ignore[attr-defined]
    # Audit JSON
    audit = json.loads(audit_path.read_text())
    assert audit["rotation_skipped"]["reason"] == "catalyst_too_close"


def test_within_24h_incumbent_also_blocks_rotation(audit_path: Path):
    """Incumbent catalyst within 24h ALSO blocks rotation (VAL-M3-055)."""
    runner = _runner_factory(challenger_grade="A", incumbent_grade="C")
    executor = _FakeExecutor()

    active = [
        _active_play("AAA", 0.55, catalyst_date="2026-06-15"),
        _active_play("BBB", 0.60, catalyst_date="2026-06-15"),
        _active_play("CCC", 0.70, catalyst_date="2026-06-15"),
        # Weakest, with catalyst today (within 24h):
        _active_play("DDD", 0.50, catalyst_date=TODAY.isoformat()),
    ]
    candidates = [
        _candidate("EEE", 0.85, catalyst_date="2026-07-01"),
    ]

    result = evaluate_rotation(
        today=TODAY,
        active_plays=active,
        candidates=candidates,
        executor=executor,
        debate_runner=runner,
        max_concurrent=4,
        audit_path=audit_path,
    )

    assert result["decisions"] == []
    assert len(result["skips"]) == 1
    assert result["skips"][0]["reason"] == "catalyst_too_close"
    assert executor.exits == []
    assert executor.entries == []


# ---------------------------------------------------------------------------
# Debate-inverted preference
# ---------------------------------------------------------------------------


def test_debate_inverted_preference_blocks_rotation(audit_path: Path):
    """When the debate ranks incumbent better, rotation is suppressed (VAL-M3-057)."""
    # Static ensemble would favour the challenger, but the debate
    # inverts the preference (challenger_grade B vs incumbent A).
    runner = _runner_factory(challenger_grade="B", incumbent_grade="A")
    executor = _FakeExecutor()

    active = [
        _active_play("AAA", 0.55),
        _active_play("BBB", 0.60),
        _active_play("CCC", 0.70),
        _active_play("DDD", 0.50),
    ]
    candidates = [
        _candidate("EEE", 0.85),
    ]

    result = evaluate_rotation(
        today=TODAY,
        active_plays=active,
        candidates=candidates,
        executor=executor,
        debate_runner=runner,
        max_concurrent=4,
        audit_path=audit_path,
    )

    assert result["decisions"] == []
    assert len(result["skips"]) == 1
    assert result["skips"][0]["reason"] == "debate_inverted_preference"
    # Debate DID fire (we still record the LLM rationale even when
    # rotation is suppressed) but no orders submitted.
    assert len(runner.calls) == 1  # type: ignore[attr-defined]
    assert executor.exits == []
    assert executor.entries == []
    audit = json.loads(audit_path.read_text())
    assert audit["rotation_skipped"]["reason"] == "debate_inverted_preference"
    assert audit["rotation_skipped"]["challenger_grade"] == "B"
    assert audit["rotation_skipped"]["incumbent_grade"] == "A"


def test_debate_equal_grade_does_not_rotate(audit_path: Path):
    """Equal grades do NOT outrank — rotation must be suppressed."""
    runner = _runner_factory(challenger_grade="B", incumbent_grade="B")
    executor = _FakeExecutor()

    active = [
        _active_play("AAA", 0.55),
        _active_play("BBB", 0.60),
        _active_play("CCC", 0.70),
        _active_play("DDD", 0.50),
    ]
    candidates = [_candidate("EEE", 0.85)]

    result = evaluate_rotation(
        today=TODAY,
        active_plays=active,
        candidates=candidates,
        executor=executor,
        debate_runner=runner,
        max_concurrent=4,
        audit_path=audit_path,
    )

    assert result["decisions"] == []
    assert result["skips"][0]["reason"] == "debate_inverted_preference"
    assert executor.exits == []
    assert executor.entries == []


# ---------------------------------------------------------------------------
# Active-count cap & multiple candidates
# ---------------------------------------------------------------------------


def test_post_run_active_count_capped_at_max_concurrent(audit_path: Path):
    """After multiple rotations, active count NEVER exceeds max_concurrent."""
    runner = _runner_factory(challenger_grade="A", incumbent_grade="C")
    executor = _FakeExecutor()

    active = [
        _active_play("AAA", 0.50),
        _active_play("BBB", 0.51),
        _active_play("CCC", 0.52),
        _active_play("DDD", 0.53),
    ]
    candidates = [
        _candidate("EEE", 0.85),  # rotates AAA out
        _candidate("FFF", 0.84),  # rotates BBB out (BBB now weakest)
    ]

    result = evaluate_rotation(
        today=TODAY,
        active_plays=active,
        candidates=candidates,
        executor=executor,
        debate_runner=runner,
        max_concurrent=4,
        audit_path=audit_path,
    )

    assert len(result["decisions"]) == 2
    # Each rotation = one sell + one buy → 2 exits + 2 entries total.
    assert len(executor.exits) == 2
    assert len(executor.entries) == 2

    # Active count must remain at the cap (4 → -1 +1 → -1 +1 = 4).
    assert result["active_count"] == 4
    assert result["active_count"] <= result["capacity"]

    # Verify the order of incumbents removed: AAA (0.50) first, then
    # BBB (0.51) since EEE replaced AAA and BBB became the new weakest.
    assert executor.exits[0]["play"]["ticker"] == "AAA"
    assert executor.exits[1]["play"]["ticker"] == "BBB"


def test_existing_active_ticker_skipped_silently(audit_path: Path):
    """A candidate whose ticker is already active is silently ignored (no skip log)."""
    runner = _runner_factory(challenger_grade="A", incumbent_grade="C")
    executor = _FakeExecutor()

    active = [
        _active_play("AAA", 0.55),
        _active_play("BBB", 0.60),
        _active_play("CCC", 0.70),
        _active_play("DDD", 0.50),
    ]
    # First candidate is already active; second is fresh and should fire.
    candidates = [
        _candidate("AAA", 0.99),  # already active - skip silently
        _candidate("EEE", 0.85),
    ]

    result = evaluate_rotation(
        today=TODAY,
        active_plays=active,
        candidates=candidates,
        executor=executor,
        debate_runner=runner,
        max_concurrent=4,
        audit_path=audit_path,
    )

    # Only EEE rotated in — AAA was silently skipped (no skip block).
    assert len(result["decisions"]) == 1
    assert result["decisions"][0]["challenger"] == "EEE"
    assert all(s["reason"] != "below_threshold" for s in result["skips"])


# ---------------------------------------------------------------------------
# Debate trigger (VAL-M3-056)
# ---------------------------------------------------------------------------


def test_every_rotation_decision_invokes_the_debate_runner(audit_path: Path):
    """Every rotation candidate MUST trigger a debate call (VAL-M3-056).

    Even when the debate inverts the preference, the runner is
    invoked exactly once per evaluated swap.
    """
    runner = _runner_factory(challenger_grade="A", incumbent_grade="C")
    executor = _FakeExecutor()

    active = [
        _active_play("AAA", 0.55),
        _active_play("BBB", 0.60),
        _active_play("CCC", 0.70),
        _active_play("DDD", 0.50),
    ]
    candidates = [
        _candidate("EEE", 0.85),  # outside-24h, above-threshold → debate
        _candidate("FFF", 0.84),  # second swap → another debate
    ]

    evaluate_rotation(
        today=TODAY,
        active_plays=active,
        candidates=candidates,
        executor=executor,
        debate_runner=runner,
        max_concurrent=4,
        audit_path=audit_path,
    )

    assert len(runner.calls) == 2  # type: ignore[attr-defined]
    # Each call carried challenger + incumbent payloads.
    for call in runner.calls:  # type: ignore[attr-defined]
        assert "ticker" in call["challenger"]
        assert "ticker" in call["incumbent"]


def test_below_threshold_does_not_invoke_debate(audit_path: Path):
    """Sub-threshold deltas short-circuit BEFORE the debate fires.

    The debate is the most expensive step (LLM cost) — the engine
    must not fire it on a candidate that was going to be rejected
    by the threshold guard anyway.
    """
    runner = _runner_factory(challenger_grade="A", incumbent_grade="C")
    executor = _FakeExecutor()

    active = [
        _active_play("AAA", 0.55),
        _active_play("BBB", 0.60),
        _active_play("CCC", 0.70),
        _active_play("DDD", 0.50),
    ]
    candidates = [_candidate("EEE", 0.55)]  # delta 0.05 < 0.10

    evaluate_rotation(
        today=TODAY,
        active_plays=active,
        candidates=candidates,
        executor=executor,
        debate_runner=runner,
        max_concurrent=4,
        audit_path=audit_path,
    )

    assert runner.calls == []  # type: ignore[attr-defined]


def test_within_24h_does_not_invoke_debate(audit_path: Path):
    """Within-24h candidates also short-circuit BEFORE the debate fires."""
    runner = _runner_factory(challenger_grade="A", incumbent_grade="C")
    executor = _FakeExecutor()

    active = [
        _active_play("AAA", 0.55, catalyst_date="2026-06-15"),
        _active_play("BBB", 0.60, catalyst_date="2026-06-15"),
        _active_play("CCC", 0.70, catalyst_date="2026-06-15"),
        _active_play("DDD", 0.50, catalyst_date="2026-06-15"),
    ]
    candidates = [
        _candidate("EEE", 0.85, catalyst_date=TODAY.isoformat()),
    ]

    evaluate_rotation(
        today=TODAY,
        active_plays=active,
        candidates=candidates,
        executor=executor,
        debate_runner=runner,
        max_concurrent=4,
        audit_path=audit_path,
    )

    assert runner.calls == []  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Audit JSON merge preserves other keys
# ---------------------------------------------------------------------------


def test_audit_merge_preserves_unrelated_keys(
    audit_path: Path,
):
    """Skip writes update ``rotation_skipped`` only; other keys survive."""
    # Pre-seed audit_latest.json with unrelated state (e.g. M2 audit).
    audit_path.write_text(
        json.dumps(
            {
                "as_of_date": "2026-04-26",
                "sources": {"clinicaltrials_gov": {"ok": True}},
                "rotation_skipped": {"reason": "stale"},
            }
        )
    )

    runner = _runner_factory(challenger_grade="A", incumbent_grade="C")
    executor = _FakeExecutor()
    active = [
        _active_play("AAA", 0.65),
        _active_play("BBB", 0.60),
        _active_play("CCC", 0.55),
        _active_play("DDD", 0.50),
    ]
    candidates = [_candidate("EEE", 0.55)]  # below_threshold

    evaluate_rotation(
        today=TODAY,
        active_plays=active,
        candidates=candidates,
        executor=executor,
        debate_runner=runner,
        max_concurrent=4,
        audit_path=audit_path,
    )

    audit = json.loads(audit_path.read_text())
    # Unrelated keys preserved verbatim.
    assert audit["as_of_date"] == "2026-04-26"
    assert audit["sources"]["clinicaltrials_gov"]["ok"] is True
    # rotation_skipped overwritten with fresh block.
    assert audit["rotation_skipped"]["reason"] == "below_threshold"


# ---------------------------------------------------------------------------
# Smoke: import + module surface
# ---------------------------------------------------------------------------


def test_module_exports_required_names():
    """The public surface mirrors the f-m3-10 spec."""
    expected = {
        "ROTATION_THRESHOLD",
        "ROTATION_EVENT",
        "ENTRY_EVENT",
        "VALID_SKIP_REASONS",
        "evaluate_rotation",
        "challenger_outranks",
        "is_within_24h",
    }
    assert expected.issubset(set(re_mod.__all__))
