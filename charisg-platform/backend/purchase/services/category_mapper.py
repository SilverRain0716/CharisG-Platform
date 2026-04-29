"""
category_mapper.py — 쿠팡 카테고리 자동 매핑 + 키워드→카테고리 캐시 + 검토 큐.

네이버 카테고리는 backend_shared.category_service.find_category_with_gemini 가 처리
(categories.db 의 naver_categories 사용). 쿠팡은 purchase.db 의 coupang_categories
(19,460 row) 를 사용하므로 별도 함수.

공용 helper:
    map_categories_for_keyword(keyword, product_name)
      → keyword_category_map 캐시 우선
      → 미스 시 네이버 + 쿠팡 AI 매핑
      → score >= 50 면 캐시 INSERT, < 50 면 category_review_queue INSERT
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# 채널별 review 임계값 차등 (B 안):
# - 네이버: 자동매칭 없음 → 70 미만은 review_queue (엄격)
# - 쿠팡: 자동매칭 가능 → 50 미만만 review, 50~70 은 자동매칭 위임 (listings_pa.coupang_auto_matched=1)
REVIEW_THRESHOLD = 70  # 호환용 (네이버 임계)
NAVER_REVIEW_THRESHOLD = 70
COUPANG_REVIEW_THRESHOLD = 50
COUPANG_AUTO_MATCH_THRESHOLD = 70  # 50 ≤ score < 70 → 쿠팡 자동매칭 위임


# ── 한국어 보장 + 토큰화 (category_service 와 동일 로직 재사용) ──
def _looks_english(text: str) -> bool:
    if not text:
        return False
    text = text.strip()
    if not text:
        return False
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return (ascii_chars / len(text)) >= 0.8


def _ensure_korean_name(text: str) -> str:
    if not text or not _looks_english(text):
        return text
    try:
        from backend_shared.ai.service import translate_text
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        coro = translate_text(
            text, source_lang="en", target_lang="ko",
            context="한국 쇼핑몰(쿠팡) 카테고리 매핑용 상품명",
        )
        if loop is None:
            res = asyncio.run(coro)
        else:
            new_loop = asyncio.new_event_loop()
            try:
                res = new_loop.run_until_complete(coro)
            finally:
                new_loop.close()
        return ((res or {}).get("translated") or "").strip() or text
    except Exception as e:
        logger.warning(f"[coupang-cat] translate_text 실패: {e}")
        return text


_HANGUL_RE = None


def _korean_tokens(text: str) -> list[str]:
    import re
    global _HANGUL_RE
    if _HANGUL_RE is None:
        _HANGUL_RE = re.compile(r"[가-힣A-Za-z0-9]+")
    seen: list[str] = []
    for t in _HANGUL_RE.findall(text or ""):
        if len(t) >= 2 and t not in seen:
            seen.append(t)
    return seen


def _ai_suggest_keywords(product_name: str) -> list[str]:
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        return []
    prompt = (
        f"한국 쇼핑몰(쿠팡) 카테고리 트리에서 다음 상품을 검색할 때 유용한 한국어 "
        f"키워드 5개를 추천해줘. 명사 위주, 한 줄에 하나씩.\n\n"
        f"상품명: {product_name}\n\n"
        f"키워드만 출력 (번호/설명 없이):"
    )
    try:
        from backend_shared.ai import gemini_limiter
        if not gemini_limiter.wait():
            return []
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        out = [line.strip(" -·•").strip() for line in text.splitlines() if line.strip()]
        return [t for t in out if 2 <= len(t) <= 30][:8]
    except Exception as e:
        logger.warning(f"[coupang-cat] _ai_suggest_keywords 실패: {e}")
        return []


def _search_coupang_candidates(terms: list[str], limit_per_term: int = 8) -> list[dict]:
    """coupang_categories (purchase.db) 에서 leaf 후보 검색."""
    from backend.purchase.database import get_db
    candidates: list[dict] = []
    seen: set[int] = set()
    with get_db() as conn:
        for term in terms:
            term = term.strip()
            if not term:
                continue
            rows = conn.execute(
                """SELECT code, name, path FROM coupang_categories
                   WHERE (name LIKE ? OR path LIKE ?)
                     AND is_leaf=1 AND status='ACTIVE'
                   LIMIT ?""",
                (f"%{term}%", f"%{term}%", limit_per_term),
            ).fetchall()
            for r in rows:
                if r["code"] in seen:
                    continue
                seen.add(r["code"])
                candidates.append({
                    "code": int(r["code"]),
                    "name": r["name"],
                    "path": r["path"],
                })
    return candidates


def find_coupang_category_with_gemini(
    product_name: str, category_hint: str = "",
    features: list = None,
    sample_titles: list = None, sample_en: str = "",
) -> dict:
    """쿠팡 leaf 카테고리 자동 매핑 (find_category_with_gemini 와 동일 패턴).

    추가 신호 (E 강화 — 키워드 모호성 해소):
    - sample_titles: 같은 키워드의 다른 상품 제목 (한국어, 최대 3건)
    - sample_en: 영문 원문 1건

    반환: {"code": int|None, "name": str, "path": str, "score": int, "needs_review": bool, "reason": str}
    """
    name_for_search = _ensure_korean_name(product_name) if product_name else ""

    terms = _korean_tokens(name_for_search)
    if category_hint:
        terms = [category_hint] + terms
    terms = terms[:8]
    candidates = _search_coupang_candidates(terms, limit_per_term=8)

    if len(candidates) < 10:
        suggested = _ai_suggest_keywords(name_for_search or product_name)
        if suggested:
            extra = _search_coupang_candidates(suggested, limit_per_term=6)
            existing = {c["code"] for c in candidates}
            for c in extra:
                if c["code"] not in existing:
                    candidates.append(c)

    if not candidates:
        logger.warning(f"[coupang-cat] 후보 0건: {(product_name or '')[:50]}")
        return {"code": None, "name": "", "path": "", "score": 0, "needs_review": True}

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        c = candidates[0]
        return {"code": c["code"], "name": c["name"], "path": c["path"],
                "score": 30, "needs_review": True}

    candidate_text = "\n".join(f"- {c['code']}: {c['path']}" for c in candidates[:30])
    features_text = ", ".join((features or [])[:5])

    # E 강화: 샘플 제목 + 영문 원문
    samples_block = ""
    if sample_titles:
        sample_lines = "\n".join(f"  - {t}" for t in sample_titles[:3] if t)
        if sample_lines:
            samples_block += f"\n같은 키워드 다른 상품 제목:\n{sample_lines}"
    if sample_en:
        samples_block += f"\n영문 원문 (참고): {sample_en[:200]}"

    prompt = (
        f"다음 상품에 가장 적합한 쿠팡 leaf 카테고리를 선택하세요.\n\n"
        f"상품명(원본): {product_name}\n"
        f"상품명(한국어): {name_for_search}\n"
        f"카테고리 힌트: {category_hint}\n"
        f"상품 특징: {features_text}{samples_block}\n\n"
        f"후보 카테고리:\n{candidate_text}\n\n"
        f"규칙:\n"
        f"1. 반드시 위 후보 중 하나만 선택\n"
        f"2. 같은 단어가 여러 카테고리에 있을 때 (예: '선글라스' → 사람용/반려동물용, "
        f"'칼날' → 면도기날/공구) 샘플 제목과 영문 원문을 종합해 의도 파악 — "
        f"모호하면 score 낮춰서 답변\n"
        f"3. 매칭 신뢰도 점수(0~100) 함께 답변 (90+ 정확, 70-89 적당, 50-69 애매, 0-49 추측)\n"
        f"4. JSON 형식으로만 응답:\n"
        f'   {{"code": 12345, "score": 85, "reason": "한 줄 이유"}}'
    )

    try:
        from backend_shared.ai import gemini_limiter
        if not gemini_limiter.wait():
            return {"code": None, "name": "", "path": "", "score": 0, "needs_review": True}
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}",
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"responseMimeType": "application/json"}},
            timeout=20,
        )
        if resp.status_code != 200:
            return {"code": None, "name": "", "path": "", "score": 0, "needs_review": True}
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            digits = "".join(c for c in text if c.isdigit())
            parsed = {"code": int(digits) if digits else None, "score": 50}
        code = parsed.get("code")
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = None
        score = int(parsed.get("score") or 0)
        matched = next((c for c in candidates if c["code"] == code), None)
        if not matched:
            logger.warning(f"[coupang-cat] Gemini 후보 외 응답: code={code}")
            return {"code": None, "name": "", "path": "", "score": 0, "needs_review": True,
                    "raw_response": text[:200]}
        # 쿠팡은 50 미만만 review_queue, 50~70 은 자동매칭 위임 (caller 가 처리)
        needs_review = score < COUPANG_REVIEW_THRESHOLD
        logger.info(f"[coupang-cat] {(product_name or '')[:30]} → {matched['path']} "
                    f"(score={score}, review={needs_review})")
        return {
            "code": matched["code"],
            "name": matched["name"],
            "path": matched["path"],
            "score": score,
            "needs_review": needs_review,
            "reason": parsed.get("reason", ""),
        }
    except Exception as e:
        logger.warning(f"[coupang-cat] Gemini 매핑 실패: {e}")
        return {"code": None, "name": "", "path": "", "score": 0, "needs_review": True}


# ── 통합 helper: 키워드 캐시 + 네이버/쿠팡 AI 매핑 + 검토 큐 ──
def map_categories_for_keyword(
    keyword: Optional[str],
    product_name: str,
    product_id: Optional[int] = None,
    product_name_en: Optional[str] = None,
    category_hint: str = "",
    features: Optional[list] = None,
    sample_titles: Optional[list] = None,
    sample_en: str = "",
) -> dict:
    """키워드 기반 네이버 + 쿠팡 카테고리 매핑.

    1) keyword 정규화 후 keyword_category_map 캐시 lookup
    2) 캐시 hit → 그대로 반환 (score=100, source="cache")
    3) 캐시 미스 → 네이버 + 쿠팡 AI 매핑 호출
    4) 둘 다 score >= 50 → keyword_category_map INSERT (source="ai") + 반환
    5) 어느 하나라도 < 50 → category_review_queue INSERT + needs_review=True 반환

    반환:
      {
        "naver_id": "50004595" or None,
        "naver_path": "디지털/가전>...",
        "naver_score": 98,
        "coupang_code": 62618 or None,
        "coupang_path": "가전/디지털>...",
        "coupang_score": 92,
        "from_cache": False,
        "needs_review": False,
        "review_id": None or int (queue id),
      }
    """
    from backend.purchase.database import get_db

    norm_keyword = _normalize_keyword(keyword)

    # 1) 캐시 lookup
    if norm_keyword:
        with get_db() as conn:
            cached = conn.execute(
                """SELECT naver_category_id, naver_category_path,
                          coupang_category_code, coupang_category_path,
                          source, ai_naver_score, ai_coupang_score
                   FROM keyword_category_map WHERE keyword=? LIMIT 1""",
                (norm_keyword,),
            ).fetchone()
        if cached:
            logger.info(f"[map_categories] cache hit keyword='{norm_keyword}'")
            cached_auto_match = (cached["source"] == 'ai_soft') and not cached["coupang_category_code"]
            return {
                "naver_id": cached["naver_category_id"],
                "naver_path": cached["naver_category_path"],
                "naver_score": 100,
                "coupang_code": cached["coupang_category_code"],
                "coupang_path": cached["coupang_category_path"],
                "coupang_score": cached["ai_coupang_score"] or 100,
                "coupang_auto_match": cached_auto_match,
                "from_cache": True,
                "needs_review": False,
                "review_id": None,
            }

    # 2) AI 매핑 (네이버 + 쿠팡 동시) — sample_titles + sample_en 으로 키워드 모호성 해소
    from backend_shared.category_service import find_category_with_gemini
    naver_r = find_category_with_gemini(
        product_name=product_name, category_hint=category_hint, features=features,
        sample_titles=sample_titles, sample_en=sample_en,
    ) or {}
    coupang_r = find_coupang_category_with_gemini(
        product_name=product_name, category_hint=category_hint, features=features,
        sample_titles=sample_titles, sample_en=sample_en,
    ) or {}

    naver_id = naver_r.get("id") or None
    naver_score = int(naver_r.get("score") or 0)
    coupang_code = coupang_r.get("code")
    coupang_score = int(coupang_r.get("score") or 0)

    # B 안 차등 처리:
    # - 네이버 < 70 → review (자동매칭 없으므로 엄격)
    # - 쿠팡 < 50 → review (확실히 모호)
    # - 50 ≤ 쿠팡 < 70 → review 안 보내고 자동매칭 위임 (coupang_code=None 처리, listings 마킹은 caller)
    naver_review = naver_score < NAVER_REVIEW_THRESHOLD
    coupang_review = coupang_score < COUPANG_REVIEW_THRESHOLD
    coupang_auto_match = (
        COUPANG_REVIEW_THRESHOLD <= coupang_score < COUPANG_AUTO_MATCH_THRESHOLD
    )
    needs_review = naver_review or coupang_review

    # 3) 캐시 INSERT 정책:
    # - 둘 다 score 충족 (>=70) → 정상 캐시
    # - 네이버 OK + 쿠팡 50~70 → 쿠팡은 NULL 로 캐시 (자동매칭 위임)
    # - 그 외 (review 케이스) → 캐시 안 함
    review_id = None
    if not needs_review and naver_id and norm_keyword:
        # 쿠팡 자동매칭 위임 시 coupang_code 는 NULL 저장
        cache_coupang_code = None if coupang_auto_match else coupang_code
        cache_coupang_path = "" if coupang_auto_match else coupang_r.get("path", "")
        if naver_id and (cache_coupang_code or coupang_auto_match):
            with get_db() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO keyword_category_map
                       (keyword, naver_category_id, naver_category_path,
                        coupang_category_code, coupang_category_path,
                        source, ai_naver_score, ai_coupang_score, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (norm_keyword, naver_id, naver_r.get("whole_name", ""),
                     cache_coupang_code, cache_coupang_path,
                     'ai_soft' if coupang_auto_match else 'ai',
                     naver_score, coupang_score),
                )
            logger.info(
                f"[map_categories] AI 매핑 캐시 keyword='{norm_keyword}' "
                f"naver={naver_id}({naver_score}) "
                f"coupang={'AUTO' if coupang_auto_match else coupang_code}({coupang_score})"
            )

    # 4) 검토 필요 → review_queue INSERT
    if needs_review:
        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO category_review_queue
                   (product_id, keyword, product_name, product_name_en,
                    ai_naver_id, ai_naver_path, ai_naver_score, ai_naver_reason,
                    ai_coupang_code, ai_coupang_path, ai_coupang_score, ai_coupang_reason,
                    status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    product_id, norm_keyword, product_name, product_name_en,
                    naver_id, naver_r.get("whole_name", ""), naver_score,
                    naver_r.get("reason", ""),
                    coupang_code, coupang_r.get("path", ""), coupang_score,
                    coupang_r.get("reason", ""),
                ),
            )
            review_id = cur.lastrowid
        logger.warning(f"[map_categories] review 큐 INSERT id={review_id} "
                       f"naver_score={naver_score} coupang_score={coupang_score}")

    return {
        "naver_id": naver_id,
        "naver_path": naver_r.get("whole_name", ""),
        "naver_score": naver_score,
        "coupang_code": None if coupang_auto_match else coupang_code,
        "coupang_path": "" if coupang_auto_match else coupang_r.get("path", ""),
        "coupang_score": coupang_score,
        "coupang_auto_match": coupang_auto_match,  # B 안 — 쿠팡 자동매칭 위임 플래그
        "from_cache": False,
        "needs_review": needs_review,
        "review_id": review_id,
    }


def _normalize_keyword(keyword: Optional[str]) -> Optional[str]:
    """키워드 정규화: lowercase + trim + 공백 정리."""
    if not keyword:
        return None
    import re
    k = keyword.strip().lower()
    k = re.sub(r"\s+", " ", k)
    return k or None
