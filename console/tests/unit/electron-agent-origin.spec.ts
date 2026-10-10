/**
 * 2026-10-10 真实链路(Electron 壳 ↔ 真 Agent)揪出的缺陷回归:
 * 渲染进程此前只用相对 `/api/v1`,在 Electron 里会打到页面来源(Vite / file://),被主进程写死 17600 的 CSP 整体拒绝。
 * 现在 preload 暴露 `qt.endpoint.agent`,渲染进程据此拼绝对地址;主进程 CSP / 令牌注入按配置的本机源生成。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { buildCsp, normalizeLocalOrigin } from '../../electron/main/origins'

async function freshApi() {
  vi.resetModules()
  const http = await import('@/api/http')
  const ws = await import('@/api/ws')
  return { http, ws }
}

afterEach(() => { vi.unstubAllGlobals(); vi.resetModules() })

describe('渲染进程的 Agent 源', () => {
  it('浏览器形态(无 qt 桥)仍是同源相对路径,交给 dev 代理', async () => {
    vi.stubGlobal('qt', undefined)
    const { http, ws } = await freshApi()
    expect(http.AGENT_BASE).toBe('/api/v1')
    expect(ws.defaultEventsUrl()).toBe(`ws://${location.host}/api/v1/events`)
  })

  it('Electron 形态按 preload 给出的源拼绝对地址(HTTP 与 WS 同源)', async () => {
    vi.stubGlobal('qt', { endpoint: { agent: 'http://127.0.0.1:17650' } })
    const { http, ws } = await freshApi()
    expect(http.AGENT_BASE).toBe('http://127.0.0.1:17650/api/v1')
    expect(ws.defaultEventsUrl()).toBe('ws://127.0.0.1:17650/api/v1/events')
  })

  it('preload 给的源不是合法 origin 时退回相对路径,不拼出坏地址', async () => {
    vi.stubGlobal('qt', { endpoint: { agent: 'http://127.0.0.1:17650/api/v1/' } })
    const { http } = await freshApi()
    expect(http.AGENT_BASE).toBe('/api/v1')
  })
})

describe('主进程 netguard 的源与 CSP', () => {
  it('只接受本机回环 http://127.0.0.1:<port>,其它回退默认', () => {
    expect(normalizeLocalOrigin('http://127.0.0.1:17650/', 'http://127.0.0.1:17600')).toBe('http://127.0.0.1:17650')
    expect(normalizeLocalOrigin('http://192.168.3.5:17600', 'http://127.0.0.1:17600')).toBe('http://127.0.0.1:17600')
    expect(normalizeLocalOrigin('https://127.0.0.1:17600', 'http://127.0.0.1:17600')).toBe('http://127.0.0.1:17600')
    expect(normalizeLocalOrigin(undefined, 'http://127.0.0.1:17600')).toBe('http://127.0.0.1:17600')
  })

  it('CSP connect-src 跟随配置的 Agent 源;dev 下另放行 Vite 来源的 http+ws,生产不放', () => {
    const prod = buildCsp('http://127.0.0.1:17650')
    expect(prod).toContain('connect-src http://127.0.0.1:17650 ws://127.0.0.1:17650;')
    expect(prod).not.toContain('5313')
    expect(prod).toContain("img-src 'self' blob: data: http://127.0.0.1:17650")
    const dev = buildCsp('http://127.0.0.1:17650', 'http://127.0.0.1:5313/')
    expect(dev).toContain('connect-src http://127.0.0.1:17650 ws://127.0.0.1:17650 http://127.0.0.1:5313 ws://127.0.0.1:5313;')
    expect(dev).not.toContain('17610')
  })
})
