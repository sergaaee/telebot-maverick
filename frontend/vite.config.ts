import react from '@vitejs/plugin-react';
import { defineConfig, loadEnv } from 'vite';

// VITE_PROXY_TARGET — куда dev-сервер проксирует /api (адрес бэкенда); см. frontend/.env.example
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '');
  const target = env.VITE_PROXY_TARGET || 'http://127.0.0.1:8000';
  return {
    plugins: [react()],
    server: { port: Number(env.VITE_PORT) || 5173, proxy: { '/api': { target, changeOrigin: true } } },
    preview: { port: Number(env.VITE_PORT) || 5173, proxy: { '/api': { target, changeOrigin: true } } },
  };
});
