"""한국 직배송 검증 라우터 — listings_pa active 상품을 별도 트리거로 확인.

업로드 파이프라인은 손대지 않음 (sourcing_promote.py / ai_processor 무관).
"""
import asyncio
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend.purchase.services.kr_shipping_verifier import verify_listings, run_batch_verify

router = APIRouter(prefix="/api/pa/kr-shipping", tags=["pa-kr-shipping"])


class VerifyBody(BaseModel):
    limit: int = Field(30, ge=1, le=50)
    channel: Optional[str] = None
    force: bool = False
    asins: Optional[list[str]] = None


@router.post("/verify")
def kr_shipping_verify(body: VerifyBody, user: dict = Depends(current_user)):
    """active listings 한국 직배 검증.

    - limit ≤ 50 (nginx 120초 한도, 건당 ~2초).
    - asins 명시 시 그 ASIN 만 (limit/channel/force 무시).
    - force=True 면 이미 검증된 항목 재검증.
    """
    if body.channel and body.channel not in ("smartstore", "coupang"):
        raise HTTPException(400, "channel 은 smartstore | coupang | null 중 하나")
    return verify_listings(
        limit=body.limit,
        channel=body.channel,
        force=body.force,
        asins=body.asins,
    )


@router.get("/summary")
def kr_shipping_summary(user: dict = Depends(current_user)):
    """active listings_pa 의 한국 직배 검증 통계 (채널 무관 + 채널별)."""
    with get_db() as conn:
        overall = conn.execute(
            """SELECT
                  COUNT(*) AS total,
                  SUM(CASE WHEN kr_shipping_eligible IS NULL THEN 1 ELSE 0 END) AS unchecked,
                  SUM(CASE WHEN kr_shipping_eligible=1 THEN 1 ELSE 0 END) AS eligible,
                  SUM(CASE WHEN kr_shipping_eligible=0 THEN 1 ELSE 0 END) AS blocked,
                  MAX(kr_shipping_checked_at) AS latest_check
                FROM listings_pa WHERE status='listed'"""
        ).fetchone()
        per_ch = conn.execute(
            """SELECT channel,
                  COUNT(*) AS total,
                  SUM(CASE WHEN kr_shipping_eligible IS NULL THEN 1 ELSE 0 END) AS unchecked,
                  SUM(CASE WHEN kr_shipping_eligible=1 THEN 1 ELSE 0 END) AS eligible,
                  SUM(CASE WHEN kr_shipping_eligible=0 THEN 1 ELSE 0 END) AS blocked
                FROM listings_pa WHERE status='listed' GROUP BY channel"""
        ).fetchall()
    return {
        "overall": dict(overall) if overall else {},
        "per_channel": [dict(r) for r in per_ch],
    }


class BatchBody(BaseModel):
    coupang_chunk: int = Field(3000, ge=0, le=20000)
    smartstore_chunk: int = Field(3000, ge=0, le=20000)


@router.post("/verify-batch")
async def kr_shipping_verify_batch(body: BatchBody, user: dict = Depends(current_user)):
    """미검증분 0 될 때까지 chunk 반복 자동 검증 — job_id 즉시 반환 후 백그라운드.

    한 cycle = smartstore chunk + coupang chunk. cycle 끝나면 30초 휴식 후 다음 cycle.
    rate limit ~1.5~2초/건 → 27,000건이면 약 15시간 (백그라운드).
    중단되어도 idempotent — 다시 호출하면 NULL 만큼만 이어서.
    """
    # 중복 실행 방지
    with get_db() as conn:
        running = conn.execute(
            """SELECT id, processed, total, phase_message
                 FROM batch_jobs
                WHERE job_type='kr_shipping_verify_batch' AND status='running'
                ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
    if running:
        return {
            "job_id": running["id"],
            "status": "already_running",
            "processed": running["processed"],
            "total": running["total"],
            "phase_message": running["phase_message"],
        }

    job_id = uuid.uuid4().hex[:12]
    asyncio.create_task(run_batch_verify(
        job_id=job_id,
        coupang_chunk=body.coupang_chunk,
        smartstore_chunk=body.smartstore_chunk,
    ))
    return {
        "job_id": job_id,
        "status": "started",
        "coupang_chunk": body.coupang_chunk,
        "smartstore_chunk": body.smartstore_chunk,
        "note": "미검증분 0 될 때까지 chunk 반복 (백그라운드, 약 15시간 소요).",
    }


@router.get("/verify-batch/{job_id}")
def kr_shipping_batch_status(job_id: str, user: dict = Depends(current_user)):
    with get_db() as conn:
        row = conn.execute(
            """SELECT id, status, total, processed, errors, phase_message,
                      created_at, started_at, finished_at
                 FROM batch_jobs WHERE id=? AND job_type='kr_shipping_verify_batch'""",
            (job_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "job_id 없음")
    return dict(row)


@router.post("/verify-batch/{job_id}/cancel")
def kr_shipping_batch_cancel(job_id: str, user: dict = Depends(current_user)):
    """진행 중인 job 에 중단 신호. 다음 ASIN 으로 넘어가기 전에 종료됨."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM batch_jobs WHERE id=? AND job_type='kr_shipping_verify_batch'",
            (job_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "job_id 없음")
        if row["status"] != "running":
            return {"ok": False, "reason": f"이미 {row['status']} 상태"}
        conn.execute(
            "UPDATE batch_jobs SET status='cancelled' WHERE id=?", (job_id,),
        )
    return {"ok": True}


@router.get("/blocked")
def kr_shipping_blocked(
    channel: Optional[str] = None,
    limit: int = 50,
    user: dict = Depends(current_user),
):
    """한국 직배 불가 판정된 active listings 목록."""
    sql = """SELECT lp.id, lp.channel, p.asin, p.title_ko,
                    lp.kr_shipping_checked_at, lp.sale_krw
               FROM listings_pa lp
               JOIN products p ON p.id = lp.product_id
              WHERE lp.status='listed' AND lp.kr_shipping_eligible = 0"""
    params: list = []
    if channel:
        sql += " AND lp.channel = ?"
        params.append(channel)
    sql += " ORDER BY lp.kr_shipping_checked_at DESC LIMIT ?"
    params.append(min(limit, 200))
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
