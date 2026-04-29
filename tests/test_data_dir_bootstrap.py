"""Unit tests for :func:`biotech_sniper.paths.ensure_data_dir` (f-misc-01).

The helper is the single source-of-truth for "make sure ``DATA_DIR``
exists before this module writes inside it". The contract this test
file locks in:

* The function is idempotent — calling it twice in a row is a no-op
  on the second call.
* The function returns :data:`DATA_DIR` as a :class:`pathlib.Path`.
* The function creates the entire directory tree (``parents=True``)
  when the parent of ``DATA_DIR`` is also missing.
* Importing :mod:`biotech_sniper.paths` does NOT create ``DATA_DIR``
  as a side-effect (the helper is opt-in; only writer entrypoints
  call it).
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest


def _reload_paths():
    """Reload paths so module-level constants pick up env changes."""
    if "biotech_sniper.paths" in sys.modules:
        return importlib.reload(sys.modules["biotech_sniper.paths"])
    return importlib.import_module("biotech_sniper.paths")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test starts with BIOTECH_SNIPER_HOME unset by default."""
    monkeypatch.delenv("BIOTECH_SNIPER_HOME", raising=False)
    yield


def test_ensure_data_dir_returns_data_dir_as_path(monkeypatch, tmp_path):
    """Return value is :data:`DATA_DIR`, typed as ``pathlib.Path``."""
    monkeypatch.setenv("BIOTECH_SNIPER_HOME", str(tmp_path))
    paths = _reload_paths()

    result = paths.ensure_data_dir()

    assert isinstance(result, Path)
    assert result == paths.DATA_DIR
    assert result == tmp_path / "data"


def test_ensure_data_dir_creates_dir_tree_when_absent(monkeypatch, tmp_path):
    """Helper creates the full DATA_DIR tree on first call."""
    # Use a deeply-nested home so even the parent of DATA_DIR is
    # missing — this exercises the ``parents=True`` flag.
    nested_home = tmp_path / "deep" / "nested" / "home"
    monkeypatch.setenv("BIOTECH_SNIPER_HOME", str(nested_home))
    paths = _reload_paths()

    expected = nested_home / "data"
    assert not expected.exists(), "precondition: DATA_DIR must be absent"
    assert not nested_home.exists(), "precondition: BASE_DIR must be absent"

    result = paths.ensure_data_dir()

    assert expected.exists(), "ensure_data_dir() should have created DATA_DIR"
    assert expected.is_dir()
    assert result == expected


def test_ensure_data_dir_is_idempotent(monkeypatch, tmp_path):
    """Repeated calls are no-ops once DATA_DIR exists."""
    monkeypatch.setenv("BIOTECH_SNIPER_HOME", str(tmp_path))
    paths = _reload_paths()

    first = paths.ensure_data_dir()
    # Drop a sentinel file inside DATA_DIR; a non-idempotent
    # implementation that re-created the directory (or rmtree'd
    # before mkdir) would wipe this file. The contract is that the
    # second call is a strict no-op on the filesystem.
    sentinel = first / "sentinel.txt"
    sentinel.write_text("preserved", encoding="utf-8")

    second = paths.ensure_data_dir()

    assert first == second
    assert sentinel.exists(), "second call must not delete existing contents"
    assert sentinel.read_text(encoding="utf-8") == "preserved"


def test_ensure_data_dir_no_op_when_present(monkeypatch, tmp_path):
    """Calling on an already-existing DATA_DIR succeeds without error."""
    monkeypatch.setenv("BIOTECH_SNIPER_HOME", str(tmp_path))
    paths = _reload_paths()

    # Pre-create DATA_DIR so the helper has nothing to do.
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    pre_stat = paths.DATA_DIR.stat()

    # The helper must not raise FileExistsError or recreate the dir.
    result = paths.ensure_data_dir()

    assert result == paths.DATA_DIR
    assert result.exists()
    # inode should be unchanged — a destructive recreate would
    # produce a new inode.
    post_stat = paths.DATA_DIR.stat()
    assert pre_stat.st_ino == post_stat.st_ino


def test_importing_paths_does_not_create_data_dir(tmp_path):
    """``import biotech_sniper.paths`` is side-effect-free on disk.

    Spawn a fresh interpreter with ``BIOTECH_SNIPER_HOME`` pointed at
    an empty ``tmp_path`` (no ``data/`` subdir). Import the module,
    then assert that ``DATA_DIR`` STILL does not exist. This is the
    contract that prevents :mod:`paths` from ever leaking writes
    into operator/test environments at import time.
    """
    home = tmp_path / "fresh_home"
    home.mkdir()
    expected_data = home / "data"
    assert not expected_data.exists()

    # Use a subprocess so the import is a true cold import (the
    # current pytest interpreter has already imported
    # ``biotech_sniper.paths`` indirectly, which would mask any
    # import-time mkdir bug).
    code = (
        "import os, sys\n"
        f"os.environ['BIOTECH_SNIPER_HOME'] = {str(home)!r}\n"
        "import biotech_sniper.paths as p\n"
        "from pathlib import Path\n"
        f"assert Path(p.DATA_DIR) == Path({str(expected_data)!r}), p.DATA_DIR\n"
        "assert not Path(p.DATA_DIR).exists(), (\n"
        "    f'paths import created DATA_DIR={p.DATA_DIR}'\n"
        ")\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"subprocess failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # And, just to be doubly sure, the directory still does not
    # exist on the parent's filesystem view either.
    assert not expected_data.exists(), (
        f"DATA_DIR was created as a side-effect of import: {expected_data}"
    )


def test_ensure_data_dir_listed_in_module_all(monkeypatch, tmp_path):
    """``ensure_data_dir`` is part of the public API surface."""
    monkeypatch.setenv("BIOTECH_SNIPER_HOME", str(tmp_path))
    paths = _reload_paths()

    assert "ensure_data_dir" in paths.__all__
    assert callable(paths.ensure_data_dir)
