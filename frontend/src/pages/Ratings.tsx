import { useEffect, useState } from 'react'
import { get, send } from '../api'

export default function Ratings() {
  const [rows, setRows] = useState<any[]>([])
  const [busy, setBusy] = useState(false)
  const load = () => get('/players').then(setRows).catch(() => {})
  useEffect(() => { load() }, [])

  const rebuild = async () => {
    setBusy(true)
    await send('POST', '/ratings/rebuild').catch(() => {})
    await load(); setBusy(false)
  }

  const formStr = (f: any) =>
    f && f.n ? `${f.wins}W ${f.draws}D ${f.losses}L` : '—'

  return (
    <>
      <h1>Player Ratings</h1>
      <p className="sub">Every player's skill rating, updated after every finished match. Higher Elo = stronger. MP = matches played (below ~25, treat the rating as unreliable — see <a href="#/glossary">Glossary</a> for every column explained).</p>
      <div className="toolbar">
        <button className="primary" onClick={rebuild} disabled={busy}>{busy ? 'Rebuilding…' : 'Rebuild ratings'}</button>
        <span className="muted mono">{rows.length} players</span>
      </div>
      <div className="card scroll-x">
        {rows.length === 0 ? <div className="empty">No players yet — import matches first.</div> : (
          <table>
            <thead><tr><th>#</th><th>Player</th><th>League</th><th>Elo</th><th>MP</th>
              <th>Form (10)</th><th>Draw%</th><th>Avg GF</th><th>Avg GA</th></tr></thead>
            <tbody>
              {rows.map((p, i) => (
                <tr key={p.id}>
                  <td className="muted">{i + 1}</td>
                  <td>{p.name}</td><td className="muted">{p.league}</td>
                  <td className={p.elo >= 1500 ? 'pos' : 'neg'}>{p.elo.toFixed(0)}</td>
                  <td className={p.matches_played < 25 ? 'amber' : ''}>{p.matches_played}</td>
                  <td>{formStr(p.form10)}</td>
                  <td>{p.form10?.draw_pct != null ? (p.form10.draw_pct * 100).toFixed(0) + '%' : '—'}</td>
                  <td>{p.attack.toFixed(2)}</td><td>{p.defense.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  )
}
