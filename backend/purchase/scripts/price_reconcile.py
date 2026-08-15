"""price_reconcile.py — 정가(sale_krw) 재계산 정기 잡.

문제: refresh-landed 는 landed 만 갱신하고, fill_discount 는 정가 재계산 없이 할인 50%까지만 적용.
      → 실가가 정가보다 50%+ 싼 listing 은 정가가 영영 안 고쳐짐(stale ap / 가격하락 미반영).

동작:
  1. 후보 = listed coupang 중  sale_krw / calc(현재 landed) >= RATIO_MIN  (정가가 올바른값의 N배+)
  2. flash-sale 방지 = amazon_price_snapshots 최근 STAB_DAYS 의 **max(landed)** 로 재검.
     max_landed(=최근 최고가) 기준으로도 ratio>=RATIO_MIN 이면 → "지속적으로 쌌다" = stale 확정.
     (최근에 비쌌던 적 있으면 max 가 높아 ratio 떨어짐 → 일시할인으로 보고 SKIP)
  3. 정가 = calc(max_landed) 로 재산정(보수적 = 최고가 기준) → 쿠팡 정가+판매가 동반 50%씩 인하 PUT.
  4. DB: sale_krw=재산정, amazon_price_usd=max_landed, discount_krw=NULL, price_reconciled_at=now.

가격변경(저위험)만 — 콘텐츠 수정 없음. idle 대기로 락 충돌 회피. --apply 없으면 dry-run.
"""
import argparse
import math
import time
import logging

from dotenv import load_dotenv
import os
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))

from backend.purchase.scripts.fill_discount_krw import (
    _calc_sale_price, _load_settings, _db_query, _db_exec, _wait_for_idle,
    _other_job_active, _now,
)
from backend.purchase.services import coupang_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("price_reconcile")

SLEEP_API = 0.7
RATIO_MIN = 2.0       # 정가가 올바른값의 2배+
STAB_DAYS = 7         # 안정성 look-back
MIN_SNAPS = 3         # 최소 스냅샷 수


def _ensure_column():
    try:
        _db_exec("ALTER TABLE listings_pa ADD COLUMN price_reconciled_at TEXT")
        logger.info("listings_pa.price_reconciled_at 컬럼 추가")
    except Exception:
        pass  # 이미 있음


def _max_recent_landed(asin, days):
    rows = _db_query(
        "SELECT landed_price_usd lp FROM amazon_price_snapshots "
        "WHERE asin=? AND fetched_at >= datetime('now', ?) AND landed_price_usd > 0",
        (asin, f"-{days} days"),
    )
    vals = [r["lp"] for r in rows if r["lp"]]
    return (max(vals), len(vals)) if vals else (None, 0)


