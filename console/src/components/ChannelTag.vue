<script setup lang="ts">
/** 通道标签(色 = §2.6.2 通道色) */
import { computed } from 'vue'
import { CHANNEL_TEXT, CHANNEL_TOKEN, type Channel } from '@/i18n/zh-CN/codes'

const props = defineProps<{ channel: Channel }>()
const color = computed(() => `var(${CHANNEL_TOKEN[props.channel]})`)
const text = computed(() => CHANNEL_TEXT[props.channel] ?? props.channel)
</script>

<template>
  <span class="ch" :style="{ borderColor: color, color }">{{ text }}</span>
</template>

<style scoped>
.ch {
  display: inline-block; padding: 0 6px; border: 1px solid; border-radius: var(--qt-radius-sm);
  font-size: var(--qt-font-xs); line-height: 18px;
}
</style>
