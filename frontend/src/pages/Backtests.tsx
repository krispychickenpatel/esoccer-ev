import { useEffect, useState } from 'react'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { cls, fmtDT, fmtMoney, get, send } from '../api'

const DEFAULTS = {
  name: '', min_ev_pct: 5, min_confidence: 0.2, min_decimal: 1.01, max_decimal: 100,
  stake_mode: 'flat', flat_stake: 10, kelly_fraction: 0.25, starting_bankroll: 1000,
  date_from: '', date_to: '', nu: 0.63,
}

export default function Backtests() {
  const [form, setForm] = useState<any>(DEFAULTS)
  const [result, setResult] = useState<any>(null)
  const [history, setHistory] = useState<any[]>([])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => { get('/backtests').then(setHistory).catch(() => {}) }, [])
  const set = (k: string) => (e: any) => setForm({ ...form, [k]: e.target.value })

  const run = async () => {
    setBusy(true); setErr('')
    try {
      const payload: any = {
        ...form,
        min_ev_pct: Number(form.min_ev_pct), min_confidence: Number(form.min_confidence),
        min_decimal: Number(form.min_decimal), max_decimal: Number(form.max_decimal),
        flat_stake: Number(form.flat_stake), kelly_fraction: Number(form.kelly_fraction),
        starting_bankroll: Number(form.starting_bankroll), nu: Number(form.nu),
        date_from: form.date_from ? new Date(form.date_from).toISOString() : null,
        date_to: form.date_to ? new Date(form.date_to).toISOString() : null,
        player_ids: [],
      }
      const r = await send('POST', '/backtests', payload)
      setResult(r)
      setHistory(await get('/backtests'))
    } catch (e) { setErr(String(e)) }
    setBusy(false)
  }

  const bucketTable = (title: string, data: Record<string, any>) => (
    <div className="card">
      <h3>{title}</h3>
      <table>
        <thead><tr><th>Bucket</th><th>Bets</th><th>Profit</th></tr></thead>
        <tbody>
          {Object.entries(data).sort((a: any, b: any) => b[1].profit - a[1].profit).map(([k, v]: any) => (
            <tr key={k}><td>{k}</td><td>{v.bets}</td><td className={cls(v.profit)}>{fmtMoney(v.profit)}</td></tr>
          ))}
        </tbody>
      </table>
    </div>
  )

  return (
    <>
      <h1>Backtests</h1>
      <p className="sub">Answers "if these rules had run on past matches, what would have happened?" Set filters (min EV%, odds range, staking method), it replays your full match history match-by-match using only data that existed before each kickoff (no cheating with future info). Output: bankroll curve, ROI, drawdown — same idea as backtesting a trading strategy. Use this BEFORE trusting a strategy with real stakes, not after. No lookahead: predictions use ratings as of each match. Assumes full stake matched — treat results as an upper bound.</p>
      {err && <div className="msg err">{err}</div>}
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="form-grid">
          <div className="field"><label>Name</label><input value={form.name} onChange={set('name')} /></div>
          <div className="field"><label>From</label><input type="date" value={form.date_from} onChange={set('date_from')} /></div>
          <div className="field"><label>To</label><input type="date" value={form.date_to} onChange={set('date_to')} /></div>
          <div className="field"><label>Min EV %</label><input value={form.min_ev_pct} onChange={set('min_ev_pct')} /></div>
          <div className="field"><label>Min confidence</label><input value={form.min_confidence} onChange={set('min_confidence')} /></div>
          <div className="field"><label>Min dec odds</label><input value={form.min_decimal} onChange={set('min_decimal')} /></div>
          <div className="field"><label>Max dec odds</label><input value={form.max_decimal} onChange={set('max_decimal')} /></div>
          <div className="field"><label>Stake mode</label>
            <select value={form.stake_mode} onChange={set('stake_mode')}>
              <option value="flat">flat</option><option value="kelly">kelly</option>
            </select></div>
          <div className="field"><label>Flat stake</label><input value={form.flat_stake} onChange={set('flat_stake')} /></div>
          <div className="field"><label>Kelly fraction</label><input value={form.kelly_fraction} onChange={set('kelly_fraction')} /></div>
          <div className="field"><label>Bankroll</label><input value={form.starting_bankroll} onChange={set('starting_bankroll')} /></div>
          <div className="field"><label>Draw ν</label><input value={form.nu} onChange={set('nu')} /></div>
        </div>
        <button className="primary" onClick={run} disabled={busy}>{busy ? 'Running…' : 'Run backtest'}</button>
      </div>

      {result && (
        <>
          <div className="grid cols-4" style={{ marginBottom: 12 }}>
            {[
              ['Bets', result.total_bets], ['W / L', `${result.wins} / ${result.losses}`],
              ['Profit', fmtMoney(result.profit), cls(result.profit)],
              ['ROI', `${result.roi_pct}%`, cls(result.roi_pct)],
              ['Max drawdown', `${result.max_drawdown_pct}%`, 'neg'],
              ['Longest L streak', result.longest_losing_streak],
              ['Avg dec odds', result.avg_decimal_odds ?? '—'],
              ['Final bankroll', fmtMoney(result.final_bankroll)],
            ].map(([l, v, tone]: any) => (
              <div className="card stat" key={l}><h3>{l}</h3><div className={`value ${tone ?? ''}`}>{v}</div></div>
            ))}
          </div>
          <div className="card" style={{ marginBottom: 12 }}>
            <h3>Bankroll Curve</h3>
            <ResponsiveContainer width="100%" height={240}>
              <LineChart data={result.curve}>
                <CartesianGrid stroke="#212a38" strokeDasharray="3 3" />
                <XAxis dataKey="t" tickFormatter={t => (t ? fmtDT(t) : 'start')} stroke="#66748a" fontSize={10} />
                <YAxis stroke="#66748a" fontSize={10} domain={['auto', 'auto']} />
                <Tooltip contentStyle={{ background: '#12161f', border: '1px solid #212a38' }} />
                <Line type="monotone" dataKey="bankroll" stroke="#5b8dd6" dot={false} strokeWidth={1.5} />
              </LineChart>
            </ResponsiveContainer>
          </div>
          <div className="grid cols-2" style={{ marginBottom: 12 }}>
            {bucketTable('Profit by Selection', result.profit_by_selection)}
            {bucketTable('Profit by Odds Range', result.profit_by_odds_range)}
          </div>
        </>
      )}

      <div className="card scroll-x">
        <h3>Past Runs</h3>
        {history.length === 0 ? <div className="empty">No saved runs yet.</div> : (
          <table>
            <thead><tr><th>Name</th><th>Created</th><th>Bets</th><th>ROI</th><th>Profit</th><th>Max DD</th></tr></thead>
            <tbody>
              {history.map(h => (
                <tr key={h.id}>
                  <td>{h.name}</td><td className="muted">{fmtDT(h.created_at)}</td>
                  <td>{h.results.total_bets}</td>
                  <td className={cls(h.results.roi_pct)}>{h.results.roi_pct}%</td>
                  <td className={cls(h.results.profit)}>{fmtMoney(h.results.profit)}</td>
                  <td className="neg">{h.results.max_drawdown_pct}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  )
}
