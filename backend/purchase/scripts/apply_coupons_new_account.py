"""신규 쿠팡 계정(카리스글로벌 A01731680) 마진대별 즉시할인쿠폰 자동 적용.

배경
  기존 apply_coupon_catchup 은 listings_pa(=구계정 spid 77K건)를 읽어 적용하므로
  신규계정엔 못 씀 — 신규계정 상품은 listings_pa 에 신규 spid 가 기록되지 않는다
  (/tmp/upload_one.py 는 DB write-back 을 안 함). 이 스크립트는 신규계정
  seller-products API 를 직접 전수 조회하고, item.externalVendorSku(='PA-{pid}')
  로 product 를 매핑해 마진대(_margin_band)를 산정한 뒤 contract 355546 의 7쿠폰에
  add_coupon_items 한다 (coupon_items UNIQUE 로 dedup).

  쿠폰 기간이 2999-12-31 까지라 월간 재발행이 불필요 — 신규 상품을 더 올린 뒤
  이 스크립트만 재실행하면 새 상품이 해당 밴드 쿠폰에 자동 편입된다.

실행 (반드시 신규계정 env):
  COUPANG_ACTIVE=new PYTHONPATH=<repo> .venv/bin/python \
      -m backend.purchase.scripts.apply_coupons_new_account --dry-run
  COUPANG_ACTIVE=new PYTHONPATH=<repo> .venv/bin/python \
      -m backend.purchase.scripts.apply_coupons_new_account --live
"""
import argparse
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

# ★ 안전장치 — 신규계정에서만 (구계정 vendorItem 에 신규쿠폰 잘못 붙는 사고 방지)
if os.environ.get("COUPANG_ACTIVE", "").strip().lower() != "new":
    raise SystemExit("이 스크립트는 COUPANG_ACTIVE=new 에서만 실행하세요 (신규계정 전용).")

from backend.purchase.database import get_db
from backend.purchase.services import coupang_service as S
from backend.purchase.services.coupon_apply import _margin_band
from backend.purchase.services.pricing_service_pa import calculate_sale_krw
from backend.purchase.services.forwarder_shipping import forwarder_shipping_usd
from backend.purchase.services.channel_listing_service import _load_default_forwarder_extras

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("apply_coupons_new")

CONTRACT_NEW = 355546          # 신규계정 즉시할인쿠폰 계약 (WING 예산 → 2999 까지)
SKU_RE = re.compile(r"PA-(\d+)")
# externalVendorSku 없는 수동 등록분 보정 (자격활성화용 첫 상품 = 다이빙게임 pid 97244)
SPID_PID_OVERRIDE = {"16266318826": 97244}
CHUNK = 10_000
_KST = timezone(timedelta(hours=9))


def _now_kst() -> str:
    return datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S")


def resolve_new_coupon(conn, band: str):
    """band 의 유효 쿠폰 (반드시 contract 355546 = 신규계정) → (coupons.id, wing couponId).

    start_at 미래(발효 전)여도 아이템 추가는 가능하므로 start_at 조건은 두지 않는다
    (할인은 start_at 도달 시 자동 발효). 만료(end_at<now)만 제외.
    """
    now = _now_kst()
    row = conn.execute(
        """SELECT id, coupon_id FROM coupons
           WHERE margin_band = ? AND contract_id = ? AND coupon_id IS NOT NULL
             AND status = 'active' AND end_at >= ?
           ORDER BY created_at DESC LIMIT 1""",
        (band, CONTRACT_NEW, now),
    ).fetchone()
    return (row["id"], row["coupon_id"]) if row else None


def compute_margin(conn, pid: int, sale_price: float):
    """(margin_krw, source). listings_pa 우선 → 없으면 표준 pricing 재계산(실판매가 보정)."""
    r = conn.execute(
        "SELECT net_margin_krw FROM listings_pa "
        "WHERE channel='coupang' AND product_id=? AND net_margin_krw IS NOT NULL LIMIT 1",
        (pid,),
    ).fetchone()
    if r:
        return float(r["net_margin_krw"]), "stored"
    p = conn.execute("SELECT cost_usd, weight_g FROM products WHERE id=?", (pid,)).fetchone()
    if not p or p["cost_usd"] is None:
        return None, "no_cost"
    extras = _load_default_forwarder_extras()
    fw = forwarder_shipping_usd(p["weight_g"])
    pr = calculate_sale_krw(
        cost_usd=float(p["cost_usd"]), amazon_shipping_usd=0.0, cj_shipping_usd=fw,
        channel="coupang", safety_margin_krw=extras["safety_krw"],
        cs_cost_krw=extras["cs_krw"], return_reserve_pct=extras["return_pct"],
    )
    # 모델 sale_krw 와 실제 salePrice 차이를 fee 제외분으로 보정
    margin = pr["net_margin_krw"] + (float(sale_price) - pr["sale_krw"]) * (1 - pr["fee_rate"])
    return float(margin), "recompute"


