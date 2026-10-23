/**
 * Agent / WinAgent 本机源与 CSP 的纯函数(不 import `electron`,渲染进程单测可直接引用)。
 *
 * 2026-10-10 真实链路实测:此前 netguard 把 Agent 源写死 17600,而渲染进程按相对路径请求页面来源(Vite / file://),
 * `connect-src` 把每一条 API 都拒掉,桌面壳对真 Agent 一条请求都发不出去。正式源由本机认证管道发现,
 * 开发配置仅认回环;CSP、令牌注入、preload 暴露给渲染进程的 `qt.endpoint.agent` 三处共用同一份。
 */
export const DEFAULT_AGENT_ORIGIN = 'http://127.0.0.1:17600'
export const DEFAULT_WINAGENT_ORIGIN = 'http://127.0.0.1:17610'
const LOCAL_ORIGIN_RE = /^http:\/\/127\.0\.0\.1:(\d{2,5})$/

/** 只接受本机回环的 `http://127.0.0.1:<port>`;其它一律回退默认(基线 §3:两端都只在本机) */
export function normalizeLocalOrigin(value: unknown, fallback: string): string {
  const s = typeof value === 'string' ? value.trim().replace(/\/+$/, '') : ''
  const match = LOCAL_ORIGIN_RE.exec(s)
  return match && Number(match[1]) <= 65535 ? s : fallback
}

/** 仅用于已经过本机管道鉴权的受管 WSL 地址,绝不用于用户输入的任意 URL。 */
export function normalizeTrustedAgentOrigin(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const match = /^http:\/\/((?:\d{1,3}\.){3}\d{1,3}):17600$/.exec(value)
  if (!match) return null
  const parts = match[1].split('.').map(Number)
  if (parts.some(n => n > 255) || parts.join('.') !== match[1]) return null
  const [a, b] = parts
  const allowed = match[1] === '127.0.0.1' || a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168)
  return allowed ? value : null
}

/**
 * CSP 随配置的 Agent 源生成。`devUrl` 非空时(`npm run dev` / 外接 Vite)把 Vite 来源的 http+ws 也放进
 * connect-src —— 那是模块热更新与开发代理;生产包里 `devUrl` 为空,不放任何额外来源。
 */
export function buildCsp(agentOrigin: string, devUrl?: string): string {
  const connect = [agentOrigin, agentOrigin.replace(/^http/, 'ws')]
  if (devUrl) {
    const dev = devUrl.replace(/\/+$/, '')
    connect.push(dev, dev.replace(/^http/, 'ws'))
  }
  return [
    "default-src 'self'",
    `connect-src ${connect.join(' ')}`,
    `img-src 'self' blob: data: ${agentOrigin}`,
    'media-src blob:',
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self' data:",
  ].join('; ')
}
