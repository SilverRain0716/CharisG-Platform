"""
shopify_service.py — Shopify 크롤링 DB(products.db) 조회 서비스
products.db의 shopify_products 테이블에서 위닝 상품 데이터를 읽어옴
"""
import sqlite3
import logging
from pathlib import Path
from typing import Optional
from contextlib import contextmanager

import os
from backend_shared._config import PROJECT_ROOT

logger = logging.getLogger(__name__)

# shopify_crawler.py가 사용하는 DB (control_tower.db와 별도)
PRODUCTS_DB_PATH = PROJECT_ROOT / "products.db"


@contextmanager
def get_products_db():
    """products.db 연결 (읽기 전용)"""
    if not PRODUCTS_DB_PATH.exists():
        logger.warning("products.db 파일 없음: %s", PRODUCTS_DB_PATH)
        yield None
        return

    conn = sqlite3.connect(str(PRODUCTS_DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def get_shopify_products(
    page: int = 1,
    per_page: int = 20,
    grade: Optional[str] = None,
    brand: Optional[str] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
    sort_by: str = "score",
    sort_dir: str = "desc",
) -> dict:
    """shopify_products에서 위닝 상품 조회 (페이지네이션 + 필터)"""
    with get_products_db() as conn:
        if conn is None:
            return {"items": [], "total": 0, "page": page, "per_page": per_page}

        # 테이블 존재 확인
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='shopify_products'"
        ).fetchone()
        if not table_check:
            return {"items": [], "total": 0, "page": page, "per_page": per_page}

        where_clauses = ["available = 1"]
        params = []

        if grade:
            where_clauses.append("grade = ?")
            params.append(grade)

        if brand:
            where_clauses.append("brand_key = ?")
            params.append(brand)

        if category:
            where_clauses.append("category = ?")
            params.append(category)

        if search:
            where_clauses.append("(title LIKE ? OR brand_name LIKE ? OR handle LIKE ?)")
            q = f"%{search}%"
            params.extend([q, q, q])

        where_sql = " AND ".join(where_clauses)

        # 정렬
        allowed_sorts = {"score", "price_usd", "price_krw", "crawled_at", "margin_pct"}
        sort_col = sort_by if sort_by in allowed_sorts else "score"
        sort_direction = "DESC" if sort_dir.lower() == "desc" else "ASC"

        # 총 개수
        count_sql = f"SELECT COUNT(*) as cnt FROM shopify_products WHERE {where_sql}"
        total = conn.execute(count_sql, params).fetchone()["cnt"]

        # 데이터 조회 — CJ 매칭 컬럼이 아직 없을 수 있으므로 체크
        cj_cols_exist = False
        try:
            col_check = conn.execute("PRAGMA table_info(shopify_products)").fetchall()
            col_names = {c[1] for c in col_check}
            cj_cols_exist = "cj_pid" in col_names
        except Exception:
            pass

        offset = (page - 1) * per_page
        if cj_cols_exist:
            extra_cols = ", source_type, cj_pid, cj_url, cj_price_usd, cj_ship_from_us, cj_match_score"
        else:
            extra_cols = ""

        data_sql = f"""
            SELECT id, source, brand_key, brand_name, shopify_id, handle,
                   title, title_kr, product_type, vendor, price_usd,
                   compare_at_price_usd, price_krw, available,
                   image_count, variant_count, score, grade, margin_pct,
                   crawled_at, product_url, category,
                   smartstore_listed, coupang_listed, images
                   {extra_cols}
            FROM shopify_products
            WHERE {where_sql}
            ORDER BY {sort_col} {sort_direction}
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(data_sql, params + [per_page, offset]).fetchall()

        items = []
        for row in rows:
            # 첫 번째 이미지 URL 추출
            images_raw = row["images"] or "[]"
            try:
                import json
                images_list = json.loads(images_raw)
                thumbnail = images_list[0] if images_list else None
            except (json.JSONDecodeError, IndexError):
                thumbnail = None

            # source_type: DB에 값이 있으면 사용, 없으면 브랜드 기반 추론
            db_source_type = row["source_type"] if "source_type" in row.keys() else ""
            source_type = db_source_type if db_source_type else _infer_source_type(row["brand_key"])

            # CJ 매칭 정보
            cj_pid = row["cj_pid"] if "cj_pid" in row.keys() else ""
            cj_url = row["cj_url"] if "cj_url" in row.keys() else ""
            cj_price = row["cj_price_usd"] if "cj_price_usd" in row.keys() else 0

            items.append({
                "id": row["id"],
                "source": "shopify",
                "source_type": source_type,
                "brand_key": row["brand_key"],
                "brand_name": row["brand_name"],
                "product_name": row["title"],
                "title_kr": row["title_kr"] or "",
                "product_type": row["product_type"] or "",
                "category": row["category"] or "",
                "price_usd": row["price_usd"],
                "compare_at_price_usd": row["compare_at_price_usd"],
                "price_krw": row["price_krw"],
                "score": row["score"],
                "grade": row["grade"],
                "margin_pct": row["margin_pct"],
                "image_count": row["image_count"],
                "variant_count": row["variant_count"],
                "thumbnail": thumbnail,
                "product_url": row["product_url"],
                "crawled_at": row["crawled_at"],
                "smartstore_listed": bool(row["smartstore_listed"]),
                "coupang_listed": bool(row["coupang_listed"]),
                "cj_pid": cj_pid or "",
                "cj_url": cj_url or "",
                "cj_price_usd": cj_price or 0,
                "status": _derive_status(row),
            })

        return {"items": items, "total": total, "page": page, "per_page": per_page}


def get_shopify_stats() -> dict:
    """Shopify 상품 통계 (KPI용)"""
    with get_products_db() as conn:
        if conn is None:
            return {"total": 0, "s_grade": 0, "a_grade": 0, "b_grade": 0,
                    "listed": 0, "brands": 0, "avg_score": 0}

        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='shopify_products'"
        ).fetchone()
        if not table_check:
            return {"total": 0, "s_grade": 0, "a_grade": 0, "b_grade": 0,
                    "listed": 0, "brands": 0, "avg_score": 0}

        stats = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN grade = 'S' THEN 1 ELSE 0 END) as s_grade,
                SUM(CASE WHEN grade = 'A' THEN 1 ELSE 0 END) as a_grade,
                SUM(CASE WHEN grade = 'B' THEN 1 ELSE 0 END) as b_grade,
                SUM(CASE WHEN smartstore_listed = 1 OR coupang_listed = 1
                    THEN 1 ELSE 0 END) as listed,
                COUNT(DISTINCT brand_key) as brands,
                ROUND(AVG(score), 1) as avg_score
            FROM shopify_products
            WHERE available = 1
        """).fetchone()

        return {
            "total": stats["total"] or 0,
            "s_grade": stats["s_grade"] or 0,
            "a_grade": stats["a_grade"] or 0,
            "b_grade": stats["b_grade"] or 0,
            "listed": stats["listed"] or 0,
            "brands": stats["brands"] or 0,
            "avg_score": stats["avg_score"] or 0,
        }