def _reprice(cpid, target, expected=None):
    """정가+판매가 동반 50%씩 인하 (89%벽 회피). 검증된 패턴."""
    vids = coupang_service.get_vendor_item_ids(str(cpid))
    if not vids:
        return False, "no_vid"
    d = coupang_service.get_seller_product(str(cpid)) or {}
    its = (d.get("data") or {}).get("items") or []
    ps = [it.get("salePrice") for it in its if it.get("salePrice") is not None]
    cur = max(ps) if ps else None
    if not cur:
        return False, "no_live"
    # ★수동편집 감지 — 라이브가가 우리 기준값과 다르면 WING에서 수정한 것 → manual 잠금
    if expected and expected > 0 and abs(cur - expected) > max(500, expected * 0.01):
        return "MANUAL", f"live={cur} != expected={expected}"
    if cur <= target:
        return True, "already_low"
    for vid in vids:
        coupang_service.update_vendor_item_original_price(vid, cur); time.sleep(SLEEP_API)
        price = cur
        while price > target:
            nxt = max(target, math.ceil(price * 0.5 / 100) * 100)
            ok, m = coupang_service.update_vendor_item_price(vid, nxt); time.sleep(SLEEP_API)
            if not ok:
                return False, f"sale:{m[:70]}"
            ok, m = coupang_service.update_vendor_item_original_price(vid, nxt); time.sleep(SLEEP_API)
            if not ok:
                return False, f"orig:{m[:70]}"
            price = nxt
    return True, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--ratio-min", type=float, default=RATIO_MIN)
    ap.add_argument("--days", type=int, default=STAB_DAYS)
    args = ap.parse_args()

    s = _load_settings()
    _ensure_column()
    if args.apply:
        logger.info("=== idle 대기 ===")
        _wait_for_idle()

    rows = _db_query(
        "SELECT lp.id lid, lp.channel_product_id cpid, lp.sale_krw sk, lp.discount_krw dk, lp.has_options ho, "
        "p.id pid, p.asin, p.landed_price_usd landed, substr(coalesce(p.title_ko,p.title_en),1,30) t "
        "FROM listings_pa lp JOIN products p ON p.id=lp.product_id "
        "WHERE lp.channel='coupang' AND lp.status='listed' AND COALESCE(lp.price_mode,'auto')<>'manual' "
        "AND lp.channel_product_id IS NOT NULL AND lp.channel_product_id!='' "
        "AND lp.sale_krw > 0 AND p.landed_price_usd > 0 "
        "ORDER BY lp.sale_krw DESC"
    )
    cand = []
    for r in rows:
        cor = _calc_sale_price(r["landed"], "coupang", s)
        if cor and r["sk"] / cor >= args.ratio_min:
            cand.append(r)
    logger.info(f"1차 후보(현재 landed 기준 ratio>={args.ratio_min}): {len(cand):,}")

    stale = []      # (r, target, max_landed)
    skip_flash = skip_nohist = 0
    for r in cand:
        max_landed, nsnap = _max_recent_landed(r["asin"], args.days)
        if nsnap < MIN_SNAPS or max_landed is None:
            skip_nohist += 1
            continue
        cor_max = _calc_sale_price(max_landed, "coupang", s)
        if not cor_max or r["sk"] / cor_max < args.ratio_min:
            skip_flash += 1          # 최근 비쌌던 적 있음 = 일시할인 의심 → 유지
            continue
        stale.append((r, cor_max, max_landed))

    stale.sort(key=lambda x: -(x[0]["sk"] / x[1]))  # ratio 큰 순
    logger.info(f"stale 확정: {len(stale):,} | flash/회복 제외: {skip_flash:,} | 히스토리부족 제외: {skip_nohist:,}")
    if args.limit and len(stale) > args.limit:
        logger.info(f"--limit {args.limit} 적용 (ratio 큰 것 우선)")
        stale = stale[:args.limit]

    okc = errc = 0
    for i, (r, target, ml) in enumerate(stale, 1):
        ratio = r["sk"] / target
        if not args.apply:
            if i <= 25 or i % 100 == 0:
                logger.info(f"[DRY] {r['asin']} 정가{int(r['sk']):,}→{target:,} (r{ratio:.1f}, max_landed7d=${ml}) {r['t']}")
            okc += 1
            continue
        expected = (r["dk"] if r["dk"] else r["sk"]) if not r["ho"] else None  # 그룹은 감지 제외(오탐방지)
        ok, info = _reprice(r["cpid"], target, expected)
        if ok == "MANUAL":
            _db_exec("UPDATE listings_pa SET price_mode='manual' WHERE id=?", (r["lid"],))
            if i <= 30:
                logger.info(f"[{i}/{len(stale)}] 🔒 수동편집 감지 → 가격고정 {r['asin']} ({info})")
            continue
        if ok:
            _db_exec("UPDATE listings_pa SET sale_krw=?, discount_krw=NULL, price_reconciled_at=?, discount_synced_at=? WHERE id=?",
                     (target, _now(), _now(), r["lid"]))
            _db_exec("UPDATE products SET amazon_price_usd=?, cost_usd=? WHERE id=?", (ml, ml, r["pid"]))
            okc += 1
            if okc <= 30 or okc % 50 == 0:
                logger.info(f"[{i}/{len(stale)}] ✅ {r['asin']} →{target:,} (r{ratio:.0f})")
        else:
            errc += 1
            if errc <= 15:
                logger.info(f"[{i}/{len(stale)}] ❌ {r['asin']} {info}")
        if i % 100 == 0 and _other_job_active():
            logger.info("다른 잡 활성 — idle 대기"); _wait_for_idle()

    logger.info(f"=== {'APPLIED' if args.apply else 'DRY-RUN'} 완료: 처리 {okc} / 실패 {errc} (stale 총 {len(stale)}) ===")


if __name__ == "__main__":
    main()
