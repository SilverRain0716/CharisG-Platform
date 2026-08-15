"""판매중지/리스팅실패 상품의 로컬 이미지 정리 (디스크 확보).

사용자 지시(2026-05-24): 쿠팡 판매중(listed) 아닌 상품 = 판매중지(paused)/실패(excluded)/
보관(archived/rotated) 의 이미지 삭제.

판정: 쿠팡 API statusName 으로는 판매중/판매중지 구분 불가(둘 다 '승인완료', onSale 필드 없음).
      → 우리 DB listings_pa status 로 판정. (listed=판매중, paused=판매중지)

대상: image_cache 보유 + 어느 채널이든 listed/pending listing 없음 + created < CUTOFF(sid=16 보호)
보존: listed(판매중) / pending(업로드 대기, sid=16) / created>=CUTOFF(이번 작업 전체)
안전: paused 등은 쿠팡 CDN 에 이미지 있어 판매중지 리스팅 영향 없음.

실행:
  .venv/bin/python -m backend.purchase.scripts.cleanup_dead_coupang_images [--apply]
"""
import argparse
import logging
import os
import shutil
import sys

from dotenv import load_dotenv
_ROOT = os.environ.get(
    "CHARISG_ROOT",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
)
load_dotenv(os.path.join(_ROOT, ".env"))

from backend.purchase import database
from backend.purchase.database import get_db
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("cleanup-dead")

MEDIA = os.path.join(_ROOT, "backend/purchase/media/products")
CUTOFF = "2026-05-23"  # 이 이후 생성분(sid=16) 전체 보존


def main(apply: bool) -> None:
    # 대상: image_cache 보유 + listed/pending listing 없음 + created < CUTOFF
    with get_db() as conn:
        safe = [r["product_id"] for r in conn.execute(
            """SELECT DISTINCT ic.product_id
               FROM image_cache ic
               JOIN products p ON p.id = ic.product_id
               WHERE p.created_at < ?
                 AND NOT EXISTS (
                   SELECT 1 FROM listings_pa l
                    WHERE l.product_id = ic.product_id
                      AND l.status IN ('listed','pending'))
               ORDER BY ic.product_id""",
            (CUTOFF,),
        ).fetchall()]
    logger.info(f"[1] 삭제 대상(판매중지/실패, listed·pending·sid16 제외): {len(safe)}건")
    if not safe:
        logger.info("대상 0건 — 종료"); return

    freed = nfiles = ndirs = 0
    for pid in safe:
        d = os.path.join(MEDIA, str(pid))
        if os.path.isdir(d):
            for root, _, files in os.walk(d):
                for f in files:
                    try:
                        freed += os.path.getsize(os.path.join(root, f)); nfiles += 1
                    except OSError:
                        pass
            ndirs += 1
            if apply:
                shutil.rmtree(d, ignore_errors=True)
        if apply and ndirs % 1000 == 0:
            logger.info(f"[2] 진행 {ndirs} dirs / {freed/1e9:.2f} GB")
    logger.info(f"[2] {'삭제완료' if apply else 'DRY-RUN 대상'}: {ndirs} dirs / {nfiles} files / {freed/1e9:.2f} GB")

    if apply and safe:
        with get_db() as conn:
            deleted = 0
            for i in range(0, len(safe), 500):
                chunk = safe[i:i+500]
                ph = ",".join("?" * len(chunk))
                cur = conn.execute(f"DELETE FROM image_cache WHERE product_id IN ({ph})", chunk)
                deleted += cur.rowcount
        logger.info(f"[3] image_cache 행 {deleted}건 삭제")
    logger.info("=== 완료 ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    try:
        main(args.apply)
    except Exception:
        logger.exception("=== 예외 ===")
        sys.exit(1)
