# tests/unit/test_skills.py
"""技能的纯函数层：匹配、收窄、注入文本。

这一层不需要数据库，因为它是「技能到底会不会越权」的唯一判断处，
用最轻的方式把它钉死，比走一遍接口再断言更可靠。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import skill_service as svc


def _skill(**kwargs):
    base = {
        "name": "patrol",
        "display_name": "巡检比对",
        "instruction": "先识别附件，再按站点查工单。",
        "trigger_keywords": ["巡检", "比对"],
        "allowed_tools": None,
        "priority": 100,
        "enabled": True,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_normalize_name_accepts_spaces_and_uppercase():
    assert svc.normalize_name("  Station Control Execute ") == "station-control-execute"


@pytest.mark.parametrize("raw", ["1abc", "巡检", "a", "a b!", "-abc"])
def test_normalize_name_rejects_bad_values(raw):
    with pytest.raises(svc.SkillError):
        svc.normalize_name(raw)


def test_keywords_are_deduped_and_trimmed():
    assert svc.normalize_keywords([" 巡检 ", "巡检", "比对"]) == ["巡检", "比对"]


def test_keyword_limit():
    with pytest.raises(svc.SkillError):
        svc.normalize_keywords([f"k{i}" for i in range(svc.MAX_KEYWORDS + 1)])


def test_tools_none_and_empty_are_different():
    """None = 不收窄；[] = 一个工具都不给。混掉会把纯指令技能变成全开。"""
    assert svc.normalize_tools(None) is None
    assert svc.normalize_tools([]) == []


def test_clip_instruction_marks_truncation():
    text, clipped = svc.clip_instruction("x" * (svc.MAX_INSTRUCTION_CHARS + 10))
    assert clipped is True
    assert len(text) == svc.MAX_INSTRUCTION_CHARS

    _, clipped = svc.clip_instruction("短指令")
    assert clipped is False


def test_explicit_name_wins_over_keyword():
    a = _skill(name="patrol", priority=1)
    b = _skill(name="report", trigger_keywords=["报告"], priority=999)
    picked = svc.match_skill("/patrol 帮我看看", [a, b])
    assert picked is not None
    skill, matched_by, keyword = picked
    assert skill.name == "patrol"
    assert matched_by == "explicit"
    assert keyword == ""


def test_explicit_requires_boundary():
    """`/patrol下这段` 不是显式调用，不能把后面的字当参数吞掉。"""
    a = _skill(name="patrol", trigger_keywords=[])
    assert svc.match_skill("/patrol下这段", [a]) is None


def test_priority_then_longer_keyword():
    low = _skill(name="low", trigger_keywords=["巡检"], priority=10)
    high = _skill(name="high", trigger_keywords=["巡检"], priority=90)
    assert svc.match_skill("今天巡检一下", [low, high])[0].name == "high"

    short = _skill(name="short", trigger_keywords=["报告"], priority=50)
    long = _skill(name="long", trigger_keywords=["巡检报告"], priority=50)
    picked = svc.match_skill("出一份巡检报告", [short, long])
    assert picked[0].name == "long"
    assert picked[2] == "巡检报告"


def test_unknown_explicit_name_falls_back_to_keyword():
    a = _skill(name="patrol", trigger_keywords=["巡检"])
    picked = svc.match_skill("/nope 帮我巡检", [a])
    assert picked is not None
    assert picked[1] == "keyword"


def test_narrow_catalog_is_intersection_only():
    catalog = [{"tool_name": "a"}, {"tool_name": "b"}, {"tool_name": "c"}]
    # 白名单里写了 d（当前身份不可见），结果里不能凭空出现。
    skill = _skill(allowed_tools=["b", "d"])
    assert [x["tool_name"] for x in svc.narrow_catalog(catalog, skill)] == ["b"]


def test_narrow_catalog_all_and_none():
    catalog = [{"tool_name": "a"}]
    assert len(svc.narrow_catalog(catalog, _skill(allowed_tools=None))) == 1
    assert svc.narrow_catalog(catalog, _skill(allowed_tools=[])) == []


def test_missing_tools_lists_invisible_names():
    catalog = [{"tool_name": "a"}]
    skill = _skill(allowed_tools=["a", "zzz"])
    assert svc.missing_tools(skill, catalog) == ["zzz"]
    assert svc.missing_tools(_skill(allowed_tools=None), catalog) == []


def test_render_block_states_tool_limit():
    skill = _skill(allowed_tools=["ocr_document"])
    text = svc.render_block(skill, tools=["ocr_document"])
    assert "## 当前技能：巡检比对" in text
    assert "只能调用 tools 列表中出现的工具" in text
    assert "可用工具已被限定为：ocr_document。" in text


def test_render_block_omits_limit_when_not_narrowed():
    text = svc.render_block(_skill(allowed_tools=None), tools=["a", "b"])
    assert "可用工具已被限定" not in text


def test_render_block_marks_truncation():
    skill = _skill(instruction="x" * (svc.MAX_INSTRUCTION_CHARS + 5))
    text = svc.render_block(skill)
    assert "（技能指令超过长度上限，已截断。）" in text
