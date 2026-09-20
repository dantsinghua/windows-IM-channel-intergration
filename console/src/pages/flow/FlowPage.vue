<script setup lang="ts">
/**
 * `P-FLOW` 工作流(01 §2.7.6)。
 * M5 只做**直线流程**(R-13):固定步骤顺序 + 失败即停 + 每步留痕;
 * 不做条件分支/循环/变量/表达式求值的任何 UI。YAML 只读。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { flow as T } from '@/testids'
import { useWorkflowsStore } from '@/stores/workflows'
import { useAccountsStore } from '@/stores/accounts'
import { workflowsApi } from '@/api/client'
import PageState from '@/components/PageState.vue'
import ResultCodeTag from '@/components/ResultCodeTag.vue'
import { WORKFLOW_STATUS_TEXT, capabilityText } from '@/i18n/zh-CN/codes'

const router = useRouter()
const store = useWorkflowsStore()
const accounts = useAccountsStore()

const runFormOpen = ref(false)
const yamlOpen = ref(false)
const yamlText = ref('')
const pickedWf = ref<string | null>(null)
const runAccounts = ref<string[]>([])
const runInputs = ref<Record<string, unknown>>({})
const busy = ref(false)

const run = computed(() => store.currentRun)
const paused = computed(() => run.value?.status === 'paused' || run.value?.status?.startsWith('paused'))

function openRunForm(wf: string): void {
  pickedWf.value = wf
  runAccounts.value = []
  runInputs.value = {}
  runFormOpen.value = true
}

async function openYaml(wf: string): Promise<void> {
  const d = await workflowsApi.get(wf)
  yamlText.value = d.yaml ?? '(该工作流未返回 YAML)'
  yamlOpen.value = true
}

async function submitRun(): Promise<void> {
  if (!pickedWf.value) return
  busy.value = true
  try {
    const r = await workflowsApi.run(pickedWf.value, { args: runInputs.value, account_ids: runAccounts.value })
    runFormOpen.value = false
    await store.loadRun(r.run_id)
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    busy.value = false
  }
}

async function cancelRun(): Promise<void> {
  if (!run.value) return
  await workflowsApi.cancelRun(run.value.run_id)
  await store.loadRun(run.value.run_id)
}

const inputDefs = computed(() => store.items.find((w) => w.id === pickedWf.value)?.inputs ?? [])

onMounted(() => {
  void store.load()
  if (!accounts.items.length) void accounts.load()
})
</script>

<template>
  <div class="qt-page qt-stack">
    <PageState
      :loading="store.loading && !store.items.length"
      :error="store.error"
      :empty="!store.loading && !store.items.length"
      empty-text="暂无工作流;可通过 API POST /workflows 导入 YAML 定义"
      @retry="store.load()"
    >
      <section class="qt-card box">
        <div class="qt-section-title">工作流</div>
        <table class="tbl" :data-testid="T.list">
          <thead><tr><th>名称</th><th>版本</th><th>步骤数</th><th>最近运行</th><th>调度</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="w in store.items" :key="w.id" :data-testid="T.row(w.id)">
              <td>{{ w.name }}</td>
              <td>v{{ w.version }}</td>
              <td>{{ w.steps }}</td>
              <td class="qt-small">{{ w.last_run_at ?? '—' }}</td>
              <td>{{ w.enabled ? (w.schedule_cron ?? '手动') : '已停用' }}</td>
              <td class="qt-row">
                <a-button size="small" type="primary" :data-testid="T.rowRun(w.id)" @click="openRunForm(w.id)">运行</a-button>
                <a-button size="small" :data-testid="T.rowYaml(w.id)" @click="openYaml(w.id)">查看 YAML</a-button>
              </td>
            </tr>
          </tbody>
        </table>
      </section>
    </PageState>

    <!-- 运行详情:线性时间线,没有分支/条件/跳转控件 -->
    <section v-if="run" class="qt-card box" :data-testid="T.run(run.run_id)">
      <div class="qt-row">
        <strong class="qt-grow">{{ run.workflow }} · {{ run.run_id }}</strong>
        <span>{{ WORKFLOW_STATUS_TEXT[run.status] ?? run.status }}</span>
        <a-button
          v-if="!['finished', 'failed', 'cancelled'].includes(run.status)"
          size="small"
          danger
          :data-testid="T.runCancel"
          @click="cancelRun"
        >取消</a-button>
      </div>
      <p v-if="paused" class="qt-warn" :data-testid="T.runPausedHint">
        已挂起:绑定账号已切换,需人工决定续跑或取消
      </p>
      <ol class="timeline">
        <li v-for="s in run.steps" :key="s.step_id" :data-testid="T.runStep(run.run_id, s.step_id)">
          <div class="qt-row">
            <strong class="qt-grow">{{ s.name }} · {{ capabilityText(s.op) }}</strong>
            <ResultCodeTag v-if="s.code" :code="s.code" />
            <span class="qt-small qt-muted">{{ s.cost_ms ?? '—' }} ms</span>
          </div>
          <div class="qt-small qt-muted">
            {{ s.args_digest ?? '' }} · {{ s.state_before ?? '—' }} → {{ s.state_after ?? '—' }} ·
            {{ WORKFLOW_STATUS_TEXT[s.status] ?? s.status }}
          </div>
          <img v-if="s.shot" class="shot" :data-testid="T.stepShot(s.step_id)" :src="`/api/v1/media/${s.shot}`" alt="步骤截图" />
          <a-button
            v-if="s.needs_human"
            size="small"
            :data-testid="T.stepGohuman(s.step_id)"
            @click="router.push(`/screen/${run!.account_ids[0] ?? ''}`)"
          >去画面</a-button>
        </li>
      </ol>
      <p class="qt-small qt-muted">失败即停:某步失败则整条流程停在该步,不做自动跳过或分支决策。</p>
    </section>

    <a-modal v-model:open="runFormOpen" title="运行工作流" :footer="null">
      <a-form layout="vertical">
        <a-form-item label="选择账号(可多选)">
          <a-select
            v-model:value="runAccounts"
            mode="multiple"
            :data-testid="T.runAccounts"
            :options="accounts.items.map((a) => ({ value: a.id, label: `${a.id} ${a.label}` }))"
          />
        </a-form-item>
        <a-form-item label="参数" :data-testid="T.runInputs">
          <div v-for="i in inputDefs" :key="i.key" class="qt-row">
            <span class="klabel">{{ i.label }}{{ i.required ? ' *' : '' }}</span>
            <a-input
              class="qt-grow"
              :value="runInputs[i.key] as string"
              @change="(e: any) => runInputs[i.key] = e.target.value"
            />
          </div>
          <span v-if="!inputDefs.length" class="qt-muted qt-small">该工作流无输入参数</span>
        </a-form-item>
        <a-button type="primary" :loading="busy" :data-testid="T.runSubmit" @click="submitRun">开始运行</a-button>
      </a-form>
    </a-modal>

    <a-modal v-model:open="yamlOpen" title="工作流定义(只读)" :footer="null" width="720px">
      <pre class="yaml qt-mono">{{ yamlText }}</pre>
      <p class="qt-small qt-muted">
        编辑器不进 v1:导入走 POST /workflows;即便 YAML 里写了分支/循环,本页也只线性展示已执行到的步骤。
      </p>
    </a-modal>
  </div>
</template>

<style scoped>
.box { padding: var(--qt-space-4); }
.tbl { width: 100%; border-collapse: collapse; }
.tbl th, .tbl td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--qt-border); }
.timeline { list-style: none; padding-left: 0; }
.timeline li { border-left: 2px solid var(--qt-border); padding: var(--qt-space-2) var(--qt-space-3); margin-left: 8px; }
.shot { max-width: 200px; display: block; margin: var(--qt-space-2) 0; border: 1px solid var(--qt-border); }
.yaml { max-height: 420px; overflow: auto; background: var(--qt-bg); padding: var(--qt-space-3); }
.klabel { width: 120px; }
</style>
