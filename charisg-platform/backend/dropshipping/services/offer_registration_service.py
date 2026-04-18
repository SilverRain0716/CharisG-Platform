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
from backend.dropshipping.services.marketplace_config import (
    get_config, get_lead_time, make_sku, local_to_usd,
)
from backend_shared.utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

DEFAULT_QUANTITY = 100

# putListingsItem 제한: 5 req/sec → 안전하게 200 RPM
_listing_limiter = RateLimiter(max_per_minute=200, name="listings_items")


def build_offer_payload(
    asin: str,
    price: float,
    quantity: int = DEFAULT_QUANTITY,
    condition: str = "new_new",
    market: str = "US",
    warehouse_country: str = "US",
) -> dict:
    """기존 ASIN에 대한 최소 offer payload 생성.

    마켓별 currency, marketplace_id, lead_time 자동 적용.
    """
    cfg = get_config(market)
    marketplace_id = cfg["marketplace_id"]
    currency = cfg["currency"]
    lead_time = get_lead_time(market, warehouse_country)

    return {
        "productType": "PRODUCT",
        "requirements": "LISTING_OFFER_ONLY",
        "attributes": {
            "merchant_suggested_asin": [{
                "value": asin,
                "marketplace_id": marketplace_id,
            }],
            "condition_type": [{
                "value": condition,
                "marketplace_id": marketplace_id,
            }],
            "purchasable_offer": [{
                "marketplace_id": marketplace_id,
                "currency": currency,
                "our_price": [{
                    "schedule": [{
                        "value_with_tax": price,
                    }],
                }],
            }],
            "fulfillment_availability": [{
                "fulfillment_channel_code": "DEFAULT",
                "quantity": quantity,
                "lead_time_to_ship_max_days": lead_time,
            }],
        },
    }


def _get_product_for_offer(product_id: int, market: str = "US") -> dict:
    """offer 등록에 필요한 상품 정보 조회 + 가격 자동 조정."""
    cfg = get_config(market)
    with get_db() as conn:
        row = conn.execute(
            """SELECT cp.id, cp.product_name, cp.matched_asin, cp.calculated_price,
                      cp.source_price, cp.stock_quantity, cp.search_keyword,
                      cp.warehouse_country, cp.us_warehouse,
                      asg.price_p75
               FROM collected_products cp
               LEFT JOIN amazon_search_agg asg ON cp.search_keyword = asg.keyword
               WHERE cp.id = ?""",
            (product_id,),
        ).fetchone()
    if not row:
        raise ValueError(f"상품 ID {product_id} 없음")
    product = dict(row)
    if not product.get("matched_asin"):
        raise ValueError(f"상품 {product_id}: matched_asin 없음 (먼저 ASIN 매칭 필요)")

    product["warehouse_country"] = product.get("warehouse_country") or ("US" if product.get("us_warehouse") else "CN")

    # 가격 자동 조정 — p75 이하로, 최소 마진 보장
    calc = product["calculated_price"] or 29.99
    p75 = product.get("price_p75")
    src = product["source_price"] or 0
    min_margin_mult = cfg["min_margin_mult"]
    min_price = src * min_margin_mult

    if p75 and calc > p75:
        adjusted = round(max(p75, min_price), 2)
        if adjusted < min_price:
            raise ValueError(
                f"상품 {product_id}: 시장가(p75=${p75:.2f}) < 최소마진(${min_price:.2f}), 수익 불가"
            )
        logger.info(f"가격 조정: {product_id} ${calc:.2f} → ${adjusted:.2f} (p75=${p75:.2f})")
        product["calculated_price"] = adjusted

    return product


