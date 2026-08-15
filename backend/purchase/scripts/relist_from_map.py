"""미매핑 승인반려 상품 복구 — spid↔product_id 재매핑 파일 기반.

쿠팡 데이터에 우리 product_id 가 없어 channel_product_id 로 매핑 안 되는 반려 상품을,
sellerProductName 정확매칭(단일 ASIN)으로 resolve 한 (spid, product_id) 맵으로 복구.
이미지 재패딩 → relist_with_fixed_images → 성공 시 listings_pa 의 dangling cpid/status 복구.

  python -m backend.purchase.scripts.relist_from_map --map-file /home/ubuntu/logs/unmapped_safe_remap.txt --dry-run --limit 3
  python -m backend.purchase.scripts.relist_from_map --map-file /home/ubuntu/logs/unmapped_safe_remap.txt --apply --sleep 1.5
"""
import argparse, os, sqlite3, sys, time
from dotenv import load_dotenv
load_dotenv()
from PIL import Image
from backend.purchase.services import coupang_service as cs
from backend.purchase.services.coupang_lister import _get_product_images
from backend.purchase.services.image_downloader import _normalize_for_coupang

DB_PATH = os.environ.get("PA_DB_PATH",
                         str(os.path.join(os.path.dirname(__file__), "..", "purchase.db")))


def _repad(con, pid):
    fixed = 0
    for r in con.execute("SELECT id, local_path FROM image_cache WHERE product_id=?", (pid,)).fetchall():
        lp = r["local_path"]
        if not lp or not os.path.isfile(lp):
            continue
        try:
            with Image.open(lp) as im:
                w, h = im.size
            if min(w, h) >= 500 and max(w, h) <= 5000:
                continue
            with Image.open(lp) as im:
                out = _normalize_for_coupang(im)
            out.save(lp, "JPEG", quality=85, optimize=True)
            con.execute("UPDATE image_cache SET size_bytes=? WHERE id=?", (os.path.getsize(lp), r["id"]))
            con.commit()
            fixed += 1
        except Exception as e:
            print(f"    재패딩 실패 pid={pid} {lp}: {e}", file=sys.stderr)
    return fixed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map-file", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=1.5)
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    pairs = []
    with open(args.map_file) as f:
        for line in f:
            p = line.strip().split("\t")
            if len(p) >= 2:
                pairs.append((p[0], int(p[1])))
    if args.limit:
        pairs = pairs[: args.limit]
    print(f"대상: {len(pairs)} (apply={apply})")

    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=180000")

    n_ok = n_fail = n_noimg = n_relinked = 0
    for spid, pid in pairs:
        if apply:
            nf = _repad(con, pid)
            if nf:
                print(f"  [{spid}] pid={pid} 이미지 {nf}장 재패딩")
        urls = _get_product_images(pid)
        if not urls:
            n_noimg += 1
            print(f"  [{spid}] SKIP 교정이미지 없음 (pid={pid})")
            continue
        ok, msg = cs.relist_with_fixed_images(spid, urls, dry_run=not apply)
        if ok:
            n_ok += 1
            print(f"  [{spid}] {'PUT성공' if apply else 'DRY'} pid={pid} — {msg}")
            if apply:
                # dangling cpid/status 복구 — 해당 product_id 의 cpid-NULL 쿠팡 행에 spid 기입
                cur = con.execute(
                    "UPDATE listings_pa SET channel_product_id=?, status='listed', "
                    "coupang_seller_status='', error_message='img-fix remap 20260602' "
                    "WHERE product_id=? AND channel='coupang' "
                    "AND (channel_product_id IS NULL OR channel_product_id='')",
                    (spid, pid))
                con.commit()
                if cur.rowcount:
                    n_relinked += 1
        else:
            n_fail += 1
            print(f"  [{spid}] FAIL pid={pid} — {msg}")
        if apply and args.sleep:
            time.sleep(args.sleep)

    print("\n=== 결과 ===")
    print(f"  {'재제출 성공' if apply else 'DRY 통과'}: {n_ok}")
    print(f"  실패: {n_fail}, 교정이미지없음: {n_noimg}, listings_pa 재링크: {n_relinked}")


if __name__ == "__main__":
    main()
