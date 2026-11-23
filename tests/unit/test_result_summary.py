# tests/unit/test_result_summary.py
"""工具结果怎么呈现给用户。

读工具这条路径不走模型：`execute_read_tool` 把 `_summarize_result` 的结果
直接当最终答复返回。所以这里输出的每一个字用户都会看到——
整段 JSON 就是让他自己解析报文。
"""

from __future__ import annotations

from app.agents.nodes import _MAX_LISTED_ITEMS, _summarize_result
from app.tools.types import ToolExecutionResult


def _result(**kwargs: object) -> ToolExecutionResult:
    defaults: dict[str, object] = {
        "success": True,
        "verified": True,
        "result": {},
        "message": "已完成远端调用。",
    }
    defaults.update(kwargs)
    return ToolExecutionResult(**defaults)  # type: ignore[arg-type]


def test_metadata_is_not_shown_to_user() -> None:
    """success / data_source / timestamp 是调用元数据，不是结论。"""
    summary = _summarize_result(
        _result(
            message="当前身份看不到任何站点。",
            result={
                "success": True,
                "data_source": "API",
                "timestamp": "2026-11-03T06:48:40",
                "query": None,
                "scope": "很长的一段说明",
                "stations": [],
            },
        )
    )

    assert summary == "当前身份看不到任何站点。"


def test_empty_result_does_not_repeat_counts() -> None:
    """
    空白名单的实测返回：message 已经说了「共 10 个授权、0 个可见」，
    再列 count / total_authorized 只是噪音。
    """
    summary = _summarize_result(
        _result(
            message="当前身份看不到任何站点：平台账号下共 10 个站点，但白名单为空。",
            result={
                "success": True,
                "count": 0,
                "total_authorized": 10,
                "visible_to_identity": 0,
                "identity_station_filter": [],
                "stations": [],
            },
        )
    )

    assert "count" not in summary
    assert "total_authorized" not in summary
    assert "identity_station_filter" not in summary


def test_collection_is_rendered_as_items_not_json() -> None:
    summary = _summarize_result(
        _result(
            message="已完成远端调用。",
            result={
                "count": 2,
                "total_authorized": 2,
                "visible_to_identity": 2,
                "identity_station_filter": "ALL",
                "stations": [
                    {"name": "硬件测试", "serial": "T002511111"},
                    {"name": "深圳燃气大厦", "serial": "T002509008"},
                ],
            },
        )
    )

    assert "1. 硬件测试（T002511111）" in summary
    assert "2. 深圳燃气大厦（T002509008）" in summary
    assert "{" not in summary
    # 权限元数据不占版面：条数已经用自然语言说了。
    assert "identity_station_filter" not in summary
    assert "visible_to_identity" not in summary


def test_generic_message_is_replaced_by_count() -> None:
    """「已完成远端调用」没回答「有多少个站」，条数才是他要的。"""
    summary = _summarize_result(
        _result(
            message="已完成远端调用。",
            result={"stations": [{"name": "A", "serial": "S1"}]},
        )
    )

    assert summary.startswith("共 1 个站点。")
    assert "已完成远端调用" not in summary


def test_identifier_already_in_title_is_not_repeated() -> None:
    """远端站名自带编号，再括注一次就成「硬件测试(T002511111)（T002511111）」。"""
    summary = _summarize_result(
        _result(
            message="已完成远端调用。",
            result={"stations": [{"name": "硬件测试(T002511111)", "serial": "T002511111"}]},
        )
    )

    assert "1. 硬件测试(T002511111)" in summary
    assert "（T002511111）" not in summary


def test_long_list_is_capped() -> None:
    stations = [{"name": f"站{i}", "serial": f"S{i}"} for i in range(_MAX_LISTED_ITEMS + 5)]

    summary = _summarize_result(_result(result={"stations": stations}))

    assert "共 25 个站点。" in summary
    assert "另有 5 条未列出" in summary
    assert f"{_MAX_LISTED_ITEMS + 1}." not in summary


def test_scalar_values_are_kept() -> None:
    """压力、阀位这类标量是查询的直接答案，不能被过滤掉。"""
    summary = _summarize_result(
        _result(result={"outlet_pressure": 2300, "empty_field": None, "unit": "Pa"})
    )

    assert "outlet_pressure：2300" in summary
    assert "unit：Pa" in summary
    assert "empty_field" not in summary
    assert "success" not in summary


