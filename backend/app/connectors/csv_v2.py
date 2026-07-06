"""v2 CSV import layer (spec: Importer requirements).

- New spec templates are canonical; v0.1 headers accepted as aliases (D10).
- Row-level validation with row numbers; nothing silently dropped.
- parse_* return (rows, errors, warnings) so routers can dry-run/preview.
- Derived fields (decimal odds, implied prob) are recomputed; if the CSV supplies
  them and they disagree beyond tolerance, that's a warning with the raw value
  preserved in the row dict.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime

from ..engines import odds_math

TEMPLATES = {
    "bet_history": ["bet_id", "date_time_placed", "sportsbook", "sport", "league",
                    "market", "selection", "opponent", "line", "american_odds",
                    "decimal_odds", "stake", "result", "payout", "profit",
                    "closing_american_odds", "notes", "screenshot_file"],
    "match_results": ["match_id", "start_time", "league", "home_player",
                      "away_player", "home_score", "away_score", "winner", "source"],
    "odds_snapshots": ["snapshot_id", "match_id", "timestamp", "sportsbook",
                       "market", "selection", "line", "american_odds",
                       "decimal_odds", "implied_probability"],
    "recommendations": ["recommendation_id", "created_at", "source", "league",
                        "scheduled_start", "home_player", "away_player",
                        "recommended_selection", "acceptable_markets", "max_spread",
                        "min_american_odds", "ideal_american_odds", "expires_at",
                        "confidence", "notes"],
    "execution_log": ["execution_id", "recommendation_id", "sportsbook", "opened_at",
                      "live_detected_at", "bet_placed_at", "actual_market",
                      "actual_line", "actual_american_odds", "stake",
                      "accepted_odds_movement", "was_within_window",
                      "latency_seconds", "status", "notes"],
}

# old header -> new header (backward compat, D10)
ALIASES = {
    "placed_at": "date_time_placed", "ext_id": "match_id",
    "collected_at": "timestamp", "screenshot_ref": "screenshot_file",
}


def _get(row: dict, key: str):
    v = row.get(key)
    if v is None:
        for old, new in ALIASES.items():
            if new == key and old in row:
                v = row[old]
    return v.strip() if isinstance(v, str) else v


def _f(v):
    return float(v) if v not in (None, "") else None


def _i(v):
    v = v.strip() if isinstance(v, str) else v
    return int(float(v)) if v not in (None, "") else None


def _b(v):
    return str(v).strip().lower() in ("1", "true", "yes", "y")


def _dt(v):
    if v in (None, ""):
        return None
    return datetime.fromisoformat(str(v).strip().replace("Z", ""))


def _markets_list(v: str) -> str:
    """'Moneyline 3-way; Spread' -> '["ML_3WAY","SPREAD_2WAY"]'."""
    if not v:
        return "[]"
    v = v.strip()
    if v.startswith("["):
        return v
    out = []
    for part in v.replace(";", ",").split(","):
        p = part.strip().lower()
        if not p:
            continue
        if "money" in p or p in ("ml", "ml_3way"):
            out.append("ML_3WAY")
        elif "spread" in p:
            out.append("SPREAD_2WAY")
        elif "total" in p or "over" in p:
            out.append("TOTAL")
        else:
            out.append(part.strip())
    return json.dumps(sorted(set(out)))


def _reader(text: str):
    return csv.DictReader(io.StringIO(text.lstrip("\ufeff")))


def parse_matches(text: str):
    rows, errors, warnings, seen = [], [], [], set()
    for i, row in enumerate(_reader(text), start=2):
        try:
            start = _dt(_get(row, "start_time"))
            home, away = _get(row, "home_player"), _get(row, "away_player")
            if not start or not home or not away:
                raise ValueError("start_time, home_player, away_player are required")
            ext = _get(row, "match_id") or None
            key = ext or (start.isoformat(), home, away)
            if key in seen:
                warnings.append(f"row {i}: duplicate of an earlier row in this file (kept, importer dedups)")
            seen.add(key)
            hs, as_ = _i(row.get("home_score")), _i(row.get("away_score"))
            winner = (_get(row, "winner") or "").lower() or None
            if winner and winner not in ("home", "away", "draw"):
                raise ValueError(f"winner must be home/away/draw, got {winner!r}")
            if hs is not None and as_ is not None:
                derived = "home" if hs > as_ else "away" if as_ > hs else "draw"
                if winner and winner != derived:
                    warnings.append(f"row {i}: winner={winner} disagrees with score {hs}-{as_}; score wins")
                winner = derived
            rows.append({
                "ext_id": ext, "start_time": start, "league": _get(row, "league") or "",
                "home_player": home, "away_player": away,
                "home_score": hs, "away_score": as_,
                "ht_home_score": _i(row.get("ht_home_score")),
                "ht_away_score": _i(row.get("ht_away_score")),
                "duration_min": _i(row.get("duration_min")),
                "winner": winner,
                "source": _get(row, "source") or "csv_import",
                "_row": i,
            })
        except (KeyError, ValueError) as e:
            errors.append(f"row {i}: {e}")
    return rows, errors, warnings


def parse_odds(text: str):
    rows, errors, warnings = [], [], []
    for i, row in enumerate(_reader(text), start=2):
        try:
            am = _i(_get(row, "american_odds"))
            ts = _dt(_get(row, "timestamp"))
            ext = _get(row, "match_id")
            if am is None or ts is None or not ext:
                raise ValueError("match_id, timestamp, american_odds are required")
            if -100 < am < 100:
                raise ValueError(f"american_odds {am} invalid (must be <=-100 or >=100)")
            dec = round(odds_math.american_to_decimal(am), 4)
            given_dec = _f(row.get("decimal_odds"))
            if given_dec and abs(given_dec - dec) > 0.02:
                warnings.append(f"row {i}: decimal_odds {given_dec} != derived {dec}; derived used, raw preserved")
            imp = round(odds_math.implied_prob(dec), 4)
            given_imp = _f(row.get("implied_probability"))
            if given_imp and abs(given_imp - imp) > 0.02:
                warnings.append(f"row {i}: implied_probability {given_imp} != derived {imp}; derived used")
            rows.append({
                "snapshot_id": _get(row, "snapshot_id") or None,
                "ext_id": ext, "sportsbook": _get(row, "sportsbook") or "",
                "market": (_get(row, "market") or "ML_3WAY").upper().replace("MONEYLINE 3-WAY", "ML_3WAY"),
                "selection": (_get(row, "selection") or "").lower(),
                "line": _f(row.get("line")), "american_odds": am, "decimal_odds": dec,
                "implied_prob": imp, "collected_at": ts,
                "is_opening": _b(row.get("is_opening")), "is_closing": _b(row.get("is_closing")),
                "raw_decimal": given_dec, "raw_implied": given_imp, "_row": i,
            })
        except (KeyError, ValueError) as e:
            errors.append(f"row {i}: {e}")
    return rows, errors, warnings


def parse_bets(text: str):
    rows, errors, warnings = [], [], []
    for i, row in enumerate(_reader(text), start=2):
        try:
            am = _i(_get(row, "american_odds"))
            placed = _dt(_get(row, "date_time_placed"))
            stake = _f(_get(row, "stake"))
            if am is None or placed is None or stake is None:
                raise ValueError("date_time_placed, american_odds, stake are required")
            dec = round(odds_math.american_to_decimal(am), 4)
            given_dec = _f(row.get("decimal_odds"))
            if given_dec and abs(given_dec - dec) > 0.02:
                warnings.append(f"row {i}: decimal_odds {given_dec} != derived {dec}; derived used")
            result = (_get(row, "result") or "open").lower()
            if result not in ("open", "win", "loss", "push", "void"):
                raise ValueError(f"result must be open/win/loss/push/void, got {result!r}")
            profit, payout = _f(row.get("profit")), _f(row.get("payout"))
            rows.append({
                "ext_id": _get(row, "bet_id") or None,
                "placed_at": placed, "sportsbook": _get(row, "sportsbook") or "",
                "league": _get(row, "league") or "",
                "match_label": _get(row, "match_label") or "",
                "selection": _get(row, "selection") or "",
                "opponent": _get(row, "opponent") or "",
                "market": (_get(row, "market") or "ML_3WAY").upper().replace("MONEYLINE 3-WAY", "ML_3WAY"),
                "line": _f(row.get("line")), "american_odds": am, "decimal_odds": dec,
                "stake": stake, "result": result,
                "payout": payout, "profit": profit,
                "closing_american_odds": _i(row.get("closing_american_odds")),
                "notes": _get(row, "notes") or "",
                "screenshot_ref": _get(row, "screenshot_file") or None,
                "_row": i,
            })
        except (KeyError, ValueError) as e:
            errors.append(f"row {i}: {e}")
    return rows, errors, warnings


def parse_recommendations(text: str):
    rows, errors, warnings = [], [], []
    for i, row in enumerate(_reader(text), start=2):
        try:
            sched = _dt(_get(row, "scheduled_start"))
            sel = _get(row, "recommended_selection") or ""
            rows.append({
                "ext_id": _get(row, "recommendation_id") or None,
                "received_at": _dt(_get(row, "created_at")),
                "source_name": _get(row, "source") or "friend",
                "league": _get(row, "league") or "",
                "scheduled_start": sched,
                "home_name": _get(row, "home_player") or "",
                "away_name": _get(row, "away_player") or "",
                "recommended_selection": sel,
                "acceptable_markets": _markets_list(_get(row, "acceptable_markets") or ""),
                "max_spread": _f(row.get("max_spread")),
                "min_american_odds": _i(row.get("min_american_odds")),
                "ideal_american_odds": _i(row.get("ideal_american_odds")),
                "expires_at": _dt(_get(row, "expires_at")),
                "confidence_label": (_get(row, "confidence") or "medium").lower(),
                "notes": _get(row, "notes") or "",
                "_row": i,
            })
            if not sched:
                warnings.append(f"row {i}: no scheduled_start — expiry/window logic disabled for this rec")
        except (KeyError, ValueError) as e:
            errors.append(f"row {i}: {e}")
    return rows, errors, warnings


def parse_executions(text: str):
    rows, errors, warnings = [], [], []
    for i, row in enumerate(_reader(text), start=2):
        try:
            live = _dt(_get(row, "live_detected_at"))
            placed = _dt(_get(row, "bet_placed_at"))
            lat = _f(row.get("latency_seconds"))
            if lat is None and live and placed:
                lat = round((placed - live).total_seconds(), 1)
            status = (_get(row, "status") or "placed").lower()
            rows.append({
                "ext_id": _get(row, "execution_id") or None,
                "rec_ext_id": _get(row, "recommendation_id") or None,
                "sportsbook": _get(row, "sportsbook") or "",
                "opened_at": _dt(_get(row, "opened_at")),
                "live_detected_at": live, "bet_placed_at": placed,
                "actual_market": (_get(row, "actual_market") or "").upper(),
                "actual_line": _f(row.get("actual_line")),
                "actual_american_odds": _i(row.get("actual_american_odds")),
                "stake": _f(row.get("stake")),
                "accepted_odds_movement": _b(row.get("accepted_odds_movement")),
                "was_within_window": _b(row.get("was_within_window")) if _get(row, "was_within_window") else None,
                "latency_seconds": lat, "status": status,
                "missed_reason": _get(row, "missed_reason") or "",
                "notes": _get(row, "notes") or "",
                "_row": i,
            })
        except (KeyError, ValueError) as e:
            errors.append(f"row {i}: {e}")
    return rows, errors, warnings
