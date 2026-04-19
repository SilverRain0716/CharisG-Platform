"""
coupang_service.py — 쿠팡 WING API.

HMAC-SHA256 서명. 상품 등록/수정/주문 조회.
EC2 의존: COUPANG_ACCESS_KEY/SECRET_KEY/VENDOR_ID + IP 화이트리스트.
"""
import hashlib
import hmac
import logging
import time
from typing import Optional
from urllib.parse import quote, urlparse

import requests
from requests.adapters import HTTPAdapter

from backend_shared._config import COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY, COUPANG_VENDOR_ID

logger = logging.getLogger(__name__)

BASE = "https://api-gateway.coupang.com"

# ── HTTP Session (Connection Pool) ────────────────────────────
_SESSION = requests.Session()
_adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20)
_SESSION.mount("https://", _adapter)
_SESSION.mount("http://", _adapter)


# ── 카테고리 제한/금지 단어 (등록 시 자동 _skip 처리) ─────────
SKIP_KEYWORD_PATTERNS = (
    "카테고리",  # "해당 카테고리에 등록 불가"
    "판매 불가",
    "등록 불가",
    "허용되지 않",
    "권한이 없",
)


def _normalize_query(query: str) -> str:
    """쿠팡 서명용 query string 정규화 — 키 ASCII 정렬 + URL encode (RFC 3986)."""
    if not query:
        return ""
    # 이미 정렬된 raw string을 받기 때문에 단순 통과 (호출자가 정렬을 보장)
    # 다중 파라미터 정렬이 필요한 경우 호출자가 사전에 정렬해서 넘김
    return query


def _signature(method: str, path: str, query: str = "") -> dict:
    """HMAC-SHA256 서명 헤더 생성.

    쿠팡 spec:
        message = timestamp + HTTP_METHOD + PATH + QUERY_STRING
        timestamp = yyMMdd'T'HHmmss'Z' (UTC)
    """
    ts = time.strftime("%y%m%dT%H%M%SZ", time.gmtime())
    query = _normalize_query(query)
    message = ts + method + path + query
    sig = hmac.new(
        COUPANG_SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Authorization": (
            f"CEA algorithm=HmacSHA256, access-key={COUPANG_ACCESS_KEY}, "
            f"signed-date={ts}, signature={sig}"
        ),
        "Content-Type": "application/json",
    }


def _request_with_retry(
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    json: Optional[dict] = None,
    timeout: int = 15,
    max_retries: int = 3,
) -> Optional[requests.Response]:
    """5xx/timeout retry + exponential backoff."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            r = _SESSION.request(method, url, headers=headers, json=json, timeout=timeout)
            if r.status_code < 500:
                return r
            logger.warning(f"쿠팡 {method} {urlparse(url).path} 5xx — attempt {attempt + 1}/{max_retries} (status={r.status_code})")
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            logger.warning(f"쿠팡 {method} {urlparse(url).path} timeout/conn — attempt {attempt + 1}/{max_retries}: {e}")
        time.sleep(2 ** attempt)  # 1s, 2s, 4s
    if last_exc:
        logger.error(f"쿠팡 요청 최종 실패 (예외): {last_exc}")
    return None


def _is_skippable_message(msg: str) -> bool:
    """등록 거절 메시지가 카테고리 제한/판매 불가 등 자동 스킵 대상인지."""
    if not msg:
        return False
    for kw in SKIP_KEYWORD_PATTERNS:
        if kw in msg:
            return True
    return False


def _extract_error_messages(body: dict) -> list[str]:
    """쿠팡 응답에서 사람이 읽을 에러 메시지 목록을 추출."""
    msgs = []
    if isinstance(body, dict):
        if body.get("message"):
            msgs.append(body["message"])
        for inv in body.get("invalidParameters", []) or []:
            if isinstance(inv, dict) and inv.get("message"):
                msgs.append(inv["message"])
        for d in body.get("data", []) if isinstance(body.get("data"), list) else []:
            if isinstance(d, dict) and d.get("message"):
                msgs.append(d["message"])
    return msgs


def register_product(payload: dict) -> Optional[dict]:
    """상품 등록 (POST /v2/providers/seller_api/apis/api/v1/marketplace/seller-products).

    응답 분기:
        - 2xx: r.json() 그대로 반환 (caller가 data.sellerProductId 사용)
        - 4xx + 카테고리 제한/금지 메시지: {"_skip": reason}
        - 4xx 그 외: None + 에러 로그
        - 5xx/timeout: _request_with_retry로 3회 재시도 후 None
    """
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        logger.warning("COUPANG_* 미설정")
        return None
    path = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
    try:
        r = _request_with_retry(
            "POST",
            BASE + path,
            headers=_signature("POST", path),
            json=payload,
            timeout=30,
        )
        if r is None:
            return None
        if r.status_code < 400:
            return r.json()

        body = r.json() if r.text else {}
        msgs = _extract_error_messages(body)
        skip_msgs = [m for m in msgs if _is_skippable_message(m)]
        if skip_msgs:
            reason = skip_msgs[0]
            logger.warning(f"쿠팡 등록 스킵 (카테고리 제한): {reason}")
            return {"_skip": reason}

        logger.error(f"쿠팡 상품 등록 실패: {r.status_code} {r.text[:300]}")
        return None
    except Exception as e:
        logger.error(f"쿠팡 등록 예외: {e}")
        return None


def get_orders(start: str, end: str) -> Optional[list]:
    path = f"/v2/providers/openapi/apis/api/v4/vendors/{COUPANG_VENDOR_ID}/ordersheets"
    query = f"createdAtFrom={start}&createdAtTo={end}&status=ACCEPT"
    try:
        r = _request_with_retry(
            "GET",
            BASE + path + "?" + query,
            headers=_signature("GET", path, query),
            timeout=15,
        )
        if r is None:
            return None
        return r.json().get("data", [])
    except Exception as e:
        logger.error(f"쿠팡 주문 조회 실패: {e}")
        return None
