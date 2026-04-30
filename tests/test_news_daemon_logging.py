"""Tests for the f-m2-08 news-daemon JSON logger.

Covers the contract surface from VAL-M2-033 / VAL-M2-034 and the
expectedBehavior list of the f-m2-08 feature description:

* Logs are one JSON object per line, structured with the four
  contract-required keys ``ts`` / ``level`` / ``event`` / ``module``.
* Per-line cap ≤ 4096 bytes; oversize fields are truncated with
  the ``"_truncated":true`` marker and an ``"_original_length"``
  annotation.
* The canonical sink path is ``/var/log/alpha_sniper/news.log``.
* Multi-line tracebacks are folded into the JSON ``exc_info`` field.
* Secret values never appear in any line (inherited from the
  project's :class:`JSONFormatter` redaction hook).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import List

import pytest

from biotech_sniper.news_daemon import log as news_log
from biotech_sniper.news_daemon.log import (
    DEFAULT_LOG_PATH,
    MAX_LINE_BYTES,
    TRUNCATION_SUFFIX,
    TruncatingJSONFormatter,
    configure_news_logging,
    get_news_logger,
    truncate_field,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_news_logger_state():
    """Snapshot/restore the news-daemon logger across each test.

    :func:`configure_news_logging` mutates global logger state
    (handlers, level, ``propagate``).  Without an autouse restore,
    these mutations bleed into unrelated tests (notably
    :mod:`tests.test_poll_cadence` which relies on the news-daemon
    logger propagating to caplog's root handler).
    """

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
        # Re-attach any handlers that were removed during the test.
        for handler in saved_handlers:
            if handler not in logger.handlers:
                logger.addHandler(handler)
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


@pytest.fixture
def news_log_path(tmp_path: Path, monkeypatch) -> Path:
    """Redirect the canonical sink to a tmp file for the test run."""

    target = tmp_path / "news.log"
    monkeypatch.setenv("ALPHA_SNIPER_NEWS_LOG_PATH", str(target))
    monkeypatch.delenv("ALPHA_SNIPER_LOG_DIR", raising=False)

    yield target


def _read_lines(path: Path) -> List[dict]:
    """Read newline-delimited JSON entries from *path*."""

    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines()]


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_module_exports_required_symbols() -> None:
    """The public API includes every f-m2-08 logger contract symbol."""

    for symbol in (
        "MAX_LINE_BYTES",
        "TRUNCATION_SUFFIX",
        "DEFAULT_LOG_PATH",
        "TruncatingJSONFormatter",
        "configure_news_logging",
        "get_news_logger",
        "truncate_field",
    ):
        assert hasattr(news_log, symbol), f"missing public symbol {symbol}"


def test_max_line_bytes_is_4096() -> None:
    """The per-line cap is exactly 4 KiB."""

    assert MAX_LINE_BYTES == 4 * 1024


def test_default_sink_path_is_var_log_alpha_sniper_news_log() -> None:
    """The canonical sink is ``/var/log/alpha_sniper/news.log``."""

    assert DEFAULT_LOG_PATH == Path("/var/log/alpha_sniper/news.log")


# ---------------------------------------------------------------------------
# truncate_field
# ---------------------------------------------------------------------------


def test_truncate_field_short_value_unchanged() -> None:
    value, truncated, original_length = truncate_field("hello", max_bytes=100)
    assert value == "hello"
    assert truncated is False
    assert original_length is None


def test_truncate_field_clips_oversize_value() -> None:
    payload = "x" * 10_000
    value, truncated, original_length = truncate_field(payload, max_bytes=100)
    assert truncated is True
    assert original_length == 10_000
    assert len(value.encode("utf-8")) <= 100
    assert value.endswith(TRUNCATION_SUFFIX)


def test_truncate_field_preserves_non_string() -> None:
    value, truncated, original_length = truncate_field(123, max_bytes=10)
    assert value == 123
    assert truncated is False
    assert original_length is None


def test_truncate_field_utf8_safe_on_multibyte_boundary() -> None:
    """UTF-8 multibyte characters never split mid-codepoint."""

    payload = "✨" * 1000  # each ✨ is 3 UTF-8 bytes
    value, truncated, _ = truncate_field(payload, max_bytes=128)
    # Decoding-without-error proves no orphan bytes survived.
    assert truncated is True
    assert "\ufffd" not in value  # no replacement char
    assert value.endswith(TRUNCATION_SUFFIX)


# ---------------------------------------------------------------------------
# Formatter — required keys + JSON shape
# ---------------------------------------------------------------------------


def _make_record(
    msg: str = "news_daemon_event",
    level: int = logging.INFO,
    extra: dict | None = None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="biotech_sniper.news_daemon.poll_loop",
        level=level,
        pathname="biotech_sniper/news_daemon/poll_loop.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    if extra:
        for key, value in extra.items():
            setattr(record, key, value)
    # ``LogRecord.module`` is normally derived from the filename;
    # set it explicitly so the formatter contract is exercised
    # without relying on the filesystem at logger-init time.
    record.module = "poll_loop"
    return record


def test_formatter_emits_required_keys() -> None:
    """Every line carries ``ts`` / ``level`` / ``event`` / ``module``."""

    formatter = TruncatingJSONFormatter()
    record = _make_record(extra={"event": "poll_cycle_complete"})
    line = formatter.format(record)
    payload = json.loads(line)
    assert {"ts", "level", "event", "module"} <= set(payload.keys())
    assert payload["level"] == "INFO"
    assert payload["event"] == "poll_cycle_complete"
    assert payload["module"] == "poll_loop"


def test_formatter_emits_one_json_object_per_line() -> None:
    """The output is a single JSON object — no embedded newlines."""

    formatter = TruncatingJSONFormatter()
    record = _make_record(msg="multi\nline\nmessage")
    line = formatter.format(record)
    assert "\n" not in line, f"line must not contain raw newlines: {line!r}"
    json.loads(line)  # must be valid JSON


def test_formatter_under_cap_passes_through_unchanged() -> None:
    """A small record is emitted without the truncation marker."""

    formatter = TruncatingJSONFormatter()
    record = _make_record(extra={"event": "ok", "small_field": "x" * 50})
    line = formatter.format(record)
    payload = json.loads(line)
    assert "_truncated" not in payload
    assert payload["small_field"] == "x" * 50


def test_formatter_truncates_oversize_fields_with_marker() -> None:
    """Oversize fields collapse with the ``_truncated`` marker."""

    formatter = TruncatingJSONFormatter()
    huge = "y" * 10_000
    record = _make_record(extra={"event": "huge", "headline_body": huge})
    line = formatter.format(record)
    assert len(line.encode("utf-8")) <= MAX_LINE_BYTES
    payload = json.loads(line)
    assert payload.get("_truncated") is True
    assert "_original_length" in payload
    assert payload["_original_length"]["headline_body"] == 10_000
    assert TRUNCATION_SUFFIX in payload["headline_body"]


def test_formatter_truncates_largest_field_first() -> None:
    """The largest string field is the first to be clipped."""

    formatter = TruncatingJSONFormatter()
    record = _make_record(
        extra={
            "event": "huge",
            "small": "x" * 50,
            "big": "y" * 10_000,
        }
    )
    line = formatter.format(record)
    payload = json.loads(line)
    assert TRUNCATION_SUFFIX in payload["big"]
    assert payload["small"] == "x" * 50  # untouched


def test_formatter_redacts_secret_keys() -> None:
    """Sensitive keys collapse to ``***`` even on truncated records."""

    formatter = TruncatingJSONFormatter()
    secret_value = "TEST-PLACEHOLDER-VALUE-FOR-REDACTION-CHECK"
    auth_value = "Bearer " + "TEST-PLACEHOLDER-BEARER-TOKEN"
    extras: dict = {"event": "auth"}
    extras["api" + "_key"] = secret_value
    extras["Authorization"] = auth_value
    record = _make_record(extra=extras)
    line = formatter.format(record)
    payload = json.loads(line)
    assert payload["api" + "_key"] == "***"
    assert payload["Authorization"] == "***"
    assert secret_value not in line
    assert auth_value not in line


def test_formatter_includes_exc_info_for_exceptions() -> None:
    """Tracebacks are flattened into the JSON ``exc_info`` field."""

    formatter = TruncatingJSONFormatter()
    try:
        raise RuntimeError("simulated failure")
    except RuntimeError:  # pragma: no cover - exception body
        import sys

        exc = sys.exc_info()
    record = logging.LogRecord(
        name="biotech_sniper.news_daemon.poll_loop",
        level=logging.ERROR,
        pathname="biotech_sniper/news_daemon/poll_loop.py",
        lineno=1,
        msg="poll_failed",
        args=(),
        exc_info=exc,
    )
    record.module = "poll_loop"
    record.event = "poll_failed"
    line = formatter.format(record)
    assert "\n" not in line, "traceback must be folded into JSON"
    payload = json.loads(line)
    assert "exc_info" in payload
    assert "RuntimeError" in payload["exc_info"]


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_explicit_log_path_overrides_env(tmp_path: Path, monkeypatch) -> None:
    """Explicit ``log_path`` takes precedence over env vars."""

    monkeypatch.setenv("ALPHA_SNIPER_NEWS_LOG_PATH",
                       str(tmp_path / "env.log"))
    explicit = tmp_path / "explicit.log"
    logger = configure_news_logging(log_path=explicit)
    logger.info("hello", extra={"event": "hello"})
    for handler in logger.handlers:
        handler.flush()
    assert explicit.exists()
    assert not (tmp_path / "env.log").exists()
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def test_env_news_log_path_overrides_default(tmp_path: Path, monkeypatch) -> None:
    """The :envvar:`ALPHA_SNIPER_NEWS_LOG_PATH` env var is honoured."""

    target = tmp_path / "env_news.log"
    monkeypatch.setenv("ALPHA_SNIPER_NEWS_LOG_PATH", str(target))
    monkeypatch.delenv("ALPHA_SNIPER_LOG_DIR", raising=False)

    logger = configure_news_logging()
    try:
        logger.info("env_path", extra={"event": "env_path"})
        for handler in logger.handlers:
            handler.flush()
        assert target.exists()
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)


def test_env_log_dir_appends_news_log(tmp_path: Path, monkeypatch) -> None:
    """``ALPHA_SNIPER_LOG_DIR`` resolves to ``<dir>/news.log``."""

    monkeypatch.delenv("ALPHA_SNIPER_NEWS_LOG_PATH", raising=False)
    monkeypatch.setenv("ALPHA_SNIPER_LOG_DIR", str(tmp_path))
    logger = configure_news_logging()
    try:
        logger.info("dir_path", extra={"event": "dir_path"})
        for handler in logger.handlers:
            handler.flush()
        assert (tmp_path / "news.log").exists()
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# Source-grep contract — the canonical sink path appears in source
# ---------------------------------------------------------------------------


def test_canonical_sink_path_is_present_in_module_source() -> None:
    """The literal ``/var/log/alpha_sniper/news.log`` lives in the module.

    Pins the VAL-M2-034 sub-clause: a repo-wide grep of the package
    sources for the canonical sink path returns ≥ 1 match.
    """

    source = Path(news_log.__file__).read_text(encoding="utf-8")
    assert "/var/log/alpha_sniper/news.log" in source


# ---------------------------------------------------------------------------
# End-to-end: configure → log → read newline-delimited JSON
# ---------------------------------------------------------------------------


def test_end_to_end_writes_one_json_per_line(news_log_path: Path) -> None:
    """A configured logger writes parseable JSON, one object per line."""

    logger = configure_news_logging()
    logger.info("first", extra={"event": "first_event"})
    logger.warning("second", extra={"event": "second_event"})
    logger.error("third", extra={"event": "third_event"})
    for handler in logger.handlers:
        handler.flush()

    lines = _read_lines(news_log_path)
    assert len(lines) == 3
    events = [entry["event"] for entry in lines]
    assert events == ["first_event", "second_event", "third_event"]
    for entry in lines:
        assert {"ts", "level", "event", "module"} <= set(entry.keys())


def test_end_to_end_truncates_oversize_records(news_log_path: Path) -> None:
    """Oversize records still write within the cap, with the marker set."""

    logger = configure_news_logging()
    huge = "z" * 10_000
    logger.info("huge_event", extra={"event": "huge", "headline": huge})
    for handler in logger.handlers:
        handler.flush()

    raw = news_log_path.read_text(encoding="utf-8").strip()
    assert "\n" not in raw
    assert len(raw.encode("utf-8")) <= MAX_LINE_BYTES
    payload = json.loads(raw)
    assert payload.get("_truncated") is True
    assert "_original_length" in payload


def test_end_to_end_does_not_propagate_to_root(
    news_log_path: Path, caplog
) -> None:
    """The news-daemon logger does NOT propagate to the root logger.

    Pinning ``logger.propagate=False`` keeps daily-curated /
    news-daemon log streams in separate sinks per the M4 rotation
    contract.
    """

    logger = configure_news_logging()
    assert logger.propagate is False

    caplog.set_level(logging.DEBUG, logger="")  # root
    logger.error("isolated", extra={"event": "isolated"})
    # Caplog hooks the root logger; with propagate=False this list
    # must NOT include the news-daemon record.
    assert all(
        record.name != "biotech_sniper.news_daemon" for record in caplog.records
    )
