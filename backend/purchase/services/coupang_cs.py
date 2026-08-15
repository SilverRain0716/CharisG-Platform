# -*- coding: utf-8 -*-
"""쿠팡 CS 문의 API 조회 (2026-07-21 신설).

3종 조회 함수:
  1) get_online_inquiries       — 온라인 문의 (배송/취소/환불 등 사후)
  2) get_callcenter_inquiries   — 콜센터 인입 문의
  3) get_product_inquiries      — 상품 문의 (상품 상세 Q&A)

응답 실패는 조용히 [] 반환 (poller 정지 방지).
호출측이 `coupang_account("old"|"new")` 컨텍스트 진입 후 호출해야 함.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from backend.purchase.services.coupang_service import (
    BASE, _access_key, _secret_key, _vendor, _signature, _request_with_retry,
)

logger = logging.getLogger(__name__)


def _iso_kst_now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _default_range(days: int = 3) -> tuple[str, str]:
    """기본 조회 범위: 오늘부터 과거 N일.

    ★날짜 형식은 yyyy-MM-dd (공식문서). 예전 ISO datetime(...T00:00:00) 은
      400 을 유발했다. 조회기간 상한은 7일이므로 days>7 금지.
    """
    days = min(days, 7)
    now = datetime.now()
    return ((now - timedelta(days=days)).strftime("%Y-%m-%d"),
            now.strftime("%Y-%m-%d"))


def _content(body: Optional[dict]) -> list[dict]:
    """응답에서 목록을 꺼낸다.

    ★쿠팡 CS API 의 data 는 list 가 아니라 dict 다:
        {"data": {"content": [...], "pagination": {...}}}
      예전 코드는 isinstance(data, list) 만 통과시켜, 200 을 받고도 전부 버렸다.
    """
    if not body:
        return []
    data = body.get("data")
    if isinstance(data, dict):
        content = data.get("content")
        return content if isinstance(content, list) else []
    return data if isinstance(data, list) else []


def _get(path: str, query: str = "") -> Optional[dict]:
    """쿠팡 GET 공통 헬퍼."""
    if not (_access_key() and _secret_key() and _vendor()):
        return None
    url = BASE + path + (f"?{query}" if query else "")
    try:
        r = _request_with_retry(
            "GET", url,
            headers=_signature("GET", path, query),
            timeout=30,
        )
        if r is None:
            return None
        body = r.json() if r.text else {}
        if r.status_code >= 400:
            logger.warning(
                "[coupang-cs] GET %s status=%s body=%s",
                path[:80], r.status_code, str(body)[:200],
            )
            return None
        return body
    except Exception as e:
        logger.warning("[coupang-cs] GET %s 예외: %s", path[:80], e)
        return None


def get_online_inquiries(
    start_at: Optional[str] = None, end_at: Optional[str] = None,
    page_size: int = 50, answered_type: str = "NOANSWER",
) -> list[dict]:
    """상품별 고객문의(온라인 문의) 목록 조회.

    endpoint: GET /v2/providers/openapi/apis/api/v5/vendors/{vendorId}/onlineInquiries
    answered_type: ALL | ANSWERED | NOANSWER  ★대문자 필수
      (소문자 'noAnswer' 는 400 "answeredType can't be null" 을 유발했다)
    pageSize 최대 50 · 조회기간 최대 7일 · 날짜 yyyy-MM-dd
    """
    vid = _vendor()
    if not vid:
        return []
    if not (start_at and end_at):
        start_at, end_at = _default_range(days=3)
    path = f"/v2/providers/openapi/apis/api/v5/vendors/{vid}/onlineInquiries"
    q = (
        f"vendorId={vid}&answeredType={answered_type.upper()}"
        f"&inquiryStartAt={start_at}&inquiryEndAt={end_at}"
        f"&pageNum=1&pageSize={min(page_size, 50)}"
    )
    return _content(_get(path, q))


def get_callcenter_inquiries(
    start_at: Optional[str] = None, end_at: Optional[str] = None,
    page_size: int = 30, counseling_status: str = "NO_ANSWER",
) -> list[dict]:
    """쿠팡 고객센터(콜센터) 문의 목록 조회.

    endpoint: GET /v2/providers/openapi/apis/api/v5/vendors/{vendorId}/callCenterInquiries
      ★경로 3가지가 전부 달랐다: v4→v5, callcenter→callCenter(대문자 C),
        그리고 파라미터가 answeredType 이 아니라 partnerCounselingStatus 다.
        (예전 코드는 404 PRECONDITION_FAILED 만 받았다)
    counseling_status: NONE(전체) | ANSWER(답변완료) | NO_ANSWER(미답변) | TRANSFER(미확인)
    pageSize 최대 30 · 조회기간 최대 7일 · 날짜 yyyy-MM-dd
    """
    vid = _vendor()
    if not vid:
        return []
    if not (start_at and end_at):
        start_at, end_at = _default_range(days=3)
    path = f"/v2/providers/openapi/apis/api/v5/vendors/{vid}/callCenterInquiries"
    q = (
        f"vendorId={vid}&partnerCounselingStatus={counseling_status}"
        f"&inquiryStartAt={start_at}&inquiryEndAt={end_at}"
        f"&pageNum=1&pageSize={min(page_size, 30)}"
    )
    return _content(_get(path, q))


def get_product_inquiries(*_args, **_kwargs) -> list[dict]:
    """[폐기] 별도의 '상품문의' 엔드포인트는 존재하지 않는다.

    공식문서의 '상품별 고객문의 조회(Customer Inquiry Query by Product)' 가 곧
    onlineInquiries 다. 예전 코드가 시도하던 두 경로는 둘 다 실재하지 않아
    404 PRECONDITION_FAILED 만 반환했다:
        /v2/.../api/v1/marketplace/cs-center/productInquiries
        /v2/.../api/v4/vendors/{vendorId}/productInquiries

    get_online_inquiries() 를 쓸 것. 여기서 같은 엔드포인트를 다시 호출하면
    동일 문의가 이중 집계되므로 빈 목록을 반환한다.
    """
    return []
