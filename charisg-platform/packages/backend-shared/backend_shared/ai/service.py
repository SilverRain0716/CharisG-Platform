"""
ai_service.py — AI 서비스 (번역, SEO, 카테고리 매핑, CS 초안)
Provider: Gemini (기본) → Claude (향후 전환 가능)

환경변수:
  AI_PROVIDER=gemini|claude (기본: gemini)
  GEMINI_API_KEY=...
  ANTHROPIC_API_KEY=... (Claude 전환 시)
"""
import hashlib
import json
import logging
import os
import threading
import time
from typing import Optional

import requests

import os
from backend_shared._config import GEMINI_API_KEY
from backend_shared.context import get_db

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════
# Gemini Rate Limiter (무료 티어: RPM 15, RPD 1500)
# ═══════════════════════════════════════

class GeminiRateLimiter:
    """Gemini API 호출 한도 보호 — 기본값은 유료 Tier 1 (RPM 1000, RPD 10000).

    GEMINI_RPM, GEMINI_RPD 환경변수로 오버라이드 가능.
    무료 티어라면 환경변수에서 RPM 14, RPD 1400 으로 낮출 것."""

    def __init__(self, rpm: int | None = None, rpd: int | None = None):
        if rpm is None:
            rpm = int(os.environ.get("GEMINI_RPM", "800"))
        if rpd is None:
            rpd = int(os.environ.get("GEMINI_RPD", "9000"))
        self._rpm = rpm
        self._rpd = rpd
        self._lock = threading.Lock()
        self._minute_calls: list[float] = []
        self._day_calls: list[float] = []
        self._day_start: float = time.time()

    def wait(self):
        """호출 전 대기 — 한도 초과 시 자동 sleep"""
        with self._lock:
            now = time.time()

            # 일간 카운터 리셋 (24시간)
            if now - self._day_start > 86400:
                self._day_calls.clear()
                self._day_start = now

            # 만료된 분간 기록 제거
            self._minute_calls = [t for t in self._minute_calls if now - t < 60]
            self._day_calls = [t for t in self._day_calls if now - self._day_start < 86400]

            # RPD 한도
            if len(self._day_calls) >= self._rpd:
                logger.warning(f"⚠️ Gemini 일간 한도 도달 ({self._rpd} RPD) — 호출 차단")
                return False

            # RPM 한도 — 대기
            if len(self._minute_calls) >= self._rpm:
                oldest = self._minute_calls[0]
                wait_sec = 60 - (now - oldest) + 0.5
                if wait_sec > 0:
                    logger.info(f"⏳ Gemini RPM 한도 — {wait_sec:.1f}초 대기")
                    time.sleep(wait_sec)

            now = time.time()
            self._minute_calls.append(now)
            self._day_calls.append(now)
            return True

    @property
    def daily_remaining(self) -> int:
        with self._lock:
            now = time.time()
            if now - self._day_start > 86400:
                return self._rpd
            active = [t for t in self._day_calls if now - self._day_start < 86400]
            return max(0, self._rpd - len(active))


# 싱글턴 — ai_service + category_service 공유
gemini_limiter = GeminiRateLimiter()

AI_PROVIDER = os.environ.get("AI_PROVIDER", "gemini")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Gemini 모델 설정
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# Gemini 임베딩 모델 (768차원 축소 사용)
GEMINI_EMBED_MODEL = "gemini-embedding-001"
GEMINI_EMBED_DIM = 768
GEMINI_EMBED_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_EMBED_MODEL}:embedContent"
GEMINI_EMBED_BATCH_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_EMBED_MODEL}:batchEmbedContents"


# ═══════════════════════════════════════
# 공개 API — 라우터에서 호출
# ═══════════════════════════════════════

async def translate_text(
    text: str,
    source_lang: str = "en",
    target_lang: str = "ko",
    context: str = "",
) -> dict:
    """
    번역 (캐시 적용)

    Returns: {"translated": "번역된 텍스트", "cached": True/False}
    """
    if not text or not text.strip():
        return {"translated": "", "cached": False}

    # 캐시 확인
    cached = _cache_get(text, source_lang, target_lang)
    if cached:
        return {"translated": cached, "cached": True}

    prompt = _build_translate_prompt(text, source_lang, target_lang, context)
    result = await _call_ai_async(prompt)

    if result:
        _cache_set(text, source_lang, target_lang, result)

    return {"translated": result or text, "cached": False}


