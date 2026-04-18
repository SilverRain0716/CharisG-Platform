"""PA 채널 리스팅 생성 — products → listings_pa (가격 이원화)."""
from backend.purchase.database import get_db
from backend.purchase.services.pricing_service_pa import calculate_sale_krw


def send_to_channels(product_id: int, channels: list[str] | None = None) -> dict:
    channels = channels or ["smartstore", "coupang"]

    with get_db() as conn:
        product = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        raise ValueError(f"product {product_id} 없음")
    if not product["ai_processed_at"]:
        raise ValueError(f"product {product_id}: AI 처리 미완료")

    cost_usd = product["cost_usd"]
    if cost_usd is None or cost_usd == "":
        raise ValueError(f"product {product_id}: cost_usd 없음")

    results = {}
    for ch in channels:
        pricing = calculate_sale_krw(cost_usd=float(cost_usd), channel=ch)

        with get_db() as conn:
            conn.execute(
                """INSERT INTO listings_pa
                   (product_id, channel, status, sale_krw, cost_krw_snapshot,
                    fee_rate, net_margin_krw, category_mapped)
                   VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
                   ON CONFLICT(product_id, channel) DO UPDATE SET
                    status=CASE
                      WHEN listings_pa.channel_product_id IS NOT NULL
                           AND listings_pa.channel_product_id != ''
                        THEN listings_pa.status
                      ELSE 'pending'
                    END,
                    sale_krw=excluded.sale_krw,
                    cost_krw_snapshot=excluded.cost_krw_snapshot,
                    fee_rate=excluded.fee_rate, net_margin_krw=excluded.net_margin_krw,
                    category_mapped=excluded.category_mapped,
                    last_synced_at=CURRENT_TIMESTAMP""",
                (product_id, ch,
                 pricing["sale_krw"], pricing["cost_krw"], pricing["fee_rate"],
                 pricing["net_margin_krw"], product["category_path"] or ""),
            )

        results[ch] = {
            "sale_krw": pricing["sale_krw"],
            "cost_krw": pricing["cost_krw"],
            "fee_rate": pricing["fee_rate"],
            "net_margin_krw": pricing["net_margin_krw"],
        }

    return {"product_id": product_id, "channels": results}
