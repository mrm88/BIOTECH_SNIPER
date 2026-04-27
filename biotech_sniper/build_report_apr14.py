#!/usr/bin/env python3
"""Alpha Sniper Run #15 — Tuesday April 14, 2026. Post-catalyst recap."""
import json, datetime, openpyxl
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter
from pathlib import Path

from biotech_sniper.paths import BASE_DIR as BASE
opts  = {r['ticker']: r for r in json.load(open(BASE/'state/options_chains_2026-04-14.json'))}
today_str = datetime.date.today().strftime('%B %d, %Y')

DARK='0D1117'; GBG='0D2818'; RBG='2D0A0A'; OBG='2D1A00'; HDR='161B22'
WHT='FFFFFF'; GRN='3FB950'; RED='F85149'; ORN='D29922'; YLW='E3B341'
GRY='8B949E'; BLU='58A6FF'; PRP='BC8CFF'

def cs(cell, bg=DARK, fg=WHT, bold=False, sz=10, al='left', wrap=False):
    cell.fill = PatternFill(fill_type='solid', fgColor=bg)
    cell.font = Font(color=fg, bold=bold, size=sz, name='Consolas')
    cell.alignment = Alignment(horizontal=al, vertical='center', wrap_text=wrap)

def hrow(ws, row, vals, bg=HDR):
    for c, v in enumerate(vals, 1):
        cell = ws.cell(row=row, column=c, value=v)
        cs(cell, bg=bg, fg=YLW, bold=True)

wb = openpyxl.Workbook()

# TAB 1 — Results + Qualifying Plays
ws1 = wb.active
ws1.title = '2.5x+ Plays'
ws1.sheet_properties.tabColor = '3FB950'
ws1.merge_cells('A1:L1')
t = ws1['A1']
t.value = f'ALPHA SNIPER  --  {today_str}  --  Run #15  --  9 qualifying (>=2.5x)'
cs(t, bg=DARK, fg=GRN, bold=True, sz=12, al='center')
ws1.row_dimensions[1].height = 28
hrow(ws1, 2, ['TICKER','DIRECTION','STRIKE','EXPIRY','MID FILL','MULTIPLE','$1k RETURN',
               'P(WIN)%','SCIENCE','OI','CATALYST DATE','NOTE'])
ws1.row_dimensions[2].height = 20

qualifying = [
    ('RGNX','CALLS','$10C','2026-07-17', 1.50, 4.2, 4200, 68,'F', 1509,'Apr-May 2026',
     'AbbVie $100M milestone 1H 2026. DMD gene therapy. Non-RCT standard for gene therapy.'),
    ('AXSM','CALLS','$200C','2026-05-15', 7.70,15.7,15700, 65,'D',  2228,'Apr 30 CERTAIN',
     'BEST MULTIPLE 15.7x. AXS-05 label extension already-approved drug. 16 days.'),
    ('IDYA','CALLS','$35C','2026-05-15', 1.82,13.2,13200, 75,'C', 11011,'NDA H2 2026',
     'POSITIVE Apr 13: statistically significant PFS. OS trend only = modest stock +7.6%. NDA H2 2026.'),
    ('RVMD','CALLS','$125C','2026-06-18',18.00, 6.7, 6700, 90,'C',   47,'NDA filing H2 2026',
     'PHASE 3 POSITIVE Apr 13: OS 13.2 vs 6.7 months. +41% to $136.30. OPTION NOW ITM. ~140% gain.'),
    ('AGIO','PUTS', '$30P','2026-08-21', 2.73, 6.7, 6700,  6,'F',  691,'May-Jun 2026',
     'P(fail)=94%. Grade F confirms put thesis.'),
    ('NTLA','CALLS','$15C','2026-07-17', 2.40, 4.4, 4400, 82,'C', 1771,'May-Jun 2026',
     'NEJM 3yr data published. BLA forces disclosure by June.'),
    ('VERA','CALLS','$55C','2027-01-15', 8.05, 2.9, 2900, 72,'C',   19,'Jul 7 CERTAIN',
     'Atacicept IgAN BLA. Jul 7 PDUFA. Stock $43.71. Spread=61%, OI=19 -- small size.'),
    ('PRAX','CALLS','$300C','2027-01-15',79.00, 3.4, 3400, 70,'C',   62,'Sep 27 CERTAIN',
     'First Nav channel SCN2A/8A blocker. Confirmed PDUFA. Spread=6%.'),
    ('MLTX','CALLS','$21C','2026-08-21', 4.10, 3.0, 3000,100,'B', 1041,'Jun-Jul 2026',
     'Warpspeed 100%. Grade B RCT triple-blind N=960.'),
]

