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
# Position sizing & paper-executor caps (M3 feature f-m3-04).
# ---------------------------------------------------------------------------

# ``RISK_PER_PLAY_USD`` is the default per-play USD cap consumed by
# :func:`biotech_sniper.paper_executor.size_position` when computing the
# contract count for an entry. The contract is
# ``contracts = floor(RISK_PER_PLAY_USD / (mid * 100))``; ``mid`` is
# the (bid + ask) / 2 from the chain row attached to the play card.
#
# Operators can override the cap on a per-deployment basis by writing
# ``{"risk_per_play_usd": <int>}`` into ``state/calibration_params.json``.
# :func:`get_risk_per_play_usd` reads that override at call time so the
# value is hot-reloadable without an executor restart.
RISK_PER_PLAY_USD: Final[int] = 250

# ``MAX_CONCURRENT_PLAYS`` caps the number of simultaneously-active
# Alpaca options positions. The paper executor calls
# ``client.get_positions()`` and refuses to submit a new entry once the
# returned length reaches this value (raising
# ``ConcurrencyCapExceeded``). Distinct from
# :data:`RISK_DEFAULTS["max_concurrent"]` (which is the broader
# mission-wide ceiling); the M3 paper executor uses the tighter
# value below per the f-m3-04 contract.
MAX_CONCURRENT_PLAYS: Final[int] = 3

# ``MAX_DEPLOYED_USD`` caps the total deployed capital across active
# Alpaca options positions. Computed as
# ``sum(qty * avg_entry_price * 100)`` and compared against this
# constant plus the planned cost of the new entry; submissions that
# would push the sum above this cap raise ``DeployedCapExceeded``.
MAX_DEPLOYED_USD: Final[int] = 750


# ``LIQUIDITY_PROBE_DAILY_USD_CAP`` is the per-day USD cap on the
# total cost of liquidity-probe orders submitted by
# :mod:`biotech_sniper.liquidity_probe`. The module refuses to submit
# a probe when the day's cumulative ``SUM(cost_usd)`` (across all
# rows in the ``liquidity_probes`` SQLite table whose
# ``submitted_at`` falls on today's UTC date) already meets or
# exceeds this cap. Default ``20`` USD per the M3 mission plan.
#
# Probe spend is intentionally tracked SEPARATELY from
# :data:`MAX_DEPLOYED_USD` — probes are diagnostic, not part of the
# strategy's deployed capital, so they must not crowd out real
# entries.
LIQUIDITY_PROBE_DAILY_USD_CAP: Final[int] = 20


# ---------------------------------------------------------------------------
# Workload caps (M4 feature f-m4-05).
# ---------------------------------------------------------------------------

# ``MAX_WORKERS`` is the hard ceiling on the ``max_workers`` argument of any
# :class:`concurrent.futures.ThreadPoolExecutor` instantiated anywhere in
# the project. The VPS is a 2-core box already operating near sustained
# 2× load (HL grok service, hl-price-recorder, geo_shock_monitor, etc.),
# so unbounded thread pools have historically caused load spikes that
# starved sibling services. Per ``AGENTS.md`` § "Workload caps on VPS",
# every executor MUST request ``max_workers ≤ MAX_WORKERS``.
#
# All callers that need a thread pool MUST go through
# :func:`biotech_sniper.thread_pool.bounded_thread_pool`, which clamps
# requested workers to this cap and raises
# :class:`biotech_sniper.thread_pool.ThreadPoolCapExceeded` if a caller
# passes a value above the cap. Direct instantiation of
# ``ThreadPoolExecutor(max_workers=N)`` with ``N > MAX_WORKERS`` is a
# mission-policy violation; the validation contract (VAL-M4-044) greps
# for it.
MAX_WORKERS: Final[int] = 4


# ---------------------------------------------------------------------------
# Stage-2 (Reading-B) probability threshold gate (M3 feature f-m3-04).
# ---------------------------------------------------------------------------

