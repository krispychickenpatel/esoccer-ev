import { useEffect, useRef, useState } from 'react'
import { fmtAm, fmtDT, get, upload } from '../api'

export default function Odds() {
  const [rows, setRows] = useState<any[]>([])
  const [msg, setMsg] = useState<{ t: string; ok: boolean } | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const load = () => get('/odds').then(setRows).catch(e => setMsg({ t: String(e), ok: false }))
  useEffect(() => { load() }, [])

  const importCsv = async (f: File) => {
    try {
      const r = await upload('/odds/import', f)
      const skipped = r.skipped_unknown_match?.length
        ? ` Skipped unknown ext_ids: ${r.skipped_unknown_match.join(', ')}` : ''
      setMsg({ t: `Imported ${r.imported} snapshots.${skipped}`, ok: true }); load()
    } catch (e) { setMsg({ t: String(e), ok: false }) }
  }

  return (
    <>
      <h1>Odds Snapshots</h1>
      <p className="sub">Append-only time series. Odds rows link to matches by ext_id — import matches first.</p>
      {msg && <div className={`msg ${msg.ok ? 'ok' : 'err'}`}>{msg.t}</div>}
      <div className="toolbar">
        <button className="primary" onClick={() => fileRef.current?.click()}>Import odds_snapshots.csv</button>
        <a className="btn" href="/api/export/odds.csv">Export CSV</a>
        <input ref={fileRef} type="file" accept=".csv" style={{ display: 'none' }}
          onChange={e => e.target.files?.[0] && importCsv(e.target.files[0])} />
        <span className="muted mono">{rows.length} shown (latest 1000)</span>
      </div>
      <div className="card scroll-x">
        {rows.length === 0 ? <div className="empty">No odds yet. Import odds_snapshots.csv.</div> : (
          <table>
            <thead><tr><th>Source</th><th>Phase</th><th>Collected</th><th>Match</th><th>Book</th><th>Mkt</th><th>Sel</th><th>Line</th><th>Amer</th><th>Dec</th><th>Impl%</th><th>Flags</th></tr></thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.id}>
                  <td>{r.data_source === 'betsapi' ? <span className="badge pos" title="Real, from BetsAPI poller">LIVE</span>
                    : <span className="badge muted" title="CSV-imported">CSV</span>}</td>
                  <td>{r.phase === 'live' ? <span className="badge amber">live</span> : <span className="muted">pre</span>}</td>
                  <td className="muted">{fmtDT(r.collected_at)}</td>
                  <td>{r.match}</td><td>{r.sportsbook}</td>
                  <td className="muted">{r.market}</td><td>{r.selection}</td>
                  <td>{r.line ?? '—'}</td>
                  <td>{fmtAm(r.american_odds)}</td><td>{r.decimal_odds.toFixed(2)}</td>
                  <td>{(r.implied_prob * 100).toFixed(1)}</td>
                  <td className="muted">{r.is_opening ? 'open ' : ''}{r.is_closing ? 'close' : ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  )
}
