"""Paper Trade Engine (v0.3.6 Module 6). No real betting, ever.

For every signal (a model PredictionLedger BET row, or a friend pick), this
simulates entry at delays 0/5/10/20/30/45 seconds after the signal became
known, using ONLY real stored odds snapshots -- never an interpolated or
fabricated price. If no snapshot exists within 60s of the target time, the
trade is MISSED_PRICE, never a fabricated fill.

DISCLAIMER (must ship with every report): "Prices come from a reference feed
with an observed ~20-30s publication lag. Simulated fills are optimistic.
This is decay analysis, not execution proof."
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Match, PaperTrade, PredictionLedger, Settings
from . import odds_math
from .execution_pricing import latest_snapshot_for, price_at_delay

DELAYS_SECONDS = [0, 5, 10, 20, 30, 45]

DISCLAIMER = ("Prices come from a reference feed with an observed ~20-30s "
             "publication lag. Simulated fills are optimistic. This is "
             "decay analysis, not execution proof.")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def simulate_signal(db: Session, *, signal_source: str, signal_id: int, match_id: int,
                    sportsbook: str, market: str, selection: str,
                    signal_time: datetime, max_entry_decimal: float | None,
                    paper_stake: float | None = None) -> list[PaperTrade]:
    """Idempotent: re-running for the same (signal_source, signal_id, delay)
    updates the existing row instead of duplicating it."""
    settings = db.get(Settings, 1)
    stake_units = paper_stake if paper_stake is not None else 1.0
    stake_usd = (settings.paper_stake_usd if settings else 100.0) * stake_units

    out = []
    for delay in DELAYS_SECONDS:
        existing = db.scalar(select(PaperTrade).where(
            PaperTrade.signal_source == signal_source, PaperTrade.signal_id == signal_id,
            PaperTrade.delay_seconds == delay))
        row = existing or PaperTrade(signal_source=signal_source, signal_id=signal_id,
                                     delay_seconds=delay, created_at=_now())
        row.match_id = match_id
        row.sportsbook = sportsbook
        row.market = market
        row.selection = selection
        row.signal_time = signal_time
        row.max_entry_decimal = max_entry_decimal
        row.paper_stake = stake_units
        row.feed_lag_caveat = True

        snap_id, price, status = price_at_delay(db, match_id, sportsbook, market, selection,
                                                signal_time, delay)
        row.price_snapshot_id = snap_id
        row.price_decimal = price

        if status != "OK" or price is None:
            row.settlement_status = "MISSED_PRICE"
            row.entry_survived = False
            row.paper_pl_usd = None
            row.proxy_clv_pct = None
            row.book_availability = "UNKNOWN"
        else:
            survived = max_entry_decimal is None or price >= max_entry_decimal
            row.entry_survived = survived
            row.book_availability = "AVAILABLE"
            if not survived:
                row.settlement_status = "MISSED_PRICE"
                row.paper_pl_usd = None
                row.proxy_clv_pct = None
            else:
                row.settlement_status = "FILLED"
                closing = latest_snapshot_for(db, match_id, sportsbook, market, selection)
                if closing is not None:
                    try:
                        row.proxy_clv_pct = round(odds_math.clv_pct(price, closing.decimal_odds) * 100, 2)
                    except (ValueError, ZeroDivisionError):
                        row.proxy_clv_pct = None
                match = db.get(Match, match_id)
                if match and match.home_score is not None and match.away_score is not None and match.winner:
                    won = match.winner == selection
                    row.paper_pl_usd = round(stake_usd * (price - 1), 2) if won else round(-stake_usd, 2)
                    row.settlement_status = "SETTLED"
                else:
                    row.paper_pl_usd = None

        if existing is None:
            db.add(row)
        out.append(row)
    db.commit()
    return out


def simulate_model_candidate(db: Session, prediction_id: int) -> list[PaperTrade] | None:
    """Simulate a frozen BET-action PredictionLedger row."""
    pred = db.get(PredictionLedger, prediction_id)
    if not pred or pred.action != "BET":
        return None
    return simulate_signal(
        db, signal_source="MODEL", signal_id=pred.id, match_id=pred.match_id,
        sportsbook=pred.sportsbook, market=pred.market, selection=pred.selection,
        signal_time=pred.prediction_time, max_entry_decimal=pred.maximum_entry_decimal,
    )


def simulate_friend_pick(db: Session, friend_pick_id: int) -> list[PaperTrade] | None:
    from ..models import FriendPick
    pick = db.get(FriendPick, friend_pick_id)
    if not pick or not pick.match_id:
        return None
    settings = db.get(Settings, 1)
    books = ["bet365"]
    if settings and settings.sportsbooks_tracked:
        try:
            books = json.loads(settings.sportsbooks_tracked) or books
        except Exception:
            pass
    return simulate_signal(
        db, signal_source="FRIEND", signal_id=pick.id, match_id=pick.match_id,
        sportsbook=books[0], market="ML_3WAY", selection=pick.pick_side,
        signal_time=pick.effective_known_at, max_entry_decimal=pick.odds_at_pick_decimal,
    )


def resettle_all(db: Session) -> dict:
    """Recompute paper_pl for any FILLED trade whose match has since settled."""
    filled = db.scalars(select(PaperTrade).where(PaperTrade.settlement_status == "FILLED")).all()
    settled = 0
    for row in filled:
        match = db.get(Match, row.match_id) if row.match_id else None
        if match and match.home_score is not None and match.away_score is not None and match.winner:
            settings = db.get(Settings, 1)
            stake_usd = (settings.paper_stake_usd if settings else 100.0) * row.paper_stake
            won = match.winner == row.selection
            row.paper_pl_usd = round(stake_usd * (row.price_decimal - 1), 2) if won else round(-stake_usd, 2)
            row.settlement_status = "SETTLED"
            settled += 1
    if settled:
        db.commit()
    return {"newly_settled": settled}


def report(db: Session) -> dict:
    rows = db.scalars(select(PaperTrade)).all()
    by_delay: dict[int, dict] = {d: {"total": 0, "filled": 0, "missed": 0, "settled": 0,
                                     "pl_usd": [], "clv": []} for d in DELAYS_SECONDS}
    for r in rows:
        b = by_delay.setdefault(r.delay_seconds, {"total": 0, "filled": 0, "missed": 0,
                                                    "settled": 0, "pl_usd": [], "clv": []})
        b["total"] += 1
        if r.settlement_status == "MISSED_PRICE":
            b["missed"] += 1
        elif r.settlement_status in ("FILLED", "SETTLED"):
            b["filled"] += 1
        if r.settlement_status == "SETTLED":
            b["settled"] += 1
            if r.paper_pl_usd is not None:
                b["pl_usd"].append(r.paper_pl_usd)
        if r.proxy_clv_pct is not None:
            b["clv"].append(r.proxy_clv_pct)

    delay_summary = {}
    for d in DELAYS_SECONDS:
        b = by_delay.get(d, {"total": 0, "filled": 0, "missed": 0, "settled": 0, "pl_usd": [], "clv": []})
        delay_summary[str(d)] = {
            "total": b["total"],
            "fill_rate_pct": round(100 * b["filled"] / b["total"], 1) if b["total"] else None,
            "settled": b["settled"],
            "total_paper_pl_usd": round(sum(b["pl_usd"]), 2) if b["pl_usd"] else None,
            "avg_proxy_clv_pct": round(sum(b["clv"]) / len(b["clv"]), 2) if b["clv"] else None,
        }
    return {
        "disclaimer": DISCLAIMER,
        "total_trades": len(rows),
        "by_delay_seconds": delay_summary,
    }
