"""DB-쿠팡 등록 대조 — DB엔 listed/pending 인데 쿠팡엔 실제로 없는/삭제된 상품 파악.

사용자 지시(2026-05-25): DB엔 있는데 쿠팡엔 입력 안 된 상품이 있을 수 있음.
대조:
  DB listings_pa(coupang) status IN (listed,pending,paused) 각각에 대해 —
  (A) channel_product_id NULL        → 쿠팡 등록ID 없음 (등록 자체 안 됨/실패)
  (B) cpid 가 쿠팡 목록에 없음          → 쿠팡에서 사라짐/미등록
  (C) cpid 있고 statusName 상품삭제/반려 → 쿠팡에서 삭제/거부됨
  (D) 정상 (쿠팡 승인완료/심사중)
출력만(읽기전용).
"""
import os
import sqlite3
from collections import Counter

from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
DB = os.path.join(_ROOT, "backend/purchase/purchase.db")


def main():
    from backend.purchase.services import coupang_service as cs
    print("[1] 쿠팡 전체 셀러상품 조회 중...")
    allp = cs.list_all_seller_products()
    live = {str(p.get("sellerProductId")): p.get("statusName") for p in allp}
    print(f"[1] 쿠팡 셀러상품: {len(live)}")

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT product_id, channel_product_id cpid, status FROM listings_pa "
        "WHERE channel='coupang' AND status IN ('listed','pending','paused')"
    ).fetchall()
    print(f"[2] DB coupang listed/pending/paused: {len(rows)}")

    no_cpid = []        # A
    not_on_coupang = [] # B
    deleted = []        # C (상품삭제/반려)
    ok = 0
    DEAD = ("상품삭제", "승인반려")
    for r in rows:
        cpid = r["cpid"]
        if not cpid or str(cpid).strip() == "":
            no_cpid.append(r)
        elif str(cpid) not in live:
            not_on_coupang.append(r)
        elif live.get(str(cpid)) in DEAD:
            deleted.append((r, live.get(str(cpid))))
        else:
            ok += 1

    print()
    print("=== 대조 결과 ===")
    print(f"  (D) 정상(쿠팡 존재): {ok}")
    print(f"  (A) ★channel_product_id 없음 (쿠팡 등록ID 미부여): {len(no_cpid)}")
    print(f"  (B) ★cpid 있으나 쿠팡 목록에 없음 (사라짐): {len(not_on_coupang)}")
    print(f"  (C) ★쿠팡 상품삭제/반려 상태: {len(deleted)}")
    print()
    print("(A) DB status 분포:", dict(Counter(r["status"] for r in no_cpid)))
    print("(B) DB status 분포:", dict(Counter(r["status"] for r in not_on_coupang)))
    print("(C) 쿠팡 statusName 분포:", dict(Counter(s for _, s in deleted)))
    print()
    print("(A) channel_product_id 없는 샘플 (pid | DB status):")
    for r in no_cpid[:15]:
        print(f"   {r['product_id']} | {r['status']}")
    print("(B) 쿠팡에 없는 샘플 (pid | cpid | DB status):")
    for r in not_on_coupang[:15]:
        print(f"   {r['product_id']} | {r['cpid']} | {r['status']}")


if __name__ == "__main__":
    main()
