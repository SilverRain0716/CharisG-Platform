"""
backend_shared.migrations — 마이그레이션 러너 공용 로직.

각 API는 자신의 schema_*.sql을 가지고 있고,
MigrationRunner(db_path).apply(sql_path) 로 적용한다.
"""
import logging
import sqlite3
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)


class MigrationRunner:
    """SQLite 단일 파일 + 외부 SQL 스크립트로 마이그레이션 적용.

    schema_meta(version INT, applied_at TEXT) 테이블로 적용 이력 추적.
    """

    def __init__(self, db_path: Union[str, Path]):
        self.db_path = str(db_path)

    def _ensure_meta(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_meta (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                description TEXT
            )"""
        )
        conn.commit()

    def get_current_version(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            self._ensure_meta(conn)
            row = conn.execute(
                "SELECT MAX(version) FROM schema_meta"
            ).fetchone()
            return row[0] or 0

    def apply(
        self,
        sql_path: Union[str, Path],
        version: int = 1,
        description: str = "",
    ) -> bool:
        """
        Args:
            sql_path: 적용할 .sql 파일
            version: 마이그레이션 버전 (단조 증가)
            description: 설명 (옵션)
        Returns: True 적용됨, False 이미 적용된 버전이라 스킵
        """
        sql_path = Path(sql_path)
        if not sql_path.exists():
            raise FileNotFoundError(f"마이그레이션 파일 없음: {sql_path}")

        sql = sql_path.read_text(encoding="utf-8")

        with sqlite3.connect(self.db_path) as conn:
            self._ensure_meta(conn)
            current = conn.execute(
                "SELECT 1 FROM schema_meta WHERE version=?", (version,)
            ).fetchone()
            if current:
                logger.info(f"마이그레이션 v{version} 이미 적용됨 — 스킵")
                return False

            try:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_meta(version, description) VALUES(?, ?)",
                    (version, description or sql_path.name),
                )
                conn.commit()
                logger.info(f"✓ 마이그레이션 v{version} 적용 완료 — {sql_path.name}")
                return True
            except Exception as e:
                conn.rollback()
                logger.error(f"마이그레이션 v{version} 실패: {e}")
                raise

    def apply_all(self, migrations: list[tuple]) -> int:
        """
        Args:
            migrations: [(version, sql_path, description), ...] 정렬된 리스트
        Returns: 적용된 개수
        """
        applied = 0
        for version, sql_path, desc in sorted(migrations, key=lambda x: x[0]):
            if self.apply(sql_path, version, desc):
                applied += 1
        return applied
