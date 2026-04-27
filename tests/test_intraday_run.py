"""Unit tests for :mod:`biotech_sniper.intraday_run` (f-m4-09).

The intraday entrypoint runs three steps in fixed order and emits one
structured JSON log line per step (event tag) so journalctl can verify
the trio ran. These tests lock in:

* the trio order (adverse_news_check → stop_loss_tick → rotation_evaluate);
* the per-step structured log line carries the right ``event`` tag;
* the dry-run path (no executor) runs every step without a broker call;
* the live path injects an executor and forwards it to each helper;
* a failure inside one step does NOT abort the remaining steps.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Mapping, Sequence

import pytest

from biotech_sniper import intraday_run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _SentinelExecutor:
    """Minimal stand-in for :class:`PaperExecutor` — only needs ``client``."""

    def __init__(self) -> None:
        # Setting ``client=None`` is fine: the trio steps that talk
        # to the broker are stubbed out in these tests.
        self.client = None


def _patch_step_helpers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    adverse: list[Mapping[str, Any]] | None = None,
    stops: list[Mapping[str, Any]] | None = None,
    rotation: Mapping[str, Any] | None = None,
    raise_on: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Stub out the three downstream helpers + the active-plays loader.

    Returns a ``calls`` dict that captures the invocation of each helper.
    """
    calls: dict[str, list[dict[str, Any]]] = {
        "scan_and_trigger": [],
        "run_stop_loss_check": [],
        "evaluate_rotation": [],
        "load_active": [],
        "attach_mids": [],
    }

    # Stub the active-plays loader — production reads from SQLite.
    def fake_load_active() -> list[dict[str, Any]]:
        calls["load_active"].append({})
        return [{"ticker": "AAA", "play_card_id": "AAA-1"}]

    monkeypatch.setattr(intraday_run, "_load_active_plays", fake_load_active)

    # Stub _attach_current_mids so we don't need a real client.
    def fake_attach(plays, *, executor):
        calls["attach_mids"].append({"plays": list(plays), "executor": executor})
        return [dict(p) for p in plays]

    monkeypatch.setattr(intraday_run, "_attach_current_mids", fake_attach)

    # Stub adverse_news.scan_and_trigger.
    import biotech_sniper.adverse_news as adv_mod

    def fake_scan(executor, plays, **kwargs):
        calls["scan_and_trigger"].append(
            {"executor": executor, "plays": list(plays), "kwargs": kwargs}
        )
        if raise_on == "adverse":
            raise RuntimeError("boom-adverse")
        return list(adverse or [])

    monkeypatch.setattr(adv_mod, "scan_and_trigger", fake_scan)
    monkeypatch.setattr(
        "biotech_sniper.intraday_run.scan_and_trigger" if False else "biotech_sniper.adverse_news.scan_and_trigger",
        fake_scan,
        raising=False,
    )

    # Stub stop_loss.run_stop_loss_check.
    import biotech_sniper.stop_loss as sl_mod

    def fake_stop_check(executor, plays, **kwargs):
        calls["run_stop_loss_check"].append(
            {"executor": executor, "plays": list(plays), "kwargs": kwargs}
        )
        if raise_on == "stop":
            raise RuntimeError("boom-stop")
        return list(stops or [])

    monkeypatch.setattr(sl_mod, "run_stop_loss_check", fake_stop_check)

    # Stub rotation_engine.evaluate_rotation.
    import biotech_sniper.rotation_engine as re_mod

    def fake_eval(**kwargs):
        calls["evaluate_rotation"].append(kwargs)
        if raise_on == "rotation":
            raise RuntimeError("boom-rotation")
        return dict(
            rotation
            or {
                "decisions": [],
                "skips": [],
                "active_count": 0,
                "capacity": 3,
            }
        )

    monkeypatch.setattr(re_mod, "evaluate_rotation", fake_eval)

    return calls


# ---------------------------------------------------------------------------
# Event tag stability
# ---------------------------------------------------------------------------


def test_event_tags_are_canonical_strings() -> None:
    """f-m4-09 contract: event tag values are stable strings used by jq."""
    assert intraday_run.EVENT_INTRADAY_START == "intraday_run_start"
    assert intraday_run.EVENT_INTRADAY_DONE == "intraday_run_done"
    assert intraday_run.EVENT_ADVERSE_NEWS == "adverse_news_check"
    assert intraday_run.EVENT_STOP_LOSS == "stop_loss_tick"
    assert intraday_run.EVENT_ROTATION == "rotation_evaluate"


