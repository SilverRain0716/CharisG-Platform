"""
category_service.py — 네이버 카테고리 자동 매핑
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. 네이버 커머스 API에서 전체 leaf 카테고리 조회 → SQLite 캐싱
2. 상품명+카테고리 키워드로 적절한 leaf 카테고리 자동 매핑
3. 캐시 만료: 7일마다 자동 갱신
"""
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
import bcrypt
import pybase64

logger = logging.getLogger(__name__)

CACHE_DAYS = 7  # 카테고리 캐시 유효기간


def _get_naver_token() -> Optional[str]:
    """네이버 커머스 API 토큰 발급"""
    from dotenv import load_dotenv
    load_dotenv()

    client_id = os.environ.get("NAVER_CLIENT_ID", "")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        logger.warning("NAVER_CLIENT_ID/SECRET 미설정")
        return None

    ts = int((datetime.now(timezone.utc) - timedelta(seconds=3)).timestamp() * 1000)
    pwd = f"{client_id}_{ts}"
    hashed = bcrypt.hashpw(pwd.encode(), client_secret.encode())
    sign = pybase64.standard_b64encode(hashed).decode()

    resp = requests.post(
        "https://api.commerce.naver.com/external/v1/oauth2/token",
        data={
            "client_id": client_id,
            "timestamp": ts,
            "grant_type": "client_credentials",
            "client_secret_sign": sign,
            "type": "SELF",
        },
        timeout=15,
    )
    return resp.json().get("access_token")


