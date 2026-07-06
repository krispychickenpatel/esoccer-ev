import { useEffect, useState } from 'react'
import { fmtAm, fmtDT, fmtMoney, get, send, upload } from '../api'

const QUICK_DEFAULTS = {
  home_name: '', away_name: '', recommended_selection: '', sportsbook: 'FanDuel',
  scheduled_start: '', min_american_odds: '', ideal_american_odds: '',
  max_spread: '-0.5', confidence_label: 'high', notes: '',
}

export default function Recs() {
  const [tab, setTab] = useState<'recs' | 'exec' | 'seed'>('recs')
  const [recs, setRecs] = useState<any[]>([])
  const [execs, setExecs] = useState<any[]>([])
  const [seed, setSeed] = useState<any>(null)
  const [msg, setMsg] = useState<{ t: string; ok: boolean } | null>(null)
  const [edit, setEdit] = useState<any | null>(null)
  const [quick, setQuick] = useState<any>(QUICK_DEFAULTS)
  const [quickBusy, setQuickBusy] = useState(false)

  const load = () => {
    get('/recommendations').then(setRecs).catch(e => setMsg({ t: String(e), ok: false }))
    get('/executions').then(setExecs).catch(() => {})
    get('/seed/review').then(setSeed).catch(() => {})
  }
  useEffect(() => { load() }, [])

  const importCsv = (path: string) => async (e: any) => {
    const f = e.target.files?.[0]; if (!f) return
    try {
      const r = await upload(`${path}?dry_run=true`, f)
      if (r.errors?.length) { setMsg({ t: `Preview found ${r.errors.length} error(s): ${r.errors.slice(0, 3).join(' | ')}`, ok: false }); return }
      const r2 = await upload(path, f)
      setMsg({ t: `Imported ${r2.imported}, duplicates ${r2.duplicates ?? 0}, warnings ${r2.warnings?.length ?? 0}`, ok: true }); load()
    } catch (err) { setMsg({ t: String(err), ok: false }) }
    e.target.value = ''
  }

  const mark = async (id: number, status: string) => { await send('POST', `/recommendations/${id}/mark?status=${status}`); load() }
  const approve = async (kind: string, id: number) => { await send('POST', `/seed/approve?kind=${kind}&item_id=${id}`); load() }
  const saveEdit = async () => {
    if (!edit) return
    try {
      await send('PUT', `/recommendations/${edit.id}`, {
        ...edit, acceptable_markets: edit.acceptable_markets,
      })
      setEdit(null); setMsg({ t: 'Saved — row now marked user_verified', ok: true }); load()
    } catch (e) { setMsg({ t: String(e), ok: false }) }
  }

  const quickAddSubmit = async () => {
    if (!quick.home_name || !quick.away_name || !quick.recommended_selection || !quick.scheduled_start) {
      setMsg({ t: 'Home, away, selection, and scheduled start are required', ok: false }); return
    }
    setQuickBusy(true)
    try {
      const sched = new Date(quick.scheduled_start)
      await send('POST', '/recommendations', {
        source_name: 'friend',
        scheduled_start: sched.toISOString(),
        home_name: quick.home_name,
        away_name: quick.away_name,
        recommended_selection: quick.recommended_selection,
        acceptable_markets: quick.max_spread ? ['ML_3WAY', 'SPREAD_2WAY'] : ['ML_3WAY'],
        max_spread: quick.max_spread ? Number(quick.max_spread) : null,
        min_american_odds: quick.min_american_odds ? Number(quick.min_american_odds) : null,
        ideal_american_odds: quick.ideal_american_odds ? Number(quick.ideal_american_odds) : null,
        confidence_label: quick.confidence_label,
        sportsbook: quick.sportsbook,
        notes: quick.notes,
      })
      setMsg({ t: 'Recommendation added', ok: true })
      setQuick(QUICK_DEFAULTS)
      load()
    } catch (e) { setMsg({ t: String(e), ok: false }) } finally { setQuickBusy(false) }
  }

  const RecRow = (r: any) => (
    <tr key={r.id}>
      <td className="muted">{r.ext_id ?? r.id}</td>
      <td>{r.home_name || '—'} vs {r.away_name || '—'}<br /><span className="muted small">{r.league}</span></td>
      <td><b>{r.recommended_selection || '—'}</b><br /><span className="muted small">→ {r.canonical_selection || '?'}</span></td>
      <td className="mono small">{(r.acceptable_markets || []).join(', ')}{r.max_spread != null ? ` ≤${r.max_spread}` : ''}</td>
      <td className="mono">{r.min_american_odds != null ? fmtAm(r.min_american_odds) : '—'} / {r.ideal_american_odds != null ? fmtAm(r.ideal_american_odds) : '—'}</td>
      <td className="muted">{fmtDT(r.scheduled_start)}<br /><span className="small">lead {r.lead_time_min ?? '—'}m</span></td>
      <td><span className="badge">{r.status}</span>{r.verification_status === 'seed_partial' && <span className="badge amber">SEED</span>}</td>
      <td className="small">{r.limit_seen != null && <div>limit {fmtMoney(r.limit_seen)}</div>}{r.notes && <span className="muted" title={r.notes}>{r.notes.slice(0, 60)}{r.notes.length > 60 ? '…' : ''}</span>}</td>
      <td>
        <button onClick={() => setEdit({ ...r })}>edit</button>{' '}
        {r.status === 'pending' && <button onClick={() => mark(r.id, 'placed')}>placed</button>}{' '}
        {r.status === 'pending' && <button onClick={() => mark(r.id, 'missed')}>missed</button>}
      </td>
    </tr>
  )

  return (
    <>
      <h1>Recommendations & Execution</h1>
      <p className="sub">Every external pick with its timing evidence. Execution latency matters as much as prediction.</p>
      {msg && <div className={`msg ${msg.ok ? 'ok' : 'err'}`}>{msg.t}</div>}
      <div className="toolbar">
        <button className={tab === 'recs' ? 'primary' : ''} onClick={() => setTab('recs')}>Recommendations ({recs.length})</button>
        <button className={tab === 'exec' ? 'primary' : ''} onClick={() => setTab('exec')}>Execution log ({execs.length})</button>
        <button className={tab === 'seed' ? 'primary' : ''} onClick={() => setTab('seed')}>Seed review {seed ? `(${seed.recommendations.length + seed.bets.length})` : ''}</button>
        <label className="btn">Import recs CSV<input type="file" accept=".csv" hidden onChange={importCsv('/recommendations/import')} /></label>
        <label className="btn">Import executions CSV<input type="file" accept=".csv" hidden onChange={importCsv('/executions/import')} /></label>
      </div>

      {tab === 'recs' && (
        <>
          <div className="card" style={{ marginBottom: 16 }}>
            <h3>Quick add — one pick, no CSV</h3>
            <p className="muted small">Type what your friend sends you directly here. Skip the CSV entirely for a single rec.</p>
            <div className="form-grid">
              <div className="field"><label>Home (team + nickname)</label><input value={quick.home_name} onChange={e => setQuick({ ...quick, home_name: e.target.value })} placeholder="Arsenal (CRUSADER)" /></div>
              <div className="field"><label>Away</label><input value={quick.away_name} onChange={e => setQuick({ ...quick, away_name: e.target.value })} placeholder="Spurs (ALIBI)" /></div>
              <div className="field"><label>Pick (which side)</label><input value={quick.recommended_selection} onChange={e => setQuick({ ...quick, recommended_selection: e.target.value })} placeholder="Arsenal (CRUSADER)" /></div>
              <div className="field"><label>Scheduled kickoff</label><input type="datetime-local" value={quick.scheduled_start} onChange={e => setQuick({ ...quick, scheduled_start: e.target.value })} /></div>
              <div className="field"><label>Min acceptable odds (american)</label><input value={quick.min_american_odds} onChange={e => setQuick({ ...quick, min_american_odds: e.target.value })} placeholder="-160" /></div>
              <div className="field"><label>Ideal odds</label><input value={quick.ideal_american_odds} onChange={e => setQuick({ ...quick, ideal_american_odds: e.target.value })} placeholder="-135" /></div>
              <div className="field"><label>Max spread (blank = ML only)</label><input value={quick.max_spread} onChange={e => setQuick({ ...quick, max_spread: e.target.value })} placeholder="-0.5" /></div>
              <div className="field"><label>Confidence</label>
                <select value={quick.confidence_label} onChange={e => setQuick({ ...quick, confidence_label: e.target.value })}>
                  <option value="high">high</option><option value="medium">medium</option><option value="low">low</option>
                </select></div>
              <div className="field"><label>Sportsbook</label><input value={quick.sportsbook} onChange={e => setQuick({ ...quick, sportsbook: e.target.value })} /></div>
              <div className="field" style={{ gridColumn: '1 / -1' }}><label>Notes</label><input value={quick.notes} onChange={e => setQuick({ ...quick, notes: e.target.value })} placeholder="place when live, either side ok, etc." /></div>
            </div>
            <button className="primary" onClick={quickAddSubmit} disabled={quickBusy}>{quickBusy ? 'Adding…' : 'Add recommendation'}</button>
          </div>
          <div className="card scroll-x">
            <table>
              <thead><tr><th>ID</th><th>Match</th><th>Pick</th><th>Markets</th><th>Min/Ideal</th><th>Kickoff</th><th>Status</th><th>Evidence</th><th></th></tr></thead>
              <tbody>{recs.map(RecRow)}</tbody>
            </table>
          </div>
        </>
      )}

      {tab === 'exec' && (
        <div className="card scroll-x">
          {execs.length === 0 ? <div className="empty">No executions logged. Import execution_log.csv or add rows via API.</div> : (
            <table>
              <thead><tr><th>Rec</th><th>Book</th><th>Live at</th><th>Placed at</th><th>Latency</th><th>Odds slip→live→bet</th><th>In window</th><th>Status</th><th>Missed reason</th></tr></thead>
              <tbody>
                {execs.map(e => (
                  <tr key={e.id}>
                    <td className="muted">{e.recommendation_id ?? '—'}</td>
                    <td>{e.sportsbook}</td>
                    <td className="muted">{fmtDT(e.live_detected_at)}</td>
                    <td className="muted">{fmtDT(e.bet_placed_at)}</td>
                    <td className="mono">{e.latency_seconds != null ? `${e.latency_seconds}s` : '—'}</td>
                    <td className="mono">{e.odds_at_slip != null ? fmtAm(e.odds_at_slip) : '—'} → {e.odds_at_first_live != null ? fmtAm(e.odds_at_first_live) : '—'} → {e.actual_american_odds != null ? fmtAm(e.actual_american_odds) : '—'}</td>
                    <td>{e.was_within_window == null ? '—' : e.was_within_window ? <span className="pos">yes</span> : <span className="neg">no</span>}</td>
                    <td><span className="badge">{e.status}</span></td>
                    <td className="muted">{e.missed_reason || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {tab === 'seed' && seed && (
        <>
          <div className="msg amber">{seed.note}</div>
          <div className="card scroll-x" style={{ marginBottom: 16 }}>
            <h3>Seed recommendations awaiting review</h3>
            {seed.recommendations.length === 0 ? <div className="empty">All seed recommendations reviewed.</div> : (
              <table>
                <thead><tr><th>ID</th><th>Match</th><th>Pick</th><th>Notes (raw, preserved)</th><th></th></tr></thead>
                <tbody>
                  {seed.recommendations.map((r: any) => (
                    <tr key={r.id}>
                      <td className="muted">{r.ext_id}</td>
                      <td>{r.home_name} vs {r.away_name}</td>
                      <td><b>{r.recommended_selection}</b></td>
                      <td className="small muted">{r.notes}</td>
                      <td><button onClick={() => approve('recommendation', r.id)}>approve</button>{' '}<button onClick={() => setEdit({ ...r })}>edit</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
          <div className="card scroll-x">
            <h3>Seed bets awaiting review</h3>
            {seed.bets.length === 0 ? <div className="empty">All seed bets reviewed.</div> : (
              <table>
                <thead><tr><th>ID</th><th>Placed</th><th>Selection</th><th>Market</th><th>Odds</th><th>Stake</th><th>Profit</th><th>Notes</th><th></th></tr></thead>
                <tbody>
                  {seed.bets.map((b: any) => (
                    <tr key={b.id}>
                      <td className="muted">{b.ext_id}</td>
                      <td className="muted">{fmtDT(b.placed_at)}</td>
                      <td>{b.selection} <span className="muted">v {b.opponent}</span></td>
                      <td>{b.market}{b.line != null ? ` ${b.line}` : ''}</td>
                      <td className="mono">{fmtAm(b.american_odds)}</td>
                      <td className="mono">{fmtMoney(b.stake)}</td>
                      <td className="mono pos">{fmtMoney(b.profit)}</td>
                      <td className="small muted">{b.notes}</td>
                      <td><button onClick={() => approve('bet', b.id)}>approve</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      )}

      {edit && (
        <div className="card" style={{ marginTop: 16 }}>
          <h3>Edit recommendation {edit.ext_id ?? edit.id}</h3>
          <div className="form-grid">
            <div className="field"><label>Home</label><input value={edit.home_name} onChange={e => setEdit({ ...edit, home_name: e.target.value })} /></div>
            <div className="field"><label>Away</label><input value={edit.away_name} onChange={e => setEdit({ ...edit, away_name: e.target.value })} /></div>
            <div className="field"><label>Selection</label><input value={edit.recommended_selection} onChange={e => setEdit({ ...edit, recommended_selection: e.target.value })} /></div>
            <div className="field"><label>Min odds (american)</label><input value={edit.min_american_odds ?? ''} onChange={e => setEdit({ ...edit, min_american_odds: e.target.value === '' ? null : Number(e.target.value) })} /></div>
            <div className="field"><label>Ideal odds</label><input value={edit.ideal_american_odds ?? ''} onChange={e => setEdit({ ...edit, ideal_american_odds: e.target.value === '' ? null : Number(e.target.value) })} /></div>
            <div className="field"><label>Limit seen</label><input value={edit.limit_seen ?? ''} onChange={e => setEdit({ ...edit, limit_seen: e.target.value === '' ? null : Number(e.target.value) })} /></div>
            <div className="field" style={{ gridColumn: '1 / -1' }}><label>Notes</label><input value={edit.notes} onChange={e => setEdit({ ...edit, notes: e.target.value })} /></div>
          </div>
          <button className="primary" onClick={saveEdit}>Save</button>{' '}
          <button onClick={() => setEdit(null)}>Cancel</button>
        </div>
      )}
    </>
  )
}
