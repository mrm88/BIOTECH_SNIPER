#!/usr/bin/env python3
"""Prediction accuracy and calibration analysis.

Originally written as a top-level script with ~74 ``print()`` calls. The
top-level prints were converted to a ``main()`` function emitting
:mod:`logging` records (per f-m4-02) so the project owns its
log surface end-to-end. Running the module still produces the report
on stdout via the JSON stream handler installed by
:mod:`biotech_sniper.logging_setup`.
"""

from __future__ import annotations

import logging

from biotech_sniper import logging_setup  # noqa: F401 — installs JSON formatter on import

log = logging.getLogger(__name__)


def main() -> None:
    """Render the calibration analysis to the structured logger."""
    log.info("calibration_report_section", extra={
        "event": "calibration_report_section",
        "section": "header",
        "title": "ALPHA SNIPER -- PREDICTION ACCURACY & CALIBRATION ANALYSIS",
        "as_of": "April 26, 2026",
    })

    resolved = [
        # ticker, P_used, direction, outcome, option_pnl, note
        ('LPCN',      62, 'LONG',  'WRONG',     '-100%',  'PPD Phase 3 failed HAM-D endpoint. Stock -78%.'),
        ('VRDN $27C', 65, 'LONG',  'RIGHT',     '-65%',   'Drug met primary but lackluster effect. Stock -34%. $27C bust.'),
        ('TVTX',      77, 'LONG',  'RIGHT',     '+19%',   'FDA approved FSGS. Stock +6% to $30.70. $35C expired OTM.'),
        ('IDYA $35C', 96, 'LONG',  'RIGHT',     '-91%',   'PFS statistically significant. Stock +7.6%. IV crush. $35C OTM.'),
        ('RVMD',      61, 'LONG',  'RIGHT',     '+193%',  'OS 13.2 vs 6.7 months. Stock +41%. Massive win.'),
        ('AGIO puts', 94, 'SHORT', 'RIGHT',     '+167%',  'Stock -28% (Novo SCD). MDS readout still pending.'),
    ]

    right = [r for r in resolved if r[3] == 'RIGHT']
    wrong = [r for r in resolved if r[3] == 'WRONG']
    log.info("calibration_section_directional_accuracy", extra={
        "event": "calibration_section_directional_accuracy",
        "right_count": len(right),
        "wrong_count": len(wrong),
        "total": len(resolved),
        "right": [{"ticker": r[0], "p": r[1], "pnl": r[4], "note": r[5]} for r in right],
        "wrong": [{"ticker": r[0], "p": r[1], "pnl": r[4], "note": r[5]} for r in wrong],
    })

    winners = [r for r in resolved if r[4].startswith('+')]
    losers = [r for r in resolved if r[4].startswith('-')]
    log.info("calibration_section_option_pnl", extra={
        "event": "calibration_section_option_pnl",
        "winners": len(winners),
        "losers": len(losers),
        "key_finding": (
            "Direction was RIGHT in 5/6 cases (83%) but option P&L was "
            "positive in only 3/6 cases (50%). Gap = directional accuracy "
            "does NOT translate to option profits; STRIKE SELECTION + "
            "IV CRUSH errors close the gap."
        ),
    })

    gaps = [
        ('VRDN $27C',
         'Correct direction (drug worked)',
         'Strike too far OTM. Stock was $18.84, $27C = 43% move needed. Revelation: TED drugs show modest stock moves on Phase 2b because market already knew mechanism works (batoclimab). -65% option despite correct direction.',
         'Use $20C or $22C for mid-stage TED data. Modest mover category.'),
        ('TVTX $35C',
         'Correct direction (FDA approved)',
         'Strike too far OTM for a label extension. Stock $28.96 -> $30.70 (+6%). $35C needed +20% move. Label extensions of already-approved drugs produce 5-15% moves, not 20%+. IV=305% priced in a bigger move that never came.',
         'For sNDA/sBLA label extensions: use ATM calls. The binary is real but the magnitude is smaller.'),
        ('IDYA $35C',
         'Correct direction (PFS positive, first ever in uveal melanoma)',
         'IV crush on partial success. Stock +7.6% vs 30% implied move. The issue: we used P=96% for sizing/strike selection. But Warpspeed showed P=90% on PFS alone and 46% on PFS+OS. The market priced in the 30% move based on the HIGHER bar (OS confirmation). PFS only = modest re-rate.',
         'For multi-endpoint trials: use the HARDER endpoint probability for option sizing. PFS positive alone does not justify 30% implied move. Should have used $32C or $33C not $35C.'),
    ]
    for name, what_was_right, what_went_wrong, fix in gaps:
        log.info("calibration_root_cause", extra={
            "event": "calibration_root_cause",
            "ticker": name,
            "what_was_right": what_was_right,
            "what_went_wrong": what_went_wrong,
            "fix": fix,
        })

    log.info("calibration_section_probability_assessment", extra={
        "event": "calibration_section_probability_assessment",
        "well_calibrated": [
            "RVMD 61%: drug worked decisively; OS 13.2 vs 6.7 months; calibration OK.",
            "AGIO 94% PUT: drug on track to fail; competitive data confirms; calibration good.",
            "TVTX 77%: correct direction; stock move too small for OTM strike but P was right.",
        ],
        "recalibration_needed": [
            "LPCN 62% LONG: PPD high-risk, base rate ~25-30%, should have been ~35-40%. "
            "NEW RULE: P>=65% minimum for LONG options on Phase 3.",
            "IDYA 96% LONG: dual endpoints; should have used joint P (~42%), not "
            "primary alone (90%). NEW RULE: for co-primary endpoints use joint P.",
            "VRDN $27C: strike selection error, not a probability calibration error.",
        ],
    })

    log.info("calibration_section_what_works", extra={
        "event": "calibration_section_what_works",
        "items": [
            "Putting on Grade F science (e.g. AGIO PUT) — Shkreli strategy.",
            "Phase 3 readout long plays with decisive endpoints (RVMD +193%).",
            "Spread plays on high-P events (ARGX $800/$850).",
            "PDUFA vs trial readout distinction — different magnitude expectations.",
        ],
    })

    rules = [
        ('Minimum P for LONG calls', '>=65% (was >=60%). LPCN at 62% was too close to 50/50.'),
        ('Strike for PDUFA events', '10-25% OTM max. These can gap 30-60% on approval.'),
        ('Strike for trial readouts', '5-15% OTM max. Even positive Phase 3 = 10-25% stock move typically.'),
        ('Strike for label extensions', 'ATM to 5% OTM only. Already-approved drugs move 5-15% on sBLA/sNDA.'),
        ('Joint probability for dual endpoints', 'Use P(joint) for option sizing, not P(primary alone).'),
        ('Spread vs naked call threshold', 'If P>=80%: use spread (reduce vega, keep binary upside).'),
        ('Put sizing on Grade F science', 'Grade F + P(fail)>=85%: highest-conviction put setup.'),
        ('IV check before entry', 'If IV > 150%: reduce position size. IV crush risk is high.'),
        ('Science grade as multiplier', 'Grade A/B: full size. Grade C: normal. Grade D/F long: half size.'),
    ]
    log.info("calibration_section_rules", extra={
        "event": "calibration_section_rules",
        "rules": [{"rule": r, "detail": d} for r, d in rules],
    })


if __name__ == "__main__":
    main()