row = 3
for ticker, direc, strike, expiry, mid, mult, k1, p, grade, oi, cat, note in qualifying:
    rbg = RBG if 'PUT' in direc else GBG
    gcol = {'A':GRN,'B':GRN,'C':YLW,'D':ORN,'F':RED}.get(grade, WHT)
    vals = [ticker, direc, strike, expiry, f'${mid:.2f}', f'{mult}x',
            f'${k1:,}', f'{p}%', f'Grade {grade}', oi, cat, note[:120]]
    for col, val in enumerate(vals, 1):
        c = ws1.cell(row=row, column=col, value=val)
        fg = (YLW if col==1 else (RED if 'PUT' in direc else GRN) if col==2
              else (ORN if mult>=10 else BLU) if col==6
              else (GRN if p>=60 else RED) if col==8
              else gcol if col==9 else WHT)
        cs(c, bg=rbg, fg=fg, bold=(col==1), wrap=(col==12))
    ws1.row_dimensions[row].height = 40
    row += 1

for i, w in enumerate([8,10,10,12,10,10,12,9,10,7,20,68], 1):
    ws1.column_dimensions[get_column_letter(i)].width = w

# TAB 2 — Yesterday's Results
ws2 = wb.create_sheet('Apr 13 Results')
ws2.sheet_properties.tabColor = 'F85149'
ws2.merge_cells('A1:F1')
t2 = ws2['A1']
t2.value = 'APRIL 13 CATALYST RESULTS -- 3 major events resolved'
cs(t2, bg=DARK, fg=RED, bold=True, sz=12, al='center')
ws2.row_dimensions[1].height = 24
hrow(ws2, 2, ['TICKER','EVENT','OUTCOME','STOCK MOVE','OPTION P&L','NOTES'])

results_data = [
    ('RVMD','RASolute 302 Phase 3 PDAC data','HUGELY POSITIVE -- OS 13.2 vs 6.7 months (+97%)',
     '+41.3% to $136.30','$125C Jun18: ~$8 entry -> $18 current = ~+140%',
     'Best outcome of any play. $125C now ITM by $11.30. AACR Apr 17-22 next. NDA filing H2 2026.'),
    ('TVTX','PDUFA sparsentan FSGS','APPROVED -- first ever FSGS drug',
     '+6.0% to $30.70','$35C Apr17: ~$3 entry -> $3.58 last = ~+19%',
     'Correct direction but stock did not clear $35 strike. Modest IV expansion. AUTO-REMOVED.'),
    ('IDYA','OptimUM-02 Phase 2/3 topline','POSITIVE PFS -- OS trend only',
     '+7.6% to $32.82','$35C May15: ~$5.35 entry -> $1.82 last = -66%',
     'PFS statistically significant (first ever). OS: early trend only = modest reaction. NDA H2 2026.'),
]
for i, rd in enumerate(results_data, 3):
    ticker, event, outcome, move, pnl, notes = rd
    rbg = GBG if 'POSITIVE' in outcome or 'APPROVED' in outcome else RBG
    for col, val in enumerate([ticker, event, outcome, move, pnl, notes], 1):
        c = ws2.cell(row=i, column=col, value=val)
        fg = (YLW if col==1 else GRN if ('POSITIVE' in str(val) or 'APPROVED' in str(val)) else WHT)
        cs(c, bg=rbg, fg=fg, bold=(col==1), wrap=(col in [2,3,5,6]))
    ws2.row_dimensions[i].height = 52

