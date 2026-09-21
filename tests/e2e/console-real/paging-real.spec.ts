/**
 * 第三轮(独立端到端复测)新增:**控制台 store 的 C-42 游标翻页** ↔ 真 Agent(全假后端)。
 *
 * 为什么在 store 层测:console-fix-3 §8 指出 N-2 的真正断点不在 `requestList()`(它早就把 `next_cursor` 取出来了),
 * 而在「调用方存不存游标、续页时带不带、是追加还是覆盖」。所以本文件直接驱动四个 store,
 * 用 harness 的 `seen` 断言**客户端实际发出**的 `cursor`/`limit`,再拿后端全量快照对「不重不漏」。
 *
 * 规格出处:
 * - 02 §3.4 通用段 **C-42**:「`?since&until&limit=50&cursor` → `{ok:true, data:[…], next_cursor}`」;
 *   **G-16**:「cursor = `base64url(JSON{ts_ms,id})`,服务端按 `(ts_ms, id)` 双键定位(**不用 OFFSET,
 *   列表在翻页期间有新行插入也不漂**),客户端**只能透传不能构造**」;02 #48「`next_cursor` 仅在本页满 `limit` 时非空」。
 * - 总控已登记口径(e2e-recheck-2 派工 ④):非法 `limit` → 422 `INVALID_ARGS` + `details[].pointer=/limit`;
 *   乱码 `cursor` → 400 `bad_cursor`(02 §3.4 通用段:「服务端能识别老 cursor 并回 `400 INVALID_ARGS`」)。
 * - 总控已登记口径 ⑤ + R6-58 (ac):`GET /mail/hmac-keys` 行 = 六键 `{short_name, sender, route_id, enabled, secret_ref, created_at}`,
 *   响应体零密钥明文(明文只在 02 #67 那一次)。
 * - 00 §6:API/事件/**邮件**时间一律 ISO 8601 带偏移;02 #58:「`mail_inbox` 行(**不含 `body_text`**,详情才给)」。
 *
 * 造数:store 页大小写死 50(C-42 字面),跨 ≥3 页要 >100 行,经公开 API 造不出来 ⇒ 用假后端的测试造数口子
 * (`serve_fake_agent.py --seed-api`,只调产品自己的写入函数;会话/消息走**真 ingest**)。
 */
import { describe, it, expect, beforeAll } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { patch, raw, seed, seen, seenSince, waitFor, ensureAccountRunning } from './harness'
import { accountsApi, mailApi } from '@/api/client'
import { useAccountsStore } from '@/stores/accounts'
import { useMessagesStore } from '@/stores/messages'
import { useMailStore } from '@/stores/mail'
import { useSettingsStore } from '@/stores/settings'

const ACC = 'qd01'
const PAGE = 50                 // 各 store 的 PAGE_LIMIT(C-42 通用段字面 `limit=50`)
const DAY = 24 * 3600 * 1000

/** 后端全量快照(不经客户端):用来对「不重不漏」 */
async function snapshot(path: string, key = 'id'): Promise<string[]> {
  const out: string[] = []
  let cursor: string | null = null
  for (let i = 0; i < 50; i++) {
    const q = cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''
    const r = await raw(`${path}${path.includes('?') ? '&' : '?'}limit=500${q}`)
    expect(r.status, `快照 ${path} 取不到`).toBe(200)
    for (const row of r.body.data as Record<string, unknown>[]) out.push(String(row[key]))
    cursor = r.body.next_cursor ?? null
    if (!cursor) break
  }
  return out
}

/** 取某次调用实际发出的查询参数 */
function query(s: { url: string }): URLSearchParams {
  return new URL(s.url).searchParams
}

function dupes(ids: string[]): string[] {
  const seenIds = new Set<string>()
  const d: string[] = []
  for (const x of ids) { if (seenIds.has(x)) d.push(x); seenIds.add(x) }
  return d
}

