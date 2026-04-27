"""Unit tests for biotech_sniper.config.

Covers:
* RISK_DEFAULTS exposes the agreed numbers (per_play=250, max_concurrent=3,
  max_deployed=750).
* LIVE_MODE defaults to False when LIVE_MODE is unset or "0".
* LIVE_MODE coerces truthy strings ("1", "true", "yes", "on").
* LLM_PROVIDERS exposes fast="xai" and deep=["anthropic","gemini"].
* LLM_PROVIDERS_DEEP env override changes the deep list and supports
  the empty-string "fast-only" mode used by VAL-M2-046.
* Secret getters return None when their env var is unset / blank, and the
  string value (stripped) when set.
* ALPACA_BASE_URL falls back to the paper endpoint by default.
* No direct ``os.environ.get`` for any secret outside config.py (policy
  enforced via repo grep).
"""

from __future__ import annotations

import importlib
import sys


def _reload_config():
    """Reload the config module so module-level constants pick up env changes."""
    if "biotech_sniper.config" in sys.modules:
        return importlib.reload(sys.modules["biotech_sniper.config"])
    return importlib.import_module("biotech_sniper.config")


def _clear_env(monkeypatch):
    for k in (
        "LIVE_MODE",
        "LLM_PROVIDERS_FAST",
        "LLM_PROVIDERS_DEEP",
        "XAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "ALPACA_KEY_ID",
        "ALPACA_SECRET_KEY",
        "ALPACA_BASE_URL",
    ):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# RISK_DEFAULTS
# ---------------------------------------------------------------------------


def test_risk_defaults_match_agreed_numbers(monkeypatch):
    _clear_env(monkeypatch)
    config = _reload_config()
    assert config.RISK_DEFAULTS == {
        "per_play": 250,
        "max_concurrent": 3,
        "max_deployed": 750,
    }


# ---------------------------------------------------------------------------
# LIVE_MODE
# ---------------------------------------------------------------------------


def test_live_mode_defaults_false_when_unset(monkeypatch):
    _clear_env(monkeypatch)
    config = _reload_config()
    assert config.LIVE_MODE is False


def test_live_mode_false_when_zero(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("LIVE_MODE", "0")
    config = _reload_config()
    assert config.LIVE_MODE is False


def test_live_mode_truthy_strings(monkeypatch):
    for truthy in ("1", "true", "TRUE", "yes", "on"):
        _clear_env(monkeypatch)
        monkeypatch.setenv("LIVE_MODE", truthy)
        config = _reload_config()
        assert config.LIVE_MODE is True, f"{truthy!r} should be truthy"


def test_live_mode_falsy_strings(monkeypatch):
    for falsy in ("0", "false", "no", "off", ""):
        _clear_env(monkeypatch)
        monkeypatch.setenv("LIVE_MODE", falsy)
        config = _reload_config()
        assert config.LIVE_MODE is False, f"{falsy!r} should be falsy"


# ---------------------------------------------------------------------------
# LLM_PROVIDERS
# ---------------------------------------------------------------------------


def test_llm_providers_default(monkeypatch):
    _clear_env(monkeypatch)
    config = _reload_config()
    assert config.LLM_PROVIDERS["fast"] == "xai"
    assert config.LLM_PROVIDERS["deep"] == ["anthropic", "gemini"]


def test_llm_providers_deep_override_single(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDERS_DEEP", "anthropic")
    config = _reload_config()
    assert config.LLM_PROVIDERS["deep"] == ["anthropic"]


def test_llm_providers_deep_override_empty_disables_tier(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDERS_DEEP", "")
    config = _reload_config()
    assert config.LLM_PROVIDERS["deep"] == []


def test_llm_providers_fast_override(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDERS_FAST", "anthropic")
    config = _reload_config()
    assert config.LLM_PROVIDERS["fast"] == "anthropic"


def test_provider_enabled_requires_key(monkeypatch):
    _clear_env(monkeypatch)
    config = _reload_config()
    # No keys set: every default provider is disabled.
    assert config.provider_enabled("xai") is False
    assert config.provider_enabled("anthropic") is False
    assert config.provider_enabled("gemini") is False


def test_provider_enabled_with_key(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("XAI_API_KEY", "xai-test-secret")
    config = _reload_config()
    assert config.provider_enabled("xai") is True
    assert config.provider_enabled("anthropic") is False


def test_provider_enabled_unknown_provider(monkeypatch):
    _clear_env(monkeypatch)
    config = _reload_config()
    assert config.provider_enabled("openai") is False


# ---------------------------------------------------------------------------
# Secret getters
# ---------------------------------------------------------------------------


def test_get_xai_api_key_unset(monkeypatch):
    _clear_env(monkeypatch)
    config = _reload_config()
    assert config.get_xai_api_key() is None


def test_get_xai_api_key_set(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("XAI_API_KEY", "  xai-abc123  ")
    config = _reload_config()
    # Whitespace stripped.
    assert config.get_xai_api_key() == "xai-abc123"


def test_get_alpaca_base_url_default(monkeypatch):
    _clear_env(monkeypatch)
    config = _reload_config()
    assert config.get_alpaca_base_url() == "https://paper-api.alpaca.markets"


def test_get_alpaca_base_url_override(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("ALPACA_BASE_URL", "https://other.example/v2")
    config = _reload_config()
    assert config.get_alpaca_base_url() == "https://other.example/v2"


def test_anthropic_and_gemini_getters(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-test")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-test")
    config = _reload_config()
    assert config.get_anthropic_api_key() == "ant-test"
    assert config.get_gemini_api_key() == "gem-test"


def test_alpaca_credential_getters(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("ALPACA_KEY_ID", "PKTEST")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    config = _reload_config()
    assert config.get_alpaca_key_id() == "PKTEST"
    assert config.get_alpaca_secret_key() == "secret"


def test_blank_env_treated_as_unset(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("XAI_API_KEY", "   ")
    config = _reload_config()
    assert config.get_xai_api_key() is None
