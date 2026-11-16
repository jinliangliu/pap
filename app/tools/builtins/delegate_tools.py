# app/tools/builtins/delegate_tools.py
"""把一件事交给另一个智能体（A2A handoff）——模型侧的入口。

规则见 `docs/A2A.md`。三句话概括：

1. **单调收窄**：被委派方的边界必须是委派方的子集。判定只做一件事——禁止变宽；
2. **不同步继承上下文**：只传 `task`（和 A 判断必要的 `context`），不传原始消息、
   提示词、附件。理由不是省 token，是数据边界；
3. **写操作仍然要过人的手，只是审批单换了个人开**：本图不会跟着中断，子图停在
   审批上就把这件事原样报回去，由用户确认后再从子图那一跳恢复。

委派本身不审批：它不产生外部副作用。给委派也加审批只会导致「一次写操作两次确认」，
而人是会对确认卡闭眼的。

**这个文件现在只是一层壳。** 真正的执行在 `app/services/agent_invoker.invoke()`，
编排（`app/orchestration/`）走的是同一个函数。为什么必须共用：两边各写一份的下场是
编排绕过 `agent_subset`，而所有单测都还是绿的——那种缺陷不会在任何一条用例里显形，
只会在某次越权调用之后被人发现。
"""

from __future__ import annotations

import logging
from typing import Any

from app.models.approval import RiskLevel
from app.services import reference_service
from app.services.agent_invoker import (
    STATUS_AWAITING_APPROVAL,
    STATUS_FAILED,
    invoke,
)
from app.services.delegation_limits import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_PER_TURN,
)
from app.tools.types import (
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionResult,
)

logger = logging.getLogger(__name__)

DELEGATE_TOOL_NAME = "delegate_to_agent"

# 与 `docs/A2A.md` §8 一致。两个都是**平台默认**：Agent 上配了更严的值时以配置为准
# （见 `app/services/delegation_limits.py`，合并规则是取 min）。
# 名字保留成 `MAX_*` 是因为旧调用点与文档都在用；实际判定在 `agent_invoker`。
MAX_DEPTH = DEFAULT_MAX_DEPTH
MAX_PER_TURN = DEFAULT_MAX_PER_TURN

# 子调用的墙钟上限。外层还有 `chat_timeout_seconds`（160s）兜底，
# 这里更短是让「对方迟迟不回」尽早变成一个可解释的失败，而不是拖死整轮。
DELEGATE_TIMEOUT_SECONDS = 90


async def delegate_to_agent(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """模型侧的一次转交。形状与拆分前逐字一致（前端与节点都在读它）。"""
    invocation = await invoke(
        context,
        target=str(arguments.get("target") or "").strip(),
        task=str(arguments.get("task") or "").strip(),
        brief=str(arguments.get("context") or "").strip(),
        refs=arguments.get("refs"),
        # 模型即兴的转交要占「本轮最多几次」的额度；这是这条预算存在的理由。
        counts_against_budget=True,
        # 链条上已经出现过的 Agent 不能再进——A→B→A 是模型最容易踩的环。
        allow_revisit=False,
    )

    if invocation.status == STATUS_FAILED:
        payload: dict[str, Any] = {
            "agent": invocation.target,
            "status": "failed",
            "error_code": invocation.error_code,
        }
        payload.update(invocation.extra)
        return ToolExecutionResult(
            success=False,
            # 不是「暂时性失败」：重试同一个目标只会得到同一个结论。
            retryable=False,
            error_code=invocation.error_code,
            message=invocation.message,
            result=payload,
        )

    if invocation.status == STATUS_AWAITING_APPROVAL and invocation.approval:
        return ToolExecutionResult(
            success=True,
            # 未执行，不可能已核验。
            verified=False,
            result=invocation.approval,
            message=invocation.message,
        )

    return ToolExecutionResult(
        success=True,
        # 委派的结果是「对方的话」，不是「已核验的业务事实」：
        # 它没有经过回读，标成 verified 会让下游把转述当原始数据。
        verified=False,
        result={
            "agent": invocation.target,
            "display_name": invocation.display_name,
            "status": "completed",
            "summary": invocation.output,
            "evidence": invocation.evidence,
            "unknown": invocation.unknown,
            # 对方这一跳是不是只读的。P2 解除只读后，这个值不再恒为 True：
            # 发起方需要知道「它说做不了」是它的职责限制，还是这一层限制。
            "readonly": invocation.readonly,
            "depth": invocation.depth,
        },
        message=invocation.message,
    )


def build_delegate_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name=DELEGATE_TOOL_NAME,
            display_name="转交给其他智能体",
            description=(
                "把一件属于别的智能体职责范围的事交给它处理，拿回它的结论、依据与缺失项。"
                "只用于对方更专业的情形；简单问答直接回答，不要转交。"
                "对方看不到当前对话，需要它读某份文件、某条消息、某篇文档时，"
                "在 refs 里显式列出——否则它只能凭任务描述作答。"
                "对方执行写操作时会自己生成审批单：返回 status=awaiting_approval 时，"
                "说明这件事停在用户的确认上，如实转告即可，不要说成已经办完。"
            ),
            kind="python",
            # 委派本身不产生外部副作用：写操作由被委派方按自己的边界与审批执行。
            risk_level=RiskLevel.READ,
            requires_approval=False,
            required_scopes=("tools:read",),
            timeout_seconds=DELEGATE_TIMEOUT_SECONDS,
            input_schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 64,
                        "description": "目标智能体的 name（不是显示名）。",
                    },
                    "task": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 2000,
                        "description": (
                            "任务描述。对方看不到当前对话，必须写成能独立完成的一句话："
                            "对象、时间范围、要回答什么。"
                        ),
                    },
                    "context": {
                        "type": "string",
                        "maxLength": 1000,
                        "description": "对方完成任务所需的背景。只放必要信息，不要整段复述对话。",
                    },
                    "refs": {
                        "type": "array",
                        "maxItems": 10,
                        "description": (
                            "要一并交给对方的材料。默认什么都不传——"
                            "对方看不到当前对话，也不继承附件；"
                            "只有在这里显式列出，它才拿得到。"
                            "任何一项不可用（无权看 / 已失效）都会整单拒绝，"
                            "不会只送过去一部分。"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {
                                    "type": "string",
                                    "enum": [
                                        reference_service.REF_ARTIFACT,
                                        reference_service.REF_MESSAGE,
                                        reference_service.REF_KNOWLEDGE,
                                        reference_service.REF_ENTITY,
                                    ],
                                    "description": (
                                        "artifact=上传文件或生成的产物；"
                                        "message=本会话里的一条消息；"
                                        "knowledge=知识库文档；"
                                        "entity=业务对象标识（如站点、工单号）。"
                                    ),
                                },
                                "id": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 256,
                                    "description": "对应类型的标识。只能用上下文里真实出现过的值，不要自己编。",
                                },
                                "section": {
                                    "type": "string",
                                    "maxLength": 64,
                                    "description": "仅 knowledge 用：只要某一小节时填小节名。",
                                },
                            },
                            "required": ["kind", "id"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["target", "task"],
                "additionalProperties": False,
            },
            handler=delegate_to_agent,
            verification_hint="返回对方智能体的结论、依据引用与未确定项，未经回读核验。",
        )
    ]
