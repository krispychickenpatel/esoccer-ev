import { useEffect, useState } from 'react'
import { fmtAm, fmtDT, fmtPct, get, send } from '../api'

export default function PredictionLab() {
  const [d, setD] = useState<any>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = () => {
    get('/lab/dashboard').then(setD).catch(e => setErr(String(e)))
  }
  useEffect(load, [])

  async function run(path: string) {
    setBusy(true); setErr(null)
    try { await send('POST', path); await load() }
    catch (e: any) { setErr(String(e)) }
    finally { setBusy(false) }
  }

  if (err) return <div className="msg err">{err}</div>
  if (!d) return <div className="muted">Loading…</div>
  const totals = d.totals || {}
  const groups = d.model_comparison || []
  const recent = d.recent_predictions || []

  return (
    <>
      <h1>Prediction Lab</h1>
      <p className="sub">Frozen horizon predictions → reality capture → self-scoring. This is where the platform learns from every match before it risks money.</p>

      <div className="toolbar">
        <button disabled={busy} onClick={() => run('/lab/freeze-due?allow_late=true')}>Freeze due predictions</button>
        <button disabled={busy} onClick={() => run('/lab/capture-reality')}>Capture reality</button>
        <button disabled={busy} onClick={() => run('/lab/score')}>Score predictions</button>
        <button disabled={busy} onClick={() => run('/lab/run-cycle')}>Run full cycle</button>
      </div>

      <div className="grid cols-4">
        <div className="stat"><label>Frozen predictions</label><b className="mono">{totals.frozen_predictions}</b></div>
        <div className="stat"><label>Scored predictions</label><b className="mono">{totals.scored_predictions}</b></div>
        <div className="stat"><label>Pending scores</label><b className="mono">{totals.pending_scores ?? '—'}</b></div>
        <div className="stat"><label>Gold reality rows</label><b className="mono">{totals.gold_reality_rows}</b></div>
      </div>

      {d.ledger_integrity && d.ledger_integrity.mismatched > 0 && (
        <div className="msg err" style={{ marginTop: 12 }}>
          Ledger integrity failure: {d.ledger_integrity.mismatched} frozen prediction(s) no longer
          match their sha256 freeze hash. These rows were edited after freezing and must not be
          trusted as evidence. IDs: {d.ledger_integrity.mismatched_ids.join(', ')}
        </div>
      )}

      <div className="grid cols-2" style={{ marginTop: 16 }}>
        <div className="card">
          <h3>Error buckets</h3>
          {Object.keys(d.error_buckets || {}).length === 0 ? <div className="empty">No scored predictions yet.</div> : (
            <table><tbody>{Object.entries(d.error_buckets).map(([k, v]: any) => (
              <tr key={k}><td>{k}</td><td className="mono">{v}</td></tr>
            ))}</tbody></table>
          )}
          <p className="muted small">Bad calls are separated into outcome, steam direction, steam magnitude, execution timing, data, and risk buckets.</p>
        </div>
        <div className="card">
          <h3>Dataset tiers</h3>
          <table><tbody>
            <tr><td>Gold</td><td className="mono">{d.dataset_tiers?.gold || 0}</td></tr>
            <tr><td>Silver</td><td className="mono">{d.dataset_tiers?.silver || 0}</td></tr>
            <tr><td>Rejected</td><td className="mono">{d.dataset_tiers?.rejected || 0}</td></tr>
          </tbody></table>
          <p className="muted small">Train only on Gold. Silver is research-only. Rejected means missing/late first-live, missing result, or unusable capture.</p>
        </div>
      </div>

      <div className="card scroll-x" style={{ marginTop: 16 }}>
        <h3>Model comparison by horizon</h3>
        {groups.length === 0 ? <div className="empty">No scored model groups yet.</div> : (
          <table>
            <thead><tr><th>Model</th><th>Horizon</th><th>Frozen</th><th>Scored</th><th>Gold</th><th>Winner acc</th><th>Steam acc</th><th>Magnitude err</th><th>Avg score</th></tr></thead>
            <tbody>{groups.map((g: any) => (
              <tr key={`${g.model_version}-${g.horizon_label}`}>
                <td className="small mono">{g.model_version}</td><td>{g.horizon_label}</td>
                <td className="mono">{g.frozen_n}</td><td className="mono">{g.scored_n}</td><td className="mono">{g.gold_n}</td>
                <td className="mono">{g.winner_accuracy == null ? '—' : `${Math.round(g.winner_accuracy * 100)}%`}</td>
                <td className="mono">{g.steam_direction_accuracy == null ? '—' : `${Math.round(g.steam_direction_accuracy * 100)}%`}</td>
                <td className="mono">{g.avg_magnitude_error_cents == null ? '—' : `${g.avg_magnitude_error_cents}¢`}</td>
                <td className="mono">{g.avg_score ?? '—'}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
        <p className="muted small">{d.next_required_sample}</p>
      </div>

      <div className="card scroll-x" style={{ marginTop: 16 }}>
        <h3>Recent frozen predictions</h3>
        {recent.length === 0 ? <div className="empty">No frozen predictions yet. Run the poller or use Freeze due predictions near scheduled matches.</div> : (
          <table>
            <thead><tr><th>Time</th><th>Horizon</th><th>Match</th><th>Side</th><th>Current</th><th>Pred. live</th><th>Steam</th><th>EV</th><th>Action</th><th>Score</th><th>Error</th></tr></thead>
            <tbody>{recent.map((p: any) => (
              <tr key={p.id}>
                <td className="small mono">{fmtDT(p.prediction_time)}</td>
                <td>{p.horizon_label}</td>
                <td>{p.match}<br /><span className="muted small">{p.league}</span></td>
                <td>{p.selection}</td>
                <td className="mono">{fmtAm(p.current_american)}</td>
                <td className="mono">{fmtAm(p.predicted_first_live_american)}</td>
                <td className="mono">{Math.round((p.steam_probability || 0) * 100)}%</td>
                <td className="mono">{fmtPct(p.ev_pct)}</td>
                <td><span className={`badge ${p.action === 'BET' ? 'pos' : p.action === 'PASS' ? 'neg' : 'amber'}`}>{p.action}</span></td>
                <td className="mono">{p.score ?? '—'}</td>
                <td className="small">{p.error_bucket ?? '—'}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </div>
    </>
  )
}
