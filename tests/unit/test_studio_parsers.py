# tests/unit/test_studio_parsers.py
"""Studio 导入的解析层：文件字节 → 草稿。

这层的每个用例都对应一个「导进去才发现错了」的现实场景：
front matter 写错、参数名不是标识符、服务地址是相对路径。
共同点是——**如果这里静默丢弃，错要到工具调用时才暴露**。
"""

from __future__ import annotations

import json

import pytest

from app.studio import parsers
from app.studio.parsers import (
    TARGET_KNOWLEDGE,
    TARGET_SKILL,
    TARGET_TOOL,
    StudioParseError,
    detect_target,
    parse_openapi,
    parse_skill_markdown,
)

SKILL_MD = """---
name: patrol-compare
display_name: 巡检比对
description: 比对附件与工单内容
trigger_keywords: [巡检, 比对]
allowed_tools:
  - knowledge_search
priority: 20
---

# 巡检比对

1. 读取附件
2. 查工单
"""


# --------------------------------------------------------------------------- #
# 技能：Markdown
# --------------------------------------------------------------------------- #


def test_front_matter_drives_every_field():
    draft = parse_skill_markdown("whatever.md", SKILL_MD)

    assert draft.name == "patrol-compare"
    assert draft.display_name == "巡检比对"
    assert draft.description == "比对附件与工单内容"
    assert draft.trigger_keywords == ["巡检", "比对"]
    assert draft.allowed_tools == ["knowledge_search"]
    assert draft.priority == 20
    # 正文不含 front matter 本身，否则指令里会混进一段 YAML。
    assert draft.instruction.startswith("# 巡检比对")
    assert "trigger_keywords" not in draft.instruction


def test_missing_front_matter_falls_back_to_filename():
    draft = parse_skill_markdown("valve-check.md", "阀门检查前先关上游阀。")

    assert draft.name == "valve-check"
    assert draft.description == "阀门检查前先关上游阀。"
    # 没有触发词就只能靠 /名字 显式调用，这一点必须说出来。
    assert any("front matter" in note for note in draft.notes)


def test_inline_keyword_string_is_split():
    """`trigger_keywords: 巡检, 比对` 是常见手写形态，不该只得到一个词。"""
    draft = parse_skill_markdown(
        "a.md", "---\nname: kv-split\ntrigger_keywords: 巡检, 比对、复核\n---\n正文"
    )

    assert draft.trigger_keywords == ["巡检", "比对", "复核"]


def test_broken_front_matter_is_reported_not_ignored():
    with pytest.raises(StudioParseError, match="YAML"):
        parse_skill_markdown("a.md", "---\nname: [未闭合\n---\n正文")


def test_empty_body_is_rejected():
    """只有元数据、没有指令的技能建出来是空的，不如当场拒绝。"""
    with pytest.raises(StudioParseError, match="正文是空的"):
        parse_skill_markdown("a.md", "---\nname: aa\n---\n\n   \n")


def test_illegal_name_points_at_the_fix():
    with pytest.raises(StudioParseError, match="front matter"):
        parse_skill_markdown("a.md", "---\nname: 巡检比对\n---\n正文")


def test_too_many_keywords_are_capped_with_a_note():
    from app.services.skill_service import MAX_KEYWORDS

    words = "、".join(f"词{i}" for i in range(MAX_KEYWORDS + 5))
    draft = parse_skill_markdown(
        "a.md", f"---\nname: kwy\ntrigger_keywords: {words}\n---\n正文"
    )

    assert len(draft.trigger_keywords) == MAX_KEYWORDS
    assert any("触发词" in note for note in draft.notes)


def test_bom_before_front_matter_is_tolerated():
    """Windows 记事本存出来的文件带 BOM，front matter 会因此失效。"""
    draft = parse_skill_markdown("a.md", "\ufeff---\nname: bom-skill\n---\n正文")

    assert draft.name == "bom-skill"


# --------------------------------------------------------------------------- #
# 工具：OpenAPI
# --------------------------------------------------------------------------- #

OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "站点服务", "version": "1"},
    "servers": [{"url": "https://api.example.com/v1"}],
    "paths": {
        "/stations/{station_id}": {
            "get": {
                "operationId": "getStation",
                "summary": "查站点",
                "parameters": [
                    {
                        "name": "station_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {"name": "X-Trace", "in": "header", "schema": {"type": "string"}},
                    {"name": "Authorization", "in": "header", "schema": {"type": "string"}},
                ],
            },
            "delete": {"operationId": "deleteStation", "summary": "删站点"},
        },
        "/stations": {
            "post": {
                "operationId": "createStation",
                "summary": "建站点",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/Station"}
                        }
                    }
                },
            }
        },
    },
    "components": {
        "schemas": {
            "Station": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string", "description": "站名"},
                    "capacity": {"type": "integer"},
                },
            }
        }
    },
}


def _spec(**overrides) -> str:
    payload = json.loads(json.dumps(OPENAPI))
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def test_each_operation_becomes_one_tool():
    drafts, _notes, base = parse_openapi("api.json", _spec())

    assert base == "https://api.example.com/v1"
    assert {draft.name for draft in drafts} == {
        "getstation",
        "deletestation",
        "createstation",
    }


def test_risk_level_follows_the_method():
    drafts, _notes, _base = parse_openapi("api.json", _spec())
    risk = {draft.name: draft.risk_level for draft in drafts}

    assert risk["getstation"] == "read"
    assert risk["createstation"] == "write"
    # DELETE 走的是「不可逆」那一档，审批策略据此更严。
    assert risk["deletestation"] == "destructive"