beforeAll(async () => {
  patch.token = 'e2e-admin-token'
  patch.noAuth = false
  patch.forceApiMin = null
  setActivePinia(createPinia())
  // 造数口子必须在:没有它就造不出 >100 行,本文件整体无从验起(不是放松断言,是前置)
  await seed({ kind: 'accounts', n: 0 })
}, 60000)

/* ══════════════ ① accounts:load(true) 带游标且追加,不重不漏 ══════════════ */

describe('① accountsStore:load(true) 带上游标续页并追加;翻页中途插新行不重不漏(C-42 / G-16)', () => {
  let expected: string[] = []
  const inserted: { head: string; mid: string[]; tail: string[] } = { head: '', mid: [], tail: [] }
  const base = Date.now() - 10 * DAY

  beforeAll(async () => {
    await ensureAccountRunning(ACC)        // 先让真 qd01 占住 01 号,造数号从后面排
    // 120 行 stopped 的号(每通道序号只有 01–98,00 §3,故企点/QQ 各 60),created_ms 从 base 往前每组 1s,
    // **每 3 行共用同一毫秒**(验 G-16 双键在排序列撞值时的定位)
    await seed({ kind: 'accounts', channel: 'qidian', n: 60, base_ms: base, step_ms: 1000, tie: 3, tag: 'pg-acct-qd' })
    await seed({ kind: 'accounts', channel: 'qq', n: 60, base_ms: base - 500, step_ms: 1000, tie: 3, tag: 'pg-acct-qq' })
    expected = await snapshot('/api/v1/accounts')
    expect(expected.length, '造数后账号总数应 > 2 页').toBeGreaterThan(2 * PAGE)
  }, 60000)

  it('load() 首页:不带 cursor、显式 limit=50,拿到 50 行且 nextCursor 非空(满页才给)', async () => {
    const st = useAccountsStore()
    const mark = seen.length
    await st.load()
    const req = seenSince(mark, '/accounts')
    expect(req.length).toBe(1)
    expect(query(req[0]).get('limit')).toBe(String(PAGE))
    expect(query(req[0]).has('cursor'), '首页不该带 cursor').toBe(false)
    expect(st.error).toBeNull()
    expect(st.items.length).toBe(PAGE)
    expect(st.nextCursor, '满页却没给 next_cursor(或 store 没存)').toBeTruthy()
    // 首页 = 后端快照的前 50 行(同一排序,created_ms 降序 + id 降序)
    expect(st.items.map((a) => a.id)).toEqual(expected.slice(0, PAGE))
  })

  it('load(true) 逐页续翻:每次都带上一页的 nextCursor 原样透传;追加不覆盖;中途插入的行不重不漏;跨 ≥3 页', async () => {
    const st = useAccountsStore()
    // 翻页中途插新行 ①:一个经**真 API** 建的号(最新 ⇒ 落在已翻过的头部,不该在后续页里冒出来造成重复)
    const created = await accountsApi.create({ channel: 'qq', label: 'pg-中途新建', login: { mode: 'qrcode', remember: false } })
    inserted.head = created.id
    // 翻页中途插新行 ②:时间落在**还没翻到**的区间里 ⇒ G-16 双键定位下后续页必须把它带出来(不漏)
    inserted.mid = (await seed({ kind: 'accounts', channel: 'qidian', n: 2, base_ms: base - 15_250, tie: 2, tag: 'pg-acct-mid' })).map(String)

    let pages = 1
    for (let guard = 0; st.nextCursor && guard < 20; guard++) {
      const before = st.items.map((a) => a.id)
      const cur = st.nextCursor
      const mark = seen.length
      await st.load(true)
      pages++
      const req = seenSince(mark, '/accounts')
      expect(req.length).toBe(1)
      expect(query(req[0]).get('cursor'), 'load(true) 没把上一页的 nextCursor 原样带上').toBe(cur)
      expect(query(req[0]).get('limit')).toBe(String(PAGE))
      expect(st.error).toBeNull()
      // 追加:前面已有的行一行不动
      expect(st.items.slice(0, before.length).map((a) => a.id)).toEqual(before)
      expect(st.items.length).toBeGreaterThan(before.length)
      if (pages === 2) {
        // 翻页中途插新行 ③:再插一批比现有全部都旧的 ⇒ 最后一页必须带出来
        inserted.tail = (await seed({ kind: 'accounts', channel: 'qidian', n: 3, base_ms: base - 3_600_000, tie: 1, tag: 'pg-acct-tail' })).map(String)
      }
    }
    expect(st.nextCursor, '翻到底 nextCursor 应为 null').toBeNull()
    expect(pages, '造了 >100 行,至少应翻 3 页').toBeGreaterThanOrEqual(3)

    const got = st.items.map((a) => a.id)
    expect(dupes(got), '翻页结果出现重复行').toEqual([])
    const missing = expected.filter((id) => !got.includes(id))
    expect(missing, '翻页开始前就存在的行被漏掉了').toEqual([])
    for (const id of [...inserted.mid, ...inserted.tail]) {
      expect(got, `翻页期间插进「尚未翻到区间」的行 ${id} 被漏掉(G-16 不漂)`).toContain(id)
    }
    expect(got.filter((id) => id === inserted.head).length, '中途新建的最新行重复出现').toBeLessThanOrEqual(1)
    // 全程 created_at 非增(后端 created_ms 降序;store 不再自己排)
    const ts = st.items.map((a) => Date.parse(String((a as unknown as Record<string, unknown>).created_at)))
    expect(ts.every((t, i) => i === 0 || t <= ts[i - 1]), '续页之间顺序被打乱').toBe(true)
  }, 120000)

  it('load()(不带 more)= 从头拉并覆盖:不带 cursor、行数回到一页、最新建的号在最前', async () => {
    const st = useAccountsStore()
    const mark = seen.length
    await st.load()
    const req = seenSince(mark, '/accounts')
    expect(query(req[0]).has('cursor'), '从头拉却带了旧游标').toBe(false)
    expect(st.items.length, 'load() 应覆盖而不是追加').toBe(PAGE)
    expect(st.items[0].id).toBe(inserted.head)
    expect(st.nextCursor).toBeTruthy()
  })
})

