"""쿠팡 listed 상품에 DB GTIN(EAN/UPC) 백필 — 바코드 미등록분.

사용자 지시(2026-05-25) (b): DB엔 GTIN 있는데 쿠팡 바코드 미등록인 27K건 채움.
방법(상품당): get_seller_product → items[].barcode=GTIN, emptyBarcode=False → PUT(전체)
              → 임시저장 전환 대기 → request_approval(즉시 자동승인, 재노출). (이름갱신과 동일)
멱등: 이미 바코드 있으면 skip. 재실행 시 이어감.

실행:
  .venv/bin/python -m backend.purchase.scripts.backfill_coupang_gtin --limit 5 --apply   # 테스트
  nohup .venv/bin/python -m backend.purchase.scripts.backfill_coupang_gtin --limit 99999 --apply > /tmp/gtinbf.log 2>&1 &
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
import time

from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
DB = os.path.join(_ROOT, "backend/purchase/purchase.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("gtin-backfill")


def extract_barcode(ij):
    if not ij:
        return ""
    try:
        arr = json.loads(ij)
    except Exception:
        return ""
    by = {}
    for x in (arr if isinstance(arr, list) else []):
        if isinstance(x, dict) and x.get("value"):
            by[str(x.get("type") or "").upper()] = str(x["value"]).strip()
    for t in ("EAN", "UPC", "GTIN"):
        v = by.get(t)
        if v and v.isdigit() and 8 <= len(v) <= 14:
            return v
    return ""


def set_barcode(cs, cpid, barcode):
    info = cs.get_seller_product(str(cpid))
    if not info:
        return "fail", "조회실패"
    data = info.get("data")
    if not isinstance(data, dict):
        return "fail", "data없음"
    items = data.get("items") or []
    if not items:
        return "fail", "items없음"
    if all((str(it.get("barcode") or "").strip()) for it in items):
        return "skip", "이미바코드"
    for it in items:
        it["barcode"] = barcode
        it["emptyBarcode"] = False
        it["emptyBarcodeReason"] = ""
    path = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
    r = cs._request_with_retry("PUT", cs.BASE + path, headers=cs._signature("PUT", path), json=data, timeout=30)
    if r is None:
        return "fail", "no response"
    body = r.json() if r.text else {}
    if r.status_code < 400 and isinstance(body, dict) and body.get("code") != "ERROR":
        return "put_ok", ""
    return "fail", f"status={r.status_code} " + "; ".join(cs._extract_error_messages(body))[:120]


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


def main(limit, apply, acct=None, since=None):
    from backend.purchase.services import coupang_service as cs
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    # ★계정 필터(2026-08-04): acct_key 를 안 거르면 신계정 상품을 구계정 자격증명으로 PUT 하게 된다.
    _sql = ("SELECT l.channel_product_id cpid, p.identifiers_json ij FROM products p "
            "JOIN listings_pa l ON l.product_id=p.id "
            "WHERE l.channel='coupang' AND l.status='listed' AND l.channel_product_id IS NOT NULL "
            "AND p.identifiers_json IS NOT NULL AND p.identifiers_json NOT IN ('','[]','null') ")
    _p = []
    if acct:
        _sql += "AND l.acct_key=? "
        _p.append(acct)
    if since:
        _sql += "AND l.created_at >= ? "
        _p.append(since)
    _sql += "ORDER BY l.product_id LIMIT ?"
    _p.append(limit)
    rows = con.execute(_sql, tuple(_p)).fetchall()
    targets = [(r["cpid"], extract_barcode(r["ij"])) for r in rows]
    targets = [(c, b) for c, b in targets if b]
    logger.info(f"[1] 백필 대상(GTIN 추출됨): {len(targets)}건, acct={acct or '(전체)'}, apply={apply}")
    if not apply:
        for c, b in targets[:10]:
            logger.info(f"  cpid={c} → barcode {b}")
        logger.info("=== 완료 (dry-run) ===")
        return

    put_ok = skip = fail = appr = stuck = 0
    stuck_ids = []
    for i, (cpid, bc) in enumerate(targets, 1):
        st, err = set_barcode(cs, cpid, bc)
        if st == "skip":
            skip += 1
            continue
        if st != "put_ok":
            fail += 1
            if fail <= 20:
                logger.warning(f"  cpid={cpid} PUT 실패: {err}")
            continue
        put_ok += 1
        a_ok = False
        for _ in range(4):
            time.sleep(2)
            _ensure_img_for(str(cpid))
            a_ok, a_err = cs.request_approval(str(cpid))
            if a_ok:
                break
        if a_ok:
            appr += 1
        else:
            stuck += 1
            stuck_ids.append(str(cpid))
        if i % 100 == 0:
            logger.info(f"[2] {i}/{len(targets)} — PUT={put_ok} 재승인={appr} skip={skip} fail={fail} 잔류={stuck}")
    logger.info(f"[2] 완료 — PUT={put_ok} 재승인={appr} skip(이미바코드)={skip} fail={fail} ★임시저장잔류={stuck}")
    if stuck_ids:
        logger.warning(f"★임시저장 잔류 {len(stuck_ids)}건 (재승인 sweep 필요): {stuck_ids[:60]}")
    logger.info("=== 완료 ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--acct", choices=["old", "new"], default=None,
                    help="쿠팡 계정 (미지정 시 활성계정 그대로)")
    ap.add_argument("--since", default=None, help="등록일 하한 (예: 2026-08-01)")
    args = ap.parse_args()
    try:
        if args.acct:
            from backend.purchase.services import coupang_service as _cs
            with _cs.coupang_account(args.acct):
                main(args.limit, args.apply, args.acct, args.since)
        else:
            main(args.limit, args.apply, None, args.since)
    except Exception:
        logger.exception("=== 예외 ===")
        sys.exit(1)
