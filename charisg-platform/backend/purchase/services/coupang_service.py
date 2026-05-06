"""
coupang_service.py — 쿠팡 WING API.

HMAC-SHA256 서명. 상품 등록/수정/주문 조회.
EC2 의존: COUPANG_ACCESS_KEY/SECRET_KEY/VENDOR_ID + IP 화이트리스트.
"""
import hashlib
import hmac
import json
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
        # 옵션/단위 오류는 원인 분석을 위해 items[*].attributes 를 함께 덤프 (40건 디버깅용).
        if "옵션" in err_summary or "단위" in err_summary:
            try:
                import json as _json
                items_attrs = [it.get("attributes") for it in (payload.get("items") or [])]
                logger.error(f"쿠팡 옵션 오류 payload.items.attributes: {_json.dumps(items_attrs, ensure_ascii=False)[:600]}")
            except Exception:
                pass
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


def list_all_seller_products(
    max_per_page: int = 100,
    page_sleep: float = 0.12,
    on_progress: Optional[callable] = None,
) -> list[dict]:
    """전체 셀러상품 목록 페이징 조회 (status 무관 — 모두).

    GET /v2/.../marketplace/seller-products?vendorId=...&nextToken=...&maxPerPage=100
    응답에 totalCount 필드가 없어 nextToken 이 빌 때까지 페이지를 끝까지 돈다.

    Returns:
        각 dict 는 sellerProductId, statusName, productName 등 쿠팡 응답 원본 키 포함.
    """
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return []
    path = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
    next_token = ""
    out: list[dict] = []
    page = 0
    while True:
        qs = f"vendorId={COUPANG_VENDOR_ID}&nextToken={next_token}&maxPerPage={max_per_page}"
        try:
            r = _request_with_retry(
                "GET", BASE + path + "?" + qs,
                headers=_signature("GET", path, qs), timeout=20,
            )
        except Exception as e:
            logger.error(f"seller-products 페이지 {page+1} 요청 실패: {e}")
            break
        if r is None or r.status_code >= 400:
            logger.warning(
                f"seller-products 페이지 {page+1} 응답 오류: "
                f"{r.status_code if r else 'None'}"
            )
            break
        try:
            body = r.json()
        except Exception:
            body = {}
        for d in body.get("data") or []:
            out.append(d)
        next_token = body.get("nextToken") or ""
        page += 1
        if on_progress:
            try:
                on_progress(page, len(out))
            except Exception:
                pass
        if not next_token:
            break
        time.sleep(page_sleep)
    return out


