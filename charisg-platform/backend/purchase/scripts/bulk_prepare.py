"""전체 상품 가격 계산 + 카테고리 네이버 ID 매핑."""
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from backend.purchase.database import get_db
from backend.purchase.services.pricing_service_pa import calculate_sale_krw
from backend.purchase.services.naver_commerce_service import _get_token
import requests

BASE = "https://api.commerce.naver.com/external"


def bulk_set_prices():
    """cost_usd 있고 sale_price_krw 없는 상품에 가격 자동 계산."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, cost_usd FROM products
               WHERE cost_usd IS NOT NULL AND sale_price_krw IS NULL AND ai_processed_at IS NOT NULL"""
        ).fetchall()

    print(f"가격 계산 대상: {len(rows)}개")
    for r in rows:
        try:
            result = calculate_sale_krw(cost_usd=float(r["cost_usd"]), channel="smartstore")
            sale_krw = result["sale_krw"]
            margin_pct = result["target_margin_rate"] * 100
            with get_db() as conn:
                conn.execute(
                    "UPDATE products SET sale_price_krw=?, margin_pct=? WHERE id=?",
                    (sale_krw, round(margin_pct, 1), r["id"]),
                )
            print(f"  product {r['id']}: ${r['cost_usd']} -> ₩{sale_krw:,}")
        except Exception as e:
            print(f"  product {r['id']}: 실패 - {e}")
    print()


def bulk_map_categories():
    """텍스트 카테고리 경로를 네이버 leaf 카테고리 ID로 매핑."""
    token = _get_token()
    if not token:
        print("네이버 토큰 발급 실패")
        return

    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(BASE + "/v1/categories?last=true", headers=headers, timeout=30)
    if r.status_code != 200:
        print(f"카테고리 조회 실패: {r.status_code}")
        return

    naver_cats = r.json()
    print(f"네이버 카테고리 {len(naver_cats)}개 로드")

    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, category_path, title_ko FROM products
               WHERE ai_processed_at IS NOT NULL"""
        ).fetchall()

    updated = 0
    failed = 0
    for r in rows:
        cp = r["category_path"] or ""
        if cp.isdigit():
            continue

        best_match = _find_best_category(cp, r["title_ko"] or "", naver_cats)
        if best_match:
            cat_id = best_match["id"]
            cat_name = best_match.get("wholeCategoryName", "")
            with get_db() as conn:
                conn.execute("UPDATE products SET category_path=? WHERE id=?", (str(cat_id), r["id"]))
            print(f"  product {r['id']}: {cp[:40]} -> {cat_id} ({cat_name[:50]})")
            updated += 1
        else:
            print(f"  product {r['id']}: 매핑 실패 - {cp[:50]}")
            failed += 1

    print(f"\n카테고리 매핑 완료: 성공 {updated}, 실패 {failed}")


def _find_best_category(text_path: str, title: str, naver_cats: list) -> dict | None:
    keywords = []
    for sep in [">", "/"]:
        parts = text_path.split(sep)
        keywords.extend([p.strip() for p in parts if p.strip()])

    best = None
    best_score = 0

    for cat in naver_cats:
        whole = cat.get("wholeCategoryName", "")
        name = cat.get("name", "")
        score = 0
        for kw in keywords:
            if kw in whole:
                score += 10
            if kw in name:
                score += 5
        for word in title.split()[:5]:
            if len(word) >= 2 and word in whole:
                score += 3
        if score > best_score:
            best_score = score
            best = cat

    return best if best_score >= 10 else None


if __name__ == "__main__":
    print("=== 1. 가격 일괄 계산 ===")
    bulk_set_prices()
    print("=== 2. 카테고리 매핑 ===")
    bulk_map_categories()
    print("\n완료!")
