import { useEffect, useState } from 'react'
import { fmtAm, fmtDT, fmtMoney, get, send } from '../api'
import EdgeStrip from '../components/EdgeStrip'

export default function Alerts() {
  const [opps, setOpps] = useState<any[]>([])
  const [alerts, setAlerts] = useState<any[]>([])
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')

  const load = async () => {
    setOpps(await get('/ev/opportunities').catch(() => []))
    setAlerts(await get('/alerts?status=open').catch(() => []))
  }
  useEffect(() => { load() }, [])

  const scan = async (notify: boolean) => {
    setBusy(true)
    const r = await send('POST', `/alerts/scan?notify=${notify}`).catch(e => ({ error: String(e) }))
    setMsg(r.error ?? `${r.opportunities} opportunities · ${r.notifications_sent} notifications sent`)
    await load(); setBusy(false)
  }

  const setStatus = async (id: number, status: string) => {
    await send('PUT', `/alerts/${id}/status?status=${status}`)
    load()
  }

  return (
    <>
      <h1>Alerts</h1>
      <p className="sub">Everything above your min-EV threshold. Remember: a big edge vs a sharp book usually means the model is wrong, not the book.</p>
      {msg && <div className="msg ok">{msg}</div>}
      <div className="toolbar">
        <button className="primary" onClick={() => scan(false)} disabled={busy}>Scan now</button>
        <button onClick={() => scan(true)} disabled={busy}>Scan + notify webhooks</button>
      </div>

      <div className="card scroll-x" style={{ marginBottom: 12 }}>
        <h3>Live EV Opportunities</h3>
        {opps.length === 0 ? <div className="empty">Nothing above threshold. Adjust min EV in Settings, or wait for new odds.</div> : (
          <table>
            <thead><tr><th>Match</th><th>Kickoff</th><th>Book</th><th>Sel</th><th>Price</th>
              <th>Edge</th><th>EV</th><th>Fair</th><th>Stake F/K</th><th>Conf</th></tr></thead>
            <tbody>
              {opps.map((o, i) => (
                <tr key={i}>
                  <td>{o.match}</td>
                  <td className="muted">{fmtDT(o.start_time)}</td>
                  <td>{o.sportsbook}</td><td>{o.selection}</td>
                  <td>{fmtAm(o.book_american)}</td>
                  <td><EdgeStrip model={o.model_prob} book={1 / o.book_decimal} /></td>
                  <td className="pos">+{o.ev_pct}%</td>
                  <td className="muted">{o.fair_decimal.toFixed(2)}</td>
                  <td>{fmtMoney(o.stake_flat)} / {fmtMoney(o.stake_kelly)}</td>
                  <td className={o.confidence < 0.5 ? 'amber' : ''}>{o.confidence}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="card scroll-x">
        <h3>Open Alerts</h3>
        {alerts.length === 0 ? <div className="empty">No open alerts. Run a scan to create them.</div> : (
          <table>
            <thead><tr><th>Created</th><th>Match</th><th>Sel</th><th>Book</th><th>Price</th><th>EV</th><th>Stake</th><th>Why</th><th></th></tr></thead>
            <tbody>
              {alerts.map(a => (
                <tr key={a.id}>
                  <td className="muted">{fmtDT(a.created_at)}</td>
                  <td>{a.match}</td><td>{a.selection}</td><td>{a.sportsbook}</td>
                  <td>{fmtAm(a.book_american)}</td>
                  <td className="pos">+{a.ev_pct}%</td>
                  <td>{fmtMoney(a.suggested_stake)}</td>
                  <td className="muted" style={{ whiteSpace: 'normal', maxWidth: 280 }}>{a.reason}</td>
                  <td>
                    <button onClick={() => setStatus(a.id, 'taken')}>taken</button>{' '}
                    <button className="danger" onClick={() => setStatus(a.id, 'dismissed')}>×</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  )
}
