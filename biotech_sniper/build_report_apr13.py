#!/usr/bin/env python3
"""Alpha Sniper Run #14 — Monday April 13, 2026. CATALYST DAY: IDYA + TVTX."""
import json, datetime, openpyxl
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter
from pathlib import Path

from biotech_sniper.paths import BASE_DIR as BASE
opts  = {r['ticker']: r for r in json.load(open(BASE/'state/options_chains_2026-04-13.json'))}
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

# TAB 1 — Qualifying Plays (sorted nearest catalyst)
ws1 = wb.active
ws1.title = '2.5x+ Plays'
ws1.sheet_properties.tabColor = '3FB950'
ws1.merge_cells('A1:L1')
t = ws1['A1']
t.value = f'ALPHA SNIPER  --  {today_str}  --  Run #14  --  9 qualifying (>=2.5x)  --  CATALYST DAY'
cs(t, bg=DARK, fg=GRN, bold=True, sz=12, al='center')
ws1.row_dimensions[1].height = 28
hrow(ws1, 2, ['TICKER','DIRECTION','STRIKE','EXPIRY','MID FILL','MULTIPLE','$1k RETURN',
               'P(WIN)%','SCIENCE','OI','CATALYST DATE','NOTE'])
ws1.row_dimensions[2].height = 20

# ARGX excluded: OI=6/3, untradeable fresh. Pre-market artifact.
qualifying = [
    ('IDYA','CALLS','$35C','2026-05-15', 5.40, 3.7, 3700, 96,'C',10972,'TODAY Apr 13',
     'CATALYST DAY: Topline OPTIMUM-02. IV=188%. OI=10,972. Announces this morning.'),
    ('TVTX','CALLS','$35C','2026-04-17', 3.40, 5.0, 5000, 77,'C', 8627,'TODAY Apr 13',
     'CATALYST DAY: PDUFA. IV=329% fully binary. Auto-remove tomorrow Apr 14.'),
    ('RGNX','CALLS','$10C','2026-07-17', 1.50, 3.9, 3900, 68,'F', 1509,'Apr-May 2026',
     'Back above 2.5x (3.9x). Gene therapy DMD. AbbVie $100M milestone. Grade F = RCT standard.'),
    ('AXSM','CALLS','$200C','2026-05-15', 8.30,14.5,14500, 65,'D',  576,'Apr 30 CERTAIN',
     'BEST MULTIPLE 14.5x. AXS-05 label extension (already approved MDD). BTD + Priority Review.'),
    ('RVMD','CALLS','$125C','2026-06-18', 7.00, 6.9, 6900, 61,'C',   60,'May-Jun 2026',
     'First multi-RAS inhibitor. Phase 3 enrollment complete. Use limit orders (OI=60).'),
    ('AGIO','PUTS', '$30P','2026-08-21', 2.73, 6.8, 6800,  6,'F',  691,'May-Jun 2026',
     'P(fail)=94%. Grade F confirms put. Wrong endpoint, wrong population.'),
    ('NTLA','CALLS','$15C','2026-07-17', 1.92, 4.8, 4800, 82,'C', 1751,'May-Jun 2026',
     'NEJM 3yr data pub (31/32 attack-free). BLA forces disclosure by June.'),
    ('VERA','CALLS','$55C','2027-01-15', 8.05, 3.2, 3200, 72,'C',   19,'Jul 7 CERTAIN',
     'NEW: Promoted from monitor today. Stock +10% to $45.04. BLA accepted Jul 7 PDUFA. Spread=61%.'),
    ('PRAX','CALLS','$300C','2027-01-15',79.00, 3.4, 3400, 70,'C',   62,'Sep 27 CERTAIN',
     'First Nav channel SCN2A/8A blocker. Confirmed PDUFA. Spread=6%. Rare epilepsy.'),
    ('MLTX','CALLS','$21C','2026-08-21', 4.10, 2.8, 2800,100,'B', 1041,'Jun-Jul 2026',
     'Warpspeed 100%. Grade B RCT triple-blind N=960. Small cap re-rating on approval.'),
]