def test_measurement_is_rendered_as_one_line() -> None:
    """
    远端测点统一是 {name, value, unit, status_text}。
    整段 JSON 化后用户得自己找 value 在哪、单位是什么。
    """
    summary = _summarize_result(
        _result(
            result={
                "station": {"station_name": "深圳燃气大厦(T002509008)", "serial": "T002509008"},
                "inlet": {
                    "name": "进口压力",
                    "value": 1.538,
                    "unit": "kPa",
                    "status_text": "正常",
                    "limit_up": 500.0,
                },
            }
        )
    )

    assert "深圳燃气大厦(T002509008)" in summary
    assert "进口压力：1.538 kPa（正常）" in summary
    assert '"value"' not in summary


def test_flat_struct_is_rendered_inline() -> None:
    """时间窗这类扁平对象一行说完，比整段 JSON 好读。"""
    summary = _summarize_result(_result(result={"window": {"a": 1, "b": 2}}))

    assert "window：a=1，b=2" in summary


def test_deep_struct_says_it_is_not_expanded() -> None:
    """
    真正嵌套的对象不展开。

    之前是 JSON 截断，用户会看到 `"csv_path": nul…` 这种半截报文——
    既读不懂，也不知道是数据就这样还是被截了。
    """
    summary = _summarize_result(
        _result(result={"artifact": {"dir": "/app/trends", "meta": {"files": ["a.csv"]}}})
    )

    assert "artifact" in summary
    assert "共 2 项明细" in summary
    assert "{" not in summary


def test_series_reports_points_and_range() -> None:
    """趋势查询的答案就是「多少点、范围多少」。"""
    summary = _summarize_result(
        _result(
            message="趋势曲线已取回。",
            result={
                "series": [
                    {"name": "进口压力(kPa)", "access": "CH1", "points": 0, "min": None},
                    {"name": "出口压力(kPa)", "access": "CH5", "points": 12, "min": 1.5, "max": 2.3},
                ]
            },
        )
    )

    assert "1. 进口压力(kPa)：0 个点（该时间段没有采样数据）" in summary
    assert "2. 出口压力(kPa)：12 个点，范围 1.5~2.3" in summary


def test_scalar_list_is_one_line() -> None:
    summary = _summarize_result(_result(result={"requested_accesses": ["CH1", "CH5", "CH2"]}))

    assert "requested_accesses：CH1、CH5、CH2" in summary


def test_long_scalar_list_reports_total() -> None:
    summary = _summarize_result(_result(result={"tags": [f"t{i}" for i in range(12)]}))

    assert "共 12 项" in summary
    assert "t8" not in summary


def test_notes_are_listed_one_per_line() -> None:
    """说明性字段一条一句，并成一行会读成一句长句。"""
    summary = _summarize_result(
        _result(result={"notes": ["点数不代表连续性", "上下限为 0 不可作判据"]})
    )

    assert "notes：\n  · 点数不代表连续性\n  · 上下限为 0 不可作判据" in summary


def test_message_is_kept_verbatim() -> None:
    """message 里的业务结论是最重要的一句，必须原样出现在开头。"""
    summary = _summarize_result(
        _result(message="控制预演未通过，未下发任何指令。", result={"blockers": ["x"]})
    )

    assert summary.startswith("控制预演未通过，未下发任何指令。")


def test_failure_only_carries_message() -> None:
    summary = _summarize_result(
        _result(success=False, verified=False, message="MCP 服务不可达。", result={"raw": 1})
    )

    assert summary == "MCP 服务不可达。"


def test_unverified_result_is_flagged() -> None:
    summary = _summarize_result(_result(verified=False, result={"count": 1}))

    assert "尚未通过回读核验" in summary


def test_empty_query_on_station_list_is_not_reported_as_no_permission() -> None:
    """count=0 但 visible_to_identity=10：是查询没匹配，不是没有授权。

    2026-11-05：用户拿现场记录表里的「梅石小区」查站点，返回 count=0，
    模型回答「当前身份看不到任何已授权站点」——同一份结果里写着可见 10 个，
    用户于是以为授权出了问题。
    """
    summary = _summarize_result(
        _result(
            message="已查询站点。",
            result={
                "success": True,
                "count": 0,
                "total_authorized": 10,
                "visible_to_identity": 10,
                "stations": [],
            },
        )
    )

    assert "可见 10 个站点" in summary
    assert "不是没有站点授权" in summary


def test_no_note_when_station_list_is_really_empty() -> None:
    """真一个站都没有时不补这句，否则是另一种误导。"""
    summary = _summarize_result(
        _result(
            message="已查询站点。",
            result={
                "success": True,
                "count": 0,
                "visible_to_identity": 0,
                "stations": [],
            },
        )
    )

    assert "不是没有站点授权" not in summary


