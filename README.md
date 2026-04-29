# BIOTECH_SNIPER

Biotech catalyst-driven options paper-trading system. The pipeline ingests
ClinicalTrials.gov, SEC EDGAR, news RSS, FDA AdCom calendars and USASpending
contract data, scores ~600 biotech tickers through a two-tier LLM ensemble
(Grok-4 fast tier + Claude / Gemini deep-science tier with a head-to-head
debate loop), generates daily play cards, and submits paper-only single-leg
long call/put orders to the Alpaca paper endpoint with hard guardrails
against real-money trading.

This repository is the canonical source of truth and the **authoritative
operational document** for the system. The VPS at `root@199.247.25.111`
clones it into `/root/alpha_sniper/repo/` and runs oneshot systemd timers
for the daily, intraday and watchdog cycles. Every change is shipped via
`git pull --ff-only` on the VPS — there is no other deploy path.

## Layout

- `biotech_sniper/` — main Python package (~22k LOC, refactored from the
  original unzipped reference project).
- `biotech_sniper/paths.py` — **single source of truth for filesystem
  paths**. No other module in the package may construct an absolute
  ``/home``, ``/root``, ``/tmp`` or ``/var`` path.
- `biotech_sniper/config.py` — **single source of truth for secrets**,
  feature flags and risk defaults. No other module may call
  `os.environ.get(...)` for an `ALPACA_*`, `XAI_*`, `ANTHROPIC_*`,
  `GEMINI_*` or `GITHUB_*` variable.
- `biotech_sniper/audit.py` — end-to-end source reachability probe and
  daily health-JSON writer.
- `biotech_sniper/training/` — M5 backtest harness, parquet feature
  store, execution dataset builder and LightGBM ranker trainer.
- `tests/` — pytest suites with VCR cassettes for network-dependent
  fixtures.
- `migrations/seed/` — historical state JSONs preserved as seed data
  for the M2 SQLite backfill (6 resolved trades, 261 NCT IDs,
  scoring cache).
- `deploy/` — systemd unit files, logrotate config, and VPS deploy
  helpers (consumed by the VPS deploy worker, not by app code).
- `requirements.txt` — fully pinned dependency set.
- `.env.example` — required env-var key names (no secret values).

## Environment

All required environment variables are listed in `.env.example`. The
runtime reads them from a `.env` file at the repo root locally, or — on
the VPS — at `/root/alpha_sniper/.env` (one level above the checkout,
mode `600`, owned by `root`, never committed).

