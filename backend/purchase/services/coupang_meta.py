"""
coupang_meta.py — 쿠팡 카테고리 메타 정보 조회 + 메모리 캐시.

상품 등록 시 노출되는 categoryGroupName / noticeCategoryName / required attributes를
displayCategoryCode 기준으로 메타 API에서 동적 조회한다.

Phase D-2 (_run_coupang_upload_bg)에서 카테고리별 prefetch.
"""
import logging
from typing import Optional

import requests

from backend.purchase.services.coupang_service import _signature, BASE

logger = logging.getLogger(__name__)


# ── 메모리 캐시 (process 내) ──────────────────
_META_CACHE: dict[str, dict] = {}


def get_category_meta(display_category_code: str) -> Optional[dict]:
    """카테고리 메타 정보 조회 + 캐시.

    응답 예 (요약):
        {
            "displayCategoryCode": "50004540",
            "displayCategoryName": "...",
            "noticeCategories": [{"noticeCategoryName": ..., "noticeCategoryDetailNames": [...]}],
            "attributes": [{"attributeTypeName": ..., "dataType": "STRING|ENUM", "required": true, ...}],
            "requiredDocumentNames": [...],
            "allowedOfferConditions": [...]
        }

    캐시 키: display_category_code
    """
    code = str(display_category_code)
    if code in _META_CACHE:
        return _META_CACHE[code]

    path = f"/v2/providers/seller_api/apis/api/v1/marketplace/meta/category-related-metas/display-category-codes/{code}"
    try:
        r = requests.get(BASE + path, headers=_signature("GET", path), timeout=15)
        if r.status_code >= 400:
            logger.warning(f"쿠팡 카테고리 메타 조회 실패 code={code}: {r.status_code} {r.text[:200]}")
            return None
        body = r.json()
        data = body.get("data", body) if isinstance(body, dict) else body
        if isinstance(data, dict):
            _META_CACHE[code] = data
            return data
        logger.warning(f"쿠팡 카테고리 메타 응답 형식 예외 code={code}: {body}")
        return None
    except Exception as e:
        logger.error(f"쿠팡 카테고리 메타 예외 code={code}: {e}")
        return None


def get_required_attributes(display_category_code: str) -> list[dict]:
    """카테고리의 required=True attribute list 만 반환.

    각 attribute: {"attributeTypeName", "dataType", "required", "basicUnits"(ENUM 값들)}
    """
    meta = get_category_meta(display_category_code)
    if not meta:
        return []
    attrs = meta.get("attributes") or []
    # ★2026-08-12 수정: API 는 required 를 문자열 'MANDATORY'/'OPTIONAL' 로 준다.
    #   기존 `is True` 비교는 절대 참이 될 수 없어 이 함수가 **항상 빈 리스트**를 반환했다.
    #   같은 저장소 안에서도 group_lister:3241 은 == "MANDATORY" 로 맞게 비교한다.
    #   실피해는 없었다 — 소비자였던 ai_fill_mandatory(→_get_or_compute_mandatory_attrs)
    #   와 required_names(3236) 가 둘 다 호출부 없는 죽은 코드였고, 살아있는 채움 경로는
    #   coupang_attributes.build_required_attributes(meta, ...) 로 meta 를 직접 읽는다.
    #   그래도 고친다 — 쓰는 쪽이 조용히 빈 리스트를 받는 지뢰다.
    return [a for a in attrs if isinstance(a, dict)
            and str(a.get("required")).strip().upper() == "MANDATORY"]


def extract_notice_category_names(meta: dict) -> list[str]:
    """카테고리고시(noticeCategoryName) 목록 추출."""
    notices = meta.get("noticeCategories") or meta.get("notices") or []
    names = []
    for n in notices:
        if isinstance(n, dict):
            name = n.get("noticeCategoryName") or n.get("categoryGroupName")
            if name:
                names.append(name)
        elif isinstance(n, str):
            names.append(n)
    return names


def build_default_notices(meta: dict, fallback_value: str = "상품 상세페이지 참조") -> list[dict]:
    """카테고리고시 페이로드를 디폴트값으로 일괄 생성.

    ⚠️ 카테고리가 여러 noticeCategoryName을 제공하는 경우(예: '어린이제품' + '스포츠용품'),
       쿠팡 validator는 정확히 ONE 선택을 요구(2 subschemas matched 에러). 따라서
       **첫 번째 noticeCategory 만** 사용한다. 추후 카테고리별 매핑 규칙이 필요하면
       이 함수를 확장.

    응답: [{"noticeCategoryName": ..., "noticeCategoryDetailName": ..., "content": fallback_value}, ...]
    """
    notices = meta.get("noticeCategories") or []
    if not notices:
        return []
    first = notices[0]
    # ★ '소형전자(MP3/전자사전 등)'는 KC인증·정격전압 등 전자제품 전용필드 요구 → 비전자 카테고리(타이어킷 등)에
    #   첫번째로 들어가도 쿠팡 등록 거부(2026-06-21 ARB 타이어수리킷 cat 78914 실측). 대안 있으면 '기타 재화' 우선.
    if isinstance(first, dict) and (first.get("noticeCategoryName") or "").startswith("소형전자") and len(notices) > 1:
        _alt = next((n for n in notices if isinstance(n, dict) and n.get("noticeCategoryName") == "기타 재화"), None) \
            or next((n for n in notices if isinstance(n, dict) and not (n.get("noticeCategoryName") or "").startswith("소형전자")), None)
        if _alt:
            first = _alt
    if not isinstance(first, dict):
        return []
    cat_name = first.get("noticeCategoryName")
    if not cat_name:
        return []
    result = []
    for d in first.get("noticeCategoryDetailNames") or []:
        detail_name = d.get("noticeCategoryDetailName") if isinstance(d, dict) else d
        if detail_name:
            result.append({
                "noticeCategoryName": cat_name,
                "noticeCategoryDetailName": detail_name,
                "content": fallback_value,
            })
    return result


def cache_size() -> int:
    """현재 캐시된 카테고리 수."""
    return len(_META_CACHE)


def clear_cache() -> None:
    """캐시 초기화 (테스트용)."""
    _META_CACHE.clear()