for i, w in enumerate([8,35,35,18,30,55], 1):
    ws2.column_dimensions[get_column_letter(i)].width = w

# TAB 3 — All Active
ws3 = wb.create_sheet('All Active')
ws3.sheet_properties.tabColor = '58A6FF'
ws3.merge_cells('A1:M1')
t3 = ws3['A1']
t3.value = f'ALL ACTIVE PLAYS -- {today_str} -- 10 plays (TVTX removed)'
cs(t3, bg=DARK, fg=BLU, bold=True, sz=11, al='center')
ws3.row_dimensions[1].height = 24
hrow(ws3, 2, ['TICKER','DRUG','DIRECTION','P(WIN)','SCIENCE','STRIKE','EXPIRY',
               'MID','MULTIPLE','OI','CATALYST','DAYS','NOTES'])

all_rows = [
    ('RGNX','RGX-202 gene therapy','CALLS',68,'F','$10C','2026-07-17',1.50,4.2,1509,'Apr-May 2026',2,
     'AbbVie $100M milestone 1H 2026. Non-RCT standard for gene therapy.'),
    ('AXSM','AXS-05 sNDA','CALLS',65,'D','$200C','2026-05-15',7.70,15.7,2228,'Apr 30 CERTAIN',16,
     'BEST MULTIPLE 15.7x. Already-approved drug label extension. Pre-PDUFA drift.'),
    ('IDYA','Darovasertib+Crizotinib','CALLS',75,'C','$35C','2026-05-15',1.82,13.2,11011,'NDA H2 2026',31,
     'POSITIVE Apr 13: PFS significant, OS trend. Option -66% (IV crush). NDA filing = next catalyst.'),
    ('ARGX','Efgartigimod $800/$850C','SPREAD',87,'D','$800/$850C','2026-05-15',16.01,2.1,5,'May 10 CERTAIN',26,
     'BELOW THRESHOLD (OI=19/5). Hold existing position through May 10 PDUFA. Stock $809.27.'),
    ('RVMD','RMC-6236/Daraxonrasib','CALLS',90,'C','$125C','2026-06-18',18.00,6.7,47,'NDA H2 2026',64,
     'PHASE 3 POSITIVE: OS 13.2 vs 6.7 months. +41%. OPTION ITM at $11.30. ~140% gain. AACR Apr 17-22.'),
    ('AGIO','Tebapivat AG-946','PUTS',6,'F','$30P','2026-08-21',2.73,6.7,691,'May-Jun 2026',31,
     'P(fail)=94%. Grade F confirms put thesis.'),
    ('NTLA','NTLA-2002 CRISPR HAE','CALLS',82,'C','$15C','2026-07-17',2.40,4.4,1771,'May-Jun 2026',31,
     'NEJM 3yr data published. BLA forces June disclosure.'),
    ('VERA','Atacicept IgAN BLA','CALLS',72,'C','$55C','2027-01-15',8.05,2.9,19,'Jul 7 CERTAIN',84,
     'New play. Stock $43.71. OI=19, spread=61%. Use limit orders.'),
    ('MLTX','Sonelokimab IZAR-1','CALLS',100,'B','$21C','2026-08-21',4.10,3.0,1041,'Jun-Jul 2026',62,
     'Warpspeed 100%. Grade B. RCT triple-blind N=960.'),
    ('PRAX','Relutrigine NDA','CALLS',70,'C','$300C','2027-01-15',79.00,3.4,62,'Sep 27 CERTAIN',166,
     'Confirmed PDUFA. Rare epilepsy. Spread=6%.'),
]

