# app/agents/studio.py
"""LangGraph Studio 入口（由 `langgraph dev` 加载）。

Studio 与生产 API 共用同一批节点，差别只在入口：

- 生产：chat_service 依据服务端认证结果构造完整状态后调用图；
- Studio：只有对话输入，缺少 tenant_id / user_id / roles / scopes。

因此这里在 START 之后加一个 prepare_context 节点补齐运行时事实。

安全边界与 API 保持一致：身份只从服务端配置的开发身份与数据库授权表解析，
Studio 输入中出现的 user_id / tenant_id / roles 一律忽略。

注意工具注册表：生产的 MCP 工具在 `main.lifespan` 里注册，而 `langgraph dev`
不走 lifespan，所以这里必须自己注册一次（见 `_ensure_mcp_tools`），否则
Studio 里看不到任何 MCP 工具（连接器接入的 `fc_*` 与管理员接入项）。

本模块不参与生产服务启动，仅供 `langgraph dev` 与 Studio 观测使用。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from langgraph.config import get_config

from app.agents.graph import build_agent_graph
from app.agents.state import AgentState
from app.auth.dependencies import AuthenticationError, resolve_identity
from app.core.config import settings
from app.db.session import async_session_factory
from app.knowledge.index import get_knowledge_index
from app.services.mcp_tool_service import load_registered_specs
from app.tools.dependencies import get_tool_gateway
from app.tools.mcp_tools import register_mcp_tools

logger = logging.getLogger(__name__)

FALLBACK_THREAD_ID = "studio-local"

_mcp_ready = False
_mcp_lock: asyncio.Lock | None = None


def _get_mcp_lock() -> asyncio.Lock:
    """延迟创建锁：模块导入时可能还没有事件循环。"""
    global _mcp_lock

    if _mcp_lock is None:
        _mcp_lock = asyncio.Lock()

    return _mcp_lock


def reset_mcp_registration() -> None:
    """测试用：清掉「已注册」标记，让下一次 prepare_context 重新注册。"""
    global _mcp_ready, _mcp_lock

    _mcp_ready = False
    _mcp_lock = None


async def _ensure_mcp_tools() -> None:
    """
    把远端 MCP 工具注册进 Studio 进程的网关，只做一次。

    生产环境在 `main.lifespan` 里注册；而 `langgraph dev` 只导入 `graph`
    这个变量，永远不会执行 FastAPI 的 lifespan。缺了这一步，Studio 进程的
    注册表里只有本地 demo/station 工具，近端 MCP 能力全部不可见——模型问
    「压力趋势」只能选中 `get_station_snapshot`，再拿 MCP 的站点码（如
    T002509008）去过 PAP 自己的 `station_grants`（那是 station-001 这种内部
    ID），必然被判成「当前资源不可访问」。

    失败不阻塞会话：远端不可达时 `register_mcp_tools` 返回 0，此处只记日志，
    并且不再每轮重试（Studio 是观测工具，重启一次的成本低于每轮探测的延迟）。
    """
    global _mcp_ready

    if _mcp_ready:
        return

    async with _get_mcp_lock():
        if _mcp_ready:
            return

        # 与 main.lifespan 同源：代码白名单 + 管理员接入项。
        # 只传代码白名单会让「控制台接的工具在 Studio 里消失」，
        # 而这个差异只有对照两边工具目录才能发现。
        try:
            async with async_session_factory() as session:
                extra_specs = await load_registered_specs(session)
        except Exception:  # pragma: no cover - 读表失败按只有代码白名单处理
            extra_specs = ()
            logger.warning(
                "读取 MCP 接入表失败，Studio 只注册代码白名单里的工具。",
                extra={"event": "studio_mcp_registered_tools_load_failed"},
                exc_info=True,
            )

        try:
            count = await register_mcp_tools(get_tool_gateway(), extra_specs=extra_specs)
        except Exception:  # pragma: no cover - 注册异常不应让会话起不来
            logger.warning(
                "Studio 接入 MCP 工具失败，本次会话只能使用本地工具。",
                extra={"event": "studio_mcp_registration_failed"},
                exc_info=True,
            )
            _mcp_ready = True
            return

        _mcp_ready = True
        logger.info(
            "Studio 已接入 MCP 工具。",
            extra={"event": "studio_mcp_registered", "registered": count},
        )



def _warm_up_knowledge_stack() -> None:
    """
    预热分词器与检索索引。

    `langgraph dev` 默认启用 blockbuster，会把 jieba 首次初始化里的
    os.getcwd()、tempfile 访问判为阻塞调用并抛 BlockingError。
    这些是进程级的一次性加载，放在模块导入期（不在任务上下文内）完成即可，
    无需用 `--allow-blocking` 把整个阻塞检查关掉——否则真正的阻塞调用
    也会被一起放行。

    失败不致命：知识索引可能尚未构建，检索会按"无命中"处理。
    """
    try:
        from app.knowledge.retriever import tokenize

        tokenize("预热")
    except Exception:  # pragma: no cover - 预热失败不影响图加载
        logger.warning(
            "知识栈预热失败，检索将按需初始化。",
            extra={"event": "studio_warmup_failed"},
            exc_info=True,
        )


_warm_up_knowledge_stack()


def _current_thread_id() -> str | None:
    """读取 Studio / Agent Server 分配的 thread_id。"""
    try:
        config = get_config()
    except RuntimeError:
        # 不在 LangGraph 运行时内（例如被 import 时直接调用），退回默认值。
        return None

    thread_id = (config.get("configurable") or {}).get("thread_id")

    return str(thread_id) if thread_id else None


async def prepare_context(state: AgentState) -> dict[str, Any]:
    """补齐 Studio 输入缺失的运行时事实。"""
    await _ensure_mcp_tools()

    thread_id = state.get("thread_id") or _current_thread_id() or FALLBACK_THREAD_ID

    async with async_session_factory() as session:
        try:
            identity = await resolve_identity(
                session,
                subject=settings.dev_subject,
                tenant_id=settings.default_tenant_id,
            )
        except AuthenticationError as exc:
            raise RuntimeError(
                f"Studio 需要已存在的开发身份 {settings.dev_subject!r}，"
                "请先执行 python -m scripts.seed_dev_data 建立身份与站点授权。"
            ) from exc

        # 顺手预热检索索引：切片在 PG 里，读取本身是异步的，
        # 不会触发 blockbuster 的阻塞调用检查。
        try:
            await get_knowledge_index().get(session, settings.default_tenant_id)
        except Exception:  # pragma: no cover - 空库/未入库时不影响会话
            logger.warning("知识索引预热失败，检索将按需加载。", exc_info=True)

    logger.info(
        "Studio 会话上下文已注入。",
        extra={
            "event": "studio_context_prepared",
            "thread_id": thread_id,
            "subject": identity.subject,
        },
    )

    return {
        "thread_id": thread_id,
        # 每次新调用生成新的 run_id：同一线程发起第二次写操作时，
        # 审批幂等键（approval:{thread_id}:{run_id}）必须变化，
        # 否则会复用上一轮已完成的审批记录。
        # interrupt 恢复时本节点不会重跑，run_id 在恢复期间保持稳定。
        "run_id": str(uuid.uuid4()),
        "tenant_id": identity.tenant_id,
        "user_id": identity.user_id,
        "roles": list(identity.roles),
        "scopes": list(identity.scopes),
        "is_platform_admin": bool(identity.is_platform_admin),
    }


# langgraph.json 指向本变量。
# 不传 checkpointer：`langgraph dev` 会为图注入自己的持久化层，
# 否则会与生产的 checkpoint 文件争用同一个 SQLite 锁。
graph = build_agent_graph(None, prelude=prepare_context)
