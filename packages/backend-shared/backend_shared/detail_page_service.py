"""detail_page_service.py — [RETIRED 2026-08-07] 호환 shim.

원본(358줄, SECTION_HTML 12섹션 + detail_templates 테이블 연동)은
/home/ubuntu/_retired_20260807/detail_page_service.py.ORIGINAL_untracked 에 보관.

폐기 근거:
  · detail_templates 테이블 = 0행 → _load_sections 가 항상 하드코딩 default 반환
  · 전제였던 프론트 Templates.jsx(SECTION_HTML_MAP) = 코드베이스에 없음
  · detail_pages 15만 행의 sections 값이 전부 ai_processor 의 PA_SECTIONS
    (["auth","shipping","gallery","amazon_notice","notice"]) → 이 엔진 산출물 0행
  · 유일한 실호출처였던 group_lister.ensure_promoted 는 ai_processor 로 전환됨

이 shim 은 구동 중인 charisg-pa-api(재시작 금지 규칙) 가 메모리에 들고 있는 옛
group_lister 코드가 런타임에 이 모듈을 lazy import 할 때 ImportError 로 죽지 않게
받아주는 용도다. 서비스가 다음에 재시작되면 참조가 사라지므로 파일째 삭제 가능.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def generate_detail_page(
    product: dict,
    template_id: Optional[int] = None,
    market: str = "KR",
    platform: str = "smartstore",
) -> dict:
    """[shim] 실사용 엔진(ai_processor._build_pa_html)으로 위임."""
    from backend.purchase.services.ai_processor import ensure_detail_html

    product_id = product["id"]
    logger.info(f"[dps-shim] {product_id} -> ai_processor.ensure_detail_html 위임")
    detail_page_id = ensure_detail_html(product_id, platform=platform)
    return {
        "product_id": product_id,
        "detail_page_id": detail_page_id,
        "platform": platform,
        "shim": True,
    }
