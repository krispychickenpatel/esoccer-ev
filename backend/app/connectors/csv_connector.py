"""CSV connector: parses verified CSV import formats.
Raises ValueError with row numbers on bad data instead of silently skipping.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from ..engines import odds_math
from .base import Connector

MATCH_COLS = ["ext_id", "start_time", "league", "home_player", "away_player",
              "home_score", "away_score", "ht_home_score", "ht_away_score", "duration_min"]
ODDS_COLS = ["ext_id", "sportsbook", "market", "selection", "line",
             "american_odds", "collected_at", "is_opening", "is_closing"]
BET_COLS = ["placed_at", "sportsbook", "league", "match_label", "selection", "opponent",
            "market", "line", "american_odds", "stake", "result", "closing_american_odds", "notes"]


def _f(v):  # optional float
    return float(v) if v not in (None, "",) else None


def _i(v):  # optional int
    return int(v) if v not in (None, "",) else None


def _b(v):
    return str(v).strip().lower() in ("1", "true", "yes", "y")


class CSVConnector(Connector):
    name = "csv"

    def __init__(self, matches_text: str = "", odds_text: str = ""):
        self.matches_text = matches_text
        self.odds_text = odds_text

    def fetch_matches(self, since=None) -> list[dict]:
        out = []
        reader = csv.DictReader(io.StringIO(self.matches_text))
        for i, row in enumerate(reader, start=2):
            try:
                out.append({
                    "ext_id": row.get("ext_id") or None,
                    "start_time": datetime.fromisoformat(row["start_time"]),
                    "league": row.get("league", ""),
                    "home_player": row["home_player"].strip(),
                    "away_player": row["away_player"].strip(),
                    "home_score": _i(row.get("home_score")),
                    "away_score": _i(row.get("away_score")),
                    "ht_home_score": _i(row.get("ht_home_score")),
                    "ht_away_score": _i(row.get("ht_away_score")),
                    "duration_min": _i(row.get("duration_min")),
                    "source": "csv",
                })
            except (KeyError, ValueError) as e:
                raise ValueError(f"match_results row {i}: {e}") from e
        return out

    def fetch_odds(self, since=None) -> list[dict]:
        out = []
        reader = csv.DictReader(io.StringIO(self.odds_text))
        for i, row in enumerate(reader, start=2):
            try:
                american = int(row["american_odds"])
                out.append({
                    "ext_id": row["ext_id"],
                    "sportsbook": row["sportsbook"].strip(),
                    "market": row.get("market", "ML_3WAY").strip(),
                    "selection": row["selection"].strip().lower(),
                    "line": _f(row.get("line")),
                    "american_odds": american,
                    "decimal_odds": round(odds_math.american_to_decimal(american), 4),
                    "collected_at": datetime.fromisoformat(row["collected_at"]),
                    "is_opening": _b(row.get("is_opening")),
                    "is_closing": _b(row.get("is_closing")),
                })
            except (KeyError, ValueError) as e:
                raise ValueError(f"odds_snapshots row {i}: {e}") from e
        return out


def parse_bets_csv(text: str) -> list[dict]:
    out = []
    reader = csv.DictReader(io.StringIO(text))
    for i, row in enumerate(reader, start=2):
        try:
            american = int(row["american_odds"])
            out.append({
                "placed_at": datetime.fromisoformat(row["placed_at"]),
                "sportsbook": row.get("sportsbook", ""),
                "league": row.get("league", ""),
                "match_label": row.get("match_label", ""),
                "selection": row.get("selection", ""),
                "opponent": row.get("opponent", ""),
                "market": row.get("market", "ML_3WAY"),
                "line": _f(row.get("line")),
                "american_odds": american,
                "decimal_odds": round(odds_math.american_to_decimal(american), 4),
                "stake": float(row["stake"]),
                "result": row.get("result", "open").strip().lower(),
                "closing_american_odds": _i(row.get("closing_american_odds")),
                "notes": row.get("notes", ""),
            })
        except (KeyError, ValueError) as e:
            raise ValueError(f"bet_history row {i}: {e}") from e
    return out
