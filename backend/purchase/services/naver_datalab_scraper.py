"""
naver_datalab_scraper.py — 데이터랩 SPA 의 비공식 JSON 엔드포인트.

공식 OpenAPI 에 '카테고리 → 인기 키워드 목록' 이 없어서
데이터랩 웹 SPA 가 호출하는 JSON 엔드포인트를 우회로로 사용.

중요:
- count 파라미터는 서버에서 무시되고 페이지당 20개 고정이다.
  TOP 100 을 받으려면 page=1~5 를 차례로 호출해야 한다.
- 짧은 User-Agent("Mozilla/5.0") 는 봇 감지로 301 로 빠진다.
  풀 UA + Referer 필수.
- 카테고리 간 1초 sleep 으로 rate limit 회피.
- 동시성(asyncio/threading) 금지. 단일 스레드 + time.sleep 만.
"""
import logging
import time
from datetime import date, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

BASE = "https://datalab.naver.com/shoppingInsight"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
REFERER = "https://datalab.naver.com/shoppingInsight/sCategory.naver"


def _headers() -> dict:
    return {
        "User-Agent": UA,
        "Referer": REFERER,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }


def fetch_category_list() -> list[dict]:
    """전체 카테고리 트리. cid=0 호출 → 1차 카테고리들 + 자식들."""
    try:
        r = requests.post(
            f"{BASE}/getCategoryList.naver",
            headers=_headers(),
            data={"cid": "0"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"getCategoryList 실패: {e}")
        return []


def fetch_keyword_rank(cid: int, days: int = 30, max_pages: int = 5) -> list[dict]:
    """카테고리 → 인기 키워드 TOP(max_pages*20).

    count 는 무시되고 페이지당 20개 고정.
    각 원소: {"rank": int, "keyword": str, "linkId": str}
    """
    end = date.today()
    start = end - timedelta(days=days)
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        try:
            r = requests.post(
                f"{BASE}/getCategoryKeywordRank.naver",
                headers=_headers(),
                data={
                    "cid": str(cid),
                    "timeUnit": "date",
                    "startDate": start.isoformat(),
                    "endDate": end.isoformat(),
                    "age": "",
                    "gender": "",
                    "device": "",
                    "page": str(page),
                    "count": "20",
                },
                timeout=10,
            )
            r.raise_for_status()
            ranks = r.json().get("ranks", [])
            if not ranks:
                break
            out.extend(ranks)
            time.sleep(1.0)
        except Exception as e:
            logger.error(f"getCategoryKeywordRank cid={cid} page={page} 실패: {e}")
            break
    return out


def fetch_category_trend(cid: int, days: int = 30) -> Optional[list[dict]]:
    """카테고리 일별 ratio 시계열 (보조용)."""
    end = date.today()
    start = end - timedelta(days=days)
    try:
        r = requests.post(
            f"{BASE}/getCategoryClickTrend.naver",
            headers=_headers(),
            data={
                "cid": str(cid),
                "timeUnit": "date",
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "age": "",
                "gender": "",
                "device": "",
            },
            timeout=10,
        )
        r.raise_for_status()
        result = r.json().get("result", [])
        return result[0].get("data", []) if result else []
    except Exception as e:
        logger.error(f"getCategoryClickTrend cid={cid} 실패: {e}")
        return None
