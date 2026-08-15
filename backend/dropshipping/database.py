"""
backend/dropshipping/database.py — DS API 전용 SQLite 컨텍스트.

DB 파일: dropshipping.db (6개 테이블 + translation_cache)
"""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(
    os.environ.get(
        "DS_DB_PATH",
        str(Path(__file__).resolve().parent / "dropshipping.db"),
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
    schema = Path(__file__).resolve().parent / "migrations" / "schema_ds.sql"
    runner.apply(schema, version=1, description="dropshipping initial schema")

    # v2~v3: 멱등 컬럼 추가
    import sqlite3
    conn = sqlite3.connect(str(DB_PATH))
    try:
        _add_column(conn, "collected_products", "matched_asin", "TEXT")
        _add_column(conn, "collected_products", "warehouse_country", "TEXT DEFAULT 'US'")
        _add_column(conn, "collected_products", "cn_shipping_cost", "REAL")
        _add_column(conn, "listings", "marketplace", "TEXT NOT NULL DEFAULT 'US'")
        _add_column(conn, "asin_match_candidates", "marketplace", "TEXT NOT NULL DEFAULT 'US'")
        _add_column(conn, "account_health", "marketplace", "TEXT NOT NULL DEFAULT 'US'")
        # 환율 테이블
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS exchange_rates (
                currency TEXT PRIMARY KEY,
                rate_to_usd REAL NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            INSERT OR IGNORE INTO exchange_rates VALUES ('USD', 1.0, CURRENT_TIMESTAMP);
            INSERT OR IGNORE INTO exchange_rates VALUES ('CAD', 0.73, CURRENT_TIMESTAMP);
            INSERT OR IGNORE INTO exchange_rates VALUES ('MXN', 0.059, CURRENT_TIMESTAMP);
        """)
        # 기존 데이터 backfill
        conn.execute("UPDATE collected_products SET warehouse_country='US' WHERE us_warehouse=1 AND (warehouse_country IS NULL OR warehouse_country='')")

        # v4: asin_match_candidates UNIQUE 제약을 (product_id, asin, marketplace)로 변경
        _migrate_amc_unique(conn)

        conn.commit()
    finally:
        conn.close()


def _migrate_amc_unique(conn):
    """asin_match_candidates UNIQUE(product_id, asin) → UNIQUE(product_id, asin, marketplace).

    멱등: 이미 마이그레이션 완료됐으면 스킵.
    """
    # 현재 UNIQUE 제약 확인
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='asin_match_candidates'"
    ).fetchone()
    if not row:
        return
    ddl = row[0] or ""
    # 이미 marketplace 포함된 UNIQUE면 스킵
    if "product_id, asin, marketplace" in ddl.replace(" ", "").lower().replace(
        "product_id,asin,marketplace", "product_id, asin, marketplace"
    ):
        return
    # marketplace 컬럼이 DDL에 없으면 아직 ALTER만 된 상태 — 테이블 재생성 필요
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS asin_match_candidates_new (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id      INTEGER NOT NULL,
                asin            TEXT NOT NULL,
                amazon_title    TEXT,
                amazon_brand    TEXT,
                amazon_price    REAL,
                title_sim       REAL,
                price_compat    REAL,
                match_score     REAL,
                match_verdict   TEXT,
                selected        BOOLEAN DEFAULT 0,
                searched_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
                marketplace     TEXT NOT NULL DEFAULT 'US',
                UNIQUE(product_id, asin, marketplace)
            );
            INSERT OR IGNORE INTO asin_match_candidates_new
                (id, product_id, asin, amazon_title, amazon_brand, amazon_price,
                 title_sim, price_compat, match_score, match_verdict, selected,
                 searched_at, marketplace)
                SELECT id, product_id, asin, amazon_title, amazon_brand, amazon_price,
                       title_sim, price_compat, match_score, match_verdict, selected,
                       searched_at, COALESCE(marketplace, 'US')
                FROM asin_match_candidates;
            DROP TABLE asin_match_candidates;
            ALTER TABLE asin_match_candidates_new RENAME TO asin_match_candidates;
            CREATE INDEX IF NOT EXISTS idx_amc_product ON asin_match_candidates(product_id);
            CREATE INDEX IF NOT EXISTS idx_amc_verdict ON asin_match_candidates(match_verdict);
            CREATE INDEX IF NOT EXISTS idx_amc_market  ON asin_match_candidates(marketplace);
        """)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"asin_match_candidates 마이그레이션 스킵: {e}")


def _add_column(conn, table: str, column: str, col_type: str):
    """멱등 컬럼 추가."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 이미 존재
