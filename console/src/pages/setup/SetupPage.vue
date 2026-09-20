<script setup lang="ts">
/**
 * `P-SETUP` 首次启动向导(01 §2.7.1)。
 * 告知 → 连接 → 自检 → 首登(可跳过)→ 完成。
 * 合规确认**以 Agent 为准**(01-P3):文案与「勾过没有」= #86 `GET /system/notice`
 * (回 `{notice_version, text, ack_ms, acked_version}`),勾选 = #87 `POST /system/notice/ack`。
 * (原先写的 `PUT /settings/compliance` 在 02 #88 的 group 枚举里不存在,真后端 404。)
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { setup as T } from '@/testids'
import { NOTICE_FIXED_PARAGRAPHS, useSetupStore } from '@/stores/setup'
import { useSessionStore } from '@/stores/session'
import { useEnvStore } from '@/stores/env'
import { useUiStore } from '@/stores/ui'
import { CHANNELS, CHANNEL_TEXT } from '@/i18n/zh-CN/codes'

const router = useRouter()
const store = useSetupStore()
const session = useSessionStore()
const env = useEnvStore()
const ui = useUiStore()

const step = ref(0)
const autoLaunch = ref(true)
const trayOnClose = ref(true)
const running = ref(false)
const noticeBox = ref<HTMLElement | null>(null)

const STEPS = ['告知', '连接', '自检', '首登(可跳过)', '完成']

/** 步 1 未勾选禁用;步 3 有红项禁用 */
const nextDisabled = computed(() => {
  if (step.value === 0) return !store.acked
  if (step.value === 1) return session.authState !== 'ok' || !session.agentReachable
  if (step.value === 2) return env.selftestHasError
  return false
})

function onScroll(): void {
  const el = noticeBox.value
  if (!el) return
  if (el.scrollTop + el.clientHeight >= el.scrollHeight - 8) store.scrolledToBottom = true
}

async function toggleAck(checked: boolean): Promise<void> {
  if (!checked) { store.acked = false; return }
  await store.ack()
}

async function runSelfcheck(): Promise<void> {
  running.value = true
  try { await env.runSelftest() } finally { running.value = false }
}

async function finish(): Promise<void> {
  await store.finish(autoLaunch.value, trayOnClose.value)
  ui.autoLaunch = autoLaunch.value
  ui.trayOnClose = trayOnClose.value
  void router.replace('/dash')
}

function quitApp(): void {
  void window.qt?.app.quit()
}

function addAccount(ch: string): void {
  void router.push({ path: '/acct/new', query: { ch, from: 'setup' } })
}

onMounted(async () => {
  await store.loadNotice().catch(() => undefined)
  await session.pingAgent()
  await env.loadAll().catch(() => undefined)
})
</script>

