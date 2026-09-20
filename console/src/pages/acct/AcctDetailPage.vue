<script setup lang="ts">
/**
 * `P-ACCT-DETAIL` 账号详情(01 §2.7.3.4),八个 Tab。
 * 概览带 `state_code` 引导卡片(D-2 等人组标题恒「登录中·等待你操作」)与企点读取降级横幅;
 * 删除 = 软删无参,彻底删除数据 = purge 两步(R-11)。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { acctDetail as T, ACCT_DETAIL_HEALTH_ITEMS, ACCT_DETAIL_TABS } from '@/testids'
import { useAccountsStore } from '@/stores/accounts'
import { useResourcesStore } from '@/stores/resources'
import { useEventsStore } from '@/stores/events'
import { useSessionStore } from '@/stores/session'
import { accountsApi, auditApi } from '@/api/client'
import { ApiFailure } from '@/api/http'
import StateDot from '@/components/StateDot.vue'
import PromptCard from '@/components/PromptCard.vue'
import JobProgress from '@/components/JobProgress.vue'
import {
  ALERT_CODES, CAPABILITY_TEXT, LOGIN_PHASE_TITLE, QIDIAN_READ_DEGRADED_CODES,
  STATE_CODES, capabilityText, stateCardSubtitle, stateCardTitle,
} from '@/i18n/zh-CN/codes'
import { auditDetail, auditTsText, type AuditRow, type Job } from '@/api/types'

const route = useRoute()
const router = useRouter()
const accounts = useAccountsStore()
const resources = useResourcesStore()
const events = useEventsStore()
const session = useSessionStore()

const id = computed(() => String(route.params.id ?? ''))
const tab = ref<string>('overview')
const editingLabel = ref('')
const editLabel = ref(false)
const purgeModal = ref(false)
const purgeInput = ref('')
const pwModal = ref(false)
const pwSecret = ref('')
const pwRemember = ref(false)
const credModal = ref(false)
const credSecret = ref('')
const exportJob = ref<string | null>(null)
const webuiUntil = ref<number | null>(null)
const now = ref(Date.now())
const recent = ref<AuditRow[]>([])
const settingsForm = ref<Record<string, unknown>>({})
const acctCaps = ref<{ capabilities: string[]; matrix: Record<string, string> } | null>(null)
let tick: ReturnType<typeof setInterval> | null = null

const a = computed(() => accounts.byId[id.value] ?? null)
const prompt = computed(() => accounts.prompts[id.value] ?? null)
const ops = computed(() => accounts.ops(a.value))
const stateCode = computed(() => a.value?.state_code ?? '')
const codeMeta = computed(() => (stateCode.value ? STATE_CODES[stateCode.value] : undefined))
/** 卡片只在 login_required / degraded / error 出现;RATE_LIMITED 不进本卡片(R-12) */
const showStateCard = computed(() =>
  !!a.value && ['login_required', 'degraded', 'error'].includes(a.value.state) && stateCode.value !== 'RATE_LIMITED')
const isWaitGroup = computed(() => codeMeta.value?.group === 'wait')

/** 企点读取降级横幅:只认 QIDIAN_NOT_ROOT / QIDIAN_DB_UNAVAILABLE 两码,且 subject=account:<本账号> */
const readDegraded = computed(() =>
  events.firing.find(
    (al) =>
      (QIDIAN_READ_DEGRADED_CODES as readonly string[]).includes(al.code) &&
      al.subject === `account:${id.value}`,
  ) ?? null)

const purgeMismatch = computed(() => purgeInput.value !== id.value)
const webuiLeft = computed(() => (webuiUntil.value ? Math.max(0, Math.round((webuiUntil.value - now.value) / 1000)) : 0))

const resSnapshot = computed(() =>
  resources.metrics?.ours.accounts.find((x) => x.id === id.value) ??
  resources.pool?.accounts?.find((x) => x.id === id.value) ?? null)

