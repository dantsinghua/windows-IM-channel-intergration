/**
 * Agent HTTP 客户端(01 §2.5「HTTP 客户端约定」/ §5.2)。
 *
 * - 统一信封 `{ok, code, data, error, trace_id}`(00 §10)
 * - 每请求带 `X-Trace-Id`(ULID,00 §6)与 `X-QT-Api-Min`(版本协商,02 §3.8)
 * - **令牌由主进程在 `webRequest` 网络层注入**,渲染进程从不接触 Authorization(§2.3)
 * - 401 → 让主进程重取令牌后重放一次;429 → 读 `Retry-After` 退避重试一次;
 *   **507 单独识别为 `DISK_FULL`,不当 500 自动重试**(R-02)
 * - 🔴 幂等键落**请求 body 的 `idempotency_key`**(02 §3.4 #2/#28/#43;全 `docs/` 不存在
 *   `X-Idempotency-Key` 这个头)
 * - 🔴 **业务结果 ≠ 传输错误**(02 §3.4 #28 R6-52):`requestCommand()` 只按 HTTP 状态判失败,
 *   `200 + ok:false + 业务结果码` 原样回完整 `CommandResult` 给页面渲染
 */

import { ulid } from './ulid'
import type { Envelope, ApiError } from './types'

/**
 * 控制台所需的最低 API 次版本(02 §3.8)。
 * 🔴 取值唯一出处 = `docs/07` `[api] api_version="1.0"`(= 02 §3.9 配置总表同一行)。
 * 凭空往高写会让**每个**请求被 middleware 判 `426 UPGRADE_REQUIRED`(E-01)。
 */
export const API_MIN_VERSION = '1.0'

/** 渲染进程只连 Agent 17600;17610 被主进程 webRequest 阻断(R-07/§11.19) */
export const AGENT_BASE = '/api/v1'

export class ApiFailure extends Error {
  readonly code: string
  readonly status: number
  readonly traceId: string
  readonly detail: ApiError
  /**
   * 原始信封。`409 IDEMPOTENT_REPLAY` 时 #28 的响应体仍是完整 `CommandResult`(02 §3.4 #28 / B-06),
   * 页面可从这里取首次结果显示,不必再发一次。
   */
  readonly envelope: Envelope<unknown> | null

