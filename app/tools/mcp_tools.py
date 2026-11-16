# app/tools/mcp_tools.py
"""把远端 MCP Server 的工具接入本地受控工具表。

设计原则（与 app/tools/dependencies.py 的注释一致）：
**远端有什么，不等于模型能看到什么。** 每个要暴露的远端工具都必须在这里
登记：本地工具名、中文说明、风险等级、是否走审批、超时。未登记的远端工具
永远不会进入模型目录。

调度映射（这一层不能被绕过）：
    模型 → 本地工具名 → 本表 → remote_tool_name → MCP

授权边界（重要，别误解）：
- 本表里的工具**不设 station_id_argument**。PAP 的 station_grants 用的是
  内部站点 ID（如 station-001），而 MCP 的 station 是站名或通信序列号，
  两者不同域，强行映射只会产生「看起来校验了、其实没有」的假授权。
- MCP 侧的可见范围由令牌的 allowed_stations 决定，写权限由 write_scope 决定。
  所以要限制某个 PAP 租户能看哪些站，请给它配一个受限令牌，
  而不是指望 PAP 的站点授权表。
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.integrations.mcp_client import (
    McpError,
    McpToolClient,
    McpToolDescriptor,
    McpUnavailableError,
)
from app.models.approval import RiskLevel
from app.tools.types import (
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionResult,
)

logger = logging.getLogger(__name__)

READ_SCOPES: tuple[str, ...] = ("tools:read",)


class McpCredentialMissingError(RuntimeError):
    """当前租户没有配置 MCP 访问令牌。"""


@dataclass(frozen=True, slots=True)
class McpToolSpec:
    """一条「本地工具 ↔ 远端工具」映射。"""

    local_name: str
    display_name: str
    description: str
    risk_level: RiskLevel
    requires_approval: bool = False
    timeout_seconds: int = 60
    # 成功时给用户的第一句话。留空则回落到「已完成远端调用」，
    # 只有确实说不出比这更有信息量的话时才留空。
    success_message: str = ""

    # 远端工具名。组合型工具（如控制下发）留空，由专用 handler 处理。
    remote_name: str | None = None

    # 这条映射所属的 MCP 端点。留空表示用 .env 的 MCP_SERVER_URL——
    # 代码白名单里的工具都是这种（它们只跟那一条通道说话）。
    server_url: str | None = None

    # 该端点的令牌。repr=False：dataclass 的 repr 会跟工具名一起进日志，
    # 令牌不能这么被打印出来。
    token: str | None = field(default=None, repr=False)

    # 来源连接器。有值时端点与凭据全部由它接管：`server_url` / `token` 不参与。
    # 「连接器赢」而不是「行上优先」——两处都能指定端点时，后改的那处不生效
    # 是最难查的一类错。
    connector_id: int | None = None
    connector_name: str = ""
    # 注册那一刻连接器是否启用。停用后它的工具必须一起从目录消失。
    connector_ready: bool = True


def endpoint_key(url: str | None) -> str:
    """比较两个端点是否是同一个服务的规范形式。

    只去空白与末尾斜杠：路径本身（/mcp）有意义，不能动。
    127.0.0.1 与 localhost 不做归一——那会把「同一个服务」猜错，
    而猜错的代价是拿 A 的令牌去调 B。
    """
    return (url or "").strip().rstrip("/")


# 远端全部 22 个工具里，这里登记 20 个直接透传 + 1 个组合封装。
# 刻意没有暴露的两个：
#   setScenario      —— 只在演示部署（PRV_DEMO_MODE=1）生效，生产环境切场景没有意义，
#                       但它会把后续查询变成假数据，风险大于收益；
#   executeStationControl —— 单独暴露时模型必须自己保管 preview_id，
#                       跨轮次极易丢失或张冠李戴，改由 prv_apply_station_control 内部串联。
MCP_TOOL_SPECS: tuple[McpToolSpec, ...] = (
    McpToolSpec(
        local_name="prv_server_status",
        display_name="查询燃气 MCP 服务状态",
        description=(
            "查看燃气调压 MCP 服务自身的配置完整性、当前身份与平台连通性。"
            "排障或确认对端可用时先调它。"
        ),
        risk_level=RiskLevel.READ,
        timeout_seconds=30,
    ),
    McpToolSpec(
        local_name="prv_list_stations",
        display_name="查询调压站目录",
        description=(
            "列出当前身份被授权访问的调压站，可用站名或通信序列号做子串筛选。"
            "不确定站名时先调它拿准确的 station 值。"
        ),
        risk_level=RiskLevel.READ,
    ),
    McpToolSpec(
        local_name="prv_station_profile",
        display_name="读取调压站设备档案",
        description="读取设备类型、名称、编号以及平台保存的采样与上报周期配置。",
        risk_level=RiskLevel.READ,
    ),
    McpToolSpec(
        local_name="prv_station_capabilities",
        display_name="核对调压站可用能力",
        description=(
            "核对当前站点的可用能力与已知缺口。"
            "在承诺控制、终端回执、阀位、事件或通知之前必须先调用。"
        ),
        risk_level=RiskLevel.READ,
    ),
    McpToolSpec(
        local_name="prv_station_overview",
        display_name="查询调压站综合概览",
        description=(
            "聚合进出口压力、切断阀、放散阀、IO 报警、运行模式与进口压力波动，"
            "是了解站点整体状态的首选工具。"
        ),
        risk_level=RiskLevel.READ,
        timeout_seconds=90,
    ),
    McpToolSpec(
        local_name="prv_query_pressure",
        display_name="查询进出口压力",
        description=(
            "查询调压站进出口压力，返回平台配置的限值上下限与超限状态。"
            "数值与单位按平台原样透传，不换算。"
        ),
        risk_level=RiskLevel.READ,
    ),
    McpToolSpec(
        local_name="prv_cutoff_valve_status",
        display_name="查询切断阀状态",
        description="查询切断阀状态，区分主路（1#）与备路（2#）。",
        risk_level=RiskLevel.READ,
    ),
    McpToolSpec(
        local_name="prv_relief_valve",
        display_name="查询放散阀状态",
        description="查询放散阀报警通道状态与放散总累积流量（m³）。",
        risk_level=RiskLevel.READ,
    ),
    McpToolSpec(
        local_name="prv_io_alarms",
        display_name="查询 IO 报警通道",
        description=(
            "查询平台实际暴露的 IO/报警通道（门禁、放散阀 1/2、切断阀主/备路），不做虚构补齐。"
        ),
        risk_level=RiskLevel.READ,
    ),
    McpToolSpec(
        local_name="prv_query_alarms",
        display_name="查询报警记录",
        description=(
            "分页查询报警记录：scope=unconfirmed 查未确认报警，"
            "confirmed 查已确认归档，diff 查差压报警归档（不支持时间范围）。"
        ),
        risk_level=RiskLevel.READ,
    ),
    McpToolSpec(
        local_name="prv_relief_samples",
        display_name="查询放散采样明细",
        description="分页查询两路放散字段的历史采样记录。",
        risk_level=RiskLevel.READ,
        timeout_seconds=90,
    ),
    McpToolSpec(
        local_name="prv_relief_report",
        display_name="查询放散量报表",
        description="查询放散量历史报表，period 取 daily（日）或 monthly（月）。",
        risk_level=RiskLevel.READ,
        timeout_seconds=90,
    ),
    # 下面三个导出类工具定成 normal 而不是 write：
    # 它们读的是站点数据，副作用仅是「在 MCP 服务端留下一个导出文件」，
    # 不改变站点配置、不下发指令。用户查一次趋势要等审批，成本远大于风险。
    # 定成 write 也没用——网关对 write 及以上会强制走审批。
    McpToolSpec(
        local_name="prv_pressure_trend",
        display_name="查询压力趋势曲线",
        description=("查询测点历史趋势曲线。远端会在服务端生成并返回 CSV 内容，不修改站点配置。"),
        risk_level=RiskLevel.NORMAL,
        timeout_seconds=180,
        success_message="趋势曲线已取回。",
    ),
    McpToolSpec(
        local_name="prv_export_records",
        display_name="导出站点记录",
        description=(
            "把报警明细或放散采样明细分批导出为 CSV（UTF-8 BOM）。"
            "只在 MCP 服务端产生导出文件，不修改站点配置。"
        ),
        risk_level=RiskLevel.NORMAL,
        timeout_seconds=180,
        success_message="记录导出完成。",
    ),
    McpToolSpec(
        local_name="prv_download_report_pdf",
        display_name="下载放散报表 PDF",
        description="把放散报表 PDF 下载到 MCP 服务端本地目录，不修改站点配置。",
        risk_level=RiskLevel.NORMAL,
        timeout_seconds=120,
        success_message="报表已下载到 MCP 服务端。",
    ),
    McpToolSpec(
        local_name="prv_control_config",
        display_name="查询控制配置",
        description="查询运行模式、压力档位、曲线压力限值与指定星期的时段表。",
        risk_level=RiskLevel.READ,
    ),
    McpToolSpec(
        local_name="prv_prepare_station_control",
        display_name="生成调压控制预览",
        description=(
            "生成一次控制预览，只做条件检查与预演，**不向设备下发任何指令**。"
            "返回的 preview_id 有效期为 5 分钟。"
        ),
        risk_level=RiskLevel.READ,
    ),
    McpToolSpec(
        local_name="prv_cancel_station_control",
        display_name="作废控制预览",
        description="作废一张尚未提交的本地预览（用户拒绝或改变主意时使用）。",
        risk_level=RiskLevel.READ,
        timeout_seconds=30,
    ),
    McpToolSpec(
        local_name="prv_station_command",
        display_name="查询控制命令记录",
        description="按编号查询一条控制命令的本地记录：创建/提交时间、动作、每步平台返回与状态。",
        risk_level=RiskLevel.READ,
        timeout_seconds=30,
    ),
    McpToolSpec(
        local_name="prv_list_commands",
        display_name="列出站点控制命令",
        description="列出指定站点最近的控制命令（最多 10 条，按创建时间倒序）。",
        risk_level=RiskLevel.READ,
        timeout_seconds=30,
    ),
    McpToolSpec(
        local_name="prv_apply_station_control",
        display_name="下发调压控制指令",
        description=(
            "向调压站下发控制指令（修改压力档位或运行模式），属于外部写入。"
            "内部先做服务端预演再提交；预演不通过时不会下发。"
            "调用前应先用 prv_station_capabilities 核对该站点是否支持该动作。"
        ),
        risk_level=RiskLevel.EXTERNAL,
        requires_approval=True,
        timeout_seconds=120,
    ),
)


# 本地工具名 → 远端工具名。组合型工具没有远端同名对应，单独处理。
_REMOTE_BY_LOCAL: dict[str, str] = {
    "prv_server_status": "getServerStatus",
    "prv_list_stations": "listStations",
    "prv_station_profile": "getStationProfile",
    "prv_station_capabilities": "getStationCapabilities",
    "prv_station_overview": "getStationOverview",
    "prv_query_pressure": "queryPressure",
    "prv_cutoff_valve_status": "getCutoffValveStatus",
    "prv_relief_valve": "queryReliefValve",
    "prv_io_alarms": "queryIOAlarms",
    "prv_query_alarms": "queryAlarms",
    "prv_relief_samples": "queryReliefSamples",
    "prv_relief_report": "queryReliefReport",
    "prv_pressure_trend": "queryPressureTrend",
    "prv_export_records": "exportStationRecords",
    "prv_download_report_pdf": "downloadReportPdf",
    "prv_control_config": "getControlConfig",
    "prv_prepare_station_control": "prepareStationControl",
    "prv_cancel_station_control": "cancelStationControl",
    "prv_station_command": "getStationCommand",
    "prv_list_commands": "listStationCommands",
}

APPLY_TOOL_NAME = "prv_apply_station_control"
_APPLY_PREPARE_REMOTE = "prepareStationControl"
_APPLY_EXECUTE_REMOTE = "executeStationControl"

# 远端返回里判定「这是演示数据」的标记。演示数据不能被当作已核验的真实结果。
_DEMO_SOURCES = frozenset({"mock", "demo", "simulated"})

# 远端用 pydantic 生成 schema，参数只有 title 没有 description，模型只能靠猜。
# 这几个参数猜错的代价很高（控制动作填错就是误操作），所以在这里补中文说明。
_STATION_HINT = (
    "调压站标识：可用站名、通信序列号或设备主键。"
    "不确定时先调 prv_list_stations 取准确值；留空则由服务端使用默认站点。"
)

_SCHEMA_HINTS: dict[str, dict[str, str]] = {
    "prv_apply_station_control": {
        "station": _STATION_HINT,
        "action": (
            "控制动作，只能取以下值："
            "close_main（关闭 1# 主路切断阀）、close_backup（关闭 2# 备用切断阀）、"
            "high_pressure（切到高压档）、low_pressure（切到低压档）、"
            "set_schedule（整体覆盖指定星期的时段表，需同时给 weekday 与 schedule）。"
            "peak_mode、valley_mode、set_pressure 当前未开放，填了会被拒绝。"
        ),
        "weekday": "星期，1=周一 … 7=周日，仅 set_schedule 需要。",
        "schedule": (
            "时段表，仅 set_schedule 需要：1~48 行，每行含 startingTime / endTime / setPoint；"
            "时间格式 HH:mm 且为 15 分钟步长，24:00 只能作结束时间，相邻行至少间隔 15 分钟；"
            "不接受空表（不允许用空表清空原表）。"
        ),
    },
    "prv_prepare_station_control": {
        "station": _STATION_HINT,
        "action": (
            "控制动作，只能取以下值：close_main / close_backup / high_pressure / "
            "low_pressure / set_schedule。返回值里 confirmation_can_execute 为 false 时"
            "表示条件不满足，blockers 会说明原因。"
        ),
        "weekday": "星期，1=周一 … 7=周日，仅 set_schedule 需要。",
        "schedule": "时段表，仅 set_schedule 需要，格式见动作说明。",
    },
    "prv_cancel_station_control": {
        "preview_id": (
            "要作废的预览编号，必须原样取自 prv_prepare_station_control 返回的 preview_id，"
            "不要自行构造。"
        ),
    },
    "prv_station_command": {
        "command_id": (
            "控制命令编号，来自控制下发结果或 prv_list_commands 的返回，不要自行编造。"
        ),
    },
    "prv_query_alarms": {
        "station": _STATION_HINT,
        "scope": (
            "unconfirmed=未确认的基础报警；confirmed=已确认的报警归档；"
            "diff=差压报警归档（不支持时间范围）。"
        ),
        "start": "起始时间，北京时间，格式 yyyy-MM-dd HH:mm:ss；留空取近 24 小时。",
        "end": "结束时间，北京时间，格式 yyyy-MM-dd HH:mm:ss。",
        "keyword": "按测点名称做子串过滤，例如「放散」只看放散相关报警。",
        "start_page": "起始页码，续查时用返回 pagination.next_query 里的值。",
        "max_pages": "最多取几页，用于控制单次返回体积。",
    },
    "prv_relief_samples": {
        "station": _STATION_HINT,
        "start": "起始时间，北京时间，格式 yyyy-MM-dd HH:mm:ss。",
        "end": "结束时间，北京时间，格式 yyyy-MM-dd HH:mm:ss。",
        "start_page": "起始页码，续查时用返回 pagination.next_query 里的值。",
        "max_pages": "最多取几页。",
    },
    "prv_relief_report": {
        "station": _STATION_HINT,
        "period": "daily=日报（period_value 传 yyyy-MM）；monthly=月报（period_value 传 yyyy）。",
        "period_value": "统计周期值，格式取决于 period；留空取当前月／当前年。",
    },
    "prv_export_records": {
        "station": _STATION_HINT,
        "record_type": "alarms=报警明细；relief=放散采样明细。",
        "start": "起始时间，北京时间，格式 yyyy-MM-dd HH:mm:ss。",
        "end": "结束时间，北京时间，格式 yyyy-MM-dd HH:mm:ss。",
        "start_page": "起始页码。",
        "max_pages": "最多导出几页，默认 100；总量大时先用较小的值试一次。",
    },
    # 这个参数填错的代价最高：远端只认它自己生成的路径，自行拼出来的会直接失败。
    "prv_download_report_pdf": {
        "station": _STATION_HINT,
        "report_path": (
            "报表路径，必须**原样**取自 prv_relief_report 返回的 report_path，"
            "不要自行拼接服务器路径。"
        ),
        "file_name": (
            "保存到 MCP 服务端时使用的文件名，可留空（用平台返回的文件名）。"
            "注意文件落在 MCP 服务端，不在用户本机。"
        ),
    },
    "prv_control_config": {
        "station": _STATION_HINT,
        "weekday": "要读取时段表的星期，1=周一 … 7=周日；留 0 或省略按北京时间当天。",
    },
    "prv_list_stations": {
        "query": "可选子串筛选，匹配站名或通信序列号，不区分大小写。",
    },
    "prv_list_commands": {
        "station": _STATION_HINT,
    },
    "prv_pressure_trend": {
        "station": _STATION_HINT,
        "accesses": (
            "测点通道代码数组（如 [\"CH1\",\"CH2\"]），最多 3 个；"
            "留空则自动取站名含「压力」的通道，通常不必填。"
        ),
        # 远端只校验格式、不猜：传 ISO 8601 会被直接拒，第一次调用就撞过。
        "start": "起始时间，北京时间，格式 yyyy-MM-dd HH:mm:ss；留空取近 24 小时。",
        "end": "结束时间，北京时间，格式 yyyy-MM-dd HH:mm:ss；与 start 跨度不超过 31 天。",
        "size": "时间选项分钟数（1~44640），不是采样间隔也不是点数上限；留空按窗口计算。",
        "max_series": "最多返回几条曲线，默认 3。",
    },
}


def _apply_schema_hints(schema: dict[str, Any], *, local_name: str) -> dict[str, Any]:
    """
    在远端 schema 上补参数说明，并禁止未声明参数。

    远端每次只给类型不给语义，模型把 action 填成「关闭主路」这种自然语言
    是最常见的失败方式。补一层描述比在提示词里解释 21 个工具便宜得多。
    """
    patched = copy.deepcopy(schema)

    properties = patched.get("properties")

    if not isinstance(properties, dict):
        patched["properties"] = {}
        properties = patched["properties"]

    if "station" in properties and "description" not in properties["station"]:
        properties["station"]["description"] = _STATION_HINT

    for name, text in _SCHEMA_HINTS.get(local_name, {}).items():
        if name not in properties:
            # 远端把参数改名后，这里的说明会静默失效——模型看不到任何提示，
            # 却也不会报错。启动时打一条警告，比等到线上答错一轮再排查便宜。
            logger.warning(
                "参数说明指向了远端不存在的参数，已忽略。",
                extra={
                    "event": "mcp_schema_hint_unknown_param",
                    "tool_name": local_name,
                    "param": name,
                    "available": sorted(properties),
                },
            )
            continue
        if not properties[name].get("description"):
            properties[name]["description"] = text

    patched.setdefault("type", "object")
    patched["additionalProperties"] = False

    return patched


def remote_to_local_map() -> dict[str, str]:
    """远端工具名 → 本地工具名。

    接入方向是 _REMOTE_BY_LOCAL（本地 → 远端），而控制台的「发现工具」
    要按远端清单反查「这个远端工具接没接」，所以在这里做一次反转。
    不在调用方各写一遍，免得以后新增映射时只改一处、另一处静默变空。
    """
    return {remote: local for local, remote in _REMOTE_BY_LOCAL.items()}


def _token_for_tenant(tenant_id: str) -> str:
    """
    取租户令牌。

    没有配置就报错，绝不回落到「另一个租户的令牌」：
    那等于用别人的身份去读数据，是最难在事后发现的那类越权。
    """
    token = settings.mcp_tenant_tokens.get(tenant_id)

    if not token:
        raise McpCredentialMissingError(f"租户 {tenant_id} 未配置 MCP 访问令牌。")

    return token


def _endpoint_of(spec: McpToolSpec | None) -> str:
    """这条映射要调的地址：自己的端点优先，没写才回落到 .env。"""
    return ((spec.server_url if spec else None) or settings.mcp_server_url or "").strip()


def _is_default_endpoint(url: str) -> bool:
    return endpoint_key(url) == endpoint_key(settings.mcp_server_url)


def _client_for(
    context: ToolExecutionContext,
    *,
    call_timeout: float,
    spec: McpToolSpec | None = None,
) -> McpToolClient:
    # 连接器优先，且完全接管：端点用它登记的，令牌用**调用方租户**自己的。
    # 不回落到 spec.server_url / spec.token——那两处是连接器出现之前的写法，
    # 混着用会出现"端点换了、令牌还是旧的"这种半新半旧的状态。
    connector = context.connector
    if connector is not None:
        url = (connector.base_url or "").strip()
        if not url:
            raise McpCredentialMissingError(f"连接器 {connector.name} 没有配置端点地址。")
        return McpToolClient(
            url=url,
            token=connector.secret or None,
            connect_timeout_seconds=settings.mcp_connect_timeout_seconds,
            call_timeout_seconds=call_timeout,
        )

    url = _endpoint_of(spec)

    if not url:
        raise McpCredentialMissingError("未配置 MCP_SERVER_URL。")

    if spec and spec.server_url and not _is_default_endpoint(spec.server_url):
        # 外部端点只认接入时登记的那一个凭据：把默认端点的租户令牌
        # 发给另一个服务，等于替管理员泄露凭据。没登记令牌就当它不鉴权。
        token: str | None = spec.token or None
    else:
        token = _token_for_tenant(context.tenant_id)

    return McpToolClient(
        url=url,
        token=token,
        connect_timeout_seconds=settings.mcp_connect_timeout_seconds,
        call_timeout_seconds=call_timeout,
    )


def _truncate(payload: dict[str, Any], *, limit: int) -> tuple[dict[str, Any], bool]:
    """
    限制回给模型的体积。

    趋势/站点目录这类结果可以很大，原样塞进上下文既贵又会挤掉真正的指令。
    超限时保留可读片段并明确告知已截断，而不是悄悄丢掉。
    """
    text = json.dumps(payload, ensure_ascii=False, default=str)

    if len(text) <= limit:
        return payload, False

    return (
        {
            "truncated": True,
            "original_chars": len(text),
            "excerpt": text[:limit],
            "note": "结果超过单次返回上限，已截断。请缩小查询范围或分页重试。",
        },
        True,
    )


# 出现这些集合字段且为空，说明远端确实没有记录。
# 「查到了 0 条」和「调用失败」对用户的含义完全不同，不能都叫「已完成」。
_EMPTY_COLLECTION_KEYS = ("stations", "records", "items", "rows", "alarms", "points")


def _is_empty_result(payload: dict[str, Any]) -> bool:
    count = payload.get("count")

    if isinstance(count, int):
        return count == 0

    for key in _EMPTY_COLLECTION_KEYS:
        value = payload.get(key)

        if isinstance(value, list) and not value:
            return True

    return False


def _empty_message(payload: dict[str, Any], *, tool_name: str | None) -> str:
    """
    空结果要说清「为什么是空的」。

    远端对空白名单返回 success=true + count=0，而不是报错。若只回一句
    「已完成」，模型和用户都会把它读成「站点全离线」或「服务异常」，
    而真实原因是权限边界。
    """
    if tool_name == "prv_list_stations":
        total = payload.get("total_authorized")
        visible = payload.get("visible_to_identity")

        if visible == 0 and isinstance(total, int) and total > 0:
            return (
                f"当前身份看不到任何站点：平台账号下共 {total} 个站点，"
                "但本租户的 MCP 令牌没有被授权任何站点（空白名单）。"
                "这是权限边界，不是服务故障，也不代表站点离线。"
            )

        return "当前身份看不到任何站点。"

    return "远端未返回任何记录。"


def _to_result(
    payload: dict[str, Any],
    *,
    message_prefix: str = "",
    ok_message: str = "",
    tool_name: str | None = None,
) -> ToolExecutionResult:
    """
    把远端业务结果翻译成平台的 ToolExecutionResult。

    远端用 success 字段表达业务失败（协议层 is_error 恒为 False），
    所以这里必须读 success，不能看有没有抛异常。
    """
    raw_success = payload.get("success", True)
    success = raw_success if isinstance(raw_success, bool) else bool(raw_success)

    data_source = str(payload.get("data_source") or "").strip()
    is_demo = data_source.lower() in _DEMO_SOURCES or payload.get("demo_mode") is True

    truncated_payload, truncated = _truncate(payload, limit=settings.mcp_max_result_chars)

    if success:
        if ok_message:
            message = ok_message
        elif message_prefix:
            message = message_prefix
        elif _is_empty_result(payload):
            message = _empty_message(payload, tool_name=tool_name)
        else:
            message = "已完成远端调用。"

        if is_demo:
            message += "注意：远端返回的是演示数据，不代表真实场站状态。"
        if truncated:
            message += "结果过大，已截断。"
    else:
        error_text = str(payload.get("error") or "远端未说明失败原因。")
        message = f"{message_prefix}远端拒绝了本次调用：{error_text}".strip()
        if truncated:
            message += "（返回内容已截断）"

    # 只有远端明确声明数据来自真实接口时才敢说「已核验」。
    # 演示数据、缺省 data_source 的解析类结果都不算。
    verified = bool(success) and not is_demo and data_source.upper() == "API"

    return ToolExecutionResult(
        success=success,
        verified=verified,
        result=truncated_payload,
        message=message,
        verification={
            "source": data_source or "mcp",
            "verified": verified,
            "truncated": truncated,
        },
        external_operation_id=(payload.get("command_id") or payload.get("preview_id") or None),
        error_code=None if success else "MCP_TOOL_ERROR",
        retryable=False,
    )


def _failure(exc: McpError | McpCredentialMissingError) -> ToolExecutionResult:
    if isinstance(exc, McpCredentialMissingError):
        return ToolExecutionResult(
            success=False,
            message=str(exc),
            error_code="MCP_CREDENTIAL_MISSING",
        )

    return ToolExecutionResult(
        success=False,
        message=str(exc),
        error_code=exc.code,
        retryable=isinstance(exc, McpUnavailableError),
    )


def _make_passthrough_handler(spec: McpToolSpec, remote_name: str):  # noqa: ANN202
    """生成一个把参数原样转发给远端同名工具的 handler。"""

    async def handler(
        context: ToolExecutionContext,
        arguments: dict[str, Any],
    ) -> ToolExecutionResult:
        try:
            client = _client_for(
                context, call_timeout=float(spec.timeout_seconds), spec=spec
            )
            payload = await client.call_tool(
                remote_tool_name=remote_name,
                arguments=arguments,
            )
        except (McpError, McpCredentialMissingError) as exc:
            logger.warning(
                "MCP 工具调用失败。",
                extra={
                    "event": "mcp_tool_failed",
                    "tool_name": spec.local_name,
                    "remote_tool_name": remote_name,
                    "error_code": getattr(exc, "code", "MCP_CREDENTIAL_MISSING"),
                    "tenant_id": context.tenant_id,
                },
            )
            return _failure(exc)

        return _to_result(
            payload,
            ok_message=spec.success_message,
            tool_name=spec.local_name,
        )

    return handler


def _make_apply_handler(spec: McpToolSpec):  # noqa: ANN202
    """
    控制下发：审批通过后，先远端预演再提交。

    之所以把 prepare 与 execute 合成一个本地工具：
    execute 需要的 preview_id 由 prepare 产生，有效期只有 5 分钟。
    若拆成两个工具交给模型保管，跨轮次很容易超时或张冠李戴，
    而审批流程本身可能停顿几分钟。合成之后，preview 只在审批通过后的
    这一瞬间存在，不存在「审批单指向一个过期预览」的窗口。
    """

    async def handler(
        context: ToolExecutionContext,
        arguments: dict[str, Any],
    ) -> ToolExecutionResult:
        try:
            client = _client_for(
                context, call_timeout=float(spec.timeout_seconds), spec=spec
            )

            prepare_args = {
                key: value
                for key, value in arguments.items()
                if value is not None and value != "" and value != []
            }

            prepared = await client.call_tool(
                remote_tool_name=_APPLY_PREPARE_REMOTE,
                arguments=prepare_args,
            )

            if not prepared.get("success", False):
                return _to_result(prepared, message_prefix="控制预演未通过，未下发任何指令。")

            # prepare 只要参数合法就会 success=true，真正能不能执行看这个字段。
            # 条件不满足时（写开关关闭、动作不在白名单、回路未启用）绝不能继续调
            # execute —— 那不是「试一下」，而是拿审批通过当条件满足。
            if not prepared.get("confirmation_can_execute", False):
                blockers = prepared.get("blockers") or []
                reason = "；".join(str(item) for item in blockers) or "远端未说明原因。"
                return ToolExecutionResult(
                    success=False,
                    result=prepared,
                    message=f"控制预演未通过，未下发任何指令。原因：{reason}",
                    error_code="MCP_PRECONDITION_FAILED",
                )

            preview_id = prepared.get("preview_id")

            if not preview_id:
                return ToolExecutionResult(
                    success=False,
                    result=prepared,
                    message="远端没有返回预览编号，为避免误下发已中止。",
                    error_code="MCP_PREVIEW_MISSING",
                )

            execute_args: dict[str, Any] = {
                "preview_id": str(preview_id),
                "confirm": True,
            }
            # 远端的 execute 会拿这两个字段与预览记录比对，传原值即可；
            # 不一致时远端返回 PREVIEW_MISMATCH，不会写设备。
            for key in ("station", "action"):
                if arguments.get(key):
                    execute_args[key] = arguments[key]

            executed = await client.call_tool(
                remote_tool_name=_APPLY_EXECUTE_REMOTE,
                arguments=execute_args,
            )
        except (McpError, McpCredentialMissingError) as exc:
            logger.warning(
                "MCP 控制下发失败。",
                extra={
                    "event": "mcp_tool_failed",
                    "tool_name": spec.local_name,
                    "error_code": getattr(exc, "code", "MCP_CREDENTIAL_MISSING"),
                    "tenant_id": context.tenant_id,
                },
            )
            return _failure(exc)

        # 把预演阶段看到的条件一起带回，便于用户判断「为什么快照与刚才不同」。
        merged = dict(executed)
        merged.setdefault("preview", prepared.get("preview") or prepared.get("plan"))

        return _to_result(merged, message_prefix="控制指令已提交。")

    return handler


def _build_definition(
    spec: McpToolSpec,
    *,
    descriptor: McpToolDescriptor | None,
) -> ToolDefinition:
    if spec.local_name == APPLY_TOOL_NAME:
        # 组合工具的入参与远端 prepare 完全相同。
        remote_name = _APPLY_PREPARE_REMOTE
        handler = _make_apply_handler(spec)
    else:
        # 代码登记的工具靠 _REMOTE_BY_LOCAL 查远端名；管理员接入的工具
        # 把远端名直接带在 spec 上（这条表外的映射只有它自己知道）。
        remote_name = spec.remote_name or _REMOTE_BY_LOCAL.get(spec.local_name)
        if not remote_name:
            raise ValueError(f"工具 {spec.local_name} 没有登记远端工具名。")
        handler = _make_passthrough_handler(spec, remote_name)

    schema = (
        descriptor.input_schema
        if descriptor
        else {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
    )

    schema = _apply_schema_hints(schema, local_name=spec.local_name)

    return ToolDefinition(
        name=spec.local_name,
        display_name=spec.display_name,
        description=spec.description,
        kind="mcp",
        source="mcp",
        risk_level=spec.risk_level,
        requires_approval=spec.requires_approval,
        required_scopes=READ_SCOPES,
        input_schema=schema,
        handler=handler,
        timeout_seconds=spec.timeout_seconds,
        connector_id=spec.connector_id,
        connector_name=spec.connector_name,
        connector_ready=spec.connector_ready,
    )


class McpConfigError(RuntimeError):
    """MCP 通道缺配置（未启用 / 没地址 / 没令牌）。属于部署问题，不是远端故障。"""


def code_local_names() -> set[str]:
    """代码里登记过的本地工具名。这些名字不允许被配置覆盖，也不允许「取消接入」。"""
    return {spec.local_name for spec in MCP_TOOL_SPECS}


async def fetch_remote_tools(
    *,
    url: str = "",
    token: str = "",
    timeout_seconds: float | None = None,
) -> dict[str, McpToolDescriptor]:
    """拉取远端工具清单，按远端名索引。

    「发现」与「接入」共用这一份判定：两处各写一遍，迟早会出现
    发现能看到、接入却报「远端没有这个工具」的错位。

    令牌按端点区分：默认端点没给令牌时回落到 .env 里的租户令牌；
    其它端点只认调用方给的那一个，没给就当它不鉴权（内网服务常这样）。
    绝不把默认端点的令牌顺手套到另一个地址上——那是替管理员泄露凭据。
    """
    default_target = (settings.mcp_server_url or "").strip()
    target = (url or default_target or "").strip()
    if not target:
        raise McpConfigError("未配置 MCP Server URL（.env 里的 MCP_SERVER_URL）。")

    bearer: str | None = (token or "").strip() or None

    if bearer is None and endpoint_key(target) == endpoint_key(default_target):
        tokens = settings.mcp_tenant_tokens or {}
        fallback = tokens.get(settings.platform_tenant_id) or next(
            (value for value in tokens.values() if value), ""
        )
        bearer = (fallback or "").strip() or None

        if bearer is None:
            raise McpConfigError(
                "没有可用的 MCP 令牌：请在 Bearer Token 里填一个，"
                "或在 .env 配 MCP_TENANT_TOKENS。"
            )

    wait = timeout_seconds or settings.mcp_connect_timeout_seconds
    client = McpToolClient(
        url=target,
        token=bearer,
        connect_timeout_seconds=wait,
        call_timeout_seconds=wait,
    )
    descriptors = await client.list_tools()
    return {descriptor.name: descriptor for descriptor in descriptors}


def build_dynamic_definition(
    spec: McpToolSpec,
    *,
    descriptor: McpToolDescriptor | None,
) -> ToolDefinition:
    """给管理员接入的 spec 构造工具定义（远端 schema 决定入参）。"""
    return _build_definition(spec, descriptor=descriptor)


async def register_mcp_tools(
    gateway: Any,
    *,
    extra_specs: tuple[McpToolSpec, ...] = (),
) -> int:
    """
    按白名单把远端工具注册进本地 Gateway。

    `extra_specs` 是管理员在控制台接入的工具（来自 mcp_registered_tools）。
    代码白名单优先：同名时跳过 extra，避免一条配置顶掉代码里的审批路径。

    返回实际注册数量。远端不可达时返回 0 并记录告警——启动不应因为
    一个外部服务没起来就失败，但模型也因此看不到这些能力，这是可接受的状态。
    """
    if not settings.mcp_enabled:
        logger.info(
            "MCP 接入未启用（MCP_ENABLED=false），跳过注册。",
            extra={"event": "mcp_disabled"},
        )
        return 0

    if not settings.mcp_server_url:
        logger.warning(
            "MCP_ENABLED=true 但未配置 MCP_SERVER_URL，跳过注册。",
            extra={"event": "mcp_url_missing"},
        )
        return 0

    if not [token for token in settings.mcp_tenant_tokens.values() if token]:
        logger.warning(
            "MCP 已启用但没有为任何租户配置令牌，跳过注册。",
            extra={"event": "mcp_token_missing"},
        )
        return 0

    try:
        default_by_name = await fetch_remote_tools()
    except (McpError, McpConfigError) as exc:
        logger.warning(
            "拉取 MCP 工具清单失败，本次不注册 MCP 工具。",
            extra={"event": "mcp_list_tools_failed", "error_code": getattr(exc, "code", "")},
            exc_info=True,
        )
        return 0

    # 端点 → 远端清单。默认端点已经拿到了，其它端点按接入项登记的地址再拉一次；
    # 某个端点连不上只影响它自己的工具，不该把默认端点的一起拖下水。
    by_server: dict[str, dict[str, McpToolDescriptor]] = {
        endpoint_key(settings.mcp_server_url): default_by_name
    }

    async def descriptors_for(spec: McpToolSpec) -> dict[str, McpToolDescriptor]:
        endpoint = _endpoint_of(spec)
        key = endpoint_key(endpoint)

        if key in by_server:
            return by_server[key]

        try:
            by_server[key] = await fetch_remote_tools(url=endpoint, token=spec.token or "")
        except (McpError, McpConfigError) as exc:
            logger.warning(
                "接入项所属的 MCP 端点不可达，本次跳过该端点的工具。",
                extra={
                    "event": "mcp_endpoint_unreachable",
                    "endpoint": endpoint,
                    "error_code": getattr(exc, "code", ""),
                },
            )
            by_server[key] = {}

        return by_server[key]

    registered = 0
    missing: list[str] = []

    for spec in MCP_TOOL_SPECS:
        remote_name = (
            _APPLY_PREPARE_REMOTE
            if spec.local_name == APPLY_TOOL_NAME
            else _REMOTE_BY_LOCAL.get(spec.local_name)
        )

        descriptor = default_by_name.get(remote_name) if remote_name else None

        if descriptor is None:
            missing.append(f"{spec.local_name}->{remote_name}")
            continue

        gateway.register_tool(_build_definition(spec, descriptor=descriptor), replace=True)
        registered += 1

    # 管理员接入的工具：代码白名单优先，同名直接跳过。
    # 跳过而不是报错——代码里的定义带审批语义，配置不该有能力顶掉它。
    code_names = code_local_names()
    dynamic_skipped: list[str] = []
    for spec in extra_specs:
        if spec.local_name in code_names:
            dynamic_skipped.append(spec.local_name)
            continue

        by_name = await descriptors_for(spec)
        remote_name = spec.remote_name
        descriptor = by_name.get(remote_name) if remote_name else None

        if descriptor is None:
            missing.append(f"{spec.local_name}@{_endpoint_of(spec)}->{remote_name}")
            continue

        gateway.register_tool(_build_definition(spec, descriptor=descriptor), replace=True)
        registered += 1

    if dynamic_skipped:
        logger.warning(
            "管理员接入的工具与代码白名单同名，已忽略配置。",
            extra={"event": "mcp_dynamic_specs_shadowed", "tool_names": dynamic_skipped},
        )

    if missing:
        logger.warning(
            "部分 MCP 工具在远端不存在，已跳过。",
            extra={"event": "mcp_tools_missing", "missing": missing},
        )

    logger.info(
        "MCP 工具注册完成。",
        extra={
            "event": "mcp_tools_registered",
            "registered": registered,
            "dynamic_specs": len(extra_specs),
            "endpoints": len(by_server),
            "remote_total": len(default_by_name),
            "skipped_by_policy": max(0, len(default_by_name) - registered),
        },
    )

    return registered
