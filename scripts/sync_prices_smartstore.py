"""스마트스토어 가격 동기화 — DB sale_krw → 네이버 커머스 API salePrice 일괄 반영.

사용법:
  python3 scripts/sync_prices_smartstore.py [--dry-run] [--limit N]

  --dry-run : 실제 API 호출 없이 변경 대상만 출력
  --limit N : 최대 N건만 처리 (기본 전체)

동작:
  1. listings_pa (channel='smartstore', status='listed') 중 channel_product_id 있는 건 조회
  2. 네이버 커머스 GET으로 현재 등록 가격 확인
  3. DB sale_krw와 다르면 PUT으로 salePrice 업데이트
  4. rate limit: 네이버 커머스 API 2/s + 80/min (naver_commerce_service._gate 사용)

주의:
  - update_product()은 GET→merge→PUT 이라 호출 1건당 API 2회 소모
  - 80 RPM 제한 → 분당 최대 40건 가격 변경 가능
  - 22000건 전체 → 약 9시간 (실제 변경 건수에 따라 단축)
"""
import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, ".")

with open(".env") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from backend.purchase.services.naver_commerce_service import get_product, update_product

DB_PATH = "backend/purchase/purchase.db"


def sync(dry_run: bool, limit: int, updated_since: str = None):
    conn = sqlite3.connect(DB_PATH, timeout=180)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=180000")
    conn.isolation_level = None  # autocommit — 백엔드 worker 와 락 충돌 완화

    where_extra = ""
    params: list = []
    if updated_since:
        where_extra = " AND p.updated_at >= ?"
        params.append(updated_since)
    limit_clause = f"LIMIT {limit}" if limit else ""
    rows = conn.execute(
        f"""SELECT l.id, l.product_id, l.channel_product_id, l.sale_krw,
                   p.asin, p.title_en
            FROM listings_pa l
            JOIN products p ON p.id = l.product_id
            WHERE l.channel = 'smartstore'
              AND l.status = 'listed'
              AND l.sale_krw > 0
              AND l.channel_product_id IS NOT NULL
              AND l.channel_product_id != ''
              {where_extra}
            ORDER BY l.id
            {limit_clause}""",
        params,
    ).fetchall()

    total = len(rows)
    print(f"동기화 대상: {total}건 (스마트스토어 listed)")
    if dry_run:
        print("⚠️  DRY RUN — 실제 API 호출 없음")
    print(f"{'='*100}")

    stats = {
        "checked": 0,
        "matched": 0,       # 가격 동일 → 스킵
        "updated": 0,       # 가격 변경 성공
        "failed": 0,        # API 실패
        "get_failed": 0,    # 현재 가격 조회 실패
    }

    for idx, r in enumerate(rows, 1):
        listing_id = r["id"]
        product_no = r["channel_product_id"]
        db_sale = r["sale_krw"] or 0
        asin = r["asin"] or ""
        title = (r["title_en"] or "")[:50]
        stats["checked"] += 1

        # 현재 네이버 등록 가격 조회
        current = get_product(product_no)
        if not current:
            stats["get_failed"] += 1
            print(f"[{idx}/{total}] ✗ #{listing_id} {asin} — 네이버 조회 실패 (productNo={product_no})")
            continue

        naver_price = current.get("originProduct", {}).get("salePrice", 0)

        if naver_price == db_sale:
            stats["matched"] += 1
            if idx % 100 == 0:
                print(f"[{idx}/{total}] ✓ (100건 스킵 중간보고) 동일={stats['matched']} 변경={stats['updated']} 실패={stats['failed']}")
            continue

        print(f"[{idx}/{total}] #{listing_id} {asin}  네이버=₩{naver_price:,} → DB=₩{db_sale:,}  {title}")

        if dry_run:
            stats["updated"] += 1
            continue

        # 가격 업데이트
        result = update_product(product_no, {
            "originProduct": {
                "salePrice": db_sale,
            },
        })

        if result:
            stats["updated"] += 1
            # last_synced_at 갱신 (autocommit)
            conn.execute(
                "UPDATE listings_pa SET last_synced_at = datetime('now') WHERE id = ?",
                (listing_id,),
            )
        else:
            stats["failed"] += 1
            print(f"  ✗ 업데이트 실패 (productNo={product_no})")

    conn.close()

    print(f"\n{'='*100}")
    print(f"동기화 완료: {stats['checked']}건 확인")
    print(f"  가격 동일 (스킵): {stats['matched']}건")
    print(f"  가격 변경 {'(예정)' if dry_run else '성공'}: {stats['updated']}건")
    print(f"  조회 실패: {stats['get_failed']}건")
    print(f"  변경 실패: {stats['failed']}건")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="스마트스토어 가격 동기화")
    parser.add_argument("--dry-run", action="store_true", help="실제 API 호출 없이 확인만")
    parser.add_argument("--limit", type=int, default=0, help="최대 처리 건수 (기본 전체)")
    parser.add_argument("--updated-since", type=str, default=None,
                        help="products.updated_at >= 이 값만 처리 (ISO 형식)")
    args = parser.parse_args()

    sync(args.dry_run, args.limit, args.updated_since)
