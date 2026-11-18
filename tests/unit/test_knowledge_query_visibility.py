# tests/unit/test_knowledge_query_visibility.py
"""「实际检索词」必须可见，且规划器不许自行扩写。

事故背景：用户问「本安设备有些要求？」，对话里命中 GB/T 3836.4 的 §1 范围，
知识库「检索验证」页命中 §G.3.1 通则。两条路走的是同一个
`answer_from_knowledge`、同一份索引、同一组权限，差别只在 query——
规划器把它改写成了「本安设备（本质安全型设备）要求 防爆电气设备 通用要求」，
多出来的限定词把「本文件规定了…要求」的章节摘要顶到了第一位。

改写本身可以接受（去掉口语词、把代词换成上文对象），但：
1. 不许添加用户没说的术语，否则等于在 `expand_query` 之外又扩写一遍；
2. 改写结果必须能被用户看到，否则「同一个问题两次不一样」无从自查。
"""

from __future__ import annotations

import pytest

from app.agents.prompts import SYSTEM_PROMPT


def test_prompt_forbids_adding_terms_the_user_did_not_say() -> None:
    # 规则第 6 条是 knowledge 的唯一入口说明，约束写在这里才生效。
    assert "query 必须是用户这一句的原话" in SYSTEM_PROMPT
    assert "不得" in SYSTEM_PROMPT
    assert "同义词扩展" in SYSTEM_PROMPT


def test_prompt_names_the_failure_it_prevents() -> None:
    # 只写「不要扩写」模型会照做一半；把后果写清楚，它才知道边界在哪。
    assert "顶到第一位" in SYSTEM_PROMPT or "被挤掉" in SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_retrieve_knowledge_reports_the_query_it_used(monkeypatch):
    from app.agents import nodes

    class _Answer:
        answer = "资料明确说明（共 0 处匹配）："
        citations: list = []
        documents: list = []
        found = False

    seen: dict[str, str] = {}

    async def fake_answer(**kwargs):
        seen.update(kwargs)
        return _Answer()

    monkeypatch.setattr(nodes, "answer_from_knowledge", fake_answer)

    updates = await nodes.retrieve_knowledge(
        {
            "planned_arguments": {"query": "本安设备有些要求？"},
            "scopes": ["knowledge.read"],
            "tenant_id": "default",
            "roles": ["dev"],
            "user_id": "u-1",
        }
    )

    # 拿到的就是送进检索的那一句，不是用户原话的副本——
    # 两者在规划器改写后本来就不一样，这里必须记实际值。
    assert seen["query"] == "本安设备有些要求？"
    assert updates["response_metadata"]["knowledge_query"] == "本安设备有些要求？"


@pytest.mark.asyncio
async def test_empty_query_is_not_written_into_metadata(monkeypatch):
    """没有 query 时不要写空串：前端会据此渲染一行空的「实际检索词」。"""
    from app.agents import nodes

    class _Answer:
        answer = ""
        citations: list = []
        documents: list = []
        found = False

    async def fake_answer(**kwargs):
        return _Answer()

    monkeypatch.setattr(nodes, "answer_from_knowledge", fake_answer)

    updates = await nodes.retrieve_knowledge(
        {
            "planned_arguments": {},
            "scopes": ["knowledge.read"],
            "tenant_id": "default",
            "roles": [],
            "user_id": "",
        }
    )

    assert "knowledge_query" not in updates["response_metadata"]
