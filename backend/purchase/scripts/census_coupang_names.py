"""쿠팡 listed 상품명 전수조사 — API(sellerProductName) 기준 영문 미번역 파악.

사용자 지시(2026-05-24): 리스팅된 상품 전수, 필요시 API로 실제 상품명 확인.
판별:
  - 쿠팡 API sellerProductName 의 한글<2 = 영문 노출 (ground truth, 고객이 보는 이름)
  - 우리 DB 매핑 후: title_ko == title_en (공백무시) → 완전 미번역(AI 영문복사 실패)
                     그 외(예: "...N권") → 도서/부분번역
출력만, 변경 없음(읽기전용).
"""
import os
import re
import sqlite3
from collections import Counter

from dotenv import load_dotenv
_ROOT = os.environ.get(
    "CHARISG_ROOT",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
)
load_dotenv(os.path.join(_ROOT, ".env"))
DB = os.path.join(_ROOT, "backend/purchase/purchase.db")


def hangul(s):
    return sum(1 for ch in (s or "") if "가" <= ch <= "힣")


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def main():
    from backend.purchase.services import coupang_service as cs
    allp = cs.list_all_seller_products()
    active = [p for p in allp if p.get("statusName") == "승인완료"]
    eng = [p for p in active if hangul(p.get("sellerProductName")) < 2]
    print(f"쿠팡 전체 {len(allp)} / 승인완료(노출) {len(active)} / ★영문명 {len(eng)} ({100*len(eng)/max(active and len(active) or 1,1):.1f}%)")

    # 우리 DB 매핑
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    spid_to = {}
    for r in con.execute(
        "SELECT l.channel_product_id cpid, p.id pid, p.title_ko tk, p.title_en te, substr(p.created_at,1,10) d "
        "FROM listings_pa l JOIN products p ON p.id=l.product_id "
        "WHERE l.channel='coupang' AND l.channel_product_id IS NOT NULL"
    ):
        spid_to[str(r["cpid"])] = r

    genuine = []   # 완전 미번역 (title_ko==title_en)
    booklike = []  # 부분번역/도서 (다름)
    unmapped = 0
    for p in eng:
        row = spid_to.get(str(p.get("sellerProductId")))
        if not row:
            unmapped += 1
            continue
        if norm(row["tk"]) == norm(row["te"]):
            genuine.append((row, p))
        else:
            booklike.append((row, p))

    print(f"  └ 완전 미번역(title_ko==title_en): {len(genuine)}")
    print(f"  └ 부분번역/도서(다름, N권 등): {len(booklike)}")
    print(f"  └ DB 매핑 안됨: {unmapped}")
    print()
    print("완전 미번역 생성일자별:")
    for d, n in Counter(r["d"] for r, _ in genuine).most_common(12):
        print(f"  {d}: {n}")
    print()
    print("완전 미번역 샘플 (pid | 쿠팡노출명):")
    for row, p in genuine[:30]:
        print(f"  {row['pid']} | {(p.get('sellerProductName') or '')[:55]}")


if __name__ == "__main__":
    main()
