"""amazon_price_usd 부채 청소 — ProductsV0 batch 가격 fetch + 마진 재산정.

배경:
  audit_prices.py 가 CatalogItems.attributes.list_price 만 보아 list_price 가
  없는 ASIN 의 amazon_price_usd 를 채우지 못함. 결과적으로 NULL/0 인 product 가
  3,389건 (active listing 보유 2,460) 누적되어 cost_snapshot 이 amazon_shipping
  뿐인 채로 listing 됨. 본 스크립트는 ProductsV0.get_competitive_pricing_for_asins
  로 buybox/landed price 를 받아 채워넣는다.

대상:
  products WHERE business_model='purchase' AND status != 'removed'
    AND (amazon_price_usd IS NULL OR amazon_price_usd = 0)
    AND id IN (SELECT product_id FROM listings_pa WHERE status IN ('listed','active'))

갱신:
  products: amazon_price_usd, cost_usd, sale_price_krw, margin_pct
  listings_pa: sale_krw, cost_krw_snapshot, net_margin_krw, net_margin_pct, margin_risk
              (cost_krw_snapshot = api_price × fx — 부채 청소를 위해 함께 갱신)

마진 식 (audit_prices.py 그대로):
  cost_krw      = api_price × fx
  forwarder     = settings('margin.forwarder_fee_krw', 5000)
  cs_cost       = settings('margin.cs_cost_krw', 2000)
  return_pct    = settings('margin.return_reserve_pct', 3) / 100
  fee_rate      = smartstore_fee_rate (0.0548) | coupang_fee_rate (0.1374)
  shipping(국내) = 3000  (sale 역산 시 cost 합산용)
  net           = sale - cost_krw - forwarder - cs_cost - sale*return_pct - sale*fee_rate
  margin_pct    = net / sale * 100
  sale 역산     = (cost_krw + forwarder + cs_cost + shipping) / (1 - target - fee_rate - return_pct)

사용법:
  python3 scripts/backfill_amazon_price.py --dry-run         # 가격 fetch + 변경 미리보기 (DB 미수정)
  python3 scripts/backfill_amazon_price.py --apply           # 실제 갱신
  python3 scripts/backfill_amazon_price.py --apply --limit 5 # 상위 5 product 만
  python3 scripts/backfill_amazon_price.py --apply --asin BXXXXX
"""
import argparse
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, ".")

# .env 로드 (audit_prices.py 와 동일 패턴)
with open(".env") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

DB_PATH = "backend/purchase/purchase.db"
BATCH_SIZE = 20             # ProductsV0 한도
SLEEP_BETWEEN_BATCH = 1.0   # SP-API rate limit 보수
COMMIT_INTERVAL = 100       # product 단위


def _load_pricing_settings(conn):
    keys = [
        "margin_target_rate", "smartstore_fee_rate", "coupang_fee_rate",
        "exchange_rate_usd_krw",
        "margin.forwarder_fee_krw", "margin.cs_cost_krw", "margin.return_reserve_pct",
        "margin.default_fx_rate",
    ]
    out = {}
    for k in keys:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
        if row and row["value"] not in (None, ""):
            try:
                out[k] = float(row["value"])
            except (TypeError, ValueError):
                pass
    # fx fallback: exchange_rate_usd_krw → margin.default_fx_rate
    if "exchange_rate_usd_krw" not in out and "margin.default_fx_rate" in out:
        out["exchange_rate_usd_krw"] = out["margin.default_fx_rate"]
    return out


def _calc_sale_price(cost_usd, channel, settings):
    """채널별 sale_price_krw 역산 — audit_prices.py 의 _calc_sale_price 그대로."""
    fx = settings.get("exchange_rate_usd_krw", 1477.0)
    margin_rate = settings.get("margin_target_rate", 0.35)
    forwarder = settings.get("margin.forwarder_fee_krw", 5000)
    cs_cost = settings.get("margin.cs_cost_krw", 2000)
    return_pct = settings.get("margin.return_reserve_pct", 3) / 100.0
    shipping = 3000

    fee_rate = (settings.get("smartstore_fee_rate", 0.0548) if channel == "smartstore"
                else settings.get("coupang_fee_rate", 0.1374))

    total_cost = cost_usd * fx + forwarder + cs_cost + shipping
    denom = 1.0 - margin_rate - fee_rate - return_pct
    if denom <= 0:
        return None
    sale_raw = total_cost / denom
    return int(math.ceil(sale_raw / 100) * 100)


