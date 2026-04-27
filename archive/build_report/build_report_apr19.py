#!/usr/bin/env python3
"""Alpha Sniper Run #20 — Sunday April 19, 2026. AACR Day 3."""
import json, datetime, openpyxl
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter
from pathlib import Path

from biotech_sniper.paths import BASE_DIR as BASE
today_str = datetime.date.today().strftime('%B %d, %Y')

DARK='0D1117'; GBG='0D2818'; RBG='2D0A0A'; OBG='2D1A00'; HDR='161B22'
WHT='FFFFFF'; GRN='3FB950'; RED='F85149'; ORN='D29922'; YLW='E3B341'
GRY='8B949E'; BLU='58A6FF'

def cs(c, bg=DARK, fg=WHT, bold=False, sz=10, al='left', wrap=False):
    c.fill = PatternFill(fill_type='solid', fgColor=bg)
    c.font = Font(color=fg, bold=bold, size=sz, name='Consolas')
    c.alignment = Alignment(horizontal=al, vertical='center', wrap_text=wrap)

def hrow(ws, row, vals, bg=HDR):
    for col, v in enumerate(vals, 1):
        cell = ws.cell(row=row, column=col, value=v)
        cs(cell, bg=bg, fg=YLW, bold=True)

wb = openpyxl.Workbook()

# TAB 1 — Qualifying Plays
ws1 = wb.active
ws1.title = '2.5x+ Plays'
ws1.sheet_properties.tabColor = '3FB950'
ws1.merge_cells('A1:L1')
t = ws1['A1']
t.value = f'ALPHA SNIPER  --  {today_str}  --  Run #20  --  10 qualifying (>=2.5x)  --  AACR Day 3'
cs(t, bg=DARK, fg=GRN, bold=True, sz=12, al='center')
ws1.row_dimensions[1].height = 28
hrow(ws1, 2, ['TICKER','DIR','STRIKE','EXPIRY','MID FILL','MULTIPLE','$1k RETURN',
               'P(WIN)%','SCIENCE','OI','CATALYST DATE','NOTE'])
ws1.row_dimensions[2].height = 20

qualifying = [
    ('AXSM','CALLS','$200C','2026-05-15',11.50,12.2,12200, 65,'D',2267,'Apr 30 CERTAIN',
     '11 days to PDUFA. OI=2,267. Stock $188.99. Option +49% from entry.'),
    ('IDYA','CALLS','$35C','2026-05-15', 1.02,25.4,25400, 75,'C',11201,'NDA H2 2026',
     'NEAR THE MONEY: stock $33.91, only $1.09 OTM. NDA announcement = catalyst. 26 days.'),
    ('AGIO','PUTS', '$30P','2026-08-21', 2.50, 7.1, 7100,  6,'F',  691,'May-Jun 2026',
     'P(fail)=94%. Grade F confirms put. Stock $35.14 (sector). Thesis unchanged.'),
    ('NTLA','CALLS','$15C','2026-07-17', 2.58, 4.6, 4600, 82,'C', 1801,'May-Jun 2026',
     'Stock $14.95, essentially ATM. BLA forces June disclosure.'),
    ('MLTX','CALLS','$21C','2026-08-21', 3.35, 3.6, 3600,100,'B', 1055,'Jun-Jul 2026',
     'Warpspeed 100%. Grade B RCT N=960. Best designed trial.'),
    ('RVMD','CALLS','$125C','2026-06-18',27.85, 5.1, 5100, 90,'C',   56,'AACR+NDA H2',
     'Phase 3 OS 13.2 vs 6.7 months. Warpspeed still 61% (pre-data -- will update). +248%.'),
    ('RGNX','CALLS','$10C', '2026-07-17', 2.60, 2.7, 2700, 68,'F', 1509,'BLA mid-2026',
     'AbbVie $100M milestone + BLA mid-2026. Gene therapy DMD.'),
    ('VRDN','CALLS','$17C', '2026-07-17', 1.95, 4.9, 4900, 72,'C', 2049,'Jun 30 CERTAIN',
     'Jun 30 PDUFA confirmed. OI=2,049. Stock flat $14.80.'),
    ('VERA','CALLS','$55C', '2027-01-15', 8.05, 2.6, 2600, 72,'C',   19,'Jul 7 CERTAIN',
     'Atacicept IgAN BLA. Jul 7 PDUFA. OI=19, use limit orders.'),
    ('PRAX','CALLS','$300C','2027-01-15',105.00, 3.0, 3000, 70,'C',  48,'Sep 27 CERTAIN',
     'Stock $342.50. $300C ITM by $42.50. Sep 27 PDUFA confirmed.'),
]

