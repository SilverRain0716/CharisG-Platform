"""이미 네이버에 등록된 상품의 상세페이지(detailContent)를 일괄 수정."""
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from backend.purchase.database import get_db
from backend.purchase.services.naver_commerce_service import update_product


def bulk_update_detail():
    with get_db() as conn:
        rows = conn.execute(
            """SELECT l.product_id, l.channel_product_id, d.html_content
               FROM listings_pa l
               JOIN detail_pages d ON l.product_id = d.product_id
               WHERE l.channel = 'smartstore'
                 AND l.status = 'listed'
                 AND l.channel_product_id IS NOT NULL
               ORDER BY l.product_id"""
        ).fetchall()

    print(f"수정 대상: {len(rows)}건")
    success = 0
    failed = 0

    for r in rows:
        product_no = r["channel_product_id"]
        detail_html = r["html_content"]

        payload = {
            "originProduct": {
                "detailContent": detail_html,
            },
        }

        result = update_product(product_no, payload)
        if result:
            success += 1
            print(f"  product {r['product_id']} (#{product_no}): OK")
        else:
            failed += 1
            print(f"  product {r['product_id']} (#{product_no}): FAIL")

        time.sleep(0.5)

    print(f"\n완료 — 성공 {success}, 실패 {failed}/{len(rows)}")


if __name__ == "__main__":
    bulk_update_detail()
