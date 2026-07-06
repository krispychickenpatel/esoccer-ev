import { useEffect, useState } from 'react'
import { fmtDT, get, send } from '../api'

export default function Predictions() {
  const [rows, setRows] = useState<any[]>([])
  const [busy, setBusy] = useState(false)
  const load = () => get('/predictions').then(setRows).catch(() => {})
  useEffect(() => { load() }, [])

  const generate = async () => {
    setBusy(true)
    await send('POST', '/predictions/generate').catch(() => {})
    await load(); setBusy(false)
  }

  const pct = (v: number) => (v * 100).toFixed(1) + '%'

  return (
    <>
      <h1>Predictions</h1>
      <p className="sub">This shows ONE signal only — the Elo/Davidson model's raw win/draw/loss probability, with a scoreboard of hits vs misses once matches finish. It never looks at odds, EV, timing, or your friend's picks. For the actual decision (does the price justify a bet, right now) see Best Picks, which combines this with five other signals. elo_davidson_v1 · fair odds = 1/p · low confidence means thin match history.</p>
      <div className="toolbar">
        <button className="primary" onClick={generate} disabled={busy}>
          {busy ? 'Generating…' : 'Generate for upcoming matches'}</button>
        <a className="btn" href="/api/export/predictions.csv">Export CSV</a>
        <span className="muted mono">{rows.length} predictions</span>
      </div>
      <div className="card scroll-x">
        {rows.length === 0 ? <div className="empty">No predictions yet — generate them once matches and ratings exist.</div> : (
          <table>
            <thead><tr><th>Match</th><th>Kickoff</th><th>P(H)</th><th>P(D)</th><th>P(A)</th>
              <th>Fair H</th><th>Fair D</th><th>Fair A</th><th>Conf</th><th>Elo Δ</th><th>Result</th></tr></thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.id}>
                  <td>{r.match}</td>
                  <td className="muted">{fmtDT(r.start_time)}</td>
                  <td>{pct(r.p_home)}</td><td>{pct(r.p_draw)}</td><td>{pct(r.p_away)}</td>
                  <td className="muted">{r.fair_home.toFixed(2)}</td>
                  <td className="muted">{r.fair_draw.toFixed(2)}</td>
                  <td className="muted">{r.fair_away.toFixed(2)}</td>
                  <td className={r.confidence < 0.5 ? 'amber' : 'pos'}>{r.confidence.toFixed(2)}</td>
                  <td>{r.features?.elo_diff ?? '—'}</td>
                  <td>{r.finished ? <span className="badge">{r.actual}</span> : <span className="muted">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  )
}
