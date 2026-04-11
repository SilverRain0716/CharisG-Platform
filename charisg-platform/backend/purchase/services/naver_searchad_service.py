"""
naver_searchad_service.py — 네이버 검색광고 API (월간 검색량 조회).

HMAC 서명 인증.
"""
import base64
import hashlib
import hmac
import logging
import time
from typing import Optional

import requests

from backend_shared._config import (
    NAVER_SEARCHAD_API_KEY,
    NAVER_SEARCHAD_SECRET_KEY,
    NAVER_SEARCHAD_CUSTOMER_ID,
)

logger = logging.getLogger(__name__)

BASE = "https://api.searchad.naver.com"


def _signature(timestamp: str, method: str, uri: str) -> str:
    msg = f"{timestamp}.{method}.{uri}"
    sig = hmac.new(NAVER_SEARCHAD_SECRET_KEY.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.b64encode(sig).decode()


def _headers(method: str, uri: str) -> dict:
    ts = str(int(time.time() * 1000))
    return {
        "X-Timestamp": ts,
        "X-API-KEY": NAVER_SEARCHAD_API_KEY,
        "X-Customer": str(NAVER_SEARCHAD_CUSTOMER_ID),
        "X-Signature": _signature(ts, method, uri),
        "Content-Type": "application/json",
    }


def get_keyword_volumes(keywords: list[str]) -> Optional[list[dict]]:
    """월간 PC + 모바일 검색량 조회. 한 번에 5개씩."""
    if not (NAVER_SEARCHAD_API_KEY and NAVER_SEARCHAD_SECRET_KEY):
        logger.warning("NAVER_SEARCHAD_* 미설정 — 스킵")
        return None
    uri = "/keywordstool"
    out = []
    for i in range(0, len(keywords), 5):
        chunk = keywords[i:i + 5]
        params = {"hintKeywords": ",".join(chunk), "showDetail": 1}
        try:
            r = requests.get(BASE + uri, headers=_headers("GET", uri), params=params, timeout=10)
            r.raise_for_status()
            for it in r.json().get("keywordList", []):
                out.append({
                    "keyword": it.get("relKeyword"),
                    "monthly_pc": int(it.get("monthlyPcQcCnt", 0) or 0),
                    "monthly_mobile": int(it.get("monthlyMobileQcCnt", 0) or 0),
                    "competition": it.get("compIdx"),
                })
        except Exception as e:
            logger.error(f"검색광고 호출 실패 ({chunk}): {e}")
    return out
