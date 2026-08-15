"""PA CS — 티켓 + AI 초안."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.database import get_db_hot as get_db
from backend_shared.ai import generate_cs_draft

router = APIRouter(prefix="/api/pa/cs", tags=["pa-cs"])


@router.get("")
def list_tickets(
    user: dict = Depends(current_user),
    status: Optional[str] = None,
    channel: Optional[str] = None,
    limit: int = 100,
):
    where = []
    params = []
    if status:
        where.append("status=?")
        params.append(status)
    if channel:
        where.append("channel=?")
        params.append(channel)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM cs_tickets {where_sql} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [dict(r) for r in rows]


class TicketCreate(BaseModel):
    channel: str
    order_id: Optional[int] = None
    customer_name: Optional[str] = None
    type: str = "기타"
    priority: str = "normal"
    customer_message: str


@router.post("")
async def create_ticket(body: TicketCreate, user: dict = Depends(current_user)):
    draft = await generate_cs_draft(
        ticket_type=body.type,
        customer_message=body.customer_message,
        market="KR",
        platform=body.channel,
    )
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO cs_tickets
               (channel, order_id, customer_name, type, priority, customer_message, ai_draft)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (body.channel, body.order_id, body.customer_name, body.type, body.priority,
             body.customer_message, draft),
        )
    return {"ok": True, "id": cur.lastrowid, "ai_draft": draft}


class ResolveBody(BaseModel):
    final_response: str


@router.patch("/{tid}/resolve")
def resolve(tid: int, body: ResolveBody, user: dict = Depends(current_user)):
    with get_db() as conn:
        conn.execute(
            """UPDATE cs_tickets SET final_response=?, status='resolved',
                       resolved_at=CURRENT_TIMESTAMP WHERE id=?""",
            (body.final_response, tid),
        )
    return {"ok": True}
