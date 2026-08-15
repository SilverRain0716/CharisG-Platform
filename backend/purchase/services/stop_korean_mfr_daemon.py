"""stop_korean_mfr_daemon.py — 한국 제조사 listed 사후 안전망.

manufacturer_is_korean=1 + listings_pa.status='listed' 인 케이스를 30분 주기로
스캔해서 자동 stop_sales. 갭 1 (sourcing_promote) / 갭 3 (coupang_lister) 자동
차단을 뚫고 listed 된 한국 mfr (재분류된 mfr 등) 을 청소.

성공 시 listings_pa.status='paused', 채널 stale 시 status='archived' 로 정리.
정책 critical — IP 라이선스 위반 회피.
"""
import asyncio
import logging

from backend.purchase.database import get_db

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 1800  # 30분
INITIAL_DELAY_SEC = 120


async def _run_once() -> dict:
    """1 cycle — 한국 mfr=1 + listed 전체 stop."""
    from backend.purchase.services import coupang_service as cps
    from backend.purchase.services import naver_commerce_service as nvs

    with get_db() as conn:
        rows = conn.execute(
            """SELECT l.id AS lid, l.channel, l.channel_product_id AS cpid,
                      p.id AS pid, p.asin, p.amazon_manufacturer AS mfr
                 FROM products p JOIN listings_pa l ON l.product_id=p.id
                WHERE p.manufacturer_is_korean=1 AND l.status='listed'"""
        ).fetchall()
    targets = [dict(r) for r in rows]
    if not targets:
        return {"checked": 0, "stopped": 0, "stale": 0}

    logger.info(f"[stop-korean-mfr] 발견 {len(targets)}건 — stop_sales 진행")
    stopped = stale = 0
    for r in targets:
        cpid = r["cpid"]
        mfr = r["mfr"]
        lid = r["lid"]
        ch = r["channel"]
        try:
            if ch == "coupang":
                if cpid:
                    success, msg = await asyncio.to_thread(cps.stop_sales, cpid)
                else:
                    success, msg = False, "cpid empty"
                await asyncio.sleep(3)
            else:
                try:
                    success, msg = await asyncio.to_thread(nvs.stop_sales, cpid)
                except Exception as e:
                    success, msg = False, str(e)
                await asyncio.sleep(1)
        except Exception as e:
            success, msg = False, str(e)

        with get_db() as conn:
            if success:
                conn.execute(
                    "UPDATE listings_pa SET status='paused', error_message=? WHERE id=?",
                    (f"korean_mfr_daemon_stopped | {mfr}", lid),
                )
                stopped += 1
            else:
                conn.execute(
                    "UPDATE listings_pa SET status='archived', error_message=? WHERE id=?",
                    (f"korean_mfr_daemon_stale: {msg[:80]} | {mfr}", lid),
                )
                stale += 1
    logger.info(
        f"[stop-korean-mfr] 완료: stopped={stopped} stale={stale}/{len(targets)}"
    )
    return {"checked": len(targets), "stopped": stopped, "stale": stale}


async def run_forever() -> None:
    """lifespan 에서 create_task 로 기동."""
    await asyncio.sleep(INITIAL_DELAY_SEC)
    logger.info(f"[stop-korean-mfr-daemon] 기동 (interval={POLL_INTERVAL_SEC}s)")
    while True:
        try:
            await _run_once()
        except Exception:
            logger.exception("[stop-korean-mfr] cycle 예외")
        await asyncio.sleep(POLL_INTERVAL_SEC)