for i, rd in enumerate(all_rows, 3):
    ticker,drug,direc,p,grade,strike,expiry,mid,mult,oi,cat,days,notes = rd
    rbg = RBG if 'PUT' in direc else (GBG if mult>=2.5 else OBG)
    gcol = {'A':GRN,'B':GRN,'C':YLW,'D':ORN,'F':RED}.get(grade, WHT)
    row_vals = [ticker,drug,direc,f'{p}%',f'Grade {grade}',strike,expiry,
                f'${mid:.2f}',f'{mult}x',oi,cat,f'{days}d',notes]
    for col,val in enumerate(row_vals,1):
        c = ws3.cell(row=i,column=col,value=val)
        fg = (YLW if col==1 else (GRN if p>=60 else RED) if col==4
              else gcol if col==5
              else (ORN if mult>=10 else BLU if mult>=2.5 else GRY) if col==9 else WHT)
        cs(c,bg=rbg,fg=fg,bold=(col==1),wrap=(col==13))
    ws3.row_dimensions[i].height = 36

for i,w in enumerate([8,28,10,8,10,12,12,9,9,7,14,7,55],1):
    ws3.column_dimensions[get_column_letter(i)].width = w

# TAB 4 — Monitor
ws4 = wb.create_sheet('Monitor')
ws4.sheet_properties.tabColor = 'D29922'
ws4.merge_cells('A1:F1')
t4 = ws4['A1']
t4.value = f'MONITOR -- {today_str}'
cs(t4, bg=DARK, fg=ORN, bold=True, sz=12, al='center')
ws4.row_dimensions[1].height = 24
hrow(ws4, 2, ['TICKER','DRUG','CATALYST','P%','NOTES','GRADUATE WHEN'])
mon_rows = [
    ('VRDN','Veligrotug TED BLA','Jun 30 2026 PDUFA','72%',
     'Bid $0 most strikes. 77 days out. Watch for liquidity in May.','When spread < 80%'),
    ('RZLT','Ersodetug upLIFT','H2 2026','72%',
     '16-patient trial. No bid on options.','If liquidity builds'),
    ('NAMS','Obicetrapib PREVAIL','Nov 2026','37%',
     'PUT candidate P=37%. N=9,541. Too far.','When within 90 days'),
    ('AMGN','Olpasiran OCEAN-a','2027','71%',
     'Mega cap. 2027 readout.','Remove if still 2027 in 60 days'),
]
for i,rd in enumerate(mon_rows,3):
    for col,val in enumerate(rd,1):
        c = ws4.cell(row=i,column=col,value=val)
        cs(c,bg=DARK,fg=(YLW if col==1 else WHT),wrap=(col in [5,6]))
    ws4.row_dimensions[i].height = 36
for i,w in enumerate([8,28,18,7,55,26],1):
    ws4.column_dimensions[get_column_letter(i)].width = w

path = BASE / 'reports/Alpha_Sniper_2026-04-14.xlsx'
wb.save(path)
print(f'Saved: {path}')

