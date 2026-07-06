from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..connectors import csv_v2
from ..connectors.csv_connector import CSVConnector
from ..engines.identity import aliases_for, resolve_player
from ..database import get_db
from ..engines.ratings import head_to_head, player_form, rebuild_ratings
from ..models import Match, OddsSnapshot, Player

router = APIRouter(prefix="/api", tags=["data"])


def get_or_create_player(db: Session, name: str, league: str = "") -> Player:
    """v2: routes through the identity engine so every formatting variant of the
    same operator collapses onto one canonical Player (D4)."""
    p = resolve_player(db, name, league=league)
    if p is None:  # blank/Unknown — keep a literal placeholder to avoid crashes
        p = db.execute(select(Player).where(Player.name == "UNKNOWN")).scalar_one_or_none()
        if p is None:
            p = Player(name="UNKNOWN", league=league)
            db.add(p)
            db.flush()
    return p


def upsert_match(db: Session, row: dict) -> tuple[Match, bool]:
    """Dedup on ext_id first, then (start_time, home, away). Returns (match, created)."""
    home = get_or_create_player(db, row["home_player"], row.get("league", ""))
    away = get_or_create_player(db, row["away_player"], row.get("league", ""))
    existing = None
    if row.get("ext_id"):
        existing = db.execute(select(Match).where(Match.ext_id == row["ext_id"])).scalar_one_or_none()
    if existing is None:
        existing = db.execute(select(Match).where(
            Match.start_time == row["start_time"],
            Match.home_player_id == home.id,
            Match.away_player_id == away.id)).scalar_one_or_none()
    created = existing is None
    m = existing or Match(home_player_id=home.id, away_player_id=away.id,
                          start_time=row["start_time"], source=row.get("source", "csv"))
    m.ext_id = row.get("ext_id") or m.ext_id
    m.league = row.get("league", m.league or "")
    for f in ("home_score", "away_score", "ht_home_score", "ht_away_score", "duration_min"):
        if row.get(f) is not None:
            setattr(m, f, row[f])
    if m.home_score is not None and m.away_score is not None:
        m.winner = "draw" if m.home_score == m.away_score else (
            "home" if m.home_score > m.away_score else "away")
    elif row.get("winner"):
        m.winner = row["winner"]  # winner-only rows still feed Elo (D5)
    if row.get("verification_status"):
        m.verification_status = row["verification_status"]
    if created:
        db.add(m)
    db.flush()
    return m, created


@router.get("/matches")
def list_matches(db: Session = Depends(get_db), limit: int = 500):
    rows = db.scalars(select(Match).order_by(Match.start_time.desc()).limit(limit)).all()
    return [{
        "id": m.id, "ext_id": m.ext_id, "start_time": m.start_time.isoformat(),
        "league": m.league, "home": m.home_player.name, "away": m.away_player.name,
        "home_score": m.home_score, "away_score": m.away_score,
        "ht": (f"{m.ht_home_score}-{m.ht_away_score}"
               if m.ht_home_score is not None else None),
        "winner": m.winner, "duration_min": m.duration_min, "source": m.source,
    } for m in rows]


@router.post("/matches/import")
async def import_matches(file: UploadFile = File(...), db: Session = Depends(get_db),
                         dry_run: bool = False):
    text = (await file.read()).decode("utf-8-sig")
    rows, errors, warnings = csv_v2.parse_matches(text)
    report = {"parsed": len(rows), "errors": errors, "warnings": warnings,
              "dry_run": dry_run, "created": 0, "updated": 0}
    if dry_run:
        report["preview"] = [{k: str(v) for k, v in r.items()} for r in rows[:20]]
        return report
    if errors:
        raise HTTPException(422, detail=report)  # never silently drop rows
    for r in rows:
        _, was_created = upsert_match(db, r)
        report["created"] += was_created
        report["updated"] += not was_created
    db.commit()
    rebuild_ratings(db)  # ratings always reflect the full match set
    report["ratings_rebuilt"] = True
    return report


@router.get("/odds")
def list_odds(db: Session = Depends(get_db), match_id: int | None = None, limit: int = 1000):
    q = select(OddsSnapshot).order_by(OddsSnapshot.collected_at.desc()).limit(limit)
    if match_id:
        q = q.where(OddsSnapshot.match_id == match_id)
    rows = db.scalars(q).all()
    match_ids = {r.match_id for r in rows}
    labels = {m.id: f"{m.home_player.name} vs {m.away_player.name}"
              for m in db.scalars(select(Match).where(Match.id.in_(match_ids))).all()} if match_ids else {}
    return [{
        "id": r.id, "match_id": r.match_id, "match": labels.get(r.match_id, ""),
        "sportsbook": r.sportsbook, "market": r.market, "selection": r.selection,
        "line": r.line, "american_odds": r.american_odds, "decimal_odds": r.decimal_odds,
        "implied_prob": round(r.implied_prob, 4),
        "is_opening": r.is_opening, "is_closing": r.is_closing,
        "collected_at": r.collected_at.isoformat(),
        "data_source": r.data_source, "verification_status": r.verification_status,
        "phase": r.phase,
    } for r in rows]


