"""PA API SQLite 컨텍스트 — Hot/Cold DB 분리 (C1).

DB 파일:
  - purchase.db (cold): products, listings_pa, batch_jobs, image_cache 등 대량 처리
  - purchase_hot.db (hot): orders, order_steps, cs_tickets, returns_pa 실시간 운영

분리 이유: 대량 batch (detailing/upload) 가 cold.db 락 점유 시 orders/cs 페이지 timeout
방지. orders 그룹은 hot.db 로 격리하여 lock contention 영향 없음.

Cross-DB 접근:
  - Hot path: orders 의 denormalized 컬럼 (product_name_cache 등) 으로 cold.db 안 봄
  - Cold path (드물게): get_db_with_attach() 로 ATTACH 사용

기본:
  - get_db()       — cold.db (기존 호환, products/listings/batch 모두 여기)
  - get_db_hot()   — hot.db (orders/cs/returns)
  - get_db_with_attach() — hot 세션에 cold 를 'cold' 로 ATTACH (cross-DB JOIN)

PRAGMA:
  - busy_timeout=30000 (30s) — lock contention 시 reader 가 30초까지 wait
"""
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

DB_PATH_HOT = Path(
    os.environ.get(
        "PA_DB_PATH_HOT",
        str(Path(__file__).resolve().parent / "purchase_hot.db"),
    )
)

_BUSY_TIMEOUT_MS = 30000  # 30s — lock contention 회피


def _setup_conn(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")


@contextmanager
def get_db():
    """cold.db (기존 purchase.db) — products/listings_pa/batch 등."""
    conn = sqlite3.connect(str(DB_PATH))
    _setup_conn(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_db_hot():
    """hot.db — orders/order_steps/cs_tickets/returns_pa.

    실시간 운영 데이터. cold.db 락에 영향 안 받음.
    """
    conn = sqlite3.connect(str(DB_PATH_HOT))
    _setup_conn(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_db_with_attach():
    """hot.db 세션에 cold.db 를 'cold' 로 ATTACH.

    cross-DB JOIN 필요할 때만 사용. 예:
        with get_db_with_attach() as conn:
            conn.execute('SELECT o.*, p.title_ko FROM orders o '
                         'LEFT JOIN cold.products p ON p.id = o.product_id')

    Hot path 는 denormalized 컬럼 사용을 우선. ATTACH 는 dashboard 통계 등 드문 케이스만.
    """
    conn = sqlite3.connect(str(DB_PATH_HOT))
    _setup_conn(conn)
    conn.execute(f"ATTACH DATABASE '{DB_PATH}' AS cold")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.execute("DETACH DATABASE cold")
        except Exception:
            pass
        conn.close()


def init_db() -> None:
    """cold.db + hot.db 마이그레이션 적용.

    cold.db: 기존 schema_pa_v* 누적 적용 (orders 등 포함된 채 유지 — 마이그 후 DROP 예정).
    hot.db: schema_pa_hot.sql 적용 (없으면 신규 DB 생성).
    """
    from backend_shared.migrations import MigrationRunner
    migrations_dir = Path(__file__).resolve().parent / "migrations"

    # cold.db
    cold_runner = MigrationRunner(str(DB_PATH))
    cold_runner.apply_all([
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
        (27, migrations_dir / "schema_pa_v27.sql", "sheet_queue (대량 import 자동 파이프라인)"),
        (28, migrations_dir / "schema_pa_v28.sql", "sheet_queue.target_channels (채널 선택 업로드)"),
        (29, migrations_dir / "schema_pa_v29.sql", "coupons + coupon_items (쿠팡 즉시할인쿠폰 발급/추적)"),
        (31, migrations_dir / "schema_pa_v31.sql", "clean_violation_log: 클린 위반 이력 기록"),
        (32, migrations_dir / "schema_pa_v32.sql", "listings_pa winner_status (쿠팡 위너 모니터링)"),
        (33, migrations_dir / "schema_pa_v33.sql", "listings_pa coupang_seller_status (반려 동기화)"),
        (34, migrations_dir / "schema_pa_v34.sql", "listings_pa kr_shipping_eligible + checked_at (한국 직배 검증 캐시)"),
        (35, migrations_dir / "schema_pa_v35.sql", "listings_pa forwarder pricing 컬럼 (직배 불가 시 배대지 경유 가격 재산정)"),
    ])

    # hot.db — 별도 마이그레이션 (단일 schema 파일)
    hot_runner = MigrationRunner(str(DB_PATH_HOT))
    hot_runner.apply_all([
        (1, migrations_dir / "schema_pa_hot.sql", "hot.db: orders + cs + returns 분리"),
        (2, migrations_dir / "schema_pa_v30.sql", "orders.cancel_* 컬럼 (쿠팡 반품/취소 sync)"),
    ])
