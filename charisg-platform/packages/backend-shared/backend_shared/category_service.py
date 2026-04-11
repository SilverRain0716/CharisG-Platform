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


def find_category_with_gemini(product_name: str, category_hint: str = "", features: list = None) -> dict:
    """
    Gemini를 사용해서 최적의 네이버 카테고리 매핑
    DB에 캐싱된 카테고리 목록 중 후보를 추려서 Gemini에게 최종 선택 요청
    """
    # 1차: 키워드 매칭으로 후보 추리기
    conn = _get_db()
    candidates = []

    search_terms = _extract_keywords(product_name, category_hint)
    for term in search_terms[:5]:
        rows = conn.execute(
            "SELECT id, whole_name FROM naver_categories WHERE whole_name LIKE ? LIMIT 5",
            (f"%{term}%",),
        ).fetchall()
        for r in rows:
            if r["id"] not in [c["id"] for c in candidates]:
                candidates.append({"id": r["id"], "whole_name": r["whole_name"]})

    conn.close()

    if not candidates:
        return find_best_category(product_name, category_hint)

    # 2차: Gemini에게 후보 중 최적 선택 요청
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        return find_best_category(product_name, category_hint)

    candidate_text = "\n".join([f"- {c['id']}: {c['whole_name']}" for c in candidates[:15]])
    features_text = ", ".join(features[:5]) if features else ""

    prompt = f"""다음 상품에 가장 적합한 네이버 스마트스토어 카테고리 ID를 선택하세요.

상품명: {product_name}
상품 카테고리 힌트: {category_hint}
상품 특징: {features_text}

후보 카테고리:
{candidate_text}

반드시 위 후보 중 하나의 ID만 숫자로 응답하세요. 다른 텍스트 없이 ID만 출력하세요."""

    try:
        from backend_shared.ai import gemini_limiter
        if not gemini_limiter.wait():
            logger.warning("Gemini 일간 한도 초과 — 카테고리 매핑 스킵")
            return find_best_category(product_name, category_hint)

        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=15,
        )

        if resp.status_code == 429:
            logger.warning("Gemini 429 — 카테고리 매핑 폴백")
            return find_best_category(product_name, category_hint)

        result = resp.json()
        text = result["candidates"][0]["content"]["parts"][0]["text"].strip()

        # ID 추출
        cat_id = "".join(c for c in text if c.isdigit())
        matched = next((c for c in candidates if c["id"] == cat_id), None)
        if matched:
            logger.info("Gemini 카테고리 매칭: %s → %s", product_name[:30], matched["whole_name"])
            return {"id": matched["id"], "name": matched["whole_name"].split(">")[-1], "whole_name": matched["whole_name"], "score": 100}

    except Exception as e:
        logger.warning("Gemini 카테고리 매칭 실패: %s", e)

    # Gemini 실패 시 키워드 매칭 폴백
    return find_best_category(product_name, category_hint)


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
