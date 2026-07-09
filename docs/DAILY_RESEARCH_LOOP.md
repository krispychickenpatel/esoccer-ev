# Daily Research Loop (v0.3.7C)

```bash
python3 scripts/research/generate_daily_research.py
```

Writes `notes/research/YYYY-MM-DD-daily-research.md`,
`notes/research/latest_research.json`, and appends a dated block to
`notes/research/experiment_backlog.md` (append-only — never overwritten,
never truncated by this script).

## Sections

- **A. Data quality** — odds rows today, system-timestamp completeness %,
  strict-close candidates, cumulative clean close count, market
  availability episodes. Computes `data_state`: `CLEAN` (>=80% system-
  timestamp completeness today), `DEGRADED` (some but not enough), or
  `BLOCKED` (zero rows today).
- **B. Execution learning** — primary-state and diagnostic-flag counts
  from `execution_classifier_v2` (see `docs/STABILIZATION_CHANGELOG_v0.3.7C.md`
  and the v0.3.7B execution-classification report for what each state means).
- **C. CLV learning** — historical (provider-time, always `DEGRADED`) vs.
  forward (system-availability-time, `PENDING` until real timestamped
  closes exist) CLV, kept in **separate keys**, never blended. ROI by delay
  bucket, reusing the paper-trade engine's own numbers.
- **D. Baseline comparison** — CurrentModel vs. FavoriteBaseline vs. a
  50% no-edge/coin-flip baseline, plus Brier score and calibration buckets,
  gated by distinct-sample count (`NOT ENOUGH DATA` / `DIRECTIONAL` /
  `EVIDENCE` / `DECISION-GRADE` at n=50/150/400).
- **E. Steam/price movement learning** — direction-alignment %, average
  move size, and how many paper-trade rows had a price still observable at
  the 20s/30s delay marks (this is a *reachability* count, not a
  profitability claim).
- **F. Friend/manual pick learning** — clean vs. retro/result-known counts
  from `notes/friend_picks.csv`, correlated-leg grouping by
  `signal_group_id`. Retro picks are always excluded from the clean sample.
- **G. Hypothesis generator** — always exactly 3 ranked hypotheses. Real
  data-driven candidates are generated first (execution bottleneck, model-
  vs-baseline margin, market-availability prevalence); if fewer than 3 real
  candidates exist, grounded fallback hypotheses about data-availability
  gaps (feed density, friend-pick coverage, spot-check coverage) fill the
  remainder — never fabricated claims about the model.
- **H. Experiment backlog** — one appended block per run, in
  `notes/research/experiment_backlog.md`.
- **I. Self-challenge** — argues against the day's own conclusions before
  they're allowed to stand: what could be wrong, what's most likely noise,
  what would waste time if chased, what to ignore until sample size
  improves, what would actually change tomorrow's plan.
- **J. Final recommendation** — exactly one of: `collect more data`,
  `fix feed/polling`, `inspect market availability`, `tune entry floor
  later`, `run spot-checks`, `defer model work`, `start candidate model
  experiment only if gates allow it`. Priority-ordered — health `FAIL`
  beats everything else, then data-blocked, then market-availability
  prevalence, then falls back to `collect more data`.

## Sample-size gates (shared with the paper-sim runner)

| n | Gate |
|---|---|
| < 50 | NOT ENOUGH DATA |
| 50–149 | DIRECTIONAL |
| 150–399 | EVIDENCE |
| >= 400 | DECISION-GRADE |

No section reports a stronger gate than its actual sample size supports.
Every section checks its gate **before** doing expensive computation, not
after — a `NOT ENOUGH DATA` section skips the elaborate stats entirely
rather than computing and then discarding them.
