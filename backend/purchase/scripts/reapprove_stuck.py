"""임시저장 잔류 복구 — DB status='listed'인데 쿠팡 statusName='임시저장'인 상품 재승인.

재번역(②)/이름수정 PUT 후 request_approval 이 503/타이밍으로 실패해 de-list 된 상품 회수.
처리: list_all_seller_products → 임시저장 ∩ (우리 DB listed) → request_approval 재시도.
읽기전용 조회 + 쿠팡 재승인만 (DB 변경 없음).

실행:
  .venv/bin/python -m backend.purchase.scripts.reapprove_stuck            # dry-run
  .venv/bin/python -m backend.purchase.scripts.reapprove_stuck --apply
"""
import argparse
import logging
import os
import sqlite3
import time

from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
from backend.purchase import database
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)
DB = os.path.join(_ROOT, "backend/purchase/purchase.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("reapprove")


def main(apply):
    from backend.purchase.services import coupang_service as cs
    allp = cs.list_all_seller_products()
    temp = {str(p.get("sellerProductId")) for p in allp if p.get("statusName") == "임시저장"}
    logger.info(f"[1] 쿠팡 임시저장 전체: {len(temp)}")

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT l.channel_product_id cpid, p.title_ko tk FROM products p "
        "JOIN listings_pa l ON l.product_id=p.id "
        "WHERE l.channel='coupang' AND l.status='listed' AND l.channel_product_id IS NOT NULL"
    ).fetchall()
    listed_cpids = {str(r["cpid"]): r["tk"] for r in rows}
    stuck = [(c, listed_cpids[c]) for c in temp if c in listed_cpids]
    logger.info(f"[2] DB listed ∩ 쿠팡 임시저장 (복구 대상): {len(stuck)}")
    for c, tk in stuck:
        logger.info(f"   cpid={c} | {str(tk)[:40]}")
    if not apply:
        logger.info("=== dry-run 종료 ===")
        return

    ok = fail = 0
    for c, _ in stuck:
        a_ok = False
        for _ in range(5):
            _ensure_img_for(str(c))
            a_ok, err = cs.request_approval(str(c))
            if a_ok:
                break
            time.sleep(3)
        if a_ok:
            ok += 1
            logger.info(f"   ✓ {c} 재승인 OK")
        else:
            fail += 1
            logger.warning(f"   ✗ {c} 재승인 실패: {err}")
    logger.info(f"[3] 완료 — 재승인 ok={ok} 실패={fail}")
    logger.info("=== 완료 ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    main(ap.parse_args().apply)


def _ensure_img_for(seller_product_id):
    """PUT/재승인 직전 로컬 이미지 보장 (2026-08-03)."""
    try:
        import sqlite3 as _sq
        from backend.purchase.services.image_downloader import ensure_local_images
        from backend.purchase.database import get_db as _g
        with _g() as _c:
            r = _c.execute(
                "SELECT product_id FROM listings_pa WHERE channel='coupang' "
                "AND channel_product_id=? LIMIT 1", (str(seller_product_id),)).fetchone()
        if r:
            ensure_local_images(r["product_id"])
    except Exception as _e:
        pass
