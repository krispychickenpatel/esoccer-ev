"""Research Notebook, pattern discovery, calibration, drift, strategies."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..engines.backtest import BacktestConfig, run_backtest
from ..engines.research import (calibration, drift, run_hypothesis,
                                scan_patterns, similar_setups)
from ..models import Hypothesis, PatternNote, Settings, Strategy

router = APIRouter(prefix="/api", tags=["research"])

TEST_TYPES = {
    "player_backed_roi": {"params": ["player", "underdog_only"],
                          "desc": "ROI when this player is backed (optionally only as underdog)"},
    "market_vs_market": {"params": ["min_ev_pct"],
                         "desc": "ML vs spread performance above an EV threshold"},
    "league_variance": {"params": ["league"], "desc": "Goal mean/variance for a league"},
    "odds_range_roi": {"params": ["min_decimal", "max_decimal"],
                       "desc": "ROI inside a decimal-odds band"},
    "live_shorten": {"params": ["player"],
                     "desc": "Does this player's line shorten at live? (needs live snapshots)"},
    "book_speed": {"params": [], "desc": "Which book updates slower (needs poller data)"},
}


class HypIn(BaseModel):
    title: str
    test_type: str
    params: dict = {}


@router.get("/hypotheses")
def list_hyps(db: Session = Depends(get_db)):
    rows = db.scalars(select(Hypothesis).order_by(Hypothesis.created_at.desc())).all()
    return {"test_types": TEST_TYPES, "hypotheses": [{
        "id": h.id, "title": h.title, "test_type": h.test_type,
        "params": json.loads(h.params_json or "{}"), "status": h.status,
        "last_result": json.loads(h.last_result_json or "{}"),
        "last_tested_at": h.last_tested_at.isoformat() if h.last_tested_at else None,
        "trend": h.trend,
    } for h in rows]}


@router.post("/hypotheses")
def create_hyp(payload: HypIn, db: Session = Depends(get_db)):
    if payload.test_type not in TEST_TYPES:
        raise HTTPException(400, f"test_type must be one of {list(TEST_TYPES)}")
    h = Hypothesis(title=payload.title, test_type=payload.test_type,
                   params_json=json.dumps(payload.params))
    db.add(h)
    db.commit()
    s = db.get(Settings, 1)
    result = run_hypothesis(db, h, include_seed=s.include_seed_data if s else True)
    return {"id": h.id, "result": result}


@router.post("/hypotheses/{hyp_id}/test")
def test_hyp(hyp_id: int, db: Session = Depends(get_db)):
    h = db.get(Hypothesis, hyp_id)
    if not h:
        raise HTTPException(404, "Hypothesis not found")
    s = db.get(Settings, 1)
    return {"id": h.id, "trend": h.trend,
            "result": run_hypothesis(db, h, include_seed=s.include_seed_data if s else True)}


@router.post("/hypotheses/test-all")
def test_all(db: Session = Depends(get_db)):
    s = db.get(Settings, 1)
    inc = s.include_seed_data if s else True
    out = []
    for h in db.scalars(select(Hypothesis).where(Hypothesis.status == "active")).all():
        out.append({"id": h.id, "title": h.title,
                    "result": run_hypothesis(db, h, include_seed=inc)})
    return out


@router.delete("/hypotheses/{hyp_id}")
def del_hyp(hyp_id: int, db: Session = Depends(get_db)):
    h = db.get(Hypothesis, hyp_id)
    if not h:
        raise HTTPException(404, "Hypothesis not found")
    h.status = "archived"
    db.commit()
    return {"id": hyp_id, "status": "archived"}


# ---------------------------------------------------------------- patterns
@router.post("/patterns/scan")
def patterns_scan(db: Session = Depends(get_db)):
    s = db.get(Settings, 1)
    created = scan_patterns(db, include_seed=s.include_seed_data if s else True)
    return {"proposed": len(created), "patterns": created,
            "note": "Proposed patterns require your approval before any engine uses them."}


@router.get("/patterns")
def patterns_list(db: Session = Depends(get_db), status: str = "all"):
    q = select(PatternNote).order_by(PatternNote.created_at.desc())
    rows = db.scalars(q).all()
    if status != "all":
        rows = [r for r in rows if r.status == status]
    return [{"id": r.id, "created_at": r.created_at.isoformat(), "kind": r.kind,
             "description": r.description, "stats": json.loads(r.stats_json or "{}"),
             "status": r.status} for r in rows]


@router.post("/patterns/{note_id}/status")
def pattern_status(note_id: int, status: str, db: Session = Depends(get_db)):
    if status not in ("approved", "rejected", "proposed"):
        raise HTTPException(400, "status must be approved|rejected|proposed")
    r = db.get(PatternNote, note_id)
    if not r:
        raise HTTPException(404, "Pattern not found")
    r.status = status
    db.commit()
    return {"id": r.id, "status": r.status}


# ------------------------------------------------------- calibration / drift
@router.get("/calibration")
def calibration_route(db: Session = Depends(get_db)):
    return calibration(db)


@router.get("/drift")
def drift_route(db: Session = Depends(get_db)):
    return drift(db)


@router.get("/similar")
def similar_route(home_id: int, away_id: int, league: str = "",
                  decimal_odds: float = 2.0, market: str = "ML_3WAY",
                  db: Session = Depends(get_db)):
    return similar_setups(db, home_id, away_id, league, decimal_odds, market)


# ---------------------------------------------------------------- strategies
class StrategyIn(BaseModel):
    name: str
    filters: dict = {}
    active: bool = True


@router.get("/strategies")
def strategies_list(db: Session = Depends(get_db)):
    return [{"id": s.id, "name": s.name, "filters": json.loads(s.filters_json or "{}"),
             "active": s.active, "stats": json.loads(s.stats_json or "{}")}
            for s in db.scalars(select(Strategy)).all()]


@router.post("/strategies")
def strategy_create(payload: StrategyIn, db: Session = Depends(get_db)):
    if db.scalar(select(Strategy).where(Strategy.name == payload.name)):
        raise HTTPException(400, "Strategy name exists")
    s = Strategy(name=payload.name, filters_json=json.dumps(payload.filters),
                 active=payload.active)
    db.add(s)
    db.commit()
    return {"id": s.id}


@router.post("/strategies/{sid}/evaluate")
def strategy_eval(sid: int, db: Session = Depends(get_db)):
    """Backtest the strategy's filter set and store rolling stats on it."""
    s = db.get(Strategy, sid)
    if not s:
        raise HTTPException(404, "Strategy not found")
    f = json.loads(s.filters_json or "{}")
    cfg = BacktestConfig(**{k: v for k, v in f.items()
                            if k in BacktestConfig.__dataclass_fields__})
    result = run_backtest(db, cfg)
    stats = {k: result[k] for k in ("total_bets", "wins", "losses", "roi_pct",
                                    "profit", "max_drawdown_pct") if k in result}
    s.stats_json = json.dumps(stats)
    if stats.get("total_bets", 0) >= 25 and (stats.get("roi_pct") or 0) < -5:
        s.active = False  # auto-deactivate degraded strategies
    db.commit()
    return {"id": s.id, "active": s.active, "stats": stats}


@router.put("/strategies/{sid}")
def strategy_update(sid: int, payload: StrategyIn, db: Session = Depends(get_db)):
    s = db.get(Strategy, sid)
    if not s:
        raise HTTPException(404, "Strategy not found")
    s.name, s.filters_json, s.active = payload.name, json.dumps(payload.filters), payload.active
    db.commit()
    return {"id": s.id}
