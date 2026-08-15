"""이미 네이버에 등록된 상품의 상세페이지(detailContent)를 일괄 수정."""
import re
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from backend.purchase.database import get_db
from backend.purchase.services.naver_commerce_service import update_product, get_product


LOCAL_IMG_RE = re.compile(r'(?:http://[^"]*)?/api/pa/images/products/\d+/img_\d+\.jpg')


def _get_naver_images(product_no: str) -> list[str]:
    """네이버에 이미 등록된 상품에서 이미지 URL 추출."""
    data = get_product(product_no)
    if not data:
        return []
    images = data.get("originProduct", {}).get("images", {})
    urls = []
    rep = images.get("representativeImage", {})
    if rep and rep.get("url"):
        urls.append(rep["url"])
    for opt in images.get("optionalImages", []):
        if opt.get("url"):
            urls.append(opt["url"])
    return urls


def _replace_local_images(html: str, naver_urls: list[str]) -> str:
    """로컬 이미지 URL을 네이버 이미지 URL로 치환."""
    local_matches = LOCAL_IMG_RE.findall(html)
    for i, local_url in enumerate(local_matches):
        if i < len(naver_urls):
            html = html.replace(local_url, naver_urls[i])
        elif naver_urls:
            html = html.replace(local_url, naver_urls[0])
        else:
            html = html.replace(local_url, "")
    return html


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

        naver_urls = _get_naver_images(product_no)
        detail_html = _replace_local_images(detail_html, naver_urls)

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
