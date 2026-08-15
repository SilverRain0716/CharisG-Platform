"""
sheet4_recovery.py — sheet 4 (자전거/낚시) 복구 + 쿠폰 추가.

상황: sheet 4 의 2,064 product가 promote만 되고 detailing/channelsending/upload 모두 누락.

실행:
  1) AI Stage 1+2 batch (run_two_stage_batch) — 1,081 (Stage 1 done) + 983 (Stage 0)
  2) ai_processed_at 설정된 product 대상 send_to_channels(pid, ['coupang']) → listings_pa pending
  3) coupang upload trigger (_run_coupang_upload_bg)
  4) 새로 listed된 product 마진대 분류 + 기존 5쿠폰에 vendorItemId 추가

기존 5쿠폰:
  91863069: PRICE 1000  / cap 10K   (margin 10-15K)
  91863079: RATE  5     / cap 10K   (margin 15-70K)
  91864546: RATE  5     / cap 20K   (margin 70-100K)
  91864571: RATE  5     / cap 30K   (margin 100-150K)
  91864581: RATE  5     / cap 50K   (margin 150K+)

사용:
  python3 -m backend.purchase.scripts.sheet4_recovery
"""
import asyncio
import logging
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

# AI service 가 backend_shared.context.get_db 의존 → db_factory 사전 등록 필수
from backend.purchase import database as _pa_db
from backend_shared.context import register_db_factory as _register_db_factory
_register_db_factory(_pa_db.get_db)

from backend.purchase.services.ai_processor import run_two_stage_batch
from backend.purchase.services.channel_listing_service import send_to_channels
from backend.purchase.services.coupang_service import (
    add_coupon_items,
    get_vendor_item_ids,
    wait_for_request,
)
from backend.purchase.routers.coupang import _run_coupang_upload_bg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# sheet 4 시점 정의
SHEET4_FROM = "2026-05-01 00:15:00"
SHEET4_UNTIL = "2026-05-01 02:11:00"

# 기존 5쿠폰 매핑 (margin_lo, margin_hi, coupon_id)
COUPON_MAP = [
    (10000, 15000, 91863069),
    (15000, 70000, 91863079),
    (70000, 100000, 91864546),
    (100000, 150000, 91864571),
    (150000, 10**12, 91864581),
]
CHUNK = 10_000


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _db():
    return sqlite3.connect(
        str(Path(__file__).resolve().parents[1] / "purchase.db")
    )


def get_sheet4_pids() -> list[int]:
    con = _db()
    rows = con.execute(
        "SELECT id FROM products WHERE created_at >= ? AND created_at < ? ORDER BY id",
        (SHEET4_FROM, SHEET4_UNTIL),
    ).fetchall()
    return [r[0] for r in rows]


async def step1_ai(pids: list[int]) -> str:
    """AI Stage 1+2 batch. job_id 반환."""
    job_id = uuid.uuid4().hex[:12]
    con = _db()
    con.execute(
        """INSERT INTO batch_jobs (id, job_type, status, total, created_at, phase_message)
           VALUES (?, 'ai_detail', 'running', ?, ?, ?)""",
        (job_id, len(pids), _now_iso(), "[recovery] sheet4 stage1+2"),
    )
    con.commit()
    con.close()
    logger.info(f"[step1] AI batch 시작 job_id={job_id} pids={len(pids)}")
    await run_two_stage_batch(job_id, pids, platform="coupang")
    logger.info(f"[step1] AI batch 완료")
    return job_id


def step2_channelsend(pids: list[int]) -> int:
    """send_to_channels(pid, ['coupang']) — listings_pa pending INSERT."""
    sent = 0
    fail = 0
    for i, pid in enumerate(pids, 1):
        try:
            send_to_channels(pid, ["coupang"])
            sent += 1
        except Exception as e:
            fail += 1
            if fail <= 10:
                logger.warning(f"  pid={pid} send_to_channels 실패: {e}")
        if i % 100 == 0:
            logger.info(f"  channelsend {i}/{len(pids)} ok={sent} fail={fail}")
    logger.info(f"[step2] channelsend 완료 ok={sent} fail={fail}")
    return sent


async def step3_upload(pids: list[int]) -> str:
    """coupang upload — _run_coupang_upload_bg."""
    # 실제 channelsending 결과 listings_pa pending 추출
    con = _db()
    rows = con.execute(
        f"SELECT product_id FROM listings_pa WHERE channel='coupang' AND status='pending' "
        f"AND product_id IN ({','.join('?' * len(pids))})",
        pids,
    ).fetchall()
    cu_pids = [r[0] for r in rows]
    con.close()

    if not cu_pids:
        logger.warning("[step3] upload 대상 없음")
        return ""

    job_id = uuid.uuid4().hex[:12]
    con = _db()
    con.execute(
        """INSERT INTO batch_jobs (id, job_type, status, total, created_at)
           VALUES (?, 'coupang_upload', 'pending', ?, ?)""",
        (job_id, len(cu_pids), _now_iso()),
    )
    con.commit()
    con.close()

    logger.info(f"[step3] coupang upload 시작 job_id={job_id} pids={len(cu_pids)}")
    await _run_coupang_upload_bg(job_id, cu_pids)
    logger.info(f"[step3] coupang upload 완료")
    return job_id


