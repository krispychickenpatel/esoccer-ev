# Workday Autopilot (v0.3.7C)

Turns real odds collection on with a bounded, self-enforcing runtime cap so
the platform can collect clean forward data while you're away, without
becoming a live-betting system. **No live betting, no bet placement
automation, no bankroll automation, and no model promotion happen anywhere
in this chain.**

## How to start manually (supervised trial)

```bash
cd /Users/krispatell/Downloads/ESoccer/current/esoccer-ev
python3 scripts/ops/run_workday_autopilot.py --max-minutes 45
```

This validates `BETSAPI_KEY`/`BETSAPI_TOKEN` is set (never prints its
value), confirms the DB is writable, confirms the v0.3.7B schema fields
exist, attaches to an already-running backend (or starts one), sets
`Settings.poller_enabled=True` with a 45-minute auto-shutoff, and then
monitors health every 60s, writing heartbeat lines to
`logs/workday/YYYY-MM-DD.jsonl` and incident snapshots to
`notes/status/incidents/` whenever health is `FAIL`.

Press `Ctrl-C` to stop early (safely disables `poller_enabled` before
exiting). Otherwise it exits on its own once the runtime cap elapses —
`poll_loop` (in `backend/app/services/poller.py`) enforces the cap itself,
independent of this monitor script, so the cap holds even if the monitor
process is killed.

## How to run a full unattended workday (with caffeinate)

```bash
python3 scripts/ops/run_workday_autopilot.py --max-minutes 480 --caffeinate
```

`--caffeinate` wraps a *freshly-started* backend in `caffeinate -i`
(prevents macOS idle sleep without needing the display on). It has no
effect if a backend is already running on port 8000 — start fresh if you
want the sleep-prevention guarantee for a real unattended day.

## How to check status

```bash
python3 scripts/ops/autopilot_status.py
curl -s http://127.0.0.1:8000/api/ops/health | python3 -m json.tool
```

## How to stop

```bash
# Ctrl-C on the running run_workday_autopilot.py process, or:
python3 -c "
import sys; sys.path.insert(0, 'backend')
from app.database import SessionLocal
from app.models import Settings
db = SessionLocal()
s = db.get(Settings, 1)
s.poller_enabled = False
s.autopilot_started_at = None
s.autopilot_max_runtime_minutes = None
db.commit()
"
```

## Densified polling (near-kickoff density boost)

Off by default, and requires **both** gates to be true:
1. `Settings.densified_polling_enabled = True` (a DB row, toggleable via API).
2. `WORKDAY_ENABLE_DENSIFIED_POLLING=true` in the environment.

```bash
# Run WITHOUT densified polling (default, safe):
python3 scripts/ops/run_workday_autopilot.py --max-minutes 45

# Run WITH densified polling, only after you've verified normal cadence
# is stable and you understand the quota cost:
WORKDAY_ENABLE_DENSIFIED_POLLING=true python3 scripts/ops/run_workday_autopilot.py \
  --max-minutes 45 --densified
```

Even when both gates are on, densified polling never exceeds
`WORKDAY_DENSIFIED_MAX_QUOTA_PCT` (default 60%) of the documented hourly
quota, degrades its own cadence (10s→15s→30s) under load, and trips a
circuit breaker on repeated 429/5xx responses.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `WORKDAY_ENABLE_DENSIFIED_POLLING` | `false` | Extra explicit gate for densified polling (see above). |
| `WORKDAY_DENSIFIED_MAX_QUOTA_PCT` | `60` | Hard ceiling on densified-polling quota usage. |
| `WORKDAY_COLLECTION_START` / `WORKDAY_COLLECTION_END` | unset (always active) | `HH:MM` local-time window during which collection is expected/allowed to run. Outside it, the poller idles without spending quota, and `/api/ops/health` reports `IDLE`, not `FAIL`. |
| `WORKDAY_TIMEZONE` | `UTC` | IANA timezone name used to interpret the window above. |
| `WORKDAY_MIN_DISK_HEADROOM_MB` | `500` | Below this, health reports `FAIL`. |
| `WORKDAY_MAX_LAST_POLL_AGE_S` | `180` | Above this during an expected-active window, health reports `DEGRADED`. |
| `WORKDAY_MAX_LAST_INGEST_AGE_S` | `600` | Same, for the most recent odds row. |
| `WORKDAY_BACKUP_DIR` | `backend/backups/` | Override where `backup_db.py` writes backups. |

## How to read reports

- `notes/status/YYYY-MM-DD-workday.md` / `latest_workday.json` — collector uptime, health, collection counts, backup status.
- `notes/research/YYYY-MM-DD-daily-research.md` / `latest_research.json` — see `docs/DAILY_RESEARCH_LOOP.md`.
- `notes/simulations/YYYY-MM-DD-paper-sim.md` / `latest_paper_sim.json` — see `docs/PAPER_SIMULATION_RUNNER.md`.
- `notes/status/YYYY-MM-DD-daily-cycle.md` / `latest_daily_cycle.json` — combined summary from `run_daily_cycle.py`.

## launchd (optional, NOT installed automatically)

A template lives at `scripts/ops/com.esoccer.workday-autopilot.plist.template`.
To install it yourself:

```bash
cp scripts/ops/com.esoccer.workday-autopilot.plist.template \
   ~/Library/LaunchAgents/com.esoccer.workday-autopilot.plist
# Edit the copy: fill in <<PATH_TO_PYTHON3>>, <<REPO_DIR>>, <<MAX_MINUTES_E_G_480>>,
# and <<YOUR_KEY_HERE...>> (your real BETSAPI_KEY). This filled-in copy is
# git-ignored (*.plist, see .gitignore) -- it will NEVER be committed even
# by accident, but double-check before pasting it anywhere else.
launchctl load ~/Library/LaunchAgents/com.esoccer.workday-autopilot.plist
```

To uninstall:

```bash
launchctl unload ~/Library/LaunchAgents/com.esoccer.workday-autopilot.plist
rm ~/Library/LaunchAgents/com.esoccer.workday-autopilot.plist
```

## What must never be committed

- `.env` (already git-ignored)
- Any filled-in `*.plist` (only `*.plist.template` is ever committed)
- `backend/*.db`, `backend/backups/*` (DB files and backups)
- `logs/` (heartbeat logs, uvicorn logs)
- `notes/status/incidents/*.json` (these live outside the repo entirely — see below)
- Screenshots/recordings from manual spot-checks

Note: all `notes/` output (status, research, simulations) lives at
`/Users/krispatell/Downloads/ESoccer/notes/`, **outside this git repo**
(`ESoccer/current/esoccer-ev`) entirely — it is never a candidate for
accidental commit in the first place.
