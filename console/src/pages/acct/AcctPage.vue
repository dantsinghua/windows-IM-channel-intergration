<script setup lang="ts">
/**
 * `P-ACCT` 账号列表(01 §2.7.3)。按通道三组;微信组是「每个 wxid 一个 wxNN」的档案列表(C-01)。
 * 槽位 pending 倒计时 + 取消绑定(R-04/R6-6);holder 故障态与人工接管(R4-8/R5-4)。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Modal, message } from 'ant-design-vue'
import { acct as T } from '@/testids'
import { useAccountsStore } from '@/stores/accounts'
import { useResourcesStore } from '@/stores/resources'
import { useEventsStore } from '@/stores/events'
import { useSessionStore } from '@/stores/session'
import { accountsApi } from '@/api/client'
import { ApiFailure } from '@/api/http'
import StateDot from '@/components/StateDot.vue'
import PageState from '@/components/PageState.vue'
import { CHANNELS, CHANNEL_TEXT, STATE_CODES, type Channel } from '@/i18n/zh-CN/codes'
import type { Account } from '@/api/types'

/** 02 §7.1:未达阈值一律 409 slot_held_by_error;前端按同一阈值置灰 */
const SLOT_ERROR_TAKEOVER_S = 300

const router = useRouter()
const accounts = useAccountsStore()
const resources = useResourcesStore()
const events = useEventsStore()
const session = useSessionStore()

const selected = ref<Set<string>>(new Set())
const deleteTarget = ref<Account | null>(null)
const now = ref(Date.now())
const batching = ref(false)
let tick: ReturnType<typeof setInterval> | null = null

const slots = computed(() => resources.slots)
const holder = computed(() => (slots.value?.holder ? accounts.byId[slots.value.holder] ?? null : null))
const holderFaulted = computed(() => holder.value?.state === 'error' && holder.value.error_since_ms != null)
const holderErrorSeconds = computed(() => accounts.errorSeconds(holder.value, now.value) ?? 0)
/** R5-4:「够阈值才让点」,与后端 409 slot_held_by_error 对齐 */
const canForceRelease = computed(() => holderFaulted.value && holderErrorSeconds.value >= SLOT_ERROR_TAKEOVER_S)
const forceReleaseGapS = computed(() => Math.max(0, SLOT_ERROR_TAKEOVER_S - holderErrorSeconds.value))

const pendingLeft = computed(() => {
  const at = slots.value?.pending_expires_at
  if (!at) return 0
  return Math.max(0, Math.round((Date.parse(at) - now.value) / 1000))
})

function rows(ch: Channel): Account[] {
  return accounts.byChannel[ch].filter((a) => a.deleted_ms == null)
}

function runtimeSummary(a: Account): string {
  if (a.channel === 'qidian') return `adb${a.runtime?.adb_port ?? '—'} 流${a.runtime?.stream_port ?? '—'}`
  if (a.channel === 'qq') return `ws${a.runtime?.ws_port ?? '—'} http${a.runtime?.http_port ?? '—'}`
  return `${a.runtime?.wechat_version ?? '—'} ${a.runtime?.wxkey_dll ?? ''}`
}

async function act(fn: () => Promise<unknown>, okText: string): Promise<void> {
  try {
    await fn()
    message.success(okText)
    await Promise.all([accounts.load(), resources.load()])
  } catch (e) {
    if (e instanceof ApiFailure) message.error(`${e.message}(trace ${e.traceShort})`)
    else message.error(String(e))
  }
}

function toggleSelect(id: string, on: boolean): void {
  const s = new Set(selected.value)
  if (on) s.add(id)
  else s.delete(id)
  selected.value = s
}

/** 批量:启动串行(Agent 侧保证),控制台按顺序逐个调并显示队列进度 */
async function batch(action: 'start' | 'stop'): Promise<void> {
  batching.value = true
  try {
    for (const id of selected.value) {
      try {
        await (action === 'start' ? accountsApi.start(id) : accountsApi.stop(id))
      } catch (e) {
        message.error(`${id}: ${e instanceof Error ? e.message : String(e)}`)
      }
    }
    await accounts.load()
  } finally {
    batching.value = false
  }
}

