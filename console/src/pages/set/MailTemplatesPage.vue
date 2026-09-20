<script setup lang="ts">
/**
 * 邮件模板子页(E-4,路由 `/set/mail-templates`,仍属 `P-SET`)。
 * 🔴 R-13 砍 custom:`compat_profile` 只有 `ibquote-163-v1` / `collector-v1` 两个预设,
 * 主题 pattern 与正文字段**锁定为兼容默认值、只读展示**;占位符收敛到 12 个核心。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { set as T } from '@/testids'
import { useSettingsStore } from '@/stores/settings'
import { useMessagesStore } from '@/stores/messages'
import { settingsApi } from '@/api/client'
import type { MailTemplate } from '@/api/types'

/** 12 个核心占位符(基线 §11.22 [SCOPE]) */
const PLACEHOLDERS: { key: string; zh: string }[] = [
  { key: '{channel}', zh: '渠道' },
  { key: '{account_id}', zh: '账号' },
  { key: '{session_name}', zh: '会话名' },
  { key: '{session_kind}', zh: '会话类型' },
  { key: '{sender_name}', zh: '发送方' },
  { key: '{ts}', zh: '时间' },
  { key: '{msg_type}', zh: '类型' },
  { key: '{text}', zh: '正文摘要' },
  { key: '{media_count}', zh: '媒体数' },
  { key: '{ext_msg_id}', zh: '消息ID' },
  { key: '{fingerprint}', zh: '指纹' },
  { key: '{seq}', zh: '序号' },
]

const PRESETS = ['ibquote-163-v1', 'collector-v1'] as const

/** 预设锁定的主题与正文字段(只读展示,保证与下游解析器逐字兼容) */
const PRESET_LOCK: Record<string, { subject: string; body: string[] }> = {
  'ibquote-163-v1': {
    subject: '[QTrade] {channel} {session_name} {ts}',
    body: ['{channel}', '{session_name}', '{sender_name}', '{ts}', '{msg_type}', '{text}', '{ext_msg_id}'],
  },
  'collector-v1': {
    subject: '[collector] {account_id}/{session_name} #{seq}',
    body: ['{account_id}', '{session_kind}', '{session_name}', '{sender_name}', '{ts}', '{text}', '{media_count}', '{fingerprint}'],
  },
}

const router = useRouter()
const store = useSettingsStore()
const messages = useMessagesStore()

const selectedId = ref<string | null>(null)
const profile = ref<(typeof PRESETS)[number]>('ibquote-163-v1')
const previewSampleId = ref<string | undefined>()
const preview = ref<{ subject: string; body_text: string } | null>(null)
const inboundAliases = ref<{ alias: string; rule: string }[]>([{ alias: 'qtrade-cmd', rule: 'qtrade-cmd-v1' }])

const current = computed<MailTemplate | null>(() =>
  store.mailTemplates.find((t) => t.template_id === selectedId.value) ?? null)
const lock = computed(() => PRESET_LOCK[profile.value])

function pick(t: MailTemplate): void {
  selectedId.value = t.template_id
  profile.value = (PRESETS as readonly string[]).includes(t.compat_profile) ? t.compat_profile : 'ibquote-163-v1'
}

async function reload(): Promise<void> {
  store.mailTemplates = (await settingsApi.mailTemplates()).items
  if (!selectedId.value && store.mailTemplates[0]) pick(store.mailTemplates[0])
}

async function createTpl(copyFrom?: MailTemplate): Promise<void> {
  const t = await settingsApi.saveMailTemplate({
    name: copyFrom ? `${copyFrom.name} 副本` : `模板-${Date.now()}`,
    kind: 'outbound',
    compat_profile: copyFrom?.compat_profile ?? 'ibquote-163-v1',
    subject_pattern: PRESET_LOCK[copyFrom?.compat_profile ?? 'ibquote-163-v1'].subject,
    body_fields: PRESET_LOCK[copyFrom?.compat_profile ?? 'ibquote-163-v1'].body
      .map((k, i) => ({ key: k, label: k, order: i, required: true })),
  })
  await reload()
  pick(t)
}

async function removeTpl(): Promise<void> {
  if (!current.value) return
  if (current.value.referenced_by?.length) { message.error('该模板被路由引用,不可删'); return }
  await settingsApi.deleteMailTemplate(current.value.template_id)
  selectedId.value = null
  await reload()
}

async function save(): Promise<void> {
  if (!current.value) return
  await settingsApi.saveMailTemplate({
    ...current.value,
    compat_profile: profile.value,
    subject_pattern: lock.value.subject,
    body_fields: lock.value.body.map((k, i) => ({ key: k, label: k, order: i, required: true })),
  })
  message.success('已保存模板')
  await reload()
}

async function setRoute(): Promise<void> {
  if (!current.value) return
  message.info(`已把 ${current.value.template_id} 设为选定路由的模板(在设置页邮箱块生效)`)
}

async function runPreview(): Promise<void> {
  if (!current.value || !previewSampleId.value) return
  const r = await settingsApi.previewMailTemplate(current.value.template_id, previewSampleId.value)
  preview.value = { subject: r.subject, body_text: r.body_text }
}

onMounted(async () => {
  await reload()
  if (!messages.items.length) await messages.search(true).catch(() => undefined)
})
</script>