def _get_db():
    """카테고리 전용 DB (메인 DB와 분리)"""
    from backend_shared._config import PROJECT_ROOT
    db_path = os.environ.get("CATEGORY_DB_PATH", str(PROJECT_ROOT / "categories.db"))
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS naver_categories (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            whole_name TEXT NOT NULL,
            is_leaf BOOLEAN DEFAULT 1,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS category_cache_meta (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn


def sync_naver_categories(force: bool = False) -> dict:
    """
    네이버 전체 leaf 카테고리를 DB에 동기화
    force=False면 CACHE_DAYS 이내에는 스킵
    """
    conn = _get_db()

    # 캐시 만료 체크
    if not force:
        row = conn.execute(
            "SELECT value FROM category_cache_meta WHERE key = 'last_sync'"
        ).fetchone()
        if row:
            last = datetime.fromisoformat(row["value"])
            if (datetime.now() - last).days < CACHE_DAYS:
                count = conn.execute("SELECT COUNT(*) as c FROM naver_categories").fetchone()["c"]
                conn.close()
                return {"status": "cached", "count": count, "last_sync": row["value"]}

    # 토큰 발급
    token = _get_naver_token()
    if not token:
        conn.close()
        return {"status": "error", "message": "네이버 토큰 발급 실패"}

    # 전체 leaf 카테고리 조회
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(
        "https://api.commerce.naver.com/external/v1/categories?last=true",
        headers=headers,
        timeout=30,
    )

    if resp.status_code != 200:
        conn.close()
        return {"status": "error", "message": f"API 오류 {resp.status_code}"}

    cats = resp.json()
    logger.info("네이버 카테고리 %d개 조회됨", len(cats))

    # DB에 저장 (upsert)
    conn.execute("DELETE FROM naver_categories")
    for c in cats:
        conn.execute(
            "INSERT INTO naver_categories (id, name, whole_name, is_leaf) VALUES (?, ?, ?, 1)",
            (c["id"], c["name"], c.get("wholeCategoryName", c["name"])),
        )

    now = datetime.now().isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO category_cache_meta (key, value, updated_at) VALUES ('last_sync', ?, ?)",
        (now, now),
    )
    conn.commit()
    count = len(cats)
    conn.close()

    logger.info("✅ 네이버 카테고리 %d개 DB 동기화 완료", count)
    return {"status": "synced", "count": count, "last_sync": now}


def search_categories(keyword: str, limit: int = 10) -> list[dict]:
    """키워드로 카테고리 검색"""
    conn = _get_db()
    rows = conn.execute(
        "SELECT id, name, whole_name FROM naver_categories WHERE whole_name LIKE ? LIMIT ?",
        (f"%{keyword}%", limit),
    ).fetchall()
    conn.close()
    return [{"id": r["id"], "name": r["name"], "whole_name": r["whole_name"]} for r in rows]


def find_best_category(product_name: str, category_hint: str = "") -> dict:
    """
    상품명 + 카테고리 힌트로 최적의 leaf 카테고리 자동 매핑
    Gemini 없이 키워드 매칭으로 1차 시도
    """
    conn = _get_db()
    count = conn.execute("SELECT COUNT(*) as c FROM naver_categories").fetchone()["c"]
    if count == 0:
        conn.close()
        # 캐시가 비어있으면 동기화 시도
        sync_naver_categories(force=True)
        conn = _get_db()

    # 키워드 추출
    keywords = _extract_keywords(product_name, category_hint)

    best_match = None
    best_score = 0

    for kw_combo in keywords:
        rows = conn.execute(
            "SELECT id, name, whole_name FROM naver_categories WHERE whole_name LIKE ?",
            (f"%{kw_combo}%",),
        ).fetchall()

        for r in rows:
            score = _score_match(r["whole_name"], product_name, category_hint)
            if score > best_score:
                best_score = score
                best_match = {"id": r["id"], "name": r["name"], "whole_name": r["whole_name"], "score": score}

    conn.close()

    if best_match:
        logger.info("카테고리 매칭: %s → %s (score: %d)", product_name[:30], best_match["whole_name"], best_score)
        return best_match

    # 기본 폴백
    return {"id": "50003767", "name": "데코용품", "whole_name": "생활/건강>문구/사무용품>이벤트/파티용품>데코용품", "score": 0}


_HANGUL_RE = None  # lazy compile


def _looks_english(text: str) -> bool:
    """ASCII 비율이 80% 이상이면 영문 위주로 판정."""
    if not text:
        return False
    text = text.strip()
    if not text:
        return False
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return (ascii_chars / len(text)) >= 0.8


def _ensure_korean_name(text: str) -> str:
    """영문 위주면 translate_text 로 한국어 변환. 실패 시 원문 유지."""
    if not text or not _looks_english(text):
        return text
    try:
        from backend_shared.ai.service import translate_text
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        coro = translate_text(text, source_lang="en", target_lang="ko",
                              context="한국 쇼핑몰(쿠팡/스마트스토어) 카테고리 매핑용 상품명")
        if loop is None:
            res = asyncio.run(coro)
        else:
            new_loop = asyncio.new_event_loop()
            try:
                res = new_loop.run_until_complete(coro)
            finally:
                new_loop.close()
        translated = (res or {}).get("translated", "").strip()
        return translated or text
    except Exception as e:
        logger.warning(f"[category] translate_text 실패: {e}")
        return text


def _korean_tokens(text: str) -> list[str]:
    """간단 한국어 토큰화 — 띄어쓰기 split + 2자 이상 + 특수문자 제거."""
    import re
    global _HANGUL_RE
    if _HANGUL_RE is None:
        _HANGUL_RE = re.compile(r"[가-힣A-Za-z0-9]+")
    tokens = _HANGUL_RE.findall(text or "")
    seen = []
    for t in tokens:
        if len(t) >= 2 and t not in seen:
            seen.append(t)
    return seen


def _ai_suggest_category_keywords(product_name: str) -> list[str]:
    """후보 부족 시 AI 가 카테고리 키워드 추천 — 한국 쇼핑몰 카테고리 어휘로."""
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        return []
    prompt = (
        f"한국 쇼핑몰(쿠팡/스마트스토어) 의 카테고리 트리에서 다음 상품을 검색할 때 "
        f"유용한 키워드 5개를 추천해줘. 명사 위주, 한국어, 한 줄에 하나씩.\n\n"
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
        tokens = [line.strip(" -·•").strip() for line in text.splitlines() if line.strip()]
        return [t for t in tokens if 2 <= len(t) <= 30][:8]
    except Exception as e:
        logger.warning(f"[category] _ai_suggest_category_keywords 실패: {e}")
        return []


def _search_candidates(terms: list[str], limit_per_term: int = 8) -> list[dict]:
    """terms 의 각 키워드로 naver_categories LIKE 검색 → 후보 dedupe 반환."""
    conn = _get_db()
    candidates: list[dict] = []
    seen_ids: set[str] = set()
    try:
        for term in terms:
            term = term.strip()
            if not term:
                continue
            rows = conn.execute(
                "SELECT id, whole_name FROM naver_categories WHERE whole_name LIKE ? AND is_leaf=1 LIMIT ?",
                (f"%{term}%", limit_per_term),
            ).fetchall()
            for r in rows:
                if r["id"] in seen_ids:
                    continue
                seen_ids.add(r["id"])
                candidates.append({"id": r["id"], "whole_name": r["whole_name"]})
    finally:
        conn.close()
    return candidates


def find_category_with_gemini(
    product_name: str, category_hint: str = "", features: list = None,
    sample_titles: list = None, sample_en: str = "",
) -> dict:
    """네이버 leaf 카테고리 자동 매핑.

    개선 사항 (2026-04-27):
    - 영문 product_name 자동 한국어 번역
    - 한국어 토큰 기반 후보 검색
    - 후보 부족 시 AI 키워드 추천 → 재검색
    - Gemini score (0~100) 응답 → caller 가 임계값 미만은 review 큐로
    - 인테리어 fallback dict 제거 (잘못된 강제 매핑 방지)

    추가 신호 (E 강화):
    - sample_titles: 같은 키워드의 다른 상품 제목 (한국어, 최대 3건) — 키워드 모호성 해소
    - sample_en: 영문 원문 1건 — 한국어 번역 누락 보완
    """
    # 1) 영문 → 한국어 보장
    name_for_search = _ensure_korean_name(product_name) if product_name else ""

    # 2) 1차 키워드 매칭 (한국어 토큰 + category_hint)
    terms = _korean_tokens(name_for_search)
    if category_hint:
        terms = [category_hint] + terms
    terms = terms[:8]
    candidates = _search_candidates(terms, limit_per_term=8)

    # 3) 후보 부족 시 AI 키워드 추천 → 재검색
    if len(candidates) < 10:
        suggested = _ai_suggest_category_keywords(name_for_search or product_name)
        if suggested:
            extra = _search_candidates(suggested, limit_per_term=6)
            existing_ids = {c["id"] for c in candidates}
            for c in extra:
                if c["id"] not in existing_ids:
                    candidates.append(c)

    if not candidates:
        logger.warning(f"[category] 후보 0건: {(product_name or '')[:50]}")
        return {"id": "", "name": "", "whole_name": "", "score": 0, "needs_review": True}

    # 4) Gemini 매핑 + score
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        return {"id": candidates[0]["id"], "name": candidates[0]["whole_name"].split(">")[-1],
                "whole_name": candidates[0]["whole_name"], "score": 30, "needs_review": True}

    candidate_text = "\n".join(f"- {c['id']}: {c['whole_name']}" for c in candidates[:30])
    features_text = ", ".join((features or [])[:5])

    # E 강화: 샘플 제목 (최대 3건) + 영문 원문 — 키워드 모호성 해소용
    samples_block = ""
    if sample_titles:
        sample_lines = "\n".join(f"  - {t}" for t in sample_titles[:3] if t)
        if sample_lines:
            samples_block += f"\n같은 키워드 다른 상품 제목:\n{sample_lines}"
    if sample_en:
        samples_block += f"\n영문 원문 (참고): {sample_en[:200]}"

    prompt = (
        f"다음 상품에 가장 적합한 네이버 스마트스토어 leaf 카테고리를 선택하세요.\n\n"
        f"상품명(원본): {product_name}\n"
        f"상품명(한국어): {name_for_search}\n"
        f"카테고리 힌트: {category_hint}\n"
        f"상품 특징: {features_text}{samples_block}\n\n"
        f"후보 카테고리:\n{candidate_text}\n\n"
        f"규칙:\n"
        f"1. 반드시 위 후보 중 하나만 선택\n"
        f"2. 같은 단어가 여러 카테고리에 있을 때 (예: '선글라스' → 사람용/반려동물용) "
        f"샘플 제목과 영문 원문을 종합해 의도 파악 — 모호하면 score 낮춰서 답변\n"
        f"3. 매칭 신뢰도 점수(0~100) 함께 답변\n"
        f"   - 90+: 카테고리가 정확히 일치\n"
        f"   - 70-89: 적당히 일치\n"
        f"   - 50-69: 애매함\n"
        f"   - 0-49: 거의 추측\n"
        f"4. JSON 형식으로만 응답 (다른 텍스트 없이):\n"
        f'   {{"id": "12345", "score": 85, "reason": "한 줄 이유"}}'
    )

    try:
        from backend_shared.ai import gemini_limiter
        if not gemini_limiter.wait():
            logger.warning("Gemini 일간 한도 초과 — 카테고리 매핑 스킵")
            return {"id": "", "name": "", "whole_name": "", "score": 0, "needs_review": True}

        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}",
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"responseMimeType": "application/json"}},
            timeout=20,
        )
        if resp.status_code == 429:
            return {"id": "", "name": "", "whole_name": "", "score": 0, "needs_review": True}
        if resp.status_code != 200:
            logger.warning(f"[category] Gemini {resp.status_code}: {resp.text[:200]}")
            return {"id": "", "name": "", "whole_name": "", "score": 0, "needs_review": True}

        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # JSON 모드 강제했지만 fallback — id 만 숫자로 추출
            digits = "".join(c for c in text if c.isdigit())
            parsed = {"id": digits, "score": 50}
        cat_id = str(parsed.get("id") or "").strip()
        score = int(parsed.get("score") or 0)
        matched = next((c for c in candidates if c["id"] == cat_id), None)
        if not matched:
            logger.warning(f"[category] Gemini 가 후보 외 ID 응답: {cat_id} (텍스트: {text[:120]})")
            return {"id": "", "name": "", "whole_name": "", "score": 0, "needs_review": True,
                    "raw_response": text[:200]}

        # 임계값 70 — 50~69 는 모호한 매핑이라 review 필수 (E 강화)
        needs_review = score < 70
        logger.info(f"[category] {(product_name or '')[:30]} → {matched['whole_name']} "
                    f"(score={score}, review={needs_review})")
        return {
            "id": matched["id"],
            "name": matched["whole_name"].split(">")[-1],
            "whole_name": matched["whole_name"],
            "score": score,
            "needs_review": needs_review,
            "reason": parsed.get("reason", ""),
        }
    except Exception as e:
        logger.warning(f"[category] Gemini 카테고리 매핑 실패: {e}")
        return {"id": "", "name": "", "whole_name": "", "score": 0, "needs_review": True}


