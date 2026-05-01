"""Schema-migration runner.

Centralised, forward-only migration runner for the Biotech Sniper
SQLite database. Applies versioned migrations (v9 → v10 today,
v10 → v11 in the future) atomically with an automatic pre-migration
backup and explicit downgrade refusal.

Public surface
--------------

* :class:`DowngradeForbidden` — raised when ``--target`` is lower than
  the current ``schema_version``.
* :class:`MigrationError` — wraps any SQLite or migration-script
  failure with the original cause.
* :func:`run` — programmatic entry point.
* ``python -m biotech_sniper.migrations.runner`` — CLI entry point.

CLI
---

::

    python -m biotech_sniper.migrations.runner --db PATH --target 10

Flags:

* ``--db PATH``        — target SQLite database (default:
                         ``$BIOTECH_SNIPER_HOME/data/alpha_sniper.db``).
* ``--target VERSION`` — target schema version (required).
* ``--no-backup``      — skip the pre-migration backup (test-only;
                         the production VPS deploy MUST always back
                         up — VAL-M1-046).
* ``--backup-dir DIR`` — override the backup destination
                         (default: ``$BIOTECH_SNIPER_HOME/backups``).
* ``--check``          — print the current schema_version and target
                         without applying any migration.

Exit codes:

* ``0`` — migration applied or no-op (target ≤ current).
* ``2`` — generic failure (SQLite error, IO error, etc.).
* ``3`` — :class:`DowngradeForbidden` (target < current).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import shutil
import sqlite3
import stat
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Final

from biotech_sniper import db
from biotech_sniper.paths import BASE_DIR, DATA_DIR

__all__ = [
    "DowngradeForbidden",
    "MigrationError",
    "EXIT_OK",
    "EXIT_FAILURE",
    "EXIT_DOWNGRADE",
    "run",
    "main",
    "load_migration",
    "default_backup_dir",
    "default_db_path",
    "take_backup",
]


logger = logging.getLogger(__name__)


EXIT_OK: Final[int] = 0
EXIT_FAILURE: Final[int] = 2
EXIT_DOWNGRADE: Final[int] = 3


# Backup file mode (rw-------; owner-only). Mirrors VAL-M1-046.
_BACKUP_FILE_MODE: Final[int] = 0o600


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class DowngradeForbidden(Exception):
    """Raised when the requested target version is lower than current.

    Reading-B migrations are forward-only by AGENTS.md policy:

        > schema_version migrations are forward-only. v9→v10 (M1
        > foundations) and possibly v10→v11 (M3 stage-2 tables).

    Operators who need to roll a deploy back must restore from the
    pre-migration backup file written by :func:`take_backup` rather
    than running a downgrade migration.
    """


class MigrationError(RuntimeError):
    """Wraps a SQLite or migration-script failure with the original cause."""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


_MIGRATIONS_DIR: Final[Path] = Path(__file__).resolve().parent


def load_migration(version: int) -> ModuleType:
    """Dynamically import the ``0NN_*.py`` migration module for ``version``.

    Migration files are named with a leading numeric prefix (e.g.
    ``010_reading_b_foundations.py``) which is not a legal Python
    module identifier — Python module names cannot start with a
    digit. We therefore use :mod:`importlib.util` to load the file
    by absolute path and assign it a synthetic module name
    (``_migration_v<N>``) inside :mod:`sys.modules` so the module
    is importable a second time with no extra work.

    Returns the loaded module. Raises :class:`FileNotFoundError`
    when no matching file is found and :class:`MigrationError`
    when the file exists but does not declare ``FROM_VERSION`` /
    ``TO_VERSION`` / ``apply``.
    """
    # Match either a 2-digit (e.g. ``10_*.py``) or 3-digit (e.g.
    # ``010_*.py``) prefix so the runner is forward-compatible with
    # whichever convention future migrations adopt.
    candidates = sorted(_MIGRATIONS_DIR.glob(f"{version:03d}_*.py")) + sorted(
        _MIGRATIONS_DIR.glob(f"{version:02d}_*.py")
    )
    candidates = [p for p in candidates if p.name != "__init__.py"]
    if not candidates:
        raise FileNotFoundError(
            f"No migration file found for target version {version} "
            f"in {_MIGRATIONS_DIR}"
        )
    path = candidates[0]
    module_name = f"_migration_v{version}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise MigrationError(
            f"Could not load migration spec for {path}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    for attr in ("FROM_VERSION", "TO_VERSION", "apply"):
        if not hasattr(module, attr):
            raise MigrationError(
                f"Migration {path.name} missing required attribute: {attr}"
            )
    if module.TO_VERSION != version:
        raise MigrationError(
            f"Migration {path.name} declares TO_VERSION="
            f"{module.TO_VERSION}, expected {version}"
        )
    return module


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


def default_backup_dir() -> Path:
    """Return the canonical backup directory under :data:`BASE_DIR`.

    The directory is NOT created here — :func:`take_backup` does the
    ``mkdir(parents=True, exist_ok=True)`` immediately before writing,
    matching the centralised ``ensure_*`` pattern in :mod:`paths`.
    """
    return BASE_DIR / "backups"


def default_db_path() -> Path:
    """Return the canonical production DB path under :data:`DATA_DIR`."""
    return DATA_DIR / "alpha_sniper.db"


def take_backup(
    db_path: Path,
    *,
    from_version: int,
    backup_dir: Path | None = None,
) -> Path:
    """Copy ``db_path`` to ``<backup_dir>/<dbname>.v<from_version>.<ts>``.

    Returns the absolute path of the backup file. Mode is set to
    ``0o600`` so the backup is owner-only readable (matches
    VAL-M1-046).

    The backup is performed via :func:`shutil.copy2` so file metadata
    (mtime, permissions) is preserved. The destination is then
    explicitly chmod'd to ``0o600`` so the backup is no looser than
    the canonical setting regardless of the source DB's permissions.

    Raises :class:`FileNotFoundError` if ``db_path`` does not exist.
    """
    if backup_dir is None:
        backup_dir = default_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)

    src = Path(db_path)
    if not src.exists():
        raise FileNotFoundError(f"Source database not found: {src}")

    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_name = f"{src.name}.v{from_version}.{timestamp}"
    dest = backup_dir / backup_name
    shutil.copy2(src, dest)
    try:
        os.chmod(dest, _BACKUP_FILE_MODE)
    except OSError:
        # Best-effort on read-only filesystems.
        pass

    # Write a sibling ``.sha256`` file so VAL-M1-046 can verify the
    # backup contents byte-for-byte against the source DB.
    try:
        digest = _sha256_file(src)
        (dest.parent / f"{backup_name}.sha256").write_text(
            f"{digest}  {backup_name}\n", encoding="utf-8"
        )
    except OSError:
        pass

    logger.info(
        "migration.backup_written src=%s dest=%s mode=%o",
        src,
        dest,
        _BACKUP_FILE_MODE,
    )
    return dest


def _sha256_file(path: Path, chunk_size: int = 1 << 16) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(
    db_path: str | Path,
    target_version: int,
    *,
    take_backup_first: bool = True,
    backup_dir: Path | None = None,
) -> dict[str, object]:
    """Apply migrations on ``db_path`` until ``schema_version=target_version``.

    Behaviour
    ---------

    * Opens a connection via :func:`biotech_sniper.db.connect` (FK
      enforcement + WAL on disk).
    * Calls :func:`db.run_migrations` to ensure the v9 baseline schema
      is in place. Idempotent for any pre-existing v9 / v10 db.
    * Reads the current ``schema_version``.
    * If ``target_version < current``: raises :class:`DowngradeForbidden`.
    * If ``target_version == current``: returns immediately (no-op).
    * If ``target_version > current``:
        1. Takes a backup unless ``take_backup_first=False``.
        2. For each ``v in (current+1 .. target_version)``:
            * Loads the migration module via :func:`load_migration`.
            * Asserts ``module.FROM_VERSION == current_at_step``.
            * Opens an explicit ``BEGIN``.
            * Calls ``module.apply(conn)``.
            * Inserts ``INSERT OR IGNORE INTO schema_version
              (version, description) VALUES (v, module.DESCRIPTION)``.
            * ``COMMIT`` on success, ``ROLLBACK`` on any exception.

    Returns a summary dict suitable for JSON-emit in CLI mode:

    .. code-block:: text

        {
            "db_path": "...",
            "from_version": 9,
            "to_version": 10,
            "applied": [10],
            "no_op": false,
            "backup_path": "/.../alpha_sniper.db.v9.20260429T120000Z" or null
        }

    Raises
    ------
    DowngradeForbidden
        When ``target_version`` is below the current ``schema_version``.
    MigrationError
        When loading or applying a migration fails. The migration's
        transaction has already been rolled back; ``schema_version``
        is unchanged.
    """
    db_path = Path(db_path)
    summary: dict[str, object] = {
        "db_path": str(db_path),
        "from_version": None,
        "to_version": target_version,
        "applied": [],
        "no_op": False,
        "backup_path": None,
    }

    conn = db.connect(db_path)
    try:
        # Ensure baseline v9 schema is in place before consulting the
        # version. This is idempotent on every db state we may
        # encounter — fresh, v8 (legacy), v9, or v10.
        #
        # f-misc-06: pin the baseline at ``target_version=9`` so that
        # bumping :data:`db.CURRENT_VERSION` to 10 (which makes the
        # default :func:`db.run_migrations` call dispatch all the way
        # to v10) does NOT collapse the runner's per-version
        # accounting (``summary['from_version']``, the ``alpha.db.v9.*``
        # backup name, the ``applied=[10]`` summary). The runner's
        # own dispatcher loop below remains the canonical path for
        # applying v10+ migrations under explicit ``run(...)``
        # invocations; ``db.run_migrations`` (called without an
        # explicit target) handles auto-bootstrap from any fresh
        # ``PaperExecutor()`` use site separately.
        db.run_migrations(conn, target_version=9)

        current = db.current_schema_version(conn)
        summary["from_version"] = current

        if target_version < current:
            raise DowngradeForbidden(
                f"Refusing to downgrade: target={target_version} < "
                f"current={current}. Restore from backup instead."
            )

        if target_version == current:
            summary["no_op"] = True
            return summary

        # Forward migration path. Take backup BEFORE applying any
        # migration so a mid-migration crash leaves a recoverable
        # snapshot.
        if take_backup_first and db_path.exists():
            backup_path = take_backup(
                db_path,
                from_version=current,
                backup_dir=backup_dir,
            )
            summary["backup_path"] = str(backup_path)

        # Apply each migration in order.
        applied: list[int] = []
        previous_isolation_level = conn.isolation_level
        conn.isolation_level = None
        try:
            for version in range(current + 1, target_version + 1):
                module = load_migration(version)
                if module.FROM_VERSION != version - 1:
                    raise MigrationError(
                        f"Migration v{version} declares FROM_VERSION="
                        f"{module.FROM_VERSION}, expected {version - 1}"
                    )
                conn.execute("BEGIN IMMEDIATE")
                try:
                    module.apply(conn)
                    description = getattr(
                        module, "DESCRIPTION", f"schema v{version}"
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO schema_version "
                        "(version, description) VALUES (?, ?)",
                        (version, description),
                    )
                    conn.execute("COMMIT")
                except Exception as exc:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    if isinstance(exc, DowngradeForbidden):
                        raise
                    raise MigrationError(
                        f"Migration v{version} failed: {exc!r}"
                    ) from exc
                applied.append(version)
                logger.info(
                    "migration.applied version=%d description=%s",
                    version,
                    description,
                )
        finally:
            conn.isolation_level = previous_isolation_level

        summary["applied"] = applied
        return summary
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.migrations.runner",
        description=(
            "Apply schema migrations atomically with an automatic "
            "pre-migration backup. Forward-only — refuses downgrade."
        ),
    )
    parser.add_argument(
        "--db",
        default=None,
        help=(
            "Path to the SQLite database (default: "
            "$BIOTECH_SNIPER_HOME/data/alpha_sniper.db)"
        ),
    )
    parser.add_argument(
        "--target",
        type=int,
        default=None,
        help=(
            "Target schema version (e.g. 10). Required unless "
            "--check is passed."
        ),
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help=(
            "Skip the pre-migration backup. Test-only; production "
            "deploys MUST always back up (VAL-M1-046)."
        ),
    )
    parser.add_argument(
        "--backup-dir",
        default=None,
        help=(
            "Override the backup directory "
            "(default: $BIOTECH_SNIPER_HOME/backups)."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Print the current schema_version and exit. No migration "
            "is applied."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else default_db_path()
    backup_dir = Path(args.backup_dir) if args.backup_dir else None

    if args.check:
        try:
            conn = db.connect(db_path)
            try:
                version = db.current_schema_version(conn)
            finally:
                conn.close()
            sys.stdout.write(
                json.dumps({"db_path": str(db_path), "version": version}) + "\n"
            )
            return EXIT_OK
        except Exception as exc:  # pragma: no cover - rare path
            sys.stderr.write(f"check failed: {exc!r}\n")
            return EXIT_FAILURE

    if args.target is None:
        parser.error("--target is required (or pass --check)")

    try:
        summary = run(
            db_path,
            args.target,
            take_backup_first=not args.no_backup,
            backup_dir=backup_dir,
        )
    except DowngradeForbidden as exc:
        sys.stderr.write(f"DowngradeForbidden: {exc}\n")
        return EXIT_DOWNGRADE
    except (MigrationError, sqlite3.Error, OSError) as exc:
        sys.stderr.write(f"migration failed: {exc!r}\n")
        return EXIT_FAILURE

    sys.stdout.write(json.dumps(summary, default=str) + "\n")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
