"""
DS API 토큰 검증. Hub 가 발급한 JWT 를 같은 SECRET 으로 검증만 한다.
"""
from typing import Optional

from fastapi import Cookie, HTTPException
from jose import JWTError, jwt

from backend_shared._config import (
    JWT_SECRET,
    JWT_ALGORITHM,
    AUTH_BYPASS,
)

COOKIE_NAME = "charisg_session"
_BYPASS_USER = {"id": 0, "username": "admin", "role": "owner"}


def current_user(charisg_session: Optional[str] = Cookie(default=None)) -> dict:
    if AUTH_BYPASS:
        return _BYPASS_USER
    if not charisg_session:
        raise HTTPException(status_code=401, detail="not authenticated")
    try:
        payload = jwt.decode(charisg_session, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"invalid token: {e}")
    return {
        "id": payload.get("uid"),
        "username": payload.get("sub"),
        "role": payload.get("role", "viewer"),
    }
