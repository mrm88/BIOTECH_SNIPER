# BIOTECH_SNIPER

Biotech catalyst-driven options paper-trading system. The pipeline ingests
ClinicalTrials.gov, SEC EDGAR, news RSS, FDA AdCom calendars and USASpending
contract data, scores ~370 biotech tickers through a two-tier LLM ensemble
(Grok-4 fast tier + Claude / Gemini deep science tier), generates daily play
cards, and submits paper-only single-leg long call/put orders to the Alpaca
paper endpoint with hard guardrails against real-money trading.

This repository is the canonical source of truth. The VPS at
`root@199.247.25.111` clones it into `/root/alpha_sniper/repo/` and runs
oneshot systemd timers for the daily, intraday, and watchdog cycles.

## Layout

- `biotech_sniper/` — main Python package (~22k LOC, refactored from the
  original unzipped reference project).
- `biotech_sniper/paths.py` — single source of truth for filesystem paths.
- `biotech_sniper/config.py` — secrets, feature flags, risk defaults.
- `biotech_sniper/audit.py` — end-to-end source reachability probe and
  daily health-JSON writer.
- `tests/` — pytest suites (with VCR cassettes for network-dependent
  fixtures, added in M2).
- `migrations/seed/` — historical state JSONs preserved as seed data for
  the M2 SQLite backfill (6 resolved trades, 261 NCT IDs, scoring cache).
- `requirements.txt` — fully pinned dependency set.
- `.env.example` — required env-var key names (no secret values).

## Environment

All required environment variables are listed in `.env.example`. The
runtime reads them from a `.env` file at the repo root (or, on the VPS,
at `/root/alpha_sniper/.env` — one level above the checkout, mode 600,
root-owned, never committed).

| Variable | Purpose |
| --- | --- |
| `BIOTECH_SNIPER_HOME` | Absolute path to the repo checkout (resolved by `paths.py`). |
| `XAI_API_KEY` | Grok-4 fast-tier scorer (M2). |
| `ANTHROPIC_API_KEY` | Claude deep-tier science reasoner (M2). |
| `GEMINI_API_KEY` | Gemini 2.5 Pro deep-tier reasoner (M2 ensemble). |
| `ALPACA_KEY_ID` / `ALPACA_SECRET_KEY` | Alpaca paper credentials (M3). |
| `ALPACA_BASE_URL` | Locked to `https://paper-api.alpaca.markets`. |
| `LIVE_MODE` | Default `0`. Real-money trading hard-blocked. |

Secrets must be read only through `biotech_sniper.config`. Calling
`os.environ.get("XAI_API_KEY")` from anywhere else is a mission policy
violation.

## Install

```bash
git clone https://github.com/mrm88/BIOTECH_SNIPER.git
cd BIOTECH_SNIPER
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env  # then fill in real values; never commit .env
```

## Run

Daily run (parameterized by date — replaces the old `build_report_aprNN.py`
proliferation):

```bash
.venv/bin/python -m biotech_sniper.master_unified_run --date 2026-04-25
```

Intraday scan (hourly during US market hours):

```bash
.venv/bin/python -m biotech_sniper.intraday_scanner
```

End-to-end source health probe (writes `state/audit_latest.json`):

```bash
.venv/bin/python -m biotech_sniper.audit
```

## Audit

`audit.py` probes ClinicalTrials.gov, SEC EDGAR, news RSS feeds, and
yfinance, writes a JSON health report under `state/audit_latest.json`,
and exits non-zero if any source is unreachable. The watchdog systemd
unit (added in M4) runs this every 15 minutes.

## VPS deploy (M1)

The VPS clone lives at `/root/alpha_sniper/repo/`. After pushing to
`origin/main` from a worker session, the VPS pulls with:

```bash
ssh root@199.247.25.111 "cd /root/alpha_sniper/repo && git pull --ff-only"
```

Three systemd units run the schedule (added in M4):

- `alpha-sniper.service` — daily 6 AM PT discovery + scoring + play cards.
- `alpha-sniper-intraday.service` — hourly during US market hours Mon-Fri.
- `alpha-sniper-watchdog.service` — health check every 15 minutes.

Cron minutes are staggered off the `:00` and `:30` slots used by
HL grok on the same VPS to avoid thundering-herd load.

## Live-mode hard block

Real-money trading is hard-blocked behind **two independent gates**:

1. Env var `LIVE_MODE=1` must be set, AND
2. The confirmation marker file `i-understand-this-trades-real-money`
   must exist at the repo root (resolved via `paths.py`).

Either gate missing → `LiveTradingBlockedError` is raised before any
order is submitted. Workers and CI MUST NOT create the confirmation
file or set `LIVE_MODE=1`. Only the user does that, manually, via direct
shell access.

## Testing

```bash
.venv/bin/pytest -q
```

VCR cassettes for LLM, Alpaca, CT.gov and SEC EDGAR fixtures land in
`tests/fixtures/cassettes/` in M2.

## License

Private / unreleased.