async def generate_seo(
    product_name: str,
    category: str = "",
    market: str = "KR",
    platform: str = "smartstore",
    description: str = "",
) -> dict:
    """
    SEO 키워드 + 최적화 상품명 생성

    Returns: {
        "optimized_title": "최적화된 상품명",
        "keywords": ["키워드1", "키워드2", ...],
        "tags": ["태그1", "태그2", ...],
    }
    """
    prompt = _build_seo_prompt(product_name, category, market, platform, description)
    result = await _call_ai_async(prompt)

    try:
        # JSON 파싱 시도
        parsed = json.loads(result)
        return parsed
    except (json.JSONDecodeError, TypeError):
        # 파싱 실패 시 원본 텍스트에서 추출
        return {
            "optimized_title": product_name,
            "keywords": [],
            "tags": [],
            "raw_response": result,
        }


async def map_category(
    product_name: str,
    source_category: str = "",
    target_platform: str = "smartstore",
) -> dict:
    """네이버 스마트스토어 leafCategoryId (숫자 8~10자리) 매핑.

    Returns: {"mapped_category": "50000313", "confidence": 0.0~1.0}
    - 숫자 ID만 반환. 한글 경로/슬래시 포함 시 무효 처리.
    """
    prompt = f"""당신은 네이버 스마트스토어 카테고리 매핑 전문가입니다.
다음 상품에 맞는 네이버 스마트스토어 leafCategoryId (숫자 8~10자리 코드)를 찾아주세요.

상품명: {product_name}
원본 카테고리 힌트: {source_category}

중요 규칙:
1. mapped_category 는 반드시 **숫자만** 포함 (예: "50000313"). 한글/슬래시/공백 금지.
2. 네이버에 실제로 존재하는 leaf(말단) 카테고리 ID만 반환.
3. 확실하지 않으면 confidence 를 0.3 이하로 낮추고, 가장 근접한 상위 카테고리 ID 사용.

JSON으로만 답변 (다른 텍스트 없이):
{{"mapped_category": "50000313", "confidence": 0.0~1.0}}"""

    result = await _call_ai_async(prompt)
    try:
        parsed = json.loads(result)
        mc = str(parsed.get("mapped_category", "")).strip()
        if not mc.isdigit() or not (6 <= len(mc) <= 12):
            return {"mapped_category": "", "confidence": 0.0, "raw": mc}
        return parsed
    except (json.JSONDecodeError, TypeError):
        return {"mapped_category": "", "confidence": 0.0}


async def generate_cs_draft(
    ticket_type: str,
    customer_message: str,
    order_info: dict = None,
    market: str = "KR",
    platform: str = "smartstore",
) -> str:
    """CS 응답 초안 생성 (M5에서 본격 사용)"""
    prompt = f"""당신은 {platform} 마켓의 고객 CS 담당자입니다.
다음 고객 문의에 대한 응답 초안을 작성해주세요.

문의 유형: {ticket_type}
고객 메시지: {customer_message}
주문 정보: {json.dumps(order_info or {}, ensure_ascii=False)}
마켓: {market}

톤: 정중하고 친절하게, {platform} 마켓 스타일에 맞게 작성.
응답만 작성하세요 (추가 설명 없이)."""

    return await _call_ai_async(prompt) or ""


async def analyze_product(
    product_data: dict,
) -> dict:
    """상품 분석 (마진율, 경쟁도, 추천 여부)"""
    prompt = f"""다음 상품 데이터를 분석하고 구매대행 상품으로서의 가치를 평가해주세요.

상품 데이터:
{json.dumps(product_data, ensure_ascii=False, indent=2)}

JSON으로만 답변하세요:
{{
  "recommendation": "추천|보류|비추천",
  "reason": "판단 근거 1-2문장",
  "estimated_margin_pct": 0,
  "competition_level": "low|medium|high",
  "tips": ["팁1", "팁2"]
}}"""

    result = await _call_ai_async(prompt)
    try:
        return json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return {"recommendation": "보류", "reason": "분석 실패", "raw": result}


# ═══════════════════════════════════════
# Provider 분기 — Gemini / Claude
# ═══════════════════════════════════════

async def _call_ai_async(prompt: str, max_tokens: int = 2000) -> Optional[str]:
    """AI API 호출 (비동기 래핑 — 이벤트 루프 블로킹 방지)"""
    import asyncio
    return await asyncio.to_thread(_call_ai_sync, prompt, max_tokens)


