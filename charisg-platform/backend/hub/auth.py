"""
backend/hub/auth.py — Hub API 인증 (JWT httpOnly 쿠키).

분리 원칙:
  - 토큰 발급/검증은 Hub만 수행
  - DS/PA API는 동일 SECRET_KEY로 검증만 수행 (각자 verify_token 사용)
  - 쿠키는 단일 도메인 httpOnly 로 3개 앱이 자동 공유
"""
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import Cookie, HTTPException, Request
from jose import JWTError, jwt

from backend_shared._config import (
    JWT_SECRET,
    JWT_ALGORITHM,
    JWT_EXPIRE_HOURS,
    AUTH_BYPASS,
)
from backend.hub.database import get_db

logger = logging.getLogger(__name__)

COOKIE_NAME = "charisg_session"
_BYPASS_USER = {"id": 0, "username": "admin", "role": "owner"}


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def create_token(user: dict) -> tuple[str, datetime]:
    expire = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS)
    payload = {
        "sub": user["username"],
        "uid": user["id"],
        "role": user.get("role", "viewer"),
        "exp": expire,
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return token, expire


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"invalid token: {e}")


def authenticate_user(username: str, password: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, role, email, name FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        return None
    return dict(row)


def store_session(user_id: int, token: str, expires: datetime, request: Request) -> None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    ua = request.headers.get("user-agent", "")
    ip = request.client.host if request.client else ""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO sessions(user_id, token_hash, expires_at, user_agent, ip)
               VALUES(?, ?, ?, ?, ?)""",
            (user_id, token_hash, expires.isoformat(), ua, ip),
        )


def revoke_session(token: str) -> None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with get_db() as conn:
        conn.execute("UPDATE sessions SET revoked=1 WHERE token_hash=?", (token_hash,))


def current_user(charisg_session: Optional[str] = Cookie(default=None)) -> dict:
    """FastAPI Depends() 용. 쿠키에서 토큰을 읽어 사용자를 반환."""
    if AUTH_BYPASS:
        return _BYPASS_USER

    if not charisg_session:
        raise HTTPException(status_code=401, detail="not authenticated")

    payload = decode_token(charisg_session)
    return {
        "id": payload.get("uid"),
        "username": payload.get("sub"),
        "role": payload.get("role", "viewer"),
    }


def ensure_admin_exists() -> None:
    """첫 기동 시 admin 계정 자동 생성. CTRL_ADMIN_PASS 환경변수 필요."""
    import os
    admin_user = os.environ.get("CTRL_ADMIN_USER", "admin")
    admin_pass = os.environ.get("CTRL_ADMIN_PASS", "")
    if not admin_pass:
        logger.warning("CTRL_ADMIN_PASS 미설정 — admin 계정 생성 스킵")
        return

    with get_db() as conn:
        row = conn.execute("SELECT id FROM users WHERE username=?", (admin_user,)).fetchone()
        if row:
            return
        conn.execute(
            "INSERT INTO users(username, password_hash, role, name) VALUES(?, ?, 'owner', ?)",
            (admin_user, hash_password(admin_pass), admin_user),
        )
        logger.info("admin 계정 생성됨: %s", admin_user)
