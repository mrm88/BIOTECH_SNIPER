"""f-m4-02a — print()-to-logger cleanup tests for universal_news_watcher.

These tests pin the contract that
:mod:`biotech_sniper.intelligence.universal_news_watcher` emits its
runtime output through the project's structured JSON logger
(:mod:`biotech_sniper.logging_setup`) and never via bare ``print()``
statements that bypass the JSON formatter + secret redaction layer.
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


def test_universal_news_watcher_has_no_print_calls() -> None:
    """The module's source must contain zero ``print(...)`` calls."""
    import biotech_sniper.intelligence.universal_news_watcher as unw

    source = Path(unw.__file__).read_text(encoding="utf-8")
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
# Runtime smoke: scan summary emits structured JSON
# ---------------------------------------------------------------------------


@pytest.fixture
def configured_logger(tmp_path, monkeypatch):
    from biotech_sniper import logging_setup

    target = tmp_path / "intraday.log"
    monkeypatch.setenv("ALPHA_SNIPER_LOG_PATH", str(target))
    logging_setup.configure(
        log_path=target,
        log_name="universal_news_watcher",
        force=True,
        add_stream=False,
    )
    yield target
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    logging_setup._CONFIGURED = False  # type: ignore[attr-defined]


def test_print_scan_summary_emits_structured_json(configured_logger):
    """``_print_scan_summary`` writes one JSON line with scan-summary fields."""
    import biotech_sniper.intelligence.universal_news_watcher as unw

    sample = {
        "run_ts": "2026-04-27T13:14:15.000000+00:00",
        "tickers_scanned": 42,
        "high_signal_alerts": [
            {
                "type": "8K_HIGH_SIGNAL",
                "ticker": "MRNA",
                "keyword": "pdufa",
                "summary": "PDUFA date set Q3 2025",
                "url": "https://example.com/x",
                "headline": "fallback",
            }
        ],
        "new_catalyst_dates": [
            {
                "ticker": "MRNA",
                "catalyst_type": "PDUFA",
                "date_str": "2026-09-01",
                "confidence": 88,
            }
        ],
        "new_8ks": [{"a": 1}, {"b": 2}],
        "news_hits": [{"a": 1}],
    }

    unw._print_scan_summary(sample)

    for handler in logging.getLogger().handlers:
        handler.flush()

    contents = configured_logger.read_text(encoding="utf-8").strip().splitlines()
    assert contents, "no log line written"
    matching = [ln for ln in contents if '"universal_news_scan_summary"' in ln]
    assert matching, f"no scan summary line in: {contents!r}"

    payload = json.loads(matching[-1])
    for required_key in ("ts", "level", "event", "module"):
        assert required_key in payload
    assert payload["event"] == "universal_news_scan_summary"
    assert payload["module"] == "universal_news_watcher"
    assert payload["tickers_scanned"] == 42
    assert payload["new_8ks_count"] == 2
    assert payload["news_hits_count"] == 1
    assert payload["high_signal_alerts_count"] == 1
    assert payload["high_signal_alerts"][0]["ticker"] == "MRNA"
    assert payload["new_catalyst_dates_count"] == 1
    assert payload["new_catalyst_dates"][0]["catalyst_type"] == "PDUFA"