describe('建号序号用尽(00 §3「NN 01–98」、02 #2「永不复用」)⇒ 仍走 00 §10 信封,不是裸 500', () => {
  it('企点序号用尽后再经 API 建企点号 ⇒ §10 错误信封(RESOURCE_EXHAUSTED),不是 text/plain 500', async () => {
    /* 00 §3:账号序号 `NN` 取 **01–98**;02 #2:`seq` 一个事务里分配、**永不复用** ⇒ 一个通道累计建满 98 个
       (含已删的)之后就再也建不了。这是可预见的业务边界,按 00 §10 应回错误信封(资源类 = 409 `RESOURCE_EXHAUSTED`),
       而不是未捕获异常漏成 `500 Internal Server Error`(text/plain,控制台只能显示兜底文案,且 02 §3.4 说 500 会触发自动重试)。 */
    const all = await snapshot('/api/v1/accounts?include_deleted=true')
    const maxSeq = Math.max(0, ...all.filter((id) => /^qd\d+$/.test(id)).map((id) => Number(id.slice(2))))
    if (maxSeq < 98) await seed({ kind: 'accounts', channel: 'qidian', n: 98 - maxSeq, base_ms: Date.now() - 30 * DAY, tag: 'pg-exhaust' })
    const r = await raw('/api/v1/accounts', {
      method: 'POST',
      body: JSON.stringify({ channel: 'qidian', label: 'pg-序号用尽', login: { mode: 'password', account: 'u', secret: 'p' },
        idempotency_key: `pg-exhaust-${Date.now()}` }),
    })
    expect(r.status, `序号用尽时回了 HTTP ${r.status}:${typeof r.body === 'string' ? r.body : JSON.stringify(r.body)}`).not.toBe(500)
    expect(r.body.ok).toBe(false)
    expect(r.body.code).toBe('RESOURCE_EXHAUSTED')
    expect(typeof r.body.trace_id).toBe('string')
  }, 60000)
})