  constructor(code: string, status: number, traceId: string, detail: ApiError, envelope: Envelope<unknown> | null = null) {
    super(detail.message || code)
    this.name = 'ApiFailure'
    this.code = code
    this.status = status
    this.traceId = traceId
    this.detail = detail
    this.envelope = envelope
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
  /** 写类指令的幂等键;**并进 body 的 `idempotency_key`**(不传则不带) */
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
  /** 426:版本协商不通过(02 §3.8)——要给出明确 UI 提示,不能只弹一句「请求失败」 */
  onUpgradeRequired?: (info: { needMin: string; serverVersion: string | null; message: string }) => void
  /**
   * #82 drain 之后**所有写操作**都会 `503` `reason=draining`(backend-api-2 §6)。
   * `true` = 刚撞上排空;`false` = 写操作又成功了(Agent 重启后自动恢复)。
   * 🔴 后端**没有 undrain 端点**,所以这个状态只能靠「写操作又通了」来解除,不能本地拦请求。
   */
  onDraining?: (draining: boolean) => void
}

let hooks: HttpHooks = {}

export function configureHttp(h: HttpHooks): void {
  hooks = { ...hooks, ...h }
}

/** 最近一次请求的 trace_id(成功响应带 `trace_id` 时用服务端的,否则回落本地 ULID) */
export interface TraceEntry {
  at: string
  method: string
  path: string
  status: number
  code: string
  traceId: string
}

const TRACE_MAX = 50
const traces: TraceEntry[] = []

/** 撞上过 `503 draining` 的写端点路径(见 `perform()` 里的说明) */
const drainingPaths = new Set<string>()

function recordTrace(e: TraceEntry): void {
  traces.unshift(e)
  if (traces.length > TRACE_MAX) traces.length = TRACE_MAX
}

/** 「复制 trace」与本地日志用(总控裁决⑤:成功响应带 `trace_id`,客户端保留) */
export function recentTraces(): readonly TraceEntry[] {
  return traces
}

export function lastTraceId(): string {
  return traces[0]?.traceId ?? ''
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

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

/**
 * 幂等键并进 body(02 §3.4 #2/#28/#43)。
 * body 里已经写了 `idempotency_key` 就不覆盖(#28/#36 是调用方自己给的那把键)。
 */
function withIdempotencyKey(body: unknown, key?: string): unknown {
  if (!key) return body
  if (body === undefined) return { idempotency_key: key }
  if (!isPlainObject(body)) return body
  return body.idempotency_key === undefined ? { ...body, idempotency_key: key } : body
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
    const parsed = JSON.parse(text) as Record<string, unknown>
    // 后端全局异常处理器缺位时 FastAPI 回 `{"detail":"Not Found"}`(不是 00 §10 信封)——
    // 归一成信封,页面才能拿到人话而不是「请求失败」四个字。
    if (!res.ok && typeof parsed.detail === 'string' && parsed.ok === undefined) {
      return {
        ok: false,
        error: { message: String(parsed.detail), retryable: false, needs_human: false },
      }
    }
    return parsed as Envelope<unknown>
  } catch {
    return { ok: false, error: { message: text.slice(0, 500), retryable: false, needs_human: false } }
  }
}

interface Attempted {
  res: Response
  env: Envelope<unknown>
  traceId: string
  serverTrace: string
}

/** 发一次请求(含 401 重取 / 429 退避),回 `{res, env}`,**不判 `ok`** */
async function perform(path: string, opts: RequestOptions): Promise<Attempted> {
  const traceId = ulid()
  const method = opts.method ?? 'GET'
  const headers: Record<string, string> = {
    'X-Trace-Id': traceId,
    'X-QT-Api-Min': API_MIN_VERSION,
    Accept: 'application/json',
  }
  const payload = withIdempotencyKey(opts.body, opts.idempotencyKey)
  let bodyInit: BodyInit | undefined
  if (payload !== undefined) {
    headers['Content-Type'] = 'application/json'
    bodyInit = JSON.stringify(payload)
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

    const env = await readEnvelope(res)
    const serverTrace = (env.trace_id as string) || traceId

    // 426:版本协商不通过 —— 给出「要 X、服务端是 Y」的明确提示(02 §3.8)
    if (res.status === 426) {
      hooks.onUpgradeRequired?.({
        needMin: API_MIN_VERSION,
        serverVersion: apiVersion,
        message: env.error?.message
          ?? `控制台需要 API ${API_MIN_VERSION},当前 Agent ${apiVersion ?? '未知'};请用安装包整体升级。`,
      })
    }

    /*
     * #82 排空:置位/解除全局横幅(提示语要写「正在为升级排空」,不是「后端故障」)。
     *
     * 🔴 解除条件只认「**曾经被 draining 拒过的那个 path** 又通了」——
     * 实测真后端的拦截面只有总线指令类(commands / broadcast / 账号动作),
     * 设置类写端点在排空期间**照样成功**;若写成「任何写操作成功就解除」,
     * 用户随手改一条设置就会把横幅抹掉,而 Agent 其实还在排空。
     */
    if (method !== 'GET') {
      if (res.status === 503 && env.error?.reason === 'draining') {
        drainingPaths.add(path)
        hooks.onDraining?.(true)
      } else if (res.ok && drainingPaths.delete(path) && drainingPaths.size === 0) {
        hooks.onDraining?.(false)
      }
    }

    recordTrace({
      at: new Date().toISOString(),
      method,
      path,
      status: res.status,
      code: (env.code as string) || (res.ok ? 'OK' : statusToCode(res.status)),
      traceId: serverTrace,
    })

    return { res, env, traceId, serverTrace }
  }
}

function toFailure(a: Attempted): ApiFailure {
  const code = (a.env.code as string) || statusToCode(a.res.status)
  const detail: ApiError = a.env.error ?? {
    message: `请求失败(HTTP ${a.res.status})`,
    retryable: false,
    needs_human: false,
  }
  return new ApiFailure(code, a.res.status, a.serverTrace, detail, a.env)
}

/** 底层请求:返回完整信封(调用方决定怎么取 data);`ok:false` 与 HTTP 错误都抛 */
export async function requestEnvelope<T>(path: string, opts: RequestOptions = {}): Promise<Envelope<T>> {
  const a = await perform(path, opts)
  if (!a.res.ok || a.env.ok === false) throw toFailure(a)
  const env = a.env as Envelope<T>
  env.trace_id = a.serverTrace
  return env
}

/** 常用形态:返回 `data`(单对象端点若顶层平铺则原样回信封,02 §3.4「单对象端点的信封」) */
export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const env = await requestEnvelope<T>(path, opts)
  if (env.data !== undefined) return env.data as T
  // 顶层平铺 + ok 的端点(#7/#9/#10/#19/#69/#72)
  const { ok: _ok, code: _code, error: _error, trace_id: _t, ...rest } = env
  return rest as unknown as T
}

