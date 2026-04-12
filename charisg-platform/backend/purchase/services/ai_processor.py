"""PA 전용 AI 처리 파이프라인 — 번역 + SEO + 카테고리 + 상세페이지 HTML 생성."""
import json
import logging

from backend.purchase.database import get_db
from backend.purchase.services.exchange_rate_service import get_current_rate
from backend_shared.ai import translate_text, generate_seo, map_category
from backend_shared.detail_page_service import SECTION_HTML, _build_html

logger = logging.getLogger(__name__)

DEFAULT_SECTIONS = [
    {"id": "header", "enabled": True},
    {"id": "gallery", "enabled": True},
    {"id": "specs", "enabled": True},
    {"id": "features", "enabled": True},
    {"id": "customs", "enabled": True},
    {"id": "policy", "enabled": True},
    {"id": "faq", "enabled": True},
    {"id": "cs", "enabled": True},
    {"id": "footer", "enabled": True},
]


def _adapt_pa_to_detail_page(row: dict, title_ko: str, description_ko: str | None) -> dict:
    """PA products row → _build_html 이 내부에서 _extract_variables 로 처리할 dict 변환."""
    sale_price = row.get("sale_price_krw")
    if sale_price is None or sale_price == "":
        cost_usd = row.get("cost_usd")
        if cost_usd is not None and cost_usd != "":
            sale_price = int(float(cost_usd) * get_current_rate())
        else:
            sale_price = None

    return {
        "id": row["id"],
        "product_name_kr": title_ko,
        "product_name_processed": title_ko,
        "product_name": row.get("title_en") or "",
        "description_kr": description_ko or "",
        "description": row.get("description_en") or "",
        "image_url": "",
        "images_processed": "[]",
        "specs": row.get("specs_json") or "{}",
        "calculated_price": sale_price,
        "source_price": row.get("cost_usd"),
        "category_mapped": row.get("category_path") or "",
        "brand": row.get("brand") or "",
    }


def _save_detail_page_pa(product_id: int, html: str, sections_json: str,
                          market: str, platform: str) -> int:
    """PA database.get_db()로 detail_pages 저장. 동일 product+platform → UPDATE."""
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM detail_pages WHERE product_id=? AND platform=?",
            (product_id, platform),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE detail_pages
                   SET html_content=?, sections=?, market=?, status='draft',
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (html, sections_json, market, existing["id"]),
            )
            return existing["id"]
        cur = conn.execute(
            """INSERT INTO detail_pages
               (product_id, sections, html_content, market, platform, status)
               VALUES (?,?,?,?,?,?)""",
            (product_id, sections_json, html, market, platform, "draft"),
        )
        return cur.lastrowid


async def process_product(product_id: int, platform: str = "smartstore") -> dict:
    """단일 상품 AI 처리 파이프라인."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not row:
        raise ValueError(f"product {product_id} 없음")
    row = dict(row)

    title_en = row.get("title_en") or ""
    if not title_en:
        raise ValueError(f"product {product_id}: title_en 없음 — 번역 불가")

    # 1. 번역
    tr_title = await translate_text(title_en, "en", "ko")
    title_ko = tr_title["translated"]

    description_en = row.get("description_en") or ""
    description_ko = None
    if description_en:
        tr_desc = await translate_text(description_en, "en", "ko")
        description_ko = tr_desc["translated"]

    # 2. SEO
    seo_result = await generate_seo(
        product_name=title_ko,
        category=row.get("category_path") or "",
        market="KR",
        platform=platform,
        description=description_ko or "",
    )
    seo_title = seo_result.get("optimized_title") or title_ko
    seo_tags_list = seo_result.get("tags") or seo_result.get("keywords") or []
    seo_tags = json.dumps(seo_tags_list, ensure_ascii=False) if seo_tags_list else "[]"

    # 3. 카테고리 매핑
    cat_result = await map_category(
        product_name=title_ko,
        source_category=row.get("category_path") or "",
        target_platform=platform,
    )
    mapped_category = cat_result.get("mapped_category") or row.get("category_path") or ""

    # 4. 어댑터 → HTML 생성
    adapted = _adapt_pa_to_detail_page(row, title_ko, description_ko)
    adapted["category_mapped"] = mapped_category
    html = _build_html(adapted, DEFAULT_SECTIONS, "KR")

    # 5. products 테이블 업데이트
    with get_db() as conn:
        conn.execute(
            """UPDATE products SET
                   title_ko=?, description_ko=?, seo_title=?, seo_tags=?,
                   category_path=COALESCE(?, category_path),
                   ai_processed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (title_ko, description_ko, seo_title, seo_tags, mapped_category, product_id),
        )

    # 6. detail_pages 저장
    sections_json = json.dumps([s["id"] for s in DEFAULT_SECTIONS if s["enabled"]])
    detail_page_id = _save_detail_page_pa(product_id, html, sections_json, "KR", platform)

    return {
        "product_id": product_id,
        "title_ko": title_ko,
        "seo_title": seo_title,
        "seo_tags": seo_tags_list,
        "category": mapped_category,
        "html_length": len(html),
        "detail_page_id": detail_page_id,
    }


async def process_batch(product_ids: list[int], platform: str = "smartstore"):
    """여러 상품 순차 AI 처리. 건별 결과를 yield하는 async generator (SSE용)."""
    total = len(product_ids)
    processed = 0
    errors = 0

    for i, pid in enumerate(product_ids, 1):
        try:
            result = await process_product(pid, platform)
            processed += 1
            yield {
                "current": i,
                "total": total,
                "pct": round(i / total * 100, 1),
                "product_id": pid,
                "title_ko": result.get("title_ko", ""),
                "status": "ok",
            }
        except Exception as e:
            errors += 1
            logger.warning(f"[ai-processor] product {pid} 실패: {e}")
            yield {
                "current": i,
                "total": total,
                "pct": round(i / total * 100, 1),
                "product_id": pid,
                "status": "error",
                "message": str(e),
            }

    yield {
        "event": "done",
        "processed": processed,
        "errors": errors,
        "total": total,
    }
