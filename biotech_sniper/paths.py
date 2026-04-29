"""Centralized path resolution for the biotech_sniper package.

`BASE_DIR` is resolved in this priority order:

1. The ``BIOTECH_SNIPER_HOME`` environment variable, if set and non-empty.
2. The repo root (the directory that *contains* the ``biotech_sniper``
   package), used as the fallback so that local development and tests
   work without any environment configuration. The fallback intentionally
   resolves to the repo root and **not** the package directory itself —
   the package directory is an implementation detail; sibling artefacts
   such as ``state/``, ``reports/``, ``archive/``, ``migrations/`` and
   the ``.env`` file all live at the repo root.

All other exported paths are derived from ``BASE_DIR`` and are returned as
``pathlib.Path`` objects. Importing this module never creates directories
on disk; consumers are responsible for ``mkdir(parents=True, exist_ok=True)``
when they need to write inside one of the derived locations. The
single-source-of-truth helper for the ``DATA_DIR`` case is
:func:`ensure_data_dir` — it is idempotent and returns ``DATA_DIR`` as a
``Path``. Callers MUST NOT mkdir anything at module-import time of this
file (the no-side-effect contract is locked in by
``tests/test_data_dir_bootstrap.py``).

Importers should always go through this module instead of constructing
absolute paths inline. The legacy hardcoded sandbox base directory used
by the original reference project must never appear in source code.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "BASE_DIR",
    "STATE_DIR",
    "REPORTS_DIR",
    "DATA_DIR",
    "LOGS_DIR",
    "DEFAULT_VPS_LOG_DIR",
    "ensure_data_dir",
]


def _resolve_base_dir() -> Path:
    """Return the project base directory.

    Reads ``BIOTECH_SNIPER_HOME`` from the environment and falls back to the
    directory that contains this file when the variable is unset or empty.
    The returned path is fully resolved (symlinks expanded, ``~`` expanded).
    """

    env_value = os.environ.get("BIOTECH_SNIPER_HOME")
    if env_value:
        # Preserve the user-provided path literally (only expanding ``~``).
        # We deliberately do NOT call ``resolve()`` here so the printed value
        # matches what the operator set in their environment (e.g. ``/tmp/abc``
        # on Linux stays ``/tmp/abc`` rather than dereferencing symlinks).
        return Path(env_value).expanduser()
    # paths.py lives at ``<repo_root>/biotech_sniper/paths.py``. The
    # ``biotech_sniper`` package is a child of the repo root, so the
    # repo root is two levels up from this file. Returning the repo
    # root keeps sibling artefacts (``state/``, ``reports/``,
    # ``archive/``, ``migrations/``, ``.env``) reachable from
    # ``BASE_DIR`` without any environment configuration.
    return Path(__file__).resolve().parent.parent


BASE_DIR: Path = _resolve_base_dir()
STATE_DIR: Path = BASE_DIR / "state"
REPORTS_DIR: Path = BASE_DIR / "reports"
DATA_DIR: Path = BASE_DIR / "data"
LOGS_DIR: Path = BASE_DIR / "logs"

#: Default VPS log directory used by ``logging_setup.py``. Centralized
#: here so that ``biotech_sniper/`` contains no other absolute system
#: paths — ``paths.py`` is the sole source of truth for filesystem
#: anchors. Overridable via the ``ALPHA_SNIPER_LOG_DIR`` environment
#: variable (see ``logging_setup._resolve_log_path``).
DEFAULT_VPS_LOG_DIR: Path = Path("/var/log/alpha_sniper")


def ensure_data_dir() -> Path:
    """Ensure :data:`DATA_DIR` exists on disk and return it.

    Single source-of-truth helper that callers should invoke at the
    first-write call site (or at module import, when the module
    *only* runs as a writer entrypoint) BEFORE opening any file
    under :data:`DATA_DIR`. The helper is idempotent — repeated
    calls are cheap because :py:meth:`pathlib.Path.mkdir` with
    ``exist_ok=True`` is a no-op when the directory is already
    present.

    Notes
    -----
    * **Never call this at import time of :mod:`biotech_sniper.paths`.**
      Importing the paths module must remain side-effect-free
      (creating directories on disk as a side-effect of any import
      would surprise tests that probe a tmp ``BIOTECH_SNIPER_HOME``
      and assert that nothing is created until a writer runs). The
      no-import-side-effect contract is exercised by
      ``tests/test_data_dir_bootstrap.py``.
    * The function returns :data:`DATA_DIR` so callers can use it
      directly in a path expression — e.g.
      ``db_path = ensure_data_dir() / "alpha_sniper.db"`` — without
      a separate import.

    Returns
    -------
    pathlib.Path
        The (now-guaranteed-to-exist) :data:`DATA_DIR`.
    """

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
