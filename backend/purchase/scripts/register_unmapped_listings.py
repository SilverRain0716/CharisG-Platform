"""dangling-FK 해결 — 복구된 미매핑 쿠팡 상품을 listings_pa 에 등록.

이미지오류로 반려됐다가 relist_from_map 으로 복구된 미매핑 상품들은 쿠팡엔 승인완료
지만 listings_pa 에 행이 없어 추적/가격/모니터링에서 누락된다(업로드시 DB writeback
누락 = dangling FK). resolve 된 (spid, product_id) 맵으로 listing 행을 생성해 정상 편입.

안전성: 사전 진단(diag_fk)에서 335건 전부 해당 product_id 및 ASIN 형제에 쿠팡 listing
행이 전무함을 확인 → INSERT 충돌 없음. 각 행도 재확인(존재 시 skip)한다.

  python -m backend.purchase.scripts.register_unmapped_listings --map-file /home/ubuntu/logs/unmapped_safe_remap.txt --dry-run
  python -m backend.purchase.scripts.register_unmapped_listings --map-file /home/ubuntu/logs/unmapped_safe_remap.txt --apply
"""
import argparse, os, sqlite3

from dotenv import load_dotenv
load_dotenv()

DB_PATH = os.environ.get("PA_DB_PATH",
                         str(os.path.join(os.path.dirname(__file__), "..", "purchase.db")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map-file", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    pairs = []
    with open(args.map_file) as f:
        for line in f:
            p = line.strip().split("\t")
            if len(p) >= 2:
                pairs.append((p[0], int(p[1])))
    print(f"대상 (spid, product_id): {len(pairs)} (apply={apply})")

    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=180000")

    inserted = skipped_exist = 0
    for spid, pid in pairs:
        # 동일 spid 또는 동일 product_id+coupang 행이 이미 있으면 skip (재실행 안전)
        ex = con.execute(
            "SELECT 1 FROM listings_pa WHERE channel='coupang' "
            "AND (channel_product_id=? OR product_id=?) LIMIT 1", (spid, pid)).fetchone()
        if ex:
            skipped_exist += 1
            continue
        if apply:
            con.execute(
                "INSERT INTO listings_pa "
                "(product_id, channel, channel_product_id, status, coupang_auto_matched, "
                " coupang_seller_status, coupang_status_name, error_message) "
                "VALUES (?, 'coupang', ?, 'listed', 0, 'APPROVED', '승인완료', "
                "'img-fix remap 재등록 20260602')",
                (pid, spid))
        inserted += 1
    if apply:
        con.commit()

    print("\n=== 결과 ===")
    print(f"  {'INSERT' if apply else 'INSERT 예정'}: {inserted}")
    print(f"  이미 존재(skip): {skipped_exist}")
    if not apply:
        print("  → 실제 등록하려면 --apply")


if __name__ == "__main__":
    main()
