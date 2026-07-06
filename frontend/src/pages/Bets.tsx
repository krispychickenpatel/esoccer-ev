import { useEffect, useRef, useState } from 'react'
import { cls, fmtAm, fmtDT, fmtMoney, fmtPct, get, send, upload } from '../api'

const EMPTY = {
  placed_at: new Date().toISOString().slice(0, 16),
  sportsbook: '', league: '', match_label: '', selection: '', opponent: '',
  market: 'ML_3WAY', line: '', american_odds: '', stake: '', result: 'open',
  closing_american_odds: '', model_prob: '', notes: '',
}

export default function Bets() {
  const [rows, setRows] = useState<any[]>([])
  const [form, setForm] = useState<any>(EMPTY)
  const [showForm, setShowForm] = useState(false)
  const [msg, setMsg] = useState<{ t: string; ok: boolean } | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const load = () => get('/bets').then(setRows).catch(e => setMsg({ t: String(e), ok: false }))
  useEffect(() => { load() }, [])

  const set = (k: string) => (e: any) => setForm({ ...form, [k]: e.target.value })

  const submit = async () => {
    try {
      await send('POST', '/bets', {
        placed_at: new Date(form.placed_at).toISOString(),
        sportsbook: form.sportsbook, league: form.league, match_label: form.match_label,
        selection: form.selection, opponent: form.opponent, market: form.market,
        line: form.line === '' ? null : Number(form.line),
        american_odds: Number(form.american_odds),
        stake: Number(form.stake), result: form.result,
        closing_american_odds: form.closing_american_odds === '' ? null : Number(form.closing_american_odds),
        model_prob: form.model_prob === '' ? null : Number(form.model_prob),
        notes: form.notes,
      })
      setMsg({ t: 'Bet saved', ok: true }); setForm(EMPTY); setShowForm(false); load()
    } catch (e) { setMsg({ t: String(e), ok: false }) }
  }

  const importCsv = async (f: File) => {
    try {
      const r = await upload('/bets/import', f)
      setMsg({ t: `Imported ${r.imported} bets`, ok: true }); load()
    } catch (e) { setMsg({ t: String(e), ok: false }) }
  }

  const del = async (id: number) => {
    if (!confirm('Delete this bet?')) return
    await send('DELETE', `/bets/${id}`); load()
  }

  return (
    <>
      <h1>Bets</h1>
      <p className="sub">Log closing odds on every bet — CLV is the fastest honest read on your edge.</p>
      {msg && <div className={`msg ${msg.ok ? 'ok' : 'err'}`}>{msg.t}</div>}
      <div className="toolbar">
        <button className="primary" onClick={() => setShowForm(!showForm)}>{showForm ? 'Close form' : '+ Add bet'}</button>
        <button onClick={() => fileRef.current?.click()}>Import CSV</button>
        <a className="btn" href="/api/export/bets.csv">Export CSV</a>
        <input ref={fileRef} type="file" accept=".csv" style={{ display: 'none' }}
          onChange={e => e.target.files?.[0] && importCsv(e.target.files[0])} />
        <span className="muted mono">{rows.length} bets</span>
      </div>

      {showForm && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="form-grid">
            <div className="field"><label>Placed at</label><input type="datetime-local" value={form.placed_at} onChange={set('placed_at')} /></div>
            <div className="field"><label>Sportsbook</label><input value={form.sportsbook} onChange={set('sportsbook')} /></div>
            <div className="field"><label>League</label><input value={form.league} onChange={set('league')} /></div>
            <div className="field"><label>Match</label><input placeholder="Kray vs Boki" value={form.match_label} onChange={set('match_label')} /></div>
            <div className="field"><label>Selection</label><input value={form.selection} onChange={set('selection')} /></div>
            <div className="field"><label>Opponent</label><input value={form.opponent} onChange={set('opponent')} /></div>
            <div className="field"><label>Market</label>
              <select value={form.market} onChange={set('market')}>
                <option>ML_3WAY</option><option>SPREAD_2WAY</option><option>TOTAL</option>
              </select></div>
            <div className="field"><label>Line</label><input placeholder="-0.5" value={form.line} onChange={set('line')} /></div>
            <div className="field"><label>American odds</label><input placeholder="-110" value={form.american_odds} onChange={set('american_odds')} /></div>
            <div className="field"><label>Stake</label><input placeholder="100" value={form.stake} onChange={set('stake')} /></div>
            <div className="field"><label>Result</label>
              <select value={form.result} onChange={set('result')}>
                <option>open</option><option>win</option><option>loss</option><option>push</option><option>void</option>
              </select></div>
            <div className="field"><label>Closing odds</label><input placeholder="-125" value={form.closing_american_odds} onChange={set('closing_american_odds')} /></div>
            <div className="field"><label>Model prob (0-1)</label><input placeholder="0.55" value={form.model_prob} onChange={set('model_prob')} /></div>
            <div className="field"><label>Notes</label><input value={form.notes} onChange={set('notes')} /></div>
          </div>
          <button className="primary" onClick={submit} disabled={!form.american_odds || !form.stake}>Save bet</button>
        </div>
      )}

      <div className="card scroll-x">
        {rows.length === 0 ? <div className="empty">No bets yet. Add one or import bet_history.csv.</div> : (
          <table>
            <thead><tr>
              <th>Source</th><th>Placed</th><th>Book</th><th>Match</th><th>Sel</th><th>Mkt</th><th>Line</th>
              <th>Odds</th><th>Stake</th><th>Result</th><th>Profit</th><th>CLV</th><th>EV@place</th><th></th>
            </tr></thead>
            <tbody>
              {rows.map(b => (
                <tr key={b.id}>
                  <td>{b.data_source === 'manual_seed' ? <span className="badge amber" title="Your friend's real reconstructed screenshot evidence">SEED</span>
                    : b.data_source === 'synthetic_demo' ? <span className="badge muted" title="Quarantined non-real source">DEMO</span>
                    : b.data_source === 'betsapi' ? <span className="badge pos" title="Real, from BetsAPI">LIVE</span>
                    : <span className="badge" title="Manually entered or CSV-imported by you">MANUAL</span>}</td>
                  <td className="muted">{fmtDT(b.placed_at)}</td>
                  <td>{b.sportsbook}</td><td>{b.match_label}</td><td>{b.selection}</td>
                  <td className="muted">{b.market}</td><td>{b.line ?? '—'}</td>
                  <td>{fmtAm(b.american_odds)}</td><td>{fmtMoney(b.stake)}</td>
                  <td><span className={`badge ${b.result}`}>{b.result}</span></td>
                  <td className={cls(b.profit)}>{fmtMoney(b.profit)}</td>
                  <td className={cls(b.clv_pct)}>{fmtPct(b.clv_pct, 2)}</td>
                  <td className={cls(b.ev_at_placement)}>{fmtPct(b.ev_at_placement, 1)}</td>
                  <td><button className="danger" onClick={() => del(b.id)}>×</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  )
}
