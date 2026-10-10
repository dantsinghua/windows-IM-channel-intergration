/**
 * Agent / WinAgent 本机源与 CSP 的纯函数(不 import `electron`,渲染进程单测可直接引用)。
 *
 * 2026-10-10 真实链路实测:此前 netguard 把 Agent 源写死 17600,而渲染进程按相对路径请求页面来源(Vite / file://),
 * `connect-src` 把每一条 API 都拒掉,桌面壳对真 Agent 一条请求都发不出去。现在源来自 console.toml `[endpoint]`,
 * 只认本机回环;CSP、令牌注入、preload 暴露给渲染进程的 `qt.endpoint.agent` 三处共用同一份。
 */
export const DEFAULT_AGENT_ORIGIN = 'http://127.0.0.1:17600'
export const DEFAULT_WINAGENT_ORIGIN = 'http://127.0.0.1:17610'
const LOCAL_ORIGIN_RE = /^http:\/\/127\.0\.0\.1:\d{2,5}$/

/** 只接受本机回环的 `http://127.0.0.1:<port>`;其它一律回退默认(基线 §3:两端都只在本机) */
export function normalizeLocalOrigin(value: unknown, fallback: string): string {
  const s = typeof value === 'string' ? value.trim().replace(/\/+$/, '') : ''
  return LOCAL_ORIGIN_RE.test(s) ? s : fallback
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
