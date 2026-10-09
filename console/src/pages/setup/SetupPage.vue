<script setup lang="ts">
/**
 * `P-SETUP` 首次启动向导(01 §2.7.1)。
 * 告知 → 连接 → 自检 → 首登(可跳过)→ 完成。
 * 合规确认**以 Agent 为准**(01-P3):文案与「勾过没有」= #86 `GET /system/notice`
 * (回 `{notice_version, text, ack_ms, acked_version}`),勾选 = #87 `POST /system/notice/ack`。
 * (原先写的 `PUT /settings/compliance` 在 02 #88 的 group 枚举里不存在,真后端 404。)
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { setup as T } from '@/testids'
import { NOTICE_FIXED_PARAGRAPHS, useSetupStore } from '@/stores/setup'
import { useSessionStore } from '@/stores/session'
import { useEnvStore } from '@/stores/env'
import { useUiStore } from '@/stores/ui'
import { CHANNELS, CHANNEL_TEXT } from '@/i18n/zh-CN/codes'
import QtIcon from '@/components/QtIcon.vue'

const router = useRouter()
const store = useSetupStore()
const session = useSessionStore()
const env = useEnvStore()
const ui = useUiStore()

/**
 * 🔴 步骤存在 `setup` store 里(不是组件内存):首登步「现在添加」要跳 `P-ACCT-NEW`,
 * 01 §2.7.1 步 4 要求「完成后回到本步」——组件一重挂就回第 1 步的话这条做不到。
 */
const step = computed({ get: () => store.step, set: (v: number) => { store.step = v } })
/** 只差重新勾一次告知(05 §6.1 告知改版):不重走五步,勾完直接进控制台 */
const reackOnly = computed(() => store.done && store.reackRequired)
const autoLaunch = ref(true)
const trayOnClose = ref(true)
const running = ref(false)
const ackPending = ref(false)
const ackError = ref('')
const noticeBox = ref<HTMLElement | null>(null)

const STEPS = ['阅读须知', '连接服务', '检查环境', '添加账号', '准备完成']

/** 步 1 未勾选禁用;步 3 有红项禁用 */
const nextDisabled = computed(() => {
  if (step.value === 0) return !store.acked
  if (step.value === 1) return session.authState !== 'ok' || !session.agentReachable
  if (step.value === 2) return env.selftestHasError
  return false
})

/**
 * 「滚到底才可勾」的本意是确保全文被看到(01 §2.7.1 步 1)。
 * 同一判据覆盖两种情形:内容不溢出(scrollTop=0 且 scrollHeight ≤ clientHeight+容差 ⇒ 全文已可见)
 * 与溢出后滚到底。只靠 `@scroll` 会在「不溢出」时永远不触发 ⇒ 向导卡死,
 * 所以挂载后、文案到货后、字体就绪后、容器/内容尺寸变化(ResizeObserver)时都重判。
 * 文案未到货(占位「正在读取…」)时不判,免得占位短文本提前解锁;一旦解锁不回锁。
 */
const NOTICE_SLACK_PX = 8
function checkNoticeRead(): void {
  const el = noticeBox.value
  if (!el || store.scrolledToBottom || !store.noticeText) return
  if (el.scrollTop + el.clientHeight >= el.scrollHeight - NOTICE_SLACK_PX) store.scrolledToBottom = true
}

// 告知区随 step 0 的 v-if 挂/卸:元素出现时装观察器,元素消失或页面卸载时断开
watch(noticeBox, (el, _old, onCleanup) => {
  if (!el) return
  checkNoticeRead()
  if (typeof ResizeObserver === 'undefined') return
  const ro = new ResizeObserver(() => checkNoticeRead())
  ro.observe(el)
  for (const child of Array.from(el.children)) ro.observe(child)
  onCleanup(() => ro.disconnect())
}, { flush: 'post' })
watch(() => store.noticeText, () => { void nextTick(checkNoticeRead) }, { flush: 'post' })

async function toggleAck(checked: boolean): Promise<void> {
  if (ackPending.value) return
  if (!checked) { store.acked = false; return }
  ackPending.value = true
  ackError.value = ''
  try {
    await store.ack()
    if (!store.acked) ackError.value = store.error || '尚未确认告知已保存，请重试。'
  } catch (e) {
    store.acked = false
    ackError.value = e instanceof Error ? e.message : String(e)
  } finally {
    ackPending.value = false
  }
}

