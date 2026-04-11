"""
naver_datalab_service.py — 네이버 데이터랩 쇼핑 인사이트 API.

키워드 트렌드 (디바이스/연령/성별별 검색량) 수집 → keywords 테이블 적재.
EC2 의존: NAVER_DATALAB_CLIENT_ID/SECRET .env 필요.
"""
import json
import logging
from datetime import date, timedelta
from typing import Optional

import requests

from backend_shared._config import NAVER_DATALAB_CLIENT_ID, NAVER_DATALAB_CLIENT_SECRET
from backend.purchase.database import get_db

logger = logging.getLogger(__name__)

API_URL = "https://openapi.naver.com/v1/datalab/shopping/categories"


def _headers() -> dict:
    return {
        "X-Naver-Client-Id": NAVER_DATALAB_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_DATALAB_CLIENT_SECRET,
        "Content-Type": "application/json",
    }


def fetch_category_trends(
    category_name: str,
    category_param: str,
    days: int = 30,
) -> Optional[list[dict]]:
    """카테고리 트렌드 (날짜별 검색량 ratio) 조회."""
    if not (NAVER_DATALAB_CLIENT_ID and NAVER_DATALAB_CLIENT_SECRET):
        logger.warning("NAVER_DATALAB_CLIENT_ID/SECRET 미설정 — fetch_category_trends 스킵")
        return None

    end = date.today()
    start = end - timedelta(days=days)
    payload = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "timeUnit": "date",
        "category": [{"name": category_name, "param": [category_param]}],
        "device": "", "ages": [], "gender": "",
    }
    try:
        r = requests.post(API_URL, headers=_headers(), json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data.get("results", [{}])[0].get("data", [])
    except Exception as e:
        logger.error(f"네이버 데이터랩 호출 실패: {e}")
        return None


def store_keyword(keyword: str, monthly_pc: int = 0, monthly_mobile: int = 0,
                  competition: float = None, source: str = "naver_datalab") -> int:
    """키워드 → keywords 테이블 적재."""
    with get_db() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO keywords
               (keyword, source, monthly_pc, monthly_mobile, competition)
               VALUES (?, ?, ?, ?, ?)""",
            (keyword, source, monthly_pc, monthly_mobile, competition),
        )
        if cur.lastrowid:
            return cur.lastrowid
        existing = conn.execute(
            "SELECT id FROM keywords WHERE keyword=? AND source=?",
            (keyword, source),
        ).fetchone()
        return existing["id"] if existing else 0


def run_full_pipeline(category_param: str = "50000000") -> dict:
    """풀 파이프라인 — 카테고리 트렌드 → 상위 50 키워드 적재."""
    trends = fetch_category_trends("전체", category_param, days=30)
    if not trends:
        return {"ok": False, "message": "데이터랩 응답 없음 — .env/네트워크 확인"}

    inserted = 0
    for t in trends[:50]:
        kid = store_keyword(t.get("period", ""), monthly_pc=int(t.get("ratio", 0)))
        if kid:
            inserted += 1
    return {"ok": True, "inserted": inserted, "total": len(trends)}
