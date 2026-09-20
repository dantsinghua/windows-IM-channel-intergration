// dev:等 vite dev server 起来后再拉 Electron(避免白屏)
import { spawn } from 'node:child_process'

const URL_ = process.env.VITE_DEV_SERVER_URL ?? 'http://127.0.0.1:5273'

async function waitFor(url, timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    try {
      const r = await fetch(url)
      if (r.ok) return
    } catch {
      // 还没起来
    }
    if (Date.now() > deadline) throw new Error(`dev server 未在 ${timeoutMs}ms 内就绪: ${url}`)
    await new Promise((r) => setTimeout(r, 300))
  }
}

await waitFor(URL_)
const electron = (await import('electron')).default
spawn(electron, ['.'], { stdio: 'inherit', env: { ...process.env, VITE_DEV_SERVER_URL: URL_ } })
