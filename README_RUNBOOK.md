# Runbook

Operational quick-reference. For architecture and the feature changelog see
`README.md`; for maintainer rules see `CLAUDE.md`.

## First-time setup

```bash
cd /Users/krispatell/Downloads/ESoccer/current/esoccer-ev
make setup      # creates backend/.venv, installs deps, npm install, seeds backend/.env if missing
make doctor     # verifies toolchain, venv, node_modules, and that BETSAPI_KEY is set
```

If `make setup` had to create `backend/.env`, open it and replace the
placeholder:

```
BETSAPI_KEY=PASTE_KEY_HERE
```

with your real key from betsapi.com. Nothing in this toolchain will ever
print, log, or commit that value.

## Day to day

```bash
make dev        # backend (127.0.0.1:8000) + frontend (127.0.0.1:5173) together
make backend    # backend only
make frontend   # frontend only
make test       # backend pytest + frontend tsc/build
make smoke      # hits /api/health, /api/lab/verify-integrity, /api/steam/report,
                #   /api/provider/capability-report on a running backend
```

## Importing a new release

```bash
make import ZIP=/Users/krispatell/Downloads/ESoccer/incoming/esoccer-ev-vX.Y.Z.zip
```

This backs up the current active repo into `ESoccer/backups/`, installs the
new code, and restores `backend/.env` and `data/seed`/`data/samples` if the
new release doesn't ship them. It will never overwrite an existing `.env`.

## Cleanup

```bash
make clean-safe   # removes __pycache__, *.pyc, frontend/dist only
```

Never deletes `.env`, `esoccer.db`, `data/`, logs, or evidence.

## Real-mode operating order

See `README.md` → "Real-mode operating order" for the full sequence
(tracked leagues → Data Health → pull BetsAPI schedule → poller → Best
Picks → Prediction Lab → paper trade).

## Backend URL / Frontend URL

- Backend:  http://127.0.0.1:8000  (docs at http://127.0.0.1:8000/docs)
- Frontend: http://127.0.0.1:5173

## Troubleshooting

- `make doctor` fails on BETSAPI_KEY: edit `backend/.env` and paste your key
  in manually. Never paste it into a chat/agent session.
- Port already in use: another `make dev`/`make backend` is likely still
  running; stop it before starting a new one.
- Frontend can't reach backend: confirm backend is on 127.0.0.1:8000 -- CORS
  in `backend/app/main.py` only allows `localhost:5173` / `127.0.0.1:5173`.
