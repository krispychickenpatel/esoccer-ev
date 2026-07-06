"""Prediction Lab API: frozen predictions -> reality -> scores."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..engines import prediction_lab as lab
from ..models import Match

router = APIRouter(prefix="/api/lab", tags=["prediction-lab"])


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    return lab.dashboard(db)


@router.get("/predictions")
def predictions(limit: int = 300, db: Session = Depends(get_db)):
    return {"items": lab.ledger_rows(db, limit=limit), "lab_version": lab.LAB_VERSION}


@router.get("/model-comparison")
def model_comparison(db: Session = Depends(get_db)):
    return lab.model_comparison(db)


@router.post("/freeze-due")
def freeze_due(tolerance_seconds: int = 75, allow_late: bool = False,
               db: Session = Depends(get_db)):
    return lab.freeze_due_predictions(db, tolerance_seconds=tolerance_seconds,
                                      allow_late=allow_late)


@router.post("/freeze-match/{match_id}")
def freeze_match(match_id: int, horizon_label: str = "MANUAL",
                 allow_late: bool = True, db: Session = Depends(get_db)):
    m = db.get(Match, match_id)
    if not m:
        raise HTTPException(404, "Match not found")
    rows = lab.freeze_match_horizon(db, m, horizon_label, allow_late=allow_late)
    return {"created": len(rows), "match_id": match_id, "horizon_label": horizon_label,
            "lab_version": lab.LAB_VERSION}


@router.post("/capture-reality")
def capture_reality(db: Session = Depends(get_db)):
    return lab.capture_reality(db)


@router.post("/score")
def score(db: Session = Depends(get_db)):
    return lab.score_predictions(db)


@router.post("/run-cycle")
def run_cycle(db: Session = Depends(get_db)):
    return lab.run_prediction_lab_cycle(db)


@router.get("/verify-integrity")
def verify_integrity(db: Session = Depends(get_db)):
    """Recompute frozen-prediction hashes; any mismatch = tampered ledger."""
    return lab.verify_integrity(db)


@router.get("/match/{match_id}")
def match_detail(match_id: int, db: Session = Depends(get_db)):
    preds = [p for p in lab.ledger_rows(db, limit=1000) if p["match_id"] == match_id]
    return {"match_id": match_id, "predictions": preds, "lab_version": lab.LAB_VERSION}