def collect_plan(sub_products):
    """주어진 승인완료 상품 리스트 → band->[{vid,spid,pid,margin,sale,src}], skipped.
    (list_all/done_spids/시간예산은 main 관리 — 여긴 서브배치만 조회/매핑)"""
    plan: dict[str, list[dict]] = {}
    skipped: list[tuple] = []
    with get_db() as conn:
        for p in sub_products:
            spid = str(p.get("sellerProductId"))
            d = (S.get_seller_product(spid) or {}).get("data") or {}
            for it in (d.get("items") or []):
                vid = it.get("vendorItemId")
                sale = it.get("salePrice") or 0
                sku = it.get("externalVendorSku") or ""
                m = SKU_RE.match(sku)
                if m:
                    pid = int(m.group(1))
                elif spid in SPID_PID_OVERRIDE:
                    pid = SPID_PID_OVERRIDE[spid]
                elif sku:
                    r2 = conn.execute(
                        "SELECT id FROM products WHERE asin=? ORDER BY id LIMIT 1", (sku,)
                    ).fetchone()
                    pid = r2["id"] if r2 else None
                else:
                    pid = None
                if not vid:
                    skipped.append((spid, vid, "no_vid")); continue
                if not pid:
                    skipped.append((spid, vid, f"no_pid(sku={sku!r})")); continue
                margin, src2 = compute_margin(conn, pid, sale)
                if margin is None:
                    skipped.append((spid, vid, "no_margin")); continue
                band = _margin_band(margin)
                if not band:
                    skipped.append((spid, vid, f"margin<10k({int(margin)})")); continue
                plan.setdefault(band, []).append({
                    "vid": int(vid), "spid": spid, "pid": pid,
                    "margin": int(margin), "sale": sale, "src": src2,
                })
            time.sleep(0.15)
    return plan, skipped


BANDS_ORDER = ["10-15K", "15-50K", "50-70K", "70-100K", "100-150K", "150-200K", "200K+"]


def apply_plan(plan, dry=False):
    """plan 적용 — band별 fresh(coupon_items 미존재)만 add_coupon_items+INSERT. 반환 (added, fail)."""
    added = fail = 0
    for band in BANDS_ORDER:
        items = plan.get(band) or []
        if not items:
            continue
        with get_db() as conn:
            resolved = resolve_new_coupon(conn, band)
            existing = set()
            if resolved:
                existing = {r["vendor_item_id"] for r in conn.execute(
                    "SELECT vendor_item_id FROM coupon_items WHERE coupon_local_id=?",
                    (resolved[0],)).fetchall()}
        if not resolved:
            log.warning(f"    band {band} 활성쿠폰(contract 355546) 없음 — skip")
            continue
        local_id, wing = resolved
        fresh = [it for it in items if it["vid"] not in existing]
        if not fresh:
            continue
        if dry:
            log.info(f"  [{band:>9}] coupon={wing} 신규={len(fresh)} 마진예:{[it['margin'] for it in fresh[:3]]}")
            continue
        vids = [it["vid"] for it in fresh]
        for i in range(0, len(vids), CHUNK):
            chunk = vids[i:i + CHUNK]
            ok, msg, req = S.add_coupon_items(wing, chunk)
            if ok and req:
                try:
                    S.wait_for_request(req, timeout=600)
                except Exception:
                    log.exception("wait_for_request 실패")
            st = "applied" if ok else "failed"
            with get_db() as conn:
                for it in fresh[i:i + CHUNK]:
                    conn.execute(
                        """INSERT OR IGNORE INTO coupon_items
                           (coupon_local_id, coupon_id, vendor_item_id, seller_product_id,
                            status, requested_id, chunk_index)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (local_id, wing, it["vid"], int(it["spid"]), st, req, i // CHUNK),
                    )
            if ok:
                added += len(chunk)
            else:
                fail += len(chunk)
                log.info(f"  [{band}] chunk ok={ok} msg={(msg or '')[:80]}")
            time.sleep(0.3)
    return added, fail


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--live", action="store_true")
    args = ap.parse_args()

    products = S.list_all_seller_products()
    approved = [p for p in products if p.get("statusName") == "승인완료"]
    with get_db() as conn:
        done_spids = {str(r[0]) for r in conn.execute(
            "SELECT DISTINCT ci.seller_product_id FROM coupon_items ci "
            "JOIN coupons c ON c.id = ci.coupon_local_id "
            "WHERE c.contract_id = ? AND ci.seller_product_id IS NOT NULL", (CONTRACT_NEW,))}
    todo = [p for p in approved if str(p.get("sellerProductId")) not in done_spids]
    budget_min = float(os.environ.get("COUPON_MAX_MIN", "50"))
    SUB = int(os.environ.get("COUPON_SUB", "200"))
    log.info(f"신규계정 {len(products)} (승인완료 {len(approved)}) / 이미적용 {len(done_spids)} / "
             f"미적용 {len(todo)} — 시간예산 {budget_min}분, 서브배치 {SUB}")

    if args.dry_run:
        plan, skipped = collect_plan(todo[:SUB])
        apply_plan(plan, dry=True)
        log.info(f"--- DRY-RUN (샘플 {min(SUB,len(todo))}개, skip {len(skipped)}) ---")
        return

    t0 = time.monotonic()
    total_added = total_fail = processed = 0
    for i in range(0, len(todo), SUB):
        elapsed = (time.monotonic() - t0) / 60.0
        if elapsed >= budget_min:
            log.info(f"[시간예산 {budget_min}분 도달] 종료 — 처리 {processed}/{len(todo)}, 나머지 다음 실행")
            break
        plan, skipped = collect_plan(todo[i:i + SUB])
        added, fail = apply_plan(plan)
        total_added += added; total_fail += fail; processed += len(todo[i:i + SUB])
        log.info(f"  진행 {processed}/{len(todo)}  누적 +{total_added}쿠폰(fail {total_fail})  경과 {elapsed:.1f}분")

    log.info(f"=== 완료 — 추가 {total_added} / 실패 {total_fail} / 처리 {processed} ===")


if __name__ == "__main__":
    main()
