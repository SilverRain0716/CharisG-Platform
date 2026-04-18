"""
offer_registration_service.py — 기존 ASIN에 offer(가격/재고) 등록.

기존 ASIN에 셀러로 붙는 방식:
- GTIN/UPC 불필요
- 최소 payload: price, quantity, condition, fulfillment
- 제목/불릿/설명/이미지는 Amazon 카탈로그 기존 데이터 사용
"""
import json
import logging
from datetime import datetime
from typing import Optional

from sp_api.api import ListingsItemsV20210801

from backend.dropshipping.database import get_db
from backend.dropshipping.services.amazon_sp_api_service import (
    get_credentials,
    get_marketplace,
    get_seller_id,
)
from backend_shared.utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

SKU_PREFIX = "CG-DS-"
DEFAULT_QUANTITY = 100
LEAD_TIME_DAYS = 14
MARKETPLACE_ID = "ATVPDKIKX0DER"

# putListingsItem 제한: 5 req/sec → 안전하게 200 RPM
_listing_limiter = RateLimiter(max_per_minute=200, name="listings_items")


def _make_sku(product_id: int) -> str:
    return f"{SKU_PREFIX}{product_id}"


def build_offer_payload(
    asin: str,
    price: float,
    quantity: int = DEFAULT_QUANTITY,
    condition: str = "new_new",
) -> dict:
    """기존 ASIN에 대한 최소 offer payload 생성.

    신규 ASIN과 달리 title, bullets, description, images, brand 불필요.
    requirements="LISTING_OFFER_ONLY"로 offer 속성만 전송.
    """
    return {
        "productType": "PRODUCT",
        "requirements": "LISTING_OFFER_ONLY",
        "attributes": {
            "merchant_suggested_asin": [{
                "value": asin,
                "marketplace_id": MARKETPLACE_ID,
            }],
            "condition_type": [{
                "value": condition,
                "marketplace_id": MARKETPLACE_ID,
            }],
            "purchasable_offer": [{
                "marketplace_id": MARKETPLACE_ID,
                "currency": "USD",
                "our_price": [{
                    "schedule": [{
                        "value_with_tax": price,
                    }],
                }],
            }],
            "fulfillment_availability": [{
                "fulfillment_channel_code": "DEFAULT",
                "quantity": quantity,
                "lead_time_to_ship_max_days": LEAD_TIME_DAYS,
            }],
        },
    }


def _get_product_for_offer(product_id: int) -> dict:
    """offer 등록에 필요한 상품 정보 조회."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT id, product_name, matched_asin, calculated_price,
                      source_price, stock_quantity
               FROM collected_products WHERE id = ?""",
            (product_id,),
        ).fetchone()
    if not row:
        raise ValueError(f"상품 ID {product_id} 없음")
    product = dict(row)
    if not product.get("matched_asin"):
        raise ValueError(f"상품 {product_id}: matched_asin 없음 (먼저 ASIN 매칭 필요)")
    return product


def validate_offer(product_id: int) -> dict:
    """VALIDATION_PREVIEW 모드로 offer payload 검증.

    실제 리스팅은 생성하지 않고, Amazon 스키마 검증 결과만 반환.
    """
    product = _get_product_for_offer(product_id)
    asin = product["matched_asin"]
    price = product["calculated_price"] or 29.99
    quantity = min(product.get("stock_quantity") or DEFAULT_QUANTITY, DEFAULT_QUANTITY)
    sku = _make_sku(product_id)

    body = build_offer_payload(asin, price, quantity)

    _listing_limiter.wait()
    creds = get_credentials()
    client = ListingsItemsV20210801(credentials=creds, marketplace=get_marketplace())
    seller_id = get_seller_id()

    try:
        result = client.put_listings_item(
            sellerId=seller_id,
            sku=sku,
            marketplaceIds=MARKETPLACE_ID,
            body=body,
            mode="VALIDATION_PREVIEW",
            issueLocale="en_US",
        )
        resp = getattr(result, "payload", result)
    except Exception as e:
        logger.error(f"VALIDATION_PREVIEW 실패 (product_id={product_id}): {e}")
        return {"ok": False, "product_id": product_id, "asin": asin, "error": str(e)}

    status = resp.get("status", "UNKNOWN")
    issues = resp.get("issues", [])
    errors = [i for i in issues if i.get("severity") == "ERROR"]
    warnings = [i for i in issues if i.get("severity") == "WARNING"]

    ok = status in ("VALID", "ACCEPTED") and not errors

    logger.info(
        f"VALIDATION_PREVIEW product_id={product_id} asin={asin}: "
        f"status={status}, errors={len(errors)}, warnings={len(warnings)}"
    )

    return {
        "ok": ok,
        "product_id": product_id,
        "asin": asin,
        "sku": sku,
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "payload": body,
    }


