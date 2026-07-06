"""Real-mode database utility.

Synthetic/demo data generation was intentionally removed in v0.3.2-real.
Use API pulls or verified CSV imports only.

    python -m app.seed --wipe

This command deletes local research data for a clean real-mode restart.
"""
from __future__ import annotations

import sys

from .database import SessionLocal, init_db
from .models import (Alert, BacktestRun, Bet, ExecutionLog, Hypothesis, MarketEvent,
                     Match, OddsSnapshot, PatternNote, Pick, Player, PlayerAlias,
                     Prediction, RatingHistory, RawProviderResponse,
                     Recommendation, Strategy)


def wipe() -> None:
    init_db()
    ordered = (Alert, Pick, ExecutionLog, Bet, Prediction, RatingHistory,
               OddsSnapshot, MarketEvent, Recommendation, Hypothesis, PatternNote,
               Strategy, RawProviderResponse, Match, PlayerAlias, Player, BacktestRun)
    with SessionLocal() as db:
        for model in ordered:
            db.query(model).delete()
        db.commit()
    print("All local research data wiped. Settings table preserved/recreated by init_db().")


def main() -> None:
    if "--wipe" not in sys.argv:
        print("Synthetic demo seeding has been removed. Use --wipe, BetsAPI pulls, or verified CSV imports.")
        return
    wipe()


if __name__ == "__main__":
    main()