row = 3
for ticker, direc, strike, expiry, mid, mult, k1, p, grade, oi, cat, note in qualifying:
    rbg = RBG if 'PUT' in direc else GBG
    gcol = {'A':GRN,'B':GRN,'C':YLW,'D':ORN,'F':RED}.get(grade, WHT)
    vals = [ticker, direc, strike, expiry, f'${mid:.2f}', f'{mult}x',
            f'${k1:,}', f'{p}%', f'Grade {grade}', oi, cat, note[:120]]
    for col, val in enumerate(vals, 1):
        cell = ws1.cell(row=row, column=col, value=val)
        fg = (YLW if col==1 else (RED if 'PUT' in direc else GRN) if col==2
              else (ORN if mult>=10 else BLU) if col==6
              else (GRN if p>=60 else RED) if col==8
              else gcol if col==9 else WHT)
        cs(cell, bg=rbg, fg=fg, bold=(col==1), wrap=(col==12))
    ws1.row_dimensions[row].height = 40
    row += 1

for i, w in enumerate([8,8,10,12,10,10,12,9,10,7,20,68], 1):
    ws1.column_dimensions[get_column_letter(i)].width = w

# TAB 2 — All Active with P&L
ws2 = wb.create_sheet('All Active')
ws2.sheet_properties.tabColor = '58A6FF'
ws2.merge_cells('A1:N1')
t2 = ws2['A1']
t2.value = f'ALL ACTIVE PLAYS -- {today_str} -- 11 plays | Friday Close (Weekend)'
cs(t2, bg=DARK, fg=BLU, bold=True, sz=11, al='center')
ws2.row_dimensions[1].height = 24
hrow(ws2, 2, ['TICKER','DRUG','DIR','P','SCI','STRIKE','EXPIRY','MID','MULT','OI',
               'CATALYST','DAYS','P&L','NOTES'])

all_rows = [
    ('AXSM','AXS-05 sNDA','CALLS',65,'D','$200C','2026-05-15',11.50,12.2,2267,'Apr 30 CERTAIN',11,
     '+49%','11 days to PDUFA. Approved drug label extension.'),
    ('IDYA','Darovasertib+Criz','CALLS',75,'C','$35C','2026-05-15',1.02,25.4,11201,'NDA H2 2026',26,
     '-81%','Stock $33.91 -- $1.09 OTM. NDA = next catalyst.'),
    ('RGNX','RGX-202 gene therapy','CALLS',68,'F','$10C','2026-07-17',2.60,2.7,1509,'BLA mid-2026',72,
     'flat','AbbVie milestone. BLA mid-2026.'),
    ('ARGX','Efgar $800/$850C','SPREAD',87,'D','$800/$850C','2026-05-15',31.15,0.6,5,'May 10 CERTAIN',21,
     '+160%','STOCK $849.04 -- $0.96 FROM MAX PAYOUT. 21d to PDUFA. HOLD.'),
    ('RVMD','Daraxonrasib','CALLS',90,'C','$125C','2026-06-18',27.85,5.1,56,'AACR+NDA H2',59,
     '+248%','Phase 3 winner. AACR Day 3. Warpspeed not yet updated (still shows pre-data 61%).'),
    ('AGIO','Tebapivat AG-946','PUTS',6,'F','$30P','2026-08-21',2.50,7.1,691,'May-Jun 2026',26,
     '-8%','Grade F put. Stock $35.14. Thesis intact. May-Jun readout.'),
    ('NTLA','NTLA-2002 CRISPR','CALLS',82,'C','$15C','2026-07-17',2.58,4.6,1801,'May-Jun 2026',26,
     'flat','Stock $14.95, ATM. BLA forces June.'),
    ('VRDN','Veligrotug TED BLA','CALLS',72,'C','$17C','2026-07-17',1.95,4.9,2049,'Jun 30 CERTAIN',72,
     'flat','OI=2,049. Jun 30 PDUFA.'),
    ('MLTX','Sonelokimab IZAR-1','CALLS',100,'B','$21C','2026-08-21',3.35,3.6,1055,'Jun-Jul 2026',57,
     'flat','Warpspeed 100%. Grade B.'),
    ('VERA','Atacicept IgAN','CALLS',72,'C','$55C','2027-01-15',8.05,2.6,19,'Jul 7 CERTAIN',79,
     'flat','Jul 7 PDUFA. OI=19.'),
    ('PRAX','Relutrigine NDA','CALLS',70,'C','$300C','2027-01-15',105.00,3.0,48,'Sep 27 CERTAIN',161,
     '+33%','Stock $342.50. ITM by $42.50. Sep 27 PDUFA.'),
]