/** 取消绑定:恒带 login_session_id(R6-6);stale no-op 不当错误处理 */
async function cancelPending(): Promise<void> {
  const target = slots.value?.pending ?? ''
  const lsid = resources.pendingLoginSessionId
  if (!target || !lsid) return
  Modal.confirm({
    title: '取消本次微信绑定并释放槽位',
    okType: 'danger',
    okText: '确认取消',
    cancelText: '再想想',
    onOk: async () => {
      try {
        const r = await accountsApi.loginCancel(target, lsid)
        if (r.stale) {
          // 幂等 no-op:不是错误,不弹 a-result,只 toast + 立即重拉 /resources
          message.info('该次登录尝试已结束')
        } else {
          message.success('已取消绑定,槽位已释放')
        }
      } catch (e) {
        message.error(e instanceof Error ? e.message : String(e))
      } finally {
        await resources.load()
      }
    },
  })
}

/** 强制释放并切换:带 confirm:true 调 switch,后端一事务内先停故障号再抢槽 */
function forceRelease(targetWxNN?: string): void {
  Modal.confirm({
    title: '强制释放并切换',
    okType: 'danger',
    okText: '确认接管',
    cancelText: '取消',
    content: '当前在线号处于故障,释放后需重新登录;确认后将登出该号、腾出微信槽位供切换。',
    okButtonProps: { 'data-testid': T.wechatHolderForceReleaseConfirm } as never,
    onOk: () => act(
      () => (targetWxNN ? accountsApi.switchTo(targetWxNN, true) : accountsApi.switchNew(true)),
      '已接管槽位',
    ),
  })
}

function openDelete(a: Account): void {
  deleteTarget.value = a
}

async function confirmDelete(): Promise<void> {
  const a = deleteTarget.value
  if (!a) return
  await act(() => accountsApi.softDelete(a.id, a.label), '已从列表移除(数据卷与登录态保留)')
  deleteTarget.value = null
}

function add(ch: Channel): void {
  void router.push({ path: '/acct/new', query: { ch } })
}

onMounted(() => {
  void accounts.load()
  void resources.load()
  tick = setInterval(() => { now.value = Date.now() }, 1000)
})
onUnmounted(() => { if (tick) clearInterval(tick) })
</script>

