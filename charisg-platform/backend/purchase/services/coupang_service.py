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
        body = r.json() if r.text else {}

        # 쿠팡은 HTTP 200에 body.code='ERROR' 패턴으로 실패를 돌려주기도 함.
        if r.status_code < 400 and isinstance(body, dict) and body.get("code") != "ERROR":
            return body

        msgs = _extract_error_messages(body)
        skip_msgs = [m for m in msgs if _is_skippable_message(m)]
        if skip_msgs:
            reason = skip_msgs[0]
            logger.warning(f"쿠팡 등록 스킵 (카테고리 제한): {reason}")
            return {"_skip": reason}

        err_summary = "; ".join(msgs) if msgs else r.text[:300]
        logger.error(f"쿠팡 상품 등록 실패: status={r.status_code} code={body.get('code') if isinstance(body, dict) else None} {err_summary}")
        return {"_error": err_summary}
    except Exception as e:
        logger.error(f"쿠팡 등록 예외: {e}")
        return None


def get_seller_product(seller_product_id: str) -> Optional[dict]:
    """셀러상품 단건 조회 (GET /v2/.../seller-products/{id}). vendorItemId 추출용."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return None
    if not seller_product_id:
        return None
    path = f"/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{seller_product_id}"
    try:
        r = _request_with_retry("GET", BASE + path, headers=_signature("GET", path), timeout=15)
        if r is None or r.status_code >= 400:
            return None
        return r.json() if r.text else None
    except Exception as e:
        logger.error(f"쿠팡 상품 조회 실패: {e}")
        return None


def request_approval(seller_product_id: str) -> tuple[bool, str]:
    """임시저장된 셀러상품에 대해 승인 요청 전송
    (PUT /v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{id}/requests/approval).

    register_product 를 requested=False 로 호출한 뒤 이 API 를 호출해야 쿠팡 심사가 시작된다.
    """
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return False, "COUPANG_* 미설정"
    if not seller_product_id:
        return False, "seller_product_id 없음"
    path = f"/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{seller_product_id}/requests/approval"
    try:
        r = _request_with_retry("PUT", BASE + path, headers=_signature("PUT", path), timeout=30)
        if r is None:
            return False, "no response"
        body = r.json() if r.text else {}
        if r.status_code < 400 and isinstance(body, dict) and body.get("code") != "ERROR":
            return True, ""
        msgs = _extract_error_messages(body)
        return False, f"status={r.status_code} " + ("; ".join(msgs) if msgs else r.text[:200])
    except Exception as e:
        return False, f"예외: {e}"


def stop_sales_vendor_item(vendor_item_id: str) -> tuple[bool, str]:
    """vendorItem 단위 판매 중지 (PUT /v2/.../vendor-items/{id}/sales/stop)."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return False, "COUPANG_* 미설정"
    if not vendor_item_id:
        return False, "vendor_item_id 없음"
    path = f"/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vendor_item_id}/sales/stop"
    try:
        r = _request_with_retry("PUT", BASE + path, headers=_signature("PUT", path), timeout=30)
        if r is None:
            return False, "no response"
        body = r.json() if r.text else {}
        if r.status_code < 400 and isinstance(body, dict) and body.get("code") != "ERROR":
            return True, ""
        msgs = _extract_error_messages(body)
        return False, f"status={r.status_code} " + ("; ".join(msgs) if msgs else r.text[:200])
    except Exception as e:
        return False, f"예외: {e}"


def stop_sales(seller_product_id: str) -> tuple[bool, str]:
    """sellerProductId 기반 판매 중지 — 셀러상품 조회 → 각 vendorItem 일괄 중지.

    쿠팡은 sellerProduct 자체엔 sales/stop 없음. items[].vendorItemId 단위로만 가능.
    """
    info = get_seller_product(seller_product_id)
    if not info or not isinstance(info, dict):
        return False, "상품 조회 실패"
    data = info.get("data")
    if not isinstance(data, dict):
        return False, f"data 없음 (code={info.get('code')})"
    items = data.get("items") or []
    if not items:
        return False, "items 비어있음 (vendorItemId 없음)"

    ok_count = 0
    fails: list[str] = []
    for it in items:
        vid = str(it.get("vendorItemId") or "").strip()
        if not vid:
            continue
        success, err = stop_sales_vendor_item(vid)
        if success:
            ok_count += 1
        else:
            fails.append(f"vid={vid}: {err}")

    if ok_count and not fails:
        return True, ""
    if ok_count and fails:
        return True, f"부분 성공 ({ok_count}); " + "; ".join(fails[:2])
    return False, "; ".join(fails[:2]) or "모든 item 실패"


def delete_product(seller_product_id: str) -> tuple[bool, str]:
    """셀러상품 삭제 (DELETE /v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{id}).

    반환: (성공여부, 에러메시지)
    - 2xx + code='SUCCESS' → (True, "")
    - 기타 → (False, 에러 요약)
    """
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return False, "COUPANG_* 미설정"
    if not seller_product_id:
        return False, "seller_product_id 없음"
    path = f"/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{seller_product_id}"
    try:
        r = _request_with_retry(
            "DELETE",
            BASE + path,
            headers=_signature("DELETE", path),
            timeout=30,
        )
        if r is None:
            return False, "no response"
        body = r.json() if r.text else {}
        if r.status_code < 400 and isinstance(body, dict) and body.get("code") != "ERROR":
            return True, ""
        msgs = _extract_error_messages(body)
        return False, f"status={r.status_code} " + ("; ".join(msgs) if msgs else r.text[:200])
    except Exception as e:
        return False, f"예외: {e}"


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
