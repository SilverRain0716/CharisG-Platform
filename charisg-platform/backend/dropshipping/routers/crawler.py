"""DS Crawler — 크롤러 상태 + 실행 + SSE 로그."""
import asyncio
import json
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.dropshipping.auth import current_user
from backend.dropshipping.database import get_db
from backend.dropshipping.services import amazon_keyword_crawler

router = APIRouter(prefix="/api/ds/crawler", tags=["ds-crawler"])


@router.get("/status")
def crawler_status(user: dict = Depends(current_user)):
    """크롤러 3종 상태 카드 (CJ / Amazon / Trends)."""
    with get_db() as conn:
        cj_count = conn.execute(
            "SELECT COUNT(*) c FROM collected_products WHERE source='cj'"
        ).fetchone()["c"]
        amazon_count = conn.execute(
            "SELECT COUNT(*) c FROM amazon_search_agg"
        ).fetchone()["c"]

    return {
        "crawlers": [
            {"id": "cj",      "label": "CJ Catalog",  "count": cj_count,    "last_run": None, "status": "idle"},
            {"id": "amazon",  "label": "Amazon Keyword", "count": amazon_count, "last_run": None, "status": "idle"},
            {"id": "trends",  "label": "Google Trends", "count": None,      "last_run": None, "status": "idle"},
        ],
    }


class RunRequest(BaseModel):
    crawler: str            # 'cj' | 'amazon' | 'trends'
    keywords: Optional[list[str]] = None
    limit: Optional[int] = 50


@router.post("/run")
def run_crawler(req: RunRequest, background: BackgroundTasks, user: dict = Depends(current_user)):
    if req.crawler == "amazon":
        kws = req.keywords or amazon_keyword_crawler.get_keywords_from_go_products(req.limit or 50)
        background.add_task(amazon_keyword_crawler.crawl_keywords, kws)
        return {"started": True, "crawler": "amazon", "keyword_count": len(kws)}
    return {"started": False, "message": f"{req.crawler} 크롤러는 EC2 측 GitHub Actions/cron 에서 실행됩니다"}


@router.get("/logs")
async def stream_logs(user: dict = Depends(current_user)):
    """간단한 SSE 로그 — 실제 진행률은 EC2 deploy 후 progress_service 와 연결."""
    async def gen():
        for i in range(3):
            yield f"data: {json.dumps({'msg': '대기 중', 'i': i})}\n\n"
            await asyncio.sleep(1)
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")
