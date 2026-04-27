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
"""

from __future__ import annotations

import pytest


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
