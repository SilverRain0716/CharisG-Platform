"""기존 라이브 sellerProductName [브랜드명] 보정 PUT.

대상: listings_pa.status='listed' AND channel='coupang' AND products.title_ko 가
       build_seller_product_name 으로 빌드한 결과 ≠ 쿠팡 측 sellerProductName.
       (B 백필 이미 적용 — DB 의 title_ko 는 이미 brand prefix 된 상태.
        하지만 쿠팡 라이브는 옛 [브랜드명] 상태이므로 정답을 PUT 으로 동기화.)

위험: 콘텐츠 수정 → 쿠팡 4~24h 재심사 + ID 분리 위험.
사용:
  python fix_seller_product_names.py --dry-run --limit 5     # 5건 미리보기
  python fix_seller_product_names.py --apply --limit 5       # 5건 시범 PUT
  python fix_seller_product_names.py --apply                 # 전체 일괄
"""
import sys, os, time, argparse
os.chdir("/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")
from backend.purchase.database import get_db
from backend.purchase.services.coupang_lister import build_seller_product_name
from backend.purchase.services.coupang_service import update_seller_product_name, get_seller_product

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--asin", default=None, help="단건 테스트용 ASIN")
ap.add_argument("--sleep", type=float, default=0.5, help="요청 간격(초)")
args = ap.parse_args()

with get_db() as conn:
    sql = """
      SELECT p.id pid, p.asin, p.brand, p.title_ko, p.title_en,
             l.channel_product_id spid
      FROM listings_pa l JOIN products p ON p.id=l.product_id
      WHERE l.channel='coupang' AND l.status='listed'
        AND l.channel_product_id IS NOT NULL AND l.channel_product_id != ''
        AND p.title_ko IS NOT NULL AND p.title_ko != ''
        AND p.brand IS NOT NULL AND p.brand != ''
        AND (p.title_ko LIKE p.brand || ' %' OR p.title_ko LIKE p.brand || '+%')
      ORDER BY p.id DESC
    """
    params = []
    if args.asin:
        sql += " AND p.asin=?"
        params.append(args.asin)
    if args.limit > 0:
        sql += " LIMIT ?"
        params.append(args.limit)
    rows = conn.execute(sql, params).fetchall()

print(f"[대상] {len(rows):,}건  apply={args.apply}")

ok = same = fail = 0
t0 = time.time()
for r in rows:
    desired = build_seller_product_name(
        title_ko=r["title_ko"], brand=r["brand"], title_en=r["title_en"], max_len=80,
    )
    if not desired or len(desired) < 3:
        print(f"  SKIP pid={r['pid']} asin={r['asin']}: desired 너무 짧음 ({desired!r})")
        fail += 1
        continue

    # 쿠팡 측 현재 sellerProductName 조회 (옛 [브랜드명] 상태인지 검증)
    info = get_seller_product(str(r["spid"]))
    if not info or not isinstance(info.get("data"), dict):
        print(f"  SKIP pid={r['pid']} asin={r['asin']} spid={r['spid']}: 조회 실패")
        fail += 1
        continue
    current = info["data"].get("sellerProductName") or ""
    if current == desired:
        same += 1
        continue
    if "[브랜드" not in current:
        # 옛 placeholder 가 아닌 케이스 — 안전상 skip (사용자 수동 입력 가능성)
        print(f"  SKIP pid={r['pid']} asin={r['asin']}: 옛 name '{current[:40]}' 에 placeholder 없음")
        same += 1
        continue

    if args.apply:
        success, msg = update_seller_product_name(str(r["spid"]), desired, dry_run=False)
    else:
        success, msg = update_seller_product_name(str(r["spid"]), desired, dry_run=True)

    if success:
        ok += 1
        print(f"  {'PUT' if args.apply else 'DRY'} pid={r['pid']} asin={r['asin']} spid={r['spid']}")
        print(f"    OLD: {current[:75]!r}")
        print(f"    NEW: {desired[:75]!r}  {msg}")
    else:
        fail += 1
        print(f"  FAIL pid={r['pid']} asin={r['asin']} spid={r['spid']}: {msg}")

    time.sleep(args.sleep)

print(f"\n[총: ok {ok} / same {same} / fail {fail} / {time.time()-t0:.1f}s]")