/* ══════════════ ② messages:会话与消息两条独立游标线 ══════════════ */

describe('② messagesStore:会话列表(sessionsCursor)与消息列表(nextCursor)是两条独立游标线', () => {
  let sessExpected: string[] = []
  let msgExpected: string[] = []

  beforeAll(async () => {
    await ensureAccountRunning(ACC)
    /* 前置(不是放松断言):企点读库的游标 bootstrap 发生在账号 running 之后的**下一轮 poll**,bootstrap 那一刻已存在的
       会话表按设计一律当历史、不回灌(R6-39)。账号刚 running 就造数会被当历史吃掉 ⇒ 先每 3 s 放一个探针对端,
       直到有一个真被 ingest 成会话(= bootstrap 已完成、增量在跑),再造 120 行。 */
    await waitFor(async () => {
      const [probe] = await seed({ kind: 'qidian_peers', n: 1, tag: 'pg-probe', peer_prefix: `93${Date.now() % 100000}` })
      for (let i = 0; i < 6; i++) {
        const ids = await snapshot(`/api/v1/sessions?account_id=${ACC}`)
        if (ids.some((x) => x.endsWith(`:${probe}`))) return true
        await new Promise((r) => setTimeout(r, 1000))
      }
      return null
    }, 60000, 0)
    // 120 个不同对端各一条入向消息 → 产品自己的企点读库 poll 真 ingest ⇒ 120 条会话 + 120 条消息
    const peers = await seed({ kind: 'qidian_peers', n: 120, step_ms: 0, tag: 'pg-sess', peer_prefix: '9100' })
    await waitFor(async () => {
      const ids = await snapshot(`/api/v1/sessions?account_id=${ACC}`)
      return ids.filter((x) => peers.some((p) => x.endsWith(`:${p}`))).length >= peers.length ? ids : null
    }, 60000, 1000)
    sessExpected = await snapshot(`/api/v1/sessions?account_id=${ACC}`)
    msgExpected = await snapshot(`/api/v1/messages?account_id=${ACC}`)
    expect(sessExpected.length).toBeGreaterThan(2 * PAGE)
    expect(msgExpected.length).toBeGreaterThan(2 * PAGE)
  }, 180000)

  it('交替续页:/sessions 只带 sessionsCursor、/messages 只带 nextCursor,互不串线、互不覆盖', async () => {
    const st = useMessagesStore()
    st.filter = { limit: PAGE, account_id: ACC }
    st.selectedSessionId = null

    await st.loadSessions()
    await st.search()
    expect(st.sessions.length).toBe(PAGE)
    expect(st.items.length).toBe(PAGE)
    const s1 = st.sessionsCursor
    const m1 = st.nextCursor
    expect(s1, '会话首页满页却没存游标').toBeTruthy()
    expect(m1, '消息首页满页却没存游标').toBeTruthy()
    expect(s1).not.toBe(m1)

    // 续会话页:只动会话线
    let mark = seen.length
    await st.loadSessions(true)
    let req = seenSince(mark)
    expect(req.length).toBe(1)
    expect(new URL(req[0].url).pathname).toMatch(/\/sessions$/)
    expect(query(req[0]).get('cursor'), '会话续页带错了游标').toBe(s1)
    expect(st.nextCursor, '续会话页把消息线的游标改了').toBe(m1)
    expect(st.items.length, '续会话页动了消息列表').toBe(PAGE)
    const s2 = st.sessionsCursor

    // 续消息页:只动消息线
    mark = seen.length
    await st.search(false)
    req = seenSince(mark)
    expect(req.length).toBe(1)
    expect(new URL(req[0].url).pathname).toMatch(/\/messages$/)
    expect(query(req[0]).get('cursor'), '消息续页带错了游标').toBe(m1)
    expect(query(req[0]).get('cursor')).not.toBe(s2)
    expect(st.sessionsCursor, '续消息页把会话线的游标改了').toBe(s2)
    expect(st.sessions.length, '续消息页动了会话列表').toBe(2 * PAGE)
  }, 60000)

  it('会话线翻到底:跨 ≥3 页,不重不漏(翻页中途新进会话也不重复)', async () => {
    const st = useMessagesStore()
    // 中途进一个新对端(最新 ⇒ 头部;不该在后续页里重复出现)
    const fresh = await seed({ kind: 'qidian_peers', n: 1, tag: 'pg-sess-new', peer_prefix: '9200' })
    let pages = 2
    for (let guard = 0; st.sessionsCursor && guard < 20; guard++) {
      const cur = st.sessionsCursor
      const mark = seen.length
      await st.loadSessions(true)
      pages++
      expect(query(seenSince(mark, '/sessions')[0]).get('cursor')).toBe(cur)
    }
    expect(pages).toBeGreaterThanOrEqual(3)
    const got = st.sessions.map((s) => s.id)
    expect(dupes(got), '会话翻页出现重复').toEqual([])
    expect(sessExpected.filter((id) => !got.includes(id)), '会话翻页漏行').toEqual([])
    expect(got.filter((id) => id.endsWith(`:${fresh[0]}`)).length).toBeLessThanOrEqual(1)
  }, 90000)

  it('消息线翻到底:跨 ≥3 页,不重不漏', async () => {
    const st = useMessagesStore()
    let pages = 2
    for (let guard = 0; st.nextCursor && guard < 20; guard++) {
      const cur = st.nextCursor
      const mark = seen.length
      await st.search(false)
      pages++
      expect(query(seenSince(mark, '/messages')[0]).get('cursor')).toBe(cur)
    }
    expect(pages).toBeGreaterThanOrEqual(3)
    const got = st.items.map((m) => m.id)
    expect(dupes(got), '消息翻页出现重复').toEqual([])
    expect(msgExpected.filter((id) => !got.includes(id)), '消息翻页漏行').toEqual([])
  }, 90000)
})

