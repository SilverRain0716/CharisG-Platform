#!/usr/bin/env python3
"""미매칭 쿠팡 주문 자동 재매칭.
- 주문 인입 시 DB 경합 등으로 external_sku(PA-<product_id>)가 product_id 로 매칭 안 된 건을 주기적으로 복구.
- PA-<id> 파싱 → product <id> 조회(cold) → product_id + 캐시(asin/name/brand/image) 채움(hot).
- systemd timer 10분마다. 2026-06-08 신설 (사용자 지시).
"""
import os, sys, re, json, time, sqlite3

ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
COLD = os.path.join(ROOT, "backend/purchase/purchase.db")        # products
HOT = os.path.join(ROOT, "backend/purchase/purchase_hot.db")     # orders

def log(m):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} rematch: {m}", flush=True)

def conn(path):
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    return c

def main():
    hot = conn(HOT)
    rows = hot.execute(
        "SELECT id, external_sku FROM orders "
        "WHERE channel='coupang' AND (product_id IS NULL OR product_id='') "
        "AND external_sku LIKE 'PA-%'"
    ).fetchall()
    if not rows:
        log("미매칭 주문 없음")
        hot.close()
        return
    log(f"미매칭 쿠팡 주문 {len(rows)}건 — 재매칭 시도")
    cold = conn(COLD)
    matched = skipped = 0
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for o in rows:
        m = re.match(r"^PA-(\d+)$", (o["external_sku"] or "").strip())
        if not m:
            skipped += 1
            continue
        pid = int(m.group(1))
        p = cold.execute(
            "SELECT id, asin, brand, title_ko, images_json FROM products WHERE id=?", (pid,)
        ).fetchone()
        if not p:
            log(f"  order {o['id']} {o['external_sku']} → product {pid} 없음, skip")
            skipped += 1
            continue
        img = ""
        try:
            imgs = json.loads(p["images_json"] or "[]")
            img = imgs[0] if imgs else ""
        except Exception:
            pass
        hot.execute(
            "UPDATE orders SET product_id=?, asin_cache=?, product_name_cache=?, "
            "brand_cache=?, product_image_cache=? WHERE id=?",
            (pid, p["asin"], p["title_ko"], p["brand"], img, o["id"]),
        )
        matched += 1
        log(f"  order {o['id']} → product {pid} ({(p['title_ko'] or '')[:25]}) 매칭")
    hot.commit()
    cold.close()
    hot.close()
    log(f"=== 재매칭 완료: {matched}건 매칭 / {skipped}건 skip ===")

if __name__ == "__main__":
    main()
