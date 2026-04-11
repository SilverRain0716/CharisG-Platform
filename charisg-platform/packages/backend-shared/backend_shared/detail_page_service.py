"""
detail_page_service.py — 상세페이지 생성 엔진
템플릿 변수 바인딩 → HTML 섹션 조립 → detail_pages 테이블 저장

기존 Templates.jsx의 SECTION_HTML_MAP과 동일한 섹션 ID 사용
"""
import json
import logging
from typing import Optional

from backend_shared.context import get_db

logger = logging.getLogger(__name__)

# ── 섹션 HTML 맵 (프론트 SECTION_HTML_MAP과 동기화)
SECTION_HTML = {
    "header": """<div style="background:#111;padding:32px 20px;text-align:center;border-radius:12px 12px 0 0">
  <p style="color:#E8845A;font-size:10px;letter-spacing:4px;margin:0 0 10px">GLOBAL SOURCING</p>
  <h1 style="color:#fff;font-size:22px;font-weight:700;margin:0 0 12px;line-height:1.4">{{product_name}}</h1>
  <div style="display:inline-flex;gap:8px;flex-wrap:wrap;justify-content:center">
    <span style="background:#E8845A;color:#fff;font-size:11px;padding:4px 10px;border-radius:20px">✈️ 해외직배송</span>
    <span style="background:#1d9e6f;color:#fff;font-size:11px;padding:4px 10px;border-radius:20px">🛡️ 품질보증</span>
  </div></div>""",

    "gallery": """<div style="background:#fff;padding:12px;border-radius:8px;margin-bottom:20px">
  <img src="{{main_image}}" style="width:100%;border-radius:8px" alt="{{product_name}}" /></div>""",

    "specs": """<div style="background:#faf8f5;padding:12px;border-radius:10px;margin:0 8px 8px">
  <div style="font-size:11px;font-weight:700;text-align:center;margin-bottom:10px">📊 핵심 스펙</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
    {{specs_grid}}
  </div></div>""",

    "features": """<div style="padding:20px">
  <h3 style="font-size:16px;font-weight:700;margin-bottom:12px">✨ 상품 특징</h3>
  <ul style="padding-left:20px;line-height:1.8">{{features_list}}</ul></div>""",

    "table": """<div style="padding:16px">
  <table style="width:100%;border-collapse:collapse;font-size:13px">
    {{spec_table_rows}}
  </table></div>""",

    "customs": """<div style="background:#fff3e0;padding:16px;border-radius:8px;margin:16px">
  <strong>🔑 개인통관고유부호 안내</strong>
  <p style="margin:8px 0 0;font-size:13px;color:#555">해외 직배송 상품 수령을 위해 개인통관고유부호가 필요합니다.</p></div>""",

    "policy": """<div style="padding:16px">
  <h4 style="font-size:14px;font-weight:700;margin-bottom:8px">📋 반품/환불 정책</h4>
  <p style="font-size:12px;color:#555;line-height:1.6">• 단순변심: 수령 후 7일, 왕복 배송비 고객 부담<br>• 불량/오배송: 30일 이내, 100% 환불</p></div>""",

    "faq": """<div style="padding:16px">
  <h4 style="font-size:14px;font-weight:700;margin-bottom:8px">❓ 자주 묻는 질문</h4>
  <details style="margin-bottom:6px"><summary style="cursor:pointer;font-size:13px">배송 기간은 얼마나 걸리나요?</summary>
    <p style="font-size:12px;color:#666;padding:8px 0">주문 후 영업일 기준 7~14일 소요됩니다.</p></details>
  <details style="margin-bottom:6px"><summary style="cursor:pointer;font-size:13px">관부가세가 부과되나요?</summary>
    <p style="font-size:12px;color:#666;padding:8px 0">$150 이하 구매 시 관부가세가 면제됩니다.</p></details></div>""",

    "cs": """<div style="text-align:center;padding:20px">
  <a href="#" style="display:inline-block;background:#1d9e6f;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-size:14px;font-weight:600">💬 문의하기</a></div>""",

    "caution": """<div style="background:#f5f5f5;padding:16px;border-radius:8px;margin:16px">
  <strong>⚠️ 구매 전 주의사항</strong>
  <p style="font-size:12px;color:#666;margin:8px 0 0;line-height:1.6">• 모니터에 따라 색상 차이가 있을 수 있습니다.<br>• 해외 상품 특성상 제품 포장이 다를 수 있습니다.</p></div>""",

    "footer": """<div style="text-align:center;padding:20px;color:#999;font-size:11px">© Charis G · 해외직배송 전문</div>""",
}



def generate_detail_page(
    product: dict,
    template_id: Optional[int] = None,
    market: str = "KR",
    platform: str = "smartstore",
) -> dict:
    """
    상세페이지 생성

    1. 템플릿에서 섹션 구성 로드 (또는 기본값)
    2. 상품 데이터 → 변수 바인딩
    3. 섹션 조립 → HTML 생성
    4. detail_pages 테이블 저장

    Returns: {"product_id": int, "html": str, "detail_page_id": int}
    """
    # 섹션 구성 로드
    sections_config = _load_sections(template_id)

    html = _build_html(product, sections_config, market)

    # DB 저장
    detail_page_id = _save_detail_page(product["id"], template_id, market, platform, html)

    # collected_products 상태 업데이트
    with get_db() as conn:
        conn.execute(
            "UPDATE collected_products SET detail_page_done=1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (product["id"],),
        )

    return {
        "product_id": product["id"],
        "detail_page_id": detail_page_id,
        "platform": platform,
        "html": html,
        "sections_count": len([s for s in sections_config if s.get("enabled", True)]),
    }


