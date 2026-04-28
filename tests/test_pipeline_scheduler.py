"""f-m4-02a — print()-to-logger cleanup tests for pipeline_scheduler.

These tests pin the contract that
:mod:`biotech_sniper.intelligence.pipeline_scheduler` emits its
runtime output through the project's structured JSON logger
(:mod:`biotech_sniper.logging_setup`) and never via bare ``print()``
statements that bypass the JSON formatter + secret redaction layer.

Coverage:

* Static AST scan asserts the active source has zero ``print(...)``
  calls (anywhere in the file — pipeline_scheduler has no legacy
  CLI banner exception, unlike audit.py).
* Runtime smoke: invoking ``print_schedule_summary()`` with the
  project logger configured at a temp file emits a single JSON line
  with the contract-required ``ts`` / ``level`` / ``event`` /
  ``module`` keys plus the schedule-specific payload.
"""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Static guard
# ---------------------------------------------------------------------------


def test_pipeline_scheduler_has_no_print_calls() -> None:
    """The module's active source must contain zero ``print(...)`` calls."""
    import biotech_sniper.intelligence.pipeline_scheduler as ps

    source = Path(ps.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    offending: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ):
            offending.append((node.lineno, ast.unparse(node)))

    assert offending == [], f"unexpected print() calls: {offending}"


# ---------------------------------------------------------------------------
# Runtime smoke: log lines parse as JSON with expected event tags
# ---------------------------------------------------------------------------


@pytest.fixture
def configured_logger(tmp_path, monkeypatch):
    """Configure logging_setup to write JSON to ``tmp_path/scheduler.log``."""
    from biotech_sniper import logging_setup

    target = tmp_path / "scheduler.log"
    monkeypatch.setenv("ALPHA_SNIPER_LOG_PATH", str(target))
    logging_setup.configure(
        log_path=target,
        log_name="pipeline_scheduler",
        force=True,
        add_stream=False,
    )
    yield target
    # Drop our handlers so subsequent tests aren't tainted.
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    logging_setup._CONFIGURED = False  # type: ignore[attr-defined]


def test_print_schedule_summary_emits_structured_json(
    configured_logger, monkeypatch
):
    """``print_schedule_summary()`` writes a JSON log line carrying the schedule fields."""
    import biotech_sniper.intelligence.pipeline_scheduler as ps

    # Avoid hitting any real disk state by stubbing the helpers that
    # load universe / pipeline state. We exercise the logging path,
    # not the schedule resolution.
    monkeypatch.setattr(ps, "_load_full_universe", lambda: {"AAA": {}, "BBB": {}})
    monkeypatch.setattr(ps, "load_pipeline_state", lambda: {"AAA": {"tier": 1, "last_updated": "1970-01-01"}})
    monkeypatch.setattr(
        ps,
        "_get_due_tickers",
        lambda u, s, t: {
            "new": ["BBB"],
            "tier_1": ["AAA"],
            "tier_2": [],
            "tier_3": [],
        },
    )

    ps.print_schedule_summary()

    # Flush handlers so the file picks up the line.
    for handler in logging.getLogger().handlers:
        handler.flush()

    contents = configured_logger.read_text(encoding="utf-8").strip().splitlines()
    assert contents, "no log line written"
    # The summary call emits exactly one structured line; later tests
    # may add more, so we filter to the event we care about.
    matching = [ln for ln in contents if '"pipeline_schedule_summary"' in ln]
    assert matching, f"no pipeline_schedule_summary line in: {contents!r}"

    payload = json.loads(matching[-1])
    for required_key in ("ts", "level", "event", "module"):
        assert required_key in payload, f"missing {required_key} in {payload!r}"
    assert payload["event"] == "pipeline_schedule_summary"
    assert payload["module"] == "pipeline_scheduler"
    assert payload["universe_total"] == 2
    assert payload["state_tracked"] == 1
    assert payload["due_total"] == 2
    assert payload["due_new"] == 1
    assert payload["due_tier_1"] == 1
    assert payload["tier_1_sample"] == ["AAA"]
    assert payload["new_sample"] == ["BBB"]
