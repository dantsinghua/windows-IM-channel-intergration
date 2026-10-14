<script setup lang="ts">
/** 告警行:severity 色 + title/message(Agent 下发)+ `hint_actions` 逐项落按钮(C-14) */
import { computed } from 'vue'
import type { AlertPayload } from '@/api/types'
import { HINT_ACTIONS, SEVERITY_TEXT, ALERT_CODES } from '@/i18n/zh-CN/codes'

const props = defineProps<{ alert: AlertPayload; testid?: string; actionTestid?: (act: string) => string }>()
const emit = defineEmits<{ (e: 'action', act: string): void; (e: 'open'): void }>()

const tone = computed(() => `var(--qt-sev-${props.alert.severity})`)
/** 未知动作只显示不落按钮 */
const actions = computed(() => (props.alert.hint_actions ?? []).filter((a) => a in HINT_ACTIONS))
const unknownActions = computed(() => (props.alert.hint_actions ?? []).filter((a) => !(a in HINT_ACTIONS)))
/** 常驻码有自己的固定文案(N-25);其余由 Agent 的 title/message 承担 */
const fallbackZh = computed(() => ALERT_CODES[props.alert.code]?.zh ?? '')
const driftEvidence = computed(() => {
  if (props.alert.code !== 'POOL_CALIBRATION_DRIFT') return []
  const labels: Record<string, string> = { budget_mb: '预算 MB', actual_mb: '实占 P95 MB', drift_pct: '偏差 %', threshold_pct: '阈值 %' }
  return Object.entries(labels).flatMap(([key, label]) => {
    const value = props.alert.evidence?.[key]
    return typeof value === 'number' && Number.isFinite(value) ? [`${label}: ${value}`] : []
  })
})
</script>

<template>
  <div class="alert" :data-testid="testid" :style="{ '--c': tone }">
    <div class="qt-row">
      <span class="sev">{{ SEVERITY_TEXT[alert.severity] }}</span>
      <a class="title qt-grow" @click="emit('open')">{{ alert.title || fallbackZh || alert.code }}</a>
      <span v-if="alert.count > 1" class="qt-small qt-muted">仍在持续 ×{{ alert.count }}</span>
      <span v-if="alert.state === 'resolved'" class="qt-small qt-muted">已恢复</span>
    </div>
    <div class="qt-small qt-muted">{{ alert.message || fallbackZh }}</div>
    <div class="qt-small qt-muted qt-mono">{{ alert.code }} · {{ alert.subject }}</div>
    <div v-if="alert.first_seen_at || alert.last_seen_at" class="qt-small qt-muted">
      <span v-if="alert.first_seen_at">首次发生: <time :datetime="alert.first_seen_at">{{ alert.first_seen_at }}</time></span>
      <span v-if="alert.last_seen_at"> · {{ alert.state === 'resolved' ? '恢复时间' : '最近观测' }}: <time :datetime="alert.last_seen_at">{{ alert.last_seen_at }}</time></span>
    </div>
    <div v-if="driftEvidence.length" class="qt-small qt-muted">校准依据: {{ driftEvidence.join(' · ') }}</div>
    <div v-if="actions.length" class="qt-row">
      <a-button
        v-for="act in actions"
        :key="act"
        size="small"
        :data-testid="actionTestid ? actionTestid(act) : undefined"
        @click="emit('action', act)"
      >{{ HINT_ACTIONS[act] }}</a-button>
    </div>
    <div v-if="unknownActions.length" class="qt-small qt-muted">其它建议动作:{{ unknownActions.join('、') }}</div>
  </div>
</template>

<style scoped>
.alert { border-left: 3px solid var(--c); padding: var(--qt-space-2) var(--qt-space-3); }
.sev { color: var(--c); font-size: var(--qt-font-xs); border: 1px solid var(--c); border-radius: 8px; padding: 0 6px; }
.title { cursor: pointer; }
</style>
