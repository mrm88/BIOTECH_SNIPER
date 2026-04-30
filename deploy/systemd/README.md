# Alpha Sniper systemd units

Source-of-truth systemd units for the Biotech Sniper / Alpha Sniper deployment
on the VPS. These files are committed to the repo and copied into
`/etc/systemd/system/` by the VPS install feature (`f-m4-06`).

## Units

| Unit                                    | Type     | Schedule (UTC)                                   | Entrypoint                                  |
|-----------------------------------------|----------|--------------------------------------------------|---------------------------------------------|
| `alpha-sniper.service` / `.timer`       | oneshot  | Daily 13:13 UTC (PDT) / 14:13 UTC (PST), `Persistent=true` | `python -m biotech_sniper.master_unified_run` |
| `alpha-sniper-intraday.service` / `.timer` | oneshot | Mon..Fri 13:22..21:22 UTC (9 elapses/weekday)   | `python -m biotech_sniper.intraday_run`     |
| `alpha-sniper-watchdog.service` / `.timer` | oneshot | Every 15 min: `*:08, *:23, *:38, *:53` UTC      | `python -m biotech_sniper.watchdog`         |
| `alpha-sniper-news.service` (no timer)  | simple   | Long-lived (Restart=on-failure, RestartSec=10s) | `python -m biotech_sniper.news_daemon`      |

## Reading-B long-lived service: `alpha-sniper-news.service`

This is the FIRST long-lived (Type=simple) service in the project — the
Stage-1 news watcher daemon that polls every ~30 s for fresh biotech
headlines on the Russell-2000 biotech universe. Reading-B does NOT add a
companion `.timer`.

Resource caps (locked spec; the VPS is 2-core under sustained 2x load):

| Knob               | Value                  | Why                                  |
|--------------------|------------------------|--------------------------------------|
| `Nice`             | `10`                   | Yield to existing daily-curated path |
| `IOSchedulingClass`| `idle`                 | Best-effort I/O only                 |
| `CPUQuota`         | `15%`                  | Stay under 2-core saturation budget  |
| `MemoryMax`        | `200M`                 | Hard cap                             |
| `MemoryHigh`       | `150M`                 | Soft cap (throttle, do not OOM)      |
| `TasksMax`         | `64`                   | Bound thread/fork explosions         |

Restart policy (long-lived; `StartLimitBurst=5/300s` thrash-protects):

```
Restart=on-failure
RestartSec=10s
StartLimitBurst=5
StartLimitIntervalSec=300s
```

Graceful shutdown:

```
KillSignal=SIGTERM
TimeoutStopSec=30s
KillMode=control-group
```

Logging is file-based (rotation delegated to `logrotate` — see
`deploy/logrotate/alpha-sniper-news`):

```
StandardOutput=append:/var/log/alpha_sniper/news.log
StandardError=append:/var/log/alpha_sniper/news.log
```

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

For Reading-B:

```
systemd-analyze verify deploy/systemd/alpha-sniper-news.service
```

also exits 0 with no warnings.
