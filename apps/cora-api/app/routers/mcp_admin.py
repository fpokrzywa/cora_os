import logging
from datetime import datetime
from typing import Annotated, Any, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.auth import CurrentUser, require_admin
from app.mcp import (
    McpClient,
    McpError,
    create_server,
    discover_and_cache,
    get_server_by_name,
    list_servers,
    update_server,
)
from app.mcp.registry import config_from_row

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/mcp", tags=["admin", "mcp"])


# ---------- Models ----------


class McpServerOut(BaseModel):
    id: str
    name: str
    description: Optional[str]
    server_type: str
    endpoint: str
    enabled: bool
    auth_type: Optional[str]
    auth_config: Optional[dict]
    capabilities: Optional[dict]
    created_at: datetime
    updated_at: datetime


class CreateMcpServerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z][A-Za-z0-9_\-]*$")
    description: Optional[str] = None
    server_type: str = Field(default="http", min_length=1, max_length=20)
    endpoint: str = Field(min_length=1, max_length=500)
    enabled: bool = True
    auth_type: Optional[str] = None
    auth_config: Optional[dict] = None


class UpdateMcpServerRequest(BaseModel):
    description: Optional[str] = None
    endpoint: Optional[str] = None
    enabled: Optional[bool] = None
    auth_type: Optional[str] = None
    auth_config: Optional[dict] = None
    clear_auth: bool = False


class TestConnectionResponse(BaseModel):
    server_name: str
    success: bool
    duration_ms: int
    error: Optional[str] = None


class CapabilitiesResponse(BaseModel):
    server_name: str
    cached: bool
    capabilities: Optional[dict]


# ---------- Helpers ----------


def _row_to_out(row: dict) -> McpServerOut:
    return McpServerOut(
        id=str(row["id"]),
        name=row["name"],
        description=row["description"],
        server_type=row["server_type"],
        endpoint=row["endpoint"],
        enabled=row["enabled"],
        auth_type=row["auth_type"],
        auth_config=row["auth_config"],
        capabilities=row["capabilities"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------- Endpoints ----------


@router.get(
    "",
    response_model=list[McpServerOut],
    summary="List all MCP servers (admin only)",
)
async def list_mcp(
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> list[McpServerOut]:
    rows = await list_servers()
    logger.info("admin list mcp: admin=%s count=%s", admin.id, len(rows))
    return [_row_to_out(r) for r in rows]


@router.post(
    "",
    response_model=McpServerOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new MCP server (admin only)",
)
async def create_mcp(
    req: CreateMcpServerRequest,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> McpServerOut:
    try:
        row = await create_server(
            name=req.name,
            description=req.description,
            server_type=req.server_type,
            endpoint=req.endpoint,
            enabled=req.enabled,
            auth_type=req.auth_type,
            auth_config=req.auth_config,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"mcp server {req.name!r} already exists",
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    logger.info("admin created mcp server: admin=%s name=%s", admin.id, req.name)
    return _row_to_out(row)


@router.patch(
    "/{server_name}",
    response_model=McpServerOut,
    summary="Update an MCP server: endpoint, enabled, description, or auth.",
)
async def patch_mcp(
    server_name: str,
    req: UpdateMcpServerRequest,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> McpServerOut:
    row = await update_server(
        server_name,
        description=req.description,
        endpoint=req.endpoint,
        enabled=req.enabled,
        auth_type=req.auth_type,
        auth_config=req.auth_config,
        clear_auth=req.clear_auth,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"mcp server {server_name!r} not found",
        )
    logger.info(
        "admin updated mcp server: admin=%s name=%s", admin.id, server_name
    )
    return _row_to_out(row)


@router.post(
    "/{server_name}/test",
    response_model=TestConnectionResponse,
    summary="Live connection check (ping → initialize fallback). (admin only)",
)
async def test_mcp(
    server_name: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> TestConnectionResponse:
    row = await get_server_by_name(server_name)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"mcp server {server_name!r} not found",
        )
    if not row["enabled"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"mcp server {server_name!r} is disabled",
        )
    logger.info(
        "admin test mcp: admin=%s server=%s endpoint=%s",
        admin.id,
        server_name,
        row["endpoint"],
    )
    client = McpClient(config_from_row(row))
    result = await client.ping()
    return TestConnectionResponse(
        server_name=server_name,
        success=result.success,
        duration_ms=result.duration_ms,
        error=result.error,
    )


@router.get(
    "/{server_name}/capabilities",
    response_model=CapabilitiesResponse,
    summary="Return cached MCP capabilities; pass ?refresh=true to rediscover.",
)
async def capabilities_mcp(
    server_name: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    refresh: bool = Query(default=False, description="Force live rediscovery"),
) -> CapabilitiesResponse:
    row = await get_server_by_name(server_name)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"mcp server {server_name!r} not found",
        )

    if refresh:
        if not row["enabled"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"mcp server {server_name!r} is disabled",
            )
        try:
            cap = await discover_and_cache(server_name)
        except McpError as exc:
            logger.warning(
                "mcp capabilities refresh failed: server=%s error=%s",
                server_name,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"discovery failed: {exc}",
            ) from exc
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc
        return CapabilitiesResponse(
            server_name=server_name,
            cached=False,
            capabilities=cap.as_dict() if cap else None,
        )

    return CapabilitiesResponse(
        server_name=server_name,
        cached=True,
        capabilities=row["capabilities"],
    )
