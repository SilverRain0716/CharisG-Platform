"""
amazon_listing_content_service.py — DS 전용 Amazon 리스팅 콘텐츠 AI 생성.

Phase C-0: collected_products → title/bullets/description/keywords/brand 생성.
Phase C 에서 putListingsItem 호출 시 이 결과를 attributes JSON 에 매핑한다.

설계 원칙 (CLAUDE.md 준수):
  - DS 전용: backend_shared.ai 의 PA-biased 함수(analyze_product 등)는 호출하지 않음.
  - 공용 저수준 (_call_ai_async, translate_text) 만 사용.
  - 모든 호출에 platform="amazon" / business_model="dropshipping" 명시.

Amazon Listings Items API 요구사항:
  - title:        200자 이내, 특수문자/promo 키워드 금지
  - bullet_points: 정확히 5개, 각 500자 이내
  - description:  2000자 이내, HTML 금지 (basic text)
  - search_terms: 250바이트 이내 (backend keywords)
  - brand_name:   필수 (CharisGlobal 고정)
"""
import json
import logging
import re
from typing import Optional

from backend.dropshipping.database import get_db
from backend_shared.ai.service import _call_ai_async, translate_text


def _clean_json(raw: str) -> str:
    """Gemini가 종종 생성하는 trailing comma 제거 (`, ]` → `]`)."""
    return re.sub(r",(\s*[}\]])", r"\1", raw)

logger = logging.getLogger(__name__)

BRAND_NAME = "CharisGlobal"


def _build_prompt(product_name_kr: str, category: str, amazon_category: str,
                  price_usd: float) -> str:
    return f"""You are an expert Amazon US FBM listing copywriter for a Korean dropshipping brand sourcing from China (CJ).

Generate a complete Amazon listing for the following product. Output **JSON only**, no prose, no markdown code fences.

[Source product]
Original name (Korean/mixed): {product_name_kr}
Source category: {category or "(unknown)"}
Target Amazon category: {amazon_category or "Everything Else"}
Retail price: ${price_usd:.2f}
Target marketplace: Amazon.com (US)
Brand: {BRAND_NAME}

[Amazon requirements — STRICT]
- title: 150-200 chars. Format: "{BRAND_NAME} [Product Type] [Key Spec] [Use Case] [Quantity/Size]". No ALL CAPS, no "!", no "best/amazing/premium" promo words, no emoji.
- bullets: EXACTLY 5 items. Each 150-250 chars. Lead with a capitalized 2-4 word benefit label followed by a colon, then the detailed benefit sentence. Use concrete specs.
- description: 800-1500 chars, plain text only, no HTML tags, 3-4 short paragraphs separated by blank lines. Focus on use cases + specs + care instructions.
- search_terms: space-separated English keywords, total < 240 bytes, no commas, no duplicates, no brand names of competitors. Include synonyms and long-tail variants.
- bullets must not repeat the title verbatim.
- description must not repeat bullets verbatim.
- Avoid any medical/safety/FDA claims. Avoid "cure", "treat", "prevent disease".

[Output schema]
{{
  "title": "...",
  "bullets": ["...", "...", "...", "...", "..."],
  "description": "...",
  "search_terms": "...",
  "brand": "{BRAND_NAME}"
}}

Remember: JSON only."""


async def generate_listing_content(product_id: int) -> dict:
    """
    상품 하나에 대해 Amazon 리스팅 콘텐츠 풀세트를 생성한다.

    Returns:
        {
          "product_id": int,
          "title": str,
          "bullets": [str, str, str, str, str],
          "description": str,
          "search_terms": str,
          "brand": str,
          "validation": {"ok": bool, "warnings": [...]},
          "raw_response": str (on parse failure),
        }
    """
    with get_db() as conn:
        row = conn.execute(
            """SELECT id, product_name, product_name_kr, category,
                      amazon_category, calculated_price, source_price
               FROM collected_products WHERE id=?""",
            (product_id,),
        ).fetchone()

    if not row:
        raise ValueError(f"collected_products id={product_id} 없음")

    name = row["product_name"] or row["product_name_kr"] or ""
    price = row["calculated_price"] or row["source_price"] or 0.0

    prompt = _build_prompt(
        product_name_kr=name,
        category=row["category"] or "",
        amazon_category=row["amazon_category"] or "Everything Else",
        price_usd=float(price),
    )

    logger.info(f"[listing-content] product_id={product_id} prompt_len={len(prompt)}")
    raw = await _call_ai_async(prompt, max_tokens=3000)

    if not raw:
        return {
            "product_id": product_id,
            "error": "AI 응답 없음 (Gemini 한도 초과 또는 에러)",
        }

    try:
        parsed = json.loads(_clean_json(raw))
    except json.JSONDecodeError as e:
        logger.error(f"[listing-content] JSON 파싱 실패: {e}")
        return {
            "product_id": product_id,
            "error": f"JSON parse error: {e}",
            "raw_response": raw[:2000],
        }

    parsed["product_id"] = product_id
    parsed.setdefault("brand", BRAND_NAME)
    parsed["validation"] = _validate(parsed)
    return parsed


def _validate(content: dict) -> dict:
    """Amazon Listings Items API 요구사항 검증."""
    warnings: list[str] = []

    title = content.get("title", "")
    if not title:
        warnings.append("title 비어있음")
    elif len(title) > 200:
        warnings.append(f"title 길이 {len(title)} > 200")
    elif len(title) < 80:
        warnings.append(f"title 길이 {len(title)} 짧음 (80↑ 권장)")

    bullets = content.get("bullets", [])
    if not isinstance(bullets, list) or len(bullets) != 5:
        warnings.append(f"bullets 개수 {len(bullets) if isinstance(bullets, list) else '?'} ≠ 5")
    else:
        for i, b in enumerate(bullets):
            if len(b) > 500:
                warnings.append(f"bullet {i+1} 길이 {len(b)} > 500")

    desc = content.get("description", "")
    if not desc:
        warnings.append("description 비어있음")
    elif len(desc) > 2000:
        warnings.append(f"description 길이 {len(desc)} > 2000")
    if "<" in desc or ">" in desc:
        warnings.append("description 에 HTML 태그 의심")

    search_terms = content.get("search_terms", "")
    if len(search_terms.encode("utf-8")) > 249:
        warnings.append(f"search_terms {len(search_terms.encode('utf-8'))}B > 249B")

    return {"ok": len(warnings) == 0, "warnings": warnings}


async def save_listing_content(product_id: int, content: dict) -> int:
    """
    생성된 콘텐츠를 listings 테이블에 저장 (없으면 INSERT, 있으면 UPDATE).
    Phase C 에서 이 레코드를 읽어 putListingsItem 호출 페이로드를 조립한다.
    """
    title = content.get("title", "")
    bullets = content.get("bullets", [])
    description = content.get("description", "")
    keywords = content.get("search_terms", "")

    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM listings WHERE product_id=? ORDER BY id DESC LIMIT 1",
            (product_id,),
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE listings SET title=?, bullets=?, description=?, keywords=?,
                                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (title, json.dumps(bullets, ensure_ascii=False),
                 description, keywords, existing["id"]),
            )
            return existing["id"]

        cur = conn.execute(
            """INSERT INTO listings (product_id, business_model, platform, tier,
                                     status, title, bullets, description, keywords)
               VALUES (?, 'dropship', 'amazon', 'tier2', 'candidate', ?, ?, ?, ?)""",
            (product_id, title, json.dumps(bullets, ensure_ascii=False),
             description, keywords),
        )
        return cur.lastrowid
