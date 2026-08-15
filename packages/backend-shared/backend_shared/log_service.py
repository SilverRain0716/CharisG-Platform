"""
log_service.py — 로그 저장 + SSE 브로드캐스트
"""
import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import AsyncGenerator

from backend_shared.context import get_db

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# SSE 구독자 목록
_subscribers: list[asyncio.Queue] = []


def add_log(level: str, message: str, pipeline_run_id: int = None) -> dict:
    """로그 저장 + SSE 브로드캐스트"""
    now = datetime.now(KST).strftime("%H:%M:%S")

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO logs (pipeline_run_id, level, message) VALUES (?, ?, ?)",
            (pipeline_run_id, level, message),
        )
        log_id = cur.lastrowid

    entry = {"id": log_id, "level": level, "message": message, "time": now}

    # 비동기 브로드캐스트
    for q in _subscribers[:]:
        try:
            q.put_nowait(entry)
        except asyncio.QueueFull:
            _subscribers.remove(q)

    return entry


def get_recent_logs(limit: int = 50) -> list[dict]:
    """최근 로그 조회"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, level, message, created_at FROM logs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    return [
        {
            "id": r["id"],
            "level": r["level"],
            "message": r["message"],
            "created_at": r["created_at"],
        }
        for r in reversed(rows)
    ]


async def subscribe() -> AsyncGenerator[str, None]:
    """SSE 구독 — 새 로그가 들어올 때마다 이벤트 전송"""
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.append(queue)
    try:
        while True:
            entry = await queue.get()
            yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
    except asyncio.CancelledError:
        pass
    finally:
        if queue in _subscribers:
            _subscribers.remove(queue)
