# app/api/routes/health.py
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.core.config import settings
from app.db.checkpoint import get_checkpoint_pool, get_checkpointer
from app.db.session import async_session_factory
from app.integrations.sandbox_client import get_sandbox_client
from app.schemas.common import HealthResponse
from app.services.model_service import is_model_configured
from app.services.rate_limit_service import get_rate_limiter
from app.tools.dependencies import get_tool_gateway

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """/health 只说明 Web 进程存活，不检查依赖。"""
    return HealthResponse(
        status="ok",
        app_name=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        timestamp=datetime.now(UTC),
        checks={},
    )


@router.get("/ready", response_model=HealthResponse)
async def ready(response: Response) -> HealthResponse:
    """/ready 检查数据库、checkpointer、模型配置与工具注册表。"""
    checks: dict[str, bool] = {}

    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        checks["database"] = False

    try:
        get_checkpointer()
        # 解析到 PG 时做一次真实取连接，确认检查点库可写而非仅"对象已构造"。
        pool = get_checkpoint_pool()
        if pool is not None:
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
        checks["checkpointer"] = True
    except Exception:
        checks["checkpointer"] = False

    # 模型可能来自控制台的供应商配置，也可能来自 .env，
    # 不能只看 settings.openai_api_key，否则控制台里配好了这里还报 false。
    try:
        checks["model_configured"] = await is_model_configured()
    except Exception:
        checks["model_configured"] = bool(settings.openai_api_key)

    try:
        checks["tool_registry"] = len(get_tool_gateway().list_tools()) > 0
    except Exception:
        checks["tool_registry"] = False

    # 限流后端是否真的可用（Redis 配了但连不上，配额就不再共享）。
    try:
        limiter = get_rate_limiter()
        checks["rate_limiter"] = await limiter.ping()
        checks["rate_limit_shared"] = limiter.backend == "redis"
    except Exception:
        checks["rate_limiter"] = False
        checks["rate_limit_shared"] = False

    # 代码沙箱是外部可选依赖：不可用时必须在 checks 里可见，
    # 但不参与 ready 判定——对话、检索、场站工具仍然可用，
    # 只是 execute_code 会明确失败。让它拖垮整个就绪检查会误伤无关能力。
    if settings.sandbox_enabled:
        try:
            checks["sandbox"] = await get_sandbox_client().health()
        except Exception:
            checks["sandbox"] = False

    # 远端 MCP 同理：对端挂了只影响它那批工具，不该让整个服务判为不可用。
    # 但必须在这里可见，否则「模型突然看不到调压工具」只能靠翻日志才发现。
    # 判定用 source 而不是名字前缀：代码白名单已经空了（prv_ 那批下线，
    # 改由 FioCloud 连接器提供 fc_*），按前缀判会永远返回 false。
    if settings.mcp_enabled:
        try:
            checks["mcp_tools"] = any(
                tool.source == "mcp" for tool in get_tool_gateway().list_tools()
            )
        except Exception:
            checks["mcp_tools"] = False

    # 模型未配置时服务仍可运行（会返回明确的错误提示），因此不判定为不可用。
    ready_ok = (
        checks["database"]
        and checks["checkpointer"]
        and checks["tool_registry"]
        and checks["rate_limiter"]
    )

    if not ready_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status="ok" if ready_ok else "degraded",
        app_name=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        timestamp=datetime.now(UTC),
        checks=checks,
    )
