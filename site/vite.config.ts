import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '')

  const base = env.VITE_BASE_PATH ? `/${env.VITE_BASE_PATH.replace(/^\/+|\/+$/g, '')}/` : '/'

  return {
    base,
    plugins: [react()],
    build: {
      target: 'es2022',
    },
  }
})