<template>
  <div class="setup">
    <a-steps :current="step" size="small" :data-testid="T.steps" class="steps">
      <a-step v-for="s in STEPS" :key="s" :title="s" />
    </a-steps>

    <!-- 步 1 告知 -->
    <section v-if="step === 0" class="qt-card pane">
      <h3>使用前请阅读</h3>
      <!-- #86 未就绪时要说清「为什么空」并给重试,不能只挂一句「正在读取」转到底 -->
      <a-alert
        v-if="store.error"
        type="error"
        show-icon
        class="mb"
        message="读不到合规告知文案"
        :description="store.error"
      >
        <template #action><a-button size="small" @click="store.loadNotice()">重试</a-button></template>
      </a-alert>
      <div ref="noticeBox" class="notice" @scroll="onScroll">
        <div :data-testid="T.noticeText" class="notice-text">
          {{ store.noticeText || (store.error ? '——' : '正在读取告知文案…') }}
        </div>
        <!-- 控制台以固定段追加在 Agent 下发文案之后(A-7 + A-5),不依赖法务改稿 -->
        <div :data-testid="T.noticeFixed" class="notice-fixed">
          <p v-for="(p, i) in NOTICE_FIXED_PARAGRAPHS" :key="i">{{ p }}</p>
        </div>
      </div>
      <a-checkbox
        :data-testid="T.noticeAck"
        :checked="store.acked"
        :disabled="!store.scrolledToBottom"
        @change="(e: any) => toggleAck(e.target.checked)"
      >我已阅读并理解上述内容,承诺账号均为本单位自有</a-checkbox>
      <p v-if="!store.scrolledToBottom" class="qt-small qt-muted">请滚动到底部后再勾选</p>
    </section>

    <!-- 步 2 连接 -->
    <section v-else-if="step === 1" class="qt-card pane">
      <h3>连接三个探针</h3>
      <div class="probe-row" :data-testid="T.connAgent">
        <span>Agent(17600)</span>
        <span :class="session.agentReachable ? 'qt-ok' : 'qt-danger'">
          {{ session.agentReachable ? '可达' : '不可达' }}
        </span>
        <span class="qt-muted qt-small">{{ env.version?.agent ?? '—' }}</span>
      </div>
      <div class="probe-row" :data-testid="T.connWinagent">
        <span>WinAgent 服务 / 用户会话代理</span>
        <span :class="session.winagentOnline ? 'qt-ok' : 'qt-danger'">
          {{ session.winagentOnline ? '在线' : '离线' }}
        </span>
        <span :class="session.userAgentOnline ? 'qt-ok' : 'qt-warn'">
          会话代理{{ session.userAgentOnline ? '在线' : '不在线' }}
        </span>
      </div>
      <p v-if="!session.userAgentOnline" class="qt-warn">{{ session.GATE_LOGIN_HINT }}</p>
      <a-button :data-testid="T.connRetry" @click="session.retryToken()">重试</a-button>
    </section>

    <!-- 步 3 自检 -->
    <section v-else-if="step === 2" class="qt-card pane">
      <h3>一键自检</h3>
      <a-button type="primary" :loading="running" :data-testid="T.selfcheckRun" @click="runSelfcheck">运行自检</a-button>
      <table class="tbl" :data-testid="T.selfcheckTable">
        <thead><tr><th>项</th><th>结果</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="r in env.selftest" :key="r.item">
            <td>{{ r.label }}</td>
            <td :class="r.level === 'error' ? 'qt-danger' : r.level === 'warn' ? 'qt-warn' : 'qt-ok'">
              {{ r.level === 'error' ? '✗' : r.level === 'warn' ? '⚠' : '✔' }}
            </td>
            <td class="qt-small">{{ r.message }}</td>
          </tr>
          <tr v-if="!env.selftest.length"><td colspan="3" class="qt-muted">还没有自检结果</td></tr>
        </tbody>
      </table>
      <p class="qt-small qt-muted">黄色项可以继续;红色项(Agent 不健康、内核不是 OURS)必须先解决。</p>
    </section>

    <!-- 步 4 首登 -->
    <section v-else-if="step === 3" class="qt-card pane">
      <h3>添加第一个账号(可跳过)</h3>
      <div class="cards">
        <div v-for="ch in CHANNELS" :key="ch" class="qt-card ch-card">
          <div class="qt-section-title">{{ CHANNEL_TEXT[ch] }}</div>
          <p v-if="ch === 'wechat' && session.wechatDisabled" class="qt-muted qt-small">未启用(可在设置里开)</p>
          <a-button
            type="primary"
            :data-testid="T.loginAdd(ch)"
            :disabled="ch === 'wechat' && session.wechatDisabled"
            @click="addAccount(ch)"
          >现在添加</a-button>
        </div>
      </div>
      <a-button :data-testid="T.loginSkip" @click="step = 4">跳过</a-button>
    </section>

    <!-- 步 5 完成 -->
    <section v-else class="qt-card pane">
      <h3>完成</h3>
      <div class="qt-col">
        <a-checkbox :data-testid="T.autolaunch" v-model:checked="autoLaunch">开机自启(只进托盘)</a-checkbox>
        <a-checkbox :data-testid="T.trayOnClose" v-model:checked="trayOnClose">关窗口最小化到托盘</a-checkbox>
      </div>
      <a-button type="primary" :data-testid="T.finish" @click="finish">进入控制台</a-button>
    </section>

    <footer class="foot">
      <a-button :data-testid="T.cancel" danger @click="quitApp">取消退出</a-button>
      <span class="qt-grow" />
      <a-button v-if="step > 0" :data-testid="T.prev" @click="step -= 1">上一步</a-button>
      <a-button
        v-if="step < 4"
        type="primary"
        :data-testid="T.next"
        :disabled="nextDisabled"
        @click="step += 1"
      >下一步</a-button>
    </footer>
  </div>
</template>

<style scoped>
.setup { max-width: 860px; margin: 0 auto; padding: var(--qt-space-6); display: flex; flex-direction: column; gap: var(--qt-space-4); height: 100%; }
.steps { flex: 0 0 auto; }
.pane { padding: var(--qt-space-4); flex: 1 1 auto; overflow: auto; }
.mb { margin-bottom: var(--qt-space-3); }
.notice { max-height: 300px; overflow: auto; border: 1px solid var(--qt-border); padding: var(--qt-space-3); margin: var(--qt-space-3) 0; }
.notice-text { white-space: pre-wrap; }
.notice-fixed { margin-top: var(--qt-space-3); border-top: 1px dashed var(--qt-border); padding-top: var(--qt-space-2); }
.probe-row { display: grid; grid-template-columns: 1fr auto auto; gap: var(--qt-space-3); padding: 6px 0; border-bottom: 1px solid var(--qt-border); }
.tbl { width: 100%; border-collapse: collapse; margin-top: var(--qt-space-3); }
.tbl th, .tbl td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--qt-border); }
.cards { display: flex; gap: var(--qt-space-3); margin: var(--qt-space-3) 0; }
.ch-card { flex: 1; padding: var(--qt-space-3); }
.foot { display: flex; gap: var(--qt-space-2); align-items: center; flex: 0 0 auto; }
</style>
