# app/agents/schemas.py
"""规划器输出结构。

模型只负责把自然语言请求转换成 ActionPlan；
权限、审批、幂等、回读核验全部由服务端决定，模型没有系统权限。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ActionType(str, Enum):
    CHAT = "chat"
    CLARIFY = "clarify"
    READ_TOOL = "read_tool"
    WRITE_TOOL = "write_tool"
    KNOWLEDGE = "knowledge"


class ClarifyOption(BaseModel):
    """一个可选答案。

    `label` 是界面上那行字，`value` 是选中后真正回填给下一轮的内容。
    分成两个字段是因为它们经常不一致：「等级：高（影响整站供气）」
    适合给人看，回填给规划器的只需要「高」。

    两个字段都给了默认值：这个结构是**模型填的嵌套参数**，一旦某个选项漏了
    label 就整份 ActionPlan 校验失败，一次普通的反问会变成「模型服务不可用」。
    缺什么由 `app/agents/clarify.py` 丢掉，不让它污染整轮。
    """

    label: str = Field(
        default="",
        description="选项文案，界面直接显示。要能独立看懂，不要写「是/否」之外的光杆词。",
    )

    value: str | None = Field(
        default=None,
        description="选中后回填给下一轮的值；省略即用 label。",
    )


class ClarifyItem(BaseModel):
    """一条需要用户回答的追问。

    `options` 为空是合法的：站点名、金额、日期这类只能手填的内容不该被编造候选，
    界面会为它渲染一个输入框。
    """

    question: str = Field(
        default="",
        description="这一项要问的具体问题，不带编号（编号由界面生成）。",
    )

    field: str | None = Field(
        default=None,
        description="对应 missing_fields 里的字段名，仅当这项就是在补该字段时填写。",
    )

    options: list[ClarifyOption] = Field(
        default_factory=list,
        description=(
            "2–6 个互斥候选答案；候选确实可枚举时必填，"
            "只能手填时留空。不要为了凑数编造候选。"
        ),
    )


class ActionPlan(BaseModel):
    action: ActionType = Field(description="请求归类结果。")

    tool_name: str | None = Field(
        default=None,
        description="仅可填写服务端提供的 tools 列表中的 tool_name。",
    )

    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="工具输入，必须符合对应 input_schema。",
    )

    response: str | None = Field(
        default=None,
        description="action=chat 时面向用户的回答。",
    )

    clarification: str | None = Field(
        default=None,
        description="action=clarify 时向用户追问的内容。",
    )

    missing_fields: list[str] = Field(
        default_factory=list,
        description="action=clarify 时缺失的必填字段名。",
    )

    clarify_items: list[ClarifyItem] = Field(
        default_factory=list,
        description=(
            "action=clarify 时的分项追问（最多 3 项）。"
            "界面会把它渲染成单选卡片，用户点一下就当作回答；"
            "全部写进 clarification 里排版会让用户只能手打。"
        ),
    )

    query: str | None = Field(
        default=None,
        description="action=knowledge 时用于检索知识库的查询语句。",
    )

    reason: str = Field(
        default="",
        description="面向用户的简短执行说明，不得包含密钥或内部路径。",
    )
