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

    "amazon_notice": """<div style="max-width:860px;margin:20px auto;font-family:'Noto Sans KR',sans-serif">
  <!-- 아마존 상품 안내 헤더 -->
  <div style="background:linear-gradient(135deg,#232F3E 0%,#37475A 100%);border-radius:16px 16px 0 0;padding:36px 32px;text-align:center">
    <div style="margin-bottom:16px">
      <span style="font-size:32px;font-weight:800;color:#FF9900;letter-spacing:-1px">amazon</span>
      <span style="display:block;width:60px;height:4px;background:#FF9900;border-radius:2px;margin:4px auto 0"></span>
    </div>
    <div style="display:inline-block;background:rgba(255,153,0,0.15);border:2px solid rgba(255,153,0,0.4);border-radius:30px;padding:8px 24px;font-size:18px;font-weight:700;color:#fff;margin-bottom:12px">ℹ️ 아마존 상품 안내</div>
    <p style="font-size:20px;font-weight:600;color:#fff;line-height:1.6;margin:0">
      아마존 글로벌 스토어에서 판매 중인 상품으로<br>
      공식 판매자인 <span style="color:#FF9900;font-weight:800">아마존 미국</span>에서 판매/배송을 책임집니다.
    </p>
  </div>

  <!-- 안내 카드들 -->
  <div style="background:#fff;padding:24px 28px;border-left:3px solid #FF9900;border-right:3px solid #FF9900">
    <!-- 파손/오배송 안내 -->
    <div style="background:#FFF8F0;border-radius:12px;padding:20px 24px;margin-bottom:16px;border:1px solid #FFE0B2">
      <div style="font-size:15px;font-weight:700;color:#232F3E;margin-bottom:8px">
        <span style="font-size:18px;color:#FF9900;font-weight:800;margin-right:6px">amazon</span> 📦 파손 / 오배송 안내
      </div>
      <p style="font-size:14px;color:#555;line-height:1.7;margin:0">
        받아보신 상품이 파손 또는 오배송 된 경우 <span style="color:#FF9900;font-weight:600;text-decoration:underline;text-underline-offset:3px">1:1 문의</span>로 사진을 첨부해 주시면<br>
        불편함 없이 환불받으실 수 있도록 도움드리겠습니다.
      </p>
    </div>

    <!-- 해외상품 특성 안내 -->
    <div style="background:#F5F8FC;border-radius:12px;padding:20px 24px;margin-bottom:16px;border:1px solid #D6E4F0">
      <div style="font-size:15px;font-weight:700;color:#232F3E;margin-bottom:8px">
        <span style="font-size:18px;color:#FF9900;font-weight:800;margin-right:6px">amazon</span> 🌍 해외 상품 안내
      </div>
      <p style="font-size:14px;color:#555;line-height:1.7;margin:0">
        미국 등 해외에서 판매하는 상품으로 <span style="color:#D94040;font-weight:600">사이즈, 사용 연령, 언어, AS</span> 등의 기준이 국내 제품과 다를 수 있습니다.
      </p>
    </div>

    <!-- 취소 및 반품 -->
    <div style="background:#F0FAF5;border-radius:12px;padding:20px 24px;margin-bottom:16px;border:1px solid #B8E6D0">
      <div style="font-size:15px;font-weight:700;color:#232F3E;margin-bottom:8px">
        <span style="font-size:18px;color:#FF9900;font-weight:800;margin-right:6px">amazon</span> 🔄 취소 및 반품
      </div>
      <ul style="font-size:14px;color:#555;line-height:1.8;margin:0;padding-left:18px">
        <li>결제 완료 후 바로 취소, <span style="color:#D94040;font-weight:600">배송 완료 후 7일 이내</span> 반품 가능</li>
        <li>이후 고객센터를 통해 배송 완료 기준 <span style="font-weight:600">최대 30일 이전</span>까지 반품 신청이 가능합니다.</li>
        <li>상품에 하자가 있는 경우 아마존 한국 고객센터(<span style="color:#FF9900;font-weight:700">1566-7171</span>/유료)로 연락주세요.</li>
      </ul>
      <div style="background:#E8F5E9;border-radius:8px;padding:12px 16px;margin-top:12px;font-size:13px;color:#666">
        ℹ️ 배송 규격에 따라서 상품이 분리 배송될 수 있습니다. 분리 배송된 상품을 고객 사유로 반품할 경우, 박스 별 반품 배송비가 부과될 수 있습니다.
      </div>
    </div>

    <!-- 기타 -->
    <div style="background:#F9F9F9;border-radius:12px;padding:16px 24px;border:1px solid #E0E0E0">
      <p style="font-size:13px;color:#888;line-height:1.7;margin:0">
        <span style="font-size:15px;color:#FF9900;font-weight:800;margin-right:4px">amazon</span>
        아마존 미국이 판매하는 상품에 대해서는 아마존 글로벌 스토어 판매 조건이 적용됩니다.
      </p>
    </div>
  </div>
  <div style="height:4px;background:linear-gradient(90deg,#FF9900,#232F3E);border-radius:0 0 16px 16px"></div>
</div>""",

    "policy": """<div style="max-width:860px;margin:16px auto;font-family:'Noto Sans KR',sans-serif">
  <div style="background:#fff;border-radius:12px;border:1px solid #eee;overflow:hidden">
    <div style="background:#F7F5F0;padding:16px 24px;border-bottom:2px solid #E8845A">
      <span style="font-size:16px;font-weight:700;color:#1B3A5C">📋 반품 / 교환 기준</span>
    </div>
    <div style="padding:20px 24px">
      <p style="font-size:14px;color:#555;line-height:1.7;margin:0 0 16px">
        상품 수령 후 <span style="color:#D94040;font-weight:600">7일 이내</span>에 신청하실 수 있습니다.
        단, 제품이 표시·광고 내용과 다르거나 계약과 다르게 이행된 날부터 <span style="font-weight:600">30일 이내</span>에 교환/반품이 가능합니다.
      </p>

      <div style="font-size:14px;font-weight:700;color:#1B3A5C;margin-bottom:8px">반품 / 교환 불가 사유</div>
      <ul style="font-size:13px;color:#555;line-height:1.8;margin:0 0 16px;padding-left:18px">
        <li>소비자의 책임 있는 사유로 상품 등이 멸실 또는 훼손된 경우 (단 상품 확인을 위한 포장 훼손 제외)</li>
        <li>소비자의 사용 또는 소비에 의해 상품 등의 가치가 현저히 감소한 경우</li>
        <li>시간 경과에 의해 재판매가 곤란할 정도로 상품 등의 가치가 현저히 감소한 경우</li>
        <li>복제가 가능한 상품 등의 포장을 훼손한 경우</li>
        <li>소비자의 주문에 따라 개별적으로 생산되는 상품이 제작에 들어간 경우</li>
      </ul>

      <div style="font-size:14px;font-weight:700;color:#1B3A5C;margin-bottom:8px">저온/신선 상품</div>
      <ul style="font-size:13px;color:#555;line-height:1.8;margin:0;padding-left:18px">
        <li>고객 귀책 사유 (단순 변심, 주소 오기재, 주문 착오, 보관 부주의 및 상품 사용으로 가치가 하락한 경우 등)</li>
        <li>다른 옵션 상품으로 교환을 요청하는 경우</li>
      </ul>
    </div>
  </div>
</div>""",

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

    # collected_products 상태 업데이트 (DS 전용 테이블 — PA 환경엔 없음)
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE collected_products SET detail_page_done=1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (product["id"],),
            )
    except Exception:
        pass

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
    """템플릿 섹션 구성 로드. detail_templates 비어있거나 잘못된 JSON 이면 default."""
    try:
        if template_id:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT sections_template FROM detail_templates WHERE id = ?",
                    (template_id,),
                ).fetchone()
                if row:
                    try:
                        return json.loads(row["sections_template"])
                    except (json.JSONDecodeError, TypeError):
                        pass

        # 활성 템플릿 (detail_templates 에 is_active 컬럼 없음 — 첫 row 사용)
        with get_db() as conn:
            row = conn.execute(
                "SELECT sections_template FROM detail_templates ORDER BY id LIMIT 1"
            ).fetchone()
            if row:
                try:
                    return json.loads(row["sections_template"])
                except (json.JSONDecodeError, TypeError):
                    pass
    except Exception:
        pass  # 테이블/스키마 이슈 → default fallback

    # 기본값
    return [
        {"id": "header", "enabled": True},
        {"id": "gallery", "enabled": True},
        {"id": "specs", "enabled": True},
        {"id": "features", "enabled": True},
        {"id": "customs", "enabled": True},
        {"id": "amazon_notice", "enabled": True},
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
                """INSERT INTO detail_pages
                   (product_id, template_id, market, platform, html_content, status, sections)
                   VALUES (?,?,?,?,?,?,'[]')""",
                (product_id, template_id, market, platform, html, "draft"),
            )
            return cur.lastrowid