/**
 * 🔴 一次性明文凭据的取法(N-1;#91 新建 API 客户端 / #92 轮换 / #67 HMAC 密钥)。
 *
 * 这类响应**只下发一次**,客户端这一跳丢了就再也拿不到(02 #91「**一次性**返回 `token` 或 `secret`」)。
 * 所以绝不套用 `request()` —— 它「有 `data` 就返回 `data`」,对
 * 「既包 `data` 又在顶层放业务键」的混合形状会**静默丢键**(与 E-04 同型,本轮 N-1 的前端次责)。
 * 一律先拿完整信封,再自己挑键。
 *
 * ⚠️ **兼容分支(待后端收口后删)**:R6-55 要求「包 `data`」与「顶层平铺」二选一,
 * 后端另一位 agent 正把 #91/#92 收口成「令牌与行同在 `data` 里」的单一形状;
 * 收口前真后端是 `{ok, data:{行}, app_id, token, trace_id}`(两种形状都占)。
 * 这里两形都认 —— 后端收口后,把「顶层」那一路连同本段注释一起删掉。
 */
export function pickOnceSecret(env: Envelope<unknown>, key: string): string | null {
  const data = isPlainObject(env.data) ? env.data : null
  const inData = data ? data[key] : undefined
  if (typeof inData === 'string' && inData) return inData
  // ↓↓ 兼容分支:令牌在顶层(后端收口后删这三行) ↓↓
  const top = env[key]
  if (typeof top === 'string' && top) return top
  // ↑↑ 兼容分支结束 ↑↑
  return null
}

/**
 * 🔴 指令类端点(#28 / #29 / #36)专用通道 —— 02 §3.4 #28 R6-52。
 *
 * 「HTTP 状态说的是这次调用有没有被受理执行,结果码说的是执行成了没有」:
 * - **只有** HTTP 层错误(鉴权/参数/目标/并发/容量/磁盘/内部)抛 `ApiFailure`;
 * - `200`/`202` 一律把**整个信封**原样回给页面 —— `ok:false` 的
 *   `SEND_FAILED` / `SEND_CALLED_BUT_UNCONFIRMED` / `GATE_BLOCKED` / `LOGIN_REQUIRED` /
 *   `CAPTCHA_REQUIRED` / `UNSUPPORTED` / `NOT_APPLICABLE` / `TIMEOUT` 是**正常业务结果**,
 *   页面按 `code`/`cost_ms`/`source`/`state_before`/`state_after`/`data` 渲染。
 *
 * 绝不套用 `request()`:`CommandResult` 顶层自带业务 `data` 键,
 * 「有 data 就返回 data」会把 `CommandResult` 拆成它的 `data`(E-04)。
 */
