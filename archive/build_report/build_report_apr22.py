#!/usr/bin/env python3
"""Alpha Sniper Run #23 — Wednesday April 22, 2026. Sector selloff continues."""
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
t.value = f'ALPHA SNIPER  --  {today_str}  --  Run #23  --  9 qualifying (>=2.5x)  --  8d to AXSM'
cs(t, bg=DARK, fg=GRN, bold=True, sz=12, al='center')
ws1.row_dimensions[1].height = 28
hrow(ws1, 2, ['TICKER','DIR','STRIKE','EXPIRY','MID FILL','MULTIPLE','$1k RETURN',
               'P(WIN)%','SCIENCE','OI','CATALYST DATE','NOTE'])

qualifying = [
    ('AXSM','CALLS','$200C','2026-05-15', 8.30,15.6,15600, 65,'D',2279,'Apr 30 CERTAIN',
     '8 DAYS TO PDUFA. Stock -3% (sector). OI=2,279. +8% on position. Grade D (PDUFA bet).'),
    ('IDYA','CALLS','$35C','2026-05-15', 1.02,21.9,21900, 75,'C',11762,'NDA H2 2026',
     'Stock -5.3% to $31.83 (sector). Still OTM $3.17. OI=11,762. NDA = catalyst.'),
    ('AGIO','PUTS', '$30P','2026-08-21', 2.35, 8.9, 8900,  4,'F',  692,'Tebapivat MDS H1 2026',
     'PUT DEEPER ITM. Stock $26.29. ITM by $3.71. MDS readout imminent. BEST MULTIPLE.'),
    ('NTLA','CALLS','$15C','2026-07-17', 3.23, 3.9, 3900, 82,'C', 1839,'May-Jun 2026',
     'Stock $15.31, ATM. BLA forces June disclosure.'),
    ('VRDN','CALLS','$17C','2026-07-17', 1.49, 5.8, 5800, 72,'C', 2309,'Jun 30 CERTAIN',
     'OI=2,309. 5.8x. Jun 30 PDUFA. Stock -2.7% (sector).'),
    ('MLTX','CALLS','$21C','2026-08-21', 2.80, 3.6, 3600,100,'B', 1058,'Jun-Jul 2026',
     'Warpspeed 100%. Grade B RCT. Stock -3.7% (sector, no news).'),
    ('RVMD','CALLS','$125C','2026-06-18',29.00, 4.9, 4900, 90,'C',   56,'NDA H2 2026',
     'Phase 3 winner. AACR concluded. NDA filing timing play. +263% from entry.'),
    ('RGNX','CALLS','$10C', '2026-07-17', 2.25, 2.8, 2800, 68,'F', 1512,'BLA mid-2026',
     'AbbVie milestone. BLA mid-2026.'),
    ('PRAX','CALLS','$300C','2027-01-15',103.53,3.0, 3000, 70,'C',   48,'Sep 27 CERTAIN',
     'Stock $340.84. $300C ITM by $40.84. Sep 27 PDUFA.'),
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
t2.value = f'ALL ACTIVE PLAYS -- {today_str} -- 11 plays'
cs(t2, bg=DARK, fg=BLU, bold=True, sz=11, al='center')
ws2.row_dimensions[1].height = 24
hrow(ws2, 2, ['TICKER','DRUG','DIR','P','SCI','STRIKE','EXPIRY','MID','MULT','OI','CATALYST','DAYS','P&L','NOTES'])

all_rows = [
    ('AXSM','AXS-05 sNDA','CALLS',65,'D','$200C','2026-05-15',8.30,15.6,2279,'Apr 30 CERTAIN',8,
     '+8%','8 DAYS. Sector -3%. OI=2,279. AD agitation label ext.'),
    ('IDYA','Darovasertib+Criz','CALLS',75,'C','$35C','2026-05-15',1.02,21.9,11762,'NDA H2 2026',23,
     '-81%','Stock $31.83. OTM $3.17. OI=11,762. NDA = catalyst.'),
    ('RGNX','RGX-202 gene therapy','CALLS',68,'F','$10C','2026-07-17',2.25,2.8,1512,'BLA mid-2026',69,
     'flat','AbbVie milestone. BLA mid-2026.'),
    ('ARGX','Efgar $800/$850C','SPREAD',87,'D','$800/$850C','2026-05-15',20.43,1.5,18,'May 10 2026',18,
     '+70%','Stock $805.38. Spread +70% at $20.43. 18d to PDUFA. HOLD.'),
    ('RVMD','Daraxonrasib','CALLS',90,'C','$125C','2026-06-18',29.00,4.9,56,'NDA H2 2026',56,
     '+263%','Phase 3 winner. AACR concluded. NDA filing.'),
    ('AGIO','Tebapivat MDS','PUTS',4,'F','$30P','2026-08-21',2.35,8.9,692,'Tebapivat MDS H1 2026',23,
     '-14%','PUT ITM BY $3.71. Stock $26.29. MDS readout imminent. BEST MULTIPLE.'),
    ('NTLA','NTLA-2002 CRISPR','CALLS',82,'C','$15C','2026-07-17',3.23,3.9,1839,'May-Jun 2026',23,
     'flat','Stock $15.31 ATM. BLA forces June.'),
    ('VRDN','Veligrotug TED BLA','CALLS',72,'C','$17C','2026-07-17',1.49,5.8,2309,'Jun 30 CERTAIN',69,
     'flat','OI=2,309. 5.8x. Jun 30 PDUFA.'),
    ('MLTX','Sonelokimab IZAR-1','CALLS',100,'B','$21C','2026-08-21',2.80,3.6,1058,'Jun-Jul 2026',54,
     'flat','Warpspeed 100%. Grade B. Sector -3.7%.'),
    ('VERA','Atacicept IgAN','CALLS',72,'C','$55C','2027-01-15',8.05,2.1,19,'Jul 7 CERTAIN',76,
     'flat','BELOW 2.5x today (stock $40.06). Jul 7 PDUFA. OI=19.'),
    ('PRAX','Relutrigine NDA','CALLS',70,'C','$300C','2027-01-15',103.53,3.0,48,'Sep 27 CERTAIN',158,
     '+31%','Stock $340.84. ITM by $40.84.'),
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
    ('RZLT','Ersodetug upLIFT','H2 2026','72%','No liquid options.','If liquidity builds'),
    ('NAMS','Obicetrapib PREVAIL','Nov 2026','37%','PUT candidate. ~199 days.','When within 90 days'),
    ('AMGN','Olpasiran OCEAN-a','2027','71%','Mega cap. REMOVE in 15 days if still 2027.','15 days'),
    ('VERA','Atacicept IgAN','Jul 7 2026 PDUFA','72%','Dropped to 2.1x (stock -3.7%). Promote back when stock recovers above $44.','When multiple >2.5x'),
]
for i,rd in enumerate(mon_rows,3):
    for col,val in enumerate(rd,1):
        c = ws3.cell(row=i,column=col,value=val)
        cs(c,bg=DARK,fg=(YLW if col==1 else WHT),wrap=(col in [5,6]))
    ws3.row_dimensions[i].height = 36
for i,w in enumerate([8,22,18,7,55,26],1):
    ws3.column_dimensions[get_column_letter(i)].width = w

path = BASE / 'reports/Alpha_Sniper_2026-04-22.xlsx'
wb.save(path)
print(f'Saved: {path}')

# EMAIL
lines = []
lines.append('ALPHA SNIPER -- April 22, 2026 (Wednesday)')
lines.append('Run #23  |  11 active plays  |  9 qualifying (>=2.5x)  |  Best: IDYA 21.9x')
lines.append('=' * 65)
lines.append('')
lines.append('SECTOR CONTEXT: Second day of macro-driven biotech selloff (tariff uncertainty)')
lines.append('  No 8-Ks or company-specific news from any active play')
lines.append('  AXSM -3%, ARGX -3.5%, IDYA -5.3%, MLTX -3.7%, VERA -3.7%')
lines.append('  All thesis unchanged. Catalysts intact.')
lines.append('')
lines.append('8 TRADING DAYS TO AXSM PDUFA (April 30)')
lines.append('  $200C May-15 at $8.30 -- option +8% from entry (~$7.70)')
lines.append('  OI=2,279. Stock $182.97. Approved drug label extension (already approved for MDD).')
lines.append('  Stock dip on sector = better entry if not already in.')
lines.append('')
lines.append('ARGX SPREAD: Stock $805.38 -- pullback continues')
lines.append('  $800/$850C May15 spread: $800C at $37.73, $850C at $17.30, net $20.43')
lines.append('  Entry ~$12. Current value $20.43 = +70% gain (still profitable).')
lines.append('  18 days to May 10 PDUFA. P=87% approval.')
lines.append('  Stock needs to recover above $850 for max payout. HOLD.')
lines.append('')
lines.append('AGIO PUT DEEPENS: Stock $26.29 -- now ITM by $3.71')
lines.append('  $30P Aug21 at $2.35. P(fail)=96%. 8.9x multiple.')
lines.append('  Tebapivat MDS Phase 2b readout IMMINENT. Hold through the catalyst.')
lines.append('')
lines.append('VERA moved to Monitor: stock $40.06 (was $43.45) -- multiple dropped to 2.1x')
lines.append('  Will promote back when stock recovers above ~$44 (multiple >2.5x).')
lines.append('')
lines.append('ONE-LINERS (sorted nearest catalyst, >=2.5x):')
lines.append('-' * 65)
one_liners = [
    ('8d',  'AXSM', '$200C May-15 @ $8.30',   '15.6x', 'P=65%',   'OI=2,279',  'Apr 30 CERTAIN    | +8% | sector dip = entry opp'),
    ('23d', 'IDYA', '$35C  May-15 @ $1.02',   '21.9x', 'P=75%',   'OI=11,762', 'NDA H2 2026       | OTM $3.17 | -81% entry'),
    ('23d', 'AGIO', '$30P  Aug-21 @ $2.35',   '8.9x',  'P(f)=96%','OI=692',    'MDS IMMINENT      | PUT ITM $3.71 | BEST MULTIPLE'),
    ('23d', 'NTLA', '$15C  Jul-17 @ $3.23',   '3.9x',  'P=82%',   'OI=1,839',  'May-Jun 2026      | ATM $15.31'),
    ('54d', 'MLTX', '$21C  Aug-21 @ $2.80',   '3.6x',  'P=100%',  'OI=1,058',  'Jun-Jul 2026      | Grade B'),
    ('56d', 'RVMD', '$125C Jun-18 @ $29.00',  '4.9x',  'P=90%',   'OI=56',     'NDA H2 2026       | Phase 3 +263%'),
    ('69d', 'RGNX', '$10C  Jul-17 @ $2.25',   '2.8x',  'P=68%',   'OI=1,512',  'BLA mid-2026'),
    ('69d', 'VRDN', '$17C  Jul-17 @ $1.49',   '5.8x',  'P=72%',   'OI=2,309',  'Jun 30 CERTAIN    | OI=2,309'),
    ('158d','PRAX', '$300C Jan-27 @ $103.53', '3.0x',  'P=70%',   'OI=48',     'Sep 27 CERTAIN    | ITM $40.84'),
]
for days, ticker, strike_str, mult, prob, oi_str, note in one_liners:
    lines.append(f'{days} | {ticker}  {strike_str} -- {mult} | {prob} | {oi_str} | {note}')

lines.append('')
lines.append('BELOW THRESHOLD / MONITOR:')
lines.append('  ARGX $800/$850C May15 -- 1.5x residual. Existing: +70% at $20.43. Hold through May 10.')
lines.append('  VERA $55C Jan27 -- 2.1x (below 2.5x). Moved to Monitor. Stock fell to $40.06.')
lines.append('')
lines.append('POSITION SUMMARY:')
lines.append('  RVMD:  entry ~$8    -> $29.00 = ~+263%  | NDA filing play')
lines.append('  ARGX:  entry ~$12   -> $20.43 = +70%    | 18d to PDUFA, stock $805')
lines.append('  AXSM:  entry ~$7.70 -> $8.30   = +8%    | 8 days to PDUFA')
lines.append('  PRAX:  entry $79    -> $103.53 = +31%   | ITM by $40.84')
lines.append('  AGIO:  entry $2.73  -> ITM $3.71        | MDS catalyst imminent')
lines.append('  IDYA:  entry $5.35  -> $1.02   = -81%   | OTM $3.17, NDA catalyst')
lines.append('')
lines.append('SCIENCE GRADES: Unchanged.')
lines.append('DISCOVERY: GD IDV task order (no binary catalyst). No new biotech candidates.')

body = '\n'.join(lines)
with open(BASE/'reports/email_body_2026-04-22.txt','w') as f:
    f.write(body)
print(f'Email: {len(body)} chars')
print('Done.')
