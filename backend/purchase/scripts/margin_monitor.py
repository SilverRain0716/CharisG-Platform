"""능동 마진 감시 (Phase 0 백필 + Phase 1 데일리 관찰). 가격 변경 없음 — DB 기록만.

현재 아마존가(refresh-landed가 갱신한 listing_price_usd+shipping_usd) 기준으로 listed 쿠팡
listing 의 실마진을 재계산 → listings_pa.net_margin_krw/pct + margin_status + margin_checked_at 기록.
공식은 margin_calculator 와 동일(설정값 1회 로드, 인라인 계산으로 5만건 고속 처리).

  현재원가 우선순위: listing_price_usd(+shipping_usd, refresh-landed 최신) > cost_usd(리스팅 기준, STALE)
  분류: DANGER(마진<=0) / WARN(0<마진<FLOOR) / OK(>=FLOOR) / STALE(현재가 없음→cost_usd 사용)

실행:
  .venv/bin/python -m backend.purchase.scripts.margin_monitor                 # dry-run(리포트만)
  .venv/bin/python -m backend.purchase.scripts.margin_monitor --apply         # 기록
  .venv/bin/python -m backend.purchase.scripts.margin_monitor --apply --floor 12
"""
import argparse
import logging
import os
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
DB = os.path.join(_ROOT, "backend/purchase/purchase.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("margin-monitor")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_settings(conn):
    keys = ("coupang_fee_rate", "margin.forwarder_fee_krw", "margin.return_reserve_pct",
            "margin.cs_cost_krw", "margin.default_fx_rate", "amazon_shipping_default_usd")
    d = {}
    for k in keys:
        r = conn.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
        if r and r[0] not in (None, ""):
            d[k] = float(r[0])
    return {
        "fee": d.get("coupang_fee_rate", 0.11),
        "forwarder": d.get("margin.forwarder_fee_krw", 5000.0),
        "return_pct": d.get("margin.return_reserve_pct", 0.0),
        "cs": d.get("margin.cs_cost_krw", 2000.0),
        "fx": d.get("margin.default_fx_rate", 1380.0),
        "ship_default": d.get("amazon_shipping_default_usd", 11.0),
    }


def _ensure_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(listings_pa)")}
    if "margin_status" not in cols:
        conn.execute("ALTER TABLE listings_pa ADD COLUMN margin_status TEXT")
        logger.info("  + listings_pa.margin_status 컬럼 추가")
    if "margin_checked_at" not in cols:
        conn.execute("ALTER TABLE listings_pa ADD COLUMN margin_checked_at TEXT")
        logger.info("  + listings_pa.margin_checked_at 컬럼 추가")


def main(apply, floor, top):
    conn = sqlite3.connect(DB, timeout=180)
    conn.execute("PRAGMA busy_timeout=180000")
    conn.row_factory = sqlite3.Row
    s = _load_settings(conn)
    logger.info(f"=== margin-monitor floor={floor}% apply={apply} | fee={s['fee']} fx={s['fx']} "
                f"forwarder={s['forwarder']} return={s['return_pct']}% cs={s['cs']} ship_def=${s['ship_default']} ===")

    rows = conn.execute(
        """SELECT l.id lid, p.asin, l.sale_krw, p.cost_usd, p.listing_price_usd, p.shipping_usd,
                  p.landed_price_usd, p.price_fetched_at, substr(COALESCE(p.title_ko,p.title_en),1,34) t
           FROM listings_pa l JOIN products p ON p.id=l.product_id
           WHERE l.channel='coupang' AND l.status='listed'"""
    ).fetchall()
    logger.info(f"listed 쿠팡: {len(rows):,}건 평가")

    updates = []          # (net_krw, net_pct, risk, status, checked_at, lid)
    cnt = {"OK": 0, "WARN": 0, "DANGER": 0, "STALE": 0, "SKIP": 0}
    dangers = []
    now = _now()
    for r in rows:
        sale = r["sale_krw"] or 0
        if sale <= 0:
            cnt["SKIP"] += 1
            continue
        # 현재원가 우선순위
        if r["listing_price_usd"] and r["listing_price_usd"] > 0:
            item, ship, stale = r["listing_price_usd"], (r["shipping_usd"] or 0), False
        elif r["landed_price_usd"] and r["landed_price_usd"] > 0:
            item, ship, stale = r["landed_price_usd"], 0.0, False   # landed=all-in
        elif r["cost_usd"] and r["cost_usd"] > 0:
            item, ship, stale = r["cost_usd"], 0.0, True             # 현재가 없음 → 리스팅 기준
        else:
            cnt["STALE"] += 1
            updates.append((None, None, None, "STALE", now, r["lid"]))
            continue

        cost_krw = item * s["fx"] + ship * s["fx"]
        seller_net = sale - cost_krw - sale * s["fee"] - s["forwarder"] - sale * (s["return_pct"] / 100.0) - s["cs"]
        margin_pct = seller_net / sale * 100.0

        if stale:
            status = "STALE"
        elif margin_pct <= 0:
            status = "DANGER"
        elif margin_pct < floor:
            status = "WARN"
        else:
            status = "OK"
        cnt[status] += 1
        risk = 1 if status in ("WARN", "DANGER") else 0
        updates.append((round(seller_net), round(margin_pct, 2), risk, status, now, r["lid"]))
        if status == "DANGER":
            dangers.append((margin_pct, r["asin"], sale, item, r["t"]))

    # ---- 리포트 ----
    ev = cnt["OK"] + cnt["WARN"] + cnt["DANGER"]
    logger.info(f"--- 결과: OK={cnt['OK']:,} WARN={cnt['WARN']:,} DANGER={cnt['DANGER']:,} "
                f"STALE(현재가없음)={cnt['STALE']:,} SKIP={cnt['SKIP']:,} ---")
    if ev:
        logger.info(f"  현재가 평가가능 {ev:,}건 중 위험(WARN+DANGER)={cnt['WARN']+cnt['DANGER']:,} "
                    f"({100*(cnt['WARN']+cnt['DANGER'])/ev:.1f}%)")
    dangers.sort()
    if dangers:
        logger.info(f"  ★ DANGER(손실) Top {min(top,len(dangers))}:")
        for mp, asin, sale, item, t in dangers[:top]:
            logger.info(f"    margin={mp:6.1f}% sale={int(sale):,}원 amzn=${item} {asin} | {t}")

    if not apply:
        logger.info("=== dry-run (--apply 로 기록) ===")
        conn.close()
        return

    _ensure_columns(conn)
    n = 0
    for i in range(0, len(updates), 200):
        chunk = updates[i:i + 200]
        conn.executemany(
            "UPDATE listings_pa SET net_margin_krw=?, net_margin_pct=?, margin_risk=COALESCE(?,margin_risk), "
            "margin_status=?, margin_checked_at=? WHERE id=?", chunk)
        conn.commit()
        n += len(chunk)
    logger.info(f"✓ 기록 완료: {n:,}건 (margin_status/checked_at/net_margin 갱신)")
    logger.info("=== 완료 ===")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--floor", type=float, default=10.0, help="WARN 임계 마진%% (기본 10)")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()
    main(args.apply, args.floor, args.top)
