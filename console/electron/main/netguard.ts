/**
 * 网络层加固(01 §2.3 / §6-2 / R-07 §11.19 [CONSOLESEC])。
 *
 * (a) `onBeforeSendHeaders` **只对 `17600/api/v1/**`** 注入 Agent 令牌 —— 按路径白名单,不按端口;
 * (b) `onBeforeRequest` 对 `17610/*` 一律 `cancel:true` —— 渲染进程连 WinAgent 都连不上;
 * (c) CSP 的 `connect-src` 只含 17600;
 * (d) 权限请求全部拒绝、导航与新窗口全部拒绝。
 */
import { session, shell, type Session } from 'electron'
import type { TokenHolder } from './token'

const AGENT_ORIGIN = 'http://127.0.0.1:17600'
const AGENT_HEADER_URLS = ['http://127.0.0.1:17600/api/v1/*', 'ws://127.0.0.1:17600/api/v1/*']
const WINAGENT_URLS = ['http://127.0.0.1:17610/*', 'ws://127.0.0.1:17610/*']

/** openExternal 只放行本机 NapCat WebUI `http://127.0.0.1:163NN/` */
export const EXTERNAL_ALLOW_RE = /^http:\/\/127\.0\.0\.1:163\d{2}\/?$/

export const CSP = [
  "default-src 'self'",
  `connect-src ${AGENT_ORIGIN} ws://127.0.0.1:17600`,
  `img-src 'self' blob: data: ${AGENT_ORIGIN}`,
  'media-src blob:',
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "font-src 'self' data:",
].join('; ')

export function installNetGuard(tokens: TokenHolder, sess: Session = session.defaultSession): void {
  // (a) 只给 Agent 的 /api/v1/** 注入 Authorization;令牌对页面脚本完全不可见
  sess.webRequest.onBeforeSendHeaders({ urls: AGENT_HEADER_URLS }, (details, cb) => {
    const token = tokens.agentToken
    const requestHeaders = { ...details.requestHeaders }
    if (token) requestHeaders.Authorization = `Bearer ${token}`
    cb({ requestHeaders })
  })

  // (b) 渲染进程发往 17610 的一切请求直接阻断
  sess.webRequest.onBeforeRequest({ urls: WINAGENT_URLS }, (_details, cb) => cb({ cancel: true }))

  // (c) 统一挂 CSP
  sess.webRequest.onHeadersReceived((details, cb) => {
    cb({
      responseHeaders: {
        ...details.responseHeaders,
        'Content-Security-Policy': [CSP],
      },
    })
  })

  // (d) 权限一律拒绝
  sess.setPermissionRequestHandler((_wc, _permission, callback) => callback(false))
  sess.setPermissionCheckHandler(() => false)
}

export async function openExternalAllowed(url: string): Promise<boolean> {
  if (!EXTERNAL_ALLOW_RE.test(url)) return false
  await shell.openExternal(url)
  return true
}