# ``DEFAULT_STAGE2_PROBABILITY_THRESHOLD`` is the canonical default for
# the Reading-B Stage-2 mean-probability gate. The gate computes the
# arithmetic mean of the four successful providers' ``probability``
# fields and rejects entry when ``mean < threshold``. The default
# (``0.75``) is sourced from ``mission.md`` and ``AGENTS.md``
# § "Reading-B specific risk gates (M3)".
#
# Operators can override the threshold for a deployment by setting the
# ``STAGE2_PROBABILITY_THRESHOLD`` environment variable (a float in
# ``[0.0, 1.0]``); :func:`get_stage2_probability_threshold` reads the
# environment at call time so the override is hot-reloadable. Invalid
# / unparseable values fall back to the default with a WARNING log so
# operators can tell the override was rejected without crashing the
# Stage-2 dispatcher.
#
# The module-level constant :data:`STAGE2_PROBABILITY_THRESHOLD` is
# resolved once at import time and exposed for the contract-evidence
# command in VAL-M3-022 (``from biotech_sniper.config import
# STAGE2_PROBABILITY_THRESHOLD; assert STAGE2_PROBABILITY_THRESHOLD ==
# 0.75``). Callers that need hot-reload semantics MUST go through
# :func:`get_stage2_probability_threshold`.
DEFAULT_STAGE2_PROBABILITY_THRESHOLD: Final[float] = 0.75


def _resolve_stage2_probability_threshold() -> float:
    """Return the active threshold, honouring ``STAGE2_PROBABILITY_THRESHOLD``.

    Returns :data:`DEFAULT_STAGE2_PROBABILITY_THRESHOLD` (``0.75``) when
    the env var is unset or unparseable. Surrounding whitespace is
    stripped. Any value outside ``[0.0, 1.0]`` is accepted as-is — the
    gate clamps mean probabilities to ``[0.0, 1.0]`` defensively, so an
    out-of-range threshold simply changes which side of the boundary
    the gate falls on (``threshold > 1.0`` is "reject everything";
    ``threshold < 0.0`` is "accept everything"). The gate does NOT
    silently coerce these to canonical defaults — that is an explicit
    operator decision.
    """
    import logging

    raw = os.environ.get("STAGE2_PROBABILITY_THRESHOLD")
    if raw is None:
        return DEFAULT_STAGE2_PROBABILITY_THRESHOLD
    raw = raw.strip()
    if not raw:
        return DEFAULT_STAGE2_PROBABILITY_THRESHOLD
    try:
        return float(raw)
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning(
            "STAGE2_PROBABILITY_THRESHOLD=%r is not a valid float; "
            "falling back to default=%.4f",
            raw,
            DEFAULT_STAGE2_PROBABILITY_THRESHOLD,
        )
        return DEFAULT_STAGE2_PROBABILITY_THRESHOLD


def get_stage2_probability_threshold() -> float:
    """Return the active Stage-2 probability threshold.

    Reads ``STAGE2_PROBABILITY_THRESHOLD`` from the environment at call
    time; falls back to :data:`DEFAULT_STAGE2_PROBABILITY_THRESHOLD`
    when unset/unparseable. Callers (e.g.
    :mod:`biotech_sniper.llm.stage2_gates`) MUST use this getter rather
    than reading the env var directly so the single-source-of-truth
    invariant is preserved.
    """
    return _resolve_stage2_probability_threshold()


#: Module-load-time snapshot of the threshold. Exposed for the
#: VAL-M3-022 contract-evidence command and for code paths that want a
#: stable per-process value. Hot-reloadable callers should use
#: :func:`get_stage2_probability_threshold` instead.
STAGE2_PROBABILITY_THRESHOLD: Final[float] = _resolve_stage2_probability_threshold()


# ``STOP_LOSS_PCT`` is the negative percentage drawdown at which the
# f-m3-09 stop-loss trigger fires. Default is ``-0.50`` (a 50% drop
# from the entry mid). When ``current_mid / entry_mid - 1`` is less
# than or equal to this value the trigger submits an exit order with
# ``event='stop_loss'`` to close 100% of the position.
#
# The value is intentionally a float so operators can tune it via a
# state override without changing the type signature; positive values
# would never fire and are treated as "stop-loss disabled".
STOP_LOSS_PCT: Final[float] = -0.50