export async function requestCommand<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const a = await perform(path, opts)
  if (!a.res.ok) throw toFailure(a)
  const env = a.env as Envelope<unknown>
  env.trace_id = a.serverTrace
  return env as unknown as T
}

/** 列表端点:统一 `{data, next_cursor}`(C-42) */
export async function requestList<T>(
  path: string,
  opts: RequestOptions = {},
): Promise<{ items: T[]; nextCursor: string | null }> {
  const env = await requestEnvelope<T[]>(path, opts)
  return { items: (env.data as T[]) ?? [], nextCursor: (env.next_cursor as string | null) ?? null }
}

/**
 * 二进制端点的完整结果(#33 截图 / #50 媒体 / #55 媒体 / #52 导出产物)。
 *
 * 🔴 非 JSON 响应**不带 `trace_id` 字段**(02 §3.4 通用段例外②:一个字节都不碰),
 * 复盘要用的三样都在响应头里(backend-api-2 §6):
 * `X-QT-Media-Id` / `X-QT-Sha256` / `X-QT-Trace-Id`。
 *
 * `#50` 懒下载还没完时后端回 **`202 {state:'pending'}`**(JSON,不是图) ——
 * 这时 `blob` 为 `null`、`pending` 为 `true`,调用方该轮询本端点或等 `message` 事件的 `media[].state`。
 */
export interface BinaryResult {
  blob: Blob | null
  mediaId: string | null
  sha256: string | null
  traceId: string
  contentType: string
  /** 202:懒下载未完成(#50) */
  pending: boolean
}

/** 二进制端点(截图、媒体、导出流);连同 `X-QT-Media-Id`/`X-QT-Sha256` 一起回 */
export async function requestBinary(path: string, opts: RequestOptions = {}): Promise<BinaryResult> {
  const localTrace = ulid()
  const res = await fetch(buildUrl(path, opts.query), {
    method: opts.method ?? 'GET',
    headers: { 'X-Trace-Id': localTrace, 'X-QT-Api-Min': API_MIN_VERSION },
    signal: opts.signal,
  })
  const traceId = res.headers.get('X-QT-Trace-Id') || localTrace
  if (!res.ok) {
    // 错误分支后端回的是 JSON 信封(00 §10),把 code/message 原样交给页面
    const env = await readEnvelope(res)
    recordTrace({
      at: new Date().toISOString(), method: opts.method ?? 'GET', path,
      status: res.status, code: (env.code as string) || statusToCode(res.status),
      traceId: (env.trace_id as string) || traceId,
    })
    throw new ApiFailure(
      (env.code as string) || statusToCode(res.status),
      res.status,
      (env.trace_id as string) || traceId,
      env.error ?? { message: `取二进制失败(HTTP ${res.status})`, retryable: res.status >= 500, needs_human: false },
      env,
    )
  }
  const contentType = res.headers.get('Content-Type') ?? ''
  // #50 的 `202 {state:'pending'}`:懒下载还没完,不是图
  const pending = res.status === 202 || contentType.includes('application/json')
  recordTrace({
    at: new Date().toISOString(), method: opts.method ?? 'GET', path,
    status: res.status, code: pending ? 'PENDING' : 'OK', traceId,
  })
  return {
    blob: pending ? null : await res.blob(),
    mediaId: res.headers.get('X-QT-Media-Id'),
    sha256: res.headers.get('X-QT-Sha256'),
    traceId,
    contentType,
    pending,
  }
}

/** 只要图本身的场合(拿不到就是异常);要 `media_id`/`sha256` 请用 `requestBinary` */
export async function requestBlob(path: string, opts: RequestOptions = {}): Promise<Blob> {
  const r = await requestBinary(path, opts)
  if (!r.blob) {
    throw new ApiFailure('NOT_READY', 202, r.traceId, {
      message: '媒体仍在下载中,请稍后重试', reason: 'media_pending', retryable: true, needs_human: false,
    })
  }
  return r.blob
}

/** 新幂等键:默认自动 ULID(01 §2.7.5) */
export function newIdempotencyKey(): string {
  return ulid()
}
