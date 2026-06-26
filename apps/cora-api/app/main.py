import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.auth import get_current_user, require_admin
from app.clients import close_clients, init_clients
from app.config import settings
from app.logging_config import configure_logging
from app.agents.registry import seed_agents
from app.mcp import seed_mcp_servers
from app.routers import (
    admin,
    agent_admin,
    auth,
    chat,
    chronos,
    conversations,
    credentials,
    delegations,
    governance,
    health,
    integration,
    integration_intents,
    integration_providers,
    execution_approval,
    execution_adapters,
    execution_switches,
    feature_flags,
    execution_governance,
    jobs,
    mcp_admin,
    memory,
    oauth,
    plans,
    provider_connectors,
    provider_execution,
    signal,
    tool_admin,
    tools,
    traces,
    workspaces,
)
from app.schema import init_schema

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info(
        "Starting %s (env=%s, dgx=%s, jwt_configured=%s)",
        settings.service_name,
        settings.cora_env,
        settings.dgx_model_endpoint or "<unset>",
        bool(settings.jwt_secret),
    )
    if not settings.jwt_secret:
        logger.warning(
            "JWT_SECRET is not set; /auth/* and protected endpoints will "
            "return 503 until configured"
        )
    await init_clients()
    await init_schema()
    await seed_agents()
    await seed_mcp_servers()
    try:
        yield
    finally:
        logger.info("Shutting down %s", settings.service_name)
        await close_clients()


app = FastAPI(
    title="Cora API",
    description="Cora AI OS backend — orchestration entry point for ATLAS and specialist agents.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://cora.local.arpa",
        "http://api.cora.local.arpa",
        "http://localhost:5173",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"status": "error", "detail": "internal server error"},
    )


_auth_required = [Depends(get_current_user)]

app.include_router(health.router)
app.include_router(health.worker_router)
app.include_router(auth.router)
app.include_router(chat.router, dependencies=_auth_required)
app.include_router(conversations.router, dependencies=_auth_required)
app.include_router(tools.router, dependencies=_auth_required)
app.include_router(memory.router, dependencies=_auth_required)
app.include_router(plans.router, dependencies=_auth_required)
app.include_router(workspaces.router, dependencies=_auth_required)
app.include_router(workspaces.sources_router, dependencies=_auth_required)
app.include_router(signal.router, dependencies=_auth_required)
app.include_router(chronos.router, dependencies=_auth_required)
app.include_router(integration.router, dependencies=_auth_required)
app.include_router(integration_intents.router, dependencies=_auth_required)
app.include_router(integration_providers.router, dependencies=_auth_required)
app.include_router(execution_approval.router, dependencies=_auth_required)
app.include_router(execution_adapters.router, dependencies=_auth_required)
app.include_router(feature_flags.router, dependencies=_auth_required)
app.include_router(execution_switches.router, dependencies=_auth_required)
app.include_router(execution_governance.router, dependencies=_auth_required)
app.include_router(credentials.router, dependencies=_auth_required)
app.include_router(provider_connectors.router, dependencies=_auth_required)
app.include_router(provider_execution.router, dependencies=_auth_required)
# OAuth router is NOT globally auth-gated: the provider callback is an
# unauthenticated browser redirect (validated by single-use state). The other
# endpoints enforce auth per-route via Depends(get_current_user).
app.include_router(oauth.router)
app.include_router(delegations.router, dependencies=_auth_required)
app.include_router(admin.router, dependencies=[Depends(require_admin)])
app.include_router(agent_admin.router, dependencies=[Depends(require_admin)])
app.include_router(mcp_admin.router, dependencies=[Depends(require_admin)])
app.include_router(tool_admin.router, dependencies=[Depends(require_admin)])
app.include_router(governance.router, dependencies=[Depends(require_admin)])
app.include_router(traces.router, dependencies=[Depends(require_admin)])
app.include_router(jobs.router, dependencies=[Depends(require_admin)])


@app.get("/", tags=["root"], summary="Service banner")
async def root() -> dict:
    return {
        "service": settings.service_name,
        "env": settings.cora_env,
        "version": app.version,
    }