def get_shopify_brands() -> list[dict]:
    """등록된 브랜드 목록 + 각 브랜드별 상품 수"""
    with get_products_db() as conn:
        if conn is None:
            return []

        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='shopify_products'"
        ).fetchone()
        if not table_check:
            return []

        rows = conn.execute("""
            SELECT brand_key, brand_name, category,
                   COUNT(*) as product_count,
                   SUM(CASE WHEN grade IN ('S','A') THEN 1 ELSE 0 END) as winning_count,
                   ROUND(AVG(score), 1) as avg_score
            FROM shopify_products
            WHERE available = 1
            GROUP BY brand_key
            ORDER BY winning_count DESC
        """).fetchall()

        return [dict(r) for r in rows]


# ── 헬퍼 ──

# 구매대행 대상 브랜드 (공홈 직접 구매 → 배대지 경유)
PURCHASE_AGENT_BRANDS = {
    "blendjet", "glossier", "rhode", "olaplex", "therabody",
    "ridge_wallet", "brooklinen", "necessaire", "tower_28",
}


def _infer_source_type(brand_key: str) -> str:
    """브랜드별 소싱 유형 추론: dropship vs purchase_agent"""
    if brand_key in PURCHASE_AGENT_BRANDS:
        return "purchase_agent"
    return "dropship"


def _derive_status(row) -> str:
    """채널 등록 상태에서 status 도출"""
    if row["smartstore_listed"] or row["coupang_listed"]:
        return "등록됨"
    if row["grade"] in ("S", "A"):
        return "대기중"
    return "분석중"
