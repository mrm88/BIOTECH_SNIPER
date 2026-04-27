"""Boundary + fixture verification for f-m2-08.

This module pins down the contract that ``f-m2-08-boundary-and-fixtures``
fulfils:

* **All three LLM provider cassette directories exist with content.**
  ``tests/fixtures/cassettes/{xai,claude,gemini}/`` each must contain
  at least one ``.json`` cassette so the deterministic-replay tests for
  the corresponding provider can execute (VAL-M2-065).

* **No live secret material has leaked into committed cassettes.**
  Even though the fake-client cassette format never encodes raw HTTP
  headers, this test re-asserts the redaction sweep across every JSON
  file under ``tests/fixtures/cassettes/`` so any future cassette that
  is hand-edited or recorded against a live endpoint cannot regress
  the invariant (VAL-M2-068). The acceptance set is empty: any token
  matching a known provider's secret prefix fails the test.

* **Ingestion fixtures for CT.gov, SEC EDGAR, and yfinance are
  committed and well-formed.** Per the M2 sealing checklist
  (VAL-M2-067), ``tests/fixtures/`` includes ``ctgov/``,
  ``sec_edgar/``, and ``yfinance/`` subdirectories with at least one
  fixture each. The fixtures are loadable (JSON parses, XML parses)
  and contain the schema fields callers rely on.

* **A divergence-detection test exists and is collected.** Per
  VAL-M2-069, the ensemble test module must contain at least one
  ``test_divergence_*`` function. We assert this from the test suite
  itself so the canonical divergence test cannot silently disappear.
"""

from __future__ import annotations

import importlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
CASSETTES_DIR = FIXTURES_DIR / "cassettes"


# ---------------------------------------------------------------------------
# Cassette presence and redaction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["xai", "claude", "gemini"])
def test_cassettes_present_for_each_provider(provider: str) -> None:
    """Every supported provider has at least one cassette committed.

    Mirrors VAL-M2-065: ``tests/fixtures/cassettes/`` contains
    per-provider directories ``xai/``, ``claude/`` (or ``anthropic/``),
    and ``gemini/``, each holding at least one ``.json`` cassette.
    """
    provider_dir = CASSETTES_DIR / provider
    assert provider_dir.is_dir(), (
        f"missing cassette directory: {provider_dir}"
    )
    cassettes = sorted(provider_dir.glob("*.json"))
    assert cassettes, (
        f"no .json cassettes found under {provider_dir}; the "
        f"{provider} client tests cannot replay deterministically"
    )


