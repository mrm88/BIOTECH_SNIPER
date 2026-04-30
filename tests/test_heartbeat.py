"""Tests for the f-m2-08 atomic heartbeat writer.

Covers the contract surface from VAL-M2-031 / VAL-M2-032 and the
expectedBehavior list of the f-m2-08 feature description:

* Heartbeat schema matches the locked spec (5 keys, types pinned).
* Atomic write via :func:`os.replace` (no partial visibility).
* Updated every poll cycle (mtime + ``last_poll_ts`` advance).
* Stale > 5 min triggers M4 watchdog alarm; the boundary at exactly
  300 s is "fresh", >300 s is stale.
* Round-trip of write → read returns an identical
  :class:`~biotech_sniper.news_daemon.heartbeat.Heartbeat`.
* :func:`resolve_version_sha` honours the
  :envvar:`BIOTECH_SNIPER_VERSION_SHA` override and returns a
  40-char lower-case hex string.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from biotech_sniper.news_daemon import heartbeat as hb_mod
from biotech_sniper.news_daemon.heartbeat import (
    DEFAULT_STALE_THRESHOLD_SECONDS,
    PLACEHOLDER_VERSION_SHA,
    Heartbeat,
    default_heartbeat_path,
    is_news_daemon_stale,
    read_heartbeat,
    resolve_version_sha,
    write_heartbeat,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


_FIXED_SHA = "abcd1234" * 5  # 40-char lower-case hex


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _make_heartbeat(**overrides) -> Heartbeat:
    defaults = dict(
        last_poll_ts=_now_iso(),
        candidates_emitted_total=10,
        candidates_emitted_session=2,
        errors_session=0,
        version_sha=_FIXED_SHA,
    )
    defaults.update(overrides)
    return Heartbeat(**defaults)


@pytest.fixture
def heartbeat_path(tmp_path: Path) -> Path:
    """Isolated heartbeat path that does NOT touch the repo state dir."""

    return tmp_path / "news_daemon_heartbeat.json"


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_module_exports_required_symbols() -> None:
    """The public API includes every f-m2-08 contract symbol."""

    for symbol in (
        "Heartbeat",
        "write_heartbeat",
        "read_heartbeat",
        "is_news_daemon_stale",
        "resolve_version_sha",
        "default_heartbeat_path",
        "DEFAULT_STALE_THRESHOLD_SECONDS",
        "PLACEHOLDER_VERSION_SHA",
    ):
        assert hasattr(hb_mod, symbol), f"missing public symbol {symbol}"


def test_default_stale_threshold_is_300_seconds() -> None:
    """M4 watchdog alarms after 5 minutes (300 s)."""

    assert DEFAULT_STALE_THRESHOLD_SECONDS == 300


def test_default_heartbeat_path_uses_state_dir(monkeypatch, tmp_path) -> None:
    """``default_heartbeat_path`` resolves under :data:`paths.STATE_DIR`.

    ``default_heartbeat_path`` performs a deferred import of
    :mod:`biotech_sniper.paths` so we only need to reload that
    sibling module to pick up the env override — the heartbeat
    module itself is left untouched, which avoids invalidating the
    ``Heartbeat`` class identity for the rest of the test session.
    """

    import importlib

    from biotech_sniper import paths as paths_mod

    monkeypatch.setenv("BIOTECH_SNIPER_HOME", str(tmp_path))
    try:
        importlib.reload(paths_mod)
        resolved = hb_mod.default_heartbeat_path()
        assert resolved == tmp_path / "state" / "news_daemon_heartbeat.json"
    finally:
        # Restore the package-level paths state so subsequent tests
        # see the canonical (unset-BIOTECH_SNIPER_HOME) values.
        monkeypatch.delenv("BIOTECH_SNIPER_HOME", raising=False)
        importlib.reload(paths_mod)


# ---------------------------------------------------------------------------
# Schema / round-trip
# ---------------------------------------------------------------------------


def test_heartbeat_schema_keys_match_spec(heartbeat_path: Path) -> None:
    """The on-disk JSON contains exactly the five locked-spec keys."""

    write_heartbeat(_make_heartbeat(), heartbeat_path)
    data = json.loads(heartbeat_path.read_text(encoding="utf-8"))

    expected_keys = {
        "last_poll_ts",
        "candidates_emitted_total",
        "candidates_emitted_session",
        "errors_session",
        "version_sha",
    }
    assert set(data.keys()) == expected_keys


def test_heartbeat_field_types_match_spec(heartbeat_path: Path) -> None:
    """Counters are integers; ``version_sha`` is a 40-char string."""

    write_heartbeat(_make_heartbeat(), heartbeat_path)
    data = json.loads(heartbeat_path.read_text(encoding="utf-8"))

    assert isinstance(data["last_poll_ts"], str)
    assert isinstance(data["candidates_emitted_total"], int)
    assert isinstance(data["candidates_emitted_session"], int)
    assert isinstance(data["errors_session"], int)
    assert isinstance(data["version_sha"], str)
    assert len(data["version_sha"]) == 40


def test_round_trip_preserves_payload(heartbeat_path: Path) -> None:
    """write → read returns the same dataclass instance."""

    payload = _make_heartbeat(
        candidates_emitted_total=12345,
        candidates_emitted_session=42,
        errors_session=3,
    )
    write_heartbeat(payload, heartbeat_path)
    loaded = read_heartbeat(heartbeat_path)
    assert loaded == payload


def test_write_accepts_mapping(heartbeat_path: Path) -> None:
    """Plain dicts with the required keys round-trip equivalently."""

    payload = {
        "last_poll_ts": _now_iso(),
        "candidates_emitted_total": 1,
        "candidates_emitted_session": 1,
        "errors_session": 0,
        "version_sha": _FIXED_SHA,
    }
    write_heartbeat(payload, heartbeat_path)
    loaded = read_heartbeat(heartbeat_path)
    assert loaded.candidates_emitted_total == 1
    assert loaded.version_sha == _FIXED_SHA


def test_write_rejects_payload_missing_required_keys(heartbeat_path: Path) -> None:
    """Missing keys raise ``ValueError`` rather than writing partial."""

    with pytest.raises(ValueError) as excinfo:
        write_heartbeat({"last_poll_ts": _now_iso()}, heartbeat_path)
    assert "missing required keys" in str(excinfo.value)
    assert not heartbeat_path.exists(), "no partial file on validation failure"


# ---------------------------------------------------------------------------
# Atomicity (os.replace)
# ---------------------------------------------------------------------------


def test_write_uses_os_replace_via_tmp_sibling(
    heartbeat_path: Path, monkeypatch
) -> None:
    """The writer goes through a ``.tmp`` sibling + :func:`os.replace`."""

    real_replace = os.replace
    calls: list[tuple[str, str]] = []

    def spy_replace(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(hb_mod.os, "replace", spy_replace)
    write_heartbeat(_make_heartbeat(), heartbeat_path)

    assert len(calls) == 1, f"expected exactly one os.replace call, got {calls}"
    src, dst = calls[0]
    assert src.endswith(".tmp"), f"src must be a .tmp sibling, got {src}"
    assert dst == str(heartbeat_path)
    # The .tmp file must be cleaned up by the rename.
    assert not Path(src).exists()


def test_write_atomic_no_partial_file_visible(
    heartbeat_path: Path, monkeypatch
) -> None:
    """Concurrent readers never see a half-written heartbeat.

    We monkeypatch :func:`os.replace` to interleave a reader between
    the temp-file write and the rename.  Because the reader looks at
    ``heartbeat_path`` (not the temp file), it must observe the
    PRIOR contents (or :class:`FileNotFoundError`) — never a partial
    JSON document.
    """

    # First write a baseline the reader can observe.
    baseline = _make_heartbeat(candidates_emitted_total=1)
    write_heartbeat(baseline, heartbeat_path)

    real_replace = os.replace
    seen_during_write: list[str] = []

    def spy_replace(src, dst):
        # Read the canonical path BEFORE the rename so we observe
        # the prior atomic state.  A non-atomic writer would have
        # already truncated the canonical path here.
        try:
            seen_during_write.append(Path(dst).read_text(encoding="utf-8"))
        except FileNotFoundError:
            seen_during_write.append("")
        return real_replace(src, dst)

    monkeypatch.setattr(hb_mod.os, "replace", spy_replace)
    write_heartbeat(_make_heartbeat(candidates_emitted_total=99), heartbeat_path)

    assert len(seen_during_write) == 1
    # The reader must have observed parseable JSON containing the
    # baseline value, NOT a partial / new payload.
    parsed = json.loads(seen_during_write[0])
    assert parsed["candidates_emitted_total"] == 1


def test_write_under_concurrent_reader(heartbeat_path: Path) -> None:
    """50-iteration reader/writer race never observes a partial file."""

    write_heartbeat(_make_heartbeat(candidates_emitted_total=0), heartbeat_path)
    stop = threading.Event()
    failures: list[str] = []

    def reader():
        while not stop.is_set():
            try:
                raw = heartbeat_path.read_text(encoding="utf-8")
                json.loads(raw)
            except json.JSONDecodeError as exc:
                failures.append(f"partial JSON: {exc}")
            except FileNotFoundError:
                # Atomic rename produces a FileNotFoundError ONLY if
                # the target was never written; we wrote the baseline
                # above so this is acceptable on cold cache only.
                pass

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    for i in range(50):
        write_heartbeat(_make_heartbeat(candidates_emitted_total=i), heartbeat_path)

    stop.set()
    thread.join(timeout=2.0)
    assert failures == [], f"observed partial reads: {failures}"


def test_write_creates_parent_directory(tmp_path: Path) -> None:
    """The writer creates ``state/`` on demand."""

    nested = tmp_path / "deep" / "state" / "heartbeat.json"
    write_heartbeat(_make_heartbeat(), nested)
    assert nested.exists()


# ---------------------------------------------------------------------------
# Update cadence (mtime advances every cycle)
# ---------------------------------------------------------------------------


def test_mtime_advances_on_each_write(heartbeat_path: Path) -> None:
    """mtime monotonically advances across successive write cycles."""

    write_heartbeat(_make_heartbeat(), heartbeat_path)
    first = heartbeat_path.stat().st_mtime
    time.sleep(0.05)
    write_heartbeat(_make_heartbeat(), heartbeat_path)
    second = heartbeat_path.stat().st_mtime
    assert second >= first, (first, second)


def test_last_poll_ts_advances_on_each_write(heartbeat_path: Path) -> None:
    """``last_poll_ts`` is the cadence anchor consumed by the watchdog."""

    write_heartbeat(_make_heartbeat(last_poll_ts="2026-04-29T12:00:00+00:00"),
                    heartbeat_path)
    first = read_heartbeat(heartbeat_path).last_poll_ts
    write_heartbeat(_make_heartbeat(last_poll_ts="2026-04-29T12:00:30+00:00"),
                    heartbeat_path)
    second = read_heartbeat(heartbeat_path).last_poll_ts
    assert first != second
    assert second > first


# ---------------------------------------------------------------------------
# Stale-detection predicate (M4 watchdog readiness)
# ---------------------------------------------------------------------------


def _iso(dt_seconds_ago: float, *, ref: float) -> str:
    """Return an ISO-8601 UTC timestamp that is ``dt_seconds_ago`` old."""

    return (
        datetime.fromtimestamp(ref - dt_seconds_ago, tz=timezone.utc)
        .isoformat(timespec="microseconds")
    )


def test_stale_predicate_fresh_returns_false(heartbeat_path: Path) -> None:
    """A 60-second-old heartbeat is fresh (not stale)."""

    now = 1_700_000_000.0
    write_heartbeat(_make_heartbeat(last_poll_ts=_iso(60.0, ref=now)),
                    heartbeat_path)
    assert is_news_daemon_stale(heartbeat_path, now=now) is False


def test_stale_predicate_5min_threshold(heartbeat_path: Path) -> None:
    """The 5-minute boundary: 360 s old → stale."""

    now = 1_700_000_000.0
    write_heartbeat(_make_heartbeat(last_poll_ts=_iso(360.0, ref=now)),
                    heartbeat_path)
    assert is_news_daemon_stale(heartbeat_path, now=now) is True


def test_stale_predicate_at_boundary_exact_300s_is_fresh(
    heartbeat_path: Path,
) -> None:
    """Exactly 300 s old is NOT stale; >300 s is."""

    now = 1_700_000_000.0
    write_heartbeat(_make_heartbeat(last_poll_ts=_iso(300.0, ref=now)),
                    heartbeat_path)
    assert is_news_daemon_stale(heartbeat_path, now=now) is False
    write_heartbeat(_make_heartbeat(last_poll_ts=_iso(300.001, ref=now)),
                    heartbeat_path)
    assert is_news_daemon_stale(heartbeat_path, now=now) is True


def test_stale_predicate_missing_file_is_stale(tmp_path: Path) -> None:
    """A missing heartbeat file is treated as stale."""

    assert is_news_daemon_stale(tmp_path / "does_not_exist.json") is True


def test_stale_predicate_malformed_json_is_stale(heartbeat_path: Path) -> None:
    """A malformed heartbeat file is treated as stale."""

    heartbeat_path.write_text("{not json", encoding="utf-8")
    assert is_news_daemon_stale(heartbeat_path) is True


def test_stale_predicate_unparseable_timestamp_is_stale(
    heartbeat_path: Path,
) -> None:
    """A non-ISO ``last_poll_ts`` is treated as stale."""

    payload = {
        "last_poll_ts": "not-a-real-timestamp",
        "candidates_emitted_total": 0,
        "candidates_emitted_session": 0,
        "errors_session": 0,
        "version_sha": _FIXED_SHA,
    }
    heartbeat_path.write_text(json.dumps(payload), encoding="utf-8")
    assert is_news_daemon_stale(heartbeat_path) is True


def test_stale_predicate_handles_z_suffix(heartbeat_path: Path) -> None:
    """``Z`` suffix on the timestamp is parsed as UTC."""

    now = 1_700_000_000.0
    iso = (
        datetime.fromtimestamp(now - 60.0, tz=timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    write_heartbeat(_make_heartbeat(last_poll_ts=iso), heartbeat_path)
    assert is_news_daemon_stale(heartbeat_path, now=now) is False


# ---------------------------------------------------------------------------
# version_sha resolver
# ---------------------------------------------------------------------------


def test_resolve_version_sha_honours_env_override(monkeypatch) -> None:
    """The env override pins the SHA without invoking ``git``."""

    monkeypatch.setenv("BIOTECH_SNIPER_VERSION_SHA", _FIXED_SHA)
    assert resolve_version_sha() == _FIXED_SHA


def test_resolve_version_sha_rejects_short_override(monkeypatch) -> None:
    """A non-40-char override is ignored; the resolver falls through."""

    monkeypatch.setenv("BIOTECH_SNIPER_VERSION_SHA", "abc")
    sha = resolve_version_sha()
    # Either a real git SHA or the placeholder — both length 40.
    assert len(sha) == 40
    assert sha != "abc"


def test_resolve_version_sha_fallback_to_placeholder(
    monkeypatch, tmp_path: Path
) -> None:
    """A non-git directory falls through to :data:`PLACEHOLDER_VERSION_SHA`."""

    monkeypatch.delenv("BIOTECH_SNIPER_VERSION_SHA", raising=False)
    sha = resolve_version_sha(repo_dir=tmp_path)
    assert sha == PLACEHOLDER_VERSION_SHA
    assert len(sha) == 40


def test_resolve_version_sha_returns_40_char_lowercase_hex(monkeypatch) -> None:
    """Fall-through produces 40 lower-case hex chars (real or placeholder)."""

    monkeypatch.delenv("BIOTECH_SNIPER_VERSION_SHA", raising=False)
    sha = resolve_version_sha()
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)
