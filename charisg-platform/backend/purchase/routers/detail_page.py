"""PA Detail Page — 13섹션 상세페이지 (AI 생성)."""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend_shared.ai import translate_text, generate_seo

router = APIRouter(prefix="/api/pa/detail-page", tags=["pa-detail-page"])

DEFAULT_SECTIONS = [
    "메인 비주얼", "후킹 카피", "주요 특장점",
    "구성품", "사용 방법", "스펙 표",
    "비교 표", "Q&A", "리뷰 캡처",
    "환경/안전", "관부가세 안내", "배송 안내", "교환/환불 정책",
]


@router.post("/{product_id}/generate")
async def generate(product_id: int, user: dict = Depends(current_user)):
    with get_db() as conn:
        row = conn.execute("SELECT title_en, title_ko FROM products WHERE id=?", (product_id,)).fetchone()
    if not row:
        raise HTTPException(404, "상품 없음")

    # 영문 → 한글 번역
    title_ko = row["title_ko"]
    if not title_ko and row["title_en"]:
        translated = await translate_text(row["title_en"], "en", "ko")
        title_ko = translated.get("translated", row["title_en"])

    sections = [{"label": s, "html": f"<h3>{s}</h3>", "order": i + 1}
                for i, s in enumerate(DEFAULT_SECTIONS)]

    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO detail_pages (product_id, sections, status)
               VALUES (?, ?, 'draft')""",
            (product_id, json.dumps(sections, ensure_ascii=False)),
        )
        if title_ko != row["title_ko"]:
            conn.execute(
                "UPDATE products SET title_ko=? WHERE id=?",
                (title_ko, product_id),
            )
    return {"ok": True, "id": cur.lastrowid, "sections_count": len(sections)}


class UpdateSection(BaseModel):
    sections: list


@router.put("/{product_id}")
def update(product_id: int, body: UpdateSection, user: dict = Depends(current_user)):
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO detail_pages (id, product_id, sections, status)
               SELECT id, product_id, ?, status FROM detail_pages
               WHERE product_id=? ORDER BY id DESC LIMIT 1""",
            (json.dumps(body.sections, ensure_ascii=False), product_id),
        )
    return {"ok": True}
