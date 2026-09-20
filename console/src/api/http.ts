/**
 * Agent HTTP 客户端(01 §2.5「HTTP 客户端约定」/ §5.2)。
 *
 * - 统一信封 `{ok, code, data, error, trace_id}`(00 §10)
 * - 每请求带 `X-Trace-Id`(ULID,00 §6)与 `X-QT-Api-Min`(版本协商,02 §3.8)
 * - **令牌由主进程在 `webRequest` 网络层注入**,渲染进程从不接触 Authorization(§2.3)
 * - 401 → 让主进程重取令牌后重放一次;429 → 读 `Retry-After` 退避重试一次;
 *   **507 单独识别为 `DISK_FULL`,不当 500 自动重试**(R-02)
 */

import { ulid } from './ulid'
import type { Envelope, ApiError } from './types'

/** 控制台所需的最低 API 次版本(02 §3.8) */
export const API_MIN_VERSION = '1.3'

/** 渲染进程只连 Agent 17600;17610 被主进程 webRequest 阻断(R-07/§11.19) */
export const AGENT_BASE = '/api/v1'

export class ApiFailure extends Error {
  readonly code: string
  readonly status: number
  readonly traceId: string
  readonly detail: ApiError

  constructor(code: string, status: number, traceId: string, detail: ApiError) {
    super(detail.message || code)
    this.name = 'ApiFailure'
    this.code = code
    this.status = status
    this.traceId = traceId
    this.detail = detail
  }

  /** 错误提示末尾显示 trace_id 前 8 位,方便对日志(01 §2.5) */
  get traceShort(): string {
    return this.traceId.slice(0, 8)
  }

  get reason(): string | null {
    return this.detail.reason ?? null
  }
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  query?: Record<string, unknown>
  body?: unknown
  /** 写类指令的幂等键(不传则不带) */
  idempotencyKey?: string
  signal?: AbortSignal
  /** 期望二进制(截图/媒体/导出流) */
  raw?: boolean
}

export interface HttpHooks {
  /** 401:让主进程经命名管道重取令牌;返回 true 表示可以重放一次 */
  onUnauthorized?: () => Promise<boolean>
  /** 仍然 401 时切门禁 */
  onTokenLost?: () => void
  /** 收到响应头里的 api 版本 */
  onApiVersion?: (v: string) => void
}

let hooks: HttpHooks = {}

export function configureHttp(h: HttpHooks): void {
  hooks = { ...hooks, ...h }
}

function buildUrl(path: string, query?: Record<string, unknown>): string {
  const url = path.startsWith('http') ? path : `${AGENT_BASE}${path}`
  if (!query) return url
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '') continue
    if (Array.isArray(v)) v.forEach((x) => qs.append(k, String(x)))
    else qs.append(k, String(v))
  }
  const s = qs.toString()
  return s ? `${url}${url.includes('?') ? '&' : '?'}${s}` : url
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}

/** HTTP 状态 → 结果码(00 §10 / 02 §3.4) */
export function statusToCode(status: number): string {
  switch (status) {
    case 400: return 'INVALID_ARGS'
    case 401: return 'UNAUTHORIZED'
    case 403: return 'FORBIDDEN'
    case 404: return 'TARGET_NOT_FOUND'
    case 409: return 'RESOURCE_EXHAUSTED'
    case 413: return 'INVALID_ARGS'
    case 426: return 'UPGRADE_REQUIRED'
    case 429: return 'RATE_LIMITED'
    // R-02:507 单独识别为 DISK_FULL,绝不当 500/INTERNAL 自动重试
    case 507: return 'DISK_FULL'
    case 503: return 'NOT_READY'
    default: return status >= 500 ? 'INTERNAL' : 'INTERNAL'
  }
}

async function readEnvelope(res: Response): Promise<Envelope<unknown>> {
  const text = await res.text()
  if (!text) return { ok: res.ok }
  try {
    return JSON.parse(text) as Envelope<unknown>
  } catch {
    return { ok: false, error: { message: text.slice(0, 500), retryable: false, needs_human: false } }
  }
}

