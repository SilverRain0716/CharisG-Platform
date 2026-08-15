"""mis-map 상품 삭제 + 재등록 — in-place 카테고리 변경 불가(쿠팡 정책)로 인한 유일한 교정법.

입력: /tmp/mismap.csv (scope_mismap_predict.py 산출 — 식품에 잘못 박힌 상품들).
절차(상품당):
  1) stop_sales(cpid)        — 판매중지 (삭제 선행조건)
  2) delete_product(cpid)    — 쿠팡 상품 삭제
  3) listings_pa 초기화: channel_product_id=NULL, coupang_category_code=NULL,
     coupang_auto_matched=1, status='pending'  (재등록 시 category="0" → 쿠팡 자동매칭)
  4) list_product(product_id) — 재등록. 자동매칭이 올바른 카테고리 할당.
  5) 재등록 후 GET 으로 새 카테고리 검증 (식품으로 또 가면 경고).
⚠️ 비가역: 삭제 시 리뷰/판매지표/URL 소실. delete 후 relist 실패 시 product_id 로 재시도 가능.

실행:
  .venv/bin/python -m backend.purchase.scripts.delete_relist_mismap            # dry-run (계획만)
  .venv/bin/python -m backend.purchase.scripts.delete_relist_mismap --limit 1 --apply   # 1건 검증
  nohup .venv/bin/python -m backend.purchase.scripts.delete_relist_mismap --apply > /tmp/relist.log 2>&1 &
"""
import argparse
import csv
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
CSV_IN = os.environ.get("MISMAP_CSV", "/tmp/mismap.csv")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("relist")


def load_rows(limit):
    with open(CSV_IN, newline="") as f:
        rows = [r for r in csv.DictReader(f)]
    return rows[:limit] if limit else rows


def main(limit, apply, min_disk_gb, explicit=False):
    from backend.purchase.services import coupang_service as cs
    from backend.purchase.services import coupang_lister
    rows = load_rows(limit)
    logger.info(f"[1] 대상 {len(rows)}건 (apply={apply}, explicit={explicit}, CSV={CSV_IN})")
    if not apply:
        for r in rows[:15]:
            logger.info(f"  pid={r['product_id']} cpid={r['cpid']} → {r['predicted_top']} / {r['predicted_name']}")
        logger.info("=== dry-run 종료 (--apply 로 실행) ===")
        return

    catpath = {}
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    for r in con.execute("SELECT code,path FROM coupang_categories"):
        catpath[str(r[0])] = r[1] or ""
    con.close()

    deleted = relisted = del_fail = relist_fail = still_food = skipped = 0
    orphan_ids = []  # 삭제됐으나 재등록 실패 (수동 재시도 필요)
    for i, r in enumerate(rows, 1):
        pid = int(r["product_id"]); cpid = str(r["cpid"])
        # 디스크 가드 (재등록 이미지 재다운로드)
        st = os.statvfs("/")
        free_gb = st.f_bavail * st.f_frsize / 1e9
        if free_gb < min_disk_gb:
            logger.warning(f"★디스크 {free_gb:.1f}GB < {min_disk_gb}GB — 중단 (i={i})")
            break

        # 현재 cpid 일치 확인 (이미 처리됐으면 skip)
        with get_db() as conn:
            lr = conn.execute("SELECT channel_product_id, status FROM listings_pa "
                              "WHERE product_id=? AND channel='coupang'", (pid,)).fetchone()
        if not lr or str(lr["channel_product_id"] or "") != cpid:
            skipped += 1
            continue

        # 1) 판매중지
        s_ok, s_err = cs.stop_sales(cpid)
        # 2) 삭제 (판매중지 실패해도 삭제 시도 — 이미 중지상태일 수 있음)
        d_ok, d_err = cs.delete_product(cpid)
        if not d_ok:
            del_fail += 1
            if del_fail <= 20:
                logger.warning(f"  pid={pid} cpid={cpid} 삭제 실패(stop={s_ok}): {d_err}")
            continue
        deleted += 1
        # 3) listings_pa 초기화
        #   - explicit 모드: CSV의 predict 코드를 명시 전송 (auto_matched=0). 자동매칭 해제 시 사용.
        #   - 기본 모드: category="0" 자동매칭 위임 (auto_matched=1).
        pcode = str(r.get("predicted_code") or "").strip()
        with get_db() as conn:
            if explicit and pcode:
                conn.execute("UPDATE listings_pa SET channel_product_id=NULL, coupang_category_code=?, "
                             "coupang_auto_matched=0, status='pending', error_message=NULL, "
                             "last_synced_at=CURRENT_TIMESTAMP WHERE product_id=? AND channel='coupang'", (pcode, pid))
            else:
                conn.execute("UPDATE listings_pa SET channel_product_id=NULL, coupang_category_code=NULL, "
                             "coupang_auto_matched=1, status='pending', error_message=NULL, "
                             "last_synced_at=CURRENT_TIMESTAMP WHERE product_id=? AND channel='coupang'", (pid,))
        # 4) 재등록
        try:
            res = coupang_lister.list_product(pid)
        except Exception as e:
            res = {"ok": False, "error": f"예외:{e}"}
        if not res.get("ok"):
            relist_fail += 1
            orphan_ids.append(pid)
            logger.error(f"  ★pid={pid} 삭제됨but재등록실패: {res.get('error')}")
            continue
        relisted += 1
        # 5) 새 카테고리 검증
        new_cpid = res.get("result", {}).get("sellerProductId") if isinstance(res.get("result"), dict) else None
        time.sleep(1)
        newcat = None
        if new_cpid:
            info = cs.get_seller_product(str(new_cpid))
            newcat = info.get("data", {}).get("displayCategoryCode") if info else None
        newtop = catpath.get(str(newcat), "").split(">")[0].strip() if newcat else "?"
        if newtop == "식품":
            still_food += 1
            logger.warning(f"  ⚠pid={pid} 재등록됐으나 또 식품 (newcat={newcat}) new_cpid={new_cpid}")
        else:
            logger.info(f"  ✓pid={pid} {cpid}→삭제, 재등록 new_cpid={new_cpid} cat={newcat}({newtop})")
        if i % 50 == 0:
            logger.info(f"[2] {i}/{len(rows)} — 삭제={deleted} 재등록={relisted} 삭제실패={del_fail} "
                        f"재등록실패={relist_fail} 또식품={still_food} skip={skipped}")
        time.sleep(0.5)

    logger.info(f"[3] 완료 — 삭제={deleted} 재등록={relisted} 삭제실패={del_fail} 재등록실패={relist_fail} "
                f"또식품={still_food} skip(이미처리)={skipped}")
    if orphan_ids:
        logger.error(f"★삭제됐으나 재등록 실패 {len(orphan_ids)}건 (list_product 재시도 필요): {orphan_ids[:80]}")
    logger.info("=== 완료 ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--min-disk-gb", type=float, default=8.0, help="이 미만이면 중단 (이미지 재다운로드 보호)")
    ap.add_argument("--explicit", action="store_true", help="CSV predict 코드를 명시 전송 (자동매칭 해제 시)")
    args = ap.parse_args()
    main(args.limit, args.apply, args.min_disk_gb, args.explicit)
