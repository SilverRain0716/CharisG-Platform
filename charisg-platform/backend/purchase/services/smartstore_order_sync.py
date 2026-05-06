"""
smartstore_order_sync.py — 네이버 커머스(스마트스토어) 주문 → orders 테이블 upsert.

쿠팡과 동일 패턴:
  1. naver_commerce_service.get_changed_product_orders 로 변경된 ID 조회
  2. naver_commerce_service.get_product_order_details 로 상세 일괄 조회
  3. 응답 dict 를 orders 스키마로 매핑 후 receive_order 로 INSERT OR IGNORE

매핑 키:
  listings_pa.channel='smartstore' 의 channel_product_id 는 네이버
  originProductNo 가 저장되어 있다 (smartstore_lister.py 참조).
"""
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# 한국 개인통관고유부호: P + 12자리 영숫자 (예: P892155820211)
PCC_PATTERN = re.compile(r'^P[0-9A-Z]{12}$', re.IGNORECASE)


def _pick(d: dict, *keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def _mask_pcc(s: str) -> str:
    if not s or len(s) < 7:
        return '***'
    return f"{s[:3]}***{s[-3:]}"


def _find_pcc_recursive(obj, path: str = '', _depth: int = 0) -> Optional[tuple[str, str]]:
    """응답 어디서든 PCC 패턴(P+12자리)을 가진 string 값을 찾는다.

    네이버 API 의 PCC 필드명/위치가 명세에 명확하지 않아 여러 후보 경로를 모두 시도하기 위함.
    첫 매칭의 (json_path, value) 반환. 명시 매핑이 실패했을 때만 fallback 으로 사용.
    """
    if _depth > 8:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f'{path}.{k}' if path else k
            if isinstance(v, str) and PCC_PATTERN.match(v.strip()):
                return (p, v.strip())
            r = _find_pcc_recursive(v, p, _depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for i, x in enumerate(obj):
            r = _find_pcc_recursive(x, f'{path}[{i}]', _depth + 1)
            if r:
                return r
    return None


def _dump_keys_only(obj, _depth: int = 0, _max_depth: int = 4):
    """PII 노출 없이 응답의 nested key 구조만 dump. 진단용."""
    if _depth > _max_depth:
        return '...'
    if isinstance(obj, dict):
        return {k: _dump_keys_only(v, _depth + 1, _max_depth) for k, v in obj.items()}
    if isinstance(obj, list):
        if obj:
            return [_dump_keys_only(obj[0], _depth + 1, _max_depth)]
        return []
    return type(obj).__name__


def _map_naver_order_to_row(entry: dict, product_id_by_origin: dict) -> Optional[dict]:
    """네이버 주문 상세 응답 1건 → orders 테이블 row dict.

    네이버 응답 구조 (관찰 기반, 누락 시 None 처리):
      entry = {
        "productOrder": {productOrderId, productName, originProductNo, channelProductNo,
                         quantity, productPrice, totalPaymentAmount, ...},
        "order": {orderId, ordererName, ordererTel, orderDate, paymentDate, ...},
        "delivery": {...},
        "shippingAddress": {name, tel1, tel2, baseAddress, detailedAddress, zipCode, ...},
        "personalCustomsClearanceCode": "...",
      }
    """
    if not isinstance(entry, dict):
        return None
    po = entry.get("productOrder") or {}
    order = entry.get("order") or {}
    # shippingAddress 는 productOrder 내부에 nested. fallback 으로 entry.shippingAddress 도 확인.
    addr = po.get("shippingAddress") or entry.get("shippingAddress") or {}

    product_order_id = _pick(po, "productOrderId")
    if not product_order_id:
        return None

    # 네이버 응답 productOrder.originalProductId 가 우리 listings_pa.channel_product_id (originProductNo).
    origin_no = str(
        _pick(po, "originalProductId", "originProductNo", "channelProductNo") or ""
    )
    product_id = product_id_by_origin.get(origin_no) if origin_no else None

    # multi-option 매핑: channelProductNo 가 listing_options.channel_option_id
    # (네이버는 옵션 단위로 channelProductNo 별도 발급).
    child_product_id = None
    child_asin = None
    channel_product_no = str(_pick(po, "channelProductNo") or "")
    if channel_product_no:
        try:
            from backend.purchase.database import get_db
            with get_db() as conn:
                row = conn.execute(
                    """SELECT lo.child_product_id, p.asin
                       FROM listing_options lo
                       LEFT JOIN products p ON p.id = lo.child_product_id
                       WHERE lo.channel_option_id = ? LIMIT 1""",
                    (channel_product_no,),
                ).fetchone()
            if row:
                child_product_id = row["child_product_id"]
                child_asin = row["asin"]
        except Exception as e:
            logger.warning(f"[smartstore-order-map] channelProductNo={channel_product_no} child 조회 실패: {e}")

    customer_name = (
        _pick(addr, "name", "receiverName")
        or _pick(order, "ordererName")
        or "—"
    )
    customer_phone = (
        _pick(addr, "tel1", "receiverTel1")
        or _pick(order, "ordererTel", "ordererTel1")
        or ""
    )
    base_addr = _pick(addr, "baseAddress") or ""
    detail_addr = _pick(addr, "detailedAddress") or ""
    zip_code = _pick(addr, "zipCode") or ""
    address = " ".join(
        p for p in (base_addr, detail_addr, f"({zip_code})" if zip_code else "") if p
    ).strip()

    quantity = int(_pick(po, "quantity", default=1) or 1)
    sale_price = float(
        _pick(po, "totalPaymentAmount", "totalProductAmount", "productPrice", default=0)
        or 0
    )

    # PCC 매핑: 정식 필드 productOrder.individualCustomUniqueCode (2026-05-05 응답 확인).
    # 호환성을 위해 personalCustomsClearanceCode 도 시도, 마지막은 PCC 패턴 재귀 fallback.
    pcc = (
        _pick(po, "individualCustomUniqueCode")
        or _pick(entry, "individualCustomUniqueCode")
        or _pick(addr, "individualCustomUniqueCode")
        or _pick(entry, "personalCustomsClearanceCode")
        or _pick(po, "personalCustomsClearanceCode")
        or _pick(addr, "personalCustomsClearanceCode")
    )
    if not pcc:
        found = _find_pcc_recursive(entry)
        if found:
            pcc_path, pcc_val = found
            logger.info(
                "[smartstore-order-map] PCC fallback hit: path=%s value=%s (productOrderId=%s)",
                pcc_path, _mask_pcc(pcc_val), product_order_id,
            )
            pcc = pcc_val
        else:
            # 응답에 PCC 패턴 자체가 없는 경우 — 진단용으로 키 구조만 한 번 dump
            logger.warning(
                "[smartstore-order-map] PCC 미발견 (productOrderId=%s) — keys=%s",
                product_order_id, _dump_keys_only(entry),
            )

    return {
        "channel": "smartstore",
        "channel_order_id": str(product_order_id),
        "product_id": product_id,
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "address": address,
        "sale_price_krw": sale_price,
        "quantity": quantity,
        # v13 확장
        "customs_clearance_code": pcc or None,
        "orderer_real_phone": _pick(order, "ordererTel", "ordererTel1") or None,
        "shipping_message": _pick(po, "shippingMemo") or _pick(addr, "shippingMemo") or None,
        "external_sku": _pick(po, "optionManageCode", "sellerProductCode") or None,
        "ordered_at": _pick(order, "orderDate") or None,
        "paid_at": _pick(order, "paymentDate") or None,
        # v18 옵션 식별
        "child_product_id": child_product_id,
        "child_asin": child_asin,
    }


def sync_orders(
    last_changed_from: str,
    last_changed_to: Optional[str] = None,
    last_changed_type: str = "PAYED",
) -> dict:
    """네이버 주문 변경 → orders 테이블 upsert.

    last_changed_from / last_changed_to: ISO8601+offset (예: "2026-04-24T00:00:00.000+09:00")
    last_changed_type: 기본 PAYED (결제완료). 필요 시 DISPATCHED 등 별도 호출.

    반환 (쿠팡 sync_orders 와 동일 형식):
      {fetched, inserted, duplicated, unmapped, errors, new_order_ids}
    """
    from backend.purchase.database import get_db, get_db_hot
    from backend.purchase.services.order_receiver_service import receive_order
    from backend.purchase.services.naver_commerce_service import (
        get_changed_product_orders,
        get_product_order_details,
    )

    ids = get_changed_product_orders(
        last_changed_from, last_changed_to, last_changed_type=last_changed_type
    )
    if not ids:
        return {
            "fetched": 0, "inserted": 0, "duplicated": 0,
            "unmapped": 0, "errors": 0, "new_order_ids": [],
        }

    details = get_product_order_details(ids)

    # originProductNo → product_id 매핑 일괄 로드
    with get_db() as conn:
        rows = conn.execute(
            """SELECT product_id, channel_product_id FROM listings_pa
               WHERE channel='smartstore' AND channel_product_id IS NOT NULL"""
        ).fetchall()
    product_id_by_origin = {str(r["channel_product_id"]): r["product_id"] for r in rows}

    inserted = 0
    duplicated = 0
    unmapped = 0
    errors = 0
    new_order_ids: list[int] = []

    if details:
        # 디버그 — 첫 응답 구조 한 번만 로그
        logger.info(
            "[smartstore-order-sync] sample keys: %s",
            list(details[0].keys()) if isinstance(details[0], dict) else type(details[0]).__name__,
        )

    for entry in details:
        try:
            mapped = _map_naver_order_to_row(entry, product_id_by_origin)
            if mapped is None:
                errors += 1
                logger.warning(
                    "[smartstore-order-sync] 매핑 실패 (productOrderId 없음): %s",
                    str(entry)[:300],
                )
                continue
            if mapped["product_id"] is None:
                unmapped += 1
            order_id, is_new = receive_order(**mapped)
            if is_new:
                inserted += 1
                if order_id:
                    new_order_ids.append(order_id)
            else:
                duplicated += 1
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
            logger.warning(
                "[smartstore-order-sync] 단건 처리 실패: %s (entry=%s)",
                e, str(entry)[:200],
            )

    return {
        "fetched": len(details),
        "inserted": inserted,
        "duplicated": duplicated,
        "unmapped": unmapped,
        "errors": errors,
        "new_order_ids": new_order_ids,
    }
