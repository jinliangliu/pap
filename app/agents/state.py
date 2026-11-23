# app/agents/state.py
from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph.message import add_messages

Intent = Literal[
    "chat",
    "clarify",
    "read_tool",
    "write_tool",
    "knowledge",
    "error",
]


class AgentState(TypedDict, total=False):
    """
    LangGraph 持久化状态。

    安全约束：状态中不写 API Key、Token、Cookie、完整敏感文档。
    thread_id 相同即恢复同一会话。
    """

    messages: Annotated[list[Any], add_messages]

    # 运行时事实：由服务端身份层注入，模型不可篡改。
    tenant_id: str
    user_id: str
    thread_id: str
    conversation_id: str
    run_id: str
    request_id: str

    roles: list[str]
    scopes: list[str]
    # 平台管理员：工具目录据此跳过 scope 检查，与路由层同一口径。
    is_platform_admin: bool

    # 本轮由哪个智能体执行，以及它的边界。
    # 这些值来自服务端解析结果（chat 路由 → agent_service），模型不可写入。
    # `agent_allowed_tools` 为 None 表示不收窄，为 [] 表示一个都不给——两者不能混。
    agent_id: str | None
    agent_name: str
    agent_display_name: str
    agent_instruction: str
    agent_revision: int
    agent_allowed_tools: list[str] | None
    agent_allowed_skills: list[str] | None
    # 允许使用的连接器名。与 agent_allowed_tools 是并集。
    agent_allowed_connectors: list[str] | None
    agent_can_write: bool
    # 委派预算：本 Agent 的配置（None = 平台默认），随边界一起冻结。
    agent_max_delegation_depth: int | None
    agent_delegation_budget: int | None

    intent: Intent
    planned_tool_name: str | None
    planned_arguments: dict[str, Any] | None
    planned_reason: str | None
    missing_fields: list[str]
    # 追问选项：`[{"question", "field", "options": [{"label", "value"}]}]`，
    # 已经过 `app/agents/clarify.py` 规范化。界面靠它渲染可就地点选的卡片。
    # 属于单轮产出，每轮开头由 `begin_turn` 清空。
    clarify_items: list[dict[str, Any]]

    retrieved_documents: list[dict[str, Any]]
    citations: list[dict[str, Any]]

    approval_id: str | None
    approval_preview: dict[str, Any] | None
    execution_status: str | None

    final_response: str | None
    response_metadata: dict[str, Any] | None
    error_category: str | None

    # 附件句柄（attachment_id / kind / mime / size / name），不存 base64。
    # 原始文件落盘在附件目录，由 ocr_document 工具按句柄读取。
    # 早期版本只有图片，字段名是 images；加入 PDF 后统一成 attachments。
    attachments: list[dict[str, Any]]

    # 更早的对话：{"summary": str, "turns": [{"role", "text"}]}，由
    # `context_service` 在轮间算好。
    #
    # **不要加进 `_PER_TURN_FIELDS`。** 那张表清的是「本轮产出了什么」，
    # 而这是本轮的输入，和 attachments、roles 同级。误放进归零表的表现是
    # 「历史注入了但模型看不见」，而且只在第二轮之后才出现。
    context_block: dict[str, Any]

    # 推理回路：模型每自主调用一次只读工具，结果就回灌给下一轮规划。
    # tool_steps 用于收敛，避免模型在工具之间无限自循环。
    tool_observations: list[dict[str, Any]]
    tool_steps: int

    # 委派链：已经进入过的智能体名字（按跳序）与本轮委派次数。
    #
    # 为什么不放 `_PER_TURN_FIELDS`：子调用会跑一遍同一个执行图，
    # `begin_turn` 在子图里也会执行一次——在那里清零，深度与环的记录当场失效。
    # 所以本轮归零放在入口（`chat_service` 构造状态时显式写空值），
    # 子调用则把父级的值原样带进来。
    delegation_path: list[str]
    delegation_count: int

    # 上游链（根 → 直接上级），每项 `{"id": ..., "name": ...}`。
    # 与 `delegation_path` 的区别见 `app/tools/types.py` 同名字段的说明。
    # 同样不放 `_PER_TURN_FIELDS`：它是跨图的执行纪律，不是本轮产出的内容。
    delegation_ancestors: list[dict[str, Any]]

    # 发起方所在的会话线程：写操作的结论要回到这里，而不是回到子线程。
    delegation_origin_thread: str

    # 本链上**已生效**的委派上限 `{"max_depth": int, "max_per_turn": int}`。
    #
    # 与 `agent_max_delegation_depth` 不是一回事：后者是这一环的配置，
    # 前者是把整条链的配置逐跳折进来之后的结果（取 min）。根节点没有这一项时
    # 由 `DelegationLimits.from_state(None)` 给出平台默认，所以入口不必显式写。
    delegation_limits: dict[str, int]

    # 子调用被审批中断时，由被转交方开出审批单，这里记下「等哪一张」。
    #
    # 为什么不能沿用 `approval_id`：那个字段属于**本图自己**的审批路径
    # （`create_and_wait_approval` 会中断本图）。委派来的审批单属于子图，
    # 本图并没有中断，混用会让「谁在等确认」这件事含混。
    pending_delegated_approval: dict[str, Any] | None
