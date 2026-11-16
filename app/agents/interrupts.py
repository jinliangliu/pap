# app/agents/interrupts.py
"""从执行图的返回值里取出中断载荷。

为什么单独一个模块：中断载荷在**两处**被读——父图（`chat_service` 看自己的
审批中断）与子图（`delegate_to_agent` 看被转交方停在审批上）。两处各写一份
解析，改了一处忘了另一处时不会报错，只会在某条路径上「审批凭空消失」。

同一个 `__interrupt__` 在不同 langgraph 版本里可能是对象（带 `.value`）
也可能是 dict，两条形状都要认——线上这两个分支都被走到过。
"""

from __future__ import annotations

from typing import Any


def extract_interrupt(result: Any) -> dict[str, Any] | None:
    """返回第一个中断的载荷（dict）；没有中断或形状不认识时返回 None。"""
    if not isinstance(result, dict):
        return None

    interrupts = result.get("__interrupt__")

    if not interrupts:
        return None

    first = interrupts[0]

    if hasattr(first, "value"):
        value = first.value
        return value if isinstance(value, dict) else None

    if isinstance(first, dict):
        value = first.get("value") or first
        return value if isinstance(value, dict) else None

    return None