def validate_offer(product_id: int, market: str = "US") -> dict:
    """VALIDATION_PREVIEW 모드로 offer payload 검증."""
    cfg = get_config(market)
    product = _get_product_for_offer(product_id, market)
    asin = product["matched_asin"]
    price = product["calculated_price"] or 29.99
    quantity = min(product.get("stock_quantity") or DEFAULT_QUANTITY, DEFAULT_QUANTITY)
    sku = make_sku(product_id, market)
    wh = product.get("warehouse_country", "US")

    body = build_offer_payload(asin, price, quantity, market=market, warehouse_country=wh)

    _listing_limiter.wait()
    creds = get_credentials()
    client = ListingsItemsV20210801(credentials=creds, marketplace=get_marketplace())
    seller_id = get_seller_id()

    try:
        result = client.put_listings_item(
            sellerId=seller_id,
            sku=sku,
            marketplaceIds=cfg["marketplace_id"],
            body=body,
            mode="VALIDATION_PREVIEW",
            issueLocale=cfg["locale"],
        )
        resp = getattr(result, "payload", result)
    except Exception as e:
        logger.error(f"VALIDATION_PREVIEW 실패 (product_id={product_id}, market={market}): {e}")
        return {"ok": False, "product_id": product_id, "asin": asin, "error": str(e)}

    status = resp.get("status", "UNKNOWN")
    issues = resp.get("issues", [])
    errors = [i for i in issues if i.get("severity") == "ERROR"]
    warnings = [i for i in issues if i.get("severity") == "WARNING"]

    ok = status in ("VALID", "ACCEPTED") and not errors

    logger.info(
        f"VALIDATION_PREVIEW product_id={product_id} market={market} asin={asin}: "
        f"status={status}, errors={len(errors)}, warnings={len(warnings)}"
    )

    return {
        "ok": ok,
        "product_id": product_id,
        "asin": asin,
        "sku": sku,
        "market": market,
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "payload": body,
    }


def register_offer(product_id: int, market: str = "US", dry_run: bool = True) -> dict:
    """기존 ASIN에 offer 등록.

    dry_run=True: VALIDATION_PREVIEW만 (기본값, 안전)
    dry_run=False: 실제 putListingsItem 호출
    """
    if dry_run:
        return validate_offer(product_id, market)

    cfg = get_config(market)
    product = _get_product_for_offer(product_id, market)
    asin = product["matched_asin"]
    price = product["calculated_price"] or 29.99
    quantity = min(product.get("stock_quantity") or DEFAULT_QUANTITY, DEFAULT_QUANTITY)
    sku = make_sku(product_id, market)
    wh = product.get("warehouse_country", "US")

    body = build_offer_payload(asin, price, quantity, market=market, warehouse_country=wh)

    _listing_limiter.wait()
    creds = get_credentials()
    client = ListingsItemsV20210801(credentials=creds, marketplace=get_marketplace())
    seller_id = get_seller_id()

    try:
        result = client.put_listings_item(
            sellerId=seller_id,
            sku=sku,
            marketplaceIds=cfg["marketplace_id"],
            body=body,
            issueLocale=cfg["locale"],
        )
        resp = getattr(result, "payload", result)
    except Exception as e:
        logger.error(f"offer 등록 실패 (product_id={product_id}, market={market}): {e}")
        return {"ok": False, "product_id": product_id, "asin": asin, "error": str(e)}

    status = resp.get("status", "UNKNOWN")
    issues = resp.get("issues", [])
    errors = [i for i in issues if i.get("severity") == "ERROR"]
    submission_id = resp.get("submissionId", "")

    ok = status == "ACCEPTED" and not errors

    # DB 업데이트
    if ok:
        now = datetime.now(datetime.timezone.utc).isoformat() if hasattr(datetime, 'timezone') else datetime.utcnow().isoformat()
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM listings WHERE product_id = ? AND marketplace = ?",
                (product_id, market),
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
                        status, current_price, current_stock, marketplace,
                        listed_at, created_at, updated_at)
                       VALUES (?, 'amazon', 'dropship', ?, ?, 'listed', ?, ?, ?, ?, ?, ?)""",
                    (product_id, asin, sku, price, quantity, market, now, now, now),
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
    market: str = "US",
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
                   LEFT JOIN listings l ON l.product_id = cp.id AND l.marketplace = ?
                   WHERE cp.matched_asin IS NOT NULL AND cp.matched_asin != ''
                     AND (l.id IS NULL OR l.status = 'candidate')
                   ORDER BY cp.sort_score DESC
                   LIMIT ?""",
                (market, limit),
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
            result = register_offer(pid, market=market, dry_run=dry_run)
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
