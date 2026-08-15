"""ASIN enrichment 임포트 — 시트의 신규 ASIN을 Amazon SP-API로 보강해 draft product 생성.

이 시트(11번가 미러)는 Amazon USD 원가가 없어 표준 import 가 안 됨 → ASIN 기반으로 직접 보강:
  1) ProductsV0 get_competitive_pricing (batch 20) → landed/listing/shipping (실원가)
  2) CatalogItems(fetch_product_info_sp_api) → title_en/brand/images/identifiers/amazon_price_usd
  3) cost_usd = landed (없으면 amazon_price_usd=list_price 폴백). cost 전무하면 skip.
  4) INSERT products (business_model='purchase', status='draft', ...). 시트 KRW가는 미사용.
  생성된 product_id → /tmp/enrich_pids.csv (이후 catchup AI→리스팅 입력).
중복: 이미 products에 있는 ASIN skip.

실행:
  .venv/bin/python -m backend.purchase.scripts.enrich_import_sheet "<url>" --limit 5      # 검증
  nohup .venv/bin/python -m backend.purchase.scripts.enrich_import_sheet "<url>" > /tmp/enrich.log 2>&1 &
"""
import argparse
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
from backend.purchase import database
from backend.purchase.database import get_db
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)
DB = os.path.join(_ROOT, "backend/purchase/purchase.db")
PIDS_OUT = os.environ.get("ENRICH_PIDS", "/tmp/enrich_pids.csv")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("enrich-import")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sheet_asins(url):
    from backend.purchase.services import sheet_importer as si
    sid = si.extract_sheet_id(url)
    r = si.discover_tabs(sid)
    tabs = r[0] if isinstance(r, tuple) else r
    asins, cat = [], {}
    for t in tabs:
        for row in si.fetch_tab_csv(sid, t["gid"]):
            pr = si.parse_row(row)
            a = None
            if pr:
                a = pr["asin"]
            else:
                # parse_row 가 price_missing 으로 None 반환할 수 있음 → ASIN 직접 추출
                av = (row.get("ASIN") or row.get("asin") or "").strip().upper()
                if si.ASIN_RE.match(av):
                    a = av
            if a:
                asins.append(a)
                cat[a] = (row.get("카테고리") or "").strip()
    # dedup 보존순서
    seen = set(); out = []
    for a in asins:
        if a not in seen:
            seen.add(a); out.append(a)
    return out, cat


def _weight_g(info):
    # CatalogItems dimensions: flat {"weight": 0.5375, "weight_unit": "pounds", ...}
    d = info.get("dimensions") or {}
    if isinstance(d, list):
        d = d[0] if d else {}
    w = d.get("weight")
    if not w:
        return None
    try:
        v = float(w)
    except (ValueError, TypeError):
        return None
    u = str(d.get("weight_unit", "")).lower()
    if "pound" in u or "lb" in u:
        return int(v * 453.592)
    if "ounce" in u or "oz" in u:
        return int(v * 28.3495)
    if "kilogram" in u or "kg" in u:
        return int(v * 1000)
    if "gram" in u or u == "g":
        return int(v)
    return None


def main(url, limit, apply):
    # ★2026-08-12 차단 — products.status='draft' 로 직행하고 전건 SP-API 를 부른다.
    #   새 파이프라인은 M5 경량조회 → M6 top10 → M10 상세수집 순이라 비용 구조가 정반대다.
    #   draft 138,312건이 이 경로로 쌓여 전량 삭제했다. 되살리려면 PA_ALLOW_LEGACY_IMPORT=1.
    import os as _os
    if _os.environ.get("PA_ALLOW_LEGACY_IMPORT") != "1":
        raise SystemExit(
            "[차단] enrich_import_sheet 는 중단됐습니다.\n"
            "  새 경로: python3 ~/asin_import.py <입력> --batch <배치명> --channel <채널> --apply\n"
            "  강제 실행: PA_ALLOW_LEGACY_IMPORT=1"
        )
    from backend.purchase.services.image_downloader import fetch_product_info_sp_api
    asins, cat = _sheet_asins(url)
    logger.info(f"[1] 시트 ASIN: {len(asins)}건")

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    existing = {a for (a,) in con.execute("SELECT DISTINCT asin FROM products WHERE asin IS NOT NULL")}
    con.close()
    new = [a for a in asins if a not in existing]
    logger.info(f"[2] 신규 ASIN(products에 없음): {len(new)}건 (기존 {len(asins)-len(new)} skip)")
    if limit:
        new = new[:limit]
        logger.info(f"[2L] 검증 {len(new)}건만")
    if not new:
        logger.info("신규 0 — 종료"); return

    # ProductsV0 client
    landed = {}
    try:
        from sp_api.api.products.products_v0 import ProductsV0
        from sp_api.base import Marketplaces
        from backend.dropshipping.services.amazon_sp_api_service import get_credentials
        from backend.purchase.scripts.refresh_landed_prices import _fetch_prices_batch
        client = ProductsV0(credentials=get_credentials(), marketplace=Marketplaces.US)
        for i in range(0, len(new), 20):
            batch = new[i:i+20]
            try:
                landed.update(_fetch_prices_batch(batch, client))
            except Exception as e:
                logger.warning(f"  landed batch {i} 실패: {e}")
            time.sleep(0.5)
    except Exception as e:
        logger.warning(f"ProductsV0 초기화 실패: {e}")

    created = skip_nocost = fail = 0
    pids = []
    for a in new:
        info = fetch_product_info_sp_api(a)
        if not info or not info.get("title"):
            fail += 1; continue
        lp = landed.get(a, {})
        cost = lp.get("landed") or info.get("amazon_price_usd")
        if not cost or cost <= 0:
            skip_nocost += 1
            logger.info(f"  {a} cost 없음 — skip (landed={lp.get('landed')} list={info.get('amazon_price_usd')})")
            continue
        images_json = json.dumps(info.get("images") or [], ensure_ascii=False) if info.get("images") else None
        ident_json = json.dumps(info.get("identifiers") or [], ensure_ascii=False) if info.get("identifiers") else None
        wt = _weight_g(info)
        if not apply:
            logger.info(f"  [dry] {a} | {str(info.get('title'))[:40]} | cost={cost} img={len(info.get('images') or [])} wt={wt}")
            continue
        now = _now()
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO products (business_model, status, asin, title_en, brand, category_path, "
                "images_json, identifiers_json, cost_usd, amazon_price_usd, landed_price_usd, "
                "listing_price_usd, shipping_usd, weight_g, is_group_master, created_at, updated_at, attributes_updated_at) "
                "VALUES ('purchase','draft',?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)",
                (a, info.get("title"), info.get("brand"), None,  # category_path: 시트 카테고리(11번가 라벨) 무시 → 리스팅 때 결정
                 images_json, ident_json, cost, info.get("amazon_price_usd"),
                 lp.get("landed"), lp.get("listing"), lp.get("shipping"), wt, now, now, now))
            pids.append(cur.lastrowid)
        created += 1
        if created % 100 == 0:
            logger.info(f"  생성 {created}/{len(new)} (skip_nocost={skip_nocost} fail={fail})")

    if apply and pids:
        with open(PIDS_OUT, "w") as f:
            f.write("\n".join(str(p) for p in pids))
    logger.info(f"[3] 완료 — 생성={created} cost없음skip={skip_nocost} 조회실패={fail} | pids→{PIDS_OUT}")
    logger.info("=== 완료 ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    main(args.url, args.limit, args.apply)