<template>
  <div class="qt-page tplgrid">
    <section class="qt-card box">
      <div class="qt-row">
        <div class="qt-section-title qt-grow">出站模板</div>
        <a-button size="small" :data-testid="T.mailtplNew" @click="createTpl()">新建</a-button>
        <a-button size="small" :data-testid="T.mailtplCopy" :disabled="!current" @click="createTpl(current!)">复制</a-button>
        <a-button size="small" danger :data-testid="T.mailtplDelete"
                  :disabled="!current || !!current.referenced_by?.length" @click="removeTpl">删除</a-button>
      </div>
      <div :data-testid="T.mailtplList">
        <div
          v-for="t in store.mailTemplates"
          :key="t.template_id"
          class="titem"
          :class="{ active: t.template_id === selectedId }"
          :data-testid="T.mailtplRow(t.template_id)"
          @click="pick(t)"
        >
          <div class="qt-row">
            <span class="qt-grow">{{ t.name }}</span>
            <span class="qt-small qt-muted">{{ t.compat_profile }}</span>
          </div>
          <div class="qt-small qt-muted">
            被引用:{{ (t.referenced_by ?? []).join('、') || '无' }} · v{{ t.version }} · {{ t.updated_at }}
          </div>
          <a-button size="small" :data-testid="T.mailtplRowSetRoute(t.template_id)" @click.stop="setRoute">
            设为某路由的模板
          </a-button>
        </div>
        <a-empty v-if="!store.mailTemplates.length" description="暂无模板" />
      </div>
    </section>

    <section class="qt-card box">
      <div class="qt-section-title">预设(只读展示)</div>
      <a-radio-group v-model:value="profile">
        <a-radio v-for="p in PRESETS" :key="p" :value="p" :data-testid="T.mailtplCompat(p)">{{ p }}</a-radio>
      </a-radio-group>
      <p class="qt-small qt-muted">
        只有两个预设(R-13 删掉 custom 自定义模板 UI);主题 pattern 与正文字段锁定为兼容默认值,不接受编辑。
      </p>

      <div class="qt-section-title mt">主题 pattern</div>
      <div class="ro qt-mono" :data-testid="T.mailtplSubject">{{ lock.subject }}</div>

      <div class="qt-section-title mt">占位符(12 核心)</div>
      <table class="tbl" :data-testid="T.mailtplPhList">
        <tbody>
          <tr v-for="p in PLACEHOLDERS" :key="p.key">
            <td class="qt-mono">{{ p.key }}</td><td>{{ p.zh }}</td>
          </tr>
        </tbody>
      </table>

      <div class="qt-section-title mt">正文字段(有序,只读)</div>
      <ol :data-testid="T.mailtplBodyList">
        <li v-for="(b, i) in lock.body" :key="b" class="qt-mono" :data-testid="T.mailtplBodyRow(i)">{{ b }}</li>
      </ol>

      <div class="qt-section-title mt">入站模板别名</div>
      <table class="tbl" :data-testid="T.mailtplInboundTable">
        <thead><tr><th>alias</th><th>解析规则版本</th><th /></tr></thead>
        <tbody>
          <tr v-for="a in inboundAliases" :key="a.alias" :data-testid="T.mailtplInboundRow(a.alias)">
            <td class="qt-mono">{{ a.alias }}</td>
            <td class="qt-mono">{{ a.rule }}</td>
            <td>
              <a-button size="small" danger :data-testid="T.mailtplInboundRowRemove(a.alias)"
                        @click="inboundAliases = inboundAliases.filter((x) => x.alias !== a.alias)">删除</a-button>
            </td>
          </tr>
        </tbody>
      </table>
      <a-button size="small" :data-testid="T.mailtplInboundAdd"
                @click="inboundAliases.push({ alias: `alias-${inboundAliases.length + 1}`, rule: 'qtrade-cmd-v1' })">
        新增别名
      </a-button>

      <div class="qt-row mt">
        <a-button type="primary" :data-testid="T.mailtplSave" :disabled="!current" @click="save">保存</a-button>
        <a-button :data-testid="T.mailtplBack" @click="router.push('/set')">返回设置</a-button>
      </div>
    </section>

    <section class="qt-card box">
      <div class="qt-section-title">预览(不落盘不发信)</div>
      <div class="qt-row">
        <a-select
          class="w260"
          :data-testid="T.mailtplPreviewPick"
          v-model:value="previewSampleId"
          placeholder="从 P-MSG 同一检索选一条真实消息"
          :options="messages.items.slice(0, 50).map((m) => ({ value: m.id, label: `${m.ts.slice(5, 16)} ${m.session.name} ${m.text ?? m.type}` }))"
        />
        <a-button :data-testid="T.mailtplPreview" :disabled="!previewSampleId || !current" @click="runPreview">预览</a-button>
      </div>
      <div class="qt-section-title mt">主题</div>
      <div class="ro" :data-testid="T.mailtplPreviewSubject">{{ preview?.subject ?? '—' }}</div>
      <div class="qt-section-title mt">正文</div>
      <pre class="ro qt-mono" :data-testid="T.mailtplPreviewBody">{{ preview?.body_text ?? '—' }}</pre>
    </section>
  </div>
</template>

<style scoped>
.tplgrid { display: grid; grid-template-columns: 280px 1fr 1fr; gap: var(--qt-space-4); align-items: start; }
.box { padding: var(--qt-space-4); }
.titem { padding: var(--qt-space-2); border-bottom: 1px solid var(--qt-border); cursor: pointer; }
.titem.active { background: var(--qt-bg); }
.ro { background: var(--qt-bg); border: 1px solid var(--qt-border); padding: 6px 8px; border-radius: var(--qt-radius-sm); white-space: pre-wrap; }
.tbl { width: 100%; border-collapse: collapse; }
.tbl th, .tbl td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--qt-border); }
.mt { margin-top: var(--qt-space-3); }
.w260 { width: 260px; }
</style>
