/**
 * `setup` store:首次向导进度 + 使用告知的只读视图。
 *
 * 🔴 端点更正:`/settings/compliance` 在 02 #88 的 `group` 枚举里**不存在**(真后端 404)。
 * 告知文案与「勾过没有」都走 #86 `GET /system/notice`(它回 `{notice_version, text, ack_ms,
 * acked_at, acked_version}`),勾选走 #87 `POST /system/notice/ack {notice_version}`。
 * 🔴 R6-84(2026-10-10 安琳):向导里的「阅读须知」步整步删除,**向导与路由守卫不再以告知确认为门**;
 * 告知只在偏好设置里按需查看(`loadNotice` / `ack` 保留给那条入口),改版重勾(原 05 §6.1 末句)一并退役。
 */
import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
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
/**
 * 当前步。上一轮只放进 Pinia:组件重挂(去建号页再回来)能保住,
 * **整页刷新保不住** —— store 随文档一起重建,`step` 回到 0,人就回到第 1 步。
 * 本机 `127.0.0.1` 转发把 Vite 热更新的长连接掐断时,页面会自己刷新,
 * 点「运行自检」正好撞上,看起来像按钮把人打回告知页。
 * `sessionStorage` 活过刷新、关标签就丢,所以中途刷新留在原来的步,新开一次从第 1 步走。
 */
const STEP_KEY = 'qt.setup.step'

/** 向导共四步(R6-84):0 连接服务 · 1 检查环境 · 2 添加账号 · 3 准备完成 */
export const SETUP_LAST_STEP = 3

function readStep(): number {
  try {
    const n = Number(globalThis.sessionStorage?.getItem(STEP_KEY))
    if (Number.isInteger(n) && n >= 0 && n <= SETUP_LAST_STEP) return n
  } catch {
    // 隐私模式 / 站点数据被禁:退回第 1 步,不让读存储把向导整页打断
  }
  return 0
}

function writeStep(v: number): void {
  try {
    globalThis.sessionStorage?.setItem(STEP_KEY, String(v))
  } catch {
    // 写不进就只留在内存里;这次不跳步,下次刷新才可能回到第 1 步
  }
}

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
   * 向导当前步(0..3)。组件重挂靠这份 store(01 §2.7.1 步 3「完成后回到本步」);
   * 整页刷新靠 `STEP_KEY`,只放内存时刷新必回第 1 步。
   */
  const step = ref(readStep())
  // sync:刷新可能紧跟在改步之后,等下一拍再写就来不及
  watch(step, writeStep, { flush: 'sync' })
  const noticeText = ref('')
  const noticeVersion = ref('')
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

  /** 偏好设置里按需确认当前版本的告知(#87);以 Agent 为准,写完重读一次,不让界面记住一个服务端没落下的勾 */
  async function ack(): Promise<void> {
    await systemApi.noticeAck(noticeVersion.value)
    await loadNotice()
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
  }

  return {
    done, step, noticeText, noticeVersion, acked, ackMs, loading, error,
    loadConfig, loadNotice, ack, finish, rerun,
  }
})
