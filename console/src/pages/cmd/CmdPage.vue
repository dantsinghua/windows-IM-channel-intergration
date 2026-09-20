<script setup lang="ts">
/**
 * `P-CMD` 指令台(01 §2.7.5)。
 * 能力目录 + 账号支持集决定可用;写类带 idempotency_key / confirm / timeout_ms;
 * `login_required` 时只放开 screenshot/get_state,一切 IM 写类禁用并悬浮「登录中不可发送」(R-06)。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { cmd as T } from '@/testids'
import { useAccountsStore } from '@/stores/accounts'
import { useCommandsStore } from '@/stores/commands'
import { useMessagesStore } from '@/stores/messages'
import { commandsApi } from '@/api/client'
import { ApiFailure, newIdempotencyKey } from '@/api/http'
import CapabilityForm from '@/components/CapabilityForm.vue'
import ResultCodeTag from '@/components/ResultCodeTag.vue'
import JsonViewer from '@/components/JsonViewer.vue'
import {
  ARGS_DIGEST_MISMATCH_TEXT, INVALID_ARGS_REASONS, OCR_REVIEW_TEXT, SOURCE_TEXT, capabilityText,
} from '@/i18n/zh-CN/codes'
import { isCommandAccepted, type CommandAccepted, type CommandResult } from '@/api/types'

const router = useRouter()
const accounts = useAccountsStore()
const store = useCommandsStore()
const messages = useMessagesStore()

const mode = ref<'single' | 'broadcast'>('single')
const accountId = ref<string | undefined>()
const broadcastIds = ref<string[]>([])
const op = ref<string | undefined>()
const args = ref<Record<string, unknown>>({})
const idem = ref(newIdempotencyKey())
const confirm = ref(true)
const timeoutMs = ref(30000)
const running = ref(false)
const result = ref<CommandResult | null>(null)
/** #28 的 `202` 受理体:同步等待超 `http_sync_max_wait_ms` 或 `async:true`(结果走 command_done) */
const accepted = ref<CommandAccepted | null>(null)
const failure = ref<ApiFailure | null>(null)
/** `409 IDEMPOTENT_REPLAY` 的响应体仍是完整 CommandResult(B-06):显示首次结果并说明来由 */
const replayNote = ref<string | null>(null)
const sessionPickOpen = ref(false)

const account = computed(() => (accountId.value ? accounts.byId[accountId.value] ?? null : null))
const channel = computed(() => account.value?.channel ?? 'qidian')
const def = computed(() => (op.value ? store.byOp[op.value] : undefined))
const isWrite = computed(() => def.value?.kind !== 'read')
const state = computed(() => account.value?.state ?? 'stopped')
const loginPhase = computed(() => state.value === 'login_required')

/** 登录态只放开 screenshot / get_state;其它写类一律禁 */
const LOGIN_PHASE_ALLOWED = ['screenshot', 'get_state']

function tagOf(o: string): string {
  if (!account.value) return ''
  const t = store.tagFor(o, channel.value, account.value.capabilities ?? [])
  return t === 'supported' ? '' : t
}

function opDisabled(o: string): boolean {
  if (!account.value) return true
  if (tagOf(o)) return true
  const d = store.byOp[o]
  if (!d) return true
  if (d.kind === 'read') return false
  if (loginPhase.value) return !LOGIN_PHASE_ALLOWED.includes(o)
  return !['running', 'degraded'].includes(state.value)
}

function opTitle(o: string): string {
  if (tagOf(o)) return tagOf(o) === 'UNSUPPORTED' ? '该通道不支持此能力' : '该通道无此概念'
  if (loginPhase.value && !LOGIN_PHASE_ALLOWED.includes(o)) return '登录中不可发送'
  if (!['running', 'degraded'].includes(state.value)) return '账号未在线'
  return ''
}

