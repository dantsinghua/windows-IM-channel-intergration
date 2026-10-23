<script setup lang="ts">
/** JSON 树(结果区 / 原始载荷);只读,等宽字体 */
const props = defineProps<{ value: unknown }>()
const text = () => {
  try {
    return JSON.stringify(props.value, null, 2)
  } catch {
    return String(props.value)
  }
}
</script>

<template>
  <pre class="json qt-mono">{{ text() }}</pre>
</template>

<style scoped>
.json {
  margin: 0; padding: var(--qt-space-2); max-height: 260px; overflow: auto;
  background: var(--qt-bg); border: 1px solid var(--qt-border); border-radius: var(--qt-radius-sm);
  font-size: var(--qt-font-xs); white-space: pre-wrap; word-break: break-all;
}
</style>
