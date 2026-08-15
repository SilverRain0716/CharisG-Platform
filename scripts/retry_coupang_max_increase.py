"""쿠팡 sync 실패 (100% 인상 한도 초과) 재시도 — 한도 내 최대 인상.

배경:
  sync_prices_coupang.py 에서 새 sale_krw 가 기존 외부 가격의 2배 초과면
  쿠팡 API 가 거부 (메시지: "변경전 판매가의 최대 100%인상까지 변경가능합니다").
  본 스크립트는 그 listing 들에 대해 최대 한도 가격(=현재×2)으로 재인상.

대상 추출:
  /tmp/sync_coupang.log 에서 "최대 100%" 사유로 실패한 listing_id 파싱.

동작:
  1. listing_id → DB → channel_product_id
  2. get_seller_product → vendor-item id 들
  3. 각 vendor-item 의 inventories GET → 현재 salePrice
  4. max_new = current * 2 (정확히 100%; 거부되면 -100 재시도)
  5. update_vendor_item_price(vid, max_new)
  6. 성공: listings_pa.sale_krw 와 net_margin/risk 재산정 (audit_prices.py 식)

사용법:
  python3 scripts/retry_coupang_max_increase.py --dry-run [--limit 5]
  python3 scripts/retry_coupang_max_increase.py --apply
"""
import argparse
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, ".")

with open(".env") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from backend.purchase.services.coupang_service import (
    get_seller_product, update_vendor_item_price,
    _request_with_retry, _signature
)

BASE = "https://api-gateway.coupang.com"
DB_PATH = "backend/purchase/purchase.db"
SYNC_LOG = "/tmp/sync_coupang.log"

# audit_prices.py 의 마진식 reuse
def _net_margin(api_price_usd, sale_krw, fee_rate, fx, settings):
    forwarder = settings.get("margin.forwarder_fee_krw", 5000)
    cs_cost = settings.get("margin.cs_cost_krw", 2000)
    return_rsv = sale_krw * settings.get("margin.return_reserve_pct", 3) / 100
    cost_krw = api_price_usd * fx
    fee = sale_krw * fee_rate
    net = sale_krw - cost_krw - forwarder - cs_cost - return_rsv - fee
    pct = round(net / sale_krw * 100, 2) if sale_krw else 0.0
    risk = "safe" if pct >= 20 else ("warning" if pct >= 10 else "danger")
    return int(net), pct, risk


def _load_settings(conn):
    keys = ("margin.forwarder_fee_krw", "margin.cs_cost_krw", "margin.return_reserve_pct",
            "exchange_rate_usd_krw", "margin.default_fx_rate", "coupang_fee_rate")
    out = {}
    for k in keys:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
        if row and row["value"] not in (None, ""):
            try:
                out[k] = float(row["value"])
            except (TypeError, ValueError):
                pass
    if "exchange_rate_usd_krw" not in out and "margin.default_fx_rate" in out:
        out["exchange_rate_usd_krw"] = out["margin.default_fx_rate"]
    return out


def get_inv_price(vid):
    path = f"/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vid}/inventories"
    r = _request_with_retry("GET", BASE + path, headers=_signature("GET", path), timeout=15)
    if r is None or r.status_code >= 400:
        return None
    body = r.json() or {}
    d = body.get("data") if isinstance(body.get("data"), dict) else body
    return d.get("salePrice")


def parse_failed_ids(log_path: str):
    """log 에서 '최대 100%' 사유로 실패한 listing_id 추출."""
    out = set()
    pat = re.compile(r"#(\d+).*?최대 100%")
    with open(log_path) as f:
        for line in f:
            if "✗" not in line:
                continue
            if "최대 100%" not in line:
                continue
            m = pat.search(line)
            if m:
                out.add(int(m.group(1)))
    return sorted(out)