def embed_text(text: str, task_type: str = "SEMANTIC_SIMILARITY") -> Optional[list[float]]:
    """Gemini 임베딩 (단건). 768-dim float list 반환."""
    if not GEMINI_API_KEY or not text:
        return None
    for attempt in range(3):
        if not gemini_limiter.wait():
            return None
        try:
            r = requests.post(
                f"{GEMINI_EMBED_URL}?key={GEMINI_API_KEY}",
                json={
                    "model": f"models/{GEMINI_EMBED_MODEL}",
                    "content": {"parts": [{"text": text[:8000]}]},
                    "taskType": task_type,
                    "outputDimensionality": GEMINI_EMBED_DIM,
                },
                timeout=15,
            )
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json().get("embedding", {}).get("values")
        except Exception as e:
            logger.warning(f"embed_text 실패 attempt={attempt}: {e}")
            time.sleep(1)
    return None


def embed_batch(texts: list[str], task_type: str = "SEMANTIC_SIMILARITY", batch_size: int = 100) -> list[Optional[list[float]]]:
    """Gemini 임베딩 (배치). batchEmbedContents API 사용."""
    if not GEMINI_API_KEY or not texts:
        return []
    results: list[Optional[list[float]]] = []
    for start in range(0, len(texts), batch_size):
        chunk = texts[start:start + batch_size]
        for attempt in range(3):
            if not gemini_limiter.wait():
                results.extend([None] * len(chunk))
                break
            try:
                r = requests.post(
                    f"{GEMINI_EMBED_BATCH_URL}?key={GEMINI_API_KEY}",
                    json={
                        "requests": [
                            {
                                "model": f"models/{GEMINI_EMBED_MODEL}",
                                "content": {"parts": [{"text": t[:8000]}]},
                                "taskType": task_type,
                                "outputDimensionality": GEMINI_EMBED_DIM,
                            } for t in chunk
                        ]
                    },
                    timeout=60,
                )
                if r.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                embeddings = r.json().get("embeddings", [])
                results.extend([e.get("values") for e in embeddings])
                break
            except Exception as e:
                logger.warning(f"embed_batch 실패 (start={start}, attempt={attempt}): {e}")
                time.sleep(1)
        else:
            results.extend([None] * len(chunk))
    return results


def _call_ai_sync(prompt: str, max_tokens: int = 2000) -> Optional[str]:
    """AI API 호출 (provider 분기, 동기)"""
    if AI_PROVIDER == "claude":
        return _call_claude(prompt, max_tokens)
    return _call_gemini(prompt, max_tokens)


