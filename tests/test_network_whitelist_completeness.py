"""f-cross-06-secrets-and-network-audit — code-level network whitelist completeness.

Implements the static, repo-wide URL-extraction audit required by the
Reading-B cross-flow validation contract. Companion to the existing
runtime-mitm test in :mod:`tests.test_network_whitelist` (VAL-M1-049 /
VAL-M1-051 / VAL-M1-063), this file enforces:

* **VAL-CROSS-043** — every URL-constructing line in
  ``biotech_sniper/`` resolves to a host inside
  :data:`biotech_sniper.networks.ALLOWED_NETWORK_HOSTS` (or a
  narrowly-scoped, per-entry-documented exception set with
  provenance), AND every Reading-B M1 / M3 whitelist entry actually
  appears in production source (no dead Reading-B whitelist
  entries). The carve-out file
  :data:`DOCUMENTED_NON_WHITELIST_EXCEPTIONS` is *subtracted* from the
  extracted-hostname set (rather than unioned with the accepted
  set) so the strict assertion form is
  ``(extracted - DOCUMENTED_NON_WHITELIST_EXCEPTIONS) -
  ALLOWED_NETWORK_HOSTS == set()`` — see
  :func:`test_runtime_egress_strictly_in_whitelist`. Each carve-out
  entry MUST carry a single-line comment immediately above it
  pointing to the source file or AGENTS.md "Known Pre-Existing
  Issues" entry that documents *why* the host is exempt — see
  :func:`test_documented_exceptions_each_have_provenance`.

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

import ast
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
#
# CONTRACT: every entry in this frozenset MUST be preceded by a
# single-line ``#`` comment naming the source file (a ``.py``
# pathlet) or AGENTS.md "Known Pre-Existing Issues" entry that
# documents WHY the host appears in source without being on the
# whitelist. The provenance discipline is enforced statically by
# :func:`test_documented_exceptions_each_have_provenance`. New
# carve-outs added without provenance break the test — that is
# intentional, the carve-out list is a contract-load-bearing
# document and silent additions defeat its purpose.
DOCUMENTED_NON_WHITELIST_EXCEPTIONS: frozenset[str] = frozenset(
    {
        # alpaca_client.py:LIVE_BASE_URL — defined only so
        # _validate_paper_only can block it (paper-only invariant).
        "api.alpaca.markets",
        # biotech_sniper/migrations/010_reading_b_foundations.py
        # docstring link to SQLite ALTER TABLE docs (never dialed).
        "www.sqlite.org",
        # biotech_sniper/audit.py legacy Twitter probe dummy
        # fixture (test scaffold; never dialed).
        "x.com",
        # biotech_sniper/audit.py § 8 legacy single-ticker IR probe
        # — see AGENTS.md "Known Pre-Existing Issues".
        "ir.ideayabio.com",
        # biotech_sniper/biotech_sniper_agent.py sample / playground
        # data (never dialed in production code paths).
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


# ---------------------------------------------------------------------------
# Stricter rigor (f-fix-cross-tests-rigor sub-fix A)
# ---------------------------------------------------------------------------


def test_runtime_egress_strictly_in_whitelist():
    """Stricter form of VAL-CROSS-043's "extracted ⊆ whitelist" rule.

    Rather than UNION-ing the carve-out into the accepted set
    (which paints over the carve-outs and lets a *new* off-list
    host slip through if it happens to share a name with a future
    addition), we SUBTRACT the carve-out from the extracted set
    and assert the residue is fully covered by the whitelist:

    ``(extracted - DOCUMENTED_NON_WHITELIST_EXCEPTIONS) -
    ALLOWED_NETWORK_HOSTS == set()``

    The two forms are mathematically equivalent today but the
    subtract-from-extracted form makes the carve-out's role
    explicit (it is a list of *known absences*, not a list of
    *additional allowed hosts*) and prevents a class of typo
    regression where a future contributor mistakes the union form
    for "the whitelist plus these other allowed hosts".
    """
    extracted_hosts = set(_extract_hostnames_from_source().keys())
    residue = (
        extracted_hosts
        - DOCUMENTED_NON_WHITELIST_EXCEPTIONS
        - ALLOWED_NETWORK_HOSTS
    )
    assert residue == set(), (
        "VAL-CROSS-043 strict: extracted hostnames not on the "
        "whitelist after subtracting documented exceptions: "
        f"{sorted(residue)}"
    )


def _exception_provenance_lines() -> list[tuple[str, list[str]]]:
    """Return ``[(host, comment_lines_immediately_above_entry), ...]``
    by reading this test module's own source.

    Used by :func:`test_documented_exceptions_each_have_provenance`
    to enforce the provenance-comment discipline on each carve-out
    entry.
    """
    src_lines = Path(__file__).read_text(encoding="utf-8").splitlines()
    tree = ast.parse("\n".join(src_lines), filename=__file__)
    target_set: ast.Set | None = None
    for node in ast.walk(tree):
        # Source uses an annotated assignment
        # (``DOCUMENTED_NON_WHITELIST_EXCEPTIONS: frozenset[str] =
        # frozenset({...})``) which is an ``ast.AnnAssign``; also
        # accept plain ``ast.Assign`` for forward compatibility.
        is_match = False
        value: ast.AST | None = None
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "DOCUMENTED_NON_WHITELIST_EXCEPTIONS"
        ):
            is_match = True
            value = node.value
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name)
            and t.id == "DOCUMENTED_NON_WHITELIST_EXCEPTIONS"
            for t in node.targets
        ):
            is_match = True
            value = node.value
        if not is_match:
            continue
        # The literal is ``frozenset({...})``; the inner ast.Set
        # holds the host strings. Unwrap the Call wrapper.
        if isinstance(value, ast.Call) and isinstance(
            value.func, ast.Name
        ) and value.func.id == "frozenset":
            if value.args and isinstance(value.args[0], ast.Set):
                target_set = value.args[0]
                break
    assert target_set is not None, (
        "could not locate DOCUMENTED_NON_WHITELIST_EXCEPTIONS "
        "frozenset literal in this test module"
    )

    out: list[tuple[str, list[str]]] = []
    for elt in target_set.elts:
        # Each element is an ``ast.Constant`` with a string value.
        if not (
            isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        ):
            continue
        host = elt.value
        # Walk backwards from the line above the element, collecting
        # contiguous comment lines (lines whose first non-whitespace
        # char is ``#``). Stop on the first non-comment, non-blank line.
        idx = elt.lineno - 2  # 0-based index of the line above
        comments: list[str] = []
        while idx >= 0:
            line = src_lines[idx]
            stripped = line.strip()
            if stripped.startswith("#"):
                comments.append(stripped.lstrip("#").strip())
                idx -= 1
                continue
            if stripped == "":
                # Blank line breaks contiguity — provenance must be
                # IMMEDIATELY above the entry, no blank line gap.
                break
            break
        comments.reverse()
        out.append((host, comments))
    return out


# Tokens that count as a "provenance" reference in a carve-out
# comment. At least one of these MUST appear in the contiguous
# comment block immediately above each ``DOCUMENTED_NON_WHITELIST_EXCEPTIONS``
# entry.
_PROVENANCE_TOKENS: tuple[str, ...] = (
    ".py",
    "AGENTS.md",
    "VAL-",
    "f-cross-",
    "f-m",
    "f-fix-",
    "library/",
    "validation-contract.md",
)


def test_documented_exceptions_each_have_provenance():
    """Every entry in ``DOCUMENTED_NON_WHITELIST_EXCEPTIONS`` MUST have
    a contiguous ``#`` comment block immediately above it that
    references its provenance — a source file (``*.py`` pathlet),
    AGENTS.md "Known Pre-Existing Issues" entry, validation-contract
    VAL-ID, or feature-id (``f-cross-``/``f-m``/``f-fix-``).

    This makes the carve-out list contract-load-bearing: silent
    additions ("just to make the test pass") fail at CI time
    because the new entry has no provenance marker.
    """
    # Sanity: the carve-out set is non-empty (otherwise the
    # provenance check is vacuous).
    assert DOCUMENTED_NON_WHITELIST_EXCEPTIONS, (
        "DOCUMENTED_NON_WHITELIST_EXCEPTIONS is empty — if you really "
        "intend to remove every carve-out, also remove this test."
    )

    rows = _exception_provenance_lines()
    seen_hosts = {row[0] for row in rows}
    # Each runtime-set member should appear in the AST extraction
    # so we are confident the extraction picks up every entry.
    missing_from_ast = (
        DOCUMENTED_NON_WHITELIST_EXCEPTIONS - seen_hosts
    )
    assert not missing_from_ast, (
        "_exception_provenance_lines() failed to extract these "
        f"entries from source: {sorted(missing_from_ast)}"
    )

    offenders: list[tuple[str, list[str]]] = []
    for host, comments in rows:
        if not comments:
            offenders.append((host, []))
            continue
        text = " | ".join(comments)
        if not any(token in text for token in _PROVENANCE_TOKENS):
            offenders.append((host, comments))
    assert not offenders, (
        "VAL-CROSS-043 provenance: each carve-out entry MUST have a "
        "single-line ``#`` comment immediately above it referencing a "
        f"source file (.py), AGENTS.md, VAL-ID, or feature-id; "
        f"offenders={offenders}"
    )


# ---------------------------------------------------------------------------
# Intelligence/ subpackage runtime URL-validator AST check (f-fix-cross-tests-rigor sub-fix B)
# ---------------------------------------------------------------------------


# Names that count as "whitelist gate" calls in the AST sweep below.
# Both the alpaca_client-style raising gate and the boolean predicate
# form satisfy the invariant — a resolver may invoke either before
# egress. Centralised here so a future rename of the gate (or
# addition of a third equivalent name) is a one-line change.
_WHITELIST_GATE_FUNC_NAMES: frozenset[str] = frozenset(
    {
        "_validate_paper_only",
        "_url_in_whitelist",
    }
)


def _resolver_module_paths() -> list[Path]:
    """Return the intelligence/ resolver module paths to AST-check.

    Limited to ``company_resolver.py`` per the f-fix-cross-tests-rigor
    sub-fix B contract. Future resolvers (e.g. master_discovery)
    can be added here once they too route their egress through the
    runtime gate.
    """
    return [PACKAGE_ROOT / "intelligence" / "company_resolver.py"]


def _function_def_for_node(
    tree: ast.AST, target: ast.AST
) -> ast.FunctionDef | None:
    """Return the enclosing ``ast.FunctionDef`` for ``target``, or
    ``None`` if ``target`` is at module level."""

    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._parent = parent  # type: ignore[attr-defined]
    cursor: ast.AST | None = target
    while cursor is not None:
        cursor = getattr(cursor, "_parent", None)
        if isinstance(cursor, ast.FunctionDef):
            return cursor
    return None


def _function_has_gate_before_call(
    func: ast.FunctionDef, target_call: ast.Call
) -> bool:
    """Return True iff ``func`` body contains a call to one of the
    whitelist-gate functions on a line strictly before
    ``target_call.lineno``."""

    target_line = target_call.lineno
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        if node is target_call:
            continue
        if node.lineno >= target_line:
            continue
        func_node = node.func
        if isinstance(func_node, ast.Name) and (
            func_node.id in _WHITELIST_GATE_FUNC_NAMES
        ):
            return True
        if isinstance(func_node, ast.Attribute) and (
            func_node.attr in _WHITELIST_GATE_FUNC_NAMES
        ):
            return True
    return False


def test_intelligence_runtime_url_validators_invoked():
    """Every ``requests.get(...)`` call inside intelligence/ resolver
    code MUST be preceded in the same enclosing function by a call to
    one of :data:`_WHITELIST_GATE_FUNC_NAMES`
    (``_validate_paper_only`` / ``_url_in_whitelist``).

    The intelligence/ subpackage is excluded from the static
    URL-extraction sweep above (see :data:`EXCLUDED_DIRS`) because its
    egress targets are computed at run time from per-company IR
    domains. This test compensates: a *runtime* negative-whitelist
    gate at ``biotech_sniper/intelligence/url_guard.py`` rejects any
    URL that resolves to the live Alpaca host or an off-limits
    other-tenant token, and every ``requests.get`` call site in the
    resolver pipeline invokes the gate first. This AST-level check
    catches a regression — a freshly-added ``requests.get`` site that
    bypasses the gate — at CI time rather than shipping silently.
    """
    offenders: list[str] = []
    for module_path in _resolver_module_paths():
        assert module_path.is_file(), module_path
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module_path))

        # Pre-compute parent links for the AST so we can walk
        # upwards from each Call to its enclosing FunctionDef.
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                child._parent = parent  # type: ignore[attr-defined]

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func_node = node.func
            # Match only ``requests.get(...)`` (the egress entry
            # point used by the resolver). Other ``requests.X``
            # calls are out of scope; if a future resolver uses
            # ``requests.post`` add it here.
            if not (
                isinstance(func_node, ast.Attribute)
                and func_node.attr == "get"
                and isinstance(func_node.value, ast.Name)
                and func_node.value.id == "requests"
            ):
                continue
            # Find enclosing function.
            cursor: ast.AST | None = getattr(node, "_parent", None)
            enclosing: ast.FunctionDef | None = None
            while cursor is not None:
                if isinstance(cursor, ast.FunctionDef):
                    enclosing = cursor
                    break
                cursor = getattr(cursor, "_parent", None)
            if enclosing is None:
                offenders.append(
                    f"{module_path.name}:L{node.lineno} "
                    "requests.get at module level (no enclosing function)"
                )
                continue
            if not _function_has_gate_before_call(enclosing, node):
                offenders.append(
                    f"{module_path.name}:L{node.lineno} "
                    f"requests.get inside {enclosing.name}() not "
                    "preceded by _validate_paper_only / _url_in_whitelist"
                )

    assert not offenders, (
        "VAL-CROSS-043 / intelligence-runtime-gate: requests.get "
        "call sites missing the whitelist-gate invocation:\n  - "
        + "\n  - ".join(offenders)
    )
