"""쿠팡 마진대별 쿠폰 자동 적용 — 재사용 가능 함수.

호출처:
  1. sheet_queue_worker (시트 처리 끝에서 신규 listed 자동 적용)
  2. backend/purchase/scripts/apply_coupon_catchup.py (catch-up 잡)

마진대 7단계 (2026-05-14 정책 갱신):
  10-15K   → PRICE 1,000원
  15-50K   → RATE 5%
  50-70K   → PRICE 5,000원
  70-100K  → PRICE 10,000원
  100-150K → PRICE 20,000원
  150-200K → PRICE 30,000원
  200K+    → PRICE 50,000원

마진 < 10K 는 적용 대상 아님 (정책).
쿠폰 ID 매핑은 coupons 테이블의 margin_band + 활성기간 동적 조회 (매월 갱신 무인 운영).
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from backend.purchase.database import get_db
# 2026-05-12 fix: module-level import (이전 function-body lazy import 시 함수 호출에서
# get_vendor_item_ids 가 빈 리스트 반환하는 버그 — 원인 불명이지만 module-level 로
# 옮기면 module-level 패턴인 /tmp/catchup_v2.py 와 동일하게 정상 동작).
from backend.purchase.services.coupang_service import (
    add_coupon_items,
    get_vendor_item_ids,
    wait_for_request,
)

logger = logging.getLogger(__name__)

CHUNK_SIZE = 10_000  # add_coupon_items 1회 한도
VID_FETCH_SLEEP = 1.0  # WAF 회피 + rate (2026-06-01 0.3→1.0 분당 60)

_KST = timezone(timedelta(hours=9))


def _now_kst_str() -> str:
    return datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S")


def _margin_band(margin_krw: Optional[float]) -> Optional[str]:
    if margin_krw is None or margin_krw < 10000:
        return None
    if margin_krw < 15000:
        return "10-15K"
    if margin_krw < 50000:
        return "15-50K"
    if margin_krw < 70000:
        return "50-70K"
    if margin_krw < 100000:
        return "70-100K"
    if margin_krw < 150000:
        return "100-150K"
    if margin_krw < 200000:
        return "150-200K"
    return "200K+"


def _resolve_active_coupon(conn, band: str) -> Optional[tuple[int, int]]:
    """현재 KST 활성 + 해당 margin_band 쿠폰의 (coupons.id, wing coupon_id) 반환.

    매월 갱신 시점 별 코드 수정 없이 자동으로 새 쿠폰 사용.
    """
    now = _now_kst_str()
    row = conn.execute(
        """SELECT id, coupon_id FROM coupons
           WHERE margin_band = ? AND coupon_id IS NOT NULL
             AND start_at <= ? AND end_at >= ?
             AND status = 'active'
           ORDER BY created_at DESC LIMIT 1""",
        (band, now, now),
    ).fetchone()
    if not row:
        return None
    return row["id"], row["coupon_id"]


def apply_coupons_to_listings(listing_ids: Optional[Iterable[int]] = None) -> dict:
    """미적용 쿠팡 listed listings 에 마진대별 쿠폰 자동 추가.

    Args:
        listing_ids: 명시된 listing 만 처리. None 이면 미적용 listed 전체.

    Returns:
        {processed, by_band: {band: {targets, vids, added, failed}}}
    """
    # 1) 대상 listing 추출 (현재 활성 쿠폰에 미적용 + 마진 ≥10K + 채널 active)
    # 2026-05-12 fix: coupang_status_name 이 '상품삭제'/'임시저장'/'심사중'/'승인반려'/'승인대기중'
    # 인 listings 는 vendor_item 없거나 비활성 → 쿠폰 적용 불가. '승인완료' 또는
    # statusName sync 안 된 (NULL/빈값) 만 시도.
    # 2026-05-14: "활성 쿠폰 어디에도 안 들어간 listing" 으로 변경. 만료 쿠폰 가입 이력은
    # 무관하게 매월 갱신 시 새 쿠폰에 자동 가입.
    now_kst = _now_kst_str()
    sql = """SELECT l.id AS lid, l.channel_product_id AS spid, l.net_margin_krw AS margin
               FROM listings_pa l
              WHERE l.channel='coupang' AND l.status='listed'
                AND l.net_margin_krw IS NOT NULL AND l.net_margin_krw >= 10000
                AND l.channel_product_id IS NOT NULL AND l.channel_product_id != ''
                AND (l.coupang_status_name = '승인완료'
                     OR l.coupang_status_name IS NULL
                     OR l.coupang_status_name = '')
                AND l.id NOT IN (
                    SELECT DISTINCT ci.listing_id FROM coupon_items ci
                    JOIN coupons c ON c.coupon_id = ci.coupon_id
                    WHERE ci.listing_id IS NOT NULL
                      AND c.status = 'active'
                      AND c.start_at <= ? AND c.end_at >= ?
                )"""
    params: list = [now_kst, now_kst]
    if listing_ids:
        ids = list(listing_ids)
        if not ids:
            return {"processed": 0, "by_band": {}}
        ph = ",".join("?" * len(ids))
        sql += f" AND l.id IN ({ph})"
        params.extend(ids)
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    logger.info(f"[coupon-apply] 미적용 대상: {len(rows)}건")
    PER_RUN_CAP = 10000  # 2026-06-05 per-run 상한 — 락 장기점유 방지 (미적용분이라 다음 실행이 이어받음)
    if len(rows) > PER_RUN_CAP:
        logger.info(f"[coupon-apply] per-run 상한 {PER_RUN_CAP:,} 적용 — 이번 {PER_RUN_CAP:,}건, 나머지 {len(rows) - PER_RUN_CAP:,}건 다음 실행")
        rows = rows[:PER_RUN_CAP]
    if not rows:
        return {"processed": 0, "by_band": {}}

    # 2) 마진대별 분류
    bands: dict[str, list[dict]] = {}
    for r in rows:
        b = _margin_band(r["margin"])
        if b:
            bands.setdefault(b, []).append(r)
    for b, lst in bands.items():
        logger.info(f"[coupon-apply]   {b}: {len(lst)}건")

    result_by_band: dict[str, dict] = {}

    # 3) 마진대별 처리 — 현재 활성 쿠폰 동적 조회
    for band, lst in bands.items():
        with get_db() as conn:
            resolved = _resolve_active_coupon(conn, band)
        if not resolved:
            logger.warning(
                f"[coupon-apply] band={band} 활성 쿠폰 없음 — skip "
                f"(매월 1일 신규 발급 timer 확인 필요)"
            )
            continue
        coupon_local, wing_id = resolved
        logger.info(
            f"[coupon-apply] === {band} 처리 (coupon_local={coupon_local} wing={wing_id} "
            f"{len(lst)}건) ==="
        )

        # 4-1) vendor_item_id 추출
        # 2026-06-08 최적화: DB(listing_options.channel_option_id) 우선 → API+sleep 회피.
        #   기존엔 listing 마다 쿠팡 API(get_vendor_item_ids)+1초 sleep = 시간당 ~30건(병목).
        #   옵션id sync 잡이 channel_option_id 를 채워둠 → 대부분 DB 캐시로 즉시 처리.
        all_vids: list[tuple[int, int, int]] = []  # (vid, listing_id, spid)
        _lids = [r["lid"] for r in lst]
        _opt_cache: dict[int, list[int]] = {}
        if _lids:
            with get_db() as _conn:
                _ph = ",".join("?" * len(_lids))
                for _row in _conn.execute(
                    f"SELECT listing_id, channel_option_id FROM listing_options "
                    f"WHERE listing_id IN ({_ph}) AND channel_option_id IS NOT NULL",
                    _lids,
                ).fetchall():
                    try:
                        _opt_cache.setdefault(_row["listing_id"], []).append(
                            int(_row["channel_option_id"])
                        )
                    except (TypeError, ValueError):
                        pass
        _cached_n = _api_n = 0
        for idx, r in enumerate(lst, 1):
            spid = r["spid"]
            cached = _opt_cache.get(r["lid"])
            if cached:
                for vid in cached:
                    try:
                        all_vids.append((vid, r["lid"], int(spid)))
                    except (TypeError, ValueError):
                        pass
                _cached_n += 1
                continue   # DB 캐시 사용 — 쿠팡 API + sleep 스킵
            # 캐시 없음 → 쿠팡 API fallback (sleep 유지)
            try:
                vids = get_vendor_item_ids(spid)
            except Exception as e:
                logger.warning(f"[coupon-apply]   vid fail spid={spid}: {e}")
                continue
            for vid in vids:
                try:
                    all_vids.append((int(vid), r["lid"], int(spid)))
                except (TypeError, ValueError) as e:
                    logger.warning(
                        f"[coupon-apply]   vid 변환 실패 vid={vid!r} spid={spid!r}: {e}"
                    )
            _api_n += 1
            time.sleep(VID_FETCH_SLEEP)
        logger.info(
            f"[coupon-apply]   {band} 옵션id: DB캐시 {_cached_n}건 / API fallback {_api_n}건"
        )

        logger.info(f"[coupon-apply]   {band} vid 총 {len(all_vids)}건")

        # 4-2) 청크 add_coupon_items + coupon_items INSERT
        added = failed = 0
        for i in range(0, len(all_vids), CHUNK_SIZE):
            chunk = all_vids[i : i + CHUNK_SIZE]
            vids_only = [v[0] for v in chunk]
            ok, msg, req_id = add_coupon_items(wing_id, vids_only)
            chunk_status = "applied" if ok else "failed"
            logger.info(
                f"[coupon-apply]   chunk {i // CHUNK_SIZE + 1}: "
                f"ok={ok} req_id={req_id} msg={msg[:120] if msg else ''}"
            )
            if ok and req_id:
                try:
                    wait_for_request(req_id, timeout=600)
                except Exception:
                    logger.exception(
                        f"[coupon-apply]   wait_for_request fail req_id={req_id}"
                    )

            with get_db() as conn:
                for vid, lid, spid in chunk:
                    try:
                        conn.execute(
                            """INSERT OR IGNORE INTO coupon_items
                                   (coupon_local_id, coupon_id, vendor_item_id,
                                    seller_product_id, listing_id, status,
                                    requested_id, chunk_index)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                coupon_local,
                                wing_id,
                                vid,
                                spid,
                                lid,
                                chunk_status,
                                req_id,
                                i // CHUNK_SIZE,
                            ),
                        )
                        if ok:
                            added += 1
                        else:
                            failed += 1
                    except Exception:
                        logger.exception(
                            f"[coupon-apply]   coupon_items INSERT fail vid={vid}"
                        )

        result_by_band[band] = {
            "targets": len(lst),
            "vids": len(all_vids),
            "added": added,
            "failed": failed,
        }
        logger.info(
            f"[coupon-apply] === {band} 완료 — vids={len(all_vids)} "
            f"added={added} failed={failed} ==="
        )

    return {
        "processed": sum(b["added"] for b in result_by_band.values()),
        "by_band": result_by_band,
    }
