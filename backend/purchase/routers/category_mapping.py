"""
category_mapping.py — 키워드→카테고리 매핑 + 검토 큐 admin API.

엔드포인트:
  GET    /api/pa/category-map              매핑 목록
  POST   /api/pa/category-map              새 매핑 추가 (수동)
  PUT    /api/pa/category-map/{id}         매핑 수정
  DELETE /api/pa/category-map/{id}         삭제
  POST   /api/pa/category-map/lookup       키워드 lookup (소싱 import 시 사용)

  GET    /api/pa/category-review           검토 큐 목록 (status filter)
  PUT    /api/pa/category-review/{id}/approve  확정 (keyword_category_map 도 INSERT)
  PUT    /api/pa/category-review/{id}/reject   거부

  POST   /api/pa/category-map/search-naver     네이버 카테고리 검색 (수동 선택용)
  POST   /api/pa/category-map/search-coupang   쿠팡 카테고리 검색 (수동 선택용)
"""
from typing import Optional
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.database import get_db

router = APIRouter(prefix="/api/pa", tags=["pa-category-mapping"])


# ── 매핑 dict CRUD ─────────────────────────────────────
class MappingBody(BaseModel):
    keyword: str
    naver_category_id: Optional[str] = None
    naver_category_path: Optional[str] = None
    coupang_category_code: Optional[int] = None
    coupang_category_path: Optional[str] = None
    notes: Optional[str] = None


@router.get("/category-map")
def list_mappings(
    user: dict = Depends(current_user),
    source: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
):
    where = []
    params: list = []
    if source:
        where.append("source=?"); params.append(source)
    if keyword:
        where.append("keyword LIKE ?"); params.append(f"%{keyword.lower()}%")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT id, keyword, naver_category_id, naver_category_path,
                       coupang_category_code, coupang_category_path,
                       source, ai_naver_score, ai_coupang_score, notes,
                       created_at, updated_at
                FROM keyword_category_map {where_sql}
                ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
            tuple(params + [limit, offset]),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) c FROM keyword_category_map {where_sql}",
            tuple(params),
        ).fetchone()["c"]
    return {"items": [dict(r) for r in rows], "total": total}


@router.post("/category-map")
def create_mapping(body: MappingBody, user: dict = Depends(current_user)):
    keyword = (body.keyword or "").strip().lower()
    if not keyword:
        raise HTTPException(400, "keyword 필수")
    with get_db() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO keyword_category_map
                   (keyword, naver_category_id, naver_category_path,
                    coupang_category_code, coupang_category_path,
                    source, notes, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'manual', ?, datetime('now'))""",
                (keyword, body.naver_category_id, body.naver_category_path,
                 body.coupang_category_code, body.coupang_category_path, body.notes),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"이미 존재하는 키워드: {keyword}")
    return {"id": cur.lastrowid, "keyword": keyword}


@router.put("/category-map/{mapping_id}")
def update_mapping(mapping_id: int, body: MappingBody, user: dict = Depends(current_user)):
    keyword = (body.keyword or "").strip().lower()
    if not keyword:
        raise HTTPException(400, "keyword 필수")
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM keyword_category_map WHERE id=?", (mapping_id,),
        ).fetchone()
        if not existing:
            raise HTTPException(404, "매핑 없음")
        conn.execute(
            """UPDATE keyword_category_map SET
                  keyword=?, naver_category_id=?, naver_category_path=?,
                  coupang_category_code=?, coupang_category_path=?,
                  notes=?, source='verified', updated_at=datetime('now')
               WHERE id=?""",
            (keyword, body.naver_category_id, body.naver_category_path,
             body.coupang_category_code, body.coupang_category_path,
             body.notes, mapping_id),
        )
    return {"id": mapping_id, "keyword": keyword}


@router.delete("/category-map/{mapping_id}")
def delete_mapping(mapping_id: int, user: dict = Depends(current_user)):
    with get_db() as conn:
        cur = conn.execute("DELETE FROM keyword_category_map WHERE id=?", (mapping_id,))
    if cur.rowcount == 0:
        raise HTTPException(404, "매핑 없음")
    return {"deleted": cur.rowcount}


# ── 키워드 lookup (소싱 import 시 호출) ────────────────
class LookupBody(BaseModel):
    keyword: str
    product_name: str
    product_id: Optional[int] = None
    product_name_en: Optional[str] = None


@router.post("/category-map/lookup")
def lookup_keyword(body: LookupBody, user: dict = Depends(current_user)):
    """키워드 기반 카테고리 매핑 (캐시 → AI → review 큐)."""
    from backend.purchase.services.category_mapper import map_categories_for_keyword
    result = map_categories_for_keyword(
        keyword=body.keyword,
        product_name=body.product_name,
        product_id=body.product_id,
        product_name_en=body.product_name_en,
    )
    return result


# ── 검토 큐 ────────────────────────────────────────────
@router.get("/category-review")
def list_reviews(
    user: dict = Depends(current_user),
    status: str = "pending",
    limit: int = 100,
    offset: int = 0,
):
    where = []
    params: list = []
    if status and status != "all":
        where.append("status=?"); params.append(status)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT * FROM category_review_queue {where_sql}
                ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            tuple(params + [limit, offset]),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) c FROM category_review_queue {where_sql}",
            tuple(params),
        ).fetchone()["c"]
    return {"items": [dict(r) for r in rows], "total": total}


class ApproveBody(BaseModel):
    naver_id: Optional[str] = None
    naver_path: Optional[str] = None
    coupang_code: Optional[int] = None
    coupang_path: Optional[str] = None
    save_to_dict: bool = True   # 같은 키워드 재매핑 시 캐시 적용
    notes: Optional[str] = None


@router.put("/category-review/{review_id}/approve")
def approve_review(review_id: int, body: ApproveBody, user: dict = Depends(current_user)):
    """검토 항목 확정. save_to_dict=True 면 keyword_category_map 도 같이 INSERT/UPDATE."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM category_review_queue WHERE id=?", (review_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "review 항목 없음")
        if row["status"] != "pending":
            raise HTTPException(400, f"이미 처리됨: status={row['status']}")

        # 사용자 입력이 비어있으면 AI 추천 그대로
        naver_id = body.naver_id or row["ai_naver_id"]
        naver_path = body.naver_path or row["ai_naver_path"]
        coupang_code = body.coupang_code if body.coupang_code is not None else row["ai_coupang_code"]
        coupang_path = body.coupang_path or row["ai_coupang_path"]

        conn.execute(
            """UPDATE category_review_queue SET
                  status='approved',
                  approved_naver_id=?, approved_naver_path=?,
                  approved_coupang_code=?, approved_coupang_path=?,
                  reviewer=?, reviewed_at=datetime('now'),
                  notes=?
               WHERE id=?""",
            (naver_id, naver_path, coupang_code, coupang_path,
             user.get("username", ""), body.notes, review_id),
        )

        # products.category_path 업데이트 — 같은 parent 의 모든 형제까지 일괄 적용
        affected = 0
        if naver_id:
            if row["product_id"]:
                # 1) 해당 product
                cur = conn.execute(
                    "UPDATE products SET category_path=? WHERE id=?",
                    (naver_id, row["product_id"]),
                )
                affected += cur.rowcount
                # 2) 같은 parent 의 형제 (카테고리 비어있는 것만)
                p = conn.execute(
                    "SELECT parent_asin FROM products WHERE id=?", (row["product_id"],),
                ).fetchone()
                if p and p["parent_asin"]:
                    cur = conn.execute(
                        """UPDATE products SET category_path=?
                           WHERE parent_asin=?
                             AND (category_path IS NULL OR category_path='')""",
                        (naver_id, p["parent_asin"]),
                    )
                    affected += cur.rowcount

        # keyword_category_map 캐시 INSERT
        if body.save_to_dict and row["keyword"]:
            conn.execute(
                """INSERT INTO keyword_category_map
                     (keyword, naver_category_id, naver_category_path,
                      coupang_category_code, coupang_category_path,
                      source, ai_naver_score, ai_coupang_score, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'verified', ?, ?, datetime('now'))
                   ON CONFLICT(keyword) DO UPDATE SET
                     naver_category_id=excluded.naver_category_id,
                     naver_category_path=excluded.naver_category_path,
                     coupang_category_code=excluded.coupang_category_code,
                     coupang_category_path=excluded.coupang_category_path,
                     source='verified', updated_at=datetime('now')""",
                (row["keyword"], naver_id, naver_path, coupang_code, coupang_path,
                 row["ai_naver_score"], row["ai_coupang_score"]),
            )
    return {"id": review_id, "status": "approved", "saved_to_dict": body.save_to_dict,
            "products_updated": affected}


