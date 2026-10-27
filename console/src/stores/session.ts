/**
 * `session` store:主进程令牌状态与门禁(01 §2.3 / §5.2 / §5.3 / §5.4)。
 * 渲染进程**拿不到任何令牌**;这里只反映 `qt.auth.state()` 的三值与降级面。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { configureHttp } from '@/api/http'
import { systemApi } from '@/api/client'
import type { RecoveryStatus } from '../../electron/main/winagent-recovery'

export type AuthState = 'ok' | 'winagent_offline' | 'no_token'

export const useSessionStore = defineStore('session', () => {
  const authState = ref<AuthState>('ok')
  /** Agent 17600 是否可达(§5.4) */
  const agentReachable = ref(true)
  const agentDownReason = ref<string | null>(null)
  /** WinAgent 服务 / 用户会话代理(§5.3) */
  const winagentOnline = ref(true)
  const userAgentOnline = ref<boolean | null>(true)
  const apiVersion = ref('')
  const appVersion = ref<{ console: string; electron: string } | null>(null)
  const localStatus = ref<RecoveryStatus | null>(null)
  const localServiceBusy = ref(false)
  const localServiceMessage = ref('')
  let localServiceStartVerified = false
  const canStartLocalService = computed(() => !!window.qt?.localService)
  const localStateText = (state: string | undefined) => state === 'online' ? '在线' : state === 'offline' ? '离线' : '未知'
  const serviceStateText = computed(() => localStateText(localStatus.value?.service))
  const sessionStateText = computed(() => localStateText(localStatus.value?.session))
  const apiStateText = computed(() => localStateText(localStatus.value?.api))
  let localQuery: Promise<void> | null = null
  function refreshLocalStatus(): Promise<void> {
    if (localQuery) return localQuery
    localQuery = (async () => {
      if (!window.qt?.localService) { localStatus.value = null; return }
      try { localStatus.value = await window.qt.localService.status() }
      catch { localStatus.value = { service: 'unknown', session: 'unknown', api: 'unknown', code: 'QUERY_FAILED' } }
    })().finally(() => { localQuery = null })
    return localQuery
  }
  async function startLocalService(): Promise<void> {
    if (localServiceBusy.value || !window.qt?.localService) return
    localServiceBusy.value = true
    localServiceStartVerified = false
    localServiceMessage.value = '正在启动 WinAgent；需要时 Windows 会请求管理员确认。'
    try {
      const result = await window.qt.localService.start()
      localStatus.value = result.status
      localServiceStartVerified = result.ok
      const failure: Record<string, string> = {
        UAC_CANCELLED: '已取消管理员确认，未完成启动。',
        SERVICE_NOT_INSTALLED: '未找到已安装的 QTradeWinAgent 服务。',
        IDENTITY_MISMATCH: '已安装服务身份校验失败，未执行启动。',
        SESSION_IDENTITY_MISMATCH: '当前 Windows 用户与服务安装用户不一致，未启动会话代理。',
        STARTING: '启动请求已执行，但服务或会话代理尚未核验就绪，请检查状态。',
      }
      localServiceMessage.value = result.ok
        ? 'WinAgent 服务与会话代理已完成启动核验。'
        : failure[result.code] ?? `启动未完成（${result.code}），请检查服务与安装状态。`
      if (result.ok) { await window.qt.auth.refresh(); await refreshAuth(); await pingAgent() }
    } catch { localServiceStartVerified = false; localServiceMessage.value = '启动操作失败，尚未确认服务已启动。' }
    finally { localServiceBusy.value = false }
  }
  /**
   * 版本协商失败(02 §3.8:`X-QT-Api-Min` 不被满足 ⇒ `426 UPGRADE_REQUIRED`)。
   * 一旦出现,任何请求都发不出去 —— 必须给一句能照着做的话,不能只弹「请求失败」。
   */
  const upgradeRequired = ref<{ needMin: string; serverVersion: string | null; message: string } | null>(null)
  /**
   * #82 drain:Agent 已停止受理新指令,**所有写操作会 `503` `reason=draining`**。
   * 🔴 这是**升级流程的预期状态**,不是后端故障 —— 提示语必须写「正在为升级排空」。
   * 后端没有 undrain 端点(backend-api-2 §7-3),只能靠重启 Agent 恢复;
   * 所以这里不本地拦请求,只驱动横幅与写按钮的禁用态,写操作一旦又成功就自动解除。
   */
  const draining = ref(false)

  /** 门禁覆盖层可见:令牌不 ok / Agent 不可达 / 版本协商不通过(保留原路由,恢复后原地继续) */
  const gateVisible = computed(
    () => authState.value !== 'ok' || !agentReachable.value || upgradeRequired.value !== null,
  )
  const gateTitle = computed(() => {
    if (upgradeRequired.value) return 'API 版本不匹配,请用安装包整体升级'
    if (authState.value === 'winagent_offline') return '本机服务尚未就绪'
    if (authState.value === 'no_token') return '机器人采集终端令牌失效,请在 WinAgent 中重新签发'
    if (!agentReachable.value) return '采集 API 暂时不可达'
    return ''
  })
  /** 门禁正文第二行:426 时写清「控制台要什么、Agent 是什么」 */
  const gateDetail = computed(() => {
    const u = upgradeRequired.value
    if (!u) return authState.value === 'winagent_offline' ? '请检查 WinAgent 服务与受管 WSL Agent。连接就绪后会自动恢复。' : ''
    return `${u.message}(机器人采集终端要求 API ≥ ${u.needMin},当前 Agent ${u.serverVersion ?? '未报告版本'})`
  })

  /** A-7:两处门禁共用的固定一句 */
  // Compatibility name used by existing pages; the message follows measured state.
  const GATE_LOGIN_HINT = computed(() => userAgentOnline.value === null
    ? '无法确认会话代理状态，请检查 Windows 服务与会话连接。'
    : userAgentOnline.value ? 'Windows 会话代理在线；采集 API 需要独立检查。'
      : 'Windows 会话代理未连接，请检查当前桌面会话。')

  /** WinAgent 离线时要降级的面(§5.3) */
  const wechatDisabled = computed(() => !winagentOnline.value || !userAgentOnline.value)
  const vaultDisabled = computed(() => !winagentOnline.value)
  const wslGroupDisabled = computed(() => !winagentOnline.value || !userAgentOnline.value)

  async function refreshAuth(): Promise<void> {
    const qt = window.qt
    if (!qt) {
      // dev:web(浏览器)下没有主进程,视为 ok
      authState.value = 'ok'
      return
    }
    authState.value = (await qt.auth.state()) as AuthState
  }

  async function retryToken(): Promise<void> {
    upgradeRequired.value = null
    await window.qt?.auth.refresh()
    await refreshAuth()
    await pingAgent()
  }

  async function pingAgent(): Promise<void> {
    try {
      const h = await systemApi.health()
      agentReachable.value = true
      if (localServiceStartVerified) {
        localServiceMessage.value = ''
        localServiceStartVerified = false
      }
      agentDownReason.value = null
      winagentOnline.value = typeof h.winagent === 'object' ? h.winagent.online : !!h.winagent
      userAgentOnline.value = typeof h.winagent === 'object' ? h.winagent.user_agent : h.user_agent ?? null
    } catch {
      agentReachable.value = false
      // §5.4 / C-32 白名单⑥:只有 Agent 不可达时才直读 WinAgent 的一句话状态
      try {
        const r = (await window.qt?.wa.invoke('wa.health', {})) as { message?: string; user_agent?: boolean } | undefined
        agentDownReason.value = r?.message ?? null
        userAgentOnline.value = r?.user_agent ?? null
        winagentOnline.value = !!r
      } catch {
        agentDownReason.value = null
        winagentOnline.value = false
        userAgentOnline.value = null
      }
    } finally {
      await refreshLocalStatus()
    }
  }

  /** 让 WinAgent 拉起 Agent(§5.4;需会话代理在线,否则 503 NOT_READY) */
  async function startWsl(): Promise<void> {
    await window.qt?.wa.invoke('wsl.start', {})
    await window.qt?.auth.refresh()
    await refreshAuth()
    await pingAgent()
  }

  let probe: ReturnType<typeof setInterval> | null = null

  function install(): void {
    configureHttp({
      onUnauthorized: async () => {
        // §5.2:主进程经管道重取令牌一次 → 自动重放原请求
        const ok = await (window.qt?.auth.refresh() ?? Promise.resolve(false))
        await refreshAuth()
        return !!ok && authState.value === 'ok'
      },
      onTokenLost: () => {
        authState.value = 'no_token'
      },
      onApiVersion: (v) => {
        apiVersion.value = v
      },
      onUpgradeRequired: (info) => {
        upgradeRequired.value = info
      },
      onDraining: (on) => {
        draining.value = on
      },
    })
    void refreshAuth()
    void pingAgent()
    window.qt?.app.version().then((v) => { appVersion.value = v }).catch(() => undefined)
    // §5.3:每 15s 探一次,恢复后自动解除降级
    if (!probe) probe = setInterval(() => { void refreshAuth(); void pingAgent() }, 15000)
  }

  function uninstall(): void {
    if (probe) clearInterval(probe)
    probe = null
  }

  return {
    authState, agentReachable, agentDownReason, winagentOnline, userAgentOnline, apiVersion, appVersion,
    localStatus, localServiceBusy, localServiceMessage, canStartLocalService,
    serviceStateText, sessionStateText, apiStateText, refreshLocalStatus, startLocalService,
    upgradeRequired, draining,
    gateVisible, gateTitle, gateDetail, GATE_LOGIN_HINT, wechatDisabled, vaultDisabled, wslGroupDisabled,
    refreshAuth, retryToken, pingAgent, startWsl, install, uninstall,
  }
})
