from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..connectors import csv_v2
from ..database import get_db
from ..engines import odds_math
from ..models import Bet

router = APIRouter(prefix="/api/bets", tags=["bets"])


class BetIn(BaseModel):
    placed_at: datetime
    sportsbook: str = ""
    league: str = ""
    match_label: str = ""
    selection: str = ""
    opponent: str = ""
    market: str = "ML_3WAY"
    line: float | None = None
    american_odds: int
    stake: float
    result: str = "open"
    closing_american_odds: int | None = None
    model_prob: float | None = None
    notes: str = ""
    screenshot_ref: str | None = None


def settle(bet: Bet) -> None:
    """Compute payout/profit from result, and CLV if closing odds present."""
    if bet.result == "win":
        bet.payout = round(bet.stake * bet.decimal_odds, 2)
        bet.profit = round(bet.payout - bet.stake, 2)
    elif bet.result == "loss":
        bet.payout = 0.0
        bet.profit = -bet.stake
    elif bet.result in ("push", "void"):
        bet.payout = bet.stake
        bet.profit = 0.0
    else:  # open
        bet.payout = 0.0
        bet.profit = 0.0
    if bet.closing_american_odds:
        closing_dec = odds_math.american_to_decimal(bet.closing_american_odds)
        bet.clv_pct = round(odds_math.clv_pct(bet.decimal_odds, closing_dec) * 100, 2)
    if bet.model_prob:
        bet.ev_at_placement = round(
            odds_math.expected_value(bet.model_prob, bet.decimal_odds) * 100, 2)


@router.get("")
def list_bets(db: Session = Depends(get_db), limit: int = 500):
    rows = db.scalars(select(Bet).order_by(Bet.placed_at.desc()).limit(limit)).all()
    return [row_to_dict(b) for b in rows]


@router.post("")
def create_bet(payload: BetIn, db: Session = Depends(get_db)):
    bet = Bet(**payload.model_dump(),
              decimal_odds=round(odds_math.american_to_decimal(payload.american_odds), 4))
    settle(bet)
    db.add(bet)
    db.commit()
    return row_to_dict(bet)


@router.put("/{bet_id}")
def update_bet(bet_id: int, payload: BetIn, db: Session = Depends(get_db)):
    bet = db.get(Bet, bet_id)
    if not bet:
        raise HTTPException(404, "Bet not found")
    for k, v in payload.model_dump().items():
        setattr(bet, k, v)
    bet.decimal_odds = round(odds_math.american_to_decimal(payload.american_odds), 4)
    settle(bet)
    db.commit()
    return row_to_dict(bet)


@router.delete("/{bet_id}")
def delete_bet(bet_id: int, db: Session = Depends(get_db)):
    bet = db.get(Bet, bet_id)
    if not bet:
        raise HTTPException(404, "Bet not found")
    db.delete(bet)
    db.commit()
    return {"deleted": bet_id}


@router.post("/import")
async def import_bets(file: UploadFile = File(...), db: Session = Depends(get_db),
                      dry_run: bool = False):
    text = (await file.read()).decode("utf-8-sig")
    rows, errors, warnings = csv_v2.parse_bets(text)
    report = {"parsed": len(rows), "errors": errors, "warnings": warnings,
              "dry_run": dry_run, "imported": 0, "duplicates": 0}
    if dry_run:
        report["preview"] = [{k: str(v) for k, v in r.items()} for r in rows[:20]]
        return report
    if errors:
        raise HTTPException(422, detail=report)  # never silently drop rows
    for r in rows:
        if r.get("ext_id") and db.scalar(select(Bet).where(Bet.ext_id == r["ext_id"])):
            report["duplicates"] += 1
            continue
        given_profit, given_payout = r.pop("profit", None), r.pop("payout", None)
        bet = Bet(**{k: v for k, v in r.items() if not k.startswith("_")})
        settle(bet)
        # CSV-supplied settled numbers win over derived (raw preserved), warn on drift
        if given_payout is not None:
            if abs(given_payout - bet.payout) > 0.05 and bet.result in ("win", "loss"):
                report["warnings"].append(
                    f"row {r['_row']}: payout {given_payout} != derived {bet.payout}; CSV value kept")
            bet.payout = given_payout
        if given_profit is not None:
            bet.profit = given_profit
        db.add(bet)
        report["imported"] += 1
    db.commit()
    return report


def row_to_dict(b: Bet) -> dict:
    return {
        "id": b.id, "placed_at": b.placed_at.isoformat(), "sportsbook": b.sportsbook,
        "league": b.league, "match_label": b.match_label, "selection": b.selection,
        "opponent": b.opponent, "market": b.market, "line": b.line,
        "american_odds": b.american_odds, "decimal_odds": b.decimal_odds,
        "stake": b.stake, "result": b.result, "payout": b.payout, "profit": b.profit,
        "closing_american_odds": b.closing_american_odds, "clv_pct": b.clv_pct,
        "model_prob": b.model_prob, "ev_at_placement": b.ev_at_placement,
        "notes": b.notes, "screenshot_ref": b.screenshot_ref,
        "data_source": b.data_source, "verification_status": b.verification_status,
    }
