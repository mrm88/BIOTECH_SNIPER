"""Tests for f-fix-m4-03a — wiring configure_news_logging into poll_loop.main.

Covers:

* ``poll_loop.main()`` calls
  :func:`biotech_sniper.news_daemon.log.configure_news_logging` exactly
  once at startup, BEFORE any ``log.info`` / ``log.warning`` emit.
* The call honours the ``LOG_LEVEL`` env var (default ``"INFO"`` when
  unset).
* When the FileHandler is actually attached (no patching), every line
  written to ``news.log`` parses as JSON with the four contract-required
  keys ``ts`` / ``level`` / ``event`` / ``module`` (per VAL-M4-017).

The bug this guards against: the systemd unit's
``StandardOutput=append:/var/log/alpha_sniper/news.log`` directive
captures stdout AND the lastResort StreamHandler's stderr output as
plain text — yielding zero JSON-parseable lines if the structured
FileHandler is never attached.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

import biotech_sniper.news_daemon.poll_loop as poll_loop
from biotech_sniper.news_daemon.log import get_news_logger


# ---------------------------------------------------------------------------
# Fixtures — restore the news-daemon logger across tests so handlers
# installed by configure_news_logging do not leak into the rest of the
# suite (mirrors tests/test_news_daemon_logging.py:_restore_news_logger_state).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_news_logger_state():
    logger = get_news_logger()
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    saved_propagate = logger.propagate
    try:
        yield
    finally:
        for handler in list(logger.handlers):
            if handler not in saved_handlers:
                try:
                    handler.close()
                except Exception:
                    pass
                logger.removeHandler(handler)
        for handler in saved_handlers:
            if handler not in logger.handlers:
                logger.addHandler(handler)
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


# ---------------------------------------------------------------------------
# Test 1 — main() calls configure_news_logging with LOG_LEVEL-derived level.
# ---------------------------------------------------------------------------


def test_main_calls_configure_news_logging_default_info(monkeypatch):
    """Default LOG_LEVEL (unset) → configure_news_logging(level='INFO')."""

    monkeypatch.delenv("LOG_LEVEL", raising=False)

    with mock.patch.object(
        poll_loop, "configure_news_logging"
    ) as mock_cfg:
        rc = poll_loop.main(["--dry-run"])

    assert rc == 0
    assert mock_cfg.call_count == 1, (
        "configure_news_logging must be called exactly once at startup"
    )
    kwargs = mock_cfg.call_args.kwargs
    assert kwargs.get("level") == "INFO", (
        f"Expected level='INFO' (default), got {mock_cfg.call_args!r}"
    )


def test_main_calls_configure_news_logging_with_loglevel_env(monkeypatch):
    """LOG_LEVEL=DEBUG → configure_news_logging(level='DEBUG')."""

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    with mock.patch.object(
        poll_loop, "configure_news_logging"
    ) as mock_cfg:
        rc = poll_loop.main(["--dry-run"])

    assert rc == 0
    assert mock_cfg.call_count == 1
    kwargs = mock_cfg.call_args.kwargs
    assert kwargs.get("level") == "DEBUG", (
        f"Expected level='DEBUG' from env, got {mock_cfg.call_args!r}"
    )


def test_main_calls_configure_news_logging_with_loglevel_warning(monkeypatch):
    """LOG_LEVEL=WARNING → configure_news_logging(level='WARNING')."""

    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    with mock.patch.object(
        poll_loop, "configure_news_logging"
    ) as mock_cfg:
        rc = poll_loop.main(["--dry-run"])

    assert rc == 0
    assert mock_cfg.call_count == 1
    kwargs = mock_cfg.call_args.kwargs
    assert kwargs.get("level") == "WARNING"


# ---------------------------------------------------------------------------
# Test 2 — every line written to news.log parses as JSON with the four
# contract-required keys.
# ---------------------------------------------------------------------------


def test_news_log_lines_are_json(tmp_path: Path, monkeypatch):
    """Every non-empty line written by main() to news.log is JSON.

    Per VAL-M4-017: ``tail -n 200 /var/log/alpha_sniper/news.log |
    while read line; do echo "$line" | jq -e 'has("ts") and has("level")
    and has("event") and has("module")'; done`` must succeed.
    """

    log_path = tmp_path / "news.log"
    monkeypatch.setenv("ALPHA_SNIPER_NEWS_LOG_PATH", str(log_path))
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    # Trigger at least one extra WARNING via the poll-cadence override
    # so the test sees more than just the dry-run-exit INFO line.
    monkeypatch.setenv("NEWS_POLL_SECONDS", "5")  # below floor → clamp WARN

    rc = poll_loop.main(["--dry-run"])
    assert rc == 0

    # Flush all handlers so the file contents land before we read.
    logger = get_news_logger()
    for handler in logger.handlers:
        try:
            handler.flush()
        except Exception:
            pass

    assert log_path.exists(), (
        f"configure_news_logging must create the log file at {log_path}"
    )

    content = log_path.read_text(encoding="utf-8")
    lines = [line for line in content.splitlines() if line.strip()]
    assert lines, (
        "Expected at least one structured JSON log line after "
        "main(['--dry-run']) but news.log is empty"
    )

    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"news.log line is not JSON: {line!r} (err={exc!r})"
            )
        assert "ts" in parsed, f"missing 'ts' in {line!r}"
        assert "level" in parsed, f"missing 'level' in {line!r}"
        assert "event" in parsed, f"missing 'event' in {line!r}"
        assert "module" in parsed, f"missing 'module' in {line!r}"
        assert isinstance(parsed["ts"], str)
        assert isinstance(parsed["level"], str)
        assert isinstance(parsed["event"], str)
        assert isinstance(parsed["module"], str)


def test_news_log_propagation_disabled(tmp_path: Path, monkeypatch):
    """The news-daemon logger must NOT propagate to root.

    configure_news_logging() pins ``propagate = False`` so the daemon's
    structured records do not duplicate into the project root logger
    (which routes to ``daily.log``).  Verifying after main() runs.
    """

    log_path = tmp_path / "news.log"
    monkeypatch.setenv("ALPHA_SNIPER_NEWS_LOG_PATH", str(log_path))
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    rc = poll_loop.main(["--dry-run"])
    assert rc == 0

    logger = get_news_logger()
    assert logger.propagate is False, (
        "configure_news_logging must disable propagation so structured "
        "JSON records do not leak to the root logger"
    )
