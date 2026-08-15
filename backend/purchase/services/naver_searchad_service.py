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


def _parse_count(v) -> int:
    """네이버 검색광고 응답 카운트를 int 로. '< 10' 같은 저볼륨 문자열 → 0."""
    if v is None:
        return 0
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    s = str(v).strip()
    if not s or s.startswith("<"):
        return 0
    try:
        return int(s.replace(",", ""))
    except ValueError:
        return 0


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
            payload = r.json().get("keywordList", [])
        except Exception as e:
            logger.error(f"검색광고 호출 실패 ({chunk}): {e}")
            continue
        for it in payload:
            try:
                out.append({
                    "keyword": it.get("relKeyword"),
                    "monthly_pc": _parse_count(it.get("monthlyPcQcCnt", 0)),
                    "monthly_mobile": _parse_count(it.get("monthlyMobileQcCnt", 0)),
                    "competition": it.get("compIdx"),
                })
            except Exception as e:
                logger.warning(f"검색광고 파싱 스킵 ({it.get('relKeyword')}): {e}")
    return out
