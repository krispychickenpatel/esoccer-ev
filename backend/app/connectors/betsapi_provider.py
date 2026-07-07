"""BetsAPI provider (spec: Data providers).

Real endpoints wired, but every call is a no-op until BETSAPI_KEY is set —
no live key required. Not hardcoded as the only future provider: anything
implementing DataProvider can replace it (see base.py / csv_v2.py).

BetsAPI ESoccer notes (verify against current docs before first live run):
- sport_id 1 (soccer) with esoccer leagues, or dedicated /v1/events endpoints
- endpoints used here: upcoming, ended, event/odds
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PollCycle, RawProviderResponse

BASE = "https://api.b365api.com"
SPORT_ID_SOCCER = 1
ESOCCER_KEYWORDS = ("esoccer", "ebattle", "gt league", "battle")


class BetsApiProvider:
    name = "betsapi"

    def __init__(self, db: Session | None = None):
        self.db = db
        self.token = os.environ.get("BETSAPI_KEY") or os.environ.get("BETSAPI_TOKEN") or ""
        self.last_status: dict = {"configured": bool(self.token), "last_call": None,
                                  "last_code": None, "calls": 0, "retries": 0,
                                  "rate_limited": 0, "empty_responses": 0,
                                  # live budget from X-RateLimit-* response headers
                                  "quota_limit": None, "quota_remaining": None,
                                  "quota_resets_at": None}
        # v0.3.7B: true system-clock timing for the most recent _get() call,
        # so callers (fetch_odds) can attach real poll/response timestamps to
        # each row instead of only the provider's own event time. Naive UTC
        # (matches this codebase's _now() convention throughout).
        self.last_poll_cycle_id: int | None = None
        self.last_polled_at: datetime | None = None
        self.last_response_received_at: datetime | None = None

    # ------------------------------------------------------------------ core
    def _get(self, path: str, params: dict, max_retries: int = 3, sportsbook: str | None = None) -> dict | None:
        """GET with retries, exponential backoff, 429 handling, raw storage."""
        if not self.token:
            return None  # unconfigured — caller treats as empty
        params = {**params, "token": self.token}
        backoff = 1.0
        for attempt in range(max_retries + 1):
            polled_at = datetime.now(timezone.utc).replace(tzinfo=None)
            t0 = time.monotonic()
            try:
                r = httpx.get(f"{BASE}{path}", params=params, timeout=10)
                response_received_at = datetime.now(timezone.utc).replace(tzinfo=None)
                duration_ms = round((time.monotonic() - t0) * 1000, 1)
                self.last_status.update(last_call=datetime.now(timezone.utc).isoformat(),
                                        last_code=r.status_code)
                self.last_status["calls"] += 1
                # BetsAPI exposes hourly budget in headers on every response --
                # fixed window, resets each hour; no mid-hour replenish exists.
                h = r.headers
                if h.get("X-RateLimit-Limit"):
                    self.last_status["quota_limit"] = int(h["X-RateLimit-Limit"])
                if h.get("X-RateLimit-Remaining"):
                    self.last_status["quota_remaining"] = int(h["X-RateLimit-Remaining"])
                if h.get("X-RateLimit-Reset"):
                    self.last_status["quota_resets_at"] = datetime.fromtimestamp(
                        int(h["X-RateLimit-Reset"]), tz=timezone.utc).isoformat()
                rate_limited = r.status_code == 429
                self._record_poll_cycle(path, polled_at, response_received_at, r.status_code,
                                        duration_ms, success=r.status_code < 300,
                                        rate_limited=rate_limited)
                self._store_raw(path, r.status_code, r.text, sportsbook=sportsbook)
                if rate_limited:            # rate limited
                    self.last_status["rate_limited"] += 1
                    time.sleep(backoff)
                    backoff *= 2
                    self.last_status["retries"] += 1
                    continue
                if r.status_code >= 500:
                    time.sleep(backoff)
                    backoff *= 2
                    self.last_status["retries"] += 1
                    continue
                r.raise_for_status()
                data = r.json()
                if not data or not data.get("results"):
                    self.last_status["empty_responses"] += 1
                    return {"results": []}
                return data
            except (httpx.HTTPError, json.JSONDecodeError) as e:
                response_received_at = datetime.now(timezone.utc).replace(tzinfo=None)
                duration_ms = round((time.monotonic() - t0) * 1000, 1)
                self._record_poll_cycle(path, polled_at, response_received_at, None, duration_ms,
                                        success=False, error_type=type(e).__name__)
                if attempt == max_retries:
                    return None
                time.sleep(backoff)
                backoff *= 2
                self.last_status["retries"] += 1
        return None

    def _record_poll_cycle(self, endpoint: str, polled_at: datetime, response_received_at: datetime,
                           status_code: int | None, duration_ms: float, success: bool,
                           error_type: str | None = None, rate_limited: bool = False):
        """v0.3.7B: true system-clock record of this HTTP call, independent of
        provider event-time. self.last_poll_cycle_id/last_polled_at/
        last_response_received_at are set even if self.db is None (tests /
        no-DB callers), just not persisted."""
        self.last_polled_at = polled_at
        self.last_response_received_at = response_received_at
        self.last_poll_cycle_id = None
        if self.db is None:
            return
        try:
            row = PollCycle(
                endpoint=endpoint, provider=self.name, intended_poll_at=polled_at,
                poll_started_at=polled_at, response_received_at=response_received_at,
                committed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                status_code=status_code, success=success, error_type=error_type,
                request_duration_ms=duration_ms,
                quota_limit=self.last_status.get("quota_limit"),
                quota_remaining=self.last_status.get("quota_remaining"),
                quota_reset_at=self.last_status.get("quota_resets_at"),
                rate_limited=rate_limited,
            )
            self.db.add(row)
            self.db.commit()
            self.last_poll_cycle_id = row.id
        except Exception:
            self.db.rollback()

    def _store_raw(self, endpoint: str, code: int, payload: str, sportsbook: str | None = None):
        if self.db is None:
            return
        try:
            self.db.add(RawProviderResponse(provider=self.name, endpoint=endpoint,
                                            status_code=code, payload=payload[:200_000],
                                            sportsbook=sportsbook,
                                            poll_cycle_id=self.last_poll_cycle_id))
            self.db.commit()
        except Exception:
            self.db.rollback()

    # ------------------------------------------------------- normalized fetches
    @staticmethod
    def _is_esoccer(ev: dict) -> bool:
        lg = (ev.get("league", {}) or {}).get("name", "").lower()
        return any(k in lg for k in ESOCCER_KEYWORDS)

    def _paged_events(self, path: str, base_params: dict, max_pages: int = 6) -> dict:
        """BetsAPI event lists can be paged. Pull a small page window so schedule
        discovery is not accidentally limited to the first soccer page before
        ESoccer leagues appear."""
        merged = []
        for page in range(1, max_pages + 1):
            params = {**base_params, "page": page}
            data = self._get(path, params)
            if not data:
                break
            results = data.get("results") or []
            merged.extend(results)
            if len(results) == 0:
                break
        return {"results": merged}

    def fetch_upcoming(self) -> list[dict]:
        """Live/upcoming ESoccer events, normalized to internal match dicts.
        skip_esports=0 is intentional: we WANT esoccer/e-sports listings."""
        data = self._paged_events("/v3/events/upcoming",
                                  {"sport_id": SPORT_ID_SOCCER, "skip_esports": 0})
        return self._normalize_events(data)

    def fetch_inplay(self) -> list[dict]:
        data = self._paged_events("/v3/events/inplay",
                                  {"sport_id": SPORT_ID_SOCCER, "skip_esports": 0}, max_pages=3)
        return self._normalize_events(data)

    def fetch_event_history(self, event_id: str, qty: int = 20) -> list[dict]:
        """Prior matches for both teams in one event (v1/event/history).
        Used for targeted backfill -- pulls history for players you're about
        to generate picks for, instead of a bulk events/ended dump. qty caps
        at 20 per BetsAPI's docs."""
        data = self._get("/v1/event/history", {"event_id": event_id, "qty": min(qty, 20)})
        if not data:
            return []
        out = []
        results = data.get("results") or {}
        # results has 'h2h' and/or 'home'/'away' keys depending on data availability
        for bucket_key in ("h2h", "home", "away"):
            for ev in results.get(bucket_key, []) or []:
                if not self._is_esoccer(ev):
                    continue
                ss = (ev.get("ss") or "").split("-")
                hs = int(ss[0]) if len(ss) == 2 and ss[0] not in (None, "") else None
                as_ = int(ss[1]) if len(ss) == 2 and ss[1] not in (None, "") else None
                if ev.get("time") is None:
                    continue
                out.append({
                    "ext_id": str(ev.get("id")),
                    "start_time": datetime.fromtimestamp(int(ev["time"]), tz=timezone.utc)
                                  .replace(tzinfo=None),
                    "league": (ev.get("league", {}) or {}).get("name", ""),
                    "home_player": (ev.get("home", {}) or {}).get("name", ""),
                    "away_player": (ev.get("away", {}) or {}).get("name", ""),
                    "home_score": hs, "away_score": as_,
                    "ht_home_score": None, "ht_away_score": None, "duration_min": None,
                    "winner": None, "source": "betsapi_history",
                })
        return out

    def fetch_results(self, day: str | None = None) -> list[dict]:
        """Historical ended ESoccer results. day='YYYYMMDD' optional."""
        params = {"sport_id": SPORT_ID_SOCCER}
        if day:
            params["day"] = day
        data = self._get("/v3/events/ended", params)
        return self._normalize_events(data, finished=True)

    def fetch_odds(self, event_id: str, source: str = "bet365") -> list[dict]:
        """Odds snapshots for one event, normalized. Empty when unconfigured.

        source: any BetsAPI bookmaker key (default 'bet365'). Pass 'fanduel'
        to match your friend's actual book -- BetsAPI lists fanduel as a valid
        source (added 2025-08-03 per their changelog). Coverage table shows
        Bet365 as Yes/Yes (in-play/pre-match) for soccer; FanDuel's own
        coverage isn't listed in the table, so fetch_odds may return empty
        for source='fanduel' on some events -- fall back to bet365 as the
        market baseline if so.

        odds_market=1,2 requests only 1X2 (ML) and Asian Handicap (spread) --
        spec doesn't need totals yet ('Totals if available later'), and
        narrowing the market list cuts response payload size.
        """
        data = self._get("/v2/event/odds",
                         {"event_id": event_id, "source": source, "odds_market": "1,2"},
                         sportsbook=source)
        # v0.3.7B: capture this call's true system-clock timing once, up
        # front, so every row from this fetch carries the SAME polled_at/
        # response_received_at/poll_cycle_id -- they describe the HTTP call,
        # not the individual tick.
        polled_at = self.last_polled_at
        response_received_at = self.last_response_received_at
        poll_cycle_id = self.last_poll_cycle_id
        if not data:
            return []
        out = []
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        odds = (data.get("results") or {}).get("odds", {}) if isinstance(data.get("results"), dict) else {}

        # market 1_1 = 1X2 (3-way ML): home_od / draw_od / away_od
        for tick in odds.get("1_1", []) or []:
            for sel, key in (("home", "home_od"), ("draw", "draw_od"), ("away", "away_od")):
                dec = tick.get(key)
                if not dec or dec in ("-",):
                    continue
                dec = float(dec)
                source_ts = (datetime.fromtimestamp(int(tick["add_time"]), tz=timezone.utc)
                            .replace(tzinfo=None) if tick.get("add_time") else now)
                out.append({
                    "ext_id": str(event_id), "sportsbook": source,
                    "market": "ML_3WAY", "selection": sel, "line": None,
                    "decimal_odds": dec,
                    "american_odds": _dec_to_american(dec),
                    "implied_prob": round(1 / dec, 4),
                    "collected_at": source_ts,
                    "is_opening": False, "is_closing": False,
                    # v0.3.7B: true system-observation fields. source_ts is a
                    # same-value alias for collected_at (which remains
                    # provider event-time, unchanged); polled_at/
                    # response_received_at are our own wall clock.
                    "source_ts": source_ts, "polled_at": polled_at,
                    "response_received_at": response_received_at,
                    "poll_cycle_id": poll_cycle_id, "provider_event_id": str(event_id),
                    "provider_book": source,
                })

        # market 1_2 = Asian Handicap (spread). FIELD NAMES UNVERIFIED --
        # BetsAPI's docs didn't include the 1_2 response schema, only that it
        # exists. This assumes the same home_od/away_od pattern as 1_1 plus a
        # handicap/line field; confirm against a real response before trusting
        # spread data, and check RawProviderResponse rows (stored on every
        # call) if this silently returns nothing.
        for tick in odds.get("1_2", []) or []:
            line = tick.get("handicap") or tick.get("handicap_home") or tick.get("ah_home")
            for sel, key in (("home", "home_od"), ("away", "away_od")):
                dec = tick.get(key)
                if not dec or dec in ("-",) or line is None:
                    continue
                try:
                    dec, line_f = float(dec), float(line) * (1 if sel == "home" else -1)
                except (TypeError, ValueError):
                    continue
                if line_f > -0.5:  # rule: spread only up to -0.5 (D-earlier)
                    continue
                source_ts = (datetime.fromtimestamp(int(tick["add_time"]), tz=timezone.utc)
                            .replace(tzinfo=None) if tick.get("add_time") else now)
                out.append({
                    "ext_id": str(event_id), "sportsbook": source,
                    "market": "SPREAD_2WAY", "selection": sel, "line": line_f,
                    "decimal_odds": dec,
                    "american_odds": _dec_to_american(dec),
                    "implied_prob": round(1 / dec, 4),
                    "collected_at": source_ts,
                    "is_opening": False, "is_closing": False,
                    "source_ts": source_ts, "polled_at": polled_at,
                    "response_received_at": response_received_at,
                    "poll_cycle_id": poll_cycle_id, "provider_event_id": str(event_id),
                    "provider_book": source,
                })
        return out

    def _normalize_events(self, data: dict | None, finished: bool = False) -> list[dict]:
        if not data:
            return []
        out = []
        for ev in data.get("results", []):
            if not self._is_esoccer(ev):
                continue
            ss = (ev.get("ss") or "").split("-") if finished else [None, None]
            hs = int(ss[0]) if finished and ss[0] not in (None, "",) else None
            as_ = int(ss[1]) if finished and len(ss) > 1 and ss[1] not in (None, "",) else None
            out.append({
                "ext_id": str(ev.get("id")),
                "start_time": datetime.fromtimestamp(int(ev["time"]), tz=timezone.utc)
                              .replace(tzinfo=None),
                "league": (ev.get("league", {}) or {}).get("name", ""),
                "home_player": (ev.get("home", {}) or {}).get("name", ""),
                "away_player": (ev.get("away", {}) or {}).get("name", ""),
                "home_score": hs, "away_score": as_,
                "ht_home_score": None, "ht_away_score": None, "duration_min": None,
                "winner": None,
                "source": "betsapi",
            })
        return out


    def capability_report(self, probe: bool = False) -> dict:
        """Endpoint-level report. probe=False is DB-only and safe.

        Status meanings:
        - works: code supports it and a 200 payload has been observed.
        - broken: a non-2xx response has been observed.
        - unknown: no key/no observation yet.
        - unsupported: deliberately not implemented in this build.
        - missing: implemented, but no useful rows parsed from observed/probed data.
        """
        definitions = {
            "upcoming_matches": {
                "endpoint": "/v3/events/upcoming",
                "method": "fetch_upcoming",
                "supports": ["Upcoming Matches", "Player Names", "Provider IDs", "Timestamps", "Raw Payloads"],
                "parser_risk": "league filter is keyword-based; confirm exact ESoccer league names",
            },
            "live_matches": {
                "endpoint": "/v3/events/inplay",
                "method": "fetch_inplay",
                "supports": ["Live Matches", "Player Names", "Provider IDs", "Timestamps", "Raw Payloads"],
                "parser_risk": "in-play payload status fields are stored raw but not fully modelled yet",
            },
            "historical_results": {
                "endpoint": "/v3/events/ended",
                "method": "fetch_results",
                "supports": ["Historical Results", "Scores", "Player Names", "Provider IDs", "Timestamps", "Raw Payloads"],
                "parser_risk": "bulk history depends on day/page availability",
            },
            "targeted_history": {
                "endpoint": "/v1/event/history",
                "method": "fetch_event_history",
                "supports": ["Historical Results", "H2H Samples", "Raw Payloads"],
                "parser_risk": "response schema must be validated against real payloads",
            },
            "odds_history": {
                "endpoint": "/v2/event/odds",
                "method": "fetch_odds",
                "supports": ["Odds", "Odds History", "Markets", "Timestamps", "Raw Payloads"],
                "parser_risk": "1X2 parser is implemented; spread/handicap parser remains unverified until real payload inspection",
            },
            "official_gt": {
                "endpoint": "Official GT",
                "method": None,
                "supports": [],
                "parser_risk": "unsupported: needs separate official-source connector",
                "unsupported": True,
            },
            "official_esportsbattle": {
                "endpoint": "Official ESportsBattle",
                "method": None,
                "supports": [],
                "parser_risk": "unsupported: needs separate official-source connector",
                "unsupported": True,
            },
        }

        if probe and self.token:
            # Conservative probes: no broad historical crawl. Odds needs a live/upcoming event id.
            upcoming = self.fetch_upcoming()
            inplay = self.fetch_inplay()
            self.fetch_results()
            sample_event = (inplay or upcoming or [{}])[0].get("ext_id")
            if sample_event:
                self.fetch_odds(sample_event)
                self.fetch_event_history(sample_event, qty=5)

        latest_by_endpoint = {}
        if self.db is not None:
            rows = self.db.scalars(select(RawProviderResponse)
                                   .where(RawProviderResponse.provider == self.name)
                                   .order_by(RawProviderResponse.at.desc())
                                   .limit(200)).all()
            for row in rows:
                latest_by_endpoint.setdefault(row.endpoint, row)

        endpoints = []
        for name, meta in definitions.items():
            observed = latest_by_endpoint.get(meta["endpoint"])
            status = "unsupported" if meta.get("unsupported") else "unknown"
            parsed = None
            if observed:
                if observed.status_code and 200 <= observed.status_code < 300:
                    try:
                        payload = json.loads(observed.payload or "{}")
                        results = payload.get("results")
                        if results in (None, [], {}):
                            status = "missing"
                            parsed = 0
                        else:
                            status = "works"
                            parsed = len(results) if isinstance(results, list) else 1
                    except json.JSONDecodeError:
                        status = "broken"
                elif observed.status_code is not None:
                    status = "broken"

            endpoints.append({
                "name": name,
                "endpoint": meta["endpoint"],
                "method": meta.get("method"),
                "status": status,
                "last_status_code": observed.status_code if observed else None,
                "last_observed_at": observed.at.isoformat() if observed else None,
                "parsed_items_observed": parsed,
                "supports": meta["supports"],
                "parser_risk": meta["parser_risk"],
            })

        return {
            "provider": self.name,
            "configured": bool(self.token),
            "probe_ran": bool(probe and self.token),
            "status_counts": {k: sum(1 for e in endpoints if e["status"] == k)
                              for k in ("works", "broken", "missing", "unsupported", "unknown")},
            "endpoints": endpoints,
            "hard_blocker": None if self.token else "BETSAPI_KEY is not configured, so endpoint capability is unknown until you run a probe with a key.",
        }

    def status(self) -> dict:
        return {**self.last_status,
                "note": None if self.token else
                "Set BETSAPI_KEY in the environment to activate this provider."}