def _calc_net_margin(api_price_usd, sale_krw, channel, settings):
    """주어진 sale_krw 의 net_margin_krw / pct / risk 산정."""
    fx = settings.get("exchange_rate_usd_krw", 1477.0)
    forwarder = settings.get("margin.forwarder_fee_krw", 5000)
    cs_cost = settings.get("margin.cs_cost_krw", 2000)
    return_rsv = sale_krw * settings.get("margin.return_reserve_pct", 3) / 100
    fee_rate = (settings.get("smartstore_fee_rate", 0.0548) if channel == "smartstore"
                else settings.get("coupang_fee_rate", 0.1374))
    cost_krw = api_price_usd * fx
    fee = sale_krw * fee_rate
    net = sale_krw - cost_krw - forwarder - cs_cost - return_rsv - fee
    pct = round(net / sale_krw * 100, 2) if sale_krw else 0.0
    risk = "safe" if pct >= 20 else ("warning" if pct >= 10 else "danger")
    return int(net), pct, risk


def _fetch_prices_batch(asins, client):
    """ProductsV0.get_competitive_pricing_for_asins → {asin: landed_usd or None}."""
    out = {a: None for a in asins}
    try:
        r = client.get_competitive_pricing_for_asins(asin_list=asins, item_condition="New")
        payload = r.payload if hasattr(r, "payload") else r
        for entry in payload or []:
            asin = entry.get("ASIN") or entry.get("asin")
            if not asin:
                continue
            prod = entry.get("Product") or {}
            cp = (prod.get("CompetitivePricing") or {}).get("CompetitivePrices") or []
            if not cp:
                continue
            # 우선순위: condition=New 의 LandedPrice → ListingPrice
            picked = None
            for p in cp:
                if (p.get("condition") or "").lower() != "new":
                    continue
                price = p.get("Price") or {}
                land = (price.get("LandedPrice") or {}).get("Amount")
                lp = (price.get("ListingPrice") or {}).get("Amount")
                ccy_l = (price.get("LandedPrice") or {}).get("CurrencyCode")
                ccy_p = (price.get("ListingPrice") or {}).get("CurrencyCode")
                if land is not None and (ccy_l or ccy_p) == "USD":
                    picked = float(land)
                    break
                if lp is not None and ccy_p == "USD":
                    picked = float(lp)
                    break
            if picked is not None:
                out[asin] = picked
    except Exception as e:
        print(f"  ⚠ batch fetch 실패: {type(e).__name__}: {str(e)[:200]}")
    return out


