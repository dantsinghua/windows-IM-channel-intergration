<script setup lang="ts">
/**
 * 等人提示卡(消费 `account_state.prompt`,§2.7.3)。
 * 🔴 `qrcode_png_b64` **只渲染到 `<img>`**:不落盘、不进环形缓冲、不进日志、不进 crash.json(§6-10)。
 * 倒计时由 `prompt.countdown_s` / `prompt.expires_at` 驱动,本地只做秒级递减。
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import type { Prompt } from '@/api/types'
import { LOGIN_PHASE_TITLE, STATE_CODES } from '@/i18n/zh-CN/codes'

const props = defineProps<{
  prompt: Prompt | null | undefined
  stateCode?: string
  testid?: string
  qrTestid?: string
  expireTestid?: string
  timerTestid?: string
}>()

const left = ref(0)
let timer: ReturnType<typeof setInterval> | null = null

const meta = computed(() => (props.stateCode ? STATE_CODES[props.stateCode] : undefined))
const title = computed(() => (meta.value?.group === 'wait' ? LOGIN_PHASE_TITLE : meta.value?.zh ?? '等待操作'))
const subtitle = computed(() => (meta.value?.group === 'wait' ? meta.value.zh : ''))
const body = computed(() => props.prompt?.text ?? subtitle.value)
const qr = computed(() => props.prompt?.qrcode_png_b64 ?? '')

function reset(): void {
  if (timer) clearInterval(timer)
  timer = null
  const p = props.prompt
  if (!p) { left.value = 0; return }
  if (typeof p.countdown_s === 'number') left.value = p.countdown_s
  else if (p.expires_at) left.value = Math.max(0, Math.round((Date.parse(p.expires_at) - Date.now()) / 1000))
  else { left.value = 0; return }
  timer = setInterval(() => { left.value = Math.max(0, left.value - 1) }, 1000)
}

watch(() => props.prompt, reset, { immediate: true, deep: true })
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <a-card v-if="prompt && prompt.kind" size="small" :data-testid="testid" class="prompt">
    <div class="qt-row">
      <strong>{{ title }}</strong>
      <span v-if="subtitle" class="qt-muted qt-small">{{ subtitle }}</span>
    </div>
    <p class="body">{{ body }}</p>
    <img v-if="qr" :data-testid="qrTestid" class="qr" :src="`data:image/png;base64,${qr}`" alt="登录二维码" />
    <div v-if="left > 0" class="qt-row">
      <span :data-testid="expireTestid ?? timerTestid" class="qt-mono">剩余 {{ left }} 秒</span>
    </div>
    <slot />
  </a-card>
</template>

<style scoped>
.prompt { margin-bottom: var(--qt-space-3); }
.body { margin: var(--qt-space-2) 0; white-space: pre-wrap; }
.qr { width: 200px; height: 200px; image-rendering: pixelated; }
</style>
