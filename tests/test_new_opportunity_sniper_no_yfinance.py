"""Regression test for f-m3-13 new_opportunity_sniper yfinance fallout.

After f-m3-02 dropped the yfinance branch from
``biotech_sniper.new_opportunity_sniper.validate_ticker``, the
helper now returns ``price=None`` whenever it would have previously
populated a vendor-backed quote. The downstream
``score_and_price_candidate`` flow then passed that ``None`` into
``get_best_option`` which performed ``price * 1.8`` /
``price * 0.35`` arithmetic, raising ``TypeError``. The error was
silently swallowed by an outer broad-except that printed and
returned ``None`` — quietly rejecting otherwise-valid candidates.

f-m3-13 makes the guard explicit:

* ``score_and_price_candidate`` resolves the underlying price via
  ``_resolve_underlying_price`` (best-effort Alpaca latest-trade)
  before invoking the options scorer, and skips the candidate with
  a WARN log line when no positive price is available.
* ``get_best_option`` validates ``price`` upfront and refuses to
  multiply ``None``; the outer ``except`` is narrowed to
  ``(ImportError, ValueError, KeyError)`` so a future ``TypeError``
  would propagate instead of being masked.

This test pins both halves of that contract.
"""

from __future__ import annotations

import importlib
from typing import Any, Optional

import pytest


# ---------------------------------------------------------------------------
# get_best_option: explicit price=None guard, no TypeError swallowed
# ---------------------------------------------------------------------------


def test_get_best_option_returns_none_on_price_none(monkeypatch, capsys):
    """``get_best_option`` returns ``None`` cleanly when price is None.

    Pre-f-m3-13 this would have raised ``TypeError`` inside the
    function (``price * 1.8``) and the broad-except would have
    swallowed it, leaking a false-negative rejection. Post-f-m3-13
    the function refuses up front with a ``[sniper] get_best_option
    ... invalid price=None`` log line and returns ``None`` without
    ever touching the chain.
    """
    sniper = importlib.import_module("biotech_sniper.new_opportunity_sniper")

    # Sentinel: pull_chain MUST NOT be called when price is None.
    def _exploder(*_a, **_kw):
        raise AssertionError(
            "pull_chain must not be invoked when price is None — the "
            "guard should short-circuit before any chain pull"
        )

    from biotech_sniper.options_chains import pull_options as pull_options_mod
    monkeypatch.setattr(pull_options_mod, "pull_chain", _exploder)

    result = sniper.get_best_option("AAPL", "2026-06-20", "LONG_CALLS", None)
    assert result is None
    captured = capsys.readouterr()
    assert "invalid price" in captured.out, (
        "get_best_option must log a clear 'invalid price' diagnostic "
        "when price is None — got: " + repr(captured.out)
    )


def test_get_best_option_does_not_swallow_type_errors(monkeypatch):
    """If pull_chain returns a row that triggers a TypeError, it propagates.

    The narrowed except block in ``get_best_option`` catches only
    ``(ImportError, ValueError, KeyError)``. A ``TypeError`` from a
    malformed row must surface to the caller so silent rejection of
    valid candidates can never recur.
    """
    sniper = importlib.import_module("biotech_sniper.new_opportunity_sniper")

    # Inject a row whose ``ask`` is a non-numeric, non-coercible
    # placeholder. ``float("notanumber")`` raises ``ValueError`` which
    # IS in the narrow tuple, so we instead simulate a chain-pull
    # itself raising ``TypeError`` to assert it is not masked.
    def _bad_pull(*_a, **_kw):
        raise TypeError("simulated downstream type error")

    from biotech_sniper.options_chains import pull_options as pull_options_mod
    monkeypatch.setattr(pull_options_mod, "pull_chain", _bad_pull)

    with pytest.raises(TypeError):
        sniper.get_best_option("AAPL", "2026-06-20", "LONG_CALLS", 100.0)


# ---------------------------------------------------------------------------
# score_and_price_candidate: price=None path is fetched-or-skipped
# ---------------------------------------------------------------------------


def _make_validate_ticker_stub(
    *,
    valid: bool,
    price: Optional[float],
    expirations: Optional[list[str]] = None,
) -> Any:
    """Build a stub ``validate_ticker`` function returning a known dict."""

    def _stub(_ticker: str) -> dict:
        return {
            "valid": valid,
            "price": price,
            "has_options": valid,
            "expirations": expirations or ["2026-06-20", "2026-07-18", "2026-08-15"],
            "mktcap": None,
        }

    return _stub


