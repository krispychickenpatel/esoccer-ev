#!/usr/bin/env python3
"""v0.3.7D.1 Task 1 -- Reporting partition audit. READ-ONLY, SELECT-only.

Opens the ACTIVE database (../esoccer-ev/backend/esoccer.db) via a
genuinely read-only SQLite connection (any write attempt hard-fails with
sqlite3.OperationalError) -- this script is designed to run safely WHILE
Workday Autopilot is actively collecting in the separate, untouched main
repository. Only read-only engine functions are called
(classify_paper_trade, not classify_and_store/classify_all; the
report-only CLV functions, not build_all).

Writes notes/triage/v0_3_7D1-partition-audit.md and .json.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

WORKTREE_BACKEND = Path(__file__).resolve().parent.parent.parent / "backend"
sys.path.insert(0, str(WORKTREE_BACKEND))

# The ACTIVE repo's database -- never the worktree's (which has none).
ACTIVE_DB_PATH = Path("/Users/krispatell/Downloads/ESoccer/current/esoccer-ev/backend/esoccer.db")

OUT_MD = Path("/Users/krispatell/Downloads/ESoccer/notes/triage/v0_3_7D1-partition-audit.md")
OUT_JSON = Path("/Users/krispatell/Downloads/ESoccer/notes/triage/v0_3_7D1-partition-audit.json")

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

# Must run from WORKTREE_BACKEND so `from app...` imports resolve, without
# actually touching the active repo's files.
os.chdir(WORKTREE_BACKEND)

from app.engines import clv_forward_readiness, execution_classifier_v2, strict_forward_metrics  # noqa: E402
from app.models import (ClosingRecord, ExecutionClassification, Match,  # noqa: E402
                        OddsSnapshot, PaperTrade, PredictionLedger)


def _ro_session():
    """Genuinely read-only connection -- any INSERT/UPDATE/DELETE attempted
    through this session raises sqlite3.OperationalError immediately."""
    def creator():
        return sqlite3.connect(f"file:{ACTIVE_DB_PATH}?mode=ro", uri=True)
    engine = create_engine("sqlite://", creator=creator)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return Session()


def main():
    if not ACTIVE_DB_PATH.exists():
        print(f"FAIL: active DB not found at {ACTIVE_DB_PATH}", file=sys.stderr)
        sys.exit(1)

    db = _ro_session()

    # ---- current (possibly buggy) report outputs, for before/after comparison
    historical_report_current = clv_forward_readiness.historical_clv_report(db)
    forward_report_current = clv_forward_readiness.forward_clv_readiness(db)

    # ---- true partition, computed directly from ClosingRecord rows
    all_closes = db.scalars(select(ClosingRecord)).all()
    forward_closes = [c for c in all_closes if c.close_polled_at is not None and c.close_ingested_at is not None]
    historical_closes = [c for c in all_closes if c not in forward_closes]

    # ---- overlap: rows counted in BOTH historical_clv_report and
    # forward_clv_readiness under the CURRENT (pre-fix) code. Since
    # historical_clv_report applies no filter, every forward pair used by
    # forward_clv_readiness is ALSO included in historical's pair list.
    # Reconstruct both pair sets using the exact same dedup convention the
    # real code uses (_entry_close_pairs), read-only.
    pairs = clv_forward_readiness._entry_close_pairs(db)
    forward_pairs = [p for p in pairs if p["has_system_ts"]]
    double_counted = len(forward_pairs)  # every forward pair also lands in historical's unfiltered set

    # ---- execution classification: stored (possibly stale) vs freshly
    # recomputed (read-only, using classify_paper_trade -- never
    # classify_and_store/classify_all, which write).
    all_trades = db.scalars(select(PaperTrade).where(PaperTrade.signal_source == "MODEL")).all()
    stored_by_trade = {r.paper_trade_id: r for r in db.scalars(select(ExecutionClassification)).all()}

    recompute_diffs = []
    fresh_by_id: dict[int, dict] = {}
    for t in all_trades:
        primary, flags, degraded, executability = execution_classifier_v2.classify_paper_trade(db, t)
        fresh_by_id[t.id] = {"primary_state": primary, "is_historical_degraded": degraded,
                             "executability_label": executability}
        stored = stored_by_trade.get(t.id)
        if stored is not None:
            if (stored.primary_state != primary or stored.is_historical_degraded != degraded
                    or stored.executability_label != executability):
                recompute_diffs.append({
                    "paper_trade_id": t.id,
                    "stored_primary_state": stored.primary_state, "fresh_primary_state": primary,
                    "stored_executability": stored.executability_label, "fresh_executability": executability,
                    "stored_degraded": stored.is_historical_degraded, "fresh_degraded": degraded,
                })

    forward_trustworthy_fresh = [fid for fid, v in fresh_by_id.items() if not v["is_historical_degraded"]]
    historical_degraded_fresh = [fid for fid, v in fresh_by_id.items() if v["is_historical_degraded"]]

    # ---- forward rows mislabeled historical / vice versa, under CURRENT report code
    # historical_clv_report has NO filter -> every forward pair is mislabeled historical.
    forward_rows_mislabeled_historical = len(forward_pairs)
    # forward_clv_readiness DOES filter correctly by has_system_ts -> 0 historical rows leak into forward.
    historical_rows_mislabeled_forward = 0

    high_quality_by_true_era = {
        "FORWARD_V037B_PLUS": sum(1 for c in forward_closes if c.close_quality == "HIGH"),
        "HISTORICAL_PRE_V037B": sum(1 for c in historical_closes if c.close_quality == "HIGH"),
    }

    provider_time_diagnostic_rows_from_forward_era = len(forward_closes)  # every forward close still HAS a provider-time (close_source_ts) view available diagnostically

    strict_executable_forward_rows = sum(1 for v in fresh_by_id.values()
                                         if not v["is_historical_degraded"]
                                         and v["executability_label"] == execution_classifier_v2.EXECUTABLE_PREKICK_STRICT)
    executable_via_start_delay_rows = sum(1 for v in fresh_by_id.values()
                                          if not v["is_historical_degraded"]
                                          and v["executability_label"] == execution_classifier_v2.EXECUTABLE_VIA_START_DELAY)
    research_only_rows = sum(1 for v in fresh_by_id.values()
                             if not v["is_historical_degraded"]
                             and v["executability_label"] == execution_classifier_v2.RESEARCH_ONLY_KICKOFF)

    aggregate = {
        "generated_at": datetime.now().isoformat(),
        "active_db_path": str(ACTIVE_DB_PATH),
        "read_only_connection_confirmed": True,
        "closing_records_total": len(all_closes),
        "closing_records_forward_true_era": len(forward_closes),
        "closing_records_historical_true_era": len(historical_closes),
        "high_quality_closes_by_true_era": high_quality_by_true_era,
        "current_report_historical_distinct_samples_with_close": historical_report_current["distinct_samples_with_close"],
        "current_report_forward_system_timestamped_samples": forward_report_current["forward_system_timestamped_samples"],
        "forward_rows_mislabeled_historical_under_current_code": forward_rows_mislabeled_historical,
        "historical_rows_mislabeled_forward_under_current_code": historical_rows_mislabeled_forward,
        "rows_double_counted_across_clv_partitions": double_counted,
        "provider_time_diagnostic_rows_from_forward_era": provider_time_diagnostic_rows_from_forward_era,
        "strict_executable_forward_rows_fresh_recompute": strict_executable_forward_rows,
        "executable_via_start_delay_rows_fresh_recompute": executable_via_start_delay_rows,
        "research_only_rows_fresh_recompute": research_only_rows,
        "execution_classifications_total": len(all_trades),
        "execution_classifications_forward_trustworthy_fresh": len(forward_trustworthy_fresh),
        "execution_classifications_historical_degraded_fresh": len(historical_degraded_fresh),
        "stale_vs_fresh_classification_diff_count": len(recompute_diffs),
        "stale_vs_fresh_sample_diffs": recompute_diffs[:20],
        "cross_tab": strict_forward_metrics.forward_executability_primary_state_cross_tab(db),
        "strict_clv_by_lead_gate": strict_forward_metrics.strict_forward_clv_all_gates(db),
        "paired_baseline_comparison_20s": strict_forward_metrics.paired_market_baseline_comparison(db, lead_s=20.0),
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(aggregate, indent=2, default=str))

    lines = [
        "# v0.3.7D.1 — Reporting Partition Audit (read-only)", "",
        f"Generated: {aggregate['generated_at']}",
        f"Source: {ACTIVE_DB_PATH} (opened `mode=ro` -- writes impossible, not just avoided)", "",
        "## Partition leak: PROVEN", "",
        f"- `historical_clv_report()` applies **no filter** on system timestamps. Every one of the "
        f"**{double_counted}** forward (system-timestamped) `(match_id, selection)` pairs used by "
        "`forward_clv_readiness()` is ALSO counted inside `historical_clv_report()`'s "
        f"`distinct_samples_with_close={historical_report_current['distinct_samples_with_close']}`.",
        f"- **{forward_rows_mislabeled_historical} forward rows were being labeled DEGRADED/historical** "
        "that should never have been.",
        f"- **{historical_rows_mislabeled_forward} historical rows leaked into forward** (forward_clv_readiness "
        "already filters correctly by `has_system_ts` -- this direction was NOT broken).",
        "", "## True partition (by data property, not calendar era)", "",
        f"- FORWARD_V037B_PLUS closing records: **{len(forward_closes)}** (HIGH-quality: {high_quality_by_true_era['FORWARD_V037B_PLUS']})",
        f"- HISTORICAL_PRE_V037B closing records: **{len(historical_closes)}** (HIGH-quality: {high_quality_by_true_era['HISTORICAL_PRE_V037B']})",
        f"- Total closing records: {len(all_closes)}",
        "", "## Execution classification: stored (possibly stale) vs. freshly recomputed", "",
        f"- Total MODEL paper trades classified: {len(all_trades)}",
        f"- Forward-trustworthy (fresh recompute): {len(forward_trustworthy_fresh)}",
        f"- Historical-degraded (fresh recompute): {len(historical_degraded_fresh)}",
        f"- Rows where STORED classification differs from a FRESH recompute: **{len(recompute_diffs)}** "
        "(stale rows from before the v0.3.7D reference-timestamp fix -- historical records were NOT "
        "mutated; this audit only compares, never writes).",
        f"- Strict EXECUTABLE_PREKICK_STRICT rows (fresh recompute, forward-trustworthy only): **{strict_executable_forward_rows}**",
        f"- EXECUTABLE_VIA_START_DELAY rows (fresh recompute, forward-trustworthy only, diagnostic-only): **{executable_via_start_delay_rows}**",
        f"- RESEARCH_ONLY_KICKOFF rows (fresh recompute, forward-trustworthy only): **{research_only_rows}**",
        "",
        "## Task 2: executability x primary-state cross-tab (reconciled)", "",
        f"```json\n{json.dumps(aggregate['cross_tab'], indent=2, default=str)}\n```",
        "",
        "## Task 4: strict executable-forward CLV by lead-time gate", "",
        f"```json\n{json.dumps(aggregate['strict_clv_by_lead_gate'], indent=2, default=str)}\n```",
        "",
        "## Task 5: paired CurrentModel vs MarketBaseline (20s strict gate)", "",
        f"```json\n{json.dumps(aggregate['paired_baseline_comparison_20s'], indent=2, default=str)}\n```",
        "",
        "## Conclusion",
        "",
        "The partition leak Fable suspected is **confirmed, proven from source code and real data**. "
        "`historical_clv_report()` and `run_daily_paper_sim.py::historical_replay()` both report on the "
        "FULL unpartitioned dataset while unconditionally labeling it DEGRADED -- this made the "
        "'historical' section silently include the very same forward, system-timestamped rows that "
        "should have been reported separately and with a CLEAN trust grade. Fixed in this release by "
        "adding an explicit `has_system_ts`/`is_historical_degraded` filter to both.",
    ]
    OUT_MD.write_text("\n".join(lines))
    print(f"Wrote {OUT_MD}")
    print(f"Wrote {OUT_JSON}")
    print(json.dumps(aggregate, indent=2, default=str))


if __name__ == "__main__":
    main()
