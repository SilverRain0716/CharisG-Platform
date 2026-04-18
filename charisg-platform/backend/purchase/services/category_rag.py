"""네이버 카테고리 RAG — 임베딩 기반 후보 검색 + Gemini 선택.

마스터 테이블 (naver_categories)을 PA DB에 두고,
상품명 임베딩과 카테고리 임베딩의 코사인 유사도로 top-K 후보를 찾은 뒤
Gemini 에게 최종 선택시켜 leafCategoryId(숫자)를 반환한다.
"""
import json
import logging
import math
import os
import sqlite3
import struct
from typing import Optional

from backend.purchase.database import get_db
from backend_shared.ai import embed_batch, embed_text

logger = logging.getLogger(__name__)

LEGACY_CATEGORIES_DB = os.environ.get(
    "LEGACY_CATEGORIES_DB",
    "/home/ubuntu/dropship-crawler/categories.db",
)

# ── 규제 카테고리 Blacklist ─────────────────────────────────
# 네이버가 KC/어린이제품/화장품/도서/자동차 인증·권한을 요구하는 카테고리.
# 해외구매대행 셀러가 등록 시도해도 거절되므로 resolve_category 에서 우회한다.
# 실제 excluded 데이터 기반 + 패턴 확장.
BLACKLIST_PATH_PREFIXES = (
    "디지털/가전>",                         # KC (전자제품 전반)
    "생활/건강>자동차",                     # 등록권한 (자동차/부품)
    "생활/건강>공구>전기용품",              # KC
    "생활/건강>공구>운반용품",              # KC (핸드카트)
    "생활/건강>생활용품>생활잡화>핸드카트", # KC
    "생활/건강>수집품>모형",                # KC (다이캐스트/프라모델)
    "출산/육아>",                           # 어린이제품 인증
    "화장품/미용>향수",                     # 등록권한
    "도서>",                                # ISBN 필수
    "가구/인테리어>인테리어소품>조명",      # KC (조명)
)


def is_blacklisted_path(whole_name: str) -> bool:
    """네이버 카테고리 경로가 규제 카테고리인지."""
    if not whole_name:
        return False
    return any(whole_name.startswith(p) for p in BLACKLIST_PATH_PREFIXES)


def _pack_vec(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_vec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def ensure_master_table() -> None:
    with get_db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS naver_categories (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                whole_name TEXT NOT NULL,
                is_leaf INTEGER DEFAULT 1,
                embedding BLOB,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_naver_cat_name ON naver_categories(name)"
        )


def sync_from_legacy() -> int:
    """레거시 categories.db → PA DB naver_categories 테이블 병합 (UPSERT)."""
    ensure_master_table()
    if not os.path.exists(LEGACY_CATEGORIES_DB):
        raise FileNotFoundError(f"레거시 DB 없음: {LEGACY_CATEGORIES_DB}")

    src = sqlite3.connect(LEGACY_CATEGORIES_DB)
    src.row_factory = sqlite3.Row
    rows = src.execute(
        "SELECT id, name, whole_name, is_leaf FROM naver_categories"
    ).fetchall()
    src.close()

    with get_db() as conn:
        for r in rows:
            conn.execute(
                """INSERT INTO naver_categories (id, name, whole_name, is_leaf)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name,
                     whole_name=excluded.whole_name,
                     is_leaf=excluded.is_leaf,
                     updated_at=CURRENT_TIMESTAMP""",
                (r["id"], r["name"], r["whole_name"], r["is_leaf"] or 1),
            )
    return len(rows)


def build_embeddings(batch_size: int = 100, force: bool = False) -> dict:
    """임베딩 없는 카테고리(또는 force=True 시 전체)에 대해 배치로 임베딩 생성."""
    ensure_master_table()
    with get_db() as conn:
        if force:
            rows = conn.execute(
                "SELECT id, name, whole_name FROM naver_categories ORDER BY id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, name, whole_name FROM naver_categories WHERE embedding IS NULL ORDER BY id"
            ).fetchall()

    if not rows:
        return {"total": 0, "embedded": 0}

    total = len(rows)
    done = 0
    failed = 0

    for start in range(0, total, batch_size):
        chunk = rows[start:start + batch_size]
        texts = [r["whole_name"] for r in chunk]
        vecs = embed_batch(texts, task_type="SEMANTIC_SIMILARITY", batch_size=batch_size)
        with get_db() as conn:
            for r, v in zip(chunk, vecs):
                if v:
                    conn.execute(
                        "UPDATE naver_categories SET embedding=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (_pack_vec(v), r["id"]),
                    )
                    done += 1
                else:
                    failed += 1
        logger.info(f"[rag] 임베딩 진행 {start + len(chunk)}/{total} (성공 {done}, 실패 {failed})")

    return {"total": total, "embedded": done, "failed": failed}


def _cosine(a: list[float], b: list[float]) -> float:
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _load_all_embeddings() -> list[tuple[str, str, str, list[float]]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, whole_name, embedding FROM naver_categories WHERE embedding IS NOT NULL"
        ).fetchall()
    return [(r["id"], r["name"], r["whole_name"], _unpack_vec(r["embedding"])) for r in rows]


_MASTER_CACHE: dict = {"rows": None, "loaded_at": 0.0}


def _get_master() -> list[tuple[str, str, str, list[float]]]:
    import time
    if _MASTER_CACHE["rows"] is None or time.time() - _MASTER_CACHE["loaded_at"] > 3600:
        _MASTER_CACHE["rows"] = _load_all_embeddings()
        _MASTER_CACHE["loaded_at"] = time.time()
    return _MASTER_CACHE["rows"]


def search_candidates(query_text: str, k: int = 30) -> list[dict]:
    """쿼리 임베딩 → 상위 k개 카테고리 후보 (유사도 내림차순)."""
    qvec = embed_text(query_text, task_type="SEMANTIC_SIMILARITY")
    if not qvec:
        return []
    master = _get_master()
    scored = [
        {"id": cid, "name": name, "whole_name": wn, "score": _cosine(qvec, vec)}
        for (cid, name, wn, vec) in master
    ]
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:k]


