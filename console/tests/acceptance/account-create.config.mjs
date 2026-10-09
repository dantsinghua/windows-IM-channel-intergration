import process from 'node:process';
import { createRequire } from 'node:module'
import path from 'node:path'

const modules = process.env.QT_PW_NODE_MODULES || '/home/anlin/work/qtrade-build/manual-20261008-a0stq5rk/console/node_modules'
const require = createRequire(path.join(modules, '__acceptance__.cjs'))
const { defineConfig } = require('@playwright/test')

export default defineConfig({
  testDir: '.',
  testMatch: 'account-create.pw.mjs',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  outputDir: process.env.QT_ACCEPT_OUTPUT_DIR || '../../../.codex/test-results/20261008-create-fix/acceptance/artifacts',
  reporter: [
    ['line'],
    ['json', { outputFile: process.env.QT_ACCEPT_REPORT || '../../../.codex/test-results/20261008-create-fix/acceptance/results.json' }],
  ],
  use: {
    browserName: 'chromium',
    headless: true,
    viewport: { width: 1360, height: 1000 },
    actionTimeout: 10_000,
    trace: 'off',
    video: 'off',
    screenshot: 'off',
    serviceWorkers: 'block',
  },
})
