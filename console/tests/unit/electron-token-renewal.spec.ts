/**
 * 2026-10-10 真实链路:Electron 开着满 1 小时必弹「控制台令牌失效」——令牌包带 `expires_at`,但主进程没有任何续签路径。
 * 现在:取到令牌即按到期时间预约续签(80% 处、至少留 60 s),失败 30 s 重试;`qt:auth.state` 到期先续签再回答。
 * `token.ts` 只依赖 `winagent-pipe` 的类型与工厂,这里注入假管道、假时钟、假定时器,不碰 electron。
 */
import { describe, expect, it, vi } from 'vitest'
import { RENEW_MIN_LEAD_MS, RENEW_RETRY_MS, TokenHolder } from '../../electron/main/token'

function harness(lifeMs: number) {
  let now = 1_000_000
  const timers: { fn: () => void; at: number; id: number }[] = []
  let nextId = 1
  const fetchTokens = vi.fn(async () => ({ agent_token: `t${fetchTokens.mock.calls.length}`, winagent_token: 'w', expires_at: now + lifeMs }))
  const holder = new TokenHolder(
    { fetchTokens, describe: () => 'fake' },
    () => now,
    (fn, ms) => { const id = nextId++; timers.push({ fn, at: now + ms, id }); return id as unknown as ReturnType<typeof setTimeout> },
    (t) => { const i = timers.findIndex((x) => x.id === (t as unknown as number)); if (i >= 0) timers.splice(i, 1) },
  )
  /** 把时间拨到 `t`,顺序触发到期的定时器 */
  async function advanceTo(t: number) {
    while (true) {
      const due = timers.filter((x) => x.at <= t).sort((a, b) => a.at - b.at)[0]
      if (!due) break
      timers.splice(timers.indexOf(due), 1)
      now = due.at
      due.fn()
      await Promise.resolve(); await Promise.resolve()
    }
    now = t
  }
  return { holder, fetchTokens, timers, advanceTo, get now() { return now } }
}

describe('Electron 主进程令牌续签', () => {
  it('取到令牌后在到期前(80% 处)自动续签,期间 authState 一直是 ok', async () => {
    const h = harness(3_600_000)
    expect(await h.holder.refresh()).toBe(true)
    expect(h.timers).toHaveLength(1)
    const renewAt = h.timers[0].at
    expect(renewAt - h.now).toBeCloseTo(3_600_000 * 0.8, -3)
    await h.advanceTo(renewAt - 1)
    expect(h.fetchTokens).toHaveBeenCalledTimes(1)
    expect(h.holder.authState).toBe('ok')
    await h.advanceTo(renewAt + 1)
    expect(h.fetchTokens).toHaveBeenCalledTimes(2)
    expect(h.holder.authState).toBe('ok')
    expect(h.holder.agentToken).toBe('t2')
    expect(h.timers).toHaveLength(1)                       // 新一轮续签已预约
    await h.advanceTo(h.now + 3_600_000 * 0.79)
    expect(h.holder.authState).toBe('ok')                  // 原本在这里会变 no_token
  })

  it('短命令牌至少留 60 s 提前量', async () => {
    const h = harness(120_000)
    await h.holder.refresh()
    expect(h.timers[0].at - h.now).toBe(120_000 - RENEW_MIN_LEAD_MS)
  })

  it('续签失败 30 s 后重试;重试成功恢复 ok', async () => {
    const h = harness(600_000)
    await h.holder.refresh()
    h.fetchTokens.mockRejectedValueOnce(new Error('pipe down'))
    const renewAt = h.timers[0].at
    await h.advanceTo(renewAt)
    expect(h.holder.authState).toBe('winagent_offline')
    expect(h.timers).toHaveLength(1)
    expect(h.timers[0].at - renewAt).toBe(RENEW_RETRY_MS)
    await h.advanceTo(h.timers[0].at + 1)
    expect(h.holder.authState).toBe('ok')
    expect(h.fetchTokens).toHaveBeenCalledTimes(3)
  })

  it('ensureFresh:已过期先续签再回答,不让渲染进程先看到 no_token', async () => {
    const h = harness(1_000)
    await h.holder.refresh()
    h.timers.splice(0)                                     // 模拟定时器没赶上(机器睡眠等)
    await h.advanceTo(h.now + 5_000)
    expect(h.holder.authState).toBe('no_token')
    expect(await h.holder.ensureFresh()).toBe('ok')
    expect(h.fetchTokens).toHaveBeenCalledTimes(2)
  })

  it('并发 refresh 合并成一次管道往返;clear 取消预约', async () => {
    const h = harness(600_000)
    await Promise.all([h.holder.refresh(), h.holder.refresh(), h.holder.refresh()])
    expect(h.fetchTokens).toHaveBeenCalledTimes(1)
    h.holder.clear()
    expect(h.timers).toHaveLength(0)
    expect(h.holder.authState).toBe('no_token')
  })
})
