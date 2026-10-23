<script setup lang="ts">
/** 每页统一三态(01 §2.7):加载=骨架屏、空=插画+一句话+主操作、错误=结果码中文 + trace_id + 重试 */
defineProps<{
  loading?: boolean
  error?: string | null
  empty?: boolean
  emptyText?: string
  traceId?: string
  rows?: number
}>()
defineEmits<{ (e: 'retry'): void }>()
</script>

<template>
  <div v-if="loading">
    <a-skeleton active :paragraph="{ rows: rows ?? 4 }" />
  </div>
  <a-result v-else-if="error" status="error" :title="error">
    <template #subTitle>
      <span v-if="traceId" class="qt-mono qt-small">trace {{ traceId.slice(0, 8) }}</span>
    </template>
    <template #extra>
      <a-button type="primary" @click="$emit('retry')">重试</a-button>
    </template>
  </a-result>
  <a-empty v-else-if="empty" :description="emptyText ?? '暂无数据'">
    <slot name="empty-action" />
  </a-empty>
  <slot v-else />
</template>