def step4_add_to_coupons(pids: list[int]) -> dict:
    """upload 완료 listed 의 sellerProductId → vendorItemId 추출 → 기존 쿠폰에 추가."""
    con = _db()
    con.row_factory = sqlite3.Row
    rows = con.execute(
        f"""SELECT id, product_id, channel_product_id, net_margin_krw
            FROM listings_pa
            WHERE channel='coupang' AND status='listed'
              AND channel_product_id IS NOT NULL AND channel_product_id != ''
              AND net_margin_krw IS NOT NULL
              AND product_id IN ({','.join('?' * len(pids))})""",
        pids,
    ).fetchall()
    listings = [dict(r) for r in rows]
    logger.info(f"[step4] 신규 listed: {len(listings)}건")

    # 마진대별 분류 + vendorItemId 추출
    by_coupon: dict[int, list] = {cid: [] for _, _, cid in COUPON_MAP}
    skip_low = 0  # 5K-10K, <5K 미적용
    for lst in listings:
        m = lst["net_margin_krw"]
        if m < 10000:
            skip_low += 1
            continue
        coupon_id = next((cid for lo, hi, cid in COUPON_MAP if lo <= m < hi), None)
        if not coupon_id:
            continue
        spid = str(lst["channel_product_id"])
        try:
            vids = get_vendor_item_ids(spid)
        except Exception as e:
            logger.warning(f"  vid extract 실패 spid={spid}: {e}")
            continue
        for vid in vids:
            by_coupon[coupon_id].append({
                "vid": int(vid), "spid": int(spid), "listing_id": lst["id"],
            })
        time.sleep(0.05)

    logger.info(f"[step4] skip 마진<10K: {skip_low}, 쿠폰별 vendor 수:")
    for cid, vs in by_coupon.items():
        logger.info(f"  coupon {cid}: {len(vs)}")

    # 각 쿠폰에 청크 단위로 add_coupon_items
    summary = {"added": 0, "failed": 0}
    for coupon_id, items in by_coupon.items():
        if not items:
            continue
        # coupons 테이블 local_id 찾기
        local_id = con.execute(
            "SELECT id FROM coupons WHERE coupon_id=?", (coupon_id,)
        ).fetchone()
        local_id = local_id[0] if local_id else None

        vid_list = [v["vid"] for v in items]
        for ci in range(0, len(vid_list), CHUNK):
            chunk = vid_list[ci : ci + CHUNK]
            ok, err, req_id = add_coupon_items(coupon_id, chunk)
            if not ok:
                logger.error(f"  coupon {coupon_id} chunk add 실패: {err}")
                summary["failed"] += len(chunk)
                continue
            res = wait_for_request(req_id, timeout=300, interval=3)
            if not res:
                logger.error(f"  coupon {coupon_id} polling timeout")
                summary["failed"] += len(chunk)
                continue
            succeeded = res.get("succeeded") or 0
            failed = res.get("failed") or 0
            summary["added"] += succeeded
            summary["failed"] += failed
            logger.info(f"  coupon {coupon_id} chunk {ci//CHUNK+1}: ok={succeeded} fail={failed}")

            # coupon_items INSERT
            failed_set = {(f.get("vendorItemId"), f.get("reason")) for f in (res.get("failedVendorItems") or [])}
            failed_vids = {fv[0] for fv in failed_set}
            for v in items[ci:ci+CHUNK]:
                if not local_id:
                    continue
                try:
                    if v["vid"] in failed_vids:
                        reason = next((rs for vid, rs in failed_set if vid == v["vid"]), "unknown")
                        con.execute(
                            """INSERT OR IGNORE INTO coupon_items
                               (coupon_local_id, coupon_id, vendor_item_id, seller_product_id, listing_id,
                                status, fail_reason, requested_id)
                               VALUES (?, ?, ?, ?, ?, 'failed', ?, ?)""",
                            (local_id, coupon_id, v["vid"], v["spid"], v["listing_id"],
                             reason[:200], req_id),
                        )
                    else:
                        con.execute(
                            """INSERT OR IGNORE INTO coupon_items
                               (coupon_local_id, coupon_id, vendor_item_id, seller_product_id, listing_id,
                                status, requested_id)
                               VALUES (?, ?, ?, ?, ?, 'applied', ?)""",
                            (local_id, coupon_id, v["vid"], v["spid"], v["listing_id"], req_id),
                        )
                except Exception:
                    pass
            con.commit()
            time.sleep(0.3)

        # coupons.items_added 업데이트
        if local_id:
            con.execute(
                "UPDATE coupons SET items_added=items_added+?, last_synced_at=CURRENT_TIMESTAMP WHERE id=?",
                (summary.get("added") - 0, local_id),  # 누적이라 정확하진 않지만 OK
            )
            con.commit()

    con.close()
    return summary


async def main():
    logger.info("=== sheet 4 recovery 시작 ===")
    pids = get_sheet4_pids()
    logger.info(f"sheet 4 pids: {len(pids)}")
    if not pids:
        logger.error("sheet 4 product 없음 — abort")
        return

    # ── Step 1: AI batch
    await step1_ai(pids)

    # ── 검증: ai_processed_at 채워진 product
    con = _db()
    n_ai = con.execute(
        f"SELECT COUNT(*) FROM products WHERE id IN ({','.join('?'*len(pids))}) AND ai_processed_at IS NOT NULL",
        pids,
    ).fetchone()[0]
    con.close()
    logger.info(f"AI 처리 완료된 product: {n_ai}/{len(pids)}")
    if n_ai == 0:
        logger.error("AI 완료 0건 — channelsend 불가, abort")
        return

    # ── Step 2: channelsending
    step2_channelsend(pids)

    # ── Step 3: coupang upload
    await step3_upload(pids)

    # ── Step 4: 쿠폰 추가
    summary = step4_add_to_coupons(pids)
    logger.info(f"=== sheet 4 recovery 완료: 쿠폰 추가성공 {summary['added']}, 실패 {summary['failed']} ===")


if __name__ == "__main__":
    asyncio.run(main())