/* ══════════════ ③ mail:收件与发件游标不串 ══════════════ */

describe('③ mailStore:reloadInbox(true) / reloadOutbox(true) 各自续页,游标不串;不重不漏', () => {
  let inboxExpected: string[] = []
  let outboxExpected: string[] = []
  const base = Date.now() - 5 * DAY
  let inMid: string[] = []
  let outMid: string[] = []

  beforeAll(async () => {
    await seed({ kind: 'inbox', n: 125, base_ms: base, step_ms: 1000, tie: 2, tag: 'pg-in' })
    await seed({ kind: 'outbox', n: 125, base_ms: base - 500, step_ms: 1000, tie: 2, tag: 'pg-out' })
    inboxExpected = await snapshot('/api/v1/mail/inbox')
    outboxExpected = await snapshot('/api/v1/mail/outbox')
    expect(inboxExpected.length).toBeGreaterThan(2 * PAGE)
    expect(outboxExpected.length).toBeGreaterThan(2 * PAGE)
  }, 60000)

  it('reloadAll() 首屏:收/发件各拿一页、各存各的游标', async () => {
    const st = useMailStore()
    const mark = seen.length
    await st.reloadAll()
    expect(st.error).toBeNull()
    const inReq = seenSince(mark, '/mail/inbox')
    const outReq = seenSince(mark, '/mail/outbox')
    expect(query(inReq[0]).get('limit')).toBe(String(PAGE))
    expect(query(inReq[0]).has('cursor')).toBe(false)
    expect(query(outReq[0]).get('limit')).toBe(String(PAGE))
    expect(query(outReq[0]).has('cursor')).toBe(false)
    expect(st.inbox.length).toBe(PAGE)
    expect(st.outbox.length).toBe(PAGE)
    expect(st.inboxCursor).toBeTruthy()
    expect(st.outboxCursor).toBeTruthy()
    expect(st.inboxCursor).not.toBe(st.outboxCursor)
  }, 60000)

  it('交替续页到底:收件请求只带 inboxCursor、发件请求只带 outboxCursor;中途插行不重不漏;各跨 ≥3 页', async () => {
    const st = useMailStore()
    let inPages = 1
    let outPages = 1
    for (let guard = 0; (st.inboxCursor || st.outboxCursor) && guard < 20; guard++) {
      if (st.inboxCursor) {
        const ic = st.inboxCursor
        const oc = st.outboxCursor
        const before = st.inbox.length
        const mark = seen.length
        await st.reloadInbox(true)
        inPages++
        const req = seenSince(mark)
        expect(req.length).toBe(1)
        expect(new URL(req[0].url).pathname).toMatch(/\/mail\/inbox$/)
        expect(query(req[0]).get('cursor'), '收件续页带错游标').toBe(ic)
        expect(st.outboxCursor, '续收件页把发件游标改了').toBe(oc)
        expect(st.inbox.length).toBeGreaterThan(before)
      }
      if (st.outboxCursor) {
        const ic = st.inboxCursor
        const oc = st.outboxCursor
        const before = st.outbox.length
        const mark = seen.length
        await st.reloadOutbox(true)
        outPages++
        const req = seenSince(mark)
        expect(req.length).toBe(1)
        expect(new URL(req[0].url).pathname).toMatch(/\/mail\/outbox$/)
        expect(query(req[0]).get('cursor'), '发件续页带错游标').toBe(oc)
        expect(st.inboxCursor, '续发件页把收件游标改了').toBe(ic)
        expect(st.outbox.length).toBeGreaterThan(before)
      }
      if (guard === 0) {
        // 翻页中途插行:落在两条线都还没翻到的区间里(G-16:后续页必须带出来)
        inMid = (await seed({ kind: 'inbox', n: 2, base_ms: base - 90_250, tie: 2, tag: 'pg-in-mid' })).map(String)
        outMid = (await seed({ kind: 'outbox', n: 2, base_ms: base - 90_750, tie: 2, tag: 'pg-out-mid' })).map(String)
        // 以及两条最新的(头部,已翻过;不该重复冒出)
        await seed({ kind: 'inbox', n: 1, tag: 'pg-in-new' })
        await seed({ kind: 'outbox', n: 1, tag: 'pg-out-new' })
      }
    }
    expect(inPages).toBeGreaterThanOrEqual(3)
    expect(outPages).toBeGreaterThanOrEqual(3)
    const gotIn = st.inbox.map((r) => String(r.id))
    const gotOut = st.outbox.map((r) => String(r.id))
    expect(dupes(gotIn), '收件翻页重复').toEqual([])
    expect(dupes(gotOut), '发件翻页重复').toEqual([])
    expect(inboxExpected.filter((id) => !gotIn.includes(id)), '收件漏行').toEqual([])
    expect(outboxExpected.filter((id) => !gotOut.includes(id)), '发件漏行').toEqual([])
    for (const id of inMid) expect(gotIn, `收件中途插入的 ${id} 被漏掉`).toContain(id)
    for (const id of outMid) expect(gotOut, `发件中途插入的 ${id} 被漏掉`).toContain(id)
  }, 120000)

  it('#58 列表行不含 body_text(02 #58 逐字「不含 body_text,详情才给」)', async () => {
    const r = await raw('/api/v1/mail/inbox?limit=3')
    expect(r.status).toBe(200)
    const row = (r.body.data as Record<string, unknown>[])[0]
    expect(row, '造数后应至少有一行').toBeTruthy()
    expect('body_text' in row, '#58 列表行带出了 body_text 键').toBe(false)
  })

  it('#58/#61 列表行的时间是 ISO 8601 带偏移(00 §6「时间(API/事件/邮件)ISO 8601」),不透出 *_ms 库列', async () => {
    /* 00 §6 表逐字:「时间(API/事件/邮件)| ISO 8601 带时区偏移」;R6-62 (f) 登记的 `_ms` 例外只有 Account 的
       `error_since_ms`/`deleted_ms` 两个键,「新增时间键一律 ISO + `*_at`」。 */
    const leaks: string[] = []
    for (const path of ['/api/v1/mail/inbox?limit=2', '/api/v1/mail/outbox?limit=2']) {
      const row = ((await raw(path)).body.data as Record<string, unknown>[])[0]
      for (const k of Object.keys(row)) if (/_ms$/.test(k)) leaks.push(`${path.split('?')[0]}.${k}`)
    }
    expect(leaks, `邮件列表行把库列毫秒时间原样透出:${leaks.join('、')}`).toEqual([])
  })
})

