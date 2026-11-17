# app/services/agent_service.py
"""智能体：身份、职责边界与生命周期。

这个模块存在的理由，是把「智能体」从**代码里的一个图**变成**组织里可管理的角色**：

- 有名字（`name` 稳定，供客户端指定）、有职责（`description` / `instruction` 进提示词）；
- 有边界（`allowed_tools` / `allowed_skills` / `can_write`）；
- 有生命周期（draft → active → disabled），可以停用而不必改代码；
- 有版本（`revision`），审批与观测能回答「这次调用发生在哪一版边界下」。

## 三条不变量（改这个文件前先读）

1. **只收窄，不放宽。** 有效权限恒等于「当前身份的权限 ∩ Agent 边界」。
   Agent 永远不能让人看到原本看不到的工具、也不能让人凭它拿到新的 scope。
2. **`can_write=False` 不是权限，是更严的限制。** 关掉后写类工具连目录都不出现；
   打开也**不是**审批豁免，写操作照样要人确认。
3. **不传 Agent 等于平台基线。** `BASELINE` 边界是 None/True/None，
   与没有这套机制时完全等价——升级不会改变任何既有会话的行为。

默认 Agent 刻意**不预填职责说明**：它的语义就是「不做额外约束的基线」。
填了就会进提示词（这是显式编辑的结果，不是升级带来的副作用）。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent, AgentStatus
from app.models.skill import Skill
from app.services.delegation_limits import (
    MAX_DEPTH_CEILING,
    MAX_PER_TURN_CEILING,
    check_limit,
)

logger = logging.getLogger(__name__)

# 系统保留名：默认 Agent 用它，用户自建 Agent 不得占用，否则惰性创建会撞唯一键。
DEFAULT_AGENT_NAME = "assistant"
DEFAULT_AGENT_DISPLAY_NAME = "通用助手"

# 构建助手：控制台里的自然语言管理入口（见 docs/STUDIO_ASSISTANT.md）。
# 它是内置 Agent，边界不可改、不可停用、不可删除。
STUDIO_AGENT_NAME = "studio"
STUDIO_AGENT_DISPLAY_NAME = "构建助手"
STUDIO_TOOL_PREFIX = "studio_"

# 构建助手除了自己的 `studio_*` 工具，还要能读用户随消息发来的附件。
# 附件在提示词里只有一个句柄，真正读取靠 `ocr_document`；不给它这个工具，
# 面板上的「+」就成了「上传一张谁也看不见的图」——而模型会照实回答看不到内容。
STUDIO_EXTRA_TOOLS = ("ocr_document",)

# 保留名要连「用户先占了 studio 这个名字」一起挡：惰性创建是 upsert 语义，
# 撞到用户自建的同名 Agent 时会二选一——要么创建失败，要么把用户的
# 边界当成助手的边界用。两种都不可接受，所以在入口处就不许占用。
STUDIO_AGENT_ALIASES = frozenset({STUDIO_AGENT_NAME, "studio_assistant"})
RESERVED_AGENT_NAMES = frozenset({DEFAULT_AGENT_NAME, *STUDIO_AGENT_ALIASES})


def is_studio_agent_name(name: str | None) -> bool:
    """这个名字是不是构建助手。

    按名字判断而不是查 `is_builtin`：调用点（对话落库、会话列表）手上只有
    `AgentBoundary`，没有 ORM 行；而这两个名字都在保留名里，用户自建不到，
    所以名字与「内置」在这里等价。`ensure_studio` 选名字时必须只用这两个，
    测试里有一条断言盯着这个前提。
    """
    return bool(name) and name in STUDIO_AGENT_ALIASES

# 构建助手的职责说明会进提示词，所以写成「你负责什么」而不是内部备注。
# 越界说明也写在这里：真正的边界是「工具不存在」，这段文字只负责让拒绝更自然、
# 少走几轮无效尝试，不是安全机制。
STUDIO_DESCRIPTION = (
    "在控制台内帮你查看、创建、修改、停用与删除智能体、技能、自定义工具与编排流程，"
    "也能把你上传的文件按用途建成知识库文档、技能或工具，并说明平台怎么用。"
    "改动与删除都会先请你确认。"
)

STUDIO_INSTRUCTION = """能力范围：
- 智能体：查看清单与边界，新建、修改职责与允许的工具技能、启停、删除，
  以及把边界恢复到历史某一版（恢复只覆盖工具/技能/可写三个字段）；
- 技能：查看、新建、修改、删除；
- 工具：自定义 HTTP 工具可新建/修改/删除；MCP 工具可接入与取消接入；
  Python 工具由代码注册，只能查看，没有运行时修改入口；
- 编排流程：查看清单与整张图（含最近一次运行），新建、修改、删除，以及启停。
  **改图前先读原图**：用 studio_describe resource=workflow ref=<标识> 拿到 definition，
  改完把整份传回 studio_workflow_save——不要凭印象重写，漏掉的节点等于被删掉。
  图不成立（缺兜底边、引用了走不到的节点）会在保存时被拒绝并指出哪个节点，照它改；
