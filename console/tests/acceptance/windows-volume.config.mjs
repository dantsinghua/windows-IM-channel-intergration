import { fileURLToPath, URL } from 'node:url'
import process from 'node:process';
import { createRequire } from 'node:module'
import path from 'node:path'

const modules = process.env.QT_PW_NODE_MODULES || fileURLToPath(new URL('../../node_modules', import.meta.url))
const require = createRequire(path.join(modules, '__volume_acceptance__.cjs'))
const { defineConfig } = require('@playwright/test')
const evidence = process.env.QT_VOLUME_EVIDENCE || path.resolve(import.meta.dirname, '../../../.codex/test-results/20261008-monitor-fix/acceptance/windows-volume')

export default defineConfig({
  testDir: '.', testMatch: 'windows-volume.pw.mjs',
  fullyParallel: false, workers: 1, retries: 0,
  timeout: 75_000, globalTimeout: 600_000,
  expect: { timeout: 10_000 },
  outputDir: path.join(evidence, 'artifacts'),
  reporter: [['line'], ['json', { outputFile: path.join(evidence, 'results.json') }]],
  use: {
    browserName: 'chromium', headless: true,
    viewport: { width: 1360, height: 1000 },
    actionTimeout: 10_000, trace: 'off', video: 'off', screenshot: 'off',
    serviceWorkers: 'block',
  },
})
