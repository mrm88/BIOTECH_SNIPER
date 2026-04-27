#!/usr/bin/env python3
"""Alpha Sniper Run #24 — Friday April 24, 2026. 6 days to AXSM PDUFA."""
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

# TAB 1
ws1 = wb.active
ws1.title = '2.5x+ Plays'
ws1.sheet_properties.tabColor = '3FB950'
ws1.merge_cells('A1:L1')
t = ws1['A1']
t.value = f'ALPHA SNIPER  --  {today_str}  --  Run #24  --  9 qualifying  --  6d to AXSM PDUFA'
cs(t, bg=DARK, fg=GRN, bold=True, sz=12, al='center')
ws1.row_dimensions[1].height = 28
hrow(ws1, 2, ['TICKER','DIR','STRIKE','EXPIRY','MID FILL','MULTIPLE','$1k RETURN',
               'P(WIN)%','SCIENCE','OI','CATALYST DATE','NOTE'])

qualifying = [
    ('AXSM','CALLS','$200C','2026-05-15', 8.95,14.4,14400, 65,'D',2295,'Apr 30 CERTAIN',
     '6 DAYS TO PDUFA. Stock $182.72. OI=2,295. Approved drug label ext. Grade D.'),
    ('IDYA','CALLS','$35C','2026-05-15', 0.50,40.5,40500, 75,'C', 7773,'NDA H2 2026',
     'LOTTERY TICKET. Stock $30.70. $4.30 OTM. 21 days. NDA filing = only catalyst.'),
    ('AGIO','PUTS', '$30P','2026-08-21', 2.35, 8.9, 8900,  4,'F',  692,'Tebapivat MDS H1 2026',
     'PUT ITM $4.26. Stock $25.74. Tebapivat MDS readout IMMINENT. Mid=$2.35 stale.'),
    ('NTLA','CALLS','$15C','2026-07-17', 3.40, 4.0, 4000, 82,'C', 1764,'May-Jun 2026',
     'Stock $15.87. ATM. BLA forces June disclosure.'),
    ('VRDN','CALLS','$17C','2026-07-17', 1.52, 5.7, 5700, 72,'C', 2346,'Jun 30 CERTAIN',
     'OI=2,346. 5.7x. Jun 30 PDUFA. Stock flat at $14.23.'),
    ('MLTX','CALLS','$21C','2026-08-21', 2.80, 3.4, 3400,100,'B', 1058,'Jun-Jul 2026',
     'Warpspeed 100%. Grade B RCT N=960. Stock $16.93.'),
    ('RVMD','CALLS','$125C','2026-06-18',23.40, 5.0, 5000, 90,'C',   56,'NDA H2 2026',
     'Phase 3 winner. Stock $134.48 (-5% sector). NDA filing timing play. +193%.'),
    ('RGNX','CALLS','$10C', '2026-07-17', 1.75, 3.5, 3500, 68,'F', 1513,'BLA mid-2026',
     'Back above threshold at 3.5x. AbbVie milestone. BLA mid-2026.'),
    ('PRAX','CALLS','$300C','2027-01-15',103.53, 2.8, 2800, 70,'C',  48,'Sep 27 CERTAIN',
     'Stock $329.60. $300C ITM by $29.60. Sep 27 PDUFA confirmed.'),
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

# TAB 2 — All Active
ws2 = wb.create_sheet('All Active')
ws2.sheet_properties.tabColor = '58A6FF'
ws2.merge_cells('A1:N1')
t2 = ws2['A1']
t2.value = f'ALL ACTIVE PLAYS -- {today_str} -- 10 active plays'
cs(t2, bg=DARK, fg=BLU, bold=True, sz=11, al='center')
ws2.row_dimensions[1].height = 24
hrow(ws2, 2, ['TICKER','DRUG','DIR','P','SCI','STRIKE','EXPIRY','MID','MULT','OI','CATALYST','DAYS','P&L','NOTES'])

all_rows = [
    ('AXSM','AXS-05 sNDA','CALLS',65,'D','$200C','2026-05-15',8.95,14.4,2295,'Apr 30 CERTAIN',6,
     '+16%','6 DAYS TO PDUFA. Approved drug label ext. AD agitation.'),
    ('IDYA','Darovasertib+Criz','CALLS',75,'C','$35C','2026-05-15',0.50,40.5,7773,'NDA H2 2026',21,
     '-91%','LOTTERY TICKET. Stock $30.70. $4.30 OTM. 21 days.'),
    ('RGNX','RGX-202 gene therapy','CALLS',68,'F','$10C','2026-07-17',1.75,3.5,1513,'BLA mid-2026',67,
     'flat','AbbVie milestone. BLA mid-2026. Back above 2.5x.'),
    ('ARGX','Efgar $800/$850C','SPREAD',87,'D','$800/$850C','2026-05-15',15.46,2.2,5,'May 10 2026',16,
     '+29%','STOCK BELOW $800 LOWER LEG. Spread +29%. 16d to PDUFA. P=87%. Hold.'),
    ('RVMD','Daraxonrasib','CALLS',90,'C','$125C','2026-06-18',23.40,5.0,56,'NDA H2 2026',54,
     '+193%','Phase 3 winner. Stock -5% (sector). NDA filing timing play.'),
    ('AGIO','Tebapivat MDS','PUTS',4,'F','$30P','2026-08-21',2.35,8.9,692,'Tebapivat MDS H1 2026',21,
     '-14%','PUT ITM $4.26. Stock $25.74. MDS readout IMMINENT.'),
    ('NTLA','NTLA-2002 CRISPR','CALLS',82,'C','$15C','2026-07-17',3.40,4.0,1764,'May-Jun 2026',21,
     'flat','Stock $15.87 ATM. BLA forces June.'),
    ('VRDN','Veligrotug TED BLA','CALLS',72,'C','$17C','2026-07-17',1.52,5.7,2346,'Jun 30 CERTAIN',67,
     'flat','OI=2,346. 5.7x. Jun 30 PDUFA.'),
    ('MLTX','Sonelokimab IZAR-1','CALLS',100,'B','$21C','2026-08-21',2.80,3.4,1058,'Jun-Jul 2026',52,
     'flat','Warpspeed 100%. Grade B.'),
    ('PRAX','Relutrigine NDA','CALLS',70,'C','$300C','2027-01-15',103.53,2.8,48,'Sep 27 CERTAIN',156,
     '+31%','Stock $329.60. ITM by $29.60.'),
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
    ('VERA','Atacicept IgAN','Jul 7 2026 PDUFA','72%','Stock $38.39. $55C at 1.5x (below 2.5x). Macro selloff.','When stock >$44'),
    ('RZLT','Ersodetug upLIFT','H2 2026','72%','No liquid options.','If liquidity builds'),
    ('NAMS','Obicetrapib PREVAIL','Nov 2026','37%','PUT candidate. ~198 days.','When within 90 days'),
    ('AMGN','Olpasiran OCEAN-a','2027','71%','Mega cap. REMOVE in 10 days.','10 days'),
]
for i,rd in enumerate(mon_rows,3):
    for col,val in enumerate(rd,1):
        c = ws3.cell(row=i,column=col,value=val)
        cs(c,bg=DARK,fg=(YLW if col==1 else WHT),wrap=(col in [5,6]))
    ws3.row_dimensions[i].height = 36
for i,w in enumerate([8,22,18,7,55,26],1):
    ws3.column_dimensions[get_column_letter(i)].width = w

path = BASE / 'reports/Alpha_Sniper_2026-04-24.xlsx'
wb.save(path)
print(f'Saved: {path}')

# EMAIL
lines = []
lines.append('ALPHA SNIPER -- April 24, 2026 (Friday)')
lines.append('Run #24  |  10 active plays  |  9 qualifying  |  Best: IDYA 40.5x (lottery)')
lines.append('=' * 65)
lines.append('')
lines.append('MACRO CONTEXT: Third week of biotech sector selloff (tariff uncertainty)')
lines.append('  No 8-Ks or company-specific news Apr 22-24 from any active play')
lines.append('  All thesis unchanged. Catalysts intact.')
lines.append('')
lines.append('6 TRADING DAYS TO AXSM PDUFA (April 30)')
lines.append('  $200C May-15 at $8.95 -- option +16% from entry (~$7.70)')
lines.append('  OI=2,295. Stock $182.72. FDA decision on AXS-05 for AD agitation.')
lines.append('  This is a label extension for an already-approved drug.')
lines.append('')
lines.append('ARGX: STOCK NOW BELOW $800 LOWER LEG')
lines.append('  Stock $787.95. Spread: $800C last $28.46, $850C last $13.00, net $15.46.')
lines.append('  Entry ~$12. Current value: +29%.')
lines.append('  16 days to May 10 PDUFA. P=87% approval.')
lines.append('  DECISION: HOLD. 87% approval = stock likely recovers above $800 at PDUFA.')
lines.append('  If approved: stock gaps to $850+ -> max $50 payout (+317% on $12 entry).')
lines.append('  Exit risk: 13% miss = spread goes to $0.')
lines.append('')
lines.append('IDYA: NOW A $0.50 LOTTERY TICKET')
lines.append('  Stock $30.70. $35C May15 at $0.50 mid. OI=7,773 (dropped from 11,762).')
lines.append('  Entry $5.35. Current $0.50 = -91%.')
lines.append('  At $0.50, this is a cheap convexity bet on NDA filing announcement.')
lines.append('  If NDA filed + stock to $40: intrinsic $5 / $0.50 = 10x.')
lines.append('  If no NDA by May 15 expiry: expires worthless.')
lines.append('  Assessment: Hold or cut. Small remaining premium, 21 days.')
lines.append('')
lines.append('AGIO: PUT NOW ITM BY $4.26')
lines.append('  Stock $25.74. $30P Aug21 at $2.35 (stale pre-market).')
lines.append('  Intrinsic $4.26 vs entry $2.73. Should reprice at open above entry.')
lines.append('  Tebapivat MDS readout IMMINENT -- could come any day.')
lines.append('')
lines.append('ONE-LINERS (sorted nearest catalyst, >=2.5x):')
lines.append('-' * 65)
one_liners = [
    ('6d',  'AXSM', '$200C May-15 @ $8.95',   '14.4x', 'P=65%',   'OI=2,295',  'Apr 30 CERTAIN    | +16% | Grade D'),
    ('21d', 'IDYA', '$35C  May-15 @ $0.50',   '40.5x', 'P=75%',   'OI=7,773',  'NDA H2 2026       | LOTTERY $0.50 | -91% entry'),
    ('21d', 'AGIO', '$30P  Aug-21 @ $2.35',   '8.9x',  'P(f)=96%','OI=692',    'MDS IMMINENT      | PUT ITM $4.26'),
    ('21d', 'NTLA', '$15C  Jul-17 @ $3.40',   '4.0x',  'P=82%',   'OI=1,764',  'May-Jun 2026      | ATM $15.87'),
    ('52d', 'MLTX', '$21C  Aug-21 @ $2.80',   '3.4x',  'P=100%',  'OI=1,058',  'Jun-Jul 2026      | Grade B'),
    ('54d', 'RVMD', '$125C Jun-18 @ $23.40',  '5.0x',  'P=90%',   'OI=56',     'NDA H2 2026       | +193% | -$14 sector'),
    ('67d', 'RGNX', '$10C  Jul-17 @ $1.75',   '3.5x',  'P=68%',   'OI=1,513',  'BLA mid-2026      | back above 2.5x'),
    ('67d', 'VRDN', '$17C  Jul-17 @ $1.52',   '5.7x',  'P=72%',   'OI=2,346',  'Jun 30 CERTAIN    | OI=2,346'),
    ('156d','PRAX', '$300C Jan-27 @ $103.53', '2.8x',  'P=70%',   'OI=48',     'Sep 27 CERTAIN    | ITM $29.60'),
]
for days, ticker, strike_str, mult, prob, oi_str, note in one_liners:
    lines.append(f'{days} | {ticker}  {strike_str} -- {mult} | {prob} | {oi_str} | {note}')

lines.append('')
lines.append('BELOW THRESHOLD:')
lines.append('  ARGX $800/$850C May15 -- 2.2x residual (stock below lower leg).')
lines.append('  Existing: +29% at $15.46. 16 days to May 10 PDUFA. P=87%. HOLD.')
lines.append('  VERA $55C Jan27 -- 1.5x. Still in Monitor (stock $38.39).')
lines.append('')
lines.append('POSITION SUMMARY:')
lines.append('  RVMD:  entry ~$8    -> $23.40 = ~+193%  | NDA filing timing play')
lines.append('  ARGX:  entry ~$12   -> $15.46 = +29%    | 16d to PDUFA, stock below lower leg')
lines.append('  AXSM:  entry ~$7.70 -> $8.95   = +16%   | 6 days to PDUFA')
lines.append('  PRAX:  entry $79    -> $103.53 = +31%   | ITM by $29.60')
lines.append('  AGIO:  entry $2.73  -> ITM $4.26        | MDS readout imminent')
lines.append('  IDYA:  entry $5.35  -> $0.50   = -91%   | lottery ticket on NDA filing')
lines.append('')
lines.append('SCIENCE GRADES: Unchanged.')
lines.append('Grade B -- MLTX. Grade C -- IDYA, RVMD, NTLA, VRDN, PRAX.')
lines.append('Grade D -- AXSM (PDUFA bet, Grade = trial design not approval odds).')
lines.append('Grade D -- ARGX (prior efgartigimod data strong, P=87% approval).')
lines.append('Grade F -- RGNX (gene therapy standard). AGIO (PUT thesis confirmed ITM).')
lines.append('')
lines.append('WEEK AHEAD: AXSM PDUFA April 30 (6 trading days).')
lines.append('  AGIO MDS readout could drop any day -- watch intraday alerts.')

body = '\n'.join(lines)
with open(BASE/'reports/email_body_2026-04-24.txt','w') as f:
    f.write(body)
print(f'Email: {len(body)} chars')
print('Done.')