# EMAIL BODY
lines = []
lines.append('ALPHA SNIPER -- April 14, 2026')
lines.append('Run #15  |  10 active plays  |  9 qualifying (>=2.5x)  |  Best: AXSM 15.7x')
lines.append('=' * 65)
lines.append('')
lines.append('APRIL 13 RECAP -- THREE EVENTS RESOLVED')
lines.append('')
lines.append('RVMD  (+41% to $136.30)  PHASE 3 POSITIVE -- BIGGEST WIN')
lines.append('  RASolute 302: daraxonrasib OS 13.2 months vs 6.7 months chemotherapy (+97%)')
lines.append('  "Unprecedented overall survival benefit" -- NDA filing H2 2026')
lines.append('  $125C Jun18 entry ~$7-8 -> current $18 last trade = ~+140% gain')
lines.append('  Option NOW ITM by $11.30. Still 64 days to expiry.')
lines.append('  Next catalysts: AACR Apr 17-22 + NDA submission')
lines.append('')
lines.append('TVTX  (+6% to $30.70)  FDA APPROVED for FSGS -- first ever FSGS drug')
lines.append('  CORRECT DIRECTION but stock +6% not enough to clear $35C strike')
lines.append('  $35C Apr17 entry ~$3 -> last $3.58 = ~+19%. Expires worthless Friday.')
lines.append('  REMOVED from active plays per auto-remove protocol.')
lines.append('')
lines.append('IDYA  (+7.6% to $32.82)  POSITIVE PFS data -- OS trend only')
lines.append('  OptimUM-02: statistically significant PFS (first ever in 1L uveal melanoma)')
lines.append('  OS: "early trend" not confirmed = why stock reacted modestly')
lines.append('  $35C May15 entry $5.35 -> last $1.82 = -66% (IV crush + OTM)')
lines.append('  KEEPING: NDA H2 2026 filing is the next catalyst. 31 days left on May15 option.')
lines.append('  UPDATED P to 75% (PFS positive confirmed, NDA likely accepted)')
lines.append('')
lines.append('=' * 65)
lines.append('ONE-LINERS (sorted nearest catalyst, >=2.5x):')
lines.append('-' * 65)
one_liners = [
    ('2d',  'RGNX', '$10C  Jul-17 @ $1.50',  '4.2x', 'P=68%',    'OI=1,509', 'Apr-May 2026      | Gene therapy standard (Grade F)'),
    ('16d', 'AXSM', '$200C May-15 @ $7.70',  '15.7x','P=65%',    'OI=2,228', 'Apr 30 CERTAIN    | BEST MULTIPLE -- Grade D (PDUFA bet)'),
    ('31d', 'IDYA', '$35C  May-15 @ $1.82',  '13.2x','P=75%',    'OI=11,011','NDA H2 2026       | PFS positive. Option -66% (IV crush). NDA = next catalyst.'),
    ('64d', 'RVMD', '$125C Jun-18 @ $18.00', '6.7x', 'P=90%',    'OI=47',    'NDA H2 2026       | PHASE 3 POSITIVE. +41% stock. Option ITM +140%.'),
    ('31d', 'AGIO', '$30P  Aug-21 @ $2.73',  '6.7x', 'P(f)=94%', 'OI=691',   'May-Jun 2026      | Grade F confirms put thesis'),
    ('31d', 'NTLA', '$15C  Jul-17 @ $2.40',  '4.4x', 'P=82%',    'OI=1,771', 'May-Jun 2026      | NEJM data pub. BLA forces June'),
    ('84d', 'VERA', '$55C  Jan-27 @ $8.05',  '2.9x', 'P=72%',    'OI=19',    'Jul 7 CERTAIN     | Spread=61%, small size'),
    ('166d','PRAX', '$300C Jan-27 @ $79.00', '3.4x', 'P=70%',    'OI=62',    'Sep 27 CERTAIN    | spread=6%'),
    ('62d', 'MLTX', '$21C  Aug-21 @ $4.10',  '3.0x', 'P=100%',   'OI=1,041', 'Jun-Jul 2026      | Grade B, Warpspeed 100%'),
]
for days, ticker, strike_str, mult, prob, oi_str, note in one_liners:
    lines.append(f'{days} | {ticker}  {strike_str} -- {mult} | {prob} | {oi_str} | {note}')

