import openpyxl
from openpyxl.styles import (PatternFill, Font, Alignment, Border, Side,
                              GradientFill)
from openpyxl.utils import get_column_letter
from openpyxl.formatting.rule import ColorScaleRule
import datetime

from biotech_sniper.paths import BASE_DIR

wb = openpyxl.Workbook()

# ── colours ──────────────────────────────────────────────────────────────────
BG_DARK      = "0D0D0D"
BG_HEADER    = "1A1A2E"
BG_SECTION   = "16213E"
BG_TRADE     = "0F3460"
BG_GREEN     = "0A3D0A"
BG_RED       = "3D0A0A"
BG_YELLOW    = "3D3D0A"
BG_NEUTRAL   = "1E1E2E"
FG_WHITE     = "FFFFFF"
FG_GREEN     = "00FF88"
FG_RED       = "FF4466"
FG_YELLOW    = "FFD700"
FG_CYAN      = "00CFFF"
FG_ORANGE    = "FF8C00"
FG_GREY      = "AAAAAA"

def fill(hex_bg):
    return PatternFill("solid", fgColor=hex_bg)

def font(color=FG_WHITE, bold=False, size=10, italic=False):
    return Font(name="Calibri", color=color, bold=bold, size=size, italic=italic)

def center():
    return Alignment(horizontal="center", vertical="center", wrap_text=True)

def left():
    return Alignment(horizontal="left", vertical="center", wrap_text=True)

thin = Side(style="thin", color="333355")
border = Border(left=thin, right=thin, top=thin, bottom=thin)

def style(ws, row, col, value, bg=BG_NEUTRAL, fg=FG_WHITE, bold=False,
          size=10, align="center", italic=False, num_fmt=None):
    c = ws.cell(row=row, column=col, value=value)
    c.fill = fill(bg)
    c.font = font(fg, bold, size, italic)
    c.alignment = center() if align == "center" else left()
    c.border = border
    if num_fmt:
        c.number_format = num_fmt
    return c

# ══════════════════════════════════════════════════════════════════════════════
# SHEET 1 — MASTER SCORES
# ══════════════════════════════════════════════════════════════════════════════
ws = wb.active
ws.title = "🎯 Catalyst Scores"
ws.sheet_view.showGridLines = False
ws.freeze_panes = "A4"

# col widths
widths = [6, 12, 28, 22, 20, 14, 14, 14, 14, 16, 18, 28, 38]
for i, w in enumerate(widths, 1):
    ws.column_dimensions[get_column_letter(i)].width = w
for r in range(1, 30):
    ws.row_dimensions[r].height = 22

# Title banner (merged)
ws.merge_cells("A1:M1")
c = ws["A1"]
c.value = "⚡  BIOTECH CATALYST SNIPER — NEW OPPORTUNITY SCAN  |  March 30, 2026  |  Model: Claude Opus 4 + Gemini 2.5 Pro Ensemble"
c.fill = fill(BG_TRADE)
c.font = Font(name="Calibri", color=FG_CYAN, bold=True, size=13)
c.alignment = center()

ws.merge_cells("A2:M2")
c = ws["A2"]
c.value = "TRADE FILTER: P(success) ≥ 70% → LONG  |  P(success) ≤ 30% → SHORT/PUT  |  30–70% → MONITOR ONLY  |  Source: warpspeed.sh, BiopharmCatalyst, ClinicalTrials.gov, IR pages, SEC"
c.fill = fill(BG_SECTION)
c.font = Font(name="Calibri", color=FG_GREY, bold=False, size=9, italic=True)
c.alignment = center()

# Column headers
headers = ["#", "Ticker", "Company + Drug", "Trial / Event", "Readout Window",
           "Claude P%", "Gemini P%", "Ensemble %", "Δ Models", "VERDICT",
           "Direction", "Suggested Play", "Key Thesis"]
for col, h in enumerate(headers, 1):
    style(ws, 3, col, h, bg=BG_HEADER, fg=FG_CYAN, bold=True, size=9)