row = 3
for ticker, direc, strike, expiry, mid, mult, k1, p, grade, oi, cat, note in qualifying:
    today_flag = 'TODAY' in cat
    rbg = RBG if 'PUT' in direc else ('161B22' if today_flag else GBG)
    gcol = {'A':GRN,'B':GRN,'C':YLW,'D':ORN,'F':RED}.get(grade, WHT)
    vals = [ticker, direc, strike, expiry, f'${mid:.2f}', f'{mult}x',
            f'${k1:,}', f'{p}%', f'Grade {grade}', oi, cat, note[:120]]
    for col, val in enumerate(vals, 1):
        fg = (YLW if col==1 else (RED if 'PUT' in direc else GRN) if col==2
              else (ORN if mult>=10 else BLU) if col==6
              else (GRN if p>=60 else RED) if col==8
              else gcol if col==9 else WHT)
        c = ws1.cell(row=row, column=col, value=val)
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
t2.value = f'ALL ACTIVE PLAYS -- {today_str} -- 11 plays (9 qualifying + ARGX below threshold + TVTX resolves today)'
cs(t2, bg=DARK, fg=BLU, bold=True, sz=11, al='center')
ws2.row_dimensions[1].height = 24
hrow(ws2, 2, ['TICKER','DRUG','DIRECTION','P(WIN)','SCIENCE','STRIKE','EXPIRY',
               'MID','MULTIPLE','OI','CATALYST','DAYS','NOTES'])

all_rows = [
    ('IDYA','Darovasertib+Crizotinib','CALLS',96,'C','$35C','2026-05-15',5.40,3.7,10972,'TODAY Apr 13',0,
     'CATALYST DAY. Topline OPTIMUM-02 announces this morning. IV=188%.'),
    ('TVTX','Sparsentan FILSPARI sNDA','CALLS',77,'C','$35C','2026-04-17',3.40,5.0,8627,'TODAY Apr 13',0,
     'CATALYST DAY. PDUFA today. IV=329% fully binary. AUTO-REMOVE TOMORROW.'),
    ('RGNX','RGX-202 gene therapy','CALLS',68,'F','$10C','2026-07-17',1.50,3.9,1509,'Apr-May 2026',2,
     'Back to 3.9x. Grade F = non-RCT standard for gene therapy. AbbVie $100M milestone.'),
    ('AXSM','AXS-05 sNDA','CALLS',65,'D','$200C','2026-05-15',8.30,14.5,576,'Apr 30 CERTAIN',17,
     'BEST MULTIPLE 14.5x. Already-approved drug label extension. 17 days.'),
    ('ARGX','Efgartigimod $800/$850C','SPREAD',87,'D','$800/$850C','2026-05-15',5.75,7.7,3,'May 10 CERTAIN',27,
     'BELOW THRESHOLD (OI=6/3, untradeable). Pre-mkt artifact. Hold existing position through May 10.'),
    ('RVMD','RMC-6236 RAS inhibitor','CALLS',61,'C','$125C','2026-06-18',7.00,6.9,60,'May-Jun 2026',32,
     'Phase 3 enrollment complete. OI=60, use limit orders.'),
    ('AGIO','Tebapivat AG-946','PUTS',6,'F','$30P','2026-08-21',2.73,6.8,691,'May-Jun 2026',32,
     'P(fail)=94%. Grade F confirms put thesis.'),
    ('NTLA','NTLA-2002 CRISPR HAE','CALLS',82,'C','$15C','2026-07-17',1.92,4.8,1751,'May-Jun 2026',32,
     'NEJM 3yr data published. BLA forces June disclosure.'),
    ('VERA','Atacicept IgAN BLA','CALLS',72,'C','$55C','2027-01-15',8.05,3.2,19,'Jul 7 CERTAIN',85,
     'NEW TODAY. Stock +10% to $45.04. BLA accepted Jul 7 PDUFA. Spread=61% (OI=19).'),
    ('MLTX','Sonelokimab IZAR-1','CALLS',100,'B','$21C','2026-08-21',4.10,2.8,1041,'Jun-Jul 2026',63,
     'Warpspeed 100%. Grade B. RCT triple-blind N=960.'),
    ('PRAX','Relutrigine NDA','CALLS',70,'C','$300C','2027-01-15',79.00,3.4,62,'Sep 27 CERTAIN',167,
     'Confirmed PDUFA. Rare epilepsy. Spread=6%.'),
]

