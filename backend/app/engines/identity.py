"""Player identity intelligence (spec: Player Identity System).

ESoccer listings look like "Arsenal (CRUSADER)" — team skin + operator nickname.
The OPERATOR is the persistent identity (docs/DECISIONS.md D4). All formatting
variants resolve to one canonical Player via the player_aliases table.
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Player, PlayerAlias

_PAREN = re.compile(r"\(([^)]+)\)")
_CLEAN = re.compile(r"[^A-Z0-9 ]")


def canonical_name(raw: str) -> str:
    """'Arsenal (CRUSADER)' -> 'CRUSADER'; 'Crusader_' -> 'CRUSADER'; 'Kray' -> 'KRAY'."""
    raw = (raw or "").strip()
    m = _PAREN.search(raw)
    core = m.group(1) if m else raw
    core = core.replace("_", " ").upper().strip()
    core = _CLEAN.sub("", core)
    core = re.sub(r"\s+", " ", core).strip()
    return core


def resolve_player(db: Session, raw: str, league: str = "",
                   data_source: str = "csv_import",
                   verification_status: str = "user_verified") -> Player | None:
    """Resolve raw display text to one canonical Player, creating alias/player rows
    as needed. Never duplicates stats over formatting differences."""
    raw = (raw or "").strip()
    if not raw or raw.lower() == "unknown":
        return None
    alias = db.scalar(select(PlayerAlias).where(PlayerAlias.alias == raw))
    if alias:
        return db.get(Player, alias.player_id)
    canon = canonical_name(raw)
    if not canon:
        return None
    player = db.scalar(select(Player).where(Player.name == canon))
    if not player:
        player = Player(name=canon, league=league,
                        data_source=data_source, verification_status=verification_status)
        db.add(player)
        db.flush()
    db.add(PlayerAlias(alias=raw, player_id=player.id))
    # also register the canonical form itself so future lookups short-circuit
    if raw != canon and not db.scalar(select(PlayerAlias).where(PlayerAlias.alias == canon)):
        db.add(PlayerAlias(alias=canon, player_id=player.id))
    db.flush()
    return player


def aliases_for(db: Session, player_id: int) -> list[str]:
    return list(db.scalars(select(PlayerAlias.alias).where(PlayerAlias.player_id == player_id)))


def alias_uncertainty(db: Session, player_id: int) -> bool:
    """True when the identity rests on a single observed alias with <3 matches —
    used as a confidence penalty (spec rule 7)."""
    p = db.get(Player, player_id)
    return bool(p and p.matches_played < 3 and len(aliases_for(db, player_id)) <= 1)
