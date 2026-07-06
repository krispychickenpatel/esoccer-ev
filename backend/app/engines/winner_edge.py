"""Winner Edge Truth Layer (v0.3.6.2 Part B).

Steam, CLV, and market movement are supporting evidence. The actual thesis:
we need to predict the winning side at a playable price often enough to
beat the market after delay, vig, book availability, and execution
friction. This module directly evaluates that, separating four questions
that are easy to conflate:

    Winner accuracy = can we pick the side?
    Price edge      = are we getting paid enough for the risk?
    Execution       = can we actually enter (paper trade fill rate)?
    Profit          = does flat-stake paper P/L survive delay?

Leakage rules (enforced throughout, see tests):
- prediction side comes from the frozen PredictionLedger row only.
- pre-kick price/probability comes from snapshots at-or-before the
  prediction's own prediction_time (or the friend pick's effective_known_at)
  -- never anything later.
- delay price/P&L comes ONLY from existing PaperTrade rows (the Paper Trade
  engine), never recomputed here.
- result is only read from Match.winner, which is only set after settlement.
- repeated horizons of the same (match_id, selection) are deduplicated to
  one sample (same rule as profit_gates.signal_gate) for winner-accuracy
  and baseline metrics. Paper-trade-derived execution metrics (fill rate,
  ROI, CLV by delay) are computed over ALL simulated paper trades, since
  each horizon represents a genuinely distinct hypothetical entry time,
  not a repeated observation of the same fact -- this is a deliberate,
  documented distinction from the dedup rule above, not an oversight.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FriendPick, FriendPickScore, Match, OddsSnapshot, PaperTrade, PredictionLedger
from . import odds_math
from .friend_picks import favorite_selection
from .paper_trade import DELAYS_SECONDS

ODDS_BUCKETS = [("<1.50", None, 1.50), ("1.50-1.80", 1.50, 1.80), ("1.80-2.20", 1.80, 2.20),
               ("2.20-3.00", 2.20, 3.00), (">3.00", 3.00, None)]
CALIBRATION_BUCKETS = [("0-40%", 0.0, 0.40), ("40-50%", 0.40, 0.50), ("50-60%", 0.50, 0.60),
                       ("60-70%", 0.60, 0.70), ("70%+", 0.70, 1.01)]

MIN_SAMPLE_WARNING = 30


def _odds_bucket(decimal_odds: float) -> str:
    for label, lo, hi in ODDS_BUCKETS:
        if lo is None and decimal_odds < hi:
            return label
        if hi is None and decimal_odds >= lo:
            return label
        if lo is not None and hi is not None and lo <= decimal_odds < hi:
            return label
    return "unknown"


def _calibration_bucket(prob: float) -> str:
    for label, lo, hi in CALIBRATION_BUCKETS:
        if lo <= prob < hi:
            return label
    return "unknown"


def _devigged_prob(db: Session, match_id: int, sportsbook: str, market: str,
                   selection: str, at: datetime) -> float | None:
    """Best-effort de-vig using sibling-selection snapshots at-or-before
    `at`. Never uses data after `at` -- leakage guard."""
    snaps = db.scalars(select(OddsSnapshot).where(
        OddsSnapshot.match_id == match_id, OddsSnapshot.sportsbook == sportsbook,
        OddsSnapshot.market == market, OddsSnapshot.collected_at <= at,
    ).order_by(OddsSnapshot.collected_at.desc())).all()
    latest: dict[str, OddsSnapshot] = {}
    for s in snaps:
        latest.setdefault(s.selection, s)
    if selection not in latest or len(latest) < 2:
        return None
    implied = {sel: odds_math.implied_prob(sn.decimal_odds) for sel, sn in latest.items()}
    fair = odds_math.remove_vig(list(implied.values()))
    fair_by_sel = dict(zip(implied.keys(), fair))
    return fair_by_sel.get(selection)


def _model_samples(db: Session) -> list[dict]:
    """One row per distinct (match_id, selection) -- latest prediction_time
    wins ties. Never uses match.winner/reality data to pick WHICH row wins
    the tie (that's purely prediction_time, known at freeze time), only to
    later ANNOTATE the outcome, so this cannot leak future info into sample
    selection (required: 'do not use final result to filter picks')."""
    preds = db.scalars(select(PredictionLedger)).all()
    best: dict[tuple[int, str], PredictionLedger] = {}
    for p in preds:
        key = (p.match_id, p.selection)
        if key not in best or p.prediction_time > best[key].prediction_time:
            best[key] = p
    out = []
    for (match_id, selection), p in best.items():
        match = db.get(Match, match_id)
        if not match:
            continue
        has_result = match.home_score is not None and match.away_score is not None and bool(match.winner)
        scored = p.status == "scored" and has_result
        winner_correct = (p.predicted_winner == match.winner) if scored else None
        fav = favorite_selection(db, match_id, p.sportsbook, p.market, p.prediction_time)
        favorite_correct = (fav == match.winner) if (scored and fav) else None
        devig = _devigged_prob(db, match_id, p.sportsbook, p.market, selection, p.prediction_time)
        out.append({
            "prediction_id": p.id, "match_id": match_id, "selection": selection,
            "prediction_time": p.prediction_time, "current_decimal": p.current_decimal,
            "model_prob": p.model_prob,
            "market_implied_prob": round(odds_math.implied_prob(p.current_decimal), 4),
            "devigged_prob": round(devig, 4) if devig is not None else None,
            "predicted_winner": p.predicted_winner,
            "actual_winner": match.winner if scored else None,
            "winner_correct": winner_correct, "favorite_correct": favorite_correct,
            "scored": scored, "league": match.league,
        })
    return out


def _paper_trades_for_source(db: Session, source: str) -> list[PaperTrade]:
    return db.scalars(select(PaperTrade).where(PaperTrade.signal_source == source)).all()


def _delay_execution_metrics(trades: list[PaperTrade]) -> dict:
    by_delay: dict[int, dict] = {d: {"total": 0, "filled": 0, "missed": 0, "settled": 0,
                                     "pl_usd": [], "clv": []} for d in DELAYS_SECONDS}
    for t in trades:
        b = by_delay.setdefault(t.delay_seconds, {"total": 0, "filled": 0, "missed": 0,
                                                    "settled": 0, "pl_usd": [], "clv": []})
        b["total"] += 1
        if t.settlement_status == "MISSED_PRICE":
            b["missed"] += 1
        elif t.settlement_status in ("FILLED", "SETTLED"):
            b["filled"] += 1
        if t.settlement_status == "SETTLED":
            b["settled"] += 1
            if t.paper_pl_usd is not None:
                b["pl_usd"].append(t.paper_pl_usd)
        if t.proxy_clv_pct is not None:
            b["clv"].append(t.proxy_clv_pct)

    out = {}
    for d in DELAYS_SECONDS:
        b = by_delay[d]
        total_pl = sum(b["pl_usd"]) if b["pl_usd"] else None
        out[str(d)] = {
            "total": b["total"],
            "fill_rate_pct": round(100 * b["filled"] / b["total"], 1) if b["total"] else None,
            "missed_price_rate_pct": round(100 * b["missed"] / b["total"], 1) if b["total"] else None,
            "settled": b["settled"],
            "total_paper_pl_usd": round(total_pl, 2) if total_pl is not None else None,
            "avg_proxy_clv_pct": round(sum(b["clv"]) / len(b["clv"]), 2) if b["clv"] else None,
        }
    return out


def _roi_by_delay(db: Session, trades: list[PaperTrade]) -> dict:
    """ROI% = 100 * total paper P/L (USD) / total USD actually staked across
    settled trades at that delay. Dollar stake per trade is
    paper_stake (units) * Settings.paper_stake_usd -- the same value
    simulate_signal() used to compute paper_pl_usd in the first place, so
    this is a true percent return, not a per-unit P/L average."""
    from ..models import Settings
    settings = db.get(Settings, 1)
    stake_usd_per_unit = (settings.paper_stake_usd if settings else 100.0) or 100.0

    by_delay: dict[int, list[PaperTrade]] = {d: [] for d in DELAYS_SECONDS}
    for t in trades:
        if t.settlement_status == "SETTLED" and t.delay_seconds in by_delay:
            by_delay[t.delay_seconds].append(t)
    out = {}
    for d in DELAYS_SECONDS:
        rows = by_delay[d]
        if not rows:
            out[str(d)] = None
            continue
        total_pl = sum(t.paper_pl_usd or 0.0 for t in rows)
        total_staked = sum((t.paper_stake or 1.0) * stake_usd_per_unit for t in rows)
        out[str(d)] = round(100 * total_pl / total_staked, 2) if total_staked else None
    return out


def model_report(db: Session) -> dict:
    samples = _model_samples(db)
    total_predictions = len(db.scalars(select(PredictionLedger)).all())
    scored_predictions = len(db.scalars(select(PredictionLedger).where(
        PredictionLedger.status == "scored")).all())

    scored_samples = [s for s in samples if s["scored"]]
    n = len(scored_samples)

    winner_acc = None
    fav_acc = None
    margin = None
    if n:
        w = [s["winner_correct"] for s in scored_samples if s["winner_correct"] is not None]
        f = [s["favorite_correct"] for s in scored_samples if s["favorite_correct"] is not None]
        winner_acc = round(100 * sum(w) / len(w), 1) if w else None
        fav_acc = round(100 * sum(f) / len(f), 1) if f else None
        if winner_acc is not None and fav_acc is not None:
            margin = round(winner_acc - fav_acc, 1)

    underdog_n = sum(1 for s in scored_samples
                     if s["market_implied_prob"] is not None and s["market_implied_prob"] < 0.5)
    favorite_n = n - underdog_n

    odds_bucket_acc: dict[str, dict] = {}
    for s in scored_samples:
        if s["current_decimal"] is None or s["winner_correct"] is None:
            continue
        label = _odds_bucket(s["current_decimal"])
        b = odds_bucket_acc.setdefault(label, {"n": 0, "correct": 0})
        b["n"] += 1
        b["correct"] += int(s["winner_correct"])
    odds_bucket_out = {label: {"n": b["n"], "accuracy_pct": round(100 * b["correct"] / b["n"], 1) if b["n"] else None}
                       for label, b in odds_bucket_acc.items()}

    brier = None
    calibration: dict[str, dict] = {}
    prob_samples = [s for s in scored_samples if s["model_prob"] is not None and s["winner_correct"] is not None]
    if prob_samples:
        sq_errors = []
        for s in prob_samples:
            outcome = 1.0 if s["winner_correct"] else 0.0
            sq_errors.append((s["model_prob"] - outcome) ** 2)
            label = _calibration_bucket(s["model_prob"])
            b = calibration.setdefault(label, {"n": 0, "correct": 0, "avg_predicted_prob": []})
            b["n"] += 1
            b["correct"] += int(s["winner_correct"])
            b["avg_predicted_prob"].append(s["model_prob"])
        brier = round(sum(sq_errors) / len(sq_errors), 4)
        for label, b in calibration.items():
            calibration[label] = {
                "n": b["n"],
                "actual_win_rate_pct": round(100 * b["correct"] / b["n"], 1) if b["n"] else None,
                "avg_predicted_prob_pct": round(100 * sum(b["avg_predicted_prob"]) / len(b["avg_predicted_prob"]), 1),
            }

    model_edges = [s["model_prob"] - (s["devigged_prob"] if s["devigged_prob"] is not None else s["market_implied_prob"])
                   for s in scored_samples if s["model_prob"] is not None]
    avg_model_edge = round(100 * sum(model_edges) / len(model_edges), 2) if model_edges else None

    trades = _paper_trades_for_source(db, "MODEL")
    execution = _delay_execution_metrics(trades)
    roi = _roi_by_delay(db, trades)

    warnings = []
    if n < MIN_SAMPLE_WARNING:
        warnings.append(f"distinct_samples={n} is below the {MIN_SAMPLE_WARNING}-sample warning threshold -- "
                        "treat every percentage above as directional only, not statistically reliable.")
    if not trades:
        warnings.append("No MODEL paper trades exist yet -- run POST /api/paper-trades/simulate-all. "
                        "Execution/ROI/CLV metrics below are all null until then.")

    return {
        "total_predictions": total_predictions,
        "scored_predictions": scored_predictions,
        "distinct_samples": n,
        "winner_accuracy_pct": winner_acc,
        "favorite_baseline_accuracy_pct": fav_acc,
        "margin_vs_favorite_pts": margin,
        "favorite_side_n": favorite_n,
        "underdog_side_n": underdog_n,
        "odds_bucket_accuracy": odds_bucket_out,
        "avg_model_edge_pct": avg_model_edge,
        "brier_score": brier,
        "calibration_buckets": calibration,
        "paper_pl_and_fill_by_delay_seconds": execution,
        "roi_pct_by_delay_seconds": roi,
        "sample_warnings": warnings,
    }


def friend_report(db: Session) -> dict:
    picks = db.scalars(select(FriendPick)).all()
    total = len(picks)
    clean_pre_kick = sum(1 for p in picks if not p.is_backfilled)
    backfilled = sum(1 for p in picks if p.is_backfilled)
    likely_test = sum(1 for p in picks if p.kickoff_time
                      and (p.created_at - p.kickoff_time).total_seconds() > 3600)
    scores = {s.friend_pick_id: s for s in db.scalars(select(FriendPickScore)).all()}
    scored_rows = [(p, scores[p.id]) for p in picks if p.id in scores]
    n = len(scored_rows)

    winner_acc = None
    fav_acc = None
    if n:
        w = [s.winner_correct for _, s in scored_rows if s.winner_correct is not None]
        winner_acc = round(100 * sum(w) / len(w), 1) if w else None
        fav_correct = []
        for p, s in scored_rows:
            if not p.match_id:
                continue
            m = db.get(Match, p.match_id)
            if not m or not m.winner:
                continue
            fav = favorite_selection(db, p.match_id, "bet365", "ML_3WAY", p.effective_known_at)
            if fav:
                fav_correct.append(fav == m.winner)
        fav_acc = round(100 * sum(fav_correct) / len(fav_correct), 1) if fav_correct else None

    odds_bucket_acc: dict[str, dict] = {}
    for p, s in scored_rows:
        if s.winner_correct is None:
            continue
        label = _odds_bucket(p.odds_at_pick_decimal)
        b = odds_bucket_acc.setdefault(label, {"n": 0, "correct": 0})
        b["n"] += 1
        b["correct"] += int(s.winner_correct)
    odds_bucket_out = {label: {"n": b["n"], "accuracy_pct": round(100 * b["correct"] / b["n"], 1) if b["n"] else None}
                       for label, b in odds_bucket_acc.items()}

    trades = _paper_trades_for_source(db, "FRIEND")
    execution = _delay_execution_metrics(trades)
    roi = _roi_by_delay(db, trades)

    warnings = []
    if n < 30:
        warnings.append(f"scored={n} is below the 30-sample warning threshold -- not statistically reliable.")
    if likely_test:
        warnings.append(f"{likely_test} pick(s) look like test artifacts (entered >60min after kickoff) -- "
                        "not excluded automatically, but should be reviewed before trusting these numbers.")

    return {
        "total_picks": total,
        "clean_pre_kick_picks": clean_pre_kick,
        "backfilled_picks": backfilled,
        "likely_test_artifacts": likely_test,
        "scored_picks": n,
        "winner_accuracy_pct": winner_acc,
        "favorite_baseline_accuracy_pct": fav_acc,
        "odds_bucket_accuracy": odds_bucket_out,
        "paper_pl_and_fill_by_delay_seconds": execution,
        "roi_pct_by_delay_seconds": roi,
        "book_proxy_caveat": ("Scoring uses reference-feed (bet365) odds regardless of the book the "
                             "friend actually saw -- see is_reference_feed_proxy on each pick."),
        "sample_warnings": warnings,
    }


def winner_edge_report(db: Session) -> dict:
    return {
        "framing": {
            "winner_accuracy": "Can we pick the side?",
            "price_edge": "Are we getting paid enough for the risk (model_prob vs market/devigged prob)?",
            "execution": "Can we actually enter (paper trade fill rate by delay)?",
            "profit": "Does flat-stake paper P/L survive delay (ROI by delay)?",
        },
        "warning": "Winner edge is not profit unless price and execution survive.",
        "model": model_report(db),
        "friend": friend_report(db),
    }
