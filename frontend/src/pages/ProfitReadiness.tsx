import { useEffect, useState } from 'react'
import { get } from '../api'

function StatusBadge({ status }: { status: string }) {
  const cls = status === 'PASS' ? 'pos' : status === 'FAIL' ? 'neg' : 'amber'
  return <span className={`badge ${cls}`}>{status}</span>
}

export default function ProfitReadiness() {
  const [d, setD] = useState<any>(null)
  const [we, setWe] = useState<any>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    get('/profit/gates').then(setD).catch(e => setErr(String(e)))
    get('/profit/winner-edge').then(setWe).catch(() => {})
  }, [])

  if (err) return <div className="msg err">{err}</div>
  if (!d) return <div className="muted">Loading…</div>

  const g = d.gates
  const h = d.pipeline_health

  return (
    <>
      <h1>Profit Readiness</h1>
      <p className="sub">Default answer is NO / NOT ENOUGH DATA unless every gate genuinely passes. This page does not soften that.</p>

      <div className="card" style={{ marginTop: 8 }}>
        <h3>Ready for live small stakes: <StatusBadge status={d.ready_for_live_small_stakes} /></h3>
        <p className="muted small">{d.disclaimer}</p>
      </div>

      <div className="grid cols-4" style={{ marginTop: 16 }}>
        <div className="stat"><label>Matches collecting</label><b><StatusBadge status={h.matches_collecting.status} /></b><p className="muted small">n={h.matches_collecting.n}</p></div>
        <div className="stat"><label>Odds collecting</label><b><StatusBadge status={h.odds_collecting.status} /></b><p className="muted small">n={h.odds_collecting.n}</p></div>
        <div className="stat"><label>Predictions freezing</label><b><StatusBadge status={h.predictions_freezing.status} /></b><p className="muted small">n={h.predictions_freezing.n}</p></div>
        <div className="stat"><label>Results scoring</label><b><StatusBadge status={h.results_scoring.status} /></b><p className="muted small">n={h.results_scoring.n}</p></div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h3>Feed gate</h3>
        <p className="muted small">{g.feed_gate.note}</p>
        <table><tbody>
          <tr><td>Pre-kickoff freshness (≤60s before KO, ≥80% of matches)</td>
            <td><StatusBadge status={g.feed_gate.pre_kickoff.status} /></td>
            <td className="mono">{g.feed_gate.pre_kickoff.pct_fresh_pre_kick ?? '—'}% (n={g.feed_gate.pre_kickoff.n})</td></tr>
          <tr><td>Live-open-manual (30-45s stress delay)</td>
            <td><StatusBadge status={g.feed_gate.live_open_manual.status} /></td>
            <td className="mono">median {g.feed_gate.live_open_manual.median_latency_s ?? '—'}s / p95 {g.feed_gate.live_open_manual.p95_latency_s ?? '—'}s (n={g.feed_gate.live_open_manual.n})</td></tr>
        </tbody></table>
      </div>

      <div className="grid cols-2" style={{ marginTop: 16 }}>
        <div className="card">
          <h3>Signal gate — model</h3>
          <p className="muted small">Gate uses distinct_samples (independent match+selection outcomes), never raw_rows (repeated horizons of the same outcome).</p>
          <table><tbody>
            <tr><td>Status</td><td><StatusBadge status={g.signal_gate_model.status} /></td></tr>
            <tr><td>Steam accuracy</td><td className="mono">{g.signal_gate_model.accuracy_pct ?? '—'}%</td></tr>
            <tr><td>Baseline accuracy</td><td className="mono">{g.signal_gate_model.baseline_accuracy_pct ?? '—'}%</td></tr>
            <tr><td>Margin</td><td className="mono">{g.signal_gate_model.margin_pts ?? '—'} pts</td></tr>
            <tr><td>n (= distinct_samples)</td><td className="mono">{g.signal_gate_model.n}</td></tr>
            <tr><td>distinct_samples</td><td className="mono">{g.signal_gate_model.distinct_samples ?? '—'}</td></tr>
            <tr><td>raw_rows (not used for the gate)</td><td className="mono muted">{g.signal_gate_model.raw_rows ?? '—'}</td></tr>
          </tbody></table>
        </div>
        <div className="card">
          <h3>Signal gate — friend</h3>
          <p className="muted small">Gate uses distinct_samples (independent match+selection outcomes), never raw_rows (repeated horizons of the same outcome).</p>
          <table><tbody>
            <tr><td>Status</td><td><StatusBadge status={g.signal_gate_friend.status} /></td></tr>
            <tr><td>Steam accuracy</td><td className="mono">{g.signal_gate_friend.accuracy_pct ?? '—'}%</td></tr>
            <tr><td>Baseline accuracy</td><td className="mono">{g.signal_gate_friend.baseline_accuracy_pct ?? '—'}%</td></tr>
            <tr><td>Margin</td><td className="mono">{g.signal_gate_friend.margin_pts ?? '—'} pts</td></tr>
            <tr><td>n (= distinct_samples)</td><td className="mono">{g.signal_gate_friend.n}</td></tr>
            <tr><td>distinct_samples</td><td className="mono">{g.signal_gate_friend.distinct_samples ?? '—'}</td></tr>
            <tr><td>raw_rows (not used for the gate)</td><td className="mono muted">{g.signal_gate_friend.raw_rows ?? '—'}</td></tr>
          </tbody></table>
        </div>
      </div>

      <div className="grid cols-3" style={{ marginTop: 16 }}>
        <div className="card">
          <h3>Execution gate</h3>
          <table><tbody>
            <tr><td>Status</td><td><StatusBadge status={g.execution_gate.status} /></td></tr>
            <tr><td>30s survival rate</td><td className="mono">{g.execution_gate.survival_pct ?? '—'}%</td></tr>
            <tr><td>n (model / friend)</td><td className="mono">{g.execution_gate.n} ({g.execution_gate.model_n ?? '—'} / {g.execution_gate.friend_n ?? '—'})</td></tr>
          </tbody></table>
        </div>
        <div className="card">
          <h3>Book gate</h3>
          <table><tbody>
            <tr><td>Status</td><td><StatusBadge status={g.book_gate.status} /></td></tr>
            <tr><td>Verified books</td><td className="small">{g.book_gate.verified_books.join(', ') || '—'}</td></tr>
          </tbody></table>
        </div>
        <div className="card">
          <h3>Risk gate</h3>
          <table><tbody>
            <tr><td>Status</td><td><StatusBadge status={g.risk_gate.status} /></td></tr>
            <tr><td>Max drawdown</td><td className="mono">{g.risk_gate.max_drawdown_units ?? '—'} units</td></tr>
            <tr><td>n (model / friend)</td><td className="mono">{g.risk_gate.n} ({g.risk_gate.model_n ?? '—'} / {g.risk_gate.friend_n ?? '—'})</td></tr>
          </tbody></table>
        </div>
      </div>

      <div className="grid cols-2" style={{ marginTop: 16 }}>
        <div className="card">
          <h3>Winner edge gate — model</h3>
          <table><tbody>
            <tr><td>Status</td><td><StatusBadge status={g.winner_edge_gate_model.status} /></td></tr>
            <tr><td>Winner accuracy</td><td className="mono">{g.winner_edge_gate_model.winner_accuracy_pct ?? '—'}%</td></tr>
            <tr><td>Favorite baseline</td><td className="mono">{g.winner_edge_gate_model.favorite_baseline_pct ?? '—'}%</td></tr>
            <tr><td>Margin</td><td className={`mono ${(g.winner_edge_gate_model.margin_pts ?? 0) > 0 ? 'pos' : (g.winner_edge_gate_model.margin_pts ?? 0) < 0 ? 'neg' : ''}`}>{g.winner_edge_gate_model.margin_pts ?? '—'} pts</td></tr>
            <tr><td>n</td><td className="mono">{g.winner_edge_gate_model.n}</td></tr>
          </tbody></table>
        </div>
        <div className="card">
          <h3>Winner edge gate — friend</h3>
          <table><tbody>
            <tr><td>Status</td><td><StatusBadge status={g.winner_edge_gate_friend.status} /></td></tr>
            <tr><td>Winner accuracy</td><td className="mono">{g.winner_edge_gate_friend.winner_accuracy_pct ?? '—'}%</td></tr>
            <tr><td>Favorite baseline</td><td className="mono">{g.winner_edge_gate_friend.favorite_baseline_pct ?? '—'}%</td></tr>
            <tr><td>Margin</td><td className="mono">{g.winner_edge_gate_friend.margin_pts ?? '—'} pts</td></tr>
            <tr><td>n</td><td className="mono">{g.winner_edge_gate_friend.n}</td></tr>
          </tbody></table>
        </div>
      </div>

      <div className="grid cols-2" style={{ marginTop: 16 }}>
        <div className="stat"><label>Viable pre-kickoff</label><b><StatusBadge status={d.viable_pre_kickoff} /></b></div>
        <div className="stat"><label>Viable at 30-45s delay</label><b><StatusBadge status={d.viable_at_30_45s_delay} /></b></div>
      </div>

      {we && (
        <>
          <h2 style={{ marginTop: 24 }}>Winner Edge Truth Layer</h2>
          <div className="msg amber">{we.warning}</div>
          <div className="grid cols-2" style={{ marginTop: 8 }}>
            <div className="card">
              <h3>Model</h3>
              <table><tbody>
                <tr><td>Winner accuracy</td><td className="mono">{we.model.winner_accuracy_pct ?? '—'}%</td></tr>
                <tr><td>Favorite baseline</td><td className="mono">{we.model.favorite_baseline_accuracy_pct ?? '—'}%</td></tr>
                <tr><td>Margin vs favorite</td><td className={`mono ${(we.model.margin_vs_favorite_pts ?? 0) >= 0 ? 'pos' : 'neg'}`}>{we.model.margin_vs_favorite_pts ?? '—'} pts</td></tr>
                <tr><td>Distinct samples</td><td className="mono">{we.model.distinct_samples}</td></tr>
                <tr><td>Brier score</td><td className="mono">{we.model.brier_score ?? '—'}</td></tr>
                <tr><td>Avg model edge</td><td className="mono">{we.model.avg_model_edge_pct ?? '—'}%</td></tr>
              </tbody></table>
              <h4 style={{ marginTop: 12 }}>Paper ROI by delay</h4>
              <table>
                <thead><tr>{Object.keys(we.model.roi_pct_by_delay_seconds).map(d => <th key={d}>{d}s</th>)}</tr></thead>
                <tbody><tr>{Object.values(we.model.roi_pct_by_delay_seconds).map((v: any, i) => (
                  <td key={i} className={`mono ${v > 0 ? 'pos' : v < 0 ? 'neg' : ''}`}>{v ?? '—'}%</td>
                ))}</tr></tbody>
              </table>
              <h4 style={{ marginTop: 12 }}>Fill / missed-price rate by delay</h4>
              <table>
                <thead><tr><th>Delay</th><th>Fill %</th><th>Missed %</th></tr></thead>
                <tbody>{Object.entries(we.model.paper_pl_and_fill_by_delay_seconds).map(([dl, v]: any) => (
                  <tr key={dl}><td>{dl}s</td><td className="mono">{v.fill_rate_pct ?? '—'}%</td><td className="mono">{v.missed_price_rate_pct ?? '—'}%</td></tr>
                ))}</tbody>
              </table>
              {we.model.sample_warnings.map((w: string) => <p key={w} className="muted small">{w}</p>)}
            </div>
            <div className="card">
              <h3>Friend</h3>
              <table><tbody>
                <tr><td>Winner accuracy</td><td className="mono">{we.friend.winner_accuracy_pct ?? '—'}%</td></tr>
                <tr><td>Favorite baseline</td><td className="mono">{we.friend.favorite_baseline_accuracy_pct ?? '—'}%</td></tr>
                <tr><td>Scored picks</td><td className="mono">{we.friend.scored_picks}</td></tr>
                <tr><td>Clean pre-kick / backfilled / likely test</td><td className="mono">{we.friend.clean_pre_kick_picks} / {we.friend.backfilled_picks} / {we.friend.likely_test_artifacts}</td></tr>
              </tbody></table>
              <p className="muted small">{we.friend.book_proxy_caveat}</p>
              {we.friend.sample_warnings.map((w: string) => <p key={w} className="muted small">{w}</p>)}
            </div>
          </div>
        </>
      )}
    </>
  )
}
