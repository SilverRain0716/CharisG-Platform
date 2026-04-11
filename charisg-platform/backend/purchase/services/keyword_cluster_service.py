"""
keyword_cluster_service.py — AI 기반 키워드 클러스터링.

Gemini/Claude 로 동의/유사 키워드를 군집화.
"""
import json
import logging
from typing import Optional

from backend_shared.ai import generate_seo  # 일반 AI 래퍼 사용
from backend_shared.ai.service import _call_ai_async
from backend.purchase.database import get_db

logger = logging.getLogger(__name__)


async def cluster_keywords(keywords: list[str]) -> list[dict]:
    """키워드 리스트 → 클러스터 결과.

    Returns:
        [{"label": "...", "representative": "...", "members": ["..."], "size": N}]
    """
    if not keywords:
        return []

    sample = keywords[:80]  # AI 토큰 한도 보호
    prompt = f"""다음 한국어 검색 키워드들을 의미가 비슷한 것끼리 묶어 클러스터링해주세요.

키워드:
{chr(10).join('- ' + k for k in sample)}

JSON 배열로만 답변하세요:
[{{"label": "클러스터명", "representative": "대표 키워드", "members": ["키워드1", "키워드2"]}}]"""

    raw = await _call_ai_async(prompt, max_tokens=2000)
    try:
        clusters = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"클러스터링 파싱 실패: {str(raw)[:200]}")
        return []

    out = []
    for c in clusters:
        out.append({
            "label": c.get("label", ""),
            "representative": c.get("representative", ""),
            "members": c.get("members", []),
            "size": len(c.get("members", [])),
        })
    return out


def store_clusters(clusters: list[dict]) -> int:
    """클러스터 → keyword_clusters 테이블 적재 + keywords.cluster_id 업데이트."""
    inserted = 0
    with get_db() as conn:
        for c in clusters:
            cur = conn.execute(
                "INSERT INTO keyword_clusters (label, representative, member_count) VALUES (?, ?, ?)",
                (c["label"], c["representative"], c["size"]),
            )
            cluster_id = cur.lastrowid
            for kw in c.get("members", []):
                conn.execute(
                    "UPDATE keywords SET cluster_id=? WHERE keyword=?",
                    (cluster_id, kw),
                )
            inserted += 1
    return inserted
