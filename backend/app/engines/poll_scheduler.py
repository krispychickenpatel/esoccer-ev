"""Near-kickoff densified polling scheduler (v0.3.7B Section 2).

Pure, unit-testable functions plus a small CircuitBreaker state object --
no I/O, no DB, no live API calls. Wired into services/poller.py behind
Settings.densified_polling_enabled, which defaults to False: shipping this
module changes nothing about live collection unless explicitly turned on.

Window: T-minus 10 minutes through live+2 minutes (s2k in [-120, 600],
s2k = seconds to kickoff, negative = after kickoff).
Target: median inter-row gap <=15s in the window, best-effort <=10s when
quota allows. Graceful degradation ladder: 10s -> 15s -> 30s under load.
Hard rule: never exceed Settings.densified_polling_quota_pct_cap (default
60%) of the documented hourly quota by design.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

DENSIFIED_WINDOW_BEFORE_S = 600.0   # T-minus 10 minutes
DENSIFIED_WINDOW_AFTER_S = -120.0   # live + 2 minutes (s2k negative after KO)

CADENCE_LADDER = (10.0, 15.0, 30.0)  # best-effort -> target -> degraded floor

# Priority tiers (lower = higher priority), reusing the same ordering
# convention as poller._match_priority so the two systems never disagree.
PRIORITY_ACTIVE_PREDICTION = 0
PRIORITY_IN_WINDOW = 1
PRIORITY_FRIEND_INTEREST = 2
PRIORITY_NORMAL_COVERAGE = 3


def in_densified_window(seconds_to_kickoff: float) -> bool:
    return DENSIFIED_WINDOW_AFTER_S <= seconds_to_kickoff <= DENSIFIED_WINDOW_BEFORE_S


def match_priority(*, has_active_prediction: bool, seconds_to_kickoff: float,
                   has_friend_interest: bool) -> int:
    """Priority order (spec): 1) matches with active/frozen predictions,
    2) matches inside the densified window, 3) matches with known
    friend/manual interest, 4) normal upcoming/inplay coverage."""
    if has_active_prediction:
        return PRIORITY_ACTIVE_PREDICTION
    if in_densified_window(seconds_to_kickoff):
        return PRIORITY_IN_WINDOW
    if has_friend_interest:
        return PRIORITY_FRIEND_INTEREST
    return PRIORITY_NORMAL_COVERAGE


def quota_pressure_pct(calls_last_hour: int, hourly_quota_cap: int) -> float:
    if hourly_quota_cap <= 0:
        return 100.0
    return round(100.0 * calls_last_hour / hourly_quota_cap, 2)


def densified_cadence_seconds(pressure_pct: float, quota_pct_cap: float) -> float:
    """Graceful degradation: best-effort 10s under light load, 15s target
    under normal load, degrades to 30s as usage approaches the hard cap.
    At or above quota_pct_cap, returns the coarsest rung -- densification
    must never itself be the reason the quota cap is breached."""
    if pressure_pct >= quota_pct_cap:
        return CADENCE_LADDER[-1]
    if pressure_pct >= quota_pct_cap * 0.75:
        return CADENCE_LADDER[-1]
    if pressure_pct >= quota_pct_cap * 0.5:
        return CADENCE_LADDER[1]
    return CADENCE_LADDER[0]


def quota_budget_ok(calls_last_hour: int, hourly_quota_cap: int, quota_pct_cap: float) -> bool:
    """Hard gate: densified polling may add calls only while total usage
    stays under quota_pct_cap of the documented hourly cap."""
    return quota_pressure_pct(calls_last_hour, hourly_quota_cap) < quota_pct_cap


@dataclass
class CircuitBreakerState:
    """Trips after `failure_threshold` consecutive 429s/failures within
    `window_s`; stays open (paused) for `cooldown_s` with exponential
    backoff on repeated trips. Pure state, no I/O -- caller persists/reads
    it (module-level singleton in the real poller, a fresh instance in
    tests)."""
    failure_threshold: int = 3
    base_cooldown_s: float = 30.0
    max_cooldown_s: float = 600.0
    consecutive_failures: int = 0
    trip_count: int = 0
    opened_at: datetime | None = None
    cooldown_s: float = field(default=30.0)

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.trip_count = 0
        self.opened_at = None
        self.cooldown_s = self.base_cooldown_s

    def record_failure(self, now: datetime) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold and self.opened_at is None:
            self.trip_count += 1
            self.opened_at = now
            self.cooldown_s = min(self.max_cooldown_s,
                                  self.base_cooldown_s * (2 ** (self.trip_count - 1)))

    def is_open(self, now: datetime) -> bool:
        """True = circuit is open (paused); densified polling must fall back
        to normal cadence while open."""
        if self.opened_at is None:
            return False
        if now - self.opened_at >= timedelta(seconds=self.cooldown_s):
            # cooldown elapsed -- half-open: allow one attempt through by
            # clearing the open state, but keep failure count so a repeat
            # failure re-trips immediately with a longer cooldown.
            self.opened_at = None
            return False
        return True