| Variable | Purpose | Notes |
| --- | --- | --- |
| `BIOTECH_SNIPER_HOME` | Absolute path to the repo checkout (resolved by `paths.py`). | Required on the VPS. |
| `XAI_API_KEY` | Grok-4 fast-tier scorer (M2). | Reused from HL grok env. |
| `ANTHROPIC_API_KEY` | Claude deep-tier science reasoner (M2). | Reused from HL grok env. |
| `GEMINI_API_KEY` | Gemini 2.5 Pro deep-tier reasoner (M2 ensemble). | User-supplied. |
| `ALPACA_KEY_ID` / `ALPACA_SECRET_KEY` | Alpaca **paper** credentials (M3). | Generated at app.alpaca.markets/paper. |
| `ALPACA_BASE_URL` | Locked to `https://paper-api.alpaca.markets`. | The live endpoint is rejected. |
| `LIVE_MODE` | Default `0`. Real-money trading hard-blocked. | See [Live-mode hard block](#live-mode-hard-block). |
| `ALPHA_SNIPER_LOG_DIR` / `ALPHA_SNIPER_LOG_PATH` | Optional override for log destination. | Defaults to `/var/log/alpha_sniper/`. |

Secrets must be read **only** through `biotech_sniper.config`. Calling
`os.environ.get("XAI_API_KEY")` (or any other secret) from anywhere
else is a mission policy violation that is enforced by both code review
and the secrets-scan check (see [Secrets scan](#secrets-scan)).

## Install

There are exactly **three operator-facing commands**: `install`, the
daily run, and the intraday run. Everything else is invoked
automatically by systemd timers on the VPS or by tests locally.

```bash
# 1. Install
git clone https://github.com/mrm88/BIOTECH_SNIPER.git
cd BIOTECH_SNIPER
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env  # then fill in real values; never commit .env
```

## Daily run

Runs the full discovery → universe refresh → news ingest → LLM scoring →
play-card generation → paper-order submission cycle for a given trade
date. This is what `alpha-sniper.service` executes at 06:00 PT on the
VPS.

```bash
# 2. Daily (parameterized by date)
.venv/bin/python -m biotech_sniper.master_unified_run --date 2026-04-25
```

Replaces the original `build_report_aprNN.py` proliferation; archived
copies live under `archive/` for reference.

## Intraday run

Runs the intraday news scan, adverse-news exit checks, stop-loss tick
and rotation evaluator. Triggered hourly during US market hours
Mon–Fri by `alpha-sniper-intraday.service`.

```bash
# 3. Intraday (no date arg — current market clock)
.venv/bin/python -m biotech_sniper.intraday_scanner
```

## Audit / health

`audit.py` probes ClinicalTrials.gov, SEC EDGAR, news RSS feeds, and
the Alpaca paper endpoint, writes a JSON health report to
`state/audit_latest.json`, and exits non-zero if any source is
unreachable. `alpha-sniper-watchdog.service` runs this every 15 minutes.

```bash
.venv/bin/python -m biotech_sniper.audit
```

## VPS deploy via `git pull`

Every worker session ends with `git push origin main`. Deployment to
the VPS is a single command — there is no build artefact, no Docker
image, no CI promotion gate:

```bash
ssh root@199.247.25.111 "cd /root/alpha_sniper/repo && git pull --ff-only \
  && .venv/bin/pip install -r requirements.txt"
```

The VPS clone lives at `/root/alpha_sniper/repo/`. The Python venv is
at `/root/alpha_sniper/repo/.venv` (Python 3.10.12). The `.env` file
sits one directory above the checkout at `/root/alpha_sniper/.env`
(mode 600, root-owned). The SQLite database is at
`/root/alpha_sniper/repo/data/alpha_sniper.db` and is backed up daily
to `/root/alpha_sniper/backups/db_YYYY-MM-DD.gz` (30-day rotation).

## systemd units

Three oneshot units run the schedule on the VPS:

| Unit | Cadence | Responsibility |
| --- | --- | --- |
| `alpha-sniper.service` (+ `.timer`) | Daily 06:00 PT (Mon–Fri) | Discovery, universe refresh, news ingest, full LLM scoring, play cards, paper-order submission. |
| `alpha-sniper-intraday.service` (+ `.timer`) | Hourly during US market hours | Intraday news scan, adverse-news exits, stop-loss ticks, rotation evaluator. |
| `alpha-sniper-watchdog.service` (+ `.timer`) | Every 15 minutes | Source-reachability audit and health-JSON refresh. |

Unit and timer files live in `deploy/systemd/` and are installed by the
VPS deploy worker into `/etc/systemd/system/`. Timer minutes are
staggered off the `:00` and `:30` slots used by HL grok on the same
VPS to avoid thundering-herd load. Logs land in
`/var/log/alpha_sniper/{daily,intraday,watchdog}.log` (one structured
JSON object per line, rotated by the logrotate config in
`deploy/logrotate/alpha-sniper`).

## Live-mode hard block

Real-money trading is hard-blocked behind **two independent gates**:

1. The env var `LIVE_MODE=1` must be set, **AND**
2. The confirmation marker file `i-understand-this-trades-real-money`
   must exist at the repo root (resolved via `paths.py`).

If either gate is missing, `LiveTradingBlockedError` is raised before
any order is submitted. The marker file is git-ignored. Workers and
CI **must not** create the marker file or set `LIVE_MODE=1`. Only the
user does that, manually, via direct shell access on the VPS. The
`paper_executor` additionally refuses to start when
`ALPACA_BASE_URL != https://paper-api.alpaca.markets`.

## Secrets scan

`.env` is committed to `.gitignore` and no real keys are ever stored in
the repo. The same checks the orchestrator runs on every milestone seal
can be reproduced locally:

```bash
# 1. No real secret values committed anywhere.
grep -RIE '(ALPACA_KEY_ID=[A-Z0-9]{4,}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,})' \
  . --include='*.py' --include='*.md' --include='*.txt' \
  --include='*.yaml' --include='*.yml' --include='*.json' \
  --exclude-dir='.git' --exclude-dir='.venv'
# expect: no output

# 2. paths.py is the sole source of truth for absolute system paths.
grep -RnE "Path\(['\"]/(home|root|tmp|var)" biotech_sniper/ \
  --include='*.py' | grep -v paths.py
# expect: no output

# 3. config.py is the sole source of truth for secret reads.
grep -RnE 'os\.environ\.get\(.(ALPACA|XAI|ANTHROPIC|GEMINI|GITHUB)' \
  biotech_sniper/ --include='*.py' | grep -v config.py
# expect: no output
```

If any of these greps return a match, treat it as a blocking finding
and fix it before the next push.

## Testing

```bash
.venv/bin/pytest -q -n 2
```

VCR cassettes for LLM, Alpaca, CT.gov and SEC EDGAR fixtures live
under `tests/fixtures/cassettes/`. No live network calls are made
during the test suite. Pytest parallelism is capped at `-n 2` to
respect the VPS's 2-core ceiling.

## Recent post-seal cleanup

After the cross-final seal (commit `e340262`), a series of `f-misc-*`
milestones landed surgical hardening across the runtime without changing
any user-facing behaviour. The current head is `38c6b83` on `main`, with
**1079 tests passing / 4 skipped**.

- **Hermetic runtime.** All `sys.path.insert` sites have been swept from
  the package (tree-wide AST guard in tests). Live LLM cassette
  infrastructure is wired for 3 of 4 providers (Gemini parked on user
  billing) so the suite never touches the network.
- **Dependency drift fixed.** `urllib3==2.2.3`, `chardet==5.2.0` and
  `charset-normalizer==3.4.0` are now pinned in `requirements.txt`.
  `yfinance` is no longer a runtime dependency (guarded for absence).
  The Gemini SDK `HttpOptions` drift is corrected and `unified_scorer`
  reports honest `providers_used`.
- **DATA_DIR boundary hardened.** `biotech_sniper.paths.ensure_data_dir()`
  is the canonical helper, `db.connect()` centralizes `parent.mkdir`
  under `DATA_DIR`, and `_ensure_parent_under_data_dir` canonicalizes
  both sides via `Path.resolve(strict=False)` before the boundary check
  to defeat `..` / symlink escapes.
- **Audit module hermetic on import.** `audit.py` no longer makes
  network calls at import time, tolerates expected provider failures,
  and stamps `last_daily_run` in UTC. Defensive `mkdir` calls were
  added to `intraday_scanner.save_log` and the `email_formatter`
  bare-namespace imports were normalized to canonical `biotech_sniper.*`
  paths (AST guard).

## License

Private / unreleased.