const runDisabled = computed(() => {
  if (!op.value) return true
  if (mode.value === 'broadcast') return broadcastIds.value.length === 0
  return opDisabled(op.value)
})

/** error.details[].pointer → 标红字段 */
const invalidFields = computed(() => {
  const d = failure.value?.detail.details ?? []
  return d.map((x) => x.pointer.replace(/^\//, ''))
})

const invalidArgsText = computed(() => {
  const r = failure.value?.reason
  return r && INVALID_ARGS_REASONS[r] ? INVALID_ARGS_REASONS[r] : ''
})

function regenIdem(): void { idem.value = newIdempotencyKey() }

async function run(): Promise<void> {
  if (!op.value) return
  running.value = true
  result.value = null
  accepted.value = null
  failure.value = null
  replayNote.value = null
  try {
    if (mode.value === 'broadcast') {
      const r = await commandsApi.broadcast({
        account_ids: broadcastIds.value, op: op.value, args: args.value,
        idempotency_key: idem.value, confirm: confirm.value,
      })
      store.broadcastResults = r.results
      message.success(`广播已发出(${broadcastIds.value.length} 个账号)`)
    } else {
      const r = await commandsApi.run(accountId.value!, {
        op: op.value, args: args.value,
        idempotency_key: isWrite.value ? idem.value : undefined,
        confirm: channel.value === 'wechat' ? undefined : confirm.value,
        timeout_ms: timeoutMs.value,
      })
      // 🔴 `ok:false` + 业务结果码(SEND_FAILED / UNCONFIRMED / GATE_BLOCKED…)是**正常业务结果**,
      // 走的就是这条路径 —— 不是异常。异常只有 HTTP 层错误(见 catch)。
      if (isCommandAccepted(r)) {
        accepted.value = r
        store.pushHistory({
          accountId: accountId.value!, op: op.value, args: { ...args.value },
          idempotencyKey: idem.value, confirm: confirm.value, timeoutMs: timeoutMs.value,
          traceId: r.trace_id,
        })
        message.info('指令已受理,仍在执行;结果会由 command_done 事件回填')
      } else {
        result.value = r
        store.lastResult = r
        store.pushHistory({
          accountId: accountId.value!, op: op.value, args: { ...args.value },
          idempotencyKey: idem.value, confirm: confirm.value, timeoutMs: timeoutMs.value,
          result: r, traceId: r.trace_id,
        })
      }
    }
  } catch (e) {
    if (e instanceof ApiFailure) {
      const env = e.envelope as unknown as CommandResult | null
      if (e.code === 'IDEMPOTENT_REPLAY' && env && env.code !== undefined) {
        // 02 #28:409 的响应体是**首次那条**完整 CommandResult,`trace_id` 也是首次的
        result.value = env
        store.lastResult = env
        replayNote.value = '这把幂等键此前已执行过(IDEMPOTENT_REPLAY);下面显示的是首次的结果与 trace。'
        store.pushHistory({
          accountId: accountId.value ?? '', op: op.value, args: { ...args.value },
          idempotencyKey: idem.value, result: env, traceId: env.trace_id,
        })
      } else {
        failure.value = e
        store.lastError = { code: e.code, message: e.message, reason: e.reason, traceId: e.traceId }
        store.pushHistory({
          accountId: accountId.value ?? '', op: op.value, args: { ...args.value },
          errorCode: e.code, errorMessage: e.message, errorReason: e.reason, traceId: e.traceId,
        })
      }
    } else {
      message.error(String(e))
    }
  } finally {
    running.value = false
  }
}

/** 同幂等键重发(「再查一次」/「重试」) */
async function resend(): Promise<void> { await run() }

function copyCurl(): void {
  const body = JSON.stringify({ op: op.value, args: args.value, idempotency_key: idem.value, confirm: confirm.value })
  const curl = `curl -X POST http://127.0.0.1:17600/api/v1/accounts/${accountId.value}/commands \\\n`
    + `  -H 'Authorization: Bearer <TOKEN>' -H 'Content-Type: application/json' \\\n  -d '${body}'`
  void navigator.clipboard.writeText(curl)
  message.success('已复制 curl(令牌用 <TOKEN> 占位)')
}

function replay(n: number): void {
  const h = store.history.find((x) => x.n === n)
  if (!h) return
  accountId.value = h.accountId
  op.value = h.op
  args.value = { ...h.args }
  // 回填参数,不自动执行
  message.info('已回填参数,未自动执行')
}

async function pickSession(key: string): Promise<void> {
  sessionPickOpen.value = true
  if (!messages.sessions.length) {
    messages.filter.account_id = accountId.value
    await messages.loadSessions()
  }
  void key
}

function chooseSession(sid: string): void {
  args.value = { ...args.value, session: sid }
  sessionPickOpen.value = false
}

onMounted(async () => {
  if (!accounts.items.length) await accounts.load()
  if (!store.catalog.length) await store.loadCatalog().catch(() => undefined)
  accountId.value = accounts.items.find((a) => a.state === 'running')?.id ?? accounts.items[0]?.id
})
</script>

<template>
  <div class="qt-page cmdgrid">
    <!-- 目标 -->
    <section class="qt-card box">
      <div class="qt-section-title">目标</div>
      <a-radio-group v-model:value="mode">
        <a-radio value="single" :data-testid="T.mode('single')">单账号</a-radio>
        <a-radio value="broadcast" :data-testid="T.mode('broadcast')">广播</a-radio>
      </a-radio-group>
      <a-select
        v-if="mode === 'single'"
        v-model:value="accountId"
        class="full"
        :data-testid="T.account"
        :options="accounts.items.map((a) => ({ value: a.id, label: `${a.id} ${a.label}` }))"
      />
      <a-select
        v-else
        v-model:value="broadcastIds"
        class="full"
        mode="multiple"
        :data-testid="T.broadcastPick"
        placeholder="必须显式勾选账号,没有「全部」"
        :options="accounts.items.map((a) => ({ value: a.id, label: `${a.id} ${a.label}` }))"
      />
      <div class="qt-small qt-muted">
        状态:{{ state }} · 能力 {{ account?.capabilities?.length ?? 0 }}/{{ store.catalog.length }}
      </div>
    </section>

    <!-- 能力 -->
    <section class="qt-card box">
      <div class="qt-section-title">能力</div>
      <div class="caplist">
        <a-tooltip v-for="c in store.catalog" :key="c.op" :title="opTitle(c.op)">
          <div
            class="capitem"
            :class="{ disabled: opDisabled(c.op), active: op === c.op }"
            :data-testid="T.cap(c.op)"
            @click="!opDisabled(c.op) && (op = c.op)"
          >
            <span class="qt-grow">{{ capabilityText(c.op) }}</span>
            <span v-if="c.danger" class="danger-tag">危险</span>
            <span v-if="tagOf(c.op)" class="tag" :data-testid="T.capTag(c.op)">{{ tagOf(c.op) }}</span>
          </div>
        </a-tooltip>
        <a-empty v-if="!store.catalog.length" description="能力目录未加载" />
      </div>
    </section>

    <!-- 参数 -->
    <section class="qt-card box">
      <div class="qt-section-title">参数(由 schema 生成)</div>
      <CapabilityForm
        v-model="args"
        :schema="def?.args_schema"
        :invalid-fields="invalidFields"
        @pick-session="pickSession"
      />
      <template v-if="isWrite">
        <a-form-item label="幂等键">
          <div class="qt-row">
            <a-input v-model:value="idem" class="qt-grow qt-mono" :data-testid="T.idem" />
            <a-button size="small" :data-testid="T.idemRegen" @click="regenIdem">↻</a-button>
          </div>
        </a-form-item>
        <!-- 微信通道不渲染 confirm 开关(恒 true,微信无发送中间确认态) -->
        <a-form-item v-if="channel !== 'wechat'" label="读回确认后返回">
          <a-switch v-model:checked="confirm" :data-testid="T.confirm" />
        </a-form-item>
      </template>
      <a-form-item label="超时 ms">
        <a-input-number v-model:value="timeoutMs" :data-testid="T.timeout" :min="1000" :max="600000" />
      </a-form-item>
      <a-tooltip :title="op ? opTitle(op) : '请选择能力'">
        <a-button type="primary" :loading="running" :disabled="runDisabled" :data-testid="T.run" @click="run">执行</a-button>
      </a-tooltip>
    </section>

    <!-- 结果 -->
    <section class="qt-card box result">
      <div class="qt-section-title">结果</div>

      <template v-if="result">
        <p v-if="replayNote" class="qt-small qt-muted">{{ replayNote }}</p>
        <div class="qt-row">
          <ResultCodeTag :data-testid="T.resultCode" :code="result.code" />
          <span :data-testid="T.result('cost')">{{ result.cost_ms }} ms</span>
          <span :data-testid="T.result('source')">来源 {{ SOURCE_TEXT[result.source ?? ''] ?? result.source ?? '—' }}</span>
          <span v-if="result.data?.needs_review" class="ocr" :data-testid="T.resultOcrReview">{{ OCR_REVIEW_TEXT }}</span>
          <span class="qt-mono qt-small qt-muted" :data-testid="T.result('trace')">trace {{ result.trace_id.slice(0, 8) }}</span>
        </div>
        <!-- 业务失败(ok:false)也要把服务端那句话摆出来,不能只剩一个码 -->
        <p v-if="result.ok === false && result.error?.message" class="qt-danger">{{ result.error.message }}</p>
        <div :data-testid="T.result('state')" class="qt-small qt-muted">
          state_before {{ result.state_before ?? '—' }} → state_after {{ result.state_after ?? '—' }}
        </div>
        <JsonViewer :data-testid="T.resultJson" :value="result.data ?? {}" />
        <div class="qt-row">
          <img v-if="result.data?.shots?.before" :data-testid="T.resultShot('before')" class="shot"
               :src="`/api/v1/media/${result.data.shots.before}`" alt="前截图" />
          <img v-if="result.data?.shots?.after" :data-testid="T.resultShot('after')" class="shot"
               :src="`/api/v1/media/${result.data.shots.after}`" alt="后截图" />
        </div>
        <div class="qt-row">
          <a-button
            v-if="result.code === 'SEND_CALLED_BUT_UNCONFIRMED'"
            size="small"
            :data-testid="T.resultRecheck"
            @click="resend"
          >再查一次</a-button>
          <a-button
            v-else-if="result.ok === false && result.error?.retryable"
            size="small"
            :data-testid="T.resultRetry"
            @click="resend"
          >重试</a-button>
          <a-button
            v-if="result.ok === false && result.error?.needs_human"
            size="small"
            :data-testid="T.resultGohuman"
            @click="router.push(`/screen/${accountId}`)"
          >去画面</a-button>
          <a-button size="small" :data-testid="T.copyCurl" @click="copyCurl">复制 curl</a-button>
        </div>
      </template>

      <template v-else-if="accepted">
        <p class="qt-small">
          指令已受理(HTTP 202),仍在执行 —— 同步等待超过 <code>http_sync_max_wait_ms</code> 或用了
          <code>async</code>;结果会由 <code>command_done</code> 事件回填。
        </p>
        <span class="qt-mono qt-small qt-muted" :data-testid="T.result('trace')">trace {{ accepted.trace_id.slice(0, 8) }}</span>
      </template>

      <template v-else-if="failure">
        <ResultCodeTag :data-testid="T.resultCode" :code="failure.code" :reason="failure.reason" />
        <p class="qt-danger">
          {{ failure.reason === 'args_digest_mismatch' ? ARGS_DIGEST_MISMATCH_TEXT : invalidArgsText || failure.message }}
        </p>
        <span class="qt-mono qt-small qt-muted" :data-testid="T.result('trace')">trace {{ failure.traceShort }}</span>
        <div class="qt-row">
          <a-button
            v-if="failure.detail.retryable && !invalidArgsText"
            size="small"
            :data-testid="T.resultRetry"
            @click="resend"
          >重试</a-button>
          <a-button v-if="failure.detail.needs_human" size="small" :data-testid="T.resultGohuman"
                    @click="router.push(`/screen/${accountId}`)">去画面</a-button>
        </div>
      </template>

      <a-empty v-else description="还没有执行过指令" />

      <div v-if="mode === 'broadcast' && Object.keys(store.broadcastResults).length" class="bcast">
        <div class="qt-section-title">广播结果</div>
        <div v-for="(r, aid) in store.broadcastResults" :key="aid" class="qt-row" :data-testid="T.broadcastResult(String(aid))">
          <span class="qt-grow qt-mono">{{ aid }}</span>
          <ResultCodeTag :code="r.code" />
          <span class="qt-small qt-muted">{{ r.cost_ms }} ms</span>
        </div>
      </div>

      <div class="history">
        <div class="qt-section-title">本会话历史(最近 50 条)</div>
        <div v-for="h in store.history" :key="h.n" class="qt-row" :data-testid="T.history(h.n)">
          <span class="qt-grow qt-small">{{ h.at.slice(11, 19) }} {{ h.accountId }} {{ capabilityText(h.op) }}</span>
          <ResultCodeTag v-if="h.result" :code="h.result.code" />
          <ResultCodeTag v-else-if="h.errorCode" :code="h.errorCode" :reason="h.errorReason" />
          <a-button size="small" :data-testid="T.historyReplay" @click="replay(h.n)">重放</a-button>
        </div>
        <a-empty v-if="!store.history.length" description="暂无历史" />
      </div>
    </section>

    <a-modal v-model:open="sessionPickOpen" title="从会话列表选" :footer="null">
      <div v-for="s in messages.sessions" :key="s.id" class="qt-row sesrow" @click="chooseSession(s.id)">
        <span class="qt-grow">{{ s.name }}</span>
        <span class="qt-mono qt-small qt-muted">{{ s.id }}</span>
      </div>
      <a-empty v-if="!messages.sessions.length" description="暂无会话" />
    </a-modal>
  </div>
</template>

<style scoped>
.cmdgrid { display: grid; grid-template-columns: 260px 260px 1fr; grid-template-rows: auto 1fr; gap: var(--qt-space-4); }
.box { padding: var(--qt-space-3); }
.result { grid-column: 1 / -1; }
.full { width: 100%; margin: var(--qt-space-2) 0; }
.caplist { max-height: 320px; overflow: auto; }
.capitem { display: flex; align-items: center; gap: 6px; padding: 4px 6px; cursor: pointer; border-radius: var(--qt-radius-sm); }
.capitem:hover { background: var(--qt-bg); }
.capitem.active { background: var(--qt-bg); color: var(--qt-primary); }
.capitem.disabled { color: var(--qt-text-disabled); cursor: not-allowed; }
.tag, .danger-tag { font-size: var(--qt-font-xs); border: 1px solid currentColor; border-radius: 8px; padding: 0 4px; }
.danger-tag { color: var(--qt-state-error); }
.ocr { color: var(--qt-sev-warn); border: 1px solid var(--qt-sev-warn); border-radius: 8px; padding: 0 6px; font-size: var(--qt-font-xs); }
.shot { max-width: 220px; border: 1px solid var(--qt-border); }
.history, .bcast { margin-top: var(--qt-space-4); }
.sesrow { padding: 6px; cursor: pointer; border-bottom: 1px solid var(--qt-border); }
</style>