/* ══════════════ 入参:非法 limit / 乱码 cursor(四个铺开 C-42 的端点) ══════════════ */

describe('C-42 入参校验:非法 limit ⇒ 422 INVALID_ARGS(/limit);乱码 cursor ⇒ 400 bad_cursor', () => {
  const PATHS = ['/api/v1/accounts', '/api/v1/sessions', '/api/v1/mail/inbox', '/api/v1/mail/outbox']

  for (const path of PATHS) {
    for (const bad of ['0', '-1', '501', 'abc']) {
      it(`${path}?limit=${bad} ⇒ 422,信封 INVALID_ARGS,details[].pointer=/limit(不静默忽略)`, async () => {
        const r = await raw(`${path}?limit=${bad}`)
        expect(r.status).toBe(422)
        expect(r.body.ok).toBe(false)
        expect(r.body.code).toBe('INVALID_ARGS')
        expect(typeof r.body.trace_id).toBe('string')
        const ptrs = (r.body.error?.details ?? []).map((d: Record<string, unknown>) => d.pointer)
        expect(ptrs).toContain('/limit')
      })
    }
    it(`${path}?cursor=<乱码> ⇒ 400 INVALID_ARGS / bad_cursor(客户端只能透传,格式不认就让它从头拉)`, async () => {
      const r = await raw(`${path}?cursor=%%%not-a-cursor`)
      expect(r.status).toBe(400)
      expect(r.body.code).toBe('INVALID_ARGS')
      expect(r.body.error.reason).toBe('bad_cursor')
      expect(r.body.error.details?.[0]?.pointer).toBe('/cursor')
    })
    it(`${path}?limit=1 真截断且满页给 next_cursor(limit 不被静默忽略)`, async () => {
      const r = await raw(`${path}?limit=1`)
      expect(r.status).toBe(200)
      expect(r.body.data.length).toBe(1)
      expect(typeof r.body.next_cursor).toBe('string')
    })
  }
})

