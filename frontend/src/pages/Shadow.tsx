import { useEffect, useState } from 'react'
import { cls, fmtMoney, get } from '../api'

const Tbl = ({ title, data }: { title: string; data: Record<string, any> }) => (
  <div className="card">
    <h3>{title}</h3>
    {Object.keys(data || {}).length === 0 ? <div className="empty">No data</div> : (
      <table>
        <thead><tr><th></th><th>n</th><th>Win%</th><th>ROI</th><th>P/L</th><th>Win CI95</th></tr></thead>
        <tbody>
          {Object.entries(data).sort((a: any, b: any) => (b[1].profit ?? 0) - (a[1].profit ?? 0)).map(([k, v]: any) => (
            <tr key={k}>
              <td>{k}</td>
              <td className="mono">{v.n}</td>
              <td className="mono">{v.win_rate != null ? `${Math.round(v.win_rate * 100)}%` : '—'}</td>
              <td className={`mono ${cls(v.roi_pct)}`}>{v.roi_pct ?? '—'}%</td>
              <td className={`mono ${cls(v.profit)}`}>{fmtMoney(v.profit)}</td>
              <td className="mono muted">{JSON.stringify(v.win_rate_ci95 ?? [])}</td>
            </tr>
          ))}
        </tbody>
      </table>
    )}
  </div>
)

export default function Shadow() {
  const [d, setD] = useState<any>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => { get('/shadow/dashboard').then(setD).catch(e => setErr(String(e))) }, [])

  if (err) return <div className="msg err">{err}</div>
  if (!d) return <div className="muted">Loading…</div>

  const s = d.settled_bets
  return (
    <>
      <h1>Shadow Model</h1>
      <p className="sub">Read-only by design — this is a report, not a tool you act on directly. Purpose: every pick your friend sends gets scored the same way the system scores itself (ROI, CLV, win rate with honest confidence intervals), so over time you can see objectively whether his picks beat the model, lose to it, or agree with it. It also feeds the ensemble — his picks count as one of six signals in Best Picks, weighted low until enough settled results prove they deserve more weight. A signal to measure, never assumed to be the truth.</p>
      {d.warning && <div className="msg amber">{d.warning}</div>}
      <div className="grid cols-4">
        <div className="stat"><label>Recommendations</label><b className="mono">{d.n_recommendations}</b></div>
        <div className="stat"><label>Avg lead time</label><b className="mono">{d.avg_lead_time_min ?? '—'} min</b></div>
        <div className="stat"><label>Pass/miss rate</label><b className="mono">{d.pass_miss_rate != null ? `${Math.round(d.pass_miss_rate * 100)}%` : '—'}</b></div>
        <div className="stat"><label>Settled bets</label><b className="mono">{s.n}</b></div>
        <div className="stat"><label>ROI</label><b className={`mono ${cls(s.roi_pct)}`}>{s.roi_pct ?? '—'}%</b></div>
        <div className="stat"><label>Win rate CI95</label><b className="mono">{JSON.stringify(s.win_rate_ci95)}</b></div>
        <div className="stat"><label>Avg CLV</label><b className="mono">{s.avg_clv_pct ?? '—'}%</b></div>
        <div className="stat"><label>Statuses</label><b className="mono small">{Object.entries(d.status_counts).map(([k, v]) => `${k}:${v}`).join(' ')}</b></div>
      </div>
      <p className="muted small">CI95 is the honest range: with {s.n} settled bets, the true win rate could plausibly be anywhere inside it. A 10/10 screenshot record proves less than it feels like it does.</p>
      <div className="grid cols-2">
        <Tbl title="P/L by player" data={d.profit_by_player} />
        <Tbl title="P/L by market" data={d.profit_by_market} />
        <Tbl title="P/L by odds range" data={d.profit_by_odds_range} />
        <Tbl title="P/L by execution latency" data={d.profit_by_latency} />
      </div>
      <div className="card" style={{ marginTop: 16 }}>
        <h3>Consensus performance (friend vs models)</h3>
        {Object.keys(d.consensus_performance || {}).length === 0
          ? <div className="empty">No settled picks linked to recommendations yet. Generate picks while recommendations are pending, then let matches settle.</div>
          : <Tbl title="" data={d.consensus_performance} />}
      </div>
    </>
  )
}
