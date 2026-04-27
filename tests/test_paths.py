"""Unit tests for biotech_sniper.paths.

Covers:
* BASE_DIR resolves from the BIOTECH_SNIPER_HOME env var when set.
* BASE_DIR falls back to the directory containing paths.py when the env var
  is unset (or set to an empty string).
* All derived sub-paths (STATE_DIR, REPORTS_DIR, DATA_DIR, LOGS_DIR) are
  rooted under BASE_DIR.
* No legacy sandbox substring (``/home/user/workspace``) appears in any
  resolved path.
* All exports are ``pathlib.Path`` instances.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest


def _reload_paths():
    """Reload the paths module so module-level constants pick up env changes."""
    if "biotech_sniper.paths" in sys.modules:
        return importlib.reload(sys.modules["biotech_sniper.paths"])
    return importlib.import_module("biotech_sniper.paths")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Ensure each test starts without BIOTECH_SNIPER_HOME set."""
    monkeypatch.delenv("BIOTECH_SNIPER_HOME", raising=False)
    yield


def test_base_dir_uses_env_var_when_set(monkeypatch, tmp_path):
    """BASE_DIR honours BIOTECH_SNIPER_HOME when the env var is non-empty."""
    custom_home = tmp_path / "custom_home"
    custom_home.mkdir()
    monkeypatch.setenv("BIOTECH_SNIPER_HOME", str(custom_home))

    paths = _reload_paths()

    assert paths.BASE_DIR == Path(str(custom_home))
    assert str(paths.BASE_DIR) == str(custom_home)


def test_base_dir_preserves_literal_env_value(monkeypatch):
    """An env value like ``/tmp/abc`` is preserved verbatim (no symlink resolution)."""
    monkeypatch.setenv("BIOTECH_SNIPER_HOME", "/tmp/abc")

    paths = _reload_paths()

    assert str(paths.BASE_DIR) == "/tmp/abc"


def test_base_dir_falls_back_to_file_when_env_unset(monkeypatch):
    """BASE_DIR falls back to the directory containing paths.py."""
    monkeypatch.delenv("BIOTECH_SNIPER_HOME", raising=False)

    paths = _reload_paths()

    expected = Path(paths.__file__).resolve().parent
    assert paths.BASE_DIR == expected


def test_empty_env_var_falls_back_to_file(monkeypatch):
    """An empty BIOTECH_SNIPER_HOME is treated as unset (falsy)."""
    monkeypatch.setenv("BIOTECH_SNIPER_HOME", "")

    paths = _reload_paths()

    expected = Path(paths.__file__).resolve().parent
    assert paths.BASE_DIR == expected


def test_derived_subpaths_rooted_under_base_dir(monkeypatch, tmp_path):
    """STATE_DIR, REPORTS_DIR, DATA_DIR, LOGS_DIR are rooted under BASE_DIR."""
    base = tmp_path / "root"
    base.mkdir()
    monkeypatch.setenv("BIOTECH_SNIPER_HOME", str(base))

    paths = _reload_paths()

    assert paths.STATE_DIR == base.resolve() / "state"
    assert paths.REPORTS_DIR == base.resolve() / "reports"
    assert paths.DATA_DIR == base.resolve() / "data"
    assert paths.LOGS_DIR == base.resolve() / "logs"
    for sub in (paths.STATE_DIR, paths.REPORTS_DIR, paths.DATA_DIR, paths.LOGS_DIR):
        assert paths.BASE_DIR in sub.parents


def test_all_exports_are_pathlib_path(monkeypatch, tmp_path):
    """Each export is a pathlib.Path instance, not a string."""
    monkeypatch.setenv("BIOTECH_SNIPER_HOME", str(tmp_path))

    paths = _reload_paths()

    for name in ("BASE_DIR", "STATE_DIR", "REPORTS_DIR", "DATA_DIR", "LOGS_DIR"):
        value = getattr(paths, name)
        assert isinstance(value, Path), f"{name} should be a pathlib.Path, got {type(value)!r}"


def test_no_sandbox_substring_in_resolved_paths(monkeypatch):
    """The legacy /home/user/workspace substring must never appear in resolved paths."""
    # Test once with env unset (fallback branch)...
    monkeypatch.delenv("BIOTECH_SNIPER_HOME", raising=False)
    paths = _reload_paths()
    for name in ("BASE_DIR", "STATE_DIR", "REPORTS_DIR", "DATA_DIR", "LOGS_DIR"):
        resolved = os.path.realpath(getattr(paths, name))
        assert "/home/user/workspace" not in resolved, f"{name} contains the legacy sandbox path: {resolved}"


def test_paths_module_exports_required_names():
    """Module exposes all five required path names at minimum."""
    paths = _reload_paths()

    for name in ("BASE_DIR", "STATE_DIR", "REPORTS_DIR", "DATA_DIR", "LOGS_DIR"):
        assert hasattr(paths, name), f"paths module is missing required export: {name}"