async def resolve_category(product_name: str, source_hint: str = "", k: int = 30,
                           avoid_blacklist: bool = True) -> dict:
    """후보 k개 추린 뒤 Gemini 선택 → {mapped_category, confidence, method, category_name}.

    avoid_blacklist=True 이면 KC/어린이/향수/도서 등 규제 카테고리는 건너뛰고
    다음 비규제 후보를 우선 채택 (해외구매대행 셀러용 우회).
    """
    if not product_name:
        return {"mapped_category": "", "confidence": 0.0, "method": "empty"}

    query = product_name if not source_hint else f"{product_name} ({source_hint})"
    import asyncio
    candidates = await asyncio.to_thread(search_candidates, query, k)
    if not candidates:
        return {"mapped_category": "", "confidence": 0.0, "method": "no_candidates"}

    def _pick(pool: list[dict]) -> dict | None:
        if not avoid_blacklist:
            return pool[0] if pool else None
        for c in pool:
            if not is_blacklisted_path(c["whole_name"]):
                return c
        return None

    clean_top = _pick(candidates)
    top_raw = candidates[0]

    # 우회해서 뽑은 후보가 점수 충분히 높으면 바로 채택
    if clean_top and clean_top["score"] >= 0.75:
        method = "embedding_top1" if clean_top is top_raw else "embedding_avoid_blacklist"
        return {
            "mapped_category": clean_top["id"],
            "confidence": round(clean_top["score"], 3),
            "method": method,
            "category_name": clean_top["whole_name"],
        }

    # LLM 선택: 우회 모드면 비규제 후보만 보여주고, 아니면 전부
    llm_pool = [c for c in candidates if not is_blacklisted_path(c["whole_name"])] if avoid_blacklist else candidates
    if not llm_pool:
        llm_pool = candidates  # 전부 blacklist면 어쩔 수 없이 전체

    from backend_shared.ai.service import _call_ai_async
    numbered = "\n".join(
        f"{i+1}. {c['id']} | {c['whole_name']} (score={c['score']:.3f})"
        for i, c in enumerate(llm_pool[:30])
    )
    prompt = f"""당신은 네이버 스마트스토어 카테고리 분류 전문가입니다.
아래 상품에 가장 적절한 카테고리를 후보 목록에서 **딱 하나** 골라주세요.

상품명: {product_name}
원본 카테고리 힌트: {source_hint}

카테고리 후보 (번호 | ID | 전체경로 | 임베딩점수):
{numbered}

규칙:
- 반드시 위 후보 목록의 ID 중 하나만 선택 (목록에 없는 ID 금지).
- leaf(말단) 카테고리를 우선.
- 근거가 부족하면 confidence 를 0.3 이하로.

JSON만 반환:
{{"mapped_category": "<선택한 ID>", "confidence": 0.0~1.0}}"""

    result = await _call_ai_async(prompt, max_tokens=200)
    try:
        parsed = json.loads(result)
        mc = str(parsed.get("mapped_category", "")).strip()
        valid_ids = {c["id"] for c in llm_pool}
        if mc in valid_ids:
            return {
                "mapped_category": mc,
                "confidence": float(parsed.get("confidence", 0.5)),
                "method": "llm_select",
                "category_name": next((c["whole_name"] for c in llm_pool if c["id"] == mc), ""),
            }
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    fallback = clean_top or top_raw
    return {
        "mapped_category": fallback["id"],
        "confidence": round(fallback["score"], 3),
        "method": "embedding_fallback" if fallback is top_raw else "embedding_avoid_blacklist_fallback",
        "category_name": fallback["whole_name"],
    }
