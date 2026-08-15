"""naver_price_reconcile.py — 네이버 스마트스토어 정가 양방향 reconcile.

문제: 네이버 listed 에 적자(원가↑ 미반영)·과소(목표마진 미달)·과대(stale-ap) 혼재.
동작:
  target_cost = 최근 7일 max(landed)  (스냅샷 부족하면 현재 landed, 없으면 amazon)
  target      = _calc_sale_price(target_cost, 'smartstore')   (35% 마진 공식)
  - sale < target*0.95  → 인상 (적자/과소 교정)
  - sale > target*RAISE_OVER (기본2.0) → 인하 (과대 stale-ap, 단 스냅샷 있을 때만 = flash-sale 방지)
  - 그 사이 → 유지
  네이버 update_product 는 가격변경 한도 없음 → 한 번에 PUT. salePrice 만 병합.
가격변경(저위험)만. --apply 없으면 dry-run.
"""
import argparse, time, logging, os
from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))

from backend.purchase.scripts.fill_discount_krw import _calc_sale_price, _load_settings, _db_query, _db_exec, _now
from backend.purchase.services import naver_commerce_service as nv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("naver_reconcile")

SLEEP = 0.5
STAB_DAYS = 7
MIN_SNAPS = 3
RAISE_OVER = 2.0      # sale > target*2 → 인하 (과대)
UNDER = 0.95          # sale < target*0.95 → 인상


def _max_recent_landed(asin, days):
    rows = _db_query(
        "SELECT landed_price_usd lp FROM amazon_price_snapshots "
        "WHERE asin=? AND fetched_at >= datetime('now', ?) AND landed_price_usd>0",
        (asin, f"-{days} days"),
    )
    vals = [r["lp"] for r in rows if r["lp"]]
    return (max(vals), len(vals)) if vals else (None, 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    s = _load_settings()

    rows = _db_query(
        "SELECT l.id lid, l.channel_product_id cpid, l.sale_krw sk, p.id pid, p.asin, "
        "p.amazon_price_usd ap, p.landed_price_usd lp, substr(coalesce(p.title_ko,p.title_en),1,30) t "
        "FROM listings_pa l JOIN products p ON p.id=l.product_id "
        "WHERE l.channel='smartstore' AND l.status='listed' AND COALESCE(l.price_mode,'auto')<>'manual' "
        "AND l.channel_product_id IS NOT NULL AND l.channel_product_id!='' AND l.sale_krw>0"
    )
    raise_jobs, lower_jobs, skip_flash = [], [], 0
    for r in rows:
        ml, nsnap = _max_recent_landed(r["asin"], STAB_DAYS)
        cost = ml if (ml and nsnap >= MIN_SNAPS) else (r["lp"] or r["ap"])
        has_hist = bool(ml and nsnap >= MIN_SNAPS)
        if not cost or cost <= 0:
            continue
        target = _calc_sale_price(cost, "smartstore", s)
        if not target:
            continue
        if r["sk"] < target * UNDER:
            raise_jobs.append((r, target, cost))
        elif r["sk"] > target * RAISE_OVER:
            if has_hist:
                lower_jobs.append((r, target, cost))
            else:
                skip_flash += 1   # 과대지만 히스토리 부족 → 보수적 유지

    raise_jobs.sort(key=lambda x: x[0]["sk"] / x[1])           # 가장 과소(적자)부터
    lower_jobs.sort(key=lambda x: -(x[0]["sk"] / x[1]))        # 가장 과대부터
    logger.info(f"인상(적자/과소): {len(raise_jobs):,} | 인하(과대): {len(lower_jobs):,} | 히스토리부족 과대 보류: {skip_flash:,}")

    jobs = [("UP", j) for j in raise_jobs] + [("DOWN", j) for j in lower_jobs]
    if args.limit:
        jobs = jobs[:args.limit]

    okc = errc = 0
    for i, (dir_, (r, target, cost)) in enumerate(jobs, 1):
        if not args.apply:
            if i <= 30 or i % 200 == 0:
                logger.info(f"[DRY {dir_}] {r['asin']} {int(r['sk']):,}→{target:,} (cost=${cost}) {r['t']}")
            okc += 1
            continue
        try:
            res = nv.update_product(str(r["cpid"]), {"originProduct": {"salePrice": int(target)}})
        except Exception as e:
            res = None; logger.warning(f"{r['asin']} 예외 {e}")
        time.sleep(SLEEP)
        if res:
            _db_exec("UPDATE listings_pa SET sale_krw=?, discount_synced_at=? WHERE id=?", (target, _now(), r["lid"]))
            # cost_usd 도 실가로 교정 — 재리스팅 시 floor 가격으로 clobber 되는 것 방지
            _db_exec("UPDATE products SET amazon_price_usd=?, cost_usd=? WHERE id=?", (cost, cost, r["pid"]))
            okc += 1
            if okc <= 30 or okc % 50 == 0:
                logger.info(f"[{i}/{len(jobs)}] ✅ {dir_} {r['asin']} →{target:,}")
        else:
            errc += 1
            if errc <= 15:
                logger.info(f"[{i}/{len(jobs)}] ❌ {r['asin']} update 실패")

    logger.info(f"=== {'APPLIED' if args.apply else 'DRY-RUN'}: 처리 {okc} / 실패 {errc} (대상 {len(jobs)}) ===")


if __name__ == "__main__":
    main()
