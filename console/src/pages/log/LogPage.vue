<script setup lang="ts">
/**
 * `P-LOG` 日志 / 审计(01 §2.7.11)。三个 Tab 同一表格组件;
 * **不显示消息正文**(基线 §11-2),`args` 只显示摘要。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { log as T, LOG_FILTER_KEYS } from '@/testids'
import { useAuditStore, type AuditKind } from '@/stores/audit'
import { useAccountsStore } from '@/stores/accounts'
import PageState from '@/components/PageState.vue'
import { AUDIT_KIND_TEXT, capabilityText } from '@/i18n/zh-CN/codes'

const route = useRoute()
const store = useAuditStore()
const accounts = useAccountsStore()

const traceOpen = ref(false)
const traceId = ref('')

const TABS: AuditKind[] = ['command', 'api', 'system']
const filterLabel: Record<string, string> = {
  account: '账号', op: '能力', code: '结果码', actor: 'actor', since: '起(ISO)', until: '止(ISO)',
}

/** ARGS_TAMPERED 行红标 */
function isTampered(reasonOrAction?: string | null): boolean {
  return !!reasonOrAction && reasonOrAction.includes('args_mismatch')
}

async function openTrace(tid: string): Promise<void> {
  traceId.value = tid
  await store.loadTrace(tid)
  traceOpen.value = true
}

async function exportCsv(): Promise<void> {
  const head = ['ts', 'kind', 'account_id', 'op', 'action', 'actor', 'code', 'cost_ms', 'trace_id']
  const lines = [head.join(',')]
  for (const r of store.rows) {
    lines.push(head.map((k) => JSON.stringify((r as unknown as Record<string, unknown>)[k] ?? '')).join(','))
  }
  await window.qt?.files.saveAs(`audit-${store.kind}-${Date.now()}.csv`, 'text/csv', lines.join('\n'))
}

const columns = computed(() => {
  if (store.kind === 'api') return ['时间', '来源 IP', 'app_id', '方法+路径', 'HTTP', '签名']
  if (store.kind === 'system') return ['时间', '来源', '动作', '对象', '结果']
  return ['时间', '账号', '能力', '来源', '结果码', '耗时', 'trace']
})

watch(() => store.kind, () => void store.load())

onMounted(async () => {
  if (!accounts.items.length) void accounts.load()
  const tid = route.query.trace_id as string | undefined
  await store.load()
  if (tid) await openTrace(tid)
})
</script>

<template>
  <div class="qt-page qt-stack">
    <a-tabs v-model:activeKey="store.kind">
      <a-tab-pane v-for="k in TABS" :key="k">
        <template #tab><span :data-testid="T.tab(k)">{{ AUDIT_KIND_TEXT[k] }}</span></template>
      </a-tab-pane>
    </a-tabs>

    <div class="qt-row filters">
      <template v-for="f in LOG_FILTER_KEYS" :key="f">
        <a-select
          v-if="f === 'account'"
          class="w140"
          :data-testid="T.filter(f)"
          :value="store.filter.account_id"
          allow-clear
          :placeholder="filterLabel[f]"
          :options="accounts.items.map((a) => ({ value: a.id, label: a.id }))"
          @change="(v: any) => store.filter.account_id = v"
        />
        <a-input
          v-else
          class="w140"
          :data-testid="T.filter(f)"
          :placeholder="filterLabel[f]"
          :value="store.filter[f]"
          @change="(e: any) => store.filter[f] = e.target.value"
        />
      </template>
      <a-button type="primary" :data-testid="T.search" @click="store.load()">搜索</a-button>
      <a-button :data-testid="T.export" @click="exportCsv">导出 CSV</a-button>
    </div>

    <PageState
      :loading="store.loading && !store.rows.length"
      :error="store.error"
      :empty="!store.loading && !store.rows.length"
      empty-text="暂无审计记录"
      @retry="store.load()"
    >
      <table class="tbl" :data-testid="T.table">
        <thead><tr><th v-for="c in columns" :key="c">{{ c }}</th></tr></thead>
        <tbody>
          <tr
            v-for="(r, i) in store.rows"
            :key="r.id"
            :data-testid="T.row(i)"
            :class="{ tampered: isTampered(r.action) }"
          >
            <template v-if="store.kind === 'command'">
              <td class="qt-small">{{ r.ts }}</td>
              <td>{{ r.account_id ?? '—' }}</td>
              <td>{{ capabilityText(r.op ?? '') }}</td>
              <td class="qt-small">{{ r.transport ?? '' }}/{{ r.actor ?? '' }}{{ r.ip ? ` @${r.ip}` : '' }}</td>
              <td>{{ r.code ?? '—' }}</td>
              <td>{{ r.cost_ms ?? '—' }}</td>
              <td>
                <a v-if="r.trace_id" :data-testid="T.rowTrace(i)" @click="openTrace(r.trace_id!)">
                  {{ r.trace_id.slice(0, 8) }}
                </a>
              </td>
            </template>
            <template v-else-if="store.kind === 'api'">
              <td class="qt-small">{{ r.ts }}</td>
              <td>{{ r.ip ?? '—' }}</td>
              <td class="qt-mono qt-small">{{ r.actor ?? '—' }}</td>
              <td class="qt-mono qt-small">{{ r.method }} {{ r.path }}</td>
              <td>{{ r.http_status ?? '—' }}</td>
              <td>{{ r.sig_ok === null || r.sig_ok === undefined ? '—' : r.sig_ok ? '通过' : '失败' }}</td>
            </template>
            <template v-else>
              <td class="qt-small">{{ r.ts }}</td>
              <td>{{ r.source ?? 'agent' }}</td>
              <td>{{ r.action ?? '—' }}</td>
              <td>{{ r.account_id ?? '—' }}</td>
              <td>
                {{ r.code ?? '—' }}
                <a v-if="r.trace_id" :data-testid="T.rowTrace(i)" @click="openTrace(r.trace_id!)">
                  {{ r.trace_id.slice(0, 8) }}
                </a>
              </td>
            </template>
          </tr>
        </tbody>
      </table>
      <a-button v-if="store.nextCursor" :data-testid="T.loadMore" @click="store.load(true)">加载更多</a-button>
    </PageState>

    <a-drawer v-model:open="traceOpen" :title="`trace ${traceId.slice(0, 8)}`" width="560" :data-testid="T.traceDrawer">
      <div v-for="r in store.traceRows" :key="r.id" class="trow">
        <div class="qt-row">
          <span class="qt-grow qt-small">{{ r.ts }}</span>
          <span>{{ r.code ?? r.http_status ?? '' }}</span>
        </div>
        <div class="qt-small qt-muted">
          {{ r.kind }} · {{ r.action ?? r.op ?? '' }} · {{ r.actor ?? '' }}
          <span v-if="r.args_digest" class="qt-mono">args {{ r.args_digest }}</span>
        </div>
      </div>
      <a-empty v-if="!store.traceRows.length" description="该 trace 暂无记录" />
      <p class="qt-small qt-muted">审计不显示消息正文;args 只显示摘要。</p>
    </a-drawer>
  </div>
</template>

<style scoped>
.filters { flex-wrap: wrap; gap: var(--qt-space-2); }
.w140 { width: 140px; }
.tbl { width: 100%; border-collapse: collapse; }
.tbl th, .tbl td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--qt-border); }
.tampered td { color: var(--qt-state-error); }
.trow { padding: 6px 0; border-bottom: 1px solid var(--qt-border); }
</style>