@router.post("/odds/import")
async def import_odds(file: UploadFile = File(...), db: Session = Depends(get_db),
                      dry_run: bool = False):
    text = (await file.read()).decode("utf-8-sig")
    rows, errors, warnings = csv_v2.parse_odds(text)
    report = {"parsed": len(rows), "errors": errors, "warnings": warnings,
              "dry_run": dry_run, "imported": 0, "skipped_unknown_match": []}
    if dry_run:
        report["preview"] = [{k: str(v) for k, v in r.items()} for r in rows[:20]]
        return report
    if errors:
        raise HTTPException(422, detail=report)
    ext_map = {m.ext_id: m.id for m in db.scalars(
        select(Match).where(Match.ext_id.is_not(None))).all()}
    matches = {m.id: m for m in db.scalars(select(Match)).all()}
    skipped = []
    for r in rows:
        mid = ext_map.get(r["ext_id"])
        if mid is None:
            skipped.append(r["ext_id"])
            continue
        m = matches[mid]
        s2k = (m.start_time - r["collected_at"]).total_seconds()
        db.add(OddsSnapshot(
            match_id=mid, sportsbook=r["sportsbook"], market=r["market"],
            selection=r["selection"], line=r["line"], american_odds=r["american_odds"],
            decimal_odds=r["decimal_odds"], implied_prob=r["implied_prob"],
            is_opening=r["is_opening"], is_closing=r["is_closing"],
            collected_at=r["collected_at"], seconds_to_kickoff=round(s2k, 1),
            phase="live" if s2k <= 0 else "pre_match"))
        report["imported"] += 1
    db.commit()
    report["skipped_unknown_match"] = sorted(set(skipped))[:20]
    return report


@router.get("/players")
def list_players(db: Session = Depends(get_db)):
    rows = db.scalars(select(Player).order_by(Player.elo.desc())).all()
    return [{
        "id": p.id, "name": p.name, "league": p.league, "elo": p.elo,
        "attack": p.attack, "defense": p.defense, "matches_played": p.matches_played,
        "form10": player_form(db, p.id, 10),
        "aliases": aliases_for(db, p.id),
        "data_source": p.data_source,
    } for p in rows]


@router.get("/players/{player_id}/detail")
def player_detail(player_id: int, vs: int | None = None, db: Session = Depends(get_db)):
    p = db.get(Player, player_id)
    if not p:
        raise HTTPException(404, "Player not found")
    out = {
        "id": p.id, "name": p.name, "elo": p.elo,
        "form5": player_form(db, p.id, 5),
        "form10": player_form(db, p.id, 10),
        "form25": player_form(db, p.id, 25),
    }
    if vs:
        out["h2h"] = head_to_head(db, p.id, vs)
    return out


@router.post("/ratings/rebuild")
def rebuild(db: Session = Depends(get_db), k: float = 32.0, nu: float = 0.63):
    rebuild_ratings(db, k=k, nu=nu)
    return {"ok": True, "k": k, "nu": nu}

@router.post("/provider/pull-upcoming")
def pull_upcoming_from_betsapi(db: Session = Depends(get_db), limit: int = 200):
    """One-click real schedule pull. This is the missing bridge between having
    a BetsAPI key and actually seeing current/upcoming ESoccer matches in the UI."""
    from ..connectors.betsapi_provider import BetsApiProvider
    from ..models import Settings

    s = db.get(Settings, 1)
    tracked = []
    if s and s.tracked_leagues:
        import json
        tracked = json.loads(s.tracked_leagues or "[]")
    provider = BetsApiProvider(db)
    rows = provider.fetch_upcoming()
    # Include currently live events too; these are the exact windows the strategy cares about.
    if hasattr(provider, "fetch_inplay"):
        rows = rows + provider.fetch_inplay()
    # Deduplicate provider events before upsert.
    by_ext = {r.get("ext_id") or f"{r.get('start_time')}-{r.get('home_player')}-{r.get('away_player')}": r for r in rows}
    rows = list(by_ext.values())
    scoped = [r for r in rows if not tracked or any(t.lower() in (r.get("league") or "").lower() for t in tracked)]
    report = {"provider_configured": bool(provider.token), "fetched": len(rows),
              "scoped": len(scoped), "created": 0, "updated": 0,
              "tracked_leagues": tracked, "provider_status": provider.status()}
    for r in scoped[:limit]:
        _, created = upsert_match(db, {**r, "verification_status": "api_verified"})
        report["created"] += 1 if created else 0
        report["updated"] += 0 if created else 1
    db.commit()
    return report


@router.post("/provider/pull-odds")
def pull_odds_for_upcoming(db: Session = Depends(get_db), minutes_ahead: int = 60):
    """Manual odds pull for upcoming matches. Uses sportsbooks_tracked in Settings.
    It does not enable betting signals by itself; it just populates odds snapshots."""
    from datetime import datetime, timedelta, timezone
    import json
    from ..connectors.betsapi_provider import BetsApiProvider
    from ..models import Settings
    from ..services.poller import process_snapshots

    s = db.get(Settings, 1)
    books = json.loads(s.sportsbooks_tracked or "[]") if s else ["bet365"]
    provider = BetsApiProvider(db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    matches = db.scalars(select(Match).where(
        Match.home_score.is_(None), Match.ext_id.is_not(None),
        Match.start_time > now - timedelta(minutes=15),
        Match.start_time < now + timedelta(minutes=minutes_ahead))).all()
    report = {"provider_configured": bool(provider.token), "matches": len(matches),
              "sportsbooks": books, "snapshots_written": 0, "events_written": 0,
              "empty_market_calls": 0, "provider_status": provider.status()}
    for m in matches:
        for book in books:
            odds = provider.fetch_odds(m.ext_id, source=book)
            if not odds:
                report["empty_market_calls"] += 1
                continue
            r = process_snapshots(db, m, odds)
            report["snapshots_written"] += r["written"]
            report["events_written"] += r["events"]
    return report
