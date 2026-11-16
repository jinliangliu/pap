# app/tools/registry.py
"""本地工具注册表。

工具只能从这里取得。模型无法通过任何参数指定 handler、URL 或 Shell 命令。
"""

from __future__ import annotations

from app.tools.types import ToolDefinition


class ToolRegistryError(RuntimeError):
    """注册表基础异常。"""


class ToolNotFoundError(ToolRegistryError):
    """请求的工具未注册。"""


class ToolAlreadyRegisteredError(ToolRegistryError):
    """同名工具已注册。"""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(
        self,
        tool: ToolDefinition,
        *,
        replace: bool = False,
    ) -> None:
        if tool.name in self._tools and not replace:
            raise ToolAlreadyRegisteredError(
                f"工具已注册：{tool.name}"
            )

        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(f"未注册的工具：{name}") from exc

    def contains(self, name: str) -> bool:
        return name in self._tools

    def unregister(self, name: str) -> bool:
        """移除一个工具。返回是否真的移除了。

        只有自定义工具会用到：删除配置后必须同时从注册表消失，
        否则模型还能看到一个「已经从库里删掉」的工具。
        """
        return self._tools.pop(name, None) is not None

    def list_tools(
        self,
        *,
        include_disabled: bool = False,
    ) -> list[ToolDefinition]:
        tools = sorted(self._tools.values(), key=lambda item: item.name)

        if include_disabled:
            return tools

        return [tool for tool in tools if tool.enabled]

    def __len__(self) -> int:
        return len(self._tools)
