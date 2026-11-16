# app/agents/slots.py
"""必填槽位收集。

不再维护硬编码的“工具 → 字段”映射表：
必填字段直接来自工具注册表里的 input_schema.required，
避免注册表与槽位表长期不一致。
"""

from __future__ import annotations

from typing import Any

from app.tools.gateway import ToolGateway


def merge_slots(
    existing: dict[str, Any] | None,
    new_values: dict[str, Any] | None,
) -> dict[str, Any]:
    """合并已收集槽位；忽略 None 与空字符串，避免模型用空值覆盖已确认内容。"""
    merged = dict(existing or {})

    for key, value in (new_values or {}).items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        merged[key] = value

    return merged


def get_missing_required_slots(
    *,
    gateway: ToolGateway,
    tool_name: str,
    values: dict[str, Any],
) -> list[str]:
    try:
        tool = gateway.get_tool(tool_name)
    except Exception:
        return []

    required = tool.input_schema.get("required") or []

    return [
        field
        for field in required
        if field not in values or values[field] in (None, "")
    ]


def build_slot_question(*, missing_fields: list[str]) -> str:
    """一次优先追问一个字段，降低用户负担。"""
    if not missing_fields:
        return "请补充完成该操作所需的信息。"

    return f"请补充字段：{missing_fields[0]}。"
