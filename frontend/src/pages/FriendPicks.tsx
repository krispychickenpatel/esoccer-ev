import { useEffect, useState } from 'react'
import { fmtDT, fmtMoney, fmtPct, get, send } from '../api'

const BLANK = {
  pick_side: 'home', home_name: '', away_name: '', odds_at_pick_decimal: '',
  book_seen: '', league: '', reason: '', confidence: '',
}

export default function FriendPicks() {
  const [items, setItems] = useState<any[]>([])
  const [report, setReport] = useState<any>(null)
  const [form, setForm] = useState<any>(BLANK)
  const [msg, setMsg] = useState<{ t: string; ok: boolean } | null>(null)
  const [busy, setBusy] = useState(false)

  const load = () => {
    get('/friend-picks').then(r => setItems(r.items)).catch(e => setMsg({ t: String(e), ok: false }))
    get('/friend-picks/report').then(setReport).catch(() => {})
  }
  useEffect(load, [])

  async function submit() {
    setBusy(true)
    try {
      const payload = { ...form, odds_at_pick_decimal: Number(form.odds_at_pick_decimal) }
      await send('POST', '/friend-picks', payload)
      setForm(BLANK)
      setMsg({ t: 'Friend pick saved', ok: true })
      load()
    } catch (e: any) { setMsg({ t: String(e), ok: false }) }
    finally { setBusy(false) }
  }

  const set = (k: string) => (e: any) => setForm({ ...form, [k]: e.target.value })

  const pending = items.filter(p => p.resolution_status !== 'RESOLVED' || p.scoring_status !== 'scored')
  const scored = items.filter(p => p.resolution_status === 'RESOLVED' && p.scoring_status === 'scored')

  return (
    <>
      <h1>Friend Picks</h1>
      <p className="sub">A friend's pick is a timestamped signal source, not truth. Scored separately from the model, against the same market-only baseline.</p>
      {msg && <div className={`msg ${msg.ok ? 'ok' : 'err'}`}>{msg.t}</div>}

      <div className="card" style={{ maxWidth: 640 }}>
        <h3>Fast entry</h3>
        <div className="form-grid">
          <div className="field"><label>Side</label>
            <select value={form.pick_side} onChange={set('pick_side')}>
              <option value="home">Home</option><option value="away">Away</option><option value="draw">Draw</option>
            </select></div>
          <div className="field"><label>Home player/team</label><input value={form.home_name} onChange={set('home_name')} /></div>
          <div className="field"><label>Away player/team</label><input value={form.away_name} onChange={set('away_name')} /></div>
          <div className="field"><label>Odds at pick (decimal)</label><input value={form.odds_at_pick_decimal} onChange={set('odds_at_pick_decimal')} placeholder="2.20" /></div>
          <div className="field"><label>Book seen</label><input value={form.book_seen} onChange={set('book_seen')} placeholder="bet365 app" /></div>
          <div className="field"><label>League</label><input value={form.league} onChange={set('league')} placeholder="Esoccer Battle - 8 mins play" /></div>
          <div className="field"><label>Confidence</label>
            <select value={form.confidence} onChange={set('confidence')}>
              <option value="">—</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option>
            </select></div>
        </div>
        <div className="field"><label>Reason (optional)</label><input value={form.reason} onChange={set('reason')} /></div>
        <button className="primary" disabled={busy || !form.home_name || !form.away_name || !form.odds_at_pick_decimal}
          onClick={submit}>Save pick</button>
        <p className="muted small">pick_timestamp defaults to now. Entering a pick more than 2 minutes after it was actually made marks it backfilled — it can never be scored as if known earlier than this moment.</p>
      </div>

      {report && (
        <div className="grid cols-4" style={{ marginTop: 16 }}>
          <div className="stat"><label>Total picks</label><b className="mono">{report.total_picks}</b></div>
          <div className="stat"><label>Pending scoring</label><b className="mono">{report.pending_scoring}</b></div>
          <div className="stat"><label>Winner accuracy</label><b className="mono">{report.winner_accuracy != null ? `${Math.round(report.winner_accuracy * 100)}%` : '—'}</b></div>
          <div className="stat"><label>Steam direction accuracy</label><b className="mono">{report.steam_direction_accuracy != null ? `${Math.round(report.steam_direction_accuracy * 100)}%` : '—'}</b></div>
        </div>
      )}
      {report && (
        <div className="grid cols-2" style={{ marginTop: 12 }}>
          <div className="stat"><label>Total paper P/L</label><b className={`mono ${report.total_paper_pl_usd > 0 ? 'pos' : report.total_paper_pl_usd < 0 ? 'neg' : ''}`}>{fmtMoney(report.total_paper_pl_usd)}</b></div>
          <div className="stat"><label>Avg proxy CLV</label><b className="mono">{fmtPct(report.avg_proxy_clv_pct)}</b></div>
        </div>
      )}
      <p className="muted small" style={{ marginTop: 4 }}>Steam accuracy, CLV, and paper P/L are shown only for scored picks — an unscored pick shows status, not a fabricated metric.</p>

      <div className="card scroll-x" style={{ marginTop: 16 }}>
        <h3>Pending / unresolved ({pending.length})</h3>
        {pending.length === 0 ? <div className="empty">Nothing pending.</div> : (
          <table>
            <thead><tr><th>Created</th><th>Match</th><th>Side</th><th>Odds</th><th>Book</th><th>Resolution</th><th>Backfilled</th></tr></thead>
            <tbody>{pending.map(p => (
              <tr key={p.id}>
                <td className="mono small">{fmtDT(p.created_at)}</td>
                <td>{p.home_name} vs {p.away_name}</td>
                <td>{p.pick_side}</td>
                <td className="mono">{p.odds_at_pick_decimal}</td>
                <td>{p.book_seen}</td>
                <td><span className={`badge ${p.resolution_status === 'RESOLVED' ? 'pos' : 'amber'}`}>{p.resolution_status}</span></td>
                <td>{p.is_backfilled ? <span className="badge amber">late</span> : ''}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </div>

      <div className="card scroll-x" style={{ marginTop: 16 }}>
        <h3>Scored ({scored.length})</h3>
        {scored.length === 0 ? <div className="empty">No scored picks yet.</div> : (
          <table>
            <thead><tr><th>Match</th><th>Side</th><th>Winner correct</th><th>Steam correct</th><th>Proxy CLV</th><th>Paper P/L</th><th>vs Model</th><th>vs Baseline</th><th>Error bucket</th></tr></thead>
            <tbody>{scored.map(p => (
              <tr key={p.id}>
                <td>{p.home_name} vs {p.away_name}</td>
                <td>{p.pick_side}</td>
                <td>{p.score?.winner_correct === null ? '—' : p.score?.winner_correct ? 'yes' : 'no'}</td>
                <td>{p.score?.steam_direction_correct === null ? '—' : p.score?.steam_direction_correct ? 'yes' : 'no'}</td>
                <td className="mono">{fmtPct(p.score?.proxy_clv_pct)}</td>
                <td className={`mono ${(p.score?.paper_pl_usd || 0) > 0 ? 'pos' : (p.score?.paper_pl_usd || 0) < 0 ? 'neg' : ''}`}>{fmtMoney(p.score?.paper_pl_usd)}</td>
                <td>{p.score?.vs_model_comparison}</td>
                <td>{p.score?.vs_baseline_comparison}</td>
                <td className="small">{p.score?.error_bucket}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </div>
    </>
  )
}
