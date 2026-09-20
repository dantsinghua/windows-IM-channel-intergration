<script setup lang="ts">
/** 状态灯:色 = 令牌,形 = §2.6.1 的灯形(颜色不超过 6 种,形状比颜色便宜) */
import { computed } from 'vue'
import { ACCOUNT_STATES, type AccountState } from '@/i18n/zh-CN/codes'

const props = defineProps<{ state: AccountState; reason?: string }>()

const meta = computed(() => ACCOUNT_STATES[props.state] ?? ACCOUNT_STATES.stopped)
const title = computed(() => props.reason || meta.value.zh)
</script>

<template>
  <span class="dot-wrap" :title="title">
    <span class="dot" :class="`shape-${meta.shape}`" :style="{ '--c': `var(${meta.token})` }">
      <span v-if="meta.shape === 'bang'" class="mark">!</span>
      <span v-else-if="meta.shape === 'cross'" class="mark">×</span>
    </span>
  </span>
</template>

<style scoped>
.dot-wrap { display: inline-flex; align-items: center; }
.dot {
  width: 10px; height: 10px; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--c); position: relative; color: #fff; font-size: 8px; line-height: 1;
}
.shape-hollow { background: transparent; border: 2px solid var(--c); }
.shape-dashed { background: transparent; border: 2px dashed var(--c); }
.shape-spin { background: transparent; border: 2px solid var(--c); border-top-color: transparent; animation: qt-spin .8s linear infinite; }
.shape-halfring::after {
  content: ''; position: absolute; inset: -3px; border-radius: 50%;
  border: 2px solid var(--c); border-right-color: transparent; border-bottom-color: transparent;
}
.mark { position: absolute; font-weight: 700; }
@keyframes qt-spin { to { transform: rotate(360deg); } }
</style>
