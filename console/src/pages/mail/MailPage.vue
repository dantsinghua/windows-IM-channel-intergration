<script setup lang="ts">
/**
 * `P-MAIL` 邮件摆渡(01 §2.7.8),五块。
 * 🔴 待确认危险指令**只能在本机控制台确认**(R-03/§11.17 ③);`approve/reject` 只按 {id} 操作、不核验任何 nonce。
 * 🔴 `409 CONFIRM_EXPIRED` 不当错误处理;`500 + args_digest_mismatch` 弹红色结果框、不给重试。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { mail as T, MAIL_HEALTH_ITEMS } from '@/testids'
import { useMailStore } from '@/stores/mail'
import { useSettingsStore } from '@/stores/settings'
import { useEventsStore } from '@/stores/events'
import { mailApi } from '@/api/client'
import { ApiFailure } from '@/api/http'
import PageState from '@/components/PageState.vue'
import {
  ALERT_CODES, ARGS_DIGEST_MISMATCH_TEXT, MAIL_BANNER_CODES, capabilityText, mailInboxStatusText,
  mailRouteText,
} from '@/i18n/zh-CN/codes'

const router = useRouter()
const store = useMailStore()
const settings = useSettingsStore()
const events = useEventsStore()

const now = ref(Date.now())
const detailOpen = ref(false)
const tamperModal = ref<{ open: boolean; traceId: string }>({ open: false, traceId: '' })
let tick: ReturnType<typeof setInterval> | null = null

const enabled = computed(() => store.status?.enabled ?? settings.mailEnabled)
const banner = computed(() =>
  events.firing.find((a) => (MAIL_BANNER_CODES as readonly string[]).includes(a.code)) ?? null)

/** TTL 由 remaining_ttl_s 起算、以 expires_at 为准;归零自动作废并刷新 */
function ttlOf(expiresAt: string, remaining: number): number {
  const byExpire = Math.round((Date.parse(expiresAt) - now.value) / 1000)
  return Math.max(0, Number.isFinite(byExpire) ? byExpire : remaining)
}

async function approve(id: string): Promise<void> {
  try {
    await mailApi.approve(id)
    message.success('已确认执行')
    await store.reloadAll()
  } catch (e) {
    if (!(e instanceof ApiFailure)) { message.error(String(e)); return }
    if (e.code === 'CONFIRM_EXPIRED') {
      // 「按晚了」不是故障:不弹 a-result,只 toast + 立即重拉
      message.info('该确认已过期,已自动作废')
      await store.reloadPending()
      await store.reloadInbox()
      return
    }
    if (e.reason === 'args_digest_mismatch') {
      tamperModal.value = { open: true, traceId: e.traceId }
      await store.reloadPending()
      await store.reloadInbox()
      return
    }
    message.error(`${e.message}(trace ${e.traceShort})`)
  }
}

async function reject(id: string): Promise<void> {
  try {
    await mailApi.reject(id)
    message.success('已驳回')
    await store.reloadAll()
  } catch (e) {
    if (e instanceof ApiFailure && e.code === 'CONFIRM_EXPIRED') {
      message.info('该确认已过期,已自动作废')
      await store.reloadPending()
      await store.reloadInbox()
      return
    }
    message.error(e instanceof Error ? e.message : String(e))
  }
}

async function openDetail(id: string): Promise<void> {
  store.inboxDetail = await mailApi.inboxDetail(id)
  detailOpen.value = true
}

async function reparse(id: string): Promise<void> {
  await mailApi.reparse(id)
  message.success('已请求重新解析')
  await store.reloadInbox()
}

async function resend(id: string): Promise<void> {
  await mailApi.resend(id)
  await store.reloadAll()
}

async function discard(id: string): Promise<void> {
  await mailApi.discard(id)
  await store.reloadAll()
}

async function cleanupRun(): Promise<void> {
  if (store.cleanupLocked()) return
  store.lockCleanup()
  await mailApi.cleanupRun()
  message.success('已置标志,下一轮立即清理')
  await store.reloadAll()
}

async function retryImap(routeId: string): Promise<void> {
  await mailApi.test('inbound', routeId)
  await store.reloadAll()
}

function bannerJump(code: string, subject: string): void {
  if (subject.startsWith('route:')) void router.push('/set')
  else if (subject.startsWith('endpoint:')) void router.push('/env')
  void code
}

