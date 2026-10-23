<script setup lang="ts">
/** 结果码芯片:色与文案全部取自 codes.ts(§2.10 单一来源) */
import { computed } from 'vue'
import { resultCodeText, resultCodeTone } from '@/i18n/zh-CN/codes'

const props = defineProps<{ code: string; reason?: string | null }>()
const tone = computed(() => resultCodeTone(props.code))
const text = computed(() => resultCodeText(props.code, props.reason))
</script>

<template>
  <span class="chip" :style="{ '--c': `var(--qt-code-${tone})` }">
    <span class="dot" />{{ text }}
  </span>
</template>

<style scoped>
.chip {
  display: inline-flex; align-items: center; gap: 6px; padding: 1px 8px;
  border: 1px solid var(--c); border-radius: 10px; color: var(--c); font-size: var(--qt-font-xs);
}
.dot { width: 6px; height: 6px; border-radius: 50%; background: var(--c); }
</style>
