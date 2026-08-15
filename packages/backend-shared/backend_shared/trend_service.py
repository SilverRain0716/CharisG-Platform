"""
trend_service.py — BSR 변동 감지 + 트렌드 신호 생성

어제 vs 오늘 순위 비교 → 급등 신호 자동 생성
collected_products의 rank 데이터를 기반으로 동작
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from backend_shared.context import get_db

logger = logging.getLogger(__name__)

# 급등 판단 기준
SURGE_THRESHOLD = 50        # 순위가 50위 이상 상승하면 급등
SURGE_PCT_THRESHOLD = 0.5   # 순위가 50% 이상 상승하면 급등
NEW_ENTRY_RANK_MAX = 30     # TOP30 안에 신규 진입하면 신호


def detect_bsr_changes() -> list[dict]:
    """
    BSR 변동 감지 — 어제 vs 오늘 수집 데이터 비교

    Returns: 생성된 trend_signal 목록
    """
    signals = []
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    with get_db() as conn:
        # 오늘 수집된 상품 (rank가 있는 것만)
        today_rows = conn.execute(
            """SELECT external_id, product_name, rank, source_price, category, image_url, source
               FROM collected_products
               WHERE rank IS NOT NULL
                 AND DATE(collected_at) = ?
               ORDER BY rank""",
            (today,),
        ).fetchall()

        # 어제 수집된 상품
        yesterday_rows = conn.execute(
            """SELECT external_id, rank
               FROM collected_products
               WHERE rank IS NOT NULL
                 AND DATE(collected_at) = ?""",
            (yesterday,),
        ).fetchall()

    if not today_rows:
        logger.info("📊 오늘 수집 데이터 없음 — BSR 감지 스킵")
        return signals

    # 어제 데이터를 external_id → rank 매핑
    yesterday_map = {row["external_id"]: row["rank"] for row in yesterday_rows if row["external_id"]}

    for row in today_rows:
        ext_id = row["external_id"]
        if not ext_id:
            continue

        today_rank = row["rank"]
        yesterday_rank = yesterday_map.get(ext_id)

        signal = None

        if yesterday_rank is None:
            # 어제 없었는데 오늘 TOP30 안에 진입
            if today_rank and today_rank <= NEW_ENTRY_RANK_MAX:
                signal = {
                    "source": row["source"] or "amazon",
                    "signal_type": "new_entry",
                    "keyword": row["product_name"] or row["category"] or "",
                    "external_id": ext_id,
                    "data": json.dumps({
                        "product_name": row["product_name"],
                        "category": row["category"] or "",
                        "today_rank": today_rank,
                        "price": row["source_price"],
                        "image_url": row["image_url"] or "",
                    }, ensure_ascii=False),
                    "score": max(0, 100 - today_rank),
                }
        else:
            # 순위 상승 감지
            rank_diff = yesterday_rank - today_rank
            if yesterday_rank > 0:
                pct_change = rank_diff / yesterday_rank
            else:
                pct_change = 0

            if rank_diff >= SURGE_THRESHOLD or pct_change >= SURGE_PCT_THRESHOLD:
                signal = {
                    "source": row["source"] or "amazon",
                    "signal_type": "rank_surge",
                    "keyword": row["product_name"] or row["category"] or "",
                    "external_id": ext_id,
                    "data": json.dumps({
                        "product_name": row["product_name"],
                        "category": row["category"] or "",
                        "yesterday_rank": yesterday_rank,
                        "today_rank": today_rank,
                        "rank_diff": rank_diff,
                        "pct_change": round(pct_change * 100, 1),
                        "price": row["source_price"],
                        "image_url": row["image_url"] or "",
                    }, ensure_ascii=False),
                    "score": min(100, rank_diff),
                }

        if signal:
            signals.append(signal)

    # DB에 저장
    if signals:
        _save_signals(signals)
        logger.info(f"📈 BSR 변동 감지: {len(signals)}개 신호 생성")

    return signals


def _save_signals(signals: list[dict]):
    """트렌드 신호 DB 저장 (중복 제거)"""
    with get_db() as conn:
        for s in signals:
            existing = conn.execute(
                "SELECT id FROM trend_signals WHERE signal_type=? AND keyword=? AND DATE(detected_at)=DATE('now')",
                (s["signal_type"], s["keyword"]),
            ).fetchone()
            if existing:
                continue
            conn.execute(
                """INSERT INTO trend_signals (source, signal_type, keyword, external_id, data, score)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (s["source"], s["signal_type"], s["keyword"],
                 s["external_id"], s["data"], s["score"]),
            )


def get_recent_signals(
    limit: int = 50,
    source: Optional[str] = None,
    signal_type: Optional[str] = None,
    days: int = 7,
) -> list[dict]:
    """최근 트렌드 신호 조회"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with get_db() as conn:
        query = "SELECT * FROM trend_signals WHERE detected_at >= ?"
        params = [since]
        if source:
            query += " AND source = ?"
            params.append(source)
        if signal_type:
            query += " AND signal_type = ?"
            params.append(signal_type)
        query += " ORDER BY score DESC, detected_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_bsr_history(external_id: str, days: int = 30) -> list[dict]:
    """특정 상품의 BSR 이력 (차트용)"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with get_db() as conn:
        rows = conn.execute(
            """SELECT DATE(collected_at) as date, rank, source_price
               FROM collected_products
               WHERE external_id = ? AND collected_at >= ? AND rank IS NOT NULL
               ORDER BY collected_at""",
            (external_id, since),
        ).fetchall()
    return [dict(r) for r in rows]


def get_category_stats() -> list[dict]:
    """카테고리별 수집 통계 (대시보드용) — 정규화된 카테고리만"""
    # 유효한 카테고리 목록 (키워드와 구분)
    valid_categories = {
        "home_decor", "home decor", "홈데코", "wall art", "kitchen",
        "주방용품", "생활용품", "뷰티", "펫", "반려동물", "전자기기",
        "패션", "스포츠", "사무용품", "자동차용품", "공구", "유아용품",
        "정원용품", "pet accessories", "jewelry", "office",
    }

    with get_db() as conn:
        rows = conn.execute(
            """SELECT category,
                      COUNT(*) as total,
                      AVG(source_price) as avg_price,
                      AVG(rating) as avg_rating,
                      MIN(rank) as best_rank
               FROM collected_products
               WHERE category IS NOT NULL AND category != ''
               GROUP BY category
               ORDER BY total DESC""",
        ).fetchall()

    # 카테고리 정규화: 유효 카테고리가 아니면 '기타'로 병합
    merged = {}
    for r in rows:
        cat = r["category"].lower().strip()
        if cat in valid_categories:
            label = r["category"]
        else:
            label = "기타"
        if label not in merged:
            merged[label] = {"category": label, "total": 0, "avg_price": 0, "_count": 0}
        merged[label]["total"] += r["total"]
        merged[label]["avg_price"] += (r["avg_price"] or 0) * r["total"]
        merged[label]["_count"] += r["total"]

    result = []
    for v in merged.values():
        v["avg_price"] = round(v["avg_price"] / v["_count"], 2) if v["_count"] else 0
        del v["_count"]
        result.append(v)

    result.sort(key=lambda x: x["total"], reverse=True)
    return result[:15]
