import { useEffect, useRef, useState } from 'react'
import { fmtDT, get, send, upload } from '../api'

export default function Matches() {
  const [rows, setRows] = useState<any[]>([])
  const [msg, setMsg] = useState<{ t: string; ok: boolean } | null>(null)
  const [busy, setBusy] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const load = () => get('/matches').then(setRows).catch(e => setMsg({ t: String(e), ok: false }))
  useEffect(() => { load() }, [])

  const importCsv = async (f: File) => {
    try {
      const r = await upload('/matches/import', f)
      setMsg({ t: `Created ${r.created}, updated ${r.updated}. Ratings rebuilt.`, ok: true }); load()
    } catch (e) { setMsg({ t: String(e), ok: false }) }
  }

  const pullSchedule = async () => {
    setBusy(true)
    try {
      const r = await send('POST', '/provider/pull-upcoming')
      setMsg({ t: `BetsAPI schedule pull: fetched ${r.fetched}, scoped ${r.scoped}, created ${r.created}, updated ${r.updated}.`, ok: true })
      load()
    } catch (e) { setMsg({ t: String(e), ok: false }) } finally { setBusy(false) }
  }

  const pullOdds = async () => {
    setBusy(true)
    try {
      const r = await send('POST', '/provider/pull-odds')
      setMsg({ t: `Odds pull: ${r.matches} matches, ${r.snapshots_written} snapshots, ${r.empty_market_calls} empty market calls.`, ok: true })
      load()
    } catch (e) { setMsg({ t: String(e), ok: false }) } finally { setBusy(false) }
  }

  const realModeClean = async () => {
    if (!confirm('Remove demo/seed bets and switch metrics to verified/API data only?')) return
    setBusy(true)
    try {
      const r = await send('POST', '/admin/real-mode-clean')
      setMsg({ t: `Real mode clean complete. Removed ${JSON.stringify(r.removed)}.`, ok: true })
      load()
    } catch (e) { setMsg({ t: String(e), ok: false }) } finally { setBusy(false) }
  }

  return (
    <>
      <h1>Matches</h1>
      <p className="sub">Pull real schedules from BetsAPI first. Real Mode Clean removes quarantined seed/old demo rows from an existing local DB.</p>
      {msg && <div className={`msg ${msg.ok ? 'ok' : 'err'}`}>{msg.t}</div>}
      <div className="toolbar">
        <button className="primary" onClick={pullSchedule} disabled={busy}>{busy ? 'Working…' : 'Pull BetsAPI schedule'}</button>
        <button onClick={pullOdds} disabled={busy}>Pull odds for upcoming</button>
        <button className="danger" onClick={realModeClean} disabled={busy}>Real mode clean</button>
        <button onClick={() => fileRef.current?.click()}>Import match_results.csv</button>
        <a className="btn" href="/api/export/matches.csv">Export CSV</a>
        <input ref={fileRef} type="file" accept=".csv" style={{ display: 'none' }}
          onChange={e => e.target.files?.[0] && importCsv(e.target.files[0])} />
        <span className="muted mono">{rows.length} shown (latest 500)</span>
      </div>
      <div className="card scroll-x">
        {rows.length === 0 ? <div className="empty">No matches. Use “Pull BetsAPI schedule” or import match_results.csv.</div> : (
          <table>
            <thead><tr><th>Start</th><th>League</th><th>Home</th><th>Away</th><th>Score</th><th>HT</th><th>Winner</th><th>Source</th></tr></thead>
            <tbody>
              {rows.map(m => (
                <tr key={m.id}>
                  <td className="muted">{fmtDT(m.start_time)}</td>
                  <td className="muted">{m.league}</td>
                  <td>{m.home}</td><td>{m.away}</td>
                  <td>{m.home_score != null ? `${m.home_score}–${m.away_score}` : <span className="amber">upcoming/live</span>}</td>
                  <td className="muted">{m.ht ?? '—'}</td>
                  <td>{m.winner ?? '—'}</td>
                  <td>{m.source === 'manual_seed' ? <span className="badge amber" title="Manual reconstructed evidence">SEED</span>
                    : m.source === 'synthetic_demo' || m.source === 'seed' ? <span className="badge muted" title="Quarantined non-real source">DEMO</span>
                    : m.source === 'betsapi' || m.source === 'betsapi_history' ? <span className="badge pos" title="Real from BetsAPI">BETSAPI</span>
                    : <span className="badge" title="CSV-imported or manually entered">MANUAL</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  )
}
