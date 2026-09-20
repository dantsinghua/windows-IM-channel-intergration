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
      // dev:web 下把 /api/v1 打到本地 mock 后端(17600 由 mock/server.mjs 监听)
      '/api/v1': { target: 'http://127.0.0.1:17600', changeOrigin: true, ws: true },
    },
  },
  build: {
    outDir: 'dist/renderer',
    emptyOutDir: true,
    target: 'chrome128',
    sourcemap: false,
  },
})
