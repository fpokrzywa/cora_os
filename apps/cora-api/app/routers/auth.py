import logging
import uuid
from datetime import datetime
from typing import Annotated, Optional

import asyncpg
import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field

from app.auth import (
    CurrentUser,
    create_access_token,
    decode_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.clients import clients

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_bearer_optional = HTTPBearer(auto_error=False)


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    display_name: Optional[str] = Field(default=None, max_length=120)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    id: str
    email: str
    display_name: Optional[str]
    role: str
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


def _require_pool():
    if clients.db_pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Postgres pool unavailable",
        )
    return clients.db_pool


def _row_to_user(row) -> UserOut:
    return UserOut(
        id=str(row["id"]),
        email=row["email"],
        display_name=row["display_name"],
        role=row["role"],
        created_at=row["created_at"],
    )


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user (first call bootstraps admin; later calls require admin auth)",
)
async def register(
    req: RegisterRequest,
    creds: Annotated[
        Optional[HTTPAuthorizationCredentials], Depends(_bearer_optional)
    ],
) -> TokenResponse:
    pool = _require_pool()
    email_lc = req.email.strip().lower()

    async with pool.acquire() as conn:
        user_count = await conn.fetchval("SELECT COUNT(*) FROM users")
        is_bootstrap = user_count == 0

        if not is_bootstrap:
            if creds is None or not creds.credentials:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="authentication required to create additional users",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            try:
                payload = decode_token(creds.credentials)
            except jwt.PyJWTError as exc:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"invalid token: {exc}",
                    headers={"WWW-Authenticate": "Bearer"},
                ) from exc
            if payload.get("role") != "admin":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="admin role required",
                )

        try:
            row = await conn.fetchrow(
                """
                INSERT INTO users (email, display_name, password_hash, role)
                VALUES ($1, $2, $3, 'admin')
                RETURNING id, email, display_name, role, created_at, updated_at
                """,
                email_lc,
                req.display_name,
                hash_password(req.password),
            )
        except asyncpg.UniqueViolationError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="email already registered",
            ) from exc

    user = _row_to_user(row)
    token = create_access_token(uuid.UUID(user.id), user.email, user.role)
    logger.info(
        "user registered: email=%s bootstrap=%s", email_lc, is_bootstrap
    )
    return TokenResponse(access_token=token, user=user)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange email + password for a JWT access token",
)
async def login(req: LoginRequest) -> TokenResponse:
    pool = _require_pool()
    email_lc = req.email.strip().lower()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, email, display_name, password_hash, role,
                   created_at, updated_at
            FROM users WHERE LOWER(email) = $1
            """,
            email_lc,
        )

    if row is None or not verify_password(req.password, row["password_hash"]):
        # Identical error regardless of which side failed (avoid enumeration)
        logger.warning("login failed: email=%s", email_lc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid email or password",
        )

    user = _row_to_user(row)
    token = create_access_token(uuid.UUID(user.id), user.email, user.role)
    logger.info("user login: email=%s user_id=%s", email_lc, user.id)
    return TokenResponse(access_token=token, user=user)


@router.get(
    "/me",
    response_model=UserOut,
    summary="Return the authenticated user",
)
async def me(
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> UserOut:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, email, display_name, role, created_at
            FROM users WHERE id = $1
            """,
            current.id,
        )
    if row is None:
        # Token valid but user was deleted
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="user no longer exists",
        )
    return _row_to_user(row)
