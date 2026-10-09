<script setup lang="ts">
withDefaults(defineProps<{ name: string; size?: number }>(), { size: 20 })
const paths: Record<string, string> = {
  dash: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  msg: 'M21 11a8 8 0 0 1-8 8H7l-4 3V11a9 9 0 0 1 18 0Z M7 10h10 M7 14h6',
  log: 'M8 3h8 M9 3v3h6V3 M6 4H4v17h16V4h-2 M8 11h8 M8 16h5',
  res: 'M3 3v18h18 M6 15l4-5 4 3 5-8',
  env: 'M3 4h18v6H3z M3 14h18v6H3z M7 7h.01 M7 17h.01 M16 7h2 M16 17h2',
  acct: 'M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z M4 21v-2a8 8 0 0 1 16 0v2',
  screen: 'M3 3h18v14H3z M8 21h8 M12 17v4',
  cmd: 'M4 5l6 6-6 6 M13 18h7',
  flow: 'M4 3h6v6H4z M14 15h6v6h-6z M7 9v9h7 M17 3v8 M14 8l3 3 3-3',
  mail: 'M3 5h18v14H3z M3 5l9 8 9-8',
  set: 'M12 8a4 4 0 1 1 0 8 4 4 0 0 1 0-8Z M12 2v3 M12 19v3 M2 12h3 M19 12h3 M5 5l2 2 M17 17l2 2 M5 19l2-2 M17 7l2-2',
  bell: 'M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9 M10 21h4',
  more: 'M5 12h.01 M12 12h.01 M19 12h.01',
  plus: 'M12 5v14 M5 12h14',
  search: 'M20 20l-5-5 M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0Z',
  refresh: 'M20 7V3l-3 3a8 8 0 0 0-13 6 M4 17v4l3-3a8 8 0 0 0 13-6',
  shield: 'M12 2 3 6v6c0 5 9 10 9 10s9-5 9-10V6Z M8 12l3 3 5-6',
  cpu: 'M6 6h12v12H6z M9 9h6v6H9z M9 2v4 M15 2v4 M9 18v4 M15 18v4 M2 9h4 M2 15h4 M18 9h4 M18 15h4',
  memory: 'M3 6h18v12H3z M7 10v4 M12 10v4 M17 10v4 M6 18v3 M10 18v3 M14 18v3 M18 18v3',
  chevron: 'M9 5l7 7-7 7',
  menu: 'M4 6h16 M4 12h16 M4 18h16',
  check: 'M5 12l4 4L19 6',
  clock: 'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18Z M12 7v5l3 2',
}
</script>
<template><svg :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path :d="paths[name] ?? paths.env" /></svg></template>
