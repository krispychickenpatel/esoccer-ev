import { useEffect, useState } from 'react'
import { fmtDT, get, send } from '../api'

export default function Health() {
  const [d, setD] = useState<any>(null)
  const [prov, setProv] = useState<any>(null)
  const [leagues, setLeagues] = useState<any[]>([])
  const [books, setBooks] = useState<any[]>([])
  const [cap, setCap] = useState<any>(null)
  const [perf, setPerf] = useState<any>(null)
  const [ingest, setIngest] = useState<any>(null)
  const [coverage, setCoverage] = useState<any>(null)
  const [scanBusy, setScanBusy] = useState(false)
  const [scanMsg, setScanMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const loadCoverage = () => get('/provider/bookmaker-coverage').then(setCoverage).catch(() => {})

  useEffect(() => {
    get('/data-health').then(setD).catch(e => setErr(String(e)))
    get('/provider/status').then(setProv).catch(() => {})
    get('/intel/leagues').then(setLeagues).catch(() => {})
    get('/intel/sportsbooks').then(setBooks).catch(() => {})
    get('/provider/capability-report').then(setCap).catch(() => {})
    get('/provider/performance-report').then(setPerf).catch(() => {})
    get('/provider/result-ingestion-report').then(setIngest).catch(() => {})
    loadCoverage()
  }, [])

  async function runScan() {
    setScanBusy(true); setScanMsg(null)
    try {
      const r = await send('POST', '/provider/bookmaker-coverage/scan')
      setScanMsg(r.skipped ? r.reason : `Scan complete — ${r.calls_used} calls used`)
      await loadCoverage()
    } catch (e: any) { setScanMsg(String(e)) }
    finally { setScanBusy(false) }
  }

  if (err) return <div className="msg err">{err}</div>
  if (!d) return <div className="muted">Loading…</div>

  return (
    <>
      <h1>Data Health</h1>
      <p className="sub">If the data is bad, every downstream number is fiction. Check here before trusting anything.</p>
      {d.warnings.map((w: string) => <div key={w} className="msg amber">{w}</div>)}

      <div className="grid cols-4">
        <div className="stat"><label>Matches</label><b className="mono">{d.totals.matches}</b></div>
        <div className="stat"><label>Odds snapshots</label><b className="mono">{d.totals.odds_snapshots}</b></div>
        <div className="stat"><label>Live-phase snapshots</label><b className="mono">{d.live_phase_snapshots}</b></div>
        <div className="stat"><label>Recommendations</label><b className="mono">{d.totals.recommendations}</b></div>
        <div className="stat"><label>Executions</label><b className="mono">{d.totals.executions}</b></div>
        <div className="stat"><label>Players</label><b className="mono">{d.totals.players}</b></div>
        <div className="stat"><label>Latest odds</label><b className="mono small">{fmtDT(d.latest_data.odds)}</b></div>
        <div className="stat"><label>Latest match</label><b className="mono small">{fmtDT(d.latest_data.match)}</b></div>
      </div>

      <div className="grid cols-2">
        <div className="card">
          <h3>Issues</h3>
          <table><tbody>
            <tr><td>Missing scores (past matches)</td><td className="mono">{d.issues.missing_scores}</td></tr>
            <tr><td>Matches without odds</td><td className="mono">{d.issues.matches_without_odds}</td></tr>
            <tr><td>Duplicate match suspects</td><td className="mono">{d.issues.duplicate_match_suspects}</td></tr>
            <tr><td>Invalid odds rows</td><td className="mono">{d.issues.invalid_odds_rows}</td></tr>
          </tbody></table>
        </div>
        <div className="card">
          <h3>Quarantined data counts</h3>
          <table><tbody>
            <tr><td>Seed/old-source matches</td><td className="mono">{d.seed_counts.matches}</td></tr>
            <tr><td>Seed/old-source bets</td><td className="mono">{d.seed_counts.bets}</td></tr>
            <tr><td>Seed recommendations</td><td className="mono">{d.seed_counts.recommendations}</td></tr>
          </tbody></table>
          <p className="muted small">Real-mode metrics exclude these rows by default; keep the include-seed toggle off unless reviewing old evidence.</p>
        </div>
      </div>

      {prov && (
        <div className="grid cols-2" style={{ marginTop: 16 }}>
          <div className="card">
            <h3>BetsAPI provider</h3>
            <table><tbody>
              <tr><td>Configured</td><td className="mono">{prov.betsapi.configured ? 'yes' : 'no'}</td></tr>
              <tr><td>Calls / retries / rate-limited</td><td className="mono">{prov.betsapi.calls} / {prov.betsapi.retries} / {prov.betsapi.rate_limited}</td></tr>
              <tr><td>Empty responses</td><td className="mono">{prov.betsapi.empty_responses}</td></tr>
              <tr><td>Last call</td><td className="mono small">{prov.betsapi.last_call ?? '—'} ({prov.betsapi.last_code ?? '—'})</td></tr>
            </tbody></table>
            {prov.betsapi.note && <p className="muted small">{prov.betsapi.note}</p>}
          </div>
          <div className="card">
            <h3>Odds poller</h3>
            <table><tbody>
              <tr><td>Running</td><td className="mono">{prov.poller.running ? 'yes' : 'no'}</td></tr>
              <tr><td>Ticks / snapshots / events</td><td className="mono">{prov.poller.ticks} / {prov.poller.snapshots_written} / {prov.poller.events_written}</td></tr>
              <tr><td>Last tick</td><td className="mono small">{prov.poller.last_tick ?? '—'}</td></tr>
              <tr><td>Status</td><td className="small">{prov.poller.note}</td></tr>
            </tbody></table>
            <p className="muted small">Cadence: one opening pull &gt;10min out · 60s at 2–10min · 10s inside 2min · 2s first 30s live · 120s tail. Enable in Settings once a provider key exists.</p>
          </div>
        </div>
      )}

      {perf && (
        <div className="grid cols-2" style={{ marginTop: 16 }}>
          <div className="card">
            <h3>Poller performance {perf.validation_mode && <span className="badge amber">VALIDATION MODE</span>}</h3>
            <table><tbody>
              <tr><td>Loop duration</td><td className="mono">{perf.loop_duration_s ?? '—'}s</td></tr>
              <tr><td>Odds calls / last minute</td><td className="mono">{perf.odds_calls_last_minute}</td></tr>
              <tr><td>Active tracked matches</td><td className="mono">{perf.active_tracked_matches}</td></tr>
              <tr><td>First-live candidates (top priority)</td><td className="mono">{perf.first_live_candidates}</td></tr>
            </tbody></table>
          </div>
          <div className="card">
            <h3>First-live capture latency</h3>
            <table><tbody>
              <tr><td>Average</td><td className="mono">{perf.avg_first_live_latency_s ?? '—'}s</td></tr>
              <tr><td>p95</td><td className="mono">{perf.p95_first_live_latency_s ?? '—'}s</td></tr>
              <tr><td>% within 15s target</td><td className="mono">{perf.pct_first_live_within_15s ?? '—'}%</td></tr>
              <tr><td>Sample size</td><td className="mono">{perf.first_live_sample_size}</td></tr>
            </tbody></table>
          </div>
          <div className="card scroll-x">
            <h3>API calls by endpoint (last hour)</h3>
            <table><tbody>
              {Object.entries(perf.api_calls_last_hour_by_endpoint || {}).map(([ep, n]: any) => (
                <tr key={ep}><td className="mono small">{ep}</td><td className="mono">{n}</td></tr>
              ))}
            </tbody></table>
          </div>
          <div className="card scroll-x">
            <h3>Odds calls by sportsbook</h3>
            <table>
              <thead><tr><th>Book</th><th>Calls</th><th>Empty rate</th></tr></thead>
              <tbody>
                {Object.keys(perf.api_calls_by_sportsbook || {}).map(book => (
                  <tr key={book}>
                    <td>{book}</td>
                    <td className="mono">{perf.api_calls_by_sportsbook[book]}</td>
                    <td className="mono">{Math.round((perf.empty_odds_rate_by_sportsbook[book] ?? 0) * 100)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {ingest && (
        <div className="card" style={{ marginTop: 16 }}>
          <h3>Ended-results ingestion</h3>
          {ingest.note ? <p className="muted small">{ingest.note}</p> : (
            <table><tbody>
              <tr><td>Ended events fetched</td><td className="mono">{ingest.ended_events_fetched}</td></tr>
              <tr><td>In tracked leagues</td><td className="mono">{ingest.in_tracked_leagues}</td></tr>
              <tr><td>Matched to existing matches</td><td className="mono">{ingest.matched_to_existing_matches}</td></tr>
              <tr><td>Scores updated</td><td className="mono">{ingest.scores_updated}</td></tr>
              <tr><td>Unmatched ended events</td><td className="mono">{ingest.unmatched_ended_events}</td></tr>
              <tr><td>Predictions newly scored</td><td className="mono">{ingest.predictions_newly_scored}</td></tr>
              <tr><td>Scoring errors</td><td className="mono">{ingest.scoring_errors}</td></tr>
              <tr><td>Last run</td><td className="mono small">{ingest.at ?? '—'}</td></tr>
            </tbody></table>
          )}
        </div>
      )}

      {coverage && (
        <div className="card scroll-x" style={{ marginTop: 16 }}>
          <h3>Book coverage</h3>
          <p className="muted small">
            <b>{coverage.reference_feed}</b> is the permanent reference feed (truth about prices, ~20-30s delayed) —
            it is never itself an execution candidate. Other books only become execution candidates once a scan
            proves a real non-empty esoccer odds response; FanDuel stays marked EMPTY until that happens.
          </p>
          <div className="toolbar">
            <button disabled={scanBusy} onClick={runScan}>Run bookmaker coverage scan</button>
          </div>
          {scanMsg && <div className="msg amber">{scanMsg}</div>}
          {coverage.books.length === 0 ? <div className="empty">No scan run yet.</div> : (
            <table>
              <thead><tr><th>Book</th><th>Status</th><th>Execution candidate</th><th>Non-empty / empty / errors</th>
                <th>ML_3WAY</th><th>SPREAD_2WAY</th><th>Live odds</th><th>Avg latency</th><th>Last success</th></tr></thead>
              <tbody>
                {coverage.books.map((b: any) => (
                  <tr key={b.source_name}>
                    <td>{b.source_name} {b.is_reference_feed && <span className="badge">reference</span>}</td>
                    <td><span className={`badge ${b.status === 'WORKS' ? 'pos' : b.status === 'BROKEN' ? 'neg' : b.status === 'EMPTY' ? 'amber' : 'muted'}`}>{b.status}</span></td>
                    <td>{b.execution_candidate ? <span className="badge pos">yes</span> : <span className="badge muted">no</span>}</td>
                    <td className="mono">{b.non_empty_responses} / {b.empty_responses} / {b.error_responses}</td>
                    <td>{b.ml_3way_available ? 'yes' : 'no'}</td>
                    <td>{b.spread_2way_available ? 'yes' : 'no'}</td>
                    <td>{b.live_odds_available ? 'yes' : 'no'}</td>
                    <td className="mono">{b.response_latency_ms_avg != null ? `${Math.round(b.response_latency_ms_avg)}ms` : '—'}</td>
                    <td className="mono small">{fmtDT(b.last_successful_observation)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="muted small">Scanner refuses to run while any tracked match is inside its live window (KO±2min), and is hard-capped at 40 API calls per scan — it never competes with first-live capture.</p>
        </div>
      )}

      {cap && (
        <div className="card scroll-x" style={{ marginTop: 16 }}>
          <h3>BetsAPI capability report</h3>
          {cap.hard_blocker && <div className="msg amber">{cap.hard_blocker}</div>}
          <table>
            <thead><tr><th>Capability</th><th>Status</th><th>Endpoint</th><th>Last code</th><th>Observed</th><th>Supports</th><th>Parser risk</th></tr></thead>
            <tbody>
              {cap.endpoints.map((e: any) => (
                <tr key={e.name}>
                  <td>{e.name}</td>
                  <td><span className={`badge ${e.status === 'works' ? 'pos' : e.status === 'broken' ? 'neg' : e.status === 'missing' ? 'amber' : 'muted'}`}>{e.status}</span></td>
                  <td className="mono small">{e.endpoint}</td>
                  <td className="mono">{e.last_status_code ?? '—'}</td>
                  <td className="mono small">{e.last_observed_at ?? '—'}</td>
                  <td className="small">{e.supports.join(', ') || '—'}</td>
                  <td className="small muted">{e.parser_risk}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted small">Run <span className="mono">/api/provider/capability-report?probe=true</span> only when a key is configured. It makes real provider calls.</p>
        </div>
      )}

      <div className="grid cols-2" style={{ marginTop: 16 }}>
        <div className="card scroll-x">
          <h3>League intelligence</h3>
          {leagues.length === 0 ? <div className="empty">No finished matches.</div> : (
            <table>
              <thead><tr><th>League</th><th>Matches</th><th>Avg goals</th><th>Variance</th><th>Draw%</th><th>Bet ROI</th></tr></thead>
              <tbody>
                {leagues.map(l => (
                  <tr key={l.league}>
                    <td>{l.league} {l.seed_influenced && <span className="badge amber">SEED</span>}</td><td className="mono">{l.matches}</td>
                    <td className="mono">{l.avg_goals}</td><td className="mono">{l.goal_variance}</td>
                    <td className="mono">{Math.round(l.draw_rate * 100)}%</td>
                    <td className="mono">{l.bet_roi_pct ?? '—'}% <span className="muted">(n={l.bet_n})</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        <div className="card scroll-x">
          <h3>Sportsbook intelligence</h3>
          {books.length === 0 ? <div className="empty">No sportsbook data.</div> : (
            <table>
              <thead><tr><th>Book</th><th>Snapshots</th><th>Bets</th><th>ROI</th><th>Avg CLV</th><th>Exec latency</th><th>Acc/Rej</th><th>Avg limit</th></tr></thead>
              <tbody>
                {books.map(b => (
                  <tr key={b.sportsbook}>
                    <td>{b.sportsbook}</td><td className="mono">{b.snapshots}</td>
                    <td className="mono">{b.bets}</td><td className="mono">{b.roi_pct ?? '—'}%</td>
                    <td className="mono">{b.avg_clv_pct ?? '—'}%</td>
                    <td className="mono">{b.avg_exec_latency_s != null ? `${b.avg_exec_latency_s}s` : '—'}</td>
                    <td className="mono">{b.accepted}/{b.rejected}</td>
                    <td className="mono">{b.avg_limit_seen ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </>
  )
}