const healthChecks = computed(() => {
  const c = (session.agentReachable ? undefined : undefined) as unknown
  void c
  return ACCT_DETAIL_HEALTH_ITEMS
})

async function reload(): Promise<void> {
  if (!id.value) return
  if (!accounts.items.length) await accounts.load()
  try { acctCaps.value = await accountsApi.capabilities(id.value) } catch { acctCaps.value = null }
  try { recent.value = (await auditApi.list({ kind: 'command', account_id: id.value, limit: 20 })).items } catch { recent.value = [] }
  if (a.value) {
    editingLabel.value = a.value.label
    // 🔴 总控裁决④:Account 不带 `settings` 子对象。账号级设置只经 #22 写,
    // 读侧目前只有 Account 顶层的这几项(其余键保存后立即生效,页面不假装回显服务端值)。
    settingsForm.value = {
      auto_recover: a.value.auto_recover,
      quota_mb: a.value.quota_mb,
    }
  }
  if (a.value && ['login_required', 'degraded', 'error'].includes(a.value.state)) {
    await accounts.refreshPrompt(id.value).catch(() => undefined)
  }
}

async function act(fn: () => Promise<unknown>, okText: string): Promise<void> {
  try {
    await fn()
    message.success(okText)
    await accounts.load()
  } catch (e) {
    if (e instanceof ApiFailure) message.error(`${e.message}(trace ${e.traceShort})`)
    else message.error(String(e))
  }
}

/** 引导卡片的默认动作按钮(act 名落 qt-acct-detail-state-card-action-{act}) */
async function runStateAction(actName: string): Promise<void> {
  switch (actName) {
    case 'login': await act(() => accountsApi.login(id.value, {}), '已重新发起登录'); break
    case 'password': pwModal.value = true; break
    case 'goto-screen': void router.push(`/screen/${id.value}`); break
    case 'key-retry': await window.qt?.wa.invoke('wechat.key.retry', {}); message.info('已请求重新取钥'); break
    case 'reinstall': void router.push({ path: '/acct/new', query: { ch: 'wechat', step: 'reinstall' } }); break
    case 'unlock': message.info('请解锁 Windows 桌面后重试'); break
    case 'cred-update': credModal.value = true; break
    case 'restart': await act(() => accountsApi.restart(id.value), '已请求重启'); break
    case 'open-env': void router.push('/env'); break
    case 'open-logs': await window.qt?.app.openLogsDir(); break
    case 'narrator-redo': message.info('请重新做一次讲述人仪式'); break
    default: break
  }
}

async function saveLabel(): Promise<void> {
  await act(() => accountsApi.patch(id.value, { label: editingLabel.value }), '已改名')
  editLabel.value = false
}

async function submitPassword(): Promise<void> {
  await act(() => accountsApi.login(id.value, { secret: pwSecret.value, remember: pwRemember.value }), '已提交登录')
  pwSecret.value = ''
  pwModal.value = false
}

async function submitCredential(): Promise<void> {
  await act(() => accountsApi.putCredential(id.value, { secret: credSecret.value, remember: true }), '已更新保险库密码')
  credSecret.value = ''
  credModal.value = false
}

async function doPurge(): Promise<void> {
  if (purgeMismatch.value) return
  await act(() => accountsApi.purge(id.value), '已发起彻底删除')
  purgeModal.value = false
  void router.push('/acct')
}

async function doSoftDelete(): Promise<void> {
  if (!a.value) return
  await act(() => accountsApi.softDelete(id.value, a.value!.label), '已从列表移除(数据与登录态保留)')
  void router.push('/acct')
}

async function copyAdb(): Promise<void> {
  await navigator.clipboard.writeText(`adb connect 127.0.0.1:${a.value?.runtime?.adb_port ?? ''}`)
  message.success('已复制 adb 连接串')
}

async function openWebui(): Promise<void> {
  const r = await accountsApi.webuiOpen(id.value, 10)
  webuiUntil.value = Date.parse(r.until)
  message.success('已临时开启 WebUI(10 分钟)')
}

