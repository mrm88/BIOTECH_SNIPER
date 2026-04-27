#!/usr/bin/env python3
"""Alpha Sniper Run #15b — April 14, 2026. Intraday catch-up after IDYA spam fix."""
import json, datetime, openpyxl
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter
from pathlib import Path

from biotech_sniper.paths import BASE_DIR as BASE
opts  = {r['ticker']: r for r in json.load(open(BASE/'state/options_chains_2026-04-14b.json'))}
now   = datetime.datetime.now().strftime('%B %d, %Y %I:%M %p PT')
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

# TAB 1 — Qualifying Plays
ws1 = wb.active
ws1.title = '2.5x+ Plays'
ws1.sheet_properties.tabColor = '3FB950'
ws1.merge_cells('A1:L1')
t = ws1['A1']
t.value = f'ALPHA SNIPER  --  {today_str}  --  Run #15b  --  8 qualifying (>=2.5x)'
cs(t, bg=DARK, fg=GRN, bold=True, sz=12, al='center')
ws1.row_dimensions[1].height = 28
hrow(ws1, 2, ['TICKER','DIRECTION','STRIKE','EXPIRY','MID FILL','MULTIPLE','$1k RETURN',
               'P(WIN)%','SCIENCE','OI','CATALYST DATE','NOTE'])
ws1.row_dimensions[2].height = 20

