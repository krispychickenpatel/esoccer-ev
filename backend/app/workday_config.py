"""v0.3.7C: Workday Autopilot environment configuration. Read-only helper --
loads WORKDAY_* env vars with safe defaults. Never prints secrets (this
module doesn't touch BETSAPI_KEY at all; see main.py/betsapi_provider.py
for that check)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    try:
        return float(v) if v is not None else default
    except ValueError:
        return default


def _env_time(name: str) -> dtime | None:
    v = os.environ.get(name)
    if not v:
        return None
    try:
        h, m = v.strip().split(":")
        return dtime(int(h), int(m))
    except (ValueError, IndexError):
        return None


@dataclass
class WorkdayConfig:
    enable_densified_polling: bool
    densified_max_quota_pct: float
    collection_start: dtime | None
    collection_end: dtime | None
    timezone: str
    min_disk_headroom_mb: float
    max_last_poll_age_s: float
    max_last_ingest_age_s: float
    autopilot_startup_grace_s: float

    def in_collection_window(self, now_utc: datetime) -> bool:
        """True if no window is configured (always-active, preserves
        pre-v0.3.7C behavior), or if `now_utc` falls inside the configured
        local window. now_utc must be naive-UTC (this codebase's convention)."""
        if self.collection_start is None or self.collection_end is None:
            return True
        try:
            tz = ZoneInfo(self.timezone)
        except Exception:
            tz = ZoneInfo("UTC")
        local_now = now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        local_t = local_now.time()
        if self.collection_start <= self.collection_end:
            return self.collection_start <= local_t <= self.collection_end
        return local_t >= self.collection_start or local_t <= self.collection_end  # wraps midnight


def load_workday_config() -> WorkdayConfig:
    return WorkdayConfig(
        enable_densified_polling=_env_bool("WORKDAY_ENABLE_DENSIFIED_POLLING", False),
        densified_max_quota_pct=_env_float("WORKDAY_DENSIFIED_MAX_QUOTA_PCT", 60.0),
        collection_start=_env_time("WORKDAY_COLLECTION_START"),
        collection_end=_env_time("WORKDAY_COLLECTION_END"),
        timezone=os.environ.get("WORKDAY_TIMEZONE", "UTC"),
        min_disk_headroom_mb=_env_float("WORKDAY_MIN_DISK_HEADROOM_MB", 500.0),
        max_last_poll_age_s=_env_float("WORKDAY_MAX_LAST_POLL_AGE_S", 180.0),
        max_last_ingest_age_s=_env_float("WORKDAY_MAX_LAST_INGEST_AGE_S", 600.0),
        # v0.3.7D.1 Task 10: grace period after autopilot_started_at during
        # which zero collector activity is expected and must NOT trigger
        # FAIL (COLLECTOR_NOT_ALIVE/COLLECTOR_NEVER_TICKED) -- the provider
        # connection, first poll cycle, and first snapshot all take a few
        # seconds to a few minutes on a cold start.
        autopilot_startup_grace_s=_env_float("AUTOPILOT_STARTUP_GRACE_SECONDS", 180.0),
    )
