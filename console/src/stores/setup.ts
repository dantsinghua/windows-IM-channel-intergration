/** `setup` store:首次向导进度;合规确认**以 Agent 为准**(01-P3) */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { settingsApi, systemApi } from '@/api/client'

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

  async function loadConfig(): Promise<void> {
    const cfg = await window.qt?.config.read()
    done.value = cfg?.setup?.done === true
  }

  async function loadNotice(): Promise<void> {
    loading.value = true
    try {
      const n = await systemApi.notice()
      noticeText.value = n.text
      noticeVersion.value = n.notice_version
      const c = await settingsApi.compliance()
      ackMs.value = c.ack_ms
      // 告知版本升高后需要重新勾选(05 §6.1)
      acked.value = !!c.ack_ms && c.notice_version === n.notice_version
    } finally {
      loading.value = false
    }
  }

  async function ack(): Promise<void> {
    const now = Date.now()
    await settingsApi.putCompliance({ ack_ms: now, notice_version: noticeVersion.value })
    ackMs.value = now
    acked.value = true
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
    done, step, noticeText, noticeVersion, scrolledToBottom, acked, ackMs, loading,
    loadConfig, loadNotice, ack, finish, rerun,
  }
})