def test_query_object_and_count_are_not_rendered_as_answers() -> None:
    """`resource` / `total` 是入参回显与计数，不是结果。

    线上真出现过答案后面跟着两行「resource：tool」「total：99」——
    读者只会以为报文被截断了，而真正的内容就在上面那句里。
    """
    summary = _summarize_result(
        _result(
            result={"resource": "tool", "total": 99},
            message="当前可调用 99 个工具：builtin 24 个、mcp 75 个。",
        )
    )

    assert summary == "当前可调用 99 个工具：builtin 24 个、mcp 75 个。"


def test_approval_card_shows_what_will_happen_before_the_ask() -> None:
    """确认卡正文：摘要排在「尚未执行」之前。

    线上出现过「新建流程」的卡片只有一行工具名 + 一份折叠 JSON，用户直接点了
    确认——他批的是自己没看过的东西。把步骤与角色写进正文，才谈得上核对。
    """
    from app.agents.nodes import _approval_message

    text = _approval_message(
        "待确认操作：新建或修改编排流程",
        "步骤：check（field-ops） → advise（field-ops） → report（汇总）",
    )

    assert text.index("check（field-ops）") < text.index("尚未执行")


def test_approval_card_reads_normally_without_a_summary() -> None:
    """没有摘要的工具不该多出空行——卡片还是原来那两句。"""
    from app.agents.nodes import _approval_message

    assert _approval_message("待确认操作：删除流程", "") == (
        "待确认操作：删除流程\n\n该操作尚未执行。请确认或取消本次操作。"
    )


def test_result_graph_is_taken_from_the_tool_result() -> None:
    """结果气泡里那张图：工具把图放在 `result["graph"]`，节点侧只认这一个键。"""
    from app.agents.nodes import _result_graph
    from app.tools.types import ToolExecutionResult

    payload = {"graph": {"entry": "a", "nodes": [{"id": "a"}], "edges": []}}
    result = ToolExecutionResult(success=True, result=payload)

    assert _result_graph(result) == {"graph": payload["graph"]}


def test_result_graph_ignores_malformed_payloads() -> None:
    """宁可少一张图，也不能因为格式不对整条消息渲染不出来。"""
    from app.agents.nodes import _result_graph
    from app.tools.types import ToolExecutionResult

    assert _result_graph(None) == {}
    assert _result_graph(ToolExecutionResult(success=True, result={})) == {}
    assert _result_graph(ToolExecutionResult(success=True, result={"graph": "x"})) == {}
    assert _result_graph(ToolExecutionResult(success=True, result={"graph": {"nodes": []}})) == {}


def test_graph_never_leaks_into_the_answer_text() -> None:
    """图是给界面画的，混进正文会变成一整段 JSON，把结论埋掉。"""
    from app.agents.nodes import _summarize_result
    from app.tools.types import ToolExecutionResult

    result = ToolExecutionResult(
        success=True,
        result={
            "graph": {"entry": "a", "nodes": [{"id": "a"}], "edges": []},
            "note": "已保存",
        },
        message="已新建流程「巡检」。",
    )

    assert "graph" not in _summarize_result(result)
    assert "已新建流程" in _summarize_result(result)


def test_draft_result_does_not_dump_ok_flag() -> None:
    """起草的 `message` 已经说了「这张图能存 / 差在哪」。

    再铺一行「ok：True」和「writable_nodes：…」就是把报文原样贴给用户——
    读者会以为那是他需要核对的东西，其实正文里已经用人话说过了。
    """
    result = _result(
        message="这张图能存。\n步骤：check（field-ops） → report（汇总）",
        result={
            "ok": True,
            "errors": [],
            "writable_nodes": ["check"],
            "graph": {"nodes": [{"id": "check"}]},
        },
    )

    summary = _summarize_result(result)
    assert "ok：True" not in summary
    assert "writable_nodes" not in summary
    # 正文与图都在，只是不再重复字段。
    assert "这张图能存" in summary


def test_result_stages_are_taken_from_the_tool_result() -> None:
    """入库那条流水线：六个步骤随结果出去，界面画成节点卡。

    它替代的是正文里那行「加工链路：解析 → 清洗 → …」。替代品少一环，
    用户就再也看不到那一项——而他不知道少了什么。
    """
    from app.agents.nodes import _result_stages

    payload = {
        "stages": [
            {"key": "parse", "label": "解析 md", "value": "23,727 字", "status": "done"},
            {"key": "clean", "label": "清洗 17 张表", "value": "61,047 → 23,727 字",
             "note": "差额是标签与样式，正文未删减", "status": "done"},
        ]
    }
    result = ToolExecutionResult(success=True, result=payload)

    stages = _result_stages(result)["stages"]
    assert [stage["key"] for stage in stages] == ["parse", "clean"]
    # 缺的字段补空串，不让前端去判 undefined。
    assert stages[0]["note"] == ""
    assert stages[1]["note"] == "差额是标签与样式，正文未删减"


