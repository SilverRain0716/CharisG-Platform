"""PA API 전용 SQLite 컨텍스트. DB 파일: purchase.db (19 테이블 + translation_cache)."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(
    os.environ.get(
        "PA_DB_PATH",
        str(Path(__file__).resolve().parent / "purchase.db"),
    )
)


@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    from backend_shared.migrations import MigrationRunner
    runner = MigrationRunner(str(DB_PATH))
    migrations_dir = Path(__file__).resolve().parent / "migrations"
    runner.apply_all([
        (1, migrations_dir / "schema_pa.sql", "purchase initial schema"),
        (2, migrations_dir / "schema_pa_v2.sql", "discovery pipeline: categories + runs"),
        (3, migrations_dir / "schema_pa_v3.sql", "sheet import: monthly_sales + category + notes"),
        (4, migrations_dir / "schema_pa_v4.sql", "products/listings_pa expand + image_cache + pricing settings seed"),
        (5, migrations_dir / "schema_pa_v5.sql", "products SEO columns + detail_pages html/market/platform"),
        (6, migrations_dir / "schema_pa_v6.sql", "sourcing_candidates image_url column"),
        (7, migrations_dir / "schema_pa_v7.sql", "batch_jobs for background AI processing"),
        (8, migrations_dir / "schema_pa_v8.sql", "listings_pa real margin fields"),
        (9, migrations_dir / "schema_pa_v9.sql", "inferred_attributes_json + batch_jobs phase_message"),
        (10, migrations_dir / "schema_pa_v10.sql", "coupang_categories tree + listings_pa coupang_category_code"),
        (11, migrations_dir / "schema_pa_v11.sql", "naver_coupang_category_map (3-stage auto-map)"),
        (12, migrations_dir / "schema_pa_v12.sql", "listings_pa approval_requested_at (coupang saveV2→approval flow)"),
        (13, migrations_dir / "schema_pa_v13.sql", "orders expansion: customs/sku/paid_at + EN translation + shipping_method"),
        (14, migrations_dir / "schema_pa_v14.sql", "sourcing_candidates price_krw column"),
        (15, migrations_dir / "schema_pa_v15.sql", "image_cache sha256 + naver_cdn_url for cross-product image reuse"),
        (16, migrations_dir / "schema_pa_v16.sql", "batch_jobs.phase column for per-stage UI labeling"),
        (17, migrations_dir / "schema_pa_v17.sql", "products SP-API Catalog facts cache columns (parent_asin + facts_json)"),
        (18, migrations_dir / "schema_pa_v18.sql", "Phase 3 variation: listing_options + variation_groups + category_split_rules + orders.child_product_id"),
        (19, migrations_dir / "schema_pa_v19.sql", "listings_pa.has_options for option-C extend marking"),
        (23, migrations_dir / "schema_pa_v23.sql", "keyword_category_map + category_review_queue (Fix 1-D)"),
        (24, migrations_dir / "schema_pa_v24.sql", "products: naver_attributes_json + coupang_attributes_json 분리"),
        (25, migrations_dir / "schema_pa_v25.sql", "product_keywords (N:M 키워드 매핑)"),
        (26, migrations_dir / "schema_pa_v26.sql", "listings_pa.coupang_auto_matched (B 안 차등 처리)"),
    ])
