"""Centralized configuration for the biotech_sniper package.

This module is the single source of truth for:

* Secrets (API keys, credentials) read from the environment.
* Feature flags (LIVE_MODE, LLM_PROVIDERS).
* Risk defaults (per-play sizing, concurrency caps, deployed cap).

All other modules MUST import secrets and feature flags from here. Direct
calls to ``os.environ.get`` for any secret outside this module are
forbidden by mission policy (see ``AGENTS.md``).

The module also loads variables from a ``.env`` file (if present at the
project root) using ``python-dotenv``. The ``.env`` file is gitignored;
``.env.example`` lists the required key names without secret values.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from biotech_sniper.paths import BASE_DIR

# ---------------------------------------------------------------------------
# .env loader (best-effort: missing python-dotenv must not break imports).
# ---------------------------------------------------------------------------

# Resolve the .env file relative to the repository root. ``paths.BASE_DIR``
# is ``<repo>/biotech_sniper`` when ``BIOTECH_SNIPER_HOME`` is unset, so we
# walk one level up to reach the repo root that holds ``.env``.
def _candidate_env_files() -> list[Path]:
    """Return the .env files we will attempt to load, in priority order."""
    candidates: list[Path] = []
    # Repo-root .env (next to README.md). Used both locally and on the VPS
    # when the env file is colocated with the clone.
    repo_root = BASE_DIR.parent if BASE_DIR.name == "biotech_sniper" else BASE_DIR
    candidates.append(repo_root / ".env")
    # VPS convention: .env is at the project parent (one level above the
    # checkout) so it survives ``git clean`` and ``git pull``.
    candidates.append(repo_root.parent / ".env")
    return candidates


def _load_dotenv_if_available() -> None:
    """Populate ``os.environ`` from the first ``.env`` file we can find.

    Uses ``python-dotenv`` when available. Existing environment variables
    take precedence over file values (i.e. ``override=False``) so that
    operators can override .env values via the shell environment.
    """
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover - dotenv is a hard dep but we don't crash imports
        return
    for candidate in _candidate_env_files():
        if candidate.is_file():
            load_dotenv(dotenv_path=str(candidate), override=False)
            break


_load_dotenv_if_available()

# ---------------------------------------------------------------------------
# Risk defaults — sourced from the mission plan.
# ---------------------------------------------------------------------------

RISK_DEFAULTS: Final[dict[str, int]] = {
    "per_play": 250,         # USD risked per single options play
    "max_concurrent": 4,     # max simultaneously open plays
    "max_deployed": 1000,    # max USD deployed across all open plays
}


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _env_str(name: str, default: str | None = None) -> str | None:
    """Return ``os.environ[name]`` stripped, or ``default`` if unset/blank."""
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_bool(name: str, default: bool = False) -> bool:
    """Coerce a textual env value to a boolean.

    Truthy values: ``"1"``, ``"true"``, ``"yes"``, ``"on"`` (case-insensitive).
    Falsy values: anything else, including unset / empty.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Secret getters — the ONLY place these env vars should be read.
# ---------------------------------------------------------------------------

def get_xai_api_key() -> str | None:
    """Return the xAI / Grok API key (used by the M2 fast-tier scorer)."""
    return _env_str("XAI_API_KEY")


def get_anthropic_api_key() -> str | None:
    """Return the Anthropic / Claude API key (M2 deep-tier scorer)."""
    return _env_str("ANTHROPIC_API_KEY")


def get_gemini_api_key() -> str | None:
    """Return the Google Gemini API key (M2 deep-tier scorer)."""
    return _env_str("GEMINI_API_KEY")


def get_alpaca_key_id() -> str | None:
    """Return the Alpaca paper account key id (M3 paper executor)."""
    return _env_str("ALPACA_KEY_ID")


def get_alpaca_secret_key() -> str | None:
    """Return the Alpaca paper account secret (M3 paper executor)."""
    return _env_str("ALPACA_SECRET_KEY")


def get_alpaca_base_url() -> str:
    """Return the Alpaca API base URL.

    Defaults to the paper-trading endpoint. The live endpoint is hard-blocked
    elsewhere via the ``LIVE_MODE`` two-flag guardrail.
    """
    return _env_str("ALPACA_BASE_URL", "https://paper-api.alpaca.markets") or "https://paper-api.alpaca.markets"


def get_biotech_sniper_home() -> str | None:
    """Return the configured project home directory, if set."""
    return _env_str("BIOTECH_SNIPER_HOME")


# ---------------------------------------------------------------------------
# Feature flags.
# ---------------------------------------------------------------------------

# LIVE_MODE: hard-blocked across the mission. Defaults to False. Workers
# MUST NOT toggle this on; only the user, via direct manual edit + a
# confirmation marker file (``i-understand-this-trades-real-money``), can
# unblock real-money trading.
LIVE_MODE: Final[bool] = _env_bool("LIVE_MODE", default=False)


def _resolve_llm_providers() -> dict[str, object]:
    """Build the ``LLM_PROVIDERS`` mapping, honouring env overrides.

    * ``LLM_PROVIDERS_FAST`` — string, default ``"xai"``.
    * ``LLM_PROVIDERS_DEEP`` — comma-separated list, default
      ``"anthropic,gemini"``. An empty value disables the deep tier
      entirely (used by the ``test_fast_only_mode`` validator).
    """
    fast = _env_str("LLM_PROVIDERS_FAST", "xai") or "xai"

    deep_raw = os.environ.get("LLM_PROVIDERS_DEEP")
    if deep_raw is None:
        deep: list[str] = ["anthropic", "gemini"]
    else:
        deep = [p.strip() for p in deep_raw.split(",") if p.strip()]
    return {"fast": fast, "deep": deep}


LLM_PROVIDERS: Final[dict[str, object]] = _resolve_llm_providers()


def provider_enabled(provider: str) -> bool:
    """Return True if the named provider is configured AND has a key set.

    The validation contract requires that providers default to
    "disabled-when-key-missing". This helper lets callers gate LLM calls
    behind both feature-flag membership and credential presence without
    duplicating the lookup logic.
    """
    fast = LLM_PROVIDERS["fast"]
    deep = LLM_PROVIDERS["deep"]
    assert isinstance(deep, list)

    if provider != fast and provider not in deep:
        return False

    if provider == "xai":
        return bool(get_xai_api_key())
    if provider == "anthropic":
        return bool(get_anthropic_api_key())
    if provider == "gemini":
        return bool(get_gemini_api_key())
    return False


__all__ = [
    "RISK_DEFAULTS",
    "LIVE_MODE",
    "LLM_PROVIDERS",
    "get_xai_api_key",
    "get_anthropic_api_key",
    "get_gemini_api_key",
    "get_alpaca_key_id",
    "get_alpaca_secret_key",
    "get_alpaca_base_url",
    "get_biotech_sniper_home",
    "provider_enabled",
]