def run(apply_changes: bool, limit: int):
    listing_ids = parse_failed_ids(SYNC_LOG)
    if limit:
        listing_ids = listing_ids[:limit]
    print(f"재시도 대상: {len(listing_ids)} listings (100% 인상 한도 초과로 실패)")
    print(f"모드: {'APPLY' if apply_changes else 'DRY-RUN'}")
    print("=" * 110)

    conn = sqlite3.connect(DB_PATH, timeout=180)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=180000")
    conn.isolation_level = None

    settings = _load_settings(conn)
    fx = settings.get("exchange_rate_usd_krw", 1477.0)
    fee_rate = settings.get("coupang_fee_rate", 0.1374)

    stats = {"checked": 0, "ok": 0, "fail": 0, "skip": 0}
    fails = []

    for idx, lid in enumerate(listing_ids, 1):
        stats["checked"] += 1
        row = conn.execute(
            """SELECT l.id, l.channel_product_id, l.sale_krw, p.amazon_price_usd, p.asin, p.title_en
               FROM listings_pa l JOIN products p ON p.id=l.product_id
               WHERE l.id = ?""",
            (lid,),
        ).fetchone()
        if not row:
            stats["skip"] += 1
            continue
        cpid = row["channel_product_id"]
        api_price = row["amazon_price_usd"] or 0.0

        sp = get_seller_product(cpid)
        items = ((sp or {}).get("data") or {}).get("items") or []
        if not items:
            stats["skip"] += 1
            print(f"[{idx}/{len(listing_ids)}] · #{lid} {row['asin']} cpid={cpid} → vendor-item 없음")
            continue

        vid = str(items[0].get("vendorItemId"))
        cur = get_inv_price(vid)
        if not cur:
            stats["skip"] += 1
            print(f"[{idx}/{len(listing_ids)}] · #{lid} {row['asin']} → 현재 가격 조회 실패")
            continue

        # 정확히 100% 인상 가격 (100원 단위 floor)
        max_new = int(cur * 2 // 100 * 100)
        # 한 번 거부되면 100원 단위 -100 으로 재시도 (최대 5회)
        title = (row["title_en"] or "")[:40]

        if not apply_changes:
            net_krw, net_pct, risk = _net_margin(api_price, max_new, fee_rate, fx, settings)
            print(f"[{idx}/{len(listing_ids)}] · #{lid} {row['asin']}  cur=₩{cur:,} → max=₩{max_new:,}  "
                  f"net={net_krw:,} ({net_pct}% {risk})  {title}")
            continue

        # apply
        attempt_price = max_new
        success = False
        last_msg = ""
        for retry in range(5):
            ok, msg = update_vendor_item_price(vid, attempt_price)
            last_msg = msg
            if ok:
                success = True
                break
            # 100% 한도 메시지면 -100 으로 재시도
            if "최대 100%" in (msg or ""):
                attempt_price -= 100
                continue
            break  # 다른 에러는 retry 안 함
        time.sleep(0.4)

        if success:
            stats["ok"] += 1
            net_krw, net_pct, risk = _net_margin(api_price, attempt_price, fee_rate, fx, settings)
            conn.execute(
                """UPDATE listings_pa SET
                     sale_krw       = ?,
                     net_margin_krw = ?,
                     net_margin_pct = ?,
                     margin_risk    = ?,
                     last_synced_at = datetime('now')
                   WHERE id = ?""",
                (attempt_price, net_krw, net_pct, risk, lid),
            )
            print(f"[{idx}/{len(listing_ids)}] ✓ #{lid} {row['asin']}  cur=₩{cur:,} → ₩{attempt_price:,} "
                  f"(±{retry}회) net={net_krw:,} ({net_pct}% {risk})  {title}")
        else:
            stats["fail"] += 1
            fails.append({"lid": lid, "asin": row["asin"], "cpid": cpid, "vid": vid, "msg": last_msg, "title": title})
            print(f"[{idx}/{len(listing_ids)}] ✗ #{lid} {row['asin']}  → {last_msg[:120]}  {title}")

    conn.close()

    print()
    print("=" * 110)
    print(f"확인: {stats['checked']}  성공: {stats['ok']}  실패: {stats['fail']}  스킵: {stats['skip']}")
    if fails:
        print(f"\n=== 실패 TOP 30 ===")
        for f in fails[:30]:
            print(f"  #{f['lid']} {f['asin']} vid={f['vid']}: {f['msg'][:120]}  {f['title']}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args()

    run(bool(args.apply), args.limit)
