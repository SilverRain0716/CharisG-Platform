"""
fill_cj_images.py — CJ 상품 상세 API로 이미지 URL 채우기.

collected_products.images (JSON 배열)가 비어 있는 상품에 대해
CJ get_product_detail API를 호출하고 이미지 URL을 채운다.

실행:
    # 특정 상품만
    charisg-platform/.venv/bin/python charisg-platform/scripts/fill_cj_images.py 638 337 842

    # GO 전체 (images가 비어 있는 것만)
    charisg-platform/.venv/bin/python charisg-platform/scripts/fill_cj_images.py --all-go
"""
import argparse
import json
import os
import sys
import time

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "packages", "backend-shared"))

from backend_shared.context import register_db_factory
from backend.dropshipping import database
register_db_factory(database.get_db)

from backend.dropshipping.services.cj_service import get_product_detail


def fill_images(product_ids: list[int]) -> dict:
    stats = {"total": len(product_ids), "updated": 0, "failed": 0, "skipped": 0}

    for i, pid in enumerate(product_ids, 1):
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT id, external_id, product_name, images, image_url "
                "FROM collected_products WHERE id=?", (pid,)
            ).fetchone()

        if not row:
            print(f"  [{i}/{stats['total']}] id={pid} — 상품 없음")
            stats["failed"] += 1
            continue

        existing_images = json.loads(row["images"] or "[]")
        if existing_images:
            print(f"  [{i}/{stats['total']}] id={pid} — 이미 {len(existing_images)}장 있음, 스킵")
            stats["skipped"] += 1
            continue

        ext_id = row["external_id"]
        if not ext_id:
            print(f"  [{i}/{stats['total']}] id={pid} — external_id 없음")
            stats["failed"] += 1
            continue

        print(f"  [{i}/{stats['total']}] id={pid} ({row['product_name'][:40]}...) — CJ API 호출 중...")
        detail = get_product_detail(ext_id)

        if not detail:
            print(f"    ❌ CJ API 응답 없음")
            stats["failed"] += 1
            continue

        # 이미지 수집: main + productImageSet
        urls: list[str] = []
        main_img = detail.get("productImage") or detail.get("bigImage") or ""
        if main_img:
            urls.append(main_img)

        for img in detail.get("productImageSet", []):
            url = img if isinstance(img, str) else img.get("imageUrl", "")
            if url and url not in urls:
                urls.append(url)

        if not urls:
            print(f"    ⚠️ CJ 상세에서 이미지 0장")
            stats["failed"] += 1
            continue

        with database.get_db() as conn:
            conn.execute(
                """UPDATE collected_products
                   SET images=?, image_url=COALESCE(NULLIF(image_url,''), ?),
                       image_count=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (json.dumps(urls), urls[0], len(urls), pid),
            )

        print(f"    ✅ {len(urls)}장 저장 완료")
        stats["updated"] += 1

        if i < stats["total"]:
            time.sleep(1)

    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("product_ids", nargs="*", type=int, help="상품 ID 목록")
    parser.add_argument("--all-go", action="store_true",
                        help="GO 상품 중 images 비어 있는 것 전부")
    args = parser.parse_args()

    print("=" * 60)
    print("  CJ 이미지 채우기 (fill_cj_images.py)")
    print("=" * 60)

    if args.all_go:
        with database.get_db() as conn:
            rows = conn.execute(
                """SELECT id FROM collected_products
                   WHERE go_decision='GO' AND hard_filter_pass=1
                     AND (images IS NULL OR images='[]')
                   ORDER BY id"""
            ).fetchall()
        ids = [r["id"] for r in rows]
        print(f"\n  GO 상품 중 이미지 미수집: {len(ids)}건")
    else:
        ids = args.product_ids

    if not ids:
        print("  대상 없음")
        return

    stats = fill_images(ids)
    print(f"\n  결과: updated={stats['updated']}, failed={stats['failed']}, skipped={stats['skipped']}")


if __name__ == "__main__":
    main()
