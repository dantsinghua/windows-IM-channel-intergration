import { describe, expect, it, vi } from 'vitest'
import { buildRecoveryScript, createWinAgentRecovery, registerRecoveryIpc } from '../../electron/main/winagent-recovery'

const online = { service: 'online', session: 'online', code: 'OK', identityVerified: true }
function fixture() {
  const run = vi.fn().mockResolvedValue(online)
  const probeWinAgent = vi.fn().mockResolvedValue(true)
  const probeAgent = vi.fn().mockResolvedValue(false)
  const recovery = createWinAgentRecovery({ platform: 'win32', run, probeWinAgent, probeAgent, refresh: vi.fn().mockResolvedValue(true), delay: vi.fn().mockResolvedValue(undefined) })
  return { run, probeWinAgent, probeAgent, recovery }
}
describe('fixed local WinAgent recovery', () => {
  it('keeps SCM, real session health and collector API independent', async () => {
    const f = fixture(); const s = await f.recovery.status()
    expect(s).toMatchObject({ service: 'online', session: 'online', api: 'offline' })
  })
  it('does not infer a logged-out session from failed authentication or unknown health', async () => {
    const f = fixture(); f.probeWinAgent.mockResolvedValue(null)
    expect((await f.recovery.status()).session).toBe('unknown')
    f.run.mockRejectedValue(new Error('access denied'))
    expect(await f.recovery.status()).toMatchObject({ service: 'unknown', session: 'unknown' })
    expect((await f.recovery.status()).api).toBe('offline')
  })
  it('preserves UAC cancellation and never reports startup success', async () => {
    const f = fixture(); f.run.mockResolvedValue({ service: 'offline', session: 'offline', code: 'UAC_CANCELLED', identityVerified: true })
    expect(await f.recovery.start()).toMatchObject({ ok: false, code: 'UAC_CANCELLED' })
    expect(f.run).toHaveBeenCalledTimes(1)
  })
  it('coalesces repeated clicks into one privileged action', async () => {
    const f = fixture(); let resolve!: (v: unknown) => void
    f.run.mockImplementationOnce(() => new Promise(r => { resolve = r }))
    const a = f.recovery.start(); const b = f.recovery.start()
    expect(a).toBe(b); resolve(online)
    expect(await a).toMatchObject({ ok: true, code: 'STARTED' })
    expect(f.run.mock.calls.filter(([action]) => action === 'start')).toHaveLength(1)
  })
  it('requires verified SCM identity and actual session health after startup', async () => {
    const f = fixture(); f.run.mockResolvedValue({ ...online, identityVerified: false, code: 'IDENTITY_MISMATCH' })
    expect(await f.recovery.start()).toMatchObject({ ok: false, code: 'IDENTITY_MISMATCH' })
    const g = fixture(); g.probeWinAgent.mockResolvedValue(null)
    expect(await g.recovery.start()).toMatchObject({ ok: false, code: 'STARTING' })
  })
  it('does not run PowerShell on an unsupported platform', async () => {
    const run = vi.fn()
    const c = createWinAgentRecovery({ platform: 'linux', run, probeWinAgent: async () => null, probeAgent: async () => null, refresh: async () => false, delay: async () => undefined })
    expect(await c.start()).toMatchObject({ ok: false, code: 'UNSUPPORTED_PLATFORM' })
    expect(run).not.toHaveBeenCalled()
  })
  it('rejects every supplied renderer argument and untrusted sender', async () => {
    const handlers = new Map<string, (...a: unknown[]) => unknown>(); const f = fixture()
    registerRecoveryIpc({ handle: (n, fn) => handlers.set(n, fn) }, f.recovery, (e) => e === 'trusted')
    await expect(async () => handlers.get('qt:localService.start')!('trusted', { command: 'anything' })).rejects.toThrow('INVALID_ARGUMENTS')
    await expect(async () => handlers.get('qt:localService.status')!('untrusted')).rejects.toThrow('UNTRUSTED_SENDER')
    expect(f.run).not.toHaveBeenCalled()
  })
  it('uses ASCII-only fixed service scripts with no WSL, container or configuration operations', () => {
    const query = buildRecoveryScript('status'); const start = buildRecoveryScript('start')
    expect(/[^\x00-\x7f]/.test(query + start)).toBe(false)
    expect(start).toContain("Start-Service -Name 'QTradeWinAgent'")
    expect(start).toContain('-Verb RunAs')
    expect(start).toContain('IDENTITY_MISMATCH')
    expect(start).toContain('qtrade-winagent-user.exe')
    expect(query).not.toContain('Start-Service')
    expect(start).not.toMatch(/wsl\.exe|docker|Set-Content|WriteAllText|Set-Service|New-Service/)
  })
})