lines.append('')
lines.append('BELOW THRESHOLD:')
lines.append('  ARGX $800/$850C May15 -- 2.1x (hold existing through May 10 PDUFA, OI=19/5)')
lines.append('')
lines.append('=' * 65)
lines.append('SCIENCE GRADES')
lines.append('-' * 65)
lines.append('Grade B -- MLTX: RCT triple-blind N=960 ACR50.')
lines.append('Grade C -- IDYA, RVMD, NTLA, VERA, PRAX: Adequate design.')
lines.append('Grade D -- AXSM (PDUFA bet). ARGX (prior data strong, hold).')
lines.append('Grade F -- RGNX (gene therapy standard). AGIO (confirms put).')
lines.append('')
lines.append('=' * 65)
lines.append('RVMD POSITION GUIDANCE')
lines.append('-' * 65)
lines.append('  Entry: ~$7-8 | Current last: $18.00 | Gain: ~+140%')
lines.append('  Stock: $136.30 | Strike: $125C | ITM by: $11.30 | Time value: $6.70')
lines.append('  Expiry: Jun 18 (64 days). AACR data Apr 17-22. NDA filing H2 2026.')
lines.append('  Options:')
lines.append('    (a) Hold: AACR could push stock further toward Jefferies $140 / Oppenheimer $150 PT')
lines.append('    (b) Partial exit: lock in 140% gain on portion, let rest run to NDA')
lines.append('    (c) Roll: sell $125C, buy $150C or $160C for lower cost basis if conviction high')
lines.append('  Risk: stock gave back some gains after initial rip -- watch for consolidation')
lines.append('')
lines.append('=' * 65)
lines.append('IDYA POSITION GUIDANCE')
lines.append('-' * 65)
lines.append('  Entry: $5.35 | Current last: $1.82 | Loss: -66%')
lines.append('  Stock: $32.82 | Strike: $35C | OTM by: $2.18 | Time value: $1.82 (all premium)')
lines.append('  Expiry: May 15 (31 days). NDA filing H2 2026 is next catalyst.')
lines.append('  Options:')
lines.append('    (a) Hold: NDA filing announcement (not the filing itself) could re-rate stock above $35')
lines.append('    (b) Cut losses: $35C May15 at $1.82 -- limited time value premium left')
lines.append('    (c) Roll: sell $35C May15, buy $35C or $40C Sep18/Jan27 for more time on NDA thesis')
lines.append('  Key: the data WAS positive. The option lost because stock +7.6% < IV implied move (~25-30%)')
lines.append('')
lines.append('=' * 65)
lines.append('FULL PLAY CARDS')
lines.append('-' * 65)