/* ══════════════ ⑤ GET /mail/hmac-keys:六键,零密钥 ══════════════ */

describe('⑤ GET /mail/hmac-keys(暂记 #67b):行六键,响应体里一个字节的密钥都没有', () => {
  const SHORT = `e2epg${Date.now() % 100000}`
  let secret = ''

  beforeAll(async () => {
    const r = await mailApi.createHmacKey('pg-ops@corp', SHORT)
    secret = r.secret ?? ''
    expect(secret.length, '#67 应一次性下发明文(否则下面「零密钥」无从验起)').toBeGreaterThan(8)
  })

  it('原始响应体里找不到那把明文(也找不到它的任何 16 字符片段)', async () => {
    const res = await fetch('/api/v1/mail/hmac-keys')
    const text = await res.text()
    expect(res.status).toBe(200)
    expect(text.includes(secret), 'GET /mail/hmac-keys 回显了密钥明文').toBe(false)
    for (let i = 0; i + 16 <= secret.length; i += 8) {
      expect(text.includes(secret.slice(i, i + 16)), '响应体里出现了密钥片段').toBe(false)
    }
  })

  it('行 = 恰六键 {short_name, sender, route_id, enabled, secret_ref, created_at};secret_ref 是引用不是值', async () => {
    const r = await raw('/api/v1/mail/hmac-keys')
    const row = (r.body.data as Record<string, unknown>[]).find((x) => x.short_name === SHORT)
    expect(row, '新建的短名不在表里').toBeTruthy()
    expect(Object.keys(row!).sort()).toEqual(['created_at', 'enabled', 'route_id', 'secret_ref', 'sender', 'short_name'])
    expect(row!.sender).toBe('pg-ops@corp')
    expect(row!.enabled).toBe(true)
    expect(String(row!.secret_ref)).toMatch(/^vault:\/\//)
    expect(String(row!.created_at)).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$/)
  })

  it('settingsStore.loadHmacKeys() 取到这一行;store 状态里同样没有明文', async () => {
    const st = useSettingsStore()
    await st.loadHmacKeys()
    const row = st.hmacKeys.find((x) => x.short_name === SHORT)
    expect(row).toBeTruthy()
    expect(JSON.stringify(st.hmacKeys).includes(secret)).toBe(false)
  })

  it('#68 吊销后从表里消失(吊销 = 删条目)', async () => {
    await mailApi.revokeHmacKey(SHORT)
    const r = await raw('/api/v1/mail/hmac-keys')
    expect((r.body.data as Record<string, unknown>[]).some((x) => x.short_name === SHORT)).toBe(false)
  })
})

