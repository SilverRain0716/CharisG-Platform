"""
keyword_queue_worker.py — 키워드 큐 폴링 daemon.

흐름:
  POST /keyword-queue/add → keyword_queue (status='queued')
  worker(60s 폴링) → 가장 오래된 queued 1건 → 'processing' 마킹 →
    translate_keyword_to_english + process_keyword(threadpool) → status='done'/'error' + result_json

sheet_queue_worker 와 동일 패턴. PA API 내부 asyncio task 로 기동 (main.py lifespan).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = int(os.environ.get("PA_KEYWORD_QUEUE_INTERVAL", "60"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _ensure_table() -> None:
    """keyword_queue 테이블 보장 (idempotent)."""
    from backend.purchase.database import get_db
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS keyword_queue (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword       TEXT NOT NULL,
                max_groups    INTEGER NOT NULL DEFAULT 5,
                channels      TEXT NOT NULL DEFAULT 'coupang',
                requested     INTEGER NOT NULL DEFAULT 0,
                dry_run       INTEGER NOT NULL DEFAULT 0,
                status        TEXT NOT NULL DEFAULT 'queued',
                result_json   TEXT,
                error_message TEXT,
                created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                started_at    TEXT,
                finished_at   TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_keyword_queue_status ON keyword_queue(status)")


def _claim_next_queued() -> dict | None:
    """가장 오래된 queued 1건을 processing 으로 마킹 + dict 반환 (없으면 None)."""
    from backend.purchase.database import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM keyword_queue WHERE status='queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        n = conn.execute(
            "UPDATE keyword_queue SET status='processing', started_at=? WHERE id=? AND status='queued'",
            (_now_iso(), row["id"]),
        ).rowcount
        if n != 1:
            return None  # 동시성: 다른 worker 가 가져감
        return dict(row)


def _mark_done(id_: int, result: dict) -> None:
    from backend.purchase.database import get_db
    with get_db() as conn:
        conn.execute(
            "UPDATE keyword_queue SET status='done', result_json=?, finished_at=? WHERE id=?",
            (json.dumps(result, ensure_ascii=False, default=str), _now_iso(), id_),
        )


def _mark_error(id_: int, err: str) -> None:
    from backend.purchase.database import get_db
    with get_db() as conn:
        conn.execute(
            "UPDATE keyword_queue SET status='error', error_message=?, finished_at=? WHERE id=?",
            (str(err)[:500], _now_iso(), id_),
        )


async def _process_one(row: dict) -> None:
    """큐 행 1건 → translate(영문) → process_keyword(threadpool)."""
    from backend.purchase.services.keyword_to_groups import (
        translate_keyword_to_english, process_keyword,
    )
    try:
        kw_en = await translate_keyword_to_english(row["keyword"])
        channels = [c.strip() for c in (row.get("channels") or "coupang").split(",") if c.strip()]
        result = await asyncio.to_thread(
            process_keyword, kw_en,
            max_groups=int(row.get("max_groups") or 5),
            channels=channels,
            dry_run=bool(row.get("dry_run")),
            requested=bool(row.get("requested")),
        )
        result["original_keyword"] = row["keyword"]
        result["search_keyword"] = kw_en
        _mark_done(int(row["id"]), result)
        logger.info(f"[kw-queue] id={row['id']} done — kw='{row['keyword']}' summary={result.get('summary')}")
    except Exception as e:
        logger.exception(f"[kw-queue] id={row['id']} 처리 실패")
        _mark_error(int(row["id"]), f"{type(e).__name__}: {e}")


async def run_forever() -> None:
    """주기적으로 queued 1건씩 처리. PA API 기동 시 asyncio task 로 시작."""
    _ensure_table()
    logger.info(f"[kw-queue] worker 기동 (interval={POLL_INTERVAL_SEC}s)")
    while True:
        try:
            row = _claim_next_queued()
            if row:
                await _process_one(row)
            else:
                await asyncio.sleep(POLL_INTERVAL_SEC)
        except asyncio.CancelledError:
            logger.info("[kw-queue] worker 취소됨"); raise
        except Exception:
            logger.exception("[kw-queue] cycle 예외 (계속 폴링)")
            await asyncio.sleep(POLL_INTERVAL_SEC)