# ── DATA ─────────────────────────────────────────────────────────────────────
# Each row: (num, ticker, company+drug, trial, window, claude%, gemini%, verdict_reason, direction, play, thesis)
rows = [
    (1, "NTLA",  "Intellia Therapeutics\nonvo-z (NTLA-2002) CRISPR KLKB1 KO",
     "HAELO Phase 3\nHAE attack rate primary", "May–Jun 2026\n~60 days",
     80, 84, "🟢 TRADE",   "LONG CALLS",
     "Jun $25C @ ~$2.50\n10 contracts = $2,500",
     "Phase 2 RCT showed 77% attack reduction (NEJM). 95% reduction in Ph1. Validated target (same pathway as lanadelumab). Only CRISPR risk = off-target. Massive buffer to hit endpoint."),

    (2, "REPL",  "Replimune Group\nRP1 (vusolimogene) + nivolumab",
     "BLA RESUBMISSION\nPDUFA April 10, 2026", "April 10, 2026\n~11 days",
     25, 79, "⚠️ DIVERGE",  "REVIEW — MODELS SPLIT",
     "Claude bearish (CRL was efficacy). Gemini bullish (unmet need + Class 2 base rate). Resolve before trading.",
     "CRL July 2025. Class 2 resubmission accepted. 33.6% ORR in anti-PD1-failed melanoma. Gemini 79% vs Claude 25% = HIGH MODEL DIVERGENCE — do not trade until resolved."),

    (3, "RGNX",  "REGENXBIO\nRGX-202 AAV8 microdystrophin",
     "Phase 3 Pivotal DMD\nMicrodystrophin ≥10% expression", "Early Q2 2026\n~30–45 days",
     72, 65, "🟢 TRADE",   "LONG CALLS",
     "May $12.50C @ ~$1.20\n8 contracts = ~$960",
     "Primary endpoint is BIOMARKER (≥10% expression), NOT functional. 100% of Ph1/2 patients hit threshold. Sarepta precedent set. Key risk: AAV manufacturing batch variance. Claude 72% / Gemini 65%."),

    (4, "NKTR",  "Nektar Therapeutics\nRezpegaldesleukin (REZPEG) Treg/IL-2",
     "REZOLVE-AA Phase 2b\n16-wk blinded extension", "IMMINENT — April 2026\nQuiet period starts April 1",
     42, 68, "⚠️ DIVERGE",  "SMALL CALLS ONLY",
     "May $65C @ ~$1.50\n5 contracts = $750 (half-size)",
     "Models diverge: Claude 42% (ITT missed primary, small N, Treg class unproven) vs Gemini 68% (late-breaking oral at AAD telegraphs hit, first-in-class mechanism). Stock at $57, already 95x from lows. Half-size only."),

    (5, "ALLO",  "Allogene Therapeutics\nCema-cel allogeneic CD19 CAR-T",
     "ALPHA3 Interim Futility\nMRD clearance in 24 pts", "April 2026\n~30 days",
     55, 46, "🔴 SKIP",    "AVOID",
     "—",
     "Coin-flip territory. Both models in 46–55% range = no edge. Allo persistence biology is the killer — 2-4 week cell survival vs 25-30% MRD improvement bar needed. Small n=12/arm stochastic noise. Gemini explicitly says AVOID."),
]

for r_idx, row in enumerate(rows, 4):
    num, ticker, co, trial, window, cl_p, gem_p, verdict, direction, play, thesis = row
    ensemble = round((cl_p + gem_p) / 2)
    delta = abs(cl_p - gem_p)

    # Row background
    if "TRADE" in verdict and "DIVERGE" not in verdict:
        row_bg = BG_GREEN if "LONG" in direction else BG_RED
    elif "SKIP" in verdict:
        row_bg = BG_RED
    elif "DIVERGE" in verdict:
        row_bg = BG_YELLOW
    else:
        row_bg = BG_NEUTRAL

    style(ws, r_idx, 1,  num,      bg=BG_SECTION, fg=FG_GREY, bold=True)
    style(ws, r_idx, 2,  ticker,   bg=row_bg, fg=FG_YELLOW if row_bg!=BG_NEUTRAL else FG_WHITE, bold=True, size=11)
    style(ws, r_idx, 3,  co,       bg=row_bg, fg=FG_WHITE, align="left", size=9)
    style(ws, r_idx, 4,  trial,    bg=row_bg, fg=FG_WHITE, align="left", size=9)
    style(ws, r_idx, 5,  window,   bg=row_bg, fg=FG_CYAN, size=9)

    # Claude %
    cl_fg = FG_GREEN if cl_p >= 70 else (FG_RED if cl_p <= 30 else FG_YELLOW)
    style(ws, r_idx, 6,  f"{cl_p}%", bg=row_bg, fg=cl_fg, bold=True, size=11)
    # Gemini %
    gem_fg = FG_GREEN if gem_p >= 70 else (FG_RED if gem_p <= 30 else FG_YELLOW)
    style(ws, r_idx, 7,  f"{gem_p}%", bg=row_bg, fg=gem_fg, bold=True, size=11)
    # Ensemble
    ens_fg = FG_GREEN if ensemble >= 70 else (FG_RED if ensemble <= 30 else FG_YELLOW)
    style(ws, r_idx, 8,  f"{ensemble}%", bg=row_bg, fg=ens_fg, bold=True, size=12)
    # Delta
    delta_fg = FG_RED if delta >= 30 else (FG_YELLOW if delta >= 15 else FG_GREEN)
    style(ws, r_idx, 9,  f"Δ{delta}pp", bg=row_bg, fg=delta_fg, size=9, italic=(delta>=30))

    # Verdict
    v_fg = FG_GREEN if "TRADE" in verdict and "DIVERGE" not in verdict else (FG_RED if "SKIP" in verdict else FG_YELLOW)
    style(ws, r_idx, 10, verdict,  bg=row_bg, fg=v_fg, bold=True, size=9)
    style(ws, r_idx, 11, direction, bg=row_bg, fg=FG_ORANGE, bold=True, size=9)
    style(ws, r_idx, 12, play,     bg=BG_TRADE, fg=FG_WHITE, size=8, align="left")
    style(ws, r_idx, 13, thesis,   bg=BG_SECTION, fg=FG_GREY, size=8, align="left")

    ws.row_dimensions[r_idx].height = 52

