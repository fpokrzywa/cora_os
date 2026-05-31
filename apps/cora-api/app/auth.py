"""Authentication primitives: bcrypt password hashing + JWT issuance/decoding
+ a FastAPI dependency that resolves the current user from a bearer token."""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _require_secret() -> str:
    if not settings.jwt_secret:
        logger.error("JWT_SECRET is not configured; auth endpoints will fail")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="JWT_SECRET is not configured",
        )
    return settings.jwt_secret


def create_access_token(user_id: uuid.UUID, email: str, role: str) -> str:
    secret = _require_secret()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int(
            (now + timedelta(minutes=settings.jwt_expires_minutes)).timestamp()
        ),
    }
    return jwt.encode(payload, secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    secret = _require_secret()
    return jwt.decode(token, secret, algorithms=[settings.jwt_algorithm])


@dataclass
class CurrentUser:
    id: uuid.UUID
    email: str
    role: str


async def get_current_user(
    creds: Annotated[
        Optional[HTTPAuthorizationCredentials], Depends(_bearer)
    ],
) -> CurrentUser:
    if (
        creds is None
        or creds.scheme.lower() != "bearer"
        or not creds.credentials
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_token(creds.credentials)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    try:
        return CurrentUser(
            id=uuid.UUID(payload["sub"]),
            email=payload["email"],
            role=payload.get("role", "admin"),
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="malformed token payload",
        ) from exc


async def require_admin(
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin role required",
        )
    return user
