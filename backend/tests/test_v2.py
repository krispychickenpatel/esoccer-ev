"""v2 unit tests — identity, pick ranking, CSV v2 validation, movement buckets,
Wilson intervals, pick grading, poller cadence. Pure-function focused."""
from datetime import datetime

from app.connectors import csv_v2
from app.engines.identity import canonical_name
from app.engines.movement import bucket_for
from app.engines.pick_engine import rank_score
from app.engines.research import wilson
from app.services.poller import cadence_seconds


# ---------------------------------------------------------------- identity
def test_canonical_name_variants():
    assert canonical_name("Arsenal (CRUSADER)") == "CRUSADER"
    assert canonical_name("CRUSADER_") == "CRUSADER"
    assert canonical_name("Crusader") == "CRUSADER"
    assert canonical_name("Newcastle UTD (ALIBI)") == "ALIBI"
    assert canonical_name("Kray") == "KRAY"
    # never merges different operators
    assert canonical_name("Spurs (BLITZ)") != canonical_name("Spurs (ALIBI)")


# ---------------------------------------------------------------- rank score
def test_rank_score_monotonic_in_ev():
    low = rank_score(2, .5, .5, 0, .5, 0, 1)
    high = rank_score(12, .5, .5, 0, .5, 0, 1)
    assert high > low


def test_rank_score_bounds():
    assert rank_score(0, 0, 0, -1, 0, -50, 0) == 0
    assert rank_score(20, 1, 1, 1, 1, 30, 1) == 100


def test_rank_score_limit_feasibility_penalty():
    full = rank_score(8, .6, .6, 0, .5, 5, 1.0)
    limited = rank_score(8, .6, .6, 0, .5, 5, 0.1)
    assert full - limited > 4  # loses most of the 5-pt feasibility term


# ---------------------------------------------------------------- csv v2
BETS_CSV = """bet_id,date_time_placed,sportsbook,sport,league,market,selection,opponent,line,american_odds,decimal_odds,stake,result,payout,profit,closing_american_odds,notes,screenshot_file
b1,2026-05-13 13:50,FanDuel,ESoccer,X,Moneyline 3-way,Spurs (CRUSADER),Newcastle UTD (ALIBI),,-135,1.7407,550,win,957.41,407.41,,ok,shot.png
b2,2026-05-13 13:50,FanDuel,ESoccer,X,Spread,A,B,-0.5,notanumber,,100,win,,,,,
b3,2026-05-13 13:50,FanDuel,ESoccer,X,ML,A,B,,+120,9.99,100,badresult,,,,,
"""


def test_parse_bets_rows_errors_warnings():
    rows, errors, warnings = csv_v2.parse_bets(BETS_CSV)
    assert len(rows) == 1 and rows[0]["ext_id"] == "b1"
    assert rows[0]["market"] == "ML_3WAY"
    assert len(errors) == 2                      # bad odds + bad result, with row numbers
    assert all("row " in e for e in errors)
    assert not warnings or all("row" in w for w in warnings)


def test_parse_bets_decimal_mismatch_warns():
    csv = ("bet_id,date_time_placed,sportsbook,market,selection,american_odds,"
           "decimal_odds,stake,result\n"
           "b9,2026-01-01 10:00,FD,ML_3WAY,A,+100,3.50,10,open\n")
    rows, errors, warnings = csv_v2.parse_bets(csv)
    assert len(rows) == 1 and not errors
    assert any("decimal_odds" in w for w in warnings)
    assert rows[0]["decimal_odds"] == 2.0        # derived wins


def test_parse_matches_winner_and_score_conflict():
    csv = ("match_id,start_time,league,home_player,away_player,home_score,away_score,winner,source\n"
           "m1,2026-01-01 10:00,L,A,B,2,1,away,x\n"      # conflict -> score wins, warn
           "m2,2026-01-01 11:00,L,A,B,,,home,x\n")        # winner-only OK (D5)
    rows, errors, warnings = csv_v2.parse_matches(csv)
    assert not errors and len(rows) == 2
    assert rows[0]["winner"] == "home" and any("disagrees" in w for w in warnings)
    assert rows[1]["winner"] == "home" and rows[1]["home_score"] is None