def register_offer(product_id: int, dry_run: bool = True) -> dict:
    """기존 ASIN에 offer 등록.

    dry_run=True: VALIDATION_PREVIEW만 (기본값, 안전)
    dry_run=False: 실제 putListingsItem 호출
    """
    if dry_run:
        return validate_offer(product_id)

    product = _get_product_for_offer(product_id)
    asin = product["matched_asin"]
    price = product["calculated_price"] or 29.99
    quantity = min(product.get("stock_quantity") or DEFAULT_QUANTITY, DEFAULT_QUANTITY)
    sku = _make_sku(product_id)

    body = build_offer_payload(asin, price, quantity)

    _listing_limiter.wait()
    creds = get_credentials()
    client = ListingsItemsV20210801(credentials=creds, marketplace=get_marketplace())
    seller_id = get_seller_id()

    try:
        result = client.put_listings_item(
            sellerId=seller_id,
            sku=sku,
            marketplaceIds=MARKETPLACE_ID,
            body=body,
            issueLocale="en_US",
        )
        resp = getattr(result, "payload", result)
    except Exception as e:
        logger.error(f"offer 등록 실패 (product_id={product_id}): {e}")
        return {"ok": False, "product_id": product_id, "asin": asin, "error": str(e)}

    status = resp.get("status", "UNKNOWN")
    issues = resp.get("issues", [])
    errors = [i for i in issues if i.get("severity") == "ERROR"]
    submission_id = resp.get("submissionId", "")

    ok = status == "ACCEPTED" and not errors

    # DB 업데이트
    if ok:
        now = datetime.utcnow().isoformat()
        with get_db() as conn:
            # listings 테이블에 기록
            existing = conn.execute(
                "SELECT id FROM listings WHERE product_id = ? AND platform = 'amazon'",
                (product_id,),
            ).fetchone()

            if existing:
                conn.execute(
                    """UPDATE listings SET asin = ?, sku = ?, status = 'listed',
                       current_price = ?, current_stock = ?, listed_at = ?, updated_at = ?
                       WHERE id = ?""",
                    (asin, sku, price, quantity, now, now, existing["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO listings
                       (product_id, platform, business_model, asin, sku,
                        status, current_price, current_stock, listed_at, created_at, updated_at)
                       VALUES (?, 'amazon', 'dropship', ?, ?, 'listed', ?, ?, ?, ?, ?)""",
                    (product_id, asin, sku, price, quantity, now, now, now),
                )

            # collected_products 상태 업데이트
            conn.execute(
                "UPDATE collected_products SET status = 'listed', listed_at = ? WHERE id = ?",
                (now, product_id),
            )

        logger.info(f"offer 등록 성공: product_id={product_id} asin={asin} sku={sku}")

    return {
        "ok": ok,
        "product_id": product_id,
        "asin": asin,
        "sku": sku,
        "status": status,
        "submission_id": submission_id,
        "errors": errors,
        "issues": issues,
    }


def batch_register(
    product_ids: Optional[list[int]] = None,
    limit: int = 10,
    dry_run: bool = True,
    progress_cb=None,
) -> dict:
    """매칭된 상품 일괄 offer 등록.

    product_ids 지정 시 해당 상품만, 미지정 시 matched_asin 있고 미등록인 상품.
    """
    with get_db() as conn:
        if product_ids:
            placeholders = ",".join("?" * len(product_ids))
            rows = conn.execute(
                f"""SELECT id FROM collected_products
                    WHERE id IN ({placeholders})
                      AND matched_asin IS NOT NULL AND matched_asin != ''""",
                product_ids,
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT cp.id FROM collected_products cp
                   LEFT JOIN listings l ON l.product_id = cp.id AND l.platform = 'amazon'
                   WHERE cp.matched_asin IS NOT NULL AND cp.matched_asin != ''
                     AND (l.id IS NULL OR l.status = 'candidate')
                   ORDER BY cp.sort_score DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()

    ids = [r["id"] for r in rows]
    total = len(ids)
    success = 0
    failed = 0
    results = []

    for i, pid in enumerate(ids):
        if progress_cb:
            progress_cb("register", i + 1, total, f"등록 중: product_id={pid}")

        try:
            result = register_offer(pid, dry_run=dry_run)
            if result["ok"]:
                success += 1
            else:
                failed += 1
            results.append(result)
        except Exception as e:
            failed += 1
            logger.error(f"offer 등록 실패 (product_id={pid}): {e}")
            results.append({"product_id": pid, "ok": False, "error": str(e)})

    return {
        "processed": total,
        "success": success,
        "failed": failed,
        "dry_run": dry_run,
        "results": results,
    }
