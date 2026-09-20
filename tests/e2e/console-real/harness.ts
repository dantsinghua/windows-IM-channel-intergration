/**
 * 联调夹具:让控制台的 `src/api/*` 源码直接打**真 Agent**(全假后端,127.0.0.1:37821)。
 *
 * 控制台在 Electron 里跑,令牌由主进程 `webRequest` 网络层注入、URL 是相对的 `/api/v1`。
 * 这里用一层 fetch 包装模拟那两件事:补 base、注 Authorization。
 * `patch` 开关用来**模拟修掉已发现的硬伤**后继续往下测(每个开关对应一条缺陷,见交接文件)。
 */
export const BASE = process.env.AGENT_BASE_URL ?? 'http://127.0.0.1:37821'
export const WS_BASE = BASE.replace(/^http/, 'ws')

export const patch = {
  /** E-01:控制台恒带 `X-QT-Api-Min: 1.3`,真后端 api_version=1.0 ⇒ 426。true = 摘掉该头 */
  stripApiMin: false,
  /** E-02:控制台把幂等键放在头 `X-Idempotency-Key`,后端只认 body.idempotency_key。true = 顺手并进 body */
  idemIntoBody: false,
  token: 'e2e-admin-token' as string,
}

const realFetch = globalThis.fetch.bind(globalThis)
export const seen: { url: string; headers: Record<string, string>; status: number }[] = []

globalThis.fetch = (async (input: any, init: any = {}) => {
  let url = typeof input === 'string' ? input : String(input?.url ?? input)
  if (url.startsWith('/')) url = BASE + url
  const headers: Record<string, string> = { ...(init.headers ?? {}) }
  headers['Authorization'] = `Bearer ${patch.token}`               // 主进程 webRequest 做的事
  if (patch.stripApiMin) delete headers['X-QT-Api-Min']
  let body = init.body
  if (patch.idemIntoBody && headers['X-Idempotency-Key']) {
    const key = headers['X-Idempotency-Key']
    if (typeof body === 'string') {
      try {
        const o = JSON.parse(body)
        if (o && typeof o === 'object' && o.idempotency_key === undefined) {
          o.idempotency_key = key
          body = JSON.stringify(o)
        }
      } catch { /* 非 JSON 体不动 */ }
    } else if (body === undefined && (init.method ?? 'GET') !== 'GET') {
      body = JSON.stringify({ idempotency_key: key })
      headers['Content-Type'] = 'application/json'
    }
  }
  const res = await realFetch(url, { ...init, headers, body })
  seen.push({ url, headers, status: res.status })
  return res
}) as typeof fetch

/** 轮询等待条件成立(真服务、真时间) */
export async function waitFor<T>(fn: () => Promise<T | null | undefined | false>, timeoutMs = 20000, stepMs = 400): Promise<T> {
  const deadline = Date.now() + timeoutMs
  let last: unknown = null
  for (;;) {
    try {
      const v = await fn()
      if (v) return v as T
    } catch (e) { last = e }
    if (Date.now() > deadline) throw new Error(`waitFor 超时(${timeoutMs}ms);最后一次错误:${String(last)}`)
    await new Promise((r) => setTimeout(r, stepMs))
  }
}
