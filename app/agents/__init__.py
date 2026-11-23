# app/agents/__init__.py
"""智能体包的公开出口。

**这三个名字是懒加载的，别改回顶层 import。** `graph` 会拉起 `nodes`，而 `nodes`
又 import `app.knowledge.service`；`app.knowledge.service` 反过来 import
`app.agents.prompts`——只要包初始化时就把 graph 拉进来，这个环就闭合了：

- 应用启动时先碰 agents，所以线上一直正常；
- 任何脚本或用例先 `import app.knowledge.service`（或先 import 工具包，它经
  `demo_tools` 回到知识库）就会 `partially initialized module`。

「能不能导入取决于谁先来」不该留在代码里。需要这三个函数的地方一律
`from app.agents.graph import ...`，本文件只保留旧写法可用。
"""

from __future__ import annotations

from typing import Any

__all__ = ["build_agent_graph", "get_agent_graph", "init_agent_graph"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from app.agents import graph

        return getattr(graph, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
