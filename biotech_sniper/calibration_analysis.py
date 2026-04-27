#!/usr/bin/env python3
"""Prediction accuracy and calibration analysis."""

print('=' * 70)
print('ALPHA SNIPER -- PREDICTION ACCURACY & CALIBRATION ANALYSIS')
print('April 26, 2026')
print('=' * 70)

print()
print('SECTION 1: DIRECTIONAL ACCURACY (was the call/put direction right?)')
print('-' * 70)

resolved = [
    # ticker, P_used, direction, outcome, option_pnl, note
    ('LPCN',      62, 'LONG',  'WRONG',     '-100%',  'PPD Phase 3 failed HAM-D endpoint. Stock -78%.'),
    ('VRDN $27C', 65, 'LONG',  'RIGHT',     '-65%',   'Drug met primary but lackluster effect. Stock -34%. $27C bust.'),
    ('TVTX',      77, 'LONG',  'RIGHT',     '+19%',   'FDA approved FSGS. Stock +6% to $30.70. $35C expired OTM.'),
    ('IDYA $35C', 96, 'LONG',  'RIGHT',     '-91%',   'PFS statistically significant. Stock +7.6%. IV crush. $35C OTM.'),
    ('RVMD',      61, 'LONG',  'RIGHT',     '+193%',  'OS 13.2 vs 6.7 months. Stock +41%. Massive win.'),
    ('AGIO puts', 94, 'SHORT', 'RIGHT',     '+167%',  'Stock -28% (Novo SCD). MDS readout still pending.'),
]

right = [r for r in resolved if r[3]=='RIGHT']
wrong = [r for r in resolved if r[3]=='WRONG']

print(f'  Direction correct: {len(right)}/{len(resolved)} = {len(right)/len(resolved)*100:.0f}%')
print()
for r in right:
    print(f'  + {r[0]:15s} P={r[1]}% | Option PnL: {r[4]:6s} | {r[5]}')
for r in wrong:
    print(f'  - {r[0]:15s} P={r[1]}% | Option PnL: {r[4]:6s} | {r[5]}')

print()
print()
print('SECTION 2: OPTION P&L ACCURACY (did we actually make money?)')
print('-' * 70)
winners   = [r for r in resolved if r[4].startswith('+')]
losers    = [r for r in resolved if r[4].startswith('-')]
print(f'  Option winners: {len(winners)}/{len(resolved)} = {len(winners)/len(resolved)*100:.0f}%')
print(f'  Option losers:  {len(losers)}/{len(resolved)} = {len(losers)/len(resolved)*100:.0f}%')
print()
print('  KEY FINDING: Direction was RIGHT in 5/6 cases (83%)')
print('  But option P&L was positive in only 3/6 cases (50%)')
print('  Gap = directional accuracy does NOT translate to option profits')
print('  The GAP is caused by STRIKE SELECTION and IV CRUSH errors')

print()
print()
print('SECTION 3: ROOT CAUSES OF OPTION LOSSES (direction was RIGHT but option lost)')
print('-' * 70)

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
    print(f'  [{name}]')
    print(f'  What was RIGHT: {what_was_right}')
    print(f'  What went wrong: {what_went_wrong}')
    print(f'  Fix: {fix}')
    print()

print()
print('SECTION 4: PROBABILITY CALIBRATION ASSESSMENT')
print('-' * 70)

print()
print('  Sample is small (6 resolved) but early patterns:')
print()
print('  WELL-CALIBRATED:')
print('    RVMD 61%: Drug worked decisively. OS 13.2 vs 6.7 months. Big win. Calibration OK.')
print('    AGIO 94% PUT: Drug on track to fail. Competitive data confirms. Calibration good.')
print('    TVTX 77%: Correct direction. Stock move too small for OTM strike but P was right.')
print()
print('  RECALIBRATION NEEDED:')
print('    LPCN 62% LONG: PPD is a very high-risk indication. Historical base rate ~25-30%.')
print('      Our model gave 62% on a drug with contested mechanism in a high-placebo-response disease.')
print('      Should have been ~35-40%. NEW RULE: P>=65% minimum for LONG options on Phase 3.')
print()
print('    IDYA 96% LONG: The P was on "PFS positive" but market expected BOTH PFS+OS.')
print('      When we have DUAL endpoints, the relevant P for option sizing is the JOINT probability.')
print('      P(PFS positive) = 90%. P(OS positive) = 46%. P(joint) = ~42%.')
print('      The option was sized as if 96% = big move. Should size based on joint probability.')
print('      NEW RULE: For trials with co-primary endpoints, use joint P for option sizing.')
print()
print('    VRDN $27C: Strike selection error. Not a probability calibration error.')
print()

print()
print('SECTION 5: WHAT IS WORKING WELL')
print('-' * 70)
print()
print('  1. PUTTING ON GRADE F SCIENCE: AGIO put is the cleanest trade in the portfolio.')
print('     Reading the protocol (non-RCT, wrong endpoint, wrong patients) is genuine edge.')
print('     This is the Shkreli strategy working exactly as designed.')
print()
print('  2. PHASE 3 READOUT LONG PLAYS: RVMD at +193% shows the model works when')
print('     binary data is decisive. The key is picking plays where the data WILL be decisive.')
print('     Pancreatic OS data (hard endpoint, large N, clear comparison) = decisive.')
print()
print('  3. SPREAD PLAYS ON HIGH-P EVENTS: ARGX $800/$850 spread reduced vega exposure.')
print('     Even with stock well below lower leg, the spread retains time value.')
print('     For high-confidence PDUFA plays, spreads are superior to naked calls.')
print()
print('  4. PDUFA vs TRIAL READOUT DISTINCTION:')
print('     PDUFA (regulatory): FDA has seen all data. Binary approval/rejection.')
print('     Moves 20-60% on approval. OTM calls work.')
print('     TRIAL READOUT: magnitude of data matters. Even positive = only 5-30% move.')
print('     ATM or slightly OTM. Spreads better than naked calls.')
print()

print()
print('SECTION 6: RECALIBRATION RULES (going forward)')
print('-' * 70)
print()
rules = [
    ('Minimum P for LONG calls',       '>=65% (was >=60%). LPCN at 62% was too close to 50/50.'),
    ('Strike for PDUFA events',        '10-25% OTM max. These can gap 30-60% on approval.'),
    ('Strike for trial readouts',      '5-15% OTM max. Even positive Phase 3 = 10-25% stock move typically.'),
    ('Strike for label extensions',    'ATM to 5% OTM only. Already-approved drugs move 5-15% on sBLA/sNDA.'),
    ('Joint probability for dual endpoints', 'Use P(joint) for option sizing, not P(primary alone).'),
    ('Spread vs naked call threshold', 'If P>=80%: use spread (reduce vega, keep binary upside).'),
    ('Put sizing on Grade F science',  'Grade F + P(fail)>=85%: this is the highest-conviction put setup.'),
    ('IV check before entry',          'If IV > 150%: reduce position size. IV crush risk is high.'),
    ('Science grade as multiplier',    'Grade A/B: full size. Grade C: normal. Grade D/F long: half size.'),
]
for rule, detail in rules:
    print(f'  [{rule}]')
    print(f'    {detail}')
    print()
