"""PA keywords — 키워드 + 클러스터."""
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend.purchase.services.keyword_cluster_service import cluster_keywords, store_clusters

router = APIRouter(prefix="/api/pa/keywords", tags=["pa-keywords"])


@router.get("")
def list_keywords(
    user: dict = Depends(current_user),
    status: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
):
    where = []
    params = []
    if status:
        where.append("status=?")
        params.append(status)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT k.id, k.keyword, k.source, k.cluster_id, k.monthly_pc, k.monthly_mobile,
                       k.competition, k.trend_score, k.status, k.discovered_at,
                       c.label cluster_label
                FROM keywords k LEFT JOIN keyword_clusters c ON k.cluster_id=c.id
                {where_sql}
                ORDER BY k.discovered_at DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) c FROM keywords {where_sql}", tuple(params),
        ).fetchone()["c"]
        unclustered = conn.execute(
            "SELECT COUNT(*) c FROM keywords WHERE cluster_id IS NULL"
        ).fetchone()["c"]

    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "unclustered": unclustered,
    }


@router.get("/clusters")
def list_clusters(user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM keyword_clusters ORDER BY member_count DESC"
        ).fetchall()
    return [dict(r) for r in rows]


class ClusterRequest(BaseModel):
    keywords: Optional[list[str]] = None  # None 이면 keywords 테이블에서 가져옴


@router.post("/cluster")
async def run_cluster(req: ClusterRequest, user: dict = Depends(current_user)):
    if not req.keywords:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT keyword FROM keywords WHERE cluster_id IS NULL LIMIT 80"
            ).fetchall()
        keywords = [r["keyword"] for r in rows]
    else:
        keywords = req.keywords

    clusters = await cluster_keywords(keywords)
    inserted = store_clusters(clusters)
    return {"clusters": clusters, "inserted": inserted}
