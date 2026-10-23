<script setup lang="ts">
/**
 * 异步作业统一进度组件(R-补,§11.21 [JOB])。
 * 凡 `202 {job_id}` 的操作都用它:进度条 + 取消 + 完成后按 `result` 处理。
 */
import { computed, onUnmounted, watch } from 'vue'
import { useJobsStore } from '@/stores/jobs'
import { JOB_STATE_TEXT } from '@/i18n/zh-CN/codes'
import type { Job } from '@/api/types'

const props = defineProps<{ jobId: string | null; testid?: string; cancelTestid?: string }>()
const emit = defineEmits<{ (e: 'done', job: Job): void }>()

const jobs = useJobsStore()
const job = computed(() => (props.jobId ? jobs.byId[props.jobId] : undefined))

watch(
  () => props.jobId,
  (id) => { if (id) jobs.track(id) },
  { immediate: true },
)

watch(
  () => job.value?.state,
  (s) => { if (s && jobs.isTerminal(job.value) && job.value) emit('done', job.value) },
)

onUnmounted(() => { if (props.jobId) jobs.clear(props.jobId) })

const status = computed(() => {
  const s = job.value?.state
  if (s === 'failed' || s === 'expired') return 'exception'
  if (s === 'succeeded') return 'success'
  return 'active'
})
</script>

<template>
  <div v-if="jobId" class="qt-row" :data-testid="testid">
    <a-progress class="qt-grow" :percent="job?.progress ?? 0" :status="status" size="small" />
    <span class="qt-small qt-muted">{{ JOB_STATE_TEXT[job?.state ?? 'queued'] }}</span>
    <!--
      🔴 进入不可逆阶段(`params.irreversible_since_ms` 出现)后取消按钮置灰:
      再点也只是 `409 NOT_CANCELLABLE`(02 #108 / backend-api-2 §6)。
    -->
    <a-button
      v-if="job && !jobs.isTerminal(job)"
      size="small"
      :disabled="jobs.isIrreversible(job)"
      :title="jobs.isIrreversible(job) ? '已进入不可逆阶段,无法取消' : '取消该作业'"
      :data-testid="cancelTestid"
      @click="jobs.cancel(jobId)"
    >取消</a-button>
    <span v-if="job && jobs.isIrreversible(job) && !jobs.isTerminal(job)" class="qt-small qt-warn">
      已进入不可逆阶段,无法取消
    </span>
    <span v-if="job?.error" class="qt-danger qt-small">{{ job.error.message }}</span>
  </div>
</template>