/** 底层请求:返回完整信封(调用方决定怎么取 data) */
export async function requestEnvelope<T>(path: string, opts: RequestOptions = {}): Promise<Envelope<T>> {
  const traceId = ulid()
  const method = opts.method ?? 'GET'
  const headers: Record<string, string> = {
    'X-Trace-Id': traceId,
    'X-QT-Api-Min': API_MIN_VERSION,
    Accept: 'application/json',
  }
  if (opts.idempotencyKey) headers['X-Idempotency-Key'] = opts.idempotencyKey
  let bodyInit: BodyInit | undefined
  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    bodyInit = JSON.stringify(opts.body)
  }

  const url = buildUrl(path, opts.query)
  let attempt = 0
  let refreshed = false

  for (;;) {
    attempt += 1
    const res = await fetch(url, { method, headers, body: bodyInit, signal: opts.signal })
    const apiVersion = res.headers.get('X-QT-Api-Version')
    if (apiVersion) hooks.onApiVersion?.(apiVersion)

    if (res.status === 401 && !refreshed) {
      refreshed = true
      const ok = await (hooks.onUnauthorized?.() ?? Promise.resolve(false))
      if (ok) continue
      hooks.onTokenLost?.()
    }

    // 429:读 Retry-After(无则 2s)自动重试一次,再失败按 RATE_LIMITED 提示(§5.2)
    if (res.status === 429 && attempt === 1) {
      const ra = Number(res.headers.get('Retry-After') ?? '')
      await sleep(Number.isFinite(ra) && ra > 0 ? ra * 1000 : 2000)
      continue
    }

    const env = (await readEnvelope(res)) as Envelope<T>
    const serverTrace = (env.trace_id as string) || traceId

    if (!res.ok || env.ok === false) {
      const code = (env.code as string) || statusToCode(res.status)
      const detail: ApiError = env.error ?? {
        message: `请求失败(HTTP ${res.status})`,
        retryable: false,
        needs_human: false,
      }
      throw new ApiFailure(code, res.status, serverTrace, detail)
    }
    env.trace_id = serverTrace
    return env
  }
}

/** 常用形态:返回 `data`(单对象端点若顶层平铺则原样回信封,02 §3.4「单对象端点的信封」) */
export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const env = await requestEnvelope<T>(path, opts)
  if (env.data !== undefined) return env.data as T
  // 顶层平铺 + ok 的端点(#7/#9/#10/#19/#69/#72/#28)
  const { ok: _ok, code: _code, error: _error, trace_id: _t, ...rest } = env
  return rest as unknown as T
}

/** 列表端点:统一 `{data, next_cursor}`(C-42) */
export async function requestList<T>(
  path: string,
  opts: RequestOptions = {},
): Promise<{ items: T[]; nextCursor: string | null }> {
  const env = await requestEnvelope<T[]>(path, opts)
  return { items: (env.data as T[]) ?? [], nextCursor: (env.next_cursor as string | null) ?? null }
}

/** 二进制端点(截图、媒体、导出流) */
export async function requestBlob(path: string, opts: RequestOptions = {}): Promise<Blob> {
  const traceId = ulid()
  const res = await fetch(buildUrl(path, opts.query), {
    method: opts.method ?? 'GET',
    headers: { 'X-Trace-Id': traceId, 'X-QT-Api-Min': API_MIN_VERSION },
    signal: opts.signal,
  })
  if (!res.ok) {
    throw new ApiFailure(statusToCode(res.status), res.status, traceId, {
      message: `取二进制失败(HTTP ${res.status})`,
      retryable: res.status >= 500,
      needs_human: false,
    })
  }
  return await res.blob()
}

/** 新幂等键:默认自动 ULID(01 §2.7.5) */
export function newIdempotencyKey(): string {
  return ulid()
}
