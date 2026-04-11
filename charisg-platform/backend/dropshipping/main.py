"""
DS API entrypoint — port 8001.

라우터 13개 + dropshipping.db.
"""
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

ROOT = os.environ.get("CHARISG_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
load_dotenv(os.path.join(ROOT, ".env"))

from backend.dropshipping import database
from backend.dropshipping.routers import (
    summary, dashboard, scoring, cj, gap, trends,
    ds_products, ds_listings, crawler, fees,
    ds_monitor, ds_settings, detail_page, process, category,
)
from backend_shared.context import register_db_factory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ds-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    register_db_factory(database.get_db)
    logger.info("DS API 기동 완료")
    yield


app = FastAPI(
    title="Charis G Dropshipping API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/ds/docs",
    openapi_url="/api/ds/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 13 routers
app.include_router(summary.router)
app.include_router(dashboard.router)
app.include_router(scoring.router)
app.include_router(cj.router)
app.include_router(gap.router)
app.include_router(trends.router)
app.include_router(ds_products.router)
app.include_router(ds_listings.router)
app.include_router(crawler.router)
app.include_router(fees.router)
app.include_router(ds_monitor.router)
app.include_router(ds_settings.router)
app.include_router(detail_page.router)
app.include_router(process.router)
app.include_router(category.router)


@app.get("/api/ds/health")
def health():
    return {"status": "ok", "service": "ds-api", "version": "1.0.0"}
