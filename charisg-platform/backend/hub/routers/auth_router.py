"""Hub auth router — /api/hub/auth/{login,logout,me}"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from backend.hub.auth import (
    COOKIE_NAME,
    authenticate_user,
    create_token,
    current_user,
    revoke_session,
    store_session,
)
from backend.hub.database import get_db
from backend_shared._config import JWT_EXPIRE_HOURS

router = APIRouter(prefix="/api/hub/auth", tags=["hub-auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    email: Optional[str] = None
    name: Optional[str] = None


@router.post("/login")
def login(req: LoginRequest, request: Request, response: Response):
    user = authenticate_user(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 올바르지 않습니다")

    token, expires = create_token(user)
    store_session(user["id"], token, expires, request)

    with get_db() as conn:
        conn.execute(
            "UPDATE users SET last_login_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), user["id"]),
        )

    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=JWT_EXPIRE_HOURS * 3600,
        path="/",
    )

    return {
        "user": UserResponse(
            id=user["id"],
            username=user["username"],
            role=user["role"],
            email=user.get("email"),
            name=user.get("name"),
        ).model_dump(),
    }


@router.post("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        revoke_session(token)
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: dict = Depends(current_user)):
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, username, role, email, name FROM users WHERE id=?",
            (user["id"],),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="user not found")
    return UserResponse(**dict(row)).model_dump()
