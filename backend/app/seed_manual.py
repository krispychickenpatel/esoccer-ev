"""Manual seed data — reconstructed from user's screenshots/messages.

Everything here is data_source='manual_seed', verification_status='seed_partial'.
It exists so the app is usable on day one and so timing schemas match reality.
It is NOT clean API data and is never used as proof of profitability (spec).
Idempotent: keyed on ext_id; safe to call on every startup.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .engines.identity import resolve_player
from .engines.odds_math import american_to_decimal
from .models import Bet, Match, Recommendation

DS = dict(data_source="manual_seed", verification_status="seed_partial")

# [Likely] User confirmed 2026-07: friend's picks come from "eSoccer H2H GG
# League 2x4mins" -- two 4-min halves = 8 min total, matching BetsAPI's
# "Esoccer H2H GG League - 8 mins play" exactly. Not independently verified
# as the identical BetsAPI league entity (no literal string match performed),
# but strong enough to replace the generic "FanDuel ESoccer" placeholder that
# existed only because the real league was unknown at reconstruction time.
# If this is ever contradicted (friend confirms a different league by name),
# revert this constant and re-flag every seed row -- don't just patch quietly.
FRIEND_LEAGUE = "Esoccer H2H GG League - 8 mins play"


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


RECS = [
    # ext_id, created, scheduled, home, away, selection, markets, max_spread,
    # min_odds, ideal, expires, confidence, notes
    ("rec_001", "2026-05-13 12:41", "2026-05-13 13:01", "Newcastle UTD (VIRUS)",
     "Arsenal (CRUSADER)", "Arsenal (CRUSADER)", '["ML_3WAY","SPREAD_2WAY"]', -0.5,
     None, 210, "2026-05-13 13:01:30", "high",
     "Friend said 1:01, Crusader ML -0.5. Screenshot: Arsenal CRUSADER ML +210, spread +0.5/-154 pre-live."),
    ("rec_002", "2026-05-13 13:38", "2026-05-13 13:50", "Spurs (CRUSADER)",
     "Newcastle UTD (ALIBI)", "Spurs (CRUSADER)", '["ML_3WAY","SPREAD_2WAY"]', -0.5,
     None, -135, "2026-05-13 13:50:30", "high",
     "Settled screenshot later: two $550 bets both won (ML and -0.5, ~-135)."),
    ("rec_003", "2026-06-10 13:21", "2026-06-10 13:20", "Spurs (PHENOM)",
     "Unknown", "Spurs (PHENOM)", '["ML_3WAY"]', None, None, None,
     "2026-06-10 13:21:30", "medium",
     "Live > Soccer > scroll > Spurs PHENOM ML, $1000, games now. App update issue — may have missed."),
    ("rec_004", "2026-06-11 13:28", "2026-06-11 13:31", "Ambassador", "Alibi",
     "Ambassador", '["ML_3WAY","SPREAD_2WAY"]', -0.5, None, None,
     "2026-06-11 13:31:30", "high",
     "Ambassador vs Alibi 1:31 PM, AMBASSADOR ML spread up to -0.5. Place $1000 when live."),
    ("rec_005", "2026-06-23 10:25", "2026-06-23 10:35", "Aston Villa (PROPHET)",
     "Spurs (ALIBI)", "Aston Villa (PROPHET)", '["ML_3WAY"]', None, None, 160,
     "2026-06-23 10:35:30", "high",
     "Screenshot: PROPHET ML +160, 10:35 ET, MAX WAGER $78.15 seen on $1000 attempt."),
    ("rec_006", "2026-06-23 13:13", "2026-06-23 13:20", "Norway (ALIBI)",
     "Portugal (BLITZ)", "Portugal (BLITZ)", '["ML_3WAY","SPREAD_2WAY"]', -0.5,
     None, 100, "2026-06-23 13:20:30", "high",
     "Screenshot: BLITZ ML +100 and -0.5 +102. Place after start; line changes when live; to win $200."),
    ("rec_007", "2026-06-24 12:29", "2026-06-24 13:20", "Unknown", "Unknown",
     "Unknown", '[]', None, None, None, "2026-06-24 13:20:30", "medium",
     "Friend: '1:20, text me at 1:15 for the line.' Line withheld until near kickoff."),
    ("rec_008", "2026-06-30 13:20", "2026-06-30 13:46", "Tottenham (BLITZ)",
     "Arsenal (ALIBI)", "Tottenham (BLITZ)", '["ML_3WAY","SPREAD_2WAY"]', -0.5,
     None, None, "2026-06-30 13:46:30", "high",
     "BLITZ ML, spread up to -0.5; later 'spread doesn't matter, either or.'"),
    ("rec_009", "2026-07-01 12:40", "2026-07-01 13:01", "Unknown", "Unknown",
     "Unknown", '[]', None, None, None, "2026-07-01 13:01:30", "medium",
     "Friend asked 1:01 pm, later 'can you run it'. Need exact slip from bet history."),
]

# (bet ext_id, rec ext_id, placed, market, selection, opponent, line, american,
#  stake, payout, profit, note)
BETS = [
    ("seed_bet_001", "rec_002", "2026-05-13 13:50", "ML_3WAY", "Spurs (CRUSADER)",
     "Newcastle UTD (ALIBI)", None, -135, 550, 957.41, 407.41, "Settled screenshot; one of two same-match bets."),
    ("seed_bet_002", "rec_002", "2026-05-13 13:50", "SPREAD_2WAY", "Spurs (CRUSADER)",
     "Newcastle UTD (ALIBI)", -0.5, -135, 550, 957.41, 407.41, "Settled screenshot; spread leg won."),
    ("seed_bet_003", "rec_004", "2026-06-11 13:31", "ML_3WAY", "Newcastle UTD (AMBASSADOR)",
     "Aston Villa (ALIBI)", None, -110, 1000, 1909.09, 909.09,
     "Settled screenshot. NOTE (D14): team skins differ from rec_004 narrative "
     "('Ambassador vs Alibi') — preserved as written; correct via Seed Review."),
    ("seed_bet_004", "rec_006", "2026-06-23 13:20", "ML_3WAY", "Portugal (BLITZ)",
     "Norway (ALIBI)", None, 100, 200, 400, 200, "Settled screenshot."),
    ("seed_bet_005", "rec_006", "2026-06-23 13:20", "SPREAD_2WAY", "Portugal (BLITZ)",
     "Norway (ALIBI)", -0.5, -105, 210, 410, 200, "Settled screenshot."),
    ("seed_bet_006", None, "2026-06-24 13:20", "ML_3WAY", "Morocco (BLITZ)",
     "Qatar (ALIBI)", None, 120, 166, 365.20, 199.20, "Settled screenshot (matches rec_007 window)."),
    ("seed_bet_007", "rec_008", "2026-06-30 13:46", "ML_3WAY", "Spurs (BLITZ)",
     "Arsenal (ALIBI)", None, 125, 100, 225, 125, "Settled screenshot."),
    ("seed_bet_008", "rec_008", "2026-06-30 13:46", "SPREAD_2WAY", "Spurs (BLITZ)",
     "Arsenal (ALIBI)", -0.5, 120, 96.15, 211.53, 115.38, "Settled screenshot."),
    ("seed_bet_009", "rec_009", "2026-07-01 13:01", "ML_3WAY", "Arsenal (CRUSADER)",
     "Spurs (ALIBI)", None, -140, 280, 480, 200, "Settled screenshot."),
    ("seed_bet_010", "rec_009", "2026-07-01 13:01", "SPREAD_2WAY", "Arsenal (CRUSADER)",
     "Spurs (ALIBI)", -0.5, -135, 270, 470, 200, "Settled screenshot."),
]


def load_manual_seed(db: Session) -> dict:
    created = {"recommendations": 0, "bets": 0, "matches": 0}
    rec_by_ext: dict[str, Recommendation] = {}

    for (ext, created_at, sched, home, away, sel, mkts, max_sp, min_o, ideal,
         exp, conf, notes) in RECS:
        r = db.scalar(select(Recommendation).where(Recommendation.ext_id == ext))
        if not r:
            r = Recommendation(
                ext_id=ext, source_name="friend", received_at=_dt(created_at),
                scheduled_start=_dt(sched), league=FRIEND_LEAGUE,
                home_name=home, away_name=away, recommended_selection=sel,
                acceptable_markets=mkts, max_spread=max_sp,
                min_american_odds=min_o, ideal_american_odds=ideal,
                expires_at=_dt(exp), confidence_label=conf, sportsbook="FanDuel",
                notes=notes, screenshot_ref="screenshot_seed", status="settled", **DS)
            if ext == "rec_005":
                r.limit_seen = 78.15
                r.status = "rejected"  # limit blocked the $1000 attempt
            db.add(r)
            db.flush()
            created["recommendations"] += 1
        rec_by_ext[ext] = r

    for (ext, rec_ext, placed, market, sel, opp, line, am, stake, payout,
         profit, note) in BETS:
        if db.scalar(select(Bet).where(Bet.ext_id == ext)):
            continue
        placed_dt = _dt(placed)
        hp = resolve_player(db, sel, league=FRIEND_LEAGUE, **DS)
        ap = resolve_player(db, opp, league=FRIEND_LEAGUE, **DS)
        match = None
        if hp and ap:
            match = db.scalar(select(Match).where(
                Match.home_player_id == hp.id, Match.away_player_id == ap.id,
                Match.start_time == placed_dt))
            if not match:
                match = Match(start_time=placed_dt, league=FRIEND_LEAGUE,
                              home_player_id=hp.id, away_player_id=ap.id,
                              winner="home",  # every seed bet won on the selection side
                              source="manual_seed", verification_status="seed_partial")
                db.add(match)
                db.flush()
                created["matches"] += 1
        rec = rec_by_ext.get(rec_ext) if rec_ext else None
        db.add(Bet(
            ext_id=ext, placed_at=placed_dt, sportsbook="FanDuel",
            league=FRIEND_LEAGUE, match_id=match.id if match else None,
            match_label=f"{sel} vs {opp}", selection=sel, opponent=opp,
            market=market, line=line, american_odds=am,
            decimal_odds=round(american_to_decimal(am), 4), stake=stake,
            result="win", payout=payout, profit=profit,
            recommendation_id=rec.id if rec else None,
            notes=note, screenshot_ref="screenshot_seed", **DS))
        created["bets"] += 1

    db.commit()
    return created
