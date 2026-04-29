"""Unit tests for the f-m4-03 ``audit.py`` extension.

The audit module ships an importable surface
(:func:`biotech_sniper.audit.build_m4_audit_payload`,
:func:`biotech_sniper.audit.write_audit_latest`, plus the ``_probe_*``
helpers) on top of the legacy module-level script. These tests cover
that importable surface in isolation:

* :func:`_atomic_write_json` writes via temp + rename, leaves no
  partial state on failure.
* :func:`_classify_http_status` maps HTTP codes to the contract enum.
* :func:`_probe_with_timing` decorates probes with timing + uniform
  error handling.
* Each ``_probe_*`` helper returns the contract shape (``status`` /
  ``last_checked`` / ``latency_ms``) under success, failure, and
  missing-credential conditions.
* :func:`build_m4_audit_payload` produces every required top-level
  field with all seven canonical source keys populated.
* :func:`write_audit_latest` merges the M4 payload over an existing
  ``audit_latest.json`` without clobbering legacy sibling keys.

Importing :mod:`biotech_sniper.audit` runs the legacy module-level
script (network probes + SQLite probes + JSON write). To keep tests
fast and offline we patch ``requests.get`` / ``requests.post`` BEFORE
the import in a session-level fixture.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(scope="module", autouse=True)
def _import_audit_offline():
    """Import :mod:`biotech_sniper.audit` with all network calls stubbed.

    The legacy module body runs ~10 HTTP probes at import time. We
    monkeypatch :mod:`requests` BEFORE the import so the test session
    stays hermetic and fast. Subsequent re-imports (via
    ``importlib.reload``) hit the patched session unless a test
    explicitly overrides ``requests.get`` / ``requests.post``.
    """
    import requests

    class _StubResponse:
        status_code = 200
        text = ""

        def json(self):  # noqa: D401
            return {"studies": [], "results": []}

    with patch.object(requests, "get", return_value=_StubResponse()), patch.object(
        requests, "post", return_value=_StubResponse()
    ):
        # Force a fresh import so the patched ``requests`` actually
        # services the module-level probes.
        sys.modules.pop("biotech_sniper.audit", None)
        import biotech_sniper.audit  # noqa: F401
    yield


@pytest.fixture
def audit_module():
    """Return the live :mod:`biotech_sniper.audit` module."""
    import biotech_sniper.audit as audit

    return audit


# ---------------------------------------------------------------------------
# _atomic_write_json
# ---------------------------------------------------------------------------


def test_atomic_write_creates_file_with_expected_contents(audit_module, tmp_path):
    target = tmp_path / "out.json"
    audit_module._atomic_write_json(target, {"a": 1, "b": [2, 3]})
    assert target.is_file()
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "b": [2, 3]}


def test_atomic_write_creates_parent_dir(audit_module, tmp_path):
    target = tmp_path / "missing" / "subdir" / "out.json"
    audit_module._atomic_write_json(target, {"x": True})
    assert target.is_file()


def test_atomic_write_replaces_existing_file(audit_module, tmp_path):
    target = tmp_path / "out.json"
    target.write_text(json.dumps({"old": True}), encoding="utf-8")
    audit_module._atomic_write_json(target, {"new": True})
    assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}


def test_atomic_write_no_partial_state_on_failure(audit_module, tmp_path, monkeypatch):
    """When ``os.replace`` fails the destination must be unchanged."""
    target = tmp_path / "existing.json"
    target.write_text(json.dumps({"keep": "this"}), encoding="utf-8")

    # Capture the temp file path that the helper produces, then simulate
    # a rename failure to verify cleanup + no-clobber behavior.
    real_replace = os.replace

    def boom(*args, **kwargs):  # noqa: ANN001 — match os.replace signature
        raise OSError("simulated rename failure")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError, match="simulated rename failure"):
        audit_module._atomic_write_json(target, {"new": "value"})

    # Destination preserved.
    assert json.loads(target.read_text(encoding="utf-8")) == {"keep": "this"}

    # Temp file should have been cleaned up — no ``.tmp`` siblings linger.
    leftovers = list(tmp_path.glob("existing.json.*.tmp"))
    assert leftovers == [], f"temp files leaked: {leftovers}"

    # Sanity-check the helper still works after the monkeypatch is undone
    # (no permanent state corruption from the failed write).
    monkeypatch.setattr("os.replace", real_replace)
    audit_module._atomic_write_json(target, {"after": "ok"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"after": "ok"}


# ---------------------------------------------------------------------------
# _classify_http_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, "ok"),
        (201, "ok"),
        (299, "ok"),
        (301, "degraded"),
        (302, "degraded"),
        (400, "degraded"),
        (401, "degraded"),
        (403, "degraded"),
        (404, "degraded"),
        (499, "degraded"),
        (500, "error"),
        (502, "error"),
        (599, "error"),
        (0, "error"),
        (-1, "error"),
    ],
)
def test_classify_http_status(audit_module, status, expected):
    assert audit_module._classify_http_status(status) == expected


# ---------------------------------------------------------------------------
# _probe_with_timing
# ---------------------------------------------------------------------------


def test_probe_with_timing_decorates_success(audit_module):
    out = audit_module._probe_with_timing(lambda: {"status": "ok", "extra": 7})
    assert out["status"] == "ok"
    assert out["extra"] == 7
    assert "last_checked" in out
    assert isinstance(out["latency_ms"], int)
    assert out["latency_ms"] >= 0


def test_probe_with_timing_catches_exceptions(audit_module):
    def boom():
        raise RuntimeError("nope")

    out = audit_module._probe_with_timing(boom)
    assert out["status"] == "error"
    assert "nope" in out["error"]
    assert "last_checked" in out
    assert isinstance(out["latency_ms"], int)


def test_probe_with_timing_handles_non_dict_return(audit_module):
    out = audit_module._probe_with_timing(lambda: "not-a-dict")
    assert out["status"] == "error"
    assert "non-dict" in out["error"]


# ---------------------------------------------------------------------------
# Individual probes — exercise success, http-error, and missing-key paths.
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code


def test_probe_ctgov_ok(audit_module, monkeypatch):
    monkeypatch.setattr(
        "biotech_sniper.audit.requests.get", lambda *a, **kw: _FakeResp(200)
    )
    out = audit_module._probe_ctgov()
    assert out["status"] == "ok"
    assert out["http_status"] == 200


def test_probe_sec_edgar_5xx_is_error(audit_module, monkeypatch):
    monkeypatch.setattr(
        "biotech_sniper.audit.requests.get", lambda *a, **kw: _FakeResp(503)
    )
    out = audit_module._probe_sec_edgar()
    assert out["status"] == "error"
    assert out["http_status"] == 503


def test_probe_news_rss_4xx_is_degraded(audit_module, monkeypatch):
    monkeypatch.setattr(
        "biotech_sniper.audit.requests.get", lambda *a, **kw: _FakeResp(429)
    )
    out = audit_module._probe_news_rss()
    assert out["status"] == "degraded"
    assert out["http_status"] == 429


def test_probe_xai_missing_key_is_degraded(audit_module, monkeypatch):
    monkeypatch.setattr("biotech_sniper.config.get_xai_api_key", lambda: None)
    out = audit_module._probe_xai()
    assert out["status"] == "degraded"
    assert out["reason"] == "api_key_missing"


def test_probe_xai_with_key(audit_module, monkeypatch):
    monkeypatch.setattr(
        "biotech_sniper.config.get_xai_api_key", lambda: "xai-fake"
    )
    monkeypatch.setattr(
        "biotech_sniper.audit.requests.get", lambda *a, **kw: _FakeResp(200)
    )
    out = audit_module._probe_xai()
    assert out["status"] == "ok"
    assert out["http_status"] == 200


def test_probe_anthropic_missing_key(audit_module, monkeypatch):
    monkeypatch.setattr(
        "biotech_sniper.config.get_anthropic_api_key", lambda: None
    )
    out = audit_module._probe_anthropic()
    assert out == {"status": "degraded", "reason": "api_key_missing"}


def test_probe_gemini_missing_key(audit_module, monkeypatch):
    monkeypatch.setattr(
        "biotech_sniper.config.get_gemini_api_key", lambda: None
    )
    out = audit_module._probe_gemini()
    assert out == {"status": "degraded", "reason": "api_key_missing"}


def test_probe_alpaca_paper_credentials_missing(audit_module, monkeypatch):
    """Without ALPACA creds the probe degrades gracefully without raising."""
    from biotech_sniper.alpaca_client import AlpacaAuthError

    def raise_auth(*a, **kw):
        raise AlpacaAuthError("creds missing")

    monkeypatch.setattr(
        "biotech_sniper.alpaca_client.AlpacaClient.__init__", raise_auth
    )
    out = audit_module._probe_alpaca_paper()
    assert out["status"] == "degraded"
    assert out["reason"] == "credentials_missing"
    assert out["equity"] is None


def test_probe_alpaca_paper_success(audit_module, monkeypatch):
    """When ``get_account`` succeeds the equity is folded into the result."""

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def get_account(self):
            return {"equity": 12345.67, "currency": "USD"}

    monkeypatch.setattr("biotech_sniper.alpaca_client.AlpacaClient", _FakeClient)
    out = audit_module._probe_alpaca_paper()
    assert out["status"] == "ok"
    assert out["equity"] == pytest.approx(12345.67)
    assert out["currency"] == "USD"


# ---------------------------------------------------------------------------
# build_m4_audit_payload — full contract shape with mocked probes.
# ---------------------------------------------------------------------------


def _patch_all_probes_ok(monkeypatch):
    """Stub every probe to succeed so we can exercise the payload builder."""
    monkeypatch.setattr(
        "biotech_sniper.audit._probe_ctgov", lambda: {"status": "ok"}
    )
    monkeypatch.setattr(
        "biotech_sniper.audit._probe_sec_edgar", lambda: {"status": "ok"}
    )
    monkeypatch.setattr(
        "biotech_sniper.audit._probe_news_rss", lambda: {"status": "ok"}
    )
    monkeypatch.setattr(
        "biotech_sniper.audit._probe_alpaca_paper",
        lambda: {"status": "ok", "equity": 25000.0},
    )
    monkeypatch.setattr(
        "biotech_sniper.audit._probe_xai", lambda: {"status": "ok"}
    )
    monkeypatch.setattr(
        "biotech_sniper.audit._probe_anthropic", lambda: {"status": "ok"}
    )
    monkeypatch.setattr(
        "biotech_sniper.audit._probe_gemini", lambda: {"status": "ok"}
    )

    # Re-bind the registry so it picks up the patched callables.
    monkeypatch.setattr(
        "biotech_sniper.audit._M4_PROBES",
        tuple(
            (name, getattr(__import__("biotech_sniper.audit", fromlist=["_x"]), fn_name))
            for name, fn_name in (
                ("ct.gov", "_probe_ctgov"),
                ("sec_edgar", "_probe_sec_edgar"),
                ("news_rss", "_probe_news_rss"),
                ("alpaca_paper", "_probe_alpaca_paper"),
                ("xai", "_probe_xai"),
                ("anthropic", "_probe_anthropic"),
                ("gemini", "_probe_gemini"),
            )
        ),
    )


def _seed_db(db_path: Path) -> None:
    """Initialise a fresh SQLite db with the project schema + 1 row each.

    Used by the payload-builder tests so ``last_daily_run`` and
    ``last_intraday_run`` are non-null, exercising the
    ``MAX(<column>)`` query helpers.
    """
    from biotech_sniper.db import connect, run_migrations

    conn = connect(db_path)
    try:
        run_migrations(conn)
        conn.execute(
            "INSERT INTO scoring_cache (ticker, as_of_date, ensemble_score, "
            "created_at) VALUES (?, ?, ?, ?)",
            ("TEST", "2026-04-27", 0.6, "2026-04-27T13:00:00.000Z"),
        )
        # ``execution_events`` requires a parent ``paper_orders`` row
        # because of the FK. Insert a sentinel paper_orders row first.
        conn.execute(
            "INSERT INTO paper_orders (id, status, client_order_id) "
            "VALUES (?, ?, ?)",
            ("ord-1", "submitted", "test-co-1"),
        )
        conn.execute(
            "INSERT INTO execution_events (paper_order_id, event_type, "
            "event_at) VALUES (?, ?, ?)",
            ("ord-1", "submitted", "2026-04-27T18:30:00.000Z"),
        )
        conn.commit()
    finally:
        conn.close()


def test_build_m4_audit_payload_contains_all_required_keys(
    audit_module, tmp_path, monkeypatch
):
    _patch_all_probes_ok(monkeypatch)
    db_path = tmp_path / "alpha_sniper.db"
    _seed_db(db_path)

    payload = audit_module.build_m4_audit_payload(db_path=db_path)

    # All M4 contract top-level keys present.
    for key in (
        "generated_at",
        "sources",
        "last_daily_run",
        "last_intraday_run",
        "db_size_bytes",
        "paper_account_equity",
    ):
        assert key in payload, f"missing top-level key: {key}"

    # Sources map carries every canonical key with the contract shape.
    for src_name in (
        "ct.gov",
        "sec_edgar",
        "news_rss",
        "alpaca_paper",
        "xai",
        "anthropic",
        "gemini",
    ):
        assert src_name in payload["sources"], f"missing source: {src_name}"
        entry = payload["sources"][src_name]
        assert "status" in entry
        assert entry["status"] in ("ok", "degraded", "error")
        assert "last_checked" in entry
        assert isinstance(entry["latency_ms"], int)
        assert entry["latency_ms"] >= 0

    # Db-state-derived fields populated from the seeded rows.
    assert payload["last_daily_run"] == "2026-04-27T13:00:00.000Z"
    assert payload["last_intraday_run"] == "2026-04-27T18:30:00.000Z"
    assert payload["db_size_bytes"] > 0
    assert payload["paper_account_equity"] == pytest.approx(25000.0)


def test_build_m4_audit_payload_handles_missing_db(audit_module, tmp_path, monkeypatch):
    """All db-derived fields tolerate a missing ``alpha_sniper.db``."""
    _patch_all_probes_ok(monkeypatch)
    payload = audit_module.build_m4_audit_payload(
        db_path=tmp_path / "nonexistent.db"
    )
    assert payload["last_daily_run"] is None
    assert payload["last_intraday_run"] is None
    assert payload["db_size_bytes"] == 0


def test_build_m4_audit_payload_paper_equity_none_on_credential_failure(
    audit_module, tmp_path, monkeypatch
):
    """Equity is ``None`` when alpaca probe degrades."""
    monkeypatch.setattr(
        "biotech_sniper.audit._probe_ctgov", lambda: {"status": "ok"}
    )
    monkeypatch.setattr(
        "biotech_sniper.audit._probe_sec_edgar", lambda: {"status": "ok"}
    )
    monkeypatch.setattr(
        "biotech_sniper.audit._probe_news_rss", lambda: {"status": "ok"}
    )
    monkeypatch.setattr(
        "biotech_sniper.audit._probe_alpaca_paper",
        lambda: {
            "status": "degraded",
            "reason": "credentials_missing",
            "equity": None,
        },
    )
    monkeypatch.setattr(
        "biotech_sniper.audit._probe_xai", lambda: {"status": "ok"}
    )
    monkeypatch.setattr(
        "biotech_sniper.audit._probe_anthropic", lambda: {"status": "ok"}
    )
    monkeypatch.setattr(
        "biotech_sniper.audit._probe_gemini", lambda: {"status": "ok"}
    )
    monkeypatch.setattr(
        "biotech_sniper.audit._M4_PROBES",
        tuple(
            (name, getattr(audit_module, fn_name))
            for name, fn_name in (
                ("ct.gov", "_probe_ctgov"),
                ("sec_edgar", "_probe_sec_edgar"),
                ("news_rss", "_probe_news_rss"),
                ("alpaca_paper", "_probe_alpaca_paper"),
                ("xai", "_probe_xai"),
                ("anthropic", "_probe_anthropic"),
                ("gemini", "_probe_gemini"),
            )
        ),
    )

    payload = audit_module.build_m4_audit_payload(
        db_path=tmp_path / "nonexistent.db"
    )
    assert payload["paper_account_equity"] is None
    assert payload["sources"]["alpaca_paper"]["status"] == "degraded"
    assert payload["sources"]["alpaca_paper"]["reason"] == "credentials_missing"


# ---------------------------------------------------------------------------
# write_audit_latest — integration over the helpers.
# ---------------------------------------------------------------------------


def test_write_audit_latest_round_trip(audit_module, tmp_path, monkeypatch):
    _patch_all_probes_ok(monkeypatch)
    db_path = tmp_path / "alpha_sniper.db"
    _seed_db(db_path)

    audit_path = tmp_path / "state" / "audit_latest.json"

    payload = audit_module.write_audit_latest(audit_path, db_path=db_path)

    # File was written atomically (no .tmp leftovers).
    assert audit_path.is_file()
    leftovers = list(audit_path.parent.glob("audit_latest.json.*.tmp"))
    assert leftovers == []

    # The on-disk contents match what was returned.
    on_disk = json.loads(audit_path.read_text(encoding="utf-8"))
    assert on_disk == payload

    # All M4 contract fields present in the file.
    for key in (
        "generated_at",
        "sources",
        "last_daily_run",
        "last_intraday_run",
        "db_size_bytes",
        "paper_account_equity",
    ):
        assert key in on_disk

    for src in (
        "ct.gov",
        "sec_edgar",
        "news_rss",
        "alpaca_paper",
        "xai",
        "anthropic",
        "gemini",
    ):
        assert src in on_disk["sources"]


def test_write_audit_latest_preserves_legacy_keys(audit_module, tmp_path, monkeypatch):
    """Sibling keys like ``news_ingestion`` survive the M4 overlay."""
    _patch_all_probes_ok(monkeypatch)

    audit_path = tmp_path / "state" / "audit_latest.json"
    audit_path.parent.mkdir(parents=True)
    audit_path.write_text(
        json.dumps(
            {
                "news_ingestion": {"daily_count": 42},
                "as_of_date": "old",
                "sources": {
                    "legacy_extra_key": {"ok": True, "note": "kept"},
                },
            }
        ),
        encoding="utf-8",
    )

    payload = audit_module.write_audit_latest(
        audit_path,
        extra={"as_of_date": "new"},
        db_path=tmp_path / "missing.db",
    )

    # Legacy non-contract key preserved.
    assert payload["news_ingestion"] == {"daily_count": 42}
    # ``extra`` overrides the existing key.
    assert payload["as_of_date"] == "new"
    # Legacy ``sources`` entry preserved alongside the seven canonical ones.
    assert "legacy_extra_key" in payload["sources"]
    # f-m4-12 (VAL-M4-035): legacy entries are normalized to carry a
    # ``status`` field while every other key is preserved verbatim.
    assert payload["sources"]["legacy_extra_key"] == {
        "ok": True,
        "note": "kept",
        "status": "ok",
    }
    # All seven canonical sources still present.
    for src in (
        "ct.gov",
        "sec_edgar",
        "news_rss",
        "alpaca_paper",
        "xai",
        "anthropic",
        "gemini",
    ):
        assert src in payload["sources"]


def test_write_audit_latest_sets_last_daily_run_to_current_utc(
    audit_module, tmp_path, monkeypatch
):
    """f-misc-06: ``write_audit_latest`` overrides ``last_daily_run`` with current UTC.

    The legacy ``build_m4_audit_payload`` derives ``last_daily_run``
    from ``MAX(scoring_cache.created_at)`` which can be stale. The
    new contract: ``write_audit_latest`` stamps ``last_daily_run``
    with the current UTC instant on every write so the watchdog
    reads a fresh "most-recent-completed" marker.
    """
    import datetime as _dt

    _patch_all_probes_ok(monkeypatch)
    db_path = tmp_path / "alpha_sniper.db"
    _seed_db(db_path)  # seeds scoring_cache.created_at = "2026-04-27T13:00:00.000Z"

    audit_path = tmp_path / "state" / "audit_latest.json"
    before = _dt.datetime.now(_dt.timezone.utc)
    payload = audit_module.write_audit_latest(audit_path, db_path=db_path)
    after = _dt.datetime.now(_dt.timezone.utc)

    last_daily_run = payload["last_daily_run"]
    assert isinstance(last_daily_run, str)
    # Must NOT echo the stale db value.
    assert last_daily_run != "2026-04-27T13:00:00.000Z"
    # Must end with ``Z`` (canonical UTC ISO-8601).
    assert last_daily_run.endswith("Z"), last_daily_run
    parsed = _dt.datetime.fromisoformat(last_daily_run.replace("Z", "+00:00"))
    assert before <= parsed <= after, (
        f"last_daily_run {last_daily_run} not within "
        f"[{before.isoformat()}, {after.isoformat()}]"
    )


def test_write_audit_latest_stamps_completed_at_on_summary(
    audit_module, tmp_path, monkeypatch
):
    """f-misc-06: when ``last_daily_run_summary`` is in the payload,
    ``write_audit_latest`` stamps ``completed_at`` on it so the
    scalar ``last_daily_run`` and the summary block agree exactly
    (well under the 1-second parity tolerance).
    """
    _patch_all_probes_ok(monkeypatch)
    audit_path = tmp_path / "state" / "audit_latest.json"
    payload = audit_module.write_audit_latest(
        audit_path,
        extra={
            "last_daily_run_summary": {
                "date": "2026-04-29",
                "duration_sec": 12.5,
                "orders_submitted": 0,
                "cards_generated": 3,
                "llm_cost_usd": 0.42,
                "success": True,
            }
        },
        db_path=tmp_path / "missing.db",
    )

    summary = payload.get("last_daily_run_summary")
    assert isinstance(summary, dict)
    assert "completed_at" in summary, summary
    assert summary["completed_at"] == payload["last_daily_run"], (
        f"completed_at ({summary['completed_at']}) must equal "
        f"last_daily_run ({payload['last_daily_run']})"
    )


def test_write_audit_latest_handles_corrupt_existing_json(
    audit_module, tmp_path, monkeypatch
):
    """A pre-existing un-parseable JSON file is treated as if it were absent."""
    _patch_all_probes_ok(monkeypatch)
    audit_path = tmp_path / "state" / "audit_latest.json"
    audit_path.parent.mkdir(parents=True)
    audit_path.write_text("not-json", encoding="utf-8")

    payload = audit_module.write_audit_latest(
        audit_path, db_path=tmp_path / "missing.db"
    )

    on_disk = json.loads(audit_path.read_text(encoding="utf-8"))
    assert on_disk == payload
    assert "generated_at" in payload


# ---------------------------------------------------------------------------
# f-m4-02a — print()-to-logger cleanup + hermetic import contract
# ---------------------------------------------------------------------------


def test_audit_module_active_code_has_no_print_calls():
    """Active code (everything outside ``if __name__ == '__main__':``) emits zero ``print()`` calls.

    The legacy probe block is wrapped in ``if __name__ == '__main__':``
    per f-m4-02a so importing :mod:`biotech_sniper.audit` is hermetic
    and the daily ``python -m biotech_sniper.audit`` run emits
    structured JSON via the project's logging_setup formatter (not bare
    stdout). This test parses the audit module's AST and asserts every
    surviving ``print(...)`` call lives inside the ``__main__`` block.
    """
    import ast
    import biotech_sniper.audit as audit

    source = Path(audit.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Locate the ``if __name__ == '__main__':`` block. Anything
    # textually inside that block is allowed to keep ``print``; every
    # other ``print`` call is a regression.
    main_block_lines: set[int] = set()
    for node in tree.body:
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
        ):
            for sub in ast.walk(node):
                if hasattr(sub, "lineno"):
                    main_block_lines.add(sub.lineno)

    assert main_block_lines, "no ``if __name__ == '__main__':`` block found"

    offending: list[tuple[int, str]] = []
    for sub in ast.walk(tree):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "print"
        ):
            if sub.lineno in main_block_lines:
                continue  # acceptable: inside the legacy CLI block
            offending.append((sub.lineno, ast.unparse(sub)))

    assert offending == [], f"unexpected print() calls in active code: {offending}"


def test_write_audit_latest_import_is_fast_and_makes_no_network_calls():
    """``from biotech_sniper.audit import write_audit_latest`` must be hermetic.

    Per the f-m4-02a feature contract: a fresh import of the audit
    module must (a) complete in well under 100 ms and (b) issue zero
    ``requests.get`` / ``requests.post`` calls. The legacy probe block
    has been wrapped in ``if __name__ == '__main__':`` so importing the
    module no longer triggers the live HTTP probes that used to run at
    module-import time.
    """
    import importlib
    import time

    import requests

    get_calls: list = []
    post_calls: list = []

    real_get = requests.get
    real_post = requests.post

    def _trip_get(*args, **kwargs):  # pragma: no cover — failure path
        get_calls.append(args)
        raise RuntimeError("network call detected during import")

    def _trip_post(*args, **kwargs):  # pragma: no cover — failure path
        post_calls.append(args)
        raise RuntimeError("network call detected during import")

    requests.get = _trip_get  # type: ignore[assignment]
    requests.post = _trip_post  # type: ignore[assignment]
    try:
        # Force a fresh import so we observe the side effects (or
        # lack thereof) on a cold module load.
        sys.modules.pop("biotech_sniper.audit", None)
        started = time.monotonic()
        module = importlib.import_module("biotech_sniper.audit")
        elapsed_ms = (time.monotonic() - started) * 1000.0
        assert hasattr(module, "write_audit_latest")
    finally:
        requests.get = real_get  # type: ignore[assignment]
        requests.post = real_post  # type: ignore[assignment]

    assert get_calls == [], f"requests.get called during import: {get_calls}"
    assert post_calls == [], f"requests.post called during import: {post_calls}"
    # 100 ms cap per the feature description; we leave generous headroom
    # for slow CI hosts but a regression that re-introduces the network
    # probes will easily blow this budget (each probe times out at 10 s).
    assert elapsed_ms < 100.0, f"import too slow: {elapsed_ms:.1f}ms"


def test_import_biotech_sniper_audit_does_not_call_requests():
    """f-misc-06: ``import biotech_sniper.audit`` must not invoke ``requests.get``/``post``.

    Focused regression test for the hermetic-import contract:
    the legacy module-level probe block (steps 1-19, including the
    Defense.gov probe and the company_ticker_map probe) is gated
    behind ``if __name__ == '__main__':``. Plain
    ``import biotech_sniper.audit`` must therefore reach zero
    HTTP-transport call sites — neither ``requests.get`` nor
    ``requests.post``. This complements the timing-budget test above
    by isolating the network-side-effect invariant.
    """
    import importlib

    import requests

    get_calls: list = []
    post_calls: list = []

    real_get = requests.get
    real_post = requests.post

    def _record_get(*args, **kwargs):  # pragma: no cover — failure path
        get_calls.append((args, kwargs))
        raise RuntimeError("requests.get called during import")

    def _record_post(*args, **kwargs):  # pragma: no cover — failure path
        post_calls.append((args, kwargs))
        raise RuntimeError("requests.post called during import")

    requests.get = _record_get  # type: ignore[assignment]
    requests.post = _record_post  # type: ignore[assignment]
    try:
        sys.modules.pop("biotech_sniper.audit", None)
        importlib.import_module("biotech_sniper.audit")
    finally:
        requests.get = real_get  # type: ignore[assignment]
        requests.post = real_post  # type: ignore[assignment]

    assert get_calls == [], (
        f"import biotech_sniper.audit must not call requests.get; "
        f"saw: {get_calls}"
    )
    assert post_calls == [], (
        f"import biotech_sniper.audit must not call requests.post; "
        f"saw: {post_calls}"
    )


# ---------------------------------------------------------------------------
# f-m4-12 — VAL-M4-035 status normalization regression tests.
# ---------------------------------------------------------------------------
#
# The legacy module-level audit script populates ``sources`` with
# entries shaped ``{'ok': bool, ...}`` (e.g. ``clinicaltrials_gov``,
# ``alpha_sniper_db``, ``scoring_cache``, ``llm_cost_ledger``,
# ``news_events``). VAL-M4-035 requires every entry in the merged
# ``sources`` map to carry a ``status`` field whose value is one of
# {ok, degraded, error, unknown, missing_credential}. The tests
# below pin that contract.


_M4_STATUS_ENUM_TUPLE = ("ok", "degraded", "error", "unknown", "missing_credential")


def test_normalize_legacy_source_entry_maps_ok_true(audit_module):
    """Legacy ``{'ok': True, ...}`` → ``status='ok'`` with all keys preserved."""
    out = audit_module._normalize_legacy_source_entry(
        {"ok": True, "count": 5, "extra": "kept"}
    )
    assert out["status"] == "ok"
    assert out["ok"] is True
    assert out["count"] == 5
    assert out["extra"] == "kept"


def test_normalize_legacy_source_entry_maps_ok_false(audit_module):
    """Legacy ``{'ok': False, 'error': ...}`` → ``status='error'`` preserved."""
    out = audit_module._normalize_legacy_source_entry(
        {"ok": False, "error": "timeout"}
    )
    assert out["status"] == "error"
    assert out["ok"] is False
    assert out["error"] == "timeout"


def test_normalize_legacy_source_entry_keeps_existing_valid_status(audit_module):
    """Entry already carrying a contract-valid ``status`` is returned untouched."""
    entry = {"status": "degraded", "reason": "credentials_missing"}
    out = audit_module._normalize_legacy_source_entry(entry)
    assert out is entry  # preserves identity when already valid
    assert out["status"] == "degraded"


def test_normalize_legacy_source_entry_unknown_when_no_ok_or_status(audit_module):
    """Entry without ``ok`` or recognised ``status`` falls back to ``unknown``."""
    out = audit_module._normalize_legacy_source_entry({"foo": 1})
    assert out["status"] == "unknown"
    assert out["foo"] == 1


def test_normalize_legacy_source_entry_overrides_invalid_status(audit_module):
    """A non-enum ``status`` value is replaced via the ``ok`` mapping."""
    out = audit_module._normalize_legacy_source_entry(
        {"status": "weird-value", "ok": True}
    )
    assert out["status"] == "ok"


def test_write_audit_latest_normalizes_legacy_ok_true_entry(
    audit_module, tmp_path, monkeypatch
):
    """``payload.sources`` legacy ``{'ok': True, 'count': 5}`` becomes ``status='ok'``.

    Pinned by VAL-M4-035: every value in ``audit_latest.json``'s
    ``sources`` map must carry a ``status`` field. The legacy entry's
    other keys (``count`` here) must be preserved verbatim.
    """
    _patch_all_probes_ok(monkeypatch)
    audit_path = tmp_path / "state" / "audit_latest.json"
    audit_path.parent.mkdir(parents=True)
    audit_path.write_text(
        json.dumps(
            {
                "sources": {
                    "legacy_ok": {"ok": True, "count": 5},
                }
            }
        ),
        encoding="utf-8",
    )

    payload = audit_module.write_audit_latest(
        audit_path, db_path=tmp_path / "missing.db"
    )

    legacy = payload["sources"]["legacy_ok"]
    assert legacy["status"] == "ok"
    assert legacy["count"] == 5
    assert legacy["ok"] is True


def test_write_audit_latest_normalizes_legacy_ok_false_entry(
    audit_module, tmp_path, monkeypatch
):
    """Legacy ``{'ok': False, 'error': 'timeout'}`` becomes ``status='error'`` with error preserved."""
    _patch_all_probes_ok(monkeypatch)
    audit_path = tmp_path / "state" / "audit_latest.json"
    audit_path.parent.mkdir(parents=True)
    audit_path.write_text(
        json.dumps(
            {
                "sources": {
                    "legacy_err": {"ok": False, "error": "timeout"},
                }
            }
        ),
        encoding="utf-8",
    )

    payload = audit_module.write_audit_latest(
        audit_path, db_path=tmp_path / "missing.db"
    )

    legacy = payload["sources"]["legacy_err"]
    assert legacy["status"] == "error"
    assert legacy["error"] == "timeout"
    assert legacy["ok"] is False


def test_write_audit_latest_every_source_has_valid_status(
    audit_module, tmp_path, monkeypatch
):
    """Comprehensive VAL-M4-035 contract check across legacy + canonical entries.

    Seeds the on-disk file with a representative slice of the legacy
    ``sources`` shapes the live ``__main__`` block emits (``ok=True``,
    ``ok=False``, no ``ok`` key) and asserts every merged value
    carries a contract-valid ``status``. Also confirms the seven
    canonical M4 sources retain their probe-derived ``status`` rather
    than being clobbered.
    """
    _patch_all_probes_ok(monkeypatch)
    audit_path = tmp_path / "state" / "audit_latest.json"
    audit_path.parent.mkdir(parents=True)
    audit_path.write_text(
        json.dumps(
            {
                "sources": {
                    "clinicaltrials_gov": {"ok": True, "studies": 5},
                    "alpha_sniper_db": {
                        "ok": False,
                        "error": "missing",
                        "present": False,
                    },
                    "scoring_cache": {"ok": True, "rows_total": 12},
                    "llm_cost_ledger": {"ok": True, "rows_total": 8},
                    "news_events": {"ok": False, "reason": "db_missing"},
                    "weird_no_ok": {"unrelated_field": 1},
                }
            }
        ),
        encoding="utf-8",
    )

    payload = audit_module.write_audit_latest(
        audit_path, db_path=tmp_path / "missing.db"
    )

    sources = payload["sources"]

    # Every entry has a status field with a value in the canonical enum.
    for name, value in sources.items():
        assert isinstance(value, dict), f"{name}: non-dict value {value!r}"
        assert "status" in value, f"{name} missing status: {value!r}"
        assert value["status"] in _M4_STATUS_ENUM_TUPLE, (
            f"{name} bad status: {value['status']!r}"
        )

    # Legacy entries got the right enum mapping with original keys preserved.
    assert sources["clinicaltrials_gov"]["status"] == "ok"
    assert sources["clinicaltrials_gov"]["studies"] == 5
    assert sources["alpha_sniper_db"]["status"] == "error"
    assert sources["alpha_sniper_db"]["error"] == "missing"
    assert sources["alpha_sniper_db"]["present"] is False
    assert sources["news_events"]["status"] == "error"
    assert sources["news_events"]["reason"] == "db_missing"
    assert sources["weird_no_ok"]["status"] == "unknown"
    assert sources["weird_no_ok"]["unrelated_field"] == 1

    # The seven M4 canonical sources keep their original probe-derived status
    # (every probe was patched to return ``status='ok'``). They must not be
    # overwritten by the legacy normalizer.
    for canonical in (
        "ct.gov",
        "sec_edgar",
        "news_rss",
        "alpaca_paper",
        "xai",
        "anthropic",
        "gemini",
    ):
        assert canonical in sources
        assert sources[canonical]["status"] == "ok"
