#!/usr/bin/env python3
"""Alpha Sniper Run #19 — Saturday April 18, 2026. AACR Day 2."""
import json, datetime, openpyxl
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter
from pathlib import Path

from biotech_sniper.paths import BASE_DIR as BASE
today_str = datetime.date.today().strftime('%B %d, %Y')

DARK='0D1117'; GBG='0D2818'; RBG='2D0A0A'; OBG='2D1A00'; HDR='161B22'
WHT='FFFFFF'; GRN='3FB950'; RED='F85149'; ORN='D29922'; YLW='E3B341'
GRY='8B949E'; BLU='58A6FF'; PRP='BC8CFF'

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
t.value = f'ALPHA SNIPER  --  {today_str}  --  Run #19  --  10 qualifying (>=2.5x)  --  AACR Day 2'
cs(t, bg=DARK, fg=GRN, bold=True, sz=12, al='center')
ws1.row_dimensions[1].height = 28
hrow(ws1, 2, ['TICKER','DIR','STRIKE','EXPIRY','MID FILL','MULTIPLE','$1k RETURN',
               'P(WIN)%','SCIENCE','OI','CATALYST DATE','NOTE'])
ws1.row_dimensions[2].height = 20

qualifying = [
    ('AXSM','CALLS','$200C','2026-05-15',11.50,12.2,12200, 65,'D',2267,'Apr 30 CERTAIN',
     '12 days. OI=2,267. Stock +2.6% to $188.99. Pre-PDUFA drift in final stretch.'),
    ('IDYA','CALLS','$35C','2026-05-15', 1.02,25.4,25400, 75,'C',11201,'NDA H2 2026',
     'Stock +4.3% to $33.91. Only $1.09 from $35 strike! NDA catalyst. 27 days.'),
    ('AGIO','PUTS', '$30P','2026-08-21', 2.50, 7.1, 7100,  6,'F',  691,'May-Jun 2026',
     'P(fail)=94%. Grade F confirms put. Stock up on sector strength. Thesis unchanged.'),
    ('NTLA','CALLS','$15C','2026-07-17', 2.58, 4.6, 4600, 82,'C', 1801,'May-Jun 2026',
     'Stock +5.6% to $14.95. Essentially ATM at $15 strike. BLA forces June disclosure.'),
    ('VRDN','CALLS','$17C','2026-07-17', 1.95, 4.9, 4900, 72,'C', 2049,'Jun 30 CERTAIN',
     'OI=2,049. Jun 30 PDUFA. Stock flat at $14.80. Pre-mkt bid=$0 (normal).'),
    ('MLTX','CALLS','$21C','2026-08-21', 3.35, 3.6, 3600,100,'B', 1055,'Jun-Jul 2026',
     'Warpspeed 100%. Grade B RCT. Stock +3.1%.'),
    ('RVMD','CALLS','$125C','2026-06-18',27.85, 5.1, 5100, 90,'C',   56,'AACR+NDA H2',
     'Phase 3 winner. AACR Day 2. Stock -0.4% (flat). Option ~+248% from entry.'),
    ('PRAX','CALLS','$300C','2027-01-15',105.00, 3.0, 3000, 70,'C',  48,'Sep 27 CERTAIN',
     'Stock +6.9% to $342.50 on Friday. $300C ITM by $42.50. Sep 27 PDUFA.'),
    ('VERA','CALLS','$55C','2027-01-15', 8.05, 2.6, 2600, 72,'C',   19,'Jul 7 CERTAIN',
     'Atacicept IgAN BLA. Jul 7 PDUFA. OI=19, use limit orders.'),
    ('RGNX','CALLS','$10C','2026-07-17', 2.60, 2.7, 2700, 68,'F', 1509,'BLA mid-2026',
     'AbbVie $100M milestone. Stock +3.3%. IV=150% -- note wide spread pre-market.'),
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
t2.value = f'ALL ACTIVE PLAYS -- {today_str} -- 11 plays | Friday Close Prices'
cs(t2, bg=DARK, fg=BLU, bold=True, sz=11, al='center')
ws2.row_dimensions[1].height = 24
hrow(ws2, 2, ['TICKER','DRUG','DIR','P','SCI','STRIKE','EXPIRY','MID','MULT','OI',
               'CATALYST','DAYS','P&L','NOTES'])

all_rows = [
    ('AXSM','AXS-05 sNDA','CALLS',65,'D','$200C','2026-05-15',11.50,12.2,2267,'Apr 30 CERTAIN',12,
     '+49%','12 days. Stock $188.99. OI=2,267.'),
    ('IDYA','Darovasertib+Criz','CALLS',75,'C','$35C','2026-05-15',1.02,25.4,11201,'NDA H2 2026',27,
     '-81%','Stock $33.91 -- only $1.09 from $35C strike! NDA catalyst.'),
    ('RGNX','RGX-202 gene therapy','CALLS',68,'F','$10C','2026-07-17',2.60,2.7,1509,'BLA mid-2026',73,
     'flat','AbbVie milestone. Stock +3.3% to $9.49. IV=150%.'),
    ('ARGX','Efgar $800/$850C','SPREAD',87,'D','$800/$850C','2026-05-15',31.15,0.6,5,'May 10 CERTAIN',22,
     '+160%','STOCK $849.04 -- ONLY $0.96 FROM MAX PAYOUT. PDUFA May 10. HOLD.'),
    ('RVMD','Daraxonrasib','CALLS',90,'C','$125C','2026-06-18',27.85,5.1,56,'AACR+NDA H2',60,
     '+248%','Phase 3 winner. AACR Day 2 today. Stock flat at $148.63.'),
    ('AGIO','Tebapivat AG-946','PUTS',6,'F','$30P','2026-08-21',2.50,7.1,691,'May-Jun 2026',27,
     '-8%','Grade F put. Stock $35.14 (sector). Thesis intact. May-Jun readout.'),
    ('NTLA','NTLA-2002 CRISPR','CALLS',82,'C','$15C','2026-07-17',2.58,4.6,1801,'May-Jun 2026',27,
     'flat','Stock +5.6% to $14.95 (essentially ATM). BLA forces June.'),
    ('VRDN','Veligrotug TED BLA','CALLS',72,'C','$17C','2026-07-17',1.95,4.9,2049,'Jun 30 CERTAIN',73,
     'flat','OI=2,049. Jun 30 PDUFA. Pre-mkt bid=$0.'),
    ('MLTX','Sonelokimab IZAR-1','CALLS',100,'B','$21C','2026-08-21',3.35,3.6,1055,'Jun-Jul 2026',58,
     'flat','Warpspeed 100%. Grade B. Stock +3.1%.'),
    ('VERA','Atacicept IgAN','CALLS',72,'C','$55C','2027-01-15',8.05,2.6,19,'Jul 7 CERTAIN',80,
     'flat','Jul 7 PDUFA. OI=19.'),
    ('PRAX','Relutrigine NDA','CALLS',70,'C','$300C','2027-01-15',105.00,3.0,48,'Sep 27 CERTAIN',162,
     '+33%','Stock +6.9% to $342.50. $300C ITM by $42.50. Sep 27 PDUFA.'),
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

# TAB 3 — ARGX Decision
ws3 = wb.create_sheet('ARGX Exit Analysis')
ws3.sheet_properties.tabColor = 'F85149'
ws3.merge_cells('A1:D1')
t3 = ws3['A1']
t3.value = 'ARGX $800/$850 CALL SPREAD -- EXIT ANALYSIS -- April 18, 2026'
cs(t3, bg=DARK, fg=YLW, bold=True, sz=12, al='center')
ws3.row_dimensions[1].height = 24
hrow(ws3, 2, ['SCENARIO','STOCK AT MAY 15','SPREAD VALUE','NET GAIN ON $12 ENTRY'])
scenarios = [
    ('Exit NOW (weekend)', '$849.04','~$31.15 current value','+$19.15 = +160%'),
    ('PDUFA miss (worst case)','<$800','$0 (spread worthless)','-$12 = -100%'),
    ('PDUFA partial move','$820','$20 of $50 max','+$8 = +67%'),
    ('PDUFA approval (base)','$850+','$50 MAX PAYOUT','+$38 = +317%'),
    ('Strong approval','$900+','$50 MAX PAYOUT (capped)','+$38 = +317% same'),
]
notes = [
    ('RECOMMENDATION','HOLD through PDUFA (May 10). P=87% approval, only $0.96 stock move needed for max.','',''),
    ('OI','$800C OI=26 | $850C OI=5 -- very thin. Verify spread can be closed at reasonable prices.','',''),
    ('TIMING','PDUFA May 10, 2026. May 15 expiry covers + 3 trading days buffer.','',''),
]
for i, rd in enumerate(scenarios+notes, 3):
    for col, val in enumerate(rd, 1):
        c = ws3.cell(row=i, column=col, value=val)
        cs(c, bg=(GBG if '+317%' in str(val) or 'HOLD' in str(val) else DARK), fg=WHT, wrap=True)
    ws3.row_dimensions[i].height = 40
for i,w in enumerate([20,22,25,25],1):
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
    ('RZLT','Ersodetug upLIFT','H2 2026','72%','No liquid options.','If liquidity builds'),
    ('NAMS','Obicetrapib PREVAIL','Nov 2026','37%','PUT candidate. ~205 days.','When within 90 days'),
    ('AMGN','Olpasiran OCEAN-a','2027','71%','Mega cap. 2027 readout.','Remove in 30d if still 2027'),
]
for i,rd in enumerate(mon_rows,3):
    for col,val in enumerate(rd,1):
        c = ws4.cell(row=i,column=col,value=val)
        cs(c,bg=DARK,fg=(YLW if col==1 else WHT),wrap=(col in [5,6]))
    ws4.row_dimensions[i].height = 36
for i,w in enumerate([8,28,18,7,55,26],1):
    ws4.column_dimensions[get_column_letter(i)].width = w

path = BASE / 'reports/Alpha_Sniper_2026-04-18.xlsx'
wb.save(path)
print(f'Saved: {path}')

# EMAIL
lines = []
lines.append('ALPHA SNIPER -- April 18, 2026 (Saturday) -- AACR Day 2')
lines.append('Run #19  |  11 active plays  |  10 qualifying (>=2.5x)  |  Best: IDYA 25.4x')
lines.append('=' * 65)
lines.append('')
lines.append('HEADLINE: ARGX AT $849.04 -- $0.96 FROM MAX PAYOUT')
lines.append('')
lines.append('  $800/$850C May15 spread:')
lines.append('  Entry: ~$12 | Current value: $31.15 | Gain: +160%')
lines.append('  Stock $849.04 -- only $0.96 from triggering full $50 max payout')
lines.append('  PDUFA May 10 = 22 days. P=87% approval.')
lines.append('  If stock opens above $850 Monday and holds -> spread maxes out at $50 (+317% on $12 entry)')
lines.append('')
lines.append('  RECOMMENDATION: HOLD. The 87% approval probability with stock this close')
lines.append('  to the trigger makes early exit expensive. Only 0.11% more stock move needed.')
lines.append('  Risk: 13% chance of miss = spread goes to $0.')
lines.append('')
lines.append('STRONG FRIDAY CLOSE (biotech recovery):')
lines.append('  PRAX +6.9% to $342.50 | $300C ITM by $42.50 | +33% option gain')
lines.append('  NTLA +5.6% to $14.95 | essentially ATM at $15C strike')
lines.append('  IDYA +4.3% to $33.91 | only $1.09 from $35C strike!')
lines.append('  AXSM +2.6% to $188.99 | OI=2,267 | 12 days to PDUFA')
lines.append('  ARGX +2.5% to $849.04 | $0.96 from max payout')
lines.append('')
lines.append('IDYA NOW NEAR THE MONEY:')
lines.append('  Stock $33.91 -- only $1.09 from the $35C strike.')
lines.append('  Option at $1.02 (was $5.35 at entry = -81%).')
lines.append('  Math multiple is 25.4x -- if stock closes above $35 at May 15, this wins.')
lines.append('  NDA filing announcement would push stock above $35.')
lines.append('  Consider: hold existing position. High math leverage at low cost.')
lines.append('')
lines.append('AGIO WATCH:')
lines.append('  Stock $35.14 (+0.6% Friday). Put mid dropped to $2.50 (was $2.73 entry = -8%).')
lines.append('  Grade F science unchanged. Readout May-Jun 2026. Hold.')
lines.append('')
lines.append('ONE-LINERS (sorted nearest catalyst, >=2.5x):')
lines.append('-' * 65)
one_liners = [
    ('12d', 'AXSM', '$200C May-15 @ $11.50',  '12.2x', 'P=65%',    'OI=2,267', 'Apr 30 CERTAIN    | +49% gain | 12 days'),
    ('27d', 'IDYA', '$35C  May-15 @ $1.02',   '25.4x', 'P=75%',    'OI=11,201','NDA H2 2026       | NEAR THE MONEY $1.09 OTM'),
    ('27d', 'AGIO', '$30P  Aug-21 @ $2.50',   '7.1x',  'P(f)=94%', 'OI=691',   'May-Jun 2026      | Grade F put | -8% entry'),
    ('27d', 'NTLA', '$15C  Jul-17 @ $2.58',   '4.6x',  'P=82%',    'OI=1,801', 'May-Jun 2026      | +5.6% Fri, ATM at $15C'),
    ('58d', 'MLTX', '$21C  Aug-21 @ $3.35',   '3.6x',  'P=100%',   'OI=1,055', 'Jun-Jul 2026      | Grade B Warpspeed 100%'),
    ('60d', 'RVMD', '$125C Jun-18 @ $27.85',  '5.1x',  'P=90%',    'OI=56',    'AACR Day 2        | Phase 3 winner +248%'),
    ('73d', 'RGNX', '$10C  Jul-17 @ $2.60',   '2.7x',  'P=68%',    'OI=1,509', 'BLA mid-2026      | AbbVie milestone'),
    ('73d', 'VRDN', '$17C  Jul-17 @ $1.95',   '4.9x',  'P=72%',    'OI=2,049', 'Jun 30 CERTAIN    | OI=2,049'),
    ('80d', 'VERA', '$55C  Jan-27 @ $8.05',   '2.6x',  'P=72%',    'OI=19',    'Jul 7 CERTAIN     | small size'),
    ('162d','PRAX', '$300C Jan-27 @ $105.00', '3.0x',  'P=70%',    'OI=48',    'Sep 27 CERTAIN    | ITM by $42.50 | +33%'),
]
for days, ticker, strike_str, mult, prob, oi_str, note in one_liners:
    lines.append(f'{days} | {ticker}  {strike_str} -- {mult} | {prob} | {oi_str} | {note}')

lines.append('')
lines.append('BELOW THRESHOLD:')
lines.append('  ARGX $800/$850C May15 -- 0.6x fresh (still dead to add).')
lines.append('  Existing position: +160% at $31.15. STOCK $0.96 FROM MAX. HOLD through May 10.')
lines.append('')
lines.append('POSITION SUMMARY (unrealized):')
lines.append('  RVMD:  entry ~$8    -> $27.85 = ~+248% | AACR Day 2 today')
lines.append('  ARGX:  entry ~$12   -> $31.15 = +160%  | $0.96 from max payout of +317%')
lines.append('  AXSM:  entry ~$7.70 -> $11.50 = +49%   | 12 days to Apr 30 PDUFA')
lines.append('  PRAX:  entry $79    -> $105.00 = +33%  | ITM by $42.50')
lines.append('  IDYA:  entry $5.35  -> $1.02   = -81%  | Near the money ($1.09 OTM)')
lines.append('  AGIO:  entry $2.73  -> $2.50   = -8%   | Thesis intact, readout May-Jun')
lines.append('')
lines.append('=' * 65)
lines.append('SCIENCE GRADES (unchanged)')
lines.append('-' * 65)
lines.append('Grade B -- MLTX: RCT triple-blind N=960 ACR50.')
lines.append('Grade C -- IDYA, RVMD, NTLA, VRDN, VERA, PRAX: Adequate design.')
lines.append('Grade D -- AXSM (PDUFA bet). ARGX (prior data strong, +160% theoretical).')
lines.append('Grade F -- RGNX (gene therapy standard). AGIO (confirms put thesis).')
lines.append('')
lines.append('WEEKEND AACR: RVMD presentations continue today and through Apr 22.')
lines.append('Next week: AXSM PDUFA on April 30 (12 trading days).')

body = '\n'.join(lines)
with open(BASE/'reports/email_body_2026-04-18.txt','w') as f:
    f.write(body)
print(f'Email: {len(body)} chars')
print('Done.')
