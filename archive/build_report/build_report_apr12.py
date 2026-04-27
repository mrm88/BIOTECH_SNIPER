#!/usr/bin/env python3
"""Alpha Sniper Run #13 — Sunday April 12, 2026."""
import json, datetime, openpyxl
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter
from pathlib import Path

from biotech_sniper.paths import BASE_DIR as BASE
opts  = {r['ticker']: r for r in json.load(open(BASE/'state/options_chains_2026-04-12.json'))}
plays = json.load(open(BASE/'state/active_plays.json'))
today = datetime.date.today().strftime('%B %d, %Y')

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

# TAB 1 — 2.5x+ Plays
ws1 = wb.active
ws1.title = '2.5x+ Plays'
ws1.sheet_properties.tabColor = '3FB950'
ws1.merge_cells('A1:L1')
t = ws1['A1']
t.value = f'ALPHA SNIPER  --  {today}  --  Run #13  --  8 qualifying (>=2.5x)'
cs(t, bg=DARK, fg=GRN, bold=True, sz=13, al='center')
ws1.row_dimensions[1].height = 28
hrow(ws1, 2, ['TICKER','DIRECTION','STRIKE','EXPIRY','MID FILL','MULTIPLE','$1k RETURN',
               'P(WIN)%','SCIENCE','OI','CATALYST DATE','NOTE'])
ws1.row_dimensions[2].height = 20

