"""
PA API entrypoint — port 8002.

라우터 19개(summary + dashboard + 17 PA 전용) + purchase.db.
"""
import asyncio
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
    customs, competition, pa_products, detail_page, smartstore, smartstore_attributes, coupang,
    orders, tracking, cs, returns, pa_monitor, pa_settings, exchange_rate, pricing,
    groups, category_mapping, kr_shipping, forwarder_pricing,
)
from backend.purchase.services import (
    coupang_order_poller, coupang_return_poller, image_cleanup_poller, smartstore_order_poller,
    sheet_queue_worker,
)
from backend_shared.context import register_db_factory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("pa-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    register_db_factory(database.get_db)
    # 쿠팡 주문 1시간 폴링 태스크.
    poller_task: asyncio.Task | None = None
    if os.environ.get("PA_DISABLE_COUPANG_ORDER_POLLER") != "1":
        poller_task = asyncio.create_task(coupang_order_poller.run_forever())
        logger.info("쿠팡 주문 폴러 기동 (interval=%ds)", coupang_order_poller.POLL_INTERVAL_SEC)
    # 쿠팡 반품/취소 30분 폴링 태스크.
    return_poller_task: asyncio.Task | None = None
    if os.environ.get("PA_DISABLE_COUPANG_RETURN_POLLER") != "1":
        return_poller_task = asyncio.create_task(coupang_return_poller.run_forever())
        logger.info("쿠팡 반품/취소 폴러 기동 (interval=%ds)", coupang_return_poller.POLL_INTERVAL_SEC)
    # 네이버(스마트스토어) 주문 1시간 폴링 태스크.
    smartstore_poller_task: asyncio.Task | None = None
    if os.environ.get("PA_DISABLE_SMARTSTORE_ORDER_POLLER") != "1":
        smartstore_poller_task = asyncio.create_task(smartstore_order_poller.run_forever())
        logger.info("스마트스토어 주문 폴러 기동 (interval=%ds)", smartstore_order_poller.POLL_INTERVAL_SEC)
    # 만료 이미지 24시간 주기 정리.
    cleanup_task: asyncio.Task | None = None
    if os.environ.get("PA_DISABLE_IMAGE_CLEANUP") != "1":
        cleanup_task = asyncio.create_task(image_cleanup_poller.run_forever())
        logger.info("이미지 정리 폴러 기동 (interval=%ds)", image_cleanup_poller.POLL_INTERVAL_SEC)
    # 시트 큐 자동 파이프라인 워커
    sheet_worker_task: asyncio.Task | None = None
    if os.environ.get("PA_DISABLE_SHEET_QUEUE") != "1":
        sheet_worker_task = asyncio.create_task(sheet_queue_worker.run_forever())
        logger.info("시트 큐 워커 기동 (poll=%ds)", sheet_queue_worker.POLL_INTERVAL_SEC)
    logger.info("PA API 기동 완료")
    try:
        yield
    finally:
        for t in (poller_task, return_poller_task, smartstore_poller_task, cleanup_task, sheet_worker_task):
            if t:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass


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

# 26 routers
for r in (summary, dashboard, datalab, discovery, searchad, keywords, sourcing, margin,
          customs, competition, pa_products, detail_page, smartstore, smartstore_attributes, coupang,
          orders, tracking, cs, returns, pa_monitor, pa_settings, exchange_rate, pricing,
          groups, category_mapping, kr_shipping, forwarder_pricing):
    app.include_router(r.router)

# 이미지 정적 파일 서빙 — /api/pa/images/products/{id}/img_000.jpg
_media_dir = Path(__file__).resolve().parent / "media"
_media_dir.mkdir(parents=True, exist_ok=True)
app.mount("/api/pa/images", StaticFiles(directory=str(_media_dir)), name="pa-images")


@app.get("/api/pa/health")
def health():
    return {"status": "ok", "service": "pa-api", "version": "1.0.0"}