cards = [
    ('RGNX','CALLS','4.2x','P=68%','Grade F (gene therapy standard)',
     '$10C Jul-17 @ $1.50 | OI=1,509',
     'Apr-May 2026 (AbbVie $100M milestone)',
     'RGX-202 gene therapy for DMD. All 4 pivotal-dose patients beat NSAA +7.4pts. AbbVie $100M milestone expected 1H 2026. BLA mid-2026. Stock at $9.04.',
     'Non-RCT is FDA-accepted standard for gene therapy (Elevidys, Zolgensma approved same design). AbbVie milestone = independent binary validation. $1k -> ~$4,200.'),
    ('AXSM','CALLS','15.7x','P=65%','Grade D',
     '$200C May-15 @ $7.70 | OI=2,228 | IV=73%',
     'Apr 30 2026 (PDUFA CERTAIN)',
     'AXS-05 (Auvelity) already FDA-approved for MDD. Label extension to Alzheimer agitation. BTD + Priority Review. Stock at $178.33. OI surged from 573 to 2,228 (smart money accumulating).',
     'Not a trial result bet. Pre-PDUFA drift historically accelerates in final 10 days. OI near-tripled to 2,228 = institutional positioning. $1k -> ~$15,700.'),
    ('IDYA','CALLS','13.2x','P=75%','Grade C',
     '$35C May-15 @ $1.82 | OI=11,011 | IV collapsed post-event',
     'NDA filing H2 2026 (post positive topline)',
     'OptimUM-02 PFS statistically significant -- first ever randomized data in 1L uveal melanoma. OS early trend. NDA submission targeted H2 2026. Stock at $32.82.',
     'Option -66% due to IV crush and modest stock move (OS uncertainty). NDA filing announcement is next binary: stock could re-rate to $40+ on NDA acceptance. 31 days left. High risk/reward remaining. $1k -> ~$13,200.'),
    ('RVMD','CALLS','6.7x','P=90%','Grade C',
     '$125C Jun-18 @ $18.00 | OI=47 | OPTION NOW ITM',
     'AACR Apr 17-22 + NDA filing H2 2026',
     'RASolute 302 Phase 3: daraxonrasib OS 13.2 vs 6.7 months chemotherapy -- unprecedented in pancreatic cancer. NDA submission H2 2026 with FDA pilot program (1-2 month review). Analyst PTs $140-150.',
     'Option entered at ~$7-8, now $18 last trade = ~+140% gain. Still ITM with 64 days to Jun18. AACR Apr 17-22 could provide additional data driving further upside. Consider partial profit-taking. $1k -> ~$6,700 on current fill.'),
    ('AGIO','PUTS','6.7x','P(fail)=94%','Grade F',
     '$30P Aug-21 @ $2.73 | OI=691 | IV=55%',
     'May-Jun 2026',
     'Phase 2b tebapivat uses wrong endpoint (8-week TI) in hardest-to-treat MDS patients. Phase 2a worked only in easiest patients. A competing PKR activator was terminated for futility in MDS.',
     'Grade F confirms put thesis. P(fail)=94%. Non-RCT + surrogate + wrong population. Best risk/reward for a structured put. $1k -> ~$6,700.'),
    ('NTLA','CALLS','4.4x','P=82%','Grade C',
     '$15C Jul-17 @ $2.40 | OI=1,771 | IV=102%',
     'May-Jun 2026',
     'NTLA-2002 CRISPR HAE. NEJM April 2: 96% attack reduction, 31/32 attack-free at 3 years. BLA H2 2026. Stock at $14.26.',
     'Data published -- market has not fully re-rated. BLA forces topline disclosure before June. Window closing. $1k -> ~$4,400.'),
    ('VERA','CALLS','2.9x','P=72%','Grade C',
     '$55C Jan-27 @ $8.05 | OI=19 | spread=61%',
     'Jul 7 2026 (PDUFA CERTAIN)',
     'Atacicept IgAN. Phase 3 ORIGIN RCT N=376. BLA accepted. Jul 7 PDUFA. Stock at $43.71.',
     'OI=19 requires limit orders and small size. Jan27 = 6-month buffer. If stock 2x to $88, intrinsic $33 / $8.05 = 4.1x. $1k -> ~$2,900.'),
    ('PRAX','CALLS','3.4x','P=70%','Grade C',
     '$300C Jan-27 @ $79.00 | OI=62 | spread=6%',
     'Sep 27 2026 (PDUFA CERTAIN)',
     'Relutrigine first Nav channel SCN2A/8A blocker. 5,000 US patients. FDA accepted NDA. Stock at $317.',
     'Confirmed PDUFA. IV builds into Sep event. Spread=6% unusually tight. Rare disease premium. $1k -> ~$3,400.'),
    ('MLTX','CALLS','3.0x','P=100%','Grade B',
     '$21C Aug-21 @ $4.10 | OI=1,041 | IV=123%',
     'Jun-Jul 2026',
     'Sonelokimab IZAR-1 PsA. Warpspeed 100%. Triple-blind RCT N=960. ACR50 = FDA gold standard. Grade B best design.',
     'Warpspeed 100% extremely rare. Small cap = large re-rating on approval. $1k -> ~$3,000.'),
]
for ticker, direc, mult, prob, grade, opt_str, cat, why, edge in cards:
    lines.append(f"\n{'='*55}")
    lines.append(f'  {ticker} -- {direc} -- {mult} -- {prob} -- {grade}')
    lines.append(f"{'='*55}")
    lines.append(f'  OPTION:   {opt_str}')
    lines.append(f'  CATALYST: {cat}')
    lines.append(f'  WHY:      {why}')
    lines.append(f'  EDGE:     {edge}')

lines.append('')
lines.append('=' * 65)
lines.append('MONITOR')
lines.append('-' * 65)
lines.append('  VRDN  veligrotug -- Jun 30 PDUFA  | Spreads $0 bid, 77 days out')
lines.append('  NAMS  obicetrapib -- Nov 2026       | PUT candidate P=37%, too far')
lines.append('')
lines.append('AXSM: 16 days to Apr 30 PDUFA. Pre-PDUFA drift watch. OI jumped 573->2,228.')
lines.append('RVMD: AACR Apr 17-22 with 9 presentations. Consider partial profit-taking.')
lines.append('IDYA: 31 days. NDA filing announcement is the next re-rating catalyst.')

body = '\n'.join(lines)
with open(BASE/'reports/email_body_2026-04-14.txt','w') as f:
    f.write(body)
print(f'Email body: {len(body)} chars')
print('Done.')
