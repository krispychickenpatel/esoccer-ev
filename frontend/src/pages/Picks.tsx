import { useEffect, useState } from 'react'
import { cls, fmtAm, fmtDT, fmtMoney, get, send } from '../api'

const STATUS_COLOR: Record<string, string> = {
  BET: 'pos', WAIT: 'amber', PASS: 'muted', MISSED: 'neg', EXPIRED: 'muted',
}

function ConfBar({ label, v }: { label: string; v: number }) {
  return (
    <div className="confrow">
      <span className="muted">{label}</span>
      <div className="confbar"><div style={{ width: `${Math.round(v * 100)}%` }} /></div>
      <span className="mono">{Math.round(v * 100)}%</span>
    </div>
  )
}

function PickCard({ c }: { c: any }) {
  const conf = c.confidence || {}
  return (
    <div className={`card pickcard ${c.status === 'BET' ? 'pick-bet' : ''}`}>
      <div className="pickhead">
        <span className="rank mono">#{c.rank}</span>
        <b>{c.match}</b>
        <span className="muted">{c.league}</span>
        <span className={`badge ${STATUS_COLOR[c.status] || ''}`}>{c.status}</span>
        {c.seed_influenced && <span className="badge amber">SEED</span>}
        <span className="mono muted" style={{ marginLeft: 'auto' }}>score {c.rank_score}</span>
      </div>
      <div className="pickgrid">
        <div>
          <div><span className="muted">Selection</span> <b>{c.selection}</b> · {c.market}{c.line != null ? ` ${c.line}` : ''} @ {c.sportsbook}</div>
          <div><span className="muted">Alt market</span> {c.acceptable_alt} (max spread {c.max_spread})</div>
          <div><span className="muted">Kickoff</span> {fmtDT(c.scheduled_start)} · {c.execution_window ?? `window ${c.exec_window_seconds}s after live`}</div>
          <div><span className="muted">Expires</span> {fmtDT(c.expires_at)}</div>
        </div>
        <div className="mono">
          <div>Current {fmtAm(c.current_american)} <span className="muted">(min {c.min_american_odds != null ? fmtAm(c.min_american_odds) : '—'} / ideal {c.ideal_american_odds != null ? fmtAm(c.ideal_american_odds) : '—'})</span></div>
          <div>First live est {fmtAm(c.predicted_first_live_american)} <span className="muted">({c.expected_line_movement_cents ?? '—'}¢)</span></div>
          <div>Steam {(Number(c.steam_probability ?? 0.5) * 100).toFixed(0)}% · max entry {fmtAm(c.maximum_entry_price)}</div>
          <div>Model P {(c.model_prob * 100).toFixed(1)}% · fair {c.fair_decimal?.toFixed(2)}</div>
          <div>EV <b className={cls(c.ev_pct)}>{c.ev_pct}%</b></div>
          <div>Stake {fmtMoney(c.suggested_stake)} {c.limit_seen != null && <span className="muted">(limit seen {fmtMoney(c.limit_seen)})</span>}</div>
        </div>
        <div>
          <ConfBar label="model" v={conf.model ?? 0} />
          <ConfBar label="market" v={conf.market ?? 0} />
          <ConfBar label="execution" v={conf.execution ?? 0} />
          <ConfBar label="data" v={conf.data_quality ?? 0} />
          <ConfBar label="history" v={conf.historical ?? 0} />
          <ConfBar label="steam" v={conf.steam ?? 0} />
          <div className="mono">overall {(conf.overall * 100 || 0).toFixed(0)}% · {c.consensus}</div>
        </div>
      </div>
      <div className="reasons">
        {c.reason_codes.map((r: string) => <span key={r} className={`badge ${r.includes('EDGE') || r === 'GOOD_PRICE' || r === 'MARKET_MISPRICE' ? 'pos' : r === 'BAD_PRICE' || r === 'WINDOW_MISSED' || r === 'LIMIT_TOO_LOW' ? 'neg' : ''}`}>{r}</span>)}
        {(conf.penalties || []).map((p: string) => <span key={p} className="muted">− {p}</span>)}
      </div>
      {c.status === 'BET' && <div className="betline mono">
        BET {c.selection} {c.market} {fmtAm(c.current_american)} · stake {fmtMoney(c.suggested_stake)} · place when live · expires {fmtDT(c.expires_at)}.
        If odds fall below {c.maximum_entry_price != null ? fmtAm(c.maximum_entry_price) : c.min_american_odds != null ? fmtAm(c.min_american_odds) : 'minimum'}, PASS.
      </div>}
      <div className="muted small">Similar setups: n={c.similar_setups?.n ?? 0}, ROI {c.similar_setups?.roi_pct ?? '—'}%, win CI {JSON.stringify(c.similar_setups?.win_rate_ci95 ?? [])}. Steam sample {c.steam?.historical_sample ?? 0}. No pick is ever guaranteed.</div>
    </div>
  )
}

