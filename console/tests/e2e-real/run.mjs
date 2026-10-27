/** Real application/API integration, with isolated external service boundaries. */
import { existsSync, mkdirSync } from 'node:fs'
import { dirname, resolve, isAbsolute } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'
import process from 'node:process'

const values = new Map()
const args = process.argv.slice(2)
if (args.includes('--help')) {
  console.log('npm run test:e2e-real -- --xunjia-root /path/xunjia-agent --collector-root /path/collector --output /path/new-evidence [--python /path/python]')
  console.log('Runs real API/protocol/storage/browser code. Gateway, Dify and native IM endpoints are synthetic; no live accounts are contacted.')
  process.exit(0)
}
const allowed = new Set(['--xunjia-root', '--collector-root', '--output', '--python'])
for (let i = 0; i < args.length; i += 2) {
  if (!allowed.has(args[i]) || values.has(args[i]) || !args[i + 1] || args[i + 1].startsWith('--')) {
    console.error('Invalid or duplicate option. Run with --help.')
    process.exit(2)
  }
  values.set(args[i], args[i + 1])
}
const root = values.get('--xunjia-root') || process.env.XUNJIA_TEST_ROOT
const output = values.get('--output')
const collector = values.get('--collector-root') || resolve(dirname(fileURLToPath(import.meta.url)), '../../..')
if (!root || !output || !isAbsolute(root) || !isAbsolute(output) || !isAbsolute(collector)) {
  console.error('Explicit absolute --xunjia-root and --output are required; no live deployment or model endpoint is inferred.')
  process.exit(2)
}
const driver = resolve(root, 'qa/its_relay_joint/run.py')
const python = values.get('--python') || resolve(root, '.venv/bin/python')
if (!existsSync(driver) || !existsSync(python) || !existsSync(resolve(collector, 'src/qtrade_agent'))) {
  console.error('The verified source tree, collector source, or Python test environment is unavailable.')
  process.exit(2)
}
if (existsSync(output)) {
  console.error('Use a new evidence directory; existing results are not overwritten.')
  process.exit(2)
}
mkdirSync(dirname(output), { recursive: true })
const result = spawnSync(python, [driver, '--collector-root', collector, '--output', output, '--suite', 'all', '--build-web'], {
  cwd: root,
  stdio: 'inherit',
  shell: false,
  timeout: 1200000,
  env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
})
if (result.error) {
  console.error(`Integration runner failed: ${result.error.code || result.error.name}`)
  process.exit(2)
}
process.exit(Number.isInteger(result.status) ? result.status : 2)
