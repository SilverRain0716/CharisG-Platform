"""PA Orders — 6단계 칸반 + 단계 진행 + 아마존 발주 준비."""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.database import get_db, get_db_hot
from backend.purchase.services.order_receiver_service import advance_step, ORDER_STEPS

router = APIRouter(prefix="/api/pa/orders", tags=["pa-orders"])


AMAZON_URL_BASE = "https://www.amazon.com/dp/"


@router.get("/kanban")
def kanban(
    user: dict = Depends(current_user),
    channel: Optional[str] = None,
):
    cols = {step: [] for step, _ in ORDER_STEPS}
    with get_db_hot() as conn:
        if channel:
            rows = conn.execute(
                """SELECT id, channel, channel_order_id, customer_name, sale_price_krw,
                          current_step, placed_at, product_name_cache FROM orders
                          WHERE channel=? ORDER BY placed_at DESC LIMIT 200""",
                (channel,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, channel, channel_order_id, customer_name, sale_price_krw,
                          current_step, placed_at, product_name_cache FROM orders
                          ORDER BY placed_at DESC LIMIT 200"""
            ).fetchall()
    for r in rows:
        cols.setdefault(r["current_step"], []).append(dict(r))
    return [
        {"id": s, "label": l, "items": cols.get(s, [])}
        for s, l in ORDER_STEPS
    ]


@router.get("")
def list_orders(
    user: dict = Depends(current_user),
    step: Optional[str] = None,
    channel: Optional[str] = None,
    limit: int = 100,
):
    where = []
    params = []
    if step:
        where.append("current_step=?")
        params.append(step)
    if channel:
        where.append("channel=?")
        params.append(channel)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    with get_db_hot() as conn:
        rows = conn.execute(
            f"SELECT * FROM orders {where_sql} ORDER BY placed_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/{oid}")
def get_order(oid: int, user: dict = Depends(current_user)):
    with get_db_hot() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not row:
            raise HTTPException(404, "주문 없음")
        steps = conn.execute(
            "SELECT * FROM order_steps WHERE order_id=? ORDER BY id", (oid,),
        ).fetchall()
    return {"order": dict(row), "steps": [dict(s) for s in steps]}


class AdvanceBody(BaseModel):
    step: str
    note: Optional[str] = None


@router.patch("/{oid}/advance")
def advance(oid: int, body: AdvanceBody, user: dict = Depends(current_user)):
    if not advance_step(oid, body.step, body.note or ""):
        raise HTTPException(400, "invalid step")
    return {"ok": True}


# ──────────────────────────────────────────────
# 아마존 발주 준비 (Phase D)
# ──────────────────────────────────────────────

@router.get("/{oid}/amazon-prep")
def amazon_prep(oid: int, user: dict = Depends(current_user)):
    """아마존 발주 준비 패널용 종합 정보.

    반환 구조:
    {
      "order": {...},                    # orders row (v13 컬럼 포함)
      "product": {ASIN, title_en, amazon_price_usd, brand, ...} | None,
      "amazon_url": "https://www.amazon.com/dp/{ASIN}" | None,
      "customer": {name_ko, name_en, phone, address_ko, address_en, postal_code, customs_code, shipping_message},
      "match_status": "matched" | "missing_product" | "missing_asin"
    }
    """
    # orders → hot.db, products → cold.db (별도 connection)
    with get_db_hot() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not order:
            raise HTTPException(404, "주문 없음")
        order = dict(order)

    # multi-option 주문이면 child_product_id 우선, 없으면 master product_id
    target_pid = order.get("child_product_id") or order.get("product_id")
    product = None
    if target_pid:
        with get_db() as cold_conn:
            row = cold_conn.execute(
                """SELECT id, asin, title_ko, title_en, brand,
                          cost_usd, weight_g, sale_price_krw, margin_pct,
                          group_master_asin, option_label
                   FROM products WHERE id=?""",
                (target_pid,),
            ).fetchone()
        if row:
            product = dict(row)

    # child_asin 명시적 저장된 경우 우선 사용
    asin = order.get("child_asin") or (product or {}).get("asin")
    amazon_url = (AMAZON_URL_BASE + asin) if asin else None

    if product is None:
        match_status = "missing_product"  # 쿠팡 sellerProductId가 listings_pa에 없음
    elif not asin:
        match_status = "missing_asin"     # 상품은 있는데 ASIN 누락
    else:
        match_status = "matched"

    # 고객 한/영 병기 — 영문 변환은 translation_status='done'이면 채워짐
    customer = {
        "name_ko": order.get("customer_name") or "",
        "name_en": order.get("customer_name_en") or "",
        "phone_safe": order.get("customer_phone") or "",   # 안심번호
        "phone_real": order.get("orderer_real_phone") or "",  # 실휴대폰
        "address_ko": order.get("address") or "",
        "address_en": order.get("address_en") or "",
        "customs_code": order.get("customs_clearance_code") or "",
        "shipping_message": order.get("shipping_message") or "",
        "translation_status": order.get("translation_status") or "pending",
    }

    return {
        "order": {
            "id": order["id"],
            "channel": order["channel"],
            "channel_order_id": order["channel_order_id"],
            "current_step": order["current_step"],
            "sale_price_krw": order["sale_price_krw"],
            "quantity": order["quantity"],
            "ordered_at": order.get("ordered_at"),
            "paid_at": order.get("paid_at"),
            "placed_at": order["placed_at"],
            "external_sku": order.get("external_sku"),
            "amazon_order_id": order.get("amazon_order_id"),
            "shipping_method": order.get("shipping_method"),
            # v18 옵션 식별
            "child_product_id": order.get("child_product_id"),
            "child_asin": order.get("child_asin"),
        },
        "product": product,
        "amazon_url": amazon_url,
        "option": {
            "is_variation": bool(order.get("child_asin") or (product or {}).get("group_master_asin")),
            "option_label": (product or {}).get("option_label"),
            "child_asin": order.get("child_asin"),
            "group_master_asin": (product or {}).get("group_master_asin"),
        },
        "customer": customer,
        "match_status": match_status,
    }


class AmazonOrderBody(BaseModel):
    amazon_order_id: str
    shipping_method: Literal["forwarder", "direct"]


@router.post("/{oid}/translate")
async def translate_now(oid: int, user: dict = Depends(current_user)):
    """이름·주소 영문 변환 수동 트리거 (자동 번역 실패·수정 시 재시도용)."""
    from backend.purchase.services.order_translator import translate_order
    result = await translate_order(oid)
    if not result.get("ok"):
        raise HTTPException(422, result.get("error") or "번역 실패")
    return result


@router.patch("/{oid}/amazon-order")
def set_amazon_order(oid: int, body: AmazonOrderBody, user: dict = Depends(current_user)):
    """아마존 발주 완료 처리 — 주문번호·배송방식 저장 + 'amazon_purchase'로 단계 이동."""
    aid = (body.amazon_order_id or "").strip()
    if not aid:
        raise HTTPException(400, "amazon_order_id 비어있음")
    with get_db_hot() as conn:
        row = conn.execute("SELECT id, current_step FROM orders WHERE id=?", (oid,)).fetchone()
        if not row:
            raise HTTPException(404, "주문 없음")
        conn.execute(
            "UPDATE orders SET amazon_order_id=?, shipping_method=? WHERE id=?",
            (aid, body.shipping_method, oid),
        )
    # 단계 이동은 'order_received'일 때만 수행 (이미 더 진행됐으면 유지)
    if row["current_step"] == "order_received":
        advance_step(
            oid, "amazon_purchase",
            f"Amazon 주문 #{aid} ({body.shipping_method})",
        )
    return {"ok": True}