def count_seller_products_by_status() -> dict:
    """전체 셀러상품 카운트 — 상태(statusName) 별 분류.

    Returns:
        {"total": int, "by_status": {"승인완료": N, "임시저장": M, ...}, "fetched_at": ISO}
        실패/credential 미설정 시 total=None, by_status={}.
    """
    from datetime import datetime, timezone
    products = list_all_seller_products()
    by_status: dict = {}
    for p in products:
        st = p.get("statusName") or "unknown"
        by_status[st] = by_status.get(st, 0) + 1
    return {
        "total": len(products),
        "by_status": by_status,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def update_vendor_item_price(vendor_item_id: str, sale_price: int) -> tuple[bool, str]:
    """vendorItem 의 판매가 변경 (PUT /vendor-items/{id}/prices/{price}).

    승인 불필요 (가격만 부분 변경). 30% 이상 인상은 일부 카테고리에서 거부될 수 있음.
    반환: (성공 여부, 메시지)
    """
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return False, "credentials missing"
    if not vendor_item_id or sale_price <= 0:
        return False, "invalid input"
    path = f"/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vendor_item_id}/prices/{int(sale_price)}"
    try:
        r = _request_with_retry("PUT", BASE + path, headers=_signature("PUT", path), timeout=15)
    except Exception as e:
        return False, f"exception: {e}"
    if r is None:
        return False, "no response"
    if r.status_code >= 400:
        return False, f"http {r.status_code}: {r.text[:300]}"
    body = r.json() if r.text else {}
    code = body.get("code")
    if code and str(code).upper() not in ("SUCCESS", "0"):
        return False, f"api code={code} msg={body.get('message')[:200] if body.get('message') else ''}"
    return True, "ok"


def get_vendor_item_ids(seller_product_id: str) -> list[str]:
    """sellerProductId 의 모든 vendorItemId 추출 (GET seller-products/{id} → items[*])."""
    body = get_seller_product(seller_product_id)
    if not body:
        return []
    data = body.get("data") or {}
    items = data.get("items") or []
    return [str(it.get("vendorItemId")) for it in items if it.get("vendorItemId")]


def request_approval(seller_product_id: str) -> tuple[bool, str]:
    """임시저장된 셀러상품에 대해 승인 요청 전송
    (PUT /v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{id}/approvals).

    register_product 를 requested=False 로 호출한 뒤 이 API 를 호출해야 쿠팡 심사가 시작된다.
    """
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return False, "COUPANG_* 미설정"
    if not seller_product_id:
        return False, "seller_product_id 없음"
    path = f"/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{seller_product_id}/approvals"
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


def update_product_name(seller_product_id: str, new_name: str, dry_run: bool = False) -> tuple[bool, str]:
    """셀러상품 이름만 변경 — GET → strip → PUT 전체 payload.

    쿠팡 PUT /seller-products 는 partial 미지원이라 GET 응답을 그대로 보내야 한다.
    sellerProductName + items[].itemName 두 곳을 _clean_product_name 으로 정리한다.

    재승인 흐름: PUT 이후 statusName 이 '승인대기' 로 돌아가며, 일부 카테고리는 노출 일시 중단될 수 있다.
    dry_run=True 면 PUT 직전까지 진행 후 정리된 payload 반환만.
    """
    from backend.purchase.services.coupang_lister import _clean_product_name

    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return False, "COUPANG_* 미설정"
    if not seller_product_id or not new_name:
        return False, "seller_product_id/new_name 비어있음"

    info = get_seller_product(seller_product_id)
    if not info or not isinstance(info, dict):
        return False, "조회 실패"
    data = info.get("data")
    if not isinstance(data, dict):
        return False, f"data 없음 (code={info.get('code')})"

    cleaned = _clean_product_name(new_name)
    if not cleaned:
        return False, "cleaned 이름 비어있음"
    data["sellerProductName"] = cleaned
    for it in data.get("items") or []:
        if it.get("itemName"):
            it["itemName"] = _clean_product_name(it["itemName"]) or it["itemName"]

    if dry_run:
        return True, f"dry_run ok — sellerProductName='{cleaned}' items={len(data.get('items') or [])}"

    path = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
    try:
        r = _request_with_retry("PUT", BASE + path, headers=_signature("PUT", path), json=data, timeout=30)
        if r is None:
            return False, "no response"
        body = r.json() if r.text else {}
        if r.status_code < 400 and isinstance(body, dict) and body.get("code") != "ERROR":
            return True, ""
        msgs = _extract_error_messages(body)
        return False, f"status={r.status_code} " + ("; ".join(msgs) if msgs else r.text[:200])
    except Exception as e:
        return False, f"예외: {e}"


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


def get_orders(start: str, end: str, status: str = "ACCEPT") -> Optional[list]:
    """WING ordersheet 조회 (GET /v2/.../vendors/{VENDOR_ID}/ordersheets).

    start/end: **yyyy-MM-dd** (KST 날짜) — 쿠팡 API 엄격 검증. 시각 포함하면 400.
    status: ACCEPT(결제완료)|INSTRUCT|DEPARTURE|DELIVERING|FINAL_DELIVERY|NONE_TRACKING|CANCEL 등.
    """
    path = f"/v2/providers/openapi/apis/api/v4/vendors/{COUPANG_VENDOR_ID}/ordersheets"
    query = f"createdAtFrom={start}&createdAtTo={end}&status={status}"
    try:
        r = _request_with_retry(
            "GET",
            BASE + path + "?" + query,
            headers=_signature("GET", path, query),
            timeout=15,
        )
        if r is None:
            return None
        body = r.json() if r.text else {}
        data = body.get("data", []) if isinstance(body, dict) else []
        # 진단: 4xx/ERROR 또는 빈 응답은 raw body를 WARNING으로 남김.
        code = body.get("code") if isinstance(body, dict) else None
        if r.status_code >= 400 or code == "ERROR" or (not data and code):
            logger.warning(
                "[coupang-get-orders] status=%s code=%s message=%s query=%s body=%s",
                r.status_code, code, body.get("message") if isinstance(body, dict) else None,
                query, str(body)[:500],
            )
        elif not data:
            logger.info(
                "[coupang-get-orders] 빈 응답 status=%s code=%s message=%s query=%s",
                r.status_code, code,
                body.get("message") if isinstance(body, dict) else None, query,
            )
        return data
    except Exception as e:
        logger.error(f"쿠팡 주문 조회 실패: {e}")
        return None


# 첫 호출 한정으로 원본 ordersheet를 INFO 로그에 남김 (필드 확정용).
_ORDERSHEET_SAMPLE_LOGGED = False


def _log_ordersheet_sample(sheet: dict) -> None:
    global _ORDERSHEET_SAMPLE_LOGGED
    if _ORDERSHEET_SAMPLE_LOGGED:
        return
    try:
        logger.info(
            "[coupang-order-sync] ordersheet 샘플 (최초 1회): %s",
            json.dumps(sheet, ensure_ascii=False)[:4000],
        )
    except Exception:
        pass
    _ORDERSHEET_SAMPLE_LOGGED = True


def _pick(d: dict, *keys, default=None):
    """dict에서 여러 후보 키 중 첫 non-empty 값을 반환."""
    for k in keys:
        v = d.get(k) if isinstance(d, dict) else None
        if v not in (None, "", []):
            return v
    return default


def _map_ordersheet_to_order(sheet: dict, product_id_by_seller: dict) -> Optional[dict]:
    """ordersheet 단건 → receive_order 인자 dict. 매핑 실패 시 None."""
    if not isinstance(sheet, dict):
        return None

    # 주문 ID: orderId 단독 사용 (shipmentBoxId는 배송 단위라 중복 가능).
    order_id = _pick(sheet, "orderId", "orderSheetId")
    if order_id is None:
        return None

    # 수령인 정보: receiver 블록 우선, 없으면 orderer.
    receiver = sheet.get("receiver") if isinstance(sheet.get("receiver"), dict) else {}
    orderer = sheet.get("orderer") if isinstance(sheet.get("orderer"), dict) else {}
    oversea = sheet.get("overseaShippingInfoDto") if isinstance(sheet.get("overseaShippingInfoDto"), dict) else {}

    customer_name = _pick(receiver, "name", "receiverName") or _pick(orderer, "name", "ordererName") or ""
    customer_phone = (
        _pick(receiver, "safeNumber", "receiverNumber", "phoneNumber1", "mobile")
        or _pick(orderer, "safeNumber", "phoneNumber", "mobile")
        or ""
    )
    addr1 = _pick(receiver, "addr1", "address1", "receiverAddr1") or ""
    addr2 = _pick(receiver, "addr2", "address2", "receiverAddr2") or ""
    zip_code = _pick(receiver, "postCode", "zipCode") or ""
    address_parts = [p for p in (addr1, addr2, f"({zip_code})" if zip_code else "") if p]
    address = " ".join(address_parts).strip()

    # 주문 아이템 — 멀티 아이템이면 첫 번째 기준 (수량·금액은 합산).
    items = sheet.get("orderItems") or []
    if not items:
        return None

    total_price = 0.0
    total_qty = 0
    first_seller_pid = None
    first_sku = None
    first_vendor_item_id = None
    for it in items:
        if not isinstance(it, dict):
            continue
        qty = int(_pick(it, "shippingCount", "orderedUnit", "quantity", default=1) or 1)
        price = float(_pick(it, "orderPrice", "salesPrice", "discountPrice", default=0) or 0)
        # orderPrice는 보통 단가. 합계 계산.
        total_qty += qty
        total_price += price * qty
        if first_seller_pid is None:
            first_seller_pid = str(_pick(it, "sellerProductId", "sellerProductItemId", "productId", default="") or "")
        if first_vendor_item_id is None:
            first_vendor_item_id = str(_pick(it, "vendorItemId") or "")
        if first_sku is None:
            first_sku = _pick(it, "externalVendorSkuCode", "vendorSkuCode") or None

    product_id = product_id_by_seller.get(first_seller_pid) if first_seller_pid else None

    # multi-option 매핑: vendorItemId → listing_options.child_product_id (Phase 3-F)
    child_product_id = None
    child_asin = None
    if first_vendor_item_id:
        try:
            from backend.purchase.database import get_db
            with get_db() as conn:
                row = conn.execute(
                    """SELECT lo.child_product_id, p.asin
                       FROM listing_options lo
                       LEFT JOIN products p ON p.id = lo.child_product_id
                       WHERE lo.channel_option_id = ? LIMIT 1""",
                    (first_vendor_item_id,),
                ).fetchone()
            if row:
                child_product_id = row["child_product_id"]
                child_asin = row["asin"]
        except Exception as e:
            logger.warning(f"[coupang-order-map] vendorItemId={first_vendor_item_id} child 조회 실패: {e}")

    return {
        "channel": "coupang",
        "channel_order_id": str(order_id),
        "product_id": product_id,
        "customer_name": customer_name or "—",
        "customer_phone": customer_phone or "",
        "address": address or "",
        "sale_price_krw": total_price,
        "quantity": total_qty or 1,
        # v13 확장
        "customs_clearance_code": _pick(oversea, "personalCustomsClearanceCode") or None,
        "orderer_real_phone": _pick(oversea, "ordererPhoneNumber") or None,
        "shipping_message": _pick(sheet, "parcelPrintMessage") or None,
        "external_sku": first_sku,
        "ordered_at": _pick(sheet, "orderedAt") or None,
        "paid_at": _pick(sheet, "paidAt") or None,
        # v18 옵션 식별
        "child_product_id": child_product_id,
        "child_asin": child_asin,
    }


def sync_orders(start: str, end: str, status: str = "ACCEPT") -> dict:
    """쿠팡 ordersheet 조회 → orders 테이블 upsert.

    반환: {"fetched": N, "inserted": M, "duplicated": K, "unmapped": P, "errors": E}
    - fetched: 쿠팡에서 받은 ordersheet 건수
    - inserted: orders 테이블에 신규 insert 된 건수
    - duplicated: 이미 존재하는 주문 (INSERT OR IGNORE로 무시)
    - unmapped: product_id 매핑 실패 (sellerProductId가 listings_pa에 없음, 주문은 저장됨)
    - errors: 매핑/저장 예외 발생 건수
    """
    from backend.purchase.database import get_db, get_db_hot
    from backend.purchase.services.order_receiver_service import receive_order

    sheets = get_orders(start, end, status=status)
    if sheets is None:
        return {"fetched": 0, "inserted": 0, "duplicated": 0, "unmapped": 0, "errors": 0, "api_error": True}
    if not sheets:
        return {"fetched": 0, "inserted": 0, "duplicated": 0, "unmapped": 0, "errors": 0}

    if sheets:
        _log_ordersheet_sample(sheets[0])

    # sellerProductId → product_id 매핑 일괄 로드.
    with get_db() as conn:
        rows = conn.execute(
            "SELECT product_id, channel_product_id FROM listings_pa WHERE channel='coupang' AND channel_product_id IS NOT NULL"
        ).fetchall()
    product_id_by_seller = {str(r["channel_product_id"]): r["product_id"] for r in rows}

    inserted = 0
    duplicated = 0
    unmapped = 0
    errors = 0

    new_order_ids: list[int] = []

    for sheet in sheets:
        try:
            mapped = _map_ordersheet_to_order(sheet, product_id_by_seller)
            if mapped is None:
                errors += 1
                logger.warning("[coupang-order-sync] 매핑 실패 (orderId 없음): %s", str(sheet)[:300])
                continue
            if mapped["product_id"] is None:
                unmapped += 1  # 주문은 저장하되 product_id만 NULL
            order_id, is_new = receive_order(**mapped)
            if is_new:
                inserted += 1
                if order_id:
                    new_order_ids.append(order_id)
            else:
                duplicated += 1
                # 기존 row가 v13 이전에 생성됐으면 신규 컬럼들이 NULL.
                # COALESCE로 기존값 우선 + NULL인 곳만 채움 (덮어쓰기 안 함).
                if order_id:
                    with get_db_hot() as conn:
                        conn.execute(
                            """UPDATE orders SET
                                  customs_clearance_code = COALESCE(customs_clearance_code, ?),
                                  orderer_real_phone     = COALESCE(orderer_real_phone, ?),
                                  shipping_message       = COALESCE(shipping_message, ?),
                                  external_sku           = COALESCE(external_sku, ?),
                                  ordered_at             = COALESCE(ordered_at, ?),
                                  paid_at                = COALESCE(paid_at, ?)
                               WHERE id=?""",
                            (
                                mapped.get("customs_clearance_code"),
                                mapped.get("orderer_real_phone"),
                                mapped.get("shipping_message"),
                                mapped.get("external_sku"),
                                mapped.get("ordered_at"),
                                mapped.get("paid_at"),
                                order_id,
                            ),
                        )
        except Exception as e:
            errors += 1
            logger.warning("[coupang-order-sync] 단건 처리 실패: %s (sheet=%s)", e, str(sheet)[:200])

    return {
        "fetched": len(sheets),
        "inserted": inserted,
        "new_order_ids": new_order_ids,
        "duplicated": duplicated,
        "unmapped": unmapped,
        "errors": errors,
    }


# ────────────────────────────────────────────────────────────
# 즉시할인쿠폰 (FMS) — 생성/조회/아이템추가/파기
# 비동기 패턴: write API 는 reqId 반환 → get_request_status 로 결과 polling
# 한도: 아이템 추가 1회 10,000건. 발급 후 아이템 삭제 불가.
# ────────────────────────────────────────────────────────────

_FMS_BASE_V1 = f"/v2/providers/fms/apis/api/v1/vendors/{COUPANG_VENDOR_ID}"
_FMS_BASE_V2 = f"/v2/providers/fms/apis/api/v2/vendors/{COUPANG_VENDOR_ID}"


def create_coupon(
    contract_id: int,
    name: str,
    discount: int,
    max_discount_price: int,
    start_at: str,
    end_at: str,
    type_: str = "RATE",
    wow_exclusive: bool = False,
) -> tuple[bool, str, Optional[str]]:
    """즉시할인쿠폰 생성. (성공여부, 메시지, requestedId).

    type_: RATE(정률 %) / PRICE(정액 원) / FIXED_WITH_QUANTITY(수량별 정액)
    discount: RATE 1-100, PRICE 1+
    start_at/end_at: 'yyyy-MM-dd HH:mm:ss' (start_at은 다음날 00시부터 가능)
    """
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return False, "credentials missing", None
    path = _FMS_BASE_V2 + "/coupon"
    body = {
        "contractId": contract_id,
        "name": name[:45],
        "maxDiscountPrice": int(max_discount_price),
        "discount": int(discount),
        "startAt": start_at,
        "endAt": end_at,
        "type": type_,
        "wowExclusive": "true" if wow_exclusive else "false",
    }
    try:
        r = _request_with_retry(
            "POST", BASE + path,
            headers=_signature("POST", path),
            json=body, timeout=20,
        )
    except Exception as e:
        return False, f"exception: {e}", None
    if r is None:
        return False, "no response", None
    if r.status_code >= 400:
        return False, f"http {r.status_code}: {r.text[:300]}", None
    body_resp = r.json() if r.text else {}
    data = body_resp.get("data") or {}
    if not data.get("success"):
        return False, f"api fail: {body_resp.get('message') or body_resp.get('errorMessage') or r.text[:200]}", None
    req_id = (data.get("content") or {}).get("requestedId")
    return True, "ok", req_id


def add_coupon_items(coupon_id: int, vendor_item_ids: list[int]) -> tuple[bool, str, Optional[str]]:
    """쿠폰에 vendorItem 추가. 1회 10,000개 한도. (ok, msg, reqId)."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return False, "credentials missing", None
    if not vendor_item_ids:
        return False, "empty vendor_item_ids", None
    if len(vendor_item_ids) > 10000:
        return False, f"vendor_items size {len(vendor_item_ids)} > 10000", None
    path = _FMS_BASE_V1 + f"/coupons/{coupon_id}/items"
    body = {"vendorItems": [int(v) for v in vendor_item_ids]}
    try:
        r = _request_with_retry(
            "POST", BASE + path,
            headers=_signature("POST", path),
            json=body, timeout=30,
        )
    except Exception as e:
        return False, f"exception: {e}", None
    if r is None:
        return False, "no response", None
    if r.status_code >= 400:
        return False, f"http {r.status_code}: {r.text[:300]}", None
    body_resp = r.json() if r.text else {}
    data = body_resp.get("data") or {}
    if not data.get("success"):
        return False, f"api fail: {body_resp.get('message') or r.text[:200]}", None
    return True, "ok", (data.get("content") or {}).get("requestedId")


def get_request_status(requested_id: str) -> Optional[dict]:
    """async 요청 결과 조회. content 반환 (status, type, couponId, succeeded, failed, failedVendorItems)."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return None
    path = _FMS_BASE_V1 + f"/requested/{requested_id}"
    try:
        r = _request_with_retry("GET", BASE + path, headers=_signature("GET", path), timeout=15)
    except Exception as e:
        logger.error(f"[coupon] request status 예외: {e}")
        return None
    if r is None or r.status_code >= 400:
        return None
    body = r.json() if r.text else {}
    return (body.get("data") or {}).get("content")


def wait_for_request(requested_id: str, timeout: int = 180, interval: float = 2.0) -> Optional[dict]:
    """status가 DONE/FAIL 될 때까지 polling. content 반환."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        c = get_request_status(requested_id)
        if c and c.get("status") in ("DONE", "FAIL"):
            return c
        time.sleep(interval)
    return get_request_status(requested_id)  # 마지막 한 번 더


def expire_coupon(coupon_id: int) -> tuple[bool, str, Optional[str]]:
    """쿠폰 파기 (action=expire). (ok, msg, reqId)."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return False, "credentials missing", None
    path = _FMS_BASE_V1 + f"/coupons/{coupon_id}"
    qs = "action=expire"
    try:
        r = _request_with_retry(
            "PUT", BASE + path + "?" + qs,
            headers=_signature("PUT", path, qs),
            timeout=20,
        )
    except Exception as e:
        return False, f"exception: {e}", None
    if r is None:
        return False, "no response", None
    if r.status_code >= 400:
        return False, f"http {r.status_code}: {r.text[:300]}", None
    body_resp = r.json() if r.text else {}
    data = body_resp.get("data") or {}
    if not data.get("success"):
        return False, f"api fail: {body_resp.get('message') or r.text[:200]}", None
    return True, "ok", (data.get("content") or {}).get("requestedId")


def get_coupon(coupon_id: int) -> Optional[dict]:
    """쿠폰 단건 조회 (contractId/status/type 등)."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return None
    path = _FMS_BASE_V2 + "/coupon"
    qs = f"couponId={coupon_id}"
    try:
        r = _request_with_retry("GET", BASE + path + "?" + qs, headers=_signature("GET", path, qs), timeout=15)
    except Exception:
        return None
    if r is None or r.status_code >= 400:
        return None
    return ((r.json() or {}).get("data") or {}).get("content")


def list_coupons(status: str, page: int = 1, size: int = 20, sort: str = "desc") -> Optional[list]:
    """쿠폰 목록 조회 by status (STANDBY/APPLIED/PAUSED/EXPIRED/DETACHED)."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return None
    path = _FMS_BASE_V2 + "/coupons"
    qs = f"status={status}&page={page}&size={size}&sort={sort}"
    try:
        r = _request_with_retry("GET", BASE + path + "?" + qs, headers=_signature("GET", path, qs), timeout=15)
    except Exception:
        return None
    if r is None or r.status_code >= 400:
        return None
    return ((r.json() or {}).get("data") or {}).get("content") or []


def discover_contract_id() -> Optional[int]:
    """기존 쿠폰 목록에서 contractId 자동 발견. 없으면 None."""
    for st in ("APPLIED", "STANDBY", "PAUSED", "EXPIRED"):
        items = list_coupons(st, page=1, size=1, sort="desc")
        if items:
            cid = items[0].get("contractId")
            if cid:
                return int(cid)
    return None


# ────────────────────────────────────────────────────────────
# 발주서 (v5) + 반품/취소 (v6/v4) — 추가 함수들
# ordersheet v5 응답엔 orderItems[].canceled / cancelCount / holdCountForCancel 포함.
# v6 returnRequests 가 cancel sync 의 핵심 (receiptId 회수).
# ────────────────────────────────────────────────────────────

_OPENAPI_V4 = f"/v2/providers/openapi/apis/api/v4/vendors/{COUPANG_VENDOR_ID}"
_OPENAPI_V5 = f"/v2/providers/openapi/apis/api/v5/vendors/{COUPANG_VENDOR_ID}"
_OPENAPI_V6 = f"/v2/providers/openapi/apis/api/v6/vendors/{COUPANG_VENDOR_ID}"


def _kst_offset_q(date_str: str, suffix_zero: bool = False) -> str:
    """v5 createdAtFrom/To 파라미터 형식 인코딩 (URL 안전, KST '+09:00' → '%2B09:00').

    date_str: 'yyyy-MM-dd' 또는 'yyyy-MM-ddTHH:mm'
    """
    if "T" not in date_str and suffix_zero:
        date_str = date_str + "T00:00"
    return date_str + "%2B09:00"


def get_orders_v5(
    start: str, end: str, status: str = "ACCEPT",
    search_type: str | None = None, max_per_page: int = 50,
    next_token: str = "",
) -> Optional[dict]:
    """v5 ordersheet 목록 조회. 단일 페이지. {data:[...], nextToken}.

    start/end:
      - 일단위 페이징: 'yyyy-MM-dd' (KST 자동)
      - 분단위 (search_type='timeFrame'): 'yyyy-MM-ddTHH:mm'
    """
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return None
    path = _OPENAPI_V5 + "/ordersheets"
    qs_parts = [
        f"createdAtFrom={_kst_offset_q(start, suffix_zero=(search_type=='timeFrame'))}",
        f"createdAtTo={_kst_offset_q(end, suffix_zero=(search_type=='timeFrame'))}",
        f"status={status}",
        f"maxPerPage={max_per_page}",
    ]
    if search_type:
        qs_parts.append(f"searchType={search_type}")
    if next_token:
        qs_parts.append(f"nextToken={next_token}")
    qs = "&".join(qs_parts)
    try:
        r = _request_with_retry("GET", BASE + path + "?" + qs, headers=_signature("GET", path, qs), timeout=20)
    except Exception as e:
        logger.error(f"v5 ordersheet 예외: {e}")
        return None
    if r is None or r.status_code >= 400:
        if r is not None:
            logger.warning(f"v5 ordersheet status={r.status_code} body={r.text[:200]}")
        return None
    body = r.json() if r.text else {}
    return body  # {code, message, data:[], nextToken}


def get_ordersheet_by_box(shipment_box_id: int) -> Optional[dict]:
    """v5 발주서 단건 조회 (shipmentBoxId)."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return None
    path = _OPENAPI_V5 + f"/ordersheets/{shipment_box_id}"
    try:
        r = _request_with_retry("GET", BASE + path, headers=_signature("GET", path), timeout=15)
    except Exception:
        return None
    if r is None or r.status_code >= 400:
        return None
    return ((r.json() or {}).get("data"))


def get_ordersheet_by_order(order_id: int) -> Optional[list]:
    """v5 발주서 단건 조회 (orderId). 같은 orderId 의 여러 ordersheet 가능 → list."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return None
    path = _OPENAPI_V5 + f"/{order_id}/ordersheets"
    try:
        r = _request_with_retry("GET", BASE + path, headers=_signature("GET", path), timeout=15)
    except Exception:
        return None
    if r is None or r.status_code >= 400:
        return None
    body = r.json() or {}
    return body.get("data") or []


def get_ordersheet_history(shipment_box_id: int) -> Optional[list]:
    """v5 배송상태 히스토리 조회. data.details[] 또는 data[]."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return None
    path = _OPENAPI_V5 + f"/ordersheets/{shipment_box_id}/history"
    try:
        r = _request_with_retry("GET", BASE + path, headers=_signature("GET", path), timeout=15)
    except Exception:
        return None
    if r is None or r.status_code >= 400:
        return None
    body = r.json() or {}
    data = body.get("data")
    if isinstance(data, dict):
        return data.get("details") or []
    if isinstance(data, list):
        return data
    return []


def get_return_requests(
    start: str, end: str,
    status: str | None = None,
    cancel_type: str = "RETURN",
    search_type: str | None = None,
    max_per_page: int = 50,
    next_token: str = "",
    order_id: int | None = None,
) -> Optional[dict]:
    """v6 반품/취소 요청 목록 조회. {data:[...], nextToken}.

    cancel_type='CANCEL' 시 status 빼고 호출 가능 (즉시취소 캐치).
    cancel_type='RETURN' 시 status 필수 (RU/UC/CC/PR).
    search_type='timeFrame' 시 분단위, 파라미터: yyyy-MM-ddTHH:mm.
    """
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return None
    path = _OPENAPI_V6 + "/returnRequests"
    qs_parts = [
        f"createdAtFrom={start}",
        f"createdAtTo={end}",
        f"cancelType={cancel_type}",
    ]
    if status:
        qs_parts.append(f"status={status}")
    if search_type:
        qs_parts.append(f"searchType={search_type}")
    if order_id:
        qs_parts.append(f"orderId={order_id}")
    if max_per_page and search_type != "timeFrame":
        qs_parts.append(f"maxPerPage={max_per_page}")
    if next_token and search_type != "timeFrame":
        qs_parts.append(f"nextToken={next_token}")
    qs = "&".join(qs_parts)
    try:
        r = _request_with_retry("GET", BASE + path + "?" + qs, headers=_signature("GET", path, qs), timeout=30)
    except Exception as e:
        logger.error(f"v6 returnRequests 예외: {e}")
        return None
    if r is None or r.status_code >= 400:
        if r is not None:
            logger.warning(f"v6 returnRequests status={r.status_code} body={r.text[:200]}")
        return None
    return r.json() or {}


def get_return_request(receipt_id: int) -> Optional[dict]:
    """v6 반품 단건 조회 (receiptId — RETURN type 만)."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return None
    path = _OPENAPI_V6 + f"/returnRequests/{receipt_id}"
    try:
        r = _request_with_retry("GET", BASE + path, headers=_signature("GET", path), timeout=15)
    except Exception:
        return None
    if r is None or r.status_code >= 400:
        return None
    body = r.json() or {}
    data = body.get("data")
    if isinstance(data, list):
        return data[0] if data else None
    return data


def acknowledge_orders(shipment_box_ids: list[int]) -> tuple[bool, dict]:
    """v4 상품준비중 처리 (ACCEPT → INSTRUCT). 50개 한도."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return False, {"error": "credentials missing"}
    if len(shipment_box_ids) > 50:
        return False, {"error": "max 50"}
    path = _OPENAPI_V4 + "/ordersheets/acknowledgement"
    body = {"vendorId": COUPANG_VENDOR_ID, "shipmentBoxIds": [int(x) for x in shipment_box_ids]}
    try:
        r = _request_with_retry("PUT", BASE + path, headers=_signature("PUT", path), json=body, timeout=30)
    except Exception as e:
        return False, {"error": f"exception: {e}"}
    if r is None:
        return False, {"error": "no response"}
    if r.status_code >= 400:
        return False, {"error": f"http {r.status_code}", "body": r.text[:300]}
    return True, r.json() or {}


def upload_invoice(items: list[dict]) -> tuple[bool, dict]:
    """v4 송장업로드 (INSTRUCT → DEPARTURE). items: [{shipmentBoxId, orderId, vendorItemId, deliveryCompanyCode, invoiceNumber, splitShipping, preSplitShipped, estimatedShippingDate}]."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return False, {"error": "credentials missing"}
    path = _OPENAPI_V4 + "/orders/invoices"
    body = {"vendorId": COUPANG_VENDOR_ID, "orderSheetInvoiceApplyDtos": items}
    try:
        r = _request_with_retry("POST", BASE + path, headers=_signature("POST", path), json=body, timeout=30)
    except Exception as e:
        return False, {"error": f"exception: {e}"}
    if r is None or r.status_code >= 400:
        return False, {"error": f"http {r.status_code if r else 'none'}", "body": (r.text[:300] if r else "")}
    return True, r.json() or {}


def update_invoice(items: list[dict]) -> tuple[bool, dict]:
    """v4 송장업데이트 (이미 등록된 송장 수정)."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return False, {"error": "credentials missing"}
    path = _OPENAPI_V4 + "/orders/updateInvoices"
    body = {"vendorId": COUPANG_VENDOR_ID, "orderSheetInvoiceApplyDtos": items}
    try:
        r = _request_with_retry("POST", BASE + path, headers=_signature("POST", path), json=body, timeout=30)
    except Exception as e:
        return False, {"error": f"exception: {e}"}
    if r is None or r.status_code >= 400:
        return False, {"error": f"http {r.status_code if r else 'none'}", "body": (r.text[:300] if r else "")}
    return True, r.json() or {}


def stop_shipment(receipt_id: int, cancel_count: int) -> tuple[bool, str]:
    """v4 출고중지완료 (발송 전 cancel 확정)."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return False, "credentials missing"
    path = _OPENAPI_V4 + f"/returnRequests/{receipt_id}/stoppedShipment"
    body = {"vendorId": COUPANG_VENDOR_ID, "receiptId": receipt_id, "cancelCount": cancel_count}
    try:
        r = _request_with_retry("PUT", BASE + path, headers=_signature("PUT", path), json=body, timeout=20)
    except Exception as e:
        return False, f"exception: {e}"
    if r is None:
        return False, "no response"
    if r.status_code >= 400:
        return False, f"http {r.status_code}: {r.text[:200]}"
    return True, "ok"


def complete_shipment(receipt_id: int, delivery_company: str, invoice_number: str) -> tuple[bool, str]:
    """v4 이미출고 처리 (이미 발송 후 cancel 처리)."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return False, "credentials missing"
    path = _OPENAPI_V4 + f"/returnRequests/{receipt_id}/completedShipment"
    body = {
        "vendorId": COUPANG_VENDOR_ID, "receiptId": receipt_id,
        "deliveryCompanyCode": delivery_company, "invoiceNumber": invoice_number,
    }
    try:
        r = _request_with_retry("PUT", BASE + path, headers=_signature("PUT", path), json=body, timeout=20)
    except Exception as e:
        return False, f"exception: {e}"
    if r is None or r.status_code >= 400:
        return False, f"http {r.status_code if r else 'none'}: {r.text[:200] if r else ''}"
    return True, "ok"


def confirm_return_received(receipt_id: int) -> tuple[bool, str]:
    """v4 반품상품 입고 확인처리."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return False, "credentials missing"
    path = _OPENAPI_V4 + f"/returnRequests/{receipt_id}/receiveConfirmation"
    body = {"vendorId": COUPANG_VENDOR_ID, "receiptId": receipt_id}
    try:
        r = _request_with_retry("PUT", BASE + path, headers=_signature("PUT", path), json=body, timeout=15)
    except Exception as e:
        return False, f"exception: {e}"
    if r is None or r.status_code >= 400:
        return False, f"http {r.status_code if r else 'none'}: {r.text[:200] if r else ''}"
    return True, "ok"


def approve_return(receipt_id: int, cancel_count: int) -> tuple[bool, str]:
    """v4 반품요청 승인 처리 (환불 진행)."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return False, "credentials missing"
    path = _OPENAPI_V4 + f"/returnRequests/{receipt_id}/approval"
    body = {"vendorId": COUPANG_VENDOR_ID, "receiptId": receipt_id, "cancelCount": cancel_count}
    try:
        r = _request_with_retry("PUT", BASE + path, headers=_signature("PUT", path), json=body, timeout=15)
    except Exception as e:
        return False, f"exception: {e}"
    if r is None or r.status_code >= 400:
        return False, f"http {r.status_code if r else 'none'}: {r.text[:200] if r else ''}"
    return True, "ok"


def get_return_withdraw(cancel_ids: list[int]) -> Optional[list]:
    """v4 반품철회 이력 조회. 50개 한도."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return None
    if len(cancel_ids) > 50:
        return None
    path = _OPENAPI_V4 + "/returnWithdrawList"
    body = {"cancelIds": [int(x) for x in cancel_ids]}
    try:
        r = _request_with_retry("POST", BASE + path, headers=_signature("POST", path), json=body, timeout=15)
    except Exception:
        return None
    if r is None or r.status_code >= 400:
        return None
    body_resp = r.json() or {}
    return body_resp.get("data") or []


def cancel_order(
    order_id: int, vendor_item_ids: list[int], receipt_counts: list[int],
    user_id: str,
    big_cancel_code: str = "CANERR",
    middle_cancel_code: str = "CCTTER",
) -> tuple[bool, dict]:
    """v5 주문상품 취소 처리 (능동 취소). 결제완료/상품준비중만 가능. 판매자점수 하락 위험."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return False, {"error": "credentials missing"}
    if len(vendor_item_ids) != len(receipt_counts):
        return False, {"error": "vendor/count length mismatch"}
    path = _OPENAPI_V5 + f"/orders/{order_id}/cancel"
    body = {
        "orderId": order_id,
        "vendorItemIds": [int(x) for x in vendor_item_ids],
        "receiptCounts": [int(x) for x in receipt_counts],
        "bigCancelCode": big_cancel_code,
        "middleCancelCode": middle_cancel_code,
        "vendorId": COUPANG_VENDOR_ID,
        "userId": user_id,
    }
    try:
        r = _request_with_retry("POST", BASE + path, headers=_signature("POST", path), json=body, timeout=30)
    except Exception as e:
        return False, {"error": f"exception: {e}"}
    if r is None:
        return False, {"error": "no response"}
    if r.status_code >= 400:
        return False, {"error": f"http {r.status_code}", "body": r.text[:300]}
    return True, r.json() or {}


def register_return_invoice(
    receipt_id: int, delivery_company: str, invoice_number: str,
    type_: str = "RETURN", reg_number: str | None = None,
) -> tuple[bool, dict]:
    """v4 회수 송장 등록. type=RETURN/EXCHANGE."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return False, {"error": "credentials missing"}
    path = _OPENAPI_V4 + "/return-exchange-invoices/manual"
    body = {
        "returnExchangeDeliveryType": type_,
        "receiptId": receipt_id,
        "deliveryCompanyCode": delivery_company,
        "invoiceNumber": invoice_number,
    }
    if reg_number:
        body["regNumber"] = reg_number
    try:
        r = _request_with_retry("POST", BASE + path, headers=_signature("POST", path), json=body, timeout=15)
    except Exception as e:
        return False, {"error": f"exception: {e}"}
    if r is None or r.status_code >= 400:
        return False, {"error": f"http {r.status_code if r else 'none'}", "body": (r.text[:300] if r else "")}
    return True, r.json() or {}


def complete_long_term(shipment_box_id: int, invoice_number: str) -> tuple[bool, str]:
    """v4 장기미배송 배송완료 처리 (DEPARTURE 30일+)."""
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        return False, "credentials missing"
    path = _OPENAPI_V4 + "/completeLongTermUndelivery"
    body = {"shipmentBoxId": int(shipment_box_id), "invoiceNumber": invoice_number}
    try:
        r = _request_with_retry("POST", BASE + path, headers=_signature("POST", path), json=body, timeout=20)
    except Exception as e:
        return False, f"exception: {e}"
    if r is None or r.status_code >= 400:
        return False, f"http {r.status_code if r else 'none'}: {r.text[:200] if r else ''}"
    return True, "ok"

