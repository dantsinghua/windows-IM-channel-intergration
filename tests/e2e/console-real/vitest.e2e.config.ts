import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

// 运行方式:把本目录整体复制到 `console/tests/e2e-real/`,在 console/ 下 `npm run test:e2e-real`
// (= `vitest run --config tests/e2e-real/vitest.e2e.config.ts`);root/alias 均按该落点解析。
export default defineConfig({
  root: fileURLToPath(new URL('../..', import.meta.url)),
  // 第三轮:set-page-real.spec.ts 真挂载 SetPage.vue(单文件用 `@vitest-environment jsdom`),需要编译 .vue
  plugins: [vue()],
  resolve: { alias: { '@': fileURLToPath(new URL('../../src', import.meta.url)) } },
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
