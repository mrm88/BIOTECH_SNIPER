"""f-cross-06-secrets-and-network-audit — code-level network whitelist completeness.

Implements the static, repo-wide URL-extraction audit required by the
Reading-B cross-flow validation contract. Companion to the existing
runtime-mitm test in :mod:`tests.test_network_whitelist` (VAL-M1-049 /
VAL-M1-051 / VAL-M1-063), this file enforces:

* **VAL-CROSS-043** — every URL-constructing line in
  ``biotech_sniper/`` resolves to a host inside
  :data:`biotech_sniper.networks.ALLOWED_NETWORK_HOSTS` (or a tightly
  documented exception set), AND every Reading-B M1 / M3 whitelist
  entry actually appears in production source (no dead Reading-B
  whitelist entries).

The static URL-extraction approach (regex over ``*.py`` source) is
chosen over runtime mitm here because it gives us a stable
codebase-wide invariant that fails fast at CI time without needing
to import or exercise any module. The runtime mitm coverage in
``test_network_whitelist.py`` complements this by proving the
extracted hostnames are also what production *actually* dials.

The test is hermetic — no network egress.

Documented exceptions (NOT in ``ALLOWED_NETWORK_HOSTS``):

* ``api.alpaca.markets`` — Alpaca live-trading host. Defined as a
  module constant ``LIVE_BASE_URL`` and used by
  :func:`_validate_paper_only` to *block* construction unless the
  two-flag LIVE_MODE gate is open. Production code paths cannot
  reach the host; the constant exists solely so that the gate can
  recognise and reject it.
* ``www.sqlite.org`` — referenced once in a migration docstring
  (link to SQLite ALTER TABLE docs). Never dialed at runtime.
* ``x.com`` — appears in a dummy fixture inside ``audit.py``'s
  legacy Twitter probe (``url='https://x.com/test'``); never dialed.
* ``ir.ideayabio.com`` — pre-existing legacy probe in ``audit.py``
  (single-ticker IR signal check). Out of scope of the Reading-B
  network policy; covered by a separate ``discoveredIssues`` entry.
* ``www.stocktitan.net`` — sample / playground data in
  ``biotech_sniper_agent.py``. Not part of any production egress
  path.

The ``biotech_sniper/intelligence/`` subpackage is excluded from the
extracted-hostname check because its company-resolver and master-
discovery modules legitimately dial *arbitrary* per-company investor-
relations domains (template substitution ``{domain}``); there is no
finite whitelist that would describe its egress surface, and the
Reading-B contract does not extend coverage to it. The package's
egress pattern is documented in ``library/architecture.md``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from biotech_sniper.networks import (
    ALLOWED_NETWORK_HOSTS,
    M1_NEW_HOSTS,
    M3_RESERVED_HOSTS,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "biotech_sniper"


# ---------------------------------------------------------------------------
# Static URL extraction
# ---------------------------------------------------------------------------


# A URL hostname is the chunk between ``://`` and the next ``/``,
# ``"``, ``'``, ``\``, ``)``, ``,``, ``>``, or whitespace. The capture
# accepts standard FQDN characters plus the ``{domain}`` template
# placeholder used by ``intelligence/company_resolver.py`` so a
# single regex can flag those (we then exclude ``intelligence/``
# elsewhere — see :data:`EXCLUDED_DIRS`).
_URL_HOST_RE = re.compile(r"https?://([a-zA-Z0-9.\-_{}]+)")


# Subdirectories of ``biotech_sniper/`` whose egress is documented as
# dynamic / unbounded; their URL constructions don't have a finite
# whitelist. Excluded from the static-extraction check.
EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        "intelligence",  # company_resolver / master_discovery probe arbitrary domains
    }
)


# Hostnames that appear in source but are NOT live-egress targets:
# documented blocked URLs, docstring references, dummy/sample data,
# or pre-existing single-ticker probes outside Reading-B scope.
DOCUMENTED_NON_WHITELIST_EXCEPTIONS: frozenset[str] = frozenset(
    {
        # Alpaca live host — defined only so _validate_paper_only can
        # block it. See alpaca_client.py:LIVE_BASE_URL.
        "api.alpaca.markets",
        # Migration 010 docstring link to SQLite ALTER TABLE docs.
        "www.sqlite.org",
        # audit.py legacy Twitter probe dummy fixture.
        "x.com",
        # audit.py § 8 legacy IR probe (single ticker, pre-existing).
        "ir.ideayabio.com",
        # biotech_sniper_agent.py sample data.
        "www.stocktitan.net",
    }
)


# Reading-B M1 hosts. Each MUST be referenced at least once in
# production source under ``biotech_sniper/`` so the whitelist is
# not "dead" — i.e. the runtime code we ship genuinely needs each
# M1 host. ``M3_RESERVED_HOSTS`` is by design "reserved-until-wired"
# (see :data:`networks.M3_RESERVED_HOSTS` docstring) so an entry
# such as ``status.perplexity.ai`` may legitimately be reserved
# for future health-poll wiring without yet appearing as a literal
# in source. Prior-mission hosts are not asserted because some
# are runtime-injected from RSS feed configs (composed dynamically)
# and may legitimately not appear as bare URL literals.
READING_B_M1_HOSTS: frozenset[str] = M1_NEW_HOSTS


def _iter_python_source_files() -> Iterable[Path]:
    """Yield every ``*.py`` file under ``biotech_sniper/`` excluding
    :data:`EXCLUDED_DIRS` subpackages."""
    for path in PACKAGE_ROOT.rglob("*.py"):
        # Skip cache and any excluded subpackage.
        rel_parts = path.relative_to(PACKAGE_ROOT).parts
        if any(part in EXCLUDED_DIRS for part in rel_parts):
            continue
        if "__pycache__" in rel_parts:
            continue
        yield path


def _extract_hostnames_from_source() -> dict[str, list[Path]]:
    """Return ``{hostname: [files mentioning it]}`` for the in-scope
    ``biotech_sniper/`` source tree."""
    hits: dict[str, list[Path]] = {}
    for fp in _iter_python_source_files():
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover — defensive
            continue
        for match in _URL_HOST_RE.finditer(text):
            host = match.group(1).lower().rstrip(".")
            # Skip template fragments like ``www.`` (ends with dot)
            # or ``ir.``: those are concatenated at runtime with a
            # company domain and have already been excluded by
            # :data:`EXCLUDED_DIRS`. Also skip empty / placeholder
            # captures from ``{domain}`` substitutions.
            if "{" in host or "}" in host:
                continue
            if host.endswith("."):
                continue
            # urlparse round-trip to canonicalise (cheap sanity check).
            parsed_host = urlparse(f"https://{host}/").hostname
            if not parsed_host:
                continue
            hits.setdefault(parsed_host, []).append(fp)
    return hits


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


def test_extracted_hostnames_subset_of_whitelist_or_documented_exception():
    """Every URL hostname in production source ⊆ whitelist ∪ exceptions.

    Concretely: ``extracted_hostnames - ALLOWED_NETWORK_HOSTS -
    DOCUMENTED_NON_WHITELIST_EXCEPTIONS == set()``.
    """
    extracted = _extract_hostnames_from_source()
    allowed = ALLOWED_NETWORK_HOSTS | DOCUMENTED_NON_WHITELIST_EXCEPTIONS
    leftover = {host: files for host, files in extracted.items() if host not in allowed}
    assert not leftover, (
        "VAL-CROSS-043: extracted hostnames not in whitelist and not in "
        f"documented exceptions: {sorted(leftover.keys())} "
        f"(occurrences: {{h: [str(p) for p in fs] for h, fs in leftover.items()}})"
    )


def test_documented_exceptions_actually_appear_in_source():
    """Every entry in ``DOCUMENTED_NON_WHITELIST_EXCEPTIONS`` must appear
    in source — otherwise the exception list itself is dead and
    pretending to defend something it doesn't.
    """
    extracted = _extract_hostnames_from_source()
    dead_exceptions = sorted(
        host for host in DOCUMENTED_NON_WHITELIST_EXCEPTIONS if host not in extracted
    )
    assert not dead_exceptions, (
        "VAL-CROSS-043: documented exceptions no longer appear in source — "
        f"remove from DOCUMENTED_NON_WHITELIST_EXCEPTIONS: {dead_exceptions}"
    )


def test_no_dead_reading_b_m1_whitelist_entries():
    """Every Reading-B M1 whitelist entry is referenced at least once
    in production source under ``biotech_sniper/``.

    This is the "no dead whitelist entries" half of VAL-CROSS-043 for
    M1 — those entries are wired into IWM / SEC / FDA / EMA fetchers.
    M3 reserved hosts (``api.perplexity.ai`` and
    ``status.perplexity.ai``) are NOT asserted here because the
    ``M3_RESERVED_HOSTS`` docstring documents them as
    "reserved-until-wired" — ``api.perplexity.ai`` is wired by
    f-m3-01 (and so DOES appear in source), but ``status.perplexity.ai``
    is reserved for the breaker health-poll, which may legitimately
    not be wired yet at this stage of Reading-B.

    Prior-mission hosts are not asserted because some (e.g. RSS feed
    hosts) are runtime-injected from configuration and don't appear
    as bare URL literals.
    """
    extracted = _extract_hostnames_from_source()
    dead = sorted(host for host in READING_B_M1_HOSTS if host not in extracted)
    assert not dead, (
        "VAL-CROSS-043: Reading-B M1 whitelist entries with no source "
        f"references (dead entries): {dead}"
    )


def test_perplexity_api_host_is_wired():
    """``api.perplexity.ai`` is the active M3 production host (wired by
    f-m3-01). It MUST appear in source under
    ``biotech_sniper/llm/``. ``status.perplexity.ai`` is reserved
    only and explicitly NOT asserted here.
    """
    extracted = _extract_hostnames_from_source()
    files = extracted.get("api.perplexity.ai", [])
    assert files, (
        "VAL-CROSS-043: api.perplexity.ai is in the M3 reserved set "
        "but no production file references it — verify f-m3-01 "
        "Perplexity client is committed."
    )
    # Sanity: the reference must come from the LLM subpackage, not
    # a stray docstring in another module.
    rel_paths = [str(fp.relative_to(PACKAGE_ROOT)) for fp in files]
    assert any("llm" in p for p in rel_paths), (
        f"api.perplexity.ai expected under biotech_sniper/llm/, found: {rel_paths}"
    )


def test_live_alpaca_host_used_only_as_blocked_constant():
    """``api.alpaca.markets`` may only appear in ``alpaca_client.py``
    (where it is the LIVE_BASE_URL constant gated by
    :func:`_validate_paper_only`). Any new file hardcoding the host
    is a paper-only invariant violation.
    """
    extracted = _extract_hostnames_from_source()
    files = extracted.get("api.alpaca.markets", [])
    offenders: list[str] = []
    for fp in files:
        rel = fp.relative_to(PACKAGE_ROOT).as_posix()
        if rel != "alpaca_client.py":
            offenders.append(rel)
    assert not offenders, (
        "VAL-CROSS-043 / paper-only invariant: api.alpaca.markets must "
        f"only appear in alpaca_client.py, found in: {sorted(set(offenders))}"
    )


def test_paper_alpaca_host_is_in_whitelist():
    """Sanity: the paper-trading Alpaca host MUST be on the active
    whitelist (the live one MUST NOT)."""
    assert "paper-api.alpaca.markets" in ALLOWED_NETWORK_HOSTS
    assert "api.alpaca.markets" not in ALLOWED_NETWORK_HOSTS


def test_excluded_dirs_have_known_dynamic_egress_pattern():
    """Sanity: the excluded ``intelligence/`` package exists and
    actually contains the dynamic ``{domain}`` URL pattern that
    motivated its exclusion. Guards against silently shipping the
    exclusion when the legacy code is gone.
    """
    intelligence_dir = PACKAGE_ROOT / "intelligence"
    assert intelligence_dir.is_dir(), intelligence_dir
    has_template = False
    for fp in intelligence_dir.rglob("*.py"):
        try:
            if "{domain}" in fp.read_text(encoding="utf-8", errors="replace"):
                has_template = True
                break
        except Exception:  # pragma: no cover
            continue
    assert has_template, (
        "biotech_sniper/intelligence/ no longer uses the {domain} "
        "URL template — re-evaluate the EXCLUDED_DIRS exemption."
    )


def test_allowed_network_hosts_is_frozenset():
    """Whitelist must be a frozenset (immutable contract surface)."""
    assert isinstance(ALLOWED_NETWORK_HOSTS, frozenset)
    # Every entry is a non-empty plain hostname — no scheme, no path.
    for host in ALLOWED_NETWORK_HOSTS:
        assert host and "/" not in host and "://" not in host, host
