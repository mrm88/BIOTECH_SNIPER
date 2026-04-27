"""Tests for :mod:`biotech_sniper.llm.cost_report` (f-m2-06).

Covers:

* The ``llm_cost_ledger`` integrity rules — every row must carry
  non-null ``model_id``, ``prompt_tokens``, ``completion_tokens``,
  ``latency_ms``, ``cost_usd``, ``called_at``.
* The ``daily_totals`` SQL grouping (VAL-M2-051) returns one row per
  ``(day, provider)`` with correct sums.
* Plausibility bounds: recorded ``cost_usd`` must sit within ±20 % of
  the published-rate estimate for the row's ``model_id``.
* The CLI entrypoint ``python -m biotech_sniper.llm.cost_report``
  rejects malformed dates, prints a tabular report, and exits non-
  zero when ``--strict`` / ``--check-plausibility`` find regressions.

All tests run against a temp SQLite db (``tmp_path / 'ledger.db'``)
so they exercise the real schema + PRAGMAs without touching the
project database. No live network or LLM calls are made.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

import pytest

from biotech_sniper import db
from biotech_sniper.llm import cost_report
from biotech_sniper.llm.cost_report import (
    DEFAULT_TOLERANCE,
    MODEL_PRICING,
    REQUIRED_FIELDS,
    daily_totals,
    expected_cost_usd,
    find_implausible_rows,
    find_rows_with_null_fields,
    format_table,
    lookup_pricing,
    main,
    plausibility_check,
)


# ---------------------------------------------------------------------------
# Helpers
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
    """Insert raw rows into ``llm_cost_ledger``.

    Caller controls every column (including nulls) so we can simulate
    legacy / corrupted data without going through the per-client
    helpers.
    """

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
                        row.get("provider"),
                        row.get("model_id"),
                        row.get("purpose", "test"),
                        row.get("prompt_tokens"),
                        row.get("completion_tokens"),
                        row.get("latency_ms"),
                        row.get("cost_usd"),
                        row.get("request_id", "req-test"),
                        row.get("called_at"),
                    ),
                )
    finally:
        conn.close()


def _good_row(
    *,
    provider: str = "xai",
    model_id: str = "grok-4-0709",
    prompt_tokens: int = 1000,
    completion_tokens: int = 200,
    latency_ms: int = 1500,
    called_at: str = "2026-04-25T10:00:00.000Z",
    cost_usd: float | None = None,
) -> dict[str, Any]:
    """Return a well-formed row with cost computed from the rate table."""

    if cost_usd is None:
        cost_usd = expected_cost_usd(
            provider, model_id, prompt_tokens, completion_tokens
        )
        assert cost_usd is not None
    return {
        "provider": provider,
        "model_id": model_id,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "latency_ms": latency_ms,
        "cost_usd": cost_usd,
        "called_at": called_at,
    }


# ---------------------------------------------------------------------------
# Required-field integrity
# ---------------------------------------------------------------------------


def test_required_fields_constant_matches_feature_spec():
    """The required-field set tracks the feature description exactly."""

    assert set(REQUIRED_FIELDS) == {
        "model_id",
        "prompt_tokens",
        "completion_tokens",
        "latency_ms",
        "cost_usd",
        "called_at",
    }


def test_no_null_required_fields_when_rows_well_formed(tmp_path):
    db_path = _make_db(tmp_path)
    _insert_rows(
        db_path,
        [
            _good_row(provider="xai", model_id="grok-4"),
            _good_row(provider="anthropic", model_id="claude-opus-4-1-20250805"),
            _good_row(provider="gemini", model_id="gemini-2.5-pro"),
        ],
    )
    conn = db.connect(db_path)
    try:
        violations = find_rows_with_null_fields(conn)
    finally:
        conn.close()
    assert violations == []


@pytest.mark.parametrize(
    "field, value",
    [
        # ``model_id`` is enforced NOT NULL by the schema, but the
        # NOT NULL constraint does not catch the empty string. The
        # report layer treats an empty model id as a violation.
        ("model_id", ""),
        # Token counts, latency and cost are not constrained at the
        # schema level (legacy rows from earlier-mission ledgers may
        # be sparse) — the report layer is the only line of defence.
        ("prompt_tokens", None),
        ("completion_tokens", None),
        ("latency_ms", None),
        ("cost_usd", None),
    ],
)
def test_null_required_fields_are_flagged(tmp_path, field, value):
    db_path = _make_db(tmp_path)
    bad = _good_row()
    bad[field] = value
    _insert_rows(db_path, [bad])

    conn = db.connect(db_path)
    try:
        violations = find_rows_with_null_fields(conn)
    finally:
        conn.close()
    assert len(violations) == 1, violations


def test_schema_enforces_not_null_on_model_id(tmp_path):
    """Defence-in-depth: the schema rejects NULL ``model_id``."""

    db_path = _make_db(tmp_path)
    conn = db.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(
                    "INSERT INTO llm_cost_ledger (provider, model_id, "
                    "prompt_tokens, completion_tokens, latency_ms, cost_usd) "
                    "VALUES ('xai', NULL, 100, 50, 1000, 0.001)"
                )
    finally:
        conn.close()


def test_schema_enforces_not_null_on_called_at(tmp_path):
    """Defence-in-depth: the schema rejects NULL ``called_at``."""

    db_path = _make_db(tmp_path)
    conn = db.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(
                    "INSERT INTO llm_cost_ledger (provider, model_id, "
                    "prompt_tokens, completion_tokens, latency_ms, "
                    "cost_usd, called_at) "
                    "VALUES ('xai', 'grok-4', 100, 50, 1000, 0.001, NULL)"
                )
    finally:
        conn.close()


def test_called_at_default_keeps_row_well_formed(tmp_path):
    """Rows inserted without called_at still pass the integrity check."""

    db_path = _make_db(tmp_path)
    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO llm_cost_ledger (provider, model_id, "
                "prompt_tokens, completion_tokens, latency_ms, cost_usd) "
                "VALUES ('xai', 'grok-4', 1000, 200, 1500, 0.008)"
            )
        violations = find_rows_with_null_fields(conn)
    finally:
        conn.close()
    assert violations == []


# ---------------------------------------------------------------------------
# Daily totals
# ---------------------------------------------------------------------------


def test_daily_totals_groups_by_day_and_provider(tmp_path):
    db_path = _make_db(tmp_path)
    _insert_rows(
        db_path,
        [
            # Day 1: 2 xai calls, 1 anthropic.
            _good_row(provider="xai", called_at="2026-04-25T08:00:00.000Z"),
            _good_row(provider="xai", called_at="2026-04-25T09:00:00.000Z"),
            _good_row(
                provider="anthropic",
                model_id="claude-opus-4-1-20250805",
                called_at="2026-04-25T10:00:00.000Z",
            ),
            # Day 2: 1 gemini call.
            _good_row(
                provider="gemini",
                model_id="gemini-2.5-pro",
                called_at="2026-04-26T07:00:00.000Z",
            ),
        ],
    )
    conn = db.connect(db_path)
    try:
        rows = daily_totals(conn, "2026-04-25")
    finally:
        conn.close()

    # Sorted DESC by day, then provider ASC.
    assert [(r["day"], r["provider"]) for r in rows] == [
        ("2026-04-26", "gemini"),
        ("2026-04-25", "anthropic"),
        ("2026-04-25", "xai"),
    ]
    by_key = {(r["day"], r["provider"]): r for r in rows}
    assert by_key[("2026-04-25", "xai")]["calls"] == 2
    assert by_key[("2026-04-25", "anthropic")]["calls"] == 1
    assert by_key[("2026-04-26", "gemini")]["calls"] == 1
    # Token sums roll up correctly.
    assert by_key[("2026-04-25", "xai")]["prompt_tokens"] == 2000
    assert by_key[("2026-04-25", "xai")]["completion_tokens"] == 400


def test_daily_totals_filters_by_since(tmp_path):
    db_path = _make_db(tmp_path)
    _insert_rows(
        db_path,
        [
            _good_row(provider="xai", called_at="2026-04-20T08:00:00.000Z"),
            _good_row(provider="xai", called_at="2026-04-25T09:00:00.000Z"),
        ],
    )
    conn = db.connect(db_path)
    try:
        rows = daily_totals(conn, "2026-04-25")
    finally:
        conn.close()
    assert [r["day"] for r in rows] == ["2026-04-25"]


def test_daily_totals_empty_db_returns_empty_list(tmp_path):
    db_path = _make_db(tmp_path)
    conn = db.connect(db_path)
    try:
        assert daily_totals(conn, "2026-04-01") == []
    finally:
        conn.close()


def test_daily_totals_cost_sum_is_non_negative(tmp_path):
    """Validates VAL-M2-053 sanity: SUM(cost_usd) is plausible."""

    db_path = _make_db(tmp_path)
    _insert_rows(
        db_path,
        [
            _good_row(provider="xai", called_at="2026-04-25T08:00:00.000Z"),
            _good_row(
                provider="anthropic",
                model_id="claude-opus-4-1-20250805",
                called_at="2026-04-25T09:00:00.000Z",
            ),
        ],
    )
    conn = db.connect(db_path)
    try:
        for row in daily_totals(conn, "2026-04-25"):
            assert row["cost_usd"] >= 0
            assert row["cost_usd"] < 5.0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Plausibility bounds
# ---------------------------------------------------------------------------


def test_lookup_pricing_resolves_known_models():
    rates = lookup_pricing("xai", "grok-4-0709")
    assert rates is not None
    assert rates.input_per_1k == pytest.approx(0.005)

    rates = lookup_pricing("anthropic", "claude-opus-4-1-20250805")
    assert rates is not None
    assert rates.output_per_1k == pytest.approx(0.075)

    rates = lookup_pricing("gemini", "gemini-2.5-pro-preview")
    assert rates is not None
    assert rates.input_per_1k == pytest.approx(0.00125)


def test_lookup_pricing_unknown_returns_none():
    assert lookup_pricing("openai", "gpt-4o") is None
    assert lookup_pricing("xai", "totally-unknown-model") is None
    assert lookup_pricing("xai", "") is None


def test_expected_cost_uses_published_rates():
    cost = expected_cost_usd("xai", "grok-4", 1000, 500)
    # 1000 input @ $5/1M = $0.005 ; 500 output @ $15/1M = $0.0075.
    assert cost == pytest.approx(0.0125)

    cost = expected_cost_usd(
        "anthropic", "claude-opus-4-1-20250805", 4000, 1500
    )
    # $0.060 (input) + $0.1125 (output) = $0.1725.
    assert cost == pytest.approx(0.1725)


@pytest.mark.parametrize(
    "deviation, ok",
    [
        (0.0, True),
        (0.10, True),
        (0.20, True),
        (-0.20, True),
        (0.21, False),
        (-0.30, False),
    ],
)
def test_plausibility_within_published_rate_bounds(deviation, ok):
    expected = expected_cost_usd("xai", "grok-4", 1000, 200)
    assert expected is not None
    cost_usd = expected * (1 + deviation)
    result = plausibility_check(
        provider="xai",
        model_id="grok-4",
        prompt_tokens=1000,
        completion_tokens=200,
        cost_usd=cost_usd,
    )
    assert result.ok is ok, result


def test_plausibility_zero_tokens_is_ok_when_cost_non_negative():
    result = plausibility_check(
        provider="xai",
        model_id="grok-4",
        prompt_tokens=0,
        completion_tokens=0,
        cost_usd=0.0,
    )
    assert result.ok is True
    assert result.reason == "zero-tokens"


def test_plausibility_negative_cost_always_fails():
    result = plausibility_check(
        provider="xai",
        model_id="grok-4",
        prompt_tokens=1000,
        completion_tokens=200,
        cost_usd=-0.01,
    )
    assert result.ok is False
    assert result.reason == "negative-cost"


def test_plausibility_unknown_model_passes_with_reason():
    result = plausibility_check(
        provider="xai",
        model_id="experimental-future-model",
        prompt_tokens=1000,
        completion_tokens=200,
        cost_usd=0.005,
    )
    assert result.ok is True
    assert result.reason == "unknown-pricing"


def test_find_implausible_rows_flags_outliers(tmp_path):
    db_path = _make_db(tmp_path)
    expected = expected_cost_usd("xai", "grok-4", 1000, 200)
    assert expected is not None
    _insert_rows(
        db_path,
        [
            # Within tolerance: ok.
            _good_row(provider="xai", model_id="grok-4"),
            # 5x cost: must trigger.
            _good_row(
                provider="xai",
                model_id="grok-4",
                cost_usd=expected * 5,
                called_at="2026-04-26T08:00:00.000Z",
            ),
        ],
    )
    conn = db.connect(db_path)
    try:
        bad = find_implausible_rows(conn, since="2026-04-25")
    finally:
        conn.close()
    assert len(bad) == 1
    assert bad[0].reason == "outside-tolerance"
    assert bad[0].deviation_pct > DEFAULT_TOLERANCE


def test_find_implausible_rows_clean_table_returns_empty(tmp_path):
    db_path = _make_db(tmp_path)
    _insert_rows(
        db_path,
        [
            _good_row(provider="xai", model_id="grok-4"),
            _good_row(provider="anthropic", model_id="claude-opus-4-1-20250805"),
            _good_row(provider="gemini", model_id="gemini-2.5-pro"),
        ],
    )
    conn = db.connect(db_path)
    try:
        bad = find_implausible_rows(conn, since="2026-04-25")
    finally:
        conn.close()
    assert bad == []


# ---------------------------------------------------------------------------
# Pricing-table consistency with per-client constants
# ---------------------------------------------------------------------------


def test_pricing_table_matches_xai_constants():
    from biotech_sniper.llm import xai_client

    rates = lookup_pricing("xai", xai_client.DEFAULT_MODEL)
    assert rates is not None
    assert rates.input_per_1k == pytest.approx(xai_client.GROK_4_INPUT_USD_PER_1K)
    assert rates.output_per_1k == pytest.approx(xai_client.GROK_4_OUTPUT_USD_PER_1K)


def test_pricing_table_matches_claude_constants():
    from biotech_sniper.llm import claude_client

    rates = lookup_pricing("anthropic", claude_client.DEFAULT_MODEL)
    assert rates is not None
    assert rates.input_per_1k == pytest.approx(
        claude_client.CLAUDE_INPUT_USD_PER_1K
    )
    assert rates.output_per_1k == pytest.approx(
        claude_client.CLAUDE_OUTPUT_USD_PER_1K
    )


def test_pricing_table_covers_each_provider():
    assert set(MODEL_PRICING) == {"xai", "anthropic", "gemini"}


# ---------------------------------------------------------------------------
# Tabular formatting
# ---------------------------------------------------------------------------


def test_format_table_includes_header_and_total():
    rows = [
        {
            "day": "2026-04-25",
            "provider": "xai",
            "calls": 2,
            "prompt_tokens": 2000,
            "completion_tokens": 400,
            "latency_ms": 3000,
            "cost_usd": 0.0260,
        },
        {
            "day": "2026-04-25",
            "provider": "anthropic",
            "calls": 1,
            "prompt_tokens": 4000,
            "completion_tokens": 1500,
            "latency_ms": 5000,
            "cost_usd": 0.1725,
        },
    ]
    text = format_table(rows)
    lines = text.splitlines()
    assert lines[0].split()[0] == "date"
    # Separator line under header.
    assert set(lines[1].replace("  ", "").replace(" ", "")) == {"-"}
    # Body rows + total.
    assert any(line.startswith("TOTAL") for line in lines)
    assert any("0.1725" in line for line in lines)


def test_format_table_empty_rows_still_emits_header():
    text = format_table([])
    lines = text.splitlines()
    assert lines[0].split()[0] == "date"
    # No body rows means just header + separator.
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_rejects_malformed_dates(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--since", "2026/04/25", "--db", str(tmp_path / "noop.db")])
    assert excinfo.value.code != 0


def test_cli_exits_nonzero_when_db_missing(tmp_path, capsys):
    code = main(["--since", "2026-04-01", "--db", str(tmp_path / "missing.db")])
    captured = capsys.readouterr()
    assert code == 2
    assert "not found" in captured.err.lower()


def test_cli_prints_table_and_exits_zero_when_clean(tmp_path, capsys):
    db_path = _make_db(tmp_path)
    _insert_rows(
        db_path,
        [
            _good_row(provider="xai", model_id="grok-4"),
            _good_row(
                provider="anthropic",
                model_id="claude-opus-4-1-20250805",
            ),
        ],
    )
    code = main([
        "--since",
        "2026-04-01",
        "--db",
        str(db_path),
        "--check-plausibility",
        "--strict",
    ])
    captured = capsys.readouterr()
    assert code == 0, captured
    assert "date" in captured.out
    assert "xai" in captured.out
    assert "anthropic" in captured.out
    assert "TOTAL" in captured.out


def test_cli_strict_flag_fails_on_null_required_fields(tmp_path, capsys):
    db_path = _make_db(tmp_path)
    bad = _good_row()
    bad["prompt_tokens"] = None
    _insert_rows(db_path, [bad])
    code = main([
        "--since",
        "2026-04-01",
        "--db",
        str(db_path),
        "--strict",
    ])
    captured = capsys.readouterr()
    assert code == 4, captured
    assert "null required fields" in captured.out


def test_cli_check_plausibility_flag_fails_on_outliers(tmp_path, capsys):
    db_path = _make_db(tmp_path)
    expected = expected_cost_usd("xai", "grok-4", 1000, 200)
    assert expected is not None
    _insert_rows(
        db_path,
        [
            _good_row(
                provider="xai",
                model_id="grok-4",
                cost_usd=expected * 10,
            ),
        ],
    )
    code = main([
        "--since",
        "2026-04-01",
        "--db",
        str(db_path),
        "--check-plausibility",
    ])
    captured = capsys.readouterr()
    assert code == 5, captured
    assert "implausible" in captured.out


def test_cli_json_output_emits_parseable_json(tmp_path, capsys):
    db_path = _make_db(tmp_path)
    _insert_rows(
        db_path,
        [
            _good_row(provider="xai", model_id="grok-4"),
        ],
    )
    code = main([
        "--since",
        "2026-04-01",
        "--db",
        str(db_path),
        "--json",
    ])
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["since"] == "2026-04-01"
    assert payload["totals"][0]["provider"] == "xai"
    assert payload["totals"][0]["calls"] == 1


# ---------------------------------------------------------------------------
# Subprocess / module-as-script invocation
# ---------------------------------------------------------------------------


def test_module_runnable_via_python_dash_m(tmp_path):
    """Smoke: invoking the module as a script via -m succeeds."""

    db_path = _make_db(tmp_path)
    _insert_rows(
        db_path,
        [
            _good_row(provider="xai", model_id="grok-4"),
        ],
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "biotech_sniper.llm.cost_report",
            "--since",
            "2026-04-01",
            "--db",
            str(db_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "xai" in completed.stdout
    assert "TOTAL" in completed.stdout


# ---------------------------------------------------------------------------
# End-to-end integration: writes from the real clients land where the
# report can read them.
# ---------------------------------------------------------------------------


def test_xai_client_cost_row_shows_up_in_daily_totals(tmp_path, monkeypatch):
    """Round-trip: a successful score_ticker call lands in daily_totals."""

    from biotech_sniper.llm import xai_client

    db_path = _make_db(tmp_path)

    class _StubResponse:
        status_code = 200
        text = "{}"

        def json(self):  # noqa: D401 - simple stub
            return {
                "id": "req-stub",
                "model": "grok-4-0709",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({
                                "probability": 0.42,
                                "rationale": "stub",
                                "confidence": 0.7,
                            })
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1200, "completion_tokens": 250},
            }

    class _StubSession:
        def post(self, *_a, **_kw):
            return _StubResponse()

    client = xai_client.XAIClient(
        api_key="test-key",
        db_path=db_path,
        session=_StubSession(),
    )
    out = client.score_ticker("ABCD", {"catalyst_date": "2026-05-01"})
    assert out["model_id"] == "grok-4-0709"
    assert out["cost_usd"] > 0

    conn = db.connect(db_path)
    try:
        violations = find_rows_with_null_fields(conn)
        rows = daily_totals(conn, "2000-01-01")
    finally:
        conn.close()
    assert violations == []
    assert any(r["provider"] == "xai" and r["calls"] == 1 for r in rows)
