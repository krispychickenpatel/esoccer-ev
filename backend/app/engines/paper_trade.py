"""Paper Trade Engine (v0.3.6 Module 6, fixed in v0.3.6.2). No real betting.

For every signal (model or friend), this simulates entry at delays
0/5/10/20/30/45 seconds after the signal became known, using ONLY real
stored odds snapshots -- never an interpolated or fabricated price. If no
snapshot exists within 60s of the target time, the trade is MISSED_PRICE,
never a fabricated fill.

v0.3.6.2 root-cause fix: v0.3.6 gated MODEL eligibility on
`PredictionLedger.action == "BET"`. In real validation-mode data, the
model's combined EV+steam-probability bar has never fired (0 of 653
predictions have action="BET"), so that gate silently zeroed out model
paper trading regardless of how much real scored/reality-verified data
existed. Eligibility is now based on structural data sufficiency (match,
selection, signal time, a usable entry price) -- NOT on the model's own
internal action decision, and NOT on execution_mode (which is NULL for
every prediction that predates v0.3.6 and must not block simulation
either).

DISCLAIMER (must ship with every report): "Prices come from a reference feed
with an observed ~20-30s publication lag. Simulated fills are optimistic.
This is decay analysis, not execution proof."
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import FriendPick, Match, PaperTrade, PredictionLedger, Settings
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


def _model_max_entry(pred: PredictionLedger) -> float | None:
    """Fallback order (v0.3.6.2 Part A3): (1) the frozen maximum_entry_decimal
    if present, (2) the current/signal-time decimal odds already stored on
    the frozen row, (3) None (simulate_signal then treats any found price as
    surviving -- it never fabricates a price either way)."""
    if pred.maximum_entry_decimal is not None:
        return pred.maximum_entry_decimal
    return pred.current_decimal


def simulate_model_candidate(db: Session, prediction_id: int) -> list[PaperTrade] | None:
    """Simulate any structurally-complete frozen PredictionLedger row --
    NOT gated on action=='BET' or execution_mode. See module docstring for
    why: that gate zeroed out model paper trading entirely on real data."""
    pred = db.get(PredictionLedger, prediction_id)
    if not pred or not pred.match_id or not pred.selection or not pred.prediction_time:
        return None
    max_entry = _model_max_entry(pred)
    return simulate_signal(
        db, signal_source="MODEL", signal_id=pred.id, match_id=pred.match_id,
        sportsbook=pred.sportsbook, market=pred.market, selection=pred.selection,
        signal_time=pred.prediction_time, max_entry_decimal=max_entry,
    )


def simulate_friend_pick(db: Session, friend_pick_id: int) -> list[PaperTrade] | None:
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


def _model_eligibility(db: Session) -> dict:
    preds = db.scalars(select(PredictionLedger)).all()
    total = len(preds)
    scored = sum(1 for p in preds if p.status == "scored")
    existing_ids = {sid for (sid,) in db.execute(
        select(PaperTrade.signal_id).where(PaperTrade.signal_source == "MODEL").distinct()).all()}

    eligible = 0
    already_simulated = 0
    skip_reasons = {"missing_match_id": 0, "missing_selection": 0, "missing_signal_time": 0,
                    "missing_max_entry_decimal": 0}
    legacy_execution_mode_null = 0
    pending_result = 0
    for p in preds:
        if p.execution_mode is None:
            legacy_execution_mode_null += 1
        if p.status != "scored":
            pending_result += 1
        if p.id in existing_ids:
            already_simulated += 1
            continue
        reasons = []
        if not p.match_id:
            reasons.append("missing_match_id")
        if not p.selection:
            reasons.append("missing_selection")
        if not p.prediction_time:
            reasons.append("missing_signal_time")
        if p.maximum_entry_decimal is None and p.current_decimal is None:
            reasons.append("missing_max_entry_decimal")
        if reasons:
            for r in reasons:
                skip_reasons[r] += 1
        else:
            eligible += 1
    skipped = total - eligible - already_simulated
    expected_delay_rows = (eligible + already_simulated) * len(DELAYS_SECONDS)
    existing_delay_rows = db.scalar(select(func.count(PaperTrade.id)).where(
        PaperTrade.signal_source == "MODEL")) or 0
    return {
        "prediction_ledger_total": total,
        "scored_predictions": scored,
        "eligible_signals": eligible,
        "already_simulated_signals": already_simulated,
        "skipped_signals": skipped,
        "skip_reasons": skip_reasons,
        "expected_delay_rows": expected_delay_rows,
        "existing_delay_rows": existing_delay_rows,
        "missing_delay_rows": max(0, expected_delay_rows - existing_delay_rows),
        "note": ("Eligibility does NOT require action=='BET' or a non-null execution_mode -- "
                "both would incorrectly zero out legacy/validation-mode data. Tracked below "
                "for transparency only, never as a block."),
        "legacy_execution_mode_null_count": legacy_execution_mode_null,
        "pending_result_count": pending_result,
    }


def _friend_eligibility(db: Session) -> dict:
    picks = db.scalars(select(FriendPick)).all()
    total = len(picks)
    existing_ids = {sid for (sid,) in db.execute(
        select(PaperTrade.signal_id).where(PaperTrade.signal_source == "FRIEND").distinct()).all()}

    eligible = 0
    already_simulated = 0
    skip_reasons = {"not_resolved": 0, "missing_match_id": 0}
    for p in picks:
        if p.id in existing_ids:
            already_simulated += 1
            continue
        reasons = []
        if p.resolution_status != "RESOLVED":
            reasons.append("not_resolved")
        elif not p.match_id:
            reasons.append("missing_match_id")
        if reasons:
            for r in reasons:
                skip_reasons[r] += 1
        else:
            eligible += 1
    skipped = total - eligible - already_simulated
    expected_delay_rows = (eligible + already_simulated) * len(DELAYS_SECONDS)
    existing_delay_rows = len(db.scalars(select(PaperTrade).where(
        PaperTrade.signal_source == "FRIEND")).all())
    return {
        "friend_picks_total": total,
        "eligible_signals": eligible,
        "already_simulated_signals": already_simulated,
        "skipped_signals": skipped,
        "skip_reasons": skip_reasons,
        "expected_delay_rows": expected_delay_rows,
        "existing_delay_rows": existing_delay_rows,
        "missing_delay_rows": max(0, expected_delay_rows - existing_delay_rows),
    }


def eligibility_report(db: Session) -> dict:
    return {"model": _model_eligibility(db), "friend": _friend_eligibility(db)}


def simulate_all(db: Session) -> dict:
    """Bulk: simulate every structurally-eligible MODEL prediction and every
    RESOLVED friend pick not yet simulated. Idempotent -- running twice never
    duplicates rows; already-simulated signals are counted separately from
    newly-created ones."""
    model_existing_ids = {sid for (sid,) in db.execute(
        select(PaperTrade.signal_id).where(PaperTrade.signal_source == "MODEL").distinct()).all()}
    model_stats = {"eligible_signals": 0, "created_trades": 0, "existing_trades": 0,
                  "skipped_signals": 0, "skip_reasons": {}}
    for pred in db.scalars(select(PredictionLedger)).all():
        if pred.id in model_existing_ids:
            model_stats["existing_trades"] += len(DELAYS_SECONDS)
            continue
        if not pred.match_id or not pred.selection or not pred.prediction_time:
            model_stats["skipped_signals"] += 1
            reason = "missing_required_fields"
            model_stats["skip_reasons"][reason] = model_stats["skip_reasons"].get(reason, 0) + 1
            continue
        model_stats["eligible_signals"] += 1
        simulate_model_candidate(db, pred.id)
        model_stats["created_trades"] += len(DELAYS_SECONDS)

    friend_existing_ids = {sid for (sid,) in db.execute(
        select(PaperTrade.signal_id).where(PaperTrade.signal_source == "FRIEND").distinct()).all()}
    friend_stats = {"eligible_signals": 0, "created_trades": 0, "existing_trades": 0,
                   "skipped_signals": 0, "skip_reasons": {}}
    for pick in db.scalars(select(FriendPick).where(FriendPick.resolution_status == "RESOLVED")).all():
        if pick.id in friend_existing_ids:
            friend_stats["existing_trades"] += len(DELAYS_SECONDS)
            continue
        if not pick.match_id:
            friend_stats["skipped_signals"] += 1
            reason = "missing_match_id"
            friend_stats["skip_reasons"][reason] = friend_stats["skip_reasons"].get(reason, 0) + 1
            continue
        friend_stats["eligible_signals"] += 1
        simulate_friend_pick(db, pick.id)
        friend_stats["created_trades"] += len(DELAYS_SECONDS)

    resettle_all(db)
    return {
        "disclaimer": DISCLAIMER,
        "model": model_stats,
        "friend": friend_stats,
        "delay_buckets": DELAYS_SECONDS,
        # backward-compat top-level fields (v0.3.6 shape)
        "model_signals_simulated": model_stats["eligible_signals"],
        "friend_signals_simulated": friend_stats["eligible_signals"],
        "model_trades_created": model_stats["created_trades"],
        "friend_trades_created": friend_stats["created_trades"],
    }


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


def _empty_delay_bucket() -> dict:
    return {d: {"total": 0, "filled": 0, "missed": 0, "settled": 0, "pl_usd": [], "clv": []}
            for d in DELAYS_SECONDS}


def _summarize_delay_bucket(bucket: dict) -> dict:
    out = {}
    for d in DELAYS_SECONDS:
        b = bucket.get(d) or {"total": 0, "filled": 0, "missed": 0, "settled": 0, "pl_usd": [], "clv": []}
        out[str(d)] = {
            "total": b["total"],
            "fill_rate_pct": round(100 * b["filled"] / b["total"], 1) if b["total"] else None,
            "missed_price_count": b["missed"],
            "settled": b["settled"],
            "total_paper_pl_usd": round(sum(b["pl_usd"]), 2) if b["pl_usd"] else None,
            "avg_proxy_clv_pct": round(sum(b["clv"]) / len(b["clv"]), 2) if b["clv"] else None,
        }
    return out


def report(db: Session) -> dict:
    """v0.3.6.2: MODEL and FRIEND are reported separately (by_source) so a
    handful of friend rows can never be mistaken for model execution
    evidence. Top-level by_delay_seconds is kept as a combined view for
    backward compatibility."""
    rows = db.scalars(select(PaperTrade)).all()
    by_source: dict[str, dict] = {"MODEL": _empty_delay_bucket(), "FRIEND": _empty_delay_bucket()}
    combined = _empty_delay_bucket()

    for r in rows:
        src_bucket = by_source.setdefault(r.signal_source, _empty_delay_bucket())
        for bucket in (src_bucket, combined):
            b = bucket.setdefault(r.delay_seconds, {"total": 0, "filled": 0, "missed": 0,
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

    model_total = sum(b["total"] for b in by_source.get("MODEL", {}).values())
    friend_total = sum(b["total"] for b in by_source.get("FRIEND", {}).values())

    return {
        "disclaimer": DISCLAIMER,
        "total_trades": len(rows),
        "model_trades": model_total,
        "friend_trades": friend_total,
        "by_source": {
            "MODEL": {"total_trades": model_total, "by_delay_seconds": _summarize_delay_bucket(by_source.get("MODEL", {}))},
            "FRIEND": {"total_trades": friend_total, "by_delay_seconds": _summarize_delay_bucket(by_source.get("FRIEND", {}))},
        },
        "by_delay_seconds": _summarize_delay_bucket(combined),
    }
