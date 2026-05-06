"""listings_pa 의 한국 직배송 가능 여부 일괄 검증 + 결과 저장.

업로드 파이프라인(sourcing_promote.py / ai_processor)을 절대 건들지 않고,
이미 등록된 active 상품을 별도 트리거로 검증한다.
결과는 listings_pa.kr_shipping_eligible / kr_shipping_checked_at 에 저장.

호출처: routers/kr_shipping.py — sync(verify_listings) + async(run_batch_verify).

rate limit: amazon_kr_shipping._REQUEST_INTERVAL = 1.5초/ASIN.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from backend.purchase.database import get_db
from backend.purchase.services.amazon_kr_shipping import AmazonKRShippingChecker

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _select_targets(
    limit: int,
    channel: Optional[str],
    force: bool,
    explicit_asins: Optional[list[str]],
) -> list[tuple[Optional[int], Optional[str], str]]:
    """검증할 (listing_id=None, channel=None, asin) 튜플 목록 선정 — ASIN 단위 distinct.

    Amazon 셀러의 한국 직배 정책은 ASIN 단위로 결정되어 채널과 무관하므로
    같은 ASIN 을 채널마다 중복 fetch 하지 않는다. _save_result(listing_id=None, ...) 가
    ASIN 기준으로 모든 listed listings 를 한 번에 update 하므로 listing_id 는 항상 None.

    explicit_asins 명시 시 그 ASIN 만.
    아닐 때는 listed + ASIN 보유 + (force=False 면 ASIN 의 모든 listings 가 미검증) 인 ASIN.
    """
    if explicit_asins:
        return [(None, None, a) for a in explicit_asins]

    # ASIN 단위 select. 채널 필터를 줄 경우 그 채널에 listed 가 있는 ASIN 만.
    # 미검증 정의: 그 ASIN 의 listed listings 중 하나라도 NULL 이면 미검증
    # (force=False 시) — ASIN 1번 fetch 로 모든 listing update 하므로 한 곳만 NULL 이면 됨.
    sql = """SELECT p.asin AS asin,
                    MIN(lp.kr_shipping_checked_at) AS oldest_check
               FROM listings_pa lp
               JOIN products p ON p.id = lp.product_id
              WHERE lp.status='listed' AND p.asin IS NOT NULL"""
    params: list = []
    if channel:
        sql += " AND lp.channel = ?"
        params.append(channel)
    sql += " GROUP BY p.asin"
    if not force:
        # GROUP HAVING — ASIN 의 listings 중 하나라도 NULL 이면 포함
        sql += " HAVING SUM(CASE WHEN lp.kr_shipping_checked_at IS NULL THEN 1 ELSE 0 END) > 0"
    sql += " ORDER BY oldest_check IS NULL DESC, oldest_check ASC LIMIT ?"
    params.append(limit)
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [(None, None, r["asin"]) for r in rows]


def _save_result(listing_id: Optional[int], asin: str, eligible: bool) -> int:
    """결과를 listings_pa 에 저장. listing_id 가 None 이면 ASIN 기준 모든 active listing 갱신.

    반환: 영향 받은 row 수.
    """
    now = _now_iso()
    with get_db() as conn:
        if listing_id is not None:
            cur = conn.execute(
                """UPDATE listings_pa
                      SET kr_shipping_eligible = ?,
                          kr_shipping_checked_at = ?
                    WHERE id = ?""",
                (1 if eligible else 0, now, listing_id),
            )
        else:
            cur = conn.execute(
                """UPDATE listings_pa
                      SET kr_shipping_eligible = ?,
                          kr_shipping_checked_at = ?
                    WHERE product_id IN (SELECT id FROM products WHERE asin = ?)
                      AND status = 'listed'""",
                (1 if eligible else 0, now, asin),
            )
        return cur.rowcount


def verify_listings(
    limit: int = 30,
    channel: Optional[str] = None,
    force: bool = False,
    asins: Optional[list[str]] = None,
) -> dict:
    """active listings 의 한국 직배송 가능 여부 확인 + 저장.

    Args:
        limit: explicit_asins 가 None 일 때 처리할 최대 건수.
        channel: 'smartstore' | 'coupang' | None (전체).
        force: True 면 이미 검증된 항목도 재검증 (오래된 것 우선).
        asins: 명시 ASIN 리스트 — 지정 시 limit/channel/force 무시하고 그 ASIN 만 처리.

    Returns:
        {checked, eligible, blocked, errors, results: [{asin, eligible, listing_id, updated_rows}]}
    """
    targets = _select_targets(limit, channel, force, asins)
    if not targets:
        return {"checked": 0, "eligible": 0, "blocked": 0, "errors": 0, "results": []}

    checked = 0
    eligible = 0
    blocked = 0
    errors = 0
    results: list[dict] = []

    checker = AmazonKRShippingChecker()
    if not checker.init_session():
        logger.error("[kr-shipping-verifier] Amazon 세션 초기화 실패")
        return {
            "checked": 0, "eligible": 0, "blocked": 0,
            "errors": len(targets),
            "results": [{"asin": a, "error": "session_init_failed"} for _, _, a in targets],
        }

    try:
        for listing_id, ch, asin in targets:
            try:
                ok = checker.check(asin)
                checked += 1
                if ok:
                    eligible += 1
                else:
                    blocked += 1
                rows = _save_result(listing_id, asin, ok)
                results.append({
                    "asin": asin,
                    "eligible": ok,
                    "listing_id": listing_id,
                    "channel": ch,
                    "updated_rows": rows,
                })
            except Exception as e:
                errors += 1
                logger.warning("[kr-shipping-verifier] %s 검증 실패: %s", asin, e)
                results.append({"asin": asin, "error": str(e)[:200]})
    finally:
        checker.close()

    return {
        "checked": checked,
        "eligible": eligible,
        "blocked": blocked,
        "errors": errors,
        "results": results,
    }


# ────────────────────────────────────────────────────────────
# 백그라운드 일괄 검증 (job_id 추적, 중단 가능, idempotent 재시작)
# ────────────────────────────────────────────────────────────

def _job_status(job_id: str) -> Optional[str]:
    with get_db() as conn:
        row = conn.execute("SELECT status FROM batch_jobs WHERE id=?", (job_id,)).fetchone()
    return row["status"] if row else None


def _job_init(job_id: str, total: int, phase_message: str) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT INTO batch_jobs (id, job_type, status, total, created_at, phase_message)
                 VALUES (?, 'kr_shipping_verify_batch', 'running', ?, ?, ?)""",
            (job_id, total, _now_iso(), phase_message),
        )


