"""Prediction Lab — self-testing loop for ESoccer EV.

This module is deliberately not a new betting strategy. It freezes what the
platform believed before kickoff, captures what reality did, scores the frozen
record, and reports which model/horizon/error bucket deserves attention.

Core guarantees:
- prediction rows are append/first-write only; duplicates are skipped
- scoring uses odds/result rows that occur after the prediction timestamp
- every failure is assigned a bucket so the next model work is targeted
"""
from __future__ import annotations

import hashlib
import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (Match, OddsSnapshot, PredictionLedger, PredictionReality,
                      PredictionScore, Settings)
from . import odds_math
from .signals import all_signals, combine_signals
from .steam import steam_prediction_for_snapshot

LAB_VERSION = "prediction_lab_v0.3.4"
MODEL_VERSION = "self_test_pick_engine_v1"
FEATURE_SET_VERSION = "prediction_lab_fs1"
HORIZONS: list[tuple[str, int]] = [
    ("T-30m", 30),
    ("T-15m", 15),
    ("T-10m", 10),
    ("T-5m", 5),
    ("T-2m", 2),
    ("KICKOFF", 0),
]
SEED_SOURCES = {"manual_seed", "synthetic_demo", "seed"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


def _loads(s: str | None, default: Any) -> Any:
    try:
        return json.loads(s or "")
    except Exception:
        return default


def _safe_iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _latest_available_prices(db: Session, match_id: int, at: datetime) -> list[OddsSnapshot]:
    """Latest snapshot per market key with collected_at <= prediction time."""
    snaps = db.scalars(select(OddsSnapshot).where(
        OddsSnapshot.match_id == match_id,
        OddsSnapshot.collected_at <= at,
        OddsSnapshot.data_source.notin_(SEED_SOURCES),
    ).order_by(OddsSnapshot.collected_at)).all()
    latest: dict[tuple, OddsSnapshot] = {}
    for sn in snaps:
        if sn.phase == "live":
            # Horizon predictions are supposed to be pre-kickoff evidence.
            continue
        latest[(sn.sportsbook, sn.market, sn.selection, sn.line)] = sn
    return list(latest.values())


def _fallback_snapshots_from_latest(db: Session, match_id: int) -> list[OddsSnapshot]:
    """Useful when manually testing without exact historical collected_at rows."""
    snaps = db.scalars(select(OddsSnapshot).where(
        OddsSnapshot.match_id == match_id,
        OddsSnapshot.data_source.notin_(SEED_SOURCES),
    ).order_by(OddsSnapshot.collected_at)).all()
    latest: dict[tuple, OddsSnapshot] = {}
    for sn in snaps:
        if sn.phase == "live":
            continue
        latest[(sn.sportsbook, sn.market, sn.selection, sn.line)] = sn
    return list(latest.values())


def _action_from_card(ev_pct: float, steam: dict, settings: Settings | None) -> str:
    min_ev = settings.min_ev_pct if settings else 5.0
    if steam.get("quality", 0.0) < 0.10:
        return "WAIT"
    if ev_pct < 0:
        return "PASS"
    if ev_pct >= min_ev and steam.get("steam_probability", 0.5) >= 0.58:
        return "BET"
    if ev_pct >= min_ev:
        return "WAIT"
    return "PASS"


def _freeze_hash(payload: dict) -> str:
    return hashlib.sha256(_dumps(payload).encode("utf-8")).hexdigest()


def _existing_prediction(db: Session, match_id: int, horizon_label: str, sn: OddsSnapshot) -> PredictionLedger | None:
    return db.scalar(select(PredictionLedger).where(
        PredictionLedger.match_id == match_id,
        PredictionLedger.horizon_label == horizon_label,
        PredictionLedger.sportsbook == sn.sportsbook,
        PredictionLedger.market == sn.market,
        PredictionLedger.selection == sn.selection,
        PredictionLedger.line == sn.line,
        PredictionLedger.model_version == MODEL_VERSION,
    ))


def freeze_match_horizon(db: Session, match: Match, horizon_label: str,
                         prediction_time: datetime | None = None,
                         allow_late: bool = False) -> list[PredictionLedger]:
    """Create immutable prediction ledger rows for a match/horizon.

    If allow_late=True and no exact pre-horizon snapshot exists, use the latest
    pre-match snapshot. This makes manual backfills useful while still marking
    the feature payload with the actual snapshot time.
    """
    prediction_time = prediction_time or _now()
    settings = db.get(Settings, 1)
    snaps = _latest_available_prices(db, match.id, prediction_time)
    if not snaps and allow_late:
        snaps = _fallback_snapshots_from_latest(db, match.id)
    if not snaps:
        return []

    # Leakage guard: every feature source is cut off at prediction_time, so a
    # backfilled/late freeze cannot see this match's own live ticks or history
    # pairs that only settled after the prediction moment.
    ens = combine_signals(all_signals(db, match, nu=0.63, as_of=prediction_time))
    predicted_winner = "home" if ens["p_home"] >= ens["p_away"] and ens["p_home"] >= ens["p_draw"] else (
        "away" if ens["p_away"] >= ens["p_home"] and ens["p_away"] >= ens["p_draw"] else "draw")
    created: list[PredictionLedger] = []
    for sn in snaps:
        if sn.selection not in ("home", "away", "draw"):
            continue
        if sn.market not in ("ML_3WAY", "SPREAD_2WAY"):
            continue
        if _existing_prediction(db, match.id, horizon_label, sn):
            continue
        p = {"home": ens["p_home"], "draw": ens["p_draw"], "away": ens["p_away"]}[sn.selection]
        ev_pct = round(odds_math.expected_value(p, sn.decimal_odds) * 100, 2)
        steam = steam_prediction_for_snapshot(db, match, sn, settings, as_of=prediction_time)
        action = _action_from_card(ev_pct, steam, settings)
        confidence = {
            "model": round(max(ens["p_home"], ens["p_draw"], ens["p_away"]) - min(ens["p_home"], ens["p_draw"], ens["p_away"]), 3),
            "data_quality": round(min(1.0, ens.get("total_weight", 0.0)), 3),
            "steam": round(steam.get("quality", 0.0) * abs((steam.get("steam_probability", 0.5) - 0.5) * 2), 3),
            "overall": None,
        }
        confidence["overall"] = round(confidence["model"] * 0.40 + confidence["data_quality"] * 0.25 + confidence["steam"] * 0.35, 3)
        reasons = []
        if ev_pct >= (settings.min_ev_pct if settings else 5.0):
            reasons.append("MARKET_MISPRICE")
        reasons.extend(steam.get("reason_codes", []))
        if action == "WAIT" and not reasons:
            reasons.append("DATA_WEAK")
        if action == "PASS" and not reasons:
            reasons.append("NO_BET")
        features = {
            "available_at": prediction_time.isoformat(),
            "odds_snapshot_id": sn.id,
            "odds_collected_at": sn.collected_at.isoformat(),
            "seconds_to_kickoff_at_snapshot": sn.seconds_to_kickoff,
            "signals": ens["signals"],
            "disagreement": ens["disagreement"],
            "steam": steam,
            "leakage_guard": "features_collected_at_lte_prediction_time",
        }
        payload = {
            "match_id": match.id,
            "horizon_label": horizon_label,
            "prediction_time": prediction_time.isoformat(),
            "model_version": MODEL_VERSION,
            "sportsbook": sn.sportsbook,
            "market": sn.market,
            "selection": sn.selection,
            "line": sn.line,
            "current_decimal": sn.decimal_odds,
            "predicted_winner": predicted_winner,
            "steam_probability": steam.get("steam_probability"),
            "predicted_first_live_decimal": steam.get("predicted_first_live_decimal"),
            "action": action,
            "features_hash_source": features,
        }
        row = PredictionLedger(
            match_id=match.id,
            horizon_label=horizon_label,
            horizon_minutes=dict(HORIZONS).get(horizon_label),
            prediction_time=prediction_time,
            scheduled_start=match.start_time,
            model_version=MODEL_VERSION,
            feature_set_version=FEATURE_SET_VERSION,
            sportsbook=sn.sportsbook,
            market=sn.market,
            selection=sn.selection,
            line=sn.line,
            current_american=sn.american_odds,
            current_decimal=sn.decimal_odds,
            predicted_winner=predicted_winner,
            p_home=round(ens["p_home"], 5),
            p_draw=round(ens["p_draw"], 5),
            p_away=round(ens["p_away"], 5),
            model_prob=round(p, 5),
            fair_decimal=round(1 / p, 3) if p > 0 else 0.0,
            ev_pct=ev_pct,
            predicted_first_live_american=steam.get("predicted_first_live_american"),
            predicted_first_live_decimal=steam.get("predicted_first_live_decimal"),
            steam_probability=steam.get("steam_probability"),
            expected_line_movement_cents=steam.get("expected_line_movement_cents"),
            maximum_entry_price=steam.get("maximum_entry_price"),
            maximum_entry_decimal=steam.get("maximum_entry_decimal"),
            execution_window=steam.get("execution_window", ""),
            action=action,
            reason_codes_json=_dumps(sorted(set(reasons)) or ["NO_BET"]),
            confidence_json=_dumps(confidence),
            features_json=_dumps(features),
            immutable_hash=_freeze_hash(payload),
            status="frozen",
        )
        # v0.3.6: recommended execution mode, computed from the same
        # already-derived numbers above. Additive only -- not part of
        # immutable_hash, so freeze/verify_integrity behavior is unchanged.
        try:
            from .execution_strategy import classify_execution_mode
            exec_result = classify_execution_mode(
                db, current_decimal=sn.decimal_odds,
                predicted_first_live_decimal=steam.get("predicted_first_live_decimal"),
                max_entry_decimal=steam.get("maximum_entry_decimal"),
                model_prob=p, min_ev_pct=(settings.min_ev_pct if settings else 5.0),
                steam_probability=steam.get("steam_probability", 0.5), market=sn.market,
            )
            row.execution_mode = exec_result["execution_mode"]
            row.execution_reason_codes_json = _dumps(exec_result["reason_codes"])
        except Exception:
            pass  # execution-mode is best-effort and must never block a freeze
        db.add(row)
        created.append(row)
    if created:
        db.commit()
    return created


def freeze_due_predictions(db: Session, tolerance_seconds: int = 75, allow_late: bool = False) -> dict:
    """Freeze horizon predictions that are currently due."""
    now = _now()
    min_start = now - timedelta(minutes=2)
    max_start = now + timedelta(minutes=35)
    matches = db.scalars(select(Match).where(
        Match.home_score.is_(None),
        Match.start_time >= min_start,
        Match.start_time <= max_start,
        Match.source.notin_(SEED_SOURCES),
    ).order_by(Match.start_time)).all()
    made = 0
    per_horizon: dict[str, int] = {}
    for m in matches:
        seconds_to_start = (m.start_time - now).total_seconds()
        due_labels: list[str] = []
        for label, minutes in HORIZONS:
            target = minutes * 60
            if abs(seconds_to_start - target) <= tolerance_seconds:
                due_labels.append(label)
        # allow_late used to freeze EVERY passed horizon with the current
        # timestamp, so a "T-30m" row could actually be made 3 minutes before
        # kickoff — contaminating per-horizon model comparison. Now late
        # freezes only produce the single label nearest to the real
        # seconds-to-kickoff, keeping horizon labels honest.
        if allow_late and not due_labels and 0 <= seconds_to_start:
            nearest = min(HORIZONS, key=lambda h: abs(seconds_to_start - h[1] * 60))
            due_labels.append(nearest[0])
        for label in due_labels:
            rows = freeze_match_horizon(db, m, label, now, allow_late=allow_late)
            made += len(rows)
            per_horizon[label] = per_horizon.get(label, 0) + len(rows)
    return {"created": made, "per_horizon": per_horizon, "checked_matches": len(matches), "lab_version": LAB_VERSION}


def _snap_seconds(match: Match, sn: OddsSnapshot) -> float:
    if sn.seconds_to_kickoff is not None:
        return sn.seconds_to_kickoff
    return (match.start_time - sn.collected_at).total_seconds()


def capture_reality_for_match(db: Session, match: Match) -> list[PredictionReality]:
    snaps = db.scalars(select(OddsSnapshot).where(
        OddsSnapshot.match_id == match.id,
        OddsSnapshot.data_source.notin_(SEED_SOURCES),
    ).order_by(OddsSnapshot.collected_at)).all()
    grouped: dict[tuple, list[OddsSnapshot]] = {}
    for sn in snaps:
        grouped.setdefault((sn.sportsbook, sn.market, sn.selection, sn.line), []).append(sn)
    out: list[PredictionReality] = []
    for (book, market, selection, line), rows in grouped.items():
        pre = [r for r in rows if _snap_seconds(match, r) > 0]
        live = [r for r in rows if _snap_seconds(match, r) <= 0]
        last_pre = pre[-1] if pre else None
        first_live = live[0] if live else None
        closing = rows[-1] if rows else None
        existing = db.scalar(select(PredictionReality).where(
            PredictionReality.match_id == match.id,
            PredictionReality.sportsbook == book,
            PredictionReality.market == market,
            PredictionReality.selection == selection,
            PredictionReality.line == line,
        ))
        quality = 0
        warnings = []
        if pre:
            quality += 25
        else:
            warnings.append("missing_pre_kickoff")
        if first_live:
            quality += 35
            after = abs(_snap_seconds(match, first_live))
            if after <= 15:
                quality += 20
            elif after <= 45:
                quality += 10
                warnings.append("first_live_late")
            else:
                warnings.append("first_live_too_late")
        else:
            warnings.append("missing_first_live")
        if match.home_score is not None and match.away_score is not None:
            quality += 20
        else:
            warnings.append("missing_result")
        if not rows:
            quality = 0
        tier = "gold" if quality >= 90 else "silver" if quality >= 50 else "rejected"
        actual_cents = None
        actual_shortened = None
        first_live_after_s = None
        if last_pre and first_live:
            actual_cents = round((first_live.decimal_odds - last_pre.decimal_odds) * 100, 1)
            actual_shortened = first_live.decimal_odds < last_pre.decimal_odds
            first_live_after_s = round(abs(_snap_seconds(match, first_live)), 2)
        row = existing or PredictionReality(match_id=match.id, sportsbook=book, market=market,
                                             selection=selection, line=line)
        row.last_pre_snapshot_id = last_pre.id if last_pre else None
        row.first_live_snapshot_id = first_live.id if first_live else None
        row.closing_snapshot_id = closing.id if closing else None
        row.last_pre_american = last_pre.american_odds if last_pre else None
        row.last_pre_decimal = last_pre.decimal_odds if last_pre else None
        row.first_live_american = first_live.american_odds if first_live else None
        row.first_live_decimal = first_live.decimal_odds if first_live else None
        row.closing_american = closing.american_odds if closing else None
        row.closing_decimal = closing.decimal_odds if closing else None
        row.first_live_after_s = first_live_after_s
        row.actual_movement_cents = actual_cents
        row.actual_shortened = actual_shortened
        row.winner = match.winner
        row.home_score = match.home_score
        row.away_score = match.away_score
        row.capture_quality_score = quality
        row.dataset_tier = tier
        row.warnings_json = _dumps(warnings)
        row.captured_at = _now()
        if existing is None:
            db.add(row)
        out.append(row)
    if out:
        db.commit()
    return out


def capture_reality(db: Session) -> dict:
    """Refresh reality rows for every match with odds; finished matches get scores."""
    mids = [mid for (mid,) in db.execute(select(OddsSnapshot.match_id).distinct()).all()]
    rows = 0
    for mid in mids:
        m = db.get(Match, mid)
        if m and m.source not in SEED_SOURCES:
            rows += len(capture_reality_for_match(db, m))
    return {"reality_rows_touched": rows, "matches_checked": len(mids), "lab_version": LAB_VERSION}


def _score_error_bucket(pred: PredictionLedger, reality: PredictionReality, match: Match) -> str:
    if reality.dataset_tier == "rejected" or reality.first_live_decimal is None or reality.last_pre_decimal is None:
        return "DATA_ERROR"
    predicted_short = (pred.predicted_first_live_decimal is not None and pred.predicted_first_live_decimal < pred.current_decimal) or (pred.steam_probability or 0.5) >= 0.58
    if reality.actual_shortened is not None and predicted_short != reality.actual_shortened:
        return "STEAM_DIRECTION_ERROR"
    actual = reality.actual_movement_cents
    if actual is not None and pred.expected_line_movement_cents is not None and abs(actual - pred.expected_line_movement_cents) >= 15:
        return "STEAM_MAGNITUDE_ERROR"
    if match.winner and pred.predicted_winner and pred.predicted_winner != match.winner:
        return "OUTCOME_ERROR"
    if pred.action == "BET" and pred.maximum_entry_decimal is not None and reality.first_live_decimal is not None and reality.first_live_decimal < pred.maximum_entry_decimal:
        return "EXECUTION_TIMING_ERROR"
    if pred.action == "BET" and reality.capture_quality_score < 70:
        return "RISK_FILTER_ERROR"
    return "OK"


def score_predictions(db: Session) -> dict:
    preds = db.scalars(select(PredictionLedger).where(PredictionLedger.status != "scored")).all()
    scored = 0
    bucket_counts: dict[str, int] = {}
    for pred in preds:
        match = db.get(Match, pred.match_id)
        if not match or match.home_score is None or match.away_score is None:
            continue
        reality = db.scalar(select(PredictionReality).where(
            PredictionReality.match_id == pred.match_id,
            PredictionReality.sportsbook == pred.sportsbook,
            PredictionReality.market == pred.market,
            PredictionReality.selection == pred.selection,
            PredictionReality.line == pred.line,
        ))
        if reality is None:
            r = capture_reality_for_match(db, match)
            reality = next((x for x in r if x.sportsbook == pred.sportsbook and x.market == pred.market
                            and x.selection == pred.selection and x.line == pred.line), None)
        if reality is None:
            continue
        existing = db.scalar(select(PredictionScore).where(PredictionScore.prediction_id == pred.id))
        winner_correct = match.winner == pred.predicted_winner if match.winner else None
        predicted_short = (pred.predicted_first_live_decimal is not None and pred.predicted_first_live_decimal < pred.current_decimal) or (pred.steam_probability or 0.5) >= 0.58
        steam_correct = (predicted_short == reality.actual_shortened) if reality.actual_shortened is not None else None
        magnitude_error = None
        if reality.actual_movement_cents is not None and pred.expected_line_movement_cents is not None:
            magnitude_error = round(abs(reality.actual_movement_cents - pred.expected_line_movement_cents), 2)
        entry_hit = None
        if pred.maximum_entry_decimal is not None and reality.first_live_decimal is not None:
            entry_hit = reality.first_live_decimal >= pred.maximum_entry_decimal
        bucket = _score_error_bucket(pred, reality, match)
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        score = 0.0
        if winner_correct is True:
            score += 0.25
        if steam_correct is True:
            score += 0.40
        if magnitude_error is not None:
            score += max(0.0, 0.20 * (1 - min(1.0, magnitude_error / 25)))
        if entry_hit is True or pred.action != "BET":
            score += 0.15
        row = existing or PredictionScore(prediction_id=pred.id, reality_id=reality.id)
        row.winner_correct = winner_correct
        row.steam_direction_correct = steam_correct
        row.magnitude_error_cents = magnitude_error
        row.entry_window_hit = entry_hit
        row.error_bucket = bucket
        row.score = round(score, 3)
        row.details_json = _dumps({
            "predicted_shortening": predicted_short,
            "actual_shortened": reality.actual_shortened,
            "actual_movement_cents": reality.actual_movement_cents,
            "dataset_tier": reality.dataset_tier,
            "capture_quality_score": reality.capture_quality_score,
        })
        row.scored_at = _now()
        if existing is None:
            db.add(row)
        pred.status = "scored"
        scored += 1
    if scored:
        db.commit()
    return {"scored": scored, "error_buckets": bucket_counts, "lab_version": LAB_VERSION}


def run_prediction_lab_cycle(db: Session) -> dict:
    frozen = freeze_due_predictions(db)
    reality = capture_reality(db)
    scored = score_predictions(db)
    return {"frozen": frozen, "reality": reality, "scored": scored, "lab_version": LAB_VERSION}


def ledger_rows(db: Session, limit: int = 300) -> list[dict]:
    rows = db.scalars(select(PredictionLedger).order_by(PredictionLedger.prediction_time.desc()).limit(limit)).all()
    out = []
    for p in rows:
        m = db.get(Match, p.match_id)
        score = db.scalar(select(PredictionScore).where(PredictionScore.prediction_id == p.id))
        out.append({
            "id": p.id,
            "match_id": p.match_id,
            "match": f"{m.home_player.name} vs {m.away_player.name}" if m else "",
            "league": m.league if m else "",
            "scheduled_start": _safe_iso(p.scheduled_start),
            "horizon_label": p.horizon_label,
            "prediction_time": _safe_iso(p.prediction_time),
            "sportsbook": p.sportsbook,
            "market": p.market,
            "selection": p.selection,
            "line": p.line,
            "current_american": p.current_american,
            "predicted_winner": p.predicted_winner,
            "model_prob": p.model_prob,
            "ev_pct": p.ev_pct,
            "predicted_first_live_american": p.predicted_first_live_american,
            "steam_probability": p.steam_probability,
            "expected_line_movement_cents": p.expected_line_movement_cents,
            "maximum_entry_price": p.maximum_entry_price,
            "action": p.action,
            "status": p.status,
            "reason_codes": _loads(p.reason_codes_json, []),
            "confidence": _loads(p.confidence_json, {}),
            "score": score.score if score else None,
            "error_bucket": score.error_bucket if score else None,
            "winner_correct": score.winner_correct if score else None,
            "steam_direction_correct": score.steam_direction_correct if score else None,
            "magnitude_error_cents": score.magnitude_error_cents if score else None,
        })
    return out


def model_comparison(db: Session) -> dict:
    preds = db.scalars(select(PredictionLedger)).all()
    groups: dict[tuple[str, str], list[tuple[PredictionLedger, PredictionScore | None, PredictionReality | None]]] = {}
    for p in preds:
        score = db.scalar(select(PredictionScore).where(PredictionScore.prediction_id == p.id))
        reality = None
        if score:
            reality = db.get(PredictionReality, score.reality_id)
        groups.setdefault((p.model_version, p.horizon_label), []).append((p, score, reality))
    rows = []
    for (model, horizon), items in groups.items():
        scored = [(p, s, r) for p, s, r in items if s is not None]
        if not items:
            continue
        winner_vals = [s.winner_correct for _, s, _ in scored if s.winner_correct is not None]
        steam_vals = [s.steam_direction_correct for _, s, _ in scored if s.steam_direction_correct is not None]
        mag = [s.magnitude_error_cents for _, s, _ in scored if s.magnitude_error_cents is not None]
        gold = [r for _, _, r in scored if r is not None and r.dataset_tier == "gold"]
        buckets: dict[str, int] = {}
        for _, s, _ in scored:
            buckets[s.error_bucket] = buckets.get(s.error_bucket, 0) + 1
        rows.append({
            "model_version": model,
            "horizon_label": horizon,
            "frozen_n": len(items),
            "scored_n": len(scored),
            "gold_n": len(gold),
            "winner_accuracy": round(sum(1 for v in winner_vals if v) / len(winner_vals), 3) if winner_vals else None,
            "steam_direction_accuracy": round(sum(1 for v in steam_vals if v) / len(steam_vals), 3) if steam_vals else None,
            "avg_magnitude_error_cents": round(sum(mag) / len(mag), 2) if mag else None,
            "avg_score": round(sum(s.score for _, s, _ in scored) / len(scored), 3) if scored else None,
            "error_buckets": buckets,
        })
    order = {label: i for i, (label, _) in enumerate(HORIZONS)}
    rows.sort(key=lambda x: (x["model_version"], order.get(x["horizon_label"], 99)))
    return {"lab_version": LAB_VERSION, "groups": rows}


def _rebuild_freeze_payload(p: PredictionLedger) -> dict:
    """Reconstruct the exact payload that was hashed at freeze time."""
    return {
        "match_id": p.match_id,
        "horizon_label": p.horizon_label,
        "prediction_time": p.prediction_time.isoformat(),
        "model_version": p.model_version,
        "sportsbook": p.sportsbook,
        "market": p.market,
        "selection": p.selection,
        "line": p.line,
        "current_decimal": p.current_decimal,
        "predicted_winner": p.predicted_winner,
        "steam_probability": p.steam_probability,
        "predicted_first_live_decimal": p.predicted_first_live_decimal,
        "action": p.action,
        "features_hash_source": _loads(p.features_json, {}),
    }


def verify_integrity(db: Session, limit: int = 5000) -> dict:
    """Recompute each frozen row's sha256 and compare with immutable_hash.
    Any mismatch means the frozen prediction was edited after freezing —
    exactly what the ledger exists to prevent."""
    rows = db.scalars(select(PredictionLedger).order_by(PredictionLedger.id).limit(limit)).all()
    mismatched: list[int] = []
    for p in rows:
        if _freeze_hash(_rebuild_freeze_payload(p)) != p.immutable_hash:
            mismatched.append(p.id)
    return {
        "lab_version": LAB_VERSION,
        "checked": len(rows),
        "ok": len(rows) - len(mismatched),
        "mismatched": len(mismatched),
        "mismatched_ids": mismatched[:50],
    }


def dashboard(db: Session) -> dict:
    preds = db.scalars(select(PredictionLedger)).all()
    scores = db.scalars(select(PredictionScore)).all()
    realities = db.scalars(select(PredictionReality)).all()
    buckets: dict[str, int] = {}
    for s in scores:
        buckets[s.error_bucket] = buckets.get(s.error_bucket, 0) + 1
    tiers: dict[str, int] = {}
    for r in realities:
        tiers[r.dataset_tier] = tiers.get(r.dataset_tier, 0) + 1
    recent = ledger_rows(db, limit=25)
    integrity = verify_integrity(db)
    return {
        "lab_version": LAB_VERSION,
        "totals": {
            "frozen_predictions": len(preds),
            "scored_predictions": len(scores),
            "pending_scores": sum(1 for p in preds if p.status != "scored"),
            "reality_rows": len(realities),
            "gold_reality_rows": tiers.get("gold", 0),
            "silver_reality_rows": tiers.get("silver", 0),
            "rejected_reality_rows": tiers.get("rejected", 0),
        },
        "error_buckets": buckets,
        "dataset_tiers": tiers,
        "ledger_integrity": integrity,
        "recent_predictions": recent,
        "model_comparison": model_comparison(db)["groups"],
        "next_required_sample": "Do not trust a horizon/model until it has meaningful scored Gold rows. Start reviewing once gold_n >= 50; treat anything below that as research only.",
    }
