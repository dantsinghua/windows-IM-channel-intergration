<script setup lang="ts">
/** 通道标签。颜色跟随当前机构主题，不使用固定通道色。 */
import { computed } from 'vue'
import { CHANNEL_TEXT, type Channel } from '@/i18n/zh-CN/codes'

const props = defineProps<{ channel: Channel }>()
const text = computed(() => CHANNEL_TEXT[props.channel] ?? props.channel)
</script>

<template>
  <span class="ch">{{ text }}</span>
</template>

<style scoped>
.ch {
  display: inline-block; padding: 0 6px; border: 1px solid color-mix(in srgb, var(--qt-primary) 42%, white);
  border-radius: var(--qt-radius-sm); background: var(--qt-primary-soft); color: var(--qt-primary);
  font-size: var(--qt-font-xs); line-height: 18px;
}
</style>
