import { useEffect, useState } from 'react'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { cls, fmtAm, fmtDT, fmtMoney, fmtPct, get } from '../api'

export default function Dashboard() {
  const [d, setD] = useState<any>(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    get('/dashboard').then(setD).catch(e => setErr(String(e)))
  }, [])

  if (err) return <div className="msg err">Backend unreachable: {err}. Start it with `uvicorn app.main:app --reload`.</div>
  if (!d) return <div className="empty">Loading…</div>

  const Stat = ({ label, value, hint, tone }: any) => (
    <div className="card stat">
      <h3>{label}</h3>
      <div className={`value ${tone ?? ''}`}>{value}</div>
      {hint && <div className="hint">{hint}</div>}
    </div>
  )

  return (
    <>
      <h1>Dashboard</h1>
      <p className="sub">Track results by CLV first, ROI second — ROI at small n is noise.</p>

      {(d.risk.daily_breached || d.risk.weekly_breached) && (
        <div className="msg err">
          RISK LIMIT BREACHED — {d.risk.daily_breached ? `daily P/L ${fmtMoney(d.risk.pnl_1d)} past limit` : ''}
          {d.risk.weekly_breached ? ` weekly P/L ${fmtMoney(d.risk.pnl_7d)} past limit` : ''}. Stop betting today.
        </div>
      )}

      {d.seed_split?.seed_warning && (
        <div className="msg amber">
          {d.seed_split.seed_warning} — verified: {d.seed_split.verified.settled} settled,
          ROI {d.seed_split.verified.roi_pct ?? '—'}% · seed/sample: {d.seed_split.seed.settled} settled,
          ROI {d.seed_split.seed.roi_pct ?? '—'}%. Toggle in Settings.
        </div>
      )}

      <div className="grid cols-4" style={{ marginBottom: 12 }}>
        <Stat label="Bankroll" value={fmtMoney(d.bankroll)} hint={`start ${fmtMoney(d.starting_bankroll)}`} />
        <Stat label="Profit / Loss" value={fmtMoney(d.profit)} tone={cls(d.profit)} />
        <Stat label="ROI" value={fmtPct(d.roi_pct)} tone={cls(d.roi_pct)}
          hint={d.roi_ci95_pct != null ? `±${d.roi_ci95_pct}% (95% CI)` : undefined} />
        <Stat label="Win Rate" value={d.win_rate != null ? `${d.win_rate}%` : '—'} hint={`${d.settled_bets} settled`} />
        <Stat label="Avg Odds" value={fmtAm(d.avg_american_odds)} hint={d.avg_decimal_odds ? `dec ${d.avg_decimal_odds}` : ''} />
        <Stat label="Avg CLV" value={fmtPct(d.avg_clv_pct, 2)} tone={cls(d.avg_clv_pct)} hint={`${d.clv_sample} bets with close`} />
        <Stat label="Open Alerts" value={d.open_alerts} tone={d.open_alerts > 0 ? 'amber' : ''} />
        <Stat label="Model Accuracy" value={d.model.hit_rate_pct != null ? `${d.model.hit_rate_pct}%` : '—'}
          hint={d.model.brier != null ? `Brier ${d.model.brier} · n=${d.model.graded_predictions}` : 'no graded predictions yet'} />
      </div>

      <div className="grid cols-2">
        <div className="card">
          <h3>Bankroll Over Time</h3>
          {d.bankroll_curve.length === 0 ? <div className="empty">No settled bets yet.</div> : (
            <ResponsiveContainer width="100%" height={220}>
              <LineChart data={d.bankroll_curve}>
                <CartesianGrid stroke="#212a38" strokeDasharray="3 3" />
                <XAxis dataKey="t" tickFormatter={fmtDT} stroke="#66748a" fontSize={10} />
                <YAxis stroke="#66748a" fontSize={10} domain={['auto', 'auto']} />
                <Tooltip contentStyle={{ background: '#12161f', border: '1px solid #212a38' }}
                  labelFormatter={fmtDT} />
                <Line type="monotone" dataKey="bankroll" stroke="#35d392" dot={false} strokeWidth={1.5} />
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>

        <div className="card">
          <h3>Markets — Best / Worst</h3>
          <table>
            <thead><tr><th>Market</th><th>Bets</th><th>Profit</th></tr></thead>
            <tbody>
              {[...d.best_markets, ...d.worst_markets.filter((w: any) =>
                !d.best_markets.some((b: any) => b.market === w.market))].map((m: any) => (
                <tr key={m.market}>
                  <td>{m.market}</td><td>{m.bets}</td>
                  <td className={cls(m.profit)}>{fmtMoney(m.profit)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3 style={{ marginTop: 16 }}>Risk Windows</h3>
          <table>
            <tbody>
              <tr><td className="muted">P/L last 24h</td><td className={cls(d.risk.pnl_1d)}>{fmtMoney(d.risk.pnl_1d)}</td>
                <td className="muted">limit {fmtMoney(-d.risk.max_daily_loss)}</td></tr>
              <tr><td className="muted">P/L last 7d</td><td className={cls(d.risk.pnl_7d)}>{fmtMoney(d.risk.pnl_7d)}</td>
                <td className="muted">limit {fmtMoney(-d.risk.max_weekly_loss)}</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <div className="card" style={{ marginTop: 12 }}>
        <h3>Recent Bets</h3>
        <div className="scroll-x">
          <table>
            <thead><tr><th>Placed</th><th>Match</th><th>Selection</th><th>Odds</th><th>Stake</th><th>Result</th><th>Profit</th></tr></thead>
            <tbody>
              {d.recent_bets.map((b: any) => (
                <tr key={b.id}>
                  <td className="muted">{fmtDT(b.placed_at)}</td>
                  <td>{b.match_label}</td><td>{b.selection}</td>
                  <td>{fmtAm(b.american_odds)}</td><td>{fmtMoney(b.stake)}</td>
                  <td><span className={`badge ${b.result}`}>{b.result}</span></td>
                  <td className={cls(b.profit)}>{fmtMoney(b.profit)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  )
}