for i, rd in enumerate(all_rows, 3):
    ticker,drug,direc,p,grade,strike,expiry,mid,mult,oi,cat,days,pnl,notes = rd
    rbg = RBG if 'PUT' in direc else (GBG if mult>=2.5 else OBG)
    gcol = {'A':GRN,'B':GRN,'C':YLW,'D':ORN,'F':RED}.get(grade, WHT)
    pnl_color = GRN if '+' in pnl else RED if '-' in pnl else GRY
    row_vals = [ticker,drug,direc,f'{p}%',f'Grade {grade}',strike,expiry,
                f'${mid:.2f}',f'{mult}x',oi,cat,f'{days}d',pnl,notes]
    for col,val in enumerate(row_vals,1):
        cell = ws2.cell(row=i,column=col,value=val)
        fg = (YLW if col==1 else (GRN if p>=60 else RED) if col==4
              else gcol if col==5
              else (ORN if mult>=10 else BLU if mult>=2.5 else GRY) if col==9
              else pnl_color if col==13 else WHT)
        cs(cell,bg=rbg,fg=fg,bold=(col==1),wrap=(col==14))
    ws2.row_dimensions[i].height = 36

for i,w in enumerate([8,20,8,7,8,12,12,9,9,7,14,6,8,55],1):
    ws2.column_dimensions[get_column_letter(i)].width = w

# TAB 3 — Monitor
ws3 = wb.create_sheet('Monitor')
ws3.sheet_properties.tabColor = 'D29922'
ws3.merge_cells('A1:F1')
t3 = ws3['A1']
t3.value = f'MONITOR -- {today_str}'
cs(t3, bg=DARK, fg=ORN, bold=True, sz=12, al='center')
ws3.row_dimensions[1].height = 24
hrow(ws3, 2, ['TICKER','DRUG','CATALYST','P%','NOTES','GRADUATE WHEN'])
mon_rows = [
    ('RZLT','Ersodetug upLIFT','H2 2026','72%','No liquid options.','If liquidity builds'),
    ('NAMS','Obicetrapib PREVAIL','Nov 2026','37%','PUT candidate. ~204 days.','When within 90 days'),
    ('AMGN','Olpasiran OCEAN-a','2027','71%','Mega cap. Remove in 30d if still 2027.','30 days'),
]
for i,rd in enumerate(mon_rows,3):
    for col,val in enumerate(rd,1):
        c = ws3.cell(row=i,column=col,value=val)
        cs(c,bg=DARK,fg=(YLW if col==1 else WHT),wrap=(col in [5,6]))
    ws3.row_dimensions[i].height = 36
for i,w in enumerate([8,28,18,7,55,26],1):
    ws3.column_dimensions[get_column_letter(i)].width = w

path = BASE / 'reports/Alpha_Sniper_2026-04-19.xlsx'
wb.save(path)
print(f'Saved: {path}')

