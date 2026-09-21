import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  // 第三轮:set-page-real.spec.ts 真挂载 SetPage.vue(单文件用 `@vitest-environment jsdom`),需要编译 .vue
  plugins: [vue()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  test: {
    globals: true,
    environment: 'node',
    include: ['tests/e2e-real/**/*.spec.ts'],
    setupFiles: ['./tests/e2e-real/harness.ts'],
    testTimeout: 60000,
    hookTimeout: 60000,
    fileParallelism: false,
  },
})
