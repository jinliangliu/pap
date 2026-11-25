# app/main.py
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from app.agents.graph import init_agent_graph
from app.api.middleware import RequestContextMiddleware
from app.api.router import api_router
from app.api.routes.health import router as health_router
from app.core.config import settings
from app.core.logging import configure_logging
from app.db.checkpoint import close_checkpointer, init_checkpointer
from app.db.session import async_session_factory, dispose_engine
from app.integrations.http_client import close_platform_client
from app.integrations.sandbox_client import close_sandbox_client
from app.mcp_server import build_asgi as build_mcp_asgi
from app.mcp_server import get_session_manager as get_mcp_session_manager
from app.observability import metrics
from app.observability.tracing import setup_tracing
from app.observability.writer import get_writer as get_obs_writer
from app.services import runtime_settings
from app.services.connector_service import load_connector_tools
from app.services.custom_tool_service import load_custom_tools
from app.services.mcp_tool_service import load_registered_specs
from app.services.rate_limit_service import (
    close_rate_limiter,
    init_rate_limiter,
)
from app.services.run_service import interrupt_stale_runs
from app.services.task_runner_service import close_task_client
from app.tools.dependencies import get_tool_gateway
from app.tools.mcp_tools import register_mcp_tools
from app.workers.maintenance import (
    start_maintenance_task,
    stop_maintenance_task,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings)

    # 限流后端必须在接受请求前确定：多进程共享配额依赖 Redis，
    # 降级为进程内计数会让配额被放大到「上限 × 进程数」。
    await init_rate_limiter()

    # 工具注册表在启动阶段加载；未注册的工具永远不可见。
    gateway = get_tool_gateway()

    # 远端 MCP 工具要在接受请求前注册完：否则首批请求的模型目录里
    # 会出现「同一进程里时有时无」的工具集合。失败不阻塞启动。
    #
    # 白名单有两个来源：代码里的 MCP_TOOL_SPECS + 管理员在控制台接入的
    # mcp_registered_tools。后者读失败就当没有——能力缺失可以接受，起不来不行。
    try:
        async with async_session_factory() as session:
            extra_mcp_specs = await load_registered_specs(session)
    except Exception:
        extra_mcp_specs = ()
        logger.warning(
            "读取 MCP 接入表失败，本次只注册代码白名单里的工具。",
            extra={"event": "mcp_registered_tools_load_failed"},
            exc_info=True,
        )

    mcp_tool_count = await register_mcp_tools(gateway, extra_specs=extra_mcp_specs)

    # 控制台新建的 HTTP 工具也要在接受请求前注册完，理由同上。
    # 表还没迁移时启动不应失败：能力缺失可以接受，起不来不行。
    custom_tool_count = 0
    try:
        async with async_session_factory() as session:
            custom_tool_count = await load_custom_tools(session, gateway)
    except Exception:
        logger.warning(
            "加载自定义 HTTP 工具失败，本次按「没有自定义工具」处理。",
            extra={"event": "custom_tools_load_failed"},
            exc_info=True,
        )

    # 连接器下的 MCP 工具单独走一条：它们的 schema 必须连远端拉一次，
    # 而且端点与凭据跟着连接器，不该混进上面按 .env 默认通道的那一群。
    connector_tool_count = 0
    try:
        connector_tool_count = await load_connector_tools(gateway)
    except Exception:
        logger.warning(
            "加载连接器工具失败，本次按「没有连接器工具」处理。",
            extra={"event": "connector_tools_load_failed"},
            exc_info=True,
        )

    logger.info(
        "工具注册表已加载。",
        extra={
            "event": "tool_registry_loaded",
            "tool_count": len(gateway.list_tools()),
            "mcp_tool_count": mcp_tool_count,
            "custom_tool_count": custom_tool_count,
            "connector_tool_count": connector_tool_count,
        },
    )

    checkpointer = await init_checkpointer()
    init_agent_graph(checkpointer)

    # 上一个进程留下的 RUNNING 永远不会自己结束（协程随进程消失），
    # 不清掉会让监控一直以为有请求在跑。
    await interrupt_stale_runs()

    if settings.otel_exporter_otlp_endpoint:
        setup_tracing(app)

    # 运行配置必须在观测写入器之前读：start() 要用观测开关决定起不起后台任务。
    # load() 自己吞异常，表还没迁移时按默认值继续跑。
    await runtime_settings.load()

    # 观测写入器：开关关闭时 start() 直接返回，不建队列、不起任务。
    await get_obs_writer().start()

    task = start_maintenance_task()

    # 入向 MCP 通道的会话管理器必须由这里驱动。
    # `streamable_http_app()` 自带的 lifespan 在 mount 之后不会执行，
    # 只挂不 run 的表现是「端点存在、每个请求都 500」。
    mcp_manager = get_mcp_session_manager() if settings.mcp_inbound_enabled else None

    try:
        if mcp_manager is not None:
            async with mcp_manager.run():
                yield
        else:
            yield
    finally:
        await stop_maintenance_task(task)
        # 先排空观测队列再关数据库：放在 dispose_engine 之前，
        # 否则最后一批 trace 会因为「引擎已释放」而整批失败。
        await get_obs_writer().stop()
        await close_rate_limiter()
        await close_checkpointer()
        await close_platform_client()
        await close_sandbox_client()
        await close_task_client()
        await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
    )

    app.add_middleware(RequestContextMiddleware)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "X-Authenticated-Subject", "X-Request-ID"],
        )

    app.include_router(health_router)
    app.include_router(api_router, prefix=settings.api_prefix)

    @app.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
    async def metrics_endpoint() -> str:
        return metrics.render()

    # 对外 MCP 通道（WorkBuddy 连接器等的入口）。
    #
    # 单独一个路径而不是塞进 /api/v1：它走的不是平台的 REST 契约，
    # 而是 JSON-RPC，认证也换成 Bearer 令牌（见 app/mcp_server/auth.py）。
    # 挂在 /api/v1 下面会让「一次接口改造」和「外部宿主」绑在一起，
    # 改一处签名就得同步所有连接器。
    #
    # **挂在根路径上**是刻意的：挂 `/mcp` 时 Starlette 会用 307 把
    # `/mcp` 重定向到 `/mcp/`（Mount 只按前缀匹配），而连接器里填的
    # URL 不会自动带尾斜杠，部分客户端也不跟随重定向。挂根路径后
    # 由 `BearerGate` 自己判前缀，`/mcp` 与 `/mcp/` 都能直连。
    #
    # **必须在最后挂。** 路由按注册顺序匹配，`Mount("/")` 会吃掉它之后
    # 注册的一切——放在 /metrics 前面时，那个端点会静默变成 404。
    if settings.mcp_inbound_enabled:
        app.mount("/", app=build_mcp_asgi())
        logger.info(
            "对外 MCP 通道已挂载。",
            extra={
                "event": "mcp_server_mounted",
                "path": "/mcp",
                # 凭据来源写清楚：只接了 PAT 却以为自己配了静态令牌，
                # 排障时会一直去翻 .env，而问题在「有没有人签过令牌」。
                "credential_source": (
                    "static+pat"
                    if (settings.mcp_inbound_token or "").strip()
                    else "pat"
                ),
                "static_subject": settings.mcp_inbound_subject,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        # 对外不暴露堆栈、URL、SQL 或外部系统原始错误。
        logger.exception(
            "未处理异常。",
            extra={"event": "unhandled_exception", "path": request.url.path},
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "服务器内部错误，请稍后重试。"},
        )

    return app


app = create_app()