def test_score_and_price_candidate_skips_with_warn_when_price_unavailable(
    monkeypatch, capsys
):
    """When validate_ticker returns price=None and Alpaca fallback fails,
    the candidate is skipped with a WARN log — never a swallowed TypeError.
    """
    sniper = importlib.import_module("biotech_sniper.new_opportunity_sniper")

    monkeypatch.setattr(
        sniper,
        "validate_ticker",
        _make_validate_ticker_stub(valid=True, price=None),
    )
    # Force the Alpaca fallback to return None as well — this is the
    # realistic CI / no-creds path.
    monkeypatch.setattr(sniper, "_resolve_underlying_price", lambda _t: None)

    # Sentinel: get_best_option must NOT be reached when price is None.
    def _exploder(*_a, **_kw):
        raise AssertionError(
            "get_best_option must not be invoked when price resolution "
            "fails — score_and_price_candidate should skip+WARN first"
        )

    monkeypatch.setattr(sniper, "get_best_option", _exploder)

    candidate = {
        "ticker": "AAPL",
        "nct_id": "",
        "primary_completion": "2026-06-15",
        "company": "Apple Inc.",
    }

    result = sniper.score_and_price_candidate(candidate)
    assert result is None

    captured = capsys.readouterr()
    assert "WARN" in captured.out and "AAPL" in captured.out, (
        "score_and_price_candidate must emit a WARN diagnostic when the "
        "underlying price is unavailable — got: " + repr(captured.out)
    )
    assert "skipping candidate" in captured.out, (
        "skip-with-WARN diagnostic must explicitly state the candidate "
        "was skipped — got: " + repr(captured.out)
    )


def test_score_and_price_candidate_uses_alpaca_fallback_when_validate_returns_none(
    monkeypatch,
):
    """When validate_ticker returns price=None, the Alpaca fallback is
    consulted and — when it returns a positive value — the pipeline
    proceeds to score the candidate.
    """
    sniper = importlib.import_module("biotech_sniper.new_opportunity_sniper")

    monkeypatch.setattr(
        sniper,
        "validate_ticker",
        _make_validate_ticker_stub(valid=True, price=None),
    )

    fallback_calls: list[str] = []

    def _fallback(ticker: str) -> Optional[float]:
        fallback_calls.append(ticker)
        return 192.50

    monkeypatch.setattr(sniper, "_resolve_underlying_price", _fallback)

    # Stub the rest of the pipeline so we observe whether the
    # resolved price reaches get_best_option.
    monkeypatch.setattr(
        sniper,
        "quick_score_candidate",
        lambda *_a, **_kw: {
            "p_success": 75,
            "direction": "LONG_CALLS",
            "confidence": "MEDIUM",
            "one_line": "test",
            "edge_line": "test",
        },
    )

    seen: dict[str, Any] = {}

    def _capture_get_best_option(ticker, expiry, direction, price):
        seen["price"] = price
        return {
            "strike": 200.0,
            "expiry": expiry,
            "direction": direction,
            "bid": 5.0,
            "ask": 5.2,
            "mid": 5.1,
            "spread_pct": 4.0,
            "oi": 100,
            "multiple": 4.0,
            "k1_return": 4000,
        }

    monkeypatch.setattr(sniper, "get_best_option", _capture_get_best_option)

    candidate = {
        "ticker": "AAPL",
        "nct_id": "",
        "primary_completion": "2026-06-15",
        "company": "Apple Inc.",
    }

    result = sniper.score_and_price_candidate(candidate)

    assert fallback_calls == ["AAPL"], (
        "_resolve_underlying_price must be consulted exactly once when "
        "validate_ticker returns price=None"
    )
    assert seen.get("price") == 192.50, (
        "The Alpaca-fallback price must be threaded into get_best_option"
    )
    assert result is not None
    assert result["price"] == 192.50
    assert result["ticker"] == "AAPL"


def test_score_and_price_candidate_does_not_silently_swallow_type_errors(monkeypatch):
    """If the pipeline ever produces a TypeError downstream of price
    resolution, it must NOT be swallowed by a broad-except — it must
    propagate so a regression of the f-m3-02 silent-rejection bug is
    instantly visible.
    """
    sniper = importlib.import_module("biotech_sniper.new_opportunity_sniper")

    monkeypatch.setattr(
        sniper,
        "validate_ticker",
        _make_validate_ticker_stub(valid=True, price=100.0),
    )
    monkeypatch.setattr(
        sniper,
        "quick_score_candidate",
        lambda *_a, **_kw: {
            "p_success": 75,
            "direction": "LONG_CALLS",
            "confidence": "MEDIUM",
            "one_line": "x",
            "edge_line": "y",
        },
    )

    def _explode(*_a, **_kw):
        raise TypeError("simulated regression of price=None silent reject")

    monkeypatch.setattr(sniper, "get_best_option", _explode)

    with pytest.raises(TypeError):
        sniper.score_and_price_candidate(
            {
                "ticker": "AAPL",
                "nct_id": "",
                "primary_completion": "2026-06-15",
            }
        )
