# -*- coding: utf-8 -*-
"""네이버 스마트스토어 고객문의 API 조회 (2026-07-21 신설, 2026-08-10 규격 교정).

endpoint: GET /v1/pay-user/inquiries
  ★예전 코드가 시도하던 3개 경로는 전부 실재하지 않아 404 GW.NOT_FOUND 였다:
      /v1/pay-order/seller/product-orders/inquiries
      /v1/products/customer-questions
      /v1/pay-order/seller/customer-questions
  ★날짜는 LocalDate(yyyy-MM-dd). ISO datetime 을 주면 400:
      "startSearchDate should be a valid LocalDate. but, ...T00:00:00.000+09:00 is not"
  응답은 Spring Page 형태 {"content": [...], "totalElements": n, ...}.

호출 제한이 빡빡하다(429 GW.RATE_LIMIT). 연속 호출 금지.
응답 실패는 조용히 [] 반환.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import requests

from backend.purchase.services.naver_commerce_service import BASE, _get_token

logger = logging.getLogger(__name__)


def _iso_kst_range(days: int = 3) -> tuple[str, str]:
    """조회 범위. ★LocalDate(yyyy-MM-dd) — ISO datetime 은 400 이다."""
    now = datetime.now()
    return ((now - timedelta(days=days)).strftime("%Y-%m-%d"),
            now.strftime("%Y-%m-%d"))


def _get(path: str, params: dict) -> Optional[dict]:
    token = _get_token()
    if not token:
        return None
    try:
        r = requests.get(BASE + path,
                         headers={"Authorization": f"Bearer {token}"},
                         params=params, timeout=30)
        if r.status_code >= 400:
            logger.warning("[naver-cs] GET %s status=%s body=%s",
                           path[:80], r.status_code, r.text[:200])
            return None
        return r.json()
    except Exception as e:
        logger.warning("[naver-cs] GET %s 예외: %s", path[:80], e)
        return None


def get_product_questions(
    start_at: Optional[str] = None, end_at: Optional[str] = None,
    page: int = 1, size: int = 100, answered: bool = False,
) -> list[dict]:
    """스마트스토어 고객문의(Q&A) 조회.

    검색 기간: 최근 N일 (기본 3일, LocalDate).
    ★파라미터는 startSearchDate/endSearchDate/page/size 조합만 200 으로 검증됐다.
      answered 필터는 명세가 확인되지 않아 보내지 않고, 필요하면 호출측에서 거른다.
    """
    if not (start_at and end_at):
        start_at, end_at = _iso_kst_range(days=3)

    body = _get("/v1/pay-user/inquiries", {
        "startSearchDate": start_at,
        "endSearchDate": end_at,
        "page": page,
        "size": size,
    })
    if not body:
        return []
    # Spring Page: content 가 정본. 과거 호환으로 나머지 키도 본다.
    for key in ("content", "elements", "contents", "items", "data"):
        arr = body.get(key)
        if isinstance(arr, list):
            return arr
    return []
