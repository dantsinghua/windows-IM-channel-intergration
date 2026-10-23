import { afterEach, describe, expect, it, vi } from 'vitest'
import { normalizeTrustedAgentOrigin } from '../../electron/main/origins'
import { TokenHolder } from '../../electron/main/token'
import { installNetGuard } from '../../electron/main/netguard'
import type { Session } from 'electron'

vi.mock('electron', () => ({ session: {}, shell: { openExternal: vi.fn() } }))
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

describe('认证管道发现的现场 WSL 地址', () => {
  it.each(['http://127.0.0.1:17600', 'http://172.20.1.2:17600'])(
    '只有本地恢复凭据时不向占位或旧 Agent 来源 %s 发业务请求', agentOrigin => {
      const requests = vi.fn(), headers = vi.fn()
      const sess = { webRequest: { onBeforeSendHeaders: headers, onBeforeRequest: requests, onHeadersReceived: vi.fn() },
        setPermissionRequestHandler: vi.fn(), setPermissionCheckHandler: vi.fn() } as unknown as Session
      const holder = { authState: 'ok', agentToken: null, agentBaseUrl: null, winagentToken: 'LOCAL_ONLY' } as TokenHolder
      installNetGuard(holder, sess, { agentOrigin })
      for (const protocol of ['http', 'ws']) {
        const callback = vi.fn()
        requests.mock.calls[0][1]({ url: `${agentOrigin.replace('http:', `${protocol}:`)}/api/v1/accounts` }, callback)
        expect(callback).toHaveBeenCalledWith({ cancel: true })
      }
      const callback = vi.fn()
      headers.mock.calls[0][1]({ requestHeaders: {} }, callback)
      expect(callback.mock.calls[0][0].requestHeaders).not.toHaveProperty('Authorization')
    })

  it('WA-only 到 guest 恢复再丢失时，刷新原子撤销旧 Agent 凭据和地址', async () => {
    vi.useFakeTimers()
    const localOnly = { agent_token: null, agent_base_url: null, winagent_token: 'LOCAL_ONLY', expires_at: Date.now() + 3600000 }
    const guestReady = { ...localOnly, agent_token: 'GUEST_ONLY', agent_base_url: 'http://172.21.1.2:17600' }
    const fetchTokens = vi.fn().mockResolvedValueOnce(localOnly).mockResolvedValueOnce(guestReady).mockResolvedValueOnce(localOnly)
    const onRefreshed = vi.fn()
    const holder = new TokenHolder({ fetchTokens, describe: () => 'fixture' }, Date.now, setTimeout, clearTimeout,
      { refreshIntervalMs: 30000, onRefreshed })
    try {
      expect(await holder.refresh()).toBe(true)
      expect(holder.authState).toBe('ok')
      expect(holder.winagentToken).toBe('LOCAL_ONLY')
      expect(holder.agentToken).toBeNull()
      expect(holder.agentBaseUrl).toBeNull()
      await vi.advanceTimersByTimeAsync(30000)
      expect(holder.agentToken).toBe('GUEST_ONLY')
      expect(holder.agentBaseUrl).toBe('http://172.21.1.2:17600')
      await vi.advanceTimersByTimeAsync(30000)
      expect(holder.authState).toBe('ok')
      expect(holder.agentToken).toBeNull()
      expect(holder.agentBaseUrl).toBeNull()
      expect(onRefreshed).toHaveBeenCalledTimes(3)
      expect(vi.getTimerCount()).toBe(1)
    } finally { holder.clear() }
  })
  it.each(['no_token', 'winagent_offline', 'ok'] as const)('Agent 请求按令牌状态 %s 受控，WinAgent始终阻断', state => {
    const requests = vi.fn()
    const sess = { webRequest: { onBeforeSendHeaders: vi.fn(), onBeforeRequest: requests, onHeadersReceived: vi.fn() },
      setPermissionRequestHandler: vi.fn(), setPermissionCheckHandler: vi.fn() } as unknown as Session
    const holder = { authState: state, agentToken: state === 'ok' ? 'fixture' : null } as TokenHolder
    installNetGuard(holder, sess, { agentOrigin: 'http://172.20.1.2:17600' })
    const callback = vi.fn()
    requests.mock.calls[0][1]({ url: 'http://172.20.1.2:17600/api/v1/accounts' }, callback)
    expect(callback).toHaveBeenLastCalledWith({ cancel: state !== 'ok' })
    requests.mock.calls[0][1]({ url: 'http://127.0.0.1:17610/wa/v1/health' }, callback)
    expect(callback).toHaveBeenLastCalledWith({ cancel: true })
  })
  it.each(['127.0.0.1', '10.23.4.5', '172.16.1.5', '172.31.254.5', '192.168.42.9'])(
    '允许规范私有 IPv4 %s 的固定 Agent 端口', ip => {
      const value = `http://${ip}:17600`
      expect(normalizeTrustedAgentOrigin(value)).toBe(value)
    })
  it.each([undefined, '', 'http://8.8.8.8:17600', 'http://172.15.1.2:17600', 'http://172.32.1.2:17600',
    'http://127.0.0.2:17600', 'http://0.0.0.0:17600', 'http://192.168.999.1:17600',
    'http://0172.20.1.2:17600', 'http://localhost:17600', 'http://172.20.1.2:80',
    'http://user:pass@172.20.1.2:17600', 'http://172.20.1.2:17600/api', 'https://172.20.1.2:17600'])(
    '拒绝缺失或非规范来源 %s', value => expect(normalizeTrustedAgentOrigin(value)).toBeNull())

  it('每 30 秒通过已有续签机制刷新，地址切换和令牌同批生效且只有一个定时器', async () => {
    vi.useFakeTimers()
    const bundle = (ip: string) => ({ agent_token: ip, winagent_token: 'fixture',
      agent_base_url: `http://${ip}:17600`, expires_at: Date.now() + 3600000 })
    const fetchTokens = vi.fn().mockResolvedValueOnce(bundle('172.20.1.2')).mockResolvedValue(bundle('172.20.1.3'))
    const refreshed = vi.fn()
    const holder = new TokenHolder({ fetchTokens, describe: () => 'fake' }, Date.now, setTimeout, clearTimeout,
      { refreshIntervalMs: 30000, onRefreshed: refreshed })
    await holder.refresh()
    expect(vi.getTimerCount()).toBe(1)
    await vi.advanceTimersByTimeAsync(29999)
    expect(fetchTokens).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(fetchTokens).toHaveBeenCalledTimes(2)
    expect(holder.agentBaseUrl).toBe('http://172.20.1.3:17600')
    expect(holder.agentToken).toBe('172.20.1.3')
    expect(refreshed).toHaveBeenCalledTimes(2)
    expect(vi.getTimerCount()).toBe(1)
    holder.clear()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('旧来源的网络回调不能取得切换后新来源的令牌', () => {
    const headers = vi.fn()
    const sess = { webRequest: { onBeforeSendHeaders: headers, onBeforeRequest: vi.fn(), onHeadersReceived: vi.fn() },
      setPermissionRequestHandler: vi.fn(), setPermissionCheckHandler: vi.fn() } as unknown as Session
    const holder = { agentToken: 'NEW_GUEST_TOKEN', agentBaseUrl: 'http://172.20.1.3:17600' } as TokenHolder
    installNetGuard(holder, sess, { agentOrigin: 'http://172.20.1.2:17600' })
    const callback = vi.fn()
    headers.mock.calls[0][1]({ requestHeaders: {} }, callback)
    expect(callback.mock.calls[0][0].requestHeaders).not.toHaveProperty('Authorization')
    installNetGuard(holder, sess, { agentOrigin: 'http://172.20.1.3:17600' })
    headers.mock.calls[1][1]({ requestHeaders: {} }, callback)
    expect(callback.mock.calls[1][0].requestHeaders.Authorization).toBe('Bearer NEW_GUEST_TOKEN')
    expect(headers.mock.calls[1][0].urls).toEqual([
      'http://172.20.1.3:17600/api/v1/*', 'ws://172.20.1.3:17600/api/v1/*',
    ])
  })

  it('guest 来源已切换时旧页面 HTTP/WS 请求直接取消，不只省略 Bearer', () => {
    const requests = vi.fn()
    const sess = { webRequest: { onBeforeSendHeaders: vi.fn(), onBeforeRequest: requests, onHeadersReceived: vi.fn() },
      setPermissionRequestHandler: vi.fn(), setPermissionCheckHandler: vi.fn() } as unknown as Session
    const holder = { authState: 'ok', agentToken: 'NEW_GUEST', agentBaseUrl: 'http://172.20.1.3:17600' } as TokenHolder
    installNetGuard(holder, sess, { agentOrigin: 'http://172.20.1.2:17600' })
    const callback = vi.fn()
    for (const protocol of ['http', 'ws']) {
      requests.mock.calls[0][1]({ url: `${protocol}://172.20.1.2:17600/api/v1/accounts` }, callback)
      expect(callback).toHaveBeenLastCalledWith({ cancel: true })
    }
  })
})