qualifying = [
    ('AXSM','CALLS','$200C','2026-05-15', 8.95,14.4,14400, 65,'D', 2228,'Apr 30 CERTAIN',
     'BEST MULTIPLE 14.4x. Approved drug label extension. OI=2,228 (tripled). 16 days.'),
    ('IDYA','CALLS','$35C','2026-05-15', 1.38,16.8,16800, 75,'C',11011,'NDA H2 2026',
     'PFS positive Apr 13. Option -74% entry but 16.8x math still valid. NDA = next catalyst.'),
    ('AGIO','PUTS','$30P','2026-08-21', 3.08, 6.0, 6000,  6,'F',  691,'May-Jun 2026',
     'P(fail)=94%. Grade F confirms put. Mid increased to $3.08.'),
    ('NTLA','CALLS','$15C','2026-07-17', 2.80, 4.1, 4100, 82,'C', 1771,'May-Jun 2026',
     'NEJM 3yr data published. BLA forces disclosure by June.'),
    ('MLTX','CALLS','$21C','2026-08-21', 3.95, 3.3, 3300,100,'B', 1041,'Jun-Jul 2026',
     'Warpspeed 100%. Grade B RCT triple-blind N=960.'),
    ('RVMD','CALLS','$125C','2026-06-18',24.70, 5.4, 5400, 90,'C',   47,'AACR + NDA H2',
     'Stock +$7 today to $143.80. Option +240% from entry. AACR ongoing. ITM by $18.80.'),
    ('PRAX','CALLS','$300C','2027-01-15',107.35, 2.9, 2900, 70,'C',  62,'Sep 27 CERTAIN',
     'Stock +$24 to $341.75 today. Option ITM by $41.75. Sep 27 PDUFA confirmed.'),
    ('VERA','CALLS','$55C','2027-01-15', 8.05, 2.8, 2800, 72,'C',   19,'Jul 7 CERTAIN',
     'Atacicept IgAN BLA. Stock $43.26. Spread=61%, OI=19.'),
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

# TAB 2 — All Active
ws2 = wb.create_sheet('All Active')
ws2.sheet_properties.tabColor = '58A6FF'
ws2.merge_cells('A1:M1')
t2 = ws2['A1']
t2.value = f'ALL ACTIVE PLAYS -- {today_str} 11:30 AM -- 10 plays'
cs(t2, bg=DARK, fg=BLU, bold=True, sz=11, al='center')
ws2.row_dimensions[1].height = 24
hrow(ws2, 2, ['TICKER','DRUG','DIR','P','SCI','STRIKE','EXPIRY','MID','MULT','OI','CATALYST','DAYS','NOTES'])

all_rows = [
    ('AXSM','AXS-05 sNDA','CALLS',65,'D','$200C','2026-05-15',8.95,14.4,2228,'Apr 30 CERTAIN',16,
     'BEST 14.4x. Label extension. OI=2,228 (tripled from 573). Pre-PDUFA drift.'),
    ('IDYA','Darovasertib+Crizotinib','CALLS',75,'C','$35C','2026-05-15',1.38,16.8,11011,'NDA H2 2026',31,
     'PFS positive Apr 13. NDA = next catalyst. Option OTM, 31 days. Consider rolling.'),
    ('RGNX','RGX-202 gene therapy','CALLS',68,'F','$10C','2026-07-17',3.10,2.1,1509,'Apr-May 2026',2,
     'BELOW 2.5x today (IV crush). AbbVie $100M milestone pending. Gene therapy standard.'),
    ('ARGX','Efgartigimod spread','SPREAD',87,'D','$800/$850C','2026-05-15',24.75,1.0,5,'May 10 CERTAIN',26,
     'BELOW THRESHOLD. Spread ~1.0x (stock $826, spread nearly worthless fresh). Hold existing.'),
    ('RVMD','RMC-6236 daraxonrasib','CALLS',90,'C','$125C','2026-06-18',24.70,5.4,47,'AACR + NDA H2',64,
     'Phase 3 winner. Stock $143.80. ITM by $18.80. Entry ~$8 -> $24.70 = ~+200%.'),
    ('AGIO','Tebapivat AG-946','PUTS',6,'F','$30P','2026-08-21',3.08,6.0,691,'May-Jun 2026',31,
     'P(fail)=94%. Grade F confirms put. Entry $2.73 -> $3.08 = +13%.'),
    ('NTLA','NTLA-2002 CRISPR HAE','CALLS',82,'C','$15C','2026-07-17',2.80,4.1,1771,'May-Jun 2026',31,
     'NEJM 3yr data published. BLA forces June disclosure.'),
    ('MLTX','Sonelokimab IZAR-1','CALLS',100,'B','$21C','2026-08-21',3.95,3.3,1041,'Jun-Jul 2026',62,
     'Warpspeed 100%. Grade B. RCT N=960.'),
    ('PRAX','Relutrigine NDA','CALLS',70,'C','$300C','2027-01-15',107.35,2.9,62,'Sep 27 CERTAIN',166,
     'Stock $341.75 (+$24 today). Option ITM by $41.75. Sep 27 PDUFA confirmed.'),
    ('VERA','Atacicept IgAN BLA','CALLS',72,'C','$55C','2027-01-15',8.05,2.8,19,'Jul 7 CERTAIN',84,
     'Jul 7 PDUFA. OI=19, small size. Stock $43.26.'),
]

for i, rd in enumerate(all_rows, 3):
    ticker,drug,direc,p,grade,strike,expiry,mid,mult,oi,cat,days,notes = rd
    rbg = RBG if 'PUT' in direc else (GBG if mult>=2.5 else OBG)
    gcol = {'A':GRN,'B':GRN,'C':YLW,'D':ORN,'F':RED}.get(grade, WHT)
    row_vals = [ticker,drug,direc,f'{p}%',f'Grade {grade}',strike,expiry,
                f'${mid:.2f}',f'{mult}x',oi,cat,f'{days}d',notes]
    for col,val in enumerate(row_vals,1):
        c = ws3_temp = ws2.cell(row=i,column=col,value=val)
        fg = (YLW if col==1 else (GRN if p>=60 else RED) if col==4
              else gcol if col==5
              else (ORN if mult>=10 else BLU if mult>=2.5 else GRY) if col==9 else WHT)
        cs(c,bg=rbg,fg=fg,bold=(col==1),wrap=(col==13))
    ws2.row_dimensions[i].height = 36

for i,w in enumerate([8,24,8,7,8,12,12,9,9,7,14,7,55],1):
    ws2.column_dimensions[get_column_letter(i)].width = w

# TAB 3 — Bug Fix Log
ws3 = wb.create_sheet('System Fixes')
ws3.sheet_properties.tabColor = 'F85149'
ws3.merge_cells('A1:C1')
t3 = ws3['A1']
t3.value = 'SYSTEM FIXES -- April 14, 2026'
cs(t3, bg=DARK, fg=RED, bold=True, sz=12, al='center')
ws3.row_dimensions[1].height = 24
hrow(ws3, 2, ['FIX','ROOT CAUSE','RESOLUTION'])
fix_rows = [
    ('IDYA removal spam (5 emails sent)',
     'check_removals() fires on pdufa_date<today every hour. pdufa_date=2026-04-13 was not cleared after announcement. No dedup in removal alerts.',
     'Fix 1: Cleared IDYA pdufa_date=None (catalyst resolved, now NDA thesis). Fix 2: Added dedup via alerts_sent so each removal fires ONCE. Fix 3: Actual removal from active_plays.json now happens on trigger (was only alerting, not removing).'),
    ('Apr 13: 5 removal emails sent to milankoch@gmail.com',
     'Intraday scanner ran hourly: each run detected IDYA pdufa_date=2026-04-13 < today and re-fired the email.',
     'IDYA correctly stays ACTIVE (NDA thesis). Spam stopped. Future PDUFA-resolved plays will be auto-removed once and deduped.'),
]
for i, rd in enumerate(fix_rows, 3):
    for col, val in enumerate(rd, 1):
        c = ws3.cell(row=i, column=col, value=val)
        cs(c, bg=DARK, fg=(YLW if col==1 else WHT), wrap=True)
    ws3.row_dimensions[i].height = 72
for i,w in enumerate([30,55,65],1):
    ws3.column_dimensions[get_column_letter(i)].width = w

path = BASE / 'reports/Alpha_Sniper_2026-04-14b.xlsx'
wb.save(path)
print(f'Saved: {path}')

# EMAIL
lines = []
lines.append(f'ALPHA SNIPER -- April 14, 2026 (11:30 AM PT update)')
lines.append(f'Run #15b  |  10 active plays  |  8 qualifying (>=2.5x)  |  Best: IDYA 16.8x (math) / AXSM 14.4x (entry)')
lines.append('=' * 65)
lines.append('')
lines.append('BUG FIX: IDYA removal spam stopped')
lines.append('  Root cause: intraday scanner was re-firing IDYA removal every hour because')
lines.append('  pdufa_date=2026-04-13 < today, but no dedup was in place.')
lines.append('  Fix: cleared IDYA pdufa_date (catalyst resolved), added removal dedup,')
lines.append('  and actual auto-removal now writes to resolved_plays.json (not just alerts).')
lines.append('  Apologies for the spam -- 5 removal emails sent this morning.')
lines.append('')
lines.append('IDYA IS STILL ACTIVE (NDA thesis).')
lines.append('  PFS was positive. NDA filing H2 2026 is the next catalyst.')
lines.append('  $35C May15 at $1.38 -- option down 74% but NDA announcement = upside event.')
lines.append('')
lines.append('MARKET UPDATE (11:30 AM PT):')
lines.append(f'  RVMD: $143.80 (+$7 from 6AM) -- AACR presentations ongoing, option at $24.70')
lines.append(f'  PRAX: $341.75 (+$24 from 6AM) -- option now ITM by $41.75')
lines.append(f'  ARGX: $826.57 -- spread debit now $24.75, max payout $50 = 1.0x (worthless fresh)')
lines.append(f'  AXSM: $182.80 -- 16 days to Apr 30 PDUFA, OI=2,228')
lines.append('')
lines.append('ONE-LINERS (sorted nearest catalyst, >=2.5x):')
lines.append('-' * 65)
one_liners = [
    ('16d', 'AXSM', '$200C May-15 @ $8.95',  '14.4x', 'P=65%',    'OI=2,228', 'Apr 30 CERTAIN    | BEST ENTRY | OI tripled'),
    ('31d', 'IDYA', '$35C  May-15 @ $1.38',  '16.8x', 'P=75%',    'OI=11,011','NDA H2 2026       | PFS positive. NDA = catalyst.'),
    ('31d', 'AGIO', '$30P  Aug-21 @ $3.08',  '6.0x',  'P(f)=94%', 'OI=691',   'May-Jun 2026      | Grade F confirms put'),
    ('31d', 'NTLA', '$15C  Jul-17 @ $2.80',  '4.1x',  'P=82%',    'OI=1,771', 'May-Jun 2026      | NEJM data pub'),
    ('62d', 'MLTX', '$21C  Aug-21 @ $3.95',  '3.3x',  'P=100%',   'OI=1,041', 'Jun-Jul 2026      | Grade B'),
    ('64d', 'RVMD', '$125C Jun-18 @ $24.70', '5.4x',  'P=90%',    'OI=47',    'AACR + NDA H2     | +200% gain. ITM.'),
    ('84d', 'VERA', '$55C  Jan-27 @ $8.05',  '2.8x',  'P=72%',    'OI=19',    'Jul 7 CERTAIN     | OI=19 small size'),
    ('166d','PRAX', '$300C Jan-27 @ $107.35','2.9x',  'P=70%',    'OI=62',    'Sep 27 CERTAIN    | stock +$24 today, ITM'),
]
for days, ticker, strike_str, mult, prob, oi_str, note in one_liners:
    lines.append(f'{days} | {ticker}  {strike_str} -- {mult} | {prob} | {oi_str} | {note}')

lines.append('')
lines.append('BELOW THRESHOLD:')
lines.append('  RGNX $10C Jul17 @ $3.10 -- 2.1x (IV crush, AbbVie milestone watch)')
lines.append('  ARGX $800/$850C May15 -- 1.0x (spread near worthless fresh, hold existing)')
lines.append('')
lines.append('POSITION UPDATES:')
lines.append('')
lines.append('RVMD: Entry ~$7-8 -> current $24.70 = ~+200% gain.')
lines.append('  Stock $143.80 (up $7 since 6AM). AACR Apr 17-22 (9 presentations).')
lines.append('  $125C ITM by $18.80 with 64 days left.')
lines.append('  Consider: partial exit to lock gains, or hold through NDA filing.')
lines.append('')
lines.append('PRAX: Entry $79 -> current $107.35 = +36% on option already.')
lines.append('  Stock $341.75 (up $24 today on macro biotech strength).')
lines.append('  $300C now ITM by $41.75. Still 166 days to Jan27 expiry.')
lines.append('')
lines.append('AGIO: Entry $2.73 -> current $3.08 = +13% on puts.')
lines.append('  Stock $33.01. $30P Aug21. Readout still May-Jun 2026.')
lines.append('')
lines.append('ARGX: Stock $826.57 vs $850 upper leg. Spread needs stock >$850 for full payout.')
lines.append('  May 10 PDUFA = 26 days. Spread debit now ~$25 fresh (1.0x) -- untradeable.')
lines.append('  Existing position: theoretical value ~$26.57 (stock barely past lower leg).')

body = '\n'.join(lines)
with open(BASE/'reports/email_body_2026-04-14b.txt','w') as f:
    f.write(body)
print(f'Email: {len(body)} chars')
print('Done.')
