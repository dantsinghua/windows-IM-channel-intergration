import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'

// 主进程 + 预加载:CJS 产物(Electron 主进程按 .cjs 加载,不受 package.json type=module 影响)
export default defineConfig({
  resolve: {
    alias: { '@shared': fileURLToPath(new URL('./electron/shared', import.meta.url)) },
  },
  build: {
    outDir: 'dist/electron',
    emptyOutDir: true,
    ssr: true,
    target: 'node20',
    minify: false,
    sourcemap: false,
    rollupOptions: {
      input: {
        'main/index': fileURLToPath(new URL('./electron/main/index.ts', import.meta.url)),
        'preload/index': fileURLToPath(new URL('./electron/preload/index.ts', import.meta.url)),
      },
      external: ['electron', 'node:fs', 'node:path', 'node:net', 'node:os', 'node:url', 'node:crypto'],
      output: {
        format: 'cjs',
        entryFileNames: '[name].cjs',
        chunkFileNames: 'chunks/[name].cjs',
      },
    },
  },
})
