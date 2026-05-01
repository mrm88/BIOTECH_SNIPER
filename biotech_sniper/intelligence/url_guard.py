"""Runtime URL guard for the ``biotech_sniper.intelligence`` subpackage.

The intelligence subpackage (company_resolver, master_discovery, …)
legitimately probes *arbitrary* per-company investor-relations
domains using runtime ``{domain}`` template substitution — there is
no finite ``ALLOWED_NETWORK_HOSTS`` subset that can describe its
egress surface. The Reading-B network whitelist regression
(``tests/test_network_whitelist_completeness.py``) therefore
*excludes* this subpackage from the static URL-extraction sweep.

The exclusion is documented in ``library/architecture.md`` and is
necessary by construction. Without a compensating runtime gate,
however, the exclusion is a blind spot — a buggy resolver could
silently dial an off-limits other-tenant host
(``hl-grok``/``hyperfund``/``hl-edge``/etc.) or the live Alpaca
trading host without any layer noticing.

This module provides the compensating runtime gate. It is a
*negative* whitelist: it does not enumerate the allowed IR
domains (impossible — they are computed per company at run time)
but instead REJECTS:

* The live Alpaca trading host ``api.alpaca.markets`` (paper-only
  invariant — mirrors :func:`biotech_sniper.alpaca_client._validate_paper_only`).
* Any URL whose host contains an off-limits other-tenant token
  from :data:`biotech_sniper.networks.OFF_LIMITS_DOMAIN_TOKENS`
  (``hyperfund`` / ``hl-grok`` / ``hl-edge`` / ``geo-shock`` /
  ``ml_analysis`` / ``palmpilot`` / ``tkl-signal-engine`` /
  ``shadow_scorer`` / ``beam-content-watcher`` / ``parallel_shadow``).

The gate is invoked by every ``requests.get`` call site inside
``company_resolver.py`` BEFORE network egress; the AST-level test
``tests/test_network_whitelist_completeness.py::
test_intelligence_runtime_url_validators_invoked`` enforces the
invocation discipline statically so a regression (a freshly-added
``requests.get`` site that bypasses the gate) is caught at CI time.
"""

from __future__ import annotations

from urllib.parse import urlparse

from biotech_sniper.networks import OFF_LIMITS_DOMAIN_TOKENS


# The live Alpaca trading host. Defined here as a plain string (NOT
# imported from ``alpaca_client``) so this guard module has zero
# dependency on the trading subsystem and can be used in isolation.
LIVE_ALPACA_HOST: str = "api.alpaca.markets"


class IntelligenceEgressBlocked(RuntimeError):
    """Raised when an intelligence/ resolver attempts egress to a
    forbidden host (live Alpaca or an off-limits other-tenant token)."""


def _extract_host(url: str) -> str:
    """Return the lowercase hostname for ``url`` (empty string if the
    URL has no host component, e.g. relative paths or templates with
    unresolved ``{domain}``)."""
    if not url:
        return ""
    parsed = urlparse(url)
    return (parsed.hostname or "").lower()


def _url_in_whitelist(url: str) -> bool:
    """Return ``True`` iff the URL's host is acceptable for an
    intelligence/ resolver call.

    "Acceptable" here means NOT a forbidden host:

    * not the live Alpaca trading host (paper-only invariant);
    * not an off-limits other-tenant token (multi-tenant VPS
      isolation invariant).

    Returns ``True`` for every other host — including arbitrary
    per-company IR domains — because the resolver legitimately
    probes those domains at run time. Use :func:`_validate_paper_only`
    when you want a raising variant.
    """
    host = _extract_host(url)
    if not host:
        # Empty / template URL — let the caller deal with it. We
        # explicitly do NOT raise on empty so unit-test scaffolds
        # using placeholder URLs still go through the same code path.
        return True
    if host == LIVE_ALPACA_HOST:
        return False
    for token in OFF_LIMITS_DOMAIN_TOKENS:
        if token in host:
            return False
    return True


def _validate_paper_only(url: str) -> None:
    """Raise :class:`IntelligenceEgressBlocked` if ``url`` resolves
    to a forbidden host.

    This is the runtime gate every resolver ``requests.get`` call site
    invokes BEFORE egress. The function name mirrors
    :func:`biotech_sniper.alpaca_client._validate_paper_only` so the
    AST-level invocation test can recognise either name as a valid
    whitelist gate (see
    ``tests/test_network_whitelist_completeness.py::
    test_intelligence_runtime_url_validators_invoked``).
    """
    if _url_in_whitelist(url):
        return
    host = _extract_host(url) or "<unparsable>"
    raise IntelligenceEgressBlocked(
        f"intelligence/ resolver attempted egress to forbidden host "
        f"{host!r} (url={url!r}). Allowed targets: per-company IR "
        f"domains (arbitrary {{domain}}) and prior-mission whitelist "
        f"hosts. Forbidden: live Alpaca ({LIVE_ALPACA_HOST}) and "
        f"off-limits other-tenant tokens "
        f"({sorted(OFF_LIMITS_DOMAIN_TOKENS)})."
    )


__all__ = [
    "IntelligenceEgressBlocked",
    "LIVE_ALPACA_HOST",
    "_url_in_whitelist",
    "_validate_paper_only",
]
