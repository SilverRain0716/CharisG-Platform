#!/usr/bin/env python3
"""
control_tower.db (모노리스) → hub.db / dropshipping.db / purchase.db 분리 마이그레이션.

EC2 에서 실행:
    python3 scripts/migrate_from_monolith.py /home/ubuntu/dropship-crawler/control_tower.db

- collected_products WHERE business_model='dropship'  → dropshipping.db.collected_products
- collected_products WHERE business_model='purchase_agent' → purchase.db.products (필드 매핑)
- amazon_search_results / amazon_search_agg → dropshipping.db (전부)
- listings (모노리스 11 cols) → dropshipping.db.listings (호환 superset)
- users / sessions → hub.db (있으면)
- 나머지 모노리스 전용 테이블(import_jobs, channels 등)은 마이그레이션 대상 외

실행 후 schema_meta(version=1) 가 각 DB 에 자동 기록됨.
"""
import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("migrate")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.hub import database as hub_db_mod
from backend.dropshipping import database as ds_db_mod
from backend.purchase import database as pa_db_mod


def fetch_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def copy_table_filtered(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    src_table: str,
    dst_table: str,
    where: str = "",
    column_map: dict = None,
) -> int:
    """모노리스 → 신규 DB 로 동일/매핑 복사. 양쪽 컬럼 교집합만 복사."""
    src_cols = fetch_columns(src, src_table)
    dst_cols = fetch_columns(dst, dst_table)
    column_map = column_map or {}

    common = []
    for sc in src_cols:
        target = column_map.get(sc, sc)
        if target in dst_cols:
            common.append((sc, target))

    if not common:
        log.warning(f"{src_table} → {dst_table}: 공통 컬럼 없음, 스킵")
        return 0

    src_cols_q = ", ".join(f'"{s}"' for s, _ in common)
    dst_cols_q = ", ".join(f'"{t}"' for _, t in common)
    placeholders = ", ".join("?" * len(common))

    where_sql = f" WHERE {where}" if where else ""
    rows = list(src.execute(f"SELECT {src_cols_q} FROM {src_table}{where_sql}"))

    if not rows:
        log.info(f"  {src_table} → {dst_table}: 0 rows")
        return 0

    dst.executemany(
        f"INSERT OR REPLACE INTO {dst_table} ({dst_cols_q}) VALUES ({placeholders})",
        rows,
    )
    dst.commit()
    log.info(f"  {src_table} → {dst_table}: {len(rows)} rows ({len(common)} cols)")
    return len(rows)


def main(monolith_path: str) -> int:
    if not os.path.exists(monolith_path):
        log.error(f"모노리스 DB 없음: {monolith_path}")
        return 2

    log.info(f"source: {monolith_path}")
    log.info(f"hub.db:           {hub_db_mod.DB_PATH}")
    log.info(f"dropshipping.db:  {ds_db_mod.DB_PATH}")
    log.info(f"purchase.db:      {pa_db_mod.DB_PATH}")

    # 신규 DB 마이그레이션 적용
    hub_db_mod.init_db()
    ds_db_mod.init_db()
    pa_db_mod.init_db()

    src = sqlite3.connect(monolith_path)
    src.row_factory = sqlite3.Row

    hub = sqlite3.connect(str(hub_db_mod.DB_PATH))
    ds  = sqlite3.connect(str(ds_db_mod.DB_PATH))
    pa  = sqlite3.connect(str(pa_db_mod.DB_PATH))

    src_tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    # ─── HUB ───
    log.info("\n[HUB]")
    if "users" in src_tables:
        copy_table_filtered(src, hub, "users", "users")

    # ─── DROPSHIPPING ───
    log.info("\n[DROPSHIPPING]")
    if "collected_products" in src_tables:
        copy_table_filtered(
            src, ds, "collected_products", "collected_products",
            where="business_model='dropship'",
        )
    if "amazon_search_results" in src_tables:
        copy_table_filtered(src, ds, "amazon_search_results", "amazon_search_results")
    if "amazon_search_agg" in src_tables:
        copy_table_filtered(src, ds, "amazon_search_agg", "amazon_search_agg")
    if "listings" in src_tables:
        # 모노리스 listings 는 business_model 컬럼이 없음 → 모든 행을 일단 dropshipping.db 에 복사
        # (PA 분리는 나중에 platform/shop_id 기준으로 추가 분류)
        copy_table_filtered(src, ds, "listings", "listings")

    # ─── PURCHASE AGENT ───
    log.info("\n[PURCHASE AGENT]")
    if "collected_products" in src_tables:
        # PA 후보 22행을 sourcing_candidates 로 매핑
        pa_cols = fetch_columns(src, "collected_products")
        rows = list(src.execute(
            "SELECT id, external_id as asin, product_name as title, url as amazon_url, "
            "image_url, source_price as price_usd, rating, review_count, "
            "stock_quantity, hard_filter_pass as cj_filter_pass, collected_at "
            "FROM collected_products WHERE business_model='purchase_agent'"
        ))
        if rows:
            pa.executemany(
                """INSERT OR IGNORE INTO sourcing_candidates
                   (id, asin, title, amazon_url, image_url, price_usd, rating,
                    review_count, in_stock, cj_filter_pass, collected_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                  1 if (r[8] or 0) > 0 else 0, r[9], r[10]) for r in rows],
            )
            pa.commit()
            log.info(f"  collected_products(purchase_agent) → sourcing_candidates: {len(rows)} rows")

    # cs_tickets / orders: 모노리스 컬럼 구조가 PA 와 다름 (channel/current_step 등 NOT NULL).
    # 모노리스에 데이터가 거의 없고 (각 2건) 의미가 다르므로 skip.
    # 향후 PA 운영 시작 후 새로 적재.
    if "cs_tickets" in src_tables:
        n = src.execute("SELECT COUNT(*) FROM cs_tickets").fetchone()[0]
        log.info(f"  cs_tickets ({n} rows): skip (모노리스/PA 컬럼 구조 비호환)")
    if "orders" in src_tables:
        n = src.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        log.info(f"  orders ({n} rows): skip (모노리스/PA 컬럼 구조 비호환)")

    src.close()
    hub.close()
    ds.close()
    pa.close()

    log.info("\n✓ 마이그레이션 완료")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("monolith_db", help="control_tower.db 경로")
    args = parser.parse_args()
    sys.exit(main(args.monolith_db))