def get_risk_per_play_usd() -> int:
    """Return the effective per-play risk cap in USD.

    Honours an optional ``state/calibration_params.json`` file with
    a ``risk_per_play_usd`` key — when present and parseable, that
    integer overrides :data:`RISK_PER_PLAY_USD`. Missing or
    unparseable JSON / non-numeric override values fall back to the
    default with a WARNING log so operators can tell the override
    was rejected without crashing the run.

    The state file is read at every call so the override is
    hot-reloadable; the executor does not need to be restarted to
    pick up a tuning change.
    """
    import json
    import logging

    from biotech_sniper.paths import STATE_DIR

    calibration_path = STATE_DIR / "calibration_params.json"
    try:
        if not calibration_path.is_file():
            return RISK_PER_PLAY_USD
        raw = calibration_path.read_text(encoding="utf-8")
    except OSError:
        logging.getLogger(__name__).warning(
            "calibration_params.json present but unreadable at %s; "
            "falling back to RISK_PER_PLAY_USD=%d",
            calibration_path,
            RISK_PER_PLAY_USD,
        )
        return RISK_PER_PLAY_USD

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logging.getLogger(__name__).warning(
            "calibration_params.json at %s is not valid JSON; "
            "falling back to RISK_PER_PLAY_USD=%d",
            calibration_path,
            RISK_PER_PLAY_USD,
        )
        return RISK_PER_PLAY_USD

    if not isinstance(data, dict):
        return RISK_PER_PLAY_USD
    override = data.get("risk_per_play_usd")
    if override is None:
        return RISK_PER_PLAY_USD
    try:
        return int(override)
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning(
            "calibration_params.json.risk_per_play_usd=%r is not numeric; "
            "falling back to RISK_PER_PLAY_USD=%d",
            override,
            RISK_PER_PLAY_USD,
        )
        return RISK_PER_PLAY_USD


# ---------------------------------------------------------------------------
# Ensemble synthesis weights (M2 ensemble layer).
# ---------------------------------------------------------------------------

# ``ENSEMBLE_WEIGHTS`` describes the weighting applied by
# :func:`biotech_sniper.llm.ensemble.compute_ensemble` when blending the
# fast-tier (Grok-4) probability with the deep-tier (Claude / Gemini)
# probabilities into a single ``ensemble_score``.
#
# Documented formula (see ``biotech_sniper.llm.ensemble``):
#
#     ensemble_score = w_grok   * grok_score
#                    + w_claude * claude_probability
#                    + w_gemini * gemini_probability
#
# The base weights below sum to 1.0. When a provider is disabled via
# :data:`LLM_PROVIDERS` (or its API key is missing), its weight is
# redistributed proportionally across the active providers so the
# active weights still sum to 1.0. When the deep tier is disabled
# entirely (``LLM_PROVIDERS["deep"] == []``), ``ensemble_score`` equals
# ``grok_score`` (effective grok weight = 1.0).
#
# The weights below were chosen so the deep tier (combined Claude +
# Gemini) carries 60% of the ensemble while the cheaper fast tier
# carries the remaining 40%. Validators in ``tests/scoring/test_ensemble.py``
# exercise this formula against fixed inputs.
ENSEMBLE_WEIGHTS: Final[dict[str, float]] = {
    "grok": 0.4,
    "claude": 0.3,
    "gemini": 0.3,
}


# ---------------------------------------------------------------------------
# Selection thresholds (M2 selection logic — f-m2-11).
# ---------------------------------------------------------------------------

# ``MIN_ENSEMBLE_SCORE`` is the minimum :data:`ensemble_score` (a float
# in ``[0.0, 1.0]``) a candidate must reach in
# :func:`biotech_sniper.sectors.unified_scorer.select_top_n` before it
# can be promoted to a play card under
# ``play_cards/YYYY-MM-DD/<TICKER>.json``.
#
# Default ``0.55`` is the documented baseline for the M2 build and is
# expected to be tuned by the M5 LightGBM ranker once the calibration
# dataset is large enough. A higher value tightens selection; a lower
# value lets weaker candidates through. Keep the value in ``[0, 1]``
# since :data:`ensemble_score` is itself a probability.
MIN_ENSEMBLE_SCORE: Final[float] = 0.55


# ``MIN_SCIENCE_GRADE`` is the minimum letter grade (per
# :data:`biotech_sniper.llm.claude_client.LETTER_GRADE_ORDER`) the
# deep-tier ``science_grade`` must reach for a candidate to be
# promoted in :func:`select_top_n`. A grade is "good enough" when its
# index in :data:`LETTER_GRADE_ORDER` (lower index = better grade) is
# **less than or equal to** the index of ``MIN_SCIENCE_GRADE``.
#
# Default ``"C+"`` matches the f-m2-11 spec — it admits A/B/C+ tiers
# and rejects C / C- / D / F. The grade is a string compared via the
# canonical ordering (NOT lexicographic), so naive ``science_grade >=
# "C+"`` SQL comparison is *not* sufficient for non-trivial ties (e.g.
# ``"B-"`` is alphabetically less than ``"C"`` but a better grade);
# the selection helper translates this constant into the canonical
# allowed-list before issuing the SQLite query.
MIN_SCIENCE_GRADE: Final[str] = "C+"


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