def test_result_stages_come_from_the_nested_import_payload() -> None:
    """`studio_import` 的结果是导入器那层 payload：界面不该知道它套了几层。"""
    from app.agents.nodes import _result_stages

    result = ToolExecutionResult(
        success=True,
        result={"result": {"stages": [{"key": "parse", "label": "解析", "status": "done"}]}},
    )

    assert _result_stages(result)["stages"][0]["label"] == "解析"


def test_result_stages_ignore_malformed_payloads() -> None:
    """宁可少一张卡，也不要半张卡：坏一条就当没有。"""
    from app.agents.nodes import _result_stages

    assert _result_stages(None) == {}
    assert _result_stages(ToolExecutionResult(success=True, result={})) == {}
    assert _result_stages(ToolExecutionResult(success=True, result={"stages": "x"})) == {}
    assert _result_stages(ToolExecutionResult(success=True, result={"stages": []})) == {}
    # 整条不可用的条目被跳过；一条都不剩时连卡也不出。
    assert _result_stages(
        ToolExecutionResult(success=True, result={"stages": [{"status": "done"}, None]})
    ) == {}
    # 失败的结果不画「全部完成」的卡。
    assert _result_stages(
        ToolExecutionResult(
            success=False, result={"stages": [{"key": "parse", "label": "解析"}]}
        )
    ) == {}


def test_stages_never_leak_into_the_answer_text() -> None:
    """步骤是给界面画的。铺进正文就是一段
    「stages：[{key: parse, label: 解析 …}]」，同一件事说成了报文。"""
    result = _result(
        result={
            "stages": [
                {"key": "parse", "label": "解析 md", "value": "23,727 字", "status": "done"}
            ]
        },
        message="已导入知识库《标准》：58 片 · 分级 internal。",
    )

    text = _summarize_result(result)
    assert "已导入知识库" in text
    assert "stages" not in text
    assert "23,727 字" not in text


def test_workflow_draft_never_leaks_into_the_answer_text() -> None:
    """草稿图是下一轮的底图，不是这一轮的结论。

    铺进正文会变成整段 definition JSON，把「这张图能存 / 差在哪」那句埋掉，
    而用户要读的正是那句。
    """
    result = _result(
        result={
            "workflow_draft": {
                "name": "triage",
                "definition": {"entry": "check", "nodes": [{"id": "check", "next": []}]},
            }
        },
        message="这张图能存。图还没落库。",
    )

    text = _summarize_result(result)
    assert "这张图能存" in text
    assert "workflow_draft" not in text
    assert "check" not in text


def test_result_workflow_draft_keeps_only_name_and_definition() -> None:
    """起草出来的图随消息存下来，但只留两个键。

    多留字段的代价是历史消息越来越胖，而下一轮改图只需要底图本身：
    `writable_nodes`、`errors` 那些是这一轮的结论，不是下一轮的输入。
    """
    from app.agents.nodes import _result_workflow_draft

    result = ToolExecutionResult(
        success=True,
        result={
            "ok": True,
            "writable_nodes": ["b"],
            "workflow_draft": {
                "name": "triage",
                "definition": {"entry": "a", "nodes": [{"id": "a", "next": []}]},
            },
        },
    )

    draft = _result_workflow_draft(result)["workflow_draft"]
    assert draft["name"] == "triage"
    assert list(draft) == ["name", "definition"]
    assert draft["definition"]["nodes"][0]["id"] == "a"


def test_result_workflow_draft_is_dropped_when_the_graph_is_unusable() -> None:
    """没有节点列表的「草稿」不能进消息元数据：下一轮 patch 会拿它当底图。"""
    from app.agents.nodes import _result_workflow_draft

    assert _result_workflow_draft(None) == {}
    assert _result_workflow_draft(ToolExecutionResult(success=True, result={})) == {}
    assert (
        _result_workflow_draft(
            ToolExecutionResult(success=True, result={"workflow_draft": {"definition": {}}})
        )
        == {}
    )
    assert (
        _result_workflow_draft(
            ToolExecutionResult(
                success=False,
                result={"workflow_draft": {"definition": {"nodes": [{"id": "a"}]}}},
            )
        )
        == {}
    )
