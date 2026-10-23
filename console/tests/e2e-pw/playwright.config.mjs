/* global process */
/**
 * 控制台浏览器形态(`dev:web`)的 Playwright 真点击用例配置。
 *
 * 🔴 文件一律用 `.pw.mjs` 后缀:
 *  - vitest 的 include 是 `tests/**\/*.spec.ts`,`.pw.mjs` 不会被 `npm test` 误收;
 *  - tsconfig 的 include 是 `tests/**\/*.ts`,`.mjs` 不进 `npm run typecheck`
 *    (仓库还没装 `@playwright/test`,若写成 .ts 会让 typecheck 因缺类型变红)。
 *
 * 运行方式(需先 `npm i -D @playwright/test && npx playwright install chromium`):
 *  A. 连一个已经在跑的 dev:web:`PW_BASE_URL=http://127.0.0.1:5273 npx playwright test -c tests/e2e-pw`
 *  B. 自起一套隔离的 mock + vite(默认端口 vite 5283 / mock 17620,可用 PW_VITE_PORT / PW_MOCK_PORT 覆盖):
 *     `npx playwright test -c tests/e2e-pw`
 *     —— vite 用 `tests/e2e-pw/vite.pw.config.mjs`(只在内存里覆盖端口与代理目标,不改 vite.config.ts)。
 *
 * 注意:mock 的状态是进程内全局的(告知勾选、设置保存等不会自动复位),
 * 所以 B 模式每次跑都是全新 mock;A 模式下用例必须容忍「已经勾过」的状态。
 */
import { defineConfig, devices } from '@playwright/test'

const VITE_PORT = Number(process.env.PW_VITE_PORT ?? 5283)
const MOCK_PORT = Number(process.env.PW_MOCK_PORT ?? 17620)
const external = process.env.PW_BASE_URL
const baseURL = external ?? `http://127.0.0.1:${VITE_PORT}`

export default defineConfig({
  testDir: '.',
  testMatch: '**/*.pw.mjs',
  // 用例之间共享一个 mock 进程 ⇒ 串行,免得互相改状态
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 8_000 },
  reporter: [['list']],
  outputDir: process.env.PW_OUTPUT_DIR ?? './.pw-results',
  use: {
    baseURL,
    headless: true,
    viewport: { width: 1280, height: 800 },
    screenshot: 'only-on-failure',
    trace: 'off',
    ...devices['Desktop Chrome'],
  },
  webServer: external
    ? undefined
    : [
        {
          command: `node mock/server.mjs`,
          cwd: new URL('../..', import.meta.url).pathname,
          env: { MOCK_PORT: String(MOCK_PORT) },
          url: `http://127.0.0.1:${MOCK_PORT}/api/v1/system/health`,
          reuseExistingServer: false,
          timeout: 30_000,
        },
        {
          command: `node node_modules/vite/bin/vite.js --config tests/e2e-pw/vite.pw.config.mjs --host 127.0.0.1`,
          cwd: new URL('../..', import.meta.url).pathname,
          env: { PW_VITE_PORT: String(VITE_PORT), PW_MOCK_PORT: String(MOCK_PORT) },
          url: baseURL,
          reuseExistingServer: false,
          timeout: 60_000,
        },
      ],
})
