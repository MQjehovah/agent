import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 构建产物直接输出到 FastAPI 的静态目录(旧版单页保留在 static/,用于回退)
export default defineConfig({
  plugins: [vue()],
  base: '/',
  build: {
    outDir: '../src/web/static_vue',
    emptyOutDir: true
  },
  server: {
    port: 5180,
    proxy: {
      // 开发模式直连本地 agent
      '/api': 'http://127.0.0.1:8080',
      '/webhook': 'http://127.0.0.1:8080'
    }
  }
})
