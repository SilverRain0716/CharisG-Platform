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
