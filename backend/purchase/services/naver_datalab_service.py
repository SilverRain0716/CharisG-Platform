"""
naver_datalab_service.py — 네이버 데이터랩 공식 OpenAPI.

- fetch_category_trends: 카테고리 일별 ratio (기존)
- fetch_keyword_search_trends: 키워드별 일별 ratio (트렌드 점수 계산용)
- compute_trend_score: 시계열 → 상승/하락 비율 (>1 상승, <1 하락)
- store_keyword: keywords 테이블 UPSERT (category_cid 지원)

EC2 의존: NAVER_DATALAB_CLIENT_ID/SECRET .env.
"""
import logging
import time
from datetime import date, timedelta
from typing import Optional

import requests

from backend_shared._config import NAVER_DATALAB_CLIENT_ID, NAVER_DATALAB_CLIENT_SECRET
from backend.purchase.database import get_db

logger = logging.getLogger(__name__)

API_URL = "https://openapi.naver.com/v1/datalab/shopping/categories"
KEYWORD_SEARCH_URL = "https://openapi.naver.com/v1/datalab/search"


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


def store_keyword(
    keyword: str,
    monthly_pc: int = 0,
    monthly_mobile: int = 0,
    competition: Optional[float] = None,
    source: str = "naver_datalab",
    category_cid: Optional[int] = None,
) -> int:
    """키워드 → keywords 테이블 UPSERT. 기존 row 가 있으면 id 반환."""
    with get_db() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO keywords
               (keyword, source, monthly_pc, monthly_mobile, competition, category_cid)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (keyword, source, monthly_pc, monthly_mobile, competition, category_cid),
        )
        if cur.lastrowid:
            return cur.lastrowid
        existing = conn.execute(
            "SELECT id FROM keywords WHERE keyword=? AND source=?",
            (keyword, source),
        ).fetchone()
        return existing["id"] if existing else 0


def fetch_keyword_search_trends(
    keywords: list[str], days: int = 30
) -> dict[str, list[dict]]:
    """공식 /v1/datalab/search — 키워드별 일별 ratio 시계열.

    한 번에 키워드 그룹 5개까지. 반환: {keyword: [{period, ratio}, ...]}
    """
    if not (NAVER_DATALAB_CLIENT_ID and NAVER_DATALAB_CLIENT_SECRET):
        logger.warning("NAVER_DATALAB_* 미설정 — fetch_keyword_search_trends 스킵")
        return {}

    end = date.today()
    start = end - timedelta(days=days)
    out: dict[str, list[dict]] = {}

    headers = {
        "X-Naver-Client-Id": NAVER_DATALAB_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_DATALAB_CLIENT_SECRET,
        "Content-Type": "application/json",
    }

    for i in range(0, len(keywords), 5):
        chunk = keywords[i:i + 5]
        payload = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "timeUnit": "date",
            "keywordGroups": [
                {"groupName": k, "keywords": [k]} for k in chunk
            ],
        }
        try:
            r = requests.post(
                KEYWORD_SEARCH_URL, headers=headers, json=payload, timeout=10
            )
            r.raise_for_status()
            for res in r.json().get("results", []):
                out[res.get("title")] = res.get("data", [])
            time.sleep(0.3)
        except Exception as e:
            logger.error(f"datalab/search chunk={chunk} 실패: {e}")
    return out


def compute_trend_score(time_series: list[dict]) -> Optional[float]:
    """비율식 트렌드 점수 = mean(last 7) / max(mean(first 7), 1).

    >1 = 상승, <1 = 하락. 데이터 14일 미만이면 None.
    """
    if not time_series or len(time_series) < 14:
        return None
    values = [float(p.get("ratio", 0) or 0) for p in time_series]
    first7 = sum(values[:7]) / 7
    last7 = sum(values[-7:]) / 7
    return round(last7 / max(first7, 1.0), 3)
