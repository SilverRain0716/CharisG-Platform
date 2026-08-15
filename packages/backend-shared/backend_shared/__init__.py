"""
backend_shared — Charis G 플랫폼 공용 백엔드 모듈.

각 API(Hub/DS/PA)는 자신의 database.py와 .env를 로드한 뒤
backend_shared.context.register_db_factory(get_db) 를 호출해
공용 모듈에 DB 컨텍스트를 주입한다.
"""
__version__ = "1.0.0"

from .context import register_db_factory, get_db

__all__ = ["register_db_factory", "get_db", "__version__"]
