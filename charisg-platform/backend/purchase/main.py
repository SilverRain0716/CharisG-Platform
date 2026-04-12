"""
PA API entrypoint — port 8002.

라우터 19개(summary + dashboard + 17 PA 전용) + purchase.db.
"""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

ROOT = os.environ.get("CHARISG_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
load_dotenv(os.path.join(ROOT, ".env"))

from backend.purchase import database
from backend.purchase.routers import (
    summary, dashboard, datalab, discovery, searchad, keywords, sourcing, margin,
    customs, competition, pa_products, detail_page, smartstore, coupang,
    orders, tracking, cs, returns, pa_monitor, pa_settings, exchange_rate, pricing,
)
from backend_shared.context import register_db_factory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("pa-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    register_db_factory(database.get_db)
    logger.info("PA API 기동 완료")
    yield


app = FastAPI(
    title="Charis G Purchase Agent API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/pa/docs",
    openapi_url="/api/pa/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3002"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 22 routers
for r in (summary, dashboard, datalab, discovery, searchad, keywords, sourcing, margin,
          customs, competition, pa_products, detail_page, smartstore, coupang,
          orders, tracking, cs, returns, pa_monitor, pa_settings, exchange_rate, pricing):
    app.include_router(r.router)

# 이미지 정적 파일 서빙 — /api/pa/images/products/{id}/img_000.jpg
_media_dir = Path(__file__).resolve().parent / "media"
_media_dir.mkdir(parents=True, exist_ok=True)
app.mount("/api/pa/images", StaticFiles(directory=str(_media_dir)), name="pa-images")


@app.get("/api/pa/health")
def health():
    return {"status": "ok", "service": "pa-api", "version": "1.0.0"}
