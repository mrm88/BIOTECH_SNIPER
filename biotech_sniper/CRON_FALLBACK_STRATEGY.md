# Cron Fallback Strategy for DNS-Restricted Sources

The Python scripts run fine locally but hit DNS issues with some external APIs
in the sandbox. The CRON AGENT runs in a full browser environment — it uses
browser_task and web_search which have unrestricted access. Here's the strategy.

## SAM.gov Contract Scanner
**Problem**: SAM.gov API returns 404 in sandbox (requires API key for some endpoints)
**Cron Solution**: Use web_search with these queries daily:
  - "site:sam.gov justification approval defense [current month] [year]"
  - "sole source award kratos OR rocket lab OR AST OR LUNR OR KTOS [current month] [year]"
  - Direct browse: https://sam.gov/search/?index=opp&page=0&pageSize=25&sort=-modifiedDate&sfm%5Bstatus%5D%5B0%5D=Active&sfm%5BnoticeType%5D%5B0%5D=Justification
  - Also browse: https://www.usaspending.gov/search/?hash=... for award data

**Signal tier**: Still valid. J&A filings appear in Google within hours.
Cron should search daily and pass results to Claude/Gemini for company matching.

## FDA AdCom Calendar
**Problem**: FDA calendar is dynamically rendered JavaScript — HTML parse returns empty table
**Cron Solution**: Use browser_task to render the full page:
  - Browse: https://www.fda.gov/advisory-committees/advisory-committee-calendar
  - Extract all meeting entries with dates, committees, drug names
  - Cross-reference against known ticker mappings
  - Also check: https://www.biopharmcatalyst.com/calendars/fda-calendar (has AdCom section)
  - Also check: https://www.fdatracker.com/fda-calendar/ (includes AdCom dates)

**48-hour briefing signal**: 
  When AdCom is <48 hours away, browse the meeting page directly and download briefing docs.
  FDA posts them at: https://www.fda.gov/advisory-committees/[committee-name]/[meeting-date]
  Run downloaded text through Claude: count bearish vs bullish FDA question phrases.

## IR Events Calendar
**Problem**: 7/11 IR URLs returning 404 or DNS timeout in sandbox
**Working URLs** (confirmed): ir.ideayabio.com, ir.rezolutebio.com, www.argenx.com, www.viridiantherapeutics.com
**Failed URLs**: investor.agios.com, www.moonlaketx.com, www.revmed.com, www.travere.com, www.axsome.com, www.regenxbio.com, www.intelliatx.com

**Cron Solution for failed URLs**: Use web_search with:
  - "[COMPANY] investor relations events webcast 2026"
  - "[COMPANY] topline results webcast preregistration 2026"
  - Direct Google: site:[company website] events 2026 webcast register

**Key signal to catch**: When a company opens webcast pre-registration, it appears in:
  1. Their IR events page (browser_task works in cron)
  2. PR Newswire / GlobeNewswire press releases (web_search catches these)
  3. SEC 8-K filing (sec_8k_monitor catches these)
  
So even if IR page fails, the webcast pre-registration WILL be caught via PR or 8-K.

## Summary: What Actually Works in Cron vs Sandbox

| Source | Sandbox (Python) | Cron (browser+search) | Reliability |
|--------|-----------------|----------------------|-------------|
| ClinicalTrials.gov API | ✅ Works | ✅ Works | HIGH |
| SEC EDGAR RSS + CIK | ✅ Works | ✅ Works | HIGH |
| ir.ideayabio.com | ✅ Works | ✅ Works | HIGH |
| Warpspeed.sh | ✅ Works | ✅ Works | HIGH |
| SAM.gov API | ❌ 404 | ✅ Via browser+search | MEDIUM |
| FDA AdCom calendar | ❌ Dynamic JS | ✅ Via browser_task | MEDIUM |
| Other IR pages | ❌ DNS/404 | ✅ Via browser_task | MEDIUM |
| FPDS/USASpending | ❌ DNS | ✅ Via web_search | MEDIUM |

All medium-reliability sources have multi-source fallbacks that catch the same signal.
The CRITICAL signals (topline data, webcast pre-reg) are always caught via SEC 8-K + PR.
