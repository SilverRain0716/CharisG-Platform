"""PA discovery — 데이터랩 카테고리 추적 + 풀 파이프라인."""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend.purchase.services import discovery_pipeline, naver_datalab_scraper

router = APIRouter(prefix="/api/pa/discovery", tags=["pa-discovery"])


class ToggleBody(BaseModel):
    tracked: bool


@router.get("/categories")
def list_categories(user: dict = Depends(current_user)):
    """저장된 카테고리 목록 + tracked flag."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT cid, name, full_path, level, parent_cid, tracked, last_synced
               FROM pa_discovery_categories
               ORDER BY level, cid"""
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/categories/sync")
def sync_categories(user: dict = Depends(current_user)):
    """데이터랩에서 카테고리 트리 재동기화 (멱등 UPSERT)."""
    tree = naver_datalab_scraper.fetch_category_list()
    if not tree:
        raise HTTPException(502, "데이터랩 카테고리 조회 실패")

    flattened: list[tuple] = []

    def walk(node: dict, parent_cid=None, parent_path: str = "") -> None:
        cid_raw = node.get("cid")
        if cid_raw is None:
            return
        try:
            cid = int(cid_raw)
        except (TypeError, ValueError):
            return
        name = node.get("name", "") or ""
        level = int(node.get("level", 1) or 1)
        full_path = f"{parent_path}/{name}" if parent_path else name
        flattened.append((cid, name, full_path, level, parent_cid))
        for child in node.get("childList", []) or []:
            walk(child, cid, full_path)

    for top in tree:
        walk(top)

    with get_db() as conn:
        for cid, name, full_path, level, parent_cid in flattened:
            conn.execute(
                """INSERT INTO pa_discovery_categories
                     (cid, name, full_path, level, parent_cid, last_synced)
                   VALUES (?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(cid) DO UPDATE SET
                     name=excluded.name,
                     full_path=excluded.full_path,
                     level=excluded.level,
                     parent_cid=excluded.parent_cid,
                     last_synced=datetime('now')""",
                (cid, name, full_path, level, parent_cid),
            )
    return {"synced": len(flattened)}


@router.patch("/categories/{cid}")
def toggle_category(
    cid: int, body: ToggleBody, user: dict = Depends(current_user)
):
    tracked = 1 if body.tracked else 0
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE pa_discovery_categories SET tracked=? WHERE cid=?",
            (tracked, cid),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, f"카테고리 cid={cid} 없음")
    return {"cid": cid, "tracked": tracked}


@router.post("/run")
def run_pipeline(
    background: BackgroundTasks, user: dict = Depends(current_user)
):
    """풀 파이프라인 실행. 이미 running 인 run 이 있으면 409."""
    with get_db() as conn:
        active = conn.execute(
            """SELECT id FROM pa_discovery_runs
               WHERE status='running'
               ORDER BY started_at DESC LIMIT 1"""
        ).fetchone()
        if active:
            raise HTTPException(
                409, f"이미 실행 중인 디스커버리 run 이 있습니다 (id={active['id']})"
            )
        cur = conn.execute(
            """INSERT INTO pa_discovery_runs (status, current_stage)
               VALUES ('running', 'init')"""
        )
        run_id = cur.lastrowid
    background.add_task(discovery_pipeline.run_discovery_pipeline, run_id)
    return {"run_id": run_id}


@router.get("/status")
def get_status(user: dict = Depends(current_user)):
    """가장 최근 run 의 상태."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM pa_discovery_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None
