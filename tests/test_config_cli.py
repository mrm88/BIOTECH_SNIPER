"""Tests for the ``biotech_sniper.config`` CLI subcommands.

Covers the ``check_env`` and ``dump_env_vars`` entry points added by
f-cross-02-final-verification (commit 6244c01) and the
``REQUIRED_ENV_VARS`` constant they consume.

The tests live in two flavours:

* In-process tests that import ``biotech_sniper.config`` and call the
  ``_cli_check_env`` / ``_cli_dump_env_vars`` helpers directly — fast,
  hermetic, and let ``monkeypatch`` control ``os.environ`` precisely.
* A subprocess test that exercises ``python -m biotech_sniper.config
  <subcommand>`` end-to-end via ``.venv/bin/python`` to confirm the
  module-level ``__main__`` dispatcher wires the helpers together
  correctly (this is the surface the validation contract probes via
  VAL-CROSS-013/014/015).

All subprocess invocations run with ``capture_output=True`` and a
short ``timeout`` so a misbehaving CLI cannot hang the suite.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from biotech_sniper.config import (
    REQUIRED_ENV_VARS,
    _cli_check_env,
    _cli_dump_env_vars,
)

# ---------------------------------------------------------------------------
# Test harness helpers.
# ---------------------------------------------------------------------------

# Repo root resolved relative to this test file so the subprocess test does
# not depend on the cwd of the pytest invocation.
REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"


def _all_required_env_set(monkeypatch):
    """Populate every REQUIRED_ENV_VAR with a non-blank placeholder."""
    for name in REQUIRED_ENV_VARS:
        monkeypatch.setenv(name, f"test-value-for-{name}")


# ---------------------------------------------------------------------------
# REQUIRED_ENV_VARS shape — guards against silent additions/removals.
# ---------------------------------------------------------------------------


def test_required_env_vars_expected_set():
    """REQUIRED_ENV_VARS must list the 8 keys documented in .env.example.

    The set is the source of truth consumed by ``check_env`` /
    ``dump_env_vars``; if it drifts away from ``.env.example`` the
    VAL-CROSS-013 diff fails. Pin the exact membership so any future
    addition forces an explicit code change here.
    """
    expected = {
        "ALPACA_BASE_URL",
        "ALPACA_KEY_ID",
        "ALPACA_SECRET_KEY",
        "ANTHROPIC_API_KEY",
        "BIOTECH_SNIPER_HOME",
        "GEMINI_API_KEY",
        "LIVE_MODE",
        "XAI_API_KEY",
    }
    assert set(REQUIRED_ENV_VARS) == expected
    assert len(REQUIRED_ENV_VARS) == 8
    # Tuple is alphabetically sorted (callers diff against `sort`).
    assert list(REQUIRED_ENV_VARS) == sorted(REQUIRED_ENV_VARS)


def test_required_env_vars_matches_dotenv_example():
    """REQUIRED_ENV_VARS must match the key-set documented in .env.example.

    Mirrors the VAL-CROSS-013 diff: ``dump_env_vars`` output (sorted)
    must equal the set of ``^[A-Z][A-Z0-9_]+`` keys parsed out of
    ``.env.example``. This protects against forgetting to update
    either side after adding a new env var.
    """
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    keys_from_file = set(
        re.findall(r"(?m)^([A-Z][A-Z0-9_]+)=", env_example)
    )
    assert keys_from_file == set(REQUIRED_ENV_VARS), (
        f"REQUIRED_ENV_VARS / .env.example drifted: "
        f"only-in-required={set(REQUIRED_ENV_VARS) - keys_from_file}, "
        f"only-in-dotenv={keys_from_file - set(REQUIRED_ENV_VARS)}"
    )


# ---------------------------------------------------------------------------
# check_env — happy path.
# ---------------------------------------------------------------------------


def test_check_env_ok_when_all_set(monkeypatch, capsys):
    """All REQUIRED_ENV_VARS set → exit 0 and stdout contains 'OK'."""
    _all_required_env_set(monkeypatch)
    rc = _cli_check_env()
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "OK"
    assert captured.err == ""


# ---------------------------------------------------------------------------
# check_env — missing-var failure mode.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing_var", list(REQUIRED_ENV_VARS))
def test_check_env_missing_var_exits_nonzero(monkeypatch, capsys, missing_var):
    """Removing any single REQUIRED_ENV_VAR must exit non-zero with a
    'ERROR: missing required env var: <NAME>' line on stderr.

    Parameterised across every required var so every entry in the
    tuple is exercised at least once.
    """
    _all_required_env_set(monkeypatch)
    monkeypatch.delenv(missing_var, raising=False)

    rc = _cli_check_env()
    captured = capsys.readouterr()

    assert rc >= 1, "missing required var must produce non-zero exit code"
    assert (
        f"ERROR: missing required env var: {missing_var}" in captured.err
    ), f"expected stderr to name {missing_var}, got: {captured.err!r}"
    # Stdout must NOT contain the success token.
    assert "OK" not in captured.out


def test_check_env_blank_value_treated_as_missing(monkeypatch, capsys):
    """A blank/whitespace-only value must be treated as missing — the
    helper strips before checking truthiness so stale .env entries
    like ``XAI_API_KEY=`` cannot mask a missing secret.
    """
    _all_required_env_set(monkeypatch)
    monkeypatch.setenv("XAI_API_KEY", "   ")  # whitespace-only
    rc = _cli_check_env()
    captured = capsys.readouterr()
    assert rc >= 1
    assert "missing required env var: XAI_API_KEY" in captured.err


def test_check_env_lists_every_missing_var(monkeypatch, capsys):
    """When several required vars are missing each one is named on its
    own ``ERROR: missing required env var: <NAME>`` line — operators
    must not have to re-run the command N times to discover each gap.
    """
    _all_required_env_set(monkeypatch)
    for victim in ("XAI_API_KEY", "ALPACA_KEY_ID"):
        monkeypatch.delenv(victim, raising=False)

    rc = _cli_check_env()
    captured = capsys.readouterr()

    assert rc >= 1
    assert "missing required env var: XAI_API_KEY" in captured.err
    assert "missing required env var: ALPACA_KEY_ID" in captured.err


# ---------------------------------------------------------------------------
# dump_env_vars
# ---------------------------------------------------------------------------


def test_dump_env_vars_prints_full_sorted_list(capsys):
    """``dump_env_vars`` must print every REQUIRED_ENV_VAR, one per
    line, in alphabetical order, and exit 0 — the CLI surface is
    designed for shell-piping into ``sort | diff`` (VAL-CROSS-013).
    """
    rc = _cli_dump_env_vars()
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.err == ""

    lines = captured.out.strip().splitlines()
    assert lines == list(REQUIRED_ENV_VARS)
    # Already-sorted by construction; keep the assertion explicit so a
    # future re-ordering of REQUIRED_ENV_VARS surfaces here.
    assert lines == sorted(lines)


def test_dump_env_vars_matches_dotenv_example_keys(capsys):
    """The output of ``dump_env_vars`` (sorted) must equal the set of
    keys parsed out of ``.env.example`` — this is the in-process
    equivalent of the VAL-CROSS-013 ``diff`` evidence.
    """
    rc = _cli_dump_env_vars()
    captured = capsys.readouterr()
    assert rc == 0

    dump_keys = set(captured.out.split())
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    file_keys = set(re.findall(r"(?m)^([A-Z][A-Z0-9_]+)=", env_example))

    assert dump_keys == file_keys


# ---------------------------------------------------------------------------
# End-to-end subprocess invocation — confirms __main__ dispatcher works.
# ---------------------------------------------------------------------------


def _run_module(args, env):
    """Run ``python -m biotech_sniper.config <args>`` with the given env.

    Uses ``.venv/bin/python`` when available (the project venv has the
    required deps installed) and falls back to ``sys.executable`` for
    portability with developer machines that have a different venv
    layout. ``timeout=10`` keeps a misbehaving CLI from hanging the
    suite.
    """
    interpreter = str(VENV_PYTHON) if VENV_PYTHON.is_file() else sys.executable
    return subprocess.run(
        [interpreter, "-m", "biotech_sniper.config", *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_subprocess_check_env_ok():
    """``python -m biotech_sniper.config check_env`` exits 0 with 'OK'
    on stdout when every required env var is provisioned."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        # Every required var populated with a placeholder value.
        **{name: f"placeholder-{name}" for name in REQUIRED_ENV_VARS},
    }
    result = _run_module(["check_env"], env=env)
    assert result.returncode == 0, (
        f"unexpected non-zero exit; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"
    assert result.stderr == ""


def test_subprocess_check_env_missing_var():
    """With at least one required env var missing, the CLI exits
    non-zero and prints a 'ERROR: missing required env var: <NAME>'
    line to stderr — covers the VAL-CROSS-015 evidence path."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        **{name: f"placeholder-{name}" for name in REQUIRED_ENV_VARS},
    }
    env.pop("XAI_API_KEY", None)

    result = _run_module(["check_env"], env=env)
    assert result.returncode != 0
    assert re.search(
        r"^ERROR: missing required env var: XAI_API_KEY$",
        result.stderr,
        re.MULTILINE,
    ), f"expected ERROR line for XAI_API_KEY, got stderr={result.stderr!r}"


def test_subprocess_dump_env_vars():
    """``python -m biotech_sniper.config dump_env_vars`` exits 0 and
    prints the full REQUIRED_ENV_VARS list, one per line, in
    alphabetical order — VAL-CROSS-013 evidence path."""
    env = {"PATH": os.environ.get("PATH", "")}
    result = _run_module(["dump_env_vars"], env=env)
    assert result.returncode == 0, (
        f"unexpected non-zero exit; stderr={result.stderr!r}"
    )
    lines = result.stdout.strip().splitlines()
    assert lines == list(REQUIRED_ENV_VARS)
    assert lines == sorted(lines)


def test_subprocess_unknown_subcommand_exits_nonzero():
    """An unknown subcommand must exit non-zero with a clear stderr
    message — guards against silent no-op behaviour if a typo'd
    subcommand reaches production."""
    env = {"PATH": os.environ.get("PATH", "")}
    result = _run_module(["not_a_real_subcommand"], env=env)
    assert result.returncode != 0
    assert "unknown subcommand" in result.stderr.lower()
