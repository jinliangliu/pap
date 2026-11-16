# tests/unit/test_node_tenant_scope.py
"""图节点的租户上下文绑定。

RLS 的过滤条件来自事务里的 `app.tenant_id`，由 contextvar 提供。生产链路
在 chat_service 外面包了 `tenant_scope`，Studio 入口没有，靠 `_tenant_bound`
在节点边界补上。
"""

from __future__ import annotations

import pytest

from app.agents.nodes import _tenant_bound
from app.tenancy.context import (
    TenantIsolationError,
    current_tenant,
    tenant_scope,
)


async def test_tenant_bound_sets_context_during_execution():
    observed: list[str | None] = []

    @_tenant_bound
    async def node(state):
        observed.append(current_tenant())
        return {}

    await node({"tenant_id": "default"})

    assert observed == ["default"]
    # 退出后必须恢复，不能把租户泄漏到同一任务里的后续代码。
    assert current_tenant() is None


async def test_tenant_bound_is_reentrant_for_same_tenant():
    """生产链路已经在外层设过同一租户，节点再包一层不能报错。"""

    with tenant_scope("default"):
        result = await _probe_node({"tenant_id": "default"})

    assert result == "default"


async def test_tenant_bound_rejects_cross_tenant_switch():
    """同一执行流里换租户必须直接失败，而不是静默切换。"""

    with pytest.raises(TenantIsolationError), tenant_scope("test"):
        await _probe_node({"tenant_id": "default"})


@_tenant_bound
async def _probe_node(state):
    return current_tenant()