def _call_gemini(prompt: str, max_tokens: int = 2000, max_retries: int = 3) -> Optional[str]:
    """Google Gemini API 호출 (rate limit + 429 재시도)"""
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY not set")
        return None

    for attempt in range(max_retries):
        # Rate limiter 대기
        if not gemini_limiter.wait():
            logger.error("Gemini 일간 한도 초과 — 호출 중단")
            return None

        try:
            resp = requests.post(
                f"{GEMINI_URL}?key={GEMINI_API_KEY}",
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "maxOutputTokens": max_tokens,
                        "temperature": 0.3,
                        "thinkingConfig": {"thinkingBudget": 0},
                    },
                },
                timeout=30,
            )

            if resp.status_code == 429:
                wait = 30 * (attempt + 1)
                logger.warning(f"⏳ Gemini 429 Rate Limit → {wait}초 대기 ({attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue

            if resp.status_code == 503:
                wait = 3 * (2 ** attempt)  # 3s, 6s, 12s
                logger.warning(f"⏳ Gemini 503 서버 과부하 → {wait}초 대기 ({attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue

            data = resp.json()

            if "candidates" in data and data["candidates"]:
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                # JSON 코드블록 제거
                text = text.strip()
                if text.startswith("```json"):
                    text = text[7:]
                if text.startswith("```"):
                    text = text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                return text.strip()

            logger.warning(f"Gemini 응답 없음: {str(data)[:200]}")
            return None

        except requests.exceptions.Timeout:
            logger.warning(f"Gemini 타임아웃 ({attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                time.sleep(5)
                continue
            return None
        except Exception as e:
            logger.error(f"Gemini API 오류: {e}")
            return None

    logger.error(f"Gemini API {max_retries}회 재시도 실패")
    return None


def _call_claude(prompt: str, max_tokens: int = 2000) -> Optional[str]:
    """Anthropic Claude API 호출 (향후 전환용)"""
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set")
        return None

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        data = resp.json()

        if "content" in data and data["content"]:
            text = data["content"][0]["text"]
            text = text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            return text.strip()

        logger.warning(f"Claude 응답 없음: {data}")
        return None

    except Exception as e:
        logger.error(f"Claude API 오류: {e}")
        return None


# ═══════════════════════════════════════
# 프롬프트 빌더
# ═══════════════════════════════════════

def _build_translate_prompt(text: str, source_lang: str, target_lang: str, context: str) -> str:
    lang_names = {"en": "영어", "ko": "한국어", "ja": "일본어", "zh": "중국어"}
    src = lang_names.get(source_lang, source_lang)
    tgt = lang_names.get(target_lang, target_lang)

    prompt = f"""{src}를 {tgt}로 번역해주세요. 상품명/설명 번역이므로 자연스러운 상업적 표현을 사용하세요.

규칙:
- 상품명은 반드시 90~95자 이내로 작성하세요 (네이버 스마트스토어 100자 제한).
- 브랜드명은 영문 그대로 유지하세요.
- 인증 배지 설명(OEKO-TEX, Climate Pledge 등)은 제외하세요.
- 특수문자 (" * ? < > \\)는 사용하지 마세요. 인치는 "인치"로, 곱하기는 "x"로 표기하세요.
- 핵심 스펙(사이즈, 수량, 색상)을 포함하되 불필요한 수식어는 생략하세요.

원문: {text}"""

    if context:
        prompt += f"\n컨텍스트: {context}"

    prompt += "\n\n번역만 출력하세요 (추가 설명 없이)."
    return prompt


def _build_seo_prompt(
    product_name: str, category: str, market: str,
    platform: str, description: str,
) -> str:
    market_rules = {
        "smartstore": "스마트스토어 규정: 상품명 90~95자 이내 (100자 제한), 특수문자(\"*?<>\\) 금지, 인치는 '인치'로 표기, 핵심 키워드 앞배치",
        "coupang": "쿠팡 규정: 상품명 100자 이내, 브랜드명 필수, 주요 스펙 포함",
        "amazon": "Amazon 규정: Title 200자 이내, bullet points 5개, backend keywords",
        "ebay": "eBay 규정: Title 80자 이내, item specifics 활용",
    }
    rules = market_rules.get(platform, "")

    return f"""다음 상품에 대해 {platform} 마켓 SEO 최적화 상품명과 키워드를 생성해주세요.

원본 상품명: {product_name}
카테고리: {category}
마켓: {market}
{rules}
상품 설명: {description[:500] if description else '없음'}

**중요**: optimized_title은 반드시 100자 이내여야 합니다. 원본이 길면 불필요한 스펙(CL 타이밍, 세부 규격 등)을 제거하고 브랜드+핵심스펙+용도만 남겨 축약하세요.

JSON으로만 답변하세요 (다른 텍스트 없이):
{{
  "optimized_title": "SEO 최적화된 상품명 (100자 이내 필수)",
  "keywords": ["핵심키워드1", "핵심키워드2", ...최대 10개],
  "tags": ["태그1", "태그2", ...최대 13개]
}}"""


# ═══════════════════════════════════════
# 번역 캐시 (translation_cache 테이블)
# ═══════════════════════════════════════

def _text_hash(text: str) -> str:
    return hashlib.md5(text.strip().lower().encode("utf-8")).hexdigest()


def _cache_get(text: str, source_lang: str, target_lang: str) -> Optional[str]:
    h = _text_hash(text)
    with get_db() as conn:
        row = conn.execute(
            "SELECT translated_text FROM translation_cache WHERE source_text_hash=? AND source_lang=? AND target_lang=?",
            (h, source_lang, target_lang),
        ).fetchone()
    return row["translated_text"] if row else None


def _cache_set(text: str, source_lang: str, target_lang: str, translated: str):
    h = _text_hash(text)
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO translation_cache
               (source_text_hash, source_lang, target_lang, translated_text)
               VALUES (?, ?, ?, ?)""",
            (h, source_lang, target_lang, translated),
        )


# ═══════════════════════════════════════
# Google Trends + AI 소싱 분석
# ═══════════════════════════════════════

async def analyze_trending_keywords(
    keywords: list[str],
    business_model: str = "purchase_agent",
    market: str = "KR",
) -> list[dict]:
    """트렌딩 키워드 AI 분석 → 구매대행/드랍쉬핑 적합성 판단"""
    batch = keywords[:20]
    keywords_str = "\n".join(f"- {kw}" for kw in batch)

    model_desc = ("한국에서 해외 구매대행 (미국 아마존 → 한국 판매)"
                  if business_model == "purchase_agent"
                  else "드랍쉬핑 (미국 공급처 → 미국/한국 판매)")

    prompt = f"""당신은 해외 구매대행/드랍쉬핑 전문 상품 소싱 분석가입니다.

아래 Google Trends 급상승 키워드들을 분석하고, {model_desc} 사업에 적합한 키워드를 선별해주세요.

키워드 목록:
{keywords_str}

각 키워드에 대해 JSON 배열로 답변하세요 (다른 텍스트 없이):
[
  {{
    "keyword": "키워드 원문",
    "suitable": true/false,
    "reason": "적합/부적합 이유 1-2문장",
    "category": "카테고리 (홈데코/주방용품/생활용품/뷰티/펫/전자기기/패션/기타)",
    "estimated_margin_pct": 예상마진율(숫자),
    "risk": "low/medium/high",
    "priority": "high/medium/low"
  }}
]

판단 기준:
- 무게 2kg 이하, 크기 작은 상품 우선 (배송비 절감)
- 마진율 30% 이상 예상되는 상품
- 경쟁이 과도하지 않은 니치 상품 우선
- 식품/의약품/위험물/대형가전은 부적합
- 브랜드 독점 상품(Apple, Nike 등)은 부적합"""

    result = _call_ai_sync(prompt, max_tokens=4000)

    try:
        items = json.loads(result)
        for item in items:
            kw = item.get("keyword", "")
            query = kw.replace(" ", "+")
            item["amazon_search_url"] = f"https://www.amazon.com/s?k={query}"
            item["amazon_bestseller_url"] = f"https://www.amazon.com/s?k={query}&s=exact-aware-popularity-rank"
        return items
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"AI 키워드 분석 파싱 실패: {str(result)[:200]}")
        return []


def generate_amazon_urls(keywords: list[str]) -> list[dict]:
    """키워드 → 아마존 검색 URL 생성 (규칙 기반)"""
    urls = []
    for kw in keywords:
        query = kw.strip().replace(" ", "+")
        urls.append({
            "keyword": kw,
            "search_url": f"https://www.amazon.com/s?k={query}",
            "bestseller_url": f"https://www.amazon.com/s?k={query}&s=exact-aware-popularity-rank",
        })
    return urls


async def generate_sourcing_keywords(
    category: str = "",
    business_model: str = "purchase_agent",
    market: str = "KR",
    count: int = 20,
) -> list[str]:
    """
    AI가 직접 트렌드 소싱 키워드를 생성
    Google Trends 대체 — AI의 학습 데이터 기반 트렌드 추천
    """
    model_desc = ("한국에서 해외 구매대행 (미국 아마존 → 한국 스마트스토어/쿠팡 판매)"
                  if business_model == "purchase_agent"
                  else "드랍쉬핑 (미국 공급처 → 미국/한국 판매)")

    category_filter = f"\n카테고리 집중: {category}" if category else ""

    prompt = f"""당신은 미국 아마존 상품 트렌드 전문 분석가입니다.

현재 미국에서 인기 급상승 중이거나, {model_desc}에 적합한 아마존 검색 키워드를 {count}개 추천해주세요.{category_filter}

조건:
- 실제 아마존에서 검색하면 상품이 나오는 구체적인 영어 키워드
- 최근 SNS(TikTok, Instagram)에서 바이럴된 상품 우선
- 시즌 트렌드 반영 (현재 시즌에 맞는 상품)
- 무게 2kg 이하, 소형 상품 우선 (국제 배송 적합)
- 식품/의약품/위험물/대형가전/브랜드 독점 제외
- 니치하지만 수요가 있는 상품 (경쟁 적당)

카테고리 분포:
- 홈데코/인테리어: 30%
- 주방/생활용품: 25%
- 뷰티/퍼스널케어: 15%
- 펫/반려동물: 10%
- 전자기기/액세서리: 10%
- 기타 (피트니스, 취미 등): 10%

JSON 배열로만 답변하세요 (다른 텍스트 없이):
["keyword1", "keyword2", "keyword3", ...]"""

    result = _call_ai_sync(prompt, max_tokens=2000)

    try:
        keywords = json.loads(result)
        if isinstance(keywords, list):
            return [str(k).strip() for k in keywords if k][:count]
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"AI 키워드 생성 파싱 실패: {str(result)[:200]}")

    return []
