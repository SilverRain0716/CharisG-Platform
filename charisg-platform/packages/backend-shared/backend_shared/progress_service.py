"""
progress_service.py — 수집 작업 진행률 SSE 브로드캐스트
"""
import asyncio
import json
import logging
from typing import AsyncGenerator

logger = logging.getLogger(__name__)

# job_id별 구독자 관리
_subscribers: dict[int, list[asyncio.Queue]] = {}


def broadcast_progress(job_id: int, data: dict):
    """진행률 데이터 브로드캐스트 (크롤러에서 호출)"""
    if job_id not in _subscribers:
        return
    for q in _subscribers[job_id][:]:
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            _subscribers[job_id].remove(q)


async def subscribe_progress(job_id: int) -> AsyncGenerator[str, None]:
    """SSE 구독 — 특정 작업의 진행률 수신"""
    if job_id not in _subscribers:
        _subscribers[job_id] = []

    queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    _subscribers[job_id].append(queue)

    try:
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=30)
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            except asyncio.TimeoutError:
                # 30초 무활동 시 heartbeat
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
    except asyncio.CancelledError:
        pass
    finally:
        if job_id in _subscribers and queue in _subscribers[job_id]:
            _subscribers[job_id].remove(queue)
            if not _subscribers[job_id]:
                del _subscribers[job_id]
