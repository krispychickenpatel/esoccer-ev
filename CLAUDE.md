# CLAUDE.md

Guidance for Claude Code (or any future maintainer) working in this repo.

## What this is

ESoccer EV — Prediction Lab Terminal. A quantitative research platform for
esoccer betting: Prediction Engine, Decision Engine, Execution Engine, Risk
Engine, Evaluation Engine. Not a generic picks app. See `README.md` for the
full architecture and per-version changelog, and `docs/` for design records
(`AUDIT_V2.md`, `BUILD_PLAN.md`, `DATA_FLOW.md`, `DECISIONS.md`, `SCHEMA.md`).

## Hard constraints — do not violate

- **Never print, log, commit, or expose `BETSAPI_KEY`** or any other secret.
  Only ever report whether it is set.
- **Never overwrite `backend/.env`.** If it's missing, create it from
  `.env.example` with a placeholder only.
- **Never add fake/demo betting data.** Real Mode is the default; manual
  seed rows only load if `AUTO_LOAD_SEED_DATA=1` is explicitly set, and that
  path is for research review only — it does not feed real-mode metrics.
- **Never remove or weaken**: Real Mode, Prediction Lab, Steam Predictor,
  Provider Capability Report, or `GET /api/lab/verify-integrity`. These are
  the platform's evidence and anti-fake-certainty guarantees.
- **Do not change betting/decision logic and do not add ML.** This is a
  deterministic, auditable system by design (see `docs/DECISIONS.md`).
- Preserve `.env`, local DB files, logs, data captures, manual evidence, and
  Prediction Lab evidence on every setup/update/import operation.

## Layout

```
backend/   FastAPI app (app/main.py), SQLite by default (backend/esoccer.db)
frontend/  Vite + React + TypeScript
data/      seed/ and samples/ CSVs -- quarantined research-only data
docs/      architecture, schema, and changelog records
scripts/   doctor.py, smoke_test.py, import_release.py, start_dev.py
```

## Where config lives

- `backend/.env` is what the backend actually loads (via `python-dotenv` in
  `backend/app/database.py`, called once at import time). It is loaded
  relative to the process's working directory, which is `backend/` per the
  documented run command — so the file must live at `backend/.env`, not repo
  root.
- `.env.example` (repo root) and `backend/.env.example` document the
  variables. `BETSAPI_KEY` is checked before `BETSAPI_TOKEN`.

## Common commands

See `README_RUNBOOK.md` for the full list. Short version: `make doctor`,
`make setup`, `make dev`, `make test`, `make smoke`.

## Importing a new release zip

Use `make import ZIP=/path/to/esoccer-ev-vX.Y.Z.zip` (wraps
`scripts/import_release.py`). It backs up the current active repo, installs
the new release, and restores `.env` / `data/seed` / `data/samples` if the
new release doesn't ship them. It never overwrites an existing `.env`.

## Workspace layout (outside this repo)

This repo lives at `ESoccer/current/esoccer-ev` and is the single active
copy. `Downloads/esoccer-ev` is a symlink to it (kept for convenience/back-
compat with old shortcuts). Old duplicate folders were moved into
`ESoccer/archive/<timestamp>/`, not deleted. Timestamped tarball backups of
everything live in `ESoccer/backups/`. See
`ESoccer/notes/workspace-cleanup-report.md` for the full history of the
cleanup that established this layout.
