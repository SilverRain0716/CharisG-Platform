"""
Hub API entrypoint — port 8000.

Charis G Platform 의 인증 + Hub 대시보드 요약을 담당.
DS/PA API는 별도 프로세스에서 동작.
"""
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# .env 로드 — backend_shared 가 환경변수 참조하기 전에
ROOT = os.environ.get("CHARISG_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
load_dotenv(os.path.join(ROOT, ".env"))

from backend.hub import auth, database
from backend.hub.routers import auth_router, summary_router
from backend_shared.context import register_db_factory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("hub-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    register_db_factory(database.get_db)
    auth.ensure_admin_exists()
    logger.info("Hub API 기동 완료")
    yield


app = FastAPI(
    title="Charis G Hub API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/hub/docs",
    openapi_url="/api/hub/openapi.json",
)

# 단일 도메인이라 CORS 는 사실상 불필요하지만 로컬 개발용
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001", "http://localhost:3002"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(summary_router.router)


@app.get("/api/hub/health")
def health():
    return {"status": "ok", "service": "hub-api", "version": "1.0.0"}
