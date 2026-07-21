"""v0.3.7D.5 reliability hotfix Task 1: evaluate_schedule() must compare
wall-clock time in an explicit, named local timezone -- matching what
macOS launchd's StartCalendarInterval actually uses -- not naive UTC
treated as if it were local. Covers EST, EDT, midnight boundaries, and both
US DST transitions (2026 spring-forward 03-08, fall-back 11-01).

Pure function tests -- no DB, no filesystem, no subprocess, no live state."""
import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_DIR = Path(__file__).resolve().parent.parent.parent
SCRIPTS_OPS = REPO_DIR / "scripts" / "ops"


def _load(rel_name: str, mod_name: str):
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPTS_OPS / rel_name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


orchestrator = _load("run_unattended_workday.py", "v37d5_orchestrator")

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def _cfg(**overrides):
    base = {"scheduled_hour": 2, "scheduled_minute": 0, "catch_up_hours": 6.0,
           "min_hours_between_runs": 18.0, "timezone_name": "America/New_York"}
    base.update(overrides)
    return base


def _utc_naive_for_local(y, mo, d, h, mi, tz=NY):
    """Build the naive-UTC datetime (this repo's `_now()` convention) that
    corresponds to a given LOCAL wall-clock time in `tz`."""
    local_dt = datetime(y, mo, d, h, mi, tzinfo=tz)
    return local_dt.astimezone(UTC).replace(tzinfo=None)


# --------------------------------------------------- EST (winter, UTC-5)

def test_est_scheduled_hour_proceeds_at_correct_utc_offset():
    # 2026-01-15 02:05 local EST (UTC-5) = 07:05 UTC
    now = _utc_naive_for_local(2026, 1, 15, 2, 5)
    assert now.hour == 7  # sanity: EST is UTC-5
    decision, reason = orchestrator.evaluate_schedule(now, None, _cfg(), tz=NY)
    assert decision == "PROCEED"


def test_est_scheduled_hour_missed_outside_catchup():
    # 2026-01-15 10:00 local EST -- 8h after the 02:00 schedule, past the 6h catch-up
    now = _utc_naive_for_local(2026, 1, 15, 10, 0)
    decision, reason = orchestrator.evaluate_schedule(now, None, _cfg(), tz=NY)
    assert decision == "MISSED_WINDOW"


# --------------------------------------------------- EDT (summer, UTC-4)

def test_edt_scheduled_hour_proceeds_at_correct_utc_offset():
    # 2026-07-15 02:05 local EDT (UTC-4) = 06:05 UTC
    now = _utc_naive_for_local(2026, 7, 15, 2, 5)
    assert now.hour == 6  # sanity: EDT is UTC-4
    decision, reason = orchestrator.evaluate_schedule(now, None, _cfg(), tz=NY)
    assert decision == "PROCEED"


def test_edt_vs_naive_utc_would_have_disagreed():
    """Reproduces the exact bug: treating the configured hour as UTC would
    put 'scheduled' 4 hours later than it really is in EDT, which can flip
    PROCEED into MISSED_WINDOW for a time that is legitimately still
    within the real local catch-up window."""
    # 2026-07-15 07:30 local EDT = 1.5h after the true 02:00 local schedule
    # (still well inside a 6h catch-up window) but 11:30 UTC -- if hour=2
    # were wrongly read as UTC, "scheduled" would compute as 02:00 UTC
    # that same day, putting window_end at 08:00 UTC, and 11:30 UTC would
    # incorrectly read as MISSED_WINDOW.
    now = _utc_naive_for_local(2026, 7, 15, 7, 30)
    decision, reason = orchestrator.evaluate_schedule(now, None, _cfg(), tz=NY)
    assert decision == "PROCEED"


# --------------------------------------------------- midnight boundary

def test_midnight_boundary_before_local_schedule_uses_yesterdays_window():
    # 2026-07-15 01:30 local EDT -- before today's 02:00 schedule, so this
    # must be evaluated against YESTERDAY's schedule/catch-up window, which
    # (assuming no recent completed run) is long past -> MISSED_WINDOW.
    now = _utc_naive_for_local(2026, 7, 15, 1, 30)
    decision, reason = orchestrator.evaluate_schedule(now, None, _cfg(), tz=NY)
    assert decision == "MISSED_WINDOW"


def test_midnight_boundary_just_after_local_schedule_proceeds():
    # 2026-07-15 02:00:30 local EDT -- just after today's schedule fires.
    now = _utc_naive_for_local(2026, 7, 15, 2, 1)
    decision, reason = orchestrator.evaluate_schedule(now, None, _cfg(), tz=NY)
    assert decision == "PROCEED"


# --------------------------------------------------- DST transitions (2026 US rules)

