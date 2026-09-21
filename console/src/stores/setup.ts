/**
 * `setup` store:首次向导进度;合规确认**以 Agent 为准**(01-P3)。
 *
 * 🔴 端点更正:`/settings/compliance` 在 02 #88 的 `group` 枚举里**不存在**(真后端 404)。
 * 告知文案与「勾过没有」都走 #86 `GET /system/notice`(它回 `{notice_version, text, ack_ms,
 * acked_at, acked_version}`),勾选走 #87 `POST /system/notice/ack {notice_version}`。
 * 判据仍**以 Agent 为准**:`acked_version === notice_version` 才算这一版勾过(05 §6.1)。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { systemApi } from '@/api/client'

/** 控制台自己固定追加的两段(不依赖法务改稿):A-7 + A-5 */
export const NOTICE_FIXED_PARAGRAPHS = [
  '用户未登录 Windows 时服务不会自动拉起(WSL/Agent/微信会话代理都随 Windows 登录后才起),人工登录 Windows 即可。',
  '升级不会清除登录信息与数据(账号、登录态、消息库、保险库、微信档案全部保留)。',
]

/**
 * 「向导已完成」的真值落点 = `console.toml [setup] done`(01 §2.7.1 首句「只在 `[setup] done=false`
 * 时进入;完成后不再出现」+ 01 §7 配置表)。Electron 里经 `window.qt.config` 读写(主进程原子写盘)。
 *
 * 🔴 浏览器(`dev:web`、无 preload)里 `window.qt` 不存在 ⇒ `config.read()` 读不到、`config.patch()`
 * 写不进 ⇒ 完成向导后一刷新又从向导进(安琳实测)。规格里**没有**「首启完成」的后端字段
 * (#86 只管告知版本与 ack),所以此处用本机 `localStorage` 兜底镜像:
 * **有 `window.qt` 时一律以它为准**(规格口径),无 `window.qt` 时才用镜像;读写全包 try/catch
 * (隐私模式 / 站点数据被禁时 `localStorage` 会抛)。
 */
const DONE_MIRROR_KEY = 'qt.setup.done'

function readDoneMirror(): boolean {
  try {
    return globalThis.localStorage?.getItem(DONE_MIRROR_KEY) === 'true'
  } catch {
    return false
  }
}

function writeDoneMirror(v: boolean): void {
  try {
    globalThis.localStorage?.setItem(DONE_MIRROR_KEY, v ? 'true' : 'false')
  } catch {
    // 存储不可用:不影响主流程(Electron 里真值本来就在 console.toml)
  }
}

export const useSetupStore = defineStore('setup', () => {
  const done = ref(true)
  /**
   * 向导当前步(0..4)。**必须放 store**:页面自己 `ref(0)` 时组件一重挂(首登去 `P-ACCT-NEW`
   * 再回来 / HMR)就回第 1 步,而 01 §2.7.1 步 4 明写「现在添加 → 进 P-ACCT-NEW(**完成后回到本步**)」。
   */
  const step = ref(0)
  /** 告知页改版 ⇒ 已完成向导的机器重启后也要重新勾(05 §6.1 末句 / §8b.6 U1) */
  const reackRequired = ref(false)
  const noticeText = ref('')
  const noticeVersion = ref('')
  const scrolledToBottom = ref(false)
  const acked = ref(false)
  const ackMs = ref<number | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function loadConfig(): Promise<void> {
    try {
      const cfg = await window.qt?.config.read()
      if (cfg) {
        done.value = cfg.setup?.done === true
        writeDoneMirror(done.value)
        return
      }
    } catch {
      // 读 console.toml 失败:退回本机镜像,不能让它把 App 的启动序列(装守卫、绑事件)整条打断
    }
    done.value = readDoneMirror()
  }

  async function loadNotice(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const n = await systemApi.notice()
      noticeText.value = n.text
      noticeVersion.value = n.notice_version
      ackMs.value = typeof n.ack_ms === 'number' ? n.ack_ms : null
      // 告知版本升高后需要重新勾选(05 §6.1)
      acked.value = !!n.acked_version && n.acked_version === n.notice_version
    } catch (e) {
      // #86 后端未就绪:向导要能显示错误 + 重试,不白屏(文案空 → 页面提示)
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  async function ack(): Promise<void> {
    await systemApi.noticeAck(noticeVersion.value)
    // 以 Agent 为准:写完重读一次,别让界面记住一个服务端没落下的勾
    await loadNotice()
    /**
     * 🔴 这里**不清** `reackRequired`(R-1):勾完就清会让页面的 `reackOnly` 立刻变 false,
     * 按钮变回「下一步」、点了进第 2 步 = 重走五步。重勾态由 `SetupPage.goNext()` 在
     * 「直进主页」那一刻清掉(清完再跳,守卫 `needsReack` 才不把人打回)。
     * #87 失败会在上面抛出,`acked` 不变 ⇒ 按钮禁用、守卫照旧按住 `/setup`;
     * 勾完没点按钮就刷新 ⇒ `refreshAck()` 以 Agent 的 `acked_version` 重判,已勾即放行。
     */
  }

  /**
   * 启动时核对「这一版告知勾过没有」。
   * 只在**已完成向导**时有意义(没完成本来就要走向导);#86 拉不到(后端未就绪)时**不判**,
   * 否则会把人锁死在告知页 —— 判据以 Agent 为准,取不到就不是「没勾」。
   */
  async function refreshAck(): Promise<void> {
    if (!done.value) return
    await loadNotice()
    if (error.value) return
    reackRequired.value = !acked.value
    // 要重勾就从告知页开始(勾完直接进控制台,不重走五步)
    if (reackRequired.value) step.value = 0
  }

  async function finish(autoLaunch: boolean, trayOnClose: boolean): Promise<void> {
    // 先把「向导已完成」落盘:开机自启在部分环境会失败(权限/平台不支持),
    // 它一抛异常就轮不到下面的 patch ⇒ `done` 没落盘 ⇒ 下次启动又从向导进。
    await window.qt?.config.patch({
      setup: { done: true },
      app: { auto_launch: autoLaunch, minimize_to_tray_on_close: trayOnClose },
    })
    done.value = true
    writeDoneMirror(true)
    try {
      await window.qt?.app.setAutoLaunch(autoLaunch)
    } catch {
      // 开机自启设置失败不回滚「向导已完成」;偏好值已写进 console.toml,可在 P-SET 再调
    }
  }

  async function rerun(): Promise<void> {
    await window.qt?.config.patch({ setup: { done: false } })
    done.value = false
    writeDoneMirror(false)
    step.value = 0
    /**
     * 01 §2.7.1「每步『上一步』可退,但告知页勾选不重置」说的是**向导内**退步;
     * 「重新运行向导」是重走一遍,滚动判定要重来,否则告知区一进去就是解锁态(等于没这道门)。
     * 「已确认」本身不由这里定 —— 仍以 Agent 的 `acked_version` 为准(`loadNotice()` 回填)。
     */
    scrolledToBottom.value = false
  }

  return {
    done, step, reackRequired, noticeText, noticeVersion, scrolledToBottom, acked, ackMs, loading, error,
    loadConfig, loadNotice, refreshAck, ack, finish, rerun,
  }
})