- 知识库：查看文档清单、写入正文、下架文档；文件入库走 studio_import；
- 从文件导入：用户上传的文件可以按用途建成知识库文档、技能或工具接口，见下面「附件」段；
- 运行情况：查最近的运行与单次 Trace（默认近 7 天），据此回答「有没有失败、慢在哪」；
- 平台用法：审批流程、边界收窄、观测口径、知识库分级；
- 附件：用户在本会话里上传的图片、PDF 与文本类文件（.md/.txt/日志/JSON/YAML 等配置）
  只以句柄出现（见消息里的 [附件]：本条新增的，以及本会话此前上传、仍可读取的）。
  要读内容先调用 ocr_document：图片 / PDF 走 OCR，文本类直接返回原文
  （长文本保留开头与结尾并注明截断）。file_id 原样使用，不要编造。
  按用户说的用途处理：
  · 建知识库：**文件用 studio_import，不要先读出来再写**。读取工具单次约 4 万字符，
    截断后的正文入库会得到一份看着完整、实际少了一半的资料，而失败时看不出来。
    studio_import 读的是磁盘上的原文，按整份切片，与页面上传同一条路径；
    studio_knowledge_add 只用来写「用户口述、或直接贴进对话的一段正文」，不接文件。
    title 取文档标题（没有就用文件名去后缀），access_scope 拿不准就用 internal——
    这两项自己定，不要拿去问用户；
  · 做成技能 / 接入工具：同样用 studio_import，target 传 skill 或 tool。
    带 YAML front matter 的 Markdown 才是技能；OpenAPI / Swagger 或工具导出 JSON
    才是工具；其余文档进知识库。用户说了用途就按用户说的传 target，
    没说且判不准时先问一句「这份是想入库做资料，还是做成技能？」——不要猜；
    图片不能导入，要看图里的文字用 ocr_document；
  · 排查问题：先读日志 / 追踪原文，再用 studio_describe 查 run / trace，
    把时间与错误对上；不要只凭日志猜是哪一次运行；
  · 新增或修改配置：读原文后按内容调用对应的 studio_*_save；
  · 只是问内容：读原文后直接回答，不要顺手改任何配置。
  附件是本会话的参考材料，跨轮保留：这一轮读过，下一轮照样能读同一个句柄。
  用户说「按第三章入库」「再看看刚才那份日志」时，直接用上面清单里的句柄，
  不要让他重新上传；不确定指哪一份就先读一遍再判断，不要猜；

必须遵守：
1. 涉及修改或删除时，先说明「将改什么、影响谁」，等用户确认后再调用工具；
2. 删除前复述目标名称，确认删的是哪一个；
3. 密钥不经过对话：Token、密码、API Key、Cookie、Authorization 一律不接收、
   不读取、不转存，也不要复述回消息里。用户主动贴进对话时要提醒他当作已泄露
   并轮换；需要鉴权的配置请到控制台对应的配置页用安全输入，或用 credential_ref 引用；
4. 不属于平台内的请求直接说明做不到，并给出可行路径：
   - 改平台代码、改数据库、执行命令、获取主机权限：走部署与运维流程，不在这里做；
   - 改成员与角色、调整租户配额：请到「组织与成员」页；