/**
 * 步 3 的「总体结论」(01 §2.7.1 步 3「结果表 + 总体结论」)。
 * `skip` 灰项不计红黄(C-18 / M4-7),只在结论里如实点出「未探测」。
 */
const selftestVerdict = computed(() => {
  const rows = env.selftest
  if (!rows.length) return '还没有自检结果,点「运行自检」开始'
  const n = (lv: string): number => rows.filter((r) => r.level === lv).length
  const parts = [`${n('ok')} 项通过`]
  if (n('warn')) parts.push(`${n('warn')} 项警告`)
  if (n('error')) parts.push(`${n('error')} 项未通过`)
  if (n('skip')) parts.push(`${n('skip')} 项未探测`)
  const head = n('error') ? '有红色项,必须先解决才能继续' : n('warn') ? '有黄色项,可以继续' : '全部通过'
  return `${head}(${parts.join('、')})`
})

async function runSelfcheck(): Promise<void> {
  running.value = true
  try { await env.runSelftest() } finally { running.value = false }
}

async function finish(): Promise<void> {
  try {
    await store.finish(autoLaunch.value, trayOnClose.value)
  } catch (e) {
    // 写不进 console.toml ⇒ 下次启动还会进向导,不能一声不吭地放人走
    message.error(`没能保存「向导已完成」:${e instanceof Error ? e.message : String(e)}`)
    return
  }
  ui.autoLaunch = autoLaunch.value
  ui.trayOnClose = trayOnClose.value
  void router.replace('/dash')
}

/**
 * 「下一步」:告知改版那一路只需重勾告知,勾完即进控制台,不重走五步。
 * 重勾态**只在这里清**(`store.ack()` 不清,R-1),且先清后跳 ⇒ 守卫 `needsReack` 不再打回。
 */
function goNext(): void {
  if (reackOnly.value) {
    store.reackRequired = false
    void router.replace('/dash')
    return
  }
  step.value += 1
}

function quitApp(): void {
  void window.qt?.app.quit()
}

function addAccount(ch: string): void {
  void router.push({ path: '/acct/new', query: { ch, from: 'setup' } })
}

onMounted(async () => {
  void document.fonts?.ready.then(checkNoticeRead).catch(() => undefined)
  await store.loadNotice().catch(() => undefined)
  await session.pingAgent()
  await env.loadAll().catch(() => undefined)
  // R6-58 (ab):不带 run_id 取最近一轮,页面直开就能显示上次结论(P-ENV 一直这么做,向导漏了)
  await env.loadSelftest().catch(() => undefined)
})
</script>

