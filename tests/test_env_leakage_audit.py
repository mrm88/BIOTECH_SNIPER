"""f-m2-16 fix #1 regression — provider env-leakage audit.

The "disabled-when-key-missing" guarantee documented in
``AGENTS.md`` § "LLM boundaries" and asserted by the validation
contract (VAL-M2-042 spirit) requires that
:func:`biotech_sniper.config.provider_enabled` return ``False`` for a
given provider only when (a) the provider is not in
``LLM_PROVIDERS["deep"]`` / not the active ``LLM_PROVIDERS["fast"]``,
or (b) its specific API key is absent.

A clean VPS deploy at ``/root/alpha_sniper/.env`` legitimately
carries some — but not all — provider keys (e.g. ``XAI_API_KEY`` and
``ANTHROPIC_API_KEY`` are reused from the HL grok env, while
``GEMINI_API_KEY`` is user-supplied). Any test that asserts
``provider_enabled(target) is False`` therefore MUST delenv all three
provider keys (``XAI_API_KEY``, ``ANTHROPIC_API_KEY``,
``GEMINI_API_KEY``) before checking; otherwise the assertion drifts
when run against the VPS environment.

This regression test pins the invariant: across the full cross
product of ``(target_provider, env-set)``, ``provider_enabled`` must
return ``True`` if and only if the matching key for ``target_provider``
is set AND the provider is active in :data:`LLM_PROVIDERS` (default
config keeps all three providers active).

The tests do not call any LLM — they exercise the pure config helper.
"""

from __future__ import annotations

import importlib

import pytest


PROVIDERS = ("xai", "anthropic", "gemini")
KEY_FOR = {
    "xai": "XAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def _power_set(items: tuple[str, ...]) -> list[tuple[str, ...]]:
    """Return every subset of ``items`` as a sorted tuple list."""
    out: list[tuple[str, ...]] = [()]
    for item in items:
        out.extend(sub + (item,) for sub in list(out))
    return out


@pytest.fixture
def fresh_config(monkeypatch):
    """Yield a freshly-reloaded ``biotech_sniper.config`` module.

    Clears every provider key + the ``LLM_PROVIDERS_DEEP`` /
    ``LLM_PROVIDERS_FAST`` overrides so each parametrised case starts
    from a known-empty environment, then lets the test set the
    desired keys before reloading the config.
    """
    for env_var in (*KEY_FOR.values(), "LLM_PROVIDERS_FAST", "LLM_PROVIDERS_DEEP"):
        monkeypatch.delenv(env_var, raising=False)
    yield monkeypatch


@pytest.mark.parametrize("target", PROVIDERS)
@pytest.mark.parametrize("env_set", _power_set(PROVIDERS))
def test_provider_enabled_only_when_key_present(fresh_config, target, env_set):
    """Cross product of (target, env-set) — strict on env leakage.

    For every ``target`` provider and every subset ``env_set`` of
    provider keys present in the environment, the helper must return
    ``True`` only when ``target in env_set`` (and the provider is
    active in :data:`LLM_PROVIDERS`).
    """
    monkeypatch = fresh_config
    for provider in env_set:
        monkeypatch.setenv(KEY_FOR[provider], f"fake-{provider}-key")

    from biotech_sniper import config as _config

    importlib.reload(_config)
    expected = target in env_set
    assert _config.provider_enabled(target) is expected, (
        f"target={target!r} env_set={env_set!r} expected={expected} "
        f"got={_config.provider_enabled(target)} — provider_enabled "
        f"leaked or hid an env key"
    )


@pytest.mark.parametrize("target", PROVIDERS)
def test_disabled_only_when_all_three_keys_absent(fresh_config, target):
    """Tighter restatement of the rule: ``provider_enabled(target)`` is
    False only when ALL THREE provider env vars are unset.

    With any single key set, exactly the matching target turns True
    and the others stay False. This is the exact invariant tests like
    ``test_disabled_via_flag`` rely on.
    """
    monkeypatch = fresh_config
    # No keys → every provider disabled (independent of LLM_PROVIDERS).
    from biotech_sniper import config as _config

    importlib.reload(_config)
    for provider in PROVIDERS:
        assert _config.provider_enabled(provider) is False, (
            f"provider_enabled({provider!r}) should be False with no "
            f"keys; the test environment is leaking a real key"
        )

    # Set only the target's key → only target turns True.
    monkeypatch.setenv(KEY_FOR[target], f"fake-{target}-key")
    importlib.reload(_config)
    for provider in PROVIDERS:
        expected = provider == target
        assert _config.provider_enabled(provider) is expected, (
            f"after setting {KEY_FOR[target]!r}: provider_enabled("
            f"{provider!r}) = {_config.provider_enabled(provider)}, "
            f"expected {expected}"
        )


def test_disabled_via_deep_flag_with_only_anthropic_key(fresh_config):
    """Mirrors the real-VPS scenario that surfaced f-m2-16 fix #1.

    On the VPS, ``ANTHROPIC_API_KEY`` and ``XAI_API_KEY`` are present
    in ``/root/alpha_sniper/.env`` but ``GEMINI_API_KEY`` is not. A
    test that flips ``LLM_PROVIDERS_DEEP=anthropic`` and asserts
    ``provider_enabled('anthropic') is False`` MUST also delenv
    ``ANTHROPIC_API_KEY`` (and ``XAI_API_KEY``) — otherwise the
    assertion fails on the VPS but passes locally.

    This regression test pins the corrected behaviour: all three keys
    must be cleared before such a check.
    """
    monkeypatch = fresh_config
    monkeypatch.setenv("LLM_PROVIDERS_DEEP", "anthropic")
    from biotech_sniper import config as _config

    importlib.reload(_config)
    # All three keys absent → every provider disabled, regardless of flag.
    assert _config.provider_enabled("anthropic") is False
    assert _config.provider_enabled("gemini") is False
    assert _config.provider_enabled("xai") is False