for i, rd in enumerate(all_rows, 3):
    ticker,drug,direc,p,grade,strike,expiry,mid,mult,oi,cat,days,notes = rd
    rbg = RBG if 'PUT' in direc else (GBG if mult>=2.5 else OBG)
    gcol = {'A':GRN,'B':GRN,'C':YLW,'D':ORN,'F':RED}.get(grade, WHT)
    row_vals = [ticker,drug,direc,f'{p}%',f'Grade {grade}',strike,expiry,
                f'${mid:.2f}',f'{mult}x',oi,cat,f'{days}d',notes]
    for col,val in enumerate(row_vals,1):
        c = ws2.cell(row=i,column=col,value=val)
        fg = (YLW if col==1 else (GRN if p>=60 else RED) if col==4
              else gcol if col==5
              else (ORN if mult>=10 else BLU if mult>=2.5 else GRY) if col==9 else WHT)
        cs(c,bg=rbg,fg=fg,bold=(col==1),wrap=(col==13))
    ws2.row_dimensions[i].height = 36

for i,w in enumerate([8,28,10,8,10,12,12,9,9,7,14,7,55],1):
    ws2.column_dimensions[get_column_letter(i)].width = w

# TAB 3 — IDYA/TVTX Catalyst Reference
ws3 = wb.create_sheet('Catalyst Day Reference')
ws3.sheet_properties.tabColor = 'F85149'
ws3.merge_cells('A1:E1')
t3 = ws3['A1']
t3.value = 'CATALYST DAY REFERENCE -- April 13, 2026 -- IDYA + TVTX'
cs(t3, bg=DARK, fg=RED, bold=True, sz=12, al='center')
ws3.row_dimensions[1].height = 24
hrow(ws3, 2, ['TICKER','EVENT','OPTION','CURRENT FILL','IF STOCK 2x'])

cat_rows = [
    ('IDYA','Topline OPTIMUM-02 (DAR-UM-2) -- First 1L uveal melanoma therapy',
     '$35C May-15','$5.40 mid | OI=10,972 | IV=188%',
     'Stock $30.50 -> 2x $61 | intrinsic $26 / $5.40 = 4.8x | $1k -> $4,800'),
    ('TVTX','PDUFA sparsentan FSGS label expansion (already approved IgAN)',
     '$35C Apr-17','$3.40 mid | OI=8,627 | IV=329%',
     'Stock $28.96 -> 2x $57.92 | intrinsic $22.92 / $3.40 = 6.7x | $1k -> $6,700'),
]
notes_rows = [
    ('IV NOTE','Both IVs are extreme (188% and 329%). Options will move violently.','','',''),
    ('DEDUP NOTE','These are independent events. Results may come at different times today.','','',''),
    ('IDYA TIMING','Announcement confirmed via PRN to come Monday April 13 before/at market open.','','',''),
    ('TVTX TIMING','FDA PDUFA typically notified during business hours (10am-5pm ET).','','',''),
    ('POST-EVENT','TVTX: auto-remove Tuesday Apr 14 regardless of result.','','',''),
    ('POST-EVENT','IDYA: update play with result. If positive keep (May 15 expiry). If negative remove.','','',''),
]
for i, rd in enumerate(cat_rows+notes_rows, 3):
    for col, val in enumerate(rd, 1):
        c = ws3.cell(row=i, column=col, value=val)
        cs(c, bg=(GBG if i<=len(cat_rows)+2 else DARK), fg=(YLW if col==1 else WHT), wrap=True)
    ws3.row_dimensions[i].height = 36