/* ══════════════ ④ settings:applySaved 回填本次提交值,保存后不回头 GET ══════════════ */

describe('④ settingsStore.applySaved:回填本次提交的值 + 标「待重启生效」;保存之后不再发 GET /settings/{g}', () => {
  it('PUT → restart_required:true → applySaved 后 groups[g] = 本次提交值、isPendingRestart 为真;其后零次 GET', async () => {
    /* 02 #89 R6-58 (ad):v1「可热加载键」= 空集,除 resources 外一律 `restart_required:true`;
       GET 读的是当前生效值 ⇒ 保存后回头 GET 会把表单洗回旧值(总控口径:用本次提交值回填 + 标「重启后生效」)。
       本条驱动的是 SetPage.saveGroup() 的同一调用序列(settingsApi.put → store.applySaved),页面级另见 set-page-real.spec.ts。 */
    const st = useSettingsStore()
    await st.loadGroup('retention')
    const cur = { ...(st.groups.retention as Record<string, unknown>) }
    const body = { ...cur, messages_days: cur.messages_days === 11 ? 12 : 11 }
    const mark = seen.length
    const r = await (await import('@/api/client')).settingsApi.put('retention', body)
    expect(r.restartRequired, '#89 retention 组应回 restart_required:true(R6-58 (ad))').toBe(true)
    st.applySaved('retention', body, r.restartRequired)
    expect(st.groups.retention).toEqual(body)
    expect(st.isPendingRestart('retention')).toBe(true)
    const after = seenSince(mark).map((s) => `${s.method} ${new URL(s.url).pathname}`)
    expect(after).toEqual(['PUT /api/v1/settings/retention'])
  })

  it('restart_required:false 的组(resources)不标待重启', async () => {
    const st = useSettingsStore()
    st.applySaved('resources', { x: 1 }, false)
    expect(st.isPendingRestart('resources')).toBe(false)
  })
})
