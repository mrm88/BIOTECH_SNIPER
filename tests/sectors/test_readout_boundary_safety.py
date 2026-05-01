"""Boundary-safe matching for the bare ``p1``/``p2``/``p3`` READOUT
short-tokens in
:func:`biotech_sniper.sectors.unified_scorer.detect_catalyst_type`.

Feature: f-fix-misc-05-readout-boundary-safety
=================================================

Background
----------
f-misc-05 widened ``detect_catalyst_type.readout_signals`` to include
the bare bigrams ``p1``/``p2``/``p3`` so callers passing the bare
phase shorthand still resolve to ``READOUT`` via the single helper
(VAL-M3-051's "all phase shorthands map to READOUT" expectation).
However, those bigrams were added as plain substring members and the
helper does substring-containment matching with ``in``, so unrelated
strings that happen to contain ``p1``/``p2``/``p3`` as part of a
longer token (e.g. ``sp2x``, ``type-p3``, ``flap1``,
``protein-p2-binding-domain``) were misclassified as READOUT.

Required behavior (this regression's contract)
----------------------------------------------
1. The bare ``p1``/``p2``/``p3`` short-tokens MUST match only with
   word-boundary semantics. The boundary-safe regex used by
   ``detect_catalyst_type`` is::

       r'(?<![A-Za-z0-9_-])(p[123])(?![A-Za-z0-9_-])'

   i.e. ``p1``/``p2``/``p3`` are NOT preceded or followed by an
   alphanumeric character, hyphen, or underscore.

2. Multi-word readout phrases (``p1 readout``, ``p2 readout``,
   ``p3 readout``, ``phase 1``, ``phase 2``, ``phase 3``,
   ``first-in-human``, ``first in human``) retain plain substring
   matching since the spaces and hyphens give them natural
   boundaries.

3. Negative cases (synthetic strings that contain the bigram inside a
   longer token) MUST NOT classify as READOUT. These regressions are
   what motivated this fix in the first place.

The positive-case set below mirrors the f-misc-05 contract: each of
the multi-word and bare phase tokens still resolves to ``READOUT``.

The negative-case set is the boundary-safety regression body for this
feature: each entry is a real-or-synthetic string that contains the
bigram ``p1``/``p2``/``p3`` as a SUBSTRING of a longer alphanumeric
or hyphenated token, and MUST NOT classify as READOUT.
"""

from __future__ import annotations

import pytest

from biotech_sniper.sectors.unified_scorer import detect_catalyst_type


# ---------------------------------------------------------------------------
# Positive cases — must resolve to ``READOUT``.
# ---------------------------------------------------------------------------

_POSITIVE_BARE_PHASE_TOKENS: tuple[str, ...] = (
    "p1",
    "p2",
    "p3",
    "P1",
    "P2",
    "P3",
)

_POSITIVE_MULTI_WORD_READOUT_PHRASES: tuple[str, ...] = (
    "p1 readout",
    "p2 readout",
    "p3 readout",
    "P2 readout reports primary endpoint hit",
    "phase 1",
    "phase 2",
    "phase 3",
    "first-in-human",
    "first in human",
)


@pytest.mark.parametrize("notes", _POSITIVE_BARE_PHASE_TOKENS)
def test_bare_phase_tokens_with_word_boundaries_classify_as_readout(notes: str) -> None:
    """Bare ``p1``/``p2``/``p3`` (and uppercase variants) — i.e. the token
    is the entire string, with whitespace/end-of-string boundaries on
    both sides — MUST classify as ``READOUT``."""

    assert detect_catalyst_type(notes=notes) == "READOUT"


@pytest.mark.parametrize("notes", _POSITIVE_MULTI_WORD_READOUT_PHRASES)
def test_multi_word_readout_phrases_classify_as_readout(notes: str) -> None:
    """Multi-word readout phrases retain plain substring matching and
    MUST classify as ``READOUT``."""

    assert detect_catalyst_type(notes=notes) == "READOUT"


# ---------------------------------------------------------------------------
# Negative boundary-safety cases — MUST NOT classify as ``READOUT``.
#
# Each entry is a string that contains ``p1``/``p2``/``p3`` as a
# substring of a longer token (alphanumeric, hyphen, or underscore on
# at least one side). Naive substring containment misclassified them
# under f-misc-05 prior to this fix.
# ---------------------------------------------------------------------------

_NEGATIVE_BOUNDARY_REGRESSIONS: tuple[str, ...] = (
    "sp2x receptor activation",
    "type-p3 collagen study",
    "flap1 mutation analysis",
    "protein-p2-binding-domain",
    "company files 8-k for compp1ngraduation",
    "capital p1 lazy lite",
)


@pytest.mark.parametrize("notes", _NEGATIVE_BOUNDARY_REGRESSIONS)
def test_negative_boundary_regressions_do_not_classify_as_readout(notes: str) -> None:
    """Strings containing ``p1``/``p2``/``p3`` only as a substring of a
    longer alphanumeric/hyphenated token MUST NOT classify as
    ``READOUT``. This is the regression body for
    f-fix-misc-05-readout-boundary-safety."""

    assert detect_catalyst_type(notes=notes) != "READOUT"