def test_parse_recommendations_markets_normalized():
    csv = ("recommendation_id,created_at,source,league,scheduled_start,home_player,"
           "away_player,recommended_selection,acceptable_markets,max_spread,"
           "min_american_odds,ideal_american_odds,expires_at,confidence,notes\n"
           'r1,2026-05-13 12:41,friend,L,2026-05-13 13:01,H,A,H,"Moneyline 3-way; Spread",'
           "-0.5,,210,2026-05-13 13:01:30,high,n\n")
    rows, errors, _ = csv_v2.parse_recommendations(csv)
    assert not errors
    assert set(eval(rows[0]["acceptable_markets"].replace("null", "None"))) >= {"ML_3WAY", "SPREAD_2WAY"} or \
           '"ML_3WAY"' in rows[0]["acceptable_markets"]


def test_parse_executions_latency_derived():
    csv = ("execution_id,recommendation_id,sportsbook,opened_at,live_detected_at,"
           "bet_placed_at,actual_market,actual_line,actual_american_odds,stake,"
           "accepted_odds_movement,was_within_window,latency_seconds,status,notes\n"
           "e1,r1,FD,2026-05-13 13:49,2026-05-13 13:50:00,2026-05-13 13:50:07,"
           "ML_3WAY,,-135,550,true,,,placed,n\n")
    rows, errors, _ = csv_v2.parse_executions(csv)
    assert not errors and rows[0]["latency_seconds"] == 7.0


# ---------------------------------------------------------------- movement
def test_bucket_for_windows():
    assert bucket_for(300) == "pre_match"
    assert bucket_for(0.0) == "live_0_10s"       # kickoff instant = live (matches poller)
    assert bucket_for(-5) == "live_0_10s"
    assert bucket_for(-20) == "live_10_30s"
    assert bucket_for(-90) == "live_30s_plus"
    assert bucket_for(None) == "pre_match"


# ---------------------------------------------------------------- wilson
def test_wilson_small_sample_is_wide():
    lo, hi = wilson(10, 10)                      # 10/10 like the seed screenshots
    assert lo < 0.75                             # can't claim >75% floor from n=10
    lo2, hi2 = wilson(100, 100)
    assert lo2 > lo                              # more evidence narrows it


# ---------------------------------------------------------------- cadence
def test_cadence_table():
    """D17: recalibrated to fit 3600 req/hr BetsAPI budget (was 3.7x over)."""
    assert cadence_seconds(700) == 9999    # >10min: single opening pull, no repeat
    assert cadence_seconds(300) == 60      # 2-10min out
    assert cadence_seconds(60) == 10       # <2min to kickoff
    assert cadence_seconds(-10) == 2       # 0-30s live: still catches the first-live jump
    assert cadence_seconds(-120) == 120    # tail: one closing/CLV pull


# ---------------------------------------------------------------- grading
class _P:  # minimal Pick stand-in
    def __init__(self, **kw):
        self.settled_result = kw.get("settled_result")
        self.clv_pct = kw.get("clv_pct")
        self.ev_pct = kw.get("ev_pct", 5)
        self.status = kw.get("status", "BET")
        self.reason_codes = kw.get("reason_codes", "[]")
        self.user_decision = kw.get("user_decision")


def test_grades():
    from app.engines.research import grade_pick
    assert grade_pick(_P(settled_result="win", clv_pct=2.0)) == "A+"
    assert grade_pick(_P(settled_result="loss", clv_pct=1.5, ev_pct=9)) == "A"
    assert grade_pick(_P(settled_result="win", clv_pct=-1.0)) == "B"
    assert grade_pick(_P(settled_result="loss", clv_pct=-3.0)) == "D"