def _job_update(
    job_id: str,
    processed: int,
    errors: int,
    phase_message: str,
    status: Optional[str] = None,
    finished: bool = False,
) -> None:
    fields = ["processed=?", "errors=?", "phase_message=?"]
    params: list = [processed, errors, phase_message]
    if status:
        fields.append("status=?")
        params.append(status)
    if finished:
        fields.append("finished_at=?")
        params.append(_now_iso())
    params.append(job_id)
    with get_db() as conn:
        conn.execute(f"UPDATE batch_jobs SET {', '.join(fields)} WHERE id=?", params)


def _count_unchecked(channel: Optional[str] = None) -> int:
    """미검증 ASIN 수 (distinct). 한 ASIN 의 listings 중 하나라도 NULL 이면 미검증으로 카운트."""
    sql = """SELECT COUNT(DISTINCT p.asin) AS c
               FROM listings_pa lp JOIN products p ON p.id = lp.product_id
              WHERE lp.status='listed' AND p.asin IS NOT NULL
                AND lp.kr_shipping_checked_at IS NULL"""
    params: list = []
    if channel:
        sql += " AND lp.channel = ?"
        params.append(channel)
    with get_db() as conn:
        row = conn.execute(sql, params).fetchone()
    return row["c"] if row else 0


async def run_batch_verify(
    job_id: str,
    coupang_chunk: int = 3000,
    smartstore_chunk: int = 3000,
) -> None:
    """채널별 chunk 씩 검증을 반복해서 미검증분이 0 될 때까지 진행.

    - 한 사이클: smartstore chunk → coupang chunk → 다음 사이클...
    - chunk 마다 새로 select → idempotent. 중간에 죽어도 재호출 시 이어서.
    - 중단: batch_jobs.status='cancelled' 신호 시 다음 ASIN 전에 종료.
    - Amazon CAPTCHA / rate limit 위험 회피: 사이클 사이 30초 휴식, 채널 사이 5초.
    """
    initial_remaining = _count_unchecked()
    _job_init(job_id, initial_remaining, f"준비 중 (총 미검증 {initial_remaining})")

    checker = AmazonKRShippingChecker()
    if not await asyncio.to_thread(checker.init_session):
        logger.error("[kr-batch %s] Amazon 세션 초기화 실패", job_id)
        _job_update(
            job_id, 0, initial_remaining,
            "Amazon 세션 초기화 실패",
            status="error", finished=True,
        )
        return

    overall_processed = 0
    overall_errors = 0
    overall_eligible = 0
    overall_blocked = 0
    cycle = 0

    try:
        while True:
            cycle += 1
            had_work = False

            for ch, chunk in (("smartstore", smartstore_chunk), ("coupang", coupang_chunk)):
                if chunk <= 0:
                    continue

                # 매 chunk 새로 select — 중간에 채워진 항목 자연 제외
                targets = await asyncio.to_thread(
                    _select_targets, chunk, ch, False, None,
                )
                if not targets:
                    logger.info("[kr-batch %s] cycle=%d %s: 미검증 0 — 스킵", job_id, cycle, ch)
                    continue
                had_work = True

                logger.info(
                    "[kr-batch %s] cycle=%d %s 시작: %d 건",
                    job_id, cycle, ch, len(targets),
                )

                for i, (listing_id, _ch_unused, asin) in enumerate(targets, 1):
                    # 중단 신호
                    if _job_status(job_id) == "cancelled":
                        logger.info("[kr-batch %s] 사용자 중단", job_id)
                        _job_update(
                            job_id, overall_processed, overall_errors,
                            f"중단됨 (cycle={cycle} {ch} {i}/{len(targets)})",
                            status="cancelled", finished=True,
                        )
                        return

                    try:
                        ok = await asyncio.to_thread(checker.check, asin)
                        await asyncio.to_thread(_save_result, listing_id, asin, ok)
                        overall_processed += 1
                        if ok:
                            overall_eligible += 1
                        else:
                            overall_blocked += 1
                    except Exception as e:
                        overall_errors += 1
                        logger.warning("[kr-batch %s] %s 실패: %s", job_id, asin, e)

                    # 매 10건마다 진행상황 갱신
                    if i % 10 == 0 or i == len(targets):
                        _job_update(
                            job_id, overall_processed, overall_errors,
                            f"cycle {cycle} {ch} {i}/{len(targets)} "
                            f"(누적 eligible {overall_eligible}, blocked {overall_blocked})",
                        )

                logger.info(
                    "[kr-batch %s] cycle=%d %s 완료 — 누적 처리 %d",
                    job_id, cycle, ch, overall_processed,
                )

                # 채널 사이 5초 휴식 (Amazon rate-limit 안전망)
                await asyncio.sleep(5)

            if not had_work:
                logger.info("[kr-batch %s] 모든 채널 미검증 0 — 종료", job_id)
                break

            # 사이클 사이 30초 휴식 (CAPTCHA 트리거 회피)
            _job_update(
                job_id, overall_processed, overall_errors,
                f"cycle {cycle} 완료 — 30초 휴식 후 다음 cycle",
            )
            await asyncio.sleep(30)

        _job_update(
            job_id, overall_processed, overall_errors,
            f"완료 — 처리 {overall_processed}, eligible {overall_eligible}, "
            f"blocked {overall_blocked}, errors {overall_errors}, cycles={cycle}",
            status="done", finished=True,
        )

    except Exception as e:
        logger.exception("[kr-batch %s] 예외 — job 실패 처리", job_id)
        _job_update(
            job_id, overall_processed, overall_errors,
            f"예외: {str(e)[:200]}",
            status="error", finished=True,
        )
    finally:
        await asyncio.to_thread(checker.close)
