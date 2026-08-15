"""mis-map 정밀 스코핑 — 필수속성 파일 식품 카테고리 상품을 쿠팡 predict API로 재분류.
쿠팡 ML 추천 대분류가 '식품'이 아니면 mis-map 후보. 결과를 /tmp/mismap.csv 로 저장(삭제+재등록 입력).
읽기전용 (쿠팡 predict GET/POST + DB 조회만, 변경 없음)."""
import csv
import os
import sqlite3
import sys
import time

import requests
from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
from backend.purchase import database
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)
from backend.purchase.services import coupang_service as cs
DB = os.path.join(_ROOT, "backend/purchase/purchase.db")
XLSX = os.environ.get("REQ_ATTRS_XLSX", "/tmp/req_attrs.xlsx")
OUT = os.environ.get("MISMAP_CSV", "/tmp/mismap.csv")
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("scope")


def predict(name, brand=""):
    path = "/v2/providers/openapi/apis/api/v1/categorization/predict"
    try:
        r = requests.post(cs.BASE + path, headers=cs._signature("POST", path),
                          json={"productName": name, "brand": brand or "", "productDescription": ""}, timeout=20)
        d = (r.json() or {}).get("data") or {}
        return str(d.get("predictedCategoryId") or ""), d.get("predictedCategoryName") or "", d.get("autoCategorizationPredictionResultType")
    except Exception as e:
        return "", f"ERR:{e}", "ERROR"


def top(path):
    p = str(path or "")
    return p.split(">")[0].strip()


def main(limit):
    import openpyxl
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    ws = wb["템플릿"]
    rows = [r for r in ws.iter_rows(min_row=7, values_only=True) if r[0] and r[0] != "Inventory ID"]
    food = [r for r in rows if "식품" in str(r[6] or "")]
    if limit:
        food = food[:limit]
    logger.info(f"[1] 식품 카테고리 대상: {len(food)}건 (predict 조회 시작)")

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    catpath = {str(r["code"]): (r["path"] or "") for r in con.execute("SELECT code,path FROM coupang_categories")}

    mismap = []        # 식품→비식품 (top 다름)
    food_stays = 0     # predict 도 식품
    pred_unknown = 0   # predict 코드가 우리 카테고리 테이블에 없음
    pred_fail = 0
    by_top = {}
    for i, r in enumerate(food, 1):
        cpid = str(r[0])
        pr = con.execute("SELECT p.id pid, p.title_ko tk, p.title_en te, p.brand br, l.coupang_category_code cur "
                         "FROM products p JOIN listings_pa l ON l.product_id=p.id "
                         "WHERE l.channel='coupang' AND l.channel_product_id=? LIMIT 1", (cpid,)).fetchone()
        if not pr:
            continue
        name = (pr["tk"] or pr["te"] or "")
        pid, cur = pr["pid"], str(pr["cur"] or "")
        pcode, pname, ptype = predict(name, pr["br"] or "")
        if ptype != "SUCCESS" or not pcode:
            pred_fail += 1
            continue
        ppath = catpath.get(pcode, "")
        ptop = top(ppath)
        if not ppath:
            pred_unknown += 1
            # path 없으면 보수적으로 mis-map 후보에 넣되 별도 표시
            mismap.append((pid, cpid, catpath.get(cur, ""), pcode, pname, "?(path없음)"))
            continue
        if ptop == "식품":
            food_stays += 1
            continue
        mismap.append((pid, cpid, catpath.get(cur, ""), pcode, pname, ptop))
        by_top[ptop] = by_top.get(ptop, 0) + 1
        if i % 200 == 0:
            logger.info(f"[2] {i}/{len(food)} — mis-map={len(mismap)} 식품유지={food_stays} 실패={pred_fail}")
        time.sleep(0.2)

    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "cpid", "current_path", "predicted_code", "predicted_name", "predicted_top"])
        w.writerows(mismap)

    logger.info("=" * 50)
    logger.info(f"[결과] 대상 {len(food)} | mis-map(식품→비식품) {len(mismap)} | 식품유지 {food_stays} | "
                f"predict코드 path없음 {pred_unknown} | predict실패 {pred_fail}")
    logger.info(f"[mis-map 대분류 분포] {dict(sorted(by_top.items(), key=lambda x:-x[1]))}")
    logger.info(f"[저장] {OUT} ({len(mismap)}건)")
    logger.info("=== 완료 ===")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
