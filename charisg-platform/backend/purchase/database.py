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
    ])