onMounted(() => {
  void store.reloadAll()
  store.startPolling()
  tick = setInterval(() => {
    now.value = Date.now()
    // TTL 归零走同一条刷新路径,不等用户点
    if (store.pending.some((p) => ttlOf(p.expires_at, p.remaining_ttl_s) === 0)) void store.reloadPending()
  }, 1000)
})
onUnmounted(() => {
  store.stopPolling()
  if (tick) clearInterval(tick)
})
</script>

<template>
  <div class="qt-page qt-stack">
    <div v-if="!enabled" class="qt-card box">
      <p :data-testid="T.disabledHint">邮件摆渡未启用。</p>
      <a-button type="primary" :data-testid="T.disabledGotoSettings" @click="router.push('/set')">去设置开启</a-button>
    </div>

    <template v-else>
      <div v-if="banner" class="banner" :data-testid="T.errorBanner" @click="bannerJump(banner.code, banner.subject)">
        {{ banner.title || ALERT_CODES[banner.code]?.zh || banner.code }} —— {{ banner.message }}
      </div>
      <div v-if="!settings.requireSignature" class="banner crit" :data-testid="T.testmodeBanner">
        测试模式:不验签,仅允许内网白名单发件人
      </div>

      <div class="qt-row">
        <span class="qt-grow" />
        <a-button :data-testid="T.refresh" :loading="store.loading" @click="store.reloadAll()">刷新</a-button>
      </div>

      <PageState :loading="store.loading && !store.status" :error="store.error" @retry="store.reloadAll()">
        <!-- ① 待确认的危险指令 -->
        <section class="qt-card box" :data-testid="T.block('danger')">
          <div class="qt-section-title">待确认的危险指令</div>
          <p class="qt-warn qt-small" :data-testid="T.dangerNote">
            危险操作只能在本机控制台确认,不能回邮件确认。
          </p>
          <table v-if="store.pending.length" class="tbl" :data-testid="T.dangerList">
            <thead>
              <tr><th>收到时间</th><th>发件人</th><th>操作</th><th>args 摘要</th><th>目标账号</th><th>剩余</th><th>动作</th></tr>
            </thead>
            <tbody>
              <tr v-for="(p, i) in store.pending" :key="p.id" :data-testid="T.dangerRow(i)">
                <td class="qt-small">{{ p.created_at }}</td>
                <td>{{ p.from_addr }}</td>
                <td>{{ capabilityText(p.op) }} <span class="qt-mono qt-small">{{ p.op }}</span></td>
                <td class="qt-mono qt-small">{{ p.args_digest }}</td>
                <td>{{ p.account_id ?? '—' }}</td>
                <td class="qt-mono" :data-testid="T.dangerRowTtl(i)">{{ ttlOf(p.expires_at, p.remaining_ttl_s) }} s</td>
                <td class="qt-row">
                  <a-popconfirm
                    title="该操作由邮件远程发起,确认后立即执行"
                    ok-type="danger"
                    @confirm="approve(p.id)"
                  >
                    <a-button size="small" danger :data-testid="T.dangerRowApprove(i)">确认执行</a-button>
                  </a-popconfirm>
                  <a-button size="small" :data-testid="T.dangerRowReject(i)" @click="reject(p.id)">拒绝</a-button>
                </td>
              </tr>
            </tbody>
          </table>
          <p v-else class="qt-muted">无待确认的危险指令</p>
        </section>

        <!-- ② 水位与健康 -->
        <section class="qt-card box" :data-testid="T.block('health')">
          <div class="qt-section-title">水位与健康</div>
          <div :data-testid="T.healthCard">
            <div
              v-for="r in store.status?.routes ?? []"
              :key="r.route_id"
              class="route"
              :data-testid="T.healthRoute(r.scope)"
            >
              <div class="qt-row">
                <strong class="qt-grow">{{ r.scope }}</strong>
                <span :data-testid="T.healthRouteItem(r.scope, 'proto')">
                  配置 {{ r.inbound.protocol_configured }} / 当前生效 <b>{{ r.inbound.protocol_active }}</b>
                </span>
              </div>
              <div
                v-if="r.inbound.protocol_configured === 'imap' && r.inbound.protocol_active === 'pop3'"
                class="fallback"
                :data-testid="T.healthProtoFallback"
              >
                IMAP 不可用,已回落 POP3:{{ r.inbound.fallback?.reason ?? '未知原因' }}
                <a-button size="small" @click="retryImap(r.route_id)">重试 IMAP</a-button>
              </div>
              <div v-if="r.inbound.protocol_active === 'pop3'" class="qt-small qt-warn" :data-testid="T.healthJunkHint">
                Junk 不可见,被反垃圾拦下的指令邮件会漏
              </div>
              <div class="qt-row qt-small qt-muted items">
                <span :data-testid="T.health(MAIL_HEALTH_ITEMS[1])">
                  水位 {{ r.inbound.folders.map((f) => `${f.name}:${f.last_uid}`).join(' ') || '—' }}
                </span>
                <span :data-testid="T.health(MAIL_HEALTH_ITEMS[2])">最近成功 {{ r.inbound.last_success_at ?? '—' }}</span>
                <span :data-testid="T.health(MAIL_HEALTH_ITEMS[3])">IDLE {{ r.inbound.idle_supported ? '✔' : '—' }}</span>
                <span :data-testid="T.health(MAIL_HEALTH_ITEMS[4])">连续失败 {{ r.inbound.consecutive_failures }}</span>
                <span :data-testid="T.health(MAIL_HEALTH_ITEMS[5])">
                  容量 {{ r.inbound.quota.used_mb }}/{{ r.inbound.quota.limit_mb }} MB
                  <span v-if="r.inbound.quota.source === 'estimate'">(估算)</span>
                </span>
                <span :data-testid="T.health(MAIL_HEALTH_ITEMS[6])">
                  发件 队列 {{ r.outbound.queued }} / 重试 {{ r.outbound.retrying }} / 死信 {{ r.outbound.dead }}
                </span>
                <span :data-testid="T.health(MAIL_HEALTH_ITEMS[7])">
                  清理 {{ r.cleanup.last_run_at ?? '—' }} · 下次 {{ r.cleanup.next_run_at ?? '—' }}
                </span>
              </div>
            </div>
            <a-empty v-if="!(store.status?.routes ?? []).length" description="未配置任何邮件路由" />
          </div>
        </section>

        <!-- ③ 收件时间线 -->
        <section class="qt-card box" :data-testid="T.block('inbox')">
          <div class="qt-section-title">收件时间线</div>
          <div class="qt-row filters">
            <a-input class="w140" :data-testid="T.inboxFilter('route')" placeholder="route"
                     :value="store.inboxFilter.route as string"
                     @change="(e: any) => store.inboxFilter.route = e.target.value" />
            <a-input class="w140" :data-testid="T.inboxFilter('status')" placeholder="状态"
                     :value="store.inboxFilter.status as string"
                     @change="(e: any) => store.inboxFilter.status = e.target.value" />
            <a-input class="w160" :data-testid="T.inboxFilter('since')" placeholder="起(ISO)"
                     :value="store.inboxFilter.since as string"
                     @change="(e: any) => store.inboxFilter.since = e.target.value" />
            <a-input class="w160" :data-testid="T.inboxFilter('until')" placeholder="止(ISO)"
                     :value="store.inboxFilter.until as string"
                     @change="(e: any) => store.inboxFilter.until = e.target.value" />
            <a-input class="w140" :data-testid="T.inboxFilter('q')" placeholder="关键字"
                     :value="store.inboxFilter.q as string"
                     @change="(e: any) => store.inboxFilter.q = e.target.value" />
            <a-button @click="store.reloadInbox()">筛选</a-button>
          </div>
          <table class="tbl">
            <thead><tr><th>收到</th><th>route</th><th>发件人</th><th>主题</th><th>状态</th><th>trace</th><th>动作</th></tr></thead>
            <tbody>
              <tr v-for="(r, i) in store.inbox" :key="r.id" :data-testid="T.inboxRow(i)">
                <td class="qt-small">{{ r.received_at }}</td>
                <!-- 后端给的是 scope 名(default/qidian/…),这里按 01 的中文显示名渲染 -->
                <td :data-testid="T.inboxRowRoute(i)">{{ mailRouteText(r.route) }}</td>
                <td>{{ r.from_addr }}</td>
                <td class="subj">{{ r.subject }}</td>
                <td :class="{ 'qt-danger': mailInboxStatusText(r.status, r.reason).red }">
                  {{ mailInboxStatusText(r.status, r.reason).zh }}
                  <span class="qt-mono qt-small qt-muted">{{ r.status }}</span>
                </td>
                <td>
                  <a v-if="r.trace_id" :data-testid="T.inboxRowTrace(i)"
                     @click="router.push({ path: '/log', query: { trace_id: r.trace_id } })">
                    {{ r.trace_id.slice(0, 8) }}
                  </a>
                  <span v-else>—</span>
                </td>
                <td class="qt-row">
                  <a-button size="small" :data-testid="T.inboxRowDetail(i)" @click="openDetail(r.id)">详情</a-button>
                  <a-button
                    v-if="r.status === 'PARSE_FAILED'"
                    size="small"
                    :data-testid="T.inboxRowReparse(i)"
                    @click="reparse(r.id)"
                  >重新解析</a-button>
                </td>
              </tr>
              <tr v-if="!store.inbox.length"><td colspan="7" class="qt-muted">暂无收件记录</td></tr>
            </tbody>
          </table>
          <!--
            C-42「加载更多」:`GET /mail/inbox` 已按游标翻页(后端 `received_ms` 降序);
            改筛选后点「筛选」= 回第一页并重置游标。
            🔴 元素 id 未在 01 §4 登记 ⇒ 暂不加 `data-testid`,清单已转文档方。
          -->
          <div v-if="store.inboxCursor" class="qt-row more">
            <a-button size="small" @click="store.reloadInbox(true)">加载更多</a-button>
          </div>
        </section>

        <!-- ④ 发件队列 -->
        <section class="qt-card box" :data-testid="T.block('outbox')">
          <div class="qt-section-title">发件队列</div>
          <table class="tbl">
            <!--
              列 = 01 §2.7.8 发件队列逐字 `kind/to/subject/status/attempts/next_attempt_at/last_error/ref`。
              `last_error`/`ref` 是 `#61` 出参视图(backend-api-4 §1 P-1)刚定稿下发的两键 ——
              不摆出来,DEAD 行就只剩一个状态、看不出为什么死,回执也对不回是哪封来信。
            -->
            <thead><tr><th>类型</th><th>收件人</th><th>主题</th><th>状态</th><th>重试</th><th>下次</th><th>失败原因</th><th>关联</th><th>动作</th></tr></thead>
            <tbody>
              <tr v-for="(o, i) in store.outbox" :key="o.id" :data-testid="T.outboxRow(i)">
                <td>{{ o.kind }}</td>
                <td>{{ o.to }}</td>
                <td class="subj">{{ o.subject }}</td>
                <td :class="{ 'qt-danger': o.status === 'DEAD' }">{{ o.status }}</td>
                <td>{{ o.attempts }}</td>
                <td class="qt-small">{{ o.next_attempt_at ?? '—' }}</td>
                <td class="qt-small" :class="{ 'qt-danger': !!o.last_error }">{{ o.last_error ?? '—' }}</td>
                <td class="qt-small qt-mono">{{ o.ref ?? '—' }}</td>
                <td class="qt-row">
                  <a-button size="small" :data-testid="T.outboxRowResend(i)" @click="resend(o.id)">重发</a-button>
                  <a-popconfirm title="丢弃这封邮件?" @confirm="discard(o.id)">
                    <a-button size="small" danger :data-testid="T.outboxRowDiscard(i)">丢弃</a-button>
                  </a-popconfirm>
                  <a-button
                    v-if="['RECEIPT_SENT', 'RECEIPT_SKIPPED'].includes(o.status)"
                    size="small"
                    :data-testid="T.outboxResendReceipt"
                    @click="resend(o.id)"
                  >重发回执</a-button>
                </td>
              </tr>
              <tr v-if="!store.outbox.length"><td colspan="9" class="qt-muted">发件队列为空</td></tr>
            </tbody>
          </table>
          <!-- C-42「加载更多」:`GET /mail/outbox` 同款(后端 `created_ms` 降序);id 未登记,同上 -->
          <div v-if="store.outboxCursor" class="qt-row more">
            <a-button size="small" @click="store.reloadOutbox(true)">加载更多</a-button>
          </div>
        </section>

        <!-- ⑤ 清理与归档 -->
        <section class="qt-card box" :data-testid="T.block('cleanup')">
          <div class="qt-section-title">清理与归档</div>
          <a-button
            :disabled="store.cleanupLocked(now)"
            :data-testid="T.cleanupRun"
            @click="cleanupRun"
          >立即清理</a-button>
          <table class="tbl" :data-testid="T.cleanupLog">
            <thead><tr><th>时间</th><th>删除</th><th>归档</th><th>结果</th></tr></thead>
            <tbody>
              <tr v-for="(c, i) in store.cleanupLog" :key="c.id" :data-testid="T.cleanupRow(i)">
                <td class="qt-small">{{ c.at }}</td>
                <td>{{ c.deleted }}</td>
                <td>{{ c.archived }}</td>
                <td :class="{ 'qt-danger': c.status !== 'ok' }">{{ c.status }} {{ c.error ?? '' }}</td>
              </tr>
              <tr v-if="!store.cleanupLog.length"><td colspan="4" class="qt-muted">暂无清理记录</td></tr>
            </tbody>
          </table>
        </section>
      </PageState>
    </template>

    <!-- 收件详情抽屉:原文与模板 / 解析结果 / 执行 -->
    <a-drawer v-model:open="detailOpen" title="收件详情" width="560" :data-testid="T.inboxDetailDrawer">
      <template v-if="store.inboxDetail">
        <h4>① 原文与命中的入站模板</h4>
        <div :data-testid="T.inboxDetail('template')">{{ store.inboxDetail.template_alias ?? '未命中模板' }}</div>

        <h4>② 解析结果</h4>
        <div :data-testid="T.inboxDetail('op')">
          {{ capabilityText(store.inboxDetail.parsed?.op ?? '') }}
          <span class="qt-mono qt-small">{{ store.inboxDetail.parsed?.op }}</span>
        </div>
        <table class="tbl" :data-testid="T.inboxDetail('args')">
          <tbody>
            <tr v-for="(v, k) in store.inboxDetail.parsed?.args ?? {}" :key="k">
              <td>{{ k }}</td><td class="qt-mono qt-small">{{ v }}</td>
            </tr>
          </tbody>
        </table>
        <div :data-testid="T.inboxDetail('target')">目标 {{ store.inboxDetail.parsed?.target ?? '—' }}</div>
        <div :data-testid="T.inboxDetail('reqid')" class="qt-mono qt-small">
          req_id {{ store.inboxDetail.parsed?.req_id ?? '—' }} · nonce {{ store.inboxDetail.parsed?.nonce ?? '—' }}
        </div>
        <div :data-testid="T.inboxDetail('sig')">验签 {{ store.inboxDetail.parsed?.sig_ok ? '通过' : '未通过' }}</div>

        <h4>③ 执行</h4>
        <a
          v-if="store.inboxDetail.trace_id"
          :data-testid="T.inboxDetailTrace"
          @click="router.push({ path: '/log', query: { trace_id: store.inboxDetail.trace_id } })"
        >trace {{ store.inboxDetail.trace_id }}</a>
        <div :data-testid="T.inboxDetailResult">
          {{ store.inboxDetail.result?.code ?? '—' }} · {{ store.inboxDetail.result?.cost_ms ?? '—' }} ms
        </div>
      </template>
    </a-drawer>

    <!-- args_digest_mismatch:红色结果框,不给「重试」 -->
    <a-modal v-model:open="tamperModal.open" title="已拒绝执行" :footer="null">
      <a-result status="error" :title="ARGS_DIGEST_MISMATCH_TEXT">
        <template #subTitle>
          <span class="qt-mono qt-small">trace {{ tamperModal.traceId.slice(0, 8) }} · 需人工</span>
        </template>
        <template #extra>
          <a-button @click="router.push('/log'); tamperModal.open = false">查看审计</a-button>
        </template>
      </a-result>
    </a-modal>
  </div>
</template>

<style scoped>
.box { padding: var(--qt-space-4); }
.banner { background: #FFFBE6; color: var(--qt-sev-warn); padding: 6px var(--qt-space-3); cursor: pointer; }
.banner.crit { background: #FFF1F0; color: var(--qt-state-error); }
.tbl { width: 100%; border-collapse: collapse; margin-top: var(--qt-space-2); }
.tbl th, .tbl td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--qt-border); }
.route { padding: var(--qt-space-2) 0; border-bottom: 1px solid var(--qt-border); }
.fallback { background: #FFFBE6; color: var(--qt-sev-warn); padding: 4px 8px; }
.items { flex-wrap: wrap; gap: var(--qt-space-3); }
.filters { flex-wrap: wrap; gap: var(--qt-space-2); }
.w140 { width: 140px; } .w160 { width: 160px; }
.more { justify-content: center; padding: var(--qt-space-2) 0; }
.subj { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
h4 { margin: var(--qt-space-3) 0 var(--qt-space-1); }
</style>