export default function Picks() {
  const [tab, setTab] = useState<'best' | 'history'>('best')
  const [cards, setCards] = useState<any[]>([])
  const [hist, setHist] = useState<any>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = () => {
    setBusy(true)
    get('/picks/best?limit=30').then(setCards).catch(e => setMsg(String(e))).finally(() => setBusy(false))
    get('/picks/history?limit=200').then(setHist).catch(() => {})
  }
  useEffect(() => { load() }, [])

  const snapshot = async () => {
    setBusy(true)
    try { const r = await send('POST', '/picks/generate'); setMsg(`Snapshot saved: ${r.generated} picks (${r.bet} BET / ${r.wait} WAIT / ${r.pass} PASS)`); load() }
    catch (e) { setMsg(String(e)) } finally { setBusy(false) }
  }

  const decide = async (id: number, d: string) => {
    await send('POST', `/picks/${id}/decision?decision=${d}`); load()
  }

  return (
    <>
      <h1>Best Picks</h1>
      <p className="sub">Ranked live/upcoming opportunities. BET means every rule passed — it never means guaranteed.</p>
      {msg && <div className="msg ok">{msg}</div>}
      <div className="toolbar">
        <button className={tab === 'best' ? 'primary' : ''} onClick={() => setTab('best')}>Best picks</button>
        <button className={tab === 'history' ? 'primary' : ''} onClick={() => setTab('history')}>Pick history</button>
        <button onClick={load} disabled={busy}>{busy ? 'Evaluating…' : 'Re-evaluate now'}</button>
        <button onClick={snapshot} disabled={busy}>Save snapshot</button>
      </div>

      {tab === 'best' && (cards.length === 0
        ? <div className="card empty">No upcoming matches to evaluate. Import matches + odds first.</div>
        : cards.map(c => <PickCard key={`${c.match_id}-${c.market}-${c.selection}-${c.sportsbook}-${c.line}`} c={c} />))}

      {tab === 'history' && hist && (
        <>
          <div className="grid cols-4">
            <div className="stat"><label>Settled</label><b className="mono">{hist.summary.settled}</b></div>
            <div className="stat"><label>Bet P/L</label><b className={`mono ${cls(hist.summary.bet_profit)}`}>{fmtMoney(hist.summary.bet_profit)}</b></div>
            <div className="stat"><label>Correct-pass rate</label><b className="mono">{hist.summary.correct_pass_rate != null ? `${Math.round(hist.summary.correct_pass_rate * 100)}%` : '—'}</b></div>
            <div className="stat"><label>Grades</label><b className="mono">{Object.entries(hist.summary.grades).map(([g, n]) => `${g}:${n}`).join(' ')}</b></div>
          </div>
          <div className="card scroll-x">
            <table>
              <thead><tr><th>Created</th><th>Match</th><th>Pick</th><th>Odds</th><th>EV</th><th>Status</th><th>Decision</th><th>Result</th><th>P/L</th><th>CLV</th><th>Grade</th><th></th></tr></thead>
              <tbody>
                {hist.picks.map((p: any) => (
                  <tr key={p.id}>
                    <td className="muted">{fmtDT(p.created_at)}</td>
                    <td>{p.match}</td>
                    <td>{p.selection} {p.market}</td>
                    <td className="mono">{fmtAm(p.current_american)}</td>
                    <td className={`mono ${cls(p.ev_pct)}`}>{p.ev_pct}%</td>
                    <td><span className={`badge ${STATUS_COLOR[p.status] || ''}`}>{p.status}</span></td>
                    <td>{p.user_decision ?? <span className="muted">—</span>}</td>
                    <td>{p.settled_result ?? <span className="muted">open</span>}</td>
                    <td className={`mono ${cls(p.profit)}`}>{p.profit != null ? fmtMoney(p.profit) : '—'}</td>
                    <td className={`mono ${cls(p.clv_pct)}`}>{p.clv_pct != null ? `${p.clv_pct}%` : '—'}</td>
                    <td className="mono">{p.grade ?? '—'}</td>
                    <td>{!p.settled_result && !p.user_decision && (
                      <>
                        <button onClick={() => decide(p.id, 'bet')}>bet</button>{' '}
                        <button onClick={() => decide(p.id, 'pass')}>pass</button>
                      </>)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  )
}
