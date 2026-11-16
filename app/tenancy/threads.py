# app/tenancy/threads.py
"""会话线程 ID 的租户命名空间。

为什么不能直接用客户端传来的 thread_id 当 LangGraph 的 thread_id：

- `conversations.thread_id` 是全局唯一的，两个租户碰巧用同一个字符串就会互相覆盖；
- LangGraph 的 checkpoint 以 thread_id 为键，一旦猜到别人的 thread_id，
  就可能恢复别人被 interrupt 的图状态（里面带着那次调用的上下文）。

因此服务端一律把可信的 (tenant_id, user_id) 与客户端 ID 拼成内部线程 ID，
客户端只能决定后缀，不能决定归属。
"""

from __future__ import annotations

import re

_CLIENT_THREAD_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

MAX_CLIENT_THREAD_ID = 128


def validate_client_thread_id(value: str) -> str:
    candidate = (value or "").strip()

    if not _CLIENT_THREAD_PATTERN.match(candidate):
        raise ValueError(
            "thread_id 只能包含字母、数字、点、下划线、冒号和短横线，长度 1-128。"
        )

    return candidate


def internal_thread_id(*, tenant_id: str, user_id: str, client_id: str) -> str:
    """
    生成内部线程 ID。

    tenant_id 已由 `normalize_tenant_id` 限制为安全字符集；
    user_id 是 UUID 字符串；client_id 在前面校验过。
    分隔符 `:` 不在任何一段的允许字符集里，拼接不会产生歧义。
    """
    return f"{tenant_id}:{user_id}:{validate_client_thread_id(client_id)}"


def client_thread_id(internal_id: str) -> str:
    """从内部线程 ID 取回客户端可见部分，用于响应与日志。"""
    return internal_id.rsplit(":", 1)[-1]
