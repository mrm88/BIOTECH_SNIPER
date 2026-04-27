"""Tests for :mod:`biotech_sniper.logging_setup`.

Covers the contract surface from f-m4-02 / VAL-M4-024 / VAL-M4-025 /
VAL-M4-026 / VAL-M4-027:

* JSON shape — every emitted line carries ``ts``, ``level``, ``event``
  and ``module``.
* ``ts`` is ISO-8601 UTC with an explicit ``+00:00`` (or ``Z``) suffix.
* ``level`` belongs to the documented enum.
* Secret redaction — keys containing ``key`` / ``secret`` / ``token``
  / ``authorization`` (case-insensitive) collapse to ``***`` and the
  literal secret value never reaches the log line.
* :func:`configure` is idempotent unless ``force=True`` is passed and
  honours environment overrides for the destination path.
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path

import pytest

from biotech_sniper import logging_setup


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_logging(tmp_path, monkeypatch):
    """Reset the root logger and the module's configured-flag.

    Each test gets a clean slate: no pre-existing handlers leak in,
    env-var overrides are stripped, and the destination file lives in
    ``tmp_path`` so the default ``/var/log/alpha_sniper`` path is
    never touched.
    """
    # Strip env overrides that could leak from the surrounding shell.
    monkeypatch.delenv("ALPHA_SNIPER_LOG_PATH", raising=False)
    monkeypatch.delenv("ALPHA_SNIPER_LOG_DIR", raising=False)

    # Force the module to forget any previous configuration so each
    # test calls ``configure`` from a clean state.
    importlib.reload(logging_setup)

    # Detach any handlers other tests / imports may have installed.
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    yield tmp_path

    # Tear down: drop our handlers so subsequent unrelated tests do
    # not see double-emitted lines.
    for handler in list(root.handlers):
        root.removeHandler(handler)


def _read_lines(path: Path) -> list[dict]:
    """Read newline-delimited JSON entries from *path*."""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines()]


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_module_exports_expected_symbols():
    """Public API includes the documented helpers."""
    for symbol in ("configure", "get_logger", "JSONFormatter", "REDACT_TOKEN"):
        assert hasattr(logging_setup, symbol), f"missing public symbol {symbol}"


def test_configure_returns_root_logger(fresh_logging):
    log_path = fresh_logging / "configured.log"
    root = logging_setup.configure(level="INFO", log_path=log_path, force=True)
    assert isinstance(root, logging.Logger)
    assert root.level == logging.INFO


# ---------------------------------------------------------------------------
# JSON shape
# ---------------------------------------------------------------------------


def test_json_line_has_required_keys(fresh_logging):
    """Every emitted line carries ts/level/event/module."""
    log_path = fresh_logging / "shape.log"
    logging_setup.configure(log_path=log_path, force=True, add_stream=False)

    log = logging.getLogger("biotech_sniper.tests.shape")
    log.info("daily_start", extra={"event": "daily_start"})
    log.warning("scoring_skipped", extra={"event": "scoring_skipped"})

    for handler in logging.getLogger().handlers:
        handler.flush()

    entries = _read_lines(log_path)
    assert len(entries) == 2
    for payload in entries:
        for key in ("ts", "level", "event", "module"):
            assert key in payload, f"line missing required key {key}: {payload}"
        assert isinstance(payload["module"], str) and payload["module"]


def test_ts_is_iso8601_utc(fresh_logging):
    """Timestamp ends with the documented timezone suffix."""
    log_path = fresh_logging / "ts.log"
    logging_setup.configure(log_path=log_path, force=True, add_stream=False)

    logging.getLogger(__name__).info("tick", extra={"event": "tick"})
    for handler in logging.getLogger().handlers:
        handler.flush()

    payload = _read_lines(log_path)[0]
    ts = payload["ts"]
    # VAL-M4-024: ISO-8601 UTC with ``Z`` or ``+00:00``.
    assert ts.endswith("+00:00") or ts.endswith("Z"), ts
    # ``T`` separator between date and time.
    assert "T" in ts


def test_level_in_documented_enum(fresh_logging):
    """``level`` matches the VAL-M4-024 documented set."""
    log_path = fresh_logging / "level.log"
    logging_setup.configure(level="DEBUG", log_path=log_path, force=True, add_stream=False)

    log = logging.getLogger("biotech_sniper.tests.level")
    log.debug("d", extra={"event": "d"})
    log.info("i", extra={"event": "i"})
    log.warning("w", extra={"event": "w"})
    log.error("e", extra={"event": "e"})
    log.critical("c", extra={"event": "c"})

    for handler in logging.getLogger().handlers:
        handler.flush()

    levels = {entry["level"] for entry in _read_lines(log_path)}
    assert levels <= {"DEBUG", "INFO", "WARN", "WARNING", "ERROR", "CRITICAL"}
    assert {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} <= levels


def test_event_falls_back_to_message_when_extra_missing(fresh_logging):
    """No explicit ``event=`` extra → message text populates the field."""
    log_path = fresh_logging / "event.log"
    logging_setup.configure(log_path=log_path, force=True, add_stream=False)
    logging.getLogger(__name__).info("plain_message")

    for handler in logging.getLogger().handlers:
        handler.flush()

    payload = _read_lines(log_path)[0]
    assert payload["event"] == "plain_message"


def test_module_field_uses_logger_module(fresh_logging):
    """``module`` reflects the python file the call originated from."""
    log_path = fresh_logging / "module.log"
    logging_setup.configure(log_path=log_path, force=True, add_stream=False)
    logging.getLogger("biotech_sniper.tests.module").info(
        "ping", extra={"event": "ping"}
    )

    for handler in logging.getLogger().handlers:
        handler.flush()

    payload = _read_lines(log_path)[0]
    # ``record.module`` is the file basename minus the extension; for
    # this test that's ``test_logging_setup``.
    assert payload["module"] == "test_logging_setup"


def test_extras_propagate_into_payload(fresh_logging):
    log_path = fresh_logging / "extras.log"
    logging_setup.configure(log_path=log_path, force=True, add_stream=False)
    logging.getLogger("biotech_sniper.tests.extras").info(
        "daily_done",
        extra={
            "event": "daily_done",
            "duration_sec": 12.5,
            "orders_submitted": 3,
        },
    )

    for handler in logging.getLogger().handlers:
        handler.flush()

    payload = _read_lines(log_path)[0]
    assert payload["duration_sec"] == 12.5
    assert payload["orders_submitted"] == 3


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_name",
    [
        "api_key",
        "API_KEY",
        "xai_api_key",
        "client_secret",
        "ANTHROPIC_API_KEY",
        "github_token",
        "Authorization",
        "authorization",
    ],
)
def test_redacts_sensitive_field_keys(fresh_logging, field_name):
    """Sensitive field keys collapse to ``***`` regardless of casing."""
    log_path = fresh_logging / "secrets.log"
    logging_setup.configure(log_path=log_path, force=True, add_stream=False)
    secret_value = "xai-thisshouldneverappearplaintext-12345"
    logging.getLogger("biotech_sniper.tests.redact").info(
        "auth_attempt",
        extra={"event": "auth_attempt", field_name: secret_value},
    )

    for handler in logging.getLogger().handlers:
        handler.flush()

    raw = log_path.read_text(encoding="utf-8")
    assert secret_value not in raw, "secret value leaked into log file"
    payload = _read_lines(log_path)[0]
    assert payload[field_name] == logging_setup.REDACT_TOKEN


def test_redaction_recurses_into_nested_dicts(fresh_logging):
    log_path = fresh_logging / "nested.log"
    logging_setup.configure(log_path=log_path, force=True, add_stream=False)
    logging.getLogger("biotech_sniper.tests.redact_nested").info(
        "outbound_request",
        extra={
            "event": "outbound_request",
            "request": {
                "url": "https://api.example.com/v1/orders",
                "headers": {
                    "Authorization": "Bearer xai-leak-do-not-log",
                    "Content-Type": "application/json",
                },
            },
        },
    )

    for handler in logging.getLogger().handlers:
        handler.flush()

    raw = log_path.read_text(encoding="utf-8")
    assert "xai-leak-do-not-log" not in raw
    payload = _read_lines(log_path)[0]
    assert payload["request"]["headers"]["Authorization"] == logging_setup.REDACT_TOKEN
    assert payload["request"]["headers"]["Content-Type"] == "application/json"
    assert payload["request"]["url"] == "https://api.example.com/v1/orders"


def test_redaction_walks_lists(fresh_logging):
    log_path = fresh_logging / "lists.log"
    logging_setup.configure(log_path=log_path, force=True, add_stream=False)
    logging.getLogger("biotech_sniper.tests.redact_list").info(
        "batch_keys",
        extra={
            "event": "batch_keys",
            "providers": [
                {"name": "xai", "api_key": "xai-AAA"},
                {"name": "anthropic", "api_key": "sk-ant-BBB"},
            ],
        },
    )

    for handler in logging.getLogger().handlers:
        handler.flush()

    raw = log_path.read_text(encoding="utf-8")
    assert "xai-AAA" not in raw
    assert "sk-ant-BBB" not in raw
    payload = _read_lines(log_path)[0]
    for provider in payload["providers"]:
        assert provider["api_key"] == logging_setup.REDACT_TOKEN
        assert provider["name"] in {"xai", "anthropic"}


def test_redaction_does_not_affect_non_sensitive_fields(fresh_logging):
    log_path = fresh_logging / "non_sensitive.log"
    logging_setup.configure(log_path=log_path, force=True, add_stream=False)
    logging.getLogger("biotech_sniper.tests.non_sensitive").info(
        "status_ping",
        extra={"event": "status_ping", "ticker": "PFE", "qty": 5},
    )

    for handler in logging.getLogger().handlers:
        handler.flush()

    payload = _read_lines(log_path)[0]
    assert payload["ticker"] == "PFE"
    assert payload["qty"] == 5


# ---------------------------------------------------------------------------
# Path resolution & idempotency
# ---------------------------------------------------------------------------


def test_log_path_env_var_override(fresh_logging, monkeypatch):
    target = fresh_logging / "env_override.log"
    monkeypatch.setenv("ALPHA_SNIPER_LOG_PATH", str(target))
    logging_setup.configure(force=True, add_stream=False)
    logging.getLogger("biotech_sniper.tests.env").info(
        "ping", extra={"event": "ping"}
    )
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert target.exists() and target.read_text(encoding="utf-8").strip()


def test_log_dir_env_var_combines_with_log_name(fresh_logging, monkeypatch):
    monkeypatch.setenv("ALPHA_SNIPER_LOG_DIR", str(fresh_logging))
    logging_setup.configure(force=True, add_stream=False, log_name="intraday")
    logging.getLogger("biotech_sniper.tests.dir").info(
        "tick", extra={"event": "tick"}
    )
    for handler in logging.getLogger().handlers:
        handler.flush()
    expected = fresh_logging / "intraday.log"
    assert expected.exists()


def test_default_path_falls_back_to_stream_when_unwritable(monkeypatch):
    """Default ``/var/log/alpha_sniper`` is unwritable on dev hosts.

    The configure() call must not raise even when the default
    directory cannot be created; instead it should keep a stream
    handler so the import-time call sites in legacy modules stay
    safe.
    """
    monkeypatch.delenv("ALPHA_SNIPER_LOG_PATH", raising=False)
    monkeypatch.delenv("ALPHA_SNIPER_LOG_DIR", raising=False)
    importlib.reload(logging_setup)
    # Should not raise even though the default dir is unreachable.
    root = logging_setup.configure(force=True, add_stream=True)
    assert any(
        isinstance(h, logging.StreamHandler) for h in root.handlers
    )


def test_configure_is_idempotent_without_force(fresh_logging):
    log_path = fresh_logging / "idem.log"
    logging_setup.configure(log_path=log_path, force=True, add_stream=False)
    handlers_before = list(logging.getLogger().handlers)
    logging_setup.configure(log_path=log_path)  # without ``force``
    handlers_after = list(logging.getLogger().handlers)
    assert handlers_before == handlers_after


def test_configure_with_force_resets_handlers(fresh_logging):
    log_path = fresh_logging / "reset.log"
    logging_setup.configure(log_path=log_path, force=True, add_stream=False)
    logging_setup.configure(log_path=log_path, force=True, add_stream=False)
    # No handler duplication after force-reconfigure.
    file_handlers = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.FileHandler)
    ]
    assert len(file_handlers) == 1


def test_get_logger_auto_configures(monkeypatch, tmp_path):
    monkeypatch.delenv("ALPHA_SNIPER_LOG_PATH", raising=False)
    monkeypatch.delenv("ALPHA_SNIPER_LOG_DIR", raising=False)
    importlib.reload(logging_setup)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    log = logging_setup.get_logger("biotech_sniper.tests.auto")
    assert isinstance(log, logging.Logger)
    # configure() ran during get_logger(); root should have at least
    # the stream handler installed.
    assert root.handlers, "get_logger did not auto-configure root"


# ---------------------------------------------------------------------------
# Single-source-of-truth contract
# ---------------------------------------------------------------------------


def test_force_strips_existing_basic_config_handlers(fresh_logging):
    """A pre-existing ``logging.basicConfig`` handler is removed."""
    # Simulate a legacy module having called ``logging.basicConfig``.
    logging.basicConfig(level=logging.INFO, format="legacy %(message)s")
    legacy_handlers = list(logging.getLogger().handlers)
    assert legacy_handlers, "fixture precondition: basicConfig added a handler"

    log_path = fresh_logging / "single_src.log"
    logging_setup.configure(log_path=log_path, force=True, add_stream=False)

    # ``configure(force=True)`` must guarantee a single, project-owned
    # FileHandler — no leftover legacy stream handlers.
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.FileHandler)
    formatter = handlers[0].formatter
    assert isinstance(formatter, logging_setup.JSONFormatter)


def test_exception_info_serialized(fresh_logging):
    log_path = fresh_logging / "exc.log"
    logging_setup.configure(log_path=log_path, force=True, add_stream=False)
    log = logging.getLogger("biotech_sniper.tests.exc")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log.error("crash", extra={"event": "crash"}, exc_info=True)
    for handler in logging.getLogger().handlers:
        handler.flush()

    payload = _read_lines(log_path)[0]
    assert payload["event"] == "crash"
    assert "RuntimeError" in payload.get("exc_info", "")
    assert "boom" in payload.get("exc_info", "")
