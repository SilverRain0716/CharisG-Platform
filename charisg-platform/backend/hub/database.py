"""
backend/hub/database.py — Hub API 전용 SQLite 컨텍스트.

DB 파일: hub.db (3개 테이블: users, sessions, app_settings)
"""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get("HUB_DB_PATH", str(Path(__file__).resolve().parent / "hub.db")))


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


def init_db():
    """기동 시 schema 적용."""
    from backend_shared.migrations import MigrationRunner
    runner = MigrationRunner(str(DB_PATH))
    schema = Path(__file__).resolve().parent / "migrations" / "schema_hub.sql"
    runner.apply(schema, version=1, description="hub initial schema")
