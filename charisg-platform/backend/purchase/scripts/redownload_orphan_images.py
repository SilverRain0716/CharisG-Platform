"""
redownload_orphan_images.py — 이미지 disk orphan 상품 재다운로드.

대상: listings_pa.channel='coupang' AND status='excluded'
      AND error_message LIKE '%이미지 파일 없음%'
      AND (이미 listed인 다른 채널 없음 — 중복 작업 회피)

로직:
  1. products.images_json 있으면 그 URL로 다운로드
  2. 없고 asin이 있으면 SP-API로 URL 조회 → products.images_json 저장 → 다운로드
  3. 다운로드 성공 + 최소 500×500 만족 이미지가 1장 이상이면 success
  4. 성공 시 listings_pa status='excluded' → 'pending' 으로 복구 (재업로드 대상)

사용법:
    python3 -m backend.purchase.scripts.redownload_orphan_images [--limit N] [--skip-resetti]
"""
import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.purchase.database import get_db
from backend.purchase.services.image_downloader import (
    download_product_images,
    fetch_amazon_images_sp_api,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _collect_orphans(limit: int | None = None) -> list[dict]:
    """재다운로드 대상 수집."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT p.id, p.asin, p.images_json
               FROM listings_pa l
               JOIN products p ON p.id = l.product_id
               WHERE l.channel='coupang' AND l.status='excluded'
                 AND l.error_message LIKE '%이미지 파일 없음%'
                 AND NOT EXISTS (
                   SELECT 1 FROM listings_pa l2
                   WHERE l2.product_id=l.product_id AND l2.channel='coupang' AND l2.status='listed'
                 )
               ORDER BY p.id"""
        ).fetchall()
    if limit:
        rows = rows[:limit]
    return [dict(r) for r in rows]


async def _process_one(p: dict, sem: asyncio.Semaphore) -> dict:
    pid = p["id"]
    images_json = p["images_json"] or "[]"
    async with sem:
        try:
            urls = json.loads(images_json) if images_json else []
        except Exception:
            urls = []
        # URL 없으면 SP-API로 조회
        if not urls and p["asin"]:
            try:
                sp_urls = await asyncio.to_thread(fetch_amazon_images_sp_api, p["asin"])
                if sp_urls:
                    urls = sp_urls
                    images_json = json.dumps(urls)
                    with get_db() as conn:
                        conn.execute(
                            "UPDATE products SET images_json=? WHERE id=?",
                            (images_json, pid),
                        )
            except Exception as e:
                logger.warning(f"SP-API 실패 pid={pid} asin={p['asin']}: {e}")
        if not urls:
            return {"pid": pid, "status": "no_urls", "downloaded": 0}
        try:
            result = await download_product_images(pid, images_json)
            return {"pid": pid, "status": "ok", "downloaded": result.get("downloaded", 0)}
        except Exception as e:
            logger.warning(f"다운로드 실패 pid={pid}: {e}")
            return {"pid": pid, "status": "download_error", "downloaded": 0}


def _restore_pending_for_downloaded(results: list[dict]) -> int:
    """이미지 1장 이상 받은 상품만 pending으로 복구."""
    ok_ids = [r["pid"] for r in results if r.get("downloaded", 0) > 0]
    if not ok_ids:
        return 0
    with get_db() as conn:
        placeholders = ",".join("?" * len(ok_ids))
        conn.execute(
            f"""UPDATE listings_pa SET status='pending', error_message=NULL, last_synced_at=NULL
                WHERE channel='coupang' AND product_id IN ({placeholders})
                  AND status='excluded'
                  AND error_message LIKE '%이미지 파일 없음%'""",
            ok_ids,
        )
        conn.commit()
    return len(ok_ids)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="처리 건수 제한 (테스트용)")
    parser.add_argument("--concurrency", type=int, default=8, help="병렬 다운로드 수")
    parser.add_argument("--skip-reset", action="store_true",
                       help="DB pending 복구 생략 (다운로드만)")
    args = parser.parse_args()

    orphans = _collect_orphans(limit=args.limit)
    logger.info(f"대상 orphan: {len(orphans)}건 (동시성 {args.concurrency})")
    if not orphans:
        return

    sem = asyncio.Semaphore(args.concurrency)
    results = await asyncio.gather(*[_process_one(p, sem) for p in orphans])

    ok = sum(1 for r in results if r["status"] == "ok" and r["downloaded"] > 0)
    no_urls = sum(1 for r in results if r["status"] == "no_urls")
    err = sum(1 for r in results if r["status"] in ("download_error",))
    zero = sum(1 for r in results if r["status"] == "ok" and r["downloaded"] == 0)

    logger.info(
        f"완료: 성공 {ok}/{len(orphans)} (no_urls {no_urls}, err {err}, zero {zero})"
    )

    if not args.skip_reset:
        restored = _restore_pending_for_downloaded(results)
        logger.info(f"DB: excluded → pending 복구 {restored}건")


if __name__ == "__main__":
    asyncio.run(main())