# Patterns that strongly indicate a real provider secret. Each pattern
# captures the documented prefix and a length boundary aligned with the
# vendor's actual key shape:
#
#   xAI         — ``xai-`` prefix + at least 20 chars of base62/base64.
#   OpenAI/xAI  — ``sk-`` prefix + at least 20 chars (covers project
#                  + classic keys; OpenAI now prefixes ``sk-proj-`` /
#                  ``sk-svcacct-`` but both still match this regex).
#   Anthropic   — ``sk-ant-`` prefix + at least 20 chars.
#   Google API  — ``AIza`` prefix + 35 chars (canonical AIza length).
#   GitHub PAT  — ``ghp_`` / ``ghs_`` / ``gho_`` prefix + 30+ chars.
#   Alpaca live — ``PKLIVE`` prefix is the documented live-account
#                  marker.
#
# The ``ALLOWED_LITERAL_TOKENS`` allow-list explicitly carves out
# placeholder strings used in fixtures (e.g. README examples that
# document what a redacted Authorization header looks like).
SECRET_PATTERNS = [
    re.compile(r"xai-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-(?!ant-)[A-Za-z0-9_-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"gh[psu]_[A-Za-z0-9]{30,}"),
    re.compile(r"PKLIVE[A-Z0-9]{14,}"),
]
ALLOWED_LITERAL_TOKENS = {
    "xai-test-key",
    "xai-fixture-key",
    "sk-ant-test-key",
    "sk-test-key",
    "AIza-test-fixture-key-do-not-use-in-production",
}


def _scan_for_secrets(path: Path) -> list[tuple[Path, str]]:
    """Return any (path, matched-substring) pairs that look like secrets."""
    text = path.read_text(encoding="utf-8", errors="replace")
    hits: list[tuple[Path, str]] = []
    for pat in SECRET_PATTERNS:
        for match in pat.finditer(text):
            token = match.group(0)
            if token in ALLOWED_LITERAL_TOKENS:
                continue
            hits.append((path, token))
    return hits


def test_cassettes_contain_no_live_secrets() -> None:
    """No committed cassette contains a live-looking provider key.

    Mirrors VAL-M2-068: a recursive grep over
    ``tests/fixtures/cassettes/`` for the canonical provider secret
    prefixes returns no matches.
    """
    cassette_files = sorted(CASSETTES_DIR.rglob("*.json"))
    assert cassette_files, (
        "no cassettes found at all under "
        f"{CASSETTES_DIR}; the redaction sweep would otherwise be "
        "vacuously true"
    )
    all_hits: list[tuple[Path, str]] = []
    for cassette in cassette_files:
        all_hits.extend(_scan_for_secrets(cassette))
    assert not all_hits, (
        "found tokens matching a known provider secret prefix in "
        f"committed cassettes: {all_hits!r}"
    )


def test_cassette_readmes_contain_no_live_secrets() -> None:
    """Cassette READMEs document recording flow but never embed real keys."""
    readmes = sorted(CASSETTES_DIR.rglob("README.md"))
    all_hits: list[tuple[Path, str]] = []
    for readme in readmes:
        all_hits.extend(_scan_for_secrets(readme))
    assert not all_hits, (
        f"cassette README files contain secret-shaped tokens: {all_hits!r}"
    )


# ---------------------------------------------------------------------------
# CT.gov, SEC, yfinance fixtures committed and well-formed
# ---------------------------------------------------------------------------


def test_ctgov_fixtures_present_and_parseable() -> None:
    """CT.gov v2 fixtures load and contain the expected envelope keys.

    Mirrors VAL-M2-067 (CT.gov leg): ``tests/fixtures/ctgov/`` exists
    with at least one fixture file that the ingestion tests can replay.
    """
    ctgov_dir = FIXTURES_DIR / "ctgov"
    assert ctgov_dir.is_dir(), f"missing fixtures directory: {ctgov_dir}"
    fixtures = sorted(ctgov_dir.glob("*.json"))
    assert fixtures, "no CT.gov fixtures committed"

    single = json.loads((ctgov_dir / "study_NCT05123456.json").read_text())
    proto = single["protocolSection"]
    assert proto["identificationModule"]["nctId"] == "NCT05123456"
    assert proto["statusModule"]["overallStatus"] == "ACTIVE_NOT_RECRUITING"
    assert "PHASE3" in proto["designModule"]["phases"]

    bulk = json.loads((ctgov_dir / "studies_phase3_recruiting.json").read_text())
    assert isinstance(bulk["studies"], list)
    assert len(bulk["studies"]) >= 1
    # Every study in the bulk fixture is uniquely keyed by NCT ID.
    nct_ids = [
        s["protocolSection"]["identificationModule"]["nctId"]
        for s in bulk["studies"]
    ]
    assert len(set(nct_ids)) == len(nct_ids), (
        f"duplicate NCT IDs in bulk fixture: {nct_ids}"
    )


def test_sec_edgar_fixtures_present_and_parseable() -> None:
    """SEC EDGAR fixtures load and contain the expected schema.

    Mirrors VAL-M2-067 (SEC EDGAR leg): the 8-K Atom feed parses as
    valid XML, and the company-tickers fixture is a JSON object whose
    inner records carry ``cik_str`` / ``ticker`` / ``title`` keys.
    """
    sec_dir = FIXTURES_DIR / "sec_edgar"
    assert sec_dir.is_dir(), f"missing fixtures directory: {sec_dir}"
    files = sorted(sec_dir.iterdir())
    assert files, "no SEC EDGAR fixtures committed"

    atom_path = sec_dir / "edgar_8k_atom.xml"
    tree = ET.parse(atom_path)
    root = tree.getroot()
    # Atom default namespace.
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries = root.findall("atom:entry", ns)
    assert len(entries) >= 1, (
        f"SEC 8-K Atom feed has no <entry> elements: {atom_path}"
    )
    for entry in entries:
        title = entry.find("atom:title", ns)
        assert title is not None and title.text, (
            "every <entry> must carry a title"
        )
        link = entry.find("atom:link", ns)
        assert link is not None and link.attrib.get("href"), (
            "every <entry> must carry a <link href=...>"
        )

    tickers = json.loads((sec_dir / "company_tickers.json").read_text())
    assert isinstance(tickers, dict) and tickers, (
        "company_tickers.json must be a non-empty dict"
    )
    for key, rec in tickers.items():
        assert {"cik_str", "ticker", "title"}.issubset(rec.keys()), (
            f"record {key!r} missing required fields: {rec}"
        )
        assert isinstance(rec["cik_str"], int)
        assert isinstance(rec["ticker"], str)
        assert isinstance(rec["title"], str)


def test_yfinance_fixtures_present_and_parseable() -> None:
    """yfinance fixtures load and contain the expected schema.

    Mirrors VAL-M2-067 (yfinance leg): an options-chain fixture
    exists, contains both calls and puts, and every contract carries
    the schema fields ingestion code reads (strike, bid, ask, mid is
    derived).
    """
    yf_dir = FIXTURES_DIR / "yfinance"
    assert yf_dir.is_dir(), f"missing fixtures directory: {yf_dir}"
    files = sorted(yf_dir.glob("*.json"))
    assert files, "no yfinance fixtures committed"

    chain = json.loads((yf_dir / "options_chain_TESTX.json").read_text())
    assert chain["symbol"] == "TESTX"
    assert isinstance(chain["calls"], list) and chain["calls"], (
        "options_chain_TESTX.json missing calls"
    )
    assert isinstance(chain["puts"], list) and chain["puts"], (
        "options_chain_TESTX.json missing puts"
    )
    for leg in (*chain["calls"], *chain["puts"]):
        for field in ("strike", "bid", "ask", "openInterest", "impliedVolatility"):
            assert field in leg, (
                f"options leg missing field {field!r}: {leg}"
            )

    history = json.loads((yf_dir / "quote_history_TESTX.json").read_text())
    assert history["symbol"] == "TESTX"
    rows = history["rows"]
    assert isinstance(rows, list) and rows
    for row in rows:
        for field in ("date", "open", "high", "low", "close", "volume"):
            assert field in row, f"quote row missing {field!r}: {row}"


# ---------------------------------------------------------------------------
# Divergence test discoverability
# ---------------------------------------------------------------------------


def test_divergence_test_function_exists() -> None:
    """A divergence-detection test exists in the canonical ensemble module.

    Mirrors VAL-M2-069: the M2 ensemble test module exposes at least
    one ``test_divergence_*`` function so a static `pytest --collect-only`
    grep can confirm the divergence path is covered.
    """
    module = importlib.import_module("tests.scoring.test_ensemble")
    divergence_tests = [
        name for name in dir(module) if name.startswith("test_divergence_")
    ]
    assert divergence_tests, (
        "no test_divergence_* function found in tests.scoring.test_ensemble; "
        "the divergence-detection contract is unverified"
    )


def test_divergence_test_exercises_two_grade_gap() -> None:
    """The canonical divergence test must exercise the ≥2 grade gap rule.

    A grade like ``A`` vs ``B-`` is a 4-grade gap and must trigger
    divergence; ``B+`` vs ``B`` is a 1-grade gap and must NOT trigger.
    Re-running the function-level parametrised test here would
    duplicate it, so instead we assert against the module's
    parametrize markers.
    """
    module = importlib.import_module("tests.scoring.test_ensemble")
    fn = getattr(module, "test_divergence_flag_two_level_gap", None)
    assert fn is not None, "missing test_divergence_flag_two_level_gap"

    pytestmark = getattr(fn, "pytestmark", None)
    assert pytestmark, (
        "test_divergence_flag_two_level_gap must be parametrised "
        "to cover both gap < 2 and gap >= 2 cases"
    )
    parametrize_marks = [m for m in pytestmark if m.name == "parametrize"]
    assert parametrize_marks, "expected a @pytest.mark.parametrize decorator"
    cases = parametrize_marks[0].args[1]
    # Confirm both signs of the rule are exercised.
    flags = {c[2] for c in cases}
    assert flags == {True, False}, (
        f"divergence test must exercise both True and False flags; got {flags}"
    )