# EMAIL
lines = []
lines.append('ALPHA SNIPER -- April 19, 2026 (Sunday) -- AACR Day 3')
lines.append('Run #20  |  11 active plays  |  10 qualifying  |  Prices unchanged (markets closed)')
lines.append('=' * 65)
lines.append('')
lines.append('NO CHANGES FROM FRIDAY: prices identical (weekend, markets closed)')
lines.append('No new 8-Ks, no Warpspeed probability updates, no AACR breaking data')
lines.append('')
lines.append('WARPSPEED NOTE ON RVMD:')
lines.append('  Warpspeed still shows RVMD at 61% (their pre-data model forecast).')
lines.append('  The actual Phase 3 result (OS 13.2 vs 6.7 months) FAR EXCEEDED their')
lines.append('  simulated 5.3 vs 3.4 months PFS. Their probability will update to 100%')
lines.append('  once they process the April 13 data release. The play is resolved positively.')
lines.append('  RVMD is now an NDA-filing timing play, not a binary trial outcome play.')
lines.append('')
lines.append('KEY WATCHPOINTS FOR MONDAY OPEN:')
lines.append('  1. ARGX: stock $849.04. If gaps above $850 -> spread maxes out at $50 (+317%)')
lines.append('  2. AXSM: 11 days to Apr 30 PDUFA. Pre-PDUFA drift final stretch.')
lines.append('  3. IDYA: stock $33.91, only $1.09 from $35C strike. NDA = catalyst.')
lines.append('  4. AGIO: Stock has been rising on sector. Readout May-Jun. Watch for signal.')
lines.append('')
lines.append('ONE-LINERS (sorted nearest catalyst, >=2.5x):')
lines.append('-' * 65)
one_liners = [
    ('11d', 'AXSM', '$200C May-15 @ $11.50',  '12.2x', 'P=65%',    'OI=2,267', 'Apr 30 CERTAIN    | +49% gain | Grade D'),
    ('26d', 'IDYA', '$35C  May-15 @ $1.02',   '25.4x', 'P=75%',    'OI=11,201','NDA H2 2026       | $1.09 OTM -- near the money'),
    ('26d', 'AGIO', '$30P  Aug-21 @ $2.50',   '7.1x',  'P(f)=94%', 'OI=691',   'May-Jun 2026      | Grade F | -8% entry | sector move'),
    ('26d', 'NTLA', '$15C  Jul-17 @ $2.58',   '4.6x',  'P=82%',    'OI=1,801', 'May-Jun 2026      | ATM at $14.95 / $15C'),
    ('57d', 'MLTX', '$21C  Aug-21 @ $3.35',   '3.6x',  'P=100%',   'OI=1,055', 'Jun-Jul 2026      | Grade B Warpspeed 100%'),
    ('59d', 'RVMD', '$125C Jun-18 @ $27.85',  '5.1x',  'P=90%',    'OI=56',    'AACR Day 3        | Phase 3 winner +248% | NDA filing'),
    ('72d', 'RGNX', '$10C  Jul-17 @ $2.60',   '2.7x',  'P=68%',    'OI=1,509', 'BLA mid-2026      | AbbVie milestone'),
    ('72d', 'VRDN', '$17C  Jul-17 @ $1.95',   '4.9x',  'P=72%',    'OI=2,049', 'Jun 30 CERTAIN    | OI=2,049'),
    ('79d', 'VERA', '$55C  Jan-27 @ $8.05',   '2.6x',  'P=72%',    'OI=19',    'Jul 7 CERTAIN     | small size'),
    ('161d','PRAX', '$300C Jan-27 @ $105.00', '3.0x',  'P=70%',    'OI=48',    'Sep 27 CERTAIN    | ITM by $42.50'),
]
for days, ticker, strike_str, mult, prob, oi_str, note in one_liners:
    lines.append(f'{days} | {ticker}  {strike_str} -- {mult} | {prob} | {oi_str} | {note}')

lines.append('')
lines.append('BELOW THRESHOLD:')
lines.append('  ARGX $800/$850C May15 -- 0.6x fresh. Existing: +160% at $31.15.')
lines.append('  Stock $849.04 ($0.96 from max $50 payout). HOLD through May 10 PDUFA.')
lines.append('')
lines.append('POSITION SUMMARY (unrealized, unchanged from Friday):')
lines.append('  RVMD:  entry ~$8    -> $27.85 = ~+248%')
lines.append('  ARGX:  entry ~$12   -> $31.15 = +160%  | $0.96 from max payout (+317%)')
lines.append('  AXSM:  entry ~$7.70 -> $11.50 = +49%   | 11 days to PDUFA')
lines.append('  PRAX:  entry $79    -> $105.00 = +33%')
lines.append('  IDYA:  entry $5.35  -> $1.02   = -81%  | $1.09 OTM, NDA catalyst')
lines.append('  AGIO:  entry $2.73  -> $2.50   = -8%   | readout May-Jun, thesis intact')
lines.append('')
lines.append('SCIENCE GRADES (unchanged):')
lines.append('Grade B -- MLTX. Grade C -- IDYA, RVMD, NTLA, VRDN, VERA, PRAX.')
lines.append('Grade D -- AXSM (PDUFA bet), ARGX (prior data). Grade F -- RGNX, AGIO (confirms put).')
lines.append('')
lines.append('WEEK AHEAD:')
lines.append('  Mon-Fri: AACR continues (RVMD 9 presentations through Apr 22)')
lines.append('  Apr 30: AXSM PDUFA (11 trading days away)')
lines.append('  May 10: ARGX PDUFA (21 days away, stock $0.96 from max payout)')

body = '\n'.join(lines)
with open(BASE/'reports/email_body_2026-04-19.txt','w') as f:
    f.write(body)
print(f'Email: {len(body)} chars')
print('Done.')
