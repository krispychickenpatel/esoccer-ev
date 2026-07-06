"""Profit Kill Gates + Profit Readiness Dashboard API (v0.3.6 Modules 7-8)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..engines import profit_gates, winner_edge

router = APIRouter(prefix="/api/profit", tags=["profit"])


@router.get("/gates")
def gates(db: Session = Depends(get_db)):
    return profit_gates.compute_all_gates(db)


@router.get("/winner-edge")
def winner_edge_route(db: Session = Depends(get_db)):
    """Winner Edge Truth Layer (v0.3.6.2 Part B): does the platform predict
    the winning side at a playable price, not just the direction of steam."""
    return winner_edge.winner_edge_report(db)