def sportsbook_empty_stats(db: Session, lookback: int = 300) -> dict:
    """Per-sportsbook /v2/event/odds call/empty-response counts, computed from
    stored raw payloads (not an in-memory counter, so it survives restarts
    and is consistent across every process that reads the same DB).

    An "empty" response is a 200 with no odds ticks at all -- BetsAPI's
    verified behavior for a configured-but-uncovered source like fanduel on
    esoccer markets (see fetch_odds docstring), not an error."""
    rows = db.scalars(select(RawProviderResponse)
                      .where(RawProviderResponse.endpoint == "/v2/event/odds",
                             RawProviderResponse.sportsbook.is_not(None))
                      .order_by(RawProviderResponse.at.desc())
                      .limit(lookback)).all()
    stats: dict[str, dict] = {}
    for row in rows:
        b = stats.setdefault(row.sportsbook, {"calls": 0, "empty": 0})
        b["calls"] += 1
        is_empty = True
        try:
            payload = json.loads(row.payload or "{}")
            results = payload.get("results")
            odds = results.get("odds") if isinstance(results, dict) else None
            is_empty = not odds
        except json.JSONDecodeError:
            is_empty = True
        if is_empty:
            b["empty"] += 1
    for v in stats.values():
        v["empty_rate"] = round(v["empty"] / v["calls"], 3) if v["calls"] else 0.0
    return stats


def _dec_to_american(dec: float) -> int:
    if dec >= 2.0:
        return round((dec - 1) * 100)
    return round(-100 / (dec - 1))
