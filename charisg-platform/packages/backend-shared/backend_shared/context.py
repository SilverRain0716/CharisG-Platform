"""
backend_shared.context — 공용 모듈이 호스트 API의 DB 연결을 받는 통로.

각 API(Hub/DS/PA)는 기동 시 자신의 get_db 컨텍스트 매니저를
register_db_factory()로 주입한다. 공용 서비스는 get_db()로 연결을 얻는다.

설정값은 환경변수 직접 참조 (.env 일원화).
"""
from contextlib import contextmanager
from typing import Callable, Optional

_db_factory: Optional[Callable] = None


def register_db_factory(factory: Callable) -> None:
    """
    Args:
        factory: contextmanager. with factory() as conn 로 사용 가능해야 함.
    """
    global _db_factory
    _db_factory = factory


@contextmanager
def get_db():
    if _db_factory is None:
        raise RuntimeError(
            "backend_shared.context.get_db: db_factory가 등록되지 않았습니다. "
            "API 기동 시 register_db_factory(get_db)를 먼저 호출하세요."
        )
    with _db_factory() as conn:
        yield conn
