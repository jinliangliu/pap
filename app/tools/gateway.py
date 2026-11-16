# app/tools/gateway.py
"""受控工具执行入口。

标准链路：

    模型提出调用意图
      → 工具是否注册、是否启用
      → JSON Schema 校验
      → 角色 / Scope 权限
      → 站点/资源授权
      → 限流
      → 审批（写操作）
      → 调用 handler（带超时）
      → 标准化结果
      → 审计

Gateway 不接受模型自定义的 handler、URL 或命令，也不代替审批服务做状态流转。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import replace
from typing import Any, NoReturn, Protocol

from app.models.approval import RiskLevel
from app.observability.context import span
from app.services import runtime_settings
from app.services.rate_limit_service import RateLimitExceeded
from app.services.tenant_service import TenantService, get_tenant_service
from app.tools.registry import ToolNotFoundError, ToolRegistry
from app.tools.schemas import (
    ToolSchemaValidationError,
    validate_tool_arguments,
    validate_tool_schema,
)
from app.tools.types import (
    ConnectorRuntime,
    ToolCallPlan,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolPublicView,
)

logger = logging.getLogger(__name__)

# 拒绝的四类来源。写进异常、审计元数据与观测口径，三处共用同一套取值。
DENIAL_SCOPE = "scope"
DENIAL_ROLE = "role"
DENIAL_TENANT = "tenant"
DENIAL_AGENT = "agent"
DENIAL_REASONS = frozenset({DENIAL_SCOPE, DENIAL_ROLE, DENIAL_TENANT, DENIAL_AGENT})


class ToolGatewayError(RuntimeError):
    """Tool Gateway 的统一基础异常。"""


class ToolDisabledError(ToolGatewayError):
    """工具已注册但当前已被停用。"""


class ToolPermissionDeniedError(ToolGatewayError):
    """当前用户缺少工具调用权限。

    `reason` 说明**是谁拦的**。四类拒绝在治理上是完全不同的信号：

    - `scope`  / `role`：这个人本身没权限，换个人来就行；
    - `tenant`         ：整个组织没向该角色开放该工具；
    - `agent`          ：人有权限，但当前智能体的边界没放开——
                         这通常意味着「边界配窄了」，是要改配置的信号。

    以前四类都只留一句文案、都归成 `tool_rejected`，运营看不出该改什么。
    """

    def __init__(self, message: str, *, reason: str = "scope") -> None:
        super().__init__(message)
        self.reason = reason if reason in DENIAL_REASONS else "scope"


class ToolResourceDeniedError(ToolGatewayError):
    """当前用户没有目标站点/资源的授权。"""


class ToolRateLimitedError(ToolGatewayError):
    """超出调用频率限制。"""


class ToolApprovalRequiredError(ToolGatewayError):
    """操作必须经审批，不能通过普通执行入口调用。"""


class ToolPreflightRejectedError(ToolGatewayError):
    """工具自己的预检判定这次调用注定失败，不给它生成审批单。

    与 `ToolPermissionDeniedError` 的区别：这里不是「谁没权限」，
    而是「这个操作本身不成立」（例如目标对象是内置的、不可修改）。
    单独一类是为了让上层能把话说清楚，而不是回一句「权限不足」。
    """


class ToolConnectorUnavailableError(ToolGatewayError):
    """工具所属的连接器已被删除或停用。

    与「工具未注册」区分开：那说明配置没了，这说说明配置还在、只是连接器那头
    断了。运维要做的动作完全不同。
    """

    code = "CONNECTOR_UNAVAILABLE"


class ToolCredentialMissingError(ToolGatewayError):
    """连接器要求凭据，而当前租户没有配置。

    **绝不回退**到环境变量或行上的遗留令牌：回退会让「忘了配凭据」表现为
    「用别人的账号调通了」，而两种情况的返回值看起来一模一样。
    """

    code = "CONNECTOR_CREDENTIAL_MISSING"


class ToolAuditSink(Protocol):
    async def record(self, event: str, payload: dict[str, Any]) -> None: ...


class ToolRateLimiter(Protocol):
    async def enforce(
        self,
        *,
        tenant_id: str,
        user_id: str,
        tool_name: str,
        is_write: bool,
        tenant_limit_per_minute: int | None = None,
    ) -> None: ...


class StationAuthorizer(Protocol):
    async def ensure_access(
        self,
        *,
        context: ToolExecutionContext,
        station_id: str,
        write: bool,
    ) -> None: ...


_WRITE_RISKS = frozenset(
    {
        RiskLevel.WRITE,
        RiskLevel.EXECUTE,
        RiskLevel.DESTRUCTIVE,
        RiskLevel.EXTERNAL,
    }
)


def is_write_risk(risk: RiskLevel) -> bool:
    """该风险等级是否属于「有外部副作用」。路由层校验风险覆盖时也用它。"""
    return risk in _WRITE_RISKS


def risk_override_error(tool: ToolDefinition, override: str | None) -> str | None:
    """校验风险覆盖是否合法。合法返回 None，否则返回给人看的理由。

    放在这里而不是某个路由里：两个写策略的入口（权限清单接口、租户工具策略接口）
    都必须拒绝下调，分开写就会出现「一处拦住、另一处放行」。
    """
    if override is None:
        return None

    if override not in ("read", "write"):
        return "risk_override 只能是 read 或 write。"

    if override == "read" and is_write_risk(tool.risk_level):
        return (
            f"{tool.display_name} 是写入类工具，风险只能上调不能下调。"
            "要免去审批请把它改成 allow（仍受写入兜底约束）。"
        )

    return None


def risk_with_override(base: RiskLevel, override: str | None) -> RiskLevel:
    """把租户配置的风险覆盖叠到工具真实风险上。

    **只上调**：`read` 覆盖不产生任何效果，因为把写方法标成读取等于给
    它开一条免审批直通，而这条直通在返回值与审计上都看不出来。
    下调在保存时就会被拒（见 permissions 路由），这里再兜一层，
    是为了防住「库里被手改成 read」这种绕过接口的路径。
    """
    if override == "write" and base not in _WRITE_RISKS:
        return RiskLevel.WRITE
    return base


def approval_required(
    *,
    tool: ToolDefinition,
    action: str,
    risk_override: str | None = None,
) -> bool:
    """动作 + 风险 → 要不要审批。**唯一实现**。

    Gateway 执行前、模型目录生成、权限页回显都调这一个函数。
    三处各写一遍时，权限页写着「需确认」而执行直接跑，是必然发生的，
    而且从任何一处代码单独看都是对的。

    语义：
    - `deny`：不审批也不执行（调用会被权限校验拦下），因此这里返回 False；
    - `confirm`：无论风险高低都审批——「只读工具也要人看一眼」的唯一入口；
    - `allow`：仍受风险兜底约束，写入及以上照旧审批。
    """
    if action == "deny":
        return False

    if risk_with_override(tool.risk_level, risk_override) in _WRITE_RISKS:
        return True

    return action == "confirm"


class ToolGateway:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        station_authorizer: StationAuthorizer | None = None,
        rate_limiter: ToolRateLimiter | None = None,
        audit_sink: ToolAuditSink | None = None,
        tenant_service: TenantService | None = None,
    ) -> None:
        self._registry = registry
        self._station_authorizer = station_authorizer
        self._rate_limiter = rate_limiter
        self._audit_sink = audit_sink
        self._tenant_service = tenant_service or get_tenant_service()

    # ---------------- 注册与查询 ----------------

    def register_tool(
        self,
        tool: ToolDefinition,
        *,
        replace: bool = False,
    ) -> None:
        """注册前先校验 input_schema 本身是否合法。"""
        validate_tool_schema(tool.input_schema)
        self._registry.register(tool, replace=replace)

    def get_tool(self, name: str) -> ToolDefinition:
        return self._registry.get(name)

    def has_tool(self, name: str) -> bool:
        return self._registry.contains(name)

    def unregister_tool(self, name: str) -> bool:
        """删除自定义工具时同步注销；内置与 MCP 工具不允许从这里移除。"""
        if not self._registry.contains(name):
            return False

        existing = self._registry.get(name)
        if existing.source != "custom":
            raise ToolGatewayError(f"工具 {name} 不是自定义工具，不允许注销。")

        return self._registry.unregister(name)

    def unregister_mcp_tool(self, name: str) -> bool:
        """取消接入时移除管理员登记过的 MCP 工具。

        与 `unregister_tool` 分开而不是加参数：这两个入口的授权语义不同，
        合并后迟早有人从「自定义」的路径把代码白名单里的工具点掉。
        代码登记的工具（source 同为 mcp）由调用方按 `code_local_names()` 拦截。
        """
        if not self._registry.contains(name):
            return False

        existing = self._registry.get(name)
        if existing.source != "mcp":
            raise ToolGatewayError(f"工具 {name} 不是 MCP 工具，不允许从这里注销。")

        return self._registry.unregister(name)

    def list_tools(
        self,
        *,
        include_disabled: bool = False,
    ) -> list[ToolDefinition]:
        return self._registry.list_tools(include_disabled=include_disabled)

    def list_available_for_context(
        self,
        *,
        context: ToolExecutionContext,
    ) -> list[ToolPublicView]:
        """
        返回当前身份可见的工具目录。

        已停用、无 Scope、无角色、无站点授权、或当前租户禁用的工具不会出现在目录中，
        模型因此看不到它，也就不会尝试调用。
        """
        views: list[ToolPublicView] = []

        for tool in self._registry.list_tools():
            if not self._has_access(tool=tool, context=context):
                continue

            views.append(
                ToolPublicView(
                    tool_name=tool.name,
                    display_name=tool.display_name,
                    description=tool.description,
                    risk_level=tool.risk_level,
                    public_input_schema=tool.input_schema,
                    requires_approval=self.requires_approval_for(tool=tool, context=context),
                )
            )

        return views

    async def refresh_tenant_policy(self, tenant_id: str) -> None:
        """预热租户策略缓存。

        `list_available_for_context` 是同步方法（模型目录生成路径），
        因此租户策略必须在进入目录生成前加载好。加载失败不阻塞目录，
        执行期仍会再校验一次。
        """
        try:
            await self._tenant_service.settings_for(tenant_id)
        except Exception:
            logger.warning(
                "加载租户工具策略失败，本次按平台默认处理。",
                extra={"event": "tenant_policy_load_failed", "tenant_id": tenant_id},
                exc_info=True,
            )

    # ---------------- 写操作预览 ----------------

    async def prepare_call(
        self,
        *,
        context: ToolExecutionContext,
        tool_name: str,
        arguments: dict[str, Any],
        reason: str,
    ) -> ToolCallPlan:
        """
        校验一次调用并生成执行计划。

        本方法绝不执行工具。它用于创建审批预览。
        """
        tool = await self._resolve(
            context=context,
            tool_name=tool_name,
            arguments=arguments,
        )

        # 预览阶段用缓存做一次快速拦截；执行阶段还会强制重新加载。
        await self._check_tenant_policy_cached(tool=tool, context=context)

        return ToolCallPlan(
            tool_name=tool.name,
            display_name=tool.display_name,
            risk_level=tool.risk_level,
            arguments=arguments,
            reason=(reason or "").strip() or tool.description,
            requires_approval=self.requires_approval_for(tool=tool, context=context),
            verification_hint=tool.verification_hint,
        )

    async def check_resource_access(
        self,
        *,
        context: ToolExecutionContext,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        """
        在生成审批单之前做站点授权预检。

        目的：未授权用户不应看到操作预览、更不该产生一条待审批记录。
        本方法只校验授权，不执行工具、不消耗限流额度。
        """
        tool = await self._resolve(
            context=context,
            tool_name=tool_name,
            arguments=arguments,
        )

        await self._check_tenant_policy(tool=tool, context=context)

        # 连接器凭据缺失要在**生成审批单之前**发现：让人对着一张注定失败的卡
        # 点确认，训练出来的是闭眼确认（与 `preflight` 同一条理由）。
        if tool.connector_id is not None:
            await self._resolve_connector(tool=tool, context=context)

        # 工具自带的预检：注定失败的操作不该走到「生成审批单」这一步。
        # 让人确认一张必然失败的卡片，等于在训练闭眼确认。
        if tool.preflight is not None:
            reason = await tool.preflight(context, arguments)
            if reason:
                raise ToolPreflightRejectedError(reason)

        station_id = arguments.get(tool.station_id_argument) if tool.station_id_argument else None

        if not station_id:
            return

        if self._station_authorizer is None:
            raise ToolResourceDeniedError("未配置站点授权校验，拒绝访问站点资源。")

        try:
            await self._station_authorizer.ensure_access(
                context=context,
                station_id=str(station_id),
                write=self.requires_approval(tool) or tool.risk_level in _WRITE_RISKS,
            )
        except ToolResourceDeniedError:
            raise
        except Exception as exc:
            raise ToolResourceDeniedError("当前资源不可访问。") from exc

    # ---------------- 执行 ----------------

    async def execute_read_only(
        self,
        *,
        context: ToolExecutionContext,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolExecutionResult:
        """执行低风险只读工具；任何写操作都会被拒绝。"""
        tool = await self._resolve(
            context=context,
            tool_name=tool_name,
            arguments=arguments,
        )

        if self.requires_approval_for(tool=tool, context=context):
            raise ToolApprovalRequiredError(
                f"工具 {tool.display_name} 可能产生外部副作用，必须先创建审批请求。"
            )

        await self._pre_execute(
            tool=tool,
            context=context,
            arguments=arguments,
            is_write=False,
        )

        return await self._traced_invoke(
            tool=tool,
            context=context,
            arguments=arguments,
        )

    async def execute_approved(
        self,
        *,
        context: ToolExecutionContext,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolExecutionResult:
        """
        已审批操作的专用执行入口。

        调用方（ApprovalService）必须已完成：
        状态锁定、parameter_hash 比对、过期校验、幂等键绑定。
        """
        tool = await self._resolve(
            context=context,
            tool_name=tool_name,
            arguments=arguments,
        )

        await self._pre_execute(
            tool=tool,
            context=context,
            arguments=arguments,
            is_write=True,
        )

        return await self._traced_invoke(
            tool=tool,
            context=context,
            arguments=arguments,
        )

    async def verify(
        self,
        *,
        context: ToolExecutionContext,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolExecutionResult | None:
        """回读核验。工具未声明 verifier 时返回 None，调用方不得声称已核验。"""
        tool = self._registry.get(tool_name)

        if tool.verifier is None:
            return None

        try:
            effective_context = await self._with_tenant_credential(
                tool=tool,
                context=context,
            )
            return await asyncio.wait_for(
                tool.verifier(effective_context, arguments),
                timeout=tool.timeout_seconds,
            )
        except TimeoutError:
            return ToolExecutionResult(
                success=False,
                message="回读核验超时，结果暂时无法确认。",
                error_code="VERIFY_TIMEOUT",
            )
        except Exception:
            logger.exception(
                "工具回读核验异常。",
                extra={"event": "tool_verify_exception", "tool_name": tool_name},
            )
            return ToolExecutionResult(
                success=False,
                message="回读核验失败，结果暂时无法确认。",
                error_code="VERIFY_EXCEPTION",
            )

    # ---------------- 内部 ----------------

    @staticmethod
    def requires_approval(tool: ToolDefinition) -> bool:
        """
        平台兜底判定：工具可显式声明审批；按风险等级保守兜底。

        即使注册时忘了 requires_approval=True，write 及以上仍进入审批路径。

        **这只是没有上下文时的答案。** 凡是有 `ToolExecutionContext` 的路径
        一律用 `requires_approval_for`——只用这一个会看不到租户的 confirm
        与风险覆盖，表现是「权限页上写着需确认、对话里直接执行了」。
        """
        return tool.requires_approval or tool.risk_level in _WRITE_RISKS

    def requires_approval_for(
        self,
        *,
        tool: ToolDefinition,
        context: ToolExecutionContext,
    ) -> bool:
        """结合租户策略的最终审批判定。

        三种动作各自的作用见 `approval_required`。这里只负责把「库里配了什么」
        解析成生效值：库里有记录以库为准，没有才用平台默认动作。
        """
        action, override = self.resolve_policy(tool_name=tool.name, context=context)
        return approval_required(tool=tool, action=action, risk_override=override)

    def resolve_policy(
        self,
        *,
        tool_name: str,
        context: ToolExecutionContext,
    ) -> tuple[str, str | None]:
        """解析 (生效动作, 风险覆盖)。租户策略未加载时按平台默认动作。"""
        tenant_settings = self._tenant_service.cached(context.tenant_id)
        default_action = runtime_settings.default_tool_action()

        if tenant_settings is None:
            return default_action, None

        policy = tenant_settings.policy_for(tool_name)
        return policy.action or default_action, policy.risk_override

    @staticmethod
    def agent_denial(
        *,
        tool: ToolDefinition,
        context: ToolExecutionContext,
    ) -> str | None:
        """智能体边界判断。返回拒绝原因，通过则 None。

        单独抽出来是为了让「目录过滤」与「执行拦截」共用同一份判断：
        两处各写一遍，早晚会出现「目录里没有、调用却能过」的缝。

        两项边界是**并集**关系：工具名直接命中，或它所属的连接器在
        `allowed_connectors` 里，都算被授予。两者都为 None 才是「不额外收窄」——
        这与「只配了连接器」时的直觉一致：那时内置工具会从目录里消失，
        因为人明确表达过「只给这些」。
        """
        granted = False

        if (
            context.agent_allowed_tools is not None
            and tool.name in context.agent_allowed_tools
        ):
            granted = True

        if (
            context.agent_allowed_connectors is not None
            and tool.connector_name
            and tool.connector_name in context.agent_allowed_connectors
        ):
            granted = True

        narrowed = (
            context.agent_allowed_tools is not None
            or context.agent_allowed_connectors is not None
        )

        if narrowed and not granted:
            # 文案分两种：工具属于某个连接器但那个连接器没被授权，
            # 与「压根没列这个工具」是不同的人为错误，提示要对得上。
            if tool.connector_name and context.agent_allowed_connectors is not None:
                return f"当前智能体未接入连接器：{tool.connector_name}"
            return f"当前智能体未被授予工具：{tool.display_name}"

        if not context.agent_can_write and tool.risk_level in _WRITE_RISKS:
            return f"当前智能体为只读模式，不能执行：{tool.display_name}"

        return None

    @staticmethod
    def missing_scopes(
        *,
        tool: ToolDefinition,
        context: ToolExecutionContext,
    ) -> list[str]:
        """这次调用缺少哪些 scope。

        **目录过滤与执行拦截必须共用这一份判断。** 分开写两遍时，
        改了一处漏了另一处，现象是「目录里看得见、一调用就被拒」——
        而错误消息看起来像权限配置问题，排查会跑偏很远（2026-11-11 踩过）。

        平台管理员按「是不是管理员」放行，与路由层 `_can_manage` 同一口径：
        只跳过 scope 这一层，角色、租户策略、站点授权、智能体边界照旧生效。
        """
        if context.is_platform_admin:
            return []
        return [scope for scope in tool.required_scopes if scope not in context.scopes]

    def _has_access(
        self,
        *,
        tool: ToolDefinition,
        context: ToolExecutionContext,
    ) -> bool:
        if not tool.enabled:
            return False

        # 连接器停用 = 它下面所有工具一起下线。判定用注册时的快照，
        # 因为这里是同步路径（目录生成）拿不到库；连接器状态变更时
        # `connector_service` 会重注册下属工具，快照随之更新。
        if not tool.connector_ready:
            return False

        if self.missing_scopes(tool=tool, context=context):
            return False

        if tool.allowed_roles and not set(tool.allowed_roles) & set(context.roles):
            return False

        if self.agent_denial(tool=tool, context=context):
            return False

        # 租户策略可能尚未加载（同步目录生成路径），此时沿用平台默认；
        # 真正执行前 `_pre_execute` 会强制加载并再校验一次。
        tenant_settings = self._tenant_service.cached(context.tenant_id)

        if tenant_settings is not None and not tenant_settings.allow_tool(
            tool.name,
            context.roles,
            default_action=runtime_settings.default_tool_action(),
        ):
            return False

        return True

    async def _resolve(
        self,
        *,
        context: ToolExecutionContext,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolDefinition:
        try:
            tool = self._registry.get(tool_name)
        except ToolNotFoundError as exc:
            raise ToolGatewayError(f"请求的工具未注册：{tool_name}") from exc

        if not tool.enabled:
            raise ToolDisabledError(f"工具当前已停用：{tool.display_name}")

        await self._check_permissions(tool=tool, context=context)

        try:
            validate_tool_arguments(
                schema=tool.input_schema,
                arguments=arguments,
            )
        except ToolSchemaValidationError as exc:
            raise ToolGatewayError(str(exc)) from exc

        return tool

    async def _check_permissions(
        self,
        *,
        tool: ToolDefinition,
        context: ToolExecutionContext,
    ) -> None:
        missing_scopes = self.missing_scopes(tool=tool, context=context)

        if missing_scopes:
            await self._deny(
                tool=tool,
                context=context,
                reason=DENIAL_SCOPE,
                message="当前用户缺少调用该工具所需的权限。",
            )

        if tool.allowed_roles and not set(tool.allowed_roles) & set(context.roles):
            await self._deny(
                tool=tool,
                context=context,
                reason=DENIAL_ROLE,
                message=f"当前角色不允许调用工具：{tool.display_name}",
            )

        # 智能体边界：目录里已经过滤过一次，这里再拦一次。
        # 模型可能凭记忆拼出目录里没有的工具名，只在目录层过滤等于没拦。
        denial = ToolGateway.agent_denial(tool=tool, context=context)
        if denial:
            await self._deny(
                tool=tool,
                context=context,
                reason=DENIAL_AGENT,
                message=denial,
            )

    async def _deny(
        self,
        *,
        tool: ToolDefinition,
        context: ToolExecutionContext,
        reason: str,
        message: str,
    ) -> NoReturn:
        """先记账，再抛。审计写失败不得改变「拒绝」这个结论。"""
        await self._record_denial(tool=tool, context=context, reason=reason)
        raise ToolPermissionDeniedError(message, reason=reason)

    async def _record_denial(
        self,
        *,
        tool: ToolDefinition,
        context: ToolExecutionContext,
        reason: str,
    ) -> None:
        """把「被拒绝的一次调用」写进审计。

        只记工具名与原因，**不记参数值**：拒绝发生在参数校验之前，
        此时的内容可能是模型瞎拼的，也可能夹带不该落库的东西。
        """
        if self._audit_sink is None:
            return

        try:
            await self._audit_sink.record(
                "tool_denied",
                {
                    "tool_name": tool.name,
                    "risk_level": tool.risk_level.value,
                    "success": False,
                    "error_code": f"DENIED_{reason.upper()}",
                    "denial_reason": reason,
                    "tenant_id": context.tenant_id,
                    "user_id": context.user_id,
                    "thread_id": context.thread_id,
                    "run_id": context.run_id,
                    "approval_id": context.approval_id,
                    "agent_name": context.agent_name,
                    "agent_revision": context.agent_revision,
                },
            )
        except Exception:
            logger.exception(
                "写入工具拒绝审计失败。",
                extra={"event": "tool_audit_failed", "tool_name": tool.name},
            )

    async def _check_tenant_policy_cached(
        self,
        *,
        tool: ToolDefinition,
        context: ToolExecutionContext,
    ) -> None:
        tenant_settings = self._tenant_service.cached(context.tenant_id)

        if tenant_settings is None:
            return

        await self._raise_if_tenant_denied(
            tool=tool,
            context=context,
            allow=tenant_settings.allow_tool(
                tool.name,
                context.roles,
                default_action=runtime_settings.default_tool_action(),
            ),
            enabled=tenant_settings.policy_for(tool.name).enabled,
        )

    async def _check_tenant_policy(
        self,
        *,
        tool: ToolDefinition,
        context: ToolExecutionContext,
    ) -> Any:
        """执行前的权威校验：策略一定是最新加载的。"""
        tenant_settings = await self._tenant_service.settings_for(context.tenant_id)
        policy = tenant_settings.policy_for(tool.name)

        await self._raise_if_tenant_denied(
            tool=tool,
            context=context,
            allow=tenant_settings.allow_tool(
                tool.name,
                context.roles,
                default_action=runtime_settings.default_tool_action(),
            ),
            enabled=policy.enabled,
        )

        return tenant_settings

    async def _raise_if_tenant_denied(
        self,
        *,
        tool: ToolDefinition,
        context: ToolExecutionContext,
        allow: bool,
        enabled: bool,
    ) -> None:
        if allow:
            return

        if not enabled:
            raise ToolDisabledError("当前组织已停用该工具。")

        await self._deny(
            tool=tool,
            context=context,
            reason=DENIAL_TENANT,
            message="当前组织未向该角色开放此工具。",
        )

    async def _resolve_connector(
        self,
        *,
        tool: ToolDefinition,
        context: ToolExecutionContext,
    ) -> ConnectorRuntime:
        """取工具所属连接器的运行时快照（含本租户凭据），不可用则抛。

        「不可用」有且只有两种，文案分开：连接器没了（配置问题）、
        凭据没配（运维动作）。合成一句「连接器不可用」等于让人去猜。
        """
        # 惰性导入：connector_service 反过来要用网关做注册，
        # 模块级互相 import 会在启动时形成环。
        from app.services.connector_service import get_connector_runtime

        runtime = await get_connector_runtime(
            connector_id=tool.connector_id,
            tenant_id=context.tenant_id,
        )

        if runtime is None:
            raise ToolConnectorUnavailableError(
                f"工具「{tool.display_name}」所属的连接器已不存在或已停用。"
            )

        if runtime.requires_secret() and not runtime.secret:
            raise ToolCredentialMissingError(
                f"连接器「{runtime.name}」需要凭据，但当前租户尚未配置。"
                "请到控制台「连接器」页填写凭据后重试。"
            )

        return runtime

    async def _with_tenant_credential(
        self,
        *,
        tool: ToolDefinition,
        context: ToolExecutionContext,
    ) -> ToolExecutionContext:
        """按租户策略解析凭据。凭据只存在于本次调用的上下文里。

        两条来源，**互不回退**：

        1. 工具属于连接器 → 取该租户在连接器上的凭据。连接器要求凭据而没有时
           直接失败，不去看 `credential_ref`：那会让「忘了配」表现成「用别人的
           账号调通了」。
        2. 独立工具 → 沿用 `tenant_tool_policies.credential_ref`（环境变量）。
        """
        if tool.connector_id is not None:
            runtime = await self._resolve_connector(tool=tool, context=context)
            return replace(context, connector=runtime)

        tenant_settings = await self._tenant_service.settings_for(context.tenant_id)
        ref = tenant_settings.policy_for(tool.name).credential_ref

        if not ref:
            return context

        credential = self._tenant_service.resolve_credential(ref)

        if not credential:
            return context

        return replace(context, credential=credential)

    async def _pre_execute(
        self,
        *,
        tool: ToolDefinition,
        context: ToolExecutionContext,
        arguments: dict[str, Any],
        is_write: bool,
    ) -> None:
        tenant_settings = await self._check_tenant_policy(tool=tool, context=context)

        station_id = arguments.get(tool.station_id_argument) if tool.station_id_argument else None

        if station_id:
            if self._station_authorizer is None:
                raise ToolResourceDeniedError("未配置站点授权校验，拒绝访问站点资源。")

            try:
                await self._station_authorizer.ensure_access(
                    context=context,
                    station_id=str(station_id),
                    write=is_write,
                )
            except ToolResourceDeniedError:
                raise
            except Exception as exc:
                raise ToolResourceDeniedError("当前资源不可访问。") from exc

        if self._rate_limiter is not None:
            try:
                await self._rate_limiter.enforce(
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                    tool_name=tool.name,
                    is_write=is_write,
                    tenant_limit_per_minute=(
                        tenant_settings.quota.resolved_tool_calls_per_minute()
                    ),
                )
            except RateLimitExceeded as exc:
                # 限流后端不可用（fail-closed）也按「拒绝本次调用」处理，
                # 消息由异常自带，避免把基础设施故障伪装成正常通过。
                raise ToolRateLimitedError(str(exc)) from exc

    async def _traced_invoke(
        self,
        *,
        tool: ToolDefinition,
        context: ToolExecutionContext,
        arguments: dict[str, Any],
    ) -> ToolExecutionResult:
        """给工具调用套一个 span，然后走正常的 `_invoke_handler`。

        两个执行入口（只读 / 已审批）共用这里，避免「审批后的写操作在观测里
        凭空消失」——而写操作恰恰是最需要事后复盘的。

        观测关闭或无 trace 上下文时 `span()` 是空转对象，这段代码等于没写。
        """
        async with span("tool.call", "tool", attrs={"tool": tool.name}) as handle:
            handle.add(
                risk_level=tool.risk_level.value,
                source=tool.source,
                # 键名用于判断「是不是调用姿势变了」，值放 content。
                args_keys=",".join(sorted(arguments.keys()))[:200] or None,
                station_id=arguments.get(tool.station_id_argument)
                if tool.station_id_argument
                else None,
            )
            handle.content(input=arguments)

            result = await self._invoke_handler(
                tool=tool,
                context=context,
                arguments=arguments,
            )

            handle.add(
                ok=result.success,
                verified=result.verified,
                error_code=result.error_code,
                result_chars=len(json.dumps(result.result, ensure_ascii=False, default=str))
                if result.result
                else 0,
            )
            handle.content(output=result.result or result.message)
            if not result.success:
                handle.fail(result.error_code or "tool_failed")
            return result

    async def _invoke_handler(
        self,
        *,
        tool: ToolDefinition,
        context: ToolExecutionContext,
        arguments: dict[str, Any],
    ) -> ToolExecutionResult:
        started = time.perf_counter()

        logger.info(
            "开始调用受控工具。",
            extra={
                "event": "tool_execution_started",
                "tool_name": tool.name,
                "tool_kind": tool.kind,
                "risk_level": tool.risk_level.value,
                "tenant_id": context.tenant_id,
                "user_id": context.user_id,
                "thread_id": context.thread_id,
                "run_id": context.run_id,
                "approval_id": context.approval_id,
            },
        )

        try:
            effective_context = await self._with_tenant_credential(
                tool=tool,
                context=context,
            )
            result = await asyncio.wait_for(
                tool.handler(effective_context, arguments),
                timeout=tool.timeout_seconds,
            )
        except TimeoutError:
            result = ToolExecutionResult(
                success=False,
                message=f"工具执行超时（{tool.timeout_seconds} 秒），结果未知。",
                retryable=False,
                error_code="TOOL_TIMEOUT",
            )
        except (ToolConnectorUnavailableError, ToolCredentialMissingError) as exc:
            # 这两类不是「工具执行异常」，是配置不完整。吞进通用异常分支的话，
            # 用户只会看到「工具调用失败」，而真正要做的事（去控制台填凭据）
            # 一个字都不会出现。
            logger.warning(
                "连接器凭据不可用，本次调用未发出。",
                extra={
                    "event": "tool_connector_unavailable",
                    "tool_name": tool.name,
                    "tenant_id": context.tenant_id,
                    "connector": tool.connector_name,
                    "error_code": exc.code,
                },
            )
            result = ToolExecutionResult(
                success=False,
                message=str(exc),
                retryable=False,
                error_code=exc.code,
            )
        except Exception:
            logger.exception(
                "工具处理函数发生未捕获异常。",
                extra={
                    "event": "tool_execution_exception",
                    "tool_name": tool.name,
                },
            )
            result = ToolExecutionResult(
                success=False,
                message="工具执行过程中出现异常。",
                retryable=False,
                error_code="TOOL_HANDLER_EXCEPTION",
            )

        duration_ms = round((time.perf_counter() - started) * 1000, 2)

        logger.info(
            "受控工具调用结束。",
            extra={
                "event": "tool_execution_finished",
                "tool_name": tool.name,
                "success": result.success,
                "verified": result.verified,
                "error_code": result.error_code,
                "duration_ms": duration_ms,
            },
        )

        if self._audit_sink is not None:
            try:
                await self._audit_sink.record(
                    "tool_executed",
                    {
                        "tool_name": tool.name,
                        "risk_level": tool.risk_level.value,
                        "success": result.success,
                        "verified": result.verified,
                        "error_code": result.error_code,
                        "duration_ms": duration_ms,
                        "argument_keys": sorted(arguments),
                        "tenant_id": context.tenant_id,
                        "user_id": context.user_id,
                        "thread_id": context.thread_id,
                        "run_id": context.run_id,
                        "approval_id": context.approval_id,
                        # 拒绝路径一直带着这两个字段，成功路径漏了：审计里
                        # 只能看到「某个工具跑过」，看不出是哪个智能体跑的。
                        # 构建助手与普通智能体共用这批工具时，这一列就是归因依据。
                        "agent_name": context.agent_name,
                        "agent_revision": context.agent_revision,
                    },
                )
            except Exception:
                # 审计失败不得改变工具执行结论，但必须留下日志痕迹。
                logger.exception(
                    "写入工具审计失败。",
                    extra={"event": "tool_audit_failed", "tool_name": tool.name},
                )

        return result
