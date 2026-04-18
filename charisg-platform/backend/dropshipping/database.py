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
        conn.commit()
    finally:
        conn.close()


def _add_column(conn, table: str, column: str, col_type: str):
    """멱등 컬럼 추가."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 이미 존재
