"""한국 제조사 스크리닝 — enrichment 배치의 amazon_manufacturer 수집 + 한국 판정.
① fetch_product_info_sp_api 로 manufacturer 재수집 → UPDATE amazon_manufacturer
② clean_policy.check_korean_manufacturer (DB캐시 → 미분류시 Naver+Gemini classify)
→ 한국 제조사(IP 차단 대상) 건수·샘플 보고. classify 결과는 DB에 캐시됨(리스팅 때 재사용)."""
import argparse
import logging
import os
import sqlite3
import time

from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
from backend.purchase import database
from backend.purchase.database import get_db
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)
DB = os.path.join(_ROOT, "backend/purchase/purchase.db")
PIDS = os.environ.get("ENRICH_PIDS", "/tmp/enrich_pids.csv")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("screen-mfr")


def main(limit, do_fetch):
    from backend.purchase.services.image_downloader import fetch_product_info_sp_api
    from backend.purchase.services import clean_policy
    with open(PIDS) as f:
        pids = [int(x) for x in f.read().split() if x.strip()]
    if limit:
        pids = pids[:limit]
    logger.info(f"[0] 대상 {len(pids)}건 (fetch={do_fetch})")

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # ① 제조사명 수집 (NULL 인 것만)
    if do_fetch:
        todo = [r["id"] for r in con.execute(
            f"SELECT id FROM products WHERE id IN ({','.join('?'*len(pids))}) "
            f"AND (amazon_manufacturer IS NULL OR amazon_manufacturer='')", pids)]
        logger.info(f"[1] manufacturer 수집 대상: {len(todo)}건")
        got = 0
        for i, pid in enumerate(todo, 1):
            asin = con.execute("SELECT asin FROM products WHERE id=?", (pid,)).fetchone()["asin"]
            info = fetch_product_info_sp_api(asin)
            mfr = (info or {}).get("manufacturer") or (info or {}).get("brand")  # mfr 없으면 brand 폴백
            if mfr:
                with get_db() as conn:
                    conn.execute("UPDATE products SET amazon_manufacturer=? WHERE id=?", (str(mfr)[:100], pid))
                got += 1
            time.sleep(0.5)  # SP-API 카탈로그 쿼터 보호
            if i % 150 == 0:
                logger.info(f"[1] {i}/{len(todo)} (수집 {got})")
        logger.info(f"[1] manufacturer 수집 완료: {got}/{len(todo)}")

    # ② 한국 판정
    rows = con.execute(
        f"SELECT id, asin, amazon_manufacturer FROM products WHERE id IN ({','.join('?'*len(pids))})", pids
    ).fetchall()
    blocked = []; passed = 0; nomfr = 0; failed = 0
    for i, r in enumerate(rows, 1):
        mfr = r["amazon_manufacturer"]
        if not mfr:
            nomfr += 1; continue
        b, reason = clean_policy.check_korean_manufacturer(mfr)
        if b:
            blocked.append((r["asin"], mfr, reason))
        elif "fail" in reason:
            failed += 1
        else:
            passed += 1
        if i % 200 == 0:
            logger.info(f"[2] 판정 {i}/{len(rows)} — 한국차단 {len(blocked)} 통과 {passed} 실패 {failed}")
    logger.info("=" * 50)
    logger.info(f"[결과] 대상 {len(rows)} | ★한국제조사 차단 {len(blocked)} | 통과 {passed} | mfr없음 {nomfr} | 판정실패 {failed}")
    for s in blocked[:25]:
        logger.info(f"  · {s[0]} | mfr={s[1]} | {s[2]}")
    logger.info("=== 완료 ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-fetch", action="store_true", help="manufacturer 수집 건너뛰고 판정만")
    args = ap.parse_args()
    main(args.limit, not args.no_fetch)