<template>
  <div class="qt-page qt-stack">
    <!-- WS 断线时列表顶部横幅 -->
    <div v-if="!events.connected" class="banner" :data-testid="T.staleBanner">
      实时状态已断开,显示的是 {{ events.disconnectedAt ? Math.round((now - events.disconnectedAt) / 1000) : 0 }} 秒前的数据
    </div>

    <PageState :loading="accounts.loading && !accounts.items.length" :error="accounts.error" @retry="accounts.load()">
      <section v-for="ch in CHANNELS" :key="ch" class="qt-card group" :data-testid="T.group(ch)">
        <header class="qt-row ghead">
          <strong class="qt-grow">
            {{ CHANNEL_TEXT[ch] }}
            <span v-if="ch !== 'wechat'">({{ accounts.summary[ch].online }} 在线 / {{ rows(ch).length }})</span>
            <template v-else>
              <span :data-testid="T.wechatSlot">
                (槽位 holder={{ slots?.holder || '—' }} · pending={{ slots?.pending || '—' }})
              </span>
            </template>
          </strong>

          <!-- 微信组头:pending 倒计时 + 取消绑定(判据是非空串) -->
          <template v-if="ch === 'wechat' && resources.hasPending">
            <span :data-testid="T.wechatPendingCountdown" class="qt-mono qt-warn">
              {{ pendingLeft > 0 ? `剩余 ${pendingLeft} 秒` : '即将释放…' }}
            </span>
            <a-button
              v-if="resources.canCancelPending"
              size="small"
              danger
              :data-testid="T.wechatPendingCancel"
              @click="cancelPending"
            >取消绑定</a-button>
          </template>

          <a-button
            v-if="ch === 'wechat'"
            size="small"
            :data-testid="T.wechatSwitch"
            :disabled="resources.hasPending"
            @click="void 0"
          >切换微信</a-button>

          <a-button
            type="primary"
            size="small"
            :data-testid="T.add(ch)"
            :disabled="ch === 'wechat' && (session.wechatDisabled || resources.hasPending)"
            @click="add(ch)"
          >+ 新增{{ CHANNEL_TEXT[ch] }}</a-button>
        </header>

        <!-- holder 故障态与人工接管入口(绝不自动释放) -->
        <div v-if="ch === 'wechat' && holderFaulted" class="fault" :data-testid="T.wechatHolderFault">
          当前在线号故障:{{ STATE_CODES[holder?.state_code ?? '']?.zh ?? holder?.state_reason ?? '未知' }}
          · 已故障 {{ accounts.errorMinutes(holder, now) ?? 0 }} 分钟
          <a-tooltip v-if="!canForceRelease" :title="`需持续故障满 ${SLOT_ERROR_TAKEOVER_S} 秒才可接管(还差 ${forceReleaseGapS} 秒)`">
            <a-button size="small" disabled :data-testid="T.wechatHolderForceRelease">强制释放并切换</a-button>
          </a-tooltip>
          <a-button
            v-else
            size="small"
            danger
            :data-testid="T.wechatHolderForceRelease"
            @click="forceRelease()"
          >强制释放并切换</a-button>
        </div>

        <div v-if="ch !== 'wechat' && selected.size" class="qt-row batchbar">
          <span class="qt-small">已选 {{ selected.size }} 个</span>
          <!-- #82 drain 期间账号动作一律 503 draining,按钮先灰 -->
          <a-button
            size="small"
            :loading="batching"
            :disabled="session.draining"
            :data-testid="T.batchStart"
            @click="batch('start')"
          >批量启动</a-button>
          <a-button
            size="small"
            :loading="batching"
            :disabled="session.draining"
            :data-testid="T.batchStop"
            @click="batch('stop')"
          >批量停止</a-button>
        </div>

        <table class="tbl">
          <thead>
            <tr>
              <th v-if="ch !== 'wechat'" />
              <th>状态</th><th>标签 / ID</th><th>昵称</th><th>运行时</th><th>配额</th><th>最近活动</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="a in rows(ch)" :key="a.id" :data-testid="T.row(a.id)">
              <td v-if="ch !== 'wechat'">
                <a-checkbox
                  :data-testid="T.batchSelect(a.id)"
                  :checked="selected.has(a.id)"
                  @change="(e: any) => toggleSelect(a.id, e.target.checked)"
                />
              </td>
              <td :data-testid="T.rowState(a.id)">
                <StateDot :state="a.state" :reason="a.state_reason" />
              </td>
              <td>
                <div>{{ a.label }}</div>
                <div class="qt-mono qt-small qt-muted">{{ a.id }}{{ a.wxid ? ` · ${a.wxid}` : '' }}</div>
              </td>
              <td>{{ a.self_nick || '—' }}</td>
              <td class="qt-mono qt-small">{{ runtimeSummary(a) }}</td>
              <td>{{ a.quota_mb ? `${(a.quota_mb / 1024).toFixed(1)}G` : '—' }}</td>
              <td class="qt-small">{{ a.last_seen_at?.slice(5, 16) ?? '—' }}</td>
              <td class="ops">
                <a-button
                  v-if="accounts.ops(a).start"
                  size="small"
                  :data-testid="T.rowStart(a.id)"
                  @click="act(() => accountsApi.start(a.id), '已请求启动')"
                >{{ a.state === 'disabled' ? '启用' : a.state === 'error' ? '重试' : '启动' }}</a-button>
                <a-button
                  v-if="accounts.ops(a).stop && !(ch === 'wechat' && a.id === slots?.holder)"
                  size="small"
                  :data-testid="T.rowStop(a.id)"
                  @click="act(() => accountsApi.stop(a.id), '已请求停止')"
                >停止</a-button>
                <a-button
                  v-if="ch === 'wechat' && a.id === slots?.holder"
                  size="small"
                  :data-testid="T.rowLogout(a.id)"
                  @click="act(() => accountsApi.stop(a.id), '已请求登出')"
                >登出</a-button>
                <a-button
                  v-if="accounts.ops(a).restart"
                  size="small"
                  :data-testid="T.rowRestart(a.id)"
                  @click="act(() => accountsApi.restart(a.id), '已请求重启')"
                >重启</a-button>
                <a-button
                  v-if="ch !== 'qq' && accounts.ops(a).screen"
                  size="small"
                  :type="a.state === 'login_required' ? 'primary' : 'default'"
                  :data-testid="T.rowScreen(a.id)"
                  @click="router.push(`/screen/${a.id}`)"
                >画面</a-button>
                <a-button
                  v-if="ch === 'wechat' && (a.state === 'stopped')"
                  size="small"
                  :disabled="resources.hasPending"
                  :data-testid="T.rowSwitch(a.id)"
                  @click="holderFaulted ? forceRelease(a.id) : act(() => accountsApi.switchTo(a.id), '已发起切换')"
                >切换到此账号</a-button>
                <a-button
                  v-if="ch === 'wechat' && a.state === 'stopped' && a.wxid"
                  size="small"
                  :disabled="resources.hasPending"
                  :data-testid="T.wechatHistorySwitch(a.wxid)"
                  @click="act(() => accountsApi.switchTo(a.id), '已发起切换')"
                >按 wxid 切换</a-button>
                <a-button size="small" :data-testid="T.rowDetail(a.id)" @click="router.push(`/acct/${a.id}`)">详情</a-button>
                <a-dropdown>
                  <a-button size="small" :data-testid="T.rowMore(a.id)">⋯</a-button>
                  <template #overlay>
                    <a-menu>
                      <a-menu-item v-if="a.enabled" :data-testid="T.rowDisable(a.id)" @click="act(() => accountsApi.disable(a.id), '已停用')">停用</a-menu-item>
                      <a-menu-item v-else :data-testid="T.rowEnable(a.id)" @click="act(() => accountsApi.enable(a.id), '已启用')">启用</a-menu-item>
                      <a-menu-item
                        v-if="accounts.ops(a).del && !(ch === 'wechat' && a.id === slots?.holder)"
                        :data-testid="T.rowDelete(a.id)"
                        @click="openDelete(a)"
                      >删除</a-menu-item>
                    </a-menu>
                  </template>
                </a-dropdown>
              </td>
            </tr>
            <tr v-if="!rows(ch).length">
              <td :colspan="ch === 'wechat' ? 7 : 8">
                <a-empty :description="`还没有${CHANNEL_TEXT[ch]}账号`">
                  <a-button type="primary" :data-testid="T.emptyAdd(ch)" @click="add(ch)">新增{{ CHANNEL_TEXT[ch] }}</a-button>
                </a-empty>
              </td>
            </tr>
          </tbody>
        </table>
      </section>
    </PageState>

    <!-- 删除 = 软删,无 keep_data(R-11) -->
    <a-modal
      :open="!!deleteTarget"
      title="停用并从列表移除"
      :data-testid="T.deleteModal"
      @cancel="deleteTarget = null"
    >
      <p>停用并从列表移除;<b>数据卷与登录态保留</b>,account_id 永不复用。</p>
      <p class="qt-small qt-muted">要清空数据卷与登录态,请到账号详情页的「彻底删除数据」。</p>
      <template #footer>
        <a-button :data-testid="T.deleteCancel" @click="deleteTarget = null">取消</a-button>
        <a-button type="primary" danger :data-testid="T.deleteConfirm" @click="confirmDelete">确认删除</a-button>
      </template>
    </a-modal>
  </div>
</template>

<style scoped>
.banner { background: #FFFBE6; color: var(--qt-sev-warn); padding: 6px var(--qt-space-3); border-radius: var(--qt-radius-sm); }
.group { padding: var(--qt-space-3); }
.ghead { margin-bottom: var(--qt-space-2); }
.fault { background: #FFF1F0; color: var(--qt-state-error); padding: 6px var(--qt-space-3); display: flex; align-items: center; gap: var(--qt-space-2); }
.batchbar { margin-bottom: var(--qt-space-2); }
.tbl { width: 100%; border-collapse: collapse; }
.tbl th, .tbl td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--qt-border); vertical-align: middle; }
.ops { display: flex; gap: 4px; flex-wrap: wrap; }
</style>