def get_perplexity_api_key() -> str | None:
    """Return the Perplexity API key (Reading-B M3 Stage-2 scorer).

    Reading-B introduces a fourth LLM provider in the Stage-2 ensemble
    fan-out (``biotech_sniper.llm.perplexity_client``). The key is read
    via this getter only — direct ``os.environ`` lookups for
    ``PERPLEXITY_API_KEY`` outside :mod:`biotech_sniper.config` are a
    mission-policy violation (see ``AGENTS.md``).
    """
    return _env_str("PERPLEXITY_API_KEY")


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


# ``LIGHTGBM_RANKER_ENABLED`` controls whether
# :func:`biotech_sniper.sectors.unified_scorer.attach_supplementary_score`
# loads the M5 LightGBM ranker (``models/ranker_v{N}.lgb``) and decorates
# every play card with a supplementary ranker probability field. Default
# OFF — leaves the LLM ensemble as the sole source of truth for trade
# gating, sizing and entry decisions (the ranker is a *supplementary*
# signal, never a replacement; see VAL-M5-031). When OFF, ``lightgbm``
# is not imported anywhere in the play-card emission path so the
# library stays absent from ``sys.modules`` (VAL-M5-030).
#
# Operators flip this on by setting ``LIGHTGBM_RANKER_ENABLED=1`` in
# the environment. The flag is read fresh on each play-card emission
# so toggling it does not require a process restart.
LIGHTGBM_RANKER_ENABLED: Final[bool] = _env_bool(
    "LIGHTGBM_RANKER_ENABLED", default=False
)


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
    "RISK_PER_PLAY_USD",
    "MAX_CONCURRENT_PLAYS",
    "MAX_DEPLOYED_USD",
    "LIQUIDITY_PROBE_DAILY_USD_CAP",
    "MAX_WORKERS",
    "STOP_LOSS_PCT",
    "DEFAULT_STAGE2_PROBABILITY_THRESHOLD",
    "STAGE2_PROBABILITY_THRESHOLD",
    "get_stage2_probability_threshold",
    "ENSEMBLE_WEIGHTS",
    "MIN_ENSEMBLE_SCORE",
    "MIN_SCIENCE_GRADE",
    "LIVE_MODE",
    "LIGHTGBM_RANKER_ENABLED",
    "LLM_PROVIDERS",
    "get_xai_api_key",
    "get_anthropic_api_key",
    "get_gemini_api_key",
    "get_perplexity_api_key",
    "get_alpaca_key_id",
    "get_alpaca_secret_key",
    "get_alpaca_base_url",
    "get_biotech_sniper_home",
    "get_risk_per_play_usd",
    "provider_enabled",
]


# ---------------------------------------------------------------------------
# CLI entry point — VAL-CROSS-013/014/015 surface (added by f-cross-02).
# ---------------------------------------------------------------------------

# Canonical list of env vars the codebase reads via this module. Kept in
# sync with ``.env.example`` (VAL-CROSS-013) and used by the
# ``check_env`` / ``dump_env_vars`` subcommands below. Order is the
# alphabetical canonical order so callers can ``diff`` against
# ``grep -oE '^[A-Z][A-Z0-9_]+' .env.example | sort`` cleanly.
REQUIRED_ENV_VARS: Final[tuple[str, ...]] = (
    "ALPACA_BASE_URL",
    "ALPACA_KEY_ID",
    "ALPACA_SECRET_KEY",
    "ANTHROPIC_API_KEY",
    "BIOTECH_SNIPER_HOME",
    "GEMINI_API_KEY",
    "LIVE_MODE",
    "XAI_API_KEY",
)


def _cli_check_env() -> int:
    import sys as _sys
    missing = [n for n in REQUIRED_ENV_VARS if not (os.environ.get(n) or "").strip()]
    if missing:
        for n in missing:
            print(f"ERROR: missing required env var: {n}", file=_sys.stderr)
        return 1
    print("OK")
    return 0


def _cli_dump_env_vars() -> int:
    for n in REQUIRED_ENV_VARS:
        print(n)
    return 0


if __name__ == "__main__":
    import sys as _sys
    _argv = _sys.argv[1:]
    if not _argv:
        print("usage: python -m biotech_sniper.config {check_env|dump_env_vars}", file=_sys.stderr)
        _sys.exit(2)
    _cmd = _argv[0]
    if _cmd == "check_env":
        _sys.exit(_cli_check_env())
    if _cmd == "dump_env_vars":
        _sys.exit(_cli_dump_env_vars())
    print(f"ERROR: unknown subcommand: {_cmd}", file=_sys.stderr)
    _sys.exit(2)
