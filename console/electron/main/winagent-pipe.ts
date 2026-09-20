/**
 * 与 WinAgent 服务的本地命名管道(01 §2.3,基线 §9 P-1)。
 *
 * `\\.\pipe\qtrade-winagent` —— 管道能拿到对端 SID 与进程路径做**操作系统级**本地鉴权,
 * 不需要预共享秘密;签发的令牌**只驻主进程内存**,永不落盘、永不进渲染进程。
 *
 * 非 Windows(开发机)用环境变量 mock,使 `npm run dev` 能在 WSL/Linux 跑起来。
 */
import { connect, type Socket } from 'node:net'

export interface TokenBundle {
  agent_token: string
  winagent_token: string
  expires_at: number
}

export interface PipeClient {
  /** 取令牌;失败抛错(调用方据此判 winagent_offline) */
  fetchTokens(): Promise<TokenBundle>
  /** WinAgent 侧一句话健康(§5.4 唯一例外) */
  describe(): string
}

export const PIPE_PATH = '\\\\.\\pipe\\qtrade-winagent'

class NamedPipeClient implements PipeClient {
  constructor(private readonly path = PIPE_PATH, private readonly timeoutMs = 4000) {}

  describe(): string {
    return `named-pipe ${this.path}`
  }

  fetchTokens(): Promise<TokenBundle> {
    return new Promise<TokenBundle>((resolve, reject) => {
      let sock: Socket
      try {
        sock = connect(this.path)
      } catch (e) {
        reject(e)
        return
      }
      const chunks: Buffer[] = []
      const timer = setTimeout(() => {
        sock.destroy()
        reject(new Error('命名管道超时'))
      }, this.timeoutMs)

      sock.on('connect', () => sock.write(JSON.stringify({ op: 'issue_console_token' }) + '\n'))
      sock.on('data', (d) => chunks.push(d))
      sock.on('error', (e) => {
        clearTimeout(timer)
        reject(e)
      })
      sock.on('close', () => {
        clearTimeout(timer)
        try {
          const text = Buffer.concat(chunks).toString('utf8').trim()
          const obj = JSON.parse(text) as Partial<TokenBundle> & { error?: string }
          if (obj.error) throw new Error(obj.error)
          if (!obj.agent_token) throw new Error('管道未返回 agent_token')
          resolve({
            agent_token: obj.agent_token,
            winagent_token: obj.winagent_token ?? '',
            expires_at: obj.expires_at ?? Date.now() + 3600_000,
          })
        } catch (e) {
          reject(e)
        }
      })
    })
  }
}

/**
 * 开发用 mock:非 Windows 或显式设了 `QT_DEV_TOKEN` 时启用。
 * **只用于开发**——它不做任何本地鉴权,生产上 Windows 平台恒走命名管道。
 */
class MockPipeClient implements PipeClient {
  constructor(private readonly token: string) {}

  describe(): string {
    return 'dev-mock(QT_DEV_TOKEN)'
  }

  async fetchTokens(): Promise<TokenBundle> {
    if (!this.token) throw new Error('开发模式未提供 QT_DEV_TOKEN')
    return { agent_token: this.token, winagent_token: this.token, expires_at: Date.now() + 3600_000 }
  }
}

export function createPipeClient(): PipeClient {
  const devToken = process.env.QT_DEV_TOKEN
  if (process.platform !== 'win32' || devToken) {
    return new MockPipeClient(devToken ?? 'dev-token')
  }
  return new NamedPipeClient()
}