# ---------------------------------------------------------------------------
# Dry-run path: every step short-circuits to status='dry_run'
# ---------------------------------------------------------------------------


def test_run_intraday_dry_run_does_not_call_helpers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """With ``executor=None`` the trio short-circuits without broker work."""
    calls = _patch_step_helpers(monkeypatch)
    caplog.set_level(logging.INFO, logger="biotech_sniper.intraday_run")

    summary = intraday_run.run_intraday(
        executor=None, today=datetime.date(2025, 4, 27), build_executor=False
    )

    # Every step recorded its dry-run status.
    assert summary["adverse_news_check"]["status"] == "dry_run"
    assert summary["stop_loss_tick"]["status"] == "dry_run"
    # Rotation runs even in dry-run (audit-only mode).
    assert summary["rotation_evaluate"]["status"] == "dry_run"

    # Adverse + stop-loss DID NOT call the broker-touching helpers.
    assert calls["scan_and_trigger"] == []
    assert calls["run_stop_loss_check"] == []
    # But rotation engine WAS still invoked (it runs in dry-run mode itself).
    assert len(calls["evaluate_rotation"]) == 1


def test_run_intraday_emits_three_step_event_tags(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Each trio step emits a log record carrying its own ``event`` tag."""
    _patch_step_helpers(monkeypatch)
    caplog.set_level(logging.INFO, logger="biotech_sniper.intraday_run")

    intraday_run.run_intraday(
        executor=None, today=datetime.date(2025, 4, 27), build_executor=False
    )

    seen = {getattr(r, "event", None) for r in caplog.records}
    for required in (
        "intraday_run_start",
        "adverse_news_check",
        "stop_loss_tick",
        "rotation_evaluate",
        "intraday_run_done",
    ):
        assert required in seen, f"missing event {required!r}; saw {seen}"


# ---------------------------------------------------------------------------
# Live path: executor is forwarded to each helper
# ---------------------------------------------------------------------------


def test_run_intraday_forwards_executor_to_each_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When an executor is supplied, every step receives it."""
    calls = _patch_step_helpers(
        monkeypatch,
        adverse=[{"ticker": "AAA", "status": "submitted"}],
        stops=[{"ticker": "AAA", "status": "submitted"}],
        rotation={
            "decisions": [{"challenger": "BBB"}],
            "skips": [],
            "active_count": 3,
            "capacity": 3,
        },
    )

    sentinel = _SentinelExecutor()
    summary = intraday_run.run_intraday(
        executor=sentinel, today="2025-04-27"
    )

    # Adverse step received the sentinel.
    assert calls["scan_and_trigger"]
    assert calls["scan_and_trigger"][0]["executor"] is sentinel

    # Stop-loss step received the sentinel.
    assert calls["run_stop_loss_check"]
    assert calls["run_stop_loss_check"][0]["executor"] is sentinel

    # Rotation step received the sentinel via kwargs.
    assert calls["evaluate_rotation"]
    assert calls["evaluate_rotation"][0].get("executor") is sentinel

    # Aggregated summary reflects the helper outputs.
    assert summary["adverse_news_check"]["submitted"] == 1
    assert summary["stop_loss_tick"]["submitted"] == 1
    assert summary["rotation_evaluate"]["decisions"] == 1
    assert summary["executor_present"] is True


def test_trio_runs_in_fixed_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """adverse_news_check must run BEFORE stop_loss_tick BEFORE rotation."""
    order: list[str] = []

    def patched_adverse(*, executor=None, today=None) -> dict[str, Any]:
        order.append("adverse")
        return {"considered": 0, "submitted": 0, "skipped": 0,
                "errors": 0, "status": "ok"}

    def patched_stop(*, executor=None, today=None) -> dict[str, Any]:
        order.append("stop")
        return {"considered": 0, "submitted": 0, "skipped": 0,
                "errors": 0, "status": "ok"}

    def patched_rotation(*, executor=None, today=None) -> dict[str, Any]:
        order.append("rotation")
        return {"decisions": 0, "skips": 0, "active_count": 0,
                "capacity": 0, "status": "ok"}

    monkeypatch.setattr(intraday_run, "run_adverse_news_check", patched_adverse)
    monkeypatch.setattr(intraday_run, "run_stop_loss_tick", patched_stop)
    monkeypatch.setattr(intraday_run, "run_rotation_evaluate", patched_rotation)

    intraday_run.run_intraday(executor=_SentinelExecutor(), today="2025-04-27")
    assert order == ["adverse", "stop", "rotation"]


# ---------------------------------------------------------------------------
# Failure isolation: one broken step does NOT abort the rest
# ---------------------------------------------------------------------------


def test_failure_in_adverse_does_not_abort_remaining_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_step_helpers(monkeypatch, raise_on="adverse")

    sentinel = _SentinelExecutor()
    summary = intraday_run.run_intraday(
        executor=sentinel, today=datetime.date(2025, 4, 27)
    )
    # Adverse logged the error and returned a populated summary.
    assert summary["adverse_news_check"]["status"] == "error"
    # Stop-loss + rotation still ran.
    assert len(calls["run_stop_loss_check"]) == 1
    assert len(calls["evaluate_rotation"]) == 1


def test_failure_in_rotation_does_not_abort_earlier_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_step_helpers(monkeypatch, raise_on="rotation")

    sentinel = _SentinelExecutor()
    summary = intraday_run.run_intraday(
        executor=sentinel, today=datetime.date(2025, 4, 27)
    )
    # Earlier steps ran successfully.
    assert len(calls["scan_and_trigger"]) == 1
    assert len(calls["run_stop_loss_check"]) == 1
    # Rotation logged error.
    assert summary["rotation_evaluate"]["status"] == "error"


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------


def test_main_dry_run_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_step_helpers(monkeypatch)
    rc = intraday_run.main(["--dry-run", "--date", "2025-04-27"])
    assert rc == 0


def test_main_rejects_malformed_date(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_step_helpers(monkeypatch)
    with pytest.raises(ValueError):
        intraday_run.main(["--dry-run", "--date", "not-a-date"])


# ---------------------------------------------------------------------------
# Per-step direct invocation
# ---------------------------------------------------------------------------


def test_adverse_news_step_log_carries_status_ok(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_step_helpers(
        monkeypatch,
        adverse=[
            {"ticker": "AAA", "status": "submitted"},
            {"ticker": "BBB", "status": "skipped"},
        ],
    )
    caplog.set_level(logging.INFO, logger="biotech_sniper.intraday_run")

    summary = intraday_run.run_adverse_news_check(
        executor=_SentinelExecutor(), today=datetime.date(2025, 4, 27)
    )
    assert summary["status"] == "ok"
    assert summary["submitted"] == 1
    assert summary["skipped"] == 1
    # Log carries the event tag.
    record = next(
        r for r in caplog.records
        if getattr(r, "event", None) == "adverse_news_check"
    )
    assert record.status == "ok"


def test_stop_loss_step_log_carries_status_ok(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_step_helpers(
        monkeypatch,
        stops=[{"ticker": "AAA", "status": "submitted"}],
    )
    caplog.set_level(logging.INFO, logger="biotech_sniper.intraday_run")

    summary = intraday_run.run_stop_loss_tick(
        executor=_SentinelExecutor(), today="2025-04-27"
    )
    assert summary["status"] == "ok"
    assert summary["submitted"] == 1
    record = next(
        r for r in caplog.records
        if getattr(r, "event", None) == "stop_loss_tick"
    )
    assert record.status == "ok"


def test_rotation_step_log_carries_decisions_count(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_step_helpers(
        monkeypatch,
        rotation={
            "decisions": [{"challenger": "X"}, {"challenger": "Y"}],
            "skips": [{"reason": "below_threshold"}],
            "active_count": 3,
            "capacity": 3,
        },
    )
    caplog.set_level(logging.INFO, logger="biotech_sniper.intraday_run")

    summary = intraday_run.run_rotation_evaluate(
        executor=_SentinelExecutor(), today=datetime.date(2025, 4, 27)
    )
    assert summary["decisions"] == 2
    assert summary["skips"] == 1
    assert summary["status"] == "ok"
    record = next(
        r for r in caplog.records
        if getattr(r, "event", None) == "rotation_evaluate"
    )
    assert record.decisions == 2
    assert record.skips == 1
