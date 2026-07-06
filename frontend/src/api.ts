const BASE = '/api'

export async function get<T = any>(path: string): Promise<T> {
  const r = await fetch(BASE + path)
  if (!r.ok) throw new Error(await errText(r))
  return r.json()
}

export async function send<T = any>(method: string, path: string, body?: any): Promise<T> {
  const r = await fetch(BASE + path, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!r.ok) throw new Error(await errText(r))
  return r.json()
}

export async function upload<T = any>(path: string, file: File): Promise<T> {
  const fd = new FormData()
  fd.append('file', file)
  const r = await fetch(BASE + path, { method: 'POST', body: fd })
  if (!r.ok) throw new Error(await errText(r))
  return r.json()
}

async function errText(r: Response): Promise<string> {
  try {
    const j = await r.json()
    return typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail ?? j)
  } catch {
    return `${r.status} ${r.statusText}`
  }
}

export const fmtMoney = (v: number | null | undefined) =>
  v === null || v === undefined ? '—' : (v < 0 ? '-$' : '$') + Math.abs(v).toFixed(2)
export const fmtPct = (v: number | null | undefined, dp = 1) =>
  v === null || v === undefined ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(dp)}%`
export const fmtAm = (v: number | null | undefined) =>
  v === null || v === undefined ? '—' : (v > 0 ? `+${v}` : `${v}`)
// Backend stores/returns naive UTC datetimes (no 'Z' suffix). Without this,
// new Date() parses them as LOCAL time and every timestamp in the UI is off
// by the viewer's UTC offset.
const asUTC = (iso: string) => (/(Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : iso + 'Z')
export const fmtDT = (iso: string | null | undefined) =>
  iso ? new Date(asUTC(iso)).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'
export const cls = (v: number | null | undefined) =>
  v === null || v === undefined ? '' : v > 0 ? 'pos' : v < 0 ? 'neg' : ''
