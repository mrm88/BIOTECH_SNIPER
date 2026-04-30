"""f-m1-08-whitelist-and-audit — network egress allow-list tests.

This test suite enforces the assertions tied to feature
``f-m1-08-whitelist-and-audit``:

* **VAL-M1-049** — :data:`biotech_sniper.networks.ALLOWED_NETWORK_HOSTS`
  contains literal entries for ``www.ishares.com``, ``data.sec.gov``,
  ``www.fda.gov``, and ``www.ema.europa.eu``.
* **VAL-M1-051** — None of the off-limits other-project domains
  (``hyperfund``, ``hl-grok``, ``hl-edge``, ``geo-shock``,
  ``ml_analysis``, ``palmpilot``, etc.) ever appear in the
  whitelist.
* **VAL-M1-052** — Every existing module under ``biotech_sniper/``
  imports cleanly post-migration.
* **VAL-M1-053** — ``python -m biotech_sniper.audit`` exits 0.
* **VAL-M1-063** — A full M1 ingestion run (IWM importer + SEC SIC
  classifier + PDUFA scraper + EMA/CHMP scraper) under a
  mitm-style ``requests`` monkeypatch contacts ONLY hosts present
  in :data:`ALLOWED_NETWORK_HOSTS`.

The mitm fixture (:func:`_capture_requests`) monkeypatches
``requests.get`` / ``requests.post`` / ``requests.head`` / the
``requests.Session`` instance methods to:

1. Record the hostname of every attempted URL.
2. Raise ``requests.exceptions.ConnectionError`` so no actual
   network egress takes place.

The M1 ingestion modules wrap network failures in their own typed
exceptions (``UpstreamUnavailable``) and either fall back to a
bundled seed file (PDUFA / EMA) or surface a non-zero exit code
(IWM / SEC). The mitm fixture exercises the same code paths the
real cron run does — only the bytes-on-the-wire step is short-
circuited — so the captured hostname set is an accurate
representation of ingestion-time egress.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import pytest

import biotech_sniper
from biotech_sniper import networks
from biotech_sniper.networks import (
    ALLOWED_NETWORK_HOSTS,
    M1_NEW_HOSTS,
    OFF_LIMITS_DOMAIN_TOKENS,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "biotech_sniper"


# ---------------------------------------------------------------------------
# VAL-M1-049 — Whitelist policy file lists the new M1 hosts.
# ---------------------------------------------------------------------------


REQUIRED_M1_HOSTS: frozenset[str] = frozenset(
    {
        "www.ishares.com",
        "data.sec.gov",
        "www.fda.gov",
        "www.ema.europa.eu",
    }
)


def test_val_m1_049_required_m1_hosts_present():
    """Every required M1 host appears in :data:`ALLOWED_NETWORK_HOSTS`."""
    missing = REQUIRED_M1_HOSTS - ALLOWED_NETWORK_HOSTS
    assert not missing, (
        f"VAL-M1-049: missing M1 hosts in ALLOWED_NETWORK_HOSTS: "
        f"{sorted(missing)}"
    )


def test_val_m1_049_required_m1_hosts_in_m1_subset():
    """The four required hosts are also in :data:`M1_NEW_HOSTS`.

    Keeps the per-milestone subset honest — a future worker can grep
    ``M1_NEW_HOSTS`` to know exactly which hosts Reading-B M1 added.
    """
    assert REQUIRED_M1_HOSTS <= M1_NEW_HOSTS


def test_allowed_network_hosts_is_immutable_frozenset():
    """Consumers must not be able to mutate the canonical list."""
    assert isinstance(ALLOWED_NETWORK_HOSTS, frozenset)
    assert all(isinstance(h, str) and h for h in ALLOWED_NETWORK_HOSTS)
    # Sanity: API is exposed via __all__.
    assert "ALLOWED_NETWORK_HOSTS" in networks.__all__


# ---------------------------------------------------------------------------
# VAL-M1-051 — No collisions with off-limits other-project domains.
# ---------------------------------------------------------------------------


def test_val_m1_051_no_off_limits_domain_collisions():
    """No ALLOWED_NETWORK_HOSTS entry contains an off-limits token."""
    offenders: list[tuple[str, str]] = []
    for host in ALLOWED_NETWORK_HOSTS:
        host_lower = host.lower()
        for token in OFF_LIMITS_DOMAIN_TOKENS:
            if token in host_lower:
                offenders.append((host, token))
    assert not offenders, (
        "VAL-M1-051: off-limits domain tokens detected in "
        f"ALLOWED_NETWORK_HOSTS: {offenders}"
    )


def test_val_m1_051_off_limits_tokens_not_used_as_hostnames():
    """No ALLOWED_NETWORK_HOSTS entry uses an off-limits token as a host.

    Documentation may legitimately mention these tokens (the
    OFF_LIMITS_DOMAIN_TOKENS frozenset itself does, as does the
    explanatory docstring). The contract is purely about the
    *active* allow-list: no whitelisted hostname may contain any
    off-limits domain token as a substring.
    """
    offenders: list[tuple[str, str]] = []
    for host in ALLOWED_NETWORK_HOSTS:
        host_lower = host.lower()
        for token in OFF_LIMITS_DOMAIN_TOKENS:
            if token in host_lower:
                offenders.append((host, token))
    assert not offenders, (
        "VAL-M1-051: off-limits tokens used as substrings of "
        f"whitelisted hostnames: {offenders}"
    )


# ---------------------------------------------------------------------------
# VAL-M1-052 — Smoke imports unchanged.
# ---------------------------------------------------------------------------


def test_val_m1_052_smoke_imports_every_biotech_sniper_module():
    """Every module under ``biotech_sniper/`` imports cleanly."""
    failed: list[tuple[str, str]] = []
    for m in pkgutil.walk_packages(
        biotech_sniper.__path__, prefix="biotech_sniper."
    ):
        try:
            importlib.import_module(m.name)
        except Exception as exc:  # pragma: no cover - regression fence
            failed.append((m.name, repr(exc)))
    assert failed == [], f"VAL-M1-052: smoke imports failed: {failed}"


# ---------------------------------------------------------------------------
# VAL-M1-053 — Daily-curated audit run is non-error post-M1.
# ---------------------------------------------------------------------------


def test_val_m1_053_audit_module_imports_cleanly():
    """:mod:`biotech_sniper.audit` imports without side-effects.

    The full ``python -m biotech_sniper.audit`` runtime invocation is
    exercised by the verification step (see feature
    ``verificationSteps``); here we guarantee the import surface is
    intact post-M1 so the module-as-script run has a chance to succeed.
    """
    audit = importlib.import_module("biotech_sniper.audit")
    # ``write_audit_latest`` is the public f-m4-03 contract entry
    # point. Its presence is a load-bearing invariant.
    assert hasattr(audit, "write_audit_latest"), (
        "VAL-M1-053: biotech_sniper.audit must expose write_audit_latest"
    )


# ---------------------------------------------------------------------------
# VAL-M1-063 — M1 ingestion contacts ONLY whitelisted hosts (mitm fixture).
# ---------------------------------------------------------------------------


def _capture_requests(monkeypatch) -> list[str]:
    """Install the mitm-style monkeypatch and return the capture list.

    The fake intercepts ``requests.get`` / ``post`` / ``head`` /
    ``put`` / ``delete`` / ``request``, and the corresponding methods
    on :class:`requests.Session` instances. Every captured URL has
    its hostname appended to the returned list and the call is then
    short-circuited by raising
    :class:`requests.exceptions.ConnectionError` so no actual bytes
    leave the test runner.
    """
    captured: list[str] = []

    import requests

    def _record_and_refuse(url: str) -> "None":
        host = urlparse(url).hostname or ""
        captured.append(host)
        raise requests.exceptions.ConnectionError(
            f"mitm-fixture: refusing to dial {url}"
        )

    def _make_verb(verb: str):
        def _fake_verb(url, *args, **kwargs):  # type: ignore[no-untyped-def]
            _record_and_refuse(url)

        _fake_verb.__name__ = f"mitm_requests_{verb}"
        return _fake_verb

    for verb in ("get", "post", "head", "put", "delete", "options", "patch"):
        if hasattr(requests, verb):
            monkeypatch.setattr(requests, verb, _make_verb(verb))

    def _fake_request(method, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        _record_and_refuse(url)

    monkeypatch.setattr(requests, "request", _fake_request)

    def _fake_session_request(self, method, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        _record_and_refuse(url)

    monkeypatch.setattr(
        requests.Session, "request", _fake_session_request
    )
    monkeypatch.setattr(
        requests.Session,
        "get",
        lambda self, url, *a, **kw: _record_and_refuse(url),
    )
    monkeypatch.setattr(
        requests.Session,
        "post",
        lambda self, url, *a, **kw: _record_and_refuse(url),
    )
    monkeypatch.setattr(
        requests.Session,
        "head",
        lambda self, url, *a, **kw: _record_and_refuse(url),
    )

    return captured


def _assert_only_whitelisted(hosts: Iterable[str]) -> None:
    """Assert every captured hostname is a member of the allow-list."""
    bad = sorted(
        {h for h in hosts if h and h not in ALLOWED_NETWORK_HOSTS}
    )
    assert not bad, (
        "VAL-M1-063: M1 ingestion contacted non-whitelisted hosts: "
        f"{bad}"
    )


def test_val_m1_063_iwm_importer_only_whitelisted_hosts(monkeypatch):
    """IWM importer fetch path dials only ``www.ishares.com``."""
    captured = _capture_requests(monkeypatch)
    from biotech_sniper.universe import iwm_importer

    # ``fetch_csv_bytes`` IS the network primitive used by
    # :func:`iwm_importer.import_iwm_holdings`. Calling it
    # directly drives the same monkey-patched ``requests.get``
    # path the real importer takes.
    with pytest.raises(iwm_importer.UpstreamUnavailable):
        iwm_importer.fetch_csv_bytes(
            iwm_importer.DEFAULT_IWM_HOLDINGS_URL,
            timeout=0.1,
        )

    assert captured, "expected ≥ 1 attempted HTTP dial"
    _assert_only_whitelisted(captured)
    assert "www.ishares.com" in captured


def test_val_m1_063_sec_sic_classifier_only_whitelisted_hosts(
    monkeypatch, tmp_path
):
    """SEC SIC classifier hits only ``www.sec.gov`` + ``data.sec.gov``.

    The classifier resolves a ticker via two HTTP calls:

    1. CIK→ticker map (``www.sec.gov/files/company_tickers_exchange.json``),
       lazily loaded once per process.
    2. Per-CIK submissions (``data.sec.gov/submissions/CIK<cik>.json``).

    The mitm fixture intercepts both call paths and raises
    ``ConnectionError`` so no real bytes leave the runner.
    """
    captured = _capture_requests(monkeypatch)
    from biotech_sniper.classifiers import sec_sic

    classifier = sec_sic.SECSICClassifier(
        db_path=tmp_path / "sic_cache.db",
        timeout=0.1,
        # Disable inter-request throttling so the test is fast;
        # the throttle does not interact with monkey-patched HTTP.
        min_inter_request_seconds=0.0,
        # Single attempt — the mitm raises ConnectionError, which
        # the classifier wraps as SECTransientError after retries.
        max_retry_attempts=1,
    )
    # ``resolve_ticker_sic`` first triggers the ticker→CIK map
    # fetch (www.sec.gov), which is the public network entry
    # point. With the mitm raising ConnectionError, this surfaces
    # as a SECTransientError.
    with pytest.raises(Exception):
        classifier.resolve_ticker_sic("VRTX")

    assert captured, "expected ≥ 1 attempted HTTP dial"
    _assert_only_whitelisted(captured)
    assert "www.sec.gov" in captured


def test_val_m1_063_pdufa_scraper_only_whitelisted_hosts(monkeypatch):
    """PDUFA scraper contacts only the documented upstream hosts."""
    captured = _capture_requests(monkeypatch)
    from biotech_sniper.calendar import pdufa

    # Default upstream is BiopharmCatalyst.
    with pytest.raises(pdufa.UpstreamUnavailable):
        pdufa.fetch_html_bytes(
            pdufa.DEFAULT_PDUFA_SOURCE_URL,
            timeout=0.1,
        )
    # Operator override path — FDA approvals page.
    with pytest.raises(pdufa.UpstreamUnavailable):
        pdufa.fetch_html_bytes(
            "https://www.fda.gov/drugs/development-approval-process-drugs/"
            "drug-approvals-and-databases",
            timeout=0.1,
        )

    assert captured, "expected ≥ 1 attempted HTTP dial"
    _assert_only_whitelisted(captured)
    assert "www.biopharmcatalyst.com" in captured
    assert "www.fda.gov" in captured


def test_val_m1_063_ema_scraper_only_whitelisted_hosts(monkeypatch):
    """EMA/CHMP scraper contacts only ``www.ema.europa.eu``."""
    captured = _capture_requests(monkeypatch)
    from biotech_sniper.calendar import ema

    with pytest.raises(ema.UpstreamUnavailable):
        ema.fetch_html_bytes(
            ema.DEFAULT_EMA_SOURCE_URL,
            timeout=0.1,
        )

    assert captured, "expected ≥ 1 attempted HTTP dial"
    _assert_only_whitelisted(captured)
    assert "www.ema.europa.eu" in captured


def test_val_m1_063_full_m1_ingestion_subset_only_whitelisted(monkeypatch, tmp_path):
    """Concatenated M1 ingestion run touches a subset of the whitelist.

    Contract evidence (VAL-M1-063): outbound requests fall inside
    ``{www.ishares.com, data.sec.gov, www.sec.gov, www.fda.gov,
    www.ema.europa.eu, www.biopharmcatalyst.com}``. Trial-calendar
    refresh is a pure SQL merge — no network — so it is not
    exercised here.
    """
    captured = _capture_requests(monkeypatch)
    from biotech_sniper.universe import iwm_importer
    from biotech_sniper.classifiers import sec_sic
    from biotech_sniper.calendar import pdufa, ema

    # IWM
    try:
        iwm_importer.fetch_csv_bytes(
            iwm_importer.DEFAULT_IWM_HOLDINGS_URL, timeout=0.1
        )
    except Exception:
        pass

    # SEC SIC — exercise both endpoints (ticker map + per-CIK).
    classifier = sec_sic.SECSICClassifier(
        db_path=tmp_path / "sic_cache.db",
        timeout=0.1,
        min_inter_request_seconds=0.0,
        max_retry_attempts=1,
    )
    try:
        classifier.fetch_sic_for_cik("0000875320")
    except Exception:
        pass
    try:
        # The ticker→CIK map endpoint is exposed via the
        # public :data:`CIK_TICKERS_URL` constant. Calling
        # ``_send_request`` directly is private, but the constant
        # is part of the module's documented surface, so we can
        # trigger the dial via ``requests.get`` itself.
        import requests
        requests.get(sec_sic.CIK_TICKERS_URL, timeout=0.1)
    except Exception:
        pass

    # PDUFA — default + override.
    try:
        pdufa.fetch_html_bytes(pdufa.DEFAULT_PDUFA_SOURCE_URL, timeout=0.1)
    except Exception:
        pass
    try:
        pdufa.fetch_html_bytes(
            "https://www.fda.gov/drugs/some-pdufa-page", timeout=0.1
        )
    except Exception:
        pass

    # EMA
    try:
        ema.fetch_html_bytes(ema.DEFAULT_EMA_SOURCE_URL, timeout=0.1)
    except Exception:
        pass

    assert captured, "expected ≥ 1 attempted HTTP dial"
    _assert_only_whitelisted(captured)

    # Per-VAL-M1-063 the captured set MUST be a subset of:
    expected_subset = {
        "www.ishares.com",
        "data.sec.gov",
        "www.sec.gov",
        "www.fda.gov",
        "www.ema.europa.eu",
        "www.biopharmcatalyst.com",
    }
    extra = sorted(set(captured) - expected_subset)
    assert not extra, (
        "VAL-M1-063: ingestion contacted hosts outside the documented "
        f"M1 subset: {extra}"
    )


# ---------------------------------------------------------------------------
# Bonus invariant — every M1 module's URL constant resolves to a
# whitelisted host. Static guard against future regressions.
# ---------------------------------------------------------------------------


def test_m1_module_url_constants_are_whitelisted():
    """Every documented URL constant in M1 modules is whitelisted."""
    from biotech_sniper.universe import iwm_importer
    from biotech_sniper.classifiers import sec_sic
    from biotech_sniper.calendar import pdufa, ema

    constants: list[tuple[str, str]] = [
        ("iwm_importer.DEFAULT_IWM_HOLDINGS_URL", iwm_importer.DEFAULT_IWM_HOLDINGS_URL),
        ("sec_sic.CIK_TICKERS_URL", sec_sic.CIK_TICKERS_URL),
        # SUBMISSIONS_URL_TEMPLATE has a {cik} placeholder — fill
        # in a 10-digit value so urlparse resolves correctly.
        (
            "sec_sic.SUBMISSIONS_URL_TEMPLATE",
            sec_sic.SUBMISSIONS_URL_TEMPLATE.format(cik="0000875320"),
        ),
        ("pdufa.DEFAULT_PDUFA_SOURCE_URL", pdufa.DEFAULT_PDUFA_SOURCE_URL),
        ("ema.DEFAULT_EMA_SOURCE_URL", ema.DEFAULT_EMA_SOURCE_URL),
    ]
    bad: list[tuple[str, str, str]] = []
    for name, url in constants:
        host = urlparse(url).hostname or ""
        if host not in ALLOWED_NETWORK_HOSTS:
            bad.append((name, url, host))
    assert not bad, (
        "M1 URL constants resolved to non-whitelisted hosts: " + str(bad)
    )
