"""title_ko 가 영문인(번역 실패) 쿠팡 상품 재번역 + 쿠팡 노출명 갱신.

사용자 지시(2026-05-25) ②: title_ko 자체가 영문 = AI 번역 실패분 재번역.
대상: coupang listed + title_ko 한글<2(영문) + 비도서(displayCategoryCode 도서 제외)
처리(상품당): translate_text(title_en,en,ko) → 한글 검증 → UPDATE products.title_ko
              → update_product_name(쿠팡 PUT) → request_approval(재노출)
도서 제외: 영문 원서는 제목을 한글로 바꾸면 안 됨.

실행:
  .venv/bin/python -m backend.purchase.scripts.retranslate_coupang --limit 5 --apply   # 테스트
  nohup .venv/bin/python -m backend.purchase.scripts.retranslate_coupang --limit 9999 --apply > /tmp/retrans.log 2>&1 &
"""
import argparse
import asyncio
import logging
import os
import re
import sqlite3
import sys

from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))

from backend.purchase import database
from backend.purchase.database import get_db
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)
DB = os.path.join(_ROOT, "backend/purchase/purchase.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("retranslate")


def hangul(s):
    return sum(1 for ch in (s or "") if "가" <= ch <= "힣")


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip())


async def main(limit, apply):
    from backend_shared.ai import translate_text
    from backend.purchase.services import coupang_service as cs

    logger.info("[1] 쿠팡 목록 조회 (도서 판정용)...")
    allp = cs.list_all_seller_products()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    catmap = {str(r["code"]): (r["path"] or "") for r in con.execute("SELECT code,path FROM coupang_categories")}
    spid_cat = {str(p.get("sellerProductId")): str(p.get("displayCategoryCode")) for p in allp
                if p.get("statusName") == "승인완료"}

    rows = con.execute(
        "SELECT p.id, l.channel_product_id cpid, p.title_en te, p.title_ko tk FROM products p "
        "JOIN listings_pa l ON l.product_id=p.id "
        "WHERE l.channel='coupang' AND l.status='listed' AND l.channel_product_id IS NOT NULL "
        "AND p.title_en IS NOT NULL AND p.title_en<>''"
    ).fetchall()
    targets = []
    for r in rows:
        if hangul(r["tk"]) >= 2:
            continue  # 이미 한글
        cpid = str(r["cpid"])
        if cpid not in spid_cat:
            continue  # 쿠팡 승인완료 아님
        if "도서" in catmap.get(spid_cat[cpid], ""):
            continue  # 도서 제외
        targets.append(r)
    logger.info(f"[2] 재번역 대상(영문 title_ko, 비도서, 쿠팡노출): {len(targets)}건, limit={limit}")
    targets = targets[:limit]
    if not targets:
        logger.info("대상 0 — 종료 (=== 완료 ===)"); return

    if not apply:
        for r in targets[:10]:
            logger.info(f"  pid={r['id']} | {r['te'][:50]}")
        logger.info("=== 완료 (dry-run) ===")
        return

    ok = trans_fail = put_fail = stuck = 0
    for i, r in enumerate(targets, 1):
        tr = await translate_text(r["te"], "en", "ko")
        ko = norm(tr.get("translated") or "")
        if hangul(ko) < 2 or norm(ko) == norm(r["te"]):
            trans_fail += 1
            if trans_fail <= 15:
                logger.warning(f"  pid={r['id']} 재번역 실패(여전히 영문): '{ko[:40]}'")
            continue
        ko = ko[:100]
        with get_db() as conn:
            conn.execute("UPDATE products SET title_ko=? WHERE id=?", (ko, r["id"]))
        u_ok, u_err = cs.update_product_name(str(r["cpid"]), ko)
        if not u_ok:
            put_fail += 1
            if put_fail <= 15:
                logger.warning(f"  pid={r['id']} 쿠팡 PUT 실패: {u_err}")
            continue
        a_ok = False
        for _ in range(4):
            await asyncio.sleep(2)
            a_ok, _ = cs.request_approval(str(r["cpid"]))
            if a_ok:
                break
        if a_ok:
            ok += 1
        else:
            stuck += 1
        if i % 50 == 0:
            logger.info(f"[3] {i}/{len(targets)} — 완료(재번역+재노출)={ok} 번역실패={trans_fail} PUT실패={put_fail} 잔류={stuck}")
    logger.info(f"[3] 완료 — 재번역+재노출 ok={ok} 번역실패={trans_fail} PUT실패={put_fail} ★임시저장잔류={stuck}")
    logger.info("=== 완료 ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    try:
        asyncio.run(main(args.limit, args.apply))
    except Exception:
        logger.exception("=== 예외 ===")
        sys.exit(1)