def _build_html(product: dict, sections_config: list, market: str) -> str:
    """HTML 상세페이지 조립"""
    variables = _extract_variables(product, market)
    parts = []

    # 래퍼 시작
    parts.append('<div style="max-width:860px;margin:0 auto;font-family:\'Noto Sans KR\',sans-serif">')

    for section in sections_config:
        if not section.get("enabled", True):
            continue

        section_id = section["id"]
        template = SECTION_HTML.get(section_id, "")
        if not template:
            continue

        # 변수 바인딩
        rendered = _bind_variables(template, variables)
        parts.append(rendered)

    parts.append("</div>")
    return "\n".join(parts)


def _extract_variables(product: dict, market: str) -> dict:
    """상품 데이터에서 템플릿 변수 추출"""
    name = product.get("product_name_processed") or product.get("product_name_kr") or product.get("product_name", "")
    description = product.get("description_kr") or product.get("description", "")

    # 이미지
    main_image = product.get("image_url", "")
    processed_images = []
    try:
        processed_images = json.loads(product.get("images_processed", "[]"))
    except (json.JSONDecodeError, TypeError):
        pass
    if processed_images:
        main_image = processed_images[0] if processed_images[0].startswith("http") else main_image

    # 스펙 파싱
    specs = {}
    try:
        specs = json.loads(product.get("specs", "{}"))
    except (json.JSONDecodeError, TypeError):
        pass

    # 스펙 그리드 HTML
    specs_grid = ""
    for key, val in list(specs.items())[:4]:
        specs_grid += f'<div style="background:#fff;border-radius:8px;padding:10px;text-align:center"><div style="font-size:10px;color:#888">{key}</div><div style="font-size:13px;font-weight:600">{val}</div></div>'

    # 스펙 테이블 rows
    spec_rows = ""
    for key, val in specs.items():
        spec_rows += f'<tr><td style="padding:8px;border:1px solid #eee;background:#fafafa;font-weight:600;width:30%">{key}</td><td style="padding:8px;border:1px solid #eee">{val}</td></tr>'

    # features
    features_list = ""
    features_text = ""
    if description:
        sentences = [s.strip() for s in description.split(".") if s.strip()][:5]
        for s in sentences:
            features_list += f"<li>{s}</li>"
            features_text += f"• {s}\n"

    # 가격
    price = product.get("calculated_price") or product.get("source_price", 0)
    currency = "₩" if market == "KR" else "$"

    return {
        "product_name": name,
        "main_image": main_image,
        "description": description,
        "price": f"{currency}{int(price):,}" if price else "",
        "category": product.get("category_mapped") or product.get("category", ""),
        "brand": product.get("brand", ""),
        "specs_grid": specs_grid or '<div style="background:#fff;border-radius:8px;padding:10px;text-align:center">상세 스펙은 상품 문의를 이용해주세요</div>',
        "spec_table_rows": spec_rows or '<tr><td style="padding:8px;border:1px solid #eee" colspan="2">상세 스펙은 판매자에게 문의해주세요</td></tr>',
        "features_list": features_list or "<li>상세 정보는 이미지를 참고해주세요</li>",
        "features_text": features_text or "• Please refer to images for details",
        "specs_text": "\n".join(f"• {k}: {v}" for k, v in specs.items()) or "• See images for specifications",
    }


def _bind_variables(template: str, variables: dict) -> str:
    """{{variable}} → 실제 값으로 치환"""
    result = template
    for key, value in variables.items():
        result = result.replace(f"{{{{{key}}}}}", str(value))
    return result


def _load_sections(template_id: Optional[int]) -> list:
    """템플릿 섹션 구성 로드"""
    if template_id:
        with get_db() as conn:
            row = conn.execute("SELECT sections_config FROM templates WHERE id = ?", (template_id,)).fetchone()
            if row:
                try:
                    return json.loads(row["sections_config"])
                except (json.JSONDecodeError, TypeError):
                    pass

    # 활성 템플릿 찾기
    with get_db() as conn:
        row = conn.execute("SELECT sections_config FROM templates WHERE is_active = 1 LIMIT 1").fetchone()
        if row:
            try:
                return json.loads(row["sections_config"])
            except (json.JSONDecodeError, TypeError):
                pass

    # 기본값
    return [
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


def _save_detail_page(product_id: int, template_id: Optional[int], market: str, platform: str, html: str) -> int:
    """detail_pages 테이블 저장 (기존 것 있으면 업데이트)"""
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM detail_pages WHERE product_id = ? AND platform = ?",
            (product_id, platform),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE detail_pages SET html_content=?, template_id=?, status='draft', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (html, template_id, existing["id"]),
            )
            return existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO detail_pages (product_id, template_id, market, platform, html_content, status) VALUES (?,?,?,?,?,?)",
                (product_id, template_id, market, platform, html, "draft"),
            )
            return cur.lastrowid
