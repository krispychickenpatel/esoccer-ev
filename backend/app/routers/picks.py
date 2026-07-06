"""Pick Engine + Shadow dashboards + provider status + poller status."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..connectors.betsapi_provider import BetsApiProvider
from ..database import get_db
from ..engines.pick_engine import generate_picks, sweep_and_settle
from ..engines.steam import steam_prediction_for_snapshot, steam_report
from ..engines.research import what_changed
from ..engines.shadow import (data_health, league_profiles, shadow_dashboard,
                              sportsbook_profiles)
from ..models import Match, OddsSnapshot, Pick, Settings
from ..services import poller as poller_service
from ..services.poller import STATUS as POLLER_STATUS

router = APIRouter(prefix="/api", tags=["picks"])


@router.post("/picks/generate")
def picks_generate(db: Session = Depends(get_db)):
    sweep_and_settle(db)
    cards = generate_picks(db, persist=True)
    return {"generated": len(cards),
            "bet": sum(1 for c in cards if c["status"] == "BET"),
            "wait": sum(1 for c in cards if c["status"] == "WAIT"),
            "pass": sum(1 for c in cards if c["status"] == "PASS")}


@router.get("/picks/best")
def picks_best(db: Session = Depends(get_db), limit: int = 30):
    """Live view — evaluates now, does not persist. The Best Picks page."""
    sweep_and_settle(db)
    cards = generate_picks(db, persist=False)
    return cards[:limit]


def _pick_out(db: Session, p: Pick) -> dict:
    m = db.get(Match, p.match_id)
    return {
        "id": p.id, "created_at": p.created_at.isoformat(),
        "match": f"{m.home_player.name} vs {m.away_player.name}" if m else "",
        "scheduled_start": m.start_time.isoformat() if m else None,
        "league": m.league if m else "",
        "market": p.market, "selection": p.selection, "line": p.line,
        "sportsbook": p.sportsbook, "current_american": p.current_american,
        "model_prob": p.model_prob, "fair_decimal": p.fair_decimal,
        "ev_pct": p.ev_pct, "rank_score": p.rank_score, "status": p.status,
        "reason_codes": json.loads(p.reason_codes or "[]"),
        "confidence": json.loads(p.confidence_json or "{}"),
        "consensus": p.consensus, "suggested_stake": p.suggested_stake,
        "user_decision": p.user_decision,
        "settled_result": p.settled_result, "profit": p.profit,
        "clv_pct": p.clv_pct, "grade": p.grade,
        "model_version": p.model_version, "feature_set_version": p.feature_set_version,
        "recommendation_id": p.recommendation_id,
        "include_in_metrics": p.include_in_metrics,
    }


@router.get("/picks/history")
def picks_history(db: Session = Depends(get_db), limit: int = 300):
    sweep_and_settle(db)
    rows = db.scalars(select(Pick).order_by(Pick.created_at.desc()).limit(limit)).all()
    out = [_pick_out(db, p) for p in rows]
    settled = [p for p in out if p["settled_result"]]
    decided_bet = [p for p in settled if p["user_decision"] == "bet"]
    passed = [p for p in settled if p["user_decision"] == "pass" or
              (p["user_decision"] is None and p["status"] in ("PASS", "WAIT"))]
    # was passing correct? A pass is "correct" when the pick lost.
    correct_passes = sum(1 for p in passed if p["settled_result"] == "loss")
    return {
        "picks": out,
        "summary": {
            "settled": len(settled),
            "bet_count": len(decided_bet),
            "bet_profit": round(sum(p["profit"] or 0 for p in decided_bet), 2),
            "pass_count": len(passed),
            "correct_pass_rate": round(correct_passes / len(passed), 3) if passed else None,
            "grades": {g: sum(1 for p in settled if p["grade"] == g)
                       for g in ("A+", "A", "B", "C", "D", "F")},
        },
    }


@router.post("/picks/{pick_id}/decision")
def pick_decide(pick_id: int, decision: str, db: Session = Depends(get_db)):
    if decision not in ("bet", "pass"):
        raise HTTPException(400, "decision must be bet|pass")
    p = db.get(Pick, pick_id)
    if not p:
        raise HTTPException(404, "Pick not found")
    p.user_decision = decision
    p.decided_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    return _pick_out(db, p)


@router.get("/what-changed/{match_id}")
def what_changed_route(match_id: int, db: Session = Depends(get_db)):
    return what_changed(db, match_id)


@router.get("/shadow/dashboard")
def shadow_route(db: Session = Depends(get_db)):
    s = db.get(Settings, 1)
    return shadow_dashboard(db, include_seed=s.include_seed_data if s else True)


@router.get("/intel/leagues")
def leagues_route(db: Session = Depends(get_db)):
    s = db.get(Settings, 1)
    return league_profiles(db, include_seed=s.include_seed_data if s else True)


@router.get("/intel/sportsbooks")
def books_route(db: Session = Depends(get_db)):
    s = db.get(Settings, 1)
    return sportsbook_profiles(db, include_seed=s.include_seed_data if s else True)


@router.get("/data-health")
def data_health_route(db: Session = Depends(get_db)):
    return data_health(db)




@router.get("/steam/report")
def steam_report_route(db: Session = Depends(get_db)):
    return steam_report(db)


@router.get("/steam/match/{match_id}")
def steam_match_route(match_id: int, db: Session = Depends(get_db)):
    m = db.get(Match, match_id)
    if not m:
        raise HTTPException(404, "Match not found")
    s = db.get(Settings, 1)
    snaps = db.scalars(select(OddsSnapshot).where(OddsSnapshot.match_id == match_id)
                       .order_by(OddsSnapshot.collected_at)).all()
    latest = {}
    for sn in snaps:
        latest[(sn.sportsbook, sn.market, sn.selection, sn.line)] = sn
    return {
        "match_id": match_id,
        "match": f"{m.home_player.name} vs {m.away_player.name}",
        "scheduled_start": m.start_time.isoformat(),
        "predictions": [steam_prediction_for_snapshot(db, m, sn, s) | {
            "sportsbook": sn.sportsbook, "market": sn.market,
            "selection": sn.selection, "line": sn.line,
        } for sn in latest.values()],
    }

@router.get("/provider/capability-report")
def provider_capability_report(db: Session = Depends(get_db), probe: bool = False):
    return BetsApiProvider(db).capability_report(probe=probe)


@router.get("/provider/status")
def provider_status(db: Session = Depends(get_db)):
    return {"betsapi": BetsApiProvider(db).status(), "poller": POLLER_STATUS}


@router.get("/provider/performance-report")
def provider_performance_report(db: Session = Depends(get_db)):
    """v0.3.5: loop timing, call volume, and first-live latency percentiles
    for a controlled validation session."""
    return poller_service.performance_report(db)


@router.get("/provider/result-ingestion-report")
def provider_result_ingestion_report():
    """v0.3.5: latest ended-results ingestion cycle -- fetched/matched/updated/
    unmatched counts plus how many Prediction Lab predictions that unblocked."""
    return POLLER_STATUS.get("result_ingestion") or {
        "note": "no ended-results ingestion cycle has run yet -- enable the poller "
                "and wait up to 45s, or it runs automatically once enabled."
    }