for i,w in enumerate([8,55,14,30,40],1):
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
     'Bid $0 on most strikes. Watch May for liquidity.','When spread < 80%'),
    ('RZLT','Ersodetug upLIFT','H2 2026','72%',
     '16-patient trial. No liquid options.','If liquidity builds'),
    ('NAMS','Obicetrapib PREVAIL','Nov 2026','37%',
     'PUT candidate P=37%. N=9,541 CV outcomes. Too far.','When within 90 days'),
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

path = BASE / 'reports/Alpha_Sniper_2026-04-13.xlsx'
wb.save(path)
print(f'Saved: {path}')

# EMAIL BODY
lines = []
lines.append('ALPHA SNIPER -- April 13, 2026 (CATALYST DAY)')
lines.append('Run #14  |  11 active plays  |  9 qualifying (>=2.5x)  |  Best: AXSM 14.5x')
lines.append('=' * 65)
lines.append('')
lines.append('TODAY: IDYA + TVTX BOTH ANNOUNCE')
lines.append('')
lines.append('  IDYA  $35C May-15 @ $5.40 -- 3.7x  |  P=96%  |  IV=188%  |  OI=10,972')
lines.append('        Topline OPTIMUM-02 -- first 1L uveal melanoma therapy')
lines.append('        If stock 2x to $61: intrinsic $26 / $5.40 = 4.8x on $1k = $4,800')
lines.append('')
lines.append('  TVTX  $35C Apr-17 @ $3.40 -- 5.0x  |  P=77%  |  IV=329%  |  OI=8,627')
lines.append('        PDUFA sparsentan FSGS -- already approved for IgAN')
lines.append('        If stock 2x to $57.92: intrinsic $22.92 / $3.40 = 6.7x on $1k = $6,700')
lines.append('        AUTO-REMOVE TOMORROW Tuesday April 14')
lines.append('')
lines.append('NEW PLAY: VERA $55C Jan27 @ $8.05 -- 3.2x')
lines.append('  Atacicept IgAN BLA. Jul 7 PDUFA confirmed. Stock +10% today to $45.04.')
lines.append('  Promoted from monitor. OI=19, spread=61% -- use limit orders, small size.')
lines.append('')
lines.append('NEW THIS RUN: RGNX back above threshold at 3.9x (was 2.2x Friday).')
lines.append('')
lines.append('ONE-LINERS (sorted nearest catalyst, >=2.5x):')
lines.append('-' * 65)
one_liners = [
    ('0d', 'IDYA', '$35C  May-15 @ $5.40', '3.7x', 'P=96%',    'OI=10,972','TODAY CERTAIN     | IV=188% | Grade C'),
    ('0d', 'TVTX', '$35C  Apr-17 @ $3.40', '5.0x', 'P=77%',    'OI=8,627', 'TODAY CERTAIN     | IV=329% | Grade C | remove Apr 14'),
    ('2d', 'RGNX', '$10C  Jul-17 @ $1.50', '3.9x', 'P=68%',    'OI=1,509', 'Apr-May 2026      | Grade F (gene therapy standard)'),
    ('17d','AXSM', '$200C May-15 @ $8.30', '14.5x','P=65%',    'OI=576',   'Apr 30 CERTAIN    | Grade D (PDUFA bet) -- BEST'),
    ('32d','RVMD', '$125C Jun-18 @ $7.00', '6.9x', 'P=61%',    'OI=60',    'May-Jun 2026      | Phase 3 done'),
    ('32d','AGIO', '$30P  Aug-21 @ $2.73', '6.8x', 'P(f)=94%', 'OI=691',   'May-Jun 2026      | Grade F confirms put'),
    ('32d','NTLA', '$15C  Jul-17 @ $1.92', '4.8x', 'P=82%',    'OI=1,751', 'May-Jun 2026      | NEJM data pub'),
    ('85d','VERA', '$55C  Jan-27 @ $8.05', '3.2x', 'P=72%',    'OI=19',    'Jul 7 CERTAIN     | NEW today, spread=61%'),
    ('167d','PRAX','$300C Jan-27 @ $79.00','3.4x', 'P=70%',    'OI=62',    'Sep 27 CERTAIN    | Rare epilepsy, spread=6%'),
    ('63d','MLTX', '$21C  Aug-21 @ $4.10', '2.8x', 'P=100%',   'OI=1,041', 'Jun-Jul 2026      | Grade B, Warpspeed 100%'),
]
for days, ticker, strike_str, mult, prob, oi_str, note in one_liners:
    lines.append(f'{days} | {ticker}  {strike_str} -- {mult} | {prob} | {oi_str} | {note}')

