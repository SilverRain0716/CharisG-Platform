"""브랜드명 쿠팡 표준화 (2026-06-03).

쿠팡 기준: 한글/영어 표준이름, 띄어쓰기·특수문자 없이.
AI(Gemini)로 표준 브랜드명 결정 + brand_normalize_map 캐시(같은 브랜드 수천 상품이라 고유만 1회 호출).
이미 깨끗한(영숫자·한글만) 브랜드는 AI 없이 그대로. AI 실패 시 정규식 폴백.
"""
import re
import logging
from backend.purchase.database import get_db

logger = logging.getLogger(__name__)

# 영숫자·한글 외 = 특수문자/공백 (제거 대상)
_SPECIAL = re.compile(r"[^0-9A-Za-z가-힣]")


def _fallback(raw: str) -> str:
    """AI 미사용 폴백 — 특수문자·공백 제거."""
    return _SPECIAL.sub("", raw or "")[:30]


def _ensure_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS brand_normalize_map ("
        "brand_raw TEXT PRIMARY KEY, brand_std TEXT, "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )


def normalize_brand(brand_raw: str) -> str:
    """쿠팡 표준 브랜드명 반환. 캐시 우선 → 미스 시 AI → 실패 시 정규식 폴백."""
    if not brand_raw or not brand_raw.strip():
        return ""
    raw = brand_raw.strip()
    # 이미 깨끗하면(특수문자·공백 없음) 그대로 (BMW/PUMA/adidas 등)
    if not _SPECIAL.search(raw):
        return raw[:30]
    # 캐시 조회
    try:
        with get_db() as conn:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT brand_std FROM brand_normalize_map WHERE brand_raw=?", (raw,)
            ).fetchone()
            if row and row["brand_std"]:
                return row["brand_std"]
    except Exception as e:
        logger.warning(f"[brand-normalize] 캐시조회 실패 '{raw}': {e}")
    # AI 호출
    std = None
    try:
        from backend_shared.ai.service import _call_gemini
        prompt = (
            "아마존 브랜드명을 쿠팡 표준 브랜드명으로 변환해라.\n"
            "규칙: 한글 또는 영어 공식 표준이름 / 띄어쓰기·특수문자 절대 없이 / "
            "유명 브랜드면 공식 표기, 무명·제너릭이면 특수문자·공백만 제거.\n"
            "브랜드명만 한 줄로 출력(따옴표·설명 없이).\n"
            f"입력: {raw}\n출력:"
        )
        resp = _call_gemini(prompt, max_tokens=30)
        if resp:
            std = _SPECIAL.sub("", resp.strip().splitlines()[0])[:30]
    except Exception as e:
        logger.warning(f"[brand-normalize] AI 실패 '{raw}': {e}")
    if not std:
        std = _fallback(raw)
    # 캐시 저장
    try:
        with get_db() as conn:
            _ensure_table(conn)
            conn.execute(
                "INSERT OR REPLACE INTO brand_normalize_map (brand_raw, brand_std) VALUES (?, ?)",
                (raw, std),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"[brand-normalize] 캐시저장 실패 '{raw}': {e}")
    logger.info(f"[brand-normalize] '{raw}' → '{std}'")
    return std
