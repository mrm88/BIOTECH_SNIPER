"""Runtime guard: yfinance must NOT be importable in the project venv.

f-misc-13 hardened the M3 yfinance teardown (VAL-M3-012 + f-m3-22) by
also clearing the dead-weight ``yfinance`` package out of the runtime
environment. ``pip install --upgrade -r requirements.txt`` does not
uninstall packages that have been removed from the manifest, which let
the dead dependency persist on the VPS ``.venv`` long after every code
reference was scrubbed in M3.

This test pins the runtime contract: in any ``.venv`` provisioned from
the canonical ``requirements.txt`` (which no longer pins yfinance),
``import yfinance`` must raise ``ModuleNotFoundError``. Stale venvs
that retained the package are caught by this test and the fix is to
run ``.venv/bin/pip uninstall -y yfinance`` (or rebuild the venv).

The test's source-level companion is :mod:`tests.test_no_yfinance`,
which gates the codebase + ``requirements.txt`` for source references.
Together they cover both layers: no source mention AND not importable
at runtime.
"""

from __future__ import annotations

import importlib
import importlib.util


def test_yfinance_is_not_importable_in_runtime_env() -> None:
    """``import yfinance`` must raise ``ModuleNotFoundError`` in the venv.

    Uses ``importlib.util.find_spec`` (rather than a bare ``import``) so
    the failure message can include the offending location of the stale
    install, which makes the cleanup obvious.
    """
    spec = importlib.util.find_spec("yfinance")
    assert spec is None, (
        "yfinance is still installed in this venv but was removed from "
        "requirements.txt in M3 (VAL-M3-012). Run "
        "`.venv/bin/pip uninstall -y yfinance` to clear the dead "
        f"weight. Stale install located at: {getattr(spec, 'origin', '?')!r}"
    )


def test_yfinance_import_raises_module_not_found() -> None:
    """Belt-and-suspenders: an actual ``import yfinance`` must raise.

    A package may exist as a metadata-only stub without an importable
    module (rare, but the find_spec check above can still tolerate it).
    Exercising the real import path closes that gap.
    """
    try:
        importlib.import_module("yfinance")
    except ModuleNotFoundError:
        return  # expected — yfinance is absent
    raise AssertionError(
        "yfinance imported successfully but the project no longer "
        "depends on it. Uninstall it from the venv to keep the "
        "runtime environment aligned with requirements.txt."
    )
