"""Sector-namespaced sub-tests for unified_scorer.

Feature: f-misc-05-unify-readout-catalyst-keywords.

Pins the canonical READOUT keyword vocabulary as the single
source-of-truth in
:func:`biotech_sniper.sectors.unified_scorer.detect_catalyst_type`. The
five tokens VAL-M3-051 mandates ('p3 readout', 'p2 readout',
'p1 readout', 'phase 1', 'first-in-human') were previously absent
from ``readout_signals`` and worked around in
``stage2_dispatcher._EXTRA_READOUT_SUBSTRINGS`` /
``_EXTRA_READOUT_WHOLE_WORDS`` pre-checks. After f-misc-05 these
tokens live on ``detect_catalyst_type`` itself and the dispatcher
delegates 100% of catalyst classification to that helper.
"""

from __future__ import annotations

import pytest

from biotech_sniper.sectors.unified_scorer import detect_catalyst_type


# Tokens that VAL-M3-051 mandates map to ``READOUT`` and that f-misc-05
# pulls into ``detect_catalyst_type.readout_signals`` directly so that
# no caller (Stage-2 dispatcher in particular) needs to maintain a
# parallel keyword pre-check.
_REQUIRED_READOUT_TOKENS: tuple[str, ...] = (
    "p3 readout",
    "p2 readout",
    "p1 readout",
    "phase 1",
    "first-in-human",
)


@pytest.mark.parametrize("token", _REQUIRED_READOUT_TOKENS)
def test_required_readout_tokens_map_to_readout_directly(token: str) -> None:
    """Each VAL-M3-051 READOUT token returns ``'READOUT'`` from
    :func:`detect_catalyst_type` directly (no dispatcher pre-check
    workaround required)."""

    assert detect_catalyst_type(notes=token) == "READOUT"


def test_dispatcher_no_longer_defines_extra_readout_pre_check_constants() -> None:
    """The dispatcher MUST NOT carry ``_EXTRA_READOUT_SUBSTRINGS`` or
    ``_EXTRA_READOUT_WHOLE_WORDS`` constants any more — the keyword
    vocabulary is consolidated into
    :func:`detect_catalyst_type.readout_signals`."""

    import biotech_sniper.exec.stage2_dispatcher as mod

    assert not hasattr(mod, "_EXTRA_READOUT_SUBSTRINGS"), (
        "stage2_dispatcher._EXTRA_READOUT_SUBSTRINGS must be retired "
        "after f-misc-05; readout vocabulary lives on "
        "detect_catalyst_type.readout_signals only."
    )
    assert not hasattr(mod, "_EXTRA_READOUT_WHOLE_WORDS"), (
        "stage2_dispatcher._EXTRA_READOUT_WHOLE_WORDS must be retired "
        "after f-misc-05; readout vocabulary lives on "
        "detect_catalyst_type.readout_signals only."
    )
