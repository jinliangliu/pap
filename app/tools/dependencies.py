# app/tools/dependencies.py
from __future__ import annotations

from functools import lru_cache

from app.services.audit_service import DbAuditSink
from app.services.authorization_service import DbStationAuthorizer
from app.services.rate_limit_service import get_rate_limiter
from app.tools.builtins.artifact_tools import build_artifact_tools
from app.tools.builtins.delegate_tools import build_delegate_tools
from app.tools.builtins.demo_tools import build_demo_tools
from app.tools.builtins.studio_admin_tools import build_studio_admin_tools
from app.tools.builtins.studio_tools import build_studio_tools
from app.tools.gateway import ToolGateway
from app.tools.mock_write_tools import build_write_tools
from app.tools.ocr_tools import build_ocr_tools
from app.tools.registry import ToolRegistry
from app.tools.sandbox_tools import build_sandbox_tools
from app.tools.station_tools import build_station_tools


@lru_cache(maxsize=1)
def get_tool_gateway() -> ToolGateway:
    """
    创建全局 Gateway 并注册本地工具。

    只有这里列出的工具，模型才可能看到和调用。
    新增能力必须显式注册，远端 MCP 发现的新工具不会自动暴露。
    """
    registry = ToolRegistry()

    gateway = ToolGateway(
        registry,
        station_authorizer=DbStationAuthorizer(),
        rate_limiter=get_rate_limiter(),
        audit_sink=DbAuditSink(),
    )

    for tool in (
        *build_demo_tools(),
        *build_station_tools(),
        *build_write_tools(),
        # 未配置 SANDBOX_BASE_URL 时这里返回空列表，代码执行能力不存在。
        *build_sandbox_tools(),
        # OCR 是模型自主选用的工具，不是流程节点：
        # 模型看到附件且需要图中文字时才会调用，纯文本对话不产生识别成本。
        *build_ocr_tools(),
        # 构建助手的管理工具。它们只被内置 Agent `studio` 的边界放行，
        # 对其它 Agent 不可见（边界外），所以注册在这里是安全的。
        *build_studio_tools(),
        # 同属构建助手：技能 / 工具 / 知识库。分成两个模块是因为
        # 「改智能体」和「改它能用的东西」是两类操作，文件混在一起会越长越难读。
        *build_studio_admin_tools(),
        # 委派：把一件事交给同租户的另一个智能体（只读，子集校验）。
        # 谁能用它由「Agent 的 allowed_tools 里有没有它」决定，没有额外开关。
        *build_delegate_tools(),
        # 产物：把生成的内容落成文件，绑当前会话。
        # 没有它，上一轮生成的表就只是回答里的一段文本，下一轮引用不到。
        *build_artifact_tools(),
    ):
        gateway.register_tool(tool)

    return gateway


def reset_tool_gateway() -> None:
    """测试用：清空缓存以便重新注册工具。"""
    get_tool_gateway.cache_clear()
