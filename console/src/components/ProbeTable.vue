<script setup lang="ts">
/** 连通性探测结果表:结论枚举 00 §8.5,建议文案由控制台 i18n 维护、可被 04 的 hint 覆盖 */
import { probeDetailOf, probeStatusOf, type ProbeRow } from '@/api/types'
import { PROBE_RESULTS, PROBE_TARGETS } from '@/i18n/zh-CN/codes'
import { env as T } from '@/testids'

defineProps<{ rows: ProbeRow[] }>()
const emit = defineEmits<{ (e: 'rerun', target: string): void }>()
</script>

<template>
  <table class="probe" :data-testid="T.probeTable">
    <thead>
      <tr><th>目标</th><th>结论</th><th>建议</th><th /></tr>
    </thead>
    <tbody>
      <!-- 结论列在后端叫 `status`(旧实现叫 `result`),诊断文字叫 `detail`;target 可能为 null(整侧被跳过) -->
      <tr v-for="(r, i) in rows" :key="r.target ?? `${r.side}-${i}`" :data-testid="T.probeRow(r.target ?? r.side ?? String(i))">
        <td>
          {{ r.target ? PROBE_TARGETS[r.target] ?? r.target : `整侧(${r.side ?? '未知'})` }}
          <span class="qt-mono qt-small qt-muted"> {{ r.target ?? '' }}</span>
        </td>
        <td>
          <span class="chip" :class="`tone-${PROBE_RESULTS[probeStatusOf(r)]?.tone ?? 'na'}`">
            {{ PROBE_RESULTS[probeStatusOf(r)]?.zh ?? probeStatusOf(r) }}
          </span>
        </td>
        <td class="qt-small">{{ probeDetailOf(r) || PROBE_RESULTS[probeStatusOf(r)]?.hint || '—' }}</td>
        <td>
          <a-button
            v-if="r.target"
            size="small"
            :data-testid="T.probeRunTarget(r.target)"
            @click="emit('rerun', r.target!)"
          >重跑</a-button>
        </td>
      </tr>
      <tr v-if="!rows.length"><td colspan="4" class="qt-muted">暂无探测结果</td></tr>
    </tbody>
  </table>
</template>

<style scoped>
.probe { width: 100%; border-collapse: collapse; }
.probe th, .probe td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--qt-border); vertical-align: top; }
.chip { padding: 0 6px; border-radius: 8px; font-size: var(--qt-font-xs); border: 1px solid currentColor; }
.tone-ok { color: var(--qt-state-running); }
.tone-warn { color: var(--qt-sev-warn); }
.tone-fail { color: var(--qt-state-error); }
.tone-na { color: var(--qt-code-na); }
</style>