def run(apply_changes: bool, limit: int, single_asin: str = None):
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    # 대상 product 수집
    where = [
        "p.business_model = 'purchase'",
        "p.status != 'removed'",
        "(p.amazon_price_usd IS NULL OR p.amazon_price_usd = 0)",
        "p.asin IS NOT NULL",
        "p.asin != ''",
        "EXISTS (SELECT 1 FROM listings_pa l WHERE l.product_id = p.id AND l.status IN ('listed','active'))",
    ]
    params = []
    if single_asin:
        where.append("p.asin = ?")
        params.append(single_asin.upper())
    sql = f"""SELECT p.id, p.asin, p.cost_usd, p.sale_price_krw, p.title_en
              FROM products p
              WHERE {' AND '.join(where)}
              ORDER BY p.id"""
    if limit:
        sql += f" LIMIT {limit}"

    rows = conn.execute(sql, params).fetchall()
    total = len(rows)
    if total == 0:
        print("처리할 product 없음")
        conn.close()
        return

    settings = _load_pricing_settings(conn)
    fx = settings.get("exchange_rate_usd_krw", 1477.0)
    target = settings.get("margin_target_rate", 0.35)
    print(f"대상: {total} product  |  fx={fx}  |  target_margin={target*100:.0f}%")
    print(f"모드: {'APPLY (DB 갱신)' if apply_changes else 'DRY-RUN (미리보기만)'}")
    print("=" * 110)

    # SP-API client
    from sp_api.api.products.products_v0 import ProductsV0
    from sp_api.base import Marketplaces
    from backend.dropshipping.services.amazon_sp_api_service import get_credentials
    creds = get_credentials()
    client = ProductsV0(credentials=creds, marketplace=Marketplaces.US)

    stats = {
        "checked": 0, "fetched": 0, "no_price": 0, "fetch_fail": 0,
        "products_updated": 0, "listings_updated": 0,
        "danger": 0, "warning": 0, "safe": 0,
    }
    danger_list = []
    no_price_list = []

    # batch 단위로 묶어 호출
    asin_to_pid = {r["asin"]: r["id"] for r in rows}
    asin_to_row = {r["asin"]: r for r in rows}
    asins = list(asin_to_pid.keys())

    processed = 0
    for i in range(0, len(asins), BATCH_SIZE):
        chunk = asins[i:i + BATCH_SIZE]
        prices = _fetch_prices_batch(chunk, client)

        for asin in chunk:
            r = asin_to_row[asin]
            pid = r["id"]
            db_cost = r["cost_usd"] or 0.0
            title = (r["title_en"] or "")[:50]
            stats["checked"] += 1
            api_price = prices.get(asin)

            if api_price is None:
                stats["no_price"] += 1
                no_price_list.append({"pid": pid, "asin": asin, "title": title})
                print(f"[{stats['checked']}/{total}] · #{pid} {asin} — 가격 없음  {title}")
                continue
            stats["fetched"] += 1

            # 채널별 sale 산정
            new_sale_ss = _calc_sale_price(api_price, "smartstore", settings)
            new_sale_cp = _calc_sale_price(api_price, "coupang", settings)
            new_sale = new_sale_ss or 0  # products.sale_price_krw 는 smartstore 기준

            # products.margin_pct (smartstore 기준)
            new_margin_pct = None
            if new_sale and api_price > 0:
                _, new_margin_pct, _ = _calc_net_margin(api_price, new_sale, "smartstore", settings)

            # listings_pa: 채널별 갱신
            listings = conn.execute(
                """SELECT id, channel, sale_krw, net_margin_pct, margin_risk
                   FROM listings_pa WHERE product_id = ?""",
                (pid,),
            ).fetchall()

            line_updates = []
            for lst in listings:
                ch = lst["channel"]
                ch_sale = new_sale_ss if ch == "smartstore" else new_sale_cp
                if not ch_sale:
                    continue
                net_krw, net_pct, risk = _calc_net_margin(api_price, ch_sale, ch, settings)
                line_updates.append({
                    "listing_id": lst["id"], "channel": ch,
                    "old_sale": lst["sale_krw"], "new_sale": ch_sale,
                    "old_pct": lst["net_margin_pct"], "new_pct": net_pct,
                    "old_risk": lst["margin_risk"], "new_risk": risk,
                    "net_krw": net_krw,
                })
                stats[risk] = stats.get(risk, 0) + 1
                if risk == "danger":
                    danger_list.append({
                        "pid": pid, "asin": asin, "channel": ch,
                        "sale": ch_sale, "net_pct": net_pct, "net_krw": net_krw,
                        "title": title,
                    })

            # 출력
            risks = "/".join(u["new_risk"][:1].upper() for u in line_updates) or "-"
            print(f"[{stats['checked']}/{total}] #{pid} {asin}  ${api_price:.2f}  →  "
                  f"SS=₩{new_sale_ss:,} ({_calc_net_margin(api_price, new_sale_ss, 'smartstore', settings)[1]:.1f}%) | "
                  f"CP=₩{new_sale_cp:,} ({_calc_net_margin(api_price, new_sale_cp, 'coupang', settings)[1]:.1f}%) "
                  f"[{risks}]  {title}")

            if apply_changes:
                # products
                conn.execute(
                    """UPDATE products SET
                         amazon_price_usd = ?,
                         cost_usd         = ?,
                         sale_price_krw   = COALESCE(?, sale_price_krw),
                         margin_pct       = COALESCE(?, margin_pct),
                         updated_at       = datetime('now')
                       WHERE id = ?""",
                    (api_price, api_price, new_sale or None, new_margin_pct, pid),
                )
                stats["products_updated"] += 1

                cost_snap = int(round(api_price * fx))
                for u in line_updates:
                    conn.execute(
                        """UPDATE listings_pa SET
                             sale_krw          = ?,
                             cost_krw_snapshot = ?,
                             net_margin_krw    = ?,
                             net_margin_pct    = ?,
                             margin_risk       = ?
                           WHERE id = ?""",
                        (u["new_sale"], cost_snap, u["net_krw"], u["new_pct"], u["new_risk"], u["listing_id"]),
                    )
                    stats["listings_updated"] += 1

                if stats["products_updated"] % COMMIT_INTERVAL == 0:
                    conn.commit()
                    print(f"  💾 {stats['products_updated']} products 커밋")

            processed += 1

        # batch sleep
        if i + BATCH_SIZE < len(asins):
            time.sleep(SLEEP_BETWEEN_BATCH)

    if apply_changes:
        conn.commit()

    # 요약
    print()
    print("=" * 110)
    print(f"검수 완료: {stats['checked']} product")
    print(f"  가격 fetch 성공: {stats['fetched']}")
    print(f"  가격 없음: {stats['no_price']}")
    if apply_changes:
        print(f"  products 갱신: {stats['products_updated']}")
        print(f"  listings 갱신: {stats['listings_updated']}")
    print(f"  위험 분포 (listings 단위):  safe={stats.get('safe',0)}  warning={stats.get('warning',0)}  danger={stats.get('danger',0)}")

    if danger_list:
        print(f"\n⚠ DANGER listings (마진 < 10%) — TOP 30:")
        danger_list.sort(key=lambda x: x["net_pct"])
        for d in danger_list[:30]:
            print(f"  pid={d['pid']} {d['asin']} {d['channel']:10s} "
                  f"sale=₩{d['sale']:,} net=₩{d['net_krw']:,} ({d['net_pct']:.1f}%)  {d['title']}")

        # 파일로도 dump
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = f"/tmp/backfill_danger_{ts}.csv"
        with open(out_path, "w") as fp:
            fp.write("pid,asin,channel,sale_krw,net_krw,net_pct,title\n")
            for d in danger_list:
                t = (d["title"] or "").replace(",", " ").replace("\n", " ")
                fp.write(f"{d['pid']},{d['asin']},{d['channel']},{d['sale']},{d['net_krw']},{d['net_pct']},{t}\n")
        print(f"\n  danger 전체 → {out_path}")

    if no_price_list:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = f"/tmp/backfill_no_price_{ts}.csv"
        with open(out_path, "w") as fp:
            fp.write("pid,asin,title\n")
            for d in no_price_list:
                t = (d["title"] or "").replace(",", " ").replace("\n", " ")
                fp.write(f"{d['pid']},{d['asin']},{t}\n")
        print(f"  no_price 전체 → {out_path}  ({len(no_price_list)} 건 — 단종/제한 매물 의심, 별도 검토 필요)")

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="amazon_price_usd 백필 + 마진 재산정")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="DB 미수정, 변경 미리보기만 (기본)")
    g.add_argument("--apply", action="store_true", help="DB 실제 갱신")
    parser.add_argument("--limit", type=int, default=0, help="상위 N product 만")
    parser.add_argument("--asin", type=str, default=None, help="특정 ASIN 1건만")
    args = parser.parse_args()

    apply_changes = bool(args.apply)
    run(apply_changes, args.limit, args.asin)
