"""기존 products의 images_json 백필 — ASIN으로 Amazon 전체 이미지 URL 수집.

사용법 (EC2에서):
  cd ~/CharisG-Platform/charisg-platform
  .venv/bin/python -m backend.purchase.scripts.backfill_images

옵션:
  --all   이미지가 3장 미만인 상품도 재수집 (기본: images_json 비어있는 것만)
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

from backend.purchase.services.image_downloader import fetch_amazon_images

DB_PATH = Path(__file__).resolve().parent.parent / "purchase.db"


def main():
    refill = "--all" in sys.argv

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    if refill:
        # 이미지 3장 미만인 상품도 포함
        rows = conn.execute(
            "SELECT id, asin, images_json FROM products WHERE asin IS NOT NULL"
        ).fetchall()
        rows = [r for r in rows if _count_images(r["images_json"]) < 3]
    else:
        rows = conn.execute(
            "SELECT id, asin, images_json FROM products "
            "WHERE (images_json IS NULL OR images_json='[]' OR images_json='') "
            "AND asin IS NOT NULL"
        ).fetchall()

    print(f"백필 대상: {len(rows)}개 상품")
    if not rows:
        print("이미지 백필 대상 없음")
        conn.close()
        return

    updated = 0
    failed = 0
    for i, r in enumerate(rows, 1):
        asin = r["asin"]
        print(f"[{i}/{len(rows)}] {asin}...", end=" ", flush=True)

        urls = fetch_amazon_images(asin)
        if urls:
            # 기존 URL과 합치기 (중복 제거)
            try:
                existing = json.loads(r["images_json"]) if r["images_json"] else []
            except (json.JSONDecodeError, TypeError):
                existing = []
            merged = list(dict.fromkeys(existing + urls))
            images_json = json.dumps(merged, ensure_ascii=False)
            conn.execute(
                "UPDATE products SET images_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (images_json, r["id"]),
            )
            conn.commit()
            updated += 1
            print(f"OK ({len(merged)}장)")
        else:
            failed += 1
            print("SKIP (이미지 못 찾음)")

        if i < len(rows):
            time.sleep(2)

    conn.close()
    print(f"\n완료: 성공 {updated}, 실패 {failed}, 총 {len(rows)}")


def _count_images(images_json: str | None) -> int:
    if not images_json:
        return 0
    try:
        return len(json.loads(images_json))
    except (json.JSONDecodeError, TypeError):
        return 0


if __name__ == "__main__":
    main()
