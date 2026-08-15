"""
image_cleanup_poller.py — 만료 이미지 주기 정리.

scheduled_delete_at 이 지난 로컬 파일 + image_cache DB 레코드 삭제. FastAPI lifespan
에서 asyncio task 로 기동. 서비스 기동 즉시 1회 실행 후 24시간 간격 반복.

환경변수:
  PA_IMAGE_CLEANUP_INTERVAL_SEC — 기본 86400 (24h)
  PA_DISABLE_IMAGE_CLEANUP=1  — 폴러 비활성화
"""
import asyncio
import logging
import os

from backend.purchase.services.image_downloader import cleanup_expired_images

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = int(os.environ.get("PA_IMAGE_CLEANUP_INTERVAL_SEC", "86400"))


async def run_forever() -> None:
    while True:
        try:
            result = await asyncio.to_thread(cleanup_expired_images)
            logger.info(f"이미지 정리 주기 실행 완료: {result}")
        except Exception as e:
            logger.exception(f"이미지 정리 실패: {e}")
        await asyncio.sleep(POLL_INTERVAL_SEC)