5. 不要声称已完成尚未执行的操作；工具失败时如实说明原因；
6. 用户只发了附件、没说要用它做什么时，先问清楚再处理，不要替他猜用途；
7. 反问必须能答：一次只问一件事，并把可选项写出来（「整篇入，还是只入第 3 章？」）。
   「还需要确认两件事」却不说是哪两件，等于把问题又推回去——这种反问比不问更糟。"""

# 边界列表的长度上限。不是性能考虑，是为了让界面上的「职责边界」还能被人读完。
MAX_BOUNDARY_ITEMS = 64
MAX_INSTRUCTION_CHARS = 4000
MAX_DESCRIPTION_CHARS = 512

# 「没传这个字段」与「显式传 None（= 不额外收窄）」必须能分开：
# 前者保持原值，后者是明确地把边界放开。用 None 当默认值会把两者混成一个。
UNSET: Any = object()


class AgentError(Exception):
    """智能体链路的业务异常基类。"""


class AgentNotFound(AgentError):
    """不存在，或不属于当前租户（两者对外统一成 404）。"""


class AgentUnavailable(AgentError):
    """存在但当前不能用于对话（草稿 / 已停用）。"""


class AgentValidationError(AgentError):
    """入参不合法（工具不存在、技能不属于本租户、保留名被占用等）。"""


@dataclass(frozen=True, slots=True)
class AgentBoundary:
    """一次对话实际生效的职责边界。

    冻结对象：它会随 state 进入执行图，中途被改写就等于边界失效。
    """

    agent_id: str | None = None
    agent_name: str = ""
    display_name: str = ""
    revision: int = 0
    is_default: bool = False

    # None = 不额外收窄；() = 明确一个都不给。这个区分必须保留到网关层。
    allowed_tools: tuple[str, ...] | None = None
    allowed_skills: tuple[str, ...] | None = None
    # 连接器名。与 allowed_tools 是并集：命中任一即可见。
    # 两者都为 None 才表示「不额外收窄」。
    allowed_connectors: tuple[str, ...] | None = None

    can_write: bool = True

    # 委派预算。None = 走平台默认（`delegation_limits.DEFAULT_*`）。
    # 折进生效上限时取 min：这一环只能更严，不能凭它把整条链放宽。
    max_delegation_depth: int | None = None
    delegation_budget: int | None = None

    # 已渲染好的职责块（description + instruction）。为空表示不注入提示词。
    instruction: str = ""

    def allows_tool(self, tool_name: str) -> bool:
        return self.allowed_tools is None or tool_name in self.allowed_tools

    def allows_skill(self, skill_name: str) -> bool:
        return self.allowed_skills is None or skill_name in self.allowed_skills


# 平台基线：没有指定 Agent 时生效，等价于「这套机制不存在」。
BASELINE = AgentBoundary()


def boundary_of(agent: Agent | None) -> AgentBoundary:
    """把 ORM 行转成不可变边界。"""
    if agent is None:
        return BASELINE

    instruction = "\n".join(
        part.strip()
        for part in (agent.description or "", agent.instruction or "")
        if part and part.strip()
    )

    return AgentBoundary(
        agent_id=str(agent.id),
        agent_name=agent.name,
        display_name=agent.display_name,
        revision=agent.revision,
        is_default=bool(agent.is_default),
        allowed_tools=(None if agent.allowed_tools is None else tuple(agent.allowed_tools)),
        allowed_skills=(None if agent.allowed_skills is None else tuple(agent.allowed_skills)),
        allowed_connectors=(
            None if agent.allowed_connectors is None else tuple(agent.allowed_connectors)
        ),
        can_write=bool(agent.can_write),
        max_delegation_depth=agent.max_delegation_depth,
        delegation_budget=agent.delegation_budget,
        instruction=instruction,
    )


def render_prompt_block(boundary: AgentBoundary) -> str:
    """职责块的提示词形态。

    与技能块一样拼在 system 末尾而不是塞进 payload：payload 里的内容是
    「数据」，模型会当事实引用；职责属于「规则」，放 system 才有正确的优先级。
    """
    if not boundary.instruction:
        return ""

    lines = [
        "## 你的角色",
        f"你是「{boundary.display_name or boundary.agent_name}」。",
        boundary.instruction,
    ]

    # 只提一句「按已给的目录来」，不罗列工具名：目录本身就在 payload 里，
    # 重复一遍只会让模型以为两处可能不一致。
    if boundary.allowed_tools is not None or boundary.allowed_connectors is not None:
        lines.append("你只能使用工具目录中列出的工具；目录之外的调用会被拒绝。")
    if not boundary.can_write:
        lines.append("你当前处于只读模式：涉及写入或执行的请求一律先说明限制，不要尝试调用。")

    return "\n".join(lines)


# ---------------- 委派：子集判定 ----------------


def agent_subset(sub: AgentBoundary, sup: AgentBoundary) -> tuple[bool, str]:
    """`sub ⊆ sup` 判定。返回 `(是否子集, 违反项)`，违反项形如 `allowed_tools:t3`。

    **唯一判定入口。** 只能在执行前调，配置期给不了保证：
    目标是模型在 `plan_request` 之后才选的，保存 B 的时候不知道将来谁会调它，
    保存 A 的时候不知道 A 会调谁。两处各写一遍判断，早晚会出现两套结论。

    判定内容（与 `docs/A2A.md` §3 一致）：

    | 项 | 规则 |
    |---|---|
    | `can_write` | `B.can_write=True` 要求 `A.can_write=True` |
    | `allowed_tools` | A 有清单时，B 的**显式**清单必须是它的子集 |
    | `allowed_skills` | 同上 |

    `B.allowed_tools is None` 放行：它的含义是「我不额外收窄」，用的是 B 自己的
    全量配置，不是从 A 那里借来的能力。把它当成越权是常见误解，代价是每建一个
    Agent 都要把父清单抄一遍，抄漏一个才是真的越权。

    反过来，`can_write` 不能靠工具清单兜住：它挡的是**一整类风险等级**，
    而不是名单里的某几个名字。
    """
    if sub.can_write and not sup.can_write:
        return False, "can_write"

    if sup.allowed_tools is not None and sub.allowed_tools is not None:
        for name in sub.allowed_tools:
            if name not in sup.allowed_tools:
                return False, f"allowed_tools:{name}"

    if sup.allowed_skills is not None and sub.allowed_skills is not None:
        for name in sub.allowed_skills:
            if name not in sup.allowed_skills:
                return False, f"allowed_skills:{name}"

    if sub.allowed_connectors is not None:
        if sup.allowed_connectors is not None:
            for name in sub.allowed_connectors:
                if name not in sup.allowed_connectors:
                    return False, f"allowed_connectors:{name}"
        elif sup.allowed_tools is not None:
            # 上级只授权了**具体工具**，下级却要按连接器拿一整组。连接器下面
            # 有哪些工具，这个纯函数看不到——保守拒绝。放行的代价是「A 只给了
            # 三个工具，B 凭一个连接器名拿到了二十个」，而这条路径没有别的地方能拦。
            head = sorted(sub.allowed_connectors)[0] if sub.allowed_connectors else ""
            return False, f"allowed_connectors:{head}"

    return True, ""


def describe_violation(reason: str) -> str:
    """把违反项翻成人话。理由要具体到位，否则排障只看到「委派失败」四个字。"""
    kind, _, name = reason.partition(":")

    if kind == "can_write":
        return "被转交的智能体可写，而当前智能体是只读的，不能转交。"
    if kind == "allowed_tools":
        return f"被转交的智能体要用的工具「{name}」超出了当前智能体的边界。"
    if kind == "allowed_skills":
        return f"被转交的智能体要用的技能「{name}」超出了当前智能体的边界。"
    if kind == "allowed_connectors":
        return f"被转交的智能体要接入的连接器「{name}」超出了当前智能体的边界。"

    return "被转交的智能体边界比当前智能体更宽。"


async def delegatable_agents(
    session: AsyncSession,
    *,
    tenant_id: str,
    exclude: str | None = None,
) -> list[Agent]:
    """可以被转交的智能体：已启用、非默认、非内置、且不是调用者自己。

    默认 Agent 要排除：它是「不传 agent 时的承接者」，把它当委派目标
    等于给了一条绕过显式配置的暗路。内置的构建助手同理——
    它是改配置的入口，不该被当成业务执行者叫来叫去。
    """
    rows = await session.scalars(
        select(Agent)
        .where(
            Agent.tenant_id == tenant_id,
            Agent.status == AgentStatus.ACTIVE,
            Agent.is_default.is_(False),
            Agent.is_builtin.is_(False),
        )
        .order_by(Agent.name)
    )

    return [row for row in rows if not exclude or row.name != exclude]


async def capabilities_of(
    session: AsyncSession,
    *,
    agent: Agent,
    context: Any,
) -> dict[str, Any]:
    """一个智能体的能力目录：运行时算出来，不落库、不可编辑。

    为什么不把手写的「能力卡」存进表里：能力是**算出来的**
    （人 ∩ agent ∩ 工具注册表），不是声明的。手写卡在第一次改 `allowed_tools`
    之后就会过期——这个坑在 `sync_studio_definition` 上踩过一次：
    建行时把清单写死，之后加工具对老租户全部失效，表现成「助手拒绝它其实能做的事」。

    两处刻意收窄：
    - 工具按**调用者**的可见性求交，否则这个接口会变成「探测别人有什么工具」；
    - 不含 `instruction`：职责说明是内部作业约束，给别的 Agent 看等于让它去模仿。
    """
    from dataclasses import replace

    from app.tools.dependencies import get_tool_gateway

    gateway = get_tool_gateway()
    await gateway.refresh_tenant_policy(context.tenant_id)

    # 把**被查询的那个 Agent** 的边界叠到调用者身份上，再让网关算一次可见性。
    # 不用手写 `if name in allowed_tools` 过滤：那种写法与 `agent_denial`
    # 是两份判断，加一个维度（连接器）就要记得改两处，漏一处的表现是
    # 「能力目录里有、调用却被拒」——而错误消息看起来像权限配置问题。
    boundary = boundary_of(agent)
    effective = replace(
        context,
        agent_id=boundary.agent_id,
        agent_name=boundary.agent_name,
        agent_revision=boundary.revision,
        agent_allowed_tools=boundary.allowed_tools,
        agent_allowed_skills=boundary.allowed_skills,
        agent_allowed_connectors=boundary.allowed_connectors,
        agent_can_write=boundary.can_write,
    )

    visible = {
        item.tool_name: item
        for item in gateway.list_available_for_context(context=effective)
    }

    tools = [
        {
            "name": name,
            "display_name": visible[name].display_name,
            "risk_level": visible[name].risk_level.value,
            "requires_approval": visible[name].requires_approval,
        }
        for name in sorted(visible)
    ]

    skills = (
        sorted(row for row in await _tenant_skill_names(session))
        if agent.allowed_skills is None
        else list(agent.allowed_skills)
    )

    reason = None
    if agent.status != AgentStatus.ACTIVE:
        reason = "未启用的智能体不能被转交。"
    elif agent.is_default:
        reason = "默认智能体不是转交目标：它承接「不指定智能体」的请求。"
    elif agent.is_builtin:
        reason = "内置智能体不承接转交任务。"

    return {
        "agent": agent.name,
        "display_name": agent.display_name,
        "description": agent.description,
        "revision": agent.revision,
        "status": agent.status.value,
        "can_write": bool(agent.can_write),
        "delegatable": reason is None,
        "delegatable_reason": reason,
        "tools": tools,
        "skills": skills,
        "connectors": sorted(boundary.allowed_connectors or ()),
    }


# ---------------- 读取 ----------------


def _looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


async def get_agent(
    session: AsyncSession,
    *,
    ref: str | uuid.UUID,
) -> Agent:
    """按 id 或 name 取 Agent。租户过滤由 `TenantScoped` 自动完成。"""
    text = str(ref).strip()
    if not text:
        raise AgentNotFound("智能体不存在。")

    if _looks_like_uuid(text):
        row = await session.get(Agent, uuid.UUID(text))
    else:
        row = await session.scalar(select(Agent).where(Agent.name == text))

    if row is None:
        raise AgentNotFound("智能体不存在。")
    return row


async def list_agents(session: AsyncSession) -> Sequence[Agent]:
    """租户内全部 Agent，默认的排最前，其余按创建时间倒序。"""
    rows = await session.scalars(
        select(Agent).order_by(Agent.is_default.desc(), Agent.created_at.desc())
    )
    return rows.all()


async def ensure_default(
    session: AsyncSession,
    *,
    tenant_id: str,
    actor_user_id: str | None = None,
) -> Agent:
    """保证租户有一个默认 Agent，返回它。

    惰性创建而不是迁移期回填：迁移里写一份职责说明，等于把当时的提示词
    钉死在历史里，以后想统一调整要再写一次数据迁移，而且分不清哪些是遗留。
    """
    existing = await session.scalar(
        select(Agent).where(Agent.tenant_id == tenant_id, Agent.is_default.is_(True))
    )
    if existing is not None:
        return existing

    agent = Agent(
        tenant_id=tenant_id,
        name=DEFAULT_AGENT_NAME,
        display_name=DEFAULT_AGENT_DISPLAY_NAME,
        # 留空是刻意的：默认 Agent 就是「平台基线」，不该悄悄改变既有提示词。
        description="",
        instruction="",
        allowed_tools=None,
        allowed_skills=None,
        can_write=True,
        status=AgentStatus.ACTIVE,
        is_default=True,
        created_by=actor_user_id,
        updated_by=actor_user_id,
    )
    session.add(agent)

    try:
        # 用 savepoint 兜并发：两个首轮请求同时进来时，后到的那个会撞
        # `uq_agents_tenant_default`，回滚 savepoint 再读一次即可，
        # 不能把整个事务带崩（外层可能还有消息落库）。
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        row = await session.scalar(
            select(Agent).where(Agent.tenant_id == tenant_id, Agent.is_default.is_(True))
        )
        if row is None:
            raise

        logger.info(
            "默认智能体已被并发创建，复用现有行。",
            extra={"event": "agent_default_race", "tenant_id": tenant_id},
        )
        return row

    logger.info(
        "已创建默认智能体。",
        extra={"event": "agent_default_created", "tenant_id": tenant_id},
    )
    return agent


async def _studio_tool_names() -> list[str]:
    """已注册的 `studio_*` 工具名，外加少量平台能力（见 `STUDIO_EXTRA_TOOLS`）。

    不在这里硬编码清单：工具模块单向依赖本模块（它们要用下面的 CRUD），
    在服务里再抄一遍名字，加一个工具就会漏，而且漏了的表现是
    「助手明明有这个能力却调不动」。
    """
    from app.tools.dependencies import get_tool_gateway

    gateway = get_tool_gateway()
    # 额外的能力也要「先用注册表确认存在」：某个部署里没注册时写进 allowed_tools，
    # 下一次保存边界就会报「工具不存在」，而那条路没人会想到跟附件有关。
    return sorted(
        tool.name
        for tool in gateway.list_tools(include_disabled=True)
        if tool.name.startswith(STUDIO_TOOL_PREFIX) or tool.name in STUDIO_EXTRA_TOOLS
    )


async def sync_studio_definition(session: AsyncSession, *, tenant_id: str) -> bool:
    """把已建好的构建助手的「代码定义部分」对齐到当前代码。

    助手的 `allowed_tools`、`instruction`、`description` 都是**代码定义**的，
    但它们在建行那一刻被写进了 `agents` 表。此后的后果是：新加一个 `studio_*`
    工具、或改了一版提示词，老租户的助手**永远拿不到**，表现成
    「助手说它没这个能力」「提示词里还在说不许做的事」——而代码、文档、
    测试都说有。这类偏差最难发现，因为界面上看到的那份「补充指令」
    看起来完全正常，它只是过时了。

    **不 bump revision。** revision 是「人改过几次边界」，而这里是平台升级
    带来的一批能力；bump 会让在途审批单的快照对不上，把正在进行的工作作废。
    工具的增减也不放宽任何既有审批的快照（快照 ∩ 当前边界只会收紧）。
    """
    agent = await session.scalar(
        select(Agent).where(Agent.tenant_id == tenant_id, Agent.is_builtin.is_(True))
    )
    if agent is None:
        return False

    tools = await _studio_tool_names()
    if not tools:
        return False

    current = set(agent.allowed_tools or [])
    changed: list[str] = []
    if current != set(tools):
        agent.allowed_tools = tools
        changed.append("allowed_tools")
    if (agent.instruction or "") != STUDIO_INSTRUCTION:
        agent.instruction = STUDIO_INSTRUCTION
        changed.append("instruction")
    if (agent.description or "") != STUDIO_DESCRIPTION:
        agent.description = STUDIO_DESCRIPTION
        changed.append("description")

    if not changed:
        return False

    await session.commit()

    logger.info(
        "已把构建助手的定义对齐到当前代码。",
        extra={
            "event": "agent_studio_synced",
            "tenant_id": tenant_id,
            "changed": changed,
            "added_tools": sorted(set(tools) - current),
            "removed_tools": sorted(current - set(tools)),
        },
    )
    return True


async def ensure_studio(
    session: AsyncSession,
    *,
    tenant_id: str,
    actor_user_id: str | None = None,
) -> Agent:
    """保证租户有构建助手（内置 Agent）。

    与 `ensure_default` 同样惰性创建：把当时的工具清单写进迁移，之后每加一个
    `studio_*` 工具都要再来一次数据迁移，而这类迁移没人会记得写。

    名字用 `studio`，被占用时退到 `studio_assistant`：保留名只能挡住**新建**，
    挡不住升级前就已经存在的同名行，而那种行不该让助手永远起不来。
    """
    existing = await session.scalar(
        select(Agent).where(Agent.tenant_id == tenant_id, Agent.is_builtin.is_(True))
    )
    if existing is not None:
        return existing

    tools = await _studio_tool_names()
    if not tools:
        # 这是代码错误（工具没注册），不是配置问题。建一个 allowed_tools=[] 的助手
        # 会变成「看起来正常但什么都不能做」，比直接失败更难查。
        raise AgentValidationError("构建助手的工具未注册，无法创建内置智能体。")

    name = STUDIO_AGENT_NAME
    if await session.scalar(select(Agent).where(Agent.name == name)) is not None:
        name = "studio_assistant"

    agent = Agent(
        tenant_id=tenant_id,
        name=name,
        display_name=STUDIO_AGENT_DISPLAY_NAME,
        description=STUDIO_DESCRIPTION,
        instruction=STUDIO_INSTRUCTION,
        allowed_tools=tools,
        allowed_skills=None,
        # 助手要能执行写操作（建/改/删 Agent），但每一次都由审批单兜住，
        # 所以这里 opened 不等于「它能自己决定改什么」。
        can_write=True,
        status=AgentStatus.ACTIVE,
        is_default=False,
        is_builtin=True,
        revision=1,
        created_by=actor_user_id,
        updated_by=actor_user_id,
    )
    session.add(agent)

    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        row = await session.scalar(
            select(Agent).where(Agent.tenant_id == tenant_id, Agent.is_builtin.is_(True))
        )
        if row is None:
            raise

        logger.info(
            "内置智能体已被并发创建，复用现有行。",
            extra={"event": "agent_studio_race", "tenant_id": tenant_id},
        )
        return row

    logger.info(
        "已创建构建助手。",
        extra={"event": "agent_studio_created", "tenant_id": tenant_id, "tools": len(tools)},
    )
    return agent


async def resolve_for_chat(
    session: AsyncSession,
    *,
    tenant_id: str,
    ref: str | None,
    actor_user_id: str | None = None,
) -> AgentBoundary:
    """对话入口的解析：不传就用默认 Agent，传了就必须可用。"""
    if not ref:
        agent = await ensure_default(session, tenant_id=tenant_id, actor_user_id=actor_user_id)
        return boundary_of(agent)

    agent = await get_agent(session, ref=ref)

    if agent.status == AgentStatus.DISABLED:
        raise AgentUnavailable(f"智能体「{agent.display_name or agent.name}」已停用。")
    if agent.status == AgentStatus.DRAFT:
        raise AgentUnavailable(f"智能体「{agent.display_name or agent.name}」尚未启用。")

    return boundary_of(agent)


# ---------------- 写入 ----------------


def _clean_list(
    values: Iterable[str] | None,
    *,
    field: str,
) -> list[str] | None:
    if values is None:
        return None

    cleaned: list[str] = []
    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        if text not in cleaned:
            cleaned.append(text)

    if len(cleaned) > MAX_BOUNDARY_ITEMS:
        raise AgentValidationError(
            f"{field} 最多 {MAX_BOUNDARY_ITEMS} 项，当前 {len(cleaned)} 项。"
        )
    return cleaned


async def _known_tool_names() -> set[str]:
    """注册表里的全部工具名（含已停用）。延迟导入避免与网关循环依赖。"""
    from app.tools.dependencies import get_tool_gateway

    gateway = get_tool_gateway()
    return {tool.name for tool in gateway.list_tools(include_disabled=True)}


async def _tenant_skill_names(session: AsyncSession) -> set[str]:
    rows = await session.scalars(select(Skill.name))
    return set(rows.all())


async def _validate_boundary(
    session: AsyncSession,
    *,
    allowed_tools: list[str] | None,
    allowed_skills: list[str] | None,
    allowed_connectors: list[str] | None = None,
) -> None:
    """校验边界里的名字真实存在。

    宁可在保存时报 400，也不要在运行时静默少给：写错一个工具名，
    表现为「这个 Agent 明明配了却做不了」，排查成本远高于一次报错。
    """
    if allowed_tools is not None:
        unknown = sorted(set(allowed_tools) - await _known_tool_names())
        if unknown:
            raise AgentValidationError(f"工具未注册：{'、'.join(unknown)}")

    if allowed_skills is not None:
        unknown = sorted(set(allowed_skills) - await _tenant_skill_names(session))
        if unknown:
            raise AgentValidationError(f"技能不存在或不属于本租户：{'、'.join(unknown)}")

    if allowed_connectors is not None and allowed_connectors:
        from app.models.connector import Connector

        known = set(
            (
                await session.scalars(
                    select(Connector.name).where(
                        Connector.name.in_(allowed_connectors)
                    )
                )
            ).all()
        )
        unknown = sorted(set(allowed_connectors) - known)
        if unknown:
            raise AgentValidationError(f"连接器不存在：{'、'.join(unknown)}")


def _validate_text(name: str, value: str, *, limit: int) -> str:
    text = (value or "").strip()
    if len(text) > limit:
        raise AgentValidationError(f"{name} 最多 {limit} 字，当前 {len(text)} 字。")
    return text


def _validate_limit(label: str, value: int | None, *, ceiling: int) -> int | None:
    """委派预算的取值范围。None 原样返回（= 走平台默认）。

    `0` 是合法值，不能当成「没配」——它是「一次都不许」这个开关。
    范围规则只有一份（`delegation_limits.check_limit`），这里只做异常翻译。
    """
    try:
        return check_limit(label, value, ceiling=ceiling)
    except ValueError as exc:
        raise AgentValidationError(str(exc)) from exc


def _validated_limits(
    max_depth: int | None, budget: int | None
) -> tuple[int | None, int | None]:
    return (
        _validate_limit("转交深度上限", max_depth, ceiling=MAX_DEPTH_CEILING),
        _validate_limit("单轮转交次数", budget, ceiling=MAX_PER_TURN_CEILING),
    )


def _validate_name(name: str) -> str:
    text = (name or "").strip()
    if not text:
        raise AgentValidationError("智能体标识不能为空。")
    if len(text) > 64:
        raise AgentValidationError("智能体标识最多 64 字符。")
    if text in RESERVED_AGENT_NAMES:
        raise AgentValidationError(f"「{text}」是系统保留标识，请换一个。")
    # 客户端用它做路由/查询参数，限制字符集比事后转义可靠。
    if not all(ch.isalnum() or ch in "-_." for ch in text):
        raise AgentValidationError("智能体标识只能用字母、数字与 - _ .")
    return text


async def create_agent(
    session: AsyncSession,
    *,
    tenant_id: str,
    name: str,
    display_name: str,
    description: str = "",
    instruction: str = "",
    allowed_tools: list[str] | None = None,
    allowed_skills: list[str] | None = None,
    allowed_connectors: list[str] | None = None,
    can_write: bool = False,
    max_delegation_depth: int | None = None,
    delegation_budget: int | None = None,
    status: AgentStatus = AgentStatus.DRAFT,
    actor_user_id: str | None = None,
) -> Agent:
    clean_name = _validate_name(name)
    clean_display = _validate_text("显示名", display_name, limit=64) or clean_name
    clean_desc = _validate_text("职责说明", description, limit=MAX_DESCRIPTION_CHARS)
    clean_ins = _validate_text("补充指令", instruction, limit=MAX_INSTRUCTION_CHARS)
    max_delegation_depth, delegation_budget = _validated_limits(
        max_delegation_depth, delegation_budget
    )

    tools = _clean_list(allowed_tools, field="allowed_tools")
    skills = _clean_list(allowed_skills, field="allowed_skills")
    connectors = _clean_list(allowed_connectors, field="allowed_connectors")
    await _validate_boundary(
        session,
        allowed_tools=tools,
        allowed_skills=skills,
        allowed_connectors=connectors,
    )

    # 先确保默认 Agent 存在：否则用户可以抢注一个非默认的 agent 再删掉默认，
    # 后续「不传 agent」就没有落点。
    await ensure_default(session, tenant_id=tenant_id, actor_user_id=actor_user_id)

    exists = await session.scalar(select(Agent).where(Agent.name == clean_name))
    if exists is not None:
        raise AgentValidationError(f"智能体标识已被占用：{clean_name}")

    agent = Agent(
        tenant_id=tenant_id,
        name=clean_name,
        display_name=clean_display,
        description=clean_desc,
        instruction=clean_ins,
        allowed_tools=tools,
        allowed_skills=skills,
        allowed_connectors=connectors,
        can_write=can_write,
        max_delegation_depth=max_delegation_depth,
        delegation_budget=delegation_budget,
        status=status,
        is_default=False,
        revision=1,
        created_by=actor_user_id,
        updated_by=actor_user_id,
    )
    session.add(agent)
    await session.flush()
    return agent


async def update_agent(
    session: AsyncSession,
    *,
    agent: Agent,
    display_name: str | None = None,
    description: str | None = None,
    instruction: str | None = None,
    allowed_tools: Any = UNSET,
    allowed_skills: Any = UNSET,
    allowed_connectors: Any = UNSET,
    can_write: bool | None = None,
    max_delegation_depth: Any = UNSET,
    delegation_budget: Any = UNSET,
    actor_user_id: str | None = None,
) -> Agent:
    """更新职责与边界。

    未传的字段保持原值；`allowed_tools` / `allowed_skills` 用 `UNSET` 哨兵区分
    「没传」与「显式设为 None（不收窄）」——这两者语义完全不同。
    """
    if agent.is_builtin and (
        allowed_tools is not UNSET
        or allowed_skills is not UNSET
        or allowed_connectors is not UNSET
        or can_write is not None
    ):
        # 职责说明（display_name / description / instruction）允许改：那是语气偏好。
        # 边界不允许：它只要放开一次，下一次调用就能用上，而中间没有任何人批准过这个能力。
        raise AgentValidationError("内置智能体的能力边界不可修改。")

    changed = False

    if display_name is not None:
        value = _validate_text("显示名", display_name, limit=64)
        if value and value != agent.display_name:
            agent.display_name = value
            changed = True

    if description is not None:
        value = _validate_text("职责说明", description, limit=MAX_DESCRIPTION_CHARS)
        if value != agent.description:
            agent.description = value
            changed = True

    if instruction is not None:
        value = _validate_text("补充指令", instruction, limit=MAX_INSTRUCTION_CHARS)
        if value != agent.instruction:
            agent.instruction = value
            changed = True

    if allowed_tools is not UNSET:
        tools = _clean_list(allowed_tools, field="allowed_tools")
        await _validate_boundary(session, allowed_tools=tools, allowed_skills=None)
        if tools != agent.allowed_tools:
            agent.allowed_tools = tools
            changed = True

    if allowed_skills is not UNSET:
        skills = _clean_list(allowed_skills, field="allowed_skills")
        await _validate_boundary(session, allowed_tools=None, allowed_skills=skills)
        if skills != agent.allowed_skills:
            agent.allowed_skills = skills
            changed = True

    if allowed_connectors is not UNSET:
        connectors = _clean_list(allowed_connectors, field="allowed_connectors")
        await _validate_boundary(
            session,
            allowed_tools=None,
            allowed_skills=None,
            allowed_connectors=connectors,
        )
        if connectors != agent.allowed_connectors:
            agent.allowed_connectors = connectors
            changed = True

    if can_write is not None and can_write != agent.can_write:
        agent.can_write = can_write
        changed = True

    # 预算用 UNSET 区分「没传」与「显式传 null」：null 是「回到平台默认」，
    # 而 0 是「一次都不许」。两者都会写进同一列，混起来就没法从界面撤销一次收窄。
    if max_delegation_depth is not UNSET:
        value = _validate_limit(
            "转交深度上限", max_delegation_depth, ceiling=MAX_DEPTH_CEILING
        )
        if value != agent.max_delegation_depth:
            agent.max_delegation_depth = value
            changed = True

    if delegation_budget is not UNSET:
        value = _validate_limit(
            "单轮转交次数", delegation_budget, ceiling=MAX_PER_TURN_CEILING
        )
        if value != agent.delegation_budget:
            agent.delegation_budget = value
            changed = True

    if changed:
        # 只在实际变化时 +1：界面上反复点保存不该让版本号虚增，
        # 否则「第 7 版边界」这种指认就失去意义。
        agent.revision += 1
        agent.updated_by = actor_user_id

    await session.flush()
    return agent


async def set_status(
    session: AsyncSession,
    *,
    agent: Agent,
    status: AgentStatus,
    actor_user_id: str | None = None,
) -> Agent:
    if agent.is_builtin:
        # 停用等于把控制台里唯一的自然语言管理入口关掉，而现象是「智能体不存在」，
        # 看起来像配置丢了。
        raise AgentValidationError("内置智能体不能停用。")

    if agent.is_default and status != AgentStatus.ACTIVE:
        # 默认 Agent 停用等于让所有不传 agent 的请求 400；
        # 要停就停整个租户的记忆/对话能力，而不是留一个隐形的坑。
        raise AgentValidationError("默认智能体不能停用。")

    if agent.status != status:
        agent.status = status
        agent.updated_by = actor_user_id
    await session.flush()
    return agent


async def delete_agent(
    session: AsyncSession,
    *,
    agent: Agent,
) -> None:
    """删除。

    只允许删**已停用**的非默认 Agent：停用已经覆盖了「先别用」，
    直接删一个还在运行的 Agent 会让历史审计里的 agent_id 指向不存在的对象。
    """
    if agent.is_builtin:
        raise AgentValidationError("内置智能体不能删除。")
    if agent.is_default:
        raise AgentValidationError("默认智能体不能删除。")
    if agent.status != AgentStatus.DISABLED:
        raise AgentValidationError("请先停用，再删除。")

    await session.delete(agent)
    await session.flush()


def status_counts(rows: Sequence[Agent]) -> dict[str, int]:
    counts = {item.value: 0 for item in AgentStatus}
    for row in rows:
        counts[row.status.value] = counts.get(row.status.value, 0) + 1
    return counts