def test_spring_forward_transition_does_not_crash():
    """2026-03-08 02:00 local America/New_York does not exist (clocks jump
    2:00 -> 3:00) -- the scheduled hour itself falls in the DST gap.
    zoneinfo constructs it anyway (PEP 495 fold=0 convention); this must
    not raise, and must produce a sane, non-None decision."""
    # pick a `now` a few hours after the transition so the run would
    # legitimately be evaluated against that day's (gap-time) schedule
    now = _utc_naive_for_local(2026, 3, 8, 6, 0)  # 06:00 EDT (post-transition)
    decision, reason = orchestrator.evaluate_schedule(now, None, _cfg(), tz=NY)
    assert decision in ("PROCEED", "MISSED_WINDOW")
    assert reason  # non-empty, didn't blow up building the message


def test_spring_forward_day_proceeds_shortly_after_transition():
    # 2026-03-08 03:15 local (15 min after the 2am->3am jump) -- should
    # still read as "just after schedule" and PROCEED.
    now = _utc_naive_for_local(2026, 3, 8, 3, 15)
    decision, reason = orchestrator.evaluate_schedule(now, None, _cfg(), tz=NY)
    assert decision == "PROCEED"


def test_fall_back_transition_does_not_crash():
    """2026-11-01 01:00-02:00 local America/New_York occurs TWICE (clocks
    fall back 2:00 -> 1:00). Must not raise."""
    now = _utc_naive_for_local(2026, 11, 1, 5, 0)  # well after the fold, unambiguous
    decision, reason = orchestrator.evaluate_schedule(now, None, _cfg(), tz=NY)
    assert decision in ("PROCEED", "MISSED_WINDOW")
    assert reason


def test_fall_back_day_proceeds_shortly_after_schedule():
    now = _utc_naive_for_local(2026, 11, 1, 2, 30)
    decision, reason = orchestrator.evaluate_schedule(now, None, _cfg(), tz=NY)
    assert decision == "PROCEED"


# --------------------------------------------------- min-hours-between-runs uses absolute UTC time

def test_min_spacing_uses_absolute_time_not_wall_clock():
    """The SKIPPED_RECENT_RUN spacing check must compare real elapsed time
    (absolute UTC), not wall-clock fields -- a run that finished right
    before a DST transition must still correctly block a run attempted
    right after it, using genuine elapsed hours."""
    last_end_utc_naive = _utc_naive_for_local(2026, 3, 7, 20, 0)  # pre-transition
    latest = {"actual_end": last_end_utc_naive.isoformat(), "acceptance_test": False}
    # 10 real hours later, spanning the spring-forward transition
    now = _utc_naive_for_local(2026, 3, 8, 7, 0)
    decision, reason = orchestrator.evaluate_schedule(now, latest, _cfg(min_hours_between_runs=18.0), tz=NY)
    assert decision == "SKIPPED_RECENT_RUN"


def test_min_spacing_respects_configured_threshold():
    last_end_utc_naive = _utc_naive_for_local(2026, 7, 14, 2, 0)
    latest = {"actual_end": last_end_utc_naive.isoformat(), "acceptance_test": False}
    now = _utc_naive_for_local(2026, 7, 15, 2, 5)  # ~24h later -- past 18h minimum
    decision, reason = orchestrator.evaluate_schedule(now, latest, _cfg(min_hours_between_runs=18.0), tz=NY)
    assert decision == "PROCEED"


# --------------------------------------------------- timezone detection / config

def test_local_timezone_defaults_to_detected_or_utc():
    tz = orchestrator.local_timezone()
    assert tz is not None
    # must be a real usable tzinfo -- exercise it
    dt = datetime(2026, 6, 1, 12, 0, tzinfo=tz)
    assert dt.utcoffset() is not None


def test_local_timezone_env_override(monkeypatch):
    monkeypatch.setenv("UNATTENDED_TIMEZONE", "America/Los_Angeles")
    tz = orchestrator.local_timezone()
    assert str(tz) == "America/Los_Angeles" or getattr(tz, "key", None) == "America/Los_Angeles"


def test_load_catchup_config_includes_timezone_name(monkeypatch):
    monkeypatch.setenv("UNATTENDED_TIMEZONE", "America/Chicago")
    cfg = orchestrator.load_catchup_config()
    assert cfg["timezone_name"] == "America/Chicago"


def test_evaluate_schedule_uses_cfg_timezone_when_tz_arg_omitted(monkeypatch):
    """When called without an explicit tz= (the real call site in main()),
    it must fall back to cfg['timezone_name'], not silently assume UTC."""
    now = _utc_naive_for_local(2026, 7, 15, 2, 5)  # 02:05 EDT local
    cfg = _cfg(timezone_name="America/New_York")
    decision, reason = orchestrator.evaluate_schedule(now, None, cfg)
    assert decision == "PROCEED"
