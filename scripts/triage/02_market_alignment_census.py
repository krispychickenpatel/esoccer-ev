#!/usr/bin/env python3
"""v0.3.7A Gate G2 -- Market alignment census. READ-ONLY, SELECT-only."""
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "backend" / "esoccer.db"


def main():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    out = {}

    cur.execute("SELECT market, COUNT(*) c FROM odds_snapshots GROUP BY market ORDER BY c DESC")
    out["odds_snapshots_market_distribution"] = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT market, COUNT(*) c FROM prediction_ledger GROUP BY market ORDER BY c DESC")
    out["prediction_ledger_market_distribution"] = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT market, COUNT(*) c FROM paper_trades GROUP BY market ORDER BY c DESC")
    out["paper_trades_market_distribution"] = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT selection, COUNT(*) c FROM odds_snapshots GROUP BY selection ORDER BY c DESC")
    out["odds_snapshots_selection_distribution"] = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT selection, COUNT(*) c FROM prediction_ledger GROUP BY selection ORDER BY c DESC")
    out["prediction_ledger_selection_distribution"] = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT selection, COUNT(*) c FROM paper_trades GROUP BY selection ORDER BY c DESC")
    out["paper_trades_selection_distribution"] = [dict(r) for r in cur.fetchall()]

    # Does any match actually settle as a draw?
    cur.execute("SELECT COUNT(*) c FROM matches WHERE winner='draw'")
    out["matches_settled_draw"] = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM matches WHERE winner IS NOT NULL")
    out["matches_settled_total"] = cur.fetchone()["c"]

    # Paper trades whose selection == 'draw'
    cur.execute("SELECT COUNT(*) c FROM paper_trades WHERE selection='draw'")
    out["paper_trades_selection_draw_count"] = cur.fetchone()["c"]

    # How do draw-selection paper trades settle when the match actually IS a draw?
    cur.execute("""
        SELECT pt.id, pt.signal_source, pt.selection, pt.settlement_status, pt.paper_pl_usd,
               m.winner, m.home_score, m.away_score
        FROM paper_trades pt JOIN matches m ON m.id = pt.match_id
        WHERE pt.selection = 'draw'
        LIMIT 20
    """)
    out["draw_selection_paper_trade_samples"] = [dict(r) for r in cur.fetchall()]

    cur.execute("""
        SELECT pt.id, pt.settlement_status, pt.paper_pl_usd, m.winner
        FROM paper_trades pt JOIN matches m ON m.id = pt.match_id
        WHERE m.winner = 'draw'
        LIMIT 20
    """)
    out["paper_trades_for_matches_that_drew"] = [dict(r) for r in cur.fetchall()]

    # Market-string alignment: for each PaperTrade row, does its stored
    # `market` match the market of the PredictionLedger row it came from
    # (for MODEL signals), and is the same market string used consistently
    # for both the entry-window snapshot lookup and the "closing" lookup
    # (both go through the same market column on OddsSnapshot -- confirm no
    # cross-market comparison is possible by construction).
    cur.execute("""
        SELECT pt.id as paper_trade_id, pt.market as paper_trade_market,
               pl.market as prediction_market, pt.signal_id, pt.selection, pl.selection as pred_selection
        FROM paper_trades pt JOIN prediction_ledger pl ON pl.id = pt.signal_id
        WHERE pt.signal_source = 'MODEL'
    """)
    rows = cur.fetchall()
    mismatches = [dict(r) for r in rows if r["paper_trade_market"] != r["prediction_market"]
                 or r["selection"] != r["pred_selection"]]
    out["model_paper_trade_vs_prediction_market_selection_mismatches"] = mismatches
    out["model_paper_trade_vs_prediction_checked"] = len(rows)

    # Confirm the OddsSnapshot query used for entry vs "closing" price lookup
    # (execution_pricing.price_at_delay / latest_snapshot_for) both filter
    # on the identical (match_id, sportsbook, market, selection) tuple --
    # this can't be verified by SQL alone (it's a code-level guarantee), but
    # we CAN verify that both market strings that exist per match are
    # internally self-consistent (no snapshot row has a market value outside
    # the known set).
    cur.execute("SELECT DISTINCT market FROM odds_snapshots")
    out["distinct_market_strings_in_odds_snapshots"] = [r["market"] for r in cur.fetchall()]

    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
