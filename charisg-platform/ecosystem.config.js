/**
 * PM2 ecosystem — t3.micro (1GB RAM) 호환 정적 빌드 모드.
 *
 * 프론트 3개는 vite build → apps/*/dist 생성 후 Nginx 정적 서빙.
 * 여기서는 Python 백엔드 3 프로세스만 PM2 로 관리.
 *
 * 메모리 예산 (총 ~700MB):
 *   hub-api  ~120MB  (auth + summary fan-out)
 *   ds-api   ~150MB  (FastAPI + sqlite WAL + ai 서비스 로드)
 *   pa-api   ~150MB
 *   nginx    ~30MB
 *   OS+sshd  ~250MB
 */
module.exports = {
  apps: [
    {
      name: 'hub-api',
      cwd: __dirname,
      script: 'python3',
      args:  '-m uvicorn backend.hub.main:app --host 127.0.0.1 --port 8000',
      interpreter: 'none',
      env: {
        PYTHONPATH:   __dirname,
        CHARISG_ROOT: __dirname,
      },
      max_memory_restart: '300M',
      autorestart: true,
      max_restarts: 10,
    },
    {
      name: 'ds-api',
      cwd: __dirname,
      script: 'python3',
      args:  '-m uvicorn backend.dropshipping.main:app --host 127.0.0.1 --port 8001',
      interpreter: 'none',
      env: {
        PYTHONPATH:   __dirname,
        CHARISG_ROOT: __dirname,
      },
      max_memory_restart: '350M',
      autorestart: true,
      max_restarts: 10,
    },
    {
      name: 'pa-api',
      cwd: __dirname,
      script: 'python3',
      args:  '-m uvicorn backend.purchase.main:app --host 127.0.0.1 --port 8002',
      interpreter: 'none',
      env: {
        PYTHONPATH:   __dirname,
        CHARISG_ROOT: __dirname,
      },
      max_memory_restart: '350M',
      autorestart: true,
      max_restarts: 10,
    },
  ],
};
