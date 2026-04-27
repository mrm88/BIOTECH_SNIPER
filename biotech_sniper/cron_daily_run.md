# BIOTECH CATALYST SNIPER — DAILY CRON TASK INSTRUCTIONS

## Schedule: Every day at 6:00 AM PT (14:00 UTC)

## What to do each run:

1. **Warpspeed Check**: Browse https://warpspeed.sh/ and check for any new experiments published since yesterday. Compare against the existing 10: NAMS, IDYA, TECX, AGIO, RZLT, NVS, CELC, MLTX, RVMD, AMGN. Note any new additions or resolved experiments.

2. **Timeline Updates**: Search web for each of the 10 tickers + "catalyst 2026 readout" to detect any new PRs, SEC filings, or clinical trial registry updates with concrete dates.

3. **IDYA DB Lock Watch**: This is PRIORITY — search "IDYA darovasertib OptimUM-02 topline results" daily. If DB lock occurred or topline data is announced, flag in report header with 🚨 ALERT.

4. **New Catalyst Scan**: Search "biotech Phase 3 topline data [CURRENT MONTH] [NEXT MONTH] 2026 binary catalyst oncology" — report any new Phase 2/3 readouts within 30-60 day window.

5. **Probability Scoring**: For any catalyst with P≥85% or P≤15% AND 30-60 day window, run Aletheia-style ensemble scoring.

6. **Generate and deliver report**: Structure exactly as the daily report format. Include copy-paste orders for any extreme-probability plays.

## Alert triggers (send notification immediately regardless of schedule):
- IDYA announces topline results (any time)
- AGIO announces topline results (any time)
- Any new Warpspeed experiment with P≥90% or P≤10% added
- Any of the 10 tickers announces surprise delay or acceleration

## History tracking:
- Save each report to /home/user/workspace/biotech_sniper/reports/report_YYYY-MM-DD.txt
- Update catalyst_history.json with any new date information
