/**
 * 令牌交接(01 §2.3):经管道向 WinAgent 取令牌,**只驻主进程内存**。
 * 渲染进程永远拿不到它——`qt.auth.state()` 只回三值。
 */
import { createPipeClient, type PipeClient, type TokenBundle } from './winagent-pipe'

export type AuthState = 'ok' | 'winagent_offline' | 'no_token'

export class TokenHolder {
  private bundle: TokenBundle | null = null
  private state: AuthState = 'no_token'
  private readonly pipe: PipeClient

  constructor(pipe: PipeClient = createPipeClient()) {
    this.pipe = pipe
  }

  get authState(): AuthState {
    if (this.bundle && this.bundle.expires_at > Date.now()) return 'ok'
    return this.state === 'ok' ? 'no_token' : this.state
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

  /** 取/重取令牌;返回是否成功 */
  async refresh(): Promise<boolean> {
    try {
      this.bundle = await this.pipe.fetchTokens()
      this.state = 'ok'
      return true
    } catch {
      this.bundle = null
      // 管道连不上 = WinAgent 服务没起来(§5.3)
      this.state = 'winagent_offline'
      return false
    }
  }

  clear(): void {
    this.bundle = null
    this.state = 'no_token'
  }
}
