/**
 * PM2 ecosystem — 6 프로세스
 *
 * 기동 순서: hub-api → ds-api → pa-api → shell-app → ds-app → pa-app
 * Nginx 가 외부 80/443 → 위 6개로 라우팅.
 */
module.exports = {
  apps: [
    // ─── Backend (Python uvicorn) ─────────────────────
    {
      name: 'hub-api',
      cwd: __dirname,
      script: 'python',
      args:  '-m uvicorn backend.hub.main:app --host 127.0.0.1 --port 8000',
      interpreter: 'none',
      env: {
        PYTHONPATH: __dirname,
        CHARISG_ROOT: __dirname,
      },
      max_memory_restart: '350M',
      autorestart: true,
    },
    {
      name: 'ds-api',
      cwd: __dirname,
      script: 'python',
      args:  '-m uvicorn backend.dropshipping.main:app --host 127.0.0.1 --port 8001',
      interpreter: 'none',
      env: {
        PYTHONPATH: __dirname,
        CHARISG_ROOT: __dirname,
      },
      max_memory_restart: '500M',
      autorestart: true,
    },
    {
      name: 'pa-api',
      cwd: __dirname,
      script: 'python',
      args:  '-m uvicorn backend.purchase.main:app --host 127.0.0.1 --port 8002',
      interpreter: 'none',
      env: {
        PYTHONPATH: __dirname,
        CHARISG_ROOT: __dirname,
      },
      max_memory_restart: '500M',
      autorestart: true,
    },

    // ─── Frontend (Vite preview) ─────────────────────
    {
      name: 'shell-app',
      cwd: `${__dirname}/apps/hub`,
      script: 'pnpm',
      args:  'preview',
      interpreter: 'none',
      max_memory_restart: '300M',
      autorestart: true,
    },
    {
      name: 'ds-app',
      cwd: `${__dirname}/apps/dropshipping`,
      script: 'pnpm',
      args:  'preview',
      interpreter: 'none',
      max_memory_restart: '300M',
      autorestart: true,
    },
    {
      name: 'pa-app',
      cwd: `${__dirname}/apps/purchase`,
      script: 'pnpm',
      args:  'preview',
      interpreter: 'none',
      max_memory_restart: '300M',
      autorestart: true,
    },
  ],
};
