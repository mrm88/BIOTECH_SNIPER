"""Pytest-wide fixtures for the biotech_sniper test suite.

The single fixture defined here is an autouse `_neutralize_dotenv` that
stubs out :func:`dotenv.load_dotenv` for the duration of every test.

Why this matters
----------------
:mod:`biotech_sniper.config` calls ``load_dotenv()`` at import time. When
the test suite runs on a host where a ``.env`` file exists (notably the
VPS at ``/root/alpha_sniper/.env``), reloading ``config`` inside a test
would otherwise re-populate environment variables that the same test had
just cleared via ``monkeypatch.delenv``. That re-population is invisible
to ``monkeypatch`` (it is a direct ``os.environ`` mutation by
python-dotenv), so the polluted values leak into every subsequent test.

Net effect: 3 tests in ``test_config.py`` and 1 in ``test_build_report.py``
fail on the VPS but pass locally where no ``.env`` file is present.
Stubbing ``dotenv.load_dotenv`` to a no-op for every test eliminates the
source of pollution while leaving production code unchanged.

websockets.legacy DeprecationWarning pre-emption
------------------------------------------------
Feature ``f-misc-02-websockets-legacy-deprecation``: ``alpaca-py==0.34.0``
imports ``websockets.legacy`` at the top of ``alpaca.trading.stream``,
and ``websockets==14.2`` emits a one-shot ``DeprecationWarning`` from
its ``legacy/__init__.py`` module body the first time that module is
loaded. The smoke-import coverage in ``test_network_whitelist`` and the
migration tests then surface the warning during ``-n 2`` runs (and
upgrade it to an error under ``-W error::DeprecationWarning``).

We cannot bump ``alpaca-py`` in this scoped feature without re-recording
cassettes, so we pre-import ``websockets.legacy`` here under a single
:func:`warnings.catch_warnings` block. The module body runs exactly once
per Python process; subsequent ``import websockets.legacy`` calls are
``sys.modules`` cache hits that never re-execute the ``warnings.warn``
line. The filter is scoped to this single import (one specific
``DeprecationWarning`` whose message contains ``websockets.legacy``) and
torn down immediately on exit, so no other warnings are silenced.
"""

from __future__ import annotations

import warnings

import pytest

# Targeted pre-import: consume the one-shot websockets.legacy
# DeprecationWarning before any test triggers an alpaca-py import. The
# filter is scoped to message=".*websockets\.legacy.*" so a sibling
# DeprecationWarning would still propagate. Errors loading the module
# (e.g. websockets uninstalled) are silently ignored — the only
# guarantee we need is that *if* it loads, no warning surfaces from it.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r".*websockets\.legacy is deprecated.*",
        category=DeprecationWarning,
    )
    try:
        import websockets.legacy  # noqa: F401  # pre-emptive load
    except Exception:  # pragma: no cover - websockets always installed
        pass


@pytest.fixture(autouse=True)
def _neutralize_dotenv(monkeypatch):
    """Disable python-dotenv side effects for every test.

    Some ``config`` tests reload the module mid-test; without this stub,
    ``load_dotenv()`` would re-set env vars that ``monkeypatch.delenv``
    just cleared, breaking the test's assumption about an empty
    environment. The stub is undone automatically when ``monkeypatch``
    tears down at end-of-test.
    """
    try:
        import dotenv  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - dotenv is a hard dep
        return
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_a, **_kw: False)