def _extract_keywords(product_name: str, category_hint: str = "") -> list[str]:
    """상품명에서 카테고리 매칭용 키워드 추출"""
    # 영어→한글 매핑
    en_to_kr = {
        "vase": "화병", "flower": "화병", "decor": "장식", "decoration": "장식",
        "wall art": "벽장식", "wall": "벽", "art": "장식",
        "cabinet": "수납장", "storage": "수납", "organizer": "수납",
        "lamp": "조명", "light": "조명", "led": "조명", "mood": "무드등",
        "halloween": "파티용품", "party": "파티용품", "christmas": "파티용품",
        "kitchen": "주방용품", "bathroom": "욕실용품",
        "desk": "데스크", "office": "사무", "stationery": "문구",
        "rug": "러그", "carpet": "카페트", "curtain": "커튼",
        "candle": "캔들", "aroma": "아로마", "diffuser": "디퓨저",
        "clock": "시계", "mirror": "거울", "frame": "액자",
        "cushion": "쿠션", "pillow": "쿠션", "blanket": "담요",
        "shelf": "선반", "rack": "선반", "hook": "후크",
        "gothic": "장식", "vintage": "장식", "retro": "장식",
        "inflatable": "파티용품", "figurine": "장식인형",
        "planter": "화분", "pot": "화분",
        "tray": "트레이", "basket": "바구니",
    }

    keywords = []

    # 카테고리 힌트 먼저
    if category_hint:
        kr = en_to_kr.get(category_hint.lower().strip(), category_hint)
        keywords.append(kr)

    # 상품명에서 키워드 추출
    name_lower = product_name.lower()
    for en, kr in en_to_kr.items():
        if en in name_lower and kr not in keywords:
            keywords.append(kr)

    # 기본 폴백
    if not keywords:
        keywords = ["장식", "인테리어소품", "데코용품"]

    return keywords


def _score_match(whole_name: str, product_name: str, category_hint: str) -> int:
    """카테고리 매칭 점수 (높을수록 좋음)"""
    score = 0
    wn = whole_name.lower()
    pn = product_name.lower()

    # 카테고리 힌트와 일치
    if category_hint and category_hint.lower() in wn:
        score += 30

    # 상품명 키워드와 일치
    keywords = ["vase", "화병", "decor", "장식", "lamp", "조명", "storage", "수납",
                "kitchen", "주방", "party", "파티", "halloween", "desk", "데스크"]
    for kw in keywords:
        if kw in pn and kw in wn:
            score += 20

    # 가구/인테리어 카테고리 선호 (홈데코 상품이 대부분)
    if "가구/인테리어" in wn:
        score += 10
    if "인테리어소품" in wn:
        score += 15

    return score
