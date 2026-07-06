"""Connector interface. Implement this to add any data provider.

Contract: connectors return plain dicts in the normalized shapes below;
they never touch the database. The import layer (routers/imports.py)
handles dedup and persistence, so provider quirks stay in one file.

Normalized match dict:
    {ext_id, start_time (ISO str), league, home_player, away_player,
     home_score, away_score, ht_home_score, ht_away_score, duration_min, source}

Normalized odds dict:
    {ext_id (match), sportsbook, market, selection, line, american_odds,
     collected_at (ISO str), is_opening, is_closing}
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime


class Connector(ABC):
    name: str = "base"

    @abstractmethod
    def fetch_matches(self, since: datetime | None = None) -> list[dict]: ...

    @abstractmethod
    def fetch_odds(self, since: datetime | None = None) -> list[dict]: ...