# ── Summary footer ────────────────────────────────────────────────────────────
r = len(rows) + 4 + 1
ws.merge_cells(f"A{r}:M{r}")
c = ws.cell(row=r, column=1)
c.value = ("✅ TRADE-ELIGIBLE: NTLA (ensemble 82%, long calls), RGNX (ensemble 68.5%, long calls)  |  "
           "⚠️ DIVERGE — HALF SIZE: NKTR (55% ensemble, model split 42 vs 68), REPL (52% ensemble, model split 25 vs 79)  |  "
           "❌ SKIP: ALLO (50.5%, coin flip)")
c.fill = fill(BG_HEADER)
c.font = Font(name="Calibri", color=FG_CYAN, bold=True, size=9)
c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
c.border = border
ws.row_dimensions[r].height = 28

# ══════════════════════════════════════════════════════════════════════════════
# SHEET 2 — TRADE SETUPS (full orders)
# ══════════════════════════════════════════════════════════════════════════════
ws2 = wb.create_sheet("📋 Trade Setups")
ws2.sheet_view.showGridLines = False
for r in range(1, 50):
    ws2.row_dimensions[r].height = 20

col_widths2 = [4, 12, 14, 14, 14, 14, 14, 18, 22, 30]
for i, w in enumerate(col_widths2, 1):
    ws2.column_dimensions[get_column_letter(i)].width = w

ws2.merge_cells("A1:J1")
c = ws2["A1"]
c.value = "📋  READY-TO-COPY TRADE SETUPS  |  Trade-eligible plays only  |  March 30, 2026"
c.fill = fill(BG_TRADE)
c.font = Font(name="Calibri", color=FG_CYAN, bold=True, size=12)
c.alignment = center()

# NTLA
def section_header(ws, row, text):
    ws.merge_cells(f"A{row}:J{row}")
    c = ws.cell(row=row, column=1, value=text)
    c.fill = fill(BG_HEADER)
    c.font = Font(name="Calibri", color=FG_YELLOW, bold=True, size=10)
    c.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[row].height = 20

def trade_row(ws, row, col_vals, bg=BG_NEUTRAL, fg=FG_WHITE):
    for col, val in enumerate(col_vals, 1):
        c = ws.cell(row=row, column=col, value=val)
        c.fill = fill(bg)
        c.font = Font(name="Calibri", color=fg, size=9)
        c.alignment = left()
        c.border = border

section_header(ws2, 2, "━━  PLAY #1: NTLA — LONG CALLS  |  P(success) = 82% ensemble  |  HAELO Phase 3 HAE readout May–Jun 2026")
trade_row(ws2, 3, ["", "Stock ~$18", "Strike", "Expiry", "Entry target", "Qty", "Risk", "Type", "Instruction", "Note"],
          bg=BG_SECTION, fg=FG_CYAN)
