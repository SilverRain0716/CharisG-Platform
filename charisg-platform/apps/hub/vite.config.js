import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    strictPort: true,
    proxy: {
      '/api/hub': 'http://127.0.0.1:8000',
      '/api/ds':  'http://127.0.0.1:8001',
      '/api/pa':  'http://127.0.0.1:8002',
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
});