async function closeWebui(): Promise<void> {
  await accountsApi.webuiClose(id.value)
  webuiUntil.value = null
}

async function exportQqData(): Promise<void> {
  const r = await accountsApi.exportIdentity(id.value)
  exportJob.value = r.job_id
}

async function onExportDone(job: Job): Promise<void> {
  exportJob.value = null
  if (job.state !== 'succeeded') { message.error('导出失败'); return }
  message.success('导出完成,请选择保存位置')
  await window.qt?.files.saveAs(`${id.value}-qq_data.tar`, 'application/x-tar', String(job.result?.download_url ?? ''))
}

async function saveSettings(): Promise<void> {
  await act(() => accountsApi.patchSettings(id.value, settingsForm.value), '账号级设置已保存')
}

async function toggleAutostop(on: boolean): Promise<void> {
  settingsForm.value = { ...settingsForm.value, auto_stop_on_pressure: on }
  await saveSettings()
}

function capTag(op: string): string {
  const m = acctCaps.value?.matrix?.[op]
  if (m === 'supported') return ''
  if (m === 'not_applicable') return 'NOT_APPLICABLE'
  return 'UNSUPPORTED'
}

watch(id, () => void reload())
onMounted(() => {
  void reload()
  tick = setInterval(() => { now.value = Date.now() }, 1000)
})
onUnmounted(() => { if (tick) clearInterval(tick) })
</script>

