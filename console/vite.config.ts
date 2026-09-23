import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 渲染进程构建:产物进 dist/renderer,供 Electron 以 file:// 加载(base 必须相对)
export default defineConfig({
  base: './',
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5273,
    strictPort: true,
    proxy: {
      // dev:web 下把 /api/v1 打到 17600。对着真 Agent 时设 QT_DEV_TOKEN(管理员 Bearer):
      // 渲染进程按规格不持有令牌,Electron 主进程才注入;纯浏览器没有那一层,勾选告知(#87,级别 A)会 401。
      '/api/v1': {
        target: 'http://127.0.0.1:17600',
        changeOrigin: true,
        ws: true,
        configure: (proxy) => {
          const token = process.env.QT_DEV_TOKEN
          if (!token) return
          const inject = (proxyReq: { getHeader: (h: string) => unknown; setHeader: (h: string, v: string) => void }) => {
            if (!proxyReq.getHeader('authorization')) proxyReq.setHeader('Authorization', `Bearer ${token}`)
          }
          proxy.on('proxyReq', inject)
          proxy.on('proxyReqWs', inject)
        },
      },
    },
  },
  build: {
    outDir: 'dist/renderer',
    emptyOutDir: true,
    target: 'chrome128',
    sourcemap: false,
  },
})