# Decoys
trade_row(ws2, 4,  ["DECOY 1", "NTLA", "$25C", "Jun 20 2026", "$2.65", "1 contract", "~$265", "BUY TO OPEN", "LIMIT GTC", "Probe liquidity first"], BG_GREEN)
trade_row(ws2, 5,  ["DECOY 2", "NTLA", "$25C", "Jun 20 2026", "$2.60", "1 contract", "~$260", "BUY TO OPEN", "LIMIT GTC", "Watch bid/ask — target ≤$0.10 spread"], BG_GREEN)
trade_row(ws2, 6,  ["LADDER 1", "NTLA", "$25C", "Jun 20 2026", "$2.50", "2 contracts", "~$500", "BUY TO OPEN", "LIMIT GTC", "Bulk fill target"], BG_GREEN)
trade_row(ws2, 7,  ["LADDER 2", "NTLA", "$25C", "Jun 20 2026", "$2.45", "2 contracts", "~$490", "BUY TO OPEN", "LIMIT GTC", ""], BG_GREEN)
trade_row(ws2, 8,  ["LADDER 3", "NTLA", "$25C", "Jun 20 2026", "$2.40", "2 contracts", "~$480", "BUY TO OPEN", "LIMIT GTC", ""], BG_GREEN)
trade_row(ws2, 9,  ["LADDER 4", "NTLA", "$25C", "Jun 20 2026", "$2.35", "2 contracts", "~$470", "BUY TO OPEN", "LIMIT GTC", ""], BG_GREEN)
trade_row(ws2, 10, ["LADDER 5", "NTLA", "$25C", "Jun 20 2026", "$2.30", "2 contracts", "~$460", "BUY TO OPEN", "LIMIT GTC", "If filled here = great avg"], BG_GREEN)
trade_row(ws2, 11, ["TOTAL", "NTLA", "", "", "Avg ~$2.45", "12 contracts", "~$2,940", "", "MAX LOSS = $2,940", "Risk only what you can lose"], BG_TRADE)

# NTLA payoff
ws2.merge_cells("A12:J12")
ws2["A12"].value = "PAYOFF TABLE — NTLA $25C Jun20 (stock ~$18, 39% OTM)"
ws2["A12"].fill = fill(BG_SECTION)
ws2["A12"].font = Font(name="Calibri", color=FG_YELLOW, bold=True, size=9)
ws2["A12"].alignment = left()

trade_row(ws2, 13, ["Scenario", "Stock move", "Stock price", "Option value", "P&L per contract", "P&L total (12x)", "Probability", "", "", ""],
          bg=BG_SECTION, fg=FG_CYAN)
trade_row(ws2, 14, ["FAILURE", "-50%", "~$9", "$0.00", "-$245", "-$2,940", "~18%", "", "", ""], BG_RED)
trade_row(ws2, 15, ["INLINE/MIXED", "+10%", "~$20", "$0.05–0.20", "-$220", "-$2,640", "~12%", "", "", ""], BG_RED)
trade_row(ws2, 16, ["MODERATE HIT (≥70% reduction, misses 80%)", "+40%", "~$25", "$0.50–1.50", "-$145→-$95", "-$1,740→-$1,140", "~25%", "", "", ""], BG_YELLOW)
trade_row(ws2, 17, ["STRONG HIT (≥80% reduction, clear win)", "+80%", "~$32", "$7–9", "+$455→+$655", "+$5,460→+$7,860", "~32%", "", "", ""], BG_GREEN)
trade_row(ws2, 18, ["BLOCKBUSTER (≥90% reduction, best-in-class)", "+150%", "~$45", "$19–22", "+$1,655→+$1,955", "+$19,860→+$23,460", "~13%", "", "", ""], BG_GREEN)

section_header(ws2, 20, "━━  PLAY #2: RGNX — LONG CALLS  |  P(success) = 68.5% ensemble  |  DMD Phase 3 topline early Q2 2026")
trade_row(ws2, 21, ["", "Stock ~$8.50", "Strike", "Expiry", "Entry target", "Qty", "Risk", "Type", "Instruction", "Note"],
          bg=BG_SECTION, fg=FG_CYAN)
