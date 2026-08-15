"""쿠팡 노출명이 영문인데 DB title_ko 는 한글인 상품 → 쿠팡 상품명을 한글로 갱신.

사용자 지시(2026-05-24) 옵션①: 재번역 불필요(한글 title_ko 보유), 쿠팡 노출명만 push.
대상: list_all_seller_products statusName='승인완료' + sellerProductName 한글<2(영문)
      + displayCategoryCode 비도서 + 우리 product.title_ko 한글>=2
처리: update_product_name(spid, title_ko)  → PUT(전체) → request_approval(즉시 자동승인)

실행:
  .venv/bin/python -m backend.purchase.scripts.fix_coupang_names --limit 5 --apply   # 테스트
  nohup .venv/bin/python -m backend.purchase.scripts.fix_coupang_names --limit 9999 --apply > /tmp/fixname.log 2>&1 &
"""
import argparse
import logging
import os
import sqlite3
import sys
import time

from dotenv import load_dotenv
_ROOT = os.environ.get(
    "CHARISG_ROOT",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
)
load_dotenv(os.path.join(_ROOT, ".env"))
DB = os.path.join(_ROOT, "backend/purchase/purchase.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("fix-cu-names")


def hangul(s):
    return sum(1 for ch in (s or "") if "가" <= ch <= "힣")


def main(limit: int, apply: bool):
    from backend.purchase.services import coupang_service as cs

    logger.info("[1] 쿠팡 전체 상품명 조회 중...")
    allp = cs.list_all_seller_products()
    eng = [p for p in allp if p.get("statusName") == "승인완료" and hangul(p.get("sellerProductName")) < 2]
    logger.info(f"[1] 영문명(승인완료): {len(eng)}")

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    catmap = {str(r["code"]): (r["path"] or "") for r in con.execute("SELECT code,path FROM coupang_categories")}
    spid_to = {}
    for r in con.execute(
        "SELECT l.channel_product_id cpid, p.title_ko tk FROM listings_pa l "
        "JOIN products p ON p.id=l.product_id "
        "WHERE l.channel='coupang' AND l.channel_product_id IS NOT NULL"
    ):
        spid_to[str(r["cpid"])] = r["tk"]

    # 대상: 비도서 + title_ko 한글>=2
    targets = []
    for p in eng:
        if "도서" in catmap.get(str(p.get("displayCategoryCode")), ""):
            continue
        spid = str(p.get("sellerProductId"))
        tk = spid_to.get(spid)
        if tk and hangul(tk) >= 2:
            targets.append((spid, tk))
    logger.info(f"[2] 갱신 대상(비도서+한글 title_ko 보유): {len(targets)}건, 이번 limit={limit}")
    targets = targets[:limit]
    if not targets:
        logger.info("대상 0 — 종료 (=== 완료 ===)"); return

    if not apply:
        logger.info("[DRY-RUN] 샘플 10건:")
        for spid, tk in targets[:10]:
            logger.info(f"  spid={spid} → '{tk[:45]}'")
        logger.info("=== 완료 (dry-run) ===")
        return

    ok = fail = appr_ok = 0
    stuck = []  # 임시저장 잔류 (재승인 실패 — 비노출 상태, 복구 필요)
    for i, (spid, tk) in enumerate(targets, 1):
        _ensure_img_for(spid)
        u_ok, u_err = cs.update_product_name(spid, tk)
        if not u_ok:
            fail += 1
            if fail <= 20:
                logger.warning(f"  spid={spid} 이름변경 실패: {u_err}")
            continue
        ok += 1
        # PUT 후 임시저장 전환 대기 → request_approval 재시도 (즉시 자동승인, 재노출 복구)
        a_ok = False
        for _ in range(4):
            time.sleep(2)
            a_ok, a_err = cs.request_approval(spid)
            if a_ok:
                break
        if a_ok:
            appr_ok += 1
        else:
            stuck.append(spid)
            if len(stuck) <= 20:
                logger.warning(f"  spid={spid} ★재승인 실패(임시저장 잔류, 비노출): {a_err}")
        if i % 50 == 0:
            logger.info(f"[3] {i}/{len(targets)} — 이름ok={ok} 재승인={appr_ok} 실패={fail} 잔류={len(stuck)}")
    logger.info(f"[3] 완료 — 이름변경 ok={ok} 재승인={appr_ok} 이름실패={fail} ★임시저장잔류(비노출)={len(stuck)}")
    if stuck:
        logger.warning(f"★임시저장 잔류 {len(stuck)}건 — 재승인 sweep 필요. spids: {stuck[:60]}")
    logger.info("=== 완료 ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    try:
        main(args.limit, args.apply)
    except Exception:
        logger.exception("=== 예외 ===")
        sys.exit(1)


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