qualifying = [
    ('IDYA', 'CALLS','$35C',       '2026-05-15',  5.35, 3.7,  3700, 96,'C',10708,'Apr 13 CERTAIN',
     'TOMORROW: topline OPTIMUM-02. IV=188%. OI=10,708. First 1L uveal melanoma therapy.'),
    ('TVTX', 'CALLS','$35C',       '2026-04-17',  2.85, 6.0,  6000, 77,'C', 8614,'Apr 13 CERTAIN',
     'TOMORROW: PDUFA. IV=329% (fully binary). Stock $28.96 vs $35C strike. Same day as IDYA.'),
    ('AXSM', 'CALLS','$200C',      '2026-05-15',  8.05,15.0, 15000, 65,'D',  573,'Apr 30 CERTAIN',
     'BEST MULTIPLE 15x. AXS-05 label extension (already approved for MDD). BTD + Priority Review.'),
    ('AGIO', 'PUTS', '$30P',       '2026-08-21',  2.82, 6.5,  6500,  6,'F',  691,'May-Jun 2026',
     'P(fail)=94%. Grade F confirms put. Wrong endpoint, wrong patient population.'),
    ('RVMD', 'CALLS','$125C',      '2026-06-18',  8.10, 6.0,  6000, 61,'C',   55,'May-Jun 2026',
     'First multi-RAS inhibitor. Phase 3 enrollment complete. Use limit orders (OI=55).'),
    ('NTLA', 'CALLS','$15C',       '2026-07-17',  2.25, 4.1,  4100, 82,'C', 1752,'May-Jun 2026',
     'NEJM 3yr data pub Apr 2 (31/32 attack-free). BLA forces disclosure by June.'),
    ('PRAX', 'CALLS','$300C',      '2027-01-15', 79.00, 3.4,  3400, 70,'C',   62,'Sep 27 CERTAIN',
     'First Nav channel SCN2A/8A blocker. 5,000 US patients. PDUFA confirmed. Spread=6%.'),
    ('MLTX', 'CALLS','$21C',       '2026-08-21',  4.20, 2.7,  2700,100,'B', 1041,'Jun-Jul 2026',
     'Warpspeed 100%. Grade B RCT. Stock dipped -7.5% Friday (thin volume, no news). Still qualifies.'),
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
    ws1.row_dimensions[row].height = 42
    row += 1

for i, w in enumerate([8,10,10,12,10,10,12,9,10,7,20,68], 1):
    ws1.column_dimensions[get_column_letter(i)].width = w

# TAB 2 — All Active
ws2 = wb.create_sheet('All Active')
ws2.sheet_properties.tabColor = '58A6FF'
ws2.merge_cells('A1:M1')
t2 = ws2['A1']
t2.value = f'ALL ACTIVE PLAYS -- {today} -- 10 plays'
cs(t2, bg=DARK, fg=BLU, bold=True, sz=12, al='center')
ws2.row_dimensions[1].height = 24
hrow(ws2, 2, ['TICKER','DRUG','DIRECTION','P(WIN)','SCIENCE','STRIKE','EXPIRY',
               'MID','MULTIPLE','OI','CATALYST','DAYS','NOTES'])

all_rows = [
    ('IDYA','Darovasertib+Crizotinib','CALLS',96,'C','$35C','2026-05-15',5.35,3.7,10708,'Apr 13 CERTAIN',2,
     'TOMORROW. IV=188%. OI=10,708.'),
    ('TVTX','Sparsentan FILSPARI sNDA','CALLS',77,'C','$35C','2026-04-17',2.85,6.0,8614,'Apr 13 CERTAIN',2,
     'TOMORROW. IV=329%. Stock $28.96 vs $35C. Auto-remove Apr 14.'),
    ('RGNX','RGX-202 gene therapy','CALLS',68,'F','$10C','2026-07-17',2.65,2.2,1509,'Apr-May 2026',4,
     'BELOW 2.5x. Grade F = non-RCT (gene therapy standard). AbbVie $100M milestone.'),
    ('AXSM','AXS-05 sNDA','CALLS',65,'D','$200C','2026-05-15',8.05,15.0,573,'Apr 30 CERTAIN',19,
     'BEST MULTIPLE 15x. Already-approved drug label extension.'),
    ('ARGX','Efgartigimod $800/$850C','SPREAD',87,'D','$800/$850C','2026-05-15',20.35,1.5,6,'May 10 CERTAIN',28,
     'BELOW THRESHOLD. Spread $20.35 debit (was $12). Hold existing position through May 10 PDUFA.'),
    ('RVMD','RMC-6236 RAS inhibitor','CALLS',61,'C','$125C','2026-06-18',8.10,6.0,55,'May-Jun 2026',34,
     'Phase 3 enrollment complete. OI thin, use limits.'),
    ('AGIO','Tebapivat AG-946','PUTS',6,'F','$30P','2026-08-21',2.82,6.5,691,'May-Jun 2026',34,
     'Grade F confirms put. P(fail)=94%.'),
    ('NTLA','NTLA-2002 CRISPR HAE','CALLS',82,'C','$15C','2026-07-17',2.25,4.1,1752,'May-Jun 2026',34,
     'NEJM 3yr data published. BLA forces June disclosure.'),
    ('MLTX','Sonelokimab IZAR-1','CALLS',100,'B','$21C','2026-08-21',4.20,2.7,1041,'Jun-Jul 2026',65,
     'Warpspeed 100%. Grade B. Stock -7.5% Fri (thin vol, no news). Still qualifies.'),
    ('PRAX','Relutrigine NDA','CALLS',70,'C','$300C','2027-01-15',79.00,3.4,62,'Sep 27 CERTAIN',169,
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

# TAB 3 — Monitor
ws3 = wb.create_sheet('Monitor')
ws3.sheet_properties.tabColor = 'D29922'
ws3.merge_cells('A1:F1')
t3 = ws3['A1']
t3.value = f'MONITOR -- {today}'
cs(t3,bg=DARK,fg=ORN,bold=True,sz=12,al='center')
ws3.row_dimensions[1].height = 24
hrow(ws3,2,['TICKER','DRUG','CATALYST','P%','NOTES','GRADUATE WHEN'])
mon_rows=[
    ('VERA','Atacicept IgAN BLA','Jul 7 2026 PDUFA','72%',
     'Jan27 $55C @ $8.05 = 2.3x. Below threshold.','When multiple hits 2.5x'),
    ('VRDN','Veligrotug TED BLA','Jun 30 2026 PDUFA','72%',
     'Bid $0 on most strikes. Watch for liquidity in May.','When spread < 80%'),
    ('RZLT','Ersodetug upLIFT','H2 2026','72%',
     '16-patient trial. No bid.','If liquidity builds'),
    ('NAMS','Obicetrapib PREVAIL','Nov 2026','37%',
     'PUT candidate. Too far out. N=9,541 CV outcomes.','When within 90 days'),
    ('AMGN','Olpasiran OCEAN-a','2027','71%',
     'Mega cap, 2027 readout.','Remove if still 2027 in 60 days'),
]
for i,rd in enumerate(mon_rows,3):
    for col,val in enumerate(rd,1):
        c = ws3.cell(row=i,column=col,value=val)
        cs(c,bg=DARK,fg=(YLW if col==1 else WHT),wrap=(col in [5,6]))
    ws3.row_dimensions[i].height = 36
for i,w in enumerate([8,28,18,7,55,26],1):
    ws3.column_dimensions[get_column_letter(i)].width = w

path = BASE / 'reports/Alpha_Sniper_2026-04-12.xlsx'
wb.save(path)
print(f'Saved: {path}')

# Email body
lines = []
lines.append('ALPHA SNIPER -- April 12, 2026 (Sunday)')
lines.append('Run #13  |  10 active plays  |  8 qualifying (>=2.5x)  |  Best: AXSM 15.0x')
lines.append('=' * 65)
lines.append('')
lines.append('TOMORROW (Monday April 13): TWO BINARY EVENTS')
lines.append('')
lines.append('  IDYA  $35C May-15 @ $5.35 -- 3.7x  |  P=96%  |  IV=188%  |  OI=10,708')
lines.append('        Topline OPTIMUM-02 (DAR-UM-2 uveal melanoma) -- date confirmed via PRN')
lines.append('')
lines.append('  TVTX  $35C Apr-17 @ $2.85 -- 6.0x  |  P=77%  |  IV=329%  |  OI=8,614')
lines.append('        PDUFA (sparsentan FSGS label expansion) -- IV=329% means fully binary')
lines.append('        Auto-remove Tuesday April 14 regardless of result.')
lines.append('')
lines.append('No portfolio changes from Friday. Prices unchanged (markets closed Sunday).')
lines.append('MLTX dipped -7.5% Friday on thin volume -- no news, Warpspeed P unchanged at 100%.')
lines.append('')
lines.append('ONE-LINERS (sorted nearest catalyst, >=2.5x):')
lines.append('-' * 65)
one_liners = [
    ('2d',  'IDYA', '$35C  May-15 @ $5.35','3.7x', 'P=96%',    'OI=10,708','Apr 13 CERTAIN   | IV=188% | Grade C'),
    ('2d',  'TVTX', '$35C  Apr-17 @ $2.85','6.0x', 'P=77%',    'OI=8,614', 'Apr 13 CERTAIN   | IV=329% | Grade C | auto-remove Apr 14'),
    ('19d', 'AXSM', '$200C May-15 @ $8.05','15.0x','P=65%',    'OI=573',   'Apr 30 CERTAIN   | Grade D (PDUFA bet, not trial)'),
    ('34d', 'AGIO', '$30P  Aug-21 @ $2.82','6.5x', 'P(f)=94%', 'OI=691',   'May-Jun 2026     | Grade F confirms puts'),
    ('34d', 'RVMD', '$125C Jun-18 @ $8.10','6.0x', 'P=61%',    'OI=55',    'May-Jun 2026     | Phase 3 done'),
    ('34d', 'NTLA', '$15C  Jul-17 @ $2.25','4.1x', 'P=82%',    'OI=1,752', 'May-Jun 2026     | NEJM data pub'),
    ('169d','PRAX', '$300C Jan-27 @ $79.00','3.4x','P=70%',    'OI=62',    'Sep 27 CERTAIN   | Rare epilepsy | spread=6%'),
    ('65d', 'MLTX', '$21C  Aug-21 @ $4.20','2.7x', 'P=100%',   'OI=1,041', 'Jun-Jul 2026     | Grade B | Warpspeed 100%'),
]
for days, ticker, strike_str, mult, prob, oi_str, note in one_liners:
    lines.append(f'{days} | {ticker}  {strike_str} -- {mult} | {prob} | {oi_str} | {note}')

lines.append('')
lines.append('BELOW THRESHOLD (Excel only):')
lines.append('  RGNX $10C Jul17 @ $2.65 -- 2.2x (gene therapy standard, AbbVie milestone pending)')
lines.append('  ARGX $800/$850C May15 -- 1.5x fresh entry (hold existing position through May 10 PDUFA)')
lines.append('')
lines.append('=' * 65)
lines.append('SCIENCE GRADES')
lines.append('-' * 65)
lines.append('')
lines.append('Grade B -- MLTX: RCT triple-blind N=960, ACR50. Best designed trial.')
lines.append('')
lines.append('Grade C -- IDYA, TVTX, RVMD, NTLA, PRAX: Adequate design.')
lines.append('')
lines.append('Grade D (LONG risk note):')
lines.append('  AXSM: PDUFA bet on already-approved drug. Grade = trial design, not approval odds.')
lines.append('  ARGX: Open-label functional endpoint. Prior data strong. Hold.')
lines.append('')
lines.append('Grade F (context matters):')
lines.append('  RGNX: Non-RCT standard for gene therapy. FDA approved Elevidys/Zolgensma same design.')
lines.append('  AGIO: Grade F CONFIRMS put thesis. Non-RCT + surrogate + wrong population.')
lines.append('')
lines.append('=' * 65)
lines.append('PLAY CARDS')
lines.append('-' * 65)

cards = [
    ('IDYA','CALLS','3.7x','P=96%','Grade C','$35C May-15 @ $5.35 | OI=10,708 | IV=188%',
     'Apr 13 2026 (TOMORROW -- topline OPTIMUM-02 confirmed via PRN)',
     'First 1L therapy for HLA-A2-neg uveal melanoma. Warpspeed 96% = 90% on PFS, 46% on both PFS+OS. OI=10,708 reflects significant smart money positioning into Monday.',
     'IV=188% means the market is fully pricing a binary. Entry before Monday open is the last clean window. Pre-reg at ir.ideayabio.com likely fires before press release. $1k -> ~$3,700.'),
    ('TVTX','CALLS','6.0x','P=77%','Grade C','$35C Apr-17 @ $2.85 | OI=8,614 | IV=329%',
     'Apr 13 2026 (TOMORROW -- PDUFA CERTAIN, same day as IDYA)',
     'FILSPARI already approved for IgAN. FSGS label expansion with Phase 3 DUPLEX data. Stock at $28.96, needs to exceed $35 at Apr-17 expiry. IV=329% = fully binary outcome priced in.',
     'Double-catalyst Monday. Already-approved drug = lower regulatory bar. IV crush on result will be extreme. $1k -> ~$6,000. AUTO-REMOVE Tuesday April 14.'),
    ('AXSM','CALLS','15.0x','P=65%','Grade D','$200C May-15 @ $8.05 | OI=573 | IV=73%',
     'Apr 30 2026 (PDUFA CERTAIN)',
     'Best multiple in portfolio at 15x. AXS-05 (Auvelity) FDA-approved for MDD. Label extension to Alzheimer agitation. BTD + Priority Review. Stock at $178.11.',
     'Not a trial result bet. FDA has all data. Pre-PDUFA drift typically accelerates in final 10 days. Grade D = trial design flaw, not approval probability. $1k -> ~$15,000.'),
    ('AGIO','PUTS','6.5x','P(fail)=94%','Grade F','$30P Aug-21 @ $2.82 | OI=691 | IV=55%',
     'May-Jun 2026',
     'Phase 2b tebapivat uses wrong endpoint (8-week TI) for hardest-to-treat MDS patients. Phase 2a worked only in easiest subset. A competing PKR activator was already terminated for futility in MDS.',
     'Science Grade F CONFIRMS put thesis. Non-RCT + surrogate endpoint + wrong patient population. Warpspeed P(success)=6%. $1k -> ~$6,500.'),
    ('RVMD','CALLS','6.0x','P=61%','Grade C','$125C Jun-18 @ $8.10 | OI=55 | IV=101%',
     'May-Jun 2026',
     'RMC-6236 (daraxonrasib) first multi-RAS(ON) inhibitor. Phase 3 RASolute-302 enrollment complete. Warpspeed: 61% on both PFS and OS at first read. Simulated median PFS 5.3 vs 3.4 months.',
     'KRAS undruggable for decades. Only multi-RAS selectivity. Phase 1/2: 35% response rate, 8.5mo PFS in 26 pts = double chemotherapy. OI=55, use limit orders only. $1k -> ~$6,000.'),
    ('NTLA','CALLS','4.1x','P=82%','Grade C','$15C Jul-17 @ $2.25 | OI=1,752 | IV=102%',
     'May-Jun 2026',
     'NTLA-2002 CRISPR HAE prophylaxis. HAELO enrollment complete September 2025. NEJM April 2 published 3yr data: 96% attack reduction, 31/32 attack-free. BLA submission H2 2026.',
     'Data already published -- market has not fully re-rated. BLA filing timeline forces final topline disclosure before June. The window is closing. $1k -> ~$4,100.'),
    ('PRAX','CALLS','3.4x','P=70%','Grade C','$300C Jan-27 @ $79.00 | OI=62 | spread=6% | IV=67%',
     'Sep 27 2026 (PDUFA CERTAIN)',
     'Relutrigine (PRAX-562) first Nav1.2/Nav1.6 channel blocker for SCN2A/SCN8A DEE. ~5,000 US patients. No targeted treatment exists. FDA accepted NDA. EMBOLD Phase 2/3 data.',
     'Confirmed PDUFA Sep 27. Jan27 expiry = 4-month buffer, IV builds into event. Spread=6% unusually tight for this price. Rare disease premium on approval. $1k -> ~$3,400.'),
    ('MLTX','CALLS','2.7x','P=100%','Grade B','$21C Aug-21 @ $4.20 | OI=1,041 | IV=123%',
     'Jun-Jul 2026',
     'Sonelokimab IZAR-1 Phase 3 PsA readout. Warpspeed P=100%. Triple-blind placebo-controlled RCT, N=960. ACR50 endpoint is FDA gold standard for PsA. Grade B = best designed trial in portfolio.',
     'Warpspeed 100% is extremely rare. Small cap ($500M) = large institutional re-rating on approval. Stock -7.5% Friday on thin volume, no news, Warpspeed unchanged. $1k -> ~$2,700.'),
]
for ticker, direc, mult, prob, grade, option_str, cat, why, edge in cards:
    lines.append(f"\n{'='*55}")
    lines.append(f'  {ticker} -- {direc} -- {mult} -- {prob} -- {grade}')
    lines.append(f"{'='*55}")
    lines.append(f'  OPTION:   {option_str}')
    lines.append(f'  CATALYST: {cat}')
    lines.append(f'  WHY:      {why}')
    lines.append(f'  EDGE:     {edge}')

lines.append('')
lines.append('=' * 65)
lines.append('MONITOR')
lines.append('-' * 65)
lines.append('  VERA  atacicept -- Jul 7 PDUFA  | 2.3x (promote when 2.5x)')
lines.append('  VRDN  veligrotug -- Jun 30 PDUFA | Spreads $0 bid, watch May')
lines.append('  NAMS  obicetrapib -- Nov 2026     | PUT candidate P=37%, too far')
lines.append('')
lines.append('MONDAY APRIL 13: IDYA + TVTX both announce.')
lines.append('TUESDAY APRIL 14: Auto-remove TVTX from active plays.')
lines.append('Intraday alerts active -- email fires immediately on any breaking signal.')

body = '\n'.join(lines)
with open(BASE/'reports/email_body_2026-04-12.txt','w') as f:
    f.write(body)
print(f'Email body: {len(body)} chars')
print('Done.')
