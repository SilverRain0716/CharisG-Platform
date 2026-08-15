"""mfr_classify_daemon — 미분류 brand 자동 분류 (idle 시 chunk, 일일 quota 5K).

조건:
  - sheet_queue 에 active 잡 (queued/importing/promoting/detailing/channelsending/uploading) 없을 때만 진행
  - 매일 KST 자정 기준 quota 5,000 brand 까지
  - chunk 100 brand/cycle, cycle 1분 간격 (idle 시)

목적:
  - B 잡 (38K unique brand 분류) 을 import 영업과 충돌 없이 점진적 진행
  - 새 manufacturer_classifier v2 logic 사용 (한글 + whitelist + Gemini + threshold)
"""
import asyncio
import logging
import time

from backend.purchase.database import get_db

logger = logging.getLogger(__name__)

DAILY_QUOTA = 5000
CHUNK_SIZE = 100
IDLE_POLL_SEC = 300   # 5분 — active 잡 있을 때 대기 간격
CYCLE_SEC = 60        # 1분 — idle 시 cycle 간격
INITIAL_DELAY = 600   # 10분 후 시작 (서비스 기동 직후 영향 최소화)

_daily_count = 0
_last_reset_date = ""


def _is_idle() -> bool:
    """sheet_queue 에 active 잡 없으면 True."""
    try:
        with get_db() as conn:
            n = conn.execute(
                """SELECT COUNT(*) FROM sheet_queue
                   WHERE status IN ('queued','importing','promoting','detailing',
                                    'channelsending','uploading_smartstore','uploading_coupang')"""
            ).fetchone()[0]
        return n == 0
    except Exception as e:
        logger.warning(f"[mfr-classify-daemon] _is_idle 예외: {e}")
        return False


async def _classify_chunk(n: int) -> int:
    """unique mfr 최대 n 건 분류. 분류 성공한 mfr 수 반환."""
    from backend.purchase.services.manufacturer_classifier import classify_korean_sync

    with get_db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT amazon_manufacturer FROM products
                WHERE amazon_manufacturer IS NOT NULL
                  AND amazon_manufacturer != '__NONE__'
                  AND manufacturer_is_korean IS NULL
                ORDER BY amazon_manufacturer
                LIMIT ?""",
            (n,),
        ).fetchall()
    mfrs = [r[0] for r in rows]
    if not mfrs:
        return 0

    processed = 0
    for mfr in mfrs:
        try:
            cls = await asyncio.to_thread(classify_korean_sync, mfr)
        except Exception as e:
            logger.warning(f"[mfr-classify-daemon] classify 예외: {mfr} {e}")
            continue
        if cls is None:
            continue
        is_kr = 1 if cls.get("is_korean") else 0
        try:
            with get_db() as conn:
                conn.execute(
                    """UPDATE products
                          SET manufacturer_is_korean=?,
                              manufacturer_classified_at=CURRENT_TIMESTAMP
                        WHERE amazon_manufacturer=?
                          AND manufacturer_is_korean IS NULL""",
                    (is_kr, mfr),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"[mfr-classify-daemon] UPDATE 예외: {mfr} {e}")
            continue
        processed += 1
    return processed


async def run_forever() -> None:
    """lifespan 에서 create_task 로 기동."""
    global _daily_count, _last_reset_date
    await asyncio.sleep(INITIAL_DELAY)
    logger.info(
        f"[mfr-classify-daemon] 기동 (daily_quota={DAILY_QUOTA}, chunk={CHUNK_SIZE}, "
        f"idle_poll={IDLE_POLL_SEC}s, cycle={CYCLE_SEC}s)"
    )
    while True:
        try:
            # KST 기준 일자 (UTC+9)
            today = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 9 * 3600))
            if today != _last_reset_date:
                _daily_count = 0
                _last_reset_date = today
                logger.info(f"[mfr-classify-daemon] 일자 변경 {today} — daily_count reset")

            if _daily_count >= DAILY_QUOTA:
                # quota 소진 — 다음 날까지 1시간 단위 대기
                await asyncio.sleep(3600)
                continue

            if not _is_idle():
                await asyncio.sleep(IDLE_POLL_SEC)
                continue

            # idle + quota 여유 → chunk 처리
            remaining = DAILY_QUOTA - _daily_count
            chunk_n = min(CHUNK_SIZE, remaining)
            n = await _classify_chunk(chunk_n)
            _daily_count += n
            if n > 0:
                logger.info(
                    f"[mfr-classify-daemon] chunk {n} 분류 완료 — "
                    f"today {_daily_count}/{DAILY_QUOTA}"
                )
            elif n == 0:
                # 미분류 mfr 없음 — 1시간 대기
                logger.info("[mfr-classify-daemon] 미분류 mfr 없음, 1h 대기")
                await asyncio.sleep(3600)
                continue
        except Exception:
            logger.exception("[mfr-classify-daemon] cycle 예외")
        await asyncio.sleep(CYCLE_SEC)
