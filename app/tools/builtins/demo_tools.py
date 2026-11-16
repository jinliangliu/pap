# app/tools/builtins/demo_tools.py
"""低风险只读工具。

这些工具不触达外部写系统，但结果是真实的（服务端时钟、本地知识库检索），
测试与生产都能安全调用。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.knowledge.service import search_as_tool
from app.models.approval import RiskLevel
from app.tools.types import (
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionResult,
)


async def get_platform_time(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    now = datetime.now(UTC)

    return ToolExecutionResult(
        success=True,
        verified=True,
        result={
            "timezone": arguments.get("timezone", "UTC"),
            "timestamp": now.isoformat(),
        },
        message="已获取服务端时间。",
        verification={
            "source": "application_server_clock",
            "checked_at": now.isoformat(),
        },
    )


async def search_knowledge(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """在已授权的知识库分级内检索；权限过滤在检索前完成。"""
    documents, citations = await search_as_tool(
        query=str(arguments["query"]),
        scopes=context.scopes,
        limit=int(arguments.get("limit", 4)),
        tenant_id=context.tenant_id,
    )

    if not documents:
        return ToolExecutionResult(
            success=True,
            verified=True,
            result={"query": arguments["query"], "items": [], "total": 0},
            message="当前知识库中未找到足以支持该结论的资料。",
            verification={"source": "knowledge_index", "result_count": 0},
        )

    return ToolExecutionResult(
        success=True,
        verified=True,
        result={
            "query": arguments["query"],
            "items": [
                {
                    "title": document.get("title"),
                    "section": document.get("section"),
                    "updated_at": document.get("updated_at"),
                    "excerpt": str(document.get("text", ""))[:300],
                    "score": document.get("score"),
                }
                for document in documents
            ],
            "citations": [citation.to_dict() for citation in citations],
            "total": len(documents),
        },
        message="查询完成。",
        verification={"source": "knowledge_index", "result_count": len(documents)},
    )


def build_demo_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="get_platform_time",
            display_name="查询平台时间",
            description="读取应用服务器当前时间。",
            kind="python",
            risk_level=RiskLevel.READ,
            required_scopes=("tools:read",),
            input_schema={
                "type": "object",
                "properties": {
                    "timezone": {"type": "string", "maxLength": 64, "default": "UTC"}
                },
                "additionalProperties": False,
            },
            handler=get_platform_time,
            verification_hint="返回应用服务器的实际时间戳。",
        ),
        ToolDefinition(
            name="search_knowledge",
            display_name="检索知识库",
            description="在已授权的知识库中按关键词检索资料片段。",
            kind="python",
            risk_level=RiskLevel.READ,
            required_scopes=("knowledge.read",),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 500},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 4,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=search_knowledge,
            verification_hint="返回可追溯的片段与引用。",
        ),
    ]
