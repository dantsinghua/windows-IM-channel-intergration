/* global process */
/**
 * Playwright 自起 vite 用的配置:在内存里继承 `vite.config.ts`,只覆盖端口与 mock 代理目标。
 * 不改仓库的 `vite.config.ts`(那里写死 5273 / 17600,是给安琳日常 dev:web 用的)。
 */
import { mergeConfig } from 'vite'
import base from '../../vite.config.ts'

const VITE_PORT = Number(process.env.PW_VITE_PORT ?? 5283)
const MOCK_PORT = Number(process.env.PW_MOCK_PORT ?? 17620)

const merged = mergeConfig(base, {
  server: { port: VITE_PORT, strictPort: true },
})
// mergeConfig 对 proxy 是深合并,这里直接整体替换目标,避免残留 17600
merged.server.proxy = {
  '/api/v1': { target: `http://127.0.0.1:${MOCK_PORT}`, changeOrigin: true, ws: true },
}
export default merged