class RejectBody(BaseModel):
    notes: Optional[str] = None


@router.put("/category-review/{review_id}/reject")
def reject_review(review_id: int, body: RejectBody, user: dict = Depends(current_user)):
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, status FROM category_review_queue WHERE id=?", (review_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "review 항목 없음")
        conn.execute(
            """UPDATE category_review_queue SET
                  status='rejected', reviewer=?, reviewed_at=datetime('now'), notes=?
               WHERE id=?""",
            (user.get("username", ""), body.notes, review_id),
        )
    return {"id": review_id, "status": "rejected"}


# ── 카테고리 검색 (수동 선택 시) ──────────────────────
@router.get("/category-map/search-naver")
def search_naver_categories(q: str, user: dict = Depends(current_user), limit: int = 20):
    """네이버 카테고리 LIKE 검색 (categories.db 의 naver_categories)."""
    if not q or not q.strip():
        return {"items": []}
    import os
    from backend_shared._config import PROJECT_ROOT
    db_path = os.environ.get("CATEGORY_DB_PATH", str(PROJECT_ROOT / "categories.db"))
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, name, whole_name FROM naver_categories
               WHERE whole_name LIKE ? AND is_leaf=1 LIMIT ?""",
            (f"%{q.strip()}%", limit),
        ).fetchall()
    finally:
        conn.close()
    return {"items": [{"id": r["id"], "name": r["name"], "path": r["whole_name"]} for r in rows]}


@router.get("/category-map/search-coupang")
def search_coupang_categories(q: str, user: dict = Depends(current_user), limit: int = 20):
    """쿠팡 카테고리 LIKE 검색 (purchase.db 의 coupang_categories)."""
    if not q or not q.strip():
        return {"items": []}
    with get_db() as conn:
        rows = conn.execute(
            """SELECT code, name, path FROM coupang_categories
               WHERE (name LIKE ? OR path LIKE ?)
                 AND is_leaf=1 AND status='ACTIVE' LIMIT ?""",
            (f"%{q.strip()}%", f"%{q.strip()}%", limit),
        ).fetchall()
    return {"items": [{"code": int(r["code"]), "name": r["name"], "path": r["path"]} for r in rows]}
