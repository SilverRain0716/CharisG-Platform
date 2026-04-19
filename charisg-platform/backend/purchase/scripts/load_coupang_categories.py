"""
load_coupang_categories.py — 쿠팡 카테고리 트리를 DB로 적재.

쿠팡 GET /v2/.../meta/display-categories 응답을 재귀 walk → coupang_categories 테이블 INSERT.

실행:
    cd /home/ubuntu/CharisG-Platform/charisg-platform
    set -a && source .env && set +a
    python3 -m backend.purchase.scripts.load_coupang_categories [--dry-run]
"""
import argparse
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

import requests

from backend.purchase.database import get_db, init_db
from backend.purchase.services.coupang_service import _signature, BASE


def fetch_tree() -> dict:
    """쿠팡 카테고리 트리 root 호출."""
    path = "/v2/providers/seller_api/apis/api/v1/marketplace/meta/display-categories"
    r = requests.get(BASE + path, headers=_signature("GET", path), timeout=60)
    r.raise_for_status()
    body = r.json()
    return body.get("data", body)


def walk(node: dict, breadcrumb: list[str], parent_code: int | None) -> list[tuple]:
    """트리 재귀 walk → INSERT 로우 목록.

    Returns: [(code, name, path, depth, status, is_leaf, parent_code), ...]
    """
    rows = []
    name = node.get("name", "")
    code = node.get("displayItemCategoryCode")
    status = node.get("status", "ACTIVE")
    children = node.get("child") or []
    is_leaf = 0 if children else 1
    full_path = " > ".join([p for p in breadcrumb + [name] if p])
    depth = len(breadcrumb)
    if code is not None and code != 0:  # ROOT 노드 제외
        rows.append((code, name, full_path, depth, status, is_leaf, parent_code))
    for c in children:
        rows.extend(walk(c, breadcrumb + ([name] if name and code != 0 else []), code if code != 0 else None))
    return rows


def insert_rows(rows: list[tuple]) -> int:
    """coupang_categories에 일괄 INSERT (REPLACE)."""
    with get_db() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO coupang_categories
               (code, name, path, depth, status, is_leaf, parent_code, synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            rows,
        )
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description="쿠팡 카테고리 트리 → DB 적재")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    init_db()  # v10 마이그레이션 보장
    logger.info("쿠팡 카테고리 트리 다운로드…")
    t0 = time.time()
    tree = fetch_tree()
    logger.info(f"  완료 ({time.time() - t0:.1f}s)")

    logger.info("트리 재귀 walk…")
    rows = walk(tree, [], None)
    leaves = sum(1 for r in rows if r[5])
    logger.info(f"  노드 {len(rows)}개 / 리프 {leaves}개")

    if args.dry_run:
        logger.info("[dry-run] DB INSERT 생략")
        for r in rows[:5]:
            logger.info(f"  sample: {r}")
        return

    logger.info("DB INSERT…")
    t0 = time.time()
    n = insert_rows(rows)
    logger.info(f"  {n}건 INSERT 완료 ({time.time() - t0:.1f}s)")

    # 검증
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM coupang_categories").fetchone()[0]
        leaf_cnt = conn.execute("SELECT COUNT(*) FROM coupang_categories WHERE is_leaf=1").fetchone()[0]
        active_leaf = conn.execute(
            "SELECT COUNT(*) FROM coupang_categories WHERE is_leaf=1 AND status='ACTIVE'"
        ).fetchone()[0]
    logger.info(f"DB 적재 후: total={total}, leaf={leaf_cnt}, active_leaf={active_leaf}")


if __name__ == "__main__":
    main()
