"""v0.3.7D.1 Task 8: daily recommendation awareness.

Combines run-state awareness (was a capped autopilot run active, or did one
just complete, or has one never run) with the strict executable-forward CLV
sample size (the one decisional number this whole release exists to
compute correctly -- see strict_forward_metrics.strict_forward_clv) into a
single, unambiguous recommendation message. Never says a bare "keep
collecting" without reference to whether a capped run already finished or
how close the strict sample is to its decision-grade gate.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from . import strict_forward_metrics

DEFAULT_LEAD_GATE_S = 20.0
DECISION_GRADE_MIN_N = 150


def build_recommendation(db: Session, health: dict, lead_s: float = DEFAULT_LEAD_GATE_S) -> dict:
    """`health` is the dict returned by app.routers.ops.health() (or the real
    backend's /api/ops/health JSON) -- must carry the v0.3.7D.1 `active_run`
    and `last_completed_run` fields."""
    strict = strict_forward_metrics.strict_forward_clv(db, lead_s)
    n = strict["strict_executable_forward_clv_n"]
    gate = DECISION_GRADE_MIN_N

    active_run = health.get("active_run")
    last_completed_run = health.get("last_completed_run")

    if active_run is not None:
        message = (f"CONTINUE DAILY COLLECTION -- strict executable CLV n={n}/{gate} "
                  f"(run in progress, started {active_run['run_started_at']}, "
                  f"cap={active_run['configured_max_minutes']}min)")
        action = "CONTINUE_COLLECTION"
    elif last_completed_run is not None:
        if n >= gate:
            message = (f"SAMPLE GATE MET -- strict executable CLV n={n}/{gate}. "
                      f"Last run completed {last_completed_run['run_completed_at']} "
                      f"({last_completed_run['actual_runtime_minutes']}min actual). "
                      "Proceed to verdict review -- see verdict_hierarchy.determine_verdict().")
            action = "REVIEW_VERDICT"
        else:
            message = (f"RUN COMPLETE, SAMPLE STILL BUILDING -- strict executable CLV n={n}/{gate}. "
                      f"Last run completed {last_completed_run['run_completed_at']} "
                      f"({last_completed_run['actual_runtime_minutes']}min actual of "
                      f"{last_completed_run['configured_max_minutes']}min cap). "
                      "Start another workday run to keep building the strict sample: "
                      "python3 scripts/ops/preflight_workday_run.py --non-interactive --allow-warn "
                      "&& python3 scripts/ops/run_workday_autopilot.py --max-minutes 480 --caffeinate "
                      "--non-interactive --allow-warn")
            action = "START_ANOTHER_RUN"
    else:
        message = (f"NO RUN RECORDED -- strict executable CLV n={n}/{gate}. Start a workday run: "
                  "python3 scripts/ops/preflight_workday_run.py --non-interactive --allow-warn "
                  "&& python3 scripts/ops/run_workday_autopilot.py --max-minutes 480 --caffeinate "
                  "--non-interactive --allow-warn")
        action = "START_FIRST_RUN"

    return {
        "message": message,
        "action": action,
        "strict_executable_clv_n": n,
        "strict_executable_clv_gate": gate,
        "strict_lead_time_gate_s": lead_s,
        "active_run": active_run,
        "last_completed_run": last_completed_run,
        "collection_status": health.get("status"),
    }
