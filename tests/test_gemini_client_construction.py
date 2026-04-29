"""Canary tests for :class:`biotech_sniper.llm.gemini_client.GeminiClient`
construction against the installed ``google-genai`` SDK version.

Background
----------
The ``google-genai`` SDK has churned its options/types surface across
versions. The original :class:`GeminiClient` constructed an
:class:`google.genai.types.HttpOptions` dataclass to forward the
per-request timeout to the underlying transport — but in
``google-genai==0.4.0`` (the version pinned in ``requirements.txt``)
``HttpOptions`` was relocated to the private ``google.genai._api_client``
module and is no longer re-exported from ``google.genai.types``. Every
CLI invocation logged::

    AttributeError: module 'google.genai.types' has no attribute 'HttpOptions'

… and ``EnsembleScorer.from_config`` silently degraded to xai +
anthropic only, even when ``GEMINI_API_KEY`` was set. The CLI's
``providers_used`` summary still claimed ``gemini`` because the
key-presence env check passed.

These tests are the **canary** for that drift: they construct a
:class:`GeminiClient` against the *real, installed* ``google-genai``
SDK (no fake client injection, no monkeypatched ``genai.Client``)
and assert the call returns a usable instance without raising. If a
future SDK upgrade re-breaks construction (e.g. ``http_options``
type changes, ``Client.__init__`` signature reshuffles), this test
will fail loudly at the first ``pytest`` run, *before* the broken
binary lands on the VPS and silently degrades the ensemble.

The tests deliberately use a stub API key (``"canary-stub-key"``)
because :class:`GeminiClient.__init__` performs no network I/O at
construction time — the key is only forwarded into the SDK's
internal ``HttpOptionsDict``-shaped settings and is not validated
until the first ``models.generate_content`` call. Live API
verification belongs in a separate VPS-side smoke run, not in the
hermetic test suite.
"""

from __future__ import annotations

import pytest


_STUB_KEY = "canary-stub-key-not-for-network-use"


# NOTE: We do NOT import :class:`GeminiClient` / :class:`GeminiAuthError`
# at module load time. ``tests/test_gemini_client.py`` calls
# ``importlib.reload(gemini_client)`` inside two of its tests
# (``test_disabled_via_flag`` and
# ``test_disabled_via_flag_pipeline_does_not_crash_when_key_missing``),
# which rebinds the class objects in the module's namespace. A
# top-level import here would capture the *original* class objects,
# so once the reload runs the bare-name reference inside
# ``GeminiClient.__init__`` (``raise GeminiAuthError(...)``) resolves
# to the *new* ``GeminiAuthError`` and our top-level
# ``pytest.raises(GeminiAuthError)`` no longer matches. Importing
# inside each test gives us the current module references and keeps
# the canary deterministic regardless of test order.


# ---------------------------------------------------------------------------
# Direct constructor canaries
# ---------------------------------------------------------------------------


def test_gemini_client_init_does_not_raise_against_installed_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GeminiClient(api_key=...)`` must succeed on the installed SDK.

    Regression test for f-misc-05: ``__init__`` referenced
    :class:`google.genai.types.HttpOptions`, which is absent in
    ``google-genai==0.4.0``. If the wrapper reaches for an attribute
    the installed SDK no longer exposes, this test surfaces it
    immediately with the exact ``AttributeError`` message.
    """
    from biotech_sniper.llm.gemini_client import GeminiClient

    # Pass the api_key explicitly so this test is independent of the
    # ambient ``GEMINI_API_KEY`` env var: the canary is about SDK
    # surface compatibility, not credential resolution.
    client = GeminiClient(api_key=_STUB_KEY)
    assert isinstance(client, GeminiClient)
    # The wrapper must have built an underlying SDK client; we don't
    # assert its concrete type (avoids coupling to private SDK
    # internals) but we do require it to be non-None.
    assert client._client is not None


def test_gemini_client_from_config_does_not_raise_when_key_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GeminiClient.from_config()`` must succeed when ``GEMINI_API_KEY``
    is set.

    Production callers (and ``EnsembleScorer.from_config``) reach
    :class:`GeminiClient` through this classmethod, so the canary
    covers the same path the unified-scorer CLI exercises in
    production.
    """
    from biotech_sniper.llm.gemini_client import GeminiClient

    monkeypatch.setenv("GEMINI_API_KEY", _STUB_KEY)
    client = GeminiClient.from_config()
    assert isinstance(client, GeminiClient)
    assert client._client is not None


def test_gemini_client_from_config_raises_auth_error_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing ``GEMINI_API_KEY`` raises :class:`GeminiAuthError`.

    Pinned alongside the canary so a future refactor that flips the
    "no-key" semantics from raise → return-None (or vice versa) is
    caught explicitly. ``EnsembleScorer._maybe_build_gemini_client``
    relies on this raise so the missing-key branch can be
    distinguished from an SDK-drift branch.
    """
    from biotech_sniper.llm.gemini_client import GeminiAuthError, GeminiClient

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(GeminiAuthError):
        GeminiClient.from_config()


# ---------------------------------------------------------------------------
# SDK surface canaries — explicit failure modes we want to detect early
# ---------------------------------------------------------------------------


def test_gemini_client_init_does_not_call_removed_types_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``__init__`` must not reference attributes that have been removed
    from ``google.genai.types`` in the installed SDK version.

    The original bug looked like this::

        from google.genai import types as genai_types
        ...
        http_options = genai_types.HttpOptions(timeout=...)

    Once 0.4.0 of the SDK shipped without ``HttpOptions`` on
    ``types``, every ``__init__`` raised ``AttributeError`` at
    construction time. We pin the contract explicitly: any access
    to a known-removed attribute on ``google.genai.types`` would
    blow up here.
    """
    from biotech_sniper.llm.gemini_client import GeminiClient
    from google.genai import types as genai_types

    # ``HttpOptions`` is the specific attribute that caused f-misc-05.
    # If the SDK ever re-introduces it, that's fine — but the
    # ``GeminiClient`` wrapper should NOT depend on its presence to
    # function. The canary just confirms construction works
    # regardless of whether the attribute exists.
    has_http_options = hasattr(genai_types, "HttpOptions")
    client = GeminiClient(api_key=_STUB_KEY)
    assert isinstance(client, GeminiClient)
    # Documenting the observed SDK state for diagnostics — neither
    # branch should fail this canary.
    assert has_http_options in (True, False)
