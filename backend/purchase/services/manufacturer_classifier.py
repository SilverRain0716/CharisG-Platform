"""manufacturer 한국 분류 v2 — Gemini 단독 + 한글 regex + whitelist 1차.

게이트(sourcing_promote, lister) 가 신규 brand 만났을 때 인라인 호출.

흐름:
  1. 한글 포함 검사 (0 호출, 100% 정확)
  2. KOREAN_WHITELIST 매칭 (0 호출, 영문 표기 한국 회사)
  3. Gemini knowledge 기반 판단 (사용자 3 조건)

v1 (Naver+Gemini) 의 50%+ false positive 사고 후 교체.

Returns dict {'is_korean': bool, 'confidence': float, 'reason': str} or None on failure.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


KOREAN_WHITELIST = {
    # 대기업
    "lg", "lg electronics", "lg display", "lg chem", "lg energy solution",
    "samsung", "samsung electronics", "samsung sdi", "samsung biologics",
    "hyundai", "hyundai motor", "hyundai mobis", "kia", "kia motors",
    "sk hynix", "sk telecom", "sk innovation", "sk biopharm",
    "posco", "kt", "naver corp", "kakao",
    # 식품/화학
    "cj cheiljedang", "cj corporation", "cj logistics", "cj olive",
    "lotte", "lotte chemical", "lotte chilsung",
    "nongshim", "ottogi", "orion",
    # 화장품
    "amorepacific", "amore pacific", "innisfree", "etude", "etude house",
    "dr. jart", "dr jart", "laneige", "sulwhasoo", "missha",
    "the face shop", "tony moly", "cosrx", "klairs",
    # 의류/액세서리
    "stylenanda", "musinsa", "8seconds", "spao",
    # 음식 브랜드
    "coupang", "baemin", "yogiyo",
    # 기타
    "doosan", "hanwha", "kogas", "hanmi",
}


PROMPT_TMPL = """다음 회사/브랜드 정보를 보고 한국 관련성을 판단해주세요.

Brand: {brand}

다음 중 **하나라도** 해당하면 한국 관련 (true):
1. 한국 회사 (본사가 한국에 있음)
2. 한국 브랜드 (한국에서 시작/만들어진 브랜드)
3. 한국 셀러가 amazon 에서 활발히 판매하는 브랜드 (한국 시장 공식 라이센스/대리점)

다음의 경우 false:
- 미국/유럽/일본/중국 등 다른 국가 명백한 회사 (Apple/Nike/Sony/Disney/Lego/PUMA 등)
- 글로벌 brand 인데 한국 셀러 활발성 명확히 없음
- 잘 모름/모호 = false (보수적)
- 약자/우연 일치 (예: 'KSIIA' 가 '한국반도체산업협회' 약자와 우연 일치하는 경우) = false

confidence 기준:
- 1.0: 매우 확실 (예: 본사가 명확히 한국)
- 0.7~0.9: 확실 (한국 brand 라는 강한 증거)
- 0.5~0.6: 약한 증거
- 0.0~0.4: 거의 모름 / 우연 일치

JSON 한 줄: {{"is_korean": true/false, "confidence": 0.0~1.0, "reason": "<15자 사유>"}}"""


def classify_korean_sync(brand: str) -> Optional[dict]:
    """매 brand 마다 호출.

    1차: 한글 포함 = 즉시 true
    2차: whitelist = 즉시 true
    3차: Gemini 호출
    """
    if not brand or not brand.strip():
        return None

    brand = brand.strip()

    # 1차: 한글 포함
    if re.search(r"[가-힣]", brand):
        return {"is_korean": True, "confidence": 1.0, "reason": "한글 포함"}

    # 2차: whitelist
    brand_lower = brand.lower()
    for kw in KOREAN_WHITELIST:
        if kw in brand_lower:
            return {"is_korean": True, "confidence": 1.0, "reason": f"whitelist:{kw}"}

    # 3차: Gemini
    try:
        from backend_shared.ai.service import _call_gemini
        prompt = PROMPT_TMPL.format(brand=brand)
        r = _call_gemini(prompt, 200, 3)
    except Exception as e:
        logger.warning(f"classify_korean_sync Gemini 호출 실패: brand={brand} err={e}")
        return None

    if not r:
        return None

    try:
        s = r.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        i, j = s.find("{"), s.rfind("}")
        if i < 0 or j <= i:
            return None
        obj = json.loads(s[i : j + 1])
        is_korean_raw = bool(obj.get("is_korean"))
        try:
            confidence = float(obj.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        reason = str(obj.get("reason", ""))[:30]

        # confidence < 0.7 인 한국 분류는 false positive 위험 → 보수적 외국 처리
        # (한국이 아닌데 약자/우연으로 한국으로 잘못 분류 회피)
        if is_korean_raw and confidence < 0.7:
            return {
                "is_korean": False,
                "confidence": confidence,
                "reason": f"low_conf {confidence:.2f}: {reason[:20]}",
            }
        return {
            "is_korean": is_korean_raw,
            "confidence": confidence,
            "reason": reason,
        }
    except Exception as e:
        logger.warning(f"classify_korean_sync JSON parse 실패: brand={brand} err={e}")
        return None
