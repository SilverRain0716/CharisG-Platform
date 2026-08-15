"""쿠팡 카테고리 API 진단:
  ① 자동매칭 서비스 동의 여부 (check-auto-category-agreed) — 켜져 있으면 우리가 보낸
     displayCategoryCode 를 쿠팡이 무시하고 ML 자동할당 → 카테고리 PUT 안 먹히는 원인.
  ② 카테고리 추천 API (categorization/predict) — 쿠팡 공식 ML 이 각 상품을 뭐라 추천하는지.
읽기전용."""
import json
import os
import sqlite3
import sys

import requests
from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
from backend.purchase import database
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)
from backend_shared._config import COUPANG_VENDOR_ID
from backend.purchase.services import coupang_service as cs
DB = os.path.join(_ROOT, "backend/purchase/purchase.db")


def predict(name, brand="", desc=""):
    path = "/v2/providers/openapi/apis/api/v1/categorization/predict"
    body = {"productName": name, "brand": brand or "", "productDescription": desc or ""}
    r = requests.post(cs.BASE + path, headers=cs._signature("POST", path), json=body, timeout=20)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:200]}


def main(cpids):
    # ① 자동매칭 동의 여부
    apath = f"/v2/providers/seller_api/apis/api/v1/marketplace/vendors/{COUPANG_VENDOR_ID}/check-auto-category-agreed"
    ar = requests.get(cs.BASE + apath, headers=cs._signature("GET", apath), timeout=15)
    print("=== ① 자동매칭 동의 (vendorId=%s) ===" % COUPANG_VENDOR_ID)
    print("   status=%s body=%s" % (ar.status_code, ar.text[:200]))

    # ② 쿠팡 ML 추천 vs 현재 카테고리
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    print("\n=== ② 쿠팡 ML 카테고리 추천 ===")
    for cpid in cpids:
        pr = con.execute("SELECT title_ko, title_en, brand FROM products p JOIN listings_pa l ON l.product_id=p.id "
                         "WHERE l.channel='coupang' AND l.channel_product_id=? LIMIT 1", (cpid,)).fetchone()
        name = (pr["title_ko"] if pr else "") or ""
        brand = (pr["brand"] if pr else "") or ""
        info = cs.get_seller_product(str(cpid))
        cur = info.get("data", {}).get("displayCategoryCode") if info else None
        sc, body = predict(name, brand)
        data = body.get("data") or {}
        print("  cpid=%s | 현재=%s" % (cpid, cur))
        print("    상품명: %s" % name[:50])
        print("    쿠팡추천: type=%s id=%s name=%s" % (
            data.get("autoCategorizationPredictionResultType"),
            data.get("predictedCategoryId"), data.get("predictedCategoryName")))


if __name__ == "__main__":
    main(sys.argv[1:] or ["16212001141", "16222513675", "16217254793"])
