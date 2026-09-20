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

export const useSetupStore = defineStore('setup', () => {
  const done = ref(true)
  const step = ref(0)
  const noticeText = ref('')
  const noticeVersion = ref('')
  const scrolledToBottom = ref(false)
  const acked = ref(false)
  const ackMs = ref<number | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function loadConfig(): Promise<void> {
    const cfg = await window.qt?.config.read()
    done.value = cfg?.setup?.done === true
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
  }

  async function finish(autoLaunch: boolean, trayOnClose: boolean): Promise<void> {
    await window.qt?.app.setAutoLaunch(autoLaunch)
    await window.qt?.config.patch({
      setup: { done: true },
      app: { auto_launch: autoLaunch, minimize_to_tray_on_close: trayOnClose },
    })
    done.value = true
  }

  async function rerun(): Promise<void> {
    await window.qt?.config.patch({ setup: { done: false } })
    done.value = false
    step.value = 0
  }

  return {
    done, step, noticeText, noticeVersion, scrolledToBottom, acked, ackMs, loading, error,
    loadConfig, loadNotice, ack, finish, rerun,
  }
})
