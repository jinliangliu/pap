# app/studio/parsers.py
"""从文件里读出「这要建成什么」。

Studio 的导入要同时收三类东西：知识库文档、技能、OpenAPI 描述的工具。
三者的差异**不在扩展名**：一个 `.md` 可能是现场规程，也可能是写了 front matter
的技能；一个 `.json` 可能是工具导出、技能导出，也可能是 OpenAPI 规格。
所以按内容判断，扩展名只用来排除明显不可能的输入。

解析与写入刻意分开：解析是纯函数（给字节、还结构），能脱离数据库单测；
写入在 `importer.py`。OpenAPI 到平台工具定义之间有大量「规格允许但平台不支持」
的情况（cookie 参数、非 JSON 请求体、数组类型、`$ref` 指向外部文件），
一律收集成 notes 交给人看，**不静默丢弃**——少一个参数，错要到调用时才暴露，
而那时人已经不知道是导入时丢的了。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.knowledge.ingest import SUPPORTED_SUFFIXES
from app.services import skill_service

# 知识库文档的判定范围。结构化的 .json/.yaml 不进知识库：它们不是「资料」，
# 而是配置，塞进切片只会污染检索。
STRUCTURED_SUFFIXES = {".json", ".yaml", ".yml"}

TARGET_KNOWLEDGE = "knowledge"
TARGET_SKILL = "skill"
TARGET_TOOL = "tool"
TARGET_AUTO = "auto"

TARGETS = (TARGET_KNOWLEDGE, TARGET_SKILL, TARGET_TOOL)

# 平台工具定义的硬上限，与 `app/schemas/custom_tool.py` 对齐。
MAX_TOOL_PARAMS = 32
MAX_TOOL_DESC_CHARS = 4000
MAX_TOOL_URL_CHARS = 1024

# 这些请求头由 HTTP 客户端自己管，或会把凭据写进配置表，一律不允许。
# 与 `app/schemas/custom_tool.py` 的 `_FORBIDDEN_HEADERS` 同一份规则：
# 这里只用来自 OpenAPI 的 header 参数里预先剔掉，真正兜底仍在那份校验里。
SKIP_PARAM_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "transfer-encoding",
        "connection",
        "authorization",
        "cookie",
        "proxy-authorization",
        "accept",
        "accept-encoding",
        "user-agent",
    }
)

_TYPE_MAP = {
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "string": "string",
}

_HTTP_METHODS = ("get", "post", "put", "patch", "delete")

# 只认「三个横线独占一行」开头的 front matter；正文里普通的 `---` 分隔线
# 不会命中闭合标记，会退回「没有 front matter」。
_FRONT_MATTER_RE = re.compile(
    r"\A\ufeff?[ \t]*---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)

_H1_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_INLINE_LIST_SEP = re.compile(r"[,，、;；]")

# 平台把参数名当标识符处理（会进模型目录的 JSON Schema）。OpenAPI 允许
# `user-id`、`X-Trace` 这类名字，转进来会被校验拒掉——与其整条工具导入失败，
# 不如在解析时剔掉并说清楚：改名会改变线上请求的形状，不能替人决定。
_PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class StudioParseError(ValueError):
    """文件读不出可导入的内容。消息要能直接告诉人下一步怎么做。"""


@dataclass
class SkillDraft:
    name: str
    display_name: str
    description: str
    instruction: str
    trigger_keywords: list[str] = field(default_factory=list)
    allowed_tools: list[str] | None = None
    priority: int = 100
    enabled: bool = True
    notes: list[str] = field(default_factory=list)

    def preview(self, *, exists: bool = False) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "trigger_keywords": self.trigger_keywords,
            "allowed_tools": self.allowed_tools or [],
            "priority": self.priority,
            "enabled": self.enabled,
            "instruction_chars": len(self.instruction),
            # 正文给人看一眼就够，不必把 8000 字全塞进预览。
            "instruction_head": self.instruction[:600],
            "exists": exists,
            "notes": self.notes,
        }


@dataclass
class ToolDraft:
    name: str
    display_name: str
    description: str
    method: str
    url: str
    risk_level: str
    params: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    deprecated: bool = False

    def preview(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "method": self.method,
            "url": self.url,
            "risk_level": self.risk_level,
            "params_count": len(self.params),
            "params": self.params,
            "deprecated": self.deprecated,
            "notes": self.notes,
        }


@dataclass
class SkillBundle:
    """一份 `{"skills": [...]}` 导出文件。"""

    entries: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)


@dataclass
class ToolBundle:
    """一份 `{"tools": [...]}` 导出文件，形状已经是平台自己的定义。"""

    entries: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 通用
# --------------------------------------------------------------------------- #


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [item.strip() for item in _INLINE_LIST_SEP.split(value)]
        return [item for item in parts if item]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _first_paragraph(text: str) -> str:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ">", "|", "!", "```")):
            continue
        return line
    return ""


def _strip_suffix(name: str) -> str:
    return Path(name).stem if Path(name).suffix else name


def _looks_absolute(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


# --------------------------------------------------------------------------- #
# 技能：Markdown + front matter
# --------------------------------------------------------------------------- #


def parse_skill_markdown(filename: str, text: str) -> SkillDraft:
    """一个 `.md` 就是一个技能：front matter 写元数据，正文就是作业指令。

    没有 front matter 时也收：名字取文件名、描述取第一段正文。理由是不能要求
    「先学会 YAML 才能建技能」——但这样建出来的技能触发词为空，只能靠 `/名字`
    显式调用，所以要在 notes 里说清楚。
    """
    notes: list[str] = []
    body = text
    meta: dict[str, Any] = {}

    match = _FRONT_MATTER_RE.match(text)

    if match:
        try:
            loaded = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            raise StudioParseError(
                f"front matter 不是合法 YAML：{exc.__class__.__name__}。"
                "文件开头到第二个 `---` 之间必须是 `键: 值`。"
            ) from exc

        if loaded is not None and not isinstance(loaded, dict):
            raise StudioParseError("front matter 必须是一组 `键: 值`，不能是列表或标量。")

        meta = loaded or {}
        body = text[match.end():]
    else:
        notes.append("没有 front matter，名字取文件名、描述取第一段正文。")

    instruction = body.strip()

    if not instruction:
        raise StudioParseError("技能正文是空的：`---` 之后要有作业指令。")

    raw_name = str(meta.get("name") or meta.get("id") or "").strip()

    if not raw_name:
        raw_name = _strip_suffix(Path(filename or "").name)
        if meta:
            notes.append(f"front matter 里没有 name，用文件名「{raw_name}」作为技能 ID。")

    try:
        name = skill_service.normalize_name(raw_name)
    except skill_service.SkillError as exc:
        raise StudioParseError(
            f"技能 ID「{raw_name}」不合规（{exc}）。在 front matter 里写 "
            "`name: 小写-英文-标识` 即可。"
        ) from exc

    title = str(meta.get("display_name") or meta.get("title") or "").strip()
    if not title:
        heading = _H1_RE.search(instruction)
        title = heading.group(1).strip() if heading else name

    description = str(meta.get("description") or meta.get("summary") or "").strip()
    if not description:
        description = _first_paragraph(instruction)
        if description:
            notes.append("描述取正文第一段，可在 front matter 的 description 里写得更准。")

    if len(description) > skill_service.MAX_DESCRIPTION_CHARS:
        description = description[: skill_service.MAX_DESCRIPTION_CHARS]
        notes.append(f"描述超过 {skill_service.MAX_DESCRIPTION_CHARS} 字，已截断。")

    keywords = _as_list(
        meta.get("trigger_keywords") or meta.get("keywords") or meta.get("trigger")
    )
    if len(keywords) > skill_service.MAX_KEYWORDS:
        notes.append(f"触发词多于 {skill_service.MAX_KEYWORDS} 个，多余的已丢弃。")
        keywords = keywords[: skill_service.MAX_KEYWORDS]

    tools_raw = meta.get("allowed_tools", meta.get("tools"))
    allowed_tools = _as_list(tools_raw) if tools_raw is not None else None

    raw_priority = meta.get("priority", 100)
    try:
        priority = int(raw_priority)
    except (TypeError, ValueError):
        priority = 100
        notes.append(f"priority「{raw_priority}」不是整数，按 100 处理。")

    enabled = meta.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in {"false", "no", "0", "off"}

    instruction, clipped = skill_service.clip_instruction(instruction)
    if clipped:
        notes.append(
            f"正文超过 {skill_service.MAX_INSTRUCTION_CHARS} 字，已截断——"
            "超出部分不会注入，需要拆分技能。"
        )

    return SkillDraft(
        name=name,
        display_name=title[:64],
        description=description,
        instruction=instruction,
        trigger_keywords=keywords,
        allowed_tools=allowed_tools,
        priority=priority,
        enabled=bool(enabled),
        notes=notes,
    )


def parse_skill_export(payload: Any) -> SkillBundle:
    """平台自己导出的 `{"skills": [...]}`。"""
    entries = payload.get("skills") if isinstance(payload, dict) else payload

    if not isinstance(entries, list):
        raise StudioParseError("技能导出文件应当形如 {\"skills\": [ ... ]}。")

    kept = [item for item in entries if isinstance(item, dict)]

    if not kept:
        raise StudioParseError("文件里没有技能条目。")

    notes = []
    if len(kept) != len(entries):
        notes.append(f"跳过了 {len(entries) - len(kept)} 条不是对象的条目。")

    return SkillBundle(entries=kept, notes=notes)


# --------------------------------------------------------------------------- #
# 工具：OpenAPI 3 / Swagger 2
# --------------------------------------------------------------------------- #


def _resolve_ref(node: Any, root: Any, depth: int = 0) -> Any:
    """只解析文档内部的 `#/...` 引用。

    外部文件引用（`other.yaml#/...`）要读第二个文件，而这里只有一个上传流；
    解析不到就返回空字典，让调用方把「参数丢了」记进 notes——
    静默当成没有参数是最坏的一种处理。
    """
    if depth > 8 or not isinstance(node, dict) or "$ref" not in node:
        return node

    ref = node.get("$ref")

    if not isinstance(ref, str) or not ref.startswith("#/"):
        return {}

    current: Any = root

    for part in ref[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or key not in current:
            return {}
        current = current[key]

    resolved = _resolve_ref(current, root, depth + 1)

    if isinstance(resolved, dict):
        # `$ref` 旁边的兄弟键（常见于 description 覆盖）合法，合并进去。
        merged = {**resolved, **{k: v for k, v in node.items() if k != "$ref"}}
        return merged

    return resolved


def _sanitize_tool_name(raw: str, fallback: str) -> str:
    value = re.sub(r"[^a-z0-9_]+", "_", (raw or "").lower()).strip("_")

    if not value:
        value = re.sub(r"[^a-z0-9_]+", "_", (fallback or "").lower()).strip("_")

    if not value or not value[0].isalpha():
        value = "op_" + value

    # 工具名要求至少两个字符（`^[a-z][a-z0-9_]{1,63}$`）：单字母名字
    # 在库里存不进去，与其等校验报错，不如在这里补成合法标识。
    if len(value) < 2:
        value = (value + "_op")[:60]

    return value[:60]


def _join_url(base: str, path: str) -> str:
    if not base:
        return path
    if base.endswith("/") and path.startswith("/"):
        return base[:-1] + path
    if base.endswith("/") or path.startswith("/"):
        return base + path
    return base + "/" + path


def _base_url(spec: dict[str, Any]) -> tuple[str, bool]:
    """返回 (基地址, 是否绝对地址)。"""
    servers = spec.get("servers")

    if isinstance(servers, list):
        for item in servers:
            url = _resolve_ref(item, spec).get("url") if isinstance(item, dict) else None
            if isinstance(url, str) and url.strip():
                # 相对地址（`/v1`）要靠调用方补全，否则拼出来的是不可调用的 URL。
                return url.strip(), _looks_absolute(url.strip())

    if str(spec.get("swagger", "")).startswith("2"):
        host = str(spec.get("host") or "").strip()
        if host:
            schemes = spec.get("schemes")
            scheme = schemes[0] if isinstance(schemes, list) and schemes else "https"
            base_path = str(spec.get("basePath") or "").strip()
            url = f"{scheme}://{host}{base_path}"
            return url, True

    return "", False


def _param_from_spec(raw: Any, root: Any, location: str, notes: list[str]) -> dict[str, Any] | None:
    param = _resolve_ref(raw, root)

    if not isinstance(param, dict):
        return None

    name = str(param.get("name") or "").strip()

    if not name:
        return None

    if not _PARAM_NAME_RE.match(name):
        notes.append(
            f"参数名 {name} 不是合法标识符（只允许字母、数字、下划线，且不能以数字开头），"
            "已跳过。改名会改变请求形状，需要手工建工具。"
        )
        return None

    if location == "header" and name.lower() in SKIP_PARAM_HEADERS:
        notes.append(f"请求头 {name} 由客户端或凭据管理，未转成参数。")
        return None

    # 路径参数只能出现在路径里，平台不校验名字是否与 `{name}` 同名，
    # 但调用时会替换失败——所以这里把不一致的情况说出来。
    schema = _resolve_ref(param.get("schema") or {}, root)
    if not isinstance(schema, dict):
        schema = {}

    declared = str(schema.get("type") or param.get("type") or "string")
    mapped = _TYPE_MAP.get(declared)

    if mapped is None:
        mapped = "string"
        notes.append(f"参数 {name} 是 {declared} 类型，平台只支持标量，已按 string 处理。")

    enum_values = schema.get("enum") or param.get("enum") or []
    enum_out: list[str] = []

    if isinstance(enum_values, list):
        enum_out = [str(item) for item in enum_values if isinstance(item, (str, int, float, bool))][:32]

    required = bool(param.get("required") or location == "path")

    return {
        "name": name,
        "location": location,
        "type": mapped,
        "required": required,
        "description": str(param.get("description") or "")[:512],
        "enum": enum_out,
    }


def _body_params(operation: dict[str, Any], root: Any, notes: list[str], label: str) -> list[dict[str, Any]]:
    body = _resolve_ref(operation.get("requestBody") or {}, root)

    if not isinstance(body, dict):
        return []

    content = body.get("content")

    if not isinstance(content, dict) or not content:
        # Swagger 2 用 parameters 里 in=body 表达请求体，走另一条路径。
        return []

    media = content.get("application/json") or content.get("application/*+json")

    if not isinstance(media, dict):
        kinds = "、".join(sorted(content)) or "（空）"
        notes.append(f"{label} 的请求体是 {kinds}，平台只发 JSON，未转成参数。")
        return []

    schema = _resolve_ref(media.get("schema") or {}, root)

    if not isinstance(schema, dict):
        return []

    properties = schema.get("properties")

    if not isinstance(properties, dict) or not properties:
        notes.append(f"{label} 的请求体是自由结构（没有 properties），未转成参数。")
        return []

    required_names = set(schema.get("required") or [])
    out: list[dict[str, Any]] = []

    for prop_name, prop_schema in properties.items():
        if not _PARAM_NAME_RE.match(str(prop_name)):
            notes.append(
                f"请求体字段 {prop_name} 不是合法标识符，已跳过（会改变请求形状，需手工建工具）。"
            )
            continue

        resolved = _resolve_ref(prop_schema or {}, root)
        if not isinstance(resolved, dict):
            resolved = {}

        declared = str(resolved.get("type") or "string")
        mapped = _TYPE_MAP.get(declared)

        if mapped is None:
            mapped = "string"
            notes.append(f"请求体字段 {prop_name} 是 {declared} 类型，已按 string 处理。")

        out.append(
            {
                "name": str(prop_name),
                "location": "body",
                "type": mapped,
                "required": prop_name in required_names,
                "description": str(resolved.get("description") or "")[:512],
                "enum": [str(x) for x in (resolved.get("enum") or []) if isinstance(x, (str, int, float, bool))][:32],
            }
        )

    return out


def _swagger2_body_params(operation: dict[str, Any], root: Any, notes: list[str], label: str) -> list[dict[str, Any]]:
    for raw in operation.get("parameters") or []:
        param = _resolve_ref(raw, root)
        if isinstance(param, dict) and param.get("in") == "body":
            schema = _resolve_ref(param.get("schema") or {}, root)
            if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
                notes.append(f"{label} 的 body 参数没有展开（schema 里没有 properties）。")
                return []
            fake = {"requestBody": {"content": {"application/json": {"schema": schema}}}}
            return _body_params(fake, root, notes, label)
    return []


def _operation_params(
    operation: dict[str, Any],
    path_params: Any,
    root: Any,
    notes: list[str],
    label: str,
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}

    for raw in list(path_params or []) + list(operation.get("parameters") or []):
        param = _resolve_ref(raw, root)
        if not isinstance(param, dict):
            continue

        location = str(param.get("in") or "query").lower()

        if location == "cookie":
            notes.append(f"{label} 的 cookie 参数 {param.get('name')} 平台不支持，已跳过。")
            continue

        if location not in {"path", "query", "header", "body"}:
            notes.append(f"{label} 的参数位置 {location} 平台不支持，已跳过。")
            continue

        item = _param_from_spec(param, root, location, notes)

        if item is None:
            continue

        # operation 级同名参数覆盖 path 级：这是 OpenAPI 的规定。
        merged[(item["name"], location)] = item

    body = _body_params(operation, root, notes, label)

    if not body:
        body = _swagger2_body_params(operation, root, notes, label)

    for item in body:
        merged[(item["name"], "body")] = item

    return list(merged.values())


def parse_openapi(
    filename: str,
    text: str,
    *,
    base_url: str | None = None,
) -> tuple[list[ToolDraft], list[str], str]:
    """把 OpenAPI 3 / Swagger 2 拆成一组工具定义。

    返回 (工具列表, 全局 notes, 实际使用的基地址)。

    ``base_url`` 是给「规格里写的是相对地址」那条路留的出口：规格只写了
    `/v1`，拼出来的工具 URL 不可调用，与其拦下来不如让人在导入时补一次。
    """
    try:
        spec = json.loads(text)
    except json.JSONDecodeError:
        try:
            spec = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise StudioParseError(
                f"既不是 JSON 也不是 YAML（{exc.__class__.__name__}）。"
            ) from exc

    if not isinstance(spec, dict):
        raise StudioParseError("OpenAPI 规格的顶层必须是对象。")

    if not (str(spec.get("openapi", "")).startswith("3") or str(spec.get("swagger", "")).startswith("2")):
        raise StudioParseError(
            "不是 OpenAPI 3 或 Swagger 2 文档：缺少 openapi/swagger 版本字段。"
        )

    paths = spec.get("paths")

    if not isinstance(paths, dict) or not paths:
        raise StudioParseError("规格里没有 paths，拆不出任何接口。")

    detected_base, absolute = _base_url(spec)

    if base_url is not None and base_url.strip():
        used_base = base_url.strip().rstrip("/")
    else:
        used_base = detected_base

    notes: list[str] = []

    if used_base and not _looks_absolute(used_base):
        raise StudioParseError(
            f"服务地址「{used_base}」不是绝对地址，工具存下来也调不通。"
            "请在导入时填一个完整地址（https://host/…）。"
        )

    if not used_base:
        notes.append("规格里没有 servers[0].url，工具 URL 只有相对路径——需要补服务地址。")

    drafts: list[ToolDraft] = []
    seen: dict[str, int] = {}

    for path, path_item in paths.items():
        item = _resolve_ref(path_item, spec)

        if not isinstance(item, dict):
            continue

        shared_params = item.get("parameters")

        for method in _HTTP_METHODS:
            operation = item.get(method)

            if not isinstance(operation, dict):
                continue

            label = f"{method.upper()} {path}"
            raw_id = str(operation.get("operationId") or "")
            name = _sanitize_tool_name(raw_id, f"{method}_{path}")

            if name in seen:
                seen[name] += 1
                name = f"{name[:58]}_{seen[name]}"
            else:
                seen[name] = 1

            summary = str(operation.get("summary") or "").strip()
            description = str(operation.get("description") or summary or "").strip()
            display = summary or str(operation.get("operationId") or label)

            url = _join_url(used_base, str(path))

            if len(url) > MAX_TOOL_URL_CHARS:
                notes.append(f"{label} 的 URL 过长（{len(url)} 字符），已跳过。")
                continue

            op_notes: list[str] = []
            params = _operation_params(operation, shared_params, spec, op_notes, label)

            if len(params) > MAX_TOOL_PARAMS:
                op_notes.append(
                    f"参数 {len(params)} 个，超过平台上限 {MAX_TOOL_PARAMS}，多余的已丢弃。"
                    "建议把接口拆细，或改用查询对象。"
                )
                params = params[:MAX_TOOL_PARAMS]

            # 路径参数没在 url 里出现，调用时替换不上。
            for param in params:
                if param["location"] == "path" and "{" + param["name"] + "}" not in url:
                    op_notes.append(
                        f"路径参数 {param['name']} 在 URL 里没有对应的 {{占位符}}，调用会失败。"
                    )

            risk = {
                "get": "read",
                "delete": "destructive",
            }.get(method, "write")

            drafts.append(
                ToolDraft(
                    name=name,
                    display_name=display[:128] or name,
                    description=description[:MAX_TOOL_DESC_CHARS],
                    method=method.upper(),
                    url=url,
                    risk_level=risk,
                    params=params,
                    notes=op_notes,
                    deprecated=bool(operation.get("deprecated")),
                )
            )

    if not drafts:
        raise StudioParseError("规格里的 paths 没有可支持的 GET/POST/PUT/PATCH/DELETE 操作。")

    return drafts, notes, used_base


def parse_tool_export(payload: Any) -> ToolBundle:
    entries = payload.get("tools") if isinstance(payload, dict) else payload

    if not isinstance(entries, list):
        raise StudioParseError("工具导出文件应当形如 {\"tools\": [ ... ]}。")

    kept = [item for item in entries if isinstance(item, dict)]

    if not kept:
        raise StudioParseError("文件里没有工具条目。")

    notes = []
    if len(kept) != len(entries):
        notes.append(f"跳过了 {len(entries) - len(kept)} 条不是对象的条目。")

    return ToolBundle(entries=kept, notes=notes)


# --------------------------------------------------------------------------- #
# 识别
# --------------------------------------------------------------------------- #


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="ignore")


def _structured(raw: bytes) -> Any:
    text = _decode(raw)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def load_structured(raw: bytes) -> Any:
    """把 `.json/.yaml` 读成对象；读不出来返回 None。

    对外只暴露这一个入口：`detect_target` 与导入编排都要读同一份内容，
    各自实现一遍「先试 JSON 再试 YAML」会在某一边漏掉 BOM 或注释。
    """
    return _structured(raw)


def looks_like_skill_markdown(text: str) -> bool:
    match = _FRONT_MATTER_RE.match(text)

    if not match:
        return False

    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return False

    if not isinstance(meta, dict):
        return False

    # 只要写到 name 就算技能：知识库文档不会在开头写 YAML。
    return any(key in meta for key in ("name", "display_name", "trigger_keywords", "allowed_tools"))


def detect_target(filename: str, raw: bytes) -> str:
    """只按内容判断，判不出来就抛错，不猜。"""
    suffix = Path(filename or "").suffix.lower()

    if suffix in STRUCTURED_SUFFIXES:
        payload = _structured(raw)

        if isinstance(payload, dict):
            if str(payload.get("openapi", "")).startswith("3") or str(payload.get("swagger", "")).startswith("2"):
                return TARGET_TOOL
            if isinstance(payload.get("skills"), list):
                return TARGET_SKILL
            if isinstance(payload.get("tools"), list) or (
                "method" in payload and "url" in payload
            ):
                return TARGET_TOOL

        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            first = payload[0]
            if "method" in first and "url" in first:
                return TARGET_TOOL
            if "instruction" in first:
                return TARGET_SKILL

        raise StudioParseError(
            "这份 JSON/YAML 里没有可识别的结构：既不是 OpenAPI 规格，"
            "也不是技能或工具的导出文件。"
        )

    if suffix in SUPPORTED_SUFFIXES:
        if suffix in {".md", ".markdown"} and looks_like_skill_markdown(_decode(raw)):
            return TARGET_SKILL
        return TARGET_KNOWLEDGE

    raise StudioParseError(
        f"不支持的文件类型：{suffix or '(无扩展名)'}。"
        f"知识库文档用 {', '.join(sorted(SUPPORTED_SUFFIXES))}；"
        "技能用 .md；工具用 .json/.yaml（OpenAPI 3 或 Swagger 2）。"
    )
