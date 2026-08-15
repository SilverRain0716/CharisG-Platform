"""쿠팡 중복 리스팅 판매중지 — img-fix 복구 시 드러난 중복 spid stop.

복구된 미매핑 335 spid 는 169 고유 product_id 로 resolve 됨 = 166개가 중복 리스팅
(같은 상품 다중 업로드). register_unmapped_listings 가 product_id 당 1 spid 만 등록.
이 스크립트는 등록 안 된 나머지 166 중복 spid 를 stop_sales(onSale=false)로 판매중지.

stop 대상 = (맵의 335 spid) − (listings_pa 에 등록된 169 spid).
가역적: 필요 시 쿠팡에서 재개 가능.

  python -m backend.purchase.scripts.stop_duplicate_listings --map-file /home/ubuntu/logs/unmapped_safe_remap.txt --dry-run
  python -m backend.purchase.scripts.stop_duplicate_listings --map-file /home/ubuntu/logs/unmapped_safe_remap.txt --apply --limit 3
"""
import argparse, os, sqlite3, time
from dotenv import load_dotenv
load_dotenv()
from backend.purchase.services import coupang_service as cs

DB_PATH = os.environ.get("PA_DB_PATH",
                         str(os.path.join(os.path.dirname(__file__), "..", "purchase.db")))
MARKER = "img-fix remap 재등록 20260602"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map-file", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=1.0)
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    map_spids = []
    with open(args.map_file) as f:
        for line in f:
            p = line.strip().split("\t")
            if p and p[0]:
                map_spids.append(p[0])

    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=180000")
    kept = set(str(r["channel_product_id"]) for r in con.execute(
        "SELECT channel_product_id FROM listings_pa WHERE error_message=?", (MARKER,)).fetchall())

    # 순서 보존 + 중복 제거하면서 kept 제외
    seen = set(); stop_list = []
    for s in map_spids:
        if s in kept or s in seen:
            continue
        seen.add(s)
        stop_list.append(s)
    print(f"맵 spid {len(map_spids)} / 등록(유지) {len(kept)} / stop 대상 {len(stop_list)}")
    if args.limit:
        stop_list = stop_list[: args.limit]

    if not apply:
        print(f"\nDRY-RUN — stop 예정 {len(stop_list)}건 (실행하려면 --apply)")
        for s in stop_list[:10]:
            print(f"  {s}")
        return

    ok = fail = 0
    for i, spid in enumerate(stop_list):
        success, msg = cs.stop_sales(spid)
        if success:
            ok += 1
            print(f"  [{spid}] STOP ok {msg}")
        else:
            fail += 1
            print(f"  [{spid}] STOP fail — {msg}")
        if args.sleep:
            time.sleep(args.sleep)
    print(f"\n=== 결과 === stop성공 {ok} / 실패 {fail} / 총 {len(stop_list)}")


if __name__ == "__main__":
    main()
