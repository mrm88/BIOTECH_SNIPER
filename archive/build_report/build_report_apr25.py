#!/usr/bin/env python3
"""Alpha Sniper Run #25 — Saturday April 25, 2026. AGIO put +167%. 5 days to AXSM PDUFA."""
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

ws1 = wb.active
ws1.title = '2.5x+ Plays'
ws1.sheet_properties.tabColor = '3FB950'
ws1.merge_cells('A1:L1')
t = ws1['A1']
t.value = f'ALPHA SNIPER  --  {today_str}  --  Run #25  --  8 qualifying  --  AGIO +167% | 5d to AXSM'
cs(t, bg=DARK, fg=GRN, bold=True, sz=12, al='center')
ws1.row_dimensions[1].height = 28
hrow(ws1, 2, ['TICKER','DIR','STRIKE','EXPIRY','MID FILL','MULTIPLE','$1k RETURN',
               'P(WIN)%','SCIENCE','OI','CATALYST DATE','NOTE'])

qualifying = [
    ('AXSM','CALLS','$200C','2026-05-15', 9.70,13.9,13900, 65,'D',2295,'Apr 30 CERTAIN',
     '5 TRADING DAYS TO PDUFA. Stock $185.96. OI=2,295. +26% on entry.'),
    ('IDYA','CALLS','$35C','2026-05-15', 0.55,36.9,36900, 75,'C', 7773,'NDA H2 2026',
     'LOTTERY $0.55. Stock $30.73. $4.27 OTM. 20 days. NDA filing = only catalyst.'),
    ('AGIO','PUTS', '$30P','2026-08-21', 7.30, 2.9, 2900,  4,'F',  692,'Tebapivat MDS H1 2026',
     'WINNER: +167% GAIN. Entry $2.73 -> $7.30. Intrinsic $4.68. MDS readout still pending.'),
    ('NTLA','CALLS','$15C','2026-07-17', 2.33, 4.1, 4100, 82,'C', 1764,'HAELO data mid-2026',
     'NTLA -14% Friday (Q1 + nex-z safety). lonvo-z HAELO thesis UNCHANGED. 20 days.'),
    ('VRDN','CALLS','$17C','2026-07-17', 1.23, 6.3, 6300, 72,'C', 2346,'Jun 30 CERTAIN',
     'OI=2,346. Multiple improved to 6.3x. Jun 30 PDUFA. Stock $13.73.'),
    ('MLTX','CALLS','$21C','2026-08-21', 2.80, 3.5, 3500,100,'B', 1058,'Jun-Jul 2026',
     'Warpspeed 100%. Grade B RCT. Stock $17.18.'),
    ('RVMD','CALLS','$125C','2026-06-18',17.55, 6.8, 6800, 90,'C',   56,'NDA H2 2026',
     'Phase 3 winner. Stock $135.30. ITM by $10.30. Entry ~$8 -> $17.55 = ~+119%.'),
    ('PRAX','CALLS','$300C','2027-01-15',104.95, 3.1, 3100, 70,'C',  48,'Sep 27 CERTAIN',
     'Stock +4.6% to $344.82. $300C ITM by $44.82. Sep 27 PDUFA. +33% on entry.'),
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

# TAB 2
ws2 = wb.create_sheet('All Active')
ws2.sheet_properties.tabColor = '58A6FF'
ws2.merge_cells('A1:N1')
t2 = ws2['A1']
t2.value = f'ALL ACTIVE PLAYS -- {today_str} -- 10 active plays | Friday close'
cs(t2, bg=DARK, fg=BLU, bold=True, sz=11, al='center')
ws2.row_dimensions[1].height = 24
hrow(ws2, 2, ['TICKER','DRUG','DIR','P','SCI','STRIKE','EXPIRY','MID','MULT','OI','CATALYST','DAYS','P&L','NOTES'])

all_rows = [
    ('AXSM','AXS-05 sNDA','CALLS',65,'D','$200C','2026-05-15',9.70,13.9,2295,'Apr 30 CERTAIN',5,
     '+26%','5 DAYS TO PDUFA. Stock $185.96. OI=2,295.'),
    ('IDYA','Darovasertib+Criz','CALLS',75,'C','$35C','2026-05-15',0.55,36.9,7773,'NDA H2 2026',20,
     '-90%','LOTTERY $0.55. Stock $30.73. NDA = only catalyst.'),
    ('RGNX','RGX-202 gene therapy','CALLS',68,'F','$10C','2026-07-17',2.85,2.0,1513,'BLA mid-2026',66,
     'flat','BELOW 2.5x. AbbVie milestone. BLA mid-2026.'),
    ('ARGX','Efgar $800/$850C','SPREAD',87,'D','$800/$850C','2026-05-15',15.46,2.2,5,'May 10 2026',15,
     '+29%','Stock $780.25 (below lower leg). Spread +29%. 15d to PDUFA. HOLD.'),
    ('RVMD','Daraxonrasib','CALLS',90,'C','$125C','2026-06-18',17.55,6.8,56,'NDA H2 2026',53,
     '+119%','Phase 3 winner. Stock $135.30. ITM by $10.30. NDA filing.'),
    ('AGIO','Tebapivat MDS','PUTS',4,'F','$30P','2026-08-21',7.30,2.9,692,'Tebapivat MDS H1 2026',20,
     '+167%','WINNER. Entry $2.73 -> $7.30. ITM $4.68. MDS readout still pending.'),
    ('NTLA','lonvo-z CRISPR HAE','CALLS',82,'C','$15C','2026-07-17',2.33,4.1,1764,'HAELO mid-2026',20,
     'flat','Stock -14.1% (Q1+nex-z). lonvo-z HAELO thesis UNCHANGED.'),
    ('VRDN','Veligrotug TED BLA','CALLS',72,'C','$17C','2026-07-17',1.23,6.3,2346,'Jun 30 CERTAIN',66,
     'flat','OI=2,346. 6.3x. Jun 30 PDUFA.'),
    ('MLTX','Sonelokimab IZAR-1','CALLS',100,'B','$21C','2026-08-21',2.80,3.5,1058,'Jun-Jul 2026',51,
     'flat','Warpspeed 100%. Grade B.'),
    ('PRAX','Relutrigine NDA','CALLS',70,'C','$300C','2027-01-15',104.95,3.1,48,'Sep 27 CERTAIN',155,
     '+33%','Stock $344.82. ITM by $44.82. Sep 27 PDUFA.'),
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

for i,w in enumerate([8,20,8,7,8,12,12,9,9,7,20,6,8,55],1):
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
    ('VERA','Atacicept IgAN','Jul 7 2026 PDUFA','72%','Stock $38.39. $55C at 1.5x.','When stock >$44'),
    ('RZLT','Ersodetug upLIFT','H2 2026','72%','No liquid options.','If liquidity builds'),
    ('NAMS','Obicetrapib PREVAIL','Nov 2026','37%','PUT candidate. ~197 days.','When within 90 days'),
    ('AMGN','Olpasiran OCEAN-a','2027','71%','Mega cap. REMOVE in 10 days.','10 days'),
]
for i,rd in enumerate(mon_rows,3):
    for col,val in enumerate(rd,1):
        c = ws3.cell(row=i,column=col,value=val)
        cs(c,bg=DARK,fg=(YLW if col==1 else WHT),wrap=(col in [5,6]))
    ws3.row_dimensions[i].height = 36
for i,w in enumerate([8,22,18,7,55,26],1):
    ws3.column_dimensions[get_column_letter(i)].width = w

path = BASE / 'reports/Alpha_Sniper_2026-04-25.xlsx'
wb.save(path)
print(f'Saved: {path}')

# EMAIL
lines = []
lines.append('ALPHA SNIPER -- April 25, 2026 (Saturday) -- 5 trading days to AXSM PDUFA')
lines.append('Run #25  |  10 active plays  |  8 qualifying  |  Best: IDYA 36.9x (lottery)')
lines.append('=' * 65)
lines.append('')
lines.append('AGIO PUT: +167% GAIN -- BEST WINNER THIS WEEK')
lines.append('')
lines.append('  Stock dropped from $35.14 (Apr 20) to $25.32 today = -28% over the week')
lines.append('  $30P Aug21: entry $2.73 -> current $7.30 = +167% gain')
lines.append('  Intrinsic: $4.68 (stock $25.32 vs $30 strike). Time value: $2.62.')
lines.append('  Tebapivat MDS readout STILL PENDING. If fails (P=96%), stock -> $15-20.')
lines.append('  At $15: put intrinsic $15 on $2.73 entry = 5.5x from entry (more upside).')
lines.append('  HOLD: thesis intact, MDS catalyst still the main event.')
lines.append('')
lines.append('5 TRADING DAYS TO AXSM PDUFA (April 30)')
lines.append('  $200C May-15 at $9.70 -- option +26% from entry (~$7.70)')
lines.append('  OI=2,295. Stock $185.96. Approved drug (Auvelity) label extension.')
lines.append('')
lines.append('NTLA -14.1% FRIDAY -- THESIS UNCHANGED')
lines.append('  Cause: Q1 2026 earnings miss + nex-z MAGNITUDE safety update.')
lines.append('  Our play is on lonvo-z (NTLA-2002) in HAE -- HAELO Phase 3, completely separate program.')
lines.append('  lonvo-z thesis UNCHANGED: Phase 3 data mid-2026, BLA H2 2026.')
lines.append('  $15C Jul17: stock $13.63, OTM $1.37, mid=$2.33, 4.1x. HOLD.')
lines.append('')
lines.append('ARGX SPREAD: Stock $780.25 (below lower leg)')
lines.append('  $800C last $28.46, $850C last $13.00, net $15.46 = +29% on $12 entry.')
lines.append('  15 days to May 10 PDUFA. P=87% approval. HOLD.')
lines.append('')
lines.append('ONE-LINERS (sorted nearest catalyst, >=2.5x):')
lines.append('-' * 65)
one_liners = [
    ('5d',  'AXSM', '$200C May-15 @ $9.70',   '13.9x', 'P=65%',   'OI=2,295', 'Apr 30 CERTAIN    | +26%'),
    ('20d', 'IDYA', '$35C  May-15 @ $0.55',   '36.9x', 'P=75%',   'OI=7,773', 'NDA H2 2026       | LOTTERY -90%'),
    ('20d', 'AGIO', '$30P  Aug-21 @ $7.30',   '2.9x',  'P(f)=96%','OI=692',   'MDS PENDING       | +167% GAIN | HOLD'),
    ('20d', 'NTLA', '$15C  Jul-17 @ $2.33',   '4.1x',  'P=82%',   'OI=1,764', 'HAELO mid-2026    | -14% Q1/nex-z | lonvo-z thesis INTACT'),
    ('51d', 'MLTX', '$21C  Aug-21 @ $2.80',   '3.5x',  'P=100%',  'OI=1,058', 'Jun-Jul 2026      | Grade B'),
    ('53d', 'RVMD', '$125C Jun-18 @ $17.55',  '6.8x',  'P=90%',   'OI=56',    'NDA H2 2026       | +119%'),
    ('66d', 'VRDN', '$17C  Jul-17 @ $1.23',   '6.3x',  'P=72%',   'OI=2,346', 'Jun 30 CERTAIN    | OI=2,346'),
    ('155d','PRAX', '$300C Jan-27 @ $104.95', '3.1x',  'P=70%',   'OI=48',    'Sep 27 CERTAIN    | ITM $44.82 | +33%'),
]
for days, ticker, strike_str, mult, prob, oi_str, note in one_liners:
    lines.append(f'{days} | {ticker}  {strike_str} -- {mult} | {prob} | {oi_str} | {note}')

lines.append('')
lines.append('BELOW THRESHOLD / EXCEL ONLY:')
lines.append('  RGNX $10C Jul17 @ $2.85 -- 2.0x (below lower leg). AbbVie milestone watch.')
lines.append('  ARGX $800/$850C May15 -- 2.2x. Existing: +29% at $15.46. 15d to PDUFA. HOLD.')
lines.append('')
lines.append('POSITION SUMMARY (Friday close):')
lines.append('  AGIO:  entry $2.73   -> $7.30   = +167%  | WINNER. MDS readout pending.')
lines.append('  RVMD:  entry ~$8     -> $17.55  = ~+119% | NDA filing timing play')
lines.append('  ARGX:  entry ~$12    -> $15.46  = +29%   | 15d to PDUFA, stock below lower leg')
lines.append('  AXSM:  entry ~$7.70  -> $9.70   = +26%   | 5 days to PDUFA')
lines.append('  PRAX:  entry $79     -> $104.95 = +33%   | ITM by $44.82')
lines.append('  IDYA:  entry $5.35   -> $0.55   = -90%   | lottery on NDA')
lines.append('')
lines.append('SCIENCE GRADES: Unchanged.')
lines.append('Grade B -- MLTX. Grade C -- IDYA, RVMD, NTLA, VRDN, PRAX.')
lines.append('Grade D -- AXSM (PDUFA bet). ARGX (prior data strong, +29% on spread).')
lines.append('Grade F -- RGNX. AGIO (PUT thesis CONFIRMED +167%).')
lines.append('')
lines.append('WEEK AHEAD:')
lines.append('  Mon Apr 27: AXSM PDUFA final stretch (5 trading days to Apr 30)')
lines.append('  Thu Apr 30: AXSM PDUFA (FDA decision on AXS-05 AD agitation)')
lines.append('  Any day: AGIO tebapivat MDS readout (watch intraday alerts)')
lines.append('  May 10: ARGX PDUFA (15 days)')

body = '\n'.join(lines)
with open(BASE/'reports/email_body_2026-04-25.txt','w') as f:
    f.write(body)
print(f'Email: {len(body)} chars')
print('Done.')
