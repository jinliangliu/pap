# tests/unit/test_knowledge_compose.py
"""知识库答复归纳的单元测试。

全部通过替换 ``compose._ask_model`` 来模拟模型输出，不发真实网络请求。

这里盯的是**边界**，不是措辞：归纳可以用模型的表述，但不能
补资料之外的事实、不能丢掉出处、不能在模型抽风时把答复变成空串。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.knowledge import compose as compose_module
from app.knowledge.compose import _build_prompt, _clean_output, compose_answer


def _record(index: int, text: str, *, section: str = "3.1 术语和定义") -> dict:
    return {
        "chunk_id": f"c{index}-0000",
        "document_id": f"d{index}",
        "chunk_index": 0,
        "text": text,
        "title": f"文档{index}",
        "source_path": f"notes/d{index}.md",
        "section": section,
        "updated_at": "2026-11-01T00:00:00+00:00",
        "content_hash": f"sha256:{index}",
        "access_scope": "internal",
    }


def _mock(monkeypatch, output):
    """把模型调用替换成给定输出或异常。"""

    async def fake(prompt: str, *, timeout: float) -> str:
        if isinstance(output, BaseException):
            raise output
        return output

    monkeypatch.setattr(compose_module, "_ask_model", fake)


def _compose(query: str, documents: list[dict]):
    return asyncio.run(compose_answer(query=query, documents=documents))


def test_prompt_covers_whole_chunk_and_numbers_sources():
    """整片必须都进 prompt，且每段带编号。

    截断点落在答案前面，归纳出来就是一句「资料未提及」——比原文摘录更糟，
    因为它看起来像结论。与重筛同一条耦合，见 docs/KNOWLEDGE.md。
    """
    from app.knowledge.ingest import CHUNK_SIZE

    assert compose_module.MAX_SOURCE_CHARS >= CHUNK_SIZE

    tail = "切断阀在压力达到设定值时自动完全切断燃气"
    body = "填" * (CHUNK_SIZE - len(tail)) + tail
    prompt = _build_prompt("切断阀有何作用", [_record(1, body), _record(2, "外力式")])

    assert tail in prompt
    assert "[1]" in prompt and "[2]" in prompt
    assert "用户问题：切断阀有何作用" in prompt


def test_prompt_drops_markdown_heading_marks():
    """片段里的 ``##### 3.1.2.2`` 是切片噪声，不进 prompt。

    它进过一次气泡：整个答复在「##### 功能 A 型」后面接着原文。
    """
    prompt = _build_prompt("功能分级", [_record(1, "##### 3.1.2.2\n功能 A 型当元件损坏时关闭")])

    assert "#####" not in prompt
    # 标题文字本身要留着，它对理解条款层级有用。
    assert "3.1.2.2" in prompt
    assert "功能 A 型当元件损坏时关闭" in prompt


def test_summary_keeps_citation_and_appends_footer(monkeypatch):
    _mock(
        monkeypatch,
        "切断阀的作用是在系统事故或故障时自动、完全切断燃气，故障排除后需人工复位。\n\n"
        "1. 常态保持开启 [1]\n"
        "2. 过压或欠压达到设定值时动作 [1][2]",
    )

    result = _compose("切断阀有何作用", [_record(1, "原文一"), _record(2, "原文二")])

    assert result.meta.applied is True
    assert result.meta.cited == 2
    assert "[1]" in result.text
    # 归纳路径的收尾话由代码写死，不让模型决定写不写。
    assert "以上为基于上述 2 处资料的归纳" in result.text
    # 旧模板句不能回来：它是「直出」的标志。
    assert "资料明确说明" not in result.text
    assert "摘自知识库原文" not in result.text


def test_falls_back_when_no_citation(monkeypatch):
    """一个编号都没标 = 这段答复在库里找不到出处。

    它可能读起来很顺，但那正是「用常识补齐」的样子。
    """
    _mock(monkeypatch, "切断阀是一种安全保护装置，广泛用于燃气输配系统。")

    result = _compose("切断阀有何作用", [_record(1, "原文一")])

    assert result.meta.applied is False
    assert result.meta.fallback == "no_citation"
    assert result.text == ""


def test_falls_back_when_citation_is_out_of_range(monkeypatch):
    """引一个不存在的 [9] 不算有引用：那是编造出处，不能放行。"""
    _mock(monkeypatch, "切断阀用于在超压时切断气源，这一点在各标准中都有规定 [9]。")

    result = _compose("切断阀有何作用", [_record(1, "原文一"), _record(2, "原文二")])

    assert result.meta.applied is False
    assert result.meta.fallback == "no_citation"
    assert result.meta.cited == 0


def test_falls_back_when_output_too_short(monkeypatch):
    """一句话的答复换掉一整段有依据的摘录属于降级。"""
    _mock(monkeypatch, "资料不足 [1]")

    result = _compose("切断阀有何作用", [_record(1, "原文一")])

    assert result.meta.applied is False
    assert result.meta.fallback == "empty_output"


def test_falls_back_on_timeout(monkeypatch):
    _mock(monkeypatch, TimeoutError())

    result = _compose("切断阀有何作用", [_record(1, "原文一")])

    assert result.meta.applied is False
    assert result.meta.fallback == "timeout"
    assert result.text == ""


def test_falls_back_on_model_error(monkeypatch):
    _mock(monkeypatch, RuntimeError("model_unavailable"))

    result = _compose("切断阀有何作用", [_record(1, "原文一")])

    assert result.meta.applied is False
    # 未配模型属于正常降级，原因要原样带出来，别写成 model_unavailable 的泛化词。
    assert result.meta.fallback == "model_unavailable"


def test_disabled_never_calls_model(monkeypatch):
    async def must_not_run(prompt: str, *, timeout: float) -> str:
        raise AssertionError("关闭归纳后不应该调用模型")

    monkeypatch.setattr(compose_module, "_ask_model", must_not_run)
    monkeypatch.setattr(
        compose_module,
        "settings",
        SimpleNamespace(
            knowledge_summary_enabled=False,
            knowledge_summary_timeout_seconds=20.0,
        ),
    )

    result = _compose("切断阀有何作用", [_record(1, "原文一")])

    assert result.meta.applied is False
    assert result.meta.fallback == "disabled"


def test_no_documents_skips_model(monkeypatch):
    async def must_not_run(prompt: str, *, timeout: float) -> str:
        raise AssertionError("没有片段时不应该调用模型")

    monkeypatch.setattr(compose_module, "_ask_model", must_not_run)

    result = _compose("切断阀有何作用", [])

    assert result.meta.applied is False
    assert result.meta.fallback == "no_sources"


def test_clean_output_strips_fence_and_reads_mixed_citation_forms():
    text, cited = _clean_output("```markdown\n结论 [1][3] 与 [2,4]，还有 [1，4]\n```", 4)

    assert not text.startswith("```")
    assert cited == {1, 2, 3, 4}
    # 越界编号不参与判定
    assert _clean_output("依据 [5] 与 [1]", 4)[1] == {1}


@pytest.mark.asyncio
async def test_node_uses_summary_and_records_mode(monkeypatch):
    """对话链路上用的是归纳后的正文，且留下「这条是归纳还是摘录」。"""
    from app.agents import nodes

    documents = [_record(1, "原文一"), _record(2, "原文二")]

    async def fake_answer(**kwargs):
        return SimpleNamespace(
            answer="资料明确说明（共 2 处匹配）：\n\n[1] 原文一\n\n[2] 原文二",
            citations=[{"title": "文档1"}, {"title": "文档2"}],
            documents=documents,
            found=True,
        )

    async def fake_compose(**kwargs):
        return compose_module.ComposeResult(
            text="切断阀在超压时自动切断燃气 [1][2]\n\n以上为基于上述 2 处资料的归纳。",
            meta=compose_module.ComposeMeta(applied=True, sources=2, cited=2),
        )

    monkeypatch.setattr(nodes, "answer_from_knowledge", fake_answer)
    monkeypatch.setattr(nodes, "compose_answer", fake_compose)

    updates = await nodes.retrieve_knowledge(
        {
            "planned_arguments": {"query": "切断阀有何作用"},
            "scopes": ["knowledge.read"],
            "tenant_id": "default",
            "roles": ["dev"],
            "user_id": "u-1",
        }
    )

    assert updates["final_response"].startswith("切断阀在超压时自动切断燃气")
    assert "资料明确说明" not in updates["final_response"]
    assert updates["response_metadata"]["knowledge_answer_mode"] == "summary"
    assert "knowledge_answer_fallback" not in updates["response_metadata"]
    assert updates["error_category"] is None


@pytest.mark.asyncio
async def test_node_falls_back_to_excerpt_when_summary_fails(monkeypatch):
    """归纳失败必须回到原文摘录，不能变成空答复。"""
    from app.agents import nodes

    excerpt = "资料明确说明（共 1 处匹配）：\n\n[1] 原文一\n\n以上内容摘自知识库原文，未做补充推断。"

    async def fake_answer(**kwargs):
        return SimpleNamespace(
            answer=excerpt,
            citations=[{"title": "文档1"}],
            documents=[_record(1, "原文一")],
            found=True,
        )

    async def fake_compose(**kwargs):
        return compose_module.ComposeResult(
            text="",
            meta=compose_module.ComposeMeta(applied=False, fallback="timeout", sources=1),
        )

    monkeypatch.setattr(nodes, "answer_from_knowledge", fake_answer)
    monkeypatch.setattr(nodes, "compose_answer", fake_compose)

    updates = await nodes.retrieve_knowledge(
        {
            "planned_arguments": {"query": "切断阀有何作用"},
            "scopes": ["knowledge.read"],
            "tenant_id": "default",
            "roles": [],
            "user_id": "",
        }
    )

    assert updates["final_response"] == excerpt
    assert updates["response_metadata"]["knowledge_answer_mode"] == "excerpt"
    assert updates["response_metadata"]["knowledge_answer_fallback"] == "timeout"
