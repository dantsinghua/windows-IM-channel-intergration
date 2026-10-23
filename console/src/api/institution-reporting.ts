export interface InstitutionReportingStatus {
  contract: 'its-relay-path-v1'
  active: boolean
  account_workers: number
  last_directory_at: number
  errors: Record<string, string>
  history: Record<string, { scanned: number; published: number; media_bytes: number; error: string | null }>
  native_presence: Record<string, { state: 'online' | 'offline' | 'unknown'; code: string; confirmed_at: number; channel?: 'qidian' | 'wechat'; account_uid?: string }>
}
const record = (v: unknown): Record<string, unknown> | null => v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : null
const number = (v: unknown): number => typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : 0
const code = (v: unknown): string => typeof v === 'string' && /^[A-Za-z0-9_.:-]{1,120}$/.test(v) ? v : 'DETAIL_UNAVAILABLE'
export function normalizeInstitutionReporting(value: unknown): InstitutionReportingStatus | undefined {
  const v = record(value)
  if (!v || v.contract !== 'its-relay-path-v1' || typeof v.active !== 'boolean') return undefined
  const out: InstitutionReportingStatus = { contract: 'its-relay-path-v1', active: v.active, account_workers: number(v.account_workers), last_directory_at: number(v.last_directory_at), errors: {}, history: {}, native_presence: {} }
  for (const [name, error] of Object.entries(record(v.errors) || {}).slice(0, 100)) {
    out.errors[/^[A-Za-z0-9_.:-]{1,120}$/.test(name) ? name : 'component'] = code(error)
  }
  for (const [uid, raw] of Object.entries(record(v.history) || {}).slice(0, 30)) {
    const h = record(raw)
    if (h && /^[0-9]{1,30}$/.test(uid)) out.history[uid] = { scanned: number(h.scanned), published: number(h.published), media_bytes: number(h.media_bytes), error: h.error == null ? null : code(h.error) }
  }
  for (const [uid, raw] of Object.entries(record(v.native_presence) || {}).slice(0, 30)) {
    const p = record(raw)
    if (p && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/.test(uid)) out.native_presence[uid] = { state: p.state === 'online' || p.state === 'offline' ? p.state : 'unknown', code: code(p.code), confirmed_at: number(p.confirmed_at), channel: p.channel === 'wechat' ? 'wechat' : 'qidian', account_uid: typeof p.account_uid === 'string' ? p.account_uid : uid }
  }
  return out
}