<template>
  <div class="setup qt-page">
    <header class="qt-page-heading setup-heading">
      <div><span class="qt-eyebrow">WELCOME TO QTRADE</span><h1>{{ reackOnly ? '更新使用须知' : '准备好你的账号工作台' }}</h1><p>{{ reackOnly ? '阅读更新内容后，即可继续使用。' : '跟着这五步完成准备，随后就能集中查看和管理 IM 账号。' }}</p></div>
      <span class="setup-emblem"><QtIcon name="shield" :size="32" /></span>
    </header>
    <div class="step-panel qt-glass">
      <a-steps :current="step" size="small" :data-testid="T.steps" class="steps">
        <a-step v-for="s in STEPS" :key="s" :title="s" />
      </a-steps>
    </div>

    <!-- 步 1 告知 -->
    <section v-if="step === 0" class="qt-surface pane">
      <div class="pane-heading"><span class="step-label">01 / 使用须知</span><h2>使用前，请先了解这些信息</h2><p>阅读完整内容并确认后，继续连接服务。</p></div>
      <!-- 05 §6.1:告知页文本改版则要求重新勾选;这一路只需重勾,不重走五步 -->
      <a-alert
        v-if="reackOnly"
        type="info"
        show-icon
        class="mb"
        message="合规告知已更新,请重新阅读并确认"
      />
      <!-- #86 未就绪时要说清「为什么空」并给重试,不能只挂一句「正在读取」转到底 -->
      <a-alert
        v-if="store.error"
        type="error"
        show-icon
        class="mb"
        message="暂时无法加载使用须知"
        :description="store.error"
      >
        <template #action><a-button size="small" @click="store.loadNotice()">重试</a-button></template>
      </a-alert>
      <div ref="noticeBox" class="notice" @scroll="checkNoticeRead">
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
        :disabled="!store.scrolledToBottom || ackPending"
        @change="(e: any) => toggleAck(e.target.checked)"
      >我已阅读并理解上述内容,承诺账号均为本单位自有</a-checkbox>
      <p v-if="ackError" role="alert" class="qt-danger">{{ ackError }}</p>
      <p v-if="!store.scrolledToBottom" class="qt-small qt-muted">请滚动到底部后再勾选</p>
    </section>

    <!-- 步 2 连接 -->
    <section v-else-if="step === 1" class="qt-surface pane">
      <div class="pane-heading"><span class="step-label">02 / 连接服务</span><h2>确认工作台可以连接各项服务</h2><p>服务连接正常后，就可以进入环境检查。</p></div>
      <div class="probe-row" :data-testid="T.connAgent">
        <div class="probe-name"><span class="probe-icon"><QtIcon name="env" /></span><div><strong>账号运行服务</strong><p>Agent · {{ env.version?.agent ?? '版本暂不可用' }}</p></div></div>
        <span class="connection-state" :class="session.agentReachable ? 'is-ready' : 'is-unavailable'">
          {{ session.agentReachable ? '已连接' : '未连接' }}
        </span>
      </div>
      <div class="probe-row" :data-testid="T.connWinagent">
        <div class="probe-name"><span class="probe-icon warm"><QtIcon name="screen" /></span><div><strong>Windows 连接服务</strong><p>WinAgent 与当前登录会话</p></div></div>
        <div class="connection-pair"><span class="connection-state" :class="session.winagentOnline ? 'is-ready' : 'is-unavailable'">
          服务{{ session.winagentOnline ? '在线' : '离线' }}
        </span>
        <span class="connection-state" :class="session.userAgentOnline ? 'is-ready' : 'needs-attention'">
          用户会话{{ session.userAgentOnline ? '在线' : '未连接' }}
        </span></div>
      </div>
      <p v-if="!session.userAgentOnline" class="qt-warn">{{ session.GATE_LOGIN_HINT }}</p>
      <a-button class="retry-connection" :data-testid="T.connRetry" @click="session.retryToken()"><template #icon><QtIcon name="refresh" :size="16" /></template>重新连接</a-button>
    </section>

    <!-- 步 3 自检 -->
    <section v-else-if="step === 2" class="qt-surface pane">
      <div class="pane-heading"><span class="step-label">03 / 检查环境</span><h2>检查账号运行环境</h2><p>一次检查，了解当前环境是否准备就绪。</p></div>
      <div class="qt-row">
        <a-button type="primary" :loading="running" :data-testid="T.selfcheckRun" @click="runSelfcheck">运行自检</a-button>
        <span class="qt-small qt-muted">上次 {{ env.selftestAt ?? '—' }}</span>
      </div>
      <p class="verdict" :class="env.selftestHasError ? 'has-error' : ''">{{ selftestVerdict }}</p>
      <table class="tbl" :data-testid="T.selfcheckTable">
        <thead><tr><th>检查项目</th><th>结果</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="r in env.selftest" :key="r.item">
            <td>{{ r.label }}</td>
            <!-- C-18 / M4-7:`skip` = 灰「未探测」,既不是 ✔ 也不是 ⚠ -->
            <td :class="r.level === 'error' ? 'qt-danger' : r.level === 'warn' ? 'qt-warn' : r.level === 'skip' ? 'qt-muted' : 'qt-ok'">
              {{ r.level === 'error' ? '未通过' : r.level === 'warn' ? '需关注' : r.level === 'skip' ? '未探测' : '已通过' }}
            </td>
            <td class="qt-small">{{ r.message }}</td>
          </tr>
          <tr v-if="!env.selftest.length"><td colspan="3" class="qt-muted empty-check">还没有检查结果，点击「运行自检」开始。</td></tr>
        </tbody>
      </table>
      <p class="qt-small qt-muted check-help">「需关注」与「未探测」可以继续；「未通过」的项目需要先处理。</p>
    </section>

    <!-- 步 4 首登 -->
    <section v-else-if="step === 3" class="qt-surface pane">
      <div class="pane-heading"><span class="step-label">04 / 添加账号</span><h2>从你的第一个 IM 账号开始</h2><p>选择需要连接的通道，也可以稍后在首页添加。</p></div>
      <div class="cards">
        <div v-for="ch in CHANNELS" :key="ch" class="qt-glass ch-card" :class="ch">
          <span class="channel-symbol"><QtIcon :name="ch === 'qidian' ? 'acct' : 'msg'" :size="28" /></span>
          <h3>{{ CHANNEL_TEXT[ch] }}</h3>
          <p v-if="ch === 'wechat' && session.wechatDisabled" class="qt-muted qt-small">微信模块尚未启用</p>
          <p v-else class="qt-muted qt-small">连接后可查看状态与历史消息</p>
          <a-button
            type="primary"
            :data-testid="T.loginAdd(ch)"
            :disabled="ch === 'wechat' && session.wechatDisabled"
            @click="addAccount(ch)"
          >添加{{ CHANNEL_TEXT[ch] }}账号</a-button>
        </div>
      </div>
      <a-button type="text" :data-testid="T.loginSkip" @click="step = 4">稍后添加，继续</a-button>
    </section>

    <!-- 步 5 完成 -->
    <section v-else class="qt-surface pane finish-pane">
      <span class="finish-icon"><QtIcon name="check" :size="36" /></span>
      <div class="pane-heading"><span class="step-label">05 / 准备完成</span><h2>欢迎来到你的账号工作台</h2><p>准备步骤已完成，选择适合你的使用习惯。</p></div>
      <div class="finish-options">
        <a-checkbox :data-testid="T.autolaunch" v-model:checked="autoLaunch">开机自动启动，保持在托盘中</a-checkbox>
        <a-checkbox :data-testid="T.trayOnClose" v-model:checked="trayOnClose">关闭窗口时，继续在托盘中运行</a-checkbox>
      </div>
      <a-button type="primary" size="large" :data-testid="T.finish" @click="finish">进入账号工作台<QtIcon name="chevron" :size="16" /></a-button>
    </section>

    <footer class="foot">
      <a-button type="text" :data-testid="T.cancel" @click="quitApp">退出设置</a-button>
      <span class="qt-grow" />
      <a-button v-if="step > 0" :data-testid="T.prev" @click="step -= 1">上一步</a-button>
      <a-button
        v-if="step < 4"
        type="primary"
        :data-testid="T.next"
        :disabled="nextDisabled"
        @click="goNext"
      >{{ reackOnly ? '确认并进入控制台' : '下一步' }}</a-button>
    </footer>
  </div>