<template>
  <div class="qt-page" :data-testid="T.drawer">
    <a-result v-if="!a" status="404" title="账号不存在或已删除">
      <template #extra><a-button @click="router.push('/acct')">返回列表</a-button></template>
    </a-result>

    <template v-else>
      <header class="qt-row head">
        <StateDot :state="a.state" :reason="a.state_reason" />
        <template v-if="!editLabel">
          <strong class="qt-grow">{{ a.label }}</strong>
          <a-button size="small" :data-testid="T.labelEdit" @click="editLabel = true">改名</a-button>
        </template>
        <template v-else>
          <a-input v-model:value="editingLabel" class="qt-grow" :maxlength="20" />
          <a-button size="small" type="primary" :data-testid="T.labelSave" @click="saveLabel">保存</a-button>
        </template>
        <span class="qt-mono qt-small qt-muted">{{ a.id }}</span>
      </header>

      <div class="qt-row ops">
        <a-button v-if="ops.start" :data-testid="T.op('start')" @click="act(() => accountsApi.start(a!.id), '已请求启动')">启动</a-button>
        <a-button v-if="ops.stop" :data-testid="T.op('stop')" @click="act(() => accountsApi.stop(a!.id), '已请求停止')">停止</a-button>
        <a-button v-if="ops.restart" :data-testid="T.op('restart')" @click="act(() => accountsApi.restart(a!.id), '已请求重启')">重启</a-button>
        <a-button v-if="ops.screen" :data-testid="T.op('screen')" @click="router.push(`/screen/${a!.id}`)">画面</a-button>
      </div>

      <a-tabs v-model:activeKey="tab">
        <a-tab-pane v-for="t in ACCT_DETAIL_TABS" :key="t">
          <template #tab>
            <span :data-testid="T.tab(t)">
              {{ ({ overview: '概览', runtime: '运行时', identity: '身份档案', login: '登录',
                    caps: '能力', settings: '账号级设置', recent: '最近指令', resource: '资源' } as Record<string, string>)[t] }}
            </span>
          </template>
        </a-tab-pane>
      </a-tabs>

      <!-- 概览 -->
      <section v-if="tab === 'overview'" class="qt-stack">
        <!-- 企点读取降级横幅:warn、无操作按钮(R6-35 已删「重试提权」) -->
        <div v-if="readDegraded" class="warnbar" :data-testid="T.readDegraded">
          {{ ALERT_CODES[readDegraded.code]?.zh ?? readDegraded.message }}
          <span class="qt-small qt-muted">Agent 每 5 分钟自动重试;要立刻重试用账号「重启」。</span>
        </div>

        <div v-if="showStateCard" class="qt-card box" :data-testid="T.stateCard">
          <div class="qt-row">
            <strong>{{ stateCardTitle(stateCode) }}</strong>
            <span v-if="isWaitGroup" class="tag" :data-testid="T.stateCardLoginPhase">{{ LOGIN_PHASE_TITLE }}</span>
          </div>
          <div v-if="stateCardSubtitle(stateCode)" class="qt-muted">{{ stateCardSubtitle(stateCode) }}</div>
          <p>{{ prompt?.text || codeMeta?.zh || a.state_reason }}</p>
          <PromptCard :prompt="prompt" :state-code="stateCode" />
          <div class="qt-row">
            <a-button
              v-for="actName in codeMeta?.actions ?? []"
              :key="actName"
              size="small"
              type="primary"
              :data-testid="T.stateCardAction(actName)"
              @click="runStateAction(actName)"
            >
              {{ ({ login: '重新登录', password: '输入密码登录', 'goto-screen': '去画面',
                    'key-retry': '重试取钥', reinstall: '重装微信', unlock: '请解锁 Windows',
                    'cred-update': '更新保险库里的密码', restart: '重试(重新启动)',
                    'open-env': '去环境页', 'open-logs': '打开日志目录',
                    'narrator-redo': '重做仪式' } as Record<string, string>)[actName] ?? actName }}
            </a-button>
          </div>
        </div>

        <div class="qt-card box">
          <div class="kv"><span>状态</span><b>{{ a.state }} {{ a.state_code }}</b></div>
          <div class="kv"><span>原因</span><b>{{ a.state_reason || '—' }}</b></div>
          <div class="kv"><span>昵称</span><b>{{ a.self_nick || '—' }}</b></div>
          <div class="kv"><span>最近活动</span><b>{{ a.last_seen_at ?? '—' }}</b></div>
        </div>

        <div class="qt-row">
          <a-popconfirm
            title="从列表移除?数据卷与登录态保留,account_id 永不复用"
            @confirm="doSoftDelete"
          >
            <a-button :disabled="!ops.del" :data-testid="T.delete">停用 / 删除</a-button>
          </a-popconfirm>
          <a-button danger :disabled="!ops.del" :data-testid="T.purge" @click="purgeModal = true">彻底删除数据</a-button>
        </div>
      </section>

      <!-- 运行时 -->
      <section v-else-if="tab === 'runtime'" class="qt-card box">
        <div v-for="(v, k) in a.runtime ?? {}" :key="k" class="kv"><span>{{ k }}</span><b class="qt-mono">{{ v }}</b></div>
        <div class="qt-row">
          <a-button v-if="a.channel === 'qidian'" :data-testid="T.copyAdb" @click="copyAdb">复制 adb 连接串</a-button>
          <a-button
            v-if="a.channel === 'qidian'"
            :disabled="!['running', 'degraded'].includes(a.state)"
            :data-testid="T.adbReconnect"
            @click="act(() => accountsApi.reconnectAdb(a!.id), '已重连 adb')"
          >重连 adb</a-button>
          <a-button
            v-if="a.channel === 'qidian'"
            :disabled="!['running', 'degraded'].includes(a.state)"
            :data-testid="T.streamRebuild"
            @click="act(() => accountsApi.restartStream(a!.id), '已重建画面流')"
          >重建画面流</a-button>
        </div>
        <div v-if="a.channel !== 'wechat'" class="qt-row health">
          <span class="qt-small qt-muted">健康项:</span>
          <span v-for="h in healthChecks" :key="h" class="hdot" :data-testid="T.health(h)">
            {{ h.toUpperCase() }}
            <span class="dot" :style="{ background: 'var(--qt-state-running)' }" />
          </span>
        </div>
        <div v-if="a.channel === 'qq'" class="qt-row">
          <a-button v-if="!webuiLeft" :data-testid="T.webuiOpen" @click="openWebui">临时开 WebUI(10 分钟)</a-button>
          <template v-else>
            <span :data-testid="T.webuiCountdown" class="qt-mono">剩余 {{ webuiLeft }} 秒</span>
            <a-button :data-testid="T.webuiClose" @click="closeWebui">提前关闭</a-button>
          </template>
        </div>
      </section>

      <!-- 身份档案 -->
      <section v-else-if="tab === 'identity'" class="qt-card box">
        <template v-if="a.channel === 'qidian'">
          <div v-for="(v, k) in a.identity ?? {}" :key="k" class="kv"><span>{{ k }}</span><b class="qt-mono">{{ v }}</b></div>
          <p class="qt-danger qt-small">机型/指纹/序列号一经生成永不改变。</p>
        </template>
        <template v-else-if="a.channel === 'qq'">
          <div class="kv"><span>qq_data 卷</span><b class="qt-mono">accounts/{{ a.id }}/data</b></div>
          <a-popconfirm
            title="只用于同机重装后恢复;换机器恢复等于新设备,仍需扫码且可能触发风控"
            @confirm="exportQqData"
          >
            <a-button :disabled="a.state !== 'stopped'" :data-testid="T.exportQqdata">导出 qq_data</a-button>
          </a-popconfirm>
          <JobProgress :job-id="exportJob" :testid="T.exportProgress" @done="onExportDone" />
        </template>
        <template v-else>
          <div class="kv"><span>wxid</span><b class="qt-mono">{{ a.wxid ?? '—' }}</b></div>
          <div class="kv"><span>昵称</span><b>{{ a.self_nick ?? '—' }}</b></div>
          <p class="qt-small qt-muted">微信登录态**不备份、不恢复**。</p>
        </template>
      </section>

      <!-- 登录 -->
      <section v-else-if="tab === 'login'" class="qt-card box">
        <div class="kv"><span>登录方式</span><b>{{ a.login?.mode ?? '—' }}</b></div>
        <div class="kv"><span>记住密码</span><b>{{ a.login?.remember ? '是' : '否' }}</b></div>
        <div class="kv">
          <span>保险库</span>
          <b :data-testid="T.credStatus">{{ a.login?.credential_ref ? '已保存' : '未保存' }}</b>
        </div>
        <p class="qt-small qt-muted">永不显示密码明文;控制台令牌对保险库只能写与删。</p>
        <div class="qt-row">
          <a-button
            v-if="a.channel === 'qidian'"
            :data-testid="T.loginPassword"
            @click="pwModal = true"
          >输入密码登录</a-button>
          <a-button
            v-if="a.channel === 'qidian'"
            :disabled="!session.winagentOnline"
            :data-testid="T.credUpdate"
            @click="credModal = true"
          >更新保险库里的密码</a-button>
          <a-popconfirm title="清除保存的密码?下次启动需手输" @confirm="act(() => accountsApi.deleteCredential(a!.id), '已清除')">
            <a-button :disabled="!session.winagentOnline" :data-testid="T.credClear">清除保存的密码</a-button>
          </a-popconfirm>
        </div>
      </section>

      <!-- 能力 -->
      <section v-else-if="tab === 'caps'" class="qt-card box">
        <p v-if="a.state === 'degraded' && a.state_code === 'KEY_FAIL'" class="qt-danger">
          取钥失败,读写均不可用。
        </p>
        <div v-for="op in Object.keys(CAPABILITY_TEXT)" :key="op" class="kv" :data-testid="T.cap(op)">
          <span :class="{ dim: !!capTag(op) }">{{ capabilityText(op) }} <span class="qt-mono qt-small">{{ op }}</span></span>
          <b>{{ capTag(op) || '可用' }}</b>
        </div>
      </section>

      <!-- 账号级设置 -->
      <section v-else-if="tab === 'settings'" class="qt-card box">
        <a-form layout="vertical">
          <a-form-item label="发送最小间隔 ms">
            <a-input-number
              :data-testid="T.settings('send-interval')"
              :value="(settingsForm['send.min_interval_ms'] as number)"
              @change="(v: any) => settingsForm['send.min_interval_ms'] = v"
            />
          </a-form-item>
          <a-form-item label="发送抖动 ms">
            <a-input-number
              :data-testid="T.settings('send-jitter')"
              :value="(settingsForm['send.jitter_ms'] as number)"
              @change="(v: any) => settingsForm['send.jitter_ms'] = v"
            />
          </a-form-item>
          <a-form-item label="每分钟最多发送">
            <a-input-number
              :data-testid="T.settings('send-max')"
              :value="(settingsForm['send.max_per_minute'] as number)"
              @change="(v: any) => settingsForm['send.max_per_minute'] = v"
            />
          </a-form-item>
          <a-form-item label="会话白名单(会话名或原生 ID,* 为全部)">
            <a-textarea
              :data-testid="T.settings('allowlist')"
              :rows="2"
              :value="(settingsForm['sessions.allowlist'] as string)"
              @change="(e: any) => settingsForm['sessions.allowlist'] = e.target.value"
            />
          </a-form-item>
          <a-form-item label="自定义闸">
            <a-textarea
              :data-testid="T.settings('gates')"
              :rows="2"
              :value="(settingsForm['gates.custom'] as string)"
              @change="(e: any) => settingsForm['gates.custom'] = e.target.value"
            />
          </a-form-item>
          <a-form-item label="记录正文">
            <a-switch
              :data-testid="T.settings('log-body')"
              :checked="!!settingsForm['log.body']"
              @change="(v: any) => settingsForm['log.body'] = !!v"
            />
          </a-form-item>
          <a-form-item label="保留天数(空 = 继承全局;上限 30)">
            <a-input-number
              :data-testid="T.settings('retention')"
              :max="30"
              :value="(settingsForm['retention_days'] as number)"
              @change="(v: any) => settingsForm['retention_days'] = v"
            />
          </a-form-item>
          <a-form-item label="开机自恢复(仅拉起容器,不含登录)">
            <a-switch
              :data-testid="T.settings('auto-recover')"
              :checked="!!settingsForm['auto_recover']"
              @change="(v: any) => settingsForm['auto_recover'] = !!v"
            />
          </a-form-item>
          <a-form-item label="内存吃紧时允许自动停用本账号">
            <a-popconfirm
              title="开启后内存吃紧时本账号可能被系统自动停用"
              @confirm="toggleAutostop(!settingsForm['auto_stop_on_pressure'])"
            >
              <a-switch :data-testid="T.autostop" :checked="!!settingsForm['auto_stop_on_pressure']" />
            </a-popconfirm>
            <div class="qt-danger qt-small" :data-testid="T.autostopNote">
              默认关;开启后内存 critical 时本账号可能被按 LRU 自动停用。
            </div>
          </a-form-item>
          <a-button type="primary" :data-testid="T.settingsSave" @click="saveSettings">保存</a-button>
        </a-form>
      </section>

      <!-- 最近指令 -->
      <section v-else-if="tab === 'recent'" class="qt-card box">
        <table class="tbl">
          <thead><tr><th>时间</th><th>能力</th><th>结果码</th><th>耗时</th><th /></tr></thead>
          <tbody>
            <tr v-for="r in recent" :key="r.id" :data-testid="T.recent(r.trace_id ?? String(r.id))">
              <td class="qt-small">{{ auditTsText(r) }}</td>
              <td>{{ capabilityText(auditDetail(r).op ?? r.action ?? '') }}</td>
              <td>{{ r.result_code ?? '—' }}</td>
              <td>{{ auditDetail(r).cost_ms ?? '—' }} ms</td>
              <td><a-button size="small" @click="router.push({ path: '/cmd', query: { replay: r.trace_id } })">重放</a-button></td>
            </tr>
            <tr v-if="!recent.length"><td colspan="5" class="qt-muted">暂无指令</td></tr>
          </tbody>
        </table>
      </section>

      <!-- 资源 -->
      <section v-else class="qt-card box">
        <div class="kv"><span>配额</span><b :data-testid="T.resField('quota')">{{ a.quota_mb }} MB</b></div>
        <div class="kv"><span>anon</span><b :data-testid="T.resField('anon')">{{ resSnapshot?.anon_mb ?? '—' }} MB</b></div>
        <div class="kv"><span>current</span><b :data-testid="T.resField('current')">{{ resSnapshot?.current_mb ?? '—' }} MB</b></div>
        <div class="kv"><span>容器 max</span><b :data-testid="T.resField('max')">{{ a.quota_mb }} MB</b></div>
        <div class="kv"><span>CPU</span><b :data-testid="T.resField('cpu')">{{ resSnapshot?.cpu_pct ?? '—' }}%</b></div>
      </section>
    </template>

    <!-- 输入密码登录 -->
    <a-modal v-model:open="pwModal" title="输入密码登录" :data-testid="T.loginPasswordModal" :footer="null">
      <a-input v-model:value="pwSecret" type="password" autocomplete="new-password" placeholder="密码(不回显、不入 store)" />
      <a-checkbox v-model:checked="pwRemember" :disabled="!session.winagentOnline" class="mt">这次保存到保险库</a-checkbox>
      <div class="qt-row mt">
        <a-button @click="pwModal = false">取消</a-button>
        <a-button type="primary" :data-testid="T.loginPasswordSubmit" @click="submitPassword">登录</a-button>
      </div>
    </a-modal>

    <!-- 更新保险库密码 -->
    <a-modal v-model:open="credModal" title="更新保险库里的密码" :footer="null">
      <a-input v-model:value="credSecret" type="password" autocomplete="new-password" placeholder="新密码(只写不读)" />
      <div class="qt-row mt">
        <a-button @click="credModal = false">取消</a-button>
        <a-button type="primary" @click="submitCredential">保存</a-button>
      </div>
    </a-modal>

    <!-- 彻底删除数据:须手输账号 ID 原文 -->
    <a-modal v-model:open="purgeModal" title="彻底删除数据" :data-testid="T.purgeModal" :footer="null">
      <p class="qt-danger">
        将清空数据卷与登录态,<b>不可恢复</b>;重新登录等于新设备,可能触发风控。
      </p>
      <p>请手动输入账号 ID 原文 <b class="qt-mono">{{ id }}</b> 以确认:</p>
      <a-input v-model:value="purgeInput" :data-testid="T.purgeInput" :status="purgeMismatch && purgeInput ? 'error' : undefined" />
      <div class="qt-row mt">
        <a-button @click="purgeModal = false">取消</a-button>
        <a-button type="primary" danger :disabled="purgeMismatch" :data-testid="T.purgeConfirm" @click="doPurge">
          确认彻底删除
        </a-button>
      </div>
    </a-modal>
  </div>
</template>

<style scoped>
.head { margin-bottom: var(--qt-space-2); }
.ops { margin-bottom: var(--qt-space-2); gap: var(--qt-space-2); }
.box { padding: var(--qt-space-4); }
.kv { display: flex; justify-content: space-between; padding: 3px 0; border-bottom: 1px dashed var(--qt-border); }
.warnbar { background: #FFFBE6; color: var(--qt-sev-warn); padding: 6px var(--qt-space-3); border-radius: var(--qt-radius-sm); }
.tag { border: 1px solid var(--qt-sev-warn); color: var(--qt-sev-warn); border-radius: 8px; padding: 0 6px; font-size: var(--qt-font-xs); }
.tbl { width: 100%; border-collapse: collapse; }
.tbl th, .tbl td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--qt-border); }
.dim { color: var(--qt-text-disabled); }
.health { margin-top: var(--qt-space-2); }
.hdot { display: inline-flex; align-items: center; gap: 4px; margin-right: var(--qt-space-3); font-size: var(--qt-font-xs); }
.hdot .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.mt { margin-top: var(--qt-space-3); }
</style>
