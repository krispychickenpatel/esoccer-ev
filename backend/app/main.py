import asyncio
import contextlib

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .database import SessionLocal, init_db
from .routers import (admin, bets, data, engine, friend_picks, lab, paper_trades,
                      picks, profit, provider_ext, recs, research)


def _startup_seed():
    """Quarantined legacy mode: load manual screenshot evidence only when
    AUTO_LOAD_SEED_DATA=1 is explicitly set."""
    from .seed_manual import load_manual_seed
    from .engines.ratings import rebuild_ratings
    db = SessionLocal()
    try:
        created = load_manual_seed(db)
        if created["matches"]:
            rebuild_ratings(db)
        return created
    finally:
        db.close()


app = FastAPI(title="ESoccer EV Research Terminal", version="0.3.6-profit-validation")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()
# Real-mode default: do not auto-insert manual/screenshot seed rows unless explicitly requested.
# This prevents old/fake/demo bets from appearing after the app has a real API key.
if os.environ.get("AUTO_LOAD_SEED_DATA", "0").lower() in ("1", "true", "yes"):
    _startup_seed()

app.include_router(bets.router)
app.include_router(data.router)
app.include_router(engine.router)
app.include_router(admin.router)
app.include_router(recs.router)
app.include_router(picks.router)
app.include_router(research.router)
app.include_router(lab.router)
app.include_router(friend_picks.router)
app.include_router(paper_trades.router)
app.include_router(profit.router)
app.include_router(provider_ext.router)

_poller_task: asyncio.Task | None = None


@app.on_event("startup")
async def _start_poller():
    """Odds Polling Service — idles unless settings.poller_enabled and a
    provider key exist (D8)."""
    global _poller_task
    from .connectors.betsapi_provider import BetsApiProvider
    from .services.poller import poll_loop
    _poller_task = asyncio.create_task(poll_loop(lambda db: BetsApiProvider(db)))


@app.on_event("shutdown")
async def _stop_poller():
    if _poller_task:
        _poller_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _poller_task


@app.get("/api/health")
def health():
    return {"ok": True, "version": "0.3.6-profit-validation"}
