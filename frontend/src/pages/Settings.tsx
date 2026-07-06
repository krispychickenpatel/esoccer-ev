import { useEffect, useState } from 'react'
import { get, send } from '../api'

export default function SettingsPage() {
  const [s, setS] = useState<any>(null)
  const [msg, setMsg] = useState<{ t: string; ok: boolean } | null>(null)

  useEffect(() => { get('/settings').then(setS).catch(e => setMsg({ t: String(e), ok: false })) }, [])
  if (!s) return <div className="empty">Loading…</div>

  const set = (k: string) => (e: any) => setS({ ...s, [k]: e.target.value })

  const save = async () => {
    try {
      const payload = {
        ...s,
        starting_bankroll: Number(s.starting_bankroll), unit_size: Number(s.unit_size),
        max_bet_size: Number(s.max_bet_size), min_ev_pct: Number(s.min_ev_pct),
        kelly_fraction: Number(s.kelly_fraction), max_daily_loss: Number(s.max_daily_loss),
        max_weekly_loss: Number(s.max_weekly_loss),
        max_drawdown_shutdown_pct: Number(s.max_drawdown_shutdown_pct),
        exec_window_seconds: Number(s.exec_window_seconds),
        min_verified_history: Number(s.min_verified_history),
        min_similar_sample: Number(s.min_similar_sample),
        validation_max_matches: Number(s.validation_max_matches),
        include_seed_data: s.include_seed_data === true || s.include_seed_data === 'true',
        poller_enabled: s.poller_enabled === true || s.poller_enabled === 'true',
        validation_mode_enabled: s.validation_mode_enabled === true || s.validation_mode_enabled === 'true',
        sportsbooks_tracked: typeof s.sportsbooks_tracked === 'string'
          ? s.sportsbooks_tracked.split(',').map((x: string) => x.trim()).filter(Boolean)
          : s.sportsbooks_tracked,
        markets_tracked: typeof s.markets_tracked === 'string'
          ? s.markets_tracked.split(',').map((x: string) => x.trim()).filter(Boolean)
          : s.markets_tracked,
        tracked_leagues: typeof s.tracked_leagues === 'string'
          ? s.tracked_leagues.split(',').map((x: string) => x.trim()).filter(Boolean)
          : s.tracked_leagues,
      }
      setS(await send('PUT', '/settings', payload))
      setMsg({ t: 'Settings saved', ok: true })
    } catch (e) { setMsg({ t: String(e), ok: false }) }
  }

  const listVal = (v: any) => (Array.isArray(v) ? v.join(', ') : v)

  return (
    <>
      <h1>Settings</h1>
      <p className="sub">Data API keys never go here — they live in backend/.env only.</p>
      {msg && <div className={`msg ${msg.ok ? 'ok' : 'err'}`}>{msg.t}</div>}
      <div className="card" style={{ maxWidth: 720 }}>
        <div className="form-grid">
          <div className="field"><label>Starting bankroll</label><input value={s.starting_bankroll} onChange={set('starting_bankroll')} /></div>
          <div className="field"><label>Unit size</label><input value={s.unit_size} onChange={set('unit_size')} /></div>
          <div className="field"><label>Max bet size</label><input value={s.max_bet_size} onChange={set('max_bet_size')} /></div>
          <div className="field"><label>Min EV %</label><input value={s.min_ev_pct} onChange={set('min_ev_pct')} /></div>
          <div className="field"><label>Kelly fraction</label><input value={s.kelly_fraction} onChange={set('kelly_fraction')} /></div>
          <div className="field"><label>Max daily loss</label><input value={s.max_daily_loss} onChange={set('max_daily_loss')} /></div>
          <div className="field"><label>Max weekly loss</label><input value={s.max_weekly_loss} onChange={set('max_weekly_loss')} /></div>
          <div className="field"><label>Max drawdown shutdown %</label><input value={s.max_drawdown_shutdown_pct} onChange={set('max_drawdown_shutdown_pct')} /></div>
          <div className="field"><label>Exec window (s after live)</label><input value={s.exec_window_seconds} onChange={set('exec_window_seconds')} /></div>
          <div className="field"><label>Min verified history for BET</label><input value={s.min_verified_history} onChange={set('min_verified_history')} /></div>
          <div className="field"><label>Min similar-setup sample</label><input value={s.min_similar_sample} onChange={set('min_similar_sample')} /></div>
          <div className="field"><label>Include seed data in analysis</label>
            <select value={String(s.include_seed_data)} onChange={e => setS({ ...s, include_seed_data: e.target.value === 'true' })}>
              <option value="true">ON (early testing)</option><option value="false">OFF (verified only)</option>
            </select></div>
          <div className="field" style={{ gridColumn: '1 / -1' }}><label>Tracked leagues (comma-sep, poller only polls these)</label>
            <input value={listVal(s.tracked_leagues)} onChange={set('tracked_leagues')}
              placeholder="Esoccer Battle - 8 mins, GT League, H2H GG League" /></div>
          <div className="field"><label>Odds poller</label>
            <select value={String(s.poller_enabled)} onChange={e => setS({ ...s, poller_enabled: e.target.value === 'true' })}>
              <option value="false">disabled</option><option value="true">enabled (needs BETSAPI_KEY)</option>
            </select></div>
          <div className="field"><label>First-Live Validation Mode</label>
            <select value={String(s.validation_mode_enabled)} onChange={e => setS({ ...s, validation_mode_enabled: e.target.value === 'true' })}>
              <option value="false">off (normal tracking)</option>
              <option value="true">on (track only the N soonest-kickoff matches)</option>
            </select></div>
          <div className="field"><label>Validation mode: max matches tracked</label>
            <input value={s.validation_max_matches} onChange={set('validation_max_matches')} /></div>
        </div>
        <p className="muted small">First-Live Validation Mode narrows the poller to just the N soonest-kickoff
          tracked matches, to prove out first-live capture latency under controlled load instead of full production
          volume. Leave off for normal operation.</p>
        <div className="field"><label>Sportsbooks tracked (comma-sep)</label>
          <input value={listVal(s.sportsbooks_tracked)} onChange={set('sportsbooks_tracked')} /></div>
        <div className="field"><label>Markets tracked (comma-sep)</label>
          <input value={listVal(s.markets_tracked)} onChange={set('markets_tracked')} /></div>
        <div className="field"><label>Discord webhook URL</label>
          <input value={s.discord_webhook_url} onChange={set('discord_webhook_url')} placeholder="https://discord.com/api/webhooks/…" /></div>
        <div className="field"><label>Telegram bot token</label>
          <input value={s.telegram_bot_token} onChange={set('telegram_bot_token')} /></div>
        <div className="field"><label>Telegram chat ID</label>
          <input value={s.telegram_chat_id} onChange={set('telegram_chat_id')} /></div>
        <button className="primary" onClick={save}>Save settings</button>
      </div>
    </>
  )
}
