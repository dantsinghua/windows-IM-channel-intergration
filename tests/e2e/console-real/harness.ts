/**
 * 联调夹具:让控制台的 `src/api/*` 源码直接打**真 Agent**(全假后端)。
 *
 * 控制台在 Electron 里跑,令牌由主进程 `webRequest` 网络层注入、URL 是相对的 `/api/v1`。
 * 这里用一层 fetch 包装模拟那两件事:补 base、注 Authorization。
 *
 * 🔴 第二轮(独立验收复测)改动:
 * 原来的 `patch.stripApiMin` / `patch.idemIntoBody` 两个开关是用来「**模拟修掉缺陷**」的
 * (E-01 版本头写死 1.3、E-02 幂等键只放请求头)。两处缺陷都已由前端修复,开关随之删除 ——
 * 留着它们会让用例继续绕过真实客户端行为,测不到「客户端现在到底发了什么」。
 * 取而代之的是 `patch.forceApiMin`:**只用于人为构造** 426,验证版本协商机制本身仍然有效
 * (02 §3.8:`X-QT-Api-Min` 声明的最低次版本不满足时服务端回 426)。
 */
export const BASE = process.env.AGENT_BASE_URL ?? 'http://127.0.0.1:37851'
export const WS_BASE = BASE.replace(/^http/, 'ws')

export const patch = {
  /** 非 null 时**覆盖**客户端发出的 `X-QT-Api-Min`,用来人为构造 426(不代表客户端的真实行为) */
  forceApiMin: null as string | null,
  /** 置 true 时不注 Authorization(模拟「主进程还没拿到令牌」) */
  noAuth: false,
  token: 'e2e-admin-token' as string,
  /**
   * 非 null 时在请求**发出前**改写请求体(返回新 body 字符串;返回 undefined = 不改)。
   * 🔴 只用于**造错误条件**:前端已按白名单过滤、界面发不出野键,要验「真后端回 400 ⇒ 界面把
   * `details[].pointer` 显示出来」这条链路,只能在出口处注入一个野键让**真后端**真的回 400。
   * 响应是真的,被测的是前端对真 400 的处置;用完必须置回 null。
   */
  mutateBody: null as ((url: string, method: string, body: string | null) => string | undefined) | null,
}

const realFetch = globalThis.fetch.bind(globalThis)
/** 每次真实请求的取证记录:用来断言**客户端实际发出的头/体**(如「不再发 X-Idempotency-Key」) */
export const seen: { url: string; method: string; headers: Record<string, string>; body: string | null; status: number }[] = []

export function lastSeen(pathFragment: string) {
  for (let i = seen.length - 1; i >= 0; i--) if (seen[i].url.includes(pathFragment)) return seen[i]
  return null
}

globalThis.fetch = (async (input: any, init: any = {}) => {
  let url = typeof input === 'string' ? input : String(input?.url ?? input)
  if (url.startsWith('/')) url = BASE + url
  const headers: Record<string, string> = { ...(init.headers ?? {}) }
  if (!patch.noAuth) headers['Authorization'] = `Bearer ${patch.token}`   // 主进程 webRequest 做的事
  if (patch.forceApiMin !== null) headers['X-QT-Api-Min'] = patch.forceApiMin
  const method = String(init.method ?? 'GET')
  let body = init.body
  if (patch.mutateBody) {
    const m = patch.mutateBody(url, method, typeof body === 'string' ? body : null)
    if (m !== undefined) body = m
  }
  const res = await realFetch(url, { ...init, headers, body })
  seen.push({
    url,
    method,
    headers,
    body: typeof body === 'string' ? body : null,
    status: res.status,
  })
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

/** 裸 fetch(不经客户端源码),用来取「后端原样回了什么」的证据 */
export async function raw(path: string, init: RequestInit & { token?: string | null } = {}) {
  const headers: Record<string, string> = { ...(init.headers as Record<string, string> ?? {}) }
  const tk = init.token === undefined ? patch.token : init.token
  if (tk) headers['Authorization'] = `Bearer ${tk}`
  if (init.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json'
  const res = await realFetch(BASE + path, { ...init, headers })
  const text = await res.text()
  let body: any = null
  try { body = JSON.parse(text) } catch { body = text }
  return { status: res.status, body: body as Record<string, any>, headers: res.headers }
}

/**
 * 把企点账号 `qd01` 拉到 `running`(create → start → login → running,05 §2.1.1 的登录阶段)。
 * 两个 spec 文件都需要它作为前置:`#72 checks.accounts`、`/sessions`、`#28` 都要求有一个跑着的账号。
 * 建号走**控制台客户端**(幂等键由 http 层落进 body,02 §3.4 #2),不绕过被测代码。
 */
export async function ensureAccountRunning(accountId = 'qd01'): Promise<void> {
  const { accountsApi } = await import('@/api/client')
  const { ApiFailure } = await import('@/api/http')
  let a
  try {
    a = await accountsApi.get(accountId)
  } catch (e) {
    if (!(e instanceof ApiFailure) || e.status !== 404) throw e
    await accountsApi.create({
      channel: 'qidian', label: 'e2e-企点', login: { mode: 'password', account: 'u', secret: 'p', remember: true },
    })
    a = await accountsApi.get(accountId)
  }
  if (a.state === 'running') return
  if (!a.enabled) await accountsApi.enable(accountId)
  if (['created', 'stopped', 'error'].includes(a.state)) await accountsApi.start(accountId)
  await waitFor(async () => {
    const x = await accountsApi.get(accountId)
    if (x.state === 'login_required') {
      await accountsApi.login(accountId, { secret: 'pwd123', remember: true })
      return false
    }
    return x.state === 'running'
  }, 40000, 500)
}

/**
 * 第三轮新增:经假后端的**测试造数口子**(`serve_fake_agent.py --seed-api` 才挂)造行。
 * 翻页用例要「跨 ≥3 页 + 翻页中途插新行」,store 页大小写死 50 ⇒ 每条线 >100 行,公开 API 造不出来
 * (建号到第 4~5 个就 RESOURCE_EXHAUSTED、收发件无写入端点)。造数只调产品自己的写入函数,见该文件 docstring。
 */
export async function seed(body: {
  kind: 'accounts' | 'qidian_peers' | 'inbox' | 'outbox'
  n: number
  base_ms?: number
  step_ms?: number
  tie?: number
  tag?: string
  channel?: string
  peer_prefix?: string
}): Promise<(string | number)[]> {
  const res = await realFetch(`${BASE}/__e2e/seed`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer e2e-admin-token' },
    body: JSON.stringify(body),
  })
  if (res.status !== 200) {
    throw new Error(`造数口子不可用(HTTP ${res.status}):假后端要带 --seed-api 启动`)
  }
  return ((await res.json()) as { ids: (string | number)[] }).ids
}

/** 取证:从 `mark`(= 当时的 `seen.length`)之后客户端**实际发出**的请求 */
export function seenSince(mark: number, pathFragment?: string) {
  const out = seen.slice(mark)
  return pathFragment ? out.filter((s) => new URL(s.url).pathname.includes(pathFragment)) : out
}
