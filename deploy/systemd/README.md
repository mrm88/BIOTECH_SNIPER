# Alpha Sniper systemd units

Source-of-truth systemd units for the Biotech Sniper / Alpha Sniper deployment
on the VPS. These files are committed to the repo and copied into
`/etc/systemd/system/` by the VPS install feature (`f-m4-06`).

## Units

| Unit                                    | Type     | Schedule (UTC)                                   | Entrypoint                                  |
|-----------------------------------------|----------|--------------------------------------------------|---------------------------------------------|
| `alpha-sniper.service` / `.timer`       | oneshot  | Daily 13:13 UTC (PDT) / 14:13 UTC (PST), `Persistent=true` | `python -m biotech_sniper.master_unified_run` |
| `alpha-sniper-intraday.service` / `.timer` | oneshot | Mon..Fri 13:22..21:22 UTC (9 elapses/weekday)   | `python -m biotech_sniper.intraday_scanner` |
| `alpha-sniper-watchdog.service` / `.timer` | oneshot | Every 15 min: `*:08, *:23, *:38, *:53` UTC      | `python -m biotech_sniper.watchdog`         |

## Stagger / collision avoidance

VPS hl-edge cron uses minutes `{0, 5, 7, 15, 30, 45}` hourly, plus the
`tier_refresh.py` cron at `*/30` (minutes 0 and 30). HL grok is a
long-running service, so no timer-minute collision applies there. The
alpha-sniper timers above pick minutes `{08, 13, 22, 23, 38, 53}` — none
collide with hl-edge cron, tier_refresh, or each other on the same minute.

## Service contract

Every service unit:
- `Type=oneshot`
- `User=root`
- `WorkingDirectory=/root/alpha_sniper/repo`
- `EnvironmentFile=/root/alpha_sniper/.env`
- `ExecStart=/root/alpha_sniper/repo/.venv/bin/python -m biotech_sniper.<entrypoint>`
- `StandardOutput=journal` and `StandardError=journal`

Daily timer carries `Persistent=true` so missed runs after a reboot are caught up.

The daily service additionally runs `deploy/scripts/backup_db.sh` via
`ExecStartPost=` to produce a gzipped SQLite snapshot at
`/root/alpha_sniper/backups/db_YYYY-MM-DD.gz`. The script enforces a
30-day rotation (by mtime) and a 30-archive hard cap, and is idempotent on
same-UTC-day re-runs.

## Verification

On a host with systemd (the VPS):

```
systemd-analyze verify deploy/systemd/alpha-sniper.service
systemd-analyze verify deploy/systemd/alpha-sniper-intraday.service
systemd-analyze verify deploy/systemd/alpha-sniper-watchdog.service
systemd-analyze verify deploy/systemd/alpha-sniper.timer
systemd-analyze verify deploy/systemd/alpha-sniper-intraday.timer
systemd-analyze verify deploy/systemd/alpha-sniper-watchdog.timer
```

All six exit 0 with no warnings on stderr.