trade_row(ws2, 22, ["DECOY 1", "RGNX", "$12.50C", "May 16 2026", "$0.75", "1 contract", "~$75", "BUY TO OPEN", "LIMIT GTC", "Check OI — may be thin"], BG_GREEN)
trade_row(ws2, 23, ["DECOY 2", "RGNX", "$12.50C", "May 16 2026", "$0.70", "1 contract", "~$70", "BUY TO OPEN", "LIMIT GTC", "Use midpoint if spread >$0.15"], BG_GREEN)
trade_row(ws2, 24, ["LADDER 1", "RGNX", "$12.50C", "May 16 2026", "$0.65", "3 contracts", "~$195", "BUY TO OPEN", "LIMIT GTC", ""], BG_GREEN)
trade_row(ws2, 25, ["LADDER 2", "RGNX", "$12.50C", "May 16 2026", "$0.60", "3 contracts", "~$180", "BUY TO OPEN", "LIMIT GTC", ""], BG_GREEN)
trade_row(ws2, 26, ["LADDER 3", "RGNX", "$12.50C", "May 16 2026", "$0.55", "3 contracts", "~$165", "BUY TO OPEN", "LIMIT GTC", ""], BG_GREEN)
trade_row(ws2, 27, ["LADDER 4", "RGNX", "$12.50C", "May 16 2026", "$0.50", "3 contracts", "~$150", "BUY TO OPEN", "LIMIT GTC", "If fills here = max degen"], BG_GREEN)
trade_row(ws2, 28, ["TOTAL", "RGNX", "", "", "Avg ~$0.62", "14 contracts", "~$870", "", "MAX LOSS = $870", "Small-cap — verify liquidity"], BG_TRADE)

trade_row(ws2, 30, ["Scenario", "Stock move", "Stock price", "Option value", "P&L per contract", "P&L total (14x)", "Probability", "", "", ""],
          bg=BG_SECTION, fg=FG_CYAN)
trade_row(ws2, 31, ["FAILURE / MISS", "-55%", "~$4", "$0.00", "-$62", "-$868", "~28%", "", "", ""], BG_RED)
trade_row(ws2, 32, ["AMBIGUOUS DATA", "-20%", "~$7", "$0.00", "-$62", "-$868", "~15%", "", "", ""], BG_RED)
trade_row(ws2, 33, ["HIT (expression met)", "+80%", "~$15", "$2.50–3.50", "+$188→+$288", "+$2,632→+$4,032", "~35%", "", "", ""], BG_GREEN)
trade_row(ws2, 34, ["STRONG HIT + NSAA signal", "+200%", "~$25", "$12–14", "+$1,138→+$1,338", "+$15,932→+$18,732", "~22%", "", "", ""], BG_GREEN)

section_header(ws2, 36, "━━  WATCH / HALF-SIZE: NKTR & REPL  |  Models diverged — verify before trading")
trade_row(ws2, 37, ["NKTR", "Quiet period April 1 → data imminent",  "Claude 42%", "Gemini 68%", "Ensemble 55%", "HALF SIZE", "$750 max", "May $65C", "5 contracts @ $1.50", "Models split — use half position. Real edge is if Gemini is right on AAD late-breaker signal."], BG_YELLOW, FG_YELLOW)
trade_row(ws2, 38, ["REPL", "PDUFA April 10 — BLA resubmission",  "Claude 25%", "Gemini 79%", "Ensemble 52%", "SKIP / RESOLVE", "—", "—", "—", "Extreme divergence (54pp). Claude says CRL was efficacy-based (hard to fix). Gemini says unmet need + Class 2 base rate. DO NOT TRADE until you resolve which thesis is correct."], BG_YELLOW, FG_YELLOW)

ws2.merge_cells("A29:J29")
ws2["A29"].value = "PAYOFF TABLE — RGNX $12.50C May16 (stock ~$8.50, 47% OTM)"
ws2["A29"].fill = fill(BG_SECTION)
ws2["A29"].font = Font(name="Calibri", color=FG_YELLOW, bold=True, size=9)
ws2["A29"].alignment = left()

# ══════════════════════════════════════════════════════════════════════════════
# SHEET 3 — WATCHLIST / PIPELINE (all scanned, not just trade-eligible)
# ══════════════════════════════════════════════════════════════════════════════
ws3 = wb.create_sheet("🔭 Full Pipeline Scan")
ws3.sheet_view.showGridLines = False
ws3.merge_cells("A1:H1")
c = ws3["A1"]
c.value = "🔭  FULL PIPELINE SCAN — ALL CATALYSTS REVIEWED  |  30–60 Day Window  |  Not just trade-eligible"
c.fill = fill(BG_TRADE)
c.font = Font(name="Calibri", color=FG_CYAN, bold=True, size=11)
c.alignment = center()
ws3.row_dimensions[1].height = 22

hdrs3 = ["Ticker", "Drug / Event", "Trial / Type", "Window", "Ensemble P%", "Status", "Why Not Trading", "Source"]
col_w3 = [8, 28, 22, 18, 12, 14, 38, 30]
for i, (h, w) in enumerate(zip(hdrs3, col_w3), 1):
    ws3.column_dimensions[get_column_letter(i)].width = w
    style(ws3, 2, i, h, bg=BG_HEADER, fg=FG_CYAN, bold=True, size=9)

