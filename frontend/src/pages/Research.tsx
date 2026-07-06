import { useEffect, useState } from 'react'
import { cls, get, send } from '../api'

export default function Research() {
  const [tab, setTab] = useState<'hyp' | 'pat' | 'cal' | 'drift' | 'strat'>('hyp')
  const [hyp, setHyp] = useState<any>(null)
  const [patterns, setPatterns] = useState<any[]>([])
  const [cal, setCal] = useState<any>(null)
  const [dr, setDr] = useState<any>(null)
  const [strats, setStrats] = useState<any[]>([])
  const [msg, setMsg] = useState<string | null>(null)
  const [form, setForm] = useState({ title: '', test_type: 'player_backed_roi', params: '{"player":"BLITZ"}' })

  const load = () => {
    get('/hypotheses').then(setHyp).catch(e => setMsg(String(e)))
    get('/patterns').then(setPatterns).catch(() => {})
    get('/calibration').then(setCal).catch(() => {})
    get('/drift').then(setDr).catch(() => {})
    get('/strategies').then(setStrats).catch(() => {})
  }
  useEffect(() => { load() }, [])

  const addHyp = async () => {
    try {
      await send('POST', '/hypotheses', { title: form.title || form.test_type, test_type: form.test_type, params: JSON.parse(form.params || '{}') })
      setMsg('Hypothesis created and tested'); load()
    } catch (e) { setMsg(String(e)) }
  }
  const testAll = async () => { await send('POST', '/hypotheses/test-all'); setMsg('All hypotheses re-tested'); load() }
  const scan = async () => { const r = await send('POST', '/patterns/scan'); setMsg(`${r.proposed} new pattern(s) proposed — approve or reject below`); load() }
  const patStatus = async (id: number, s: string) => { await send('POST', `/patterns/${id}/status?status=${s}`); load() }

  const R = ({ r }: { r: any }) => (
    <span className="mono small">
      {r.n != null && <>n={r.n} </>}
      {r.win_rate != null && <>win {Math.round(r.win_rate * 100)}% </>}
      {r.roi_pct != null && <span className={cls(r.roi_pct)}>ROI {r.roi_pct}% </span>}
      {r.win_rate_ci95 && <span className="muted">CI {JSON.stringify(r.win_rate_ci95)} </span>}
      {r.avg_goals != null && <>goals μ{r.avg_goals} σ²{r.variance} </>}
      {r.note && <span className="muted">{r.note}</span>}
      {r.ml && <>ML: n={r.ml.n} ROI {r.ml.roi_pct}% · Spread: n={r.spread.n} ROI {r.spread.roi_pct}%</>}
    </span>
  )

  return (
    <>
      <h1>Research</h1>
      <p className="sub">A lab notebook, not a dashboard. Purpose: test specific ideas against your real match/bet history before trusting them — "does X actually work, or does it just feel like it works?" Write a hypothesis (e.g. "BLITZ profitable when backed"), the system tests it against real data and reports sample size + win rate + ROI with honest confidence intervals. Nothing here changes Best Picks automatically — approved patterns are proposals you review, not auto-applied rules.</p>
      {msg && <div className="msg ok">{msg}</div>}
      <div className="toolbar">
        {(['hyp', 'pat', 'cal', 'drift', 'strat'] as const).map(t => (
          <button key={t} className={tab === t ? 'primary' : ''} onClick={() => setTab(t)}>
            {{ hyp: 'Notebook', pat: `Patterns (${patterns.filter(p => p.status === 'proposed').length} proposed)`, cal: 'Calibration', drift: 'Drift', strat: 'Strategies' }[t]}
          </button>
        ))}
      </div>

      {tab === 'hyp' && hyp && (
        <>
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="form-grid">
              <div className="field"><label>Title</label><input value={form.title} onChange={e => setForm({ ...form, title: e.target.value })} placeholder="BLITZ outperforms as underdog" /></div>
              <div className="field"><label>Test type</label>
                <select value={form.test_type} onChange={e => setForm({ ...form, test_type: e.target.value })}>
                  {Object.entries(hyp.test_types).map(([k, v]: any) => <option key={k} value={k}>{k} — {v.desc}</option>)}
                </select></div>
              <div className="field"><label>Params (JSON)</label><input value={form.params} onChange={e => setForm({ ...form, params: e.target.value })} /></div>
            </div>
            <button className="primary" onClick={addHyp}>Add & test</button>{' '}
            <button onClick={testAll}>Re-test all</button>
          </div>
          <div className="card scroll-x">
            {hyp.hypotheses.length === 0 ? <div className="empty">No hypotheses yet. Example: "CRUSADER lines shorten within 10s of live" (live_shorten).</div> : (
              <table>
                <thead><tr><th>Hypothesis</th><th>Type</th><th>Latest evidence</th><th>Trend</th><th>Tested</th></tr></thead>
                <tbody>
                  {hyp.hypotheses.map((h: any) => (
                    <tr key={h.id}>
                      <td>{h.title}</td>
                      <td className="muted">{h.test_type}</td>
                      <td><R r={h.last_result} /></td>
                      <td><span className={h.trend === 'increasing' ? 'pos' : h.trend === 'decreasing' ? 'neg' : 'muted'}>{h.trend}</span></td>
                      <td className="muted small">{h.last_tested_at?.slice(0, 16) ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      )}

      {tab === 'pat' && (
        <>
          <div className="toolbar"><button className="primary" onClick={scan}>Scan database for patterns</button><span className="muted">Discovered patterns are proposals — nothing enters the model without your approval.</span></div>
          <div className="card scroll-x">
            {patterns.length === 0 ? <div className="empty">No patterns yet. Run a scan.</div> : (
              <table>
                <thead><tr><th>Kind</th><th>Description</th><th>Status</th><th></th></tr></thead>
                <tbody>
                  {patterns.map(p => (
                    <tr key={p.id}>
                      <td className="muted">{p.kind}</td>
                      <td className="small">{p.description}</td>
                      <td><span className={`badge ${p.status === 'approved' ? 'pos' : p.status === 'rejected' ? 'neg' : 'amber'}`}>{p.status}</span></td>
                      <td>{p.status === 'proposed' && <><button onClick={() => patStatus(p.id, 'approved')}>approve</button>{' '}<button onClick={() => patStatus(p.id, 'rejected')}>reject</button></>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      )}

      {tab === 'cal' && cal && (
        <div className="card">
          <h3>Calibration — do stated probabilities match reality?</h3>
          <p className="muted small">Scored predictions: {cal.n_scored} · mean abs error: {cal.mean_abs_error ?? '—'} {cal.overconfident && <span className="neg">· model looks OVERCONFIDENT — confidence penalty applies</span>}</p>
          <table>
            <thead><tr><th>Bucket</th><th>n</th><th>Expected win%</th><th>Actual win%</th><th>Error</th></tr></thead>
            <tbody>
              {cal.buckets.map((b: any) => (
                <tr key={b.bucket}>
                  <td>{b.bucket}</td><td className="mono">{b.n}</td>
                  <td className="mono">{b.expected != null ? `${Math.round(b.expected * 100)}%` : '—'}</td>
                  <td className="mono">{b.actual != null ? `${Math.round(b.actual * 100)}%` : '—'}</td>
                  <td className={`mono ${cls(b.error)}`}>{b.error ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {tab === 'drift' && dr && (
        <div className="card">
          <h3>Drift detection {dr.model_degraded && <span className="badge neg">MODEL DEGRADED</span>}</h3>
          <table>
            <thead><tr><th>Window</th><th>n</th><th>ROI</th><th>Avg CLV</th><th>Status</th></tr></thead>
            <tbody>
              {Object.entries(dr).filter(([k]) => k !== 'model_degraded').map(([k, v]: any) => (
                <tr key={k}>
                  <td>{k}</td><td className="mono">{v.n}</td>
                  <td className={`mono ${cls(v.roi_pct)}`}>{v.roi_pct ?? '—'}%</td>
                  <td className="mono">{v.avg_clv_pct ?? '—'}%</td>
                  <td><span className={`badge ${v.status === 'healthy' ? 'pos' : v.status === 'degraded' ? 'neg' : ''}`}>{v.status}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted small">Degraded = rolling ROI &lt; −5% or rolling CLV &lt; −1%. When flagged, the Pick Engine's history confidence drops with it.</p>
        </div>
      )}

      {tab === 'strat' && (
        <div className="card scroll-x">
          <h3>Strategy profiles</h3>
          {strats.length === 0 ? <div className="empty">No strategies. Create via POST /api/strategies with backtester filters (e.g. {'{'}"min_ev_pct": 8, "stake_mode": "kelly"{'}'}).</div> : (
            <table>
              <thead><tr><th>Name</th><th>Active</th><th>Filters</th><th>Stats</th><th></th></tr></thead>
              <tbody>
                {strats.map(s => (
                  <tr key={s.id}>
                    <td>{s.name}</td>
                    <td>{s.active ? <span className="pos">active</span> : <span className="neg">inactive</span>}</td>
                    <td className="mono small">{JSON.stringify(s.filters)}</td>
                    <td className="mono small">{JSON.stringify(s.stats)}</td>
                    <td><button onClick={async () => { await send('POST', `/strategies/${s.id}/evaluate`); load() }}>evaluate</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </>
  )
}