</template>

<style scoped>
.setup { position: relative; isolation: isolate; width: 100%; max-width: 1120px; margin: 0 auto; display: flex; flex-direction: column; gap: 22px; min-height: 100%; }
.setup::before { content: ''; position: absolute; z-index: -1; width: 350px; height: 350px; top: 30px; right: 0; border-radius: 50%; background: radial-gradient(circle, #e3d4f5b3, transparent 70%); filter: blur(25px); pointer-events: none; }
.setup::after { content: ''; position: absolute; z-index: -1; width: 270px; height: 270px; bottom: 60px; left: 0; border-radius: 50%; background: radial-gradient(circle, #ffe6a880, transparent 70%); filter: blur(30px); pointer-events: none; }
.setup-heading { margin: 8px 0 0; }.setup-emblem { display: grid; place-items: center; width: 68px; height: 68px; flex-shrink: 0; border-radius: 24px; color: var(--qt-primary); background: linear-gradient(140deg, #f0e2ffb3, #fff0cfb3); border: 1px solid white; box-shadow: 0 10px 30px #7046a118; }
.step-panel { padding: 24px 30px; }.steps { flex: 0 0 auto; }.steps :deep(.ant-steps-item-title) { font-size: 13px; }
.pane { padding: 32px; flex: 1 1 auto; overflow: auto; min-height: 340px; }.pane-heading { margin-bottom: 24px; }.pane-heading h2 { margin: 8px 0; font-size: 22px; font-weight: 600; letter-spacing: -.5px; }.pane-heading p { margin: 0; color: var(--qt-text-secondary); line-height: 1.7; }.step-label { color: var(--qt-primary); font-size: 12px; letter-spacing: 1px; font-weight: 600; }
.mb { margin-bottom: var(--qt-space-3); }
.notice { max-height: 320px; overflow: auto; border: 1px solid #ebe5f1; border-radius: 18px; padding: 24px; margin: 20px 0; background: #faf8fc; line-height: 1.95; scrollbar-color: #c6b5d9 transparent; scrollbar-width: thin; }
.notice-text { white-space: pre-wrap; }.notice-fixed { margin-top: 20px; border-top: 1px dashed #ddd1e9; padding-top: 16px; color: #65596f; }
.probe-row { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 22px 0; border-bottom: 1px dashed var(--qt-border); }.probe-name { display: flex; align-items: center; gap: 16px; }.probe-name strong { font-size: 15px; }.probe-name p { margin: 6px 0 0; color: var(--qt-text-secondary); font-size: 12px; }.probe-icon { display: grid; place-items: center; width: 46px; height: 46px; border-radius: 16px; color: var(--qt-primary); background: #f0e8f9; }.probe-icon.warm { background: #fff3d9; color: #9b6b20; }.connection-pair { display: flex; flex-wrap: wrap; gap: 10px; }.connection-state { padding: 6px 12px; border-radius: 20px; font-size: 12px; white-space: nowrap; }.is-ready { color: #32785a; background: #edf8f1; }.is-unavailable { color: #b63f3a; background: #fff1f0; }.needs-attention { color: #906416; background: #fff4dc; }.retry-connection { margin-top: 24px; }
.verdict { padding: 14px 18px; margin: 22px 0 0; border-radius: 14px; background: #f4eff9; color: #72518f; }.verdict.has-error { color: #b34841; background: #fff2ef; }.tbl { width: 100%; border-collapse: collapse; margin-top: 18px; }.tbl th, .tbl td { text-align: left; padding: 15px 16px; border-bottom: 1px dashed #e8e2ee; }.tbl th { background: #faf8fc; font-size: 12px; color: var(--qt-text-secondary); font-weight: 500; }.tbl td:nth-child(2) { white-space: nowrap; }.tbl .empty-check { text-align: center; padding: 42px 16px; }.check-help { margin-top: 20px; }
.cards { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; margin: 28px 0 20px; }.ch-card { padding: 26px 22px; display: flex; align-items: flex-start; flex-direction: column; background: linear-gradient(140deg, #f1eafa99, #ffffffd9); border-color: #ede6f4; }.ch-card.qq { background: linear-gradient(140deg, #fff2d799, #ffffffd9); border-color: #f2ead9; }.ch-card.wechat { background: linear-gradient(140deg, #f5eefb99, #fff7eccc); }.channel-symbol { width: 52px; height: 52px; display: grid; place-items: center; color: var(--qt-primary); background: #ffffffa6; box-shadow: 0 5px 15px #8e68a512; border: 1px solid #fff; border-radius: 18px; margin-bottom: 18px; }.ch-card h3 { font-size: 20px; margin: 0; }.ch-card p { flex: 1; margin: 10px 0 24px; line-height: 1.7; }.ch-card :deep(.ant-btn) { width: 100%; }
.finish-pane { text-align: center; display: flex; flex-direction: column; align-items: center; padding-top: 40px; padding-bottom: 40px; }.finish-icon { display: grid; place-items: center; width: 84px; height: 84px; border-radius: 28px; color: var(--qt-primary); background: linear-gradient(140deg, #e5d3f7, #fff0c8); border: 1px solid white; box-shadow: 0 14px 28px #8054a21f; margin-bottom: 26px; }.finish-options { display: flex; flex-direction: column; gap: 16px; align-items: flex-start; text-align: left; padding: 22px 28px; border-radius: 18px; background: #faf8fc; margin-bottom: 28px; }.finish-options :deep(.ant-checkbox-wrapper) { margin-inline-start: 0; }.finish-pane > :deep(.ant-btn) { display: inline-flex; align-items: center; gap: 14px; }
.foot { display: flex; gap: 12px; align-items: center; flex: 0 0 auto; padding: 0 4px 8px; }
@media (max-width: 760px) { .setup { gap: 16px; }.setup-heading { align-items: flex-start; }.setup-emblem { display: none; }.step-panel { padding: 20px; }.pane { padding: 22px; }.cards { grid-template-columns: 1fr; }.ch-card { align-items: center; text-align: center; }.probe-row { align-items: flex-start; flex-direction: column; }.notice { padding: 18px; }.finish-options { padding: 20px; }.setup::before, .setup::after { right: 0; left: 0; max-width: 100%; } }
</style>
