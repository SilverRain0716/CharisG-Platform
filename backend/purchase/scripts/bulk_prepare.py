"""전체 상품 가격 계산 + 카테고리 네이버 ID 매핑 (Gemini 지원)."""
import sys
import os
import time
import json
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from backend.purchase.database import get_db
from backend.purchase.services.pricing_service_pa import calculate_sale_krw
from backend.purchase.services.naver_commerce_service import _get_token
import requests

logger = logging.getLogger(__name__)

BASE = "https://api.commerce.naver.com/external"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"


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
            """SELECT p.id, p.category_path, p.title_ko,
                      l.category_mapped
               FROM products p
               LEFT JOIN listings_pa l ON p.id = l.product_id AND l.channel = 'smartstore'
               WHERE p.ai_processed_at IS NOT NULL"""
        ).fetchall()

    updated = 0
    failed = 0
    for r in rows:
        cp = r["category_path"] or ""
        # 이미 숫자 ID면 listings_pa의 원본 텍스트로 재매핑 시도
        text_path = cp
        if cp.isdigit() and r["category_mapped"] and not r["category_mapped"].isdigit():
            text_path = r["category_mapped"]
        elif cp.isdigit():
            continue

        best_match = _find_best_category(text_path, r["title_ko"] or "", naver_cats)
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


def _find_candidates(text_path: str, title: str, naver_cats: list, limit: int = 20) -> list[dict]:
    """키워드 매칭으로 후보 카테고리 추출 (Gemini에 넘길 후보)."""
    keywords = []
    for sep in [">", "/"]:
        parts = text_path.split(sep)
        keywords.extend([p.strip() for p in parts if p.strip()])

    scored = []
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
        if score >= 5:
            scored.append((score, cat))

    scored.sort(key=lambda x: -x[0])
    return [s[1] for s in scored[:limit]]


def _gemini_pick(text_path: str, title: str, candidates: list[dict]) -> dict | None:
    """Gemini에게 후보 중 최적 카테고리 선택 요청."""
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        return None

    cat_list = "\n".join(
        f"  {c['id']}: {c.get('wholeCategoryName', c.get('name', ''))}"
        for c in candidates
    )
    prompt = f"""네이버 스마트스토어에 상품을 등록하려 합니다.
아래 상품 정보를 보고, 후보 카테고리 중 가장 적합한 카테고리 ID를 1개만 선택하세요.

상품명: {title}
원래 카테고리: {text_path}

후보 카테고리:
{cat_list}

반드시 위 후보 중 하나의 ID 숫자만 응답하세요. 설명 없이 숫자만."""

    try:
        r = requests.post(
            f"{GEMINI_BASE}?key={gemini_key}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=15,
        )
        if r.status_code == 429:
            print("    ⚠ Gemini 429 rate limit — 3초 대기")
            time.sleep(3)
            r = requests.post(
                f"{GEMINI_BASE}?key={gemini_key}",
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=15,
            )
        if r.status_code != 200:
            return None

        text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        cat_id = "".join(c for c in text if c.isdigit())
        matched = next((c for c in candidates if str(c["id"]) == cat_id), None)
        return matched
    except Exception as e:
        print(f"    ⚠ Gemini 오류: {e}")
        return None


def _find_best_category(text_path: str, title: str, naver_cats: list) -> dict | None:
    """1차 키워드 후보 → 2차 Gemini 선택. Gemini 실패 시 키워드 1위 폴백."""
    candidates = _find_candidates(text_path, title, naver_cats)
    if not candidates:
        return None

    # Gemini로 최적 선택
    gemini_pick = _gemini_pick(text_path, title, candidates)
    if gemini_pick:
        return gemini_pick

    # Gemini 실패 시 키워드 1위
    return candidates[0]


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--prices", action="store_true", help="가격만 계산")
    parser.add_argument("--categories", action="store_true", help="카테고리만 매핑")
    args = parser.parse_args()

    run_all = not args.prices and not args.categories

    if run_all or args.prices:
        print("=== 1. 가격 일괄 계산 ===")
        bulk_set_prices()
    if run_all or args.categories:
        print("=== 2. 카테고리 매핑 (Gemini) ===")
        bulk_map_categories()
    print("\n완료!")