lines.append('')
lines.append('BELOW THRESHOLD (Excel only):')
lines.append('  ARGX $800/$850C May15 -- OI=6/3 untradeable, pre-mkt artifact shows 7.7x. Hold existing position.')
lines.append('')
lines.append('=' * 65)
lines.append('SCIENCE GRADES')
lines.append('-' * 65)
lines.append('Grade B -- MLTX: RCT triple-blind N=960 ACR50. Best designed trial.')
lines.append('Grade C -- IDYA, TVTX, RVMD, NTLA, VERA, PRAX: Adequate design.')
lines.append('Grade D -- AXSM (PDUFA bet, not trial). ARGX (open-label, prior data strong).')
lines.append('Grade F -- RGNX (non-RCT standard for gene therapy). AGIO (confirms put thesis).')
lines.append('')
lines.append('=' * 65)
lines.append('PLAY CARDS')
lines.append('-' * 65)

cards = [
    ('IDYA','CALLS','3.7x','P=96%','Grade C','$35C May-15 @ $5.40 | OI=10,972 | IV=188%',
     'TODAY April 13 (topline OPTIMUM-02 confirmed via PRN)',
     'First 1L therapy for HLA-A2-neg uveal melanoma. Warpspeed P=96% = 90% on PFS, 46% on both PFS+OS. OI=10,972 reflects significant positioning. DB lock was April 1-15.',
     'Market fully prices the binary at IV=188%. Pre-reg at ir.ideayabio.com likely fires before press release. If stock 2x to $61, intrinsic $26 on $5.40 entry = 4.8x. $1k -> ~$4,800.'),
    ('TVTX','CALLS','5.0x','P=77%','Grade C','$35C Apr-17 @ $3.40 | OI=8,627 | IV=329%',
     'TODAY April 13 (PDUFA sparsentan FSGS -- same day as IDYA)',
     'FILSPARI already approved for IgAN. FSGS label expansion with Phase 3 DUPLEX data. Stock $28.96 vs $35 strike. IV=329% = fully binary. AUTO-REMOVE TOMORROW April 14.',
     'Already-approved drug = lower regulatory bar. If stock 2x to $57.92, intrinsic $22.92 on $3.40 entry = 6.7x. $1k -> ~$6,700.'),
    ('RGNX','CALLS','3.9x','P=68%','Grade F (gene therapy standard)',
     '$10C Jul-17 @ $1.50 | OI=1,509 | IV=stable',
     'Apr-May 2026',
     'RGX-202 gene therapy for Duchenne MD. AbbVie $100M milestone expected 1H 2026. All 4 pivotal-dose patients beat NSAA trajectory +7.4pts at JPM. BLA mid-2026. Back above threshold at 3.9x.',
     'Grade F = non-RCT design but FDA-accepted standard for gene therapy (Elevidys, Zolgensma approved same way). If stock 2x to $15.84, intrinsic $5.84 on $1.50 = 3.9x. $1k -> ~$3,900.'),
    ('AXSM','CALLS','14.5x','P=65%','Grade D','$200C May-15 @ $8.30 | OI=576 | IV=73%',
     'Apr 30 2026 (PDUFA CERTAIN)',
     'Best multiple at 14.5x. AXS-05 (Auvelity) already FDA-approved for MDD. Label extension to Alzheimer agitation. BTD + Priority Review. Stock at $178.11.',
     'Not a trial result bet. FDA has all the data. Label extension = lower bar. Pre-PDUFA drift accelerates in final 10 days. Grade D = trial design only. $1k -> ~$14,500.'),
    ('RVMD','CALLS','6.9x','P=61%','Grade C','$125C Jun-18 @ $7.00 | OI=60 | IV=101%',
     'May-Jun 2026',
     'RMC-6236 first multi-RAS(ON) selective inhibitor targeting KRAS/NRAS/HRAS. Phase 3 RASolute-302 enrollment complete. Phase 1/2: 35% ORR, 8.5mo PFS vs 3.4mo chemotherapy.',
     'KRAS undruggable for decades. Only multi-RAS selectivity. OI=60 -- use limit orders only. $1k -> ~$6,900.'),
    ('AGIO','PUTS','6.8x','P(fail)=94%','Grade F','$30P Aug-21 @ $2.73 | OI=691 | IV=55%',
     'May-Jun 2026',
     'Phase 2b tebapivat uses wrong endpoint (8-week TI) for hardest-to-treat MDS patients. Phase 2a worked only in easiest subset. Competing PKR activator already terminated for futility.',
     'Grade F confirms put thesis. Non-RCT + surrogate endpoint + wrong patient population. P(fail)=94%. $1k -> ~$6,800.'),
    ('NTLA','CALLS','4.8x','P=82%','Grade C','$15C Jul-17 @ $1.92 | OI=1,751 | IV=102%',
     'May-Jun 2026',
     'NTLA-2002 CRISPR HAE prophylaxis. NEJM April 2: 96% attack reduction, 31/32 attack-free at 3 years. BLA H2 2026. Multiple improved from 4.1x to 4.8x on fill improvement.',
     'Data already published. BLA timeline forces final topline disclosure before June. Window closing. $1k -> ~$4,800.'),
    ('VERA','CALLS','3.2x','P=72%','Grade C','$55C Jan-27 @ $8.05 | OI=19 | spread=61%',
     'Jul 7 2026 (PDUFA CERTAIN)',
     'Atacicept IgAN BLA. Phase 3 ORIGIN completed May 2025. RCT double-blind placebo-controlled N=376. BLA accepted. Stock +10% today to $45.04. Promoted from monitor.',
     'OI=19 and spread=61% means use limit orders and small size. Jan27 expiry gives 6-month buffer past PDUFA. If stock 2x to $81, intrinsic $26 on $8.05 = 3.2x. $1k -> ~$3,200.'),
    ('PRAX','CALLS','3.4x','P=70%','Grade C','$300C Jan-27 @ $79.00 | OI=62 | spread=6%',
     'Sep 27 2026 (PDUFA CERTAIN)',
     'Relutrigine (PRAX-562) first Nav1.2/Nav1.6 blocker for SCN2A/SCN8A DEE. 5,000 US patients. No targeted treatment. FDA accepted NDA. Stock at $316.14.',
     'Confirmed PDUFA Sep 27. Jan27 = 4-month buffer. Spread=6% unusually tight. IV builds into event. Rare disease premium on approval. $1k -> ~$3,400.'),
    ('MLTX','CALLS','2.8x','P=100%','Grade B','$21C Aug-21 @ $4.10 | OI=1,041 | IV=123%',
     'Jun-Jul 2026',
     'Sonelokimab IZAR-1 Phase 3 PsA readout. Warpspeed P=100%. Triple-blind placebo-controlled RCT, N=960. ACR50 = FDA gold standard. Grade B = best designed trial in portfolio.',
     'Warpspeed 100% extremely rare. Small cap ($500M) = large re-rating on approval. $1k -> ~$2,800.'),
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
lines.append('  VRDN  veligrotug -- Jun 30 PDUFA  | Spreads $0 bid, watch May')
lines.append('  NAMS  obicetrapib -- Nov 2026       | PUT candidate P=37%, too far')
lines.append('')
lines.append('TOMORROW April 14: Auto-remove TVTX. Update IDYA with result.')
lines.append('Intraday alerts fire immediately on any IDYA/TVTX result today.')

body = '\n'.join(lines)
with open(BASE/'reports/email_body_2026-04-13.txt','w') as f:
    f.write(body)
print(f'Email body: {len(body)} chars')
print('Done.')
