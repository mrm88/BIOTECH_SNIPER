#!/usr/bin/env python3
"""Alpha Sniper Run #21 — Monday April 20, 2026. AACR Day 4. 10 days to AXSM PDUFA."""
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
t.value = f'ALPHA SNIPER  --  {today_str}  --  Run #21  --  10 qualifying  --  10d to AXSM PDUFA'
cs(t, bg=DARK, fg=GRN, bold=True, sz=12, al='center')
ws1.row_dimensions[1].height = 28
hrow(ws1, 2, ['TICKER','DIR','STRIKE','EXPIRY','MID FILL','MULTIPLE','$1k RETURN',
               'P(WIN)%','SCIENCE','OI','CATALYST DATE','NOTE'])
ws1.row_dimensions[2].height = 20

qualifying = [
    ('AXSM','CALLS','$200C','2026-05-15',12.00,11.7,11700, 65,'D',2280,'Apr 30 CERTAIN',
     '10 DAYS TO PDUFA. OI=2,280. Stock $188.99. Option +56% from entry. FINAL STRETCH.'),
    ('IDYA','CALLS','$35C','2026-05-15', 1.60,16.3,16300, 75,'C',11828,'NDA H2 2026',
     'Stock $33.91. $1.09 OTM. OI surged to 11,828. NDA = next catalyst. 25 days.'),
    ('AGIO','PUTS', '$30P','2026-08-21', 2.35, 7.5, 7500,  6,'F',  692,'May-Jun 2026',
     'P(fail)=94%. Grade F confirms put. Best multiple today 7.5x. Thesis unchanged.'),
    ('NTLA','CALLS','$15C','2026-07-17', 2.85, 4.2, 4200, 82,'C', 1799,'May-Jun 2026',
     'Stock $14.95, essentially ATM. BLA forces June disclosure.'),
    ('VRDN','CALLS','$17C','2026-07-17', 1.92, 5.0, 5000, 72,'C', 2170,'Jun 30 CERTAIN',
     'OI=2,170. Jun 30 PDUFA. Stock $14.80. Pre-mkt bid=$0, use limit at $1.92.'),
    ('MLTX','CALLS','$21C','2026-08-21', 3.40, 3.5, 3500,100,'B', 1068,'Jun-Jul 2026',
     'Warpspeed 100%. Grade B RCT N=960. Best designed trial in portfolio.'),
    ('RVMD','CALLS','$125C','2026-06-18',29.00, 4.9, 4900, 90,'C',   56,'AACR+NDA H2',
     'Phase 3 winner. AACR Day 4 today. Option +263% from entry.'),
    ('RGNX','CALLS','$10C', '2026-07-17', 2.54, 2.8, 2800, 68,'F', 1516,'BLA mid-2026',
     'AbbVie $100M milestone. BLA mid-2026. Gene therapy DMD.'),
    ('VERA','CALLS','$55C', '2027-01-15', 8.05, 2.6, 2600, 72,'C',   19,'Jul 7 CERTAIN',
     'Atacicept IgAN BLA. Jul 7 PDUFA. OI=19, use limit orders.'),
    ('PRAX','CALLS','$300C','2027-01-15',103.53, 3.1, 3100, 70,'C',  48,'Sep 27 CERTAIN',
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
t2.value = f'ALL ACTIVE PLAYS -- {today_str} -- 11 plays | Pre-Market (Friday Close)'
cs(t2, bg=DARK, fg=BLU, bold=True, sz=11, al='center')
ws2.row_dimensions[1].height = 24
hrow(ws2, 2, ['TICKER','DRUG','DIR','P','SCI','STRIKE','EXPIRY','MID','MULT','OI',
               'CATALYST','DAYS','P&L','NOTES'])

all_rows = [
    ('AXSM','AXS-05 sNDA','CALLS',65,'D','$200C','2026-05-15',12.00,11.7,2280,'Apr 30 CERTAIN',10,
     '+56%','10 DAYS TO PDUFA. OI=2,280. Approved drug label extension.'),
    ('IDYA','Darovasertib+Criz','CALLS',75,'C','$35C','2026-05-15',1.60,16.3,11828,'NDA H2 2026',25,
     '-70%','Stock $33.91. $1.09 OTM. OI=11,828. NDA = next catalyst.'),
    ('RGNX','RGX-202 gene therapy','CALLS',68,'F','$10C','2026-07-17',2.54,2.8,1516,'BLA mid-2026',71,
     'flat','AbbVie milestone. BLA mid-2026.'),
    ('ARGX','Efgar $800/$850C','SPREAD',87,'D','$800/$850C','2026-05-15',35.70,0.4,5,'May 10 2026',20,
     '+198%','Stock $849.04. $0.96 FROM MAX PAYOUT. Spread value $35.70. 20d to PDUFA. HOLD.'),
    ('RVMD','Daraxonrasib','CALLS',90,'C','$125C','2026-06-18',29.00,4.9,56,'AACR+NDA H2',58,
     '+263%','Phase 3 winner. AACR Day 4. NDA filing play.'),
    ('AGIO','Tebapivat AG-946','PUTS',6,'F','$30P','2026-08-21',2.35,7.5,692,'May-Jun 2026',25,
     '-14%','Grade F put. 7.5x today. Stock $35.14. Thesis intact. May-Jun readout.'),
    ('NTLA','NTLA-2002 CRISPR','CALLS',82,'C','$15C','2026-07-17',2.85,4.2,1799,'May-Jun 2026',25,
     'flat','Stock $14.95 ATM. BLA forces June.'),
    ('VRDN','Veligrotug TED BLA','CALLS',72,'C','$17C','2026-07-17',1.92,5.0,2170,'Jun 30 CERTAIN',71,
     'flat','OI=2,170. Jun 30 PDUFA.'),
    ('MLTX','Sonelokimab IZAR-1','CALLS',100,'B','$21C','2026-08-21',3.40,3.5,1068,'Jun-Jul 2026',56,
     'flat','Warpspeed 100%. Grade B.'),
    ('VERA','Atacicept IgAN','CALLS',72,'C','$55C','2027-01-15',8.05,2.6,19,'Jul 7 CERTAIN',78,
     'flat','Jul 7 PDUFA. OI=19.'),
    ('PRAX','Relutrigine NDA','CALLS',70,'C','$300C','2027-01-15',103.53,3.1,48,'Sep 27 CERTAIN',160,
     '+31%','Stock $342.50. ITM by $42.50. Sep 27 PDUFA.'),
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
    ('NAMS','Obicetrapib PREVAIL','Nov 2026','37%','PUT candidate. ~201 days.','When within 90 days'),
    ('AMGN','Olpasiran OCEAN-a','2027','71%','Mega cap. REMOVE in 25 days if still 2027.','25 days'),
]
for i,rd in enumerate(mon_rows,3):
    for col,val in enumerate(rd,1):
        c = ws3.cell(row=i,column=col,value=val)
        cs(c,bg=DARK,fg=(YLW if col==1 else WHT),wrap=(col in [5,6]))
    ws3.row_dimensions[i].height = 36
for i,w in enumerate([8,28,18,7,55,26],1):
    ws3.column_dimensions[get_column_letter(i)].width = w

path = BASE / 'reports/Alpha_Sniper_2026-04-20.xlsx'
wb.save(path)
print(f'Saved: {path}')

# EMAIL
lines = []
lines.append('ALPHA SNIPER -- April 20, 2026 (Monday) -- AACR Day 4')
lines.append('Run #21  |  11 active plays  |  10 qualifying (>=2.5x)  |  Best: AXSM 11.7x entry')
lines.append('=' * 65)
lines.append('')
lines.append('10 TRADING DAYS TO AXSM PDUFA (April 30)')
lines.append('  $200C May-15 at $12.00 -- option +56% from entry (~$7.70)')
lines.append('  OI=2,280. Stock $188.99. Pre-PDUFA drift in final 10-day stretch.')
lines.append('  FDA decision on AXS-05 for Alzheimer agitation (BTD + Priority Review)')
lines.append('')
lines.append('ARGX: SPREAD AT +198% -- STOCK $849.04 ($0.96 FROM MAX PAYOUT)')
lines.append('  Entry ~$12. Current spread value $35.70 = +198% gain.')
lines.append('  May 10 PDUFA = 20 days. P=87% approval.')
lines.append('  Max payout $50 on $12 entry = +317%. Only $0.96 stock move needed.')
lines.append('  HOLD. Watch Monday open carefully.')
lines.append('')
lines.append('DISCOVERY: No new qualified candidates.')
lines.append('  Nektar (NKTR) alopecia mid-stage data -> Phase 3 planning = too early.')
lines.append('  Lilly acquiring Kelonia (private) = no ticker to trade.')
lines.append('  AstraZeneca CT = mega cap, no edge.')
lines.append('')
lines.append('ONE-LINERS (sorted nearest catalyst, >=2.5x):')
lines.append('-' * 65)
one_liners = [
    ('10d', 'AXSM', '$200C May-15 @ $12.00',  '11.7x', 'P=65%',    'OI=2,280', 'Apr 30 CERTAIN    | FINAL STRETCH | +56%'),
    ('25d', 'IDYA', '$35C  May-15 @ $1.60',   '16.3x', 'P=75%',    'OI=11,828','NDA H2 2026       | $1.09 OTM | -70% entry'),
    ('25d', 'AGIO', '$30P  Aug-21 @ $2.35',   '7.5x',  'P(f)=94%', 'OI=692',   'May-Jun 2026      | BEST MULTIPLE | Grade F'),
    ('25d', 'NTLA', '$15C  Jul-17 @ $2.85',   '4.2x',  'P=82%',    'OI=1,799', 'May-Jun 2026      | ATM at $14.95'),
    ('56d', 'MLTX', '$21C  Aug-21 @ $3.40',   '3.5x',  'P=100%',   'OI=1,068', 'Jun-Jul 2026      | Grade B'),
    ('58d', 'RVMD', '$125C Jun-18 @ $29.00',  '4.9x',  'P=90%',    'OI=56',    'AACR Day 4        | Phase 3 winner +263%'),
    ('71d', 'RGNX', '$10C  Jul-17 @ $2.54',   '2.8x',  'P=68%',    'OI=1,516', 'BLA mid-2026      | AbbVie milestone'),
    ('71d', 'VRDN', '$17C  Jul-17 @ $1.92',   '5.0x',  'P=72%',    'OI=2,170', 'Jun 30 CERTAIN    | OI=2,170'),
    ('78d', 'VERA', '$55C  Jan-27 @ $8.05',   '2.6x',  'P=72%',    'OI=19',    'Jul 7 CERTAIN     | small size'),
    ('160d','PRAX', '$300C Jan-27 @ $103.53', '3.1x',  'P=70%',    'OI=48',    'Sep 27 CERTAIN    | ITM by $42.50'),
]
for days, ticker, strike_str, mult, prob, oi_str, note in one_liners:
    lines.append(f'{days} | {ticker}  {strike_str} -- {mult} | {prob} | {oi_str} | {note}')

lines.append('')
lines.append('BELOW THRESHOLD:')
lines.append('  ARGX $800/$850C May15 -- 0.4x fresh. Existing: +198% at $35.70.')
lines.append('  Stock $849.04, $0.96 from max payout. 20 days to May 10 PDUFA. HOLD.')
lines.append('')
lines.append('POSITION SUMMARY (pre-market, unchanged from Friday):')
lines.append('  RVMD:  entry ~$8    -> $29.00 = ~+263%')
lines.append('  ARGX:  entry ~$12   -> $35.70 = +198%  | $0.96 from max (+317%)')
lines.append('  AXSM:  entry ~$7.70 -> $12.00 = +56%   | 10 days to PDUFA')
lines.append('  PRAX:  entry $79    -> $103.53 = +31%')
lines.append('  IDYA:  entry $5.35  -> $1.60   = -70%  | $1.09 OTM, NDA catalyst')
lines.append('  AGIO:  entry $2.73  -> $2.35   = -14%  | thesis intact, readout May-Jun')
lines.append('')
lines.append('SCIENCE GRADES (unchanged):')
lines.append('Grade B -- MLTX. Grade C -- IDYA, RVMD, NTLA, VRDN, VERA, PRAX.')
lines.append('Grade D -- AXSM (PDUFA bet, Grade D flagged for trial design not approval odds).')
lines.append('Grade D -- ARGX (prior efgartigimod data very strong, spread +198%).')
lines.append('Grade F -- RGNX (gene therapy standard, non-RCT acceptable by FDA).')
lines.append('Grade F -- AGIO (confirms put thesis, 7.5x today).')

body = '\n'.join(lines)
with open(BASE/'reports/email_body_2026-04-20.txt','w') as f:
    f.write(body)
print(f'Email: {len(body)} chars')
print('Done.')