def test_path_param_is_required_and_joined_into_url():
    drafts, _notes, _base = parse_openapi("api.json", _spec())
    tool = next(draft for draft in drafts if draft.name == "getstation")

    assert tool.url == "https://api.example.com/v1/stations/{station_id}"
    assert tool.params[0]["location"] == "path"
    assert tool.params[0]["required"] is True
    # 路径参数即使规格里漏写 required 也必须是必填的，否则 URL 替换不上。
    assert "{station_id}" in tool.url


def test_credential_headers_never_become_params():
    """Authorization 若能当参数填，凭据就会写进工具定义表。"""
    drafts, _notes, _base = parse_openapi("api.json", _spec())
    tool = next(draft for draft in drafts if draft.name == "getstation")
    names = {param["name"] for param in tool.params}

    assert "Authorization" not in names
    assert "X-Trace" not in names


def test_ref_resolved_body_becomes_params():
    drafts, _notes, _base = parse_openapi("api.json", _spec())
    tool = next(draft for draft in drafts if draft.name == "createstation")
    params = {param["name"]: param for param in tool.params}

    assert params["name"]["location"] == "body"
    assert params["name"]["required"] is True
    assert params["capacity"]["type"] == "integer"
    assert params["capacity"]["required"] is False


def test_relative_server_url_is_rejected_with_actionable_message():
    """`/v1` 拼出来的工具 URL 调不通，存进库里等于埋一个必失败的接口。"""
    spec = _spec(servers=[{"url": "/v1"}])

    with pytest.raises(StudioParseError, match="绝对地址"):
        parse_openapi("api.json", spec)


def test_relative_server_url_can_be_overridden_by_caller():
    drafts, _notes, base = parse_openapi(
        "api.json", _spec(servers=[{"url": "/v1"}]), base_url="https://real.example.com"
    )

    assert base == "https://real.example.com"
    assert drafts[0].url.startswith("https://real.example.com/")


def test_duplicate_operation_ids_get_distinct_names():
    spec = json.loads(_spec())
    spec["paths"]["/other"] = {"get": {"operationId": "getStation"}}

    drafts, _notes, _base = parse_openapi("api.json", json.dumps(spec))
    names = [draft.name for draft in drafts]

    assert len(names) == len(set(names))
    assert "getstation_2" in names


def test_non_scalar_param_falls_back_to_string_with_a_note():
    spec = json.loads(_spec())
    spec["paths"]["/stations"]["post"]["parameters"] = [
        {"name": "tags", "in": "query", "schema": {"type": "array"}}
    ]

    drafts, _notes, _base = parse_openapi("api.json", json.dumps(spec))
    tool = next(draft for draft in drafts if draft.name == "createstation")
    tags = next(param for param in tool.params if param["name"] == "tags")

    assert tags["type"] == "string"
    assert any("数组" in note or "array" in note for note in tool.notes)


def test_swagger2_host_becomes_base_url():
    spec = {
        "swagger": "2.0",
        "info": {"title": "旧服务", "version": "1"},
        "host": "legacy.example.com",
        "basePath": "/api",
        "schemes": ["https"],
        "paths": {"/ping": {"get": {"operationId": "ping"}}},
    }

    drafts, _notes, base = parse_openapi("swagger.json", json.dumps(spec))

    assert base == "https://legacy.example.com/api"
    assert drafts[0].url == "https://legacy.example.com/api/ping"


def test_spec_without_operations_is_rejected():
    spec = {"openapi": "3.0.0", "info": {}, "paths": {}}

    with pytest.raises(StudioParseError, match="paths"):
        parse_openapi("api.json", json.dumps(spec))


def test_not_an_openapi_document_is_rejected():
    with pytest.raises(StudioParseError, match="OpenAPI"):
        parse_openapi("api.json", json.dumps({"hello": "world"}))


# --------------------------------------------------------------------------- #
# 识别
# --------------------------------------------------------------------------- #


def test_markdown_with_front_matter_is_a_skill():
    assert detect_target("x.md", SKILL_MD.encode()) == TARGET_SKILL


def test_plain_markdown_is_knowledge():
    """现场规程也是 .md，不能因为后缀就把它们全建成技能。"""
    body = "# 调压箱操作规程\n\n1. 检查进出口阀门\n".encode()

    assert detect_target("规程.md", body) == TARGET_KNOWLEDGE


def test_markdown_front_matter_without_known_keys_is_knowledge():
    body = "---\ntitle: xxx\n---\n\n# 正文\n".encode()

    assert detect_target("doc.md", body) == TARGET_KNOWLEDGE


def test_openapi_json_is_a_tool():
    assert detect_target("api.json", _spec().encode()) == TARGET_TOOL


def test_tool_export_json_is_a_tool():
    body = json.dumps({"tools": [{"name": "ping", "method": "GET", "url": "https://a"}]})

    assert detect_target("tools.json", body.encode()) == TARGET_TOOL


def test_skill_export_json_is_a_skill():
    body = json.dumps({"skills": [{"name": "a", "instruction": "做事"}]})

    assert detect_target("skills.json", body.encode()) == TARGET_SKILL


def test_unknown_json_is_rejected_rather_than_guessed():
    with pytest.raises(StudioParseError, match="没有可识别的结构"):
        detect_target("config.json", b'{"debug": true}')


def test_unsupported_suffix_lists_what_is_accepted():
    with pytest.raises(StudioParseError, match="不支持的文件类型"):
        detect_target("a.exe", b"MZ")


def test_load_structured_accepts_yaml():
    """`.yaml` 的 OpenAPI 很常见，不能只认 JSON。"""
    payload = parsers.load_structured(b"openapi: '3.0.0'\npaths: {}\n")

    assert payload["openapi"] == "3.0.0"
