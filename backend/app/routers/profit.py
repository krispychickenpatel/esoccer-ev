"""Profit Kill Gates + Profit Readiness Dashboard API (v0.3.6 Modules 7-8)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..engines import profit_gates

router = APIRouter(prefix="/api/profit", tags=["profit"])


@router.get("/gates")
def gates(db: Session = Depends(get_db)):
    return profit_gates.compute_all_gates(db)
