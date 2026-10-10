/**
 * 令牌交接(01 §2.3):经管道向 WinAgent 取令牌,**只驻主进程内存**。
 * 渲染进程永远拿不到它——`qt.auth.state()` 只回三值。
 */
import { createPipeClient, type PipeClient, type TokenBundle } from './winagent-pipe'

export type AuthState = 'ok' | 'winagent_offline' | 'no_token'

/** 到期前多久续签(到期时间的 80% 处,且至少留 60 s);续签失败后的重试间隔 */
export const RENEW_AT_FRACTION = 0.8
export const RENEW_MIN_LEAD_MS = 60_000
export const RENEW_RETRY_MS = 30_000

export class TokenHolder {
  private bundle: TokenBundle | null = null
  private state: AuthState = 'no_token'
  private readonly pipe: PipeClient
  private renewTimer: ReturnType<typeof setTimeout> | null = null
  private inflight: Promise<boolean> | null = null

  constructor(pipe: PipeClient = createPipeClient(), private readonly now: () => number = Date.now,
              private readonly schedule: (fn: () => void, ms: number) => ReturnType<typeof setTimeout> = setTimeout,
              private readonly cancel: (t: ReturnType<typeof setTimeout>) => void = clearTimeout) {
    this.pipe = pipe
  }

  get authState(): AuthState {
    if (this.bundle && this.bundle.expires_at > this.now()) return 'ok'
    return this.state === 'ok' ? 'no_token' : this.state
  }

  /** 到期后再问状态 ⇒ 先原地续签一次再回答(2026-10-10 真机:开着满 1 小时门禁必弹,此前没有任何续签路径) */
  async ensureFresh(): Promise<AuthState> {
    if (this.authState !== 'ok') await this.refresh()
    return this.authState
  }

  /**
   * 成功取到令牌后按 `expires_at` 预约续签:到期时间的 80% 处(至少留 60 s);失败 30 s 后再试。
   * 不预约 ⇒ 任何一次运行超过令牌寿命都会弹「控制台令牌失效」,生产命名管道同样带 `expires_at`。
   */
  private armRenewal(): void {
    if (this.renewTimer) this.cancel(this.renewTimer)
    this.renewTimer = null
    if (!this.bundle) return
    const life = this.bundle.expires_at - this.now()
    if (life <= 0) return
    const lead = Math.max(RENEW_MIN_LEAD_MS, life * (1 - RENEW_AT_FRACTION))
    const delay = Math.max(1_000, life - lead)
    this.renewTimer = this.schedule(() => {
      this.renewTimer = null
      void this.refresh().then((ok) => {
        if (!ok) this.renewTimer = this.schedule(() => { this.renewTimer = null; void this.refresh() }, RENEW_RETRY_MS)
      })
    }, delay)
    ;(this.renewTimer as { unref?: () => void }).unref?.()
  }

  get agentToken(): string | null {
    return this.bundle?.agent_token ?? null
  }

  get winagentToken(): string | null {
    return this.bundle?.winagent_token ?? null
  }

  describe(): string {
    return this.pipe.describe()
  }

  /** 取/重取令牌;返回是否成功。并发调用合并成一次管道往返。 */
  refresh(): Promise<boolean> {
    if (this.inflight) return this.inflight
    this.inflight = (async () => {
      try {
        this.bundle = await this.pipe.fetchTokens()
        this.state = 'ok'
        this.armRenewal()
        return true
      } catch {
        this.bundle = null
        // 管道连不上 = WinAgent 服务没起来(§5.3)
        this.state = 'winagent_offline'
        return false
      } finally {
        this.inflight = null
      }
    })()
    return this.inflight
  }

  clear(): void {
    if (this.renewTimer) this.cancel(this.renewTimer)
    this.renewTimer = null
    this.bundle = null
    this.state = 'no_token'
  }
}