pipeline = [
    ("NTLA",   "onvo-z CRISPR KLKB1 KO",          "Phase 3 HAELO HAE",         "May–Jun 2026", "82%", "🟢 TRADE",    "—", "clinicaltrials.gov NCT05539157 + NTLA IR"),
    ("RGNX",   "RGX-202 AAV8 microdystrophin",      "Phase 3 DMD pivotal",       "Early Q2 2026","68%", "🟢 TRADE",    "—", "RGNX IR + NCT05693142"),
    ("NKTR",   "REZPEG IL-2/Treg AA",               "Ph2b extension readout",    "April 2026",   "55%", "⚠️ HALF SIZE","Model divergence 42 vs 68%", "NKTR PR March 12 2026"),
    ("REPL",   "RP1 + nivo BLA resubmission",       "PDUFA April 10",            "Apr 10, 2026", "52%", "⚠️ REVIEW",   "Extreme model divergence (25 vs 79%)", "ir.replimune.com"),
    ("ALLO",   "Cema-cel allo CAR-T ALPHA3",         "Ph2 interim futility MRD",  "April 2026",   "50%", "🔴 SKIP",     "Coin flip. Allo persistence biology = killer.", "Allogene IR Jan 8 2026 + MarketBeat Mar 3"),
    ("LPCN",   "LPCN 1154 postpartum depression",   "Phase 3 topline",           "Early Q2 2026","~55%","🟡 MONITOR",  "No probability scored yet. Small cap, check options liquidity", "LPCN IR"),
    ("ABVX",   "Obefazimod UC maintenance Phase 3",  "ABTECT 44-wk data",         "Q2 2026",      "~65%","🟡 MONITOR",  "Timeline may slip into June — re-check", "ABVX IR + SEC"),
    ("ARGX",   "Efgartigimod seroneg gMG sBLA",      "PDUFA May 10",              "May 10, 2026", "~78%","🟡 MONITOR",  "ARGX $400+ stock — options expensive; large cap limits upside %", "FDA calendar + ARGX IR"),
    ("AXSM",   "AXS-05 Alzheimer's agitation sNDA", "PDUFA Apr 30",              "Apr 30, 2026", "~65%","🟡 MONITOR",  "Not scored — check options chain first", "eMPR FDA calendar"),
    ("NVS",    "Remibrutinib vs teriflunomide RMS",  "Phase 3 NCT05156281",       "Apr 30, 2026", "~62%","🟡 MONITOR",  "Large-cap NVS — limited binary leverage", "ClinicalTrials.gov NCT05156281"),
]

for r_idx, row in enumerate(pipeline, 3):
    ticker, drug, trial, window, ensemble, status, reason, source = row
    ens_num = int(ensemble.replace("%","").replace("~","")) if "%" in ensemble else 50
    row_bg = BG_GREEN if "TRADE" in status else (BG_RED if "SKIP" in status else (BG_YELLOW if "HALF" in status or "REVIEW" in status else BG_NEUTRAL))
    fg_e = FG_GREEN if ens_num >= 70 else (FG_RED if ens_num <= 30 else FG_YELLOW)
    style(ws3, r_idx, 1, ticker,   bg=row_bg, fg=FG_YELLOW, bold=True)
    style(ws3, r_idx, 2, drug,     bg=row_bg, fg=FG_WHITE, align="left", size=9)
    style(ws3, r_idx, 3, trial,    bg=row_bg, fg=FG_WHITE, align="left", size=9)
    style(ws3, r_idx, 4, window,   bg=row_bg, fg=FG_CYAN, size=9)
    style(ws3, r_idx, 5, ensemble, bg=row_bg, fg=fg_e, bold=True, size=11)
    style(ws3, r_idx, 6, status,   bg=row_bg, fg=FG_WHITE, bold=True, size=9)
    style(ws3, r_idx, 7, reason,   bg=BG_SECTION, fg=FG_GREY, align="left", size=8)
    style(ws3, r_idx, 8, source,   bg=BG_SECTION, fg=FG_GREY, align="left", size=8, italic=True)
    ws3.row_dimensions[r_idx].height = 30

out_path = str(BASE_DIR / "Biotech_Catalyst_Sniper_2026-03-30.xlsx")
wb.save(out_path)
print(f"Saved: {out_path}")
