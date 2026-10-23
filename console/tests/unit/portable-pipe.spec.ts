// @vitest-environment node
import { EventEmitter } from 'node:events'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPipeClient, PIPE_PATH } from '../../electron/main/winagent-pipe'

const h = vi.hoisted(() => ({ reply: {} as Record<string, unknown>, connects: [] as string[], writes: [] as string[], delayMs: 0 }))
vi.mock('node:net', async (importOriginal) => {
  const actual = await importOriginal<typeof import('node:net')>()
  const { EventEmitter: Emitter } = await import('node:events')
  function connect(path: string) {
    h.connects.push(path)
    const socket = new Emitter() as EventEmitter & { write: (text: string) => void; destroy: () => void }
    socket.write = text => {
      h.writes.push(text)
      const reply = () => {
        const bytes = Buffer.from(JSON.stringify(h.reply))
        socket.emit('data', bytes.subarray(0, 4))
        socket.emit('data', bytes.subarray(4))
        socket.emit('close')
      }
      if (h.delayMs) setTimeout(reply, h.delayMs)
      else reply()
    }
    socket.destroy = () => socket.emit('close')
    queueMicrotask(() => socket.emit('connect'))
    return socket
  }
  return { ...actual, connect, default: { ...actual, connect } }
})
beforeEach(() => {
  h.connects.splice(0); h.writes.splice(0)
  h.delayMs = 0
  h.reply = { agent_token: 'TEST_CONSOLE', winagent_token: 'TEST_CONSOLE', expires_at: Date.now() + 3600000,
    agent_base_url: 'http://172.20.16.2:17600' }
})
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

describe('真实管道客户端的线协议边界', () => {
  it('从固定本地管道请求令牌，分帧回复恢复完整可信端点', async () => {
    const bundle = await createPipeClient({ isPackaged: true }).fetchTokens()
    expect(h.connects).toEqual([PIPE_PATH])
    expect(h.writes.map(text => JSON.parse(text))).toEqual([{ op: 'issue_console_token' }])
    expect(bundle).toEqual(h.reply)
  })
  it.each([
    { agent_base_url: undefined }, { agent_base_url: 'http://8.8.8.8:17600' },
    { agent_token: '' }, { winagent_token: '' }, { expires_at: 0 }, { expires_at: '9999999999999' },
    { error: 'NOT_READY' },
  ])('拒绝不完整、过期或未就绪回复 %j', async patch => {
    Object.assign(h.reply, patch)
    await expect(createPipeClient({ isPackaged: true }).fetchTokens()).rejects.toThrow()
  })

  it('已鉴权的本地恢复可用，但明确没有 Agent 地址或 Agent 凭据', async () => {
    Object.assign(h.reply, { agent_token: null, agent_base_url: null })
    await expect(createPipeClient({ isPackaged: true }).fetchTokens()).resolves.toEqual(h.reply)
  })

  it.each([
    { agent_token: null }, { agent_base_url: null },
    { agent_token: undefined, agent_base_url: null },
    { agent_token: null, agent_base_url: undefined },
    { agent_token: '', agent_base_url: null },
    { agent_token: null, agent_base_url: '' },
    { agent_token: null, agent_base_url: 'http://127.0.0.1:17600' },
    { agent_token: null, agent_base_url: 'http://8.8.8.8:17600' },
    { agent_token: null, agent_base_url: null, winagent_token: '' },
    { agent_token: null, agent_base_url: null, winagent_token: '   ' },
    { agent_token: null, agent_base_url: null, winagent_token: undefined },
    { agent_token: null, agent_base_url: null, expires_at: 0 },
    { agent_token: null, agent_base_url: null, expires_at: '9999999999999' },
  ])('WA-only 必须严格 null/null，拒绝不对称或无效回复 %j', async patch => {
    Object.assign(h.reply, patch)
    await expect(createPipeClient({ isPackaged: true }).fetchTokens()).rejects.toThrow()
  })

  it('等待服务端 5 秒 guest 发现后仍能取得明确的 WA-only 回复', async () => {
    vi.useFakeTimers()
    h.delayMs = 5000
    Object.assign(h.reply, { agent_token: null, agent_base_url: null })
    const result = createPipeClient({ isPackaged: true }).fetchTokens().then(
      value => ({ ok: true, value }), () => ({ ok: false, value: null }),
    )
    await Promise.resolve()
    await vi.advanceTimersByTimeAsync(5000)
    expect(await result).toEqual({ ok: true, value: h.reply })
    expect(vi.getTimerCount()).toBe(0)
  })

  it('管道没有及时回复时 8 秒内失败，不永久阻塞连接界面', async () => {
    vi.useFakeTimers()
    h.delayMs = 20000
    let rejected = false
    const request = createPipeClient({ isPackaged: true }).fetchTokens().catch(() => { rejected = true })
    await Promise.resolve()
    await vi.advanceTimersByTimeAsync(7999)
    expect(rejected).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    await request
    expect(rejected).toBe(true)
    vi.clearAllTimers()
  })
})
