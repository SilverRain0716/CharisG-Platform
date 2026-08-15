"""마진 위험 listing 판매중지 — margin_risk IN ('warning','danger') AND status='listed'.

사용자 정책: 마진 < 20% 인 listing 은 판매하지 않음 (sync 직후 재산정된 net_margin_pct 기준).

사용법:
  python3 scripts/stop_sales_low_margin.py --dry-run
  python3 scripts/stop_sales_low_margin.py --apply
"""
import argparse
import os
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

from backend.purchase.services.coupang_service import stop_sales as coupang_stop
from backend.purchase.services.naver_commerce_service import stop_sales as naver_stop

DB_PATH = "backend/purchase/purchase.db"


def run(apply_changes: bool):
    conn = sqlite3.connect(DB_PATH, timeout=180)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=180000")
    conn.isolation_level = None

    rows = conn.execute(
        """SELECT l.id, l.channel, l.channel_product_id, l.sale_krw,
                  l.net_margin_krw, l.net_margin_pct, l.margin_risk,
                  p.asin, p.title_en
           FROM listings_pa l JOIN products p ON p.id = l.product_id
           WHERE l.status = 'listed'
             AND l.margin_risk IN ('warning', 'danger')
             AND l.channel_product_id IS NOT NULL
             AND l.channel_product_id != ''
           ORDER BY l.net_margin_pct"""
    ).fetchall()

    by = {}
    for r in rows:
        k = (r["channel"], r["margin_risk"])
        by[k] = by.get(k, 0) + 1

    print(f"대상: {len(rows)} listings  분포: {dict(by)}")
    print(f"모드: {'APPLY' if apply_changes else 'DRY-RUN'}")
    print("=" * 110)

    if not apply_changes:
        for r in rows[:30]:
            print(f"  [{r['channel']:10s}] #{r['id']} {r['asin']}  sale=₩{r['sale_krw']:,}  "
                  f"net=₩{r['net_margin_krw']:,} ({r['net_margin_pct']}%)  risk={r['margin_risk']}  "
                  f"{(r['title_en'] or '')[:50]}")
        if len(rows) > 30:
            print(f"  ... ({len(rows) - 30} 건 더)")
        return

    stats = {"checked": 0, "ok": 0, "fail": 0}
    for idx, r in enumerate(rows, 1):
        stats["checked"] += 1
        ch = r["channel"]
        cpid = r["channel_product_id"]
        title = (r["title_en"] or "")[:40]

        try:
            if ch == "coupang":
                ok, msg = coupang_stop(cpid)
                time.sleep(0.6)
            elif ch == "smartstore":
                ok, msg = naver_stop(cpid)
            else:
                ok, msg = False, f"unknown {ch}"
        except Exception as e:
            ok, msg = False, f"{type(e).__name__}: {str(e)[:200]}"

        if ok:
            stats["ok"] += 1
            conn.execute(
                "UPDATE listings_pa SET status='paused', last_synced_at=datetime('now') WHERE id=?",
                (r["id"],),
            )
            print(f"[{idx}/{len(rows)}] ✓ {ch:10s} #{r['id']} {r['asin']}  "
                  f"net={r['net_margin_pct']}% → paused  {title}")
        else:
            stats["fail"] += 1
            print(f"[{idx}/{len(rows)}] ✗ {ch:10s} #{r['id']} {r['asin']}  → {msg[:100]}  {title}")

    conn.close()
    print()
    print("=" * 110)
    print(f"확인: {stats['checked']}  성공: {stats['ok']}  실패: {stats['fail']}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = p.parse_args()

    run(bool(args.apply))